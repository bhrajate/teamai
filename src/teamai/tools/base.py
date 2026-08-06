"""工具抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    error: str | None = None


class ToolError(Exception):
    """工具调用失败。"""


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    async def call(self, args: dict[str, Any], auth_scope: object) -> ToolResult:
        """执行工具调用。auth_scope 为频道权限上下文。"""
