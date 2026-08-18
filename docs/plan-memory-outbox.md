# 记忆写入改造:transactional outbox + 异步投影

Status: 设计已定,待实施
Created: 2026-08-18

本方案取代 `plan-memory-overhaul.md` §3 的 Phase 2(Qdrant → pgvector)。那一节的判断在「消除双写不一致」这个目标上仍然成立,但它同时放弃了一批与规模无关、现在就能拿到的解耦收益。本文给出选型对比与完整设计。

结论来自对 mem0 OSS 现状(已核对 main 分支源码)、Zep/Graphiti 的存储形态、以及在本仓库真实表形状上跑的 pgvector 实测(见 §4.2)。

## 1. 背景

记忆当前是**双存储**:`memory_entries` 表是权威源(正文、类型、`superseded_by`),Qdrant 只存向量与两个 payload 字段(`entry_id`、`channel_instance_id`)。两者由 `MemoryService` 在应用层同步双写,无事务保护,失败只打 warning。

这个模型的**概念是对的** —— 正文只在 Postgres,向量是可重建的派生索引,所以不一致的后果封顶在「召回变差」,不会串内容、不会泄露已删记忆(`_semantic_hits` 里 `e is not None` 与 `e.is_current` 两道过滤兜住了)。

问题在于派生过程写在同步写路径里、失败静默、且**全项目没有任何对账机制**。按 `vector.py:99` 与 `memory.py:57` 的注释,向量路径此前因 embedder 从未注入而根本没运行过,那些失败模式一直被掩盖;改造刚把它们激活。

## 2. 现状的九个确认缺陷

前六个是双写窗口,后三个是相邻的同类问题。全部经代码核实。

| # | 位置 | 缺口 | 后果 |
|---|---|---|---|
| 1 | `memory.py:99-100` | `_repo.store()` 已 commit,`_embed_if_available()` 才执行 | 行有、向量无。该记忆退出语义检索,只能靠 `FALLBACK_LIMIT=20` 时间倒序偶然捞到 |
| 2 | `memory.py:425-431` | 向量写成功,`embedding_ref` 回填失败 | 反向孤儿。本身无害,但废掉了 `embedding_ref IS NULL` 这个唯一可观测抓手 |
| 3 | `memory.py:148-150` | `_reembed()` 在 `_repo.update(entry)` **之前** | 向量已按新内容重算,PG 还是旧内容。崩溃则检索按一份没人存的文本命中 |
| 4 | `memory.py:204-219` | 四个独立 commit:写新 → 标旧 superseded → 删旧向量 → 再 update 清 `embedding_ref` | 崩在第 2 步后:旧条目已作废但向量还在(靠 `is_current` 兜住,代价是白占 top_k)。崩在第 1 步后:同一事实两条并列 |
| 5 | `memory.py:352-356` | PG 删除已 commit,`_drop_vector` 失败只 warning | 孤儿向量持续占 top_k 名额 |
| 6 | 全局 | worker 定时任务只有 distill / stale-task / purge,**无向量对账** | 缺陷 1~5 的偏差永久累积,无自愈路径 |
| 7 | `vector.py:106,130,157` | Qdrant 不可用时降级到 `_InMemoryVectorStore`(进程内 dict),且 `_fallback.upsert` 返回非空值使 `embedding_ref` 被填上 | **制造假成功**。向量写进进程内存、重启蒸发,而该行看起来「已建索引」,任何基于 `embedding_ref IS NULL` 的补齐都会跳过它。web 与 worker 各持一份,互不可见 |
| 8 | `memory.py:99-106` + `audit_writer.py:39` | `AuditLogWriter` 走独立 repo 独立 commit,与记忆写入**不共事务** | 「记忆写成功但审计没记上」及反向均可能。`supersede()` 四步里夹着一次 `_audit.record()`,崩在中间即审计流水与实际状态不符 —— 而审计是排查「机器人为什么这么说」的第一手材料 |
| 9 | `embedding.py:84-88` | `embed()` 的 `except` 直接 `return []`,**无重试、无退避、无限流处理** | embedding 供应商限流的那几分钟里写入的记忆,永久没有向量,无人知晓、无补偿 |

## 3. 目标与非目标

**目标**

1. 记忆写入与「该建向量」这个意图落进同一事务 —— 消除缺陷 1、3、4
2. 向量投影可重试、可退避、失败可见 —— 消除缺陷 5、9
3. 存在一个覆盖全部失败模式的对账谓词,任何中间态都能自愈 —— 消除缺陷 2、6
4. 删掉制造假成功的降级路径 —— 消除缺陷 7
5. 审计记录与数据变更同事务 —— 消除缺陷 8
6. 写路径不再同步等 embedding API(控制台「保存」当前就在等)
7. 换 embedding 模型不需要停机、不需要动权威表

**非目标**

- **有序投递。** 投影是状态式的,见 §5.2
- 跨频道记忆(`plan-memory-overhaul.md` Phase 4 的事,本方案只保证不给它添障碍)
- 引用频次 reranker(Phase 3)
- BM25 / 混合检索

## 4. 方案对比

### 4.1 候选

**A. 维持现状 + 补对账。** 加一个 sweep 扫 `embedding_ref IS NULL`、删掉 `_InMemoryVectorStore`、把 `_reembed` 挪到 `update` 之后。最小改动,能压住缺陷 2、3、5、6、7,但缺陷 1、8、9 仍在(写路径仍同步 embed、审计仍不同事务、仍无重试)。

**B. pgvector 合表。** 向量并进 `memory_entries` 一列,一个事务写完。缺陷 1~7 **结构上消失**,`embedding_ref` / `_drop_vector` / `_reembed` / `_InMemoryVectorStore` / `qdrant-client` 全部退场 —— 净删代码。

**C. transactional outbox + 异步投影(本方案)。** 记忆写入与 outbox 行同事务;常驻 projector 消费 outbox,调 embedder、写 Qdrant、回填标记。

### 4.2 pgvector 的实测数据

B 案不是纸上推演,已在 `pgvector/pgvector:pg16`(pgvector 0.8.5)上按本表真实形状(含 `content` TEXT)实测两组规模,热缓存:

| 规模 | 场景 | 结果 |
|---|---|---|
| 36k 行 | 单频道 720 行 · 精确扫描 | 7.5 ms |
| 36k 行 | 全表 · 精确扫描 | 206 ms(`plan-memory-overhaul.md` §1 估算的 28 ms 低了约 7 倍,漏了 detoast 取页) |
| 300k 行 | 单频道 6000 行 · 精确扫描 | 34.7 ms |
| 300k 行 | 全表 · 精确扫描(2 并行 worker) | 1593 ms |
| 300k 行 | HNSW 建索引(单线程,`maintenance_work_mem=2GB`) | 104 s,索引 1103 MB |
| 300k 行 | HNSW 检索(带 2% 频道过滤) | 0.3 ms |
| 300k 行 | 偏索引 `WHERE superseded_by IS NULL`(10% 已取代) | 780 MB vs 全量 2281 MB,0.7 ms |

结论:**B 案在性能上完全够用,十年量级下 0.3 ms。** 所以选 C 不能拿性能当理由,拿了就是错的。

两条附带结论,后续若回到 B 案需要知道:pgvector 给 `vector` 列定的是 `attstorage='e'`(EXTERNAL,**行外但不压缩**),TOAST 阈值实测落在 1544(行内)与 3076(行外)之间;「filtered HNSW 在低选择度下返回不足 k 行」在 0.8.5 默认配置下**未复现**(0.02% 选择度仍返回完整结果)。召回率在合成数据上**测不了** —— PG `random()` 生成的 1536 维全正向量,余弦距离退化到 min=max=avg、标准差 0。

### 4.3 为什么仍选 C

B 与 C 在「消除双写不一致」上**等价**,而 B 代码更少。选 C 的全部理由是 B 拿不到的解耦收益,且这些收益**与规模无关,现在就成立**:

| 收益 | B(pgvector 合表) | C(outbox) |
|---|---|---|
| 换 embedding 模型 | `vector(N)` 的 N 定了就定了。换维度要在生产表上加列、回填、切读、删列。`config.py` 的 `embedding_dimensions` 是**假可配** | 蓝绿:起新 collection、重放灌满、切流量、删旧。权威表一字节未动 |
| 重建索引 | 与 task/budget/audit 抢同一个 `maintenance_work_mem` 与 shared_buffers。实测 300k 行 104 s | 在另一台机器上,OLTP 侧无感 |
| 备份 / WAL / 副本 | 1.1 GB TOAST 进 WAL、进 `pg_dump`、推给每个副本 —— 而这些字节本可再生 | 主库只多几十字节 outbox 行 |
| embedder 限流 | 仍需 NULL-then-backfill,即一个简化版 outbox | 条目留在表里退避重试,超限进死信 |
| 审计同事务 | 需要另做(与向量无关) | UoW 顺带解决 |
| 控制台写入延迟 | 仍同步等 embed(或写 NULL) | 立即返回 |
| 量化 | 有 `halfvec`/binary,但**无自动 rescore 管线** | Qdrant 内建 scalar/binary/product + rescore,常驻内存可压到 1/32 |
| 换向量引擎 | 迁移表结构 | 换消费者,写路径不动 |

代价要写明:**C 是净增机制**(一张表、一个 projector、退避、死信、指标、对账),B 是净减代码。另外 C 保留 Qdrant 这个有状态服务的运维成本,并引入一个最终一致的时间窗(§5.4 给出 lag 目标)。

**本方案不追求的**:有序投递。投影是**状态式**的 —— outbox 只存 `entry_id`,投影时回读当前行再重新求值。`edit` 与 `supersede` 会让同一条记忆变化多次,重放滞后事件就是拿旧内容覆盖新向量;回读当前行则天然幂等,顺序无关紧要。这也是 `embedded_hash` 对账能顶掉大部分 outbox 语义的原因。

## 5. 设计

### 5.1 要维护的不变量

全系统关于向量只有一条不变量。写在这里是为了让对账谓词逐字对应它:

> 一条记忆**应当有向量**,当且仅当 `type != PREFERENCE` 且 `superseded_by IS NULL`。若应当有,则该向量必须是由本行**当前** `content` 求出的。

形式化成两个谓词,`MemoryReconciler` 逐字用它们:

```sql
-- 缺向量或向量过期 → 需要 UPSERT
type <> 'PREFERENCE' AND superseded_by IS NULL
  AND (embedding_ref IS NULL OR embedded_hash IS DISTINCT FROM md5(content))

-- 不该有向量却有 → 需要 DELETE
(type = 'PREFERENCE' OR superseded_by IS NOT NULL) AND embedding_ref IS NOT NULL
```

两条合起来覆盖 §2 缺陷 1~7 的全部失败模式,含崩在任意中间态、以及合表前遗留的脏数据。

`embedded_hash` 是新增列,存「向量是按哪份内容建的」。与 `embedding_ref`(向量存不存在)是两件事,缺一不可:只有 ref 判不出内容漂移,只有 hash 判不出向量丢失。mem0 在 payload 里存 `hash: md5(data)` 是同一个做法。

### 5.2 投影决策纯状态式

projector 取到一条 outbox 记录后,**只看当前数据库状态**,不看记录里的 `op`:

| 回读 `memory_entries` 的结果 | 动作 |
|---|---|
| 行不存在 | 删向量 |
| 行存在,但 `type = PREFERENCE` 或已被取代 | 删向量 |
| 行存在且应当有向量 | embed 当前 `content` → upsert → 回填 `embedding_ref` 与 `embedded_hash` |

`op` 列保留,但**只作为可观测信息**(是什么操作入的队),永不作为指令。这条纪律是刻意的:一旦按 `op` 行事,滞后的 `UPSERT` 就会拿旧内容覆盖新向量 —— 正是 §4.3 末尾说的那个 bug 类。

一个推论:`delete` 必须让「删行」与「写 outbox」同事务(本方案如此)。行删掉后 `entry_id` 仍在 outbox 里,回读为空即触发删向量,语义正确。

### 5.3 数据模型

`memory_entries` 加一列:

| 列 | 类型 | 说明 |
|---|---|---|
| `embedded_hash` | `varchar(32)` nullable | 建索引时所用 `content` 的 md5,见 §5.1 |

新表 `memory_outbox`:

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `varchar(32)` PK | `gen_id("obx")` |
| `entry_id` | `varchar(32)` idx | 目标记忆。**不做外键** —— 记忆物理删除后这条仍要被处理,与 `superseded_by` 的同类取舍一致 |
| `op` | `Enum(OutboxOp)` | `UPSERT` / `DELETE`。仅可观测,见 §5.2 |
| `attempts` | int, default 0 | 已尝试次数 |
| `next_attempt_at` | timestamptz idx | 退避到点才可取,新记录即 `now()` |
| `claimed_at` | timestamptz nullable | 租约起点,见 §5.4 |
| `claimed_by` | `varchar(64)` nullable | 持租者(进程名+pid),排查用 |
| `last_error` | text nullable | 最后一次失败原因,截断存 |
| `failed_at` | timestamptz nullable | 非空即死信,不再被取 |
| `created_at` | timestamptz idx | 入队时间,lag 由它算 |

死信不另建表:`failed_at IS NOT NULL` 就是死信,可查、可人工重置、可统计。多一张表只是多一处要同步的形状。

处理成功即**删行**,不留已完成记录:审计已由 `audit_logs` 承担,outbox 只是待办队列。留着会让 lag 查询要额外带状态过滤,且这张表会无界增长。

### 5.4 Projector 运行时

**形态**:worker 进程里一个常驻 asyncio 循环(不是 APScheduler 定时任务)。有活就连续处理,空转 `sleep(poll_interval)`,默认 2 秒。选常驻而非 30 秒定时的理由:「刚写入的记忆半分钟内搜不到」在同一个会话里就会被用户察觉 —— 蒸馏刚提炼出一条结论,紧接着的提问就该能命中它。

**目标 lag**:p99 < 5 秒(2 秒轮询 + 一次 embed 往返)。这不是硬 SLO,是设定告警阈值的依据。

**抢占用租约,不用 `FOR UPDATE SKIP LOCKED` 长事务。** 两者都能防重复处理,选租约的理由是 embed 是远程调用:`SKIP LOCKED` 要在整个 embed 期间持有行锁,即持有一个数据库连接,而连接池有限、embed 可能因限流耗上几十秒。租约把「抢占」与「处理」拆成两个短事务:

```sql
-- 抢占(短事务,立即提交)
UPDATE memory_outbox SET claimed_at = now(), claimed_by = :who
WHERE id IN (
  SELECT id FROM memory_outbox
  WHERE failed_at IS NULL AND next_attempt_at <= now()
    AND (claimed_at IS NULL OR claimed_at < now() - :lease)
  ORDER BY created_at
  LIMIT :batch
  FOR UPDATE SKIP LOCKED
)
RETURNING *
```

`claimed_at < now() - lease` 即租约过期回收 —— projector 崩溃后那批记录自动重新可取,不需要额外的清理任务。`lease` 默认 300 秒,取值须显著大于单次 embed 的最坏耗时。

**退避**:`next_attempt_at = now() + min(2^attempts, 300) 秒`,`attempts >= 8` 置 `failed_at`。8 次约覆盖 10 分钟,足够熬过多数限流窗口;再失败就该有人看告警了。

**批量**:一次抢 `batch_size` 条(默认 32)。embed 逐条调 —— 批量 embed 接口能省往返,但一条失败会牵连整批的重试语义,而当前写入量(每 10 分钟一轮蒸馏、一次几条)不值得为此复杂化。这是可以后续单独优化的点。

**幂等**:同一 `entry_id` 可能有多条 outbox 记录(连续 edit)。都会被处理,后处理的按当前内容重算 —— 结果相同,只是多花一次 embed。不去重是有意的:去重要在写入侧查 outbox,把「写记忆」变成读写混合,而重复处理的代价只是几次多余的 embed 调用。

### 5.5 Unit of Work

**问题**:现在九个仓储、共 15 处 `commit()`,每个方法各自提交。记忆写入与 outbox 行必须同事务,审计也要一并进去。

**做法**:引入 `UnitOfWork` 包住 session,仓储不再 commit,由用例层在边界提交。全仓储统一改造(不只 memory),避免同一层出现两种事务风格。

**可重入**:`UnitOfWork` 用引用计数,嵌套 `async with` 时内层是 no-op,最外层提交。这一点是必须的 —— `MemoryService.supersede()` 内部调 `self.store()`,若两者各开一个 UoW,不可重入的实现会在内层就提交掉半个操作。有了可重入,服务方法可以各自声明事务边界而不必关心谁是外层。

```python
async with uow:                      # 外层:开事务
    await memory.supersede(...)      #   内部 async with uow 是 no-op
# 退出最外层 → commit;异常 → rollback
```

**边界放在服务方法上**,不是 adapter。理由:adapter 有四类入口(admin router、平台 router、worker job、scheduler),放在那里要改四处且容易漏;放在服务方法上,`store` / `edit` / `supersede` / `delete` 各自是一个原子操作,语义与调用方无关。

**session 的现状要一并处理**:`build_container()` 用**单个共享 session**(`container.py:142` 的注释自己写了「MVP,接入 FastAPI 后应替换为 session-per-request」),而 `job_scope()` 每次开新 session。共享 session 上做事务边界是危险的 —— 两个并发请求会互相提交对方的未完成变更。本方案**必须**顺带修掉这一点,否则 UoW 是假的。改为 admin/平台入口 per-request 建 session,详见 §6 第 2 步。

### 5.6 可观测面

项目此前 `prometheus` / `/metrics` / `Counter(` 全部零命中。本方案建起第一个可观测面:`prometheus-client`,web 进程暴露 `/metrics`。

| 指标 | 类型 | 说明 |
|---|---|---|
| `teamai_memory_outbox_pending` | Gauge | 待处理条数(`failed_at IS NULL`) |
| `teamai_memory_outbox_dead` | Gauge | 死信条数 |
| `teamai_memory_outbox_lag_seconds` | Gauge | `now() - min(created_at)`,最老待处理条目的等待时长 |
| `teamai_memory_projected_total` | Counter | 按 `op` 与 `result` 分标签 |
| `teamai_memory_embed_seconds` | Histogram | embed 调用耗时 |
| `teamai_memory_reconcile_total` | Counter | 对账补出来的条数,按方向(`upsert`/`delete`)分标签 |

**lag 用 `min(created_at)` 而不是「平均等待」**:平均值会被大量刚入队的记录稀释,而我们要答的问题是「最坏情况下一条记忆多久能被搜到」。

**Gauge 的采集方式**:projector 每轮结束时写入,而不是在 `/metrics` 被抓时查库 —— 后者让抓取端能触发数据库查询,是个放大面。代价是 web 进程暴露的是 worker 上次循环时的值,滞后一个轮询周期(2 秒),对告警足够。跨进程用 `prometheus_client` 的 multiprocess 模式实现。

`teamai_memory_reconcile_total` 是最该接告警的一个:**它长期为 0 才是正常**。一旦持续非零,说明 projector 在漏活,而不是对账在干活 —— 对账是安全网,不该是常态路径。

### 5.7 Reconciler

**形态**:worker 的 APScheduler 定时任务,间隔复用 `jobs_sweep_interval_minutes`(默认 10 分钟)。

**做法**:按 §5.1 的两个谓词各扫一遍,给命中的行补写 outbox 记录(而不是直接调 embedder)。这样投影逻辑只有一处 —— projector,对账只负责「发现遗漏并重新入队」。

**为什么必须存在**,即便 outbox 已经保证了不丢:

1. 合表前、以及本方案上线前写入的存量行(缺陷 1、2、7 的历史残留)不在 outbox 里
2. projector 自己会有 bug,死信会被人工重置,`op` 语义可能被误用
3. Qdrant 侧的数据可能被外部操作(重建集合、误删、恢复备份到旧时点)—— 这时 Postgres 的 `embedding_ref` 与实际不符,而**只有对账能发现**

第 3 条是 outbox 覆盖不到的:outbox 保证「我发出的意图最终会执行」,不保证「执行结果后来没被别人改掉」。

**一个已知限制要写明**:对账检测不出「向量存在但内容错误且 hash 恰好匹配」这种情况 —— 那需要重新 embed 一遍再比向量本身,成本等于全量重建。md5 碰撞的概率可以忽略,但「向量被写进了错误的 collection」这类配置错误确实检测不出。补救手段是提供一个全量重建入口(§6 第 8 步)。

### 5.8 要删掉的东西

| 删什么 | 理由 |
|---|---|
| `_InMemoryVectorStore` 及三处降级分支 | 制造假成功(缺陷 7)。Qdrant 不可用时应让 `upsert` 抛异常,由 projector 走退避重试 —— 这正是 outbox 存在的意义 |
| `MemoryService._embed_if_available` | 职责移交 projector |
| `MemoryService._reembed` | 同上 |
| `MemoryService._drop_vector` | 同上 |
| `scripts/cleanup_chat_memories.py` 里 `embedding_ref IS NULL` 判据 | 该判据的语义变了(现在「没向量」是暂态而非「非正规路径写入」)。这个脚本的其余判据(type + 长度)仍然有效,只需去掉这一条并更新 docstring |

`VectorStore` 协议本身保留,但 `upsert` / `delete` 的契约从「失败返回 None / 只告警」改为**失败抛异常**。这是本方案对基础设施层契约的唯一改动,且是必要的:projector 要靠异常决定是否重试。

## 6. 实施步骤

每一步都应独立可验证(单测通过 + `make lint` 干净)。顺序有依赖,不要跳。

**1. UnitOfWork 基础设施。** `domain/ports/uow.py` 声明抽象(可重入、`commit`/`rollback`),`infrastructure/uow.py` 给 SQLAlchemy 实现。此步不改任何仓储,只加新文件 + 单测。

**2. session-per-request,并去掉共享 session。** 改 `container.py`:`build_container()` 不再持有 session 与仓储实例,改为提供一个 `request_scope()`(形状同现有 `job_scope()`)。admin / 平台入口各自开 scope。这一步是 §5.5 指出的前置,不做则 UoW 无意义。**这步改动面最大、回归风险最高**,单独提交。

**3. 仓储去 commit。** 九个仓储共 15 处 `commit()` 全部删除,由 UoW 提交。同时在每个仓储类的 docstring 里写明「事务由 UoW 管理」—— `task.py:52` 那条解释为何自提交的注释要改掉,否则后来者会照抄。

**4. 服务层加事务边界。** `MemoryService` 的 `store` / `edit` / `supersede` / `delete` 各包一个 `async with self._uow`。其余服务(orchestrator / budget / tag / policy / channel / interaction)同样处理 —— 全仓储改造的必然结果。

**5. outbox 领域模型与表。** `domain/models/outbox.py`(`OutboxEntry` + `OutboxOp`)、`domain/repositories/outbox.py`、`infrastructure/orm/outbox.py`、`infrastructure/repositories/outbox.py`。迁移:建 `memory_outbox`、给 `memory_entries` 加 `embedded_hash`。`down_revision` 指向当前 head(`2120bce9e0f3`),按仓库惯例写中文 docstring 说明理由,并对真 Postgres 验 upgrade → downgrade → upgrade 往返。

**6. MemoryService 改为只入队。** 删 `_embed_if_available` / `_reembed` / `_drop_vector`,四个写方法改为在同一 UoW 里写 outbox 行。此步之后向量**不会被写入**(projector 还没上),单测要相应改断言 —— 这是预期的中间态。

**7. Projector。** `application/projector.py` 实现 §5.2 的状态式决策与 §5.4 的租约/退避;`app/worker/main.py` 起常驻循环。`VectorStore` 的 `upsert`/`delete` 改为抛异常,删 `_InMemoryVectorStore`。到这一步链路重新贯通。

**8. Reconciler + 全量重建入口。** `application/reconciler.py` 按 §5.1 两个谓词补写 outbox;注册为 APScheduler 任务。另加一个 `scripts/rebuild_memory_vectors.py`(把指定频道或全部记忆重新入队),对应 §5.7 的已知限制。

**9. 指标。** `pyproject.toml` 加 `prometheus-client`;`infrastructure/metrics.py` 定义 §5.6 那六个;web 挂 `/metrics`;projector 每轮回写 Gauge。multiprocess 模式需要 `PROMETHEUS_MULTIPROC_DIR`,在 `.env.example` 与 `config.example.yaml` 里给出。

**10. 配置项。** `projector_poll_interval_seconds`(2)、`projector_batch_size`(32)、`projector_lease_seconds`(300)、`projector_max_attempts`(8)。按仓库惯例:非敏感项进 `config.example.yaml`,展平后可用环境变量覆盖。

**11. 清理与文档。** `cleanup_chat_memories.py` 去掉 `embedding_ref IS NULL` 判据;`plan-memory-overhaul.md` §3 标注被本文取代;`tasklist.md` 补项;`Design-conversation-context.md` 补一节写投影链路。

## 7. 验证

**必须新增的用例**,每条都对应一个当前会失败的场景:

| 用例 | 断言 |
|---|---|
| 写记忆时 embedder 抛异常 | 记忆行**在库里**,outbox 行也在,`attempts` 递增且 `next_attempt_at` 被推后 |
| 同一条记忆连续 edit 两次 | 两条 outbox 都被处理,最终 `embedded_hash == md5(最新 content)` |
| projector 取到已被删除的 entry_id | 调了 `vector.delete`,未调 `embed`,outbox 行被清掉 |
| projector 取到 PREFERENCE 条目 | 调了 `vector.delete`,未调 `embed` |
| 租约过期 | 另一个 projector 实例能重新抢到同一条 |
| `attempts` 达上限 | `failed_at` 被置,此后不再被抢占 |
| 对账:行有 `embedding_ref` 但 hash 不符 | 补出一条 outbox |
| 对账:已被取代的行仍有 `embedding_ref` | 补出一条 DELETE 方向的 outbox |
| UoW 回滚 | 记忆行与 outbox 行**都不存在**(这是本方案的核心保证) |
| UoW 可重入 | `supersede` 内部调 `store`,异常时新旧两条记忆都没落库 |
| 审计同事务 | 写记忆失败时,审计行也不存在 |

**冒烟**:补一个 `scripts/verify_outbox_flow.py`,形状对齐现有 `verify_long_task_flow.py` —— 写一条记忆、等 projector 处理、断言向量可被检索到、断言 outbox 已清空。

**迁移验证**:对真 Postgres 跑 upgrade → downgrade → upgrade。

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 第 2 步(session-per-request)回归面大 | 单独提交、单独验证。这一步不涉及 outbox,可先合入观察 |
| 第 6 步之后到第 7 步之前,向量不写入 | 中间态,不要在此处停下发布。若必须发布,先上第 7 步 |
| projector 挂掉无人知 | 这正是 lag 指标要接告警的原因(第 9 步)。在此之前靠 reconciler 兜底(10 分钟粒度) |
| Qdrant 长时间不可用 | 检索侧已有时间倒序回落(`FALLBACK_LIMIT=20`),不影响可用性;outbox 堆积,恢复后自动追平 |
| 死信堆积无人处理 | `teamai_memory_outbox_dead` 接告警;`rebuild_memory_vectors.py` 可批量重置 |

**回滚**:第 5 步的迁移可 downgrade(两个对象都是新增,无数据丢失)。第 2~4 步是代码改动,git revert 即可。已写入 Qdrant 的向量不受影响。

## 9. 与既有文档的关系

- 本文**取代** `plan-memory-overhaul.md` §3(Phase 2:Qdrant → pgvector)。那一节的实测数据(本文 §4.2)仍有参考价值,若将来回到 pgvector 方案可直接用
- `plan-memory-overhaul.md` Phase 3(引用频次、偏好收窄)与 Phase 4(跨频道)**不受影响**,本方案不改检索侧语义
- Phase 4 若落地,跨频道过滤要在 Qdrant payload 上做(而非 SQL `WHERE`)—— 这是选 C 而非 B 的一处代价,当时的分析里写过,此处重申
