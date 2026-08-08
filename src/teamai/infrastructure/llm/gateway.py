"""LLMGateway 的 pydantic-ai 实现。

领域端口只声明「发起一次带工具的模型调用」；具体怎么选模型、怎么降级、怎么把
SDK 的超限信号翻译成域信号，都收在这里。用例层因此不再 import 任何 SDK。

每次调用都新建 Agent 实例：Agent 持有工具集，而工具集按频道裁剪，
复用实例会造成跨频道的工具污染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, UsageLimitExceeded, UsageLimits
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.toolsets import AbstractToolset

from teamai.config import Settings
from teamai.domain.ports import LLMGateway, LLMResult, TokenBudgetExceeded, ToolBundle


@dataclass
class ModelConfig:
    """模型档位到具体模型 ID 的映射。属基础设施细节，领域只认 light / full。"""

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


class PydanticAIGateway(LLMGateway):
    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def _model(self, level: str) -> Any:
        """``full`` 用旗舰模型；``light`` 用 FallbackModel，主模型失败自动降级。"""
        if level == "full":
            return AnthropicModel(self._config.full)
        return FallbackModel(
            AnthropicModel(self._config.light_primary),
            AnthropicModel(self._config.light_fallback),
        )

    async def run(
        self,
        prompt: str,
        *,
        model_level: str,
        system_prompt: str = "",
        tools: ToolBundle | None = None,
        token_limit: int | None = None,
    ) -> LLMResult:
        toolsets: list[AbstractToolset[Any]] | None = None
        if tools is not None:
            # ToolBundle 对领域不透明，到这一层才解释成 pydantic-ai 的工具集。
            # 类型不符属装配错误（container 接错了实现），尽早炸掉而非静默降级。
            if not isinstance(tools, AbstractToolset):
                raise TypeError(f"工具集类型不匹配：期望 AbstractToolset，收到 {type(tools).__name__}")
            toolsets = [tools]

        agent = Agent(
            self._model(model_level),
            instructions=system_prompt or None,
            toolsets=toolsets,
        )
        limits = UsageLimits(total_tokens_limit=max(token_limit, 1)) if token_limit is not None else None

        try:
            result = await agent.run(prompt, usage_limits=limits)
        except UsageLimitExceeded as exc:
            raise TokenBudgetExceeded(str(exc)) from exc
        return LLMResult(output=str(result.output), tokens=int(getattr(result.usage, "total_tokens", 0)))
