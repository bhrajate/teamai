"""崩溃续跑全链路：真 SQLite + 真仓储 + 真 gateway（FunctionModel 充当模型）。

单元测试各自只覆盖一层，接缝处的错配在那些测试里都是绿的。这里把整条路走完：

1. 第一段跑到第二个工具轮后强制崩溃 → 检查点已落库、崩溃段已计费
2. 巡检发现超时的 RUNNING → 有检查点 → 重新入队、attempts+1、状态仍 RUNNING
3. 第二段从检查点续跑 → 只跑第三轮工具 → 产出与不崩溃时一致
4. 任务进终态 → 检查点被清掉
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from pydantic_ai import Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from teamai.application.agent.context import ContextBundle
from teamai.application.agent.runtime import AgentRuntime, StageStatus
from teamai.application.budget import BudgetController
from teamai.application.orchestrator import TaskOrchestrator
from teamai.config import Settings
from teamai.domain.models import (
    BudgetPeriod,
    BudgetQuota,
    BudgetScope,
    ChannelInstance,
    PermissionPolicy,
    Task,
    TaskStatus,
)
from teamai.domain.ports import QueuePayload
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.db import Base
from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway
from teamai.infrastructure.repositories import (
    SQLAuditRepository,
    SQLBudgetRepository,
    SQLCheckpointRepository,
    SQLTaskRepository,
)

CH = "ch_flow"
CALLS: list[str] = []


async def ping(n: int) -> str:
    """记账用工具。真实场景里这可能是一次 GitHub API 调用。"""
    CALLS.append(f"ping({n})")
    return f"pong-{n}"


class ThreeRounds:
    """三轮工具后收尾，可在第 N 轮抛异常模拟 worker 崩溃。

    ⚠️ 行为从**传入历史**推导，不用自增计数器：续跑时 FunctionModel 是新实例，
    计数器归零会从第一轮重放 —— 那测的是替身自己的 bug 而非真实行为。
    """

    __name__ = "three_rounds"

    def __init__(self, crash_at: int | None = None) -> None:
        self.crash_at = crash_at

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = sum(1 for m in messages for p in m.parts if isinstance(p, ToolReturnPart))
        if self.crash_at is not None and done >= self.crash_at:
            raise RuntimeError("worker 崩了（测试注入）")
        if done < 3:
            return ModelResponse(parts=[ToolCallPart("ping", {"n": done + 1})])
        return ModelResponse(parts=[TextPart("三轮都跑完了")])


class _Uow:
    """把仓储的 flush 收口成 commit。仓储只 flush，边界在用例层。"""

    def __init__(self, session) -> None:
        self._s = session

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, exc_type, *_: object) -> None:
        if exc_type is None:
            await self._s.commit()
        else:
            await self._s.rollback()


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[QueuePayload] = []

    async def enqueue(self, payload: QueuePayload) -> None:
        self.enqueued.append(payload)

    async def dequeue(self, timeout_seconds: float = 0) -> QueuePayload | None:
        return self.enqueued.pop(0) if self.enqueued else None


class NoTools:
    """工具集固定，不按频道裁剪 —— 本测试关注的是检查点而非权限。"""

    def register(self, tool: object) -> None: ...

    def for_channel(self, allowed: list[str], skills: object = None):
        return FunctionToolset([Tool(ping)])


class Env:
    def __init__(self, session, queue: FakeQueue) -> None:
        self.session = session
        self.queue = queue
        self.checkpoints = SQLCheckpointRepository(session)
        self.tasks = SQLTaskRepository(session)
        self.budget_repo = SQLBudgetRepository(session)
        self.audit = AuditLogWriter(SQLAuditRepository(session))
        self.budget = BudgetController(self.budget_repo, self.audit)
        self.orchestrator = TaskOrchestrator(
            self.tasks, self.audit, queue, self.checkpoints
        )
        self.uow = _Uow(session)

    def runtime(self, crash_at: int | None = None) -> AgentRuntime:
        """每段一个新 runtime + 新 gateway —— 模拟「换了个 worker 进程」。"""
        gw = PydanticAIGateway(ModelConfig())
        model = FunctionModel(ThreeRounds(crash_at))
        gw._model = lambda level: model  # type: ignore[assignment]  # noqa: SLF001
        return AgentRuntime(
            gw,
            NoTools(),
            self.budget,
            self.audit,
            Settings(context_max_messages=60, context_summary_threshold=120),
            checkpoints=self.checkpoints,
        )


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield Env(s, FakeQueue())
    await engine.dispose()


def _instance() -> ChannelInstance:
    return ChannelInstance(
        id=CH, platform="slack", channel_id="C1", workspace_id="W1", agent_identity="teamai"
    )


def _bundle(task: Task) -> ContextBundle:
    return ContextBundle(
        task_id=task.id,
        channel_instance_id=CH,
        user_prompt="审查这个 PR",
        system_prompt="（系统提示词）",
        model_level="light",
        instance=_instance(),
        policy=PermissionPolicy(id="p1", channel_instance_id=CH, allowed_tools=["ping"]),
    )


async def _new_task(env: Env, tid: str = "task_flow") -> Task:
    task = Task(
        id=tid,
        channel_instance_id=CH,
        thread_ref="ts1",
        requester_id="u1",
        intent="code_review",
    )
    task.status = TaskStatus.RUNNING
    async with env.uow:
        await env.tasks.create(task)
    return task


async def _quota(env: Env, limit: int = 1_000_000) -> None:
    async with env.uow:
        await env.budget_repo.upsert(
            BudgetQuota(
                id="bq_1",
                scope=BudgetScope.CHANNEL,
                channel_instance_id=CH,
                token_limit=limit,
                period=BudgetPeriod.DAILY,
            )
        )


async def _used(env: Env) -> int:
    q = await env.budget_repo.get_for_channel(CH)
    return q.used_tokens if q else 0


# ---- 基线 ----


async def test_不崩溃时三轮跑完(env: Env) -> None:
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)

    r = await env.runtime().run(task, _bundle(task))

    assert r.status is StageStatus.DONE
    assert r.output == "三轮都跑完了"
    assert CALLS == ["ping(1)", "ping(2)", "ping(3)"]
    # 三轮工具 → 三个检查点，最后一个仍在（终态清理由 transition 做）
    cp = await env.checkpoints.get(task.id)
    assert cp is not None


# ---- 崩溃 → 巡检 → 续跑 ----


async def test_崩溃续跑只跑剩余轮次(env: Env) -> None:
    """本测试是整个改造的核心验收。"""
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)

    # --- 第一段：跑完两轮后崩 ---
    r1 = await env.runtime(crash_at=2).run(task, _bundle(task))
    assert r1.status is StageStatus.FAILED
    assert CALLS == ["ping(1)", "ping(2)"]

    cp = await env.checkpoints.get(task.id)
    assert cp is not None, "崩溃前应已落检查点"
    assert cp.attempts == 0
    crashed_tokens = cp.tokens_used
    assert crashed_tokens > 0
    # 崩溃段的 token 必须已计费 —— 否则崩一次白花一次配额
    assert await _used(env) == crashed_tokens

    # --- 第二段：巡检发现超时，重新入队 ---
    task.updated_at = datetime.now(UTC) - timedelta(hours=48)
    async with env.uow:
        await env.tasks.update(task)

    report = await env.orchestrator.sweep_stale_tasks(
        pending_timeout=timedelta(minutes=30),
        running_timeout=timedelta(hours=24),
        max_resume_attempts=3,
    )
    assert [t.id for t in report.resumed] == [task.id]
    assert report.swept == []
    assert len(env.queue.enqueued) == 1
    assert (await env.checkpoints.get(task.id)).attempts == 1  # type: ignore[union-attr]

    reloaded = await env.tasks.get(task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.RUNNING, "续跑不改状态"

    # --- 第三段：续跑 ---
    CALLS.clear()
    r2 = await env.runtime().run(reloaded, _bundle(reloaded))

    assert r2.status is StageStatus.DONE
    assert r2.output == "三轮都跑完了", "续跑产出应与不崩溃时一致"
    assert CALLS == ["ping(3)"], f"只该跑第三轮，实际跑了 {CALLS}"


async def test_续跑后总计费大于崩溃前(env: Env) -> None:
    """续跑必然重发累积历史，故 input token 重复计费 —— 这是机制固有代价。

    锁住这条是为了防止有人把它「修」成相等：那只能靠不重发历史实现，
    而不重发历史就等于不能续跑。
    """
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)

    await env.runtime(crash_at=2).run(task, _bundle(task))
    after_crash = await _used(env)

    await env.runtime().run(task, _bundle(task))
    after_resume = await _used(env)

    assert after_resume > after_crash
    # 且检查点记的总量与实际扣费一致
    # （终态未迁移，检查点仍在）
    cp = await env.checkpoints.get(task.id)
    assert cp is not None
    assert cp.tokens_used <= after_resume


async def test_反复崩溃到上限后判死(env: Env) -> None:
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)

    for expected_attempts in (1, 2, 3):
        await env.runtime(crash_at=2).run(task, _bundle(task))
        task.updated_at = datetime.now(UTC) - timedelta(hours=48)
        async with env.uow:
            await env.tasks.update(task)
        report = await env.orchestrator.sweep_stale_tasks(
            pending_timeout=timedelta(minutes=30),
            running_timeout=timedelta(hours=24),
            max_resume_attempts=3,
        )
        assert [t.id for t in report.resumed] == [task.id]
        cp = await env.checkpoints.get(task.id)
        assert cp is not None and cp.attempts == expected_attempts

    # 第 4 次：已达上限 → 判死
    await env.runtime(crash_at=2).run(task, _bundle(task))
    task.updated_at = datetime.now(UTC) - timedelta(hours=48)
    async with env.uow:
        await env.tasks.update(task)
    report = await env.orchestrator.sweep_stale_tasks(
        pending_timeout=timedelta(minutes=30),
        running_timeout=timedelta(hours=24),
        max_resume_attempts=3,
    )

    assert [t.id for t in report.swept] == [task.id]
    assert report.resumed == []
    # 从库里回读而非看本地那个 task 对象：list_stale 返回的是新实例，
    # 状态迁移落在它身上。回读也顺带验证了确实落库。
    reloaded = await env.tasks.get(task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.FAILED
    # 判死走 transition() → 检查点已清
    assert await env.checkpoints.get(task.id) is None


# ---- 检查点内容 ----


async def test_原始提问保留在检查点里(env: Env) -> None:
    """这是「载荷可纯从 DB 重建」的前提 —— 巡检重投时 Redis 那条消息已被删。"""
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)

    await env.runtime(crash_at=1).run(task, _bundle(task))

    cp = await env.checkpoints.get(task.id)
    assert cp is not None
    assert b"PR" in cp.messages, "原始提问应在序列化的历史里"


async def test_检查点无悬空调用(env: Env) -> None:
    """带悬空调用的历史续跑时 SDK 直接抛 UserError。"""
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    from teamai.infrastructure.llm.gateway import _dangling

    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)

    await env.runtime(crash_at=2).run(task, _bundle(task))

    cp = await env.checkpoints.get(task.id)
    assert cp is not None
    msgs = ModelMessagesTypeAdapter.validate_json(cp.messages)
    assert _dangling(msgs) == 0


async def test_完成后检查点被清(env: Env) -> None:
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)
    await env.runtime().run(task, _bundle(task))
    assert await env.checkpoints.get(task.id) is not None

    await env.orchestrator.transition(task, TaskStatus.DONE, actor="u1")

    assert await env.checkpoints.get(task.id) is None


async def test_取消后检查点被清(env: Env) -> None:
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)
    await env.runtime(crash_at=2).run(task, _bundle(task))

    await env.orchestrator.cancel(task, actor="u1")

    assert await env.checkpoints.get(task.id) is None


# ---- 向后兼容 ----


async def test_未装配检查点时崩溃即彻底失败(env: Env) -> None:
    """行为与改造前一致。"""
    CALLS.clear()
    await _quota(env)
    task = await _new_task(env)
    gw = PydanticAIGateway(ModelConfig())
    gw._model = lambda level: FunctionModel(ThreeRounds(2))  # type: ignore[assignment]  # noqa: SLF001
    runtime = AgentRuntime(
        gw,
        NoTools(),
        env.budget,
        env.audit,
        Settings(context_max_messages=60, context_summary_threshold=120),
    )

    r = await runtime.run(task, _bundle(task))

    assert r.status is StageStatus.FAILED
    assert await env.checkpoints.get(task.id) is None, "没装配就不该有检查点"
