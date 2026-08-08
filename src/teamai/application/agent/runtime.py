"""AgentRuntime：核心 Agent 执行循环。

纯用例层策略，不认识任何 LLM SDK：
- 预算前置检查，与剩余配额作为本次调用的 token 上限
- 按频道权限取工具集（经 ToolProvider 端口）
- 记忆上下文与线程历史组装
- 审计留痕与预算消耗
- token 超限（域异常 TokenBudgetExceeded）转 PAUSED

模型调用走 LLMGateway 端口，实现在 infrastructure/llm。预算控制器同在用例层，
直接依赖具体类即可 —— 倒置前 agent 是 application 之下的独立层，够不着
BudgetController，才不得不手写一个 duck-typed 的 BudgetControllerPort；
runtime 上移后那个桩子随之删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from teamai.application.agent.context import ContextBundle
from teamai.application.budget import BudgetController
from teamai.config import Settings
from teamai.domain.models import AuditAction, AuditResult, Task
from teamai.domain.ports import LLMGateway, TokenBudgetExceeded, ToolProvider
from teamai.domain.services import AuditLogWriter


class StageStatus(Enum):
    DONE = "DONE"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


@dataclass
class StageResult:
    status: StageStatus
    output: str = ""
    error: str | None = None
    usage_tokens: int = 0


class AgentRuntime:
    def __init__(
        self,
        gateway: LLMGateway,
        tools: ToolProvider,
        budget: BudgetController,
        audit: AuditLogWriter,
        settings: Settings,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._budget = budget
        self._audit = audit
        self._settings = settings

    async def run(self, task: Task, bundle: ContextBundle) -> StageResult:
        if not await self._budget.check_quota(task.channel_instance_id):
            await self._audit_transition(task, bundle, "PAUSED", {"reason": "budget"}, AuditResult.PAUSED)
            return StageResult(status=StageStatus.PAUSED, error="预算配额已耗尽")

        bundle = bundle.compact(self._settings.context_max_messages, self._settings.context_summary_threshold)
        try:
            return await self._run_agent(task, bundle)
        except Exception as exc:  # pragma: no cover - 顶层兜底
            await self._audit_transition(task, bundle, "FAILED", {"error": str(exc)}, AuditResult.FAILURE)
            return StageResult(status=StageStatus.FAILED, error=str(exc))

    async def _run_agent(self, task: Task, bundle: ContextBundle) -> StageResult:
        tools = self._tools.for_channel(bundle.allowed_tools)
        remaining = await self._budget.remaining(task.channel_instance_id)

        try:
            result = await self._gateway.run(
                self._compose_prompt(bundle),
                model_level=bundle.model_level,
                system_prompt=bundle.system_prompt,
                tools=tools,
                token_limit=remaining,
            )
        except TokenBudgetExceeded:
            await self._audit_transition(
                task, bundle, "PAUSED", {"reason": "token_budget_exceeded"}, AuditResult.PAUSED
            )
            return StageResult(status=StageStatus.PAUSED, error="token 预算上限触发，任务暂停")

        await self._budget.consume(task.channel_instance_id, result.tokens)
        await self._audit_transition(task, bundle, "DONE", {"tokens": result.tokens}, tokens=result.tokens)
        return StageResult(status=StageStatus.DONE, output=result.output, usage_tokens=result.tokens)

    @staticmethod
    def _compose_prompt(bundle: ContextBundle) -> str:
        parts = [bundle.user_prompt]
        if bundle.memory_context:
            parts.append(f"\n\n[频道记忆]\n{bundle.memory_context}")
        if bundle.thread_history:
            parts.append("\n\n[线程历史]\n" + "\n".join(f"- {m}" for m in bundle.thread_history))
        return "\n".join(parts)

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
