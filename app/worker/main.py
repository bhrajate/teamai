"""Worker 进程入口：消费长任务队列 + 运行定时调度。

为什么与 web 进程分开：设计文档 §6 把长任务定为「小时/天级」，与 Slack 事件的
毫秒级响应放同一进程会互相拖累 —— 一个长任务卡住事件循环就会让 Slack 事件超时
重投，而 web 进程按 QPS 扩容也会把 worker 副本一并放大、导致同一任务被多次执行。
两者的扩缩容维度与崩溃影响面不同，所以按进程切开。

用法：
    python -m app.worker.main
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
from datetime import timedelta

from teamai.config import settings
from teamai.container import Container, build_container, open_job_scope
from teamai.domain.models import TaskStatus
from teamai.domain.ports import QueuePayload, ReplyTarget
from teamai.infrastructure.db import init_db_or_warn
from teamai.infrastructure.scheduler import scheduler

logger = logging.getLogger(__name__)

# 出队的阻塞等待上限。队列空时 BLPOP 挂住直到有任务或超时，故这不是轮询
# 间隔，而是「最坏情况下多久醒来一次去查停止信号」：调大只影响收到 SIGTERM
# 后的退出延迟，不影响取任务的及时性 —— 一有任务即刻返回。
DEQUEUE_BLOCK_SECONDS = 5.0

# 队列不可用时的重试间隔。这条路径上 dequeue 立即抛错、不会阻塞，
# 故仍需显式 sleep，否则退化成忙循环。
ERROR_RETRY_SECONDS = 1.0


async def handle_payload(container: Container, payload: QueuePayload) -> None:
    """执行一个出队任务。

    异常不外抛：单个任务失败不应终止整个 worker 循环，故在此兜底并落审计。
    """
    task = await container.task_repo.get(payload.task_id)
    if task is None:
        logger.warning(f"任务不存在，丢弃载荷: {payload.task_id}")
        return

    if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        logger.info(f"任务已终态 {task.status.value}，跳过: {task.id}")
        return

    instance = await container.channels.get(payload.channel_instance_id)
    if instance is None:
        logger.warning(f"频道实例不存在，丢弃载荷: {payload.channel_instance_id}")
        return

    actor = task.requester_id
    try:
        if task.status is TaskStatus.PENDING:
            await container.orchestrator.transition(task, TaskStatus.RUNNING, actor)

        decision = await container.router.execute_task(
            task,
            payload.prompt or task.intent,
            payload.tag_name or task.tag_name,
            instance,
            actor=actor,
        )
        logger.info(f"任务完成 {task.id}: {task.status.value}")
        if decision.message:
            # 回帖经 MessagePublisher 端口，按 instance.platform 分发到对应平台。
            # worker 不再需要具体平台依赖 —— ReplyTarget 由 instance + task 拼出。
            target = ReplyTarget(
                platform=instance.platform,
                channel_id=instance.channel_id,
                thread_ref=task.thread_ref,
            )
            try:
                await container.publisher.reply(target, decision.message)
            except ConnectionError as exc:
                logger.warning(f"任务 {task.id} 回复发送失败: {exc}")
        logger.debug(f"任务 {task.id} 输出: {decision.message[:200]}")
    except Exception as exc:
        logger.error(f"任务执行异常 {task.id}: {exc}")
        with contextlib.suppress(Exception):
            await container.orchestrator.transition(task, TaskStatus.FAILED, actor)


async def run_worker(container: Container, stop: asyncio.Event) -> None:
    """消费循环，直到 stop 被置位。

    出队用阻塞取：任务一入队就立刻被拿到，空转时也不再每秒打一次 Redis。
    返回 None 只意味着「这段时间没活」，回到循环顶部重新检查 stop。

    刻意不把 stop 与 dequeue 放进 asyncio.wait 抢跑：那样能秒退，但 stop 先到
    时要 cancel 正在进行的 BLPOP —— 若 Redis 已弹出元素而客户端还没收到，这个
    任务就凭空丢了（既不在队列里，也没被执行）。宁可多等至多
    DEQUEUE_BLOCK_SECONDS 再退出，也不丢任务。
    """
    logger.info("Worker 已启动，开始消费任务队列")
    while not stop.is_set():
        try:
            payload = await container.queue.dequeue(DEQUEUE_BLOCK_SECONDS)
        except ConnectionError as exc:
            logger.warning(f"队列不可用，{ERROR_RETRY_SECONDS}s 后重试: {exc}")
            await _sleep_or_stop(stop, ERROR_RETRY_SECONDS)
            continue

        if payload is None:  # 阻塞等待超时，无任务
            continue

        await handle_payload(container, payload)

    logger.info("Worker 已停止")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """可被 stop 打断的 sleep，保证收到信号后立刻退出而非等满一轮。"""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def reset_budget_periods(container: Container) -> None:
    """预算周期重置。

    经 open_job_scope 拿独立 session：定时任务与消费循环同在一个事件循环上，
    共用容器那个 session 会并发撞车（详见 open_job_scope 的说明）。

    异常不外抛：定时任务抛出去没人接，只会污染 scheduler 的日志。
    """
    try:
        async with open_job_scope(container) as scope:
            count = await scope.budget.reset_expired_periods()
        if count:
            logger.info(f"预算周期重置: {count} 条配额已翻页")
    except Exception as exc:
        logger.error(f"预算周期重置失败: {exc}")


async def sweep_stale_tasks(container: Container) -> None:
    """任务超时巡检。独立 session 与异常兜底的理由同上。"""
    try:
        async with open_job_scope(container) as scope:
            swept = await scope.orchestrator.sweep_stale_tasks(
                pending_timeout=timedelta(minutes=settings.jobs_pending_timeout_minutes),
                running_timeout=timedelta(minutes=settings.jobs_running_timeout_minutes),
            )
        if swept:
            logger.warning(f"超时巡检: {len(swept)} 个任务判为 FAILED: {[t.id for t in swept]}")
    except Exception as exc:
        logger.error(f"超时巡检失败: {exc}")


def register_jobs(container: Container) -> None:
    """注册定时任务。

    两个 job 共用一个间隔：都只扫一遍表，跑得更密没有收益。

    预算重置不可缺：没有它，任一频道耗尽配额后就永久 EXHAUSTED，
    BudgetQuota.period 形同虚设。
    超时巡检不可缺：worker 崩溃时在执行的任务会永久停在 RUNNING，
    既不重投也不失败，发起人等不到任何回复。
    """
    interval = settings.jobs_sweep_interval_minutes
    # ⚠️ 必须用 functools.partial 绑定 container，不能写成 lambda: job(container)。
    # APScheduler 用 iscoroutinefunction_partial 判断该 await 还是丢线程池：它会
    # 拆开 partial 看到里面是协程函数（True），但对 lambda 只看到普通函数
    # （False）—— 于是把 lambda 丢进线程池执行，拿到一个从未被 await 的协程
    # 对象就丢掉，job 体静默不执行，连报错都没有。
    scheduler.add_interval(
        "budget-period-reset",
        minutes=interval,
        coro=functools.partial(reset_budget_periods, container),
    )
    scheduler.add_interval(
        "stale-task-sweep",
        minutes=interval,
        coro=functools.partial(sweep_stale_tasks, container),
    )
    logger.info(f"已注册定时任务: 预算周期重置 / 超时巡检，每 {interval} 分钟")


async def _main() -> None:
    await init_db_or_warn()
    container = build_container()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 不支持
            loop.add_signal_handler(sig, stop.set)

    register_jobs(container)
    scheduler.start()
    logger.info("Scheduler 已启动")
    try:
        await run_worker(container, stop)
    finally:
        scheduler.shutdown()
        logger.info("Scheduler 已关闭")
        # 共享 Redis 连接池是长期存活的，须显式关闭
        try:
            await container.aclose()
        except Exception as exc:  # pragma: no cover - 退出路径尽力而为
            logger.warning(f"释放容器资源异常: {exc}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
