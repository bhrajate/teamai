"""审批超时巡检。

与 sweep_stale_tasks 分开：那个扫 PENDING/RUNNING（worker 挂了），这个扫
WAITING_INPUT（人没回）。阈值差一个数量级、处置也完全不同。

orchestrator 只**找出**超时的任务，不推进 —— 处置要经 ApprovalService 与 gateway
（拿 ToolDenied 恢复 run），属用例编排。这条边界有专测。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.domain.models import AuditLog, PendingApproval, Task, TaskStatus
from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.ports import QueuePayload
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.domain.services import AuditLogWriter


class FakeTaskRepo:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.items = {t.id: t for t in (tasks or [])}

    async def get(self, task_id: str) -> Task | None:
        return self.items.get(task_id)

    async def create(self, task: Task) -> None:
        self.items[task.id] = task

    async def update(self, task: Task) -> None:
        self.items[task.id] = task

    async def list_by_channel(self, cid: str, status: TaskStatus | None = None) -> list[Task]:
        out = [t for t in self.items.values() if t.channel_instance_id == cid]
        return [t for t in out if status is None or t.status is status]

    async def list_stale(self, statuses: tuple, before: datetime) -> list[Task]:
        return []


class FakeQueue:
    async def enqueue(self, payload: QueuePayload) -> None: ...

    async def dequeue(self, timeout_seconds: float = 0) -> QueuePayload | None:
        return None


class FakeAudit:
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)


class FakeCheckpoints(CheckpointRepository):
    def __init__(self, pending_ids: list[str] | None = None) -> None:
        self.pending = {tid: PendingApproval("tc", "github") for tid in (pending_ids or [])}
        self.asked_cutoff: datetime | None = None

    async def get(self, task_id: str) -> TaskCheckpoint | None:
        return None

    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None: ...

    async def delete(self, task_id: str) -> None: ...

    async def bump_attempts(self, task_id: str) -> int:
        return 0

    async def set_pending_approval(self, task_id, messages, pending) -> None:
        self.pending[task_id] = pending

    async def get_pending_approval(self, task_id: str) -> PendingApproval | None:
        return self.pending.get(task_id)

    async def clear_pending_approval(self, task_id: str) -> None:
        self.pending.pop(task_id, None)

    async def list_pending_before(self, cutoff: datetime) -> list[str]:
        self.asked_cutoff = cutoff
        return list(self.pending)


def _task(tid: str = "task_1", status: TaskStatus = TaskStatus.WAITING_INPUT) -> Task:
    t = Task(
        id=tid,
        channel_instance_id="ch_1",
        thread_ref="ts_1",
        requester_id="U9",
        intent="code_review",
    )
    t.status = status
    return t


def _orch(
    tasks: list[Task], checkpoints: FakeCheckpoints | None = None
) -> tuple[TaskOrchestrator, FakeCheckpoints]:
    cps = checkpoints if checkpoints is not None else FakeCheckpoints()
    orch = TaskOrchestrator(
        FakeTaskRepo(tasks),  # type: ignore[arg-type]
        AuditLogWriter(FakeAudit()),
        FakeQueue(),  # type: ignore[arg-type]
        cps,
    )
    return orch, cps


_TIMEOUT = timedelta(days=1)


async def test_找出超时的待批任务() -> None:
    task = _task()
    orch, _ = _orch([task], FakeCheckpoints(["task_1"]))

    stale = await orch.sweep_stale_approvals(_TIMEOUT)

    assert [t.id for t in stale] == ["task_1"]


async def test_按阈值算截止时刻() -> None:
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    orch, cps = _orch([_task()], FakeCheckpoints(["task_1"]))

    await orch.sweep_stale_approvals(timedelta(hours=6), now=now)

    assert cps.asked_cutoff == datetime(2026, 8, 20, 6, 0, tzinfo=UTC)


async def test_只返回不推进状态() -> None:
    """处置要经 ApprovalService 与 gateway（拿 ToolDenied 恢复 run），
    orchestrator 只管任务状态、不认识审批语义。"""
    task = _task()
    orch, _ = _orch([task], FakeCheckpoints(["task_1"]))

    await orch.sweep_stale_approvals(_TIMEOUT)

    assert task.status is TaskStatus.WAITING_INPUT, "不该在这里改状态"


@pytest.mark.parametrize(
    "status",
    [TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED],
)
async def test_状态已不是WAITING_INPUT的不返回(status: TaskStatus) -> None:
    """待批项可能已被处理但清理尚未落库，或任务已被取消 —— 那时不该再走超时处置。"""
    orch, _ = _orch([_task(status=status)], FakeCheckpoints(["task_1"]))

    assert await orch.sweep_stale_approvals(_TIMEOUT) == []


async def test_任务已不存在时跳过() -> None:
    """检查点还在但任务被物理删了（运维操作）—— 不该抛异常。"""
    orch, _ = _orch([], FakeCheckpoints(["task_gone"]))

    assert await orch.sweep_stale_approvals(_TIMEOUT) == []


async def test_没有待批时返回空() -> None:
    orch, _ = _orch([_task()])
    assert await orch.sweep_stale_approvals(_TIMEOUT) == []


async def test_未装配检查点仓储时返回空() -> None:
    orch = TaskOrchestrator(
        FakeTaskRepo([_task()]),  # type: ignore[arg-type]
        AuditLogWriter(FakeAudit()),
        FakeQueue(),  # type: ignore[arg-type]
    )
    assert await orch.sweep_stale_approvals(_TIMEOUT) == []


async def test_多个超时任务一起返回() -> None:
    tasks = [_task("a"), _task("b"), _task("c", status=TaskStatus.DONE)]
    orch, _ = _orch(tasks, FakeCheckpoints(["a", "b", "c"]))

    stale = await orch.sweep_stale_approvals(_TIMEOUT)

    assert sorted(t.id for t in stale) == ["a", "b"], "c 已完成，不该返回"
