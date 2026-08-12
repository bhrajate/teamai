"""记忆蒸馏：把对话窗口提炼成少量结论，而不是逐条存原文。

改造前 router 对每条非 @ 消息调一次 `MemoryService.store`，于是 memory_entries
变成了掐掉 500 字的聊天日志：向量检索的信噪比被稀释，「收到」与真正的项目背景
知识在同一个索引里竞争 top_k。

现在的链路是：router 只把消息 append 进 MessageWindow（Redis 滚动缓冲），本服务
由 worker 的定时任务驱动，成窗取出、跑一次轻量模型抽取、只把结论写进记忆库，
原文随即丢弃。

为什么蒸馏放在 worker 而不是 router 里判断「窗口是否已满」：后者会让某条恰好
触发阈值的普通消息承担一次 LLM 调用的延迟，而这条消息的发送者根本没有 @ 机器人、
不该为此等待。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from teamai.application.agent.prompts import DISTILL_NONE, DISTILL_SYSTEM_PROMPT, build_distill_prompt
from teamai.application.budget import BudgetController
from teamai.application.memory import MemoryService
from teamai.domain.models import AuditAction, MemorySource, MemoryType
from teamai.domain.ports import LLMGateway, MessageWindow, TokenBudgetExceeded

logger = logging.getLogger(__name__)

# 蒸馏用轻量档：这是后台批处理，不值得用旗舰模型。
DISTILL_MODEL_LEVEL = "light"

# 单条记忆的长度上限。蒸馏结果本应是一句话，超长通常意味着模型没在提炼而是
# 在复述整段对话，截断比丢弃好（仍有信息），但要留痕。
MAX_ENTRY_LENGTH = 500


@dataclass
class DistillReport:
    """一轮蒸馏的结果。

    分开记三类而不是只报数字：全部频道蒸馏失败与「没有到期的窗口」在日志里
    长得一样，而前者是故障。这与 SweepReport / AmbientReport 的取舍一致。
    """

    distilled: dict[str, int] = field(default_factory=dict)  # channel_id -> 产出条数
    empty: list[str] = field(default_factory=list)  # 蒸馏后无可记内容
    failed: list[tuple[str, str]] = field(default_factory=list)  # (channel_id, 错因)
    skipped_budget: list[str] = field(default_factory=list)  # 配额不足，未蒸馏

    @property
    def considered(self) -> int:
        return (
            len(self.distilled) + len(self.empty) + len(self.failed) + len(self.skipped_budget)
        )

    @property
    def total_entries(self) -> int:
        return sum(self.distilled.values())


def _parse_entries(raw: str) -> list[tuple[MemoryType, str]]:
    """解析模型输出的 `类型|内容` 行。

    宽容解析：类型无法识别时归入 BACKGROUND_KNOWLEDGE 而不是丢弃整行 ——
    分类错了还能用，内容丢了就没了。整行没有分隔符时同样按背景知识收下。
    """
    out: list[tuple[MemoryType, str]] = []
    for line in raw.splitlines():
        text = line.strip().lstrip("-*•").strip()
        if not text or text.upper() == DISTILL_NONE:
            continue
        kind, sep, content = text.partition("|")
        if not sep:
            kind, content = "", text
        content = content.strip()
        if not content:
            continue
        try:
            mem_type = MemoryType[kind.strip().upper()]
        except KeyError:
            mem_type = MemoryType.BACKGROUND_KNOWLEDGE
        if len(content) > MAX_ENTRY_LENGTH:
            logger.warning(f"蒸馏产出超长（{len(content)} 字），已截断")
            content = content[:MAX_ENTRY_LENGTH]
        out.append((mem_type, content))
    return out


class MemoryDistiller:
    def __init__(
        self,
        window: MessageWindow,
        memory: MemoryService,
        gateway: LLMGateway,
        budget: BudgetController,
        *,
        window_size: int = 20,
        max_idle_seconds: int = 600,
    ) -> None:
        self._window = window
        self._memory = memory
        self._gateway = gateway
        self._budget = budget
        self._window_size = window_size
        self._max_idle_seconds = max_idle_seconds

    async def observe(self, channel_instance_id: str, author_id: str, text: str) -> None:
        """把一条普通消息放进待蒸馏窗口。

        不在此触发蒸馏：见模块注释。调用方（router）应对私聊消息不调本方法。
        """
        line = f"{author_id or 'unknown'}: {text}"
        try:
            await self._window.append(channel_instance_id, line)
        except Exception as exc:
            # 缓冲写失败只是「这条对话没进记忆素材」，不该影响消息处理主流程
            logger.warning(f"消息入窗失败 {channel_instance_id}: {exc}")

    async def sweep(self) -> DistillReport:
        """蒸馏全部到期窗口。由 worker 定时任务调用。"""
        report = DistillReport()
        try:
            channels = await self._window.due_channels(self._window_size, self._max_idle_seconds)
        except Exception as exc:
            logger.error(f"查询到期蒸馏窗口失败: {exc}")
            return report

        for channel_id in channels:
            try:
                if not await self._budget.check_quota(channel_id):
                    # 配额耗尽时不蒸馏，但**也不 drain**：窗口留着，配额恢复后
                    # 下一轮再处理。drain 掉等于让「预算暂停」顺带丢掉记忆素材，
                    # 而用户对暂停的预期只是「任务先不跑」。
                    report.skipped_budget.append(channel_id)
                    continue
                count = await self._distill_channel(channel_id)
            except TokenBudgetExceeded:
                # 蒸馏途中触达上限。原文已 drain 且不重投，理由同下方注释。
                logger.info(f"频道 {channel_id} 蒸馏触达 token 上限，跳过")
                report.skipped_budget.append(channel_id)
                continue
            except Exception as exc:
                # 单个频道失败不打断整轮：一次模型调用异常不该让其余频道的
                # 窗口继续堆积。窗口已被 drain，这批原文就此丢弃 —— 记忆是
                # 尽力而为的增益，不值得为它引入重试队列与幂等设计。
                logger.warning(f"频道 {channel_id} 蒸馏失败: {exc}")
                report.failed.append((channel_id, str(exc)))
                continue
            if count:
                report.distilled[channel_id] = count
            else:
                report.empty.append(channel_id)
        return report

    async def _distill_channel(self, channel_instance_id: str) -> int:
        lines = await self._window.drain(channel_instance_id)
        if not lines:
            return 0

        remaining = await self._budget.remaining(channel_instance_id)
        result = await self._gateway.run(
            build_distill_prompt(lines),
            model_level=DISTILL_MODEL_LEVEL,
            system_prompt=DISTILL_SYSTEM_PROMPT,
            token_limit=remaining,
        )
        # 先记账再写记忆：蒸馏是后台行为，但烧的是该频道的配额。不计入的话
        # 「任意调用序列下 token 不超过配额」这条正确性属性会被后台任务绕过。
        await self._budget.consume(channel_instance_id, result.tokens)
        entries = _parse_entries(result.output)
        for mem_type, content in entries:
            await self._memory.store(
                channel_instance_id,
                content,
                type=mem_type,
                source=MemorySource.DISTILLED,
                action=AuditAction.MEMORY_DISTILL,
            )
        logger.info(
            f"频道 {channel_instance_id} 蒸馏 {len(lines)} 条对话 → {len(entries)} 条记忆"
        )
        return len(entries)
