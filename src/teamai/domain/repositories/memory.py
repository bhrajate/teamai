"""记忆仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.memory import MemoryEntry, Preference


class MemoryRepository(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None: ...

    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str) -> list[MemoryEntry]: ...

    @abstractmethod
    async def get(self, entry_id: str) -> MemoryEntry | None: ...

    @abstractmethod
    async def delete(self, entry_id: str) -> None: ...

    @abstractmethod
    async def set_preference(self, pref: Preference) -> None: ...

    @abstractmethod
    async def list_preferences(self, channel_instance_id: str) -> list[Preference]: ...
