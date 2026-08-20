"""工具审批（HITL）领域模型。

危险工具（改外部系统的那些）在执行前中断，等指定的人批准。核心是**四眼原则**：
发起人不得批准自己的动作，关键动作可要求两个不同的人各批一次。

设计与业界实践对照见 docs/SPEC-hitl-approval.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ApprovalOutcome(Enum):
    """一次审批尝试的结果。

    ``REJECTED_SELF`` 单列而不是并进 DENIED：它是四眼原则被触发的证据，
    安全审计要能查「有没有人试过绕过它」，而 DENIED 是正常的业务拒绝。
    """

    GRANTED = "GRANTED"
    DENIED = "DENIED"
    # 发起人试图批准自己的任务 —— 被 SoD 拦下
    REJECTED_SELF = "REJECTED_SELF"
    # 不在审批人名单里
    REJECTED_NOT_APPROVER = "REJECTED_NOT_APPROVER"
    # 同一人重复批准（不计入凑数）
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
    # 该频道没有可用的审批人 —— 有意拒绝而非放宽，见 SPEC §4.3
    REJECTED_NO_APPROVER = "REJECTED_NO_APPROVER"

    @property
    def is_rejection(self) -> bool:
        """是否属于「这次审批动作本身不被接受」。

        与 DENIED 区分：DENIED 是审批人**行使权力**拒绝了工具调用（合法结果），
        这些是审批动作被系统挡下（校验没过）。两者对任务状态的影响完全不同 ——
        前者要恢复 run 让模型收尾，后者任务继续等着。
        """
        return self in (
            ApprovalOutcome.REJECTED_SELF,
            ApprovalOutcome.REJECTED_NOT_APPROVER,
            ApprovalOutcome.REJECTED_DUPLICATE,
            ApprovalOutcome.REJECTED_NO_APPROVER,
        )


@dataclass(frozen=True)
class ApprovalRecord:
    """一条已收到的批准。

    记 user_id 而非「批准数 +1」：双批要求两个**不同的人**，只记计数就能让
    同一人点两次凑够数，四眼原则也就没了。
    """

    user_id: str
    at: datetime = field(default_factory=_utcnow)
    # 审批时覆盖的参数。审批不是批/否二选一 —— 人可以改完再放行
    # （pydantic-ai 的 ToolApproved(override_args=...) 支持，已实测）。
    override_args: dict[str, Any] | None = None


@dataclass
class PendingApproval:
    """一个等待批准的工具调用。"""

    # pydantic-ai 的工具调用 id。恢复执行时必须原样传回去，
    # 否则框架不知道这个批准对应哪次调用。
    tool_call_id: str
    tool_name: str
    # 模型给出的原始参数。必须完整展示给审批人 —— 只说「我要建 PR」
    # 等于让人盲签，而支持改参数的前提是人看得见参数。
    args: dict[str, Any] = field(default_factory=dict)
    # 需要几个批准。1 = 单人批，2 = 四眼原则的双批。
    required: int = 1
    approvals: list[ApprovalRecord] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def approved_by(self) -> set[str]:
        """已批准的**人**（去重）。"""
        return {a.user_id for a in self.approvals}

    @property
    def satisfied(self) -> bool:
        """批准是否已凑够。

        ⚠️ 用 ``approved_by``（集合）而非 ``len(self.approvals)``：后者会让同一人
        点两次凑够双批，那正是四眼原则要防的。
        """
        return len(self.approved_by) >= self.required

    @property
    def progress(self) -> str:
        """``已批/需要``，用于通知文案与审计。"""
        return f"{len(self.approved_by)}/{self.required}"

    @property
    def effective_args(self) -> dict[str, Any]:
        """最终执行用的参数。

        取**最后一个**带 override 的批准：多人批且都改了参数时，后批的人看到的
        是前一个人改过的结果（通知会重发），故最后一次覆盖即当前共识。
        """
        for record in reversed(self.approvals):
            if record.override_args is not None:
                return record.override_args
        return self.args

    def can_approve(self, user_id: str, *, requester_id: str, approvers: set[str]) -> ApprovalOutcome:
        """校验某人能否批准本项。**不修改状态**，只判定。

        判定顺序有意如此 —— 每一步的失败原因都要能单独审计：

        1. 没有可用审批人 → 拒绝执行（不放宽，见 SPEC §4.3）
        2. 发起人本人 → SoD 拦下。**即便他在名单里也拒绝**：配置的含义是
           「他平时可以批别人的」，不是「他能批自己的」
        3. 不在名单里 → 无权
        4. 已经批过 → 不重复计数

        先判「是不是发起人」再判「在不在名单里」：若顺序反了，一个不在名单里的
        发起人会被记成 NOT_APPROVER，而审计上我们更想看到「有人试图自批」。
        """
        if not approvers:
            return ApprovalOutcome.REJECTED_NO_APPROVER
        if user_id == requester_id:
            return ApprovalOutcome.REJECTED_SELF
        if user_id not in approvers:
            return ApprovalOutcome.REJECTED_NOT_APPROVER
        if user_id in self.approved_by:
            return ApprovalOutcome.REJECTED_DUPLICATE
        return ApprovalOutcome.GRANTED
