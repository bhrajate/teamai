# 会话上下文与记忆的存储设计

本文回答一个问题：**IM 里的聊天记录该不该存进关系库，该存什么、存在哪里。**

结论先行：不做全量镜像。原始消息以平台为唯一权威源、按需拉取并短时缓存；关系库只存两样东西 —— 机器人自己的交互记录（审计与成本核算的地基）和从对话中蒸馏出的结论（记忆）。三者的保留期、粒度与介质各不相同，把它们混成一张表是当前实现最主要的缺陷。

## 1. 现状与缺陷

改造前，库里八张表没有任何一张存消息（`ambient.py` 的模块注释已明确记下这点）。但**代码实际上一直在存聊天记录**，只是存进了 `memory_entries` —— 一张语义上属于「结论」的表。

`application/router.py` 的 observe 分支对每条非 @ 消息无条件落库：

```python
if not msg.is_mention:
    if msg.text.strip() and not msg.text.startswith("/"):
        await self._memory.store(instance.id, msg.text[:500], source_user_id=msg.user_id)
```

唯一的过滤是「非空且不以 `/` 开头」。于是「收到」「好的」「哈哈」与真正的项目背景知识并列写入，`type` 一律 `BACKGROUND_KNOWLEDGE`。由此派生出五个缺陷，其中两个是线上故障级别的。

### 1.1 记忆检索退化成随机取样（故障级）

`application/memory.py` 的 `query_for_context` 在向量检索无结果时回落到 `list_by_channel`，而该实现没有 `ORDER BY`、没有 `LIMIT`：

```python
stmt = select(MemoryEntryModel).where(MemoryEntryModel.channel_instance_id == channel_instance_id)
```

把该频道的全部历史读进进程内存，再在 Python 侧 `hits[:top_k]` 切前 5 条。行序由 Postgres 自行决定，等于随机拿 5 条聊天碎片喂给模型。

### 1.2 向量路径是死代码，所以 1.1 是必经之路而非降级路径（故障级）

`MemoryService.__init__` 收 `embedder` 参数，但 `container.build_container()` 只传了 `vector_store`：

```python
memory = MemoryService(memory_repo, channel_repo, audit, vector_store=build_vector_store())
```

`self._embedder` 恒为 `None`，于是 `_embed_if_available` 每次直接 return，Qdrant 从未被写入；`query_for_context` 的向量分支也永不进入。**语义检索整条链路从未运行过**，1.1 的无界全表扫描是生产环境唯一的检索路径。

### 1.3 机器人看不见对话上下文

`ContextBundle.thread_history` 字段存在，`compact()` 的压缩逻辑也写好了，`runtime._compose_prompt` 还会把它拼进提示词 —— 但生产代码从不给它赋值，全仓库只有 `tests/unit/test_agent_runtime.py` 在填。`MessagePublisher` 端口只有 `reply`，没有任何读取能力。被 @ 时机器人不知道上一句在说什么。

### 1.4 PRD 的隔离承诺没有落地

PRD §4.2 写明「私密频道与私信内容默认不进入记忆」，`Visibility` 枚举也建好了，但 `MemoryService.store()` 硬编码走默认值 `CHANNEL`，router 里没有任何 channel_type 判定。单聊（Slack `im` / 飞书 `p2p`）内容照样进频道记忆。

### 1.5 审计记不下「模型看到了什么、说了什么」

`audit_logs` 记的是动作（九个 `AuditAction` 枚举）加一个 `detail` 字典，不存实际组装出的提示词、不存模型返回的正文、不区分输入/输出 token。任务回答错了、越权调了工具、token 烧超了，都无法复现当时的输入。这个缺口比「没有聊天记录」严重得多，而它恰恰不是靠镜像聊天记录能补上的。

## 2. 为什么不做全量镜像

先排除一个常被误当成主要理由的因素：**容量不是问题。** 200 人公司、50 个活跃频道、2000 条/天，一年约 73 万行、文本 150MB 上下。Postgres 处理这个量级毫无压力，加按月 RANGE 分区能撑到千万行级。所以「关系库存不下聊天记录」在单租户内部部署场景下是错的，真正的理由是另外三条。

**所有权与合规。** 一旦镜像全量，本服务就成了公司全部沟通内容的第二个数据控制者。GDPR 的删除权要求原平台删一条时同步删，而我们没有这个 webhook。更麻烦的是 ACL：IM 的频道权限是逐条消息生效的，镜像会把它拍平 —— 某人退出频道后在原平台失去访问权，我们的库还在把那些内容喂给该频道的 agent。Slack 把全量归档能力（Discovery API）锁在 Enterprise Grid 的独立授权后面，这个产品决策本身就是信号：它不是一个 bot 该顺手做的事。

**时效性。** 消息会被编辑和撤回。镜像天然滞后，拿一条已撤回的消息当上下文去回答，比没有上下文更糟。

**它不解决真正的问题。** 当前最大的功能缺口是 §1.3「被 @ 时看不见上一句」，这个用一次线程拉取就解决了，与建不建镜像表无关。

## 3. 目标架构：三层各司其职

「存聊天记录」是四个不同需求被压成了一句话。拆开之后，只有第四项必须自己持久化。

| 需求 | 真实所需 | 介质 | 保留期 |
|---|---|---|---|
| Agent 的线程上下文 | 当前线程最近 N 条，秒级新鲜 | 平台 API + Redis 缓存 | 秒级 |
| 记忆蒸馏的输入 | 原文只是中间态，产物是结论 | Redis 滚动窗口 | 分钟级 |
| Ambient 规则（error_spike 等） | 滚动窗口聚合 | 同上（复用窗口） | 分钟级 |
| 审计 / 成本 / 复现 / 评测语料 | 机器人自己的输入输出与提示词来源 | **Postgres** | 可配，默认 90 天 |

### 3.1 第一层：原始消息 —— 平台是 system of record

新增 `ThreadReader` 端口（`domain/ports/conversation.py`），与 `MessagePublisher` 分开而不是给后者加方法：一个是出向发送、一个是入向读取，合并会让「只需发送」的 worker 也被迫实现读取。两平台各自实现，外面套一层 `CachedThreadReader` 按 `platform:channel:thread_ref` 在 Redis 缓存（默认 45 秒）—— 同一线程内连续几条消息不必反复打平台 API。

`ConversationService` 在 `router._run_agent()` 里填 `ContextBundle.thread_history`，已有的 `compact()` 直接复用。拉取失败一律降级为空列表：没有上下文的回答仍然可用，为拉不到历史而让整个任务失败是不划算的。

平台差异消化在各自实现里：

- **Slack**：`conversations_replies(channel, ts=thread_ref, limit)`，语义直接对应。
- **飞书**：没有「按 root message_id 拉整串」的接口。`im/v1/messages` 的 `container_id_type` 只接受 `chat` 与 `thread`，而 `thread` 要的是话题群的 `omt_` 前缀 thread_id，与我们持有的 root message_id（`om_` 前缀）不是一回事。故取 `container_id_type="chat"` 拉该会话最近一批消息，再按 `root_id == thread_ref or message_id == thread_ref` 在客户端过滤。代价是多拉一些消息后丢掉，换来的是不依赖话题群这个前提。

需要提前验证的风险：Slack 近年收紧了非 Marketplace 应用对 `conversations.history` 系接口的速率限制，具体配额取决于应用类型与上架状态。上线前用真实凭据压一轮，若配额确实紧，就把缓存 TTL 拉长到线程级、只在被 @ 时拉一次，而不是转向全量镜像。

### 3.2 第二层：机器人自己的交互记录 —— 这才是该建的表

新表 `agent_interactions`，只记机器人参与的那一小部分，量级比全量小两三个数量级：

```sql
CREATE TABLE agent_interactions (
  id                  VARCHAR(32) PRIMARY KEY,
  task_id             VARCHAR(32) NOT NULL,
  channel_instance_id VARCHAR(32) NOT NULL,
  thread_ref          VARCHAR(128) NOT NULL,
  requester_id        VARCHAR(64),
  user_prompt         TEXT NOT NULL,   -- @ 机器人的原话
  system_prompt       TEXT NOT NULL,   -- 实际组装出的系统提示词
  context_refs        TEXT NOT NULL,   -- JSON：引用了哪些 memory_entry.id、拉了几条线程历史
  model_level         VARCHAR(16) NOT NULL,
  model_id            VARCHAR(128) NOT NULL,  -- 实际生效的模型，FallbackModel 降级后是备用模型
  response            TEXT,
  tokens_in           INTEGER NOT NULL DEFAULT 0,
  tokens_out          INTEGER NOT NULL DEFAULT 0,
  result              VARCHAR(16) NOT NULL,   -- DONE / PAUSED / FAILED
  error               TEXT,
  created_at          TIMESTAMPTZ NOT NULL
);
```

三个设计取舍：

**`context_refs` 存引用而非内容快照。** 记忆条目被管理员删除后审计链仍在，但不会留下第二份内容副本 —— 否则「删除记忆」这个操作就成了假的。

**`model_id` 单独存，不从 `model_level` 反推。** `light` 档走 `FallbackModel(primary → fallback)`，主模型失败时实际生效的是备用模型。只记档位的话，成本核算会把降级后的调用按主模型单价算错。为此 `LLMResult` 需要新增 `model_id` 字段，由 gateway 从 pydantic-ai 的结果里取实际模型名。

**`tokens_in` / `tokens_out` 分开。** 输入输出单价差数倍（多数供应商相差 3-5 倍），只记 total 无法做成本归因。pydantic-ai 2.25 的 `RunUsage` 提供 `input_tokens` / `output_tokens`，直接取。

保留期由 `interactions_retention_days` 控制（默认 90），worker 定时任务按 `created_at` 清理。这张表同时服务四个用途：审计复现、按频道核算成本、`docs/tasklist.md` 第 13 项那个未做的端到端评测集的语料来源、以及回答「这个回答当时到底引用了哪条记忆」。

暂不做分区：默认保留期 90 天下单表规模有限，分区带来的迁移与运维复杂度此时不划算。等真实数据量证明需要再加，届时按 `created_at` 做 RANGE 分区即可。

### 3.3 第三层：蒸馏记忆 —— 让 `memory_entries` 回归「结论」

router 的 observe 分支不再逐条落库，改为把消息 append 进 Redis 滚动窗口（`MessageWindow` 端口）。窗口满（默认 20 条）或超过静置时长（默认 10 分钟）时，由 worker 的定时任务取出整窗，跑一次轻量模型抽取，产出的才是 `MemoryEntry`，并带上判定出的 `MemoryType`。原文只在窗口内存在于 Redis，蒸馏完即弃。

放在 worker 而不是在 router 里同步判断「窗口是否已满」：后者会让某条恰好触发阈值的普通消息承担一次 LLM 调用的延迟，而这条消息的发送者根本没有 @ 机器人、不该为此等待。

窗口按 `channel_instance_id` 分键，另用一个 sorted set 记录各频道窗口的首次 append 时间，供定时任务判断哪些到期 —— 否则要遍历全部频道键才能找出该蒸馏的那几个。

同时补上 §1.4 的隔离判定：`channel_type` 为 `im` / `mpim`（Slack）或 `p2p`（飞书）时不进窗口。这个判断放在 router，因为只有它同时持有 `IncomingMessage`（带 channel_type）与频道实例。

蒸馏提示词要求模型只输出「值得跨会话记住的事实」，明确排除寒暄、情绪、一次性问答，并给出空结果的表达方式（返回 `NONE`）—— 大多数窗口本就不该产出任何记忆，不给模型一个「什么都没有」的出口，它会为了满足格式而编造。

### 3.4 顺带修掉的两处

**`list_by_channel` 加 `ORDER BY created_at DESC` 与 `LIMIT`。** 即使做了蒸馏，这个查询也不该没有上界。

**接上真实 embedder，并让 Qdrant 维度跟随它。** `infrastructure/vector.py` 里 collection 的 `size` 硬编码 384，而常用 embedding 模型的维度是 1536（OpenAI text-embedding-3-small）或 1024。硬编码 384 意味着一旦真接上 embedder，建 collection 就会与向量维度不匹配而报错 —— 这个 bug 此前被「embedder 从未注入」掩盖着。改为由 `Embedder.dimensions` 声明，vector store 建集合时读它。未配置 embedding 凭据时装 `NullEmbedder`，`query_for_context` 走按时间倒序的有界回落，行为退化但不再是全表扫描。

## 4. 数据流总览

```
IM 消息
  │
  ├─ 非 @ ──→ [私聊?] ─是→ 丢弃（PRD §4.2）
  │            └─否→ Redis 滚动窗口 ──(worker 定时)──→ LLM 蒸馏 ──→ memory_entries + Qdrant
  │
  └─ @ ────→ router
              ├─ ThreadReader（平台 API + Redis 45s 缓存）──→ thread_history
              ├─ MemoryService.query_for_context（Qdrant 向量检索）──→ memory_hits
              ├─ ContextBundle → compact() → AgentRuntime → LLM
              └─ 落 agent_interactions（提示词 + 响应 + model_id + in/out tokens + context_refs）
```

## 5. 迁移与清理

新表由 alembic 迁移建立（`c7f3a9d1e485`，已验证 upgrade / downgrade 往返干净，含 Postgres 枚举类型的显式清理）。

已有的 `memory_entries` 里混着大量聊天碎片，需要一次清理，但**清理条件依赖真实数据分布，不适合写死在迁移里**（迁移的 downgrade 无法恢复被删的行）。故提供独立脚本 `scripts/cleanup_chat_memories.py`，默认 dry-run 只统计与抽样展示，加 `--apply` 才实际删除。判定条件是三者同时满足：类型为 `BACKGROUND_KNOWLEDGE`（蒸馏产出的 DECISION / FACT / PREFERENCE 不碰）、内容长度低于阈值、无 `embedding_ref`。

阈值默认 12 个**字符**，这个值是实测校准出来的：最初取 25，dry-run 立刻暴露它会删掉「订单服务的超时阈值是 30 秒，由网关侧统一配置」—— 只有 23 个字符，却是一条完整的背景知识。中文每字符承载的信息量远高于英文，按英文语感定的阈值在这里偏大一倍。12 能覆盖「收到」（2）、「好的我看下」（5）这类附和，而带主语、谓语与具体参数的陈述句基本都超过它。各团队说话习惯不同，仍应先跑 dry-run 看抽样。

这也正是把清理做成 dry-run-by-default 脚本而非迁移的价值：阈值定错时代价是「看一眼抽样再调参数」，而不是「数据已经没了」。

## 6. 与既有分层约束的关系

`tests/unit/test_layering.py` 锁死的规则全部保持：新端口（`ThreadReader` / `Embedder` / `MessageWindow`）声明在 `domain/ports`，只用标准库；平台 SDK 的读取实现落在 `infrastructure/messaging`；`ConversationService` / `MemoryDistiller` / `InteractionService` 是用例层策略，只依赖端口。`test_orm_registry.py` 的 `EXPECTED_TABLES` 与 `orm/__init__.py` 同步新增 `agent_interactions`。

改造过程中另有两处既有守卫被触发，都是它们该起作用的地方，故按守卫的意思改而非放宽它：

- `test_config.py::test_不含凭据字段` 拦下了写进 `config.example.yaml` 的 `embedding.base_url`。连接串属凭据侧，应与 `LLM_BASE_URL` 一致走 `.env`，故从 yaml 移除、在 `.env.example` 里说明。
- `test_repository_commit.py::test_只读方法不提交` 把 `purge_before` 误判成只读方法。根因是它的 `_mutates` 只认 `session.add/merge/delete`，识别不出 `session.execute(delete(...))` 这类批量 DML。补了 `_builds_dml`：扫函数体里是否构造了 `delete()` / `update()` / `insert()`，而非只看 `execute()` 的内联实参 —— 语句通常先赋给变量再执行，只匹配内联会漏掉最常见的写法。

## 7. 落地状态

已完成并验证：

| 项 | 验证方式 |
|---|---|
| 三层架构全部代码 | 552 个单测通过，`make lint` 干净 |
| `agent_interactions` 迁移 | 对真 Postgres 跑 upgrade → downgrade → upgrade，表结构与四个索引核对无误，枚举类型无残留 |
| Admin 交互记录端点 | 起真 web 进程，写入一条后经 API 读回，全部字段（含 `context_refs` 与分拆 token）往返正确 |
| Redis 滚动窗口 | 对真 Redis 验证 append / due_channels / drain 语义与内存实现一致，ZSET 的 NX 首写时间不被后续消息刷新，drain 后 list 与 index 均无残留 |
| 清理脚本 | 对真 Postgres 造数据跑 dry-run 与 `--apply`，确认只删碎片、保留真实知识 |

未做，且都是独立决策：

- **上下文摘要化。** `compact()` 现在是「丢弃最旧 + 说明丢了几条」，真正的摘要化需要额外一次 LLM 调用。`context_summary_threshold` 配置项与形参保留着，接上时不必改调用点。
- **`error_spike` / `deploy_status` 两条 ambient 规则。** 前者现在有数据源了（滚动窗口），但它需要的是「按错误关键词聚合计数」而非「蒸馏成记忆」，得给窗口加一个只读不清空的取数方法；后者仍缺对外 webhook 入口。
- **`agent_interactions` 分区。** 默认保留期 90 天下单表规模有限，等真实数据量证明需要再按 `created_at` 做 RANGE 分区。
- **Slack 速率限制的实测。** 见 §3.1，需要真实凭据压一轮才能定 `conversation_cache_ttl_seconds` 的最终取值。
