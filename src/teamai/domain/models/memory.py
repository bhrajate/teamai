"""记忆领域模型：MemoryEntry 与 Preference。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryType(Enum):
    BACKGROUND_KNOWLEDGE = "BACKGROUND_KNOWLEDGE"
    PREFERENCE = "PREFERENCE"
    DECISION = "DECISION"
    FACT = "FACT"


class MemorySource(Enum):
    """记忆的产生方式。

    与 `source_user_id` 是两件事：那个答「哪个用户的话变成了这条」，本字段答
    「这条是谁写下的」。两者都可能为空/系统 —— 蒸馏产出与管理台人工写入的
    `source_user_id` 都是 None，在控制台里长得一模一样，而这张表的内容直接
    影响机器人的回答，「这句话是谁写的」是出问题时第一个要问的。
    """

    # 由 MemoryDistiller 从对话窗口提炼
    DISTILLED = "DISTILLED"
    # 人经 Admin API / 控制台直接写入
    MANUAL = "MANUAL"
    # 原为蒸馏产出，后被人工修正过。不并入 MANUAL：区分「人写的」与
    # 「模型写了人改的」，后者的原始判断仍来自模型，排查时含义不同。
    EDITED = "EDITED"


@dataclass
class MemoryEntry:
    id: str
    channel_instance_id: str
    content: str
    type: MemoryType = MemoryType.BACKGROUND_KNOWLEDGE
    source_user_id: str | None = None
    source: MemorySource = MemorySource.MANUAL
    # 向量库里对应点的 id。有值即表示「已建索引」，据此能查出哪些记忆漏了索引。
    # ⚠️ 改造前这个字段声明了、mapper 两侧也在传，但没有任何代码写入它 ——
    # 于是 scripts/cleanup_chat_memories.py 里 `embedding_ref IS NULL` 恒为真，
    # 那条注释声称的「有向量引用的是正规路径写入的」是不存在的机制。
    embedding_ref: str | None = None
    # 取代本条的那条记忆的 id。非 None 即表示「本条已不是现行事实」，
    # 检索默认排除它们，但行仍在库里 —— 排查「机器人为什么这么说」时，
    # 「当时的说法是什么、被什么取代」比一条已消失的记录有用得多。
    #
    # 为什么不物理删除：与 mem0 / Zep 的取舍一致（两者都是 mark invalid
    # rather than physically removing）。删除不可逆，而蒸馏出的「矛盾」判断
    # 来自模型，可能是错的。
    #
    # 为什么只有一维时间而不是 Zep 的双时间轴（arXiv:2501.13956 §2.1）：
    # 那套模型分开记「事实在现实中何时成立」（t_valid / t_invalid）与
    # 「系统何时知道」（t'_created / t'_expired），边失效时把旧边的 t_invalid
    # 设为新边的 t_valid。本项目的蒸馏是近实时的（窗口满 20 条或静置 600s
    # 即触发），created_at 与事实实际成立时间的偏差在分钟级，双时间轴的收益
    # 接近零，而代价是模型要从对话里额外抽取时间信息。故退化为单时间轴：
    # created_at 兼任 t'_created，superseded_at 兼任 t_invalid。
    # 若将来真需要表达「某事实在某段区间内有效」，superseded_at 已在表里，
    # 补一个 valid_from 即可，不必重构。
    superseded_by: str | None = None
    superseded_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def is_current(self) -> bool:
        """本条是否仍是现行事实。"""
        return self.superseded_by is None

    def supersede(self, by_entry_id: str, at: datetime | None = None) -> None:
        """标记本条被 `by_entry_id` 取代。"""
        self.superseded_by = by_entry_id
        self.superseded_at = at or _utcnow()

    def edited(self) -> MemorySource:
        """人工修改后该落到哪个 source。

        DISTILLED → EDITED；MANUAL 与 EDITED 保持原样（人改人写的东西，
        仍然是人写的；改第二次也不必再变）。
        """
        return MemorySource.EDITED if self.source is MemorySource.DISTILLED else self.source


@dataclass
class Preference:
    id: str
    channel_instance_id: str
    user_id: str
    preference: str
    created_at: datetime = field(default_factory=_utcnow)
