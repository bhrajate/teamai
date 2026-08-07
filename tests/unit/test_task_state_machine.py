"""TaskStatus 状态机测试。

对应设计文档正确性属性「任务终态确定性」：
终态不可再迁移，任意非法迁移必抛 InvalidTransition。
"""

from __future__ import annotations

import itertools

import pytest

from teamai.domain.models.task import _TRANSITIONS, InvalidTransition, Task, TaskStatus

TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}

# 期望的合法迁移集，与实现独立书写，用于交叉校验迁移表未被误改
EXPECTED: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_INPUT,
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.WAITING_INPUT: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.PAUSED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.DONE: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def make_task(status: TaskStatus = TaskStatus.PENDING) -> Task:
    t = Task(
        id="task_1",
        channel_instance_id="ch1",
        thread_ts="1700000000.1",
        requester_id="u1",
        intent="review",
    )
    t.status = status
    return t


def test_迁移表覆盖所有状态且与期望一致() -> None:
    assert set(_TRANSITIONS) == set(TaskStatus), "迁移表遗漏状态"
    assert _TRANSITIONS == EXPECTED, "迁移表与期望不符"


@pytest.mark.parametrize(
    ("frm", "to"),
    [(f, t) for f, tos in EXPECTED.items() for t in tos],
)
def test_合法迁移可执行(frm: TaskStatus, to: TaskStatus) -> None:
    task = make_task(frm)
    task.transition(to, actor="u1")
    assert task.status is to


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (f, t)
        for f, t in itertools.product(TaskStatus, TaskStatus)
        if t not in EXPECTED[f]
    ],
)
def test_任意非法迁移必抛异常(frm: TaskStatus, to: TaskStatus) -> None:
    """穷举 7×7 组合中全部非法者（含自迁移），状态须保持不变。"""
    task = make_task(frm)
    with pytest.raises(InvalidTransition) as ei:
        task.transition(to, actor="u1")
    assert ei.value.current is frm
    assert ei.value.to is to
    assert task.status is frm, "抛异常后状态被污染"


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.name))
def test_终态不可再迁移(terminal: TaskStatus) -> None:
    assert _TRANSITIONS[terminal] == set()
    for to in TaskStatus:
        task = make_task(terminal)
        with pytest.raises(InvalidTransition):
            task.transition(to, actor="u1")


def test_自迁移一律非法() -> None:
    for s in TaskStatus:
        assert s not in _TRANSITIONS[s], f"{s.name} 允许自迁移"


def test_取消时记录操作人() -> None:
    task = make_task(TaskStatus.RUNNING)
    task.transition(TaskStatus.CANCELLED, actor="admin")
    assert task.canceled_by == "admin"


def test_非取消迁移不写_canceled_by() -> None:
    task = make_task(TaskStatus.PENDING)
    task.transition(TaskStatus.RUNNING, actor="u1")
    assert task.canceled_by is None


def test_迁移刷新_updated_at() -> None:
    task = make_task(TaskStatus.PENDING)
    before = task.updated_at
    task.transition(TaskStatus.RUNNING, actor="u1")
    assert task.updated_at >= before


def test_can_transit_与迁移表一致() -> None:
    for frm in TaskStatus:
        for to in TaskStatus:
            assert frm.can_transit(to) is (to in EXPECTED[frm])
