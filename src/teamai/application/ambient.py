"""Ambient Mode：不被 @ 也主动介入。

默认关闭，由频道管理员开启（`ChannelInstance.ambient_enabled`），具体触发条件
由该频道策略里的 `PermissionPolicy.ambient_rules` 决定。两个开关都要过：
频道级总闸 + 规则级配置，缺任一则不介入。

本文件是规则引擎骨架 + `thread_stale` 一条规则。规则按 `trigger` 注册进
`_HANDLERS`，新增规则只加一个 handler，`sweep()` 不改。

为什么只有一条规则：另两条（`error_spike` / `deploy_status`）仍缺可用的数据源。

`deploy_status` 要 CI 的入向 webhook，而项目没有对外的 webhook 入口。

`error_spike` 的情况变了但还不够：会话上下文改造后，非 @ 消息会进 `MessageWindow`
滚动缓冲（见 application/distiller.py），正文终于有地方可读。但那个端口只有
`drain`（取出即清空）—— 它是为蒸馏设计的，而 error_spike 要的是「按错误关键词
聚合计数」且不能把素材吃掉，否则记忆蒸馏就拿不到了。补它需要给端口加一个只读
不清空的取数方法，那是独立决策。

注意仍然**没有**消息历史表：原始聊天记录不入库，以平台为唯一权威源，
理由见 docs/Design-conversation-context.md §2。所以 error_spike 的窗口天然只有
分钟级，做不了「过去 24 小时的错误趋势」那类规则 —— 那种需求应该接监控系统，
而不是靠翻聊天记录。

由 worker 的定时任务驱动（app/worker/main.py 的 register_jobs），与超时巡检
分开：那个是运维兜底、判 FAILED；这个是产品行为、给人发提醒。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from teamai.domain.models import Task, TaskStatus
from teamai.domain.models.channel import ChannelInstance
from teamai.domain.models.policy import AmbientRule
from teamai.domain.ports import AmbientCooldown, MessagePublisher, ReplyTarget
from teamai.domain.repositories.channel import ChannelRepository
from teamai.domain.repositories.policy import PolicyRepository
from teamai.domain.repositories.task import TaskRepository

logger = logging.getLogger(__name__)

# thread_stale 的参数默认值。params 是自由 dict，缺项即用这里的值。
DEFAULT_IDLE_MINUTES = 60
# 冷却默认取沉寂阈值本身：同一任务持续沉寂时按同样的节奏提醒，
# 既不至于每轮巡检都打扰，也不会久到让人以为没人管。
DEFAULT_COOLDOWN_MULTIPLIER = 1

# 仍在进行、可被催的状态。终态与 PAUSED 不催：
# PAUSED 是预算耗尽导致的，催也推不动，该修的是配额。
ACTIVE_STATUSES = (TaskStatus.RUNNING, TaskStatus.WAITING_INPUT)


@dataclass
class AmbientReport:
    """一轮巡检的结果。区分「发了」「被冷却挡住」「发送失败」三类。

    只报数字不够：全部发送失败与一条都不该发在日志里长得一样，
    而前者是故障、后者是正常。
    """

    nudged: list[str] = field(default_factory=list)
    cooled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return len(self.nudged) + len(self.cooled) + len(self.failed)


def _minutes(rule: AmbientRule, key: str, default: int) -> int:
    """从 params 取正整数分钟数，缺失或不合法则用默认值。

    不抛异常：策略由管理员经 Admin API 写入，一个笔误不该让整轮巡检崩掉、
    连带其他频道的提醒一起丢。
    """
    raw = rule.params.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"ambient 规则 {rule.trigger} 的 {key} 不是整数: {raw!r}，用默认 {default}")
        return default
    if value <= 0:
        logger.warning(f"ambient 规则 {rule.trigger} 的 {key} 须为正数: {value}，用默认 {default}")
        return default
    return value


def _nudge_text(task: Task, idle_minutes: int) -> str:
    stage = f"（当前阶段：{task.current_stage}）" if task.current_stage else ""
    if task.status is TaskStatus.WAITING_INPUT:
        return f"任务 {task.id} 还在等补充信息{stage}，已经 {idle_minutes} 分钟没有新进展了。"
    return f"任务 {task.id} 已执行 {idle_minutes} 分钟没有新进展{stage}，需要我继续推进还是先搁置？"


class AmbientService:
    """规则巡检。每轮扫一遍进行中的任务，按频道策略决定是否主动提醒。"""

    def __init__(
        self,
        tasks: TaskRepository,
        channels: ChannelRepository,
        policies: PolicyRepository,
        cooldown: AmbientCooldown,
        publisher: MessagePublisher,
    ) -> None:
        self._tasks = tasks
        self._channels = channels
        self._policies = policies
        self._cooldown = cooldown
        self._publisher = publisher
        # 规则按 trigger 分发。新增规则只在此登记，sweep() 不动。
        self._handlers: dict[str, Callable[[ChannelInstance, AmbientRule, list[Task]], Awaitable[AmbientReport]]] = {
            "thread_stale": self._thread_stale,
        }

    async def sweep(self) -> AmbientReport:
        """跑一轮全部规则。

        驱动方式是「先查进行中的任务，再按频道聚合」而不是「遍历开了 ambient
        的频道」：仓储没有「列出 ambient_enabled 频道」的方法，而任务本身带
        channel_instance_id，这样零新增仓储接口。代价是没有进行中任务的频道
        不会被扫到 —— 对 thread_stale 无影响（它本就以任务为对象），但将来
        接 error_spike（以频道消息为对象）时得换驱动方式，那时再加接口。

        取「最宽的窗口」一次查库：各频道的 idle_minutes 不同，按最小值查出候选
        再逐频道用自己的阈值筛，避免每个频道打一次库。
        """
        report = AmbientReport()
        candidates = await self._candidates()
        if not candidates:
            return report

        by_channel: dict[str, list[Task]] = defaultdict(list)
        for task in candidates:
            by_channel[task.channel_instance_id].append(task)

        for channel_id, tasks in by_channel.items():
            try:
                sub = await self._sweep_channel(channel_id, tasks)
            except Exception as exc:
                # 一个频道的策略读取或发送失败不该带崩其他频道
                logger.error(f"ambient 巡检频道 {channel_id} 失败: {exc}")
                continue
            report.nudged.extend(sub.nudged)
            report.cooled.extend(sub.cooled)
            report.failed.extend(sub.failed)
        return report

    async def _candidates(self) -> list[Task]:
        """按最宽窗口取进行中的候选任务。

        这里用默认阈值作为窗口：策略里若配了更短的 idle_minutes，短于默认值的
        那部分任务本轮取不到，会在下一轮（或阈值走满后）被取到。用最短阈值查
        需要先读全部策略，反而多打一遍库；主动提醒差一轮巡检无实质影响。
        """
        before = datetime.now(UTC) - timedelta(minutes=DEFAULT_IDLE_MINUTES)
        return await self._tasks.list_stale(ACTIVE_STATUSES, before)

    async def _sweep_channel(self, channel_id: str, tasks: list[Task]) -> AmbientReport:
        report = AmbientReport()
        instance = await self._channels.get(channel_id)
        if instance is None or not instance.ambient_enabled:
            return report  # 总闸没开，静默跳过

        policy = await self._policies.get_for_channel(channel_id)
        if policy is None:
            return report

        for rule in policy.ambient_rules:
            handler = self._handlers.get(rule.trigger)
            if handler is None:
                logger.warning(f"频道 {channel_id} 配了未实现的 ambient 规则: {rule.trigger}")
                continue
            sub = await handler(instance, rule, tasks)
            report.nudged.extend(sub.nudged)
            report.cooled.extend(sub.cooled)
            report.failed.extend(sub.failed)
        return report

    async def _thread_stale(
        self, instance: ChannelInstance, rule: AmbientRule, tasks: list[Task]
    ) -> AmbientReport:
        """任务停滞提醒：进行中但久无进展的任务，在原线程里问一句。"""
        report = AmbientReport()
        idle = _minutes(rule, "idle_minutes", DEFAULT_IDLE_MINUTES)
        cooldown_minutes = _minutes(rule, "cooldown_minutes", idle * DEFAULT_COOLDOWN_MULTIPLIER)
        cutoff = datetime.now(UTC) - timedelta(minutes=idle)

        for task in tasks:
            if task.updated_at > cutoff:
                continue  # 该频道阈值比查询窗口更严，这条还不算沉寂
            key = f"{rule.trigger}:{task.id}"
            if await self._cooldown.is_cooling(key, cooldown_minutes * 60):
                report.cooled.append(task.id)
                continue
            target = ReplyTarget(
                platform=instance.platform,
                channel_id=instance.channel_id,
                thread_ref=task.thread_ref,
            )
            try:
                await self._publisher.reply(target, _nudge_text(task, idle))
            except Exception as exc:
                # 冷却已占位：本轮不再重试，等冷却过期后自然重来，
                # 避免平台故障时每轮巡检都对同一任务重试一遍
                logger.warning(f"ambient 提醒发送失败 {task.id}: {exc}")
                report.failed.append(task.id)
                continue
            report.nudged.append(task.id)
        return report
