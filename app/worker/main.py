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
from teamai.container import Container, build_container, build_projector, open_job_scope
from teamai.domain.models import TaskStatus
from teamai.domain.ports import QueuePayload, ReplyTarget
from teamai.infrastructure.db import init_db_or_warn
from teamai.infrastructure.metrics import mark_process_exit
from teamai.infrastructure.scheduler import scheduler

logger = logging.getLogger(__name__)

# 出队的阻塞等待上限。队列空时 BLPOP 挂住直到有任务或超时，故这不是轮询
# 间隔，而是「最坏情况下多久醒来一次去查停止信号」：调大只影响收到 SIGTERM
# 后的退出延迟，不影响取任务的及时性 —— 一有任务即刻返回。
# 审批超时处置的 actor。取固定串而非某个真实用户：审计里要能一眼区分
# 「人拒绝的」与「等太久系统判拒的」。
_APPROVAL_ACTOR = "system:approval-timeout"

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

    经 open_job_scope 每次开一个新 session。容器那个不能用：build_container 只
    调了一次 session 工厂，全部仓储共用同一个实例，而两个 job 同间隔触发时会
    并发碰它（详见 open_job_scope 的说明）。

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
    """任务超时巡检。独立 session 与异常兜底的理由同上。

    卡住的 RUNNING 有两种出路：有执行检查点的重新入队续跑（已完成的工具不重跑），
    没有或已超续跑上限的收敛到 FAILED。故三类结局分别记日志 —— 续跑是正常自愈，
    用 info；判死要人看，用 warning。

    失败单独记 error：巡检对每个任务分别兜异常，若只看 swept 就会把「找到 5 个
    全都推进失败」当成「一个都没卡住」。
    """
    try:
        async with open_job_scope(container) as scope:
            report = await scope.orchestrator.sweep_stale_tasks(
                pending_timeout=timedelta(minutes=settings.jobs_pending_timeout_minutes),
                running_timeout=timedelta(minutes=settings.jobs_running_timeout_minutes),
                max_resume_attempts=settings.jobs_max_resume_attempts,
            )
        if report.resumed:
            logger.info(
                f"超时巡检: {len(report.resumed)} 个任务从检查点续跑: "
                f"{[t.id for t in report.resumed]}"
            )
        if report.swept:
            logger.warning(
                f"超时巡检: {len(report.swept)} 个任务判为 FAILED: {[t.id for t in report.swept]}"
            )
        if report.failed:
            logger.error(f"超时巡检有 {len(report.failed)} 个任务未能推进: {report.failed}")
    except Exception as exc:
        logger.error(f"超时巡检失败: {exc}")


async def sweep_stale_approvals(container: Container) -> None:
    """审批超时巡检：等太久的待批按拒绝处理，让模型收尾说明。

    与 sweep_stale_tasks 分开：那个扫的是 PENDING/RUNNING（worker 挂了），
    这个扫 WAITING_INPUT（人没回）。阈值差一个数量级、处置也不同。

    转拒绝而非取消任务：模型能说明「因为没等到审批，PR 没有创建」，用户看到的
    是解释而非任务凭空消失。
    """
    try:
        timeout = timedelta(minutes=settings.jobs_approval_timeout_minutes)
        async with open_job_scope(container) as scope:
            stale = await scope.orchestrator.sweep_stale_approvals(timeout)
            for task in stale:
                instance = await container.channels.get(task.channel_instance_id)
                if instance is None:
                    logger.warning(f"审批超时任务的频道不存在，跳过: {task.id}")
                    continue
                outcome = await scope.approvals.timeout(task)
                if outcome.pending is None:
                    continue
                decisions = scope.approvals.decisions_for_resume(
                    outcome.pending, approved=False, reason="审批超时，未获批准"
                )
                await scope.approvals.clear(task.id)
                await scope.orchestrator.transition(task, TaskStatus.RUNNING, actor=_APPROVAL_ACTOR)
                # 经 router 恢复：状态推进与回帖文案与人工审批那条路一致
                decision = await container.router.execute_task(
                    task,
                    task.intent,
                    task.tag_name,
                    instance,
                    actor=_APPROVAL_ACTOR,
                    approval_results=decisions,
                )
                if decision.message:
                    await container.publisher.reply(
                        ReplyTarget(
                            platform=instance.platform,
                            channel_id=instance.channel_id,
                            thread_ref=task.thread_ref,
                        ),
                        decision.message,
                    )
        if stale:
            logger.warning(f"审批超时: {len(stale)} 个任务按拒绝处理: {[t.id for t in stale]}")
    except Exception as exc:
        logger.error(f"审批超时巡检失败: {exc}")


async def distill_memories(container: Container) -> None:
    """记忆蒸馏：把到期的对话窗口提炼成记忆。独立 session 与异常兜底的理由同上。

    没有这一步，router 攒进滚动缓冲的非 @ 消息永远不会落地成记忆 ——
    窗口只增不减，「频道记忆」这个能力等于没有。

    失败单独记 error 而非只看产出条数：全部频道蒸馏失败与「没有到期窗口」
    在日志里长得一样，前者是故障。
    """
    try:
        async with open_job_scope(container) as scope:
            report = await scope.distiller.sweep()
        if report.total_entries:
            logger.info(
                f"记忆蒸馏: {len(report.distilled)} 个频道产出 {report.total_entries} 条记忆"
            )
        if report.skipped_budget:
            logger.info(f"记忆蒸馏: {len(report.skipped_budget)} 个频道配额不足已跳过")
        if report.failed:
            logger.error(f"记忆蒸馏有 {len(report.failed)} 个频道失败: {report.failed}")
    except Exception as exc:
        logger.error(f"记忆蒸馏失败: {exc}")


async def reconcile_memory_vectors(container: Container) -> None:
    """记忆向量对账：把「向量状态与记忆状态不符」的行重新入队。

    这是投影链路的安全网。outbox 保证「我发出的意图最终会执行」，不保证「执行
    结果后来没被别人改掉」—— 向量库被重建、误删、恢复到旧时点，以及本方案上线前
    的存量偏差，都只有对账能发现（见 application/reconciler.py 的模块说明）。

    ⚠️ **长期补出 0 条才是正常。** 持续非零说明 projector 在漏活，而不是对账在
    干活。所以这里补出非零时记 warning 而非 info —— 它该引起注意。
    """
    try:
        async with open_job_scope(container) as scope:
            report = await scope.reconciler.run_once()
        if report.total:
            logger.warning(
                f"记忆向量对账补了 {report.total} 条"
                f"（缺失/过期 {len(report.missing)}、残留 {len(report.stale)}）—— "
                f"长期非零说明投影在漏活，值得排查"
            )
    except Exception as exc:
        logger.error(f"记忆向量对账失败: {exc}")


async def purge_expired_interactions(container: Container) -> None:
    """交互记录保留期清理。

    这张表存提示词与响应全文，不清理会无限增长 —— 既是存储负担，更是合规
    负担：保留期是对外承诺的一部分，不执行等于没承诺。
    """
    try:
        async with open_job_scope(container) as scope:
            deleted = await scope.interactions.purge_expired()
        if deleted:
            logger.info(f"交互记录清理: 删除 {deleted} 条超出保留期的记录")
    except Exception as exc:
        logger.error(f"交互记录清理失败: {exc}")


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
    scheduler.add_interval(
        "approval-timeout-sweep",
        minutes=interval,
        coro=functools.partial(sweep_stale_approvals, container),
    )
    scheduler.add_interval(
        "memory-distill",
        minutes=interval,
        coro=functools.partial(distill_memories, container),
    )
    scheduler.add_interval(
        "memory-vector-reconcile",
        minutes=interval,
        coro=functools.partial(reconcile_memory_vectors, container),
    )
    purge_interval = settings.jobs_purge_interval_minutes
    scheduler.add_interval(
        "interaction-purge",
        minutes=purge_interval,
        coro=functools.partial(purge_expired_interactions, container),
    )
    logger.info(
        f"已注册定时任务: 预算周期重置 / 超时巡检 / 审批超时 / 记忆蒸馏 / 向量对账，每 {interval} 分钟；"
        f"交互记录清理，每 {purge_interval} 分钟"
    )


async def _main() -> None:
    await init_db_or_warn()
    container = build_container()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 不支持
            loop.add_signal_handler(sig, stop.set)

    # MCP server 装载：改配置后重启 worker 即生效（重启生效的决策见 SPEC）。
    # 单个 server 连接失败不阻塞启动，last_error 落库、前端可见。
    await container.mcp.load_and_register()

    register_jobs(container)
    scheduler.start()
    logger.info("Scheduler 已启动")

    # 记忆向量投影：常驻 task，不是定时任务。它是链路的一部分而非周期性维护 ——
    # 记忆写入只入队，向量要靠它补上，间隔越长「刚写入的记忆搜不到」的窗口越大
    # （见 docs/plan-memory-outbox.md §5.4）。
    projector_task = asyncio.create_task(build_projector(container).run_forever(stop))

    try:
        await run_worker(container, stop)
    finally:
        # 先等投影器停：它可能正在一次 embed 往返里，给它机会收尾。超时就放弃 ——
        # 租约会过期，那批记录下次启动时自动回到可取状态，不会丢。
        try:
            await asyncio.wait_for(projector_task, timeout=10)
        except TimeoutError:
            logger.warning("记忆投影器未在 10 秒内停止，放弃等待（租约会自动回收）")
            projector_task.cancel()
        except Exception as exc:  # pragma: no cover - 退出路径尽力而为
            logger.warning(f"记忆投影器退出异常: {exc}")

        scheduler.shutdown()
        logger.info("Scheduler 已关闭")
        # 共享 Redis 连接池是长期存活的，须显式关闭
        try:
            await container.aclose()
        except Exception as exc:  # pragma: no cover - 退出路径尽力而为
            logger.warning(f"释放容器资源异常: {exc}")

        # 清掉本进程的 Gauge 样本文件。不清的话重启后旧 pid 的样本会留在目录里，
        # 而 'liveall' 模式的 Gauge 会继续暴露它们 —— 表现是「投影明明停了 lag
        # 却不涨」，比没有指标更误导。
        mark_process_exit()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
