"""工具审批全链路：真 SQLite + 真仓储 + 真 gateway（FunctionModel 充当模型）。

单元测试各自只覆盖一层，接缝处的错配在那些测试里都是绿的。这里把整条路走完：

1. 危险工具被闸拦下 → 落待批 → 任务转 WAITING_INPUT → 通知里 @ 审批人
2. 发起人试图自批 → 被拒 → 任务仍在等
3. 合法审批人 /approve → 工具执行 → 任务 DONE
4. 只读工具在同一轮里照常执行、不受影响
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from teamai.application.agent.runtime import AgentRuntime
from teamai.application.approval import ApprovalService
from teamai.application.budget import BudgetController
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.config import Settings
from teamai.domain.models import (
    BudgetPeriod,
    BudgetQuota,
    BudgetScope,
    ChannelInstance,
    PermissionPolicy,
    TaskStatus,
)
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.db import Base
from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway
from teamai.infrastructure.repositories import (
    SQLAuditRepository,
    SQLBudgetRepository,
    SQLChannelRepository,
    SQLCheckpointRepository,
    SQLPolicyRepository,
    SQLTaskRepository,
)
from teamai.infrastructure.tools.registry import ToolRegistry
from tests.doubles import (
    FakeConversation,
    FakeDistiller,
    FakeIntentClassifier,
    FakeMemory,
    FakeTags,
    mention,
)

CH = "ch_appr"
EXECUTED: list[str] = []


async def github(action: str, title: str = "") -> str:
    """有真副作用的工具 —— 未经批准不该执行。"""
    EXECUTED.append(f"github({action},{title})")
    return f"PR 已创建：{title}"


async def monitoring(action: str) -> str:
    """只读工具，不需要审批。"""
    EXECUTED.append(f"monitoring({action})")
    return "无告警"


class Script:
    """先查监控，再提 PR，最后收尾。行为从历史推导 —— 恢复时是新实例。"""

    __name__ = "script"

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = {p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)}
        if "monitoring" not in done:
            return ModelResponse(parts=[ToolCallPart("monitoring", {"action": "alerts"})])
        if "github" not in done:
            return ModelResponse(
                parts=[ToolCallPart("github", {"action": "create_pr", "title": "修登录超时"})]
            )
        return ModelResponse(parts=[TextPart("都处理完了")])


class _Uow:
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
    async def enqueue(self, payload: object) -> None: ...

    async def dequeue(self, timeout_seconds: float = 0) -> None:
        return None


class FakeChannelsSvc:
    def __init__(self, instance: ChannelInstance) -> None:
        self._i = instance

    async def get_or_create(self, platform: str, channel_id: str, workspace_id: str):
        return self._i

    async def get(self, channel_instance_id: str):
        return self._i


class Env:
    def __init__(self, session) -> None:
        self.session = session
        self.uow = _Uow(session)
        self.tasks = SQLTaskRepository(session)
        self.policies = SQLPolicyRepository(session)
        self.budget_repo = SQLBudgetRepository(session)
        self.channels_repo = SQLChannelRepository(session)
        self.checkpoints = SQLCheckpointRepository(session)
        self.audit_repo = SQLAuditRepository(session)
        self.audit = AuditLogWriter(self.audit_repo)
        self.approvals = ApprovalService(self.checkpoints, self.audit)
        self.instance = ChannelInstance(
            id=CH, platform="slack", channel_id="C1", workspace_id="T1", agent_identity="teamai"
        )
        self.orchestrator = TaskOrchestrator(
            self.tasks, self.audit, FakeQueue(), self.checkpoints
        )
        registry = ToolRegistry()
        registry.register(Tool(github))
        registry.register(Tool(monitoring))
        gw = PydanticAIGateway(ModelConfig())
        gw._model = lambda level: FunctionModel(Script())  # type: ignore[assignment]  # noqa: SLF001
        self.runtime = AgentRuntime(
            gw,
            registry,
            BudgetController(self.budget_repo, self.audit),
            self.audit,
            Settings(context_max_messages=60, context_summary_threshold=120),
            checkpoints=self.checkpoints,
        )
        self.router = MessageRouter(
            orchestrator=self.orchestrator,
            intent=FakeIntentClassifier("query"),
            tags=FakeTags(),
            memory=FakeMemory(),
            budget=BudgetController(self.budget_repo, self.audit),
            runtime=self.runtime,
            channels=FakeChannelsSvc(self.instance),
            policy_repo=self.policies,
            conversation=FakeConversation(),
            distiller=FakeDistiller(),
            approvals=self.approvals,
        )

    async def setup(self, *, approvers: list[str], required: int = 1) -> None:
        async with self.uow:
            await self.channels_repo.upsert(self.instance)
            await self.budget_repo.upsert(
                BudgetQuota(
                    id="bq_1",
                    scope=BudgetScope.CHANNEL,
                    channel_instance_id=CH,
                    token_limit=1_000_000,
                    period=BudgetPeriod.DAILY,
                )
            )
        await self.policies.upsert(
            PermissionPolicy(
                id="pol_1",
                channel_instance_id=CH,
                allowed_tools=["github", "monitoring"],
                approval_required_tools={"github": required},
                approver_ids=approvers,
            )
        )

    async def events(self) -> list[str]:
        logs = await self.audit_repo.list_by_channel(CH, limit=200)
        return [x.detail.get("event", "") for x in logs]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield Env(s)
    await engine.dispose()


async def _current_task(env: Env):
    tasks = await env.tasks.list_by_channel(CH)
    assert tasks, "没有任务"
    return tasks[0]


# ---- 主链路 ----


async def test_危险工具被拦下并通知审批人(env: Env) -> None:
    CALLS = EXECUTED
    CALLS.clear()
    await env.setup(approvers=["U_APPROVER"])

    decision = await env.router.route(mention("帮我修 bug 并提 PR", user="U_REQUESTER"))

    assert CALLS == ["monitoring(alerts)"], f"只读工具该跑、危险工具不该跑: {CALLS}"
    task = await _current_task(env)
    assert task.status is TaskStatus.WAITING_INPUT
    assert "@U_APPROVER" in decision.message
    assert "github" in decision.message
    # 参数要列全 —— 审批人得看全才能判断
    assert "修登录超时" in decision.message
    pending = await env.checkpoints.get_pending_approval(task.id)
    assert pending is not None
    assert pending.tool_name == "github"
    assert "approval_required" in await env.events()


async def test_发起人自批被拒任务仍在等(env: Env) -> None:
    """四眼原则的核心。发起人在名单里也拦。"""
    EXECUTED.clear()
    await env.setup(approvers=["U_REQUESTER", "U_APPROVER"])
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    task = await _current_task(env)

    decision = await env.router.route(mention("/approve", user="U_REQUESTER"))

    assert "发起人" in decision.message
    reloaded = await env.tasks.get(task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.WAITING_INPUT, "被拒后仍该等着"
    assert "github(create_pr,修登录超时)" not in EXECUTED
    assert "approval_rejected_self" in await env.events()


async def test_合法审批人批准后工具执行(env: Env) -> None:
    EXECUTED.clear()
    await env.setup(approvers=["U_APPROVER"])
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    task = await _current_task(env)
    EXECUTED.clear()

    decision = await env.router.route(mention("/approve", user="U_APPROVER"))

    assert EXECUTED == ["github(create_pr,修登录超时)"], f"该执行且不重放: {EXECUTED}"
    reloaded = await env.tasks.get(task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.DONE
    assert decision.message == "都处理完了"
    # 待批项已清，但检查点该留着
    assert await env.checkpoints.get_pending_approval(task.id) is None
    assert "approval_granted" in await env.events()


async def test_拒绝后工具不执行且模型收尾(env: Env) -> None:
    EXECUTED.clear()
    await env.setup(approvers=["U_APPROVER"])
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    task = await _current_task(env)
    EXECUTED.clear()

    decision = await env.router.route(mention("/deny 现在不该提", user="U_APPROVER"))

    assert EXECUTED == [], "被拒的工具不该执行"
    reloaded = await env.tasks.get(task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.DONE, "拒绝也是结论，run 该跑完"
    assert decision.message, "模型该说明"
    assert "approval_denied" in await env.events()


async def test_无审批人时判失败不放行(env: Env) -> None:
    """**最危险的退化**：默认放宽等于没有审批。"""
    EXECUTED.clear()
    await env.setup(approvers=[])

    decision = await env.router.route(mention("提个 PR", user="U_REQUESTER"))

    assert "github(create_pr,修登录超时)" not in EXECUTED
    task = await _current_task(env)
    assert task.status is TaskStatus.FAILED
    assert "没有配置审批人" in decision.message


# ---- 双批 ----


async def test_双批需两个不同的人(env: Env) -> None:
    EXECUTED.clear()
    await env.setup(approvers=["U_A", "U_B"], required=2)
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    task = await _current_task(env)
    EXECUTED.clear()

    first = await env.router.route(mention("/approve", user="U_A"))
    assert "1/2" in first.message
    assert EXECUTED == [], "一人批完还不该执行"

    dup = await env.router.route(mention("/approve", user="U_A"))
    assert "已经批准过" in dup.message
    assert EXECUTED == [], "同一人点两次不该凑够"

    await env.router.route(mention("/approve", user="U_B"))
    assert EXECUTED == ["github(create_pr,修登录超时)"]
    reloaded = await env.tasks.get(task.id)
    assert reloaded is not None and reloaded.status is TaskStatus.DONE


# ---- 改参数 ----


async def test_审批时改参数生效(env: Env) -> None:
    EXECUTED.clear()
    await env.setup(approvers=["U_APPROVER"])
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    EXECUTED.clear()

    await env.router.route(
        mention('/approve action=create_pr title="人改过的标题"', user="U_APPROVER")
    )

    assert EXECUTED == ["github(create_pr,人改过的标题)"], f"参数没被覆盖: {EXECUTED}"


# ---- 不受影响的路径 ----


async def test_未配审批时照常执行(env: Env) -> None:
    """向后兼容。"""
    EXECUTED.clear()
    await env.setup(approvers=["U_APPROVER"])
    await env.policies.upsert(
        PermissionPolicy(
            id="pol_1",
            channel_instance_id=CH,
            allowed_tools=["github", "monitoring"],
            approval_required_tools={},
            approver_ids=[],
        )
    )

    decision = await env.router.route(mention("提个 PR", user="U_REQUESTER"))

    assert EXECUTED == ["monitoring(alerts)", "github(create_pr,修登录超时)"]
    task = await _current_task(env)
    assert task.status is TaskStatus.DONE
    assert decision.message == "都处理完了"


async def test_线程无待批时approve给提示(env: Env) -> None:
    await env.setup(approvers=["U_APPROVER"])

    decision = await env.router.route(mention("/approve", user="U_APPROVER"))

    assert "没有等待审批" in decision.message


async def test_owner优先于频道名单(env: Env) -> None:
    """任务有负责人时由他批准，频道名单里的人反而无权。"""
    EXECUTED.clear()
    await env.setup(approvers=["U_CHANNEL"])
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    task = await _current_task(env)
    task.owner_id = "U_OWNER"
    async with env.uow:
        await env.tasks.update(task)
    EXECUTED.clear()

    # 频道名单里的人现在无权
    denied = await env.router.route(mention("/approve", user="U_CHANNEL"))
    assert "不在这个任务的审批人名单里" in denied.message
    assert EXECUTED == []

    # 负责人可以
    await env.router.route(mention("/approve", user="U_OWNER"))
    assert EXECUTED == ["github(create_pr,修登录超时)"]


async def test_频道默认负责人自动填进任务并有权批准(env: Env) -> None:
    """default_owner_id 在建任务时自动填进 Task.owner_id（SPEC §4.6）。

    无需审批人名单：负责人是第一级来源，权限只归他一人（四眼：发起人除外）。
    """
    EXECUTED.clear()
    await env.setup(approvers=["U_CHANNEL"])  # 频道名单里有另一个人
    env.instance.default_owner_id = "U_OWNER"
    await env.router.route(mention("提个 PR", user="U_REQUESTER"))
    task = await _current_task(env)
    assert task.owner_id == "U_OWNER", "建任务时该自动填上默认负责人"

    EXECUTED.clear()
    # 频道名单里的人；有负责人时以负责人为准，无权
    denied = await env.router.route(mention("/approve", user="U_CHANNEL"))
    assert "不在这个任务的审批人名单里" in denied.message
    assert EXECUTED == []
    # 负责人可批
    await env.router.route(mention("/approve", user="U_OWNER"))
    assert EXECUTED == ["github(create_pr,修登录超时)"]
