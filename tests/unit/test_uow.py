"""UnitOfWork 的可重入语义与提交/回滚时机。

这些用例守的是 outbox 方案的核心保证:一组写入要么全落、要么全不落
(见 docs/plan-memory-outbox.md §5.5)。可重入尤其要守 —— MemoryService
的 supersede 内部调 store,不可重入的实现会在内层就提交掉半个操作。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.domain.models import MemoryEntry, MemorySource, MemoryType
from teamai.domain.ports.uow import UnitOfWork
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.memory import MemoryEntryModel
from teamai.infrastructure.repositories.memory import SQLMemoryRepository
from teamai.infrastructure.uow import SQLUnitOfWork


class _RecordingUoW(UnitOfWork):
    """记录提交/回滚次数的探针实现。"""

    def __init__(self) -> None:
        super().__init__()
        self.commits = 0
        self.rollbacks = 0

    async def _do_commit(self) -> None:
        self.commits += 1

    async def _do_rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_单层退出即提交():
    uow = _RecordingUoW()
    async with uow:
        assert uow.depth == 1
    assert (uow.commits, uow.rollbacks) == (1, 0)
    assert uow.depth == 0


@pytest.mark.asyncio
async def test_嵌套只在最外层提交一次():
    """内层退出必须是 no-op，否则 supersede 会撕开成半个操作。"""
    uow = _RecordingUoW()
    async with uow:
        async with uow:
            async with uow:
                assert uow.depth == 3
            assert uow.commits == 0, "内层退出不该提交"
        assert uow.commits == 0, "中层退出不该提交"
    assert (uow.commits, uow.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_异常时回滚且不提交():
    uow = _RecordingUoW()
    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            raise RuntimeError("boom")
    assert (uow.commits, uow.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_内层抛异常时外层回滚而不是内层():
    """内层不该自己回滚：那会撤掉外层已做的变更而外层并不知情。"""
    uow = _RecordingUoW()
    with pytest.raises(ValueError):
        async with uow:
            async with uow:
                raise ValueError("inner")
    # 只回滚一次，且发生在最外层
    assert (uow.commits, uow.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_异常照常向上抛不被吞掉():
    uow = _RecordingUoW()
    with pytest.raises(KeyError):
        async with uow:
            raise KeyError("must propagate")


@pytest.mark.asyncio
async def test_显式提交与回滚不受深度影响():
    uow = _RecordingUoW()
    await uow.commit()
    await uow.rollback()
    assert (uow.commits, uow.rollbacks) == (1, 1)
    assert uow.depth == 0


@pytest.mark.asyncio
async def test_同一实例可复用于连续两个事务():
    uow = _RecordingUoW()
    async with uow:
        pass
    async with uow:
        pass
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_null_uow_不抛不做():
    from teamai.infrastructure.uow import NullUnitOfWork

    uow = NullUnitOfWork()
    async with uow:
        pass
    await uow.commit()
    await uow.rollback()
    assert uow.depth == 0


# ── 打真 SQL：验证「同一 session 上的写入按边界整体落盘/整体撤销」 ──
# 用内存 SQLite。事务边界语义与方言无关，与 test_memory_repository.py 同思路。


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _entry(entry_id: str) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        channel_instance_id="ch_1",
        content="订单服务的超时阈值是 30 秒",
        type=MemoryType.FACT,
        source=MemorySource.MANUAL,
    )


async def _count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(MemoryEntryModel))).scalar_one()


@pytest.mark.asyncio
async def test_真会话_边界内写入在退出后可见(session: AsyncSession):
    repo = SQLMemoryRepository(session)
    uow = SQLUnitOfWork(session)

    async with uow:
        await repo.store(_entry("mem_a"))
        await repo.store(_entry("mem_b"))

    assert await _count(session) == 2


@pytest.mark.asyncio
async def test_真会话_异常时两条都不落库(session: AsyncSession):
    """这是 outbox 方案的核心保证：记忆行与 outbox 行同生共死。"""
    repo = SQLMemoryRepository(session)
    uow = SQLUnitOfWork(session)

    with pytest.raises(RuntimeError):
        async with uow:
            await repo.store(_entry("mem_a"))
            await repo.store(_entry("mem_b"))
            raise RuntimeError("模拟 embed 之外的任意失败")

    assert await _count(session) == 0


@pytest.mark.asyncio
async def test_真会话_嵌套边界内层异常整体回滚(session: AsyncSession):
    repo = SQLMemoryRepository(session)
    uow = SQLUnitOfWork(session)

    with pytest.raises(ValueError):
        async with uow:
            await repo.store(_entry("mem_outer"))
            async with uow:
                await repo.store(_entry("mem_inner"))
                raise ValueError("inner")

    assert await _count(session) == 0, "内层的异常必须把外层那条也撤掉"


@pytest.mark.asyncio
async def test_真会话_session_属性就是传入的那个(session: AsyncSession):
    """projector 的抢占 UPDATE 要直接用它，必须是同一个对象。"""
    uow = SQLUnitOfWork(session)
    assert uow.session is session
