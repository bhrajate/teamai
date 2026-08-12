"""记忆服务：频道记忆存储/检索、偏好管理。

本服务只处理「结论」—— 值得跨会话记住的事实、决策与偏好。原始聊天消息不进
这里：它们先进 MessageWindow 滚动缓冲，由 MemoryDistiller 蒸馏后才可能变成
一条记忆。此前 router 把每条非 @ 消息直接塞进来，导致「收到」「好的」与真正
的项目背景知识并列存放，向量检索的信噪比被彻底稀释。
"""

from __future__ import annotations

import logging

from teamai.domain.identity import gen_id
from teamai.domain.models import (
    AuditAction,
    MemoryEntry,
    MemorySource,
    MemoryType,
    Preference,
    Visibility,
)
from teamai.domain.ports import Embedder
from teamai.domain.repositories import ChannelRepository, MemoryRepository
from teamai.domain.services import AuditLogWriter

logger = logging.getLogger(__name__)

# 向量检索不可用时的回落上限。取一个小值而非「全部」：这是喂给模型的上下文，
# 多了既烧 token 又冲淡真正相关的那几条。
FALLBACK_LIMIT = 20


class MemoryService:
    def __init__(
        self,
        repo: MemoryRepository,
        channel_repo: ChannelRepository,
        audit: AuditLogWriter,
        vector_store=None,
        embedder: Embedder | None = None,
    ) -> None:
        self._repo = repo
        self._channel_repo = channel_repo
        self._audit = audit
        self._vector = vector_store
        self._embedder = embedder

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
        visibility: Visibility = Visibility.CHANNEL,
        source: MemorySource = MemorySource.MANUAL,
        action: AuditAction = AuditAction.MEMORY_STORE,
    ) -> MemoryEntry:
        """写入一条记忆。

        `action` 与 `source` 都可覆盖成蒸馏用的值：人工写入与系统蒸馏既要在
        审计里能区分（action），也要在记忆本身上能区分（source）—— 只有前者的话，
        排查「这条是谁写的」得去翻审计流水。
        """
        entry = MemoryEntry(
            id=gen_id("mem"),
            channel_instance_id=channel_instance_id,
            content=content,
            type=type,
            source_user_id=source_user_id,
            source=source,
            visibility=visibility,
        )
        await self._repo.store(entry)
        await self._embed_if_available(entry)
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

        `visibility` 不在可改字段里 —— 把 private 改成 channel 等于把本不该进
        频道记忆的内容放出去，属权限变更而非内容编辑，应走独立的授权路径。
        """
        entry = await self._repo.get(entry_id)
        if entry is None:
            return None

        old_content, old_type = entry.content, entry.type
        content_changed = content is not None and content != old_content

        if content is not None:
            entry.content = content
        if type is not None:
            entry.type = type
        entry.source = entry.edited()

        if content_changed:
            # 内容变了必须重算向量：旧向量对应旧文本，不重算就等于「按旧内容
            # 命中新条目」。重算失败时**删掉**旧向量而不是留着 —— 留着比没有
            # 更糟，检索会持续按已被改掉的内容命中它。
            entry.embedding_ref = await self._reembed(entry)

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

    async def set_preference(
        self, channel_instance_id: str, user_id: str, preference: str
    ) -> Preference:
        pref = Preference(
            id=gen_id("pref"),
            channel_instance_id=channel_instance_id,
            user_id=user_id,
            preference=preference,
        )
        await self._repo.set_preference(pref)
        return pref

    async def query_for_context(
        self, channel_instance_id: str, query: str, top_k: int = 5
    ) -> list[MemoryEntry]:
        """面向 Agent 上下文的记忆检索：语义命中 + 该频道全部偏好。

        偏好不参与向量检索、一律全带上：它们是「怎么回答」的约束（语气、格式、
        禁忌），与当前问题的语义相关度无关 —— 按相似度筛会让偏好在问到无关话题
        时失效。
        """
        hits = await self._semantic_hits(channel_instance_id, query, top_k)
        if not hits:
            # 回落：按时间倒序取最近若干条，有界。此前这里是无 ORDER BY、
            # 无 LIMIT 的全表查询再在 Python 侧切片，等于随机取样，且随频道
            # 使用时长线性变慢。
            hits = await self._repo.list_by_channel(
                channel_instance_id, limit=min(top_k, FALLBACK_LIMIT)
            )

        prefs = await self._repo.list_preferences(channel_instance_id)
        result = hits[:top_k]
        result.extend(
            MemoryEntry(
                id=p.id,
                channel_instance_id=channel_instance_id,
                content=f"偏好({p.user_id}): {p.preference}",
                type=MemoryType.PREFERENCE,
                source_user_id=p.user_id,
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
            ids = await self._vector.query(channel_instance_id, embedding, top_k)
        except Exception as exc:  # pragma: no cover - 向量服务异常时降级
            logger.warning(f"向量检索失败，回落到时间倒序: {exc}")
            return []
        entries = [await self._repo.get(eid) for eid in ids]
        return [e for e in entries if e is not None]

    async def list(self, channel_instance_id: str, limit: int | None = 200) -> list[MemoryEntry]:
        """列出记忆，默认有上界（控制台分页用）。"""
        return await self._repo.list_by_channel(channel_instance_id, limit=limit)

    async def delete(self, entry_id: str, actor: str | None = None) -> None:
        entry = await self._repo.get(entry_id)
        if entry is None:
            return
        await self._repo.delete(entry_id)
        # 同步清向量。不清的话向量留在库里继续被检索命中，随后因取不到实体
        # 而被过滤 —— 不会泄露已删内容，但白占 top_k 名额，删多了检索质量
        # 静默下降。改造前没有这一步，缺陷被「向量路径从未运行」掩盖着。
        await self._drop_vector(entry_id)
        await self._audit.record(
            entry.channel_instance_id,
            AuditAction.MEMORY_DELETE,
            user_id=actor,
            detail={"entry_id": entry_id},
        )

    async def _drop_vector(self, entry_id: str) -> None:
        """删向量。失败只告警：Postgres 行是权威源，它已经删了。

        为向量库不可用而让整个删除失败是更糟的结果 —— 用户点了删除却报错，
        而数据其实已经没了。与 _embed_if_available 的降级取舍一致。
        """
        if self._vector is None:
            return
        try:
            await self._vector.delete(entry_id)
        except Exception as exc:  # pragma: no cover - 外部服务不可用
            logger.warning(f"记忆 {entry_id} 的向量删除失败，索引里可能残留: {exc}")

    async def _reembed(self, entry: MemoryEntry) -> str | None:
        """重算向量，返回新的 embedding_ref。

        失败时删掉旧向量并返回 None：见 edit() 里的说明 —— 留着旧向量会让
        检索按已被改掉的内容命中它，比没有索引更糟。
        """
        if not self._vector_ready:
            await self._drop_vector(entry.id)
            return None
        try:
            embedding = await self._embedder.embed(entry.content)  # type: ignore[union-attr]
            if embedding:
                return await self._vector.upsert(entry, embedding)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"记忆 {entry.id} 向量重算失败: {exc}")
        await self._drop_vector(entry.id)
        return None

    async def _embed_if_available(self, entry: MemoryEntry) -> None:
        """建索引并把引用回填到 entry.embedding_ref。

        回填之后「哪些记忆已建索引」才可查 —— 改造前这个字段声明了、mapper
        两侧也在传，但没有任何代码写入它，于是
        scripts/cleanup_chat_memories.py 里 `embedding_ref IS NULL` 恒为真。
        """
        if not self._vector_ready:
            return
        try:
            embedding = await self._embedder.embed(entry.content)  # type: ignore[union-attr]
            if not embedding:
                return
            ref = await self._vector.upsert(entry, embedding)
        except Exception as exc:  # pragma: no cover
            # 写向量失败不回滚记忆：条目本身已落库，检索时会经时间倒序的回落
            # 路径拿到它。反过来（为向量失败而丢弃记忆）损失更大。
            logger.warning(f"记忆 {entry.id} 向量写入失败: {exc}")
            return
        if ref:
            entry.embedding_ref = ref
            try:
                await self._repo.update(entry)
            except Exception as exc:  # pragma: no cover
                # 回填失败只影响「能否查出漏索引的条目」，向量本身已经写进去了
                logger.warning(f"记忆 {entry.id} 的 embedding_ref 回填失败: {exc}")
