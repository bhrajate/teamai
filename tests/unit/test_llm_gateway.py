"""LLM 网关的模型/协议装配。

只验「配置 → 模型类 + 端点」这段映射，不发真实请求：换供应商靠改配置而非改
代码，这条保证若失效必须立刻可见。

同时钉住两个对 pydantic-ai 版本敏感的结论（升级时这些断言会先红）：
- `openai:` 前缀打 Responses API，`openai-chat:` 打 Chat Completions；
- 自带固定端点的 provider（deepseek）不接受 base_url，须被跳过而非报错。
"""

from __future__ import annotations

import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from teamai.config import Settings
from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway, _normalize

BASE = "https://gw.test/v1"


def _gw(**kw: str) -> PydanticAIGateway:
    """api_key 必给：provider 拿不到 key 会在构造期抛 UserError。"""
    kw.setdefault("api_key", "k")
    return PydanticAIGateway(ModelConfig(**kw))


class Test裸模型名兼容:
    def test_无前缀补默认provider(self) -> None:
        assert _normalize("claude-opus-4-8") == "anthropic:claude-opus-4-8"

    def test_已带前缀不改动(self) -> None:
        assert _normalize("openai-chat:gpt-5") == "openai-chat:gpt-5"

    def test_旧配置仍装配成anthropic(self) -> None:
        """三个默认值都是裸名，改动前的配置不能因此失效。"""
        assert isinstance(_gw()._model("full"), AnthropicModel)


class Test协议由前缀决定:
    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("anthropic:claude-opus-4-8", AnthropicModel),
            ("openai-chat:gpt-5", OpenAIChatModel),  # /v1/chat/completions
            ("openai:gpt-5", OpenAIResponsesModel),  # /v1/responses
        ],
    )
    def test_前缀映射到模型类(self, model_id: str, expected: type) -> None:
        assert isinstance(_gw(full=model_id)._model("full"), expected)

    def test_三档可混用不同供应商(self) -> None:
        gw = _gw(
            full="anthropic:claude-opus-4-8",
            light_primary="openai-chat:gpt-5-mini",
            light_fallback="deepseek:deepseek-chat",
        )
        primary, fallback = gw._model("light").models
        assert isinstance(gw._model("full"), AnthropicModel)
        assert isinstance(primary, OpenAIChatModel)
        assert isinstance(fallback, OpenAIChatModel)  # deepseek 走 OpenAI 兼容类


class Test端点注入:
    def test_配了base_url则生效(self) -> None:
        assert _gw(base_url=BASE)._model("full").base_url.startswith(BASE)

    def test_自带端点的provider忽略base_url(self) -> None:
        """DeepSeekProvider 不接受 base_url，硬塞会 TypeError，须按签名跳过。"""
        model = _gw(full="deepseek:deepseek-chat", base_url=BASE)._model("full")
        assert "deepseek.com" in model.base_url

    def test_未配base_url不报错(self) -> None:
        """留空即交回 SDK 默认行为，不应因缺参数而失败。"""
        assert _gw()._model("full").base_url


class Test档位结构:
    def test_full为单模型(self) -> None:
        assert not isinstance(_gw()._model("full"), FallbackModel)

    def test_light为双模型降级链(self) -> None:
        light = _gw()._model("light")
        assert isinstance(light, FallbackModel)
        assert [m.model_name for m in light.models] == ["claude-sonnet-4-5", "claude-3-5-haiku"]


class Test从Settings装配:
    def test_带上端点与凭据(self) -> None:
        cfg = ModelConfig.from_settings(
            Settings(model_full="openai-chat:gpt-5", llm_base_url=BASE, llm_api_key="secret")
        )
        assert (cfg.full, cfg.base_url, cfg.api_key) == ("openai-chat:gpt-5", BASE, "secret")

    def test_key兼容旧环境变量名(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """协议可配后 key 改名 llm_api_key，但旧 .env 的 ANTHROPIC_API_KEY 不能失效。"""
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-old-name")
        assert Settings().llm_api_key == "from-old-name"

    def test_新名优先于旧名(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "old")
        monkeypatch.setenv("LLM_API_KEY", "new")
        assert Settings().llm_api_key == "new"
