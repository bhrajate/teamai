"""频道实例领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ChannelInstance:
    id: str
    platform: str
    channel_id: str
    workspace_id: str
    agent_identity: str
    ambient_enabled: bool = False
    cross_channel_learning: bool = False
    policy_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
