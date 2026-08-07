"""记忆服务：频道记忆存储/检索、偏好管理、跨频道授权检查。"""

from __future__ import annotations

from teamai.domain.identity import gen_id
from teamai.domain.models import AuditAction, MemoryEntry, MemoryType, Preference
from teamai.domain.repositories import ChannelRepository, MemoryRepository
from teamai.domain.services import AuditLogWriter


class MemoryService:
    def __init__(
        self,
        repo: MemoryRepository,
        channel_repo: ChannelRepository,
        audit: AuditLogWriter,
        vector_store=None,
        embedder=None,
    ) -> None:
        self._repo = repo
        self._channel_repo = channel_repo
        self._audit = audit
        self._vector = vector_store
        self._embedder = embedder

    async def store(self, channel_instance_id: str, content: str, source_user_id: str | None = None) -> MemoryEntry:
        entry = MemoryEntry(
            id=gen_id("mem"),
            channel_instance_id=channel_instance_id,
            content=content,
            type=MemoryType.BACKGROUND_KNOWLEDGE,
            source_user_id=source_user_id,
        )
        await self._repo.store(entry)
        await self._embed_if_available(entry)
        await self._audit.record(
            channel_instance_id,
            AuditAction.MEMORY_STORE,
            user_id=source_user_id,
            detail={"entry_id": entry.id},
        )
        return entry

    async def set_preference(self, channel_instance_id: str, user_id: str, preference: str) -> Preference:
        pref = Preference(
            id=gen_id("pref"),
            channel_instance_id=channel_instance_id,
            user_id=user_id,
            preference=preference,
        )
        await self._repo.set_preference(pref)
        return pref

    async def query_for_context(self, channel_instance_id: str, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """面向 Agent 上下文的记忆检索：向量命中 + 该频道全部偏好。"""
        hits: list[MemoryEntry] = []
        if self._vector is not None and self._embedder is not None:
            try:
                embedding = await self._embedder(query)
                ids = await self._vector.query(channel_instance_id, embedding, top_k)
                for eid in ids:
                    entry = await self._repo.get(eid)
                    if entry is not None:
                        hits.append(entry)
            except Exception:  # pragma: no cover - 向量服务异常时降级
                hits = []
        if not hits:
            hits = await self._repo.list_by_channel(channel_instance_id)
        prefs = await self._repo.list_preferences(channel_instance_id)
        for p in prefs:
            hits.append(
                MemoryEntry(
                    id=p.id,
                    channel_instance_id=channel_instance_id,
                    content=f"偏好({p.user_id}): {p.preference}",
                    type=MemoryType.PREFERENCE,
                    source_user_id=p.user_id,
                )
            )
        return hits[: max(top_k + len(prefs), top_k)]

    async def list(self, channel_instance_id: str) -> list[MemoryEntry]:
        return await self._repo.list_by_channel(channel_instance_id)

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
        if self._vector is None or self._embedder is None:
            return
        try:
            embedding = await self._embedder(entry.content)
            await self._vector.upsert(entry, embedding)
        except Exception:  # pragma: no cover
            pass
