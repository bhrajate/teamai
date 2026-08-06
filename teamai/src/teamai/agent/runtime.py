"""AgentRuntime：核心 Agent 执行循环。

基于 pydantic-ai Agent 执行，处理：
- 预算前置检查与 UsageLimits 兜底
- 按频道权限构建 Agent 与工具注入
- 记忆上下文与线程历史组装
- 审计留痕与预算消耗
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from teamai.agent.context import ContextBundle
from teamai.agent.models import ModelRegistry
from teamai.config import Settings
from teamai.domain.audit import AuditAction, AuditResult
from teamai.domain.task import Task
from teamai.infrastructure.audit_log import AuditLogWriter
from teamai.tools.base import BaseTool
from teamai.tools.registry import ToolRegistry


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


class BudgetControllerPort:
    """预算控制端口（由用例层实现注入，避免基础设施耦合）。"""

    async def check_quota(self, channel_instance_id: str) -> bool: ...

    async def remaining(self, channel_instance_id: str) -> int: ...

    async def consume(self, channel_instance_id: str, tokens: int) -> bool: ...


class AgentRuntime:
    def __init__(
        self,
        registry: ModelRegistry,
        tools: ToolRegistry,
        budget: BudgetControllerPort,
        audit: AuditLogWriter,
        settings: Settings,
    ) -> None:
        self._registry = registry
        self._tools = tools
        self._budget = budget
        self._audit = audit
        self._settings = settings

    async def run(self, task: Task, bundle: ContextBundle) -> StageResult:
        if not await self._budget.check_quota(task.channel_instance_id):
            await self._audit.record(
                bundle.channel_instance_id,
                AuditAction.TASK_TRANSITION,
                user_id=task.requester_id,
                task_id=task.id,
                detail={"to": "PAUSED", "reason": "budget"},
                result=AuditResult.PAUSED,
            )
            return StageResult(status=StageStatus.PAUSED, error="预算配额已耗尽")

        bundle = bundle.compact(self._settings.context_max_messages, self._settings.context_summary_threshold)
        try:
            return await self._run_agent(task, bundle)
        except Exception as exc:  # pragma: no cover - 顶层兜底
            await self._audit.record(
                bundle.channel_instance_id,
                AuditAction.TASK_TRANSITION,
                user_id=task.requester_id,
                task_id=task.id,
                detail={"to": "FAILED", "error": str(exc)},
                result=AuditResult.FAILURE,
            )
            return StageResult(status=StageStatus.FAILED, error=str(exc))

    async def _run_agent(self, task: Task, bundle: ContextBundle) -> StageResult:
        from pydantic_ai import UsageLimitExceeded, UsageLimits

        allowed_tools: list[BaseTool] = []
        for name in bundle.allowed_tools:
            tool = self._tools.get(name)
            if tool is not None:
                allowed_tools.append(tool)

        agent = self._registry.build(bundle.model_level, tools=allowed_tools)

        remaining = await self._budget.remaining(task.channel_instance_id)
        limits = UsageLimits(total_tokens_limit=max(remaining, 1))

        deps: dict = {"policy": bundle.policy}

        prompt_parts = [bundle.user_prompt]
        if bundle.memory_context:
            prompt_parts.append(f"\n\n[频道记忆]\n{bundle.memory_context}")
        if bundle.thread_history:
            prompt_parts.append("\n\n[线程历史]\n" + "\n".join(f"- {m}" for m in bundle.thread_history))
        prompt = "\n".join(prompt_parts)

        try:
            result = await agent.run(
                prompt,
                deps=deps,
                usage_limits=limits,
            )
            tokens = int(getattr(result.usage, "total_tokens", 0))
            await self._budget.consume(task.channel_instance_id, tokens)
            await self._audit.record(
                bundle.channel_instance_id,
                AuditAction.TASK_TRANSITION,
                user_id=task.requester_id,
                task_id=task.id,
                detail={"to": "DONE", "tokens": tokens},
                tokens_consumed=tokens,
            )
            return StageResult(status=StageStatus.DONE, output=str(result.output), usage_tokens=tokens)
        except UsageLimitExceeded:
            await self._audit.record(
                bundle.channel_instance_id,
                AuditAction.TASK_TRANSITION,
                user_id=task.requester_id,
                task_id=task.id,
                detail={"to": "PAUSED", "reason": "usage_limit_exceeded"},
                result=AuditResult.PAUSED,
            )
            return StageResult(status=StageStatus.PAUSED, error="token 预算上限触发，任务暂停")
