"""ContextBundle 的记忆渲染。

锁住一个缺陷：**矛盾记忆进了上下文却没有裁决依据**。

写入侧的去重与取代（MemoryDistiller 的 ADD / UPDATE / NOOP）只在蒸馏候选范围内
生效，而至少三条路会绕过它 —— 旧记忆没排进语义 top-10、向量不可用时候选退化成
「最近 10 条」、人工经 Admin API 写入完全不过冲突检查。于是库里可以有两条并列
现行而互相矛盾的记忆，它们语义相似度几乎相同、会一起进 top_k。

改造前渲染成裸 `- {content}`：模型手里零信号，只能靠猜或按顺序蒙，而顺序恰恰
不代表新旧（语义段按相似度排、回落段按时间倒序排，渲染出来一模一样）。
见 docs/Design-conversation-context.md §3.3.1。
"""

from __future__ import annotations

from datetime import UTC, datetime

from teamai.application.agent.context import ContextBundle
from teamai.domain.models import ChannelInstance, MemoryEntry, MemoryType

CH = ChannelInstance(
    id="ch_1",
    platform="slack",
    channel_id="C1",
    workspace_id="W1",
    agent_identity="ai_1",
)


def _entry(
    entry_id: str,
    content: str,
    *,
    day: int,
    type: MemoryType = MemoryType.FACT,
) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        channel_instance_id="ch_1",
        content=content,
        type=type,
        created_at=datetime(2026, 3, day, 10, 30, tzinfo=UTC),
    )


def _bundle(hits: list[MemoryEntry]) -> ContextBundle:
    return ContextBundle(
        task_id="t_1",
        channel_instance_id="ch_1",
        user_prompt="超时设的是多少",
        system_prompt="（系统提示词）",
        model_level="light",
        instance=CH,
        policy=None,
        memory_hits=hits,
    )


def test_每条记忆带写入日期() -> None:
    out = _bundle([_entry("mem_1", "超时设为 3 秒", day=5)]).memory_context

    assert out == "- [2026-03-05] 超时设为 3 秒"


def test_只到日不到秒() -> None:
    """矛盾记忆的间隔通常在天到月，精确到时刻纯属白耗 token。"""
    out = _bundle([_entry("mem_1", "超时设为 3 秒", day=5)]).memory_context

    assert "10:30" not in out
    assert ":" not in out


def test_矛盾的两条能靠日期分辨() -> None:
    """回归点。这两条都是现行（写入侧没抓到冲突），日期是唯一的裁决依据。

    刻意把旧的那条放在**前面**：语义段按相似度排序，越新不代表越靠前。改造前
    渲染出来两行只有内容不同，模型无从判断该信 3 秒还是 5 秒。
    """
    out = _bundle(
        [
            _entry("mem_old", "超时设为 3 秒", day=2),
            _entry("mem_new", "超时设为 5 秒", day=28),
        ]
    ).memory_context

    assert out.splitlines() == [
        "- [2026-03-02] 超时设为 3 秒",
        "- [2026-03-28] 超时设为 5 秒",
    ]


def test_无命中时为空串() -> None:
    """空串而非 "- "：runtime 靠它的真假决定是否插入 `[频道记忆]` 段，
    渲染出一个空标题会让模型以为「这个频道没有任何背景」是一条已知事实。"""
    assert _bundle([]).memory_context == ""


def test_偏好也带日期() -> None:
    """偏好是最容易前后矛盾的一类（同一个人改了主意，两条都是现行），
    而它不建向量、由 query_for_context 全量带上，没有任何相似度筛选拦着。"""
    out = _bundle(
        [_entry("mem_p", "偏好(U1): 回答要简短", day=9, type=MemoryType.PREFERENCE)]
    ).memory_context

    assert out == "- [2026-03-09] 偏好(U1): 回答要简短"


def test_压缩不动记忆段() -> None:
    """compact 只裁线程历史。记忆已受 top_k 约束，再裁一次会悄悄丢背景。"""
    hits = [_entry("mem_1", "超时设为 5 秒", day=5)]
    bundle = _bundle(hits)

    compacted = bundle.compact(max_history=2, summary_threshold=10)

    assert compacted.memory_context == bundle.memory_context
