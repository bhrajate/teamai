"""AgentRuntime：核心 Agent 执行循环。

纯用例层策略，不认识任何 LLM SDK：
- 预算前置检查，与剩余配额作为本次调用的 token 上限
- 按频道权限取工具集（经 ToolProvider 端口）
- 记忆上下文与线程历史组装
- 审计留痕、交互留痕与预算消耗
- token 超限（域异常 TokenBudgetExceeded）转 PAUSED

模型调用走 LLMGateway 端口，实现在 infrastructure/llm。预算控制器同在用例层，
直接依赖具体类即可 —— 倒置前 agent 是 application 之下的独立层，够不着
BudgetController，才不得不手写一个 duck-typed 的 BudgetControllerPort；
runtime 上移后那个桩子随之删除。

审计与交互记录并存、不合并：审计是「动作流水」（永久留存、字段窄、可按枚举
统计），交互记录是「内容快照」（含提示词与响应全文、按保留期清理）。合成一张
表的话，要么审计被大字段拖胖，要么内容被塞进 detail 的 JSON 里而没法按字段
查询与统计成本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from teamai.application.agent.context import ContextBundle
from teamai.application.budget import BudgetController
from teamai.application.interaction import InteractionService
from teamai.config import Settings
from teamai.domain.models import AuditAction, AuditResult, InteractionResult, Task
from teamai.domain.ports import (
    ApprovalDecision,
    ApprovalRequest,
    LLMGateway,
    LLMResult,
    TokenBudgetExceeded,
    ToolProvider,
)
from teamai.domain.repositories import CheckpointRepository
from teamai.domain.services import AuditLogWriter


class StageStatus(Enum):
    DONE = "DONE"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    # 工具需人工批准，run 中断。与 PAUSED 分开：那个是预算耗尽（要追加配额），
    # 这个是等人点头（要有人来批）—— 两者的解法与催办文案都不同，混作一个会让
    # 「为什么停住了」答不清楚。
    AWAITING_APPROVAL = "AWAITING_APPROVAL"


# StageStatus 到交互记录结果的映射。两个枚举分开而非共用：StageStatus 是
# 执行阶段的产出，InteractionResult 是留痕的分类维度，日后任一侧增值不该
# 牵连另一侧。
_INTERACTION_RESULT = {
    StageStatus.DONE: InteractionResult.DONE,
    StageStatus.PAUSED: InteractionResult.PAUSED,
    StageStatus.FAILED: InteractionResult.FAILED,
    # 待批也记 PAUSED：交互记录的维度是「这次往返的结局」，而待批与预算暂停
    # 在那个维度上是同一类（没跑完、可恢复）。要区分看 context_refs。
    StageStatus.AWAITING_APPROVAL: InteractionResult.PAUSED,
}


@dataclass
class StageResult:
    status: StageStatus
    output: str = ""
    error: str | None = None
    usage_tokens: int = 0
    # 待批的工具调用。仅 AWAITING_APPROVAL 时非空，供调用方发通知。
    pending_approvals: list[ApprovalRequest] = field(default_factory=list)
    # 恢复执行所需的消息历史。与 pending_approvals 同时非空 —— 调用方要把两者
    # 一起交给 ApprovalService.record_request。
    approval_history: bytes | None = None


class AgentRuntime:
    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolProvider,
        budget: BudgetController,
        audit: AuditLogWriter,
        settings: Settings,
        interactions: InteractionService | None = None,
        checkpoints: CheckpointRepository | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._budget = budget
        self._audit = audit
        self._settings = settings
        self._interactions = interactions
        # 未装配时整个检查点能力不出现（窄装配与测试场景），
        # 行为与改造前一致：崩溃即失败，不续跑。
        self._checkpoints = checkpoints

    async def run(
        self,
        task: Task,
        bundle: ContextBundle,
        approval_results: dict[str, ApprovalDecision] | None = None,
    ) -> StageResult:
        """执行一次 agent run。

        ``approval_results`` 非空时是**审批后恢复**：带着上次中断的历史与裁决
        继续跑，已完成的工具不重放。调用方（router 处理 /approve）负责组装它。
        """
        if not await self._budget.check_quota(task.channel_instance_id):
            await self._audit_transition(task, bundle, "PAUSED", {"reason": "budget"}, AuditResult.PAUSED)
            result = StageResult(status=StageStatus.PAUSED, error="预算配额已耗尽")
            # 预算拦下的调用也留痕：排查「为什么没回答」时要能看到它到了这一步
            # 才被拦，而不是根本没触发。此时没有 model_id 与 token 消耗。
            await self._record(task, bundle, result)
            return result

        bundle = bundle.compact(self._settings.context_max_messages, self._settings.context_summary_threshold)
        try:
            return await self._run_agent(task, bundle, approval_results)
        except Exception as exc:  # pragma: no cover - 顶层兜底
            await self._audit_transition(task, bundle, "FAILED", {"error": str(exc)}, AuditResult.FAILURE)
            result = StageResult(status=StageStatus.FAILED, error=str(exc))
            await self._record(task, bundle, result)
            return result

    async def _run_agent(
        self,
        task: Task,
        bundle: ContextBundle,
        approval_results: dict[str, ApprovalDecision] | None = None,
    ) -> StageResult:
        # skills 一并传下去：本频道启用了 skill 时，工具集里要多一个 load_skill。
        # 它不受 allowed_tools 管制（启用即授权，见 domain/ports/tools.py）。
        # approvals 让需审批的工具在执行前中断（见 domain/ports/tools.py）。
        # 取自策略 —— 没配策略即没有需审批的工具，行为与改造前一致。
        approvals = bundle.policy.approval_required_tools if bundle.policy else None
        tools = self._tools.for_channel(bundle.allowed_tools, bundle.skills, approvals)
        remaining = await self._budget.remaining(task.channel_instance_id)
        prompt = self._compose_prompt(bundle)

        checkpoint = await self._checkpoints.get(task.id) if self._checkpoints else None
        # 前几段的累计消耗。gateway 报的 token 只含本段（它传空 RunUsage），
        # 故任务总量 = base + 本段。
        base = checkpoint.tokens_used if checkpoint else 0
        resume_count = checkpoint.attempts if checkpoint else 0
        # 本段已计费的量，用于算增量。闭包共享，故用可变容器而非 nonlocal 上的 int
        # （sink 是内部函数，nonlocal 也可以，这里取列表是为了让「它会被改」显眼）。
        consumed = [0]

        async def sink(messages: bytes, segment_tokens: int) -> None:
            """每个干净的轮边界：存检查点 + 补扣本轮增量。

            计费放在这里而不是等 run 结束一次扣完 —— 那样 worker 崩溃时这一段
            已经花掉的 token 永远不会被计费，崩一次白花一次配额，而配额账面上
            看不出来。
            """
            assert self._checkpoints is not None  # sink 只在装配了仓储时才传出去
            await self._checkpoints.upsert(task.id, messages, base + segment_tokens)
            if (delta := segment_tokens - consumed[0]) > 0:
                await self._budget.consume(task.channel_instance_id, delta)
                consumed[0] = segment_tokens

        try:
            llm = await self._gateway.run(
                prompt,
                model_level=bundle.model_level,
                system_prompt=bundle.system_prompt,
                tools=tools,
                token_limit=remaining,
                # 续跑起点。gateway 在 history 非空时忽略 prompt —— 原始提问
                # 已是历史的第一条。
                history=checkpoint.messages if checkpoint else None,
                on_checkpoint=sink if self._checkpoints else None,
                approval_results=approval_results,
            )
        except TokenBudgetExceeded as exc:
            await self._audit_transition(
                task, bundle, "PAUSED", {"reason": "token_budget_exceeded"}, AuditResult.PAUSED
            )
            result = StageResult(status=StageStatus.PAUSED, error="token 预算上限触发，任务暂停")
            await self._record(task, bundle, result, error=str(exc), resume_count=resume_count)
            # 检查点**不删**：追加配额后应当从断点续跑而非重新开始。
            # 只有终态迁移才清理（见 TaskOrchestrator.transition）。
            return result

        # 只补最后一个检查点之后的增量。llm.tokens 与 consumed 同为本段量纲，
        # 故不需要减 base。
        if (tail := llm.tokens - consumed[0]) > 0:
            await self._budget.consume(task.channel_instance_id, tail)

        total = base + llm.tokens

        # 工具等人批准：run 没跑完，output 是空的（gateway 已置空）。
        # 这里只负责把待批项交出去 —— 落库、通知、状态迁移都在调用方
        # （router）做，它才知道审批人是谁、往哪个线程发。
        if llm.awaiting_approval:
            await self._audit_transition(
                task,
                bundle,
                "WAITING_INPUT",
                {
                    "reason": "tool_approval_required",
                    "tools": [r.tool_name for r in llm.pending_approvals],
                    "tokens": total,
                },
                AuditResult.PAUSED,
                tokens=total,
            )
            result = StageResult(
                status=StageStatus.AWAITING_APPROVAL,
                usage_tokens=total,
                pending_approvals=list(llm.pending_approvals),
                approval_history=llm.history,
            )
            await self._record(task, bundle, result, llm=llm, resume_count=resume_count)
            return result
        await self._audit_transition(task, bundle, "DONE", {"tokens": total}, tokens=total)
        result = StageResult(status=StageStatus.DONE, output=llm.output, usage_tokens=total)
        await self._record(task, bundle, result, llm=llm, resume_count=resume_count)
        return result

    @staticmethod
    def _compose_prompt(bundle: ContextBundle) -> str:
        """拼装用户提示词。

        线程历史放在记忆之后：记忆是稳定的背景，历史是当前对话的即时上下文，
        后者更贴近「用户此刻在说什么」，靠后放能减少被长背景冲淡。
        """
        parts = [bundle.user_prompt]
        if bundle.memory_context:
            parts.append(f"\n\n[频道记忆]\n{bundle.memory_context}")
        if bundle.history_context:
            parts.append(f"\n\n[线程历史]\n{bundle.history_context}")
        return "\n".join(parts)

    async def _record(
        self,
        task: Task,
        bundle: ContextBundle,
        result: StageResult,
        *,
        llm: LLMResult | None = None,
        error: str | None = None,
        resume_count: int = 0,
    ) -> None:
        """落一条交互记录。未装配 InteractionService 时 no-op（测试与窄装配场景）。"""
        if self._interactions is None:
            return
        await self._interactions.record(
            task_id=task.id,
            channel_instance_id=bundle.channel_instance_id,
            thread_ref=task.thread_ref,
            requester_id=task.requester_id,
            user_prompt=self._compose_prompt(bundle),
            system_prompt=bundle.system_prompt,
            model_level=bundle.model_level,
            model_id=llm.model_id if llm else "",
            response=result.output,
            tokens_in=llm.tokens_in if llm else 0,
            tokens_out=llm.tokens_out if llm else 0,
            result=_INTERACTION_RESULT[result.status],
            error=error or result.error,
            context_refs={
                "memory_entry_ids": bundle.memory_ref_ids,
                "thread_history_count": len(bundle.thread_history),
                "dropped_history": bundle.dropped_history,
                "allowed_tools": list(bundle.allowed_tools),
                "tag": bundle.tag.name if bundle.tag else None,
                # 挂上的技能名（不是实际载入的，见 ContextBundle.skill_ref_names）
                "skills": bundle.skill_ref_names,
                # >0 表示这个回答是崩溃后续跑出来的。排查「为什么这条特别慢/贵」
                # 时要能看出它经历过几次续跑（每次都要重发累积历史）。
                "resume_count": resume_count,
            },
        )

    async def _audit_transition(
        self,
        task: Task,
        bundle: ContextBundle,
        to: str,
        detail: dict,
        result: AuditResult = AuditResult.SUCCESS,
        *,
        tokens: int = 0,
    ) -> None:
        await self._audit.record(
            bundle.channel_instance_id,
            AuditAction.TASK_TRANSITION,
            user_id=task.requester_id,
            task_id=task.id,
            detail={"to": to, **detail},
            result=result,
            tokens_consumed=tokens,
        )
