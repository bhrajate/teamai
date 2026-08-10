"""MessageRouter 测试，重点是同步/异步两条链路的分叉。

长任务链路的契约有三条，都在这里锁住：
1. 判定为长任务时只入队，不在 web 进程里执行 Agent（否则拆进程就白拆了）；
2. 入队后任务留在 PENDING —— RUNNING 由 worker 推进，用于区分「排队中」与
   「执行中」；
3. 队列不可用时降级为同步执行，而不是把 ConnectionError 抛给平台。
"""

from __future__ import annotations

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.domain.models import TaskStatus
from teamai.domain.services import AuditLogWriter
from tests.doubles import (
    FakeChannels,
    FakeIntentClassifier,
    FakeMemory,
    FakePolicyRepo,
    FakeRuntime,
    FakeTags,
    mention,
)
from tests.fakes import FakeAuditRepository, FakeTaskQueue, FakeTaskRepository


def _build(kind: str, queue: FakeTaskQueue) -> tuple[MessageRouter, FakeRuntime, FakeTaskRepository]:
    task_repo = FakeTaskRepository()
    orchestrator = TaskOrchestrator(task_repo, AuditLogWriter(FakeAuditRepository()), queue)
    runtime = FakeRuntime()
    router = MessageRouter(
        orchestrator=orchestrator,
        intent=FakeIntentClassifier(kind),
        tags=FakeTags(),
        memory=FakeMemory(),
        budget=object(),
        runtime=runtime,
        channels=FakeChannels(),
        policy_repo=FakePolicyRepo(),
    )
    return router, runtime, task_repo


# ===== 长任务：只入队，不执行 =====


@pytest.mark.parametrize(
    "kind", ["code_review", "bugfix", "data_analysis", "documentation", "pr_operation"]
)
async def test_长任务入队且不在web进程执行(kind: str) -> None:
    queue = FakeTaskQueue()
    router, runtime, _ = _build(kind, queue)

    decision = await router.route(mention())

    assert len(queue.enqueued) == 1, "长任务必须入队"
    assert runtime.runs == 0, "长任务不该在 web 进程里跑 Agent"
    assert decision.handler == "respond"
    assert "已受理" in decision.message


async def test_长任务入队后状态留在PENDING() -> None:
    """RUNNING 由 worker 推进，以此区分「排队中」与「执行中」。"""
    queue = FakeTaskQueue()
    router, _, task_repo = _build("code_review", queue)

    await router.route(mention())

    task = next(iter(task_repo.items.values()))
    assert task.status is TaskStatus.PENDING


async def test_入队载荷携带原始指令与线程引用() -> None:
    """tasks 表只存 intent 不存原文，prompt 必须由载荷带走，否则 worker 拿不到指令。"""
    queue = FakeTaskQueue()
    router, _, task_repo = _build("code_review", queue)

    await router.route(mention("帮我审查 payment 模块"))

    payload = queue.enqueued[0]
    task = next(iter(task_repo.items.values()))
    assert payload.task_id == task.id
    assert payload.prompt == "帮我审查 payment 模块"
    assert payload.thread_ref == "1700000000.1"
    assert payload.channel_instance_id == "ch_1"
    assert payload.model_level == task.model_level


# ===== 短任务：同步执行，不入队 =====


@pytest.mark.parametrize("kind", ["query", "chat", "ticket", "general_task"])
async def test_短任务同步执行且不入队(kind: str) -> None:
    queue = FakeTaskQueue()
    router, runtime, task_repo = _build(kind, queue)

    decision = await router.route(mention("现在几点"))

    assert queue.enqueued == []
    assert runtime.runs == 1
    assert decision.message == "执行完毕"
    assert next(iter(task_repo.items.values())).status is TaskStatus.DONE


# ===== 降级 =====


async def test_入队失败降级为同步执行() -> None:
    """Redis 挂掉时功能不能整体失效，代价是本次响应变慢。"""
    queue = FakeTaskQueue()
    queue.fail_next = True
    router, runtime, task_repo = _build("code_review", queue)

    decision = await router.route(mention())

    assert queue.enqueued == []
    assert runtime.runs == 1, "入队失败后应就地执行"
    assert decision.message == "执行完毕"
    assert next(iter(task_repo.items.values())).status is TaskStatus.DONE


async def test_入队失败不重复建任务() -> None:
    """降级路径复用已落库的那个任务，不该留下多余的 PENDING 记录。"""
    queue = FakeTaskQueue()
    queue.fail_next = True
    router, _, task_repo = _build("code_review", queue)

    await router.route(mention())

    assert task_repo.create_calls == 1
    assert len(task_repo.items) == 1


# ===== 非 @ 消息 =====


async def test_非mention消息只记上下文不建任务() -> None:
    queue = FakeTaskQueue()
    router, runtime, task_repo = _build("code_review", queue)

    decision = await router.route(mention("随便聊聊", is_mention=False))

    assert decision.handler == "observe"
    assert task_repo.create_calls == 0
    assert queue.enqueued == []
    assert runtime.runs == 0
