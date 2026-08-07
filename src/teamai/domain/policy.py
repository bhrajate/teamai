"""权限策略领域模型：PermissionPolicy 与 AmbientRule。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class AmbientRule:
    trigger: str
    params: dict[str, Any] = field(default_factory=dict)
    action: str = "nudge"


@dataclass
class PermissionPolicy:
    id: str
    channel_instance_id: str
    allowed_tools: list[str] = field(default_factory=list)
    ambient_rules: list[AmbientRule] = field(default_factory=list)
    updated_by: str | None = None
    updated_at: datetime = field(default_factory=_utcnow)

    def can_use_tool(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools
