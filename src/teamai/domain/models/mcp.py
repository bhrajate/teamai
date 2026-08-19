"""MCP server 领域模型。

一个频道可挂多个 MCP server（streamable HTTP），每个 server 的工具以
``mcp__<server_name>__<tool_name>`` 进入该频道的工具白名单机制。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# name 的字符约束：它会拼进工具名前缀 ``mcp__<name>__``，白名单与
# ToolRegistry 都要解析这个名字，只允许小写字母数字与连字符。
NAME_PATTERN = r"^[a-z0-9-]+$"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class McpServer:
    id: str
    channel_instance_id: str
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    # 最近一次连接失败原因（worker 启动快照，不是实时探活）
    last_error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def tool_prefix(self) -> str:
        """该 server 工具的命名前缀：`mcp__<name>__`。"""
        return f"mcp__{self.name}__"
