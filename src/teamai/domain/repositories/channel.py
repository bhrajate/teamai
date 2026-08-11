"""频道实例仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.channel import ChannelInstance


class ChannelRepository(ABC):
    @abstractmethod
    async def get(self, channel_instance_id: str) -> ChannelInstance | None: ...

    @abstractmethod
    async def list(self) -> list[ChannelInstance]:
        """全部频道实例，按创建时间倒序。

        没有 channel_instance_id 入参：Admin 控制台要先列出频道才能让人选一个，
        而其余所有 Admin 端点都以 channel_instance_id 为路径参数，这是唯一的入口。
        频道数量级是「团队装了多少个群」，无需分页。
        """
        ...

    @abstractmethod
    async def get_by_platform_channel(
        self, platform: str, channel_id: str, workspace_id: str
    ) -> ChannelInstance | None: ...

    @abstractmethod
    async def upsert(self, instance: ChannelInstance) -> None: ...
