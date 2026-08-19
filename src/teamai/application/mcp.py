"""MCP server 装载服务。

把 DB 里配置的 MCP server 变成运行时可调的工具：worker 启动时调用
``load_and_register``，对每个 enabled 的 server 连接、握手，工具以
``mcp__<server>__<tool>`` 注册进工具注册表 —— 与内置工具同构，白名单
裁剪与错误收口全复用现有机制。

连接失败的 server 不阻塞启动：记 ``last_error`` 落库（前端可见），其工具
不注册；白名单里残留的 ``mcp__<server>`` 条目由 registry 自然忽略
（与「策略里残留已下线工具名」的现有语义一致）。

连接与注册目标都走 domain 端口（McpConnectorFactory / ToolProvider），
本层不感知具体 SDK。
"""

from __future__ import annotations

import contextlib
import logging

from teamai.domain.models.mcp import McpServer
from teamai.domain.ports.mcp import McpConnectionError, McpConnectorFactory, McpSessionPort
from teamai.domain.ports.tools import ToolProvider
from teamai.domain.ports.uow import UnitOfWork
from teamai.domain.repositories.mcp import McpServerRepository

logger = logging.getLogger(__name__)


class McpService:
    def __init__(
        self,
        repo: McpServerRepository,
        registry: ToolProvider,
        uow: UnitOfWork,
        connectors: McpConnectorFactory,
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._uow = uow
        self._connectors = connectors
        self._sessions: list[McpSessionPort] = []

    async def load_and_register(self) -> None:
        """读全部 enabled 的 MCP server 并注册其工具（启动时调用一次）。"""
        servers = await self._repo.list_enabled()
        for server in servers:
            await self._register_one(server)
        if servers:
            logger.info(f"MCP server 装载完成: {len(self._sessions)}/{len(servers)} 个连接成功")

    async def _register_one(self, server: McpServer) -> None:
        session = self._connectors.create(server.url, server.headers)
        try:
            tools = await session.connect(server.name)
        except McpConnectionError as exc:
            logger.warning(f"MCP server {server.name} 连接失败: {exc}")
            await self._record_error(server, str(exc))
            return

        for tool in tools:
            self._registry.register(tool)
        if len(tools):
            logger.info(f"MCP server {server.name}: 注册 {len(tools)} 个工具")
        self._sessions.append(session)
        # 连接成功要清掉上一次的失败快照，否则前端一直显示旧错误
        if server.last_error:
            server.last_error = None
            async with self._uow:
                await self._repo.upsert(server)

    async def _record_error(self, server: McpServer, error: str) -> None:
        server.last_error = error
        async with self._uow:
            await self._repo.upsert(server)

    async def test_connection(self, url: str, headers: dict[str, str] | None = None) -> list[str]:
        """握手一次并返回工具名列表，供管理控制台配置时验证连接。"""
        return await self._connectors.probe(url, headers)

    async def aclose(self) -> None:
        """关闭全部长活会话（进程退出路径）。"""
        for session in self._sessions:
            with contextlib.suppress(Exception):
                await session.aclose()
        self._sessions.clear()
