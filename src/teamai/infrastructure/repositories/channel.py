"""ChannelRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.channel import ChannelInstance
from teamai.domain.repositories.channel import ChannelRepository
from teamai.infrastructure.orm.channel import ChannelInstanceModel


def _channel_to_model(c: ChannelInstance) -> ChannelInstanceModel:
    return ChannelInstanceModel(
        id=c.id,
        platform=c.platform,
        channel_id=c.channel_id,
        workspace_id=c.workspace_id,
        agent_identity=c.agent_identity,
        ambient_enabled=c.ambient_enabled,
        cross_channel_learning=c.cross_channel_learning,
        policy_id=c.policy_id,
        created_at=c.created_at,
    )


def _model_to_channel(m: ChannelInstanceModel) -> ChannelInstance:
    return ChannelInstance(
        id=m.id,
        platform=m.platform,
        channel_id=m.channel_id,
        workspace_id=m.workspace_id,
        agent_identity=m.agent_identity,
        ambient_enabled=m.ambient_enabled,
        cross_channel_learning=m.cross_channel_learning,
        policy_id=m.policy_id,
        created_at=m.created_at,
    )


class SQLChannelRepository(ChannelRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        m = await self._session.get(ChannelInstanceModel, channel_instance_id)
        return _model_to_channel(m) if m else None

    async def get_by_slack(self, channel_id: str, workspace_id: str) -> ChannelInstance | None:
        stmt = select(ChannelInstanceModel).where(
            ChannelInstanceModel.channel_id == channel_id,
            ChannelInstanceModel.workspace_id == workspace_id,
        )
        m = (await self._session.execute(stmt)).scalars().first()
        return _model_to_channel(m) if m else None

    async def upsert(self, instance: ChannelInstance) -> None:
        await self._session.merge(_channel_to_model(instance))
