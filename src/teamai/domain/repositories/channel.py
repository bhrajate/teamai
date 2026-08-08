"""频道实例仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.channel import ChannelInstance


class ChannelRepository(ABC):
    @abstractmethod
    async def get(self, channel_instance_id: str) -> ChannelInstance | None: ...

    @abstractmethod
    async def get_by_platform_channel(
        self, platform: str, channel_id: str, workspace_id: str
    ) -> ChannelInstance | None: ...

    @abstractmethod
    async def upsert(self, instance: ChannelInstance) -> None: ...
