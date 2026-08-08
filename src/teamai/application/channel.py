"""频道实例服务：Slack 频道到内部实例的映射与复用。"""

from __future__ import annotations

from teamai.domain.identity import gen_id
from teamai.domain.models import ChannelInstance
from teamai.domain.repositories import ChannelRepository, PolicyRepository


class ChannelService:
    def __init__(self, channel_repo: ChannelRepository, policy_repo: PolicyRepository) -> None:
        self._channel_repo = channel_repo
        self._policy_repo = policy_repo

    async def get_or_create(self, platform: str, channel_id: str, workspace_id: str) -> ChannelInstance:
        instance = await self._channel_repo.get_by_platform_channel(platform, channel_id, workspace_id)
        if instance is not None:
            return instance
        instance = ChannelInstance(
            id=gen_id("ch"),
            platform=platform,
            channel_id=channel_id,
            workspace_id=workspace_id,
            agent_identity=gen_id("ai"),
        )
        await self._channel_repo.upsert(instance)
        return instance

    async def get(self, channel_instance_id: str) -> ChannelInstance | None:
        return await self._channel_repo.get(channel_instance_id)
