"""AgentRuntime 的检查点装配与增量计费。

计费是本次改造最容易出错的一处，两个方向都要锁死：

- **重复计费**：崩溃续跑时把「前几段累计」又扣一遍
- **漏计**：崩溃段已经花掉的 token 从未被 consume

故断言的是 ``consume`` 的**调用序列**，不只是最终总量 —— 总量对得上而序列错，
说明中途某次扣多了另一次扣少了，那在崩溃时就会露出来。
"""

from __future__ import annotations

import pytest

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.runtime import AgentRuntime, StageStatus
from teamai.config import Settings
from teamai.domain.models import ChannelInstance, PermissionPolicy, Task
from teamai.domain.models.checkpoint import TaskCheckpoint
from teamai.domain.ports import LLMGateway, LLMResult, TokenBudgetExceeded, ToolBundle
from teamai.domain.repositories.checkpoint import CheckpointRepository
from teamai.domain.services import AuditLogWriter
from tests.fakes import FakeAuditRepository


class ScriptedGateway(LLMGateway):
    """按脚本回调检查点，可在第 N 次回调后崩溃。"""

    def __init__(
        self,
        *,
        checkpoints: list[int],
        final_tokens: int,
        crash_after: int | None = None,
        exceed: bool = False,
    ) -> None:
        self.calls: list[dict] = []
        self._cps = checkpoints
        self._final = final_tokens
        self._crash_after = crash_after
        self._exceed = exceed

    async def run(
        self,
        prompt: str,
        *,
        model_level: str,
        system_prompt: str = "",
        tools: ToolBundle | None = None,
        token_limit: int | None = None,
        history: bytes | None = None,
        on_checkpoint: object | None = None,
    ) -> LLMResult:
        self.calls.append({"history": history, "token_limit": token_limit, "prompt": prompt})
        for i, seg in enumerate(self._cps, 1):
            if on_checkpoint is not None:
                await on_checkpoint(f"cp-{i}".encode(), seg)
            if self._crash_after is not None and i >= self._crash_after:
                raise RuntimeError("worker 崩了")
        if self._exceed:
            raise TokenBudgetExceeded("token 上限")
        return LLMResult(output="已处理", tokens=self._final)


class FakeCheckpoints(CheckpointRepository):
    def __init__(self, initial: TaskCheckpoint | None = None) -> None:
        self.store: dict[str, TaskCheckpoint] = {}
        if initial:
            self.store[initial.task_id] = initial
        self.upserts: list[tuple[str, bytes, int]] = []
        self.deleted: list[str] = []

    async def get(self, task_id: str) -> TaskCheckpoint | None:
        return self.store.get(task_id)

    async def upsert(self, task_id: str, messages: bytes, tokens_used: int) -> None:
        self.upserts.append((task_id, messages, tokens_used))
        old = self.store.get(task_id)
        self.store[task_id] = TaskCheckpoint(
            task_id=task_id,
            messages=messages,
            tokens_used=tokens_used,
            attempts=old.attempts if old else 0,
        )

    async def delete(self, task_id: str) -> None:
        self.deleted.append(task_id)
        self.store.pop(task_id, None)

    async def bump_attempts(self, task_id: str) -> int:
        cp = self.store.get(task_id)
        if cp is None:
            return 0
        cp.attempts += 1
        return cp.attempts


class SpyBudget:
    """记下 consume 的调用序列。remaining 可配。"""

    def __init__(self, remaining: int = 100_000) -> None:
        self.consumed: list[int] = []
        self._remaining = remaining

    async def check_quota(self, channel_instance_id: str) -> bool:
        return True

    async def remaining(self, channel_instance_id: str) -> int:
        return self._remaining

    async def consume(self, channel_instance_id: str, tokens: int) -> bool:
        # 对齐真实 BudgetController：<=0 不记账
        if tokens > 0:
            self.consumed.append(tokens)
        return True


class NoTools:
    def register(self, tool: object) -> None: ...

    def for_channel(self, allowed: list[str], skills: object = None) -> ToolBundle | None:
        return None


def _bundle() -> ContextBundle:
    instance = ChannelInstance(
        id="ch1", platform="slack", channel_id="C1", workspace_id="W1", agent_identity="teamai"
    )
    return ContextBundle(
        task_id="task_1",
        channel_instance_id="ch1",
        user_prompt="看下这个 PR",
        system_prompt="（系统提示词）",
        model_level="light",
        instance=instance,
        policy=PermissionPolicy(id="p1", channel_instance_id="ch1", allowed_tools=[]),
    )


def _task() -> Task:
    return Task(
        id="task_1", channel_instance_id="ch1", thread_ref="ts1", requester_id="u1", intent="ask"
    )


def _runtime(
    gateway: LLMGateway,
    budget: SpyBudget,
    checkpoints: CheckpointRepository | None = None,
) -> AgentRuntime:
    return AgentRuntime(
        gateway,
        NoTools(),
        budget,  # type: ignore[arg-type]
        AuditLogWriter(FakeAuditRepository()),
        Settings(context_max_messages=60, context_summary_threshold=120),
        checkpoints=checkpoints,
    )


# ---- 首次执行 ----


async def test_按增量计费而非每次扣总量() -> None:
    """回调报的是「本段累计」，故每次只该扣差值。

    扣总量的话：40 + 90 + 150 = 280，而实际只花了 150。
    """
    gw = ScriptedGateway(checkpoints=[40, 90], final_tokens=150)
    budget = SpyBudget()
    cps = FakeCheckpoints()

    r = await _runtime(gw, budget, cps).run(_task(), _bundle())

    assert r.status is StageStatus.DONE
    assert budget.consumed == [40, 50, 60], f"应按增量扣: {budget.consumed}"
    assert sum(budget.consumed) == 150


async def test_检查点落库带累计token() -> None:
    gw = ScriptedGateway(checkpoints=[40, 90], final_tokens=150)
    cps = FakeCheckpoints()

    await _runtime(gw, SpyBudget(), cps).run(_task(), _bundle())

    assert cps.upserts == [("task_1", b"cp-1", 40), ("task_1", b"cp-2", 90)]


async def test_无检查点时不重复计费() -> None:
    """纯文本任务：一个检查点都不落，收尾一次扣完。"""
    gw = ScriptedGateway(checkpoints=[], final_tokens=150)
    budget = SpyBudget()

    await _runtime(gw, budget, FakeCheckpoints()).run(_task(), _bundle())

    assert budget.consumed == [150]


async def test_首次执行不传history() -> None:
    gw = ScriptedGateway(checkpoints=[], final_tokens=10)

    await _runtime(gw, SpyBudget(), FakeCheckpoints()).run(_task(), _bundle())

    assert gw.calls[0]["history"] is None


# ---- 崩溃 ----


async def test_崩溃前的检查点已计费() -> None:
    """核心：崩溃段花掉的 token 必须已经扣掉。

    等 run 结束一次扣完的话，这些 token 永远不会被计费 —— 崩一次白花一次配额，
    而配额账面上看不出来。
    """
    gw = ScriptedGateway(checkpoints=[40, 90], final_tokens=999, crash_after=2)
    budget = SpyBudget()
    cps = FakeCheckpoints()

    r = await _runtime(gw, budget, cps).run(_task(), _bundle())

    assert r.status is StageStatus.FAILED
    assert budget.consumed == [40, 50], "崩溃前两个检查点的增量都该已扣"
    assert cps.store["task_1"].tokens_used == 90


async def test_崩溃后检查点保留供续跑() -> None:
    gw = ScriptedGateway(checkpoints=[40], final_tokens=999, crash_after=1)
    cps = FakeCheckpoints()

    await _runtime(gw, SpyBudget(), cps).run(_task(), _bundle())

    assert "task_1" in cps.store
    assert cps.deleted == [], "崩溃不该删检查点 —— 那是终态迁移的事"


# ---- 续跑 ----


async def test_续跑传history并累加base() -> None:
    """base=90 已在上一段扣过，本段只扣本段的量。"""
    existing = TaskCheckpoint(task_id="task_1", messages=b"cp-2", tokens_used=90, attempts=1)
    gw = ScriptedGateway(checkpoints=[30], final_tokens=70)
    budget = SpyBudget()
    cps = FakeCheckpoints(existing)

    r = await _runtime(gw, budget, cps).run(_task(), _bundle())

    assert gw.calls[0]["history"] == b"cp-2"
    assert budget.consumed == [30, 40], f"只该扣本段增量: {budget.consumed}"
    # 检查点里存的是任务总量
    assert cps.upserts == [("task_1", b"cp-1", 120)]
    # 返回的 usage_tokens 是任务总量（base + 本段）
    assert r.usage_tokens == 160


async def test_续跑不重复扣前几段() -> None:
    """回归点：若把 base 也 consume 一遍，前几段就被扣了两次。"""
    existing = TaskCheckpoint(task_id="task_1", messages=b"cp", tokens_used=1000, attempts=1)
    gw = ScriptedGateway(checkpoints=[], final_tokens=50)
    budget = SpyBudget()

    await _runtime(gw, SpyBudget(), FakeCheckpoints(existing)).run(_task(), _bundle())
    await _runtime(gw, budget, FakeCheckpoints(existing)).run(_task(), _bundle())

    assert budget.consumed == [50], f"1000 那段不该再扣: {budget.consumed}"


async def test_续跑次数进留痕() -> None:
    """排查「为什么这条特别慢/贵」时要能看出它续跑过几次。"""
    recorded: list[dict] = []

    class SpyInteractions:
        async def record(self, **kw: object) -> None:
            recorded.append(kw)

    existing = TaskCheckpoint(task_id="task_1", messages=b"cp", tokens_used=90, attempts=2)
    runtime = AgentRuntime(
        ScriptedGateway(checkpoints=[], final_tokens=10),
        NoTools(),
        SpyBudget(),  # type: ignore[arg-type]
        AuditLogWriter(FakeAuditRepository()),
        Settings(context_max_messages=60, context_summary_threshold=120),
        interactions=SpyInteractions(),  # type: ignore[arg-type]
        checkpoints=FakeCheckpoints(existing),
    )

    await runtime.run(_task(), _bundle())

    assert recorded[0]["context_refs"]["resume_count"] == 2


# ---- 预算上限 ----


async def test_token上限时检查点保留() -> None:
    """PAUSED 不是终态 —— 追加配额后应从断点续跑而非重新开始。"""
    gw = ScriptedGateway(checkpoints=[40], final_tokens=0, exceed=True)
    cps = FakeCheckpoints()

    r = await _runtime(gw, SpyBudget(), cps).run(_task(), _bundle())

    assert r.status is StageStatus.PAUSED
    assert "task_1" in cps.store
    assert cps.deleted == []


async def test_剩余配额直接作为本段上限() -> None:
    """不减 base —— 前几段已在各自的检查点扣过，remaining 已反映它们。"""
    existing = TaskCheckpoint(task_id="task_1", messages=b"cp", tokens_used=500, attempts=1)
    gw = ScriptedGateway(checkpoints=[], final_tokens=10)

    await _runtime(gw, SpyBudget(remaining=3000), FakeCheckpoints(existing)).run(
        _task(), _bundle()
    )

    assert gw.calls[0]["token_limit"] == 3000


# ---- 向后兼容 ----


async def test_未装配检查点仓储时行为不变() -> None:
    gw = ScriptedGateway(checkpoints=[40, 90], final_tokens=150)
    budget = SpyBudget()

    r = await _runtime(gw, budget, None).run(_task(), _bundle())

    assert r.status is StageStatus.DONE
    # 不传 on_checkpoint，故 gateway 的回调循环不生效，收尾一次扣完
    assert budget.consumed == [150]
    assert gw.calls[0]["history"] is None


@pytest.mark.parametrize("crash_at", [1, 2, 3])
async def test_任意点崩溃总计费不超实际花费(crash_at: int) -> None:
    """无论崩在哪个检查点，累计扣费都等于最后一个检查点报的量。"""
    gw = ScriptedGateway(
        checkpoints=[40, 90, 150], final_tokens=999, crash_after=crash_at
    )
    budget = SpyBudget()
    cps = FakeCheckpoints()

    await _runtime(gw, budget, cps).run(_task(), _bundle())

    expected_total = [40, 90, 150][crash_at - 1]
    assert sum(budget.consumed) == expected_total
    assert cps.store["task_1"].tokens_used == expected_total
