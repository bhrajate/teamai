"""SQLCheckpointRepository 的真 SQL 行为。

跑在内存 SQLite 上。三处语义必须真过一遍 SQL 才有意义：

- **覆盖写保留 attempts** —— 用 merge 会把它清零，于是反复崩溃的任务能无限续跑
- **bytes 往返** —— LargeBinary 在不同方言下的绑定行为，这里存的是消息历史
- **bump_attempts 的 RETURNING** —— 原子自增，读改写会丢计数
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from teamai.infrastructure.db import Base
from teamai.infrastructure.repositories.checkpoint import SQLCheckpointRepository


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[SQLCheckpointRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield SQLCheckpointRepository(s)
    await engine.dispose()


async def test_落库后能取回(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"hello", 100)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.task_id == "task_1"
    assert cp.messages == b"hello"
    assert cp.tokens_used == 100
    assert cp.attempts == 0


async def test_不存在时返回None(repo: SQLCheckpointRepository) -> None:
    """首次执行、以及纯文本任务从未落过检查点，都走这条路径。"""
    assert await repo.get("task_nope") is None


async def test_bytes原样往返(repo: SQLCheckpointRepository) -> None:
    """消息历史是二进制 blob，含 0x00 与非 UTF-8 字节也必须原样存取。"""
    blob = bytes(range(256)) + b'{"json": "\xe4\xb8\xad\xe6\x96\x87"}'
    await repo.upsert("task_1", blob, 0)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.messages == blob


async def test_覆盖写换内容与token(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"first", 100)
    await repo.upsert("task_1", b"second", 250)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.messages == b"second"
    assert cp.tokens_used == 250


async def test_覆盖写保留attempts(repo: SQLCheckpointRepository) -> None:
    """回归点。用 session.merge 写一个新对象会把 attempts 覆盖成 0 ——
    于是每落一个检查点就把续跑计数清零，attempts 上限形同虚设，
    一个反复崩溃的任务能无限续跑。
    """
    await repo.upsert("task_1", b"a", 10)
    await repo.bump_attempts("task_1")
    await repo.bump_attempts("task_1")

    await repo.upsert("task_1", b"b", 20)

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.attempts == 2, "覆盖写把续跑计数清零了"


async def test_覆盖写保留created_at(repo: SQLCheckpointRepository) -> None:
    """created_at 记的是「检查点首次出现在何时」，与本次写入内容无关。"""
    await repo.upsert("task_1", b"a", 10)
    first = await repo.get("task_1")
    assert first is not None

    await repo.upsert("task_1", b"b", 20)
    second = await repo.get("task_1")
    assert second is not None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


async def test_bump_attempts返回自增后的值(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"a", 0)

    assert await repo.bump_attempts("task_1") == 1
    assert await repo.bump_attempts("task_1") == 2
    assert await repo.bump_attempts("task_1") == 3

    cp = await repo.get("task_1")
    assert cp is not None
    assert cp.attempts == 3


async def test_bump不存在的任务返回0(repo: SQLCheckpointRepository) -> None:
    """巡检可能在检查点刚被删掉后才 bump，不该抛异常。"""
    assert await repo.bump_attempts("task_nope") == 0


async def test_删除(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"a", 0)
    await repo.delete("task_1")
    assert await repo.get("task_1") is None


async def test_删不存在的静默返回(repo: SQLCheckpointRepository) -> None:
    """大多数任务（纯文本、单轮）从未落过检查点，而终态迁移对它们同样会走到
    这里 —— 抛异常会让正常任务的完成路径炸掉。"""
    await repo.delete("task_nope")  # 不抛即通过


async def test_任务之间互不影响(repo: SQLCheckpointRepository) -> None:
    await repo.upsert("task_1", b"one", 10)
    await repo.upsert("task_2", b"two", 20)
    await repo.bump_attempts("task_1")

    await repo.delete("task_1")

    assert await repo.get("task_1") is None
    cp2 = await repo.get("task_2")
    assert cp2 is not None
    assert cp2.messages == b"two"
    assert cp2.attempts == 0
