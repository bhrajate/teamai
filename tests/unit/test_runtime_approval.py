"""AgentRuntime 的审批装配。

runtime 只负责三件事：把策略里的审批配置透传给工具集、识别 run 因待批而中断、
把待批项与历史交出去。「谁能批」在 ApprovalService，「通知谁」在 router。

两处漏了会静默失效：
- 不透传 approvals → 闸挂不上，危险工具照跑
- 不透传 approval_results → 批准了也恢复不了，任务永远卡着
"""

from __future__ import annotations

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.runtime import AgentRuntime, StageStatus
from teamai.config import Settings
from teamai.domain.models import ChannelInstance, PermissionPolicy, Task
from teamai.domain.ports import (
    ApprovalDecision,
    ApprovalRequest,
    LLMGateway,
    LLMResult,
    ToolBundle,
)
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeBudgetRepository


class SpyGateway(LLMGateway):
    """记参数；可按需返回「待批」结果。"""

    def __init__(self, *, pending: list[ApprovalRequest] | None = None, tokens: int = 100) -> None:
        self.calls: list[dict] = []
        self._pending = pending or []
        self._tokens = tokens

    async def run(
        self,
        prompt: str,
        *,
        model_level: str,
        system_prompt: str = "",
        tools: ToolBundle | None = None,
        token_limit: int | None = None,
        history: bytes | None = None,
        on_checkpoint: object | None = None,
        approval_results: object | None = None,
    ) -> LLMResult:
        self.calls.append(
            {"history": history, "approval_results": approval_results, "prompt": prompt}
        )
        if self._pending:
            return LLMResult(
                output="",
                tokens=self._tokens,
                pending_approvals=list(self._pending),
                history=b"resume-me",
            )
        return LLMResult(output="做完了", tokens=self._tokens)


class SpyTools:
    def __init__(self) -> None:
        self.asked_approvals: list[dict] = []

    def register(self, tool: object) -> None: ...

    def for_channel(
        self, allowed: list[str], skills: object = None, approvals: object = None
    ) -> ToolBundle | None:
        self.asked_approvals.append(dict(approvals or {}))
        return "<toolset>"


def _bundle(approval_tools: dict[str, int] | None = None) -> ContextBundle:
    instance = ChannelInstance(
        id="ch1", platform="slack", channel_id="C1", workspace_id="W1", agent_identity="teamai"
    )
    return ContextBundle(
        task_id="task_1",
        channel_instance_id="ch1",
        user_prompt="提个 PR",
        system_prompt="（系统提示词）",
        model_level="light",
        instance=instance,
        policy=PermissionPolicy(
            id="p1",
            channel_instance_id="ch1",
            allowed_tools=["github"],
            approval_required_tools=approval_tools or {},
        ),
        allowed_tools=["github"],
    )


def _task() -> Task:
    return Task(
        id="task_1", channel_instance_id="ch1", thread_ref="ts1", requester_id="U9", intent="ask"
    )


def _runtime(gateway: LLMGateway, tools: SpyTools) -> AgentRuntime:
    audit = AuditLogWriter(FakeAuditRepository())
    from teamai.application.budget import BudgetController

    return AgentRuntime(
        gateway,
        tools,  # type: ignore[arg-type]
        BudgetController(FakeBudgetRepository(), audit),
        audit,
        Settings(context_max_messages=60, context_summary_threshold=120),
    )


# ---- 透传配置 ----


async def test_审批配置透传给工具集() -> None:
    """漏了的话闸挂不上，危险工具照跑 —— 静默失效。"""
    tools = SpyTools()
    await _runtime(SpyGateway(), tools).run(_task(), _bundle({"github": 1}))

    assert tools.asked_approvals == [{"github": 1}]


async def test_无策略时不传审批配置() -> None:
    tools = SpyTools()
    bundle = _bundle()
    bundle.policy = None

    await _runtime(SpyGateway(), tools).run(_task(), bundle)

    assert tools.asked_approvals == [{}]


async def test_未配审批时为空() -> None:
    tools = SpyTools()
    await _runtime(SpyGateway(), tools).run(_task(), _bundle())
    assert tools.asked_approvals == [{}]


# ---- 待批中断 ----


async def test_待批时状态是AWAITING_APPROVAL() -> None:
    """与 PAUSED 分开：那个是预算耗尽（要追加配额），这个是等人点头。"""
    gw = SpyGateway(pending=[ApprovalRequest("tc_1", "github", {"title": "修 bug"})])

    r = await _runtime(gw, SpyTools()).run(_task(), _bundle({"github": 1}))

    assert r.status is StageStatus.AWAITING_APPROVAL
    assert r.status is not StageStatus.PAUSED


async def test_待批时交出待批项与历史() -> None:
    """调用方要拿这两样去 ApprovalService.record_request。"""
    gw = SpyGateway(pending=[ApprovalRequest("tc_1", "github", {"title": "修 bug"})])

    r = await _runtime(gw, SpyTools()).run(_task(), _bundle({"github": 1}))

    assert [x.tool_name for x in r.pending_approvals] == ["github"]
    assert r.pending_approvals[0].args == {"title": "修 bug"}
    assert r.pending_approvals[0].tool_call_id == "tc_1"
    assert r.approval_history == b"resume-me"


async def test_待批时output为空() -> None:
    """那时的 output 不是给用户的答复，调用方不该把它发出去。"""
    gw = SpyGateway(pending=[ApprovalRequest("tc_1", "github")])

    r = await _runtime(gw, SpyTools()).run(_task(), _bundle({"github": 1}))

    assert r.output == ""


async def test_待批也计费() -> None:
    """已经花掉的 token 要算 —— 中断不等于免费。"""
    gw = SpyGateway(pending=[ApprovalRequest("tc_1", "github")], tokens=250)

    r = await _runtime(gw, SpyTools()).run(_task(), _bundle({"github": 1}))

    assert r.usage_tokens == 250


async def test_待批写审计并标注原因() -> None:
    repo = FakeAuditRepository()
    audit = AuditLogWriter(repo)
    from teamai.application.budget import BudgetController

    gw = SpyGateway(pending=[ApprovalRequest("tc_1", "github")])
    runtime = AgentRuntime(
        gw,
        SpyTools(),  # type: ignore[arg-type]
        BudgetController(FakeBudgetRepository(), audit),
        audit,
        Settings(context_max_messages=60, context_summary_threshold=120),
    )

    await runtime.run(_task(), _bundle({"github": 1}))

    detail = repo.logs[-1].detail
    assert detail["to"] == "WAITING_INPUT"
    assert detail["reason"] == "tool_approval_required"
    assert detail["tools"] == ["github"]


# ---- 恢复 ----


async def test_恢复时透传裁决() -> None:
    """漏了的话批准了也恢复不了，任务永远卡着。"""
    gw = SpyGateway()
    decisions = {"tc_1": ApprovalDecision(approved=True)}

    await _runtime(gw, SpyTools()).run(_task(), _bundle({"github": 1}), decisions)

    assert gw.calls[0]["approval_results"] == decisions


async def test_不传裁决时为None() -> None:
    gw = SpyGateway()
    await _runtime(gw, SpyTools()).run(_task(), _bundle())
    assert gw.calls[0]["approval_results"] is None


async def test_恢复后跑完是DONE() -> None:
    gw = SpyGateway()

    r = await _runtime(gw, SpyTools()).run(
        _task(), _bundle({"github": 1}), {"tc_1": ApprovalDecision(approved=True)}
    )

    assert r.status is StageStatus.DONE
    assert r.output == "做完了"


async def test_多个待批一起交出() -> None:
    """模型可能在同一轮里发起多个需审批的调用。"""
    gw = SpyGateway(
        pending=[ApprovalRequest("tc_1", "github"), ApprovalRequest("tc_2", "mcp__deploy")]
    )

    r = await _runtime(gw, SpyTools()).run(_task(), _bundle({"github": 1, "mcp__deploy": 2}))

    assert [x.tool_name for x in r.pending_approvals] == ["github", "mcp__deploy"]
