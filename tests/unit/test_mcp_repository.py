"""SQLMcpServerRepository 的真 SQL 行为：CRUD、JSON 编解码、唯一约束。

跑在内存 SQLite 上。唯一约束与 JSON 编解码语义与方言无关，
与 test_memory_repository.py 同思路。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from teamai.domain.models.mcp import McpServer
from teamai.infrastructure.db import Base
from teamai.infrastructure.orm.mcp import McpServerModel
from teamai.infrastructure.repositories.mcp import SQLMcpServerRepository


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[SQLMcpServerRepository]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield SQLMcpServerRepository(s)
    await engine.dispose()


def _session(repo: SQLMcpServerRepository) -> AsyncSession:
    return repo._session  # noqa: SLF001  测试要直接查表核对落库结果


def _server(name: str = "github", channel: str = "ch_1", **kw) -> McpServer:
    return McpServer(
        id=kw.pop("id", f"mcp_{name}_{channel}"),
        channel_instance_id=channel,
        name=name,
        url=f"https://mcp.example.com/{name}",
        headers={"Authorization": "Bearer secret"},
        **kw,
    )


@pytest.mark.asyncio
async def test_upsert后headers_json_roundtrip(repo: SQLMcpServerRepository):
    s = _server()
    await repo.upsert(s)

    row = (
        await _session(repo).execute(
            select(McpServerModel).where(McpServerModel.id == s.id)
        )
    ).scalars().one()
    assert row.headers == '{"Authorization": "Bearer secret"}'

    got = await repo.get(s.channel_instance_id, s.id)
    assert got is not None
    assert got.headers == s.headers
    assert got.enabled is True
    assert got.last_error is None


@pytest.mark.asyncio
async def test_upsert覆盖已有行_不产生重复(repo: SQLMcpServerRepository):
    s = _server()
    await repo.upsert(s)
    s.url = "https://mcp.example.com/v2"
    s.enabled = False
    await repo.upsert(s)

    rows = (
        await _session(repo).execute(select(McpServerModel))
    ).scalars().all()
    assert len(rows) == 1
    got = await repo.get(s.channel_instance_id, s.id)
    assert got is not None
    assert got.url == "https://mcp.example.com/v2"
    assert got.enabled is False


@pytest.mark.asyncio
async def test_同频道name唯一_不同频道可同名(repo: SQLMcpServerRepository):
    await repo.upsert(_server(name="github", channel="ch_1"))

    # 不同频道同名：允许
    await repo.upsert(_server(name="github", channel="ch_2", id="mcp_github_ch_2"))
    assert len(await repo.list_for_channel("ch_1")) == 1
    assert len(await repo.list_for_channel("ch_2")) == 1

    # 同频道同名：唯一约束拦截
    with pytest.raises(IntegrityError):
        await repo.upsert(_server(name="github", channel="ch_1", id="mcp_github_dup"))


@pytest.mark.asyncio
async def test_find_by_name(repo: SQLMcpServerRepository):
    await repo.upsert(_server())
    assert (await repo.find_by_name("ch_1", "github")) is not None
    assert (await repo.find_by_name("ch_1", "nope")) is None
    assert (await repo.find_by_name("ch_2", "github")) is None


@pytest.mark.asyncio
async def test_list_enabled只返回启用的(repo: SQLMcpServerRepository):
    await repo.upsert(_server(name="a", channel="ch_1"))
    await repo.upsert(_server(name="b", channel="ch_1", id="mcp_b_ch_1", enabled=False))

    enabled = await repo.list_enabled()
    assert [s.name for s in enabled] == ["a"]


@pytest.mark.asyncio
async def test_delete仅删指定频道与id(repo: SQLMcpServerRepository):
    await repo.upsert(_server(name="a", channel="ch_1"))
    await repo.upsert(_server(name="a", channel="ch_2", id="mcp_a_ch_2"))
    await repo.delete("ch_1", "mcp_a_ch_1")

    assert await repo.get("ch_1", "mcp_a_ch_1") is None
    assert await repo.get("ch_2", "mcp_a_ch_2") is not None


@pytest.mark.asyncio
async def test_list_for_channel按name排序(repo: SQLMcpServerRepository):
    await repo.upsert(_server(name="zeta", channel="ch_1"))
    await repo.upsert(_server(name="alpha", channel="ch_1", id="mcp_alpha_ch_1"))

    assert [s.name for s in await repo.list_for_channel("ch_1")] == ["alpha", "zeta"]
