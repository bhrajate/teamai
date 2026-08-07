"""标签模板仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.tag import TagTemplate


class TagRepository(ABC):
    @abstractmethod
    async def create(self, tag: TagTemplate) -> None: ...

    @abstractmethod
    async def get(self, channel_instance_id: str, name: str) -> TagTemplate | None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str) -> list[TagTemplate]: ...

    @abstractmethod
    async def delete(self, tag_id: str) -> None: ...

    @abstractmethod
    async def set_active(self, tag_id: str, active: bool) -> None: ...
