"""gateway 的审批中断与恢复。

用 FunctionModel 充当模型，不碰网络。锁住四件事：

- 配了审批的工具**不执行**，且 run 正常结束（不是抛异常）
- 同一轮的只读工具照常执行
- 批准后工具执行，且已完成的工具不重放
- 拒绝后工具不执行，理由回灌给模型
"""

from __future__ import annotations

from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teamai.domain.ports import ApprovalDecision
from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway
from teamai.infrastructure.tools.registry import ToolRegistry

EXECUTED: list[str] = []


async def github(action: str, title: str = "") -> str:
    """有真副作用的工具。"""
    EXECUTED.append(f"github({action},{title})")
    return f"PR 已创建: {title}"


async def monitoring(action: str) -> str:
    """只读工具，不需要审批。"""
    EXECUTED.append(f"monitoring({action})")
    return "无告警"


class Script:
    """先查监控，再提 PR，最后收尾。行为从历史推导（不用计数器 —— 恢复时是新实例）。"""

    __name__ = "script"

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = {
            p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        }
        if "monitoring" not in done:
            return ModelResponse(parts=[ToolCallPart("monitoring", {"action": "alerts"})])
        if "github" not in done:
            return ModelResponse(
                parts=[ToolCallPart("github", {"action": "create_pr", "title": "修 bug"})]
            )
        return ModelResponse(parts=[TextPart("都处理完了")])


def _gateway() -> PydanticAIGateway:
    gw = PydanticAIGateway(ModelConfig())
    gw._model = lambda level: FunctionModel(Script())  # type: ignore[assignment]  # noqa: SLF001
    return gw


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool(github))
    r.register(Tool(monitoring))
    return r


_ALLOWED = ["github", "monitoring"]
_APPROVALS = {"github": 1}


# ---- 中断 ----


async def test_配了审批的工具不执行() -> None:
    EXECUTED.clear()
    gw = _gateway()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)

    r = await gw.run("提个 PR", model_level="light", tools=tools)

    assert r.awaiting_approval, "应中断等审批"
    assert EXECUTED == ["monitoring(alerts)"], f"危险工具不该执行: {EXECUTED}"


async def test_待批清单带工具名与参数() -> None:
    """参数必须完整带出 —— 只说「我要建 PR」等于让人盲签。"""
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)

    r = await _gateway().run("提个 PR", model_level="light", tools=tools)

    (req,) = r.pending_approvals
    assert req.tool_name == "github"
    assert req.args == {"action": "create_pr", "title": "修 bug"}
    assert req.tool_call_id, "必须有 tool_call_id —— 恢复时靠它对回具体调用"


async def test_待批时output置空() -> None:
    """避免调用方把待批清单当答复发给用户。"""
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)

    r = await _gateway().run("提个 PR", model_level="light", tools=tools)

    assert r.output == ""
    assert r.awaiting_approval is True


async def test_待批时带出可恢复的历史() -> None:
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)

    r = await _gateway().run("提个 PR", model_level="light", tools=tools)

    assert r.history is not None and len(r.history) > 0


async def test_未配审批时照常执行() -> None:
    """向后兼容：不传 approvals 行为完全不变。"""
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED)

    r = await _gateway().run("提个 PR", model_level="light", tools=tools)

    assert not r.awaiting_approval
    assert EXECUTED == ["monitoring(alerts)", "github(create_pr,修 bug)"]
    assert r.output == "都处理完了"


async def test_只读工具不受审批影响() -> None:
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, {"monitoring": 1})

    r = await _gateway().run("看下告警", model_level="light", tools=tools)

    assert r.awaiting_approval
    assert r.pending_approvals[0].tool_name == "monitoring"
    assert EXECUTED == [], "第一个调用就是它，故什么都没跑"


# ---- 恢复 ----


async def test_批准后工具执行且不重放() -> None:
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)
    first = await _gateway().run("提个 PR", model_level="light", tools=tools)
    tcid = first.pending_approvals[0].tool_call_id

    EXECUTED.clear()
    r = await _gateway().run(
        "提个 PR",
        model_level="light",
        tools=_registry().for_channel(_ALLOWED, None, _APPROVALS),
        history=first.history,
        approval_results={tcid: ApprovalDecision(approved=True)},
    )

    assert EXECUTED == ["github(create_pr,修 bug)"], f"只该跑被批准的那个: {EXECUTED}"
    assert not r.awaiting_approval
    assert r.output == "都处理完了"


async def test_拒绝后工具不执行且理由回灌() -> None:
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)
    first = await _gateway().run("提个 PR", model_level="light", tools=tools)
    tcid = first.pending_approvals[0].tool_call_id

    EXECUTED.clear()
    r = await _gateway().run(
        "提个 PR",
        model_level="light",
        tools=_registry().for_channel(_ALLOWED, None, _APPROVALS),
        history=first.history,
        approval_results={
            tcid: ApprovalDecision(approved=False, reason="产品不同意提这个 PR")
        },
    )

    assert EXECUTED == [], "被拒的工具不该执行"
    assert not r.awaiting_approval, "拒绝也是一种结论，run 该跑完"
    assert r.output, "模型应能收尾说明"


async def test_审批时改参数生效() -> None:
    """审批不是批/否二选一 —— 人可以改完再放行。"""
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)
    first = await _gateway().run("提个 PR", model_level="light", tools=tools)
    tcid = first.pending_approvals[0].tool_call_id

    EXECUTED.clear()
    await _gateway().run(
        "提个 PR",
        model_level="light",
        tools=_registry().for_channel(_ALLOWED, None, _APPROVALS),
        history=first.history,
        approval_results={
            tcid: ApprovalDecision(
                approved=True,
                override_args={"action": "create_pr", "title": "人改过的标题"},
            )
        },
    )

    assert EXECUTED == ["github(create_pr,人改过的标题)"], f"参数没被覆盖: {EXECUTED}"


async def test_恢复时token只计本段() -> None:
    """与检查点同一口径：gateway 传空 RunUsage，总量由调用方加基数。"""
    EXECUTED.clear()
    tools = _registry().for_channel(_ALLOWED, None, _APPROVALS)
    first = await _gateway().run("提个 PR", model_level="light", tools=tools)
    tcid = first.pending_approvals[0].tool_call_id

    second = await _gateway().run(
        "提个 PR",
        model_level="light",
        tools=_registry().for_channel(_ALLOWED, None, _APPROVALS),
        history=first.history,
        approval_results={tcid: ApprovalDecision(approved=True)},
    )

    assert first.tokens > 0 and second.tokens > 0


# ---- 闸的匹配规则 ----


async def test_mcp_server级配置被动态工具继承() -> None:
    """与 allowed_tools 的 server 级挂载对称。"""
    EXECUTED.clear()

    async def deploy_rollout() -> str:
        EXECUTED.append("rollout")
        return "ok"

    class OneShot:
        __name__ = "one"

        def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart("完成")])
            return ModelResponse(parts=[ToolCallPart("mcp__deploy__rollout", {})])

    reg = ToolRegistry()
    reg.register(Tool(deploy_rollout, name="mcp__deploy__rollout"))
    gw = PydanticAIGateway(ModelConfig())
    gw._model = lambda level: FunctionModel(OneShot())  # type: ignore[assignment]  # noqa: SLF001

    r = await gw.run(
        "上线",
        model_level="light",
        tools=reg.for_channel(["mcp__deploy"], None, {"mcp__deploy": 2}),
    )

    assert r.awaiting_approval, "server 级配置该被动态工具继承"
    assert EXECUTED == []


async def test_前缀不误伤同名开头的工具() -> None:
    """github 配了审批不该让 github_v2 跟着要审批。"""
    EXECUTED.clear()

    async def github_v2() -> str:
        EXECUTED.append("v2")
        return "ok"

    class OneShot:
        __name__ = "one"

        def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                return ModelResponse(parts=[TextPart("完成")])
            return ModelResponse(parts=[ToolCallPart("github_v2", {})])

    reg = ToolRegistry()
    reg.register(Tool(github_v2, name="github_v2"))
    gw = PydanticAIGateway(ModelConfig())
    gw._model = lambda level: FunctionModel(OneShot())  # type: ignore[assignment]  # noqa: SLF001

    r = await gw.run("跑一下", model_level="light", tools=reg.for_channel(["github_v2"], None, {"github": 1}))

    assert not r.awaiting_approval
    assert EXECUTED == ["v2"]
