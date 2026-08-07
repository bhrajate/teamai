"""工具注册表：注册、鉴权、调用分发。"""

from __future__ import annotations

from teamai.domain.policy import PermissionPolicy
from teamai.tools.base import BaseTool, ToolError, ToolResult


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    def can_call(self, name: str, policy: PermissionPolicy | None) -> bool:
        if policy is None:
            return False
        return policy.can_use_tool(name)

    async def call(self, name: str, args: dict, policy: PermissionPolicy | None) -> ToolResult:
        """按权限白名单调用工具。未授权返回 DENIED 结果，不抛异常。"""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, data={}, error=f"未知工具: {name}")
        if not self.can_call(name, policy):
            return ToolResult(ok=False, data={}, error=f"工具 {name} 未获频道授权")
        try:
            return await tool.call(args, policy)
        except ToolError as exc:
            return ToolResult(ok=False, data={}, error=str(exc))
        except Exception as exc:  # pragma: no cover
            return ToolResult(ok=False, data={}, error=f"工具 {name} 调用异常: {exc}")
