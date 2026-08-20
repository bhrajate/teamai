"""LLMGateway 的 pydantic-ai 实现。

领域端口只声明「发起一次带工具的模型调用」；具体怎么选模型、怎么降级、怎么把
SDK 的超限信号翻译成域信号，都收在这里。用例层因此不再 import 任何 SDK。

模型 ID 取 `provider:model` 形式，provider 段同时决定协议与端点，故换供应商
（Anthropic / OpenAI chat / OpenAI responses / DeepSeek / Ollama ...）只改配置、
不改代码 —— 分发交给 pydantic-ai 的 `infer_model`，本模块不写协议分支。

每次调用都新建 Agent 实例：Agent 持有工具集，而工具集按频道裁剪，
复用实例会造成跨频道的工具污染。

## 为什么用 agent.iter() 而不是 agent.run()

`run()` 把整个工具调用循环包在一个 await 里，中途完全不可观测 —— worker 崩在
第三次工具调用时，前两次的结果一并作废。`iter()` 把图节点交出来，让我们在干净
的轮边界把消息历史存下来，崩溃后据此续跑（见 docs/SPEC-agent-checkpoint.md）。

不传 `on_checkpoint` 时行为与改造前一致：照样一路迭代到 End，只是不落检查点。
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, UsageLimitExceeded, UsageLimits
from pydantic_ai._agent_graph import ModelRequestNode
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import infer_model, parse_model_id
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from teamai.config import Settings
from teamai.domain.ports import (
    CheckpointSink,
    LLMGateway,
    LLMResult,
    TokenBudgetExceeded,
    ToolBundle,
)

logger = logging.getLogger(__name__)

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


def _dangling(messages: list[ModelMessage]) -> int:
    """未被应答的工具调用数。

    按 ``tool_call_id`` 配对而非靠位置推断：一轮里可以有多个并行工具调用，
    按位置数会把「两个 call + 两个 return」误判成有悬空。

    带悬空调用的历史**不能**用于续跑 —— SDK 直接抛
    ``UserError: Cannot provide a new user prompt when the message history
    contains unprocessed tool calls``（实测），所以这是落检查点的硬前提。
    """
    calls: set[str] = set()
    returns: set[str] = set()
    for m in messages:
        for p in m.parts:
            if isinstance(p, ToolCallPart):
                calls.add(p.tool_call_id)
            elif isinstance(p, ToolReturnPart):
                returns.add(p.tool_call_id)
    return len(calls - returns)


def _has_tool_return(messages: list[ModelMessage]) -> bool:
    """历史里是否已有工具结果。

    用 ``isinstance`` 而非比较类名字符串：后者会随 SDK 版本悄悄失效，而失效
    表现是「再也不落检查点」—— 没有任何报错，只是崩溃后又变成从头重跑。
    """
    return any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)


def _total_tokens(usage: Any) -> int:
    """从 usage 取总量。2.x 里 ``run.usage`` 是 property 而非方法。"""
    return int(getattr(usage, "total_tokens", 0) or 0)


def _actual_model_id(result: Any) -> str:
    """从运行结果里取**实际生效**的模型，拼成 `provider:model`。

    不能拿配置里的档位反推：light 档走 FallbackModel(primary → fallback)，
    主模型失败时真正跑的是备用模型，而两者单价可能差数倍。交互记录要按这个
    做成本归因，所以必须问结果本身。

    取不到就返回空串：这只是留痕字段，缺了不影响任务，不值得为它抛异常。
    """
    try:
        response = result.response
    except Exception:  # pragma: no cover - 无 ModelResponse 的极端情况
        return ""
    model = getattr(response, "model_name", None) or ""
    provider = getattr(response, "provider_name", None) or ""
    if model and provider:
        return f"{provider}:{model}"
    return model


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
        history: bytes | None = None,
        on_checkpoint: CheckpointSink | None = None,
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
        hist = ModelMessagesTypeAdapter.validate_json(history) if history else None

        try:
            result = await self._iterate(
                agent,
                prompt,
                hist=hist,
                limits=limits,
                on_checkpoint=on_checkpoint,
            )
        except UsageLimitExceeded as exc:
            raise TokenBudgetExceeded(str(exc)) from exc

        # RunUsage 在 pydantic-ai 2.x 里是 property（早期版本曾是方法），
        # 且分报 input/output —— 两者单价差数倍，只记 total 没法做成本归因。
        usage = result.usage
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0) or tokens_in + tokens_out
        return LLMResult(
            output=str(result.output),
            tokens=total,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_id=_actual_model_id(result),
        )

    @staticmethod
    async def _iterate(
        agent: Agent,
        prompt: str,
        *,
        hist: list[ModelMessage] | None,
        limits: UsageLimits | None,
        on_checkpoint: CheckpointSink | None,
    ) -> Any:
        """驱动 agent 图，并在干净的轮边界回调 ``on_checkpoint``。

        续跑时（``hist`` 非空）传 ``None`` 作为 user prompt：原始提问已是历史的
        第一条，再给一次 SDK 会抛 ``UserError``（实测）。

        `usage` 传空 ``RunUsage()``，于是 ``run.usage`` 只统计**本段**用量。
        这是有意的：``UsageLimits`` 作用在传入的 usage 对象上，若传入累计值，
        上限的语义就变成「整个任务的总上限」，而调用方给的 ``token_limit`` 是
        「当前剩余配额」，两者对不上。总量由调用方自己加基数。
        """
        last_blob: bytes | None = None
        async with agent.iter(
            None if hist else prompt,
            message_history=hist,
            usage=RunUsage(),
            usage_limits=limits,
        ) as run:
            async for node in run:
                if on_checkpoint is None or not isinstance(node, ModelRequestNode):
                    continue

                # ⚠️ 必须把 node.request 拼上。`ctx.state.message_history` 滞后
                # 一轮：工具已经执行完，但承载其结果的那条 ModelRequest 还没进
                # state —— 它此刻只由节点自己持有。只看 state 的话，「悬空==0
                # 且有工具结果」在整条 run 里只在终点成立，对续跑毫无用处。
                candidate = [*run.ctx.state.message_history, node.request]
                if _dangling(candidate) or not _has_tool_return(candidate):
                    continue

                blob = ModelMessagesTypeAdapter.dump_json(candidate)
                if blob == last_blob:
                    continue
                last_blob = blob
                try:
                    await on_checkpoint(blob, _total_tokens(run.usage))
                except Exception as exc:
                    # 落不下检查点不该毁掉一次正在成功的 run —— 最坏结果只是
                    # 崩溃后从更早的点重来。与 AgentRuntime 对留痕失败的处置同理。
                    logger.warning(f"检查点持久化失败: {exc}")

        if run.result is None:  # pragma: no cover - 正常迭代必然产出 End
            raise RuntimeError("agent 迭代结束但没有结果")
        return run.result
