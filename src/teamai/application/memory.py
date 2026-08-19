"""记忆服务：频道记忆的存储、检索与偏好分层。

## 写路径不再碰向量

四个写方法（store / edit / supersede / delete）只做两件事：改 `memory_entries`，
并在**同一事务**里往 `memory_outbox` 记一条「该重算向量」的意图。向量由 worker
里的 `MemoryProjector` 异步投影。

这么改消除的是一整类缺陷：改造前 `store()` 先提交记忆行、再调 embedding API，
中间崩溃或 API 失败就得到一条永远没有向量的记忆，而项目没有对账、无人发现。
`edit()` 更糟 —— 它在写库**之前**就重算了向量，崩溃后检索会按一份没人存的文本
命中。九个确认缺陷与完整设计见 `docs/plan-memory-outbox.md`。

读路径（`query_for_context` / `find_similar` / `_semantic_hits`）仍直接用
`vector_store` 与 `embedder`：检索是只读的，向量不可用时回落到时间倒序即可，
不需要经队列。所以这两样仍在构造参数里。


本服务只处理「结论」—— 值得跨会话记住的事实、决策与偏好。原始聊天消息不进
这里：它们先进 MessageWindow 滚动缓冲，由 MemoryDistiller 蒸馏后才可能变成
一条记忆。此前 router 把每条非 @ 消息直接塞进来，导致「收到」「好的」与真正
的项目背景知识并列存放，向量检索的信噪比被彻底稀释。

偏好（MemoryType.PREFERENCE）是普通记忆里的一类，没有独立表（合表改动）：
它不建向量、检索时由 query_for_context 全量带上，因为它是「怎么回答」的约束
而非「回答什么」的候选。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from teamai.domain.identity import gen_id
from teamai.domain.models import (
    AuditAction,
    MemoryEntry,
    MemorySource,
    MemoryType,
    OutboxOp,
    should_embed,
)
from teamai.domain.ports import Embedder, UnitOfWork
from teamai.domain.repositories import ChannelRepository, MemoryRepository, OutboxRepository
from teamai.domain.services import AuditLogWriter

logger = logging.getLogger(__name__)

# 向量检索不可用时的回落上限。取一个小值而非「全部」：这是喂给模型的上下文，
# 多了既烧 token 又冲淡真正相关的那几条。
FALLBACK_LIMIT = 20

# 冲突检查向向量库要多少条候选。比检索的 top_k=5 小：这些要摊给人一条条看，
# 给多了没人看得完，而阈值本来就该把不相干的滤掉。
CONFLICT_TOP_K = 5

# 文本兜底比对时忽略的字符：空白与常见中英标点。
# ⚠️ 刻意不含数字与单位 —— 「超时 3 秒」与「超时 5 秒」的差别全在那个数字上，
# 归一化掉它们会把真正的矛盾判成字面相同，那正是这道检查要抓的东西。
_NOISE = re.compile(r"[\s，。、；：！？「」『』（）,.;:!?\"'()\[\]{}—\-_/\\|]+")


def _normalize(text: str) -> str:
    """归一化文本用于字面比对。见 `_NOISE` 的说明。"""
    return _NOISE.sub("", text).lower()


@dataclass
class MemoryConflict:
    """一条疑似与待写入内容冲突的现行记忆。"""

    entry: MemoryEntry
    # 余弦相似度。文本兜底路径下为 None —— 那时判据是字面重复，报一个假的
    # 相似度会让人以为向量检查生效了，而它恰恰没生效。
    score: float | None


@dataclass
class ConflictCheck:
    """冲突检查的结果。

    `degraded` 必须与冲突列表一起返回，不能只看 `conflicts` 是否为空：
    「没查到冲突」与「查不了冲突」在调用方看来长得一样，而后者要告诉录入人
    ——「未配 embedding，只能查出字面重复」。把这个区分留给调用方自己判
    `_vector_ready`，就等于让它重新实现一遍本方法的判断。
    """

    conflicts: list[MemoryConflict] = field(default_factory=list)
    degraded: bool = False

    def __bool__(self) -> bool:
        return bool(self.conflicts)


class MemoryService:
    def __init__(
        self,
        repo: MemoryRepository,
        channel_repo: ChannelRepository,
        audit: AuditLogWriter,
        outbox: OutboxRepository,
        uow: UnitOfWork,
        vector_store=None,
        embedder: Embedder | None = None,
        *,
        conflict_threshold: float = 0.85,
        conflict_scan_limit: int = 50,
    ) -> None:
        self._repo = repo
        self._channel_repo = channel_repo
        self._audit = audit
        self._outbox = outbox
        self._uow = uow
        self._vector = vector_store
        self._embedder = embedder
        # 默认值与 config.py 的同名配置项一致，真值由容器注入。给默认是为了让
        # 测试与窄装配不必每次都传 —— 但两处的数字必须一样，不然「改了配置没生效」
        # 会取决于是谁构造的。
        self._conflict_threshold = conflict_threshold
        self._conflict_scan_limit = conflict_scan_limit

    @property
    def _vector_ready(self) -> bool:
        """向量检索是否真的可用。

        必须同时有向量库与**可用的** embedder。此前只判两者非 None，而组合根
        从未注入 embedder，于是这个条件恒为假、向量路径从未运行过 —— 缺陷被
        「静默降级」的写法藏了很久。现在未配置凭据时装的是 NullEmbedder，
        它显式报 available=False，装配缺失与凭据缺失都能在日志里看出来。
        """
        return self._vector is not None and self._embedder is not None and self._embedder.available


    async def store(
        self,
        channel_instance_id: str,
        content: str,
        source_user_id: str | None = None,
        *,
        type: MemoryType = MemoryType.BACKGROUND_KNOWLEDGE,
        source: MemorySource = MemorySource.MANUAL,
        action: AuditAction = AuditAction.MEMORY_STORE,
    ) -> MemoryEntry:
        """写入一条记忆。

        `action` 与 `source` 都可覆盖成蒸馏用的值：人工写入与系统蒸馏既要在
        审计里能区分（action），也要在记忆本身上能区分（source）—— 只有前者的话，
        排查「这条是谁写的」得去翻审计流水。

        向量不在这里写：入队一条 UPSERT，由 projector 异步投影。所以本方法返回
        后 `entry.embedding_ref` 仍是 None —— 调用方不该依赖它，「有没有向量」是
        暂态，由对账保证最终收敛。
        """
        async with self._uow:
            entry = MemoryEntry(
                id=gen_id("mem"),
                channel_instance_id=channel_instance_id,
                content=content,
                type=type,
                source_user_id=source_user_id,
                source=source,
            )
            await self._repo.store(entry)
            # 偏好不建向量，连队都不入 —— 入了 projector 也只会判「不该有向量」
            # 然后删一次，白跑一轮。
            if should_embed(entry.type):
                await self._outbox.enqueue(entry.id, OutboxOp.UPSERT)
            await self._audit.record(
                channel_instance_id,
                action,
                user_id=source_user_id,
                detail={"entry_id": entry.id, "type": type.value, "source": source.value},
            )
        return entry

    async def edit(
        self,
        entry_id: str,
        *,
        content: str | None = None,
        type: MemoryType | None = None,
        actor: str | None = None,
    ) -> MemoryEntry | None:
        """原地修改一条记忆。不存在则返回 None（调用方转 404）。

        与「删一条 + 建一条」的区别不只是省一次调用：那样做 id 会变、
        `created_at` 被重置，审计里也看不出是同一条的演进。

        与 `supersede()` 的分工：本方法是「这条记忆写错了」（人工修正笔误、
        补充措辞），改完仍是同一条事实；supersede 是「事实变了」（超时从 3 秒
        改成 5 秒），旧的那条曾经成立、留着可查。混用会丢失后者的历史。
        """
        # 读在事务外：不存在时直接返回，不必为一次空查询开一个事务再提交。
        entry = await self._repo.get(entry_id)
        if entry is None:
            return None

        async with self._uow:
            old_content, old_type = entry.content, entry.type
            content_changed = content is not None and content != old_content

            if content is not None:
                entry.content = content
            if type is not None:
                entry.type = type
            entry.source = entry.edited()

            # 只改 type 也要入队：向量该不该存在由 should_embed 决定，而它读的正是
            # type。普通记忆改成偏好要删旧向量（否则残留向量白占 top_k 名额），
            # 偏好改回普通记忆要补建（它当初跳过了建索引，不补就永远只能靠时间
            # 倒序回落偶然捞到）。两个方向都由 projector 按当前状态自行决定，
            # 这里只负责「告诉它这条变了」。
            embed_changed = should_embed(entry.type) is not should_embed(old_type)
            if content_changed or embed_changed:
                # ⚠️ 顺序与改造前相反且这很重要：改造前是先重算向量、后写库，
                # 崩溃后检索会按一份没人存的文本命中。现在两者同事务，且投影
                # 发生在提交之后 —— projector 回读到的必然是已落库的内容。
                op = OutboxOp.UPSERT if should_embed(entry.type) else OutboxOp.DELETE
                await self._outbox.enqueue(entry.id, op)

            await self._repo.update(entry)
            await self._audit.record(
                entry.channel_instance_id,
                AuditAction.MEMORY_EDIT,
                user_id=actor,
                detail={
                    "entry_id": entry.id,
                    # 只留摘要不留全文：审计表不该变成内容的第二份副本
                    "old_content": old_content[:50],
                    "content_changed": content_changed,
                    "old_type": old_type.value,
                    "new_type": entry.type.value,
                    "source": entry.source.value,
                },
            )
        return entry

    async def supersede(
        self,
        old_entry_id: str,
        channel_instance_id: str,
        content: str,
        *,
        type: MemoryType = MemoryType.BACKGROUND_KNOWLEDGE,
        source: MemorySource = MemorySource.DISTILLED,
        action: AuditAction = AuditAction.MEMORY_DISTILL,
    ) -> MemoryEntry | None:
        """用新内容取代一条既有记忆：写新条目 + 给旧条目打取代标记。

        返回新条目；旧 id 不存在时返回 None（调用方应降级为纯 store —— 见
        MemoryDistiller._apply_actions 里的说明）。

        改造前这里是**四个独立提交**（写新 → 标旧 → 删旧向量 → 再写一次清
        embedding_ref），崩在中间会留下两种坏状态：新条目已存在而旧条目仍是现行
        （同一事实两条并列），或旧条目已作废但向量还在（过期事实继续被命中）。
        当时的注释说「顺序就是唯一的保障手段」—— 现在不必了，整个操作在一个
        事务里，要么全落要么全不落。

        旧条目**不删**：这正是 superseded_by 存在的意义，见 MemoryEntry 的
        字段注释。
        """
        # 读与校验在事务外：两条早退路径都不该开事务。
        old = await self._repo.get(old_entry_id)
        if old is None:
            return None
        if old.channel_instance_id != channel_instance_id:
            # 跨频道取代一律拒绝。模型输出的 id 可能来自别的频道（提示词里给的
            # 候选虽只有本频道的，但模型会编 id），而放行等于让 A 频道的蒸馏
            # 结果改写 B 频道的记忆 —— 频道隔离是 Design-claude-tag.md §5 的
            # 正确性属性之一。
            logger.warning(
                f"拒绝跨频道取代：记忆 {old_entry_id} 属 {old.channel_instance_id}，"
                f"请求方是 {channel_instance_id}"
            )
            return None

        async with self._uow:
            # store() 内部也开一个工作单元，但 UnitOfWork 可重入 —— 内层退出是
            # no-op，只有这里的最外层提交。没有可重入性，store() 返回时就会把
            # 「新条目已写、旧条目还没标记」这个中间态提交出去。
            new_entry = await self.store(
                channel_instance_id,
                content,
                type=type,
                source=source,
                action=action,
            )

            old.supersede(new_entry.id)
            await self._repo.update(old)
            # 旧条目的向量要撤掉：留着它会让检索按已作废的事实命中，而 top_k
            # 名额有限。一个指向过期内容的向量比没有向量更糟。
            #
            # 这里只入队，不直接删：删向量是远程调用，放进事务等于让事务等一次
            # 网络往返；而入队之后 projector 会回读到「已被取代」并执行删除。
            # embedding_ref 也不在这里清 —— 由 projector 删成功后回填，那才是
            # 「向量真的没了」的时刻。改造前在这里清，于是删失败时库里说没有、
            # 实际还在，对账也就查不出来。
            await self._outbox.enqueue(old.id, OutboxOp.DELETE)

            await self._audit.record(
                channel_instance_id,
                AuditAction.MEMORY_EDIT,
                detail={
                    "action": "supersede",
                    "old_entry_id": old.id,
                    "new_entry_id": new_entry.id,
                    "old_content": old.content[:50],
                },
            )
        return new_entry

    async def find_similar(
        self, channel_instance_id: str, content: str, top_k: int
    ) -> list[MemoryEntry]:
        """按内容查该频道已有的近似记忆，供蒸馏时判断该 ADD 还是 UPDATE。

        与 `query_for_context` 的区别：那个的输入是用户的提问、结果要喂给模型
        当上下文，故一并带上全部偏好；这个的输入是刚蒸馏出的一条结论、结果是
        给模型看的「候选比对项」，同样要包含偏好 —— 偏好不建向量、`_semantic_hits`
        找不回它；若候选不含偏好，模型对「团队偏好」内容每窗口都判 ADD，
        偏好会确定性堆积。故偏好显式追加，按 id 去重。

        向量不可用时回落到时间倒序：宁可让模型比对最近若干条，也不要直接返回
        空 —— 空候选会让每条蒸馏结果都判成 ADD，去重完全失效。
        """
        hits = await self._semantic_hits(channel_instance_id, content, top_k)
        if not hits:
            hits = await self._repo.list_by_channel(
                channel_instance_id, limit=min(top_k, FALLBACK_LIMIT)
            )
        prefs = await self._repo.list_preferences(channel_instance_id)
        # 先切片再算 seen：反过来的话，被切掉那截里若有偏好，它的 id 已进 seen，
        # 于是既不在候选里也不会被追加 —— 静默丢失。当前两条来源都不超过 top_k、
        # 切片是空操作，但顺序不该依赖这个前提。
        hits = hits[:top_k]
        seen = {e.id for e in hits}
        hits.extend(p for p in prefs if p.id not in seen)
        return hits

    async def find_conflicts(
        self,
        channel_instance_id: str,
        content: str,
        *,
        type: MemoryType = MemoryType.BACKGROUND_KNOWLEDGE,
    ) -> ConflictCheck:
        """查该频道有没有现行记忆与将要写入的 `content` 疑似冲突。

        用于**手工写入**（Admin API）。蒸馏路径不用这个：那边由模型对整窗产出
        逐条给 ADD / UPDATE / NOOP，判断依据比这里丰富得多。这里只有孤零零一句
        待写入的话，凭它替人决定「这是新版本还是另一件事」是不够的 —— 所以本方法
        只返回材料，取代还是并列由录入人定（见 adapters/admin/memory.py 的 409）。

        与 `find_similar` 的区别，别把两者合并：那个为蒸馏服务，会把偏好无条件
        追加进候选、向量不可用时回落到「最近若干条」当候选 —— 两种行为在这里都是
        错的。追加的偏好没有分数，阈值对它们无从施加；而把「最近 10 条」原样当成
        冲突，等于对每次写入都报一堆不相干的条目。

        **偏好不参与**（`type is PREFERENCE` 时直接返回空）：偏好按 `should_embed`
        不建向量，语义检查对它结构性无效 —— 走向量路径会查出零条，那会被读成
        「没有冲突」，而真相是「没能力查」。要覆盖偏好得换机制（字面匹配或一次小
        模型调用），记在 docs/tasklist.md 22.4。

        向量不可用时（默认装配就是 `NullEmbedder`）退化为字面比对，并置
        `degraded=True`。这条路只能查出字面重复、查不出语义矛盾，但比放行强 ——
        「静默什么都不做」正是这个项目反复踩的那类缺陷。
        """
        if type is MemoryType.PREFERENCE:
            return ConflictCheck()

        if self._vector_ready:
            return await self._semantic_conflicts(channel_instance_id, content)
        return await self._text_conflicts(channel_instance_id, content)

    async def _semantic_conflicts(self, channel_instance_id: str, content: str) -> ConflictCheck:
        try:
            embedding = await self._embedder.embed(content)  # type: ignore[union-attr]
            if not embedding:
                # embed 返回空（凭据失效、服务降级）与「没配 embedder」是同一种
                # 处境，走同一条兜底 —— 否则这里会静默放行。
                return await self._text_conflicts(channel_instance_id, content)
            scored = await self._vector.query(channel_instance_id, embedding, CONFLICT_TOP_K)
        except Exception as exc:
            logger.warning(f"冲突检查的向量查询失败，退化为字面比对: {exc}")
            return await self._text_conflicts(channel_instance_id, content)

        out: list[MemoryConflict] = []
        for entry_id, score in scored:
            if score < self._conflict_threshold:
                # Qdrant 已按分数降序，本可以 break —— 用 continue 是因为「降序」
                # 是它的行为而非本方法的前提，换了向量库或加了重排就不成立。
                continue
            entry = await self._repo.get(entry_id)
            # 已被取代的不算冲突：它已经不是现行事实，拿它问人「要不要取代」
            # 是个没有意义的问题。残留向量在投影追上前会命中，同 _semantic_hits。
            if entry is None or not entry.is_current or not should_embed(entry.type):
                continue
            out.append(MemoryConflict(entry=entry, score=score))
        return ConflictCheck(conflicts=out)

    async def _text_conflicts(self, channel_instance_id: str, content: str) -> ConflictCheck:
        """字面比对兜底：归一化后互为子串即算疑似冲突。

        双向包含都算：录入人可能在补充一条已有记忆（新内容更长），也可能在写一条
        更精简的表述（新内容更短）。只判一个方向会漏掉另一半。
        """
        target = _normalize(content)
        if not target:
            return ConflictCheck(degraded=True)
        recent = await self._repo.list_by_channel(
            channel_instance_id,
            limit=self._conflict_scan_limit,
            exclude_type=MemoryType.PREFERENCE,
        )
        out = [
            MemoryConflict(entry=e, score=None)
            for e in recent
            if (norm := _normalize(e.content)) and (norm in target or target in norm)
        ]
        return ConflictCheck(conflicts=out, degraded=True)

    async def query_for_context(
        self, channel_instance_id: str, query: str, top_k: int = 5
    ) -> list[MemoryEntry]:
        """面向 Agent 上下文的记忆检索：语义命中 + 该频道全部偏好。

        检索分两段，互不抢名额：
        - **语义段**：与问题相关的现行记忆（`_semantic_hits` 已排除偏好；向量
          不可用时按时间倒序回落，同样用 `exclude_type` 排掉偏好）。
        - **偏好段**：该频道全部现行偏好，无条件全量带上。偏好是「怎么回答」的
          约束（语气、格式、禁忌），与当前问题的语义相关度无关 —— 按相似度筛
          会让偏好在问到无关话题时失效。

        偏好渲染只在返回侧加 `偏好(U1): ` 前缀、不污染存储 content：写进内容
        会让未来蒸馏比对「偏好(…)」开头的文本永远匹配不进旧偏好，去重失效。
        `source_user_id` 为 None（蒸馏偏好的常态）时不加前缀。
        """
        hits = await self._semantic_hits(channel_instance_id, query, top_k)
        if not hits:
            # 回落：按时间倒序取最近若干条，有界、并排除偏好。此前这里是无
            # ORDER BY、无 LIMIT 的全表查询再在 Python 侧切片，等于随机取样，
            # 且随频道使用时长线性变慢。
            hits = await self._repo.list_by_channel(
                channel_instance_id,
                limit=min(top_k, FALLBACK_LIMIT),
                exclude_type=MemoryType.PREFERENCE,
            )

        prefs = await self._repo.list_preferences(channel_instance_id)
        result = hits[:top_k]
        result.extend(
            MemoryEntry(
                id=p.id,
                channel_instance_id=p.channel_instance_id,
                content=(
                    f"偏好({p.source_user_id}): {p.content}"
                    if p.source_user_id
                    else p.content
                ),
                type=MemoryType.PREFERENCE,
                source_user_id=p.source_user_id,
                source=p.source,
                # ⚠️ created_at 必须显式传。它有 default_factory=_utcnow，漏了就
                # 每条偏好都渲染成「今天」—— 而 ContextBundle.memory_context 把
                # 日期作为矛盾记忆的裁决依据，偏好恰恰是最容易前后冲突的一类
                # （同一个人改了主意，两条都是现行）。全都标今天等于把裁决依据
                # 变成噪声，且这种错看起来完全正常，不会有任何报错。
                created_at=p.created_at,
            )
            for p in prefs
        )
        return result

    async def _semantic_hits(
        self, channel_instance_id: str, query: str, top_k: int
    ) -> list[MemoryEntry]:
        if not self._vector_ready:
            return []
        try:
            embedding = await self._embedder.embed(query)  # type: ignore[union-attr]
            if not embedding:
                return []
            # 分数在这条路上没用，显式丢掉。它是给 find_conflicts 的 —— 那里要按
            # 阈值判「像到该拦下手工写入」，而检索只关心排序，Qdrant 已按分数降序。
            scored = await self._vector.query(channel_instance_id, embedding, top_k)
            ids = [eid for eid, _score in scored]
        except Exception as exc:  # pragma: no cover - 向量服务异常时降级
            logger.warning(f"向量检索失败，回落到时间倒序: {exc}")
            return []
        entries = [await self._repo.get(eid) for eid in ids]
        # 过滤已被取代的、以及偏好：被取代条目在 supersede 时会入队删向量，但
        # 投影是异步的 —— 从提交到 projector 处理完之间有个窗口（目标 p99 < 5
        # 秒），期间残留向量仍会命中。这里兜一道，避免过期事实在那几秒里流回
        # 上下文。改造前这道过滤兜的是「删向量失败只打 warning」，现在兜的是
        # 投影延迟；两者都需要它，理由不同。
        #
        # 偏好按 `should_embed` 本不该有向量，这里仍要滤：生产库可能残留合表前
        # 蒸馏写下的 PREFERENCE 向量，或投影尚未追上的残留 —— 不滤的话它占着
        # top_k 名额，还与偏好段重复。这是对向量库实际状态的兜底，不是判定。
        return [e for e in entries if e is not None and e.is_current and should_embed(e.type)]

    async def list(
        self,
        channel_instance_id: str,
        limit: int | None = 200,
        *,
        current_only: bool = True,
    ) -> list[MemoryEntry]:
        """列出记忆，默认有上界（控制台分页用）。

        `current_only=False` 给控制台排查历史用 —— 「这条事实之前是什么」只有
        连被取代的一起列出来才看得到。
        """
        return await self._repo.list_by_channel(
            channel_instance_id, limit=limit, current_only=current_only
        )

    async def delete(self, entry_id: str, actor: str | None = None) -> None:
        entry = await self._repo.get(entry_id)
        if entry is None:
            return
        async with self._uow:
            await self._repo.delete(entry_id)
            # 入队删向量。⚠️ 必须与删行同事务：行删掉后 projector 回读为空，
            # 那正是「删向量」的信号（见 plan-memory-outbox.md §5.2）。若这条
            # 入队丢了，向量会永远留在库里白占 top_k 名额 —— 改造前就是这样，
            # `_drop_vector` 失败只打一条 warning。
            await self._outbox.enqueue(entry_id, OutboxOp.DELETE)
            await self._audit.record(
                entry.channel_instance_id,
                AuditAction.MEMORY_DELETE,
                user_id=actor,
                detail={"entry_id": entry_id},
            )

