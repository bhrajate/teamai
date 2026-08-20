"""AgentRuntime 测试。

倒置后 runtime 是纯用例层策略：预算、审计、上下文压缩、prompt 组装。
模型调用与工具集都走 domain port，故这里用纯内存假实现，不碰 pydantic-ai。

同时锁住一条此前的缺陷修复：system_prompt 现在真的传给了模型。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.runtime import AgentRuntime, StageStatus
from teamai.application.budget import BudgetController
from teamai.config import Settings
from teamai.domain.models import (
    AuditResult,
    BudgetQuota,
    BudgetScope,
    ChannelInstance,
    PermissionPolicy,
    Skill,
    Task,
)
from teamai.domain.ports import (
    LLMGateway,
    LLMResult,
    ThreadMessage,
    TokenBudgetExceeded,
    ToolBundle,
)
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository, FakeBudgetRepository


class SpyGateway(LLMGateway):
    """记下每次调用的参数；可按需模拟 token 超限与中途检查点。"""

    def __init__(
        self,
        *,
        tokens: int = 120,
        exceed: bool = False,
        checkpoints: list[int] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._tokens = tokens
        self._exceed = exceed
        # 要回调的检查点序列，元素是「本段截至此刻的累计 token」。
        # 用来验证运行时按增量计费，而非每次都扣总量。
        self._checkpoints = checkpoints or []

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
            {
                "prompt": prompt,
                "model_level": model_level,
                "system_prompt": system_prompt,
                "tools": tools,
                "token_limit": token_limit,
                "history": history,
                "on_checkpoint": on_checkpoint,
            }
        )
        if on_checkpoint is not None:
            for i, seg in enumerate(self._checkpoints, 1):
                await on_checkpoint(f"cp-{i}".encode(), seg)  # type: ignore[operator]
        if self._exceed:
            raise TokenBudgetExceeded("token 上限")
        return LLMResult(output="已处理", tokens=self._tokens)


class SpyToolProvider:
    """记下被问过哪些白名单与 skill，并返回一个可辨识的假句柄。"""

    def __init__(self, bundle: ToolBundle | None = "<toolset>") -> None:
        self.asked: list[list[str]] = []
        # 每次 for_channel 收到的 skill 名。运行时必须把 bundle.skills 透传下去，
        # 否则频道启用了技能而 load_skill 工具挂不上。
        self.asked_skills: list[list[str]] = []
        self.asked_approvals: list[dict[str, int]] = []
        self._bundle = bundle

    def for_channel(
        self,
        allowed: list[str],
        skills: Sequence[Skill] | None = None,
        approvals: Mapping[str, int] | None = None,
    ) -> ToolBundle | None:
        self.asked.append(list(allowed))
        self.asked_skills.append([s.name for s in skills or []])
        # 需审批的工具配置。runtime 必须把它透传下去，否则闸挂不上、
        # 危险工具照跑 —— 而那是静默失效。
        self.asked_approvals.append(dict(approvals or {}))
        # 有 skill 时即便白名单为空也该有工具集（load_skill 不受白名单管制）
        return self._bundle if (allowed or skills) else None


def _bundle(allowed: list[str], *, history: list[str] | None = None) -> ContextBundle:
    instance = ChannelInstance(
        id="ch1",
        platform="slack",
        channel_id="C1",
        workspace_id="W1",
        agent_identity="teamai",
    )
    return ContextBundle(
        task_id="t1",
        channel_instance_id="ch1",
        user_prompt="帮我看下昨天的告警",
        system_prompt="（系统提示词）",
        model_level="light",
        instance=instance,
        policy=PermissionPolicy(id="p1", channel_instance_id="ch1", allowed_tools=allowed),
        allowed_tools=allowed,
        thread_history=history or [],
    )


def _task() -> Task:
    return Task(id="t1", channel_instance_id="ch1", thread_ref="ts1", requester_id="u1", intent="ask")


@pytest.fixture
def audit_repo() -> FakeAuditRepository:
    return FakeAuditRepository()


def _runtime(
    gateway: LLMGateway,
    tools: SpyToolProvider,
    audit_repo: FakeAuditRepository,
    quota: BudgetQuota | None = None,
) -> tuple[AgentRuntime, FakeBudgetRepository]:
    budget_repo = FakeBudgetRepository(quota)
    audit = AuditLogWriter(audit_repo)
    runtime = AgentRuntime(
        gateway,
        tools,
        BudgetController(budget_repo, audit),
        audit,
        Settings(context_max_messages=60, context_summary_threshold=120),
    )
    return runtime, budget_repo


async def test_按白名单取工具集并原样交给gateway(audit_repo: FakeAuditRepository) -> None:
    gateway = SpyGateway()
    tools = SpyToolProvider()
    runtime, _ = _runtime(gateway, tools, audit_repo)

    result = await runtime.run(_task(), _bundle(["monitoring"]))

    assert result.status is StageStatus.DONE
    assert tools.asked == [["monitoring"]]
    assert gateway.calls[0]["tools"] == "<toolset>"
    assert gateway.calls[0]["model_level"] == "light"


async def test_白名单为空时不挂工具(audit_repo: FakeAuditRepository) -> None:
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo)

    await runtime.run(_task(), _bundle([]))

    assert gateway.calls[0]["tools"] is None


async def test_系统提示词传到了模型(audit_repo: FakeAuditRepository) -> None:
    """回归点：倒置前 bundle.system_prompt 组装完就被丢掉，从未发给模型。"""
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo)

    await runtime.run(_task(), _bundle(["github"]))

    assert gateway.calls[0]["system_prompt"] == "（系统提示词）"


async def test_记忆与线程历史拼进提示词(audit_repo: FakeAuditRepository) -> None:
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo)

    history = [ThreadMessage(author_id="U9", text="昨天聊过部署")]
    await runtime.run(_task(), _bundle(["github"], history=history))

    prompt = gateway.calls[0]["prompt"]
    assert "帮我看下昨天的告警" in prompt
    assert "昨天聊过部署" in prompt


async def test_线程历史标出机器人自己的发言(audit_repo: FakeAuditRepository) -> None:
    """混作一堆无署名文本时，模型容易把自己上一轮的输出当成用户诉求。"""
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo)

    history = [
        ThreadMessage(author_id="U9", text="部署失败了"),
        ThreadMessage(author_id="B1", text="我看下日志", is_self=True),
    ]
    await runtime.run(_task(), _bundle(["github"], history=history))

    prompt = gateway.calls[0]["prompt"]
    assert "U9: 部署失败了" in prompt
    assert "AI: 我看下日志" in prompt


async def test_压缩掉的历史条数在提示词里说明(audit_repo: FakeAuditRepository) -> None:
    """不说明的话，模型会把「历史只有这么多」当成事实。"""
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo)
    # settings.context_max_messages 默认 60，造 62 条触发压缩
    history = [ThreadMessage(author_id="U1", text=f"第 {i} 句") for i in range(62)]

    await runtime.run(_task(), _bundle(["github"], history=history))

    prompt = gateway.calls[0]["prompt"]
    assert "更早的 2 条已省略" in prompt
    assert "第 0 句" not in prompt, "最旧的应被丢弃"
    assert "第 61 句" in prompt


async def test_成功后扣预算并写审计(audit_repo: FakeAuditRepository) -> None:
    quota = BudgetQuota(id="b1", scope=BudgetScope.CHANNEL, channel_instance_id="ch1", token_limit=10_000)
    gateway = SpyGateway(tokens=120)
    runtime, budget_repo = _runtime(gateway, SpyToolProvider(), audit_repo, quota)

    result = await runtime.run(_task(), _bundle(["github"]))

    assert result.status is StageStatus.DONE
    assert result.output == "已处理"
    assert result.usage_tokens == 120
    assert budget_repo.quota is not None and budget_repo.quota.used_tokens == 120
    (log,) = audit_repo.logs
    assert log.detail["to"] == "DONE"
    assert log.result is AuditResult.SUCCESS


async def test_剩余配额作为本次token上限(audit_repo: FakeAuditRepository) -> None:
    quota = BudgetQuota(
        id="b1", scope=BudgetScope.CHANNEL, channel_instance_id="ch1", token_limit=10_000, used_tokens=9_000
    )
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo, quota)

    await runtime.run(_task(), _bundle(["github"]))

    assert gateway.calls[0]["token_limit"] == 1_000


async def test_配额耗尽直接暂停且不调模型(audit_repo: FakeAuditRepository) -> None:
    quota = BudgetQuota(
        id="b1", scope=BudgetScope.CHANNEL, channel_instance_id="ch1", token_limit=10_000, used_tokens=10_000
    )
    gateway = SpyGateway()
    runtime, _ = _runtime(gateway, SpyToolProvider(), audit_repo, quota)

    result = await runtime.run(_task(), _bundle(["github"]))

    assert result.status is StageStatus.PAUSED
    assert gateway.calls == [], "配额不足时不应调用模型"
    (log,) = audit_repo.logs
    assert log.result is AuditResult.PAUSED
    assert log.detail["reason"] == "budget"


async def test_token超限转暂停而非失败(audit_repo: FakeAuditRepository) -> None:
    """域异常 TokenBudgetExceeded 驱动 PAUSED，用例层不认识任何 SDK 异常。"""
    quota = BudgetQuota(id="b1", scope=BudgetScope.CHANNEL, channel_instance_id="ch1", token_limit=10_000)
    runtime, _ = _runtime(SpyGateway(exceed=True), SpyToolProvider(), audit_repo, quota)

    result = await runtime.run(_task(), _bundle(["github"]))

    assert result.status is StageStatus.PAUSED
    (log,) = audit_repo.logs
    assert log.result is AuditResult.PAUSED
    assert log.detail["reason"] == "token_budget_exceeded"
