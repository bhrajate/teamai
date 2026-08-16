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
from enum import Enum

from teamai.application.agent.prompts import DISTILL_NONE, DISTILL_SYSTEM_PROMPT, build_distill_prompt
from teamai.application.budget import BudgetController
from teamai.application.memory import MemoryService
from teamai.domain.models import AuditAction, MemoryEntry, MemorySource, MemoryType
from teamai.domain.ports import LLMGateway, MessageWindow, TokenBudgetExceeded

logger = logging.getLogger(__name__)

# 蒸馏用轻量档：这是后台批处理，不值得用旗舰模型。
DISTILL_MODEL_LEVEL = "light"

# 单条记忆的长度上限。蒸馏结果本应是一句话，超长通常意味着模型没在提炼而是
# 在复述整段对话，截断比丢弃好（仍有信息），但要留痕。
MAX_ENTRY_LENGTH = 500

# 送给模型比对的已有记忆条数。取 10 与 mem0 的实验配置一致（arXiv:2504.19413
# §2.1 的 s=10）。比检索用的 top_k=5 大：这些是给模型看的候选比对项，漏掉一条
# 会让本该 UPDATE 的判成 ADD（多一条重复），而多给几条只是多花些 token。
CANDIDATE_TOP_K = 10


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


class DistillAction(Enum):
    """蒸馏结果对记忆库的动作。

    对齐 mem0 的 update 阶段（arXiv:2504.19413 §2.1），但**不给 DELETE**：
    那篇论文里 DELETE 用于移除「被新信息矛盾掉」的记忆，而本项目用
    `MemoryEntry.superseded_by` 表达同一件事且可回溯。删除不可逆，而「矛盾」
    的判断来自模型、可能是错的。真要删走人工路径（Admin API）。
    """

    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"


@dataclass
class DistillItem:
    """模型输出的一条动作。

    `ref` 是模型引用的**候选列表序号**（1-based），不是记忆 id —— 让模型输出
    ULID 极易出错，映射回真实 id 由 _apply_actions 做。
    """

    action: DistillAction
    type: MemoryType
    ref: int | None
    content: str


def _parse_entries(raw: str) -> list[DistillItem]:
    """解析模型输出的 `动作|类型|编号|内容` 行。

    宽容解析的原则不变：能救的都救回来，救不回来的丢一行而不是丢整批。

    - 动作无法识别时按 ADD 处理（不是丢弃）：模型漏写动作最可能是想新增，
      而按 ADD 处理最多多一条重复；判成 UPDATE 会误伤一条正确记忆。
    - 类型无法识别时归入 BACKGROUND_KNOWLEDGE —— 分类错了还能用。
    - UPDATE 缺编号时降级为 ADD：没有编号就无从取代。
    - 兼容旧的三段乃至两段格式（`类型|内容`）：那是加动作维度之前的输出形状，
      模型偶尔会退回去。缺动作即视为 ADD。
    """
    out: list[DistillItem] = []
    for line in raw.splitlines():
        text = line.strip().lstrip("-*•").strip()
        if not text or text.upper() == DISTILL_NONE:
            continue

        # ⚠️ 只对前三段 strip，内容段保持原样再 join。对每一段都 strip 会破坏
        # 内容里的 `|` 两侧空格 —— 模型贴命令或表格时（`a | b | c`）会被压成
        # `a|b|c`。前三段是动作/类型/编号，本就不含有意义的空格。
        parts = text.split("|")
        if len(parts) >= 4:
            raw_action, raw_type, raw_ref = parts[0].strip(), parts[1].strip(), parts[2].strip()
            content = "|".join(parts[3:])
        elif len(parts) == 3:
            parts = [p.strip() for p in parts]
            # 可能是 `动作|类型|内容`（漏了编号位）或 `类型|编号|内容`
            if parts[0].upper() in {a.value for a in DistillAction}:
                raw_action, raw_type, raw_ref, content = parts[0], parts[1], "", parts[2]
            else:
                raw_action, raw_type, raw_ref, content = "ADD", parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            raw_action, raw_type, raw_ref, content = "ADD", parts[0], "", parts[1]
        else:
            raw_action, raw_type, raw_ref, content = "ADD", "", "", parts[0]

        content = content.strip()

        try:
            action = DistillAction(raw_action.strip().upper())
        except ValueError:
            action = DistillAction.ADD

        try:
            mem_type = MemoryType[raw_type.strip().upper()]
        except KeyError:
            mem_type = MemoryType.BACKGROUND_KNOWLEDGE

        ref: int | None = None
        digits = raw_ref.strip()
        if digits.isdigit():
            ref = int(digits)

        if action is DistillAction.NOOP:
            # NOOP 不需要内容，但需要编号才有意义；两者都缺就是一行噪声
            if ref is None:
                continue
            out.append(DistillItem(action, mem_type, ref, ""))
            continue

        if not content:
            continue

        if action is DistillAction.UPDATE and ref is None:
            logger.warning("蒸馏输出 UPDATE 但缺编号，降级为 ADD")
            action = DistillAction.ADD

        if len(content) > MAX_ENTRY_LENGTH:
            logger.warning(f"蒸馏产出超长（{len(content)} 字），已截断")
            content = content[:MAX_ENTRY_LENGTH]

        out.append(DistillItem(action, mem_type, ref, content))
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

        # 先取该频道已有的近似记忆作为比对候选。用整窗文本作查询：这一步问的是
        # 「这段对话可能涉及哪些已有知识」，而具体哪条对应哪条由模型判断。
        candidates = await self._memory.find_similar(
            channel_instance_id, "\n".join(lines), CANDIDATE_TOP_K
        )

        remaining = await self._budget.remaining(channel_instance_id)
        result = await self._gateway.run(
            build_distill_prompt(lines, [c.content for c in candidates]),
            model_level=DISTILL_MODEL_LEVEL,
            system_prompt=DISTILL_SYSTEM_PROMPT,
            token_limit=remaining,
        )
        # 先记账再写记忆：蒸馏是后台行为，但烧的是该频道的配额。不计入的话
        # 「任意调用序列下 token 不超过配额」这条正确性属性会被后台任务绕过。
        await self._budget.consume(channel_instance_id, result.tokens)

        items = _parse_entries(result.output)
        written = await self._apply_actions(channel_instance_id, items, candidates)
        logger.info(
            f"频道 {channel_instance_id} 蒸馏 {len(lines)} 条对话 → "
            f"{len(items)} 条动作（新增/取代 {written} 条，候选 {len(candidates)} 条）"
        )
        return written

    async def _apply_actions(
        self,
        channel_instance_id: str,
        items: list[DistillItem],
        candidates: list[MemoryEntry],
    ) -> int:
        """把解析出的动作落到记忆库，返回实际写入（新增或取代）的条数。

        NOOP 不计入返回值：它表示「库里已经有了」，本轮没有产生新知识。若把它
        算进去，DistillReport 会把「什么都没变」报成有产出，而 sweep 的调用方
        靠这个数字判断是否有实际进展。

        一个编号只允许被取代一次：模型可能对同一条候选输出两个 UPDATE（例如把
        一件事拆成两句话表述），第二次取代的是已被标记的旧条目，会形成
        A→B→C 的链，而中间那条 B 从未进入过检索。第二次及以后降级为 ADD。
        """
        written = 0
        superseded_refs: set[int] = set()

        for item in items:
            if item.action is DistillAction.NOOP:
                continue

            target: MemoryEntry | None = None
            if item.action is DistillAction.UPDATE and item.ref is not None:
                # 编号是 1-based 的候选列表序号
                if 1 <= item.ref <= len(candidates):
                    if item.ref in superseded_refs:
                        logger.warning(
                            f"编号 {item.ref} 已被本轮取代过，本条降级为 ADD"
                        )
                    else:
                        target = candidates[item.ref - 1]
                else:
                    # 模型编了一个不存在的编号。降级而非丢弃：内容本身可能有效，
                    # 丢掉等于让这条知识彻底进不来，多一条重复的代价小得多。
                    logger.warning(
                        f"蒸馏引用了不存在的候选编号 {item.ref}"
                        f"（候选共 {len(candidates)} 条），本条降级为 ADD"
                    )

            if target is not None:
                new_entry = await self._memory.supersede(
                    target.id,
                    channel_instance_id,
                    item.content,
                    type=item.type,
                    source=MemorySource.DISTILLED,
                    action=AuditAction.MEMORY_DISTILL,
                )
                if new_entry is not None:
                    superseded_refs.add(item.ref)  # type: ignore[arg-type]
                    written += 1
                    continue
                # supersede 返回 None：旧条目已不存在或不属本频道（跨频道被拒）。
                # 落到下面按 ADD 处理。
                logger.warning(f"取代记忆 {target.id} 失败，本条改为新增")

            await self._memory.store(
                channel_instance_id,
                item.content,
                type=item.type,
                source=MemorySource.DISTILLED,
                action=AuditAction.MEMORY_DISTILL,
            )
            written += 1

        return written
