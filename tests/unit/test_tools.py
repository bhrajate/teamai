"""工具层测试。

重点锁死迁移到 pydantic-ai 原生工具后拿回的两项能力：

1. 手写 JSON Schema 的年代，工具以 ``**kwargs`` 注册，发给模型的 schema 实际是
   ``{"properties": {}, "additionalProperties": true}``——参数全靠 docstring 里
   的散文描述。这里断言 schema 里真的有 properties 与枚举约束。
2. 非法参数由 pydantic-ai 在调用前拦下并回灌校验错误，工具体不会执行。

用 FunctionModel 充当模型：它能拿到本次 run 发给模型的工具定义，也能按脚本发起
一次工具调用，因此不需要真实网络或真实模型。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

from teamai.infrastructure.tools.base import ToolUnavailable, check_http, fail, ok
from teamai.infrastructure.tools.crm_tool import build_crm_tool
from teamai.infrastructure.tools.github_tool import build_github_tool
from teamai.infrastructure.tools.monitoring_tool import build_monitoring_tool
from teamai.infrastructure.tools.registry import ToolRegistry


class _Recorder:
    """记下模型看到的工具定义，并可按脚本发起一次工具调用。

    ``__name__`` 是 FunctionModel 的硬性要求（它据此生成 model_name）。
    """

    __name__ = "recorder"

    def __init__(self, call: tuple[str, dict] | None = None) -> None:
        self.tools: list[ToolDefinition] = []
        self._call = call
        self._step = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.tools = list(info.function_tools)
        self._step += 1
        if self._call is not None and self._step == 1:
            return ModelResponse(parts=[ToolCallPart(self._call[0], self._call[1])])
        return ModelResponse(parts=[TextPart("done")])


async def _run(registry: ToolRegistry, allowed: list[str], call: tuple[str, dict] | None = None):
    toolset = registry.for_channel(allowed)
    recorder = _Recorder(call)
    agent = Agent(FunctionModel(recorder), toolsets=[toolset] if toolset is not None else None)
    result = await agent.run("go")
    return recorder, result


def _parts(result, kind: type) -> list:
    return [p for m in result.all_messages() for p in m.parts if isinstance(p, kind)]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """三个工具都装上，且都带凭据（未配置路径另有专测）。"""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    reg = ToolRegistry()
    reg.register(build_github_tool(token="tok"))
    reg.register(build_monitoring_tool(endpoint="https://mon.example", api_key="k"))
    reg.register(build_crm_tool(instance_url="https://crm.example", token="k"))
    return reg


def test_工具名与库内白名单契约一致(registry: ToolRegistry) -> None:
    """DB 里存的是这三个名字，改名会让既有频道策略失效。"""
    assert sorted(registry.names) == ["crm", "github", "monitoring"]


async def test_发给模型的schema含参数与枚举约束(registry: ToolRegistry) -> None:
    recorder, _ = await _run(registry, ["github"])

    (tool,) = recorder.tools
    schema = tool.parameters_json_schema
    # 回归点：**kwargs 写法下这里是空 dict
    assert schema["properties"], "schema 没有参数，模型只能靠猜"
    assert schema["properties"]["action"]["enum"] == ["read_file", "list_issues", "create_pr"]
    assert schema["required"] == ["action", "repo"]
    # 参数描述来自 docstring 的 Args 段，而非塞进 description 的字典字面量
    assert schema["properties"]["repo"]["description"]
    assert "参数 Schema" not in (tool.description or "")


@pytest.mark.parametrize("name", ["github", "monitoring", "crm"])
async def test_每个工具都有非空schema(registry: ToolRegistry, name: str) -> None:
    recorder, _ = await _run(registry, [name])
    (tool,) = recorder.tools
    assert tool.parameters_json_schema["properties"], f"{name} 的 schema 为空"


async def test_只挂载频道授权的工具(registry: ToolRegistry) -> None:
    recorder, _ = await _run(registry, ["github", "crm"])
    assert sorted(t.name for t in recorder.tools) == ["crm", "github"]


async def test_未注册的工具名被忽略(registry: ToolRegistry) -> None:
    recorder, _ = await _run(registry, ["github", "已下线的工具"])
    assert [t.name for t in recorder.tools] == ["github"]


@pytest.mark.parametrize("allowed", [[], ["全都不认识"]])
def test_无可用工具时不挂工具集(registry: ToolRegistry, allowed: list[str]) -> None:
    assert registry.for_channel(allowed) is None


async def test_非法枚举值在调用前被拦下(registry: ToolRegistry) -> None:
    _, result = await _run(registry, ["github"], ("github", {"action": "rm_rf", "repo": "o/r"}))

    retries = _parts(result, RetryPromptPart)
    assert retries, "非法 action 应被 pydantic-ai 拦下并回灌校验错误"
    assert "read_file" in retries[0].model_response()
    assert not _parts(result, ToolReturnPart), "工具体不应被执行"


async def test_条件必填参数缺失时要求模型重试(registry: ToolRegistry) -> None:
    """path 只在 action=read_file 时必填，schema 表达不了，由函数体兜。"""
    _, result = await _run(registry, ["github"], ("github", {"action": "read_file", "repo": "o/r"}))

    retries = _parts(result, RetryPromptPart)
    assert retries and "path" in retries[0].model_response()


async def test_未配置凭据返回错误文本而非炸掉任务(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    reg = ToolRegistry()
    reg.register(build_github_tool())

    _, result = await _run(reg, ["github"], ("github", {"action": "list_issues", "repo": "o/r"}))

    (ret,) = _parts(result, ToolReturnPart)
    payload = json.loads(ret.content)
    assert payload == {"ok": False, "error": "GitHub 未配置访问令牌（GITHUB_TOKEN）"}


async def test_工具不可用被收口成错误文本(monkeypatch: pytest.MonkeyPatch) -> None:
    """ToolUnavailable 不该冒到 run 顶层，应转成模型能读的错误。"""

    def boom(**_: object) -> None:
        raise ToolUnavailable("凭据无效")

    monkeypatch.setattr("teamai.infrastructure.tools.github_tool._list_issues", lambda *a, **k: boom())
    reg = ToolRegistry()
    reg.register(build_github_tool(token="tok"))

    _, result = await _run(reg, ["github"], ("github", {"action": "list_issues", "repo": "o/r"}))

    (ret,) = _parts(result, ToolReturnPart)
    assert json.loads(ret.content) == {"ok": False, "error": "凭据无效"}


def test_结果是合法json而非python字面量() -> None:
    payload = ok(issues=[{"number": 1}])
    assert json.loads(payload) == {"ok": True, "issues": [{"number": 1}]}
    assert json.loads(fail("坏了")) == {"ok": False, "error": "坏了"}


@pytest.mark.parametrize("status", [401, 403])
def test_凭据类错误不重试(status: int) -> None:
    resp = httpx.Response(status_code=status, text="denied")
    with pytest.raises(ToolUnavailable):
        check_http(resp, "读取文件")


@pytest.mark.parametrize("status", [404, 429, 500, 502])
def test_可自救或抖动类错误让模型重试(status: int) -> None:
    resp = httpx.Response(status_code=status, text="oops")
    with pytest.raises(ModelRetry):
        check_http(resp, "读取文件")


def test_成功状态码直接通过() -> None:
    check_http(httpx.Response(status_code=200, text="ok"), "读取文件")
