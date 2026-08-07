"""TaskOrchestrator 测试。

全程纯内存替身，不连 Redis / Postgres —— 依赖倒置归位后的直接收益。
"""

from __future__ import annotations

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.domain.models import AuditAction, InvalidTransition, TaskStatus
from tests.fakes import FakeAuditRepository, FakeTaskQueue, FakeTaskRepository


async def test_创建任务落库并写审计(
    orchestrator: TaskOrchestrator,
    task_repo: FakeTaskRepository,
    audit_repo: FakeAuditRepository,
) -> None:
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")

    assert task.status is TaskStatus.PENDING
    assert task_repo.create_calls == 1
    assert task_repo.items[task.id] is task

    assert len(audit_repo.logs) == 1
    log = audit_repo.logs[0]
    assert log.action is AuditAction.TASK_CREATE
    assert log.channel_instance_id == "ch1"
    assert log.user_id == "u1"
    assert log.task_id == task.id
    assert log.detail == {"intent": "review", "tag": None}


async def test_同步任务不入队(orchestrator: TaskOrchestrator, queue: FakeTaskQueue) -> None:
    await orchestrator.create_task("ch1", "ts1", "u1", "review")
    assert queue.enqueued == []


async def test_异步任务入队且载荷正确(
    orchestrator: TaskOrchestrator,
    queue: FakeTaskQueue,
) -> None:
    task = await orchestrator.create_task(
        "ch1", "ts1", "u1", "refactor", model_level="full", async_execution=True
    )

    assert len(queue.enqueued) == 1
    p = queue.enqueued[0]
    assert p.task_id == task.id
    assert p.channel_instance_id == "ch1"
    assert p.model_level == "full"


async def test_入队失败向上抛出(orchestrator: TaskOrchestrator, queue: FakeTaskQueue) -> None:
    """队列不可用时异常不应被吞掉，由调用方决定降级策略。"""
    queue.fail_next = True
    with pytest.raises(ConnectionError):
        await orchestrator.create_task("ch1", "ts1", "u1", "x", async_execution=True)


async def test_入队失败前任务已落库(
    orchestrator: TaskOrchestrator,
    queue: FakeTaskQueue,
    task_repo: FakeTaskRepository,
) -> None:
    """记录当前实现的行为：入队在落库之后，失败不回滚，任务留在 PENDING。"""
    queue.fail_next = True
    with pytest.raises(ConnectionError):
        await orchestrator.create_task("ch1", "ts1", "u1", "x", async_execution=True)
    assert len(task_repo.items) == 1
    assert next(iter(task_repo.items.values())).status is TaskStatus.PENDING


async def test_迁移更新仓储并写审计(
    orchestrator: TaskOrchestrator,
    task_repo: FakeTaskRepository,
    audit_repo: FakeAuditRepository,
) -> None:
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")
    await orchestrator.transition(task, TaskStatus.RUNNING, actor="u1")

    assert task.status is TaskStatus.RUNNING
    assert task_repo.update_calls == 1
    assert audit_repo.logs[-1].action is AuditAction.TASK_TRANSITION
    assert audit_repo.logs[-1].detail == {"to": "RUNNING"}


async def test_非法迁移不落库不写审计(
    orchestrator: TaskOrchestrator,
    task_repo: FakeTaskRepository,
    audit_repo: FakeAuditRepository,
) -> None:
    """校验在写库之前发生 —— 失败不应留下副作用。"""
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")
    audit_before = len(audit_repo.logs)

    with pytest.raises(InvalidTransition):
        await orchestrator.transition(task, TaskStatus.DONE, actor="u1")

    assert task.status is TaskStatus.PENDING
    assert task_repo.update_calls == 0
    assert len(audit_repo.logs) == audit_before


async def test_取消运行中任务(orchestrator: TaskOrchestrator) -> None:
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")
    await orchestrator.transition(task, TaskStatus.RUNNING, actor="u1")
    await orchestrator.cancel(task, actor="admin")

    assert task.status is TaskStatus.CANCELLED
    assert task.canceled_by == "admin"


@pytest.mark.parametrize(
    "terminal",
    [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
async def test_终态任务取消报错(orchestrator: TaskOrchestrator, terminal: TaskStatus) -> None:
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")
    task.status = terminal

    with pytest.raises(ValueError, match="终态"):
        await orchestrator.cancel(task, actor="admin")


async def test_get_命中与未命中(orchestrator: TaskOrchestrator) -> None:
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")
    assert await orchestrator.get(task.id) is task
    assert await orchestrator.get("nonexistent") is None


async def test_list_按频道隔离(orchestrator: TaskOrchestrator) -> None:
    """频道隔离：ch1 的查询不得返回 ch2 的任务。"""
    a = await orchestrator.create_task("ch1", "ts1", "u1", "a")
    await orchestrator.create_task("ch2", "ts2", "u2", "b")

    got = await orchestrator.list("ch1")
    assert [t.id for t in got] == [a.id]


async def test_list_按状态过滤(orchestrator: TaskOrchestrator) -> None:
    a = await orchestrator.create_task("ch1", "ts1", "u1", "a")
    await orchestrator.create_task("ch1", "ts2", "u1", "b")
    await orchestrator.transition(a, TaskStatus.RUNNING, actor="u1")

    running = await orchestrator.list("ch1", TaskStatus.RUNNING)
    pending = await orchestrator.list("ch1", TaskStatus.PENDING)
    assert [t.id for t in running] == [a.id]
    assert len(pending) == 1


async def test_任务_id_唯一(orchestrator: TaskOrchestrator) -> None:
    ids = {(await orchestrator.create_task("ch1", "ts", "u1", "x")).id for _ in range(20)}
    assert len(ids) == 20


async def test_审计条数与操作数一致(
    orchestrator: TaskOrchestrator,
    audit_repo: FakeAuditRepository,
) -> None:
    """审计完整性：每个成功动作恰好一条留痕。"""
    task = await orchestrator.create_task("ch1", "ts1", "u1", "review")
    await orchestrator.transition(task, TaskStatus.RUNNING, actor="u1")
    await orchestrator.transition(task, TaskStatus.WAITING_INPUT, actor="u1")
    await orchestrator.cancel(task, actor="admin")

    assert len(audit_repo.logs) == 4
    assert all(x.task_id == task.id for x in audit_repo.logs)
