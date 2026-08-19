"""McpService 装载行为：注册、失败落 last_error、成功清旧错误。

仓储用 fakes.FakeMcpServerRepository；server 用 conftest 的 mcp_server_url
fixture（真实 FastMCP + uvicorn）。
"""

from __future__ import annotations

import pytest

from teamai.application.mcp import McpService
from teamai.domain.models.mcp import McpServer
from teamai.infrastructure.mcp.client import ConnectorFactory
from teamai.infrastructure.tools.registry import ToolRegistry
from teamai.infrastructure.uow import NullUnitOfWork
from tests.conftest import free_port
from tests.fakes import FakeMcpServerRepository


def _server(server_id: str, name: str, url: str, enabled: bool = True) -> McpServer:
    return McpServer(
        id=server_id,
        channel_instance_id="ch_1",
        name=name,
        url=url,
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_装载注册server级工具名(mcp_server_url: str):
    repo = FakeMcpServerRepository([_server("mcp_1", "github", mcp_server_url)])
    registry = ToolRegistry()
    service = McpService(repo, registry, NullUnitOfWork(), ConnectorFactory())

    await service.load_and_register()

    assert sorted(registry.names) == [
        "mcp__github__add",
        "mcp__github__boom",
        "mcp__github__greet",
    ]
    await service.aclose()


@pytest.mark.asyncio
async def test_连接失败记last_error且不注册工具(mcp_server_url: str):
    dead_port = free_port()  # 没有服务在监听
    repo = FakeMcpServerRepository(
        [
            _server("mcp_ok", "good", mcp_server_url),
            _server("mcp_bad", "dead", f"http://127.0.0.1:{dead_port}/mcp"),
        ]
    )
    registry = ToolRegistry()
    service = McpService(repo, registry, NullUnitOfWork(), ConnectorFactory())

    await service.load_and_register()

    # 好的注册了，坏的没有
    assert sorted(registry.names) == [
        "mcp__good__add",
        "mcp__good__boom",
        "mcp__good__greet",
    ]
    # 失败快照落库（前端可见）
    dead = next(s for s in repo._servers.values() if s.name == "dead")
    assert dead.last_error and "连接 MCP server 失败" in dead.last_error
    good = next(s for s in repo._servers.values() if s.name == "good")
    assert good.last_error is None
    await service.aclose()


@pytest.mark.asyncio
async def test_连接成功清掉旧last_error(mcp_server_url: str):
    server = _server("mcp_1", "github", mcp_server_url)
    server.last_error = "上一次启动时连不上"
    repo = FakeMcpServerRepository([server])
    service = McpService(repo, ToolRegistry(), NullUnitOfWork(), ConnectorFactory())

    await service.load_and_register()

    assert server.last_error is None, "连接成功应清掉旧失败快照"
    await service.aclose()


@pytest.mark.asyncio
async def test_disabled的server不装载(mcp_server_url: str):
    repo = FakeMcpServerRepository([_server("mcp_1", "off", mcp_server_url, enabled=False)])
    registry = ToolRegistry()
    service = McpService(repo, registry, NullUnitOfWork(), ConnectorFactory())

    await service.load_and_register()

    assert registry.names == []
    await service.aclose()
