"""模型注册表：按 model_level 预装配 pydantic-ai Agent。

每次 build 返回全新 Agent 实例并按频道权限注册工具，
避免共享实例带来的跨频道工具/状态污染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext

from teamai.config import Settings
from teamai.tools.base import BaseTool


@dataclass
class ModelConfig:
    light_primary: str = "claude-sonnet-4-5"
    light_fallback: str = "claude-3-5-haiku"
    full: str = "claude-opus-4-8"

    @classmethod
    def from_settings(cls, s: Settings) -> ModelConfig:
        return cls(
            light_primary=s.model_light_primary,
            light_fallback=s.model_light_fallback,
            full=s.model_full,
        )


class ModelRegistry:
    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def build(self, level: str, tools: list[BaseTool] | None = None) -> Any:
        """返回全新 Agent 实例，并按 tools 列表注册频道允许的工具。

        - ``light``：FallbackModel(primary -> fallback)，主模型失败自动降级
        - ``full``：旗舰模型
        """
        from pydantic_ai import Agent
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.models.fallback import FallbackModel

        if level == "full":
            model = AnthropicModel(self._config.full)
        else:
            model = FallbackModel(
                AnthropicModel(self._config.light_primary),
                AnthropicModel(self._config.light_fallback),
            )
        agent = Agent(model, deps_type=dict)
        for tool in tools or []:
            self._register_tool(agent, tool)
        return agent

    @staticmethod
    def _register_tool(agent: Any, tool: BaseTool) -> None:
        """将 BaseTool 包装为 pydantic-ai 工具注册到 agent。

        使用唯一函数名注册，避免闭包循环覆盖。
        """
        async def _impl(ctx: RunContext[dict], **kwargs: Any) -> str:
            deps = ctx.deps or {}
            policy = deps.get("policy")
            result = await tool.call(kwargs, policy)
            if not result.ok:
                return f"错误: {result.error}"
            return str(result.data)

        _impl.__name__ = f"tool_{tool.name.replace('.', '_')}"
        _impl.__doc__ = f"{tool.description}\n\n参数 Schema: {tool.input_schema}"
        agent.tool(_impl)
