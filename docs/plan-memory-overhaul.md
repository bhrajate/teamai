# 记忆系统改造方案

Status: Phase 1 已落地，Phase 2-4 待做
Created: 2026-08-16

本方案是对 `Design-conversation-context.md` 三层架构落地后暴露出的问题的修补,以及对 `Design-claude-tag.md §3.8` 跨频道授权的重新设计。结论来自对 mem0(arXiv:2504.19413)、Zep/Graphiti(arXiv:2501.13956)两篇论文与其开源实现的核对,以及对本仓库现状的核实。

## 0. 现状的四个确认缺陷

| 缺陷 | 证据 | 后果 |
|---|---|---|
| 写入侧无去重与冲突处理 | `distiller.py:155-183`,`grep dedup\|去重\|合并\|conflict` 零命中 | 同一事实重复堆积占满 `top_k=5`;矛盾事实并列共存且无法裁决 |
| `Visibility` 是死枚举 | 无调用方传 `visibility=`,检索侧 `query_for_context` 不看该字段 | 「私密不外传」的承诺由 `router.py:91` 丢弃单聊来兜,与该字段无关 |
| `cross_channel_learning` 从未被读作条件 | `grep cross_channel` 全部命中都是存/取/改/序列化 | `tasklist.md:67` 声称已实现「跨频道授权检查」,实际不存在 |
| `ChannelInstance` 无来源可见性字段 | `channel.py:14-23` | `Design-claude-tag.md §3.8` 的「目标频道为公共频道」条件当前不可实现;Slack private_channel 照常蒸馏入库且无法区分 |

## 1. 已定的设计决策与理由

**存储:pgvector,不建 ANN 索引。** 规模测算(按 `Design-conversation-context.md:55` 的 200 人/50 频道/2000 条每天):单频道 220~730 条/年,全部 50 频道 11k~36.5k 条/年。顺序扫 700 行约 0.5ms,36000 行约 28ms,而链路上本就有秒级 LLM 调用。单频道过滤的选择度是 2%,恰是 HNSW 最差的形态。arXiv:2602.11443 的发现:pgvector 的 cost-based optimizer 常在精确顺序扫描能给出完美召回且延迟相当时,仍错选近似索引扫描 —— 规避方式是压根不建那个索引。

**放弃 Qdrant 的理由不是它不好,是双写在本仓库已经在漏:** `memory.py:228-238` 孤儿向量、`memory.py:281-283` 回填失败只告警、`vector.py:94` 记录的「传 dict 而非 PointStruct、异常被吞成 warning、向量写入从未成功过且不报错」、以及需要 `scripts/cleanup_chat_memories.py` 兜底。旁证:mem0 的 `mem0/vector_stores/` 下 25 个后端中 `pgvector.py` 与 `qdrant.py` 平级;Zep/Graphiti 全部后端都是图库,embedding 作为节点/边属性(`e.fact_embedding`),索引定义里没有一条 vector index。

**治理抄 mem0 的 update 阶段,但不给 DELETE。** mem0 的四操作 ADD/UPDATE/DELETE/NOOP 由 LLM 经 function-calling 直接选择(论文明确「Rather than using a separate classifier」),配置 m=10 近期消息、s=10 相似记忆、GPT-4o-mini。DELETE 不给的理由:`superseded_by` 能表达同一件事且可回溯,而删除不可逆。

**时序治理取单时间轴,不做双时间轴。** Zep 的 `t_valid`/`t_invalid`(现实时间)与 `t'_created`/`t'_expired`(系统时间)分离,边失效时把旧边 `t_invalid` 设为新边的 `t_valid`。本项目蒸馏近实时(窗口静置 600s 即触发),`created_at` 与事实成立时间偏差在分钟级,双时间轴收益接近零。取舍要写进字段注释,否则后来者会以为漏了。

**不建图。** mem0 自己的数据:`Mem0^g` 只比 base 高约 2%,而代价是实体抽取+关系生成+Neo4j。Zep 的大头收益在 LongMemEval(+18.5%,temporal reasoning)而非 DMR(+1.4%)—— 收益来自时序而非图结构。

**不做 BM25。** Zep 三路混合(cosine + Okapi BM25 + BFS)里,BM25 撞中文分词(Postgres 无内置中文 tsvector),BFS 要有图。

**跨频道用「来源仅限公共频道」硬约束,不用继承发起人权限。** 继承在 Claude Tag 里安全,靠的是它同时具备 ephemeral sandbox(无持久记忆)与 drafts responses privately first(输出前人工审核)。本项目回答直接 `publisher.reply()` 进频道,只搬继承不搬私下起草,泄露仍发生在输出侧。且 ambient 路径无真实发起人(`ambient.py:124` 巡检驱动)、scheduler 路径发起人会过期、平台侧无成员查询端口(`ports/conversation.py` 只有 `fetch_thread`)。而在只收公共来源的池子里,继承检查结果恒为真。

**工具权限不动。** `PermissionPolicy` 挂 `channel_instance_id`,工具经 `runtime.py:95` 的 `for_channel` 过滤(未授权者不注册给模型,比调用时拒绝更强)。继承需 per-user OAuth token,量级不同,且 Claude Tag 自己的工具也是频道/组织级服务凭据。

## 2. Phase 1:去重合并 + superseded_by ✅ 已落地

不依赖任何存储层改动,可独立验证效果。

**落地结果**:659 个单测通过,`make lint` 干净,TypeScript `tsc --noEmit` 干净。迁移 `f3b9d27a5c14` 对真 Postgres 验过 upgrade → downgrade → upgrade 往返。设计文档补了 §3.3.1 记录机制与取舍。

**实现中发现的两处、计划里没写到的**:

一是模型引用候选**用列表序号而非记忆 id**。让模型输出 `mem_01K…` 这种 ULID 极易出错(编造、截断、张冠李戴),1..N 的小整数几乎不会错,映射回真实 id 由 `_apply_actions` 做。这条改变了提示词的形状。

二是**同一编号被取代两次要降级为 ADD**。模型可能对同一条候选输出两个 UPDATE(把一件事拆成两句话表述),第二次取代的是已被标记的旧条目,会形成 A→B→C 的链,而中间那条 B 从未进入过检索。`_apply_actions` 用 `superseded_refs` 集合挡住。

还有一处是测试抓出来的真 bug:`_parse_entries` 原先对每一段都 `strip()`,内容里含 `|` 时(模型贴命令或表格,`a | b | c`)会被压成 `a|b|c`。改为只对前三段 strip、内容段原样 join。

**领域模型** `domain/models/memory.py`:`MemoryEntry` 加 `superseded_by: str | None = None` 与 `superseded_at: datetime | None = None`。注释写明为何是单时间轴。

**ORM** `infrastructure/orm/memory.py`:两列,`superseded_by` 加索引(检索要按它过滤)。

**迁移**:新建一个 revision,`down_revision` 指向 `e5a71c9d3b28`。两列都可空,无需回填(既有行全是未被取代)。按 `d2e8b41f7c96` 的惯例写中文 docstring 说明理由。

**提示词** `application/agent/prompts.py`:`DISTILL_SYSTEM_PROMPT` 的输出格式从 `类型|内容` 扩为 `动作|类型|id|内容`,动作取 `ADD`/`UPDATE`/`NOOP`。`ADD` 的 id 位留空,`UPDATE` 必须带既有 id,`NOOP` 只带 id。保留 `DISTILL_NONE` 出口(它解决的是「模型为满足格式而编造」,与动作维度无关,理由见 `prompts.py:50`)。新增一条约束:同一次输出的多条之间也不得互相重复 —— 本项目按窗口批量蒸馏,一次调用会同时产出多条,而 mem0 按消息对增量处理天然逐条比对,这个差异必须在提示词里补。

**蒸馏器** `application/distiller.py` 的 `_distill_channel`:drain 之后先按窗口内容检索该频道已有的近似记忆(top_k 取大些,候选给模型看宁可多给),连同新消息一起送 LLM,再按动作分派 store / supersede。`_parse_entries` 加 id 合法性校验:id 必须存在且属于本频道,不合法降级成 `ADD` 而非丢弃 —— 丢弃会让知识彻底进不来,降级只是可能多一条重复。

**记忆服务** `application/memory.py`:新增按内容查近似的方法(可复用 `_semantic_hits` 的路径,输入换成蒸馏产出的内容);新增 supersede 操作(写新条目 + 给旧条目打 `superseded_by`/`superseded_at`,记一条审计);`query_for_context` 与 `list` 默认过滤 `superseded_by IS NULL`。

**仓储层**:mapper 两侧带上新字段,查询加 superseded 过滤。⚠️ `domain/repositories/memory.py` 与 `infrastructure/repositories/memory.py` 这两个文件我未能读到(工具故障),已知的是 `store`/`get`/`update`/`delete`/`list_by_channel(limit=)`/`set_preference`/`list_preferences` 存在、mapper 在 `:21` 与 `:35`、`list_by_channel` 已有 ORDER BY + LIMIT。动手前需先读这两个文件确认签名。

**验证**:两个当前必然失败的用例 —— 同一事实在三个窗口被提到,断言库里最终只有一条;「先 3 秒后 5 秒」序列,断言最终生效的是 5 秒那条且旧条目带 `superseded_by`。

### 2.1 删掉 `Visibility`(已确认)

它想表达的「这条不该外传」在 Phase 4 由 `ChannelInstance` 的来源可见性承载 —— 那是客观事实,而非写入时的判断。

**为什么删而不是标注废弃**:Phase 4 恰恰是要拿可见性做判断的阶段,而这个字段「看起来在做权限控制、实际恒为默认值」。那时写 `WHERE visibility != 'private'` 会静默匹配零行并被当成已获得保护。删掉之后同样的写法在开发期就报错。

**为什么不补活**:补活需要先反转 `router.py:91` —— 现在私密消息在进蒸馏窗口之前就被丢弃,压根不产生记忆条目;要让 `visibility` 有意义,得让这些内容进来、打标记、靠检索侧过滤。这是把结构性保证(不采集)换成逻辑保证(采集后过滤),且不可逆:私密内容一旦入库,退回去只能删行。若不反转 router,它唯一能标的就是「来自私密频道的记忆」,而那正是 Phase 4 用 `ChannelInstance` 表达的同一件事,且做得更差 —— 一个频道一条事实 vs 几千条记忆各标一遍,后者在频道 public → private 转换时会全部失效需要回填。

**迁移风险异常低**:库里所有行都是 `CHANNEL`(无写入方产生 `PRIVATE`),drop 掉不丢任何信息。这一点与一般的删列不同。

**完整改动面**(经 grep 核实,跨前后端十余处):

| 位置 | 改动 |
|---|---|
| `domain/models/memory.py:39,52` | 删 `Visibility` 枚举与 `MemoryEntry.visibility` 字段 |
| `domain/models/__init__.py:23,50` | 去导出 |
| `infrastructure/orm/memory.py:10,29` | 去 import 与列定义 |
| `infrastructure/repositories/memory.py:21,35` | mapper 两侧去字段 |
| `application/memory.py:20,66,83` | 去 import 与 `store()` 参数 |
| `adapters/admin/serializers.py:47` | 不再吐给前端 |
| 新迁移 | `drop_column` + `sa.Enum(name="visibility").drop(bind, checkfirst=True)`(枚举类型在 Postgres 里是独立对象,不显式删会撞 DuplicateObject,见 `d2e8b41f7c96:59-61` 的同类处理) |
| `web/src/api/types.ts:91` | 去类型字段 |
| `tests/unit/test_memory.py:27,132,350,358,369` | 去 import 与断言;`:358` 是直接构造 `Visibility.PRIVATE` 的实体,整个用例要重写 |
| `tests/unit/test_memory_repository.py:27,93,105` | 同上 |
| `tests/unit/test_admin_routes.py:32` | 注释 |
| `tests/unit/test_router.py:190` | 注释里提到该枚举,改为说明私密判定由 `channel_type` 直接丢弃承担 |

**三处为它辩护的注释要一并处理**(它们的理由在字段存在时成立,删掉后失去对象):`application/memory.py:108`、`adapters/admin/memory.py:55`、`web/src/api/index.ts:52`。

**留一条后路**:若 Phase 4 之后真出现「单条记忆需人工限制外传」的需求,那时新加一个语义明确、只由人工设置的字段(如 `share_scope`),而不是复用这个半成品。

## 3. Phase 2:Qdrant → pgvector

**依赖与部署**:`pyproject.toml` 去 `qdrant-client`、加 `pgvector`;`deploy/docker-compose.yml` 的 `postgres:16` 换 `pgvector/pgvector:pg16`,删掉 qdrant service(`:41`)与 `qdrant_data` 卷(`:50`);`config.py` 去 `qdrant_url`/`qdrant_collection`。

**schema**:`memory_entries` 加 `embedding vector(N)` 列,N 取 `embedding_dimensions`(默认 1536)。迁移里 `CREATE EXTENSION IF NOT EXISTS vector`。**不建 HNSW/IVFFlat**,在迁移与 ORM 两处都写注释说明这是有意为之及其规模依据 —— 否则后来者会当成遗漏而补上,反而引入 arXiv:2602.11443 说的错误计划选择。

**`embedding_ref` 退场**:向量成为本行的列之后,「是否已建索引」退化为 `WHERE embedding IS NULL`,不再需要单独追踪的引用。`_drop_vector`、`_reembed` 里的孤儿清理、`_embed_if_available` 的回填、以及 `scripts/cleanup_chat_memories.py` 依赖的 `embedding_ref IS NULL` 判断都随之简化或删除。

**保留 `Embedder` 端口与 `NullEmbedder` 降级路径** —— 那层抽象是对的,只换向量的落地方式。`_vector_ready` 的判定(`memory.py:57`)仍需要,因为 embedder 仍可能不可用。

**检索**:`ORDER BY embedding <=> :q LIMIT :k` 配 `WHERE channel_instance_id = :cid AND superseded_by IS NULL`。向量不可用时的时间倒序回落(`FALLBACK_LIMIT`)保留不变。

**迁移存量向量**:Qdrant 里的向量不迁 —— 重新 embed 一遍即可(单频道几百条,成本可忽略),比写一个跨库搬运脚本可靠。迁移后跑一次「补齐所有 `embedding IS NULL`」的任务。

## 4. Phase 3:引用频次信号 + 偏好收窄

**引用频次(抄 Zep 的 episode-mentions reranker)**:被引用多的记忆更易被取到。`ContextBundle.memory_ref_ids`(`context.py:49`)已经在留痕,加一个计数列,检索时作为次序权重。这个信号对「哪条记忆真的重要」的判断可能比余弦相似度更准,实现成本低。

**偏好只带发起人自己的**:`query_for_context` 现在把频道内所有人的偏好全量带上。一旦偏好跨 channel(下一条),张三设的「回答尽量简短」会跟着他影响李四的提问。这个改动是收窄权限,应与偏好跨 channel 同时落地。

**偏好按 user 跨 channel**:独立 `preferences` 表已于 2026-08-18 合表删除 —— 偏好是 `memory_entries` 里 `type='PREFERENCE'` 的行,归属由 `source_user_id` 承载,同一人换频道要重新交代。作用域取 `workspace_id`(不取全局,避免一个公司的上下文漏到另一个)。偏好是「怎么回答」的约束(语气、格式、禁忌)而非团队内容,泄露风险低,且不参与向量检索,改作用域不影响检索质量。

## 5. Phase 4:跨频道记忆(最后做)

**前提**:`ChannelInstance` 加来源可见性字段,从 `IncomingMessage.channel_type` 落库。存量数据无法回溯判定,一律标记为未知并排除在跨频道池外 —— 宁可少召回。这是 Phase 4 的硬前置,`Design-claude-tag.md §3.8` 的条件在它落地前不可实现。

**池子定义**:同 workspace + 来源为公共频道 + `superseded_by IS NULL`。管理员开关复用现有 `cross_channel_learning`(终于让这个字段被读作条件)。安全性来自「来源仅限公共」这个硬约束本身,不来自开关 —— 公共频道内容工作区内任何人本来就可搜可加入,机器人蒸馏后说出来不构成越权。

**检索**:`WHERE channel_instance_id = ANY(:candidates)`,候选集由上述条件算出。这是 pgvector 相对 Qdrant 的实际好处 —— Qdrant 里加一个过滤维度要重灌全部 point。

**必须写进文档的已知限制**:Slack 单频道访客(single-channel guest)看不到所有公共频道,所以「公共 = 工作区所有人可读」对有访客的工作区不严格成立。要么接受并写明,要么只对访客账号补一层成员检查(访客是少数,比全员继承便宜得多)。

**正确性属性要改写**:`Design-claude-tag.md §5` 的频道隔离不变量与 §7.4 的隔离性安全测试,当前表述是「仅访问 ch_A 的数据」,需改成带条件的版本,测试同步重写。

## 6. 顺序与理由

Phase 1 → Phase 2 → Phase 3 → Phase 4。

Phase 1 最先:不修去重就做跨频道,等于把重复与矛盾扩散到更大的池子,`top_k=5` 会被跨频道的近似条目挤满,效果可能比不跨更差。而它不依赖存储层改动,能独立验证。

Phase 2 必须在 Phase 4 之前:否则跨频道的过滤逻辑要在 Qdrant payload 上写一遍、迁移后再写一遍。

Phase 1 完成后可重新评估 Phase 2 的紧迫性:若去重后单频道稳定在几百条,Qdrant 那些双写故障在低写入量下暴露概率低;反之若发现存量重复量大,清理会是独立任务,与迁移一并做更划算。

## 7. 文档同步

- `tasklist.md:67` 的 `[x]` 要改 —— 「跨频道授权检查」当前不存在
- `tasklist.md` 补记 `cross_channel_learning` 是死字段(已记了 `TagTemplate.shared` 的同类问题,见 `:93`)
- `Design-conversation-context.md` 补一节说明写入侧治理(当前 §3.3 只讲了蒸馏产出结论,没讲产出之后如何合并)
- `Design-claude-tag.md §3.8` 的跨频道授权描述按 Phase 4 的实际设计改写
