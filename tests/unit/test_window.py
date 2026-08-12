"""MessageWindow 语义测试（内存实现）。

Redis 实现的分支（pipeline、ZSET 索引）需要真 Redis，不在单测覆盖；这里锁住
两个实现共有的语义契约，内存实现同时是 Redis 不可用时的降级路径：

1. 窗口满或静置到期才算「该蒸馏」—— 判据错了会导致要么永不落地、要么每条
   消息都触发一次 LLM 调用；
2. drain 必须清空。留着会让下一轮重复蒸馏同一批对话、产出重复记忆。
"""

from __future__ import annotations

import time

from teamai.infrastructure.window import MAX_WINDOW_SIZE, InMemoryMessageWindow


async def test_append返回窗口长度() -> None:
    window = InMemoryMessageWindow()

    assert await window.append("ch_1", "第一句") == 1
    assert await window.append("ch_1", "第二句") == 2


async def test_窗口未满且未静置时不到期() -> None:
    window = InMemoryMessageWindow()
    await window.append("ch_1", "一句话")

    assert await window.due_channels(max_size=20, max_idle_seconds=600) == []


async def test_窗口满则到期() -> None:
    window = InMemoryMessageWindow()
    for i in range(3):
        await window.append("ch_1", f"第 {i} 句")

    assert await window.due_channels(max_size=3, max_idle_seconds=600) == ["ch_1"]


async def test_静置到期即使窗口没满() -> None:
    """冷清频道的对话不该一直攒着不落地。"""
    window = InMemoryMessageWindow()
    await window.append("ch_1", "唯一一句")

    assert await window.due_channels(max_size=999, max_idle_seconds=0) == ["ch_1"]


async def test_首次写入时间不被后续消息刷新() -> None:
    """若每条消息都刷新计时，「持续有人说话」的频道永远不满足静置条件 ——
    而热闹频道恰恰最该及时蒸馏。"""
    window = InMemoryMessageWindow()
    await window.append("ch_1", "第一句")
    time.sleep(0.02)
    await window.append("ch_1", "第二句")

    # 静置阈值取一个极小值：只要计时没被第二条消息重置，就该判到期
    assert await window.due_channels(max_size=999, max_idle_seconds=0.01) == ["ch_1"]


async def test_drain取出并清空() -> None:
    window = InMemoryMessageWindow()
    for i in range(3):
        await window.append("ch_1", f"第 {i} 句")

    assert await window.drain("ch_1") == ["第 0 句", "第 1 句", "第 2 句"]
    assert await window.drain("ch_1") == []
    assert await window.due_channels(max_size=1, max_idle_seconds=0) == []


async def test_drain后重新计时() -> None:
    """否则蒸馏过一次的频道会因为旧时间戳而每轮都被判到期。"""
    window = InMemoryMessageWindow()
    await window.append("ch_1", "旧的一句")
    await window.drain("ch_1")

    await window.append("ch_1", "新的一句")

    assert await window.due_channels(max_size=999, max_idle_seconds=600) == []


async def test_频道之间互不干扰() -> None:
    window = InMemoryMessageWindow()
    await window.append("ch_A", "A 的话")
    await window.append("ch_B", "B 的话")

    assert await window.drain("ch_A") == ["A 的话"]
    assert await window.drain("ch_B") == ["B 的话"]


async def test_空窗口不算到期() -> None:
    """drain 过的频道键还在 dict 里时，不该被当成候选。"""
    window = InMemoryMessageWindow()
    await window.append("ch_1", "一句")
    await window.drain("ch_1")

    assert await window.due_channels(max_size=0, max_idle_seconds=0) == []


async def test_超上限时丢最旧的() -> None:
    """防止某个频道刷屏把内存吃满。蒸馏取的是近期讨论的结论，最旧的价值最低。"""
    window = InMemoryMessageWindow()
    for i in range(MAX_WINDOW_SIZE + 10):
        await window.append("ch_1", f"第 {i} 句")

    lines = await window.drain("ch_1")

    assert len(lines) == MAX_WINDOW_SIZE
    assert lines[0] == "第 10 句", "最旧的 10 条应被丢弃"
    assert lines[-1] == f"第 {MAX_WINDOW_SIZE + 9} 句"
