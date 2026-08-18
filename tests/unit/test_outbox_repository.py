"""SQLOutboxRepository 的真 SQL 行为:租约、退避、死信、stats。

跑在内存 SQLite 上（`dialect="sqlite"`，跳过 `FOR UPDATE SKIP LOCKED`）。
这些语义与方言无关，与 test_memory_repository.py 同思路。

⚠️ 单并发。`claim` 在 sqlite 分支下「选 id」与「打租约」之间有竞态，
生产走 Postgres 的 SKIP LOCKED（见仓储里的注释）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.domain.models.outbox import OutboxOp
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.outbox import MemoryOutboxModel
from teamai.infrastructure.repositories.outbox import SQLOutboxRepository

WHO = "test-worker:1"


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[SQLOutboxRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield SQLOutboxRepository(s, dialect="sqlite")
    await engine.dispose()


def _session(repo: SQLOutboxRepository) -> AsyncSession:
    return repo._session  # noqa: SLF001  测试要直接查表核对落库结果


@pytest.mark.asyncio
async def test_入队后即刻可取(repo: SQLOutboxRepository):
    await repo.enqueue("mem_1", OutboxOp.UPSERT)
    claimed = await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO)
    assert [c.entry_id for c in claimed] == ["mem_1"]
    assert claimed[0].claimed_by == WHO
    assert claimed[0].attempts == 0


@pytest.mark.asyncio
async def test_已被领取的不会被重复领取(repo: SQLOutboxRepository):
    await repo.enqueue("mem_1", OutboxOp.UPSERT)
    first = await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO)
    second = await repo.claim(limit=10, lease_seconds=300, claimed_by="other:2")
    assert len(first) == 1
    assert second == [], "租约未过期时不该被他人领走"


@pytest.mark.asyncio
async def test_租约过期后可被重新领取(repo: SQLOutboxRepository):
    """projector 崩溃后那批记录要自动回到可取状态，不依赖额外的清理任务。"""
    await repo.enqueue("mem_1", OutboxOp.UPSERT)
    await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO)
    # lease_seconds=0 即「任何已持有的租约都算过期」
    again = await repo.claim(limit=10, lease_seconds=0, claimed_by="other:2")
    assert [c.entry_id for c in again] == ["mem_1"]
    assert again[0].claimed_by == "other:2"


@pytest.mark.asyncio
async def test_按创建时间升序领取(repo: SQLOutboxRepository):
    """最老的先处理，与 lag 指标取 min(created_at) 的定义一致。"""
    for i in range(3):
        await repo.enqueue(f"mem_{i}", OutboxOp.UPSERT)
    # 手工把 created_at 拉开，避免同一时刻入队导致顺序不确定
    base = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    rows = (await _session(repo).execute(select(MemoryOutboxModel))).scalars().all()
    for offset, row in enumerate(rows):
        row.created_at = base + timedelta(minutes=offset)
    await _session(repo).flush()

    claimed = await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO)
    assert [c.created_at for c in claimed] == sorted(c.created_at for c in claimed)


@pytest.mark.asyncio
async def test_limit_生效(repo: SQLOutboxRepository):
    for i in range(5):
        await repo.enqueue(f"mem_{i}", OutboxOp.UPSERT)
    claimed = await repo.claim(limit=2, lease_seconds=300, claimed_by=WHO)
    assert len(claimed) == 2


@pytest.mark.asyncio
async def test_complete_删行(repo: SQLOutboxRepository):
    """处理成功不留记录:审计由 audit_logs 承担，留着会让这张表无界增长。"""
    e = await repo.enqueue("mem_1", OutboxOp.UPSERT)
    await repo.complete(e.id)
    remaining = (await _session(repo).execute(select(MemoryOutboxModel))).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_fail_递增尝试并推后重试且释放租约(repo: SQLOutboxRepository):
    e = await repo.enqueue("mem_1", OutboxOp.UPSERT)
    await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO)
    await repo.fail(e.id, "embedding 限流", max_attempts=11, backoff_seconds=60)

    row = await _session(repo).get(MemoryOutboxModel, e.id)
    assert row is not None
    assert row.attempts == 1
    assert row.last_error == "embedding 限流"
    assert row.failed_at is None
    # 释放租约:重试时机由 next_attempt_at 控制，留着 claimed_at 会让退避被
    # 租约时长拉长
    assert row.claimed_at is None
    assert row.claimed_by is None


@pytest.mark.asyncio
async def test_退避未到点时取不到(repo: SQLOutboxRepository):
    e = await repo.enqueue("mem_1", OutboxOp.UPSERT)
    await repo.fail(e.id, "boom", max_attempts=11, backoff_seconds=300)
    assert await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO) == []


@pytest.mark.asyncio
async def test_达到上限转死信且不再被领取(repo: SQLOutboxRepository):
    e = await repo.enqueue("mem_1", OutboxOp.UPSERT)
    for _ in range(3):
        await repo.fail(e.id, "boom", max_attempts=3, backoff_seconds=0)

    row = await _session(repo).get(MemoryOutboxModel, e.id)
    assert row is not None
    assert row.attempts == 3
    assert row.failed_at is not None
    assert await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO) == []


@pytest.mark.asyncio
async def test_fail_对不存在的记录是_noop(repo: SQLOutboxRepository):
    """并发下另一实例可能已 complete 掉它，不该因此抛异常。"""
    await repo.fail("obx_nonexistent", "boom", max_attempts=11, backoff_seconds=60)


@pytest.mark.asyncio
async def test_同一记忆允许多条记录(repo: SQLOutboxRepository):
    """连续 edit 会产生多条，不去重 —— 理由见 OutboxRepository.enqueue 的说明。"""
    await repo.enqueue("mem_1", OutboxOp.UPSERT)
    await repo.enqueue("mem_1", OutboxOp.UPSERT)
    claimed = await repo.claim(limit=10, lease_seconds=300, claimed_by=WHO)
    assert len(claimed) == 2


@pytest.mark.asyncio
async def test_stats_空队列(repo: SQLOutboxRepository):
    s = await repo.stats()
    assert (s.pending, s.dead, s.lag_seconds) == (0, 0, 0.0)


@pytest.mark.asyncio
async def test_stats_区分待处理与死信(repo: SQLOutboxRepository):
    alive = await repo.enqueue("mem_1", OutboxOp.UPSERT)
    dead = await repo.enqueue("mem_2", OutboxOp.UPSERT)
    await repo.fail(dead.id, "boom", max_attempts=1, backoff_seconds=0)

    s = await repo.stats()
    assert (s.pending, s.dead) == (1, 1)
    assert alive.id  # 保留引用，表明存活那条就是它


@pytest.mark.asyncio
async def test_stats_lag_取最老待处理条目(repo: SQLOutboxRepository):
    """lag 不取平均:平均会被大量刚入队的记录稀释，而要答的是最坏等待时长。

    这条同时守着 `_aware()` —— SQLite 读回 naive 时间戳，不补 UTC 会在
    与 now(UTC) 相减时抛 TypeError。
    """
    old = await repo.enqueue("mem_old", OutboxOp.UPSERT)
    await repo.enqueue("mem_new", OutboxOp.UPSERT)
    await _session(repo).execute(
        update(MemoryOutboxModel)
        .where(MemoryOutboxModel.id == old.id)
        .values(created_at=datetime.now(UTC) - timedelta(seconds=120))
    )
    await _session(repo).flush()

    s = await repo.stats()
    assert s.lag_seconds >= 120
    assert s.pending == 2


@pytest.mark.asyncio
async def test_stats_忽略死信的_lag(repo: SQLOutboxRepository):
    """死信不该拉高 lag —— 它已经不在待处理路径上，由 dead 指标单独盯。"""
    e = await repo.enqueue("mem_1", OutboxOp.UPSERT)
    await _session(repo).execute(
        update(MemoryOutboxModel)
        .where(MemoryOutboxModel.id == e.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=5))
    )
    await repo.fail(e.id, "boom", max_attempts=1, backoff_seconds=0)

    s = await repo.stats()
    assert (s.pending, s.dead, s.lag_seconds) == (0, 1, 0.0)
