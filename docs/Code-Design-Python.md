# TeamAI Python 代码设计文档

Feature Name: claude-tag-collaboration
Updated: 2026-08-06
Status: Draft v1.0

## 1. 技术栈选型

| 领域 | 选型 | 理由 |
|------|------|------|
| 语言 | Python 3.11+ | asyncio 原生异步，AI Agent 生态成熟 |
| Web 框架 | FastAPI | Admin API 与健康检查，异步优先 |
| Slack 集成 | slack-bolt (AsyncApp) | 官方 SDK，事件驱动 + Socket Mode |
| LLM 客户端 | pydantic-ai v2（内置 AnthropicModel + UsageLimits + Agent.tool） | 原生 async、工具调用、预算管控、结构化输出开箱即用 |
| 数据库 | PostgreSQL + SQLAlchemy 2.0 (async) | 任务/实例/审计持久化 |
| 向量库 | Qdrant（本地开发用 Chroma） | 频道记忆语义检索 |
| 任务队列 | Redis + ARQ | 异步长任务与定时任务 |
| 调度 | APScheduler（cron 语义） | 定时/周期任务 |
| 配置 | pydantic-settings | 类型安全的环境配置 |
| 测试 | pytest + pytest-asyncio | 单元/集成/评测 |
| 包管理 | poetry 或 uv | 依赖锁定 |

## 2. 项目目录结构

```
├── pyproject.toml
├── .env.example
├── docker-compose.yml            # postgres + redis + qdrant
├── docs/                         # PRD / 设计 / 实施文档
├── app/                          # 进程入口（一个子包一个进程，只做装配与启动）
│   ├── backend/main.py           # web 进程：create_app() ASGI 装配 + uvicorn 启动
│   └── worker/main.py            # worker 进程：队列消费循环 + Scheduler 生命周期
├── src/
│   └── teamai/
│       ├── container.py          # 组合根：装配全部依赖 + 进程内共享单例
│       ├── config.py             # Settings (pydantic-settings)
│       ├── domain/               # 领域模型与抽象契约（无外部依赖）
│       │   ├── identity.py      # gen_id：ULID 实体 ID（各层共用，按时间可排序）
│       │   ├── models/           # 领域模型（子包 __init__ 汇总导出）
│       │   │   ├── channel.py    # ChannelInstance
│       │   │   ├── task.py       # Task + TaskStatus 状态机
│       │   │   ├── memory.py     # MemoryEntry / Preference
│       │   │   ├── tag.py        # TagTemplate
│       │   │   ├── policy.py     # PermissionPolicy / AmbientRule
│       │   │   ├── budget.py     # BudgetQuota
│       │   │   └── audit.py      # AuditLog
│       │   ├── repositories/     # 仓储抽象接口（依赖倒置，一聚合一文件）
│       │   │   ├── task.py       # TaskRepository
│       │   │   ├── memory.py     # MemoryRepository
│       │   │   ├── tag.py        # TagRepository
│       │   │   ├── policy.py     # PolicyRepository
│       │   │   ├── budget.py     # BudgetRepository
│       │   │   ├── channel.py    # ChannelRepository
│       │   │   └── audit.py      # AuditRepository
│       │   ├── ports/            # 外部系统端口
│       │   │   └── queue.py      # TaskQueue / QueuePayload
│       │   └── services/         # 领域服务
│       │       └── audit_writer.py  # AuditLogWriter（application/agent 共用）
│       ├── application/          # 用例层（编排逻辑）
│       │   ├── router.py         # MessageRouter（唯一协调者，组合下列用例）
│       │   ├── orchestrator.py   # TaskOrchestrator
│       │   ├── intent.py         # IntentClassifier + Intent（含意图→模型档位）
│       │   ├── channel.py        # ChannelService
│       │   ├── memory.py         # MemoryService
│       │   ├── tag.py            # TagResolver
│       │   └── budget.py         # BudgetController
│       ├── agent/                # Agent 执行层
│       │   ├── runtime.py        # AgentRuntime（核心循环）
│       │   ├── context.py        # ContextBundle 组装
│       │   ├── models.py         # pydantic-ai Agent/模型装配与模型分级
│       │   └── prompts.py        # 系统提示词模板
│       ├── tools/                # 工具层
│       │   ├── base.py           # BaseTool 抽象
│       │   ├── registry.py       # ToolRegistry
│       │   ├── github_tool.py    # GitHubConnector
│       │   ├── monitoring_tool.py
│       │   └── crm_tool.py
│       ├── infrastructure/       # 基础设施层（只放实现，抽象在 domain）
│       │   ├── db.py             # SQLAlchemy async engine/session + Base
│       │   ├── orm/              # 表定义按聚合分模块
│       │   │   ├── task.py       # TaskModel
│       │   │   ├── channel.py    # ChannelInstanceModel
│       │   │   ├── memory.py     # MemoryEntryModel + PreferenceModel
│       │   │   ├── tag.py        # TagTemplateModel
│       │   │   ├── policy.py     # PolicyModel
│       │   │   ├── budget.py     # BudgetQuotaModel
│       │   │   └── audit.py      # AuditLogModel
│       │   ├── repositories/     # 仓储实现，与 domain/repositories/ 一一对应
│       │   │   ├── task.py       # SQLTaskRepository + 领域↔表 mapper
│       │   │   ├── memory.py     # SQLMemoryRepository
│       │   │   ├── tag.py        # SQLTagRepository
│       │   │   ├── policy.py     # SQLPolicyRepository（JSON 字段转换）
│       │   │   ├── budget.py     # SQLBudgetRepository
│       │   │   ├── channel.py    # SQLChannelRepository
│       │   │   └── audit.py      # SQLAuditRepository（JSON 字段转换）
│       │   ├── queue.py          # RedisTaskQueue（TaskQueue 实现）
│       │   ├── vector.py         # Qdrant/Chroma 适配器
│       │   └── scheduler.py      # APScheduler 封装
│       ├── adapters/             # 平台适配层
│       │   ├── slack.py         # slack-bolt AsyncApp 装配 + 两种接入方式
│       │   └── admin/           # Admin API，按资源分模块
│       │       ├── __init__.py  # build_admin_router：挂 /api 前缀并逐个 include
│       │       ├── serializers.py # 领域对象 → JSON
│       │       ├── memory.py    # /channels/{id}/memories、/memories/{id}
│       │       ├── budget.py    # /channels/{id}/budget
│       │       ├── policy.py    # /channels/{id}/policy
│       │       ├── audit.py     # /channels/{id}/audit
│       │       ├── task.py      # /channels/{id}/tasks
│       │       └── tag.py       # /channels/{id}/tags
└── tests/
    ├── conftest.py
    ├── unit/                     # 状态机/预算/鉴权单元测试
    ├── integration/              # Slack 模拟全链路测试
    └── e2e/                      # 频道评测集
```

## 3. 分层依赖规则

```
app/（进程入口）→ adapters ─┬→ application → agent → tools
                            │        ↓          ↓       ↓
                            └→ container      domain ←──┘
                                     ↓            ↑
                               infrastructure ─────┘
```

依赖只允许向下，`domain` 为叶子层（零内部依赖）。原先另有一个 `util/` 目录承载跨层工具，但它只装了 `gen_id`，且需要一条「任何层都可导入 util」的特例规则；`gen_id` 移入 `domain/identity.py` 后该目录与特例一并撤销 —— domain 本就是所有层的公共底座，跨层词汇放这里无需开例外。

`app/` 在包外且不随 wheel 分发（`hatchling` 只打 `src/teamai`），因此方向严格单向：`app.*` 可 import `teamai.*`，`teamai.*` 不得 import `app.*`，否则安装态直接 ImportError。此约束由 `tests/unit/test_layering.py::test_src不依赖进程入口` 校验。

- `domain` 无外部依赖，内部按类型分四个子包：领域模型（`models/`）+ 仓储接口（`repositories/`）+ 外部端口（`ports/`）+ 领域服务（`services/`）。各子包 `__init__.py` 汇总导出，跨层调用方写 `from teamai.domain.models import Task`；`domain` 内部互相引用走具体子模块（如 `teamai.domain.models.task`）以免绕回包级 `__init__`
- `application` 依赖 domain / agent / tools，**不依赖 infrastructure**——持久化与队列均通过 domain 声明的抽象访问
- `application` 平铺不分子包：整层同质（六个用例服务 + 一个协调者 `router.py`），没有 domain 那样的类型轴可分，534 行也撑不起嵌套。文件名不带 `_service` 后缀（这一层全是 service，后缀是噪声）、用单数，聚合型的与下层同名：`domain/models/tag.py` ↔ `infrastructure/repositories/tag.py` ↔ `application/tag.py`
- `agent` 依赖 domain + tools，不依赖 application（避免与 `application/router.py` 形成环）
- `infrastructure` 只依赖 domain，实现 domain 声明的抽象（`SQL*Repository`、`RedisTaskQueue`）。内部布局镜像 domain：`orm/` 与 `repositories/` 都按聚合分模块，一个聚合的表、mapper 与仓储实现路径同名，改字段时三边位置可推测
- `infrastructure/orm/__init__.py` **必须导入全部表模块**：SQLAlchemy 只在类定义被执行时才把表注册进 `Base.metadata`，而 `init_db()` 依赖 `Base.metadata.create_all` 建表，漏一个 import 对应的表就会静默不创建。此约束由 `tests/unit/test_orm_registry.py` 静态校验
- `container.py` 是唯一同时 import application 与 infrastructure 的模块（组合根，负责绑定抽象与实现）。同时持有进程内共享单例 `get_container()`：容器内含共享 AsyncSession 与 Redis 连接，重复装配会按调用次数放大连接数
- `adapters` 最外层，接收已装配好的 Container。`admin/` 每个资源模块导出一个 `build_*_router(container)`，子路由自身不带前缀、完整路径写在模块内，`/api` 前缀由 `build_admin_router` 统一挂
- `adapters/admin/__init__.py` **必须 include 全部资源路由**：漏一个该组路由静默不注册，请求时才 404。此约束由 `tests/unit/test_admin_routes.py` 校验（AST 静态检查 + 完整路由表断言）。注意路由表须从 `app.openapi()` 取 —— FastAPI 把 `include_router` 存成惰性对象，遍历 `app.routes` 只能看到 `/api/health`

抽象归属原则：**谁消费、谁定义**。接口放在消费方所在层或其下层，实现放在 infrastructure，从而保证 import 方向与依赖倒置一致。

### 3.1 两个进程入口

| 进程 | 启动 | 职责 |
|------|------|------|
| web | `python -m app.backend.main`，或 `uvicorn app.backend.main:create_app --factory` | Admin API + Slack 事件入口（Socket Mode 或 Events API 二选一，由 `slack_app_token` 是否配置决定） |
| worker | `python -m app.worker.main` | 消费长任务队列 + APScheduler 定时调度 |

拆两个进程的理由：长任务是小时/天级，与 Slack 事件的毫秒级响应放同一进程会互相拖累——长任务卡住事件循环会让 Slack 事件超时重投，而 web 按 QPS 扩容也会把 worker 副本一并放大、导致同一任务被多次执行。

两者共用的启动动作是建表 `init_db_or_warn()`（在 `infrastructure/db.py`）：失败只告警不中断，否则 Postgres 未就绪时 `/api/health` 也探不到，编排系统无从区分「进程没起来」与「依赖没起来」。

## 4. 核心接口设计

### 4.1 domain/models/task.py — 任务状态机

```python
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime

class TaskStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"          # 预算暂停

    # 合法迁移表：state -> 允许的下一状态
    TRANSITIONS = {
        PENDING: {RUNNING, CANCELLED, FAILED},
        RUNNING: {WAITING_INPUT, DONE, FAILED, CANCELLED, PAUSED},
        WAITING_INPUT: {RUNNING, CANCELLED},
        PAUSED: {RUNNING, CANCELLED},
        DONE: set(),
        FAILED: set(),
        CANCELLED: set(),
    }

    def can_transit(self, to: "TaskStatus") -> bool:
        return to in self.TRANSITIONS[self]

@dataclass
class Task:
    id: str
    channel_instance_id: str
    thread_ts: str
    requester_id: str
    intent: str
    tag_name: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    current_stage: str | None = None
    owner_id: str | None = None
    canceled_by: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def transition(self, to: TaskStatus, actor: str) -> None:
        if not self.status.can_transit(to):
            raise InvalidTransition(self.status, to)
        self.status = to
        self.updated_at = datetime.utcnow()
```

### 4.2 agent/runtime.py — 核心 Agent 循环

```python
from pydantic_ai import Agent, RunContext, UsageLimitExceeded, UsageLimits
from pydantic_ai.models.anthropic import AnthropicModel

class AgentRuntime:
    def __init__(self, agent_factory: ModelRegistry, tools: ToolRegistry,
                 memory: MemoryService, budget: BudgetController,
                 audit: AuditLogWriter):
        self._registry = agent_factory      # 模型分级注册表
        self._tools = tools
        self._memory = memory
        self._budget = budget
        self._audit = audit

    async def run(self, task: Task, context: ContextBundle) -> StageResult:
        # 预算前置检查
        if not await self.budget.check_quota(task.channel_instance_id):
            await self._pause_for_budget(task)
            return StageResult(status="PAUSED")

        agent = self._registry.build(context.model_level)   # 按任务分级选型
        for tool_name in context.allowed_tools:             # 权限白名单注入工具
            agent.tool(self._tools.handler(tool_name))
        agent.system_prompt = context.system_prompt

        limits = UsageLimits(
            total_tokens_limit=self.budget.remaining(task.channel_instance_id),
        )
        try:
            result = await agent.run(
                context.user_prompt,
                deps=context.policy,            # RunContext.deps 传入权限包
                usage_limits=limits,
            )
            await self.budget.consume(task.channel_instance_id,
                                      result.usage.total_tokens)
            return StageResult(status="DONE", output=result.output)
        except UsageLimitExceeded:
            await self._pause_for_budget(task)
            return StageResult(status="PAUSED")
```

### 4.3 agent/models.py — 模型分级注册表（替代 LLMGateway）

```python
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel

class ModelRegistry:
    """按 model_level 返回预装配的 Agent。简单任务轻量模型，复杂任务旗舰模型。"""

    def __init__(self, config: ModelConfig):
        self._agents = {
            "light":  Agent(FallbackModel(
                          AnthropicModel(config.sonnet),   # 主
                          AnthropicModel(config.haiku))),  # 备，自动降级
            "full":   Agent(AnthropicModel(config.opus)), # 复杂任务用旗舰
        }

    def build(self, level: str) -> Agent:
        return self._agents[level]
```

> 注：`result.usage` 提供 `total_tokens`/`cost`/`requests`，替代手写 token 统计；`UsageLimits` 与 Anthropic `task_budget` 共同实现预算硬上限。
```

### 4.4 tools/base.py — 工具抽象

```python
@dataclass
class ToolResult:
    ok: bool
    data: dict
    tokens: int = 0
    error: str | None = None

class BaseTool(ABC):
    name: str                      # 如 "github.pr_create"
    description: str
    input_schema: dict             # JSON Schema

    @abstractmethod
    async def call(self, args: dict, auth_scope: PermissionPolicy) -> ToolResult:
        ...
```

### 4.5 domain/repositories/ — 仓储接口（依赖倒置）

```python
class TaskRepository(ABC):
    @abstractmethod
    async def create(self, task: Task) -> None: ...
    @abstractmethod
    async def update(self, task: Task) -> None: ...
    @abstractmethod
    async def get(self, task_id: str) -> Task | None: ...
    @abstractmethod
    async def list_by_channel(self, channel_instance_id: str,
                              status: TaskStatus | None = None) -> list[Task]: ...

class MemoryRepository(ABC):
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None: ...
    @abstractmethod
    async def query(self, channel_instance_id: str, query: str,
                    top_k: int) -> list[MemoryEntry]: ...
    @abstractmethod
    async def set_preference(self, pref: Preference) -> None: ...
```

### 4.6 adapters/slack.py — Slack 装配

```python
from slack_bolt.async_app import AsyncApp

app = AsyncApp(token=..., signing_secret=...)

@app.event("app_mention")
async def on_mention(event, say, thread_ts, logger):
    internal = adapt_event(event)
    await router.route(internal)      # 分派到用例层

@app.message()
async def on_message(event, say, logger):
    # 非 @ 消息：作为频道上下文/记忆素材（Ambient 模式）
    await router.observe(internal)
```

## 5. 关键数据流

### 5.1 响应式任务（@TeamAI）

```mermaid
sequenceDiagram
    participant U as 频道成员
    participant S as Slack Connector
    participant R as MessageRouter
    participant I as IntentClassifier
    participant T as TagResolver
    participant O as TaskOrchestrator
    participant A as AgentRuntime
    participant M as MemoryService

    U->>S: @TeamAI 发起任务
    S->>R: app_mention 事件
    R->>I: classify(text, context)
    I-->>R: intent
    R->>T: resolveTag(channel, tag)
    T-->>R: tag_template?
    R->>O: createTask(...)
    O->>M: query(channel, topK)
    M-->>O: memoryHits
    O->>A: run(task, context)
    A-->>O: StageResult
    O-->>S: 汇报到子线程
    S-->>U: 结果展示
```

### 5.2 预算暂停链路

```mermaid
sequenceDiagram
    participant A as AgentRuntime
    participant B as BudgetController
    participant O as TaskOrchestrator
    participant S as Slack

    A->>B: consume(channel, tokens)
    B-->>A: quota_exceeded=true
    A->>O: transition(PAUSED)
    O->>S: 通知负责人(预算已达上限)
    S-->>O: 管理员追加预算/审批
    O->>O: transition(RUNNING)
```

## 6. 关键实现决策

| 决策点 | 方案 | 理由 |
|--------|------|------|
| 长任务执行模型 | ARQ worker 异步消费任务队列 | 与请求线程解耦，支持小时/天级任务 |
| 任务状态持久化 | PostgreSQL 事务 + 状态机校验 | 崩溃恢复与审计一致 |
| 记忆检索 | 文本 embedding 入 Qdrant + KV 存偏好 | 语义召回 + 精确偏好 |
| 幂等 | channel+ts+subtype 生成事件幂等键 | 防 Slack 重投/乱序 |
| 模型分级 | ModelRegistry.build(model_level) 预装配不同 Agent | 简单任务轻量模型，复杂任务旗舰模型 |
| 预算硬上限 | pydantic-ai UsageLimits + Anthropic task_budget | 超限抛 UsageLimitExceeded → 任务 PAUSED |
| 上下文压缩 | 最近优先 + 历史摘要化 | 防止长任务上下文溢出 |
| 配置隔离 | 每频道 policy 独立加载进 ContextBundle | 工具鉴权不依赖调用人个人权限 |

## 7. 测试策略映射

| 测试类型 | 覆盖内容 | 对应设计文档章节 |
|----------|----------|------------------|
| 单元测试 | Task 状态机合法迁移、Budget 配额核算、Permission 鉴权矩阵 | §5 正确性属性 |
| 属性测试 | 状态机任意非法迁移必抛异常；预算在任意序列下不超过上限（pydantic-ai UsageLimits 兜底） | §5 正确性属性 |
| 集成测试 | Slack 模拟事件 → 路由 → 编排 → 回复全链路；记忆写入→检索命中 | §7.2 |
| E2E 评测 | 频道评测集（代码审查/数据汇总/文档生成）人工标注 | §7.3 |
| 安全测试 | 频道隔离（ch_A 无法读 ch_B 记忆）；审计完整性断言 | §7.4 |
| 模型降级 | FallbackModel 主模型失败自动切换备模型；重试逻辑由 pydantic-ai 托管 | §3.6 |

## 8. 开发环境依赖（docker-compose）

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: teamai
      POSTGRES_PASSWORD: teamai
      POSTGRES_DB: teamai
  redis:
    image: redis:7
  qdrant:
    image: qdrant/qdrant:latest
```

## 9. 参考

[^1]: (文档) - [PRD-claude-tag.md](./PRD-claude-tag.md) - 产品需求文档
[^2]: (文档) - [Design-claude-tag.md](./Design-claude-tag.md) - 技术设计文档
