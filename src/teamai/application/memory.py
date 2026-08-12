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
        action: AuditAction = AuditAction.MEMORY_STORE,
    ) -> MemoryEntry:
        """写入一条记忆。

        `action` 可覆盖成 MEMORY_DISTILL：人工写入与系统蒸馏在审计里要能区分，
        否则排查「记忆库里怎么会有这条」时无从下手。
        """
        entry = MemoryEntry(
            id=gen_id("mem"),
            channel_instance_id=channel_instance_id,
            content=content,
            type=type,
            source_user_id=source_user_id,
            visibility=visibility,
        )
        await self._repo.store(entry)
        await self._embed_if_available(entry)
        await self._audit.record(
            channel_instance_id,
            action,
            user_id=source_user_id,
            detail={"entry_id": entry.id, "type": type.value},
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
        await self._audit.record(
            entry.channel_instance_id,
            AuditAction.MEMORY_DELETE,
            user_id=actor,
            detail={"entry_id": entry_id},
        )

    async def _embed_if_available(self, entry: MemoryEntry) -> None:
        if not self._vector_ready:
            return
        try:
            embedding = await self._embedder.embed(entry.content)  # type: ignore[union-attr]
            if embedding:
                await self._vector.upsert(entry, embedding)
        except Exception as exc:  # pragma: no cover
            # 写向量失败不回滚记忆：条目本身已落库，检索时会经时间倒序的回落
            # 路径拿到它。反过来（为向量失败而丢弃记忆）损失更大。
            logger.warning(f"记忆 {entry.id} 向量写入失败: {exc}")
