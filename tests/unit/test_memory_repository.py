"""SQLMemoryRepository 的真 SQL 行为。

跑在内存 SQLite 上：生产走 asyncpg，但「merge 按主键匹配、故换 id 就是
INSERT」「ORDER BY 决定返回顺序」这类语义与方言无关，用 SQLite 验足够且不必
起容器（与 test_budget_configure.py 同思路）。

两条契约值得用真 SQL 而不是替身来锁：

1. `update` 走 merge，**复用原 id 才是 UPDATE**。`budget_quotas` 上踩过这个坑
   —— 当时每次都 gen_id 一个新 id，于是每次「改配额」都插一行新的，而读取用
   `.first()` 且无排序，表现成「管理员改完上限读回的还是旧行」。内存替身按 id
   查找替换，天然不会重现这个坑，所以必须打真 SQL。
2. `list_by_channel` 的倒序与 limit。改造前这个查询既无 ORDER BY 也无 LIMIT，
   而调用方在 Python 侧切前 N 条当检索结果 —— 等于随机取样。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.domain.models import MemoryEntry, MemorySource, MemoryType
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.memory import MemoryEntryModel
from teamai.infrastructure.repositories.memory import SQLMemoryRepository

BASE_TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def repo(session: AsyncSession) -> SQLMemoryRepository:
    return SQLMemoryRepository(session)


def _entry(
    eid: str = "mem_1",
    *,
    content: str = "订单服务超时是 30 秒",
    channel: str = "ch_1",
    offset_minutes: int = 0,
    **kwargs,
) -> MemoryEntry:
    return MemoryEntry(
        id=eid,
        channel_instance_id=channel,
        content=content,
        created_at=BASE_TS + timedelta(minutes=offset_minutes),
        **kwargs,
    )


async def _count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(MemoryEntryModel))).scalar_one()


# ===== update =====


async def test_update是原地改而非插新行(repo: SQLMemoryRepository, session) -> None:
    """核心契约。复用原 id 时 merge 必须是 UPDATE。"""
    await repo.store(_entry("mem_1", content="原内容"))

    entry = await repo.get("mem_1")
    assert entry is not None
    entry.content = "改过的内容"
    await repo.update(entry)

    assert await _count(session) == 1, "不该多出一行"
    assert (await repo.get("mem_1")).content == "改过的内容"


async def test_update保留未改动的字段(repo: SQLMemoryRepository) -> None:
    await repo.store(
        _entry(
            "mem_1",
            source=MemorySource.DISTILLED,
            source_user_id="ou_abc",
        )
    )

    entry = await repo.get("mem_1")
    created_before = entry.created_at
    entry.content = "只改内容"
    await repo.update(entry)

    got = await repo.get("mem_1")
    assert got.source is MemorySource.DISTILLED
    assert got.source_user_id == "ou_abc"
    # 与更新前读到的值比，而不是与 BASE_TS 比：SQLite 不保留 tzinfo（返回 naive
    # datetime），而 Postgres 的 DateTime(timezone=True) 会保留。这里要验的契约
    # 是「created_at 没被改动」，不是时区表示，所以两边取同一来源即可。
    assert got.created_at == created_before


async def test_换id会变成插入而非更新(repo: SQLMemoryRepository, session) -> None:
    """把那个坑本身钉住：merge 按主键匹配，换 id 就是 INSERT。

    这不是期望行为，而是「为什么 update 的文档要求复用原 id」的证据 ——
    将来若有人改成 gen_id 一个新 id，这条会提醒他后果是什么。
    """
    await repo.store(_entry("mem_1", content="原内容"))

    entry = await repo.get("mem_1")
    entry.id = "mem_2"  # 模拟误用
    entry.content = "改过的"
    await repo.update(entry)

    assert await _count(session) == 2, "换 id 后是 INSERT，于是表里有两行"
    assert (await repo.get("mem_1")).content == "原内容", "原行没被改到"


async def test_source与embedding_ref可被更新(repo: SQLMemoryRepository) -> None:
    """回填 embedding_ref 与编辑后改 source 都走这条路。"""
    await repo.store(_entry("mem_1", source=MemorySource.DISTILLED))

    entry = await repo.get("mem_1")
    entry.source = MemorySource.EDITED
    entry.embedding_ref = "point-abc"
    entry.type = MemoryType.DECISION
    await repo.update(entry)

    got = await repo.get("mem_1")
    assert got.source is MemorySource.EDITED
    assert got.embedding_ref == "point-abc"
    assert got.type is MemoryType.DECISION


# ===== 查询 =====


async def test_按创建时间倒序(repo: SQLMemoryRepository) -> None:
    for i in range(4):
        await repo.store(_entry(f"mem_{i}", offset_minutes=i))

    rows = await repo.list_by_channel("ch_1")

    assert [r.id for r in rows] == ["mem_3", "mem_2", "mem_1", "mem_0"]


async def test_limit生效(repo: SQLMemoryRepository) -> None:
    for i in range(10):
        await repo.store(_entry(f"mem_{i}", offset_minutes=i))

    rows = await repo.list_by_channel("ch_1", limit=3)

    assert [r.id for r in rows] == ["mem_9", "mem_8", "mem_7"]


async def test_limit为None时返回全部(repo: SQLMemoryRepository) -> None:
    """全量重建向量索引这类场景仍需要拿全部。"""
    for i in range(5):
        await repo.store(_entry(f"mem_{i}", offset_minutes=i))

    assert len(await repo.list_by_channel("ch_1", limit=None)) == 5


async def test_频道隔离(repo: SQLMemoryRepository) -> None:
    await repo.store(_entry("mem_a", channel="ch_A"))
    await repo.store(_entry("mem_b", channel="ch_B"))

    rows = await repo.list_by_channel("ch_B")

    assert [r.id for r in rows] == ["mem_b"]


async def test_exclude_type排除指定类型(repo: SQLMemoryRepository) -> None:
    """语义回落路径用它排掉偏好，别让偏好混进 top_k 名额。"""
    await repo.store(_entry("mem_fact", content="超时 30 秒"))
    await repo.store(
        _entry("mem_pref", content="回答要简短", type=MemoryType.PREFERENCE, offset_minutes=1)
    )

    rows = await repo.list_by_channel("ch_1", exclude_type=MemoryType.PREFERENCE)

    assert [r.id for r in rows] == ["mem_fact"]


async def test_list_preferences只列现行偏好且按时间倒序(repo: SQLMemoryRepository) -> None:
    await repo.store(_entry("mem_fact", content="超时 30 秒"))
    await repo.store(_entry("mem_pref_a", content="回答要简短", type=MemoryType.PREFERENCE))
    await repo.store(
        _entry("mem_pref_b", content="别用 emoji", type=MemoryType.PREFERENCE, offset_minutes=5)
    )
    # 被取代的偏好不是现行事实，不应出现
    pref_a = await repo.get("mem_pref_a")
    pref_a.supersede("mem_pref_b")
    await repo.update(pref_a)

    rows = await repo.list_preferences("ch_1")

    assert [r.id for r in rows] == ["mem_pref_b"]


async def test_删除后读不到(repo: SQLMemoryRepository, session) -> None:
    await repo.store(_entry("mem_1"))

    await repo.delete("mem_1")

    assert await repo.get("mem_1") is None
    assert await _count(session) == 0


async def test_删除不存在的id不报错(repo: SQLMemoryRepository) -> None:
    await repo.delete("mem_missing")
