"""预算配额领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    @property
    def remaining(self) -> int:
        return max(0, self.token_limit - self.used_tokens)

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
