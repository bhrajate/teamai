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
    async def update(self, entry: MemoryEntry) -> None:
        """原地更新。

        ⚠️ 实现须复用传入 entry 的 id。走 `session.merge` 时按主键匹配，
        传一个新 id 进去是 INSERT 而非 UPDATE —— `budget_quotas` 上踩过这个坑
        （见 BudgetController.configure_channel_quota 的说明），那次的表现是
        「管理员改完上限读回的仍是旧行」。
        """
        ...

    @abstractmethod
    async def delete(self, entry_id: str) -> None: ...

    @abstractmethod
    async def set_preference(self, pref: Preference) -> None: ...

    @abstractmethod
    async def list_preferences(self, channel_instance_id: str) -> list[Preference]: ...
