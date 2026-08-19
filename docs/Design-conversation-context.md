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
| Agent 的线程上下文 | 当前线程最近 N 条，秒级新鲜 | 平台 API + Redis 自更新缓存 | 秒级 |
| 记忆蒸馏的输入 | 原文只是中间态，产物是结论 | Redis 滚动窗口 | 分钟级 |
| Ambient 规则（error_spike 等） | 滚动窗口聚合 | 同上（复用窗口） | 分钟级 |
| 审计 / 成本 / 复现 / 评测语料 | 机器人自己的输入输出与提示词来源 | **Postgres** | 可配，默认 90 天 |

### 3.1 第一层：原始消息 —— 平台是 system of record

新增 `ThreadReader` 端口（`domain/ports/conversation.py`），与 `MessagePublisher` 分开而不是给后者加方法：一个是出向发送、一个是入向读取，合并会让「只需发送」的 worker 也被迫实现读取。两平台各自实现，外面套一层 `CachedThreadReader` 按 `platform:channel:thread_ref` 在 Redis 缓存（默认 45 秒）—— 同一线程内连续几条消息不必反复打平台 API。

`ConversationService` 在 `router._run_agent()` 里填 `ContextBundle.thread_history`，已有的 `compact()` 直接复用。拉取失败一律降级为空列表：没有上下文的回答仍然可用，为拉不到历史而让整个任务失败是不划算的。

**缓存必须能自更新，只读缓存在这里是错的。** 线程历史不是不可变对象 —— 每来一条消息它就变了。最初的实现把「线程最近 N 条」当快照整体缓存、只靠 TTL 过期重建，于是 TTL 窗口内的第二次读取拿到的是过期快照：

```
t=0   用户 @ 机器人「列几个方案」   → 打 API，写快照 A（此时还没有机器人的回复）
t=3   机器人回复，列了三个方案      → 平台上有了，缓存里没有
t=15  用户 @ 机器人「第二个细化下」 → 命中快照 A，机器人不知道「第二个」指什么
```

45 秒恰好落在最坏区间：人类追问的间隔通常是 10 到 30 秒，几乎必然命中。丢的是两样东西 —— 机器人上一轮自己的回复（`is_bot` 字段的全部意义就在于让模型区分它），以及其间别人插的话。更根本的是，**只有被 @ 的消息才会拉历史**（非 @ 消息走 observe 进滚动窗口，不碰 reader），所以缓存想省的调用只发生在连续 @ 机器人时 —— 那正是多轮对话。省配额的动机与最需要新鲜数据的时刻完全重合。

故 `CachedThreadReader` 同时实现 `ThreadHistorySink`：每条经手的消息 append 进缓存，缓存自己保持新鲜。四个设计取舍：

**数据结构从单个 JSON 串改为每线程一个 LIST。** 追加一条才能是一次 `RPUSHX`，不必读出整表、改完再写回 —— 后者在 web 与 worker 两个进程并发追加时会互相覆盖。

**用 `RPUSHX` 而非 `RPUSH`：键不存在时不建键。** 否则一条追加就凭空造出一段「只有一条消息的线程历史」，下次读取命中它、把真实历史整个挡住 —— 比缓存陈旧严重得多。键不存在意味着下次读取本就会穿透到平台拿全量，那才是正确的补法。

**追加不 EXPIRE。** Redis 的 TTL 是键的属性，`RPUSHX` / `LTRIM` 这类改值命令不重置它（只有 `SET` 那样的整键覆盖会）。这正是想要的：TTL 的作用从「保证新鲜」变成「兜底纠错」—— 追加过程中丢的、重的、乱序的，都在下个窗口被平台的权威数据抹平，不会永久驻留。

**键里不再含 limit。** 原先含它是为了防「要 30 条的调用拿到只有 10 条的缓存」，但那样同一线程会有多份互不相干的缓存，`note()` 无从知道该往哪几个键追加（SCAN 一遍键空间太贵）。改为缓存固定按 `cache_limit`（取 `conversation_history_limit`）存、读取时切出末尾 `limit` 条，一份缓存服务所有不超过容量的请求；`limit > cache_limit` 的请求绕过缓存直取平台，不拿短缓存充数。

回填的调用点收在两处，都在 router：入向消息在 `route()` 分支之前记（非 @ 消息同样在线程里，平台拉取会返回它们，只记 @ 消息会让缓存与平台不一致）；出向回复在 `_respond()` 里记，它是同步链路与 worker 链路回帖文案的唯一汇聚点。判据是「这条文案会不会发到平台」—— 会发的都记（含「任务已受理」与失败文案），`_observe` 的返回文案不记（Slack 丢弃返回值、飞书只在 `is_mention` 时回复，那些字根本没发出去）。

两个已知的不完美，都是有意接受的：回填的是「即将发送」而非「已发送」，发送失败时缓存会多一条平台上不存在的消息（下个 TTL 窗口消失）—— 要拿真实发送结果就得把回填挪到三处调用点，漏一处就是静默不一致。以及私密会话仍然回填线程缓存：PRD §4.2 管的是「不进记忆」（跨会话留存），不是「不看当前对话」，单聊里机器人本就看得见这些话，而缓存是平台数据的秒级镜像、45 秒后即消失。

**`is_self` 必须是「本 bot」而不是「某个机器人」。** `ThreadMessage` 的这个字段决定 `render()` 输出 `AI:` 还是 `<author_id>:`，而团队频道里往往还有 CI 通知、告警机器人。字段原名 `is_bot`（语义是「某个机器人」），两个平台的实现也就都按字面写成了「有 `bot_id`」与「`sender_type` 是 `app`」—— 于是别的机器人的消息被渲染成 `AI:`，模型会以为那些话是自己上一轮说的，可能围绕别的机器人的输出继续往下答，或「承认」一个自己没做过的判断。飞书侧尤其隐蔽：`bot_open_id` 形参一直存在，但没有任何调用方传值（container 里是 `FeishuThreadReader()`），恒为空串，于是那个精确判定的 `and` 分支永远为假，只剩 `sender_type == "app"` 这半边在起作用。

改成严格判定：Slack 比对 `auth.test` 返回的 `bot_id` 与 `user_id`（经 `chat.postMessage` 发出的消息通常两个字段都有，只比一个会漏），飞书比对 `/open-apis/bot/v3/info` 的 `open_id`。两边都在首次拉取时取一次身份并缓存，失败也不重试（权限不足是稳定失败，重试只是每次白打一发）。读取器自己取而不依赖连接器传入，是因为 worker 进程根本不起连接器，而它同样要拉线程历史。

身份未知时 `is_self` 一律为假 —— **降级方向必须是「少标」而不是「错标」**：自己的回复退化成普通参与者，模型至多少一点署名信息；而错标是把别人的话认领成自己的，那会让模型的自我认知出错。别的机器人不单独标记，按普通参与者渲染成 `<bot_id>: ...`；模型本来也不知道任何一个 ID 背后是人还是机器，多一档只在「想让模型知道这是机器输出」时才有价值，那是另一件事。

改名是修复的一部分而非顺手重构：留着 `is_bot` 这个名字，下一个人还会照字面再写错一遍。出向回填那条路径（`note_outbound`）的 `is_self=True` 按构造成立 —— 那条回复就是本进程发出去的，不必比对 id。

平台差异消化在各自实现里：

- **Slack**：`conversations_replies(channel, ts=thread_ref, limit)`，语义直接对应。
- **飞书**：没有「按 root message_id 拉整串」的接口。`im/v1/messages` 的 `container_id_type` 只接受 `chat` 与 `thread`，而 `thread` 要的是话题群的 `omt_` 前缀 thread_id，与我们持有的 root message_id（`om_` 前缀）不是一回事。故取 `container_id_type="chat"` 拉该会话最近一批消息，再按 `root_id == thread_ref or message_id == thread_ref` 在客户端过滤。代价是多拉一些消息后丢掉，换来的是不依赖话题群这个前提。

需要提前验证的风险：Slack 近年收紧了非 Marketplace 应用对 `conversations.history` 系接口的速率限制，具体配额取决于应用类型与上架状态。上线前用真实凭据压一轮。若配额确实紧，现在可以放心拉长 TTL —— 自更新之后 TTL 只影响「多久由平台数据校准一次」，不再影响历史的新鲜度，这正是加回填换来的余地。转向全量镜像仍然不是选项（理由见 §2）。

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

### 3.3.1 蒸馏产出之后：去重与取代

上面只说了「原文如何变成结论」，没说结论之间如何相处 —— 而这是第三层真正的质量瓶颈。

改造之初蒸馏只会追加：drain 窗口、喂模型、把产出逐条写库，输入里不含任何已有记忆。后果有两个，第二个是真问题。**重复堆积**：同一件事在三个窗口被提到就存三条，`top_k=5` 的名额被同一个事实占掉多个，检索质量随使用时长单调下降。**矛盾共存且无法裁决**:团队三月定「超时 3 秒」、六月改成 5 秒，库里两条并列,检索按语义相似度取 top_k 时两条相似度几乎一样,模型看到互相矛盾的上下文,而没有任何信号告诉它哪条是现行的。

现在蒸馏前先按整窗文本检索该频道已有的近似记忆(默认 10 条,`CANDIDATE_TOP_K`),连同新消息一起送给模型,由模型对每条产出给出动作:

| 动作 | 语义 | 落库 |
|---|---|---|
| `ADD` | 已有记忆里没有这件事 | 新写一条 |
| `UPDATE` | 有同一件事,但现在的说法取代了它 | 新写一条 + 给旧条目打 `superseded_by` / `superseded_at` |
| `NOOP` | 已经有了且说法一致 | 什么都不做,不计入产出条数 |

这套动作对齐 mem0 的 update 阶段(arXiv:2504.19413 §2.1,配置 s=10 相似记忆、轻量模型),但**不给 `DELETE`**:那篇论文里 DELETE 用于移除被新信息矛盾掉的记忆,而这里用 `superseded_by` 表达同一件事且可回溯 —— 删除不可逆,而「矛盾」的判断来自模型、可能是错的。真要删走人工路径。

**被取代的条目不物理删除**,只是不再进入检索。这与 mem0 的 graph 变体和 Zep 的取舍一致(两者都是 mark invalid rather than physically removing)。留着它才能回答「这条事实之前是什么、被什么取代」,而这正是排查「机器人为什么这么说」时要问的。

**几个刻意的宽容设计。** 模型引用候选用的是**列表序号**(1..N)而不是记忆 id:让模型输出 `mem_01K…` 这种 ULID 极易出错(编造、截断、张冠李戴),小整数几乎不会错,映射回真实 id 由调用方做。引用了不存在的编号时**降级为 ADD 而非丢弃** —— 内容本身可能有效,丢掉等于让这条知识彻底进不来,多一条重复的代价小得多。同一编号被取代两次时第二次也降级为 ADD,否则会形成 A→B→C 的链而中间那条 B 从未进入过检索。跨频道取代一律拒绝:模型可能编出别的频道的 id,而放行等于让 A 频道的蒸馏改写 B 频道的记忆。

**提示词里多一条约束:输出的多条之间也不能互相重复。** mem0 按消息对增量处理,天然逐条比对;本项目按窗口批量蒸馏,一次调用会同时产出多条,同一件事在窗口内被提到两次时模型可能输出两条近似结果。这个差异只能在提示词里补。

**写入侧兜不住的那部分,在读取侧兜。** 上面这套只在**蒸馏候选范围内**生效,而至少三条路会绕过它:旧记忆没排进语义 top-10(候选窗口是 `CANDIDATE_TOP_K = 10`,库越大漏得越多);向量不可用时 `find_similar` 回落成「最近 10 条」,三个月前那条矛盾记忆永远进不了候选;人工经 Admin API 写入完全不过冲突检查,`store()` 不做任何近似检索。这三条路留下的都是**两条并列现行而互相矛盾**的记忆,而它们语义相似度几乎相同、会一起进 `top_k`。所以 `ContextBundle.memory_context` 给每条渲染上写入日期(`- [2026-03-28] 网关重试超时设为 5 秒`),系统提示词的行为规范里给出裁决方向:说法不一致时以日期较晚的为准,并在回答里点出依据的日期与旧说法。**日期必须显式写出来,不能靠顺序暗示** —— 语义段按相似度排、回落段按时间倒序排,两者渲染出来完全一样,靠位置判断新旧在前一条路上就是错的。只给日期不给时刻:矛盾记忆的间隔通常在天到月,精确到秒纯属白耗 token。

这不是把裁决推给模型了事,而是承认写入侧的召回有天花板(mem0 同样有),读取侧是最后一道。代价只有每条记忆十来个 token,而它覆盖的是写入侧漏下来的全部冲突。

**ADD 偏向不靠「以后再合」撑着。** 蒸馏提示词曾写「拿不准时选 ADD —— 多一条重复可以后续再合」,而那个合并任务从未存在(`register_jobs` 里五个定时任务没有它)。这句话是误导:它让偏向看起来有下游兜底,实际是永久累积。现在措辞改成只讲真正的理由 —— **错误取代会作废一条正确的记忆,而多一条重复只是占掉检索名额,两者的代价不对等**。这个论证本身就足以支撑偏向,不需要那个不存在的承诺。

真要建合并任务,建议先量再建:字面重复主要在候选召回失败时形成,而那条路(向量不可用时候选退化成「最近 10 条」)已经由下面的可见性改动暴露出来了。先看重复到底涨不涨,涨才建。届时能自动合的只有**归一化后字面相同**的那部分(内容一样,取代旧的不丢信息);语义相似但措辞不同的不能无人裁决地合,理由同上。

**人工写入过冲突检查,由录入人裁决。** 上面三条绕过写入侧去重的路里,第三条(Admin API 不做任何近似检索)已经堵上:`POST /channels/{id}/memories` 写前调 `MemoryService.find_conflicts`,相似度超过 `memory_conflict_threshold`(默认 0.85,余弦)就返回 **409** 并带上候选列表,不写库;录入人在控制台选「取代第 k 条」(带 `supersede_id` 重发,走 `supersede()`)或「都不取代,并列写入」(带 `force`)。两个参数同时给报 400 —— 它们表达相反的意图,静默择一会让另一半无声丢掉。

**为什么是 409 而不是「写进去再警告」**:警告没人看,而这里要的正是让人当场决定。**为什么不自动取代**:凭一句待写入的话判不出「这是新版本」还是「另一件事」,而错误取代会作废一条正确的记忆 —— 理由与蒸馏侧不给 `DELETE` 相同。控制台默认不预选任何一项、选出之前确认按钮禁用:预选「取代最像的那条」等于替人做了那个判断,而这道界面存在的全部理由就是不替人做判断。

**向量不可用时退化为字面比对**,并在 409 body 里置 `degraded=true`、控制台明示「只能查字面重复」。默认装配就是 `NullEmbedder`,若此时静默放行,这道检查在默认部署下等于没做 —— 那正是本项目反复踩的那类缺陷。**这件事本身也做成可见的**:`build_embedder` 装不上时打 warning 并列全三层后果(语义检索关闭 / 冲突检查退化 / 蒸馏去重对旧记忆失效),`GET /api/embedding` 供控制台在记忆页挂降级提示,`teamai_embedder_available` 让告警规则挂得上去。三层里第三层最贵 —— 前两层是「这次回答差一点」,第三层是记忆库持续劣化,而它要几周才从回答质量上看出来,只靠一条启动期日志是看不住的。字面路径的 `score` 给 `null` 而不是编一个数,控制台据此显示「字面重复」而非一个假的百分比。归一化刻意**不含数字**:「超时 3 秒」与「超时 5 秒」的差别全在那个数字上,归一化掉它会把真正的矛盾判成字面相同。

**偏好不参与这道检查。** 偏好按 `should_embed` 不建向量,语义检查对它结构性无效 —— 走向量路径会查出零条,而那会被读成「没有冲突」,真相是「没能力查」。故显式跳过并记在 `docs/tasklist.md` 22.4,而不是让它看起来查过了。

**时序治理只做一维。** Zep 的双时间轴(arXiv:2501.13956 §2.1)分开记「事实在现实中何时成立」(`t_valid` / `t_invalid`)与「系统何时知道」(`t'_created` / `t'_expired`),边失效时把旧边的 `t_invalid` 设为新边的 `t_valid`。本项目蒸馏是近实时的(窗口满 20 条或静置 600s 即触发),`created_at` 与事实实际成立时间的偏差在分钟级,双时间轴的收益接近零,而代价是模型要额外从对话里抽取时间信息。故退化为:`created_at` 兼任 `t'_created`,`superseded_at` 兼任 `t_invalid`。若将来真要表达「某事实在某段区间内有效」,补一个 `valid_from` 即可,不必重构。

**不做知识图谱。** mem0 自己的数据:graph 变体只比 base 高约 2%,而代价是实体抽取 + 关系生成 + Neo4j。Zep 的大头收益在 LongMemEval(+18.5%,temporal reasoning)而非 DMR(+1.4%)—— 收益来自时序治理而非图结构。

### 3.4 顺带修掉的两处

**`list_by_channel` 加 `ORDER BY created_at DESC` 与 `LIMIT`。** 即使做了蒸馏，这个查询也不该没有上界。

**接上真实 embedder，并让 Qdrant 维度跟随它。** `infrastructure/vector.py` 里 collection 的 `size` 硬编码 384，而常用 embedding 模型的维度是 1536（OpenAI text-embedding-3-small）或 1024。硬编码 384 意味着一旦真接上 embedder，建 collection 就会与向量维度不匹配而报错 —— 这个 bug 此前被「embedder 从未注入」掩盖着。改为由 `Embedder.dimensions` 声明，vector store 建集合时读它。未配置 embedding 凭据时装 `NullEmbedder`，`query_for_context` 走按时间倒序的有界回落，行为退化但不再是全表扫描。

## 4. 数据流总览

```
IM 消息
  │
  ├─ 无论是否 @：note_inbound ──→ 线程缓存（RPUSHX，无缓存则跳过）
  │
  ├─ 非 @ ──→ [私聊?] ─是→ 丢弃（PRD §4.2）
  │            └─否→ Redis 滚动窗口 ──(worker 定时)──→ LLM 蒸馏 ──→ memory_entries + Qdrant
  │
  └─ @ ────→ router
              ├─ ThreadReader（平台 API + Redis 45s 自更新缓存）──→ thread_history
              ├─ MemoryService.query_for_context（Qdrant 向量检索）──→ memory_hits
              ├─ ContextBundle → compact() → AgentRuntime → LLM
              ├─ 落 agent_interactions（提示词 + 响应 + model_id + in/out tokens + context_refs）
              └─ _respond：note_outbound ──→ 线程缓存 ──→ 回帖（同步 say / worker publisher）
```

两个方向的回填让缓存在 TTL 窗口内始终等于「平台此刻会返回的内容」，TTL 到点时再由平台数据整体校准一次。

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

## 6.5 记忆的删除与编辑

上面的三层架构落地后，记忆的写入路径变了（蒸馏产出为主），但读写生命周期上还缺两块，且暴露出三个此前被掩盖的缺陷。

### 缺陷一：删除不清向量索引

`MemoryService.delete()` 只删 Postgres 行，而 `VectorStore` 协议压根没有 delete。已删记忆的向量留在库里继续被检索命中，随后因取不到实体被过滤掉 —— 不泄露已删内容，但白占 top_k 名额，删得多了检索质量静默下降。

修法是给协议加 `delete(entry_id)`，Qdrant 侧**按 payload 里的 `entry_id` 过滤删除**，而不是按 `uuid5(entry_id)` 反推 point id。两种都可行，选过滤是因为它不依赖 id 推导：按 point id 删要求删除侧与 upsert 侧的推导逐字一致，哪天改了映射方案却漏改一边，删除会静默变成 no-op —— 恰好是本条要修的那类缺陷。删向量失败只告警不抛：Postgres 行是权威源，它已经删了，为向量库故障而报错会让用户以为没删掉。

### 缺陷二：`upsert` 传 dict，向量写入从未成功过

qdrant-client 的 `upload_points` 会直接取 `record.id`，传 dict 会抛 `AttributeError: 'dict' object has no attribute 'id'`。而调用方 `_embed_if_available` 把异常吞成一条 warning —— 于是**向量写入从未成功过，且不报错**。

这个缺陷此前被「embedder 从未注入、这段代码根本没被执行」掩盖着。改为 `PointStruct` 后已对真 Qdrant 验证：写入、检索、删除全通，且 `_client is not None` 证明走的是真服务而非静默降级到内存兜底（后者会让验证假通过）。

顺带记一个 SDK 的不一致：`delete` 的 `points_selector` **只接受模型对象**（传 dict 抛 `ValueError: Unsupported points selector type`），而同一个 client 的 `query_points(query_filter=...)` **接受 dict**。两者形态不同不是本项目的疏漏，代码里已注明。

### 缺陷三：`embedding_ref` 从未被赋值

`MemoryEntry` 声明了它、mapper 两侧都在传，但没有任何代码写入 —— `upsert` 成功后不回填。于是 `scripts/cleanup_chat_memories.py` 里 `embedding_ref IS NULL` 恒为真，那条注释声称的「有向量引用的是经过正规写入路径的」是不存在的机制。

修法是让 `upsert` 返回可回填的引用（而不是让用例层自己推导 point id —— 映射规则是基础设施层的实现细节），`_embed_if_available` 回填后 `update` 落库。回填之后「哪些记忆已建索引」才真的可查，控制台也据此加了一列「索引」。

### 新增能力：编辑

`MemoryRepository.update()` + `MemoryService.edit()` + `PATCH /api/memories/{entry_id}`，可改 `content` 与 `type`。

与「删一条 + 建一条」的区别不只是省一次调用：那样做 id 会变、`created_at` 被重置，审计里也看不出是同一条的演进。

**改内容必须重算向量**，且重算失败时要**删掉旧向量而不是留着** —— 留着比没有索引更糟：检索会持续按已被改掉的内容命中它。这个失败模式只在编辑路径上存在，删除路径没有，是本次最容易漏的一点。

有意**不支持改 `visibility`**：把 `private` 改成 `channel` 等于把本不该进频道记忆的内容放出去，属权限变更而非内容编辑，应走独立的授权路径。

`type` 的非法值立即 400 而不是静默按背景知识收下 —— 那是蒸馏解析（`_parse_entries`）的宽容策略，理由是模型输出不可控、丢内容比分错类更糟；而这边是人在调接口，静默改成别的类型只会让人以为自己设对了。

### 新增字段：`source`

`MemorySource` 枚举 `DISTILLED` / `MANUAL` / `EDITED`。

为什么不用已有的 `source_user_id` 表达：那一列答的是「哪个用户的话变成了这条」，而蒸馏产出与管理台人工写入的 `source_user_id` **都是 NULL** —— 控制台里两者显示成同一个「系统」，完全不可区分。而这张表的内容直接影响机器人的回答，「这句话是谁写的」是出问题时第一个要问的，只靠审计流水回溯太绕。

`EDITED` 不并入 `MANUAL`：区分「人写的」与「模型写了人改的」，后者的原始判断仍来自模型，排查时含义不同。迁移把已有行回填为 `DISTILLED` 而非 `MANUAL` —— 改造前 router 把每条非 @ 消息直接塞进这张表，那些行确实都是系统自动写入的，回填成 MANUAL 会把「系统攒的聊天碎片」错标成「人工录入的知识」，恰好反了。

### 连带修复：`auditaction` 枚举漏迁移（已进主干的缺陷）

上一轮改造给 `AuditAction` 加 `MEMORY_DISTILL` 时只改了 Python 枚举，**没有迁移 Postgres 的 `auditaction` 类型**。于是记忆蒸馏在任何已存在的库上都是失败的 —— 写审计时 asyncpg 抛 `InvalidTextRepresentationError`，异常冒泡到 `MemoryDistiller` 的按频道兜底，整个频道被记成蒸馏失败。

为什么此前全部测试都没抓到：application 层用内存替身，根本不碰数据库；仓储层的真 SQL 测试跑在 SQLite 上，那里 `sa.Enum` 落成 VARCHAR + CHECK 且 CHECK 按当前 Python 定义生成，插什么都过；而 `init_db()` 的 `create_all` 在新库上按当前定义建出完整枚举，所以本地开发与 CI 的新库全都正常 —— **只有升级过的库会炸**。这是一次对真 Postgres 打请求才暴露出来的缺陷。

除补迁移外，新增 `tests/unit/test_enum_migrations.py` 静态兜住这一类：遍历 ORM metadata 里所有落库的枚举列，要求每个取值都在某个迁移文件里出现过。这条守卫本身也验证过 —— 临时给 `AuditAction` 加一个未迁移的值，它确实变红。

该迁移单向不可回退：Postgres 没有 `ALTER TYPE ... DROP VALUE`，真要移除得重建类型并处理已写入的审计行（删掉？改成别的动作？两者都是篡改审计），代价与收益不成比例。

## 7. 落地状态

已完成并验证：

| 项 | 验证方式 |
|---|---|
| 三层架构全部代码 | 642 个单测通过，`make lint` 干净 |
| 写入侧去重与取代（§3.3.1） | 659 个单测通过，`make lint` 干净。七个针对性用例：同一事实跨三个窗口只存一条、候选带编号进提示词、事实变化时旧条目被取代而非并列、被取代的不再进检索但显式可查、引用不存在的编号降级为新增、同一编号二次取代降级为新增、全 NOOP 的一轮不记作有产出 |
| `superseded_by` 迁移与删 `visibility` | 对真 Postgres 跑 upgrade → downgrade → upgrade：列与索引核对无误；`visibility` 枚举类型在 downgrade 后无残留（`pg_type` 查为 0），回滚时回填为 `'channel'` 与升级前一致 |
| `agent_interactions` 迁移 | 对真 Postgres 跑 upgrade → downgrade → upgrade，表结构与四个索引核对无误，枚举类型无残留 |
| Admin 交互记录端点 | 起真 web 进程，写入一条后经 API 读回，全部字段（含 `context_refs` 与分拆 token）往返正确 |
| Redis 滚动窗口 | 对真 Redis 验证 append / due_channels / drain 语义与内存实现一致，ZSET 的 NX 首写时间不被后续消息刷新，drain 后 list 与 index 均无残留 |
| 线程缓存自更新 | `scripts/verify_thread_cache.py` 对真 Redis 验证七组语义：RPUSHX 不建键、写快照设 TTL、追加不续期（44s→44s 仍在原窗口倒数）、机器人回复在窗口内可见且全程只打一次平台、LTRIM 按容量截尾、小 limit 从缓存切尾、超容量绕过缓存、TTL 到点后本地追加被平台数据抹平 |
| 清理脚本 | 对真 Postgres 造数据跑 dry-run 与 `--apply`，确认只删碎片、保留真实知识 |
| 记忆删除/编辑（§6.5） | 对真 Qdrant 验 upsert→query→delete 全通且未静默降级；对真 Postgres 打 POST/PATCH，验字段往返、404/400 边界、审计区分 `content_changed` |
| `source` 与 `auditaction` 两条迁移 | 各自 upgrade→downgrade→upgrade 往返（`source` 验回填为 DISTILLED；`auditaction` 单向，已注明理由） |
| 控制台 | `npx tsc --noEmit` 干净、`npm run build` 通过、`npm run smoke` 12 个页面全渲染 |

未做，且都是独立决策：

- **上下文摘要化。** `compact()` 现在是「丢弃最旧 + 说明丢了几条」，真正的摘要化需要额外一次 LLM 调用。`context_summary_threshold` 配置项与形参保留着，接上时不必改调用点。
- **`error_spike` / `deploy_status` 两条 ambient 规则。** 前者现在有数据源了（滚动窗口），但它需要的是「按错误关键词聚合计数」而非「蒸馏成记忆」，得给窗口加一个只读不清空的取数方法；后者仍缺对外 webhook 入口。
- **`agent_interactions` 分区。** 默认保留期 90 天下单表规模有限，等真实数据量证明需要再按 `created_at` 做 RANGE 分区。
- **Slack 速率限制的实测。** 见 §3.1，需要真实凭据压一轮才能定 `conversation_cache_ttl_seconds` 的最终取值。缓存自更新之后这个取值不再影响历史新鲜度，只影响多久由平台数据校准一次，调整的余地大了很多。
