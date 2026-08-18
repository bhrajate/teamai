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
    async def find_vector_drift(self, limit: int) -> tuple[list[str], list[str]]:
        """找出向量状态与记忆状态不符的行，返回 (需补建的 id, 需撤除的 id)。

        判据是 `MemoryEntry.should_embed()` 的 SQL 等价形式，逐字对应
        `docs/plan-memory-outbox.md` §5.1 的不变量：

        - **需补建**：`type != PREFERENCE` 且未被取代，而 `embedding_ref` 为空
          或 `embedded_hash` 与当前 content 的 md5 不符。后半句覆盖「编辑过但
          向量没重算」—— 只看 `embedding_ref IS NULL` 判不出这种内容漂移。
        - **需撤除**：偏好或已被取代，却仍有 `embedding_ref`。

        ⚠️ 这两个谓词必须与 `should_embed()` 保持等价。它们分叉时不会报错 ——
        只会让对账与投影互相拆台（一方判「该有向量」不断入队，另一方判「不该有」
        不断删掉），症状是烧钱的死循环。`tests/unit/test_reconciler.py` 穷举了
        type × superseded 的组合来核对这件事。

        为什么放在仓储而不是让对账服务自己写 SQL：`md5()` 的方言差异、以及
        「哪几列参与判断」都是持久化细节，application 层不该知道。此前对账服务
        直接 import 了 `infrastructure.orm`，被分层测试拦住 —— 那个拦得对。

        两个方向合成一个方法而不是两个：它们是同一个不变量的两侧，分开声明会让
        「改了一侧忘了另一侧」变得容易。

        `limit` 分别作用于两个方向（各取至多 limit 条）。有上界是因为首次上线时
        存量偏差可能成千上万，一次全塞进 outbox 会让 lag 指标瞬间爆表、且挤掉
        正常写入的投影。
        """
        ...

    @abstractmethod
    async def list_preferences(self, channel_instance_id: str) -> list[MemoryEntry]:
        """列该频道的现行偏好（type='PREFERENCE' 且未被取代），按 created_at 倒序。

        偏好是 memory_entries 里 type='PREFERENCE' 的一类，不单独建向量（见
        MemoryService._vector_ready / _embed_if_available 的取舍）：语义检索
        找不到它，只有本方法这条显式路径会取 —— 检索时由调用方全量带上。
        """
        ...
