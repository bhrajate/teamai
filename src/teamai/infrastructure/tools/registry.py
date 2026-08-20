"""工具注册表：持有全部工具，按频道白名单裁剪出本次 run 可见的工具集。

工具本身是 pydantic-ai 的 ``Tool``（由带类型标注的函数生成），schema 与参数
校验都由 pydantic-ai 负责，这里只做两件事：

1. 按频道 ``allowed_tools`` 裁剪。未授权的工具根本不出现在发给模型的工具列表里，
   所以不需要再在调用时二次鉴权——模型无法调用它看不见的工具。
2. 把 ``ToolUnavailable`` 收口成一条错误文本（见 ``_GracefulToolset``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Tool
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool, WrapperToolset

from teamai.domain.models.skill import Skill
from teamai.domain.ports import ToolBundle, ToolProvider
from teamai.infrastructure.tools.base import ToolUnavailable, fail
from teamai.infrastructure.tools.skill_tool import build_skill_file_tool, build_skill_tool


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

    @staticmethod
    def _is_server_entry(name: str) -> bool:
        """白名单条目是否为 server 级挂载（``mcp__<server>``，恰好一段）。

        McpServer.name 只允许 ``[a-z0-9-]``，不会含 ``__``，故分隔符计数可靠：
        一段是 server 级条目，两段是完整工具名。
        """
        return name.startswith("mcp__") and name.count("__") == 1

    def _expand_allowed(self, allowed: list[str]) -> list[str]:
        """把白名单展开成精确工具名列表。

        - ``mcp__<server>`` → server 级挂载，展开为该 server 注册的全部工具
        - 完整工具名（``mcp__<server>__<tool>`` 或内置名）→ 原样保留
        - 未注册名字保留给下方过滤（策略里可能残留已下线或未连上的 server 名，
          展开结果为空即被自然忽略）
        """
        expanded: list[str] = []
        for name in allowed:
            if self._is_server_entry(name):
                prefix = f"{name}__"
                expanded.extend(t for t in self._tools if t.startswith(prefix))
            else:
                expanded.append(name)
        return expanded

    def for_channel(
        self,
        allowed: list[str],
        skills: Sequence[Skill] | None = None,
    ) -> ToolBundle | None:
        """按白名单裁出工具集，必要时补上 ``load_skill``。

        返回值对上层不透明，只有 gateway 会解释它。未注册的名字直接忽略
        （策略里可能残留已下线的工具名）。

        ``load_skill`` 不参与白名单裁剪，而是在 ``skills`` 非空时无条件挂上 ——
        频道启用 skill 本身即授权，理由见 ``ToolProvider.for_channel`` 的文档。
        它也不进 ``self._tools``：那份是全局注册表，而这个工具闭包捕获了本次
        run 的 skill 集合，存进去会让下一个频道拿到上一个频道的技能。

        白名单空、且本频道无启用的 skill 时返回 ``None``（不挂任何工具）。
        """
        selected = [
            tool for name in self._expand_allowed(allowed) if (tool := self._tools.get(name)) is not None
        ]
        if skills:
            selected.append(build_skill_tool(skills))
            # read_skill_file 只在真有文件时才挂：一个永远返回「没有这个文件」的
            # 工具会占着模型的注意力，且诱导它去猜路径。
            if any(s.files for s in skills):
                selected.append(build_skill_file_tool(skills))
        if not selected:
            return None
        return _GracefulToolset(FunctionToolset(selected))
