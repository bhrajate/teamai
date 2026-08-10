"""组合根：装配全部依赖，供进程入口（app/）与适配层复用。

`build_container()` 每次调用都新装一套，测试可按需要造独立实例；
`get_container()` 是进程内共享的那一份，由入口脚本与适配层取用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from teamai.application.agent.runtime import AgentRuntime
from teamai.application.budget import BudgetController
from teamai.application.channel import ChannelService
from teamai.application.intent import IntentClassifier
from teamai.application.memory import MemoryService
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.application.tag import TagResolver
from teamai.config import settings
from teamai.domain.ports import EventDeduplicator, MessagePublisher, TaskQueue
from teamai.domain.repositories import (
    AuditRepository,
    BudgetRepository,
    ChannelRepository,
    MemoryRepository,
    PolicyRepository,
    TagRepository,
    TaskRepository,
)
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.dedup import build_event_deduplicator
from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway
from teamai.infrastructure.messaging import FeishuPublisher, PublisherRegistry, SlackPublisher
from teamai.infrastructure.queue import RedisTaskQueue
from teamai.infrastructure.redis_client import RedisClientProvider
from teamai.infrastructure.repositories import (
    SQLAuditRepository,
    SQLBudgetRepository,
    SQLChannelRepository,
    SQLMemoryRepository,
    SQLPolicyRepository,
    SQLTagRepository,
    SQLTaskRepository,
)
from teamai.infrastructure.tools.crm_tool import build_crm_tool
from teamai.infrastructure.tools.github_tool import build_github_tool
from teamai.infrastructure.tools.monitoring_tool import build_monitoring_tool
from teamai.infrastructure.tools.registry import ToolRegistry
from teamai.infrastructure.vector import build_vector_store


@dataclass
class Container:
    task_repo: TaskRepository
    memory_repo: MemoryRepository
    tag_repo: TagRepository
    policy_repo: PolicyRepository
    budget_repo: BudgetRepository
    channel_repo: ChannelRepository
    audit_repo: AuditRepository
    queue: TaskQueue
    dedup: EventDeduplicator
    # 出向消息：按平台分发，同步与异步链路共用
    publisher: MessagePublisher
    # 唯一持有的具体基础设施类型，为的是退出时能关掉共享连接池
    redis: RedisClientProvider

    audit: AuditLogWriter
    orchestrator: TaskOrchestrator
    budget: BudgetController
    memory: MemoryService
    tags: TagResolver
    channels: ChannelService
    runtime: AgentRuntime
    router: MessageRouter
    tools: ToolRegistry

    async def aclose(self) -> None:
        """释放长期存活的外部连接。由进程入口在退出路径调用。

        client 改为复用后连接不再随调用结束而关闭，必须显式收尾，
        否则退出时留下未关闭的 socket 并打出警告。
        """
        await self.redis.aclose()
        # publisher 字段按 MessagePublisher 端口声明，实际持有的是实现了
        # aclose 的 PublisherRegistry；容器是组合根，允许持有该具体类型。
        await self.publisher.aclose()  # type: ignore[attr-defined]


def build_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_github_tool())
    registry.register(build_monitoring_tool())
    registry.register(build_crm_tool())
    return registry


def build_container() -> Container:
    """依赖装配。

    MVP：使用单个共享 AsyncSession 由组合根管理，仓库共享该 session。
    接入 FastAPI 后应替换为 session-per-request 依赖注入。
    """
    from teamai.infrastructure.db import get_session_factory

    factory = get_session_factory()
    session = factory()

    task_repo = SQLTaskRepository(session)
    memory_repo = SQLMemoryRepository(session)
    tag_repo = SQLTagRepository(session)
    policy_repo = SQLPolicyRepository(session)
    budget_repo = SQLBudgetRepository(session)
    channel_repo = SQLChannelRepository(session)
    audit_repo = SQLAuditRepository(session)

    # queue 与 dedup 共用一个 client，全进程一个连接池
    redis = RedisClientProvider()
    queue = RedisTaskQueue(redis)
    dedup = build_event_deduplicator(redis)

    # 出向消息按平台注册。凭据齐备才注册对应 publisher，
    # 未启用的平台在 registry 里缺失，worker 回帖时记 warning 丢弃。
    publisher = PublisherRegistry()
    if settings.slack_enabled:
        publisher.register("slack", SlackPublisher())
    if settings.feishu_enabled:
        publisher.register("feishu", FeishuPublisher())

    audit = AuditLogWriter(audit_repo)
    orchestrator = TaskOrchestrator(task_repo, audit, queue)
    budget = BudgetController(budget_repo, audit)
    memory = MemoryService(memory_repo, channel_repo, audit, vector_store=build_vector_store())
    tags = TagResolver(tag_repo, audit)
    channels = ChannelService(channel_repo, policy_repo)

    tools = build_tools()
    gateway = PydanticAIGateway(ModelConfig.from_settings(settings))
    runtime = AgentRuntime(gateway, tools, budget, audit, settings)
    intent = IntentClassifier()
    router = MessageRouter(
        orchestrator=orchestrator,
        intent=intent,
        tags=tags,
        memory=memory,
        budget=budget,
        runtime=runtime,
        channels=channels,
        policy_repo=policy_repo,
    )

    return Container(
        task_repo=task_repo,
        memory_repo=memory_repo,
        tag_repo=tag_repo,
        policy_repo=policy_repo,
        budget_repo=budget_repo,
        channel_repo=channel_repo,
        audit_repo=audit_repo,
        queue=queue,
        dedup=dedup,
        publisher=publisher,
        redis=redis,
        audit=audit,
        orchestrator=orchestrator,
        budget=budget,
        memory=memory,
        tags=tags,
        channels=channels,
        runtime=runtime,
        router=router,
        tools=tools,
    )


@dataclass
class JobScope:
    """定时任务用的窄依赖集合，建在一个独立 session 上。"""

    budget: BudgetController
    orchestrator: TaskOrchestrator


@asynccontextmanager
async def open_job_scope(container: Container) -> AsyncIterator[JobScope]:
    """为一次定时任务运行开一个独立 session，用完即关。

    不复用 Container 那个共享 session：AsyncSession 不允许并发使用，而定时任务
    与消费循环跑在同一个事件循环上。共用会撞出
    「This session is provisioning a new connection; concurrent operations are
    not permitted」或「This transaction is closed」——实测两个同间隔的 job 一起
    触发时，写库成功了但紧随其后的审计写入失败，留下没有留痕的状态变更。

    只装 job 真正要用的两样东西，不整个 build_container()：后者还会新建 Redis
    连接池与各平台 publisher，每次巡检都建一套等于按巡检频率泄漏连接。queue
    直接借用容器那个 —— 它走 Redis，与 session 无关。
    """
    from teamai.infrastructure.db import get_session_factory

    session = get_session_factory()()
    try:
        audit = AuditLogWriter(SQLAuditRepository(session))
        yield JobScope(
            budget=BudgetController(SQLBudgetRepository(session), audit),
            orchestrator=TaskOrchestrator(SQLTaskRepository(session), audit, container.queue),
        )
    finally:
        await session.close()


_container: Container | None = None


def get_container() -> Container:
    """进程内共享的组合根。首次调用时装配。

    单例而非每次新建：容器持有共享 AsyncSession 与 Redis 连接，
    重复装配会按调用次数放大连接数。
    """
    global _container
    if _container is None:
        _container = build_container()
    return _container


def reset_container() -> None:
    """丢弃已装配的容器。供测试隔离用。"""
    global _container
    _container = None
