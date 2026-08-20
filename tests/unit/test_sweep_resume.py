"""超时巡检的续跑分流，与终态时的检查点清理。

三处最容易出错，都写死：

- **updated_at 必须刷新** —— 漏了会让同一任务被无限续跑，且不报任何错
- **状态留在 RUNNING** —— 状态机没有自环，走 transition() 会抛 InvalidTransition
- **终态清检查点** —— 留着的话巡检会去续跑一个已经结束的任务
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.domain.models import AuditLog, Task, TaskStatus
from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.ports import QueuePayload
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.domain.services import AuditLogWriter


class FakeTaskRepo:
    def __init__(self, stale: list[Task] | None = None) -> None:
        self.tasks = {t.id: t for t in (stale or [])}
        self._stale = stale or []
        self.updated: list[tuple[str, TaskStatus, datetime]] = []

    async def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    async def create(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def update(self, task: Task) -> None:
        self.updated.append((task.id, task.status, task.updated_at))

    async def list_stale(self, statuses: tuple, before: datetime) -> list[Task]:
        return [t for t in self._stale if t.status in statuses]


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[QueuePayload] = []

    async def enqueue(self, payload: QueuePayload) -> None:
        self.enqueued.append(payload)

    async def dequeue(self, timeout_seconds: float = 0) -> QueuePayload | None:
        return None


class FakeAudit:
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)


class FakeCheckpoints(CheckpointRepository):
    def __init__(self, store: dict[str, TaskCheckpoint] | None = None) -> None:
        self.store = store or {}
        self.deleted: list[str] = []
        self.bumped: list[str] = []

    async def get(self, task_id: str) -> TaskCheckpoint | None:
        return self.store.get(task_id)

    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None:
        old = self.store.get(task_id)
        self.store[task_id] = TaskCheckpoint(
            task_id=task_id,
            messages=messages,
            tokens_used=tokens_used,
            attempts=old.attempts if old else 0,
        )

    async def delete(self, task_id: str) -> None:
        self.deleted.append(task_id)
        self.store.pop(task_id, None)

    async def bump_attempts(self, task_id: str) -> int:
        self.bumped.append(task_id)
        cp = self.store.get(task_id)
        if cp is None:
            return 0
        cp.attempts += 1
        return cp.attempts


def _task(status: TaskStatus = TaskStatus.RUNNING, tid: str = "task_1") -> Task:
    t = Task(
        id=tid,
        channel_instance_id="ch1",
        thread_ref="ts1",
        requester_id="u1",
        intent="code_review",
    )
    t.status = status
    t.updated_at = datetime.now(UTC) - timedelta(hours=48)
    return t


def _orch(
    stale: list[Task],
    checkpoints: FakeCheckpoints | None = None,
) -> tuple[TaskOrchestrator, FakeTaskRepo, FakeQueue, FakeCheckpoints]:
    repo = FakeTaskRepo(stale)
    queue = FakeQueue()
    cps = checkpoints if checkpoints is not None else FakeCheckpoints()
    orch = TaskOrchestrator(repo, AuditLogWriter(FakeAudit()), queue, cps)  # type: ignore[arg-type]
    return orch, repo, queue, cps


_TIMEOUTS = {"pending_timeout": timedelta(minutes=30), "running_timeout": timedelta(hours=24)}


# ---- 分流 ----


async def test_有检查点则续跑而非判死() -> None:
    task = _task()
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"msgs", 100, attempts=0)})
    orch, repo, queue, _ = _orch([task], cps)

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert [t.id for t in report.resumed] == ["task_1"]
    assert report.swept == []
    assert len(queue.enqueued) == 1
    assert task.status is TaskStatus.RUNNING, "续跑不改状态"


async def test_无检查点仍判死() -> None:
    orch, _, queue, _ = _orch([_task()])

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert [t.id for t in report.swept] == ["task_1"]
    assert report.resumed == []
    assert queue.enqueued == []


async def test_超上限判死() -> None:
    """一个每次都崩在同一处的任务不该无限续跑一直烧 token。"""
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100, attempts=3)})
    orch, _, queue, _ = _orch([_task()], cps)

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert [t.id for t in report.swept] == ["task_1"]
    assert queue.enqueued == []


async def test_上限为0时关闭续跑() -> None:
    """行为退回改造前：崩溃一律收敛 FAILED。"""
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100)})
    orch, _, queue, _ = _orch([_task()], cps)

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=0)

    assert [t.id for t in report.swept] == ["task_1"]
    assert queue.enqueued == []


async def test_PENDING不参与续跑() -> None:
    """它还没开始执行，没有检查点可言 —— 有也是脏数据。"""
    cps = FakeCheckpoints({"task_p": TaskCheckpoint("task_p", b"m", 100)})
    orch, _, queue, _ = _orch([_task(TaskStatus.PENDING, "task_p")], cps)

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert [t.id for t in report.swept] == ["task_p"]
    assert queue.enqueued == []


async def test_未装配检查点仓储时照旧判死() -> None:
    repo = FakeTaskRepo([_task()])
    orch = TaskOrchestrator(repo, AuditLogWriter(FakeAudit()), FakeQueue())  # type: ignore[arg-type]

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert [t.id for t in report.swept] == ["task_1"]


# ---- 死循环防护 ----


async def test_续跑必须刷新updated_at() -> None:
    """回归点。漏了的表现是同一任务被**无限续跑**：下一轮巡检立刻又把它捞出来，
    而它每次都还有检查点、attempts 每次 +1，直到烧穿上限 —— 期间每次都重发
    累积历史，越跑越贵。且这个过程不报任何错。
    """
    task = _task()
    before = task.updated_at
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100)})
    orch, repo, _, _ = _orch([task], cps)

    await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert task.updated_at > before, "updated_at 没刷新 → 会被无限续跑"
    assert repo.updated, "必须落库，否则内存里改了库里没改"
    assert repo.updated[0][1] is TaskStatus.RUNNING


async def test_续跑自增attempts() -> None:
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100, attempts=1)})
    orch, _, _, _ = _orch([_task()], cps)

    await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert cps.bumped == ["task_1"]
    assert cps.store["task_1"].attempts == 2


async def test_重投载荷不带prompt() -> None:
    """原始提问已在检查点里；worker 侧 `payload.prompt or task.intent` 兜底。"""
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100)})
    orch, _, queue, _ = _orch([_task()], cps)

    await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert queue.enqueued[0].prompt == ""
    assert queue.enqueued[0].task_id == "task_1"


async def test_多个任务分别分流() -> None:
    a, b, c = _task(tid="a"), _task(tid="b"), _task(tid="c")
    cps = FakeCheckpoints(
        {
            "a": TaskCheckpoint("a", b"m", 100, attempts=0),  # 可续跑
            "c": TaskCheckpoint("c", b"m", 100, attempts=9),  # 超限
        }
    )
    orch, _, queue, _ = _orch([a, b, c], cps)

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert [t.id for t in report.resumed] == ["a"]
    assert sorted(t.id for t in report.swept) == ["b", "c"]
    assert len(queue.enqueued) == 1


async def test_入队失败不吞进resumed() -> None:
    """队列挂了时不该谎报「已续跑」—— 那个任务实际没人接。"""

    class BrokenQueue(FakeQueue):
        async def enqueue(self, payload: QueuePayload) -> None:
            raise ConnectionError("队列挂了（测试注入）")

    repo = FakeTaskRepo([_task()])
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100)})
    orch = TaskOrchestrator(repo, AuditLogWriter(FakeAudit()), BrokenQueue(), cps)  # type: ignore[arg-type]

    report = await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert report.resumed == []
    assert [tid for tid, _ in report.failed] == ["task_1"]


# ---- 终态清理 ----


@pytest.mark.parametrize(
    "terminal", [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED]
)
async def test_进终态清检查点(terminal: TaskStatus) -> None:
    """留着的话巡检会去续跑一个已经结束的任务。"""
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100)})
    orch, _, _, _ = _orch([], cps)
    task = _task()

    await orch.transition(task, terminal, actor="u1")

    assert cps.deleted == ["task_1"]
    assert "task_1" not in cps.store


async def test_PAUSED不清检查点() -> None:
    """预算耗尽是暂停而非终结 —— 追加配额后应从断点续跑而非重新开始。"""
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100)})
    orch, _, _, _ = _orch([], cps)

    await orch.transition(_task(), TaskStatus.PAUSED, actor="u1")

    assert cps.deleted == []
    assert "task_1" in cps.store


async def test_判死时也清检查点() -> None:
    """巡检把超限任务判死，那条路径同样要清 —— 它走的是 transition()。"""
    cps = FakeCheckpoints({"task_1": TaskCheckpoint("task_1", b"m", 100, attempts=9)})
    orch, _, _, _ = _orch([_task()], cps)

    await orch.sweep_stale_tasks(**_TIMEOUTS, max_resume_attempts=3)

    assert cps.deleted == ["task_1"]


async def test_无检查点时终态迁移不报错() -> None:
    """大多数任务（纯文本、单轮）从未落过检查点，而它们同样会走终态迁移。"""
    orch, _, _, cps = _orch([])

    await orch.transition(_task(), TaskStatus.DONE, actor="u1")

    assert cps.deleted == ["task_1"]  # 删不存在的是静默的
