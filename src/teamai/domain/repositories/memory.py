"""记忆仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.memory import MemoryEntry, Preference


class MemoryRepository(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None: ...

    @abstractmethod
    async def list_by_channel(
        self, channel_instance_id: str, limit: int | None = None
    ) -> list[MemoryEntry]:
        """按 created_at 倒序返回，limit 为 None 时返回全部。

        实现必须显式排序：此前实现既无 ORDER BY 也无 LIMIT，调用方却在
        Python 侧切前 N 条当作检索结果 —— 行序由数据库自行决定，等于随机取样。
        全量重建向量索引等场景仍需要拿全部，故保留 None 语义，但默认应传 limit。
        """
        ...

    @abstractmethod
    async def get(self, entry_id: str) -> MemoryEntry | None: ...

    @abstractmethod
    async def delete(self, entry_id: str) -> None: ...

    @abstractmethod
    async def set_preference(self, pref: Preference) -> None: ...

    @abstractmethod
    async def list_preferences(self, channel_instance_id: str) -> list[Preference]: ...
