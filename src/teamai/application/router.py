"""消息路由：将平台事件分派到编排链路。"""

from __future__ import annotations

from dataclasses import dataclass

from teamai.agent.context import ContextBundle
from teamai.agent.prompts import build_system_prompt
from teamai.agent.runtime import AgentRuntime, StageResult, StageStatus
from teamai.application.budget import BudgetController
from teamai.application.channel import ChannelService
from teamai.application.intent import IntentClassifier
from teamai.application.memory import MemoryService
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.tag import TagResolver
from teamai.domain.models import ChannelInstance, Task, TaskStatus
from teamai.domain.repositories import PolicyRepository


@dataclass
class RoutingDecision:
    handler: str = "respond"  # respond | observe
    message: str = ""


class MessageRouter:
    def __init__(
        self,
        orchestrator: TaskOrchestrator,
        intent: IntentClassifier,
        tags: TagResolver,
        memory: MemoryService,
        budget: BudgetController,
        runtime: AgentRuntime,
        channels: ChannelService,
        policy_repo: PolicyRepository,
    ) -> None:
        self._orchestrator = orchestrator
        self._intent = intent
        self._tags = tags
        self._memory = memory
        self._budget = budget
        self._runtime = runtime
        self._channels = channels
        self._policy_repo = policy_repo

    async def route(
        self,
        platform: str,
        workspace_id: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        text: str,
        *,
        is_mention: bool,
    ) -> RoutingDecision:
        instance = await self._channels.get_or_create(platform, channel_id, workspace_id)

        if not is_mention:
            # 普通消息：作为频道上下文素材（Ambient 模式下会用于记忆学习）
            if text.strip() and not text.startswith("/"):
                await self._memory.store(instance.id, text[:500], source_user_id=user_id)
            return RoutingDecision(handler="observe", message="已记录频道上下文")

        return await self._handle_task(instance, thread_ts, user_id, text)

    async def _handle_task(
        self,
        instance: ChannelInstance,
        thread_ts: str,
        user_id: str,
        text: str,
    ) -> RoutingDecision:
        channel_instance_id = instance.id
        tag_name = None
        parts = text.split()
        if parts and parts[0].startswith("/"):
            candidate = parts[0][1:]
            tag = await self._tags.resolve(channel_instance_id, candidate)
            if tag is not None:
                tag_name = tag.name
                text = " ".join(parts[1:]) or tag.instruction
            else:
                return RoutingDecision(handler="respond", message=f"未找到标签 /{candidate}")

        intent = await self._intent.classify(text)

        task = await self._orchestrator.create_task(
            channel_instance_id,
            thread_ts,
            user_id,
            intent.kind,
            tag_name=tag_name,
            model_level=intent.model_level,
        )
        await self._orchestrator.transition(task, TaskStatus.RUNNING, user_id)
        return await self.execute_task(task, text, tag_name, instance, actor=user_id)

    async def execute_task(
        self,
        task: Task,
        prompt: str,
        tag_name: str | None,
        instance: ChannelInstance,
        *,
        actor: str,
    ) -> RoutingDecision:
        """执行 Agent 并按结果推进任务状态。

        调用方须已把任务置为 RUNNING。同步链路（MessageRouter）与异步链路
        （worker 消费队列）共用本方法，保证两条路径的状态推进与回复文案一致。
        """
        result = await self._run_agent(task, prompt, tag_name, instance)

        if result.status is StageStatus.DONE:
            await self._orchestrator.transition(task, TaskStatus.DONE, actor)
            return RoutingDecision(handler="respond", message=result.output)
        if result.status is StageStatus.PAUSED:
            await self._orchestrator.transition(task, TaskStatus.PAUSED, actor)
            return RoutingDecision(handler="respond", message=result.error or "任务因预算暂停")
        await self._orchestrator.transition(task, TaskStatus.FAILED, actor)
        return RoutingDecision(handler="respond", message=f"任务执行失败：{result.error}")

    async def _run_agent(
        self,
        task: Task,
        prompt: str,
        tag_name: str | None,
        instance: ChannelInstance,
    ) -> StageResult:
        policy = await self._policy_repo.get_for_channel(task.channel_instance_id)
        tag = await self._tags.resolve(task.channel_instance_id, tag_name) if tag_name else None

        memory_hits = await self._memory.query_for_context(task.channel_instance_id, prompt)
        allowed_tools = list(policy.allowed_tools) if policy else []

        system_prompt = build_system_prompt(
            instance,
            policy,
            role=tag.role if tag else None,
            tag_instruction=tag.instruction if tag else None,
        )
        bundle = ContextBundle(
            task_id=task.id,
            channel_instance_id=task.channel_instance_id,
            user_prompt=prompt,
            system_prompt=system_prompt,
            model_level=task.model_level,
            instance=instance,
            policy=policy,
            allowed_tools=allowed_tools,
            memory_hits=memory_hits,
        )
        return await self._runtime.run(task, bundle)
