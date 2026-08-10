"""任务超时巡检。

没有这道巡检，worker 崩溃时正在执行的任务会永久停在 RUNNING：既不重投也不
失败，发起人等不到任何回复，Admin 里也看不出它已经死了。同理 PENDING —— 入队
成功但 worker 全挂时，任务就永远排在队里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from teamai.application.orchestrator import TaskOrchestrator
from teamai.domain.models import AuditAction, Task, TaskStatus
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeTaskQueue, FakeTaskRepository

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
PENDING_TIMEOUT = timedelta(minutes=30)
RUNNING_TIMEOUT = timedelta(hours=24)


def _task(
    status: TaskStatus,
    *,
    updated_ago: timedelta,
    tid: str = "task_1",
) -> Task:
    task = Task(
        id=tid,
        channel_instance_id="ch1",
        thread_ref="ts1",
        requester_id="U1",
        intent="code_review",
    )
    task.status = status
    task.updated_at = NOW - updated_ago
    return task


@pytest.fixture
def rig() -> tuple[TaskOrchestrator, FakeTaskRepository, FakeAuditRepository]:
    repo = FakeTaskRepository()
    audit_repo = FakeAuditRepository()
    orch = TaskOrchestrator(repo, AuditLogWriter(audit_repo), FakeTaskQueue())
    return orch, repo, audit_repo


async def _sweep(orch: TaskOrchestrator) -> list[Task]:
    return await orch.sweep_stale_tasks(PENDING_TIMEOUT, RUNNING_TIMEOUT, now=NOW)


# ===== 该被收掉的 =====


async def test_超时的PENDING判为FAILED(rig) -> None:  # type: ignore[no-untyped-def]
    orch, repo, _ = rig
    task = _task(TaskStatus.PENDING, updated_ago=timedelta(hours=2))
    repo.items[task.id] = task

    swept = await _sweep(orch)

    assert [t.id for t in swept] == [task.id]
    assert task.status is TaskStatus.FAILED


async def test_超时的RUNNING判为FAILED(rig) -> None:  # type: ignore[no-untyped-def]
    orch, repo, _ = rig
    task = _task(TaskStatus.RUNNING, updated_ago=timedelta(days=2))
    repo.items[task.id] = task

    swept = await _sweep(orch)

    assert [t.id for t in swept] == [task.id]
    assert task.status is TaskStatus.FAILED


# ===== 不该被碰的 =====


async def test_未超时的任务不动(rig) -> None:  # type: ignore[no-untyped-def]
    orch, repo, _ = rig
    p = _task(TaskStatus.PENDING, updated_ago=timedelta(minutes=5), tid="t_p")
    r = _task(TaskStatus.RUNNING, updated_ago=timedelta(hours=3), tid="t_r")
    repo.items.update({p.id: p, r.id: r})

    assert await _sweep(orch) == []
    assert p.status is TaskStatus.PENDING
    assert r.status is TaskStatus.RUNNING


async def test_长时间RUNNING但未过24h不动(rig) -> None:  # type: ignore[no-untyped-def]
    """长任务设计上是小时/天级，阈值必须宽到不误杀正常执行的任务。"""
    orch, repo, _ = rig
    task = _task(TaskStatus.RUNNING, updated_ago=timedelta(hours=23, minutes=59))
    repo.items[task.id] = task

    assert await _sweep(orch) == []
    assert task.status is TaskStatus.RUNNING


async def test_PENDING与RUNNING用各自的阈值(rig) -> None:  # type: ignore[no-untyped-def]
    """同样闲置 2 小时：PENDING 早已超时，RUNNING 还远没到。"""
    orch, repo, _ = rig
    p = _task(TaskStatus.PENDING, updated_ago=timedelta(hours=2), tid="t_p")
    r = _task(TaskStatus.RUNNING, updated_ago=timedelta(hours=2), tid="t_r")
    repo.items.update({p.id: p, r.id: r})

    swept = await _sweep(orch)

    assert [t.id for t in swept] == ["t_p"]
    assert r.status is TaskStatus.RUNNING


@pytest.mark.parametrize("terminal", [TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED])
async def test_终态任务不被巡检碰(rig, terminal: TaskStatus) -> None:  # type: ignore[no-untyped-def]
    """终态任务再老也不该动 —— 且 DONE→FAILED 本身就是非法迁移。"""
    orch, repo, _ = rig
    task = _task(terminal, updated_ago=timedelta(days=90))
    repo.items[task.id] = task

    assert await _sweep(orch) == []
    assert task.status is terminal


@pytest.mark.parametrize("status", [TaskStatus.PAUSED, TaskStatus.WAITING_INPUT])
async def test_等待中的任务不被巡检碰(rig, status: TaskStatus) -> None:  # type: ignore[no-untyped-def]
    """PAUSED（等追加预算）与 WAITING_INPUT（等用户回话）本就该长期停着。"""
    orch, repo, _ = rig
    task = _task(status, updated_ago=timedelta(days=30))
    repo.items[task.id] = task

    assert await _sweep(orch) == []
    assert task.status is status


# ===== 留痕与健壮性 =====


async def test_巡检写审计且actor标明系统(rig) -> None:  # type: ignore[no-untyped-def]
    """事后要能区分「人取消的」与「系统判超时的」。"""
    orch, repo, audit_repo = rig
    task = _task(TaskStatus.RUNNING, updated_ago=timedelta(days=2))
    repo.items[task.id] = task

    await _sweep(orch)

    log = audit_repo.logs[-1]
    assert log.action is AuditAction.TASK_TRANSITION
    assert log.detail == {"to": "FAILED"}
    assert log.user_id == "system:timeout-sweeper"


async def test_单条失败不打断整轮(rig) -> None:  # type: ignore[no-untyped-def]
    """一个任务的状态机异常不该让其余任务继续挂着。"""
    orch, repo, _ = rig
    bad = _task(TaskStatus.RUNNING, updated_ago=timedelta(days=2), tid="t_bad")
    good = _task(TaskStatus.RUNNING, updated_ago=timedelta(days=2), tid="t_good")
    repo.items.update({bad.id: bad, good.id: good})

    def _boom(to: TaskStatus, actor: str) -> None:
        raise RuntimeError("状态机炸了（测试注入）")

    bad.transition = _boom  # type: ignore[method-assign]

    swept = await _sweep(orch)

    assert [t.id for t in swept] == ["t_good"]
    assert good.status is TaskStatus.FAILED


async def test_无任务时空转(rig) -> None:  # type: ignore[no-untyped-def]
    orch, _, _ = rig
    assert await _sweep(orch) == []
