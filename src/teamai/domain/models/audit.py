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
