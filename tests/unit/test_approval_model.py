"""PendingApproval / PermissionPolicy 的审批判定。

这些是纯函数，但它们就是四眼原则本身。三处退化会让整个机制失效，各有专测：

- 用 len(approvals) 而非集合 → 同一人点两次凑够双批
- 先判名单再判发起人 → 自批被记成「无权」，审计上看不出有人试过绕过
- 审批人为空时放宽 → 最危险的退化，等于没有审批
"""

from __future__ import annotations

import pytest

from teamai.domain.models import ApprovalOutcome, ApprovalRecord, PendingApproval
from teamai.domain.models.policy import PermissionPolicy


def _pending(required: int = 1, **kw) -> PendingApproval:
    return PendingApproval(
        tool_call_id=kw.pop("tool_call_id", "tc_1"),
        tool_name=kw.pop("tool_name", "github"),
        args=kw.pop("args", {"action": "create_pr", "repo": "team/api"}),
        required=required,
        **kw,
    )


def _policy(**kw) -> PermissionPolicy:
    return PermissionPolicy(
        id="pol_1",
        channel_instance_id="ch_1",
        approval_required_tools=kw.pop("approval_required_tools", {}),
        approver_ids=kw.pop("approver_ids", []),
        **kw,
    )


# ---- 双批去重 ----


def test_单批一个人就够() -> None:
    p = _pending(required=1)
    p.approvals.append(ApprovalRecord(user_id="U1"))
    assert p.satisfied
    assert p.progress == "1/1"


def test_同一人点两次不算双批() -> None:
    """回归点。用 len(self.approvals) 而非 approved_by 就会让这条通过 ——
    而那正是四眼原则要防的：一个人独自完成「发起-批准」全链路。
    """
    p = _pending(required=2)
    p.approvals.append(ApprovalRecord(user_id="U1"))
    p.approvals.append(ApprovalRecord(user_id="U1"))

    assert len(p.approvals) == 2, "记录确实有两条"
    assert p.approved_by == {"U1"}, "但只有一个人"
    assert not p.satisfied, "同一人点两次不该凑够双批"
    assert p.progress == "1/2"


def test_两个不同人凑够双批() -> None:
    p = _pending(required=2)
    p.approvals.append(ApprovalRecord(user_id="U1"))
    p.approvals.append(ApprovalRecord(user_id="U2"))

    assert p.satisfied
    assert p.progress == "2/2"


def test_没有批准时未满足() -> None:
    assert not _pending().satisfied
    assert _pending(required=2).progress == "0/2"


# ---- 校验顺序与结果 ----


def test_发起人不能批自己() -> None:
    p = _pending()
    assert p.can_approve("U9", requester_id="U9", approvers={"U1"}) is ApprovalOutcome.REJECTED_SELF


def test_发起人在名单里也不能批自己() -> None:
    """配置的含义是「他平时可以批别人的」，不是「他能批自己的」。

    这条是 SoD 的核心：审批人身份必须独立于发起动作的那个会话。
    """
    p = _pending()
    outcome = p.can_approve("U9", requester_id="U9", approvers={"U9", "U1"})
    assert outcome is ApprovalOutcome.REJECTED_SELF


def test_自批判定优先于名单判定() -> None:
    """一个不在名单里的发起人应记成 REJECTED_SELF 而非 NOT_APPROVER。

    审计上我们更想看到「有人试图自批」—— 那是安全信号；
    「不在名单里」只是配置问题。
    """
    p = _pending()
    outcome = p.can_approve("U9", requester_id="U9", approvers={"U1"})
    assert outcome is ApprovalOutcome.REJECTED_SELF


def test_不在名单里无权批() -> None:
    p = _pending()
    assert (
        p.can_approve("U8", requester_id="U9", approvers={"U1"})
        is ApprovalOutcome.REJECTED_NOT_APPROVER
    )


def test_重复批准不计数() -> None:
    p = _pending(required=2)
    p.approvals.append(ApprovalRecord(user_id="U1"))
    assert (
        p.can_approve("U1", requester_id="U9", approvers={"U1", "U2"})
        is ApprovalOutcome.REJECTED_DUPLICATE
    )


def test_审批人为空时拒绝而非放宽() -> None:
    """**最危险的退化**：默认放宽等于没有审批。

    与 allowed_tools 的语义一致 —— 白名单里没有的工具不是「谁都能用」。
    """
    p = _pending()
    assert (
        p.can_approve("U1", requester_id="U9", approvers=set())
        is ApprovalOutcome.REJECTED_NO_APPROVER
    )


def test_空名单时连发起人自己也拒() -> None:
    """没有审批人 → 谁都批不了，包括发起人。顺序上 NO_APPROVER 先判。"""
    p = _pending()
    assert (
        p.can_approve("U9", requester_id="U9", approvers=set())
        is ApprovalOutcome.REJECTED_NO_APPROVER
    )


def test_合法审批人通过() -> None:
    p = _pending()
    assert p.can_approve("U1", requester_id="U9", approvers={"U1"}) is ApprovalOutcome.GRANTED


@pytest.mark.parametrize(
    ("outcome", "is_rejection"),
    [
        (ApprovalOutcome.GRANTED, False),
        (ApprovalOutcome.DENIED, False),
        (ApprovalOutcome.REJECTED_SELF, True),
        (ApprovalOutcome.REJECTED_NOT_APPROVER, True),
        (ApprovalOutcome.REJECTED_DUPLICATE, True),
        (ApprovalOutcome.REJECTED_NO_APPROVER, True),
    ],
)
def test_is_rejection区分审批动作被拒与工具被拒(
    outcome: ApprovalOutcome, is_rejection: bool
) -> None:
    """DENIED 不算 rejection —— 它是审批人**行使权力**拒绝工具调用（合法结果，
    要恢复 run 让模型收尾）；其余是审批动作没通过校验（任务继续等着）。
    两者对任务状态的影响完全相反，混淆会让被拒的任务永远挂着或提前收尾。
    """
    assert outcome.is_rejection is is_rejection


# ---- 参数覆盖 ----


def test_无覆盖时用原始参数() -> None:
    p = _pending()
    p.approvals.append(ApprovalRecord(user_id="U1"))
    assert p.effective_args == {"action": "create_pr", "repo": "team/api"}


def test_覆盖参数生效() -> None:
    p = _pending()
    p.approvals.append(ApprovalRecord(user_id="U1", override_args={"title": "人改过的"}))
    assert p.effective_args == {"title": "人改过的"}


def test_多人批时取最后一次覆盖() -> None:
    """后批的人看到的是前一个人改过的结果（通知会重发），故最后一次即当前共识。"""
    p = _pending(required=2)
    p.approvals.append(ApprovalRecord(user_id="U1", override_args={"title": "第一版"}))
    p.approvals.append(ApprovalRecord(user_id="U2", override_args={"title": "第二版"}))
    assert p.effective_args == {"title": "第二版"}


def test_后批未改参数时沿用前一次的覆盖() -> None:
    p = _pending(required=2)
    p.approvals.append(ApprovalRecord(user_id="U1", override_args={"title": "改过的"}))
    p.approvals.append(ApprovalRecord(user_id="U2"))
    assert p.effective_args == {"title": "改过的"}


# ---- 策略侧：哪些工具要审批 ----


def test_未配置时不需要审批() -> None:
    assert _policy().approvals_needed("github") == 0


def test_精确匹配() -> None:
    pol = _policy(approval_required_tools={"github": 1, "mcp__deploy": 2})
    assert pol.approvals_needed("github") == 1
    assert pol.approvals_needed("mcp__deploy") == 2
    assert pol.approvals_needed("crm") == 0


def test_mcp_server级配置被动态工具继承() -> None:
    """与 allowed_tools 的 server 级挂载对称。

    否则管理员得为一个 MCP server 的每个动态工具各配一条 —— 而那些工具名
    要连上 server 才知道，配置时根本写不出来。
    """
    pol = _policy(approval_required_tools={"mcp__deploy": 2})
    assert pol.approvals_needed("mcp__deploy__rollout") == 2
    assert pol.approvals_needed("mcp__deploy__status") == 2
    assert pol.approvals_needed("mcp__other__x") == 0


def test_前缀不误伤同名开头的工具() -> None:
    """github 配了审批不该让 github_v2 跟着要审批 —— 只认 `__` 分隔的层级。"""
    pol = _policy(approval_required_tools={"github": 1})
    assert pol.approvals_needed("github_v2") == 0
    assert pol.approvals_needed("github__sub") == 1


def test_精确配置优先于前缀继承() -> None:
    pol = _policy(approval_required_tools={"mcp__deploy": 2, "mcp__deploy__status": 1})
    assert pol.approvals_needed("mcp__deploy__status") == 1, "精确配置该赢"
    assert pol.approvals_needed("mcp__deploy__rollout") == 2
