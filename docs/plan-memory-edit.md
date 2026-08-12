> **已实施完毕，本文保留作决策记录。** 落地结果与实施中的新发现见
> `docs/Design-conversation-context.md` §6.5 —— 那里是长期维护的正文，本文只是当时的施工计划。
>
> 与计划的三处偏差：
> 1. 实施中发现 `upsert` 传 dict 导致**向量写入从未成功过**（计划里没有这一条，它是第 1 步动手后才暴露的）。
> 2. 发现 `auditaction` 枚举漏迁移，导致**已进主干的记忆蒸馏在任何已升级的库上都失败**。补了迁移 + 一条静态守卫。
> 3. 前端范围比计划大：除编辑按钮外还加了「产生方式」与「索引」两列，并补了 `memory_distill` / `memory_edit` 两个漏掉的审计动作映射。

# 修复计划：记忆的删除与编辑

## 已核实的三个问题

**一、删除不清向量索引。** `MemoryService.delete()` 只删 Postgres 行；`VectorStore` 协议本身只有 `upsert` / `query`，没有 delete。已删记忆的向量留在 Qdrant 里，检索仍会命中其 id，随后 `_repo.get(eid)` 返回 `None` 被过滤 —— 不会把已删内容喂给模型，但白占 top_k 名额。删得多了检索质量静默下降。这个缺陷在改造前不可见（向量路径是死代码），接上 embedder 后成为真问题。

**二、编辑能力不存在。** `MemoryRepository` 只有 `store` / `list_by_channel` / `get` / `delete` / `set_preference` / `list_preferences`，没有 `update`；Admin 也没有 PATCH 端点。改内容只能删了重建，代价是 id 变了、`created_at` 重置、审计里是「删一条 + 加一条」而非「改了一条」。

**三、`embedding_ref` 字段从未被赋值。** `MemoryEntry` 声明了它，mapper 两侧都在传，但没有任何代码写入 —— `_embed_if_available` 调 `vector.upsert()` 后不回填。后果是 `scripts/cleanup_chat_memories.py` 里 `embedding_ref IS NULL` 这个条件恒为真，注释声称的「有向量引用的是经过正规写入路径的，留着」是不存在的机制。

## 修复顺序（三步，可分别 review）

### 第 1 步：删除时清向量（修既有缺陷，与新增能力解耦）

- `VectorStore` 协议加 `delete(entry_id: str) -> None`。
- `_InMemoryVectorStore.delete`：`self._vectors.pop(entry_id, None)`。
- `QdrantVectorStore.delete`：**按 payload 里的 `entry_id` 过滤删除**，而不是按 `uuid5(NAMESPACE_DNS, entry_id)` 反推 point id。

  两种都可行（已核实 `client.delete` 的 `points_selector` 同时接受 id 列表与 `Filter`），选过滤的理由是它不依赖 id 推导：按 point id 删要求删除侧与 `upsert` 侧的推导逐字一致，一旦哪天改了 id 方案而漏改另一边，删除会静默变成 no-op —— 恰好是本条要修的那类缺陷。过滤方式慢一点，但单点删除在这个量级上无所谓。

  `upsert` 已经把 `entry_id` 写进 payload 了，所以不需要额外改写入侧。
- `MemoryService.delete()` 在 `_repo.delete()` 之后调 `vector.delete()`，失败只 `logger.warning` 不抛：与 `_embed_if_available` 的降级取舍一致，向量库不可用不该让删除失败（Postgres 行已删，那才是权威源）。

顺带修 §3：`_embed_if_available` 在 upsert 成功后回填 `embedding_ref`（存 Qdrant 里的 point id），并 `_repo.update()` 落库。这让「哪些记忆已建索引」变得可查，清理脚本的那个条件也才名副其实。因为要回填就需要 `update`，所以这一步与第 2 步的仓储改动有依赖 —— 实施时把 `update` 一并做掉，但 `edit()` 与端点留到第 2 步。

### 第 2 步：编辑能力

- `MemoryRepository` 加 `update(entry) -> None`；SQL 实现走 `session.merge` + `commit`（同 `SQLBudgetRepository.upsert`）。**必须复用原 id**，否则 merge 是 INSERT 而非 UPDATE —— `budget_configure` 上踩过这个坑，注释指回去。
- `MemoryService.edit(entry_id, *, content=None, type=None, actor=None) -> MemoryEntry | None`：
  - 条目不存在返回 `None`（调用方转 404）。
  - 内容变了要**重算向量**：旧向量对应旧文本，不重算等于检索按旧内容命中新条目。
  - 重算失败时**删掉旧向量**而不是留着 —— 留着比没有更糟：会继续按旧内容命中。这是本次最容易漏的失败模式。
  - `id` / `created_at` / `visibility` 不变。新审计动作 `MEMORY_EDIT`，`detail` 只带旧内容摘要（前 50 字）与新旧 type，不放全文（审计表不该变成第二份内容副本）。
- `PATCH /api/memories/{entry_id}`，接受 `content` 与 `type`。两者都缺 → 400；`type` 非法值 → 400（立即拒绝，不静默按背景知识收下 —— 那是蒸馏解析的宽容策略，人工输入该严格）。
- **不允许改 `visibility`**：把 `private` 改成 `channel` 等于把本不该进频道记忆的内容放出去，属权限变更而非内容编辑，应走独立授权路径。这条写进端点注释。

### 第 3 步：`source` 字段（待你确认，见下）

`MemorySource` 枚举 `DISTILLED` / `MANUAL` / `EDITED`，`MemoryEntry` 加字段，一次迁移（三步走：加可空列 → 回填 → 收紧 NOT NULL，同 `budget_quotas` 那次）。`store()` 默认 `MANUAL`，distiller 传 `DISTILLED`，`edit()` 把 `DISTILLED` 改成 `EDITED`、`MANUAL` 保持不变。序列化器输出该字段。

## 测试

新增约 20 条，重点在几个易漏的失败模式而非 CRUD 本身：

- Qdrant 删除传的是按 `entry_id` 的 Filter（用假 client 断言 `points_selector` 的形状），且 upsert→delete→query 后不再命中。
- 删除时向量库不可用 → Postgres 行仍被删、只记 warning。
- 编辑后向量被重算（假 vector store 记录 upsert 次数与内容）。
- **编辑时 embedding 失败 → 旧向量被删而非保留。**
- `id` / `created_at` / `visibility` 在编辑后不变。
- 审计 `detail` 不含全文。
- `_builds_dml` 那条守卫仍绿（`update` 走 merge 不走 DML，不受影响）。

真依赖验证：迁移对真 Postgres 跑 upgrade → downgrade → upgrade；起真 web 进程打一次 PATCH，确认落库与向量都变了。

## 第 4 步：前端（`web/src/routes/MemoryPage.tsx`）

现状核实过了：表格有 内容 / 类型 / 来源 / ID / 写入时间 五列，操作列只有一个删除按钮（带 Popconfirm），另有「新增记忆」的 Modal + Form。

- `api/index.ts` 的 `memoryApi` 加 `update(entryId, body)` 打 PATCH；`types.ts` 的 `Memory` 加 `source` 字段、`AuditAction` 联合类型加 `'memory_edit'` 与 `'memory_distill'`（后者是上轮改造漏的 —— 前端类型与 `serializers.py` 逐字段对应是这个项目的纪律，蒸馏审计现在会落一个前端不认识的动作值）。
- 操作列加编辑按钮，复用现有 Modal（`content` + `type` 两个字段，`type` 用已有的 `MEMORY_TYPE_OPTIONS`）。改成「新增/编辑」双模式比另起一个 Modal 省一半代码。
- **来源列有命名冲突要处理**：现有「来源」列渲染的是 `source_user_id`，无值时显示「系统」—— 于是蒸馏产出与管理台人工写入长得一模一样。这正是 `source` 字段要填的空。建议现有列改叫「来源用户」，新增一列「产生方式」渲染 `source`。
- 跑 `npm run smoke` 验渲染。注意它只覆盖首帧、不跑 `useEffect`，所以编辑 Modal 的交互它验不到 —— 这一步只能靠人点一遍。

## 一个要你定的点

**`source` 字段做不做？** 我倾向做，而且核实前端之后倾向更强了：现在「来源」列对蒸馏记忆和管理台写入都显示「系统」，两者在界面上不可区分。而这张表的内容直接影响机器人的回答，「这句话是谁写的」在出问题时是第一个要问的 —— 只靠审计日志得翻流水。代价是一次迁移加一列。

你说不做，我就删掉第 3 步、第 4 步只留编辑按钮与 API，第 1、2 步不受影响。
