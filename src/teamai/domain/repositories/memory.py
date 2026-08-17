"""记忆仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.memory import MemoryEntry, MemoryType


class MemoryRepository(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None: ...

    @abstractmethod
    async def list_by_channel(
        self,
        channel_instance_id: str,
        limit: int | None = None,
        *,
        current_only: bool = True,
        exclude_type: MemoryType | None = None,
    ) -> list[MemoryEntry]:
        """按 created_at 倒序返回，limit 为 None 时返回全部。

        实现必须显式排序：此前实现既无 ORDER BY 也无 LIMIT，调用方却在
        Python 侧切前 N 条当作检索结果 —— 行序由数据库自行决定，等于随机取样。
        全量重建向量索引等场景仍需要拿全部，故保留 None 语义，但默认应传 limit。

        `current_only` 默认 True，排除已被取代的条目（`superseded_by` 非空）。
        默认值选 True 而非 False：喂给模型的上下文绝不能含被取代的事实，而
        「忘记传参」在这两个方向上的后果不对称 —— 漏掉历史条目只是少看到几条，
        混入过期事实会让机器人给出已经作废的答案。控制台排查历史时显式传 False。

        `exclude_type` 排除指定类型的条目，默认 None＝全部。它目前只为
        `MemoryService.query_for_context` 的语义回落服务（排除 PREFERENCE，
        避免偏好混进 top_k 名额）；`find_similar` 的回落不加这个参数 ——
        那里偏好靠显式追加、按 id 去重。
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
    async def list_preferences(self, channel_instance_id: str) -> list[MemoryEntry]:
        """列该频道的现行偏好（type='PREFERENCE' 且未被取代），按 created_at 倒序。

        偏好是 memory_entries 里 type='PREFERENCE' 的一类，不单独建向量（见
        MemoryService._vector_ready / _embed_if_available 的取舍）：语义检索
        找不到它，只有本方法这条显式路径会取 —— 检索时由调用方全量带上。
        """
        ...