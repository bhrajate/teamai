"""MCP 连接端口。

与 ToolBundle 同哲学：领域层只关心「连上一个 server、拿到一批工具」这件事，
不关心工具形状（pydantic-ai Tool 对领域不透明），具体协议由 infrastructure
实现方负责。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class McpConnectionError(Exception):
    """连接、握手或拉取工具列表失败（URL 错 / 服务未起 / 协议不符）。"""


class McpSessionPort(ABC):
    """一个已连接 MCP server 的会话：connect 后长活，工具调用期间保持连接。"""

    @abstractmethod
    async def connect(self, server_name: str | None = None) -> list[Any]:
        """握手并返回该 server 的全部工具（对领域不透明）。

        传 ``server_name`` 时工具以 ``mcp__<server_name>__<tool>`` 命名，
        供注册进工具白名单；不传（连接测试）保持 server 原名。
        失败抛 :class:`McpConnectionError`。
        """
        ...

    @abstractmethod
    async def aclose(self) -> None: ...


class McpConnectorFactory(ABC):
    """创建会话的工厂：McpService 与连接测试共用。"""

    @abstractmethod
    def create(
        self, url: str, headers: dict[str, str] | None = None
    ) -> McpSessionPort: ...

    @abstractmethod
    async def probe(self, url: str, headers: dict[str, str] | None = None) -> list[str]:
        """握手一次并返回工具名列表，供配置时的连接测试。"""
        ...
