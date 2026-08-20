"""router 的审批分支：通知、/approve、/deny。

覆盖的接缝：待批时的状态迁移与通知文案、审批指令的解析与绑定、审批人配不出来
时的拒绝路径。「谁能批」的规则本身在 test_approval_service.py。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from teamai.application.agent.runtime import StageResult, StageStatus
from teamai.application.approval import ApprovalService
from teamai.application.budget import BudgetController
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter, _approval_prompt, _parse_overrides
from teamai.domain.models import (
    ChannelInstance,
    PendingApproval,
    PermissionPolicy,
    Task,
    TaskStatus,
)
from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.ports import ApprovalRequest
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.domain.services import AuditLogWriter
from tests.doubles import (
    FakeChannels,
    FakeConversation,
    FakeDistiller,
    FakeIntentClassifier,
    FakeMemory,
    FakeRuntime,
    FakeTags,
    mention,
)
from tests.fakes import FakeAuditRepository, FakeBudgetRepository, FakeTaskQueue, FakeTaskRepository

CH = "ch1"


class FakeCheckpoints(CheckpointRepository):
    def __init__(self) -> None:
        self.store: dict[str, TaskCheckpoint] = {}
        self.pending: dict[str, PendingApproval] = {}
        self.cleared: list[str] = []

    async def get(self, task_id: str) -> TaskCheckpoint | None:
        return self.store.get(task_id)

    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None:
        self.store[task_id] = TaskCheckpoint(task_id, messages, tokens_used)

    async def delete(self, task_id: str) -> None:
        self.store.pop(task_id, None)
        self.pending.pop(task_id, None)

    async def bump_attempts(self, task_id: str) -> int:
        return 0

    async def set_pending_approval(
        self, task_id: str, messages: bytes, pending: PendingApproval
    ) -> None:
        self.pending[task_id] = pending
        self.store[task_id] = TaskCheckpoint(task_id, messages, 0)

    async def get_pending_approval(self, task_id: str) -> PendingApproval | None:
        return self.pending.get(task_id)

    async def clear_pending_approval(self, task_id: str) -> None:
        self.cleared.append(task_id)
        self.pending.pop(task_id, None)

    async def list_pending_before(self, cutoff: datetime) -> list[str]:
        return list(self.pending)


class FakePolicyRepo:
    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self._policy = policy

    async def get_for_channel(self, channel_instance_id: str) -> PermissionPolicy | None:
        return self._policy

    async def upsert(self, policy: PermissionPolicy) -> None: ...


def _instance() -> ChannelInstance:
    return ChannelInstance(
        id=CH, platform="slack", channel_id="C1", workspace_id="W1", agent_identity="teamai"
    )


def _policy(approver_ids: list[str] | None = None, **kw) -> PermissionPolicy:
    """⚠️ 用 `is None` 而非 `or` 判默认值：显式传 `[]`（测「没有审批人」那条路）
    会被 `or` 变回默认名单，那条最要紧的用例就测不到了。"""
    return PermissionPolicy(
        id="p1",
        channel_instance_id=CH,
        allowed_tools=["github"],
        approval_required_tools=kw.pop("approval_required_tools", {"github": 1}),
        approver_ids=["U1"] if approver_ids is None else approver_ids,
    )


class Env:
    def __init__(
        self,
        runtime: FakeRuntime,
        policy: PermissionPolicy | None = None,
        *,
        with_approvals: bool = True,
    ) -> None:
        self.tasks = FakeTaskRepository()
        self.audit_repo = FakeAuditRepository()
        audit = AuditLogWriter(self.audit_repo)
        self.checkpoints = FakeCheckpoints()
        self.approvals = ApprovalService(self.checkpoints, audit) if with_approvals else None
        self.orchestrator = TaskOrchestrator(self.tasks, audit, FakeTaskQueue())
        self.router = MessageRouter(
            orchestrator=self.orchestrator,
            intent=FakeIntentClassifier("query"),
            tags=FakeTags(),
            memory=FakeMemory(),
            budget=BudgetController(FakeBudgetRepository(), audit),
            runtime=runtime,
            channels=FakeChannels(_instance()),
            policy_repo=FakePolicyRepo(policy),
            conversation=FakeConversation(),
            distiller=FakeDistiller(),
            approvals=self.approvals,
        )

    def events(self) -> list[str]:
        return [x.detail.get("event", "") for x in self.audit_repo.logs]


def _awaiting(tool: str = "github", **args) -> FakeRuntime:
    """一个返回「待批」的 runtime。"""
    r = FakeRuntime()
    r._result = StageResult(  # noqa: SLF001
        status=StageStatus.AWAITING_APPROVAL,
        pending_approvals=[ApprovalRequest("tc_1", tool, args or {"title": "修 bug"})],
        approval_history=b"resume-me",
        usage_tokens=100,
    )
    return r


# ---- 待批通知 ----


async def test_待批时转WAITING_INPUT并通知(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _awaiting()
    monkeypatch.setattr(runtime, "run", _fixed_run(runtime))
    env = Env(runtime, _policy(["U1"]))

    decision = await env.router.route(mention("提个 PR"))

    task = next(iter(env.tasks.items.values()))
    assert task.status is TaskStatus.WAITING_INPUT
    assert "需要你确认" in decision.message
    assert "@U1" in decision.message, "要定向 @ 审批人 —— 只有 @ 才有推送"
    assert "github" in decision.message
    assert "/approve" in decision.message
    assert env.checkpoints.pending[task.id].tool_name == "github"
    # 不断言在最后一条：WAITING_INPUT 的状态迁移会在它之后再写一条 task_transition
    assert "approval_required" in env.events()


async def test_通知列全参数(monkeypatch: pytest.MonkeyPatch) -> None:
    """只说「我要建 PR」等于让人盲签，而改参数的前提是人看得见参数。"""
    runtime = _awaiting("github", repo="team/api", title="修登录超时", base="main")
    monkeypatch.setattr(runtime, "run", _fixed_run(runtime))
    env = Env(runtime, _policy(["U1"]))

    decision = await env.router.route(mention("提个 PR"))

    for expect in ("repo: team/api", "title: 修登录超时", "base: main"):
        assert expect in decision.message


async def test_无审批人时判失败并说明(monkeypatch: pytest.MonkeyPatch) -> None:
    """**不放宽** —— 放行等于让审批配置形同虚设。"""
    runtime = _awaiting()
    monkeypatch.setattr(runtime, "run", _fixed_run(runtime))
    env = Env(runtime, _policy(approver_ids=[]))

    decision = await env.router.route(mention("提个 PR"))

    task = next(iter(env.tasks.items.values()))
    assert task.status is TaskStatus.FAILED
    assert "没有配置审批人" in decision.message
    assert env.checkpoints.pending == {}


async def test_未装配审批能力时判失败(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _awaiting()
    monkeypatch.setattr(runtime, "run", _fixed_run(runtime))
    env = Env(runtime, _policy(["U1"]), with_approvals=False)

    decision = await env.router.route(mention("提个 PR"))

    assert next(iter(env.tasks.items.values())).status is TaskStatus.FAILED
    assert "未启用审批" in decision.message


async def test_双批时通知说明进度(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _awaiting()
    monkeypatch.setattr(runtime, "run", _fixed_run(runtime))
    env = Env(runtime, _policy(["U1", "U2"], approval_required_tools={"github": 2}))

    decision = await env.router.route(mention("提个 PR"))

    assert "2 位审批人" in decision.message
    assert "0/2" in decision.message


# ---- /approve 与 /deny ----


async def _seed_waiting(env: Env, requester: str = "U9") -> Task:
    """造一个正在等审批的任务。"""
    task = Task(
        id="task_w",
        channel_instance_id=CH,
        # 与 mention() 的 thread_ref 一致 —— 审批指令按线程绑定
        thread_ref="1700000000.1",
        requester_id=requester,
        intent="code_review",
    )
    task.status = TaskStatus.WAITING_INPUT
    await env.tasks.create(task)
    env.checkpoints.pending[task.id] = PendingApproval(
        tool_call_id="tc_1", tool_name="github", args={"title": "修 bug"}, required=1
    )
    env.checkpoints.store[task.id] = TaskCheckpoint(task.id, b"hist", 0)
    return task


async def test_approve后恢复执行() -> None:
    runtime = FakeRuntime(status=StageStatus.DONE, output="PR 已建")
    env = Env(runtime, _policy(["U1"]))
    task = await _seed_waiting(env)

    decision = await env.router.route(mention("/approve", user="U1"))

    assert decision.message == "PR 已建"
    assert task.status is TaskStatus.DONE
    assert env.checkpoints.cleared == [task.id]
    # 恢复时必须带裁决，否则待批的调用仍未应答
    assert runtime.approval_results[-1] is not None
    assert "tc_1" in runtime.approval_results[-1]


async def test_发起人自批被拒且任务仍在等() -> None:
    runtime = FakeRuntime()
    env = Env(runtime, _policy(["U1", "U9"]))
    task = await _seed_waiting(env, requester="U9")

    decision = await env.router.route(mention("/approve", user="U9"))

    assert "发起人" in decision.message
    assert task.status is TaskStatus.WAITING_INPUT, "被拒后仍该等着"
    assert runtime.runs == 0, "不该恢复执行"
    assert env.events()[-1] == "approval_rejected_self"


async def test_deny后恢复让模型收尾() -> None:
    runtime = FakeRuntime(status=StageStatus.DONE, output="PR 没有创建，因为审批被拒")
    env = Env(runtime, _policy(["U1"]))
    task = await _seed_waiting(env)

    decision = await env.router.route(mention("/deny 现在不合适", user="U1"))

    assert task.status is TaskStatus.DONE
    assert "PR 没有创建" in decision.message
    decisions = runtime.approval_results[-1]
    assert decisions is not None
    assert decisions["tc_1"].approved is False
    assert decisions["tc_1"].reason == "现在不合适"


async def test_双批第一人批完仍在等() -> None:
    runtime = FakeRuntime()
    env = Env(runtime, _policy(["U1", "U2"], approval_required_tools={"github": 2}))
    task = await _seed_waiting(env)
    env.checkpoints.pending[task.id].required = 2

    decision = await env.router.route(mention("/approve", user="U1"))

    assert "1/2" in decision.message
    assert task.status is TaskStatus.WAITING_INPUT
    assert runtime.runs == 0


async def test_双批第二人批完才恢复() -> None:
    runtime = FakeRuntime(status=StageStatus.DONE, output="做完了")
    env = Env(runtime, _policy(["U1", "U2"], approval_required_tools={"github": 2}))
    task = await _seed_waiting(env)
    env.checkpoints.pending[task.id].required = 2
    await env.router.route(mention("/approve", user="U1"))

    await env.router.route(mention("/approve", user="U2"))

    assert task.status is TaskStatus.DONE
    assert runtime.runs == 1


async def test_线程无待批时给提示() -> None:
    env = Env(FakeRuntime(), _policy(["U1"]))

    decision = await env.router.route(mention("/approve", user="U1"))

    assert "没有等待审批" in decision.message


async def test_approve带参数覆盖() -> None:
    runtime = FakeRuntime(status=StageStatus.DONE, output="ok")
    env = Env(runtime, _policy(["U1"]))
    await _seed_waiting(env)

    await env.router.route(mention('/approve title="人改过的"', user="U1"))

    decisions = runtime.approval_results[-1]
    assert decisions is not None
    assert decisions["tc_1"].override_args == {"title": "人改过的"}


# ---- 纯函数 ----


def test_解析覆盖参数() -> None:
    assert _parse_overrides(['title="新标题"', "base=main"]) == {
        "title": "新标题",
        "base": "main",
    }


def test_不含等号的词被忽略() -> None:
    """用户可能写 /approve 看着没问题 —— 那是注释不是参数。"""
    assert _parse_overrides(["看着没问题"]) is None
    assert _parse_overrides(["看着没问题", "title=x"]) == {"title": "x"}


def test_无参数时返回None() -> None:
    assert _parse_overrides([]) is None


def test_值统一当字符串() -> None:
    """这里没有工具的参数 schema，猜类型会引入第二套更弱的判断。"""
    assert _parse_overrides(["count=2"]) == {"count": "2"}


def test_通知文案不含参数时也成立() -> None:
    p = PendingApproval(tool_call_id="tc", tool_name="deploy", args={}, required=1)
    text = _approval_prompt(p, {"U1"})
    assert "@U1" in text and "deploy" in text and "/approve" in text


def _fixed_run(runtime: FakeRuntime):
    """让 FakeRuntime 返回预置的 StageResult。"""

    async def run(task: object, bundle: object, approval_results: object = None) -> StageResult:
        runtime.runs += 1
        runtime.bundles.append(bundle)
        runtime.approval_results.append(approval_results)
        return runtime._result  # noqa: SLF001

    return run
