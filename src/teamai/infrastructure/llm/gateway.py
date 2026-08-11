"""LLMGateway 的 pydantic-ai 实现。

领域端口只声明「发起一次带工具的模型调用」；具体怎么选模型、怎么降级、怎么把
SDK 的超限信号翻译成域信号，都收在这里。用例层因此不再 import 任何 SDK。

模型 ID 取 `provider:model` 形式，provider 段同时决定协议与端点，故换供应商
（Anthropic / OpenAI chat / OpenAI responses / DeepSeek / Ollama ...）只改配置、
不改代码 —— 分发交给 pydantic-ai 的 `infer_model`，本模块不写协议分支。

每次调用都新建 Agent 实例：Agent 持有工具集，而工具集按频道裁剪，
复用实例会造成跨频道的工具污染。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, UsageLimitExceeded, UsageLimits
from pydantic_ai.models import infer_model, parse_model_id
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class
from pydantic_ai.toolsets import AbstractToolset

from teamai.config import Settings
from teamai.domain.ports import LLMGateway, LLMResult, TokenBudgetExceeded, ToolBundle

# 裸模型名（不带 `provider:` 前缀）的归属。历史配置里的三个默认值都是
# Anthropic 模型，且 parse_model_id 对无前缀的名字返回 provider=None
# 会让 infer_model 抛 UserError，故在此补默认前缀而非报错。
DEFAULT_PROVIDER = "anthropic"


@dataclass
class ModelConfig:
    """模型档位到模型 ID 的映射，外加端点覆盖。领域只认 light / full。

    三个 ID 取 `provider:model` 形式；`base_url` / `api_key` 留空即走各
    provider 的默认行为（官方端点 + 各家自己认的环境变量）。
    """

    light_primary: str = "claude-sonnet-4-5"
    light_fallback: str = "claude-3-5-haiku"
    full: str = "claude-opus-4-8"
    base_url: str = ""
    api_key: str = ""

    @classmethod
    def from_settings(cls, s: Settings) -> ModelConfig:
        return cls(
            light_primary=s.model_light_primary,
            light_fallback=s.model_light_fallback,
            full=s.model_full,
            base_url=s.llm_base_url,
            api_key=s.llm_api_key,
        )


def _normalize(model_id: str) -> str:
    """给裸模型名补上默认 provider 前缀，已带前缀的原样返回。"""
    provider, _ = parse_model_id(model_id)
    return model_id if provider is not None else f"{DEFAULT_PROVIDER}:{model_id}"


class PydanticAIGateway(LLMGateway):
    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def _provider(self, name: str) -> Provider[Any]:
        """按 provider 名构造实例，按需注入 base_url / api_key。

        两处都不硬编码 provider 名单：
        - 未配 base_url 与 api_key 时直接交给 `infer_provider`，行为与不传
          provider 完全一致（各家 SDK 自读它认的环境变量）；
        - 配了则按 `__init__` 签名决定能塞哪个参数 —— `AnthropicProvider` 与
          `OpenAIProvider` 都收 base_url + api_key，而 `DeepSeekProvider`
          这类自带固定端点的只收 api_key，硬塞会 TypeError（已在 2.25.0 上验证）。

        `gateway/` 前缀（pydantic-ai 自家的模型网关）走另一套构造路径，
        不接受这两个参数，原样交回 SDK。
        """
        if name.startswith("gateway/") or not (self._config.base_url or self._config.api_key):
            return infer_provider(name)

        cls = infer_provider_class(name)
        params = inspect.signature(cls.__init__).parameters
        kwargs: dict[str, str] = {}
        if self._config.base_url and "base_url" in params:
            kwargs["base_url"] = self._config.base_url
        if self._config.api_key and "api_key" in params:
            kwargs["api_key"] = self._config.api_key
        return cls(**kwargs) if kwargs else infer_provider(name)

    def _build(self, model_id: str) -> Any:
        return infer_model(_normalize(model_id), provider_factory=self._provider)

    def _model(self, level: str) -> Any:
        """``full`` 用旗舰模型；``light`` 用 FallbackModel，主模型失败自动降级。"""
        if level == "full":
            return self._build(self._config.full)
        return FallbackModel(
            self._build(self._config.light_primary),
            self._build(self._config.light_fallback),
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
