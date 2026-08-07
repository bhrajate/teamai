"""budget_quotas 表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from teamai.domain.models.budget import BudgetPeriod, BudgetScope, BudgetState
from teamai.infrastructure.db import Base


class BudgetQuotaModel(Base):
    __tablename__ = "budget_quotas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope: Mapped[BudgetScope] = mapped_column(Enum(BudgetScope))
    token_limit: Mapped[int] = mapped_column(Integer)
    period: Mapped[BudgetPeriod] = mapped_column(Enum(BudgetPeriod))
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    channel_instance_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    state: Mapped[BudgetState] = mapped_column(Enum(BudgetState), default=BudgetState.ACTIVE)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
