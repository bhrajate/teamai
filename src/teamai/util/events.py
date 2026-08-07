"""通用工具：ID 生成与事件幂等键。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


def gen_id(prefix: str = "id") -> str:
    """生成 ULID 风格前缀 ID。"""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


@dataclass(frozen=True)
class EventIdempotencyKey:
    """Slack 事件幂等键：channel + ts + subtype。"""

    channel: str
    ts: str
    subtype: str = ""

    @classmethod
    def from_event(cls, event: dict) -> EventIdempotencyKey:
        return cls(
            channel=str(event.get("channel", "")),
            ts=str(event.get("ts", "")),
            subtype=str(event.get("subtype", "")),
        )

    @property
    def value(self) -> str:
        return f"{self.channel}:{self.ts}:{self.subtype}"
