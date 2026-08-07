"""MemoryRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.memory import MemoryEntry, Preference
from teamai.domain.repositories.memory import MemoryRepository
from teamai.infrastructure.orm.memory import MemoryEntryModel, PreferenceModel


def _memory_to_model(e: MemoryEntry) -> MemoryEntryModel:
    return MemoryEntryModel(
        id=e.id,
        channel_instance_id=e.channel_instance_id,
        content=e.content,
        type=e.type,
        source_user_id=e.source_user_id,
        visibility=e.visibility,
        embedding_ref=e.embedding_ref,
        created_at=e.created_at,
    )


def _model_to_memory(m: MemoryEntryModel) -> MemoryEntry:
    return MemoryEntry(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        content=m.content,
        type=m.type,
        source_user_id=m.source_user_id,
        visibility=m.visibility,
        embedding_ref=m.embedding_ref,
        created_at=m.created_at,
    )


def _preference_to_model(p: Preference) -> PreferenceModel:
    return PreferenceModel(
        id=p.id,
        channel_instance_id=p.channel_instance_id,
        user_id=p.user_id,
        preference=p.preference,
        created_at=p.created_at,
    )


def _model_to_preference(m: PreferenceModel) -> Preference:
    return Preference(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        user_id=m.user_id,
        preference=m.preference,
        created_at=m.created_at,
    )


class SQLMemoryRepository(MemoryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def store(self, entry: MemoryEntry) -> None:
        self._session.add(_memory_to_model(entry))

    async def list_by_channel(self, channel_instance_id: str) -> list[MemoryEntry]:
        stmt = select(MemoryEntryModel).where(MemoryEntryModel.channel_instance_id == channel_instance_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_memory(r) for r in rows]

    async def get(self, entry_id: str) -> MemoryEntry | None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        return _model_to_memory(m) if m else None

    async def delete(self, entry_id: str) -> None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        if m:
            await self._session.delete(m)

    async def set_preference(self, pref: Preference) -> None:
        self._session.add(_preference_to_model(pref))

    async def list_preferences(self, channel_instance_id: str) -> list[Preference]:
        stmt = select(PreferenceModel).where(PreferenceModel.channel_instance_id == channel_instance_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_preference(r) for r in rows]
