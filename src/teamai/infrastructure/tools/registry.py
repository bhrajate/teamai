"""工具注册表：持有全部工具，按频道白名单裁剪出本次 run 可见的工具集。

工具本身是 pydantic-ai 的 ``Tool``（由带类型标注的函数生成），schema 与参数
校验都由 pydantic-ai 负责，这里只做两件事：

1. 按频道 ``allowed_tools`` 裁剪。未授权的工具根本不出现在发给模型的工具列表里，
   所以不需要再在调用时二次鉴权——模型无法调用它看不见的工具。
2. 把 ``ToolUnavailable`` 收口成一条错误文本（见 ``_GracefulToolset``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Tool
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool, WrapperToolset

from teamai.domain.ports import ToolBundle, ToolProvider
from teamai.infrastructure.tools.base import ToolUnavailable, fail


@dataclass
class _GracefulToolset(WrapperToolset[Any]):
    """把 ``ToolUnavailable`` 转成错误文本，而不是让异常冒到 run 顶层。

    缺凭据、集成点未实现这类问题，重试和换参数都无济于事，但也不该让整个任务
    FAILED——更有用的行为是让模型收到原因并向用户说明。可重试的失败仍以
    ``ModelRetry`` 形式向上传播，交给 pydantic-ai 的重试机制处理。
    """

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except ToolUnavailable as exc:
            return fail(str(exc))


class ToolRegistry(ToolProvider):
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    def for_channel(self, allowed: list[str]) -> ToolBundle | None:
        """按白名单裁出工具集。返回值对上层不透明，只有 gateway 会解释它。

        未注册的名字直接忽略（策略里可能残留已下线的工具名）。白名单为空或
        全部未命中时返回 ``None``，表示本次调用不挂任何工具。
        """
        selected = [tool for name in allowed if (tool := self._tools.get(name)) is not None]
        if not selected:
            return None
        return _GracefulToolset(FunctionToolset(selected))
