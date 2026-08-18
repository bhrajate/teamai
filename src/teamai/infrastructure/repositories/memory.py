"""MemoryRepository 的 SQLAlchemy 实现。"""

from __future__ import annotations

from sqlalchemy import func, or_, select
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
        embedded_hash=e.embedded_hash,
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
        embedded_hash=m.embedded_hash,
        superseded_by=m.superseded_by,
        superseded_at=m.superseded_at,
        created_at=m.created_at,
    )


class SQLMemoryRepository(MemoryRepository):
    """⚠️ 本仓储**不提交事务**，边界由 `UnitOfWork` 管理（用例层声明）。

    写方法用 `flush()` 而非 `commit()`：flush 把 SQL 发到数据库，于是同一
    session 内的后续读能看到这次写入（`supersede` 依赖这一点 —— 它写完新条目
    紧接着要按 id 读旧条目）；但它不结束事务，所以整组写入仍能被一起回滚。

    改造前每个写方法各自 `commit()`，于是「写记忆」与「记下该建向量的意图」是
    两次独立提交，中间崩溃就丢掉后者。理由与完整设计见
    `docs/plan-memory-outbox.md` §5.5。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def store(self, entry: MemoryEntry) -> None:
        self._session.add(_memory_to_model(entry))
        await self._session.flush()

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
        await self._session.flush()

    async def delete(self, entry_id: str) -> None:
        m = await self._session.get(MemoryEntryModel, entry_id)
        if m:
            await self._session.delete(m)
            await self._session.flush()

    async def find_vector_drift(self, limit: int) -> tuple[list[str], list[str]]:
        """见 MemoryRepository.find_vector_drift 的契约说明。

        ⚠️ `md5()` 是 Postgres 内置函数，SQLite 没有。单测给 SQLite 连接注册了
        一个同名实现（`tests/unit/test_reconciler.py`），端到端在真 Postgres 上
        由 `scripts/verify_outbox_flow.py` 验。

        `md5()` 按数据库编码算，而 `projector.content_hash()` 按 UTF-8 算 ——
        两者一致的前提是库是 UTF-8（本项目的 Postgres 镜像默认如此）。若部署到
        非 UTF-8 的库，对账会把所有行判成「需重算」，症状是补出的条数持续等于
        总行数。
        """
        missing_stmt = (
            select(MemoryEntryModel.id)
            .where(
                MemoryEntryModel.type != MemoryType.PREFERENCE,
                MemoryEntryModel.superseded_by.is_(None),
                or_(
                    MemoryEntryModel.embedding_ref.is_(None),
                    MemoryEntryModel.embedded_hash.is_distinct_from(
                        func.md5(MemoryEntryModel.content)
                    ),
                ),
            )
            .order_by(MemoryEntryModel.created_at)
            .limit(limit)
        )
        stale_stmt = (
            select(MemoryEntryModel.id)
            .where(
                or_(
                    MemoryEntryModel.type == MemoryType.PREFERENCE,
                    MemoryEntryModel.superseded_by.is_not(None),
                ),
                MemoryEntryModel.embedding_ref.is_not(None),
            )
            .order_by(MemoryEntryModel.created_at)
            .limit(limit)
        )
        missing = list((await self._session.execute(missing_stmt)).scalars().all())
        stale = list((await self._session.execute(stale_stmt)).scalars().all())
        return missing, stale

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
