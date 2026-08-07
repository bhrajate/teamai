"""记忆领域模型：MemoryEntry 与 Preference。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryType(Enum):
    BACKGROUND_KNOWLEDGE = "BACKGROUND_KNOWLEDGE"
    PREFERENCE = "PREFERENCE"
    DECISION = "DECISION"
    FACT = "FACT"


class Visibility(Enum):
    CHANNEL = "channel"
    PRIVATE = "private"


@dataclass
class MemoryEntry:
    id: str
    channel_instance_id: str
    content: str
    type: MemoryType = MemoryType.BACKGROUND_KNOWLEDGE
    source_user_id: str | None = None
    visibility: Visibility = Visibility.CHANNEL
    embedding_ref: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class Preference:
    id: str
    channel_instance_id: str
    user_id: str
    preference: str
    created_at: datetime = field(default_factory=_utcnow)
