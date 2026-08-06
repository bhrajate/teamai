"""对话标签模板领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TagTemplate:
    id: str
    channel_instance_id: str
    name: str
    instruction: str
    role: str | None = None
    output_style: str | None = None
    shared: bool = True
    created_by: str | None = None
    active: bool = True
    created_at: datetime = field(default_factory=_utcnow)
