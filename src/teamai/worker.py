"""Worker 进程入口：消费长任务队列 + 运行定时调度。

为什么与 web 进程分开：设计文档 §6 把长任务定为「小时/天级」，与 Slack 事件的
毫秒级响应放同一进程会互相拖累 —— 一个长任务卡住事件循环就会让 Slack 事件超时
重投，而 web 进程按 QPS 扩容也会把 worker 副本一并放大、导致同一任务被多次执行。
两者的扩缩容维度与崩溃影响面不同，所以按进程切开。

用法：python -m teamai.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from teamai.app import init_database
from teamai.container import Container, build_container
from teamai.domain.models import TaskStatus
from teamai.domain.ports import QueuePayload

logger = logging.getLogger(__name__)

# 队列空转时的轮询间隔。RedisTaskQueue 用 LPOP 非阻塞取，
# 故这里靠 sleep 控制空转频率；换成 BLPOP 后可去掉。
IDLE_SLEEP_SECONDS = 1.0


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
        # TODO 回写 Slack 线程需要 bot client，当前 worker 无 Slack 依赖，
        # 结果先落审计与任务状态，回帖待补。
        logger.debug(f"任务 {task.id} 输出: {decision.message[:200]}")
    except Exception as exc:
        logger.error(f"任务执行异常 {task.id}: {exc}")
        with contextlib.suppress(Exception):
            await container.orchestrator.transition(task, TaskStatus.FAILED, actor)


async def run_worker(container: Container, stop: asyncio.Event) -> None:
    """消费循环，直到 stop 被置位。"""
    logger.info("Worker 已启动，开始消费任务队列")
    while not stop.is_set():
        try:
            payload = await container.queue.dequeue()
        except ConnectionError as exc:
            logger.warning(f"队列不可用，{IDLE_SLEEP_SECONDS}s 后重试: {exc}")
            await _sleep_or_stop(stop, IDLE_SLEEP_SECONDS)
            continue

        if payload is None:
            await _sleep_or_stop(stop, IDLE_SLEEP_SECONDS)
            continue

        await handle_payload(container, payload)

    logger.info("Worker 已停止")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """可被 stop 打断的 sleep，保证收到信号后立刻退出而非等满一轮。"""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def register_jobs(container: Container) -> None:
    """注册定时任务。

    目前无内置周期任务；预算周期重置、任务超时巡检等应在此登记。
    留作显式挂载点，避免 scheduler 又变成起了但没人用的状态。
    """
    return None


async def _main() -> None:
    from teamai.infrastructure.scheduler import scheduler

    await init_database()
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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
