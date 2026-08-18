"""记忆领域模型：MemoryEntry（偏好是其 PREFERENCE 类型）。"""

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
    # 对普通记忆：哪条用户消息变成了这条（蒸馏时通常为 None，人工写入时是录入人）。
    # 对 PREFERENCE 类型（偏好）：谁设的这条偏好 —— 对应合表前独立 preferences 表的
    # `user_id` 字段。「偏好只带发起人自己的」收窄实现时需要按它过滤。
    source_user_id: str | None = None
    source: MemorySource = MemorySource.MANUAL
    # 向量库里对应点的 id。有值即表示「已建索引」，据此能查出哪些记忆漏了索引。
    # ⚠️ 改造前这个字段声明了、mapper 两侧也在传，但没有任何代码写入它 ——
    # 于是 scripts/cleanup_chat_memories.py 里 `embedding_ref IS NULL` 恒为真，
    # 那条注释声称的「有向量引用的是正规路径写入的」是不存在的机制。
    embedding_ref: str | None = None
    # 建索引时所用 content 的 md5。与 embedding_ref 是两件事，缺一不可:
    # 只有 ref 判不出内容漂移（编辑过但向量没重算），只有 hash 判不出向量丢失。
    # 对账谓词同时用这两列，见 docs/plan-memory-outbox.md §5.1。
    #
    # ⚠️ 加这个字段时必须同步改 infrastructure/repositories/memory.py 的
    # mapper **两侧**。漏在 mapper 里的后果特别隐蔽:projector 回填了值但存不进
    # 库，于是每轮对账都判「hash 不符」而重新 embed —— 向量始终是对的、检索
    # 正常，只有账单和 reconcile 指标会暴露。本项目在 mapper 漏字段上踩过一次。
    embedded_hash: str | None = None
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

    def should_embed(self) -> bool:
        """本条是否应当有向量。见模块底部 `should_embed()` 的说明。"""
        return should_embed(self.type) and self.is_current

    def edited(self) -> MemorySource:
        """人工修改后该落到哪个 source。

        DISTILLED → EDITED；MANUAL 与 EDITED 保持原样（人改人写的东西，
        仍然是人写的；改第二次也不必再变）。
        """
        return MemorySource.EDITED if self.source is MemorySource.DISTILLED else self.source


def should_embed(type: MemoryType) -> bool:
    """这个类型的记忆该不该有向量。唯一的判定处。

    偏好不建向量：它在检索时被无条件全量带上（`MemoryService.query_for_context`
    的偏好段），建了向量只会白白竞争 top_k 名额。

    为什么收口成一个函数而不是在各处写 `if type is PREFERENCE`：合表后
    「有没有向量」取决于 `type` 这个**可变**字段，散开写就会漏 —— 漏掉的正是
    `edit()` 只改 type 不改 content 的那条路径，两个方向都错（改成偏好则旧向量
    残留、白占名额；改回普通记忆则永远没有向量、语义检索找不到）。

    为什么放在 domain 而不是用例层：写入（MemoryService）、投影（MemoryProjector）
    与对账（MemoryReconciler）三处都要问它，而后两者不该依赖用例层。

    ⚠️ 对账用的 SQL 谓词是本函数的等价形式（`type <> 'PREFERENCE' AND
    superseded_by IS NULL`，见 docs/plan-memory-outbox.md §5.1）。改这里必须同步
    改 `MemoryReconciler` —— 两者不一致会让对账与投影互相拆台：一方判「该有
    向量」不断入队，另一方判「不该有」不断删掉，形成烧钱的死循环。
    """
    return type is not MemoryType.PREFERENCE
