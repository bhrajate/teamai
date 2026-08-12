"""审计日志领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditAction(Enum):
    TASK_CREATE = "task_create"
    TASK_TRANSITION = "task_transition"
    TOOL_CALL = "tool_call"
    TOOL_DENIED = "tool_denied"
    MEMORY_STORE = "memory_store"
    MEMORY_DELETE = "memory_delete"
    # 从对话窗口蒸馏出记忆。与 MEMORY_STORE 分开：后者是人或管理员显式写入，
    # 这个是系统自动提取 —— 排查「记忆库里怎么会有这条」时要能区分来源。
    MEMORY_DISTILL = "memory_distill"
    # 人工修改已有记忆。与「删一条 + 加一条」分开记：那样审计里看不出是同一
    # 条的演进，而 id 与 created_at 也会被重置。
    MEMORY_EDIT = "memory_edit"
    POLICY_CHANGE = "policy_change"
    BUDGET_CHANGE = "budget_change"
    AMBIENT_TRIGGER = "ambient_trigger"


class AuditResult(Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"
    PAUSED = "PAUSED"


@dataclass
class AuditLog:
    id: str
    channel_instance_id: str
    user_id: str | None
    action: AuditAction
    detail: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    tokens_consumed: int = 0
    result: AuditResult = AuditResult.SUCCESS
    ts: datetime = field(default_factory=_utcnow)
