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


class Visibility(Enum):
    CHANNEL = "channel"
    PRIVATE = "private"


@dataclass
class MemoryEntry:
    id: str
    channel_instance_id: str
    content: str
    type: MemoryType = MemoryType.BACKGROUND_KNOWLEDGE
    source_user_id: str | None = None
    source: MemorySource = MemorySource.MANUAL
    visibility: Visibility = Visibility.CHANNEL
    # 向量库里对应点的 id。有值即表示「已建索引」，据此能查出哪些记忆漏了索引。
    # ⚠️ 改造前这个字段声明了、mapper 两侧也在传，但没有任何代码写入它 ——
    # 于是 scripts/cleanup_chat_memories.py 里 `embedding_ref IS NULL` 恒为真，
    # 那条注释声称的「有向量引用的是正规路径写入的」是不存在的机制。
    embedding_ref: str | None = None
    created_at: datetime = field(default_factory=_utcnow)

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
