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
from teamai.application.conversation import ConversationService
from teamai.application.distiller import MemoryDistiller
from teamai.application.intent import IntentClassifier
from teamai.application.interaction import InteractionService
from teamai.application.memory import MemoryService
from teamai.application.orchestrator import TaskOrchestrator
from teamai.application.router import MessageRouter
from teamai.application.tag import TagResolver
from teamai.config import settings
from teamai.domain.ports import (
    Embedder,
    EventDeduplicator,
    LLMGateway,
    MessagePublisher,
    MessageWindow,
    TaskQueue,
    ThreadReader,
)
from teamai.domain.repositories import (
    AuditRepository,
    BudgetRepository,
    ChannelRepository,
    InteractionRepository,
    MemoryRepository,
    PolicyRepository,
    TagRepository,
    TaskRepository,
)
from teamai.domain.services import AuditLogWriter
from teamai.infrastructure.dedup import build_event_deduplicator
from teamai.infrastructure.llm.embedding import build_embedder
from teamai.infrastructure.llm.gateway import ModelConfig, PydanticAIGateway
from teamai.infrastructure.messaging import (
    CachedThreadReader,
    FeishuPublisher,
    FeishuThreadReader,
    PublisherRegistry,
    SlackPublisher,
    SlackThreadReader,
    ThreadReaderRegistry,
)
from teamai.infrastructure.queue import RedisTaskQueue
from teamai.infrastructure.redis_client import RedisClientProvider
from teamai.infrastructure.repositories import (
    SQLAuditRepository,
    SQLBudgetRepository,
    SQLChannelRepository,
    SQLInteractionRepository,
    SQLMemoryRepository,
    SQLPolicyRepository,
    SQLTagRepository,
    SQLTaskRepository,
)
from teamai.infrastructure.tools.crm_tool import build_crm_tool
from teamai.infrastructure.tools.github_tool import build_github_tool
from teamai.infrastructure.tools.monitoring_tool import build_monitoring_tool
from teamai.infrastructure.tools.registry import ToolRegistry
from teamai.infrastructure.vector import VectorStore, build_vector_store
from teamai.infrastructure.window import build_message_window


@dataclass
class Container:
    task_repo: TaskRepository
    memory_repo: MemoryRepository
    tag_repo: TagRepository
    policy_repo: PolicyRepository
    budget_repo: BudgetRepository
    channel_repo: ChannelRepository
    audit_repo: AuditRepository
    interaction_repo: InteractionRepository
    queue: TaskQueue
    dedup: EventDeduplicator
    # 出向消息：按平台分发，同步与异步链路共用
    publisher: MessagePublisher
    # 入向线程读取：按平台分发，外套 Redis 缓存
    thread_reader: ThreadReader
    # 待蒸馏对话的滚动缓冲（Redis），原文只在这里停留分钟级
    window: MessageWindow
    # 唯一持有的具体基础设施类型，为的是退出时能关掉共享连接池
    redis: RedisClientProvider

    # 这三样是无状态的外部客户端封装，提成一等字段而非藏在服务内部：
    # open_job_scope 需要复用它们（每次巡检重建等于按巡检频率泄漏连接），
    # 而伸手取 memory._vector / runtime._gateway 这类私有属性太脆。
    gateway: LLMGateway
    embedder: Embedder
    vector_store: VectorStore

    audit: AuditLogWriter
    orchestrator: TaskOrchestrator
    budget: BudgetController
    memory: MemoryService
    interactions: InteractionService
    conversation: ConversationService
    distiller: MemoryDistiller
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
        # publisher / thread_reader 按端口类型声明，实际持有的是实现了 aclose 的
        # 注册表；容器是组合根，允许持有该具体类型。
        await self.publisher.aclose()  # type: ignore[attr-defined]
        await self.thread_reader.aclose()  # type: ignore[attr-defined]


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
    interaction_repo = SQLInteractionRepository(session)

    # queue / dedup / window 共用一个 client，全进程一个连接池
    redis = RedisClientProvider()
    queue = RedisTaskQueue(redis)
    dedup = build_event_deduplicator(redis)
    window = build_message_window(redis)

    # 出向消息按平台注册。凭据齐备才注册对应实现，
    # 未启用的平台在 registry 里缺失，worker 回帖时记 warning 丢弃。
    publisher = PublisherRegistry()
    readers = ThreadReaderRegistry()
    if settings.slack_enabled:
        publisher.register("slack", SlackPublisher())
        readers.register("slack", SlackThreadReader())
    if settings.feishu_enabled:
        publisher.register("feishu", FeishuPublisher())
        readers.register("feishu", FeishuThreadReader())
    # 缓存统一套在注册表外层，各平台实现里不必各自处理 —— 否则每加一个平台
    # 就要重复一遍缓存逻辑，且容易出现某个平台忘了加。
    thread_reader = CachedThreadReader(
        readers, redis, ttl_seconds=settings.conversation_cache_ttl_seconds
    )

    # embedder 必须真的装上：改造前这里只传了 vector_store，MemoryService 里
    # 的 embedder 恒为 None，于是向量写入与语义检索整条链路从未运行过，
    # 记忆检索一直走无界全表扫描的回落路径。未配置凭据时装的是 NullEmbedder，
    # 它显式报 available=False，缺失是可见的。
    embedder = build_embedder(settings)
    # 集合维度跟随 embedder，不再硬编码 384（那与常见模型的 1536 不匹配）
    vector_store = build_vector_store(embedder.dimensions)
    gateway = PydanticAIGateway(ModelConfig.from_settings(settings))

    audit = AuditLogWriter(audit_repo)
    orchestrator = TaskOrchestrator(task_repo, audit, queue)
    budget = BudgetController(budget_repo, audit)
    memory = MemoryService(
        memory_repo, channel_repo, audit, vector_store=vector_store, embedder=embedder
    )
    interactions = InteractionService(
        interaction_repo, retention_days=settings.interactions_retention_days
    )
    conversation = ConversationService(
        thread_reader, default_limit=settings.conversation_history_limit
    )
    distiller = MemoryDistiller(
        window,
        memory,
        gateway,
        budget,
        window_size=settings.memory_window_size,
        max_idle_seconds=settings.memory_window_idle_seconds,
    )
    tags = TagResolver(tag_repo, audit)
    channels = ChannelService(channel_repo, policy_repo, audit)

    tools = build_tools()
    runtime = AgentRuntime(gateway, tools, budget, audit, settings, interactions)
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
        conversation=conversation,
        distiller=distiller,
    )

    return Container(
        task_repo=task_repo,
        memory_repo=memory_repo,
        tag_repo=tag_repo,
        policy_repo=policy_repo,
        budget_repo=budget_repo,
        channel_repo=channel_repo,
        audit_repo=audit_repo,
        interaction_repo=interaction_repo,
        queue=queue,
        dedup=dedup,
        publisher=publisher,
        thread_reader=thread_reader,
        window=window,
        redis=redis,
        gateway=gateway,
        embedder=embedder,
        vector_store=vector_store,
        audit=audit,
        orchestrator=orchestrator,
        budget=budget,
        memory=memory,
        interactions=interactions,
        conversation=conversation,
        distiller=distiller,
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
    distiller: MemoryDistiller
    interactions: InteractionService


@asynccontextmanager
async def open_job_scope(container: Container) -> AsyncIterator[JobScope]:
    """为一次定时任务运行开一个独立 session，用完即关。

    这里每次都调一次工厂，故每次运行拿到的是新 session —— 与 build_container()
    不同：那边虽然也取的是工厂（get_session_factory 返回 async_sessionmaker），
    但只调用了一次，把同一个实例交给全部仓储。所以 container.budget 与
    container.orchestrator 底下是同一个 session 对象。

    而 AsyncSession 不允许并发使用，定时任务又与消费循环跑在同一个事件循环上。
    共用会撞出「another operation is in progress」/「This session is in
    'prepared' state」/「This transaction is closed」。实测两个同间隔的 job 一起
    触发时，一个 job 写库成功、另一个的每次状态迁移全部失败 —— 而巡检对单条
    任务兜了异常，于是它返回空结果、job 照报成功，故障整段隐形（这也是
    SweepReport 要把失败项带回来的原因）。

    只装 job 真正要用的东西，不整个 build_container()：后者还会新建 Redis
    连接池与各平台 publisher，每次巡检都建一套等于按巡检频率泄漏连接。
    queue / window / gateway / embedder / vector_store 直接借用容器那份 ——
    它们走 Redis 或 HTTP，与 session 无关。
    """
    from teamai.infrastructure.db import get_session_factory

    session = get_session_factory()()
    try:
        audit = AuditLogWriter(SQLAuditRepository(session))
        budget = BudgetController(SQLBudgetRepository(session), audit)
        memory = MemoryService(
            SQLMemoryRepository(session),
            SQLChannelRepository(session),
            audit,
            vector_store=container.vector_store,
            embedder=container.embedder,
        )
        yield JobScope(
            budget=budget,
            orchestrator=TaskOrchestrator(SQLTaskRepository(session), audit, container.queue),
            distiller=MemoryDistiller(
                container.window,
                memory,
                container.gateway,
                budget,
                window_size=settings.memory_window_size,
                max_idle_seconds=settings.memory_window_idle_seconds,
            ),
            interactions=InteractionService(
                SQLInteractionRepository(session),
                retention_days=settings.interactions_retention_days,
            ),
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
