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
from typing import Any

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.prompts import build_system_prompt
from teamai.application.agent.runtime import AgentRuntime, StageResult, StageStatus
from teamai.application.approval import ApprovalService, resolve_approvers
from teamai.application.budget import BudgetController
from teamai.application.channel import ChannelService
from teamai.application.conversation import ConversationService
from teamai.application.distiller import MemoryDistiller
from teamai.application.events import IncomingMessage
from teamai.application.intent import IntentClassifier
from teamai.application.memory import MemoryService
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.skill import SkillService
from teamai.application.tag import TagResolver
from teamai.domain.models import (
    ApprovalOutcome,
    ChannelInstance,
    PendingApproval,
    Task,
    TaskStatus,
)
from teamai.domain.ports import ApprovalDecision
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


def _parse_overrides(tokens: list[str]) -> dict[str, Any] | None:
    """把 ``/approve title="新标题" base=main`` 里的 key=值 解析出来。

    只认 ``key=value`` 形态，不含 ``=`` 的词忽略 —— 用户可能写
    ``/approve 看着没问题``，那是注释不是参数。

    值统一当字符串：这里没有工具的参数 schema，猜类型会把 ``count=2`` 变成 int
    而 ``title=2`` 也变成 int。工具侧由 pydantic-ai 按 schema 做转换与校验，
    在这里猜只会引入第二套（且更弱的）类型判断。
    """
    overrides: dict[str, Any] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or not key:
            continue
        overrides[key] = value.strip("\"'")
    return overrides or None


def _approval_prompt(pending: PendingApproval, approvers: set[str]) -> str:
    """待批通知。

    参数**逐项列全**：只说「我要建 PR」等于让人盲签，而审批支持改参数的前提是
    人看得见参数。

    定向 @ 审批人而非泛泛在线程里喊 —— 只有 @ 才有推送。
    """
    mentions = " ".join(f"@{uid}" for uid in sorted(approvers))
    lines = [f"{mentions} 需要你确认：{pending.tool_name}"]
    for key, value in pending.args.items():
        lines.append(f"  {key}: {value}")
    if pending.required > 1:
        lines.append(f"\n本操作需要 {pending.required} 位审批人确认（当前 {pending.progress}）。")
    lines.append("\n在本线程回复 /approve 放行，/deny <理由> 拒绝。")
    lines.append("改参数后放行：/approve key=值")
    return "\n".join(lines)


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
        skills: SkillService | None = None,
        approvals: ApprovalService | None = None,
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
        self._skills = skills
        # 未装配时审批能力整体不出现（窄装配与测试场景）
        self._approvals = approvals

    async def route(self, msg: IncomingMessage) -> RoutingDecision:
        instance = await self._channels.get_or_create(msg.platform, msg.channel_id, msg.workspace_id)

        # 回填线程历史缓存放在分支之前：非 @ 消息也在线程里，平台拉取时会返回
        # 它们，只记 @ 消息会让缓存与平台不一致。这与 _observe 的记忆窗口是两件
        # 不同的事 —— 那个按频道攒待蒸馏的原文，这个按线程维持秒级新鲜的上下文。
        if self._conversation is not None:
            await self._conversation.note_inbound(instance, msg.thread_ref, msg.user_id, msg.text)

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

        # 审批指令优先于标签解析与意图分类：这条线程若有待批项，/approve 与
        # /deny 是对它的答复，不该被当成新任务。判据是「本线程有待批」而非
        # 「命令名是 approve」—— 否则没有待批时打 /approve 会被当成找不到的标签。
        if parts and parts[0] in ("/approve", "/deny"):
            return await self._handle_approval(instance, msg, parts)

        if parts and parts[0].startswith("/"):
            candidate = parts[0][1:]
            tag = await self._tags.resolve(channel_instance_id, candidate)
            if tag is not None:
                tag_name = tag.name
                text = " ".join(parts[1:]) or tag.instruction
            else:
                return await self._respond(instance, thread_ref, f"未找到标签 /{candidate}")

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
                #
                # 受理确认也回填：它确实出现在线程里，平台拉取时会返回。缓存少了
                # 它，机器人看到的历史就会随「缓存是否命中」而不同 —— 这种不确定
                # 性比多一行「任务已受理」更难排查。
                return await self._respond(
                    instance,
                    thread_ref,
                    f"任务已受理（{intent.kind}），完成后在本线程回复。",
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
        approval_results: dict[str, ApprovalDecision] | None = None,
    ) -> RoutingDecision:
        """执行 Agent 并按结果推进任务状态。

        调用方须已把任务置为 RUNNING。同步链路（MessageRouter）与异步链路
        （worker 消费队列）共用本方法，保证两条路径的状态推进与回复文案一致。

        ``approval_results`` 非空时是审批后恢复 —— 带着上次中断的裁决继续跑。
        """
        result = await self._run_agent(task, prompt, tag_name, instance, approval_results)

        if result.status is StageStatus.AWAITING_APPROVAL:
            return await self._await_approval(task, instance, result, actor=actor)
        if result.status is StageStatus.DONE:
            await self._orchestrator.transition(task, TaskStatus.DONE, actor)
            return await self._respond(instance, task.thread_ref, result.output)
        if result.status is StageStatus.PAUSED:
            await self._orchestrator.transition(task, TaskStatus.PAUSED, actor)
            return await self._respond(instance, task.thread_ref, result.error or "任务因预算暂停")
        await self._orchestrator.transition(task, TaskStatus.FAILED, actor)
        return await self._respond(instance, task.thread_ref, f"任务执行失败：{result.error}")

    async def _handle_approval(
        self,
        instance: ChannelInstance,
        msg: IncomingMessage,
        parts: list[str],
    ) -> RoutingDecision:
        """处理 /approve 与 /deny。

        绑定键是 ``thread_ref`` 而非 task_id —— 用户不必抄一个 26 位 ULID。
        代价是同一线程同时只能有一个待批项，这在实践中是对的（一个线程一个任务）。
        """
        if self._approvals is None:
            return await self._respond(instance, msg.thread_ref, "本部署未启用审批能力。")

        task = await self._orchestrator.find_awaiting_approval(instance.id, msg.thread_ref)
        if task is None:
            return await self._respond(
                instance, msg.thread_ref, "这个线程当前没有等待审批的操作。"
            )

        policy = await self._policy_repo.get_for_channel(instance.id)
        rest = parts[1:]

        if parts[0] == "/deny":
            outcome = await self._approvals.deny(
                task, policy, user_id=msg.user_id, reason=" ".join(rest)
            )
        else:
            outcome = await self._approvals.approve(
                task, policy, user_id=msg.user_id, override_args=_parse_overrides(rest)
            )

        if not outcome.ready_to_resume:
            # 校验没过（自批/无权/重复），或双批还差一个 —— 任务继续等着
            return await self._respond(instance, msg.thread_ref, outcome.message)

        assert outcome.pending is not None
        approved = outcome.outcome is ApprovalOutcome.GRANTED
        decisions = self._approvals.decisions_for_resume(
            outcome.pending,
            approved=approved,
            reason=" ".join(rest) if not approved else "",
        )
        await self._approvals.clear(task.id)
        # 转回 RUNNING 再恢复执行：execute_task 要求调用方已置 RUNNING
        await self._orchestrator.transition(task, TaskStatus.RUNNING, msg.user_id)
        return await self.execute_task(
            task,
            task.intent,
            task.tag_name,
            instance,
            actor=msg.user_id,
            approval_results=decisions,
        )

    async def _await_approval(
        self,
        task: Task,
        instance: ChannelInstance,
        result: StageResult,
        *,
        actor: str,
    ) -> RoutingDecision:
        """工具等人批准：落待批项、转 WAITING_INPUT、@ 审批人。

        审批人配不出来时**不放宽**：直接判失败并说明原因。放行等于让审批配置
        形同虚设 —— 与 allowed_tools 的语义一致（白名单里没有的不是「谁都能用」）。
        """
        if self._approvals is None:  # 未装配审批能力，理论上闸也挂不上
            await self._orchestrator.transition(task, TaskStatus.FAILED, actor)
            return await self._respond(
                instance, task.thread_ref, "需要人工批准的操作，但本部署未启用审批能力。"
            )

        policy = await self._policy_repo.get_for_channel(task.channel_instance_id)
        approvers = resolve_approvers(task, policy)
        request = result.pending_approvals[0]

        if not approvers:
            await self._orchestrator.transition(task, TaskStatus.FAILED, actor)
            return await self._respond(
                instance,
                task.thread_ref,
                f"{request.tool_name} 需要人工批准，但这个频道还没有配置审批人。"
                "请让管理员在权限策略里配置审批人后重试。",
            )

        required = policy.approvals_needed(request.tool_name) if policy else 1
        pending = await self._approvals.record_request(
            task,
            request,
            required=max(required, 1),
            history=result.approval_history or b"",
            approvers=approvers,
        )
        await self._orchestrator.transition(task, TaskStatus.WAITING_INPUT, actor)
        return await self._respond(
            instance, task.thread_ref, _approval_prompt(pending, approvers)
        )

    async def _respond(
        self,
        instance: ChannelInstance,
        thread_ref: str,
        message: str,
    ) -> RoutingDecision:
        """构造回复，并把它回填进线程历史缓存。

        所有真正会发出去的文案都走这里，回填才不会漏 —— 同步链路（适配层 say）
        与异步链路（worker 经 publisher 回帖）都取本方法返回的 message。
        `_observe` 的文案不走这里：那些返回值两个平台都不发送。

        回填的是「即将发送」而非「已发送」：发送失败时缓存里会多一条平台上不存在
        的消息，代价是本 TTL 窗口内多一行上下文，下个窗口由平台数据重建时消失。
        为拿到真实发送结果就得把回填挪到三处调用点（Slack say / 飞书 publisher /
        worker publisher），漏一处就是静默不一致，不划算。
        """
        if self._conversation is not None and message:
            await self._conversation.note_outbound(instance, thread_ref, message)
        return RoutingDecision(handler="respond", message=message)

    async def _run_agent(
        self,
        task: Task,
        prompt: str,
        tag_name: str | None,
        instance: ChannelInstance,
        approval_results: dict[str, ApprovalDecision] | None = None,
    ) -> StageResult:
        policy = await self._policy_repo.get_for_channel(task.channel_instance_id)
        tag = await self._tags.resolve(task.channel_instance_id, tag_name) if tag_name else None

        memory_hits = await self._memory.query_for_context(task.channel_instance_id, prompt)
        allowed_tools = list(policy.allowed_tools) if policy else []

        # 本频道启用的技能。取的是完整对象（含正文）—— 正文要交给 load_skill 工具
        # 做内存查表，理由见 ContextBundle.skills 的注释。未装配 SkillService 时
        # 空着（窄装配与测试场景），技能能力整体不出现，任务照跑。
        skills = []
        if self._skills is not None:
            skills = await self._skills.list_for_channel(task.channel_instance_id)

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
            skill_catalog="\n".join(s.catalog_line for s in skills),
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
            skills=skills,
        )
        return await self._runtime.run(task, bundle, approval_results)
