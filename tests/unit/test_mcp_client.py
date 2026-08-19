"""McpSession / test_connection 的集成行为。

server 由 tests/conftest.py 的共享 fixture 起在随机端口的真实 uvicorn 上。
"""

from __future__ import annotations

import pytest

from teamai.domain.ports.mcp import McpConnectionError
from teamai.infrastructure.mcp.client import McpSession
from teamai.infrastructure.mcp.client import test_connection as mcp_test_connection
from tests.conftest import free_port


@pytest.mark.asyncio
async def test_connect返回工具元数据(mcp_server_url: str):
    session = McpSession(mcp_server_url)
    try:
        tools = await session.connect()
        by_name = {t.name: t for t in tools}
        assert set(by_name) == {"add", "greet", "boom"}
        assert by_name["add"].description == "Add two numbers."
        schema = by_name["add"].tool_def.parameters_json_schema
        assert schema["properties"]["a"]["type"] == "integer"
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_工具调用返回文本(mcp_server_url: str):
    session = McpSession(mcp_server_url)
    try:
        tools = await session.connect()
        add = next(t for t in tools if t.name == "add")
        # t.function 是 from_schema 注入的调用函数（私有属性，测试直取）
        assert await add.function(a=2, b=3) == "5"
        greet = next(t for t in tools if t.name == "greet")
        assert await greet.function(name="世界") == "hello 世界"
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_工具报错转为错误文本_不抛出(mcp_server_url: str):
    session = McpSession(mcp_server_url)
    try:
        tools = await session.connect()
        boom = next(t for t in tools if t.name == "boom")
        text = await boom.function()
        assert text.startswith("工具调用失败：")
        assert "内部分裂" in text
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_连接失败抛McpConnectionError():
    port = free_port()  # 没有服务在监听
    session = McpSession(f"http://127.0.0.1:{port}/mcp")
    with pytest.raises(McpConnectionError):
        await session.connect()


@pytest.mark.asyncio
async def test_test_connection返回工具名列表(mcp_server_url: str):
    names = await mcp_test_connection(mcp_server_url)
    assert sorted(names) == ["add", "boom", "greet"]


@pytest.mark.asyncio
async def test_server_name前缀拼进工具名(mcp_server_url: str):
    session = McpSession(mcp_server_url)
    try:
        tools = await session.connect("github")
        assert sorted(t.name for t in tools) == [
            "mcp__github__add",
            "mcp__github__boom",
            "mcp__github__greet",
        ]
    finally:
        await session.aclose()
