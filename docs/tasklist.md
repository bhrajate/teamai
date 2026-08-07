# TeamAI 需求实施计划

- [x] 1. 初始化项目结构与基础设施
   - 创建 src/teamai/ 分层目录结构（domain/application/agent/tools/infrastructure/adapters）
   - 配置 pyproject.toml、.env.example、docker-compose.yml（postgres + redis + qdrant）
   - 实现 config.py（pydantic-settings：Slack token、Anthropic key、DB/Redis/Qdrant 连接、模型分级映射）
   - 实现 util/events.py（ULID 生成 gen_id、事件幂等键 channel+ts+subtype）
   - 搭建 pytest 框架与 tests/conftest.py
   - 参考 Code-Design-Python.md §1、§2

- [ ]* 1.1 编写配置加载与幂等键的单元测试

- [x] 2. 实现领域模型层
   - 实现 domain/task.py：TaskStatus 枚举 + 合法迁移表 + Task dataclass（含 transition 校验）
   - 实现 domain/channel.py、domain/memory.py、domain/tag.py
   - 实现 domain/policy.py、domain/budget.py、domain/audit.py
   - 参考 Code-Design-Python.md §4.1 与 Design-claude-tag.md §4 数据模型

- [ ]* 2.1 编写 Task 状态机迁移的属性测试（任意非法迁移必抛异常，对应正确性属性"任务终态确定性"）
- [ ]* 2.2 编写各领域模型序列化/校验单元测试

- [x] 3. 检查点 - 领域模型层测试通过

- [x] 4. 实现基础设施层（DB/仓储/队列/向量/审计）
   - 实现 infrastructure/db.py（SQLAlchemy async engine/session）
   - 实现 infrastructure/repositories.py：TaskRepository、MemoryRepository、TagRepository、PolicyRepository、BudgetRepository、AuditRepository 的 SQL 实现（接口见 domain/repositories.py §4.5）
   - 实现 infrastructure/queue.py（RedisTaskQueue：长任务入队/消费/结果回写）
   - 实现 infrastructure/vector.py（Qdrant/Chroma 适配器：embedding 写入与语义检索）
   - 实现 infrastructure/scheduler.py（APScheduler：cron 定时任务创建与取消）
   - 实现 domain/audit_writer.py（仅追加审计写入，领域服务）
   - 参考 Code-Design-Python.md §4.5、§6

- [ ]* 4.1 编写仓储 CRUD 与审计仅追加的集成测试（审计完整性对应正确性属性）
- [ ]* 4.2 编写向量检索写入→召回命中率测试

- [x] 5. 实现模型注册表与 pydantic-ai Agent 装配
   - 实现 agent/models.py：ModelRegistry（按 model_level 预装配 Agent，轻量任务用 FallbackModel(sonnet→haiku) 自动降级，复杂任务用 opus）
   - 实现 agent/prompts.py：系统提示词模板（频道身份、记忆引用规范、工具调用约束）
   - 配置 UsageLimits（total_tokens_limit 从 BudgetController 读取）与 Anthropic task_budget
   - 参考 Code-Design-Python.md §4.3、§6

- [ ]* 5.1 编写模型注册表分级与 FallbackModel 降级的 mock 测试

- [x] 6. 实现工具层
   - 实现 tools/base.py：BaseTool 抽象（name/description/input_schema/call）
   - 实现 tools/registry.py：注册、按权限白名单校验、调用分发
   - 实现 tools/github_tool.py：GitHubConnector（PR 创建、代码读取、issue 操作）
   - 实现 tools/monitoring_tool.py 与 tools/crm_tool.py（可 mock 实现）
   - 参考 Code-Design-Python.md §4.4 与 Design-claude-tag.md §3.7

- [ ]* 6.1 编写工具鉴权矩阵测试（工具 x 频道 x 策略，对应正确性属性"工具调用鉴权"）

- [x] 7. 实现 Agent 运行时
   - 实现 agent/context.py：ContextBundle 组装（记忆检索结果+线程历史+工具 Schema+策略白名单+模型级别）
   - 实现 agent/runtime.py：核心 Agent 循环（预算前置检查、LLM 调用、工具鉴权拒绝注入、预算消耗、结果合成）
   - 实现上下文压缩策略（最近优先 + 历史摘要化，防溢出）
   - 参考 Code-Design-Python.md §4.2、§6

- [ ]* 7.1 编写预算硬上限属性测试（任意调用序列下 token 不超过配额，对应正确性属性"预算硬上限"；验证 UsageLimitExceeded → PAUSED 路径）

- [x] 8. 实现用例层（编排逻辑）
   - 实现 application/router.py：MessageRouter（事件分派：@提及走意图分类，普通消息走 observe）
   - 实现 application/intent.py：IntentClassifier（LLM 零样本分类 + 关键词规则兜底）
   - 实现 application/orchestrator.py：TaskOrchestrator（创建、阶段推进、WAITING_INPUT、取消、超时提醒与自动取消、长任务入队）
   - 实现 application/tags.py：TagResolver（标签解析与激活）
   - 实现 application/budget.py：BudgetController（配额核算、上限触发 PAUSED 与通知）
   - 实现 application/memory_service.py：MemoryService（频道记忆存储/检索、偏好管理、跨频道授权检查）
   - 参考 Design-claude-tag.md §3.2-§3.11

- [ ]* 8.1 编写编排层集成测试（Slack 模拟事件 → 路由 → 编排 → 回复全链路）

- [x] 9. 检查点 - 核心编排链路测试通过

- [x] 10. 实现 Slack 适配层与 Admin API
   - 实现 adapters/slack_app.py：slack-bolt AsyncApp 装配（app_mention、message 事件、Socket Mode）
   - 实现 adapters/admin_api.py：FastAPI 路由（频道实例管理、记忆管理、预算配置、权限策略、审计查询、标签管理）
   - 实现 main.py：装配所有依赖，启动 Slack app + Admin API + Scheduler
   - 参考 Code-Design-Python.md §4.6 与 Design-claude-tag.md §3.1、§3.13

- [ ]* 10.1 编写 Admin API 路由契约测试

- [ ] 11. 实现 Ambient Mode 主动介入
   - 实现 ambient 规则引擎（thread_stale 沉寂线程提醒、异常事件监测、CI/部署状态通知）
   - 实现频道级开关与规则配置（接入 PermissionService 的 ambientRules）
   - 参考 Design-claude-tag.md §3.10 与 PRD §4.3

- [ ]* 11.1 编写 Ambient 规则触发/误报的合成事件测试

- [ ] 12. 实现对话标签模板复用
   - 实现标签 CRUD（创建/激活/删除/共享）
   - 实现标签激活时按预设指令/角色/风格执行（接入 TagResolver 与 AgentRuntime）
   - 参考 PRD §4.7

- [ ]* 12.1 编写标签模板解析与激活流程测试

- [ ] 13. 端到端评测与安全验证
   - 构建频道级评测集（代码审查、Bug 修复、数据汇总、文档生成 golden 数据）
   - 运行 Agent 循环，输出完成率/正确率/返工率
   - 频道隔离安全测试（ch_A 无法读取 ch_B 记忆与上下文）
   - 审计完整性自动化断言（每个动作均有可追溯 AuditLog）
   - 参考 Design-claude-tag.md §7.3、§7.4

- [ ]* 13.1 编写 E2E 评测集加载与指标统计工具

- [ ] 14. 检查点 - 全量测试通过并汇报整体实施结果
