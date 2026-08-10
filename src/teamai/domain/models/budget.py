"""预算配额领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetScope(Enum):
    ORGANIZATION = "ORGANIZATION"
    CHANNEL = "CHANNEL"


class BudgetPeriod(Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class BudgetState(Enum):
    ACTIVE = "ACTIVE"
    EXHAUSTED = "EXHAUSTED"


# 各周期的长度。用固定时长而非日历对齐（「每月 1 号」）：配额是从设定那刻
# 起算的一段额度，按自然月对齐会让月中新建的配额头一个周期不足月。
_PERIOD_LENGTH: dict[BudgetPeriod, timedelta] = {
    BudgetPeriod.DAILY: timedelta(days=1),
    BudgetPeriod.WEEKLY: timedelta(weeks=1),
    BudgetPeriod.MONTHLY: timedelta(days=30),
}


@dataclass
class BudgetQuota:
    id: str
    scope: BudgetScope
    token_limit: int
    period: BudgetPeriod = BudgetPeriod.MONTHLY
    used_tokens: int = 0
    channel_instance_id: str | None = None
    state: BudgetState = BudgetState.ACTIVE
    updated_at: datetime = field(default_factory=_utcnow)
    # 当前计费周期的起点。独立于 updated_at：后者每次消费都会刷新，用它判断
    # 周期是否翻页会导致「一直在用的频道永远不重置」。
    period_started_at: datetime = field(default_factory=_utcnow)

    @property
    def remaining(self) -> int:
        return max(0, self.token_limit - self.used_tokens)

    @property
    def period_length(self) -> timedelta:
        return _PERIOD_LENGTH[self.period]

    def should_reset(self, now: datetime) -> bool:
        """当前周期是否已走完。"""
        return now - self.period_started_at >= self.period_length

    def reset(self, now: datetime) -> None:
        """翻到新周期：清零用量、恢复 ACTIVE、重置周期起点。

        起点取 now 而非 period_started_at + period_length：后者在停机数个
        周期后会连续补上多次重置（每次只推进一个周期），而语义上「补发」
        没有意义 —— 用量早已清零，重复推进只是让起点落在过去。
        """
        self.used_tokens = 0
        self.state = BudgetState.ACTIVE
        self.period_started_at = now
        self.updated_at = now

    def can_consume(self, tokens: int) -> bool:
        return self.state is BudgetState.ACTIVE and self.remaining >= tokens

    def consume(self, tokens: int) -> bool:
        """尝试消费；成功返回 True，超限返回 False 并置 EXHAUSTED。"""
        if not self.can_consume(tokens):
            self.state = BudgetState.EXHAUSTED
            return False
        self.used_tokens += tokens
        if self.used_tokens >= self.token_limit:
            self.state = BudgetState.EXHAUSTED
        self.updated_at = _utcnow()
        return True
