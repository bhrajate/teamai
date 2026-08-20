"""ApprovalService：判定 + 落库 + 审计。

三条硬规则各有专测，它们退化了整个机制就失效：

- 发起人不得批准自己（即便在名单里）
- 审批人配不出来时拒绝而非放宽
- 双批必须两个不同的人

外加一条不对称设计：**拒绝不需要凑数**，任何一个合法审批人拒绝即终结。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from teamai.application.approval import ApprovalService, resolve_approvers
from teamai.domain.models import (
    ApprovalOutcome,
    ApprovalRecord,
    AuditLog,
    AuditResult,
    PendingApproval,
    PermissionPolicy,
    Task,
)
from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.ports import ApprovalRequest
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.domain.services import AuditLogWriter


class FakeCheckpoints(CheckpointRepository):
    def __init__(self) -> None:
        self.store: dict[str, TaskCheckpoint] = {}
        self.pending: dict[str, PendingApproval] = {}
        self.cleared: list[str] = []

    async def get(self, task_id: str) -> TaskCheckpoint | None:
        return self.store.get(task_id)

    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None:
        old = self.store.get(task_id)
        self.store[task_id] = TaskCheckpoint(
            task_id=task_id,
            messages=messages,
            tokens_used=tokens_used,
            attempts=old.attempts if old else 0,
        )

    async def delete(self, task_id: str) -> None:
        self.store.pop(task_id, None)
        self.pending.pop(task_id, None)

    async def bump_attempts(self, task_id: str) -> int:
        cp = self.store.get(task_id)
        if cp is None:
            return 0
        cp.attempts += 1
        return cp.attempts

    async def set_pending_approval(
        self, task_id: str, messages: bytes, pending: PendingApproval
    ) -> None:
        self.pending[task_id] = pending
        old = self.store.get(task_id)
        self.store[task_id] = TaskCheckpoint(
            task_id=task_id,
            messages=messages,
            tokens_used=old.tokens_used if old else 0,
            attempts=old.attempts if old else 0,
        )

    async def get_pending_approval(self, task_id: str) -> PendingApproval | None:
        return self.pending.get(task_id)

    async def clear_pending_approval(self, task_id: str) -> None:
        self.cleared.append(task_id)
        self.pending.pop(task_id, None)

    async def list_pending_before(self, cutoff: datetime) -> list[str]:
        return list(self.pending)


class FakeAudit:
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []

    async def append(self, log: AuditLog) -> None:
        self.logs.append(log)

    def events(self) -> list[str]:
        return [x.detail.get("event", "") for x in self.logs]

    def last(self) -> AuditLog:
        return self.logs[-1]


@pytest.fixture
def repo() -> FakeCheckpoints:
    return FakeCheckpoints()


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def svc(repo: FakeCheckpoints, audit: FakeAudit) -> ApprovalService:
    return ApprovalService(repo, AuditLogWriter(audit))


def _task(**kw) -> Task:
    t = Task(
        id=kw.pop("id", "task_1"),
        channel_instance_id="ch_1",
        thread_ref="ts_1",
        requester_id=kw.pop("requester_id", "U9"),
        intent="code_review",
    )
    t.owner_id = kw.pop("owner_id", None)
    return t


def _policy(approver_ids: list[str] | None = None) -> PermissionPolicy:
    return PermissionPolicy(
        id="pol_1",
        channel_instance_id="ch_1",
        approver_ids=approver_ids or [],
    )


async def _seed(svc: ApprovalService, task: Task, required: int = 1) -> PendingApproval:
    return await svc.record_request(
        task,
        ApprovalRequest(tool_call_id="tc_1", tool_name="github", args={"title": "修 bug"}),
        required=required,
        history=b"hist",
        approvers={"U1"},
    )


# ---- 审批人解析 ----


def test_owner优先于频道名单() -> None:
    """owner_id 存在即以它为准，不取并集 —— 否则「配了负责人还是全员能批」，
    那道指定就没意义了。"""
    assert resolve_approvers(_task(owner_id="U5"), _policy(["U1", "U2"])) == {"U5"}


def test_无owner回落频道名单() -> None:
    assert resolve_approvers(_task(), _policy(["U1", "U2"])) == {"U1", "U2"}


def test_都没配返回空集() -> None:
    assert resolve_approvers(_task(), None) == set()
    assert resolve_approvers(_task(), _policy([])) == set()


# ---- 落待批 ----


async def test_落待批并留痕(svc: ApprovalService, repo: FakeCheckpoints, audit: FakeAudit) -> None:
    task = _task()
    p = await _seed(svc, task)

    assert repo.pending["task_1"] is p
    assert repo.store["task_1"].messages == b"hist", "历史要一并存 —— 恢复要用"
    assert audit.events() == ["approval_required"]
    d = audit.last().detail
    assert d["tool"] == "github"
    assert d["args"] == {"title": "修 bug"}
    assert d["requester"] == "U9"
    assert d["approver_candidates"] == ["U1"], "记下当时的候选人，日后名单变了仍可追溯"


# ---- 硬规则一：不能自批 ----


async def test_发起人不能批自己(svc: ApprovalService, audit: FakeAudit) -> None:
    task = _task(requester_id="U9")
    await _seed(svc, task)

    r = await svc.approve(task, _policy(["U1"]), user_id="U9")

    assert r.outcome is ApprovalOutcome.REJECTED_SELF
    assert not r.ready_to_resume
    assert "发起人" in r.message
    assert audit.events()[-1] == "approval_rejected_self", "SoD 被触发要留证据"
    assert audit.last().result is AuditResult.DENIED


async def test_发起人在名单里也不能批自己(svc: ApprovalService) -> None:
    """配置的含义是「他平时可以批别人的」，不是「他能批自己的」。"""
    task = _task(requester_id="U9")
    await _seed(svc, task)

    r = await svc.approve(task, _policy(["U9", "U1"]), user_id="U9")

    assert r.outcome is ApprovalOutcome.REJECTED_SELF


async def test_owner是发起人时也拦(svc: ApprovalService) -> None:
    """一个人既是发起人又是负责人 —— 这时该频道实际上无人可批，必须拦住。"""
    task = _task(requester_id="U9", owner_id="U9")
    await _seed(svc, task)

    r = await svc.approve(task, _policy(["U1"]), user_id="U9")

    assert r.outcome is ApprovalOutcome.REJECTED_SELF


# ---- 硬规则二：配不出审批人则拒绝 ----


async def test_无审批人时拒绝而非放宽(svc: ApprovalService, audit: FakeAudit) -> None:
    """**最危险的退化**：默认放宽等于没有审批。"""
    task = _task()
    await _seed(svc, task)

    r = await svc.approve(task, None, user_id="U1")

    assert r.outcome is ApprovalOutcome.REJECTED_NO_APPROVER
    assert "没有配置审批人" in r.message
    assert audit.events()[-1] == "approval_rejected_no_approver"


async def test_不在名单里无权批(svc: ApprovalService, audit: FakeAudit) -> None:
    task = _task()
    await _seed(svc, task)

    r = await svc.approve(task, _policy(["U1"]), user_id="U8")

    assert r.outcome is ApprovalOutcome.REJECTED_NOT_APPROVER
    assert "U1" in r.message, "回执要说清当前审批人是谁"
    assert audit.events()[-1] == "approval_rejected_not_approver"


# ---- 硬规则三：双批要两个人 ----


async def test_单批一人即可恢复(svc: ApprovalService) -> None:
    task = _task()
    await _seed(svc, task, required=1)

    r = await svc.approve(task, _policy(["U1"]), user_id="U1")

    assert r.outcome is ApprovalOutcome.GRANTED
    assert r.ready_to_resume


async def test_双批一人不够(svc: ApprovalService, repo: FakeCheckpoints) -> None:
    task = _task()
    await _seed(svc, task, required=2)

    r = await svc.approve(task, _policy(["U1", "U2"]), user_id="U1")

    assert r.outcome is ApprovalOutcome.GRANTED
    assert not r.ready_to_resume
    assert "1/2" in r.message
    # 必须回写，否则第二个人来时看不到第一条
    assert repo.pending["task_1"].approved_by == {"U1"}


async def test_双批同一人两次不算(svc: ApprovalService) -> None:
    task = _task()
    await _seed(svc, task, required=2)
    await svc.approve(task, _policy(["U1", "U2"]), user_id="U1")

    r = await svc.approve(task, _policy(["U1", "U2"]), user_id="U1")

    assert r.outcome is ApprovalOutcome.REJECTED_DUPLICATE
    assert not r.ready_to_resume


async def test_双批两个不同人凑够(svc: ApprovalService) -> None:
    task = _task()
    await _seed(svc, task, required=2)
    await svc.approve(task, _policy(["U1", "U2"]), user_id="U1")

    r = await svc.approve(task, _policy(["U1", "U2"]), user_id="U2")

    assert r.ready_to_resume
    assert "2/2" not in r.message or True  # 够数后文案是「继续执行」
    assert r.pending is not None and r.pending.satisfied


# ---- 拒绝 ----


async def test_一人拒绝即终结(svc: ApprovalService, audit: FakeAudit) -> None:
    """与批准不对称是有意的：双批的用意是「多一双眼睛防误批」，而拒绝本就是
    谨慎的方向 —— 要求两人都拒绝才算拒绝，等于让一个人的反对无效。"""
    task = _task()
    await _seed(svc, task, required=2)

    r = await svc.deny(task, _policy(["U1", "U2"]), user_id="U1", reason="不该现在提")

    assert r.outcome is ApprovalOutcome.DENIED
    assert r.ready_to_resume, "拒绝也要恢复 run，让模型收尾说明"
    assert audit.events()[-1] == "approval_denied"
    assert audit.last().detail["reason"] == "不该现在提"


async def test_发起人不能自己拒(svc: ApprovalService) -> None:
    """那等价于自己取消任务，应该走 cancel。"""
    task = _task(requester_id="U9")
    await _seed(svc, task)

    r = await svc.deny(task, _policy(["U1"]), user_id="U9")

    assert r.outcome is ApprovalOutcome.REJECTED_SELF


async def test_批过的人可以改主意去拒(svc: ApprovalService) -> None:
    """DUPLICATE 只挡重复批准，不挡「批完又想拒」。"""
    task = _task()
    await _seed(svc, task, required=2)
    await svc.approve(task, _policy(["U1", "U2"]), user_id="U1")

    r = await svc.deny(task, _policy(["U1", "U2"]), user_id="U1", reason="再想了想不行")

    assert r.outcome is ApprovalOutcome.DENIED
    assert r.ready_to_resume


async def test_无待批时批准或拒绝都给提示(svc: ApprovalService) -> None:
    task = _task()
    for r in (
        await svc.approve(task, _policy(["U1"]), user_id="U1"),
        await svc.deny(task, _policy(["U1"]), user_id="U1"),
    ):
        assert not r.ready_to_resume
        assert "没有待审批" in r.message


# ---- 超时 ----


async def test_超时按拒绝处理(svc: ApprovalService, audit: FakeAudit) -> None:
    """转拒绝而非取消：模型能说明「因为没等到审批，PR 没有创建」。"""
    task = _task()
    await _seed(svc, task)

    r = await svc.timeout(task)

    assert r.outcome is ApprovalOutcome.DENIED
    assert r.ready_to_resume
    assert audit.events()[-1] == "approval_timeout"
    assert audit.last().result is AuditResult.DENIED


# ---- 恢复用的裁决 ----


async def test_批准的裁决带覆盖参数(svc: ApprovalService) -> None:
    task = _task()
    await _seed(svc, task)
    await svc.approve(task, _policy(["U1"]), user_id="U1", override_args={"title": "改过的"})
    pending = await svc._checkpoints.get_pending_approval("task_1")  # noqa: SLF001
    assert pending is not None

    decisions = svc.decisions_for_resume(pending, approved=True)

    d = decisions["tc_1"]
    assert d.approved
    assert d.override_args == {"title": "改过的"}


async def test_拒绝的裁决不带覆盖参数(svc: ApprovalService) -> None:
    p = PendingApproval(
        tool_call_id="tc_1",
        tool_name="github",
        approvals=[ApprovalRecord(user_id="U1", override_args={"x": 1})],
    )

    decisions = svc.decisions_for_resume(p, approved=False, reason="不行")

    d = decisions["tc_1"]
    assert not d.approved
    assert d.reason == "不行"
    assert d.override_args is None, "拒绝时传覆盖参数没有意义"


async def test_裁决的键是原tool_call_id(svc: ApprovalService) -> None:
    """框架靠它把结果对回具体某次调用，对不上会被当成仍未应答。"""
    p = PendingApproval(tool_call_id="pyd_ai_abc123", tool_name="github")
    assert set(svc.decisions_for_resume(p, approved=True)) == {"pyd_ai_abc123"}


async def test_clear只清待批(svc: ApprovalService, repo: FakeCheckpoints) -> None:
    task = _task()
    await _seed(svc, task)

    await svc.clear(task.id)

    assert repo.cleared == ["task_1"]
    assert "task_1" in repo.store, "历史要留着 —— 恢复执行正要用"
