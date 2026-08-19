"""MCP server 管理路由：CRUD、脱敏、校验、更新语义、连接测试。

不接真实 DB：build_mcp_router 收 container 即可，用内存 fake 的
mcp_repo + McpService（test_connection 是纯透传，真握手用 conftest 的
mcp_server_url）。

⚠️ 不用 fastapi TestClient：它的 portal 是独立线程的事件循环，而 conftest
的 mcp_server_url（uvicorn）跑在 pytest_asyncio 的循环里，跨循环的 streamable
HTTP 握手会挂起。全部用例走 httpx.AsyncClient + ASGITransport，同一循环。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from teamai.adapters.admin.mcp import build_mcp_router
from teamai.application.mcp import McpService
from teamai.domain.models import McpServer
from teamai.infrastructure.mcp.client import ConnectorFactory
from teamai.infrastructure.tools.registry import ToolRegistry
from teamai.infrastructure.uow import NullUnitOfWork
from tests.fakes import FakeMcpServerRepository

CHANNEL = "ch_1"


async def _client(servers: list[McpServer] | None = None) -> tuple[httpx.AsyncClient, FakeMcpServerRepository]:
    repo = FakeMcpServerRepository(servers)
    container = SimpleNamespace(
        mcp_repo=repo,
        mcp=McpService(repo, ToolRegistry(), NullUnitOfWork(), ConnectorFactory()),
        uow=NullUnitOfWork(),
    )
    app = FastAPI()
    app.include_router(build_mcp_router(container))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, repo


def _server(server_id: str = "mcp_1", name: str = "github", **kw) -> McpServer:
    return McpServer(
        id=server_id,
        channel_instance_id=CHANNEL,
        name=name,
        url="https://mcp.example.com/github",
        **kw,
    )


@pytest.mark.asyncio
async def test_创建后headers脱敏回显_库中为真值():
    client, repo = await _client()
    try:
        resp = await client.post(
            f"/channels/{CHANNEL}/mcp-servers",
            json={"name": "github", "url": "https://mcp.example.com", "headers": {"Authorization": "Bearer sekrit"}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["headers"] == {"Authorization": "***"}
        assert body["last_error"] is None
        stored = repo._servers[body["id"]]
        assert stored.headers == {"Authorization": "Bearer sekrit"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_列表不泄露凭据():
    client, _ = await _client([_server(headers={"X-Api-Key": "secret", "Authorization": "Bearer t"})])
    try:
        resp = await client.get(f"/channels/{CHANNEL}/mcp-servers")
        assert resp.status_code == 200
        (row,) = resp.json()
        assert set(row["headers"].values()) == {"***"}
        assert "secret" not in resp.text and "Bearer t" not in resp.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_name非法字符422():
    client, _ = await _client()
    try:
        for bad in ("Github", "github server", "github__x", "中文"):
            resp = await client.post(f"/channels/{CHANNEL}/mcp-servers", json={"name": bad, "url": "https://x"})
            assert resp.status_code == 422, f"{bad} 应被拒"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_url非http422():
    client, _ = await _client()
    try:
        resp = await client.post(f"/channels/{CHANNEL}/mcp-servers", json={"name": "github", "url": "ftp://x"})
        assert resp.status_code == 422
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_同频道重名409_不同频道同名可创建():
    client, _ = await _client([_server()])
    try:
        resp = await client.post(f"/channels/{CHANNEL}/mcp-servers", json={"name": "github", "url": "https://x"})
        assert resp.status_code == 409

        resp = await client.post("/channels/ch_2/mcp-servers", json={"name": "github", "url": "https://x"})
        assert resp.status_code == 200
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_更新headers占位保留原值_空串删键_真值覆盖():
    client, repo = await _client([_server(headers={"Keep": "v1", "Drop": "v2", "Change": "old"})])
    try:
        resp = await client.put(
            f"/channels/{CHANNEL}/mcp-servers/mcp_1",
            json={"headers": {"Keep": "***", "Drop": "", "Change": "new"}},
        )
        assert resp.status_code == 200, resp.text
        assert repo._servers["mcp_1"].headers == {"Keep": "v1", "Change": "new"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_更新启停与改名():
    client, repo = await _client([_server()])
    try:
        resp = await client.put(f"/channels/{CHANNEL}/mcp-servers/mcp_1", json={"enabled": False})
        assert resp.status_code == 200
        assert repo._servers["mcp_1"].enabled is False

        resp = await client.put(f"/channels/{CHANNEL}/mcp-servers/mcp_1", json={"name": "gh"})
        assert resp.status_code == 200
        assert repo._servers["mcp_1"].name == "gh"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_改名撞同频道已有名409():
    client, _ = await _client([_server(), _server("mcp_2", "other")])
    try:
        resp = await client.put(f"/channels/{CHANNEL}/mcp-servers/mcp_2", json={"name": "github"})
        assert resp.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_不存在404():
    client, _ = await _client()
    try:
        assert (await client.put(f"/channels/{CHANNEL}/mcp-servers/nope", json={"enabled": True})).status_code == 404
        assert (await client.delete(f"/channels/{CHANNEL}/mcp-servers/nope")).status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_删除():
    client, repo = await _client([_server()])
    try:
        resp = await client.delete(f"/channels/{CHANNEL}/mcp-servers/mcp_1")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert repo._servers == {}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_连接测试成功返回工具名(mcp_server_url: str):
    client, _ = await _client()
    try:
        resp = await client.post(f"/channels/{CHANNEL}/mcp-servers/test", json={"url": mcp_server_url})
        assert resp.status_code == 200
        assert sorted(resp.json()["tools"]) == ["add", "boom", "greet"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_连接测试失败422带错误详情():
    client, _ = await _client()
    try:
        resp = await client.post(
            f"/channels/{CHANNEL}/mcp-servers/test",
            json={"url": "http://127.0.0.1:1/mcp"},  # 无服务监听，连接立即被拒
        )
        assert resp.status_code == 422
        assert "连接 MCP server 失败" in resp.json()["detail"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_连接测试url校验():
    client, _ = await _client()
    try:
        resp = await client.post(f"/channels/{CHANNEL}/mcp-servers/test", json={"url": "not-a-url"})
        assert resp.status_code == 422
    finally:
        await client.aclose()
