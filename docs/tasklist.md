# TeamAI 需求实施计划

- [x] 1. 初始化项目结构与基础设施
   - 创建 src/teamai/ 分层目录结构（domain/application/agent/tools/infrastructure/adapters）
   - 配置 pyproject.toml、Makefile、.env.example、config/config.example.yaml、deploy/docker-compose.yml（postgres + redis + qdrant）、deploy/Dockerfile
   - 实现 config.py（pydantic-settings：凭据与连接串走 .env，非敏感可调项走 config/config.yaml 并展平嵌套；优先级 环境变量 > .env > yaml > 默认值）
   - 实现 domain/identity.py（gen_id：`<前缀>_<ULID>`，标准库自实现，字典序即生成时间序）
   - 搭建 pytest 框架与 tests/conftest.py
   - 参考 Code-Design-Python.md §1、§2

- [ ]* 1.1 编写配置加载的单元测试（事件去重已由 tests/unit/test_dedup.py 覆盖）

- [x] 2. 实现领域模型层
   - 实现 domain/models/task.py：TaskStatus 枚举 + 合法迁移表 + Task dataclass（含 transition 校验）
   - 实现 domain/models/channel.py、domain/models/memory.py、domain/models/tag.py
   - 实现 domain/models/policy.py、domain/models/budget.py、domain/models/audit.py
   - 参考 Code-Design-Python.md §4.1 与 Design-claude-tag.md §4 数据模型

- [ ]* 2.1 编写 Task 状态机迁移的属性测试（任意非法迁移必抛异常，对应正确性属性"任务终态确定性"）
- [ ]* 2.2 编写各领域模型序列化/校验单元测试

- [x] 3. 检查点 - 领域模型层测试通过

- [x] 4. 实现基础设施层（DB/仓储/队列/向量/审计）
   - 实现 infrastructure/db.py（SQLAlchemy async engine/session）
   - 实现 infrastructure/repositories/：TaskRepository、MemoryRepository、TagRepository、PolicyRepository、BudgetRepository、AuditRepository 的 SQL 实现，按聚合一文件（接口见 domain/repositories/ §4.5）
   - 实现 infrastructure/queue.py（RedisTaskQueue：长任务入队/消费/结果回写）
   - 实现 infrastructure/vector.py（Qdrant/Chroma 适配器：embedding 写入与语义检索）
   - 实现 infrastructure/scheduler.py（APScheduler：cron 定时任务创建与取消）
   - 实现 domain/services/audit_writer.py（仅追加审计写入，领域服务）
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
   - 实现 application/tag.py：TagResolver（标签解析与激活）
   - 实现 application/budget.py：BudgetController（配额核算、上限触发 PAUSED 与通知）
   - 实现 application/memory.py：MemoryService（频道记忆存储/检索、偏好管理、写入侧去重与取代）。**「跨频道授权检查」未实现** —— `ChannelInstance.cross_channel_learning` 有列、有 Admin 端点能改、serializer 也吐给前端，但除存取之外从未被读作任何条件；`query_for_context` 硬绑 `channel_instance_id`，`_semantic_hits` 拿它当向量 namespace。与 `TagTemplate.shared`（见 9. 标签）是同一类死字段。要做须先给 `ChannelInstance` 补来源可见性字段：`Design-claude-tag.md §3.8` 的「目标频道为公共频道」条件当前**不可实现**，库里没有这个信息，而 Slack 的 private_channel 照常建实例、照常蒸馏入库
   - 参考 Design-claude-tag.md §3.2-§3.11

- [ ]* 8.1 编写编排层集成测试（Slack 模拟事件 → 路由 → 编排 → 回复全链路）

- [x] 9. 检查点 - 核心编排链路测试通过

- [x] 10. 实现 Slack 适配层与 Admin API
   - 实现 adapters/slack.py：slack-bolt AsyncApp 装配（app_mention、message 事件、Socket Mode），入口按信封 event_id 去重
   - 实现 domain/ports/dedup.py 与 infrastructure/dedup.py：EventDeduplicator 端口 + Redis `SET NX EX` 实现（内存兜底）
   - 实现 infrastructure/redis_client.py：RedisClientProvider（进程内共享连接池，queue 与 dedup 共用，退出时由 container.aclose() 关闭）
   - 实现 adapters/admin/：FastAPI 路由，按资源分模块（记忆管理、预算配置、权限策略、审计查询、任务查询、标签管理）
   - 实现 app/backend/main.py：装配所有依赖，启动 Admin API + Slack 事件入口（Scheduler 在 worker 进程）
   - 参考 Code-Design-Python.md §4.6 与 Design-claude-tag.md §3.1、§3.13

- [ ]* 10.1 编写 Admin API 路由契约测试

- [~] 11. 实现 Ambient Mode 主动介入（规则引擎与开关已完成，三类触发器只落地一条）
   - 规则引擎骨架已完成（`application/ambient.py`），按 `trigger` 注册处理器
   - **thread_stale 已实现**（沉寂线程提醒，含冷却）。**异常事件监测、CI/部署状态通知未实现** —— 这两类要先有事件来源（监控 webhook、CI 回调），不只是加个 handler
   - 频道级开关与规则配置已完成：两级开关（`ChannelInstance.ambient_enabled` 总闸 + `PermissionPolicy.ambient_rules` 逐条规则），控制台的概览页与策略页都可改
   - 参考 Design-claude-tag.md §3.10 与 PRD §4.3

- [x] 11.1 编写 Ambient 规则触发/误报的合成事件测试（`tests/unit/test_ambient.py`：两级开关、阈值、冷却、失败隔离；重点验「不该打扰时确实不打扰」）

- [x] 12. 实现对话标签模板复用
   - 标签 CRUD：创建/激活/删除已完成（`adapters/admin/tag.py` + 控制台标签页）。**「共享」未实现** —— `TagTemplate.shared` 默认 True，除存取之外从未被读作任何条件，也没有端点能改它；要做跨频道共享须先定清语义（谁可见、按什么隔离）
   - 标签激活时按预设指令/角色/风格执行：三项都已接进 `build_system_prompt`。`output_style` 此前只存不用（控制台上可填、API 能读回，但对模型完全没有效果），现已接上，并由 `tests/unit/test_prompts.py` 的字段反查用例锁死
   - 参考 PRD §4.7

- [ ]* 12.1 编写标签模板解析与激活流程测试

- [ ] 13. 端到端评测与安全验证
   - 构建频道级评测集（代码审查、Bug 修复、数据汇总、文档生成 golden 数据）
   - 运行 Agent 循环，输出完成率/正确率/返工率
   - 频道隔离安全测试（ch_A 无法读取 ch_B 记忆与上下文）
   - 审计完整性自动化断言（每个动作均有可追溯 AuditLog）
   - 参考 Design-claude-tag.md §7.3、§7.4

- [ ]* 13.1 编写 E2E 评测集加载与指标统计工具

- [x] 14. 多平台接入（参考 Design-multi-platform.md）
   - 平台无关化：domain/ports/messaging.py（MessagePublisher/ReplyTarget）、application/events.py（IncomingMessage）、ChannelRepository.get_by_platform_channel 补 platform 条件、Task/ORM 的 thread_ts→thread_ref 与 ID 列加宽、channel_instances 复合唯一约束
   - adapters/slack 拆子包（app.py + translator.py），dedup 键加 slack: 前缀
   - infrastructure/messaging/：SlackPublisher + FeishuPublisher + PublisherRegistry（worker 回帖 TODO 落地）
   - adapters/base.py PlatformConnector + 进程入口遍历连接器 + platforms.slack.mode
   - adapters/feishu/：crypto（AES+验签）、translator（content JSON/mentions/p2p）、callback（FastAPI 路由）、ws（独立线程+跨 loop 桥接）、connector（模式选择 + bot open_id 拉取）
   - 引入 alembic（async 模板，baseline 迁移与 create_all 无漂移），Makefile migrate 目标，镜像启动先跑迁移
   - 测试：crypto/translator/callback/ws 单测、test_app 矩阵（两平台 × 两模式 × 凭据）、layering 平台 SDK 锁定、worker 双平台回帖

- [x] 15. 管理控制台前端（`web/`，Vite + React + TypeScript + Ant Design）
   - Admin API 补齐控制台所需端点：`GET /api/channels`（此前无从列举频道，只能手输 id）、`GET/PATCH /api/channels/{id}`（频道级开关）、`GET /api/tools`（工具名的唯一来源，避免前端硬编码随 `build_tools()` 漂移）、标签的 PATCH/DELETE
   - 可选令牌鉴权（`adapters/admin/auth.py`）：配了 `ADMIN_API_TOKEN` 则资源路由要求 Bearer，`/health` 有意不保护以便探针匿名可打；比较走 `secrets.compare_digest` 防计时侧信道
   - CORS 中间件按 `admin_api.cors_origins` 启用，独立部署时必需（不配则浏览器拦掉全部 /api 请求）
   - 十个页面：频道列表 / 概览 / 任务 / 记忆 / 预算 / 策略 / 标签 / 审计 / 设置 / 404
   - 前后端对齐纪律：`web/src/api/index.ts` 按资源分组对应 `adapters/admin/` 的模块，`types.ts` 与 `serializers.py` 逐字段对应。后端返回 `dict[str, Any]`，OpenAPI 里没有响应形状，故这层只能靠人守
   - 渲染冒烟（`npm run smoke`）：各页面在 Node 里 SSR 一遍。`vite build` 只保证编译过，抓不到缺失导出、渲染期抛错这类「编译得过但白屏」的问题，也顺带暴露 AntD 废弃 prop。**只覆盖首帧** —— SSR 不跑 `useEffect`，取数分支（表格/抽屉/空态）未被覆盖；要覆盖须换 jsdom 挂载 + 打桩 fetch，未做
   - 未做的检查：配色对比度（WCAG 数值核验）、无障碍规则（axe-core）。这两项都得看真实渲染，且完整的 WCAG 判定还需辅助技术实测与专家复核
   - 修 `PUT /budget`：原先每次 `gen_id("bq")` 生成新 id，而 `upsert` 走 `session.merge` 按主键匹配，故是 INSERT 而非 UPDATE；`budget_quotas` 无 channel 唯一约束、`get_for_channel` 又 `.first()` 无排序，导致管理员改完上限读回的仍是旧行，看起来「改了没生效」。改为复用既有 id 原地更新，用量与周期起点保留，新上限高于已用量时把 EXHAUSTED 放回 ACTIVE
   - 新增测试 49 条：`test_admin_auth.py`（22）、`test_channel_service.py`（9）、`test_budget_configure.py`（9，打真 SQL）、`test_prompts.py`（9）

- [x] 17. 会话上下文与记忆存储改造（设计见 `docs/Design-conversation-context.md`）
   - 定性问题：库里八张表不存消息，但代码一直在存 —— router 把每条非 @ 消息截到 500 字塞进 `memory_entries`，一张语义上属于「结论」的表。由此派生五个缺陷，两个是故障级
   - 故障级一：记忆检索退化成随机取样。`list_by_channel` 无 ORDER BY 无 LIMIT，调用方却在 Python 侧切前 5 条，行序由数据库决定，且随频道使用时长线性变慢
   - 故障级二：向量路径是死代码。`MemoryService` 收 `embedder` 但组合根从未注入（只传了 `vector_store`），故 Qdrant 从未被写入、语义检索分支永不进入 —— 上一条的无界扫描是生产唯一路径，而非「降级」路径。顺带暴露 Qdrant 集合维度硬编码 384 与常见模型 1536 不匹配，此前被「embedder 从未注入」掩盖
   - 另三处：`thread_history` 字段与压缩逻辑齐备但生产代码从不赋值（机器人被 @ 时看不见上一句）；`Visibility` 枚举建好却无人判定，单聊内容照样进频道记忆（PRD §4.2 承诺未落地）；`audit_logs` 只记动作枚举，还原不出实际提示词与响应
   - `Visibility` 的后续：判定已由 `router.py` 在进蒸馏窗口**之前**丢弃单聊消息补上（`PRIVATE_CHANNEL_TYPES`），而枚举本身随后**删除** —— 它自始至终没有任何调用方传过非默认值，检索侧也不看它，是个「看起来在做权限控制、实际恒为默认值」的死字段。留着它做跨频道时会写出 `WHERE visibility != 'private'` 这种静默匹配零行的条件并误以为已获得保护。单条记忆的可见性判定改由 `ChannelInstance` 的来源可见性承载：那是客观事实，而非写入时的判断（迁移 `f3b9d27a5c14`）
   - 定论：不做全量镜像。容量不是理由（73 万行/年、150MB，Postgres 毫无压力），真正的理由是所有权与合规（GDPR 删除权无从落实、IM 的逐条消息 ACL 会被镜像拍平）、时效性（编辑与撤回后镜像滞后）、以及它不解决真正的缺口
   - 三层架构：原始消息按需拉平台（`ThreadReader` 端口 + Slack/飞书实现 + Redis 45s 缓存）；`agent_interactions` 表存机器人自己的交互（提示词、响应、实际生效的 `model_id`、分拆 in/out token、`context_refs` 存引用而非快照），按保留期清理；`memory_entries` 回归结论 —— 非 @ 消息进 Redis 滚动窗口，worker 定时蒸馏，原文即弃
   - 飞书线程拉取的结构性差异：没有「按根消息 ID 拉整串」的接口，`container_id_type` 只接受 `chat` 与 `thread`，而 `thread` 要的是话题群的 `omt_` ID，与我们持有的 `om_` 根消息 ID 不是一回事。故按 chat 拉最近一批再按 `root_id` 客户端过滤，代价是多拉后丢弃，换来不依赖话题群这个前提
   - 蒸馏的两个取舍：放 worker 而非 router 内判断窗口是否已满（否则某条恰好触发阈值的普通消息要承担一次 LLM 调用延迟，而发送者根本没 @ 机器人）；配额不足时跳过但**不 drain** 窗口（预算暂停的预期是「任务先不跑」，不该顺带丢掉记忆素材）。蒸馏 token 计入频道配额，否则后台任务会绕过「预算硬上限」这条正确性属性
   - 新增 worker 定时任务两个：记忆蒸馏（挂 sweep 间隔）、交互记录保留期清理（独立的天级间隔）
   - Admin 补三个只读端点：按频道/按任务/按 id 查交互记录。有意不给写入与删除入口 —— 人工写入会污染成本统计，审计类数据的删除应是策略性的
   - 清理脚本 `scripts/cleanup_chat_memories.py`：dry-run 默认，`--apply` 才删。阈值实测校准过 —— 初值 25 会删掉只有 23 字符的「订单服务的超时阈值是 30 秒，由网关侧统一配置」，中文每字符信息量远高于英文，改为 12
   - 触发并按其意图修正两处既有守卫：`test_不含凭据字段` 拦下写进 yaml 的 `embedding.base_url`（连接串应走 .env）；`test_只读方法不提交` 误判 `purge_before`，根因是 `_mutates` 认不出 `session.execute(delete(...))`，补 `_builds_dml` 扫函数体
   - 新增测试 65 条：`test_distiller.py`（17）、`test_conversation.py`（14）、`test_memory.py`（13）、`test_interaction.py`（11，打真 SQL）、`test_window.py`（10）；另扩 router / runtime / worker_jobs 各若干条
   - 真依赖验证：迁移对真 Postgres 跑 upgrade→downgrade→upgrade 往返（含枚举类型清理）；Admin 端点起真 web 进程读回全字段；Redis 窗口验 NX 首写时间不被刷新与 drain 无残留；清理脚本对真库造数据跑两种模式

- [x] 18. 记忆的删除与编辑（设计见 `docs/Design-conversation-context.md` §6.5）
   - 修「删除不清向量索引」：`VectorStore` 加 `delete`，Qdrant 侧按 payload 的 `entry_id` 过滤删除而非反推 point id（不依赖 id 推导，否则改了映射方案漏改一边会静默变 no-op）。删向量失败只告警 —— Postgres 行是权威源且已删，为向量库故障报错会让用户以为没删掉
   - 修「向量写入从未成功过」：`upload_points` 的 points 必须是 `PointStruct`，传 dict 会抛 AttributeError 而调用方把它吞成 warning。此前被「embedder 从未注入、这段代码没被执行」掩盖。另记 SDK 的不一致：`delete` 的 points_selector 只收模型对象，而 `query_points` 的 query_filter 收 dict
   - 修「`embedding_ref` 从未被赋值」：`upsert` 改为返回可回填的引用（映射规则是基础设施层细节，不该让用例层推导），回填后落库。清理脚本里 `embedding_ref IS NULL` 这个条件从此名副其实
   - 新增编辑：`MemoryRepository.update` + `MemoryService.edit` + `PATCH /api/memories/{entry_id}`，可改 content 与 type，保留 id / created_at / visibility。**改内容必须重算向量，且重算失败要删掉旧向量而不是留着** —— 留着比没索引更糟，检索会持续按已改掉的内容命中它。有意不支持改 visibility（private→channel 属权限变更）。type 非法值立即 400，不沿用蒸馏解析的宽容策略
   - 新增 `source` 字段（DISTILLED / MANUAL / EDITED）：`source_user_id` 答「哪个用户的话变成了这条」，对蒸馏与管理台写入都是 NULL，控制台里两者不可区分；而这张表直接影响机器人回答，「谁写的」是出问题时第一个要问的。迁移把已有行回填 DISTILLED 而非 MANUAL —— 改造前那些行确实是系统塞进去的聊天碎片
   - **连带修一个已进主干的缺陷**：`AuditAction` 上次加 `MEMORY_DISTILL` 时没迁移 Postgres 的 `auditaction` 类型，于是记忆蒸馏在任何已升级的库上都失败（新库经 create_all 正常，故本地与 CI 都看不出来）。补迁移，并加 `test_enum_migrations.py` 静态兜住这一类 —— 该守卫本身也验证过（临时加一个未迁移的值确实变红）
   - 前端：`memoryApi.update`、编辑按钮（Modal 改双模式）、「产生方式」与「索引」两列、现有「来源」列改名「来源用户」；补 `memory_distill` / `memory_edit` 两个漏掉的审计动作映射（`satisfies Record<AuditAction, Meta>` 让这个漏项直接编译报错）
   - 新增测试 50 条：`test_memory.py` 扩至 31、`test_memory_repository.py`（10，打真 SQL，含「换 id 会变成 INSERT」这个坑本身）、`test_vector.py`（10，用假 client 断言传给 SDK 的参数形态）、`test_enum_migrations.py`（12）

- [x] 19. 线程历史缓存改为自更新（设计见 `docs/Design-conversation-context.md` §3.1）
   - 修「TTL 窗口内看不见新消息」：原实现把「线程最近 N 条」当快照整体缓存、只靠 TTL 过期重建，于是同一线程 45 秒内的第二轮对话拿到的是旧快照 —— 机器人看不见自己上一轮的回复，「第二个方案细化下」无从理解。而只有被 @ 的消息才拉历史，所以缓存省下的调用只发生在多轮对话时，省配额的动机与最需要新鲜数据的时刻完全重合
   - 新增 `ThreadHistorySink` 端口，`CachedThreadReader` 同时实现读写两端：每条经手的消息 append 进缓存。数据结构从单个 JSON 串改为每线程一个 LIST，追加才能是一次 `RPUSHX`，不必读出整表改完写回（web 与 worker 并发追加会互相覆盖）
   - 三个关键语义：`RPUSHX` 而非 `RPUSH`（键不存在时不建键，否则一条追加就造出「只有一条消息的假历史」并把真实历史挡住）；追加不 EXPIRE（TTL 从「保证新鲜」变成「兜底纠错」，追加中丢的重的乱序的都在下个窗口被平台数据抹平）；键里去掉 limit（否则同一线程有多份缓存，`note()` 无从知道该往哪几个键追加），改为按 `cache_limit` 存、读取时切尾，超容量的请求绕过缓存
   - 回填调用点收在 router 两处：入向在 `route()` 分支之前（非 @ 消息同样在线程里，漏掉会让缓存与平台不一致），出向在新增的 `_respond()`（同步链路与 worker 链路回帖文案的唯一汇聚点）。判据是「这条文案会不会发到平台」—— 含「任务已受理」与失败文案，不含 `_observe` 的返回值（两平台都不发送）
   - 两个有意接受的不完美：回填的是「即将发送」而非「已发送」（发送失败则缓存多一条，下个窗口消失；要真实结果就得散到三处调用点，漏一处即静默不一致）；私密会话仍回填线程缓存（PRD §4.2 管的是「不进记忆」即跨会话留存，不是「不看当前对话」）
   - 新增测试 22 条：`test_conversation.py` 扩至 27（含容量切尾、绕过缓存、无缓存不建键、追加不续期），router 补 5 条回填契约
   - 真依赖验证：`scripts/verify_thread_cache.py` 对真 Redis 核对七组语义，重点是替身测不出的两条 —— RPUSHX 不建键、RPUSHX/LTRIM 不重置 TTL（实测 44s→44s 仍在原窗口倒数）

- [x] 20. 修 `is_self` 错标：别的机器人的消息被当成自己说的（设计见 `docs/Design-conversation-context.md` §3.1）
   - 缺陷：`ThreadMessage.is_bot` 决定 `render()` 输出 `AI:` 还是 `<author_id>:`，但两个平台的判定写的都是「某个机器人」—— Slack 是 `bool(m.get("bot_id"))`、飞书是 `sender_type == "app"`。团队频道里的 CI 通知、告警机器人因此都被渲染成 `AI:`，模型会以为那些话是自己上一轮说的
   - 飞书侧尤其隐蔽：`bot_open_id` 形参一直存在，但没有任何调用方传值（container 里是 `FeishuThreadReader()`），恒为空串，于是精确判定的 `and` 分支永远为假，只剩 `sender_type == "app"` 在起作用。注释里写的「宁可少标注，不可错标」实际没做到
   - 字段 `is_bot` 改名 `is_self`，语义收窄为「本 bot 自己发的」。改名是修复的一部分：名字本身诚实（「某个机器人」），但它被当成「我」来渲染，留着旧名下一个人还会照字面再写错一遍
   - 身份获取：Slack 比对 `auth.test` 的 `bot_id` 与 `user_id`（`chat.postMessage` 发出的消息通常两个字段都有，只比一个会漏），飞书比对 `/open-apis/bot/v3/info` 的 `open_id`。均首次拉取时取一次并缓存，失败不重试（权限不足是稳定失败）。读取器自己取而非由连接器传入 —— worker 进程不起连接器但同样拉线程历史
   - 降级方向是「少标」而非「错标」：身份未知时 `is_self` 一律为假，自己的回复退化成普通参与者。错标会把别人的话认领成自己的，那让模型的自我认知出错。别的机器人不单独标记，按普通参与者渲染
   - 新增 `tests/unit/test_thread_readers.py`（20 条）—— 两个读取器此前完全没有测试，这正是缺陷能活下来的原因。验证过守卫有效：临时改回旧判定，Slack 侧 3 条、飞书侧 2 条立刻变红

- [ ]* 17.1 补 `error_spike` ambient 规则（现在有数据源了，但需给滚动窗口加只读不清空的取数方法 —— 它要的是按关键词聚合计数，不是蒸馏成记忆）
- [ ]* 17.2 上下文摘要化（`compact()` 目前是「丢弃最旧 + 说明丢了几条」，真摘要要多一次 LLM 调用；配置项与形参已预留）
- [ ]* 17.3 实测 Slack `conversations.replies` 的速率配额，据此定 `conversation_cache_ttl_seconds` 终值

- [ ] 16. 检查点 - 全量测试通过并汇报整体实施结果
