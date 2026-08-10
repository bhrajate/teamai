"""长任务链路的冒烟验证：web 侧入队 → worker 侧消费 → 回帖。

为什么除了单测还要这个：单测把队列换成了内存替身，封住了序列化这一环；
而跨进程时载荷要真的过一遍 json.dumps → Redis → json.loads，且仓储必须
真的 commit（否则 worker 那个 session 读不到）。这两类缺陷只有连真依赖
才暴露 —— 本脚本就是为此存在的。

两种模式：

    # ① 真 Redis + 内存仓储：单进程内跑完整链路，验证序列化往返
    REDIS_URL=redis://localhost:6390/0 python -m scripts.verify_long_task_flow

    # ② 真 Redis + 真 Postgres：只做 web 侧入队，随后手动起真 worker 进程
    #    （python -m app.worker.main）观察它消费，验证跨进程可见性
    DATABASE_URL=... REDIS_URL=... python -m scripts.verify_long_task_flow --real-db
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.worker.main import handle_payload
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.domain.models import TaskStatus
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.queue import RedisTaskQueue
from teamai.infrastructure.redis_client import RedisClientProvider
from tests.doubles import (
    FakeChannels,
    FakeIntentClassifier,
    FakeMemory,
    FakePolicyRepo,
    FakeRuntime,
    FakeTags,
    mention,
)
from tests.fakes import FakeAuditRepository, FakeMessagePublisher, FakeTaskRepository

MEMORY_QUEUE = "teamai-verify-longtask"
PROMPT = "帮我 review 一下 payment 模块的并发安全"


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


async def _verify_with_memory_repo() -> None:
    """真 Redis，其余内存替身。验证载荷的序列化往返与两侧的字段对齐。"""
    provider = RedisClientProvider()
    queue = RedisTaskQueue(provider, queue_name=MEMORY_QUEUE)
    client = provider.client()
    await client.delete(MEMORY_QUEUE)  # 清掉上次残留，从空队列开始

    task_repo = FakeTaskRepository()
    publisher = FakeMessagePublisher()
    runtime = FakeRuntime()
    channels = FakeChannels()
    orchestrator = TaskOrchestrator(task_repo, AuditLogWriter(FakeAuditRepository()), queue)
    router = MessageRouter(
        orchestrator=orchestrator,
        intent=FakeIntentClassifier("code_review"),
        tags=FakeTags(),
        memory=FakeMemory(),
        budget=object(),
        runtime=runtime,
        channels=channels,
        policy_repo=FakePolicyRepo(),
    )

    class _Container:
        task_repo = None  # 下面逐个赋真值，仅为让 worker 取到属性

    container = _Container()
    container.task_repo = task_repo
    container.channels = channels
    container.orchestrator = orchestrator
    container.router = router
    container.queue = queue
    container.publisher = publisher

    print("\n[1] web 侧：收到一条长任务 @消息")
    decision = await router.route(mention(PROMPT))
    assert "已受理" in decision.message, decision.message
    assert runtime.runs == 0, "web 进程不该跑 Agent"
    _ok(f"立即回复: {decision.message}")
    _ok("Agent 未在 web 进程执行")

    depth = await client.llen(MEMORY_QUEUE)
    assert depth == 1, f"队列深度应为 1，实为 {depth}"
    _ok(f"Redis 队列深度: {depth}")

    task = next(iter(task_repo.items.values()))
    assert task.status is TaskStatus.PENDING
    _ok(f"任务 {task.id} 状态: {task.status.value}（排队中，RUNNING 由 worker 推进）")

    print("\n[2] Redis 里的原始载荷")
    raw = await client.lindex(MEMORY_QUEUE, 0)
    print(f"  {raw.decode() if isinstance(raw, bytes) else raw}")

    print("\n[3] worker 侧：出队并执行")
    payload = await queue.dequeue()
    assert payload is not None, "出队拿到 None"
    _ok(f"反序列化成功: task_id={payload.task_id} model_level={payload.model_level}")
    assert payload.prompt == PROMPT, payload.prompt
    _ok(f"原始指令跨进程送达: {payload.prompt}")

    await handle_payload(container, payload)
    assert runtime.runs == 1, f"Agent 应执行 1 次，实为 {runtime.runs}"
    _ok("Agent 在 worker 侧执行 1 次")
    assert task.status is TaskStatus.DONE, task.status
    _ok(f"任务终态: {task.status.value}")

    assert len(publisher.replies) == 1, publisher.replies
    target, text = publisher.replies[0]
    _ok(f"回帖 -> platform={target.platform} channel={target.channel_id} thread={target.thread_ref}")
    _ok(f"回帖内容: {text}")

    depth = await client.llen(MEMORY_QUEUE)
    assert depth == 0, f"队列应排空，实为 {depth}"
    _ok(f"队列已排空，深度: {depth}")

    await client.delete(MEMORY_QUEUE)
    await provider.aclose()


async def _verify_with_real_db() -> None:
    """真 Redis + 真 Postgres，用真 Container 走 web 侧那一半。

    跑完后队列里留一条待消费的载荷，接着手动起真 worker 进程即可验证
    跨进程可见性 —— 那是「仓储写方法必须 commit」这条约束的真实考场。
    """
    from teamai.application.events import IncomingMessage
    from teamai.config import settings
    from teamai.container import build_container
    from teamai.infrastructure.db import init_db_or_warn

    logging.basicConfig(level=logging.INFO)
    await init_db_or_warn()
    container = build_container()

    msg = IncomingMessage(
        platform="slack",
        event_id="slack:EvVERIFY1",
        workspace_id="T_VERIFY",
        channel_id="C_VERIFY",
        channel_type="channel",
        user_id="U_VERIFY",
        text=PROMPT,  # 命中 code_review 关键词 → 判为长任务
        message_id="1700000000.1",
        thread_ref="1700000000.1",
        is_mention=True,
    )

    print("\n[1] web 侧：真 Container 处理一条长任务 @消息")
    decision = await container.router.route(msg)
    _ok(f"handler={decision.handler}")
    _ok(f"立即回复: {decision.message}")

    client = container.redis.client()
    depth = await client.llen(settings.queue_name)
    _ok(f"队列 {settings.queue_name} 深度: {depth}")
    raw = await client.lindex(settings.queue_name, 0)
    if raw:
        print(f"  队首载荷: {raw.decode() if isinstance(raw, bytes) else raw}")

    instance = await container.channels.get_or_create("slack", "C_VERIFY", "T_VERIFY")
    for t in await container.task_repo.list_by_channel(instance.id):
        _ok(f"任务 {t.id} status={t.status.value} intent={t.intent} model={t.model_level}")

    print("\n下一步：另起一个终端跑真 worker 进程，它应当取走这条载荷")
    print("  DATABASE_URL=... REDIS_URL=... python -m app.worker.main")

    await container.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real-db",
        action="store_true",
        help="用真 Container（需 Postgres），只做 web 侧入队，配合真 worker 进程验证",
    )
    args = parser.parse_args()

    if args.real_db:
        await _verify_with_real_db()
        print("\n\033[32mweb 侧入队完成，等待 worker 消费\033[0m\n")
    else:
        await _verify_with_memory_repo()
        print("\n\033[32m长任务链路验证通过\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
