"""OutboxRepository 的 SQLAlchemy 实现。

⚠️ 本仓储**不提交事务**，边界由 `UnitOfWork` 管理（用例层声明）。写方法用
`flush()` —— 理由见 SQLMemoryRepository 的类说明。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from teamai.domain.identity import gen_id
from teamai.domain.models.outbox import OutboxEntry, OutboxOp
from teamai.domain.repositories.outbox import OutboxRepository, OutboxStats
from teamai.infrastructure.orm.outbox import MemoryOutboxModel

# 单条错误信息的存储上限。留痕够用即可，完整堆栈在日志里。
_MAX_ERROR_LENGTH = 500


def _aware(dt: datetime | None) -> datetime | None:
    """把可能是 naive 的时间戳补成 UTC aware。

    ⚠️ 必须有这一步。Postgres 的 `timestamptz` 读回来是 aware 的，而 SQLite
    没有时区概念、读回来是 naive 的。stats() 要拿它与 `datetime.now(UTC)` 相减，
    naive 与 aware 相减会抛 `TypeError: can't subtract offset-naive and
    offset-aware datetimes` —— 单测跑在 SQLite 上，不补这一步那里会红。
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_entry(m: MemoryOutboxModel) -> OutboxEntry:
    return OutboxEntry(
        id=m.id,
        entry_id=m.entry_id,
        op=m.op,
        attempts=m.attempts,
        next_attempt_at=_aware(m.next_attempt_at),  # type: ignore[arg-type]
        claimed_at=_aware(m.claimed_at),
        claimed_by=m.claimed_by,
        last_error=m.last_error,
        failed_at=_aware(m.failed_at),
        created_at=_aware(m.created_at),  # type: ignore[arg-type]
    )


class SQLOutboxRepository(OutboxRepository):
    def __init__(self, session: AsyncSession, *, dialect: str = "postgresql") -> None:
        """`dialect` 由组合根传入，不向 session 询问。

        `AsyncSession` 上取方言要绕 `get_bind()`，而它在异步上下文里的行为随
        SQLAlchemy 版本变过。组合根本来就知道自己连的是什么库，显式传更稳，
        测试里也能直接指定 sqlite。
        """
        self._session = session
        self._dialect = dialect

    async def enqueue(self, entry_id: str, op: OutboxOp) -> OutboxEntry:
        now = datetime.now(UTC)
        model = MemoryOutboxModel(
            id=gen_id("obx"),
            entry_id=entry_id,
            op=op,
            attempts=0,
            next_attempt_at=now,  # 新记录即刻可取
            created_at=now,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_entry(model)

    async def claim(self, *, limit: int, lease_seconds: int, claimed_by: str) -> list[OutboxEntry]:
        now = datetime.now(UTC)
        lease_cutoff = now - timedelta(seconds=lease_seconds)

        inner = (
            select(MemoryOutboxModel.id)
            .where(
                MemoryOutboxModel.failed_at.is_(None),
                MemoryOutboxModel.next_attempt_at <= now,
                # 未被持有，或租约已过期。后半句让 projector 崩溃后那批记录自动
                # 回到可取状态，不需要单独的清理任务。
                (MemoryOutboxModel.claimed_at.is_(None))
                | (MemoryOutboxModel.claimed_at < lease_cutoff),
            )
            .order_by(MemoryOutboxModel.created_at)
            .limit(limit)
        )
        if self._dialect == "postgresql":
            # SKIP LOCKED 让多个 projector 实例互不阻塞地各取一批。
            #
            # ⚠️ SQLite 不支持它，那条分支下「选 id」与「打租约」之间存在竞态:
            # 两个并发 projector 可能都认领同一行。单测是单并发，不会触发；
            # 生产是 Postgres，走上面这条。若哪天要在别的方言上跑多实例，
            # 这里必须改成单语句的 `UPDATE ... RETURNING`。
            inner = inner.with_for_update(skip_locked=True)

        ids = list((await self._session.execute(inner)).scalars().all())
        if not ids:
            return []

        await self._session.execute(
            update(MemoryOutboxModel)
            .where(MemoryOutboxModel.id.in_(ids))
            .values(claimed_at=now, claimed_by=claimed_by[:64])
        )
        await self._session.flush()

        rows = (
            (
                await self._session.execute(
                    select(MemoryOutboxModel)
                    .where(MemoryOutboxModel.id.in_(ids))
                    .order_by(MemoryOutboxModel.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [_to_entry(r) for r in rows]

    async def complete(self, outbox_id: str) -> None:
        m = await self._session.get(MemoryOutboxModel, outbox_id)
        if m:
            await self._session.delete(m)
            await self._session.flush()

    async def fail(
        self, outbox_id: str, error: str, *, max_attempts: int, backoff_seconds: int
    ) -> None:
        m = await self._session.get(MemoryOutboxModel, outbox_id)
        if m is None:
            return
        m.attempts += 1
        m.last_error = error[:_MAX_ERROR_LENGTH]
        m.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds)
        # 失败即释放租约:重试时机由 next_attempt_at 控制，不必再等租约过期。
        # 留着 claimed_at 会让退避时间被租约时长（默认 300s）拉长。
        m.claimed_at = None
        m.claimed_by = None
        if m.attempts >= max_attempts:
            m.failed_at = datetime.now(UTC)
        await self._session.flush()

    async def stats(self) -> OutboxStats:
        pending = (
            await self._session.execute(
                select(func.count())
                .select_from(MemoryOutboxModel)
                .where(MemoryOutboxModel.failed_at.is_(None))
            )
        ).scalar_one()
        dead = (
            await self._session.execute(
                select(func.count())
                .select_from(MemoryOutboxModel)
                .where(MemoryOutboxModel.failed_at.is_not(None))
            )
        ).scalar_one()
        oldest = (
            await self._session.execute(
                select(func.min(MemoryOutboxModel.created_at)).where(
                    MemoryOutboxModel.failed_at.is_(None)
                )
            )
        ).scalar_one()

        lag = 0.0
        oldest_aware = _aware(oldest)
        if oldest_aware is not None:
            lag = max(0.0, (datetime.now(UTC) - oldest_aware).total_seconds())
        return OutboxStats(pending=int(pending), dead=int(dead), lag_seconds=lag)
