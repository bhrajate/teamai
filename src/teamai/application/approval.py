"""工具审批用例层：判定谁能批、够不够数、结果怎么落。

四眼原则的落地处。三条硬规则（依据见 docs/SPEC-hitl-approval.md §4）：

1. **发起人不得批准自己** —— 即便他在审批人名单里
2. **审批人配不出来时拒绝执行** —— 不放宽（与 allowed_tools 语义一致）
3. **双批必须是两个不同的 user_id** —— 同一人点两次不算

审批人三级 fallback：``task.owner_id`` → ``policy.approver_ids`` → 拒绝。
第一级兑现 PRD §4.6 的「通知负责人」。

审批事件复用 ``AuditAction.POLICY_CHANGE`` + ``detail.event``，不新增枚举成员 ——
``audit_logs.action`` 在 Postgres 上是原生枚举，加成员必须配 ALTER TYPE 迁移，
漏了会让已升级的库写审计时抛 InvalidTextRepresentationError（见
tests/unit/test_enum_migrations.py 的背景）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from teamai.domain.models import (
    ApprovalOutcome,
    ApprovalRecord,
    AuditAction,
    AuditResult,
    PendingApproval,
    PermissionPolicy,
    Task,
)
from teamai.domain.ports import ApprovalDecision, ApprovalRequest
from teamai.domain.repositories import CheckpointRepository
from teamai.domain.services import AuditLogWriter

logger = logging.getLogger(__name__)


@dataclass
class ApprovalResult:
    """一次审批动作的处置结果，供调用方决定下一步。"""

    outcome: ApprovalOutcome
    pending: PendingApproval | None = None
    # 够数了、可以恢复执行。仅在 GRANTED 且凑满时为真。
    ready_to_resume: bool = False
    # 给人看的回执文案（发回原线程）。
    message: str = ""


def resolve_approvers(task: Task, policy: PermissionPolicy | None) -> set[str]:
    """算出本任务的合法审批人集合。

    三级 fallback。**空集合意味着拒绝执行**，不是「谁都能批」—— 调用方须据此
    拒绝，理由见模块文档第 2 条。

    owner_id 优先于频道名单：它是「这个任务的负责人」，比频道级配置更具体。
    两者都有时取并集？不 —— owner_id 存在即以它为准，否则「配了负责人还是全员
    能批」，那道指定就没意义了。
    """
    if task.owner_id:
        return {task.owner_id}
    if policy is not None and policy.approver_ids:
        return set(policy.approver_ids)
    return set()


class ApprovalService:
    def __init__(
        self,
        checkpoints: CheckpointRepository,
        audit: AuditLogWriter,
    ) -> None:
        self._checkpoints = checkpoints
        self._audit = audit

    async def record_request(
        self,
        task: Task,
        request: ApprovalRequest,
        *,
        required: int,
        history: bytes,
        approvers: set[str],
    ) -> PendingApproval:
        """落一个待批项（工具被闸拦下时调用）。"""
        pending = PendingApproval(
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            args=request.args,
            required=required,
        )
        await self._checkpoints.set_pending_approval(task.id, history, pending)
        await self._audit.record(
            task.channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=task.requester_id,
            task_id=task.id,
            detail={
                "event": "approval_required",
                "tool": request.tool_name,
                "args": request.args,
                "required": required,
                # 记下当时的候选审批人：日后名单变了，审计里仍能看出「当时该谁批」
                "approver_candidates": sorted(approvers),
                "requester": task.requester_id,
            },
        )
        return pending

    async def approve(
        self,
        task: Task,
        policy: PermissionPolicy | None,
        *,
        user_id: str,
        override_args: dict[str, Any] | None = None,
    ) -> ApprovalResult:
        """批准。校验通过则记一条，够数则标记可恢复。"""
        pending = await self._checkpoints.get_pending_approval(task.id)
        if pending is None:
            return ApprovalResult(
                outcome=ApprovalOutcome.DENIED,
                message="这个任务当前没有待审批的操作。",
            )

        approvers = resolve_approvers(task, policy)
        outcome = pending.can_approve(
            user_id, requester_id=task.requester_id, approvers=approvers
        )
        if outcome is not ApprovalOutcome.GRANTED:
            await self._audit_rejection(task, pending, user_id, outcome)
            return ApprovalResult(
                outcome=outcome,
                pending=pending,
                message=_rejection_text(outcome, approvers),
            )

        pending.approvals.append(
            ApprovalRecord(user_id=user_id, override_args=override_args)
        )
        # 回写：双批时第一个人批完要落库，否则第二个人来时看不到前一条
        await self._checkpoints.set_pending_approval(
            task.id, await self._history_for(task), pending
        )
        await self._audit.record(
            task.channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=user_id,
            task_id=task.id,
            detail={
                "event": "approval_granted",
                "tool": pending.tool_name,
                "approver": user_id,
                "progress": pending.progress,
                **({"override_args": override_args} if override_args else {}),
            },
        )

        if pending.satisfied:
            return ApprovalResult(
                outcome=ApprovalOutcome.GRANTED,
                pending=pending,
                ready_to_resume=True,
                message=f"已批准，继续执行 {pending.tool_name}。",
            )
        return ApprovalResult(
            outcome=ApprovalOutcome.GRANTED,
            pending=pending,
            message=(
                f"已记录你的批准（{pending.progress}）。"
                f"还需另一位审批人确认后才会执行 {pending.tool_name}。"
            ),
        )

    async def deny(
        self,
        task: Task,
        policy: PermissionPolicy | None,
        *,
        user_id: str,
        reason: str = "",
    ) -> ApprovalResult:
        """拒绝。**任何一个合法审批人拒绝即终结**，不需要凑数。

        与批准不对称是有意的：双批的用意是「多一双眼睛防误批」，而拒绝本就是
        谨慎的方向 —— 要求两人都拒绝才算拒绝，等于让一个人的反对无效。
        """
        pending = await self._checkpoints.get_pending_approval(task.id)
        if pending is None:
            return ApprovalResult(
                outcome=ApprovalOutcome.DENIED,
                message="这个任务当前没有待审批的操作。",
            )

        approvers = resolve_approvers(task, policy)
        # 拒绝也要过同一套校验：发起人不能自己拒（那等价于自己取消任务，
        # 应该走 cancel），不在名单里的人不能代替审批人决定。
        outcome = pending.can_approve(
            user_id, requester_id=task.requester_id, approvers=approvers
        )
        # DUPLICATE 对拒绝不适用：已经批过的人改主意要拒，应当允许。
        if outcome is not ApprovalOutcome.GRANTED and outcome is not ApprovalOutcome.REJECTED_DUPLICATE:
            await self._audit_rejection(task, pending, user_id, outcome)
            return ApprovalResult(
                outcome=outcome, pending=pending, message=_rejection_text(outcome, approvers)
            )

        await self._audit.record(
            task.channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=user_id,
            task_id=task.id,
            detail={
                "event": "approval_denied",
                "tool": pending.tool_name,
                "approver": user_id,
                "reason": reason,
            },
            result=AuditResult.DENIED,
        )
        return ApprovalResult(
            outcome=ApprovalOutcome.DENIED,
            pending=pending,
            ready_to_resume=True,  # 拒绝也要恢复 run，让模型收尾说明
            message=f"已拒绝 {pending.tool_name}。",
        )

    async def timeout(self, task: Task) -> ApprovalResult:
        """审批超时：按拒绝处理。

        转拒绝而非取消任务：模型能说明「因为没等到审批，PR 没有创建」，
        用户看到的是解释而非任务凭空消失。
        """
        pending = await self._checkpoints.get_pending_approval(task.id)
        if pending is None:
            return ApprovalResult(outcome=ApprovalOutcome.DENIED)

        waited = pending.created_at
        await self._audit.record(
            task.channel_instance_id,
            AuditAction.POLICY_CHANGE,
            task_id=task.id,
            detail={
                "event": "approval_timeout",
                "tool": pending.tool_name,
                "waited_since": waited.isoformat(),
            },
            result=AuditResult.DENIED,
        )
        return ApprovalResult(
            outcome=ApprovalOutcome.DENIED,
            pending=pending,
            ready_to_resume=True,
            message=f"审批超时，{pending.tool_name} 未执行。",
        )

    def decisions_for_resume(
        self, pending: PendingApproval, *, approved: bool, reason: str = ""
    ) -> dict[str, ApprovalDecision]:
        """组装交给 gateway 的裁决。

        键必须是原来那个 ``tool_call_id`` —— 框架靠它把结果对回具体某次调用。
        """
        return {
            pending.tool_call_id: ApprovalDecision(
                approved=approved,
                reason=reason,
                override_args=pending.effective_args if approved else None,
            )
        }

    async def clear(self, task_id: str) -> None:
        """审批有结论后清掉待批项，**留着历史** —— 恢复执行正要用它。"""
        await self._checkpoints.clear_pending_approval(task_id)

    async def _history_for(self, task: Task) -> bytes:
        """取该任务当前的消息历史。

        待批项回写时要连历史一起传（仓储的 set 是覆盖式的）。从检查点读回来而非
        让调用方传：调用方（router 处理 /approve）手上只有一条聊天消息，不该
        要求它知道消息历史这种执行期细节。
        """
        cp = await self._checkpoints.get(task.id)
        return cp.messages if cp else b""

    async def _audit_rejection(
        self,
        task: Task,
        pending: PendingApproval,
        user_id: str,
        outcome: ApprovalOutcome,
    ) -> None:
        """审批动作被挡下时留痕。

        REJECTED_SELF 尤其要记：它是四眼原则被触发的证据，安全审计要能查
        「有没有人试过绕过它」。
        """
        event = {
            ApprovalOutcome.REJECTED_SELF: "approval_rejected_self",
            ApprovalOutcome.REJECTED_NOT_APPROVER: "approval_rejected_not_approver",
            ApprovalOutcome.REJECTED_DUPLICATE: "approval_rejected_duplicate",
            ApprovalOutcome.REJECTED_NO_APPROVER: "approval_rejected_no_approver",
        }.get(outcome, "approval_rejected")
        await self._audit.record(
            task.channel_instance_id,
            AuditAction.POLICY_CHANGE,
            user_id=user_id,
            task_id=task.id,
            detail={
                "event": event,
                "tool": pending.tool_name,
                "attempted_by": user_id,
            },
            result=AuditResult.DENIED,
        )


def _rejection_text(outcome: ApprovalOutcome, approvers: set[str]) -> str:
    """审批动作被挡下时给人看的回执。

    说清「为什么不行」而非只说「不行」—— 尤其自批那条：用户往往不知道有这条
    规则，只回「无权」会让他以为配置错了。
    """
    if outcome is ApprovalOutcome.REJECTED_SELF:
        return "你是这个任务的发起人，不能批准自己的操作。需要另一位审批人确认。"
    if outcome is ApprovalOutcome.REJECTED_NO_APPROVER:
        return (
            "这个频道没有配置审批人，需要审批的操作无法执行。"
            "请让管理员在权限策略里配置审批人。"
        )
    if outcome is ApprovalOutcome.REJECTED_NOT_APPROVER:
        listed = "、".join(sorted(approvers)) if approvers else "（未配置）"
        return f"你不在这个任务的审批人名单里。当前审批人：{listed}"
    if outcome is ApprovalOutcome.REJECTED_DUPLICATE:
        return "你已经批准过了，还在等其他审批人确认。"
    return "无法处理这次审批。"
