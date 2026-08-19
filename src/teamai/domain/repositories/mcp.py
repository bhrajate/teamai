"""MCP server 仓储抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from teamai.domain.models.mcp import McpServer


class McpServerRepository(ABC):
    @abstractmethod
    async def list_for_channel(self, channel_instance_id: str) -> list[McpServer]: ...

    @abstractmethod
    async def list_enabled(self) -> list[McpServer]: ...

    @abstractmethod
    async def get(self, channel_instance_id: str, server_id: str) -> McpServer | None: ...

    @abstractmethod
    async def find_by_name(self, channel_instance_id: str, name: str) -> McpServer | None: ...

    @abstractmethod
    async def upsert(self, server: McpServer) -> None: ...

    @abstractmethod
    async def delete(self, channel_instance_id: str, server_id: str) -> None: ...
