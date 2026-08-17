"""MemoryRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.models.memory import MemoryEntry, MemoryType
from teamai.domain.repositories.memory import MemoryRepository
from teamai.infrastructure.orm.memory import MemoryEntryModel


def _memory_to_model(e: MemoryEntry) -> MemoryEntryModel:
    return MemoryEntryModel(
        id=e.id,
        channel_instance_id=e.channel_instance_id,
        content=e.content,
        type=e.type,
        source_user_id=e.source_user_id,
        source=e.source,
        embedding_ref=e.embedding_ref,
        superseded_by=e.superseded_by,
        superseded_at=e.superseded_at,
        created_at=e.created_at,
    )


def _model_to_memory(m: MemoryEntryModel) -> MemoryEntry:
    return MemoryEntry(
        id=m.id,
        channel_instance_id=m.channel_instance_id,
        content=m.content,
        type=m.type,
        source_user_id=m.source_user_id,
        source=m.source,
        embedding_ref=m.embedding_ref,
        superseded_by=m.superseded_by,
        superseded_at=m.superseded_at,
        created_at=m.created_at,
    )


class SQLMemoryRepository(MemoryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def store(self, entry: MemoryEntry) -> None:
        # 提交理由见 SQLTaskRepository 的类说明。
        self._session.add(_memory_to_model(entry))
        await self._session.commit()

    async def list_by_channel(
        self,
        channel_instance_id: str,
        limit: int | None = None,
        *,
        current_only: bool = True,
        exclude_type: MemoryType | None = None,
    ) -> list[MemoryEntry]:
        """按 created_at 倒序返回，limit 为 None 时全量。

        ⚠️ ORDER BY 不可省。此前这里既无排序也无上限，而调用方
        （MemoryService.query_for_context）在 Python 侧切前 5 条当检索结果 ——
        行序由数据库自行决定，等于随机取样；且随频道使用时长线性变慢，
        因为每次都要把该频道全部记忆读进进程内存。
        """
        stmt = select(MemoryEntryModel).where(
            MemoryEntryModel.channel_instance_id == channel_instance_id
        )
        if current_only:
            stmt = stmt.where(MemoryEntryModel.superseded_by.is_(None))
        if exclude_type is not None:
            stmt = stmt.where(MemoryEntryModel.type != exclude_type)
        stmt = stmt.order_by(MemoryEntryModel.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_memory(r) for r in rows]

    async def get(self, entry_id: str) -> MemoryEntry | None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        return _model_to_memory(m) if m else None

    async def update(self, entry: MemoryEntry) -> None:
        """原地更新。走 merge 而非 add：后者对已存在的主键会撞唯一约束。

        ⚠️ merge 按主键匹配，所以传进来的 entry 必须带原 id ——
        换个新 id 就是 INSERT 而不是 UPDATE（`budget_quotas` 上踩过，
        表现是「改完读回的还是旧行」）。
        """
        await self._session.merge(_memory_to_model(entry))
        await self._session.commit()

    async def delete(self, entry_id: str) -> None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        if m:
            await self._session.delete(m)
            await self._session.commit()

    async def list_preferences(self, channel_instance_id: str) -> list[MemoryEntry]:
        """该频道现行偏好：type='PREFERENCE' 且未被取代，按 created_at 倒序。

        检索时偏好是被全量带上的固定上下文（query_for_context / find_similar），
        不是 top_k 竞争的候选，故不建向量、不走语义路径，就这一条查询。
        """
        stmt = (
            select(MemoryEntryModel)
            .where(
                MemoryEntryModel.channel_instance_id == channel_instance_id,
                MemoryEntryModel.type == MemoryType.PREFERENCE,
                MemoryEntryModel.superseded_by.is_(None),
            )
            .order_by(MemoryEntryModel.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_model_to_memory(r) for r in rows]