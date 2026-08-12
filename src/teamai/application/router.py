"""消息路由：将平台事件分派到编排链路。

两条执行链路在此分叉：短任务就地同步跑完再回复；长任务（见
Intent.is_long_running）入队后立即回「已受理」，由 worker 进程消费并
经 MessagePublisher 回帖。分叉理由是平台的事件响应窗口只有 3 秒，
而多轮工具调用的任务耗时不可控。

非 @ 消息不再逐条写进记忆库（那会让 memory_entries 退化成聊天日志），
而是 append 进滚动窗口，由 worker 定时蒸馏成结论 —— 见 application/distiller.py。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.prompts import build_system_prompt
from teamai.application.agent.runtime import AgentRuntime, StageResult, StageStatus
from teamai.application.budget import BudgetController
from teamai.application.channel import ChannelService
from teamai.application.conversation import ConversationService
from teamai.application.distiller import MemoryDistiller
from teamai.application.events import IncomingMessage
from teamai.application.intent import IntentClassifier
from teamai.application.memory import MemoryService
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.tag import TagResolver
from teamai.domain.models import ChannelInstance, Task, TaskStatus
from teamai.domain.repositories import PolicyRepository

logger = logging.getLogger(__name__)

# 视为私密会话的 channel_type。PRD §4.2 要求「私密频道与私信内容默认不进入记忆」。
# slack: im（单聊）/ mpim（多人私聊）；feishu: p2p（单聊）。
# 此前 Visibility 枚举建好了却无人判定，单聊内容照样进频道记忆 —— 承诺没落地。
PRIVATE_CHANNEL_TYPES = frozenset({"im", "mpim", "p2p"})


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
        conversation: ConversationService | None = None,
        distiller: MemoryDistiller | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._intent = intent
        self._tags = tags
        self._memory = memory
        self._budget = budget
        self._runtime = runtime
        self._channels = channels
        self._policy_repo = policy_repo
        self._conversation = conversation
        self._distiller = distiller

    async def route(self, msg: IncomingMessage) -> RoutingDecision:
        instance = await self._channels.get_or_create(msg.platform, msg.channel_id, msg.workspace_id)

        if not msg.is_mention:
            return await self._observe(instance, msg)

        return await self._handle_task(instance, msg)

    async def _observe(self, instance: ChannelInstance, msg: IncomingMessage) -> RoutingDecision:
        """普通消息：作为记忆素材进滚动窗口，稍后由 worker 蒸馏。

        私密会话直接丢弃（PRD §4.2）。斜杠开头的也跳过：那是给别的机器人或
        平台自身的指令，不是团队对话内容。
        """
        if msg.channel_type in PRIVATE_CHANNEL_TYPES:
            return RoutingDecision(handler="observe", message="私密会话不进入记忆")
        text = msg.text.strip()
        if not text or text.startswith("/"):
            return RoutingDecision(handler="observe", message="已忽略")
        if self._distiller is None:
            return RoutingDecision(handler="observe", message="未启用记忆蒸馏")
        await self._distiller.observe(instance.id, msg.user_id, text)
        return RoutingDecision(handler="observe", message="已记录频道上下文")

    async def _handle_task(
        self,
        instance: ChannelInstance,
        msg: IncomingMessage,
    ) -> RoutingDecision:
        channel_instance_id = instance.id
        thread_ref = msg.thread_ref
        user_id = msg.user_id
        text = msg.text
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
            thread_ref,
            user_id,
            intent.kind,
            tag_name=tag_name,
            model_level=intent.model_level,
        )

        if intent.is_long_running:
            try:
                await self._orchestrator.enqueue(task, text)
            except ConnectionError as exc:
                # 队列不可用不该让功能整体失效：任务已落库且仍在 PENDING，
                # 就地同步执行即可，代价是本次响应变慢（可能超平台窗口）。
                logger.warning(f"任务 {task.id} 入队失败，降级为同步执行: {exc}")
            else:
                # 状态留在 PENDING，由 worker 取出后推进 RUNNING ——
                # 在此提前置 RUNNING 会让「排队中」与「执行中」无法区分，
                # 超时巡检也就没法判断任务是卡在队列还是卡在执行。
                return RoutingDecision(
                    handler="respond",
                    message=f"任务已受理（{intent.kind}），完成后在本线程回复。",
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

        # 线程历史按需向平台拉取，不自建镜像表 —— 平台是聊天记录的唯一权威源，
        # 理由见 docs/Design-conversation-context.md §2。拉不到就空着，任务照跑。
        thread_history = []
        if self._conversation is not None:
            thread_history = await self._conversation.thread_history(instance, task.thread_ref)

        system_prompt = build_system_prompt(
            instance,
            policy,
            role=tag.role if tag else None,
            tag_instruction=tag.instruction if tag else None,
            output_style=tag.output_style if tag else None,
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
            thread_history=thread_history,
            tag=tag,
        )
        return await self._runtime.run(task, bundle)
