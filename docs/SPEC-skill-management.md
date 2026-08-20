# SPEC：Skill 管理功能

> 状态：已实现
> 日期：2026-08-20
> 范围：前端（React 管理控制台）+ 后端（FastAPI admin API + agent 运行时）

## 1. 背景与目标

teamai 已有 `TagTemplate`：用户在频道里打 `/名字`，把 role + instruction + output_style 注入系统提示词。它解决的是「人知道该用哪套指令」的场景。

**目标**：让管理员维护一批「做事规范」，由**模型自己**判断当前任务该用哪一套，而不必依赖用户记住命令。

**与 tag 的分工是触发方式，不是内容**。tag 由人触发，一次只可能有一个，正文直接进系统提示词；skill 由模型触发，可同时启用多个，正文只在被载入时才进上下文。两者并存、互不替代 —— 这也是本功能不做成「tag 的增强」的原因。

## 2. 需求决策（用户已拍板）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 触发机制 | **渐进式披露，模型自主选** | 系统提示词只常驻 `name: description`，token 成本与 skill 数量脱钩；且与 tag 语义正交 |
| 配置作用域 | **全局定义 + 按频道启用** | 「写周报的规范」天然要跨频道复用，按频道存会分裂成多份副本各自漂移 |
| 工具关联 | **不做，skill 只是指令文本** | 权限保持单一来源（`policy.allowed_tools`），skill 正文可以提到工具但不改变授权 |
| 捆绑文件 | **清单 + 第二个工具读取** | 内容内联进 `load_skill` 会让「带 3 个文档的 skill」每次载入都付全部文档的代价 |
| 文件类型 | **只存文本** | 模型读不了二进制，存进来只占空间；入库做 UTF-8 校验 |
| 文件执行 | **只读，不执行** | 脚本对模型也只是可读源码。真要执行需要沙箱、资源限额、超时与凭据隔离，是独立决策 |

## 3. 三级渐进式披露

这是整个设计的核心。每级只付上一级点名了的代价：

| 级别 | 载体 | 内容 | 成本 |
|---|---|---|---|
| 1 | 系统提示词 | `- name: description` | 常驻，每 skill 约 30~50 token |
| 2 | `load_skill(name)` | 正文 + **文件清单**（路径、大小、用途） | 只有被载入的那个才付 |
| 3 | `read_skill_file(skill, path)` | 某个文件的内容 | 只有被点名的那个文件才付 |

第 2 级只给清单是关键。若把文件内容一并内联，第 1 级好不容易省下的东西会在第 2 级又花掉。

`description` 因此是整个模型里最要紧的字段：它是模型判断「这件事该不该用这个 skill」的唯一依据，且每次调用都常驻。写成「审查 PR」不够（无法判断适用边界），要写成「按团队 Go 规范审查 PR，产出分级问题清单」。后端限长 200 字，把「详细步骤」逼进正文。

## 4. 领域模型

`domain/models/skill.py`：

```
Skill:
  id / name（全局唯一，^[a-z0-9-]+$）/ description（≤200 字）
  content: str            # Markdown 散文，长度不限
  enabled: bool           # 全局停用开关
  files: list[SkillFile]  # 仓储读取时一并装上
  created_at / updated_at

SkillFile:
  id / skill_id / path（同 skill 内唯一）/ description / content
  size_bytes: int         # UTF-8 字节数（派生）
```

`name` 的字符约束是因为模型要在 `load_skill(name)` 里原样打出这个名字：大小写混排与空格会让它抄错，而抄错就是一次无谓的工具往返。

`FILE_MAX_BYTES = 64 KB` 不是随手取的数，它是「文件预加载进 ContextBundle」这个设计得以成立的前提 —— 见 §6。

`is_safe_path()` 拦四类：`..` 段（目录穿越）、绝对路径、尾随斜杠、空串。path 目前只是库里的标识符，但日后若把文件落到磁盘（导出、给沙箱挂载），带 `../` 的 path 就成了穿越。在入库处拦比事后审计便宜。

## 5. 数据模型

三张表（`infrastructure/orm/skill.py`，迁移 `d4a7b2e9f150` + `e6c1f4a8b920`）：

- `skills`：全局库。`name` 唯一 —— 模型照名字调工具，重名会让「载入哪一个」取决于查询顺序
- `channel_skills`：频道 ↔ skill 关联。`(channel_instance_id, skill_id)` 复合主键 + 唯一约束，防覆盖式写入中途重试攒出重复行（表现是清单里同一 skill 出现两遍）
- `skill_files`：附带文件，内容存 Text 列。`(skill_id, path)` 唯一

**不设外键**（对齐本项目其余表，`channel_instance_id` 在各表里都是裸字符串）。删 skill 时的级联清理由 `SQLSkillRepository.delete` 显式做两件事：清关联行、清文件。漏了不会报错 —— 关联行成孤儿、文件永久占库且无从发现。

**读方法一律带文件**，不给「要不要带」的开关：agent 侧必须带（见 §6），管理页也要显示文件列表。留一个开关就意味着有一条「忘了带」的路径，而它的表现是模型看不到文件清单 —— 没有报错，只是能力静默缺失。`_with_files` 用一条 `IN` 查询批量取，避免 N+1（这个方法在每次 agent run 上都会被调用）。

## 6. 运行时接入：两个 per-run 工具

`load_skill` 与 `read_skill_file` 与其余工具（github / monitoring / MCP）有两点不同：

**按 run 构造，不进全局注册表。** 其余工具是启动时注册一次的全局单例，而这两个闭包捕获「本频道启用了哪些 skill」—— 那是 per-run 的。`ToolRegistry.for_channel` 在每次裁剪工具集时新建它们。这是安全的：`PydanticAIGateway` 每次 run 都新建 `Agent`，工具对象不跨 run 复用。存进 `self._tools` 会让下一个频道拿到上一个频道的技能（跨频道信息泄漏，已有测试锁住）。

**闭包里带完整数据，不带仓储。** 工具执行发生在 agent run 进行中，此时去查库会复用组合根那个共享 `AsyncSession`，而它不允许并发使用 —— 这类故障的完整描述见 `container.open_job_scope` 的文档，而且它会被 run 的顶层兜底吞成一句错误文本，很难定位。所以正文与文件在组装 `ContextBundle` 时就已取回，工具内只是内存查表。

代价是每次 run 都把本频道全部启用 skill 的文件读进内存，即便一个都没被载入。这是 DB 读而非 token 支出，且总量由 `FILE_MAX_BYTES` × 文件数兜住。若日后 skill 规模上来，正确的做法是给工具一个独立的 session 工厂做懒加载（`MemoryProjector` 已有这个先例），而不是放开上限。

**`load_skill` 不受 `allowed_tools` 白名单管制。** 频道启用某个 skill 这个动作本身就是授权；再要求管理员去策略页补一条才生效，等于同一件事配两处，漏配的表现是「skill 明明启用了，模型却说没有这个能力」。`read_skill_file` 只在至少有一个 skill 带文件时才挂 —— 一个永远返回「没有这个文件」的工具会占着模型的注意力，且诱导它去猜路径。

**返回值不套 `ok()` 的 JSON 包装**，这是本项目工具返回值的一处有意例外。其余工具返回的是**数据**（issue 列表、指标值），JSON 便于解析；这两个返回的是**要照着做的散文**，JSON 编码会把几千字 Markdown 里的每个换行变成 `\n` 字面量，既多花 token 又更难读。

名字/路径写错走 `ModelRetry` 而非 `fail()`：清单就在模型的上下文里，它能自己改对，把有效取值回灌给它即可。技能名错与路径错分开给提示 —— 合成一句「找不到」会让模型不知道该改哪个参数。

## 7. 审计

复用 `AuditAction.POLICY_CHANGE` + `detail.event`（`skill_create` / `skill_update` / `skill_delete` / `skill_file_*` / `channel_skills_set`），**不新增枚举成员**。

理由是 `audit_logs.action` 在 Postgres 上是原生枚举类型，加成员必须配一条 `ALTER TYPE ... ADD VALUE` 迁移，漏了会让**已升级的库**在写审计时抛 `InvalidTextRepresentationError` —— 这是真实发生过的故障，背景见 `tests/unit/test_enum_migrations.py`。tag 也是这个先例。

全局资源的变更没有频道可归属，记在 `GLOBAL_SCOPE = "global"` 下（定义在 `domain/models/audit.py`，读写两侧共用）。用固定串而非留空的两个理由：审计表里要能一眼看出「这条不是某个频道的事」；按频道查审计时不会把全局变更混进任意一个频道的流水里。与真实频道 id 不冲突 —— 那些由 `gen_id("ch")` 生成，形如 `ch_<26 位 ULID>`。

配套加了 `GET /api/audit/global`，控制台审计页据此多一档「全局变更」。给专门端点而非让前端打 `/channels/global/audit`：哨兵值是后端的内部约定，泄到前端就成了两处各存一份。

## 8. Admin API

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/skills` | 全局库（带正文 + 文件**摘要**） |
| POST | `/skills` | 创建（name 字符校验、description 必填限长、重名 409） |
| PUT | `/skills/{id}` | 更新（只改传入字段；允许改名） |
| DELETE | `/skills/{id}` | 删除（级联清文件与频道关联） |
| GET | `/skills/{id}/files/{fid}` | 单个文件，**带内容** |
| POST | `/skills/{id}/files` | 新建文件（路径校验、64 KB 上限、同 skill 内重复 409） |
| PUT | `/skills/{id}/files/{fid}` | 更新文件 |
| DELETE | `/skills/{id}/files/{fid}` | 删文件 |
| GET | `/channels/{cid}/skills` | 全局库摘要 + 该频道勾选状态 |
| PUT | `/channels/{cid}/skills` | 覆盖式设置启用集合 |

几处有意的取舍：

**列表带正文但文件只给摘要。** 管理页要直接编辑正文，故必须带；而每文件上限 64 KB，列表里全带上会让响应膨胀到 64 KB × 文件数 × 技能数。编辑单个文件时再单取。

**频道页一次返回库 + 勾选。** 分两次取会在「另一人正在增删技能」时让勾选指向不存在的行。

**`enabled_ids` 不过滤全局 enabled。** 它答的是「这个频道勾了哪些」。过滤掉的话，管理员全局停用一个 skill 后再打开频道页会看到勾选被凭空取消，以为关联关系丢了 —— 而库里那行还在，重新勾一次是空操作。前端据 `skills[].enabled` 显示成「已全局停用」而不是把勾去掉。

**`PUT /channels/{cid}/skills` 对不存在的 id 静默丢弃**，不报 422。管理页的勾选基于它上一次拉到的列表，期间有人删了某个 skill 时提交里就会有幽灵 id；为此报错会让用户面对一个自己无法理解也无法修正的错误（他勾的东西看着还在页面上），返回实际生效的集合则让前端刷新后自然收敛。

**允许改 name**（与 MCP server 锁死 name 相反）：模型每次都从当前清单读名字，不存在「白名单里残留旧名」的问题。

**文件大小按 UTF-8 字节校验**，且 `size_bytes` 由后端算并回显 —— 前端按字符数算会与上限对不上（一个汉字 3 字节），表现是「前端认为没超、后端拒掉」。

## 9. 前端

- `/skills` → `SkillPage`：全局库 CRUD。作为**全局资源**与「全部频道」同级，不在「当前频道」组里
- `SkillFileDrawer`：附带文件管理（抽屉 + 编辑弹窗）
- `/channels/:id/skills` → `ChannelSkillPage`：勾选启用，统一保存
- 审计页多一档「全局变更」，走 URL（`?scope=global`）而非组件 state —— 与 `InteractionPage` 的 `?task=` 同一套做法，刷新与分享链接都能回到同一视图；只认 `global`，拼错的值回落到频道视图

频道页挂一条提示：技能不需要在权限策略里额外授权，但技能正文里提到的**工具**仍受策略管制。这两件事很容易被混为一谈。

## 10. 边界与已知限制

- **不做工具声明**：skill 不能声明 `required_tools`，权限单一来源仍是 `policy.allowed_tools`
- **文件只读**：不执行脚本。真要执行需要沙箱、资源限额、超时与凭据隔离
- **只存文本**：不支持二进制文件
- **单文件 64 KB**：这是预加载预算，不是存储限制。要放更大的东西应该让 agent 去拉取，而不是捆在 skill 里
- **载入无留痕**：审计要写库，会撞上 §6 的同一个 session 问题。交互记录里只能看到「本次挂了哪些可选项」（`context_refs.skills`），不是「模型实际载入了哪几个」。排查「为什么没用某个技能」时，这个字段能区分「它没挂上」与「挂上了但模型判断不相关」，后者要看响应正文里的工具往返
- **改正文不留版本历史**：原地覆盖
- **没有「强制载入」开关**：那等价于 tag，用 tag

## 11. 验收标准

1. 管理员建 skill 后在频道启用，系统提示词只出现 name + description，不含正文与文件清单 ✅
2. 模型判断相关时调 `load_skill` 拿到正文 + 文件清单（不含文件内容）✅
3. 模型调 `read_skill_file` 拿到文件内容 ✅
4. 名字/路径打错收到 `ModelRetry` 提示而非 run 失败 ✅
5. 未启用的频道拿不到；B 频道载不到 A 频道的技能 ✅
6. 全局停用后所有频道立刻失效，但勾选记录保留 ✅
7. 删 skill 清掉文件与所有频道的启用记录 ✅
8. 改动无需重启 worker（每次 run 从库里读）✅
9. 超 64 KB 的文件被拒，且按字节而非字符判定 ✅
10. 前后端检查全绿：`make check` + `npm run check` ✅
