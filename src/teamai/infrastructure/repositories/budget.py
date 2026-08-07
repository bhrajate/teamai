"""BudgetRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.budget import BudgetQuota
from teamai.domain.repositories.budget import BudgetRepository
from teamai.infrastructure.orm.budget import BudgetQuotaModel


def _budget_to_model(b: BudgetQuota) -> BudgetQuotaModel:
    return BudgetQuotaModel(
        id=b.id,
        scope=b.scope,
        token_limit=b.token_limit,
        period=b.period,
        used_tokens=b.used_tokens,
        channel_instance_id=b.channel_instance_id,
        state=b.state,
        updated_at=b.updated_at,
    )


def _model_to_budget(m: BudgetQuotaModel) -> BudgetQuota:
    return BudgetQuota(
        id=m.id,
        scope=m.scope,
        token_limit=m.token_limit,
        period=m.period,
        used_tokens=m.used_tokens,
        channel_instance_id=m.channel_instance_id,
        state=m.state,
        updated_at=m.updated_at,
    )


class SQLBudgetRepository(BudgetRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_channel(self, channel_instance_id: str) -> BudgetQuota | None:
        stmt = select(BudgetQuotaModel).where(
            BudgetQuotaModel.channel_instance_id == channel_instance_id,
            BudgetQuotaModel.scope == "CHANNEL",
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_budget(m) if m else None

    async def upsert(self, quota: BudgetQuota) -> None:
        await self._session.merge(_budget_to_model(quota))
