"""MemoryReconciler：两个对账谓词，逐字对应 should_embed 的不变量。

跑在内存 SQLite 上。⚠️ 谓词里的 `md5()` 是 Postgres 内置函数，SQLite 没有 ——
这里给连接**注册一个同名函数**（`hashlib.md5`，与 projector 的 `content_hash`
同一实现），于是谓词本身能在单测里验到，而不是只靠冒烟脚本。

注册的 md5 与 Postgres 的行为一致的前提是库是 UTF-8。真 Postgres 上的端到端
验证在 scripts/verify_outbox_flow.py。

要守的核心性质：这两个谓词必须与 `domain/models/memory.should_embed()` 等价。
不一致会让对账与投影互相拆台 —— 一方判「该有向量」不断入队，另一方判「不该有」
不断删掉，形成烧钱的死循环。故本文件对每种 type × superseded 组合都断言了方向。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.application.projector import content_hash
from teamai.application.reconciler import MemoryReconciler
from teamai.domain.models import MemoryEntry, MemoryType, OutboxOp, should_embed
from teamai.infrastructure.db import Base
from teamai.infrastructure.repositories.memory import SQLMemoryRepository
from tests.fakes import FakeOutboxRepository


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _register_md5(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.create_function(
            "md5", 1, lambda s: hashlib.md5((s or "").encode("utf-8")).hexdigest()
        )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _store(session: AsyncSession, entry: MemoryEntry) -> MemoryEntry:
    await SQLMemoryRepository(session).store(entry)
    return entry


def _entry(eid: str, **kw) -> MemoryEntry:
    base = dict(
        id=eid,
        channel_instance_id="ch_1",
        content="订单服务的超时阈值是 30 秒",
        type=MemoryType.FACT,
    )
    base.update(kw)
    return MemoryEntry(**base)  # type: ignore[arg-type]


def _reconciler(session: AsyncSession, outbox: FakeOutboxRepository, **kw) -> MemoryReconciler:
    """用**真** SQLMemoryRepository —— 要测的正是它那两个 SQL 谓词。

    换成内存替身就等于在测替身自己的判断（它直接调 `should_embed()`），
    而分叉恰恰会发生在「SQL 改写」与「Python 函数」之间。
    """
    return MemoryReconciler(SQLMemoryRepository(session), outbox, **kw)


# ===== 注册的 md5 与 projector 的 content_hash 一致 =====


async def test_测试用md5与content_hash一致(session: AsyncSession) -> None:
    """自检：两者不一致的话，下面所有关于 hash 的断言都在测一个假前提。"""
    from sqlalchemy import text

    got = (await session.execute(text("SELECT md5('订单服务的超时阈值是 30 秒')"))).scalar_one()
    assert got == content_hash("订单服务的超时阈值是 30 秒")


# ===== 补 UPSERT：该有向量却缺失或过期 =====


async def test_缺向量的行被补入队(session: AsyncSession) -> None:
    await _store(session, _entry("mem_1"))
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox).run_once()

    assert report.missing == ["mem_1"]
    assert outbox.enqueued == [("mem_1", OutboxOp.UPSERT)]


async def test_hash不符的行被补入队(session: AsyncSession) -> None:
    """内容漂移：向量在但对应旧文本。只看 embedding_ref 判不出这种情况 ——
    这正是 embedded_hash 必须与它并存的理由。"""
    await _store(session, _entry("mem_1", embedding_ref="point-1", embedded_hash="stale"))
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox).run_once()

    assert report.missing == ["mem_1"]


async def test_hash为空但有ref的行被补入队(session: AsyncSession) -> None:
    """存量行的形态：改造前写过向量但没有 hash 可追溯。全部判为需重算 ——
    比写一个猜测原内容的回填脚本可靠。"""
    await _store(session, _entry("mem_1", embedding_ref="point-1", embedded_hash=None))
    outbox = FakeOutboxRepository()

    assert (await _reconciler(session, outbox).run_once()).missing == ["mem_1"]


async def test_已是最新的行不被入队(session: AsyncSession) -> None:
    content = "订单服务的超时阈值是 30 秒"
    await _store(
        session,
        _entry("mem_1", embedding_ref="point-1", embedded_hash=content_hash(content)),
    )
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox).run_once()

    assert report.total == 0
    assert outbox.enqueued == []


# ===== 补 DELETE：不该有向量却有 =====


async def test_偏好有向量则补删(session: AsyncSession) -> None:
    """生产库可能残留合表前蒸馏写下的 PREFERENCE 向量。"""
    await _store(
        session,
        _entry("mem_p", type=MemoryType.PREFERENCE, embedding_ref="point-p", embedded_hash="h"),
    )
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox).run_once()

    assert report.stale == ["mem_p"]
    assert outbox.enqueued == [("mem_p", OutboxOp.DELETE)]


async def test_已被取代且有向量则补删(session: AsyncSession) -> None:
    """过期事实的向量留着比没有更糟 —— 检索会按已作废的内容命中。"""
    await _store(
        session,
        _entry("mem_old", superseded_by="mem_new", embedding_ref="point-o", embedded_hash="h"),
    )
    outbox = FakeOutboxRepository()

    assert (await _reconciler(session, outbox).run_once()).stale == ["mem_old"]


async def test_偏好没有向量则不动(session: AsyncSession) -> None:
    """偏好本来就不该有向量，没有就是正确状态，不该被当成缺失去补。"""
    await _store(session, _entry("mem_p", type=MemoryType.PREFERENCE))
    outbox = FakeOutboxRepository()

    assert (await _reconciler(session, outbox).run_once()).total == 0


async def test_已被取代且没有向量则不动(session: AsyncSession) -> None:
    await _store(session, _entry("mem_old", superseded_by="mem_new"))
    outbox = FakeOutboxRepository()

    assert (await _reconciler(session, outbox).run_once()).total == 0


# ===== 与 should_embed 的等价性 =====


@pytest.mark.parametrize(
    "type_",
    [MemoryType.BACKGROUND_KNOWLEDGE, MemoryType.PREFERENCE, MemoryType.DECISION, MemoryType.FACT],
)
@pytest.mark.parametrize("superseded", [None, "mem_new"])
async def test_对账方向与should_embed一致(
    session: AsyncSession, type_: MemoryType, superseded: str | None
) -> None:
    """穷举 type × superseded 的八种组合，逐一核对对账的判断与 should_embed 相同。

    这是本文件最要紧的一条。SQL 谓词与 Python 函数是同一个不变量的两种表述，
    它们分叉时不会报错 —— 只会让对账与投影互相拆台，症状是 reconcile 指标持续
    非零而向量反复重建。

    做法：给行一个「已经建好且最新」的向量状态，然后看对账是否想删它。
    should_embed 为真 → 不该动；为假 → 该补 DELETE。
    """
    content = "订单服务的超时阈值是 30 秒"
    await _store(
        session,
        _entry(
            "mem_x",
            type=type_,
            superseded_by=superseded,
            embedding_ref="point-x",
            embedded_hash=content_hash(content),
        ),
    )
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox).run_once()

    entry = _entry("mem_x", type=type_, superseded_by=superseded)
    if entry.should_embed():
        assert report.total == 0, f"{type_}/{superseded} 应当有向量，不该被动"
    else:
        assert report.stale == ["mem_x"], f"{type_}/{superseded} 不该有向量，应补 DELETE"
        assert report.missing == []


def test_should_embed_的两个维度都被覆盖() -> None:
    """自检：上面的参数化必须同时覆盖 type 与 superseded 两个维度的真假两侧，
    否则等价性断言可能一侧空转。"""
    assert should_embed(MemoryType.FACT) is True
    assert should_embed(MemoryType.PREFERENCE) is False
    assert _entry("m", superseded_by="x").should_embed() is False
    assert _entry("m").should_embed() is True


# ===== 上界 =====


async def test_limit生效(session: AsyncSession) -> None:
    """首次上线时存量偏差可能成千上万，一次全塞进 outbox 会让 lag 指标瞬间爆表、
    且挤掉正常写入的投影。分几轮补完即可 —— 对账是安全网，不赶时间。"""
    for i in range(5):
        await _store(session, _entry(f"mem_{i}"))
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox, limit=2).run_once()

    assert len(report.missing) == 2


async def test_按创建时间升序补(session: AsyncSession) -> None:
    """最老的先补 —— 与 outbox 的领取顺序一致，避免新写入的一直插队。"""
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 8, 1, tzinfo=UTC)
    for i, offset in enumerate([2, 0, 1]):
        await _store(session, _entry(f"mem_{i}", created_at=base + timedelta(hours=offset)))
    outbox = FakeOutboxRepository()

    report = await _reconciler(session, outbox, limit=2).run_once()

    assert report.missing == ["mem_1", "mem_2"], "按 created_at 升序，不是按 id"
