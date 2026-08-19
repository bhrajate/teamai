"""MCP streamable HTTP 客户端封装。

把 fastmcp 的 Client 接进现有工具体系：连接成功后将 server 的每个工具转成
pydantic-ai ``Tool``（``Tool.from_schema``，参数 schema 保持 server 下发的
动态 JSON schema），以 ``mcp__<server>__<tool>`` 命名 —— 与内置工具同构，
白名单裁剪与错误收口全复用现有机制。

调用侧分两种生命周期：
- 启动注册（McpService）：session 长活，持有 client 供工具调用，退出时 aclose
- 连接测试（admin test 端点）：connect 后立即 aclose，只取工具清单
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import mcp.types as mcp_types
from fastmcp import Client
from pydantic_ai import Tool
from pydantic_ai.mcp import StreamableHttpTransport

from teamai.domain.ports.mcp import (
    McpConnectionError,
    McpConnectorFactory,
    McpSessionPort,
)

logger = logging.getLogger(__name__)


def _tool_result_to_text(result: mcp_types.CallToolResult) -> str:
    """把 CallToolResult 的 content 块拼成文本返回给模型。

    只取文本块；图像/资源类块首期不支持，标注为不可渲染。is_error 时原样
    返回错误文本 —— MCP 工具的业务错误不该炸 run，模型看到原因后自行向
    用户说明或换参数重试（与 _GracefulToolset 的收口哲学一致）。
    """
    parts: list[str] = []
    for block in result.content:
        if block.type == "text":
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} 内容块，首期不支持渲染]")
    text = "\n".join(parts).strip()
    if result.is_error:
        return f"工具调用失败：{text or '未知错误'}"
    return text


class McpSession(McpSessionPort):
    """一个已连接 MCP server 的会话。连接失败在 ``connect`` 处抛错。

    fastmcp 的 Client 把连接生命周期绑在 ``async with`` 上（进入时建 session、
    退出时复位）。长活会话用 ``AsyncExitStack`` 保持进入状态，``aclose`` 时才
    退出 —— 与 worker 同寿的工具调用都在这期间发生。
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._url = url
        self._headers = headers or None
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[Tool] = []

    async def connect(self, server_name: str | None = None) -> list[Tool]:
        """握手并构建该 server 全部工具的 pydantic-ai Tool 列表。"""
        # 注册名前缀：server 名拼进工具名（mcp__<server>__<tool>）。
        # 连接测试不关心注册名，传 None 保持原名。
        name_prefix = f"mcp__{server_name}__" if server_name else ""
        stack = AsyncExitStack()
        try:
            client = Client(
                StreamableHttpTransport(self._url, headers=self._headers)
            )
            # auto_initialize=True（默认）：进入上下文即完成握手
            await stack.enter_async_context(client)
            server_tools = await client.list_tools()
        except Exception as exc:
            # fastmcp 的异常链含 transport 层细节，剥到可读的根因文本
            await stack.aclose()
            raise McpConnectionError(f"连接 MCP server 失败：{exc}") from exc

        self._stack = stack
        self._client = client
        self._tools = [self._build_tool(t, name_prefix) for t in server_tools]
        return self._tools

    def _build_tool(self, server_tool: mcp_types.Tool, name_prefix: str) -> Tool:
        """把 server 下发的一个工具定义包成 pydantic-ai Tool。

        调用函数统一走 ``client.call_tool`` —— 参数 schema 以 server 下发的
        inputSchema 为准（Tool.from_schema），本层不做二次建模。
        """
        client = self._client
        assert client is not None  # connect 成功后才会走到这里

        async def _call(**kwargs: Any) -> str:
            # fastmcp 对工具执行错误默认直接抛异常（raise_on_error=True）。
            # 业务错误不该炸掉整个 run：转成文本让模型看到原因后自行向用户
            # 说明或换参数 —— 与 _GracefulToolset 的收口哲学一致。
            try:
                result = await client.call_tool(server_tool.name, kwargs)
            except Exception as exc:
                return f"工具调用失败：{exc}"
            return _tool_result_to_text(result)

        return Tool.from_schema(
            _call,
            name=f"{name_prefix}{server_tool.name}",
            description=server_tool.description,
            json_schema=server_tool.inputSchema,
        )

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None


async def test_connection(url: str, headers: dict[str, str] | None = None) -> list[str]:
    """握手一次并返回工具名列表，用于配置时的连接测试。"""
    session = McpSession(url, headers)
    try:
        tools = await session.connect()
        return [t.name for t in tools]
    finally:
        await session.aclose()


class ConnectorFactory(McpConnectorFactory):
    def create(
        self, url: str, headers: dict[str, str] | None = None
    ) -> McpSessionPort:
        return McpSession(url, headers)

    async def probe(self, url: str, headers: dict[str, str] | None = None) -> list[str]:
        return await test_connection(url, headers)
