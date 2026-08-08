"""任务领域模型：TaskStatus 状态机与 Task。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"

    def can_transit(self, to: TaskStatus) -> bool:
        return to in _TRANSITIONS[self]


# 合法迁移表（枚举类体内定义的 dict 会被 Enum 当作成员，故放模块级）
_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
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


class InvalidTransition(Exception):
    def __init__(self, current: TaskStatus, to: TaskStatus) -> None:
        self.current = current
        self.to = to
        super().__init__(f"非法状态迁移: {current.value} -> {to.value}")


@dataclass
class Task:
    id: str
    channel_instance_id: str
    thread_ref: str  # 线程根引用：slack 装 thread_ts，飞书装根消息 message_id
    requester_id: str
    intent: str
    tag_name: str | None = None
    model_level: str = "light"
    status: TaskStatus = TaskStatus.PENDING
    current_stage: str | None = None
    owner_id: str | None = None
    canceled_by: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def transition(self, to: TaskStatus, actor: str) -> None:
        if not self.status.can_transit(to):
            raise InvalidTransition(self.status, to)
        self.status = to
        if to is TaskStatus.CANCELLED:
            self.canceled_by = actor
        self.updated_at = _utcnow()
