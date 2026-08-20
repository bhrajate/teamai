# SPEC：工具审批（HITL）

> 状态：设计中（待评审）
> 日期：2026-08-20
> 范围：`PermissionPolicy` 扩字段 + gateway 挂 `DeferredToolRequests` + 待批载荷存储 + router 审批分支 + 巡检超时兜底 + 管理控制台待批列表

## 1. 需求

### 1.1 问题

`github.create_pr` 是模型说调就调。一个人 @ 机器人让它提 PR，改的是**全团队**的仓库，而当前没有任何确认环节。

`docs/SPEC-agent-checkpoint.md` §12 还记着一条已知风险：续跑时模型可能重复决定调 `create_pr`。当时写的解法是「幂等键」—— 不完全对。审批是更直接的解法：重复的那次会再弹一次审批，人一眼看出「这个 PR 已经建过了」。幂等键要求每个工具各自实现，审批是横切的。

### 1.2 目标

危险工具执行前必须由**指定的人**批准，且**发起人不能批自己的**。

### 1.3 非目标

- 模型主动提问（`WAITING_INPUT` 的字面语义）—— 需要多轮对话状态管理，独立议题
- 完整 RBAC / 用户模型 —— 见 §4.2 的降级方案
- 平台按钮交互（Slack Block Kit / 飞书卡片）—— 见 §6.4
- 审批人轮换（业界建议，防橡皮章）—— 团队规模不到，先不做
- 紧急绕过通道 —— 业界建议有，但需要先有「什么算紧急」的定义

### 1.4 验收标准

1. 配了审批的工具被调用时任务转 `WAITING_INPUT`，工具**不执行**
2. 同一轮里的只读工具照常执行
3. 发起人批自己的任务被拒绝，即便他在审批人名单里
4. 未配审批的工具行为不变
5. 批准后工具执行，且此前已完成的工具不重放
6. 拒绝后工具不执行，拒绝理由回灌给模型
7. 审批时可改参数
8. 配了 `required_approvals=2` 的工具需两个**不同的人**各批一次
9. 待批超时自动拒绝（不是永久挂着）
10. 审批人未配置时**拒绝执行**，不放宽
11. 审批的完整链路进审计：谁请求、谁批/拒、原参数、改后参数、理由
12. 不装配审批仓储时行为与现状一致
13. `make check` + `npm run check` 全绿

## 2. 实测结论（pydantic-ai 2.25.0）

方案的每处设计都指回这里某一条。

| # | 实测 | 结果 |
|---|---|---|
| 1 | 工具抛 `ApprovalRequired` | run **正常结束**（非异常），`output` 是 `DeferredToolRequests`，带 `approvals` 清单（工具名 + 参数 + `tool_call_id`） |
| 2 | 危险工具是否执行 | **未执行**；同一轮的只读工具 `read_file` 正常跑完 |
| 3 | 历史能否序列化 | 能，2305 字节，**用的就是检查点那套 `ModelMessagesTypeAdapter`** |
| 4 | 批准后恢复 | `DeferredToolResults(approvals={tcid: True})` → 工具执行，`read_file` **不重放** |
| 5 | 拒绝后恢复 | `ToolDenied("产品不同意提这个 PR")` → 工具不执行，理由回灌给模型，模型自行收尾 |
| 6 | 审批时改参数 | `ToolApproved(override_args={"title": "人改过的标题"})` → **生效**，实际执行的是人改过的参数 |
| 7 | 待批历史的形状 | `Requ[UserPrompt] Resp[Call:read_file] Requ[Ret:read_file] Resp[Call:create_pr]`，**悬空调用 = 1** |

第 6 条尤其有用：审批不是「批/否」二选一，人可以**改完再放行**。

第 7 条决定了与检查点的关系（§3.3）。

## 3. 与既有机制的关系

### 3.1 复用检查点的骨架

三级披露那次改造留下的东西正好是审批需要的：

| 需要的 | 现状 |
|---|---|
| 消息历史序列化 | ✅ `ModelMessagesTypeAdapter`（实测同一套） |
| 中断后落库 | ✅ `task_checkpoints` 可复用，加两列 |
| 恢复执行 | ✅ `gateway.run(history=...)` 已有 |
| 「等人」的状态 | ✅ `WAITING_INPUT` 已在状态机里 |
| 定时兜底 | ✅ `sweep_stale_tasks` 已在扫超时 |
| 主动外发 | ✅ `MessagePublisher` + `ambient` 的冷却机制 |

### 3.2 三个空壳字段一并填上

仓库里有三个同源的空壳，都是「设计文档规划了、实现只做了一半」：

| 空壳 | 现状 | 本方案 |
|---|---|---|
| `TaskStatus.WAITING_INPUT` | 有读侧（`ambient` 催办文案），**无写侧** | 待批任务进这个状态 → 催办文案自动生效 |
| `Task.owner_id` | 有 ORM 列，**零个赋值点** | 审批人的第一级来源 |
| `tag.shared` | 无跨频道共享语义 | 不在本方案范围 |

`ambient.ACTIVE_STATUSES` 已含 `WAITING_INPUT`，`_nudge_text()` 里「任务还在等补充信息」的文案早就写好 —— **催办零代码自动生效**。

### 3.3 与检查点判据不冲突（实测确认）

待批状态的历史里，那个待批的 `create_pr` 就是一个**悬空调用**（§2 第 7 条）。而检查点判据要求 `_dangling == 0` —— 它会自动跳过这个状态，不会把待批当检查点落库。

两套机制各走各的恢复路径：

- **崩溃** → `history` 续跑
- **待批** → `history` + `DeferredToolResults` 恢复

同一个任务可能两者都经历过（跑了两轮工具 → 落了检查点 → 第三轮撞审批 → 待批），两份状态并存不冲突：检查点记「跑到哪了」，待批载荷记「在等什么」。

## 4. 谁能批：四眼原则

### 4.1 业界共识

查到的资料里，**没有系统把审批权交给「任何人」**：

| 来源 | 要点 |
|---|---|
| [Four Eyes Principle](https://www.flagsmith.com/blog/what-is-the-four-eyes-principle) | 「发起人不能批准自己的改动（SoD）」；审批人靠 RBAC 指定；建议**轮换**审批人防橡皮章；审计必须记「谁请求、谁批准、原状态、应用的规则、理由」 |
| [HITL Auth with Auth0](https://auth0.com/blog/async-ciba-python-langgraph-auth0/) | 用 CIBA 协议，核心洞察是**审批人身份必须独立于当前会话** |
| [RBAC for LangGraph](https://hoop.dev/blog/rbac-for-langgraph) | 角色对应「明确的业务目的 + 有界的节点集合」 |
| [HITL Engineering Patterns](https://activewizards.com/blog/hitl-engineering-patterns-langgraph-interrupts/) | 按风险分三档：自动过 / 单人批 / **双人批**；「把审批人的注意力留给高风险动作」 |
| [Segregation of Duties](https://www.sikich.com/insight/why-segregation-of-duties-is-a-key-internal-control-and-how-to-implement-it/) | 拆开关键活动，使**没有任何单个人**能独自完成「发起-批准-记录-对账」全链路 |

### 4.2 本项目的答案早在 PRD 里

**PRD §3.1 的角色表**把「团队负责人」与「管理员」列为**两个不同角色**：

| 角色 | 核心诉求 |
|---|---|
| 团队负责人 | 任务透明可追踪、**资源可控**、知识沉淀 |
| 管理员 | 权限管控、预算上限、审计合规 |

**PRD §4.6 验收标准 2**：「…在该频道达到上限后暂停任务并**通知负责人**」—— 不是「通知频道」。

而 **Design §4.2 的 Task 模型**里两个字段本来就是分开的：

```json
"requesterId": "U123",   ← 发起人
"ownerId": "U456",       ← 负责人
```

设计阶段就区分了这两者，正是四眼原则要的那道区分。`Task.owner_id` 现在是空壳（零赋值点），本方案把它填上。

### 4.3 三级 fallback

```
审批人 =
  1. task.owner_id            ← 任务负责人（兑现 PRD 的「通知负责人」）
  2. policy.approver_ids      ← 频道级审批人（管理员配）
  3. 都没配 → 拒绝执行
```

**第 3 级是关键**：配不出审批人时**不放宽**，而是拒绝，模型收到「该频道未配置审批人，无法执行该工具」。

这与现有 `allowed_tools` 的语义一致 —— 白名单里没有的工具不是「谁都能用」，而是不能用。「频道内任何人可批」恰恰违反了这条既有语义。

### 4.4 硬校验：发起人不能批自己

```python
if approver_id == task.requester_id:
    拒绝，记审计
```

**即便发起人在 `approver_ids` 里也拒绝** —— 配置的含义是「他平时可以批别人的」，不是「他能批自己的」。这是 SoD 的最小落地，也是 Auth0 那条「审批人身份必须独立于当前会话」的具体形态。

### 4.5 为什么不做完整 RBAC

系统没有用户模型，唯一可信的身份来源是**平台签过名的 `msg.user_id`**。Admin API 只有一个共享 token，`actor` 是前端随便填的字符串 —— 审批这种动作的审计链不该建立在不可信字段上。

所以简化成「审批人 id 列表」：一个频道通常 1~3 个审批人，列表够用。这个设计可平滑升级 —— 日后有了角色系统，`approver_ids` 变成「角色解析后的结果」，配置形态不变。

### 4.6 `owner_id` 从哪来

按 CODEOWNERS 的模式（配置指定 + 可覆盖）：`ChannelInstance` 加 `default_owner_id`，建任务时自动填进 `task.owner_id`。没配则 `owner_id` 为空，自然回落到第 2 级。

后续迭代可支持发起时显式指定（`/review @U456 看下这个 PR`），本期不做。

## 5. 风险分级

业界普遍做法是三档（自动过 / 单批 / 双批），但那套依赖**风险分数**（`risk_score >= 0.95` 自动过等），而我们没有评分系统，硬造一个只会得到假精度。

改成**按工具名直接配需要几个批准**：

```python
# PermissionPolicy 新增
approval_required_tools: dict[str, int]   # 工具名 → 需要几个批准
approver_ids: list[str]                   # 频道级审批人
```

```yaml
approval_required_tools:
  github: 1              # 提 PR 单人批
  mcp__prod-deploy: 2    # 生产部署双人批
```

这等价于业界的「硬性覆盖」（`high_value_override` 那类）—— 它们本质也是配置驱动的，只是我们把「分数 → 档位」这一步省了，直接配档位。

**为什么用 `dict` 而不是 `list`**：`list` 只能表达「要不要批」，第二个危险工具出现时就得再加一个字段。MCP 工具是外部的、风险面不可控，早晚要区分档位。

**双批的去重**：`required_approvals=2` 时必须是**两个不同的 `user_id`** 各批一次，同一人点两次只算一次。这是四眼原则的核心，不是防误触。

## 6. 通知与审批入口

### 6.1 主入口：原线程

`MessagePublisher.reply(ReplyTarget, text)` 已有，`ReplyTarget` 由 `ChannelInstance`(platform + channel_id) + `task.thread_ref` 拼出 —— worker 回帖就是这么干的。

**定向 @ 到审批人**，不是在线程里泛泛喊一声：

```
@U456 需要你确认：github.create_pr
  repo:  team/api
  title: 修复登录超时
  base:  main ← head: fix/login-timeout

在本线程回复 /approve 放行，/deny <理由> 拒绝
改参数后放行：/approve title="新标题"
```

参数必须**列全**。只说「我要建 PR」等于让人盲签 —— 而实测支持改参数（§2 第 6 条），人看得见才改得动。

### 6.2 审批指令复用 `/` 前缀解析

`router._handle_task` 已有 `parts[0].startswith("/")` 分支（tag 用的）。审批分支插在它**之前**：先查该 `thread_ref` 有无待批项，命中则走审批、不进意图分类。

绑定键用 `thread_ref` 而非 task_id —— 用户不必抄一个 26 位 ULID。代价是同一线程同时只能有一个待批项，这在实践中是对的（一个线程一个任务）。

### 6.3 两级节奏

1. **初次通知立即发**（审批是阻塞的，等巡检太慢）
2. **后续催办交给 `ambient`** 的 `thread_stale` 规则 —— `WAITING_INPUT` 已在 `ACTIVE_STATUSES` 里，零代码生效，且有 Redis 冷却防刷

### 6.4 控制台只做只读待批列表

**不给放行按钮。** 控制台的 `actor` 不可信（§4.5），放行会在审计里留下不可信的审批人。控制台负责「能看见、能追溯、能看到参数全文」，放行回线程里做 —— 那里的 `user_id` 是平台签过名的。

### 6.5 平台按钮不做

Slack Block Kit / 飞书卡片各是一套回调路由 + 签名校验 + 防重复点击，而 `MessagePublisher.reply()` 目前只发文本。收益是省两秒打字，成本是两个平台各一套交互链路。等主流程跑顺再加 —— 那时按钮回调可以直接映射到已验证过的 `/approve` 语义上。

## 7. 数据模型

### 7.1 待批载荷

复用 `task_checkpoints` 加两列，而不是新建表：

```
task_checkpoints
  ...（既有六列）
  pending_approval  LargeBinary NULL   # DeferredToolRequests 的序列化
  approvals         Text NULL          # 已收到的批准，JSON: [{"user_id","at","override_args"}]
```

**为什么不新建表**：待批与检查点是同一个任务的两份执行期状态，主键都是 `task_id`，生命周期也一样（终态时一起清）。分表会让「取一个任务的执行状态」变成两次查询，且要各自维护清理逻辑。

**为什么 `approvals` 是列表**：双批要记「谁批过了」才能去重。单批时列表长度 1。

### 7.2 领域模型

```python
@dataclass
class PendingApproval:
    """一个待批的工具调用。"""
    tool_call_id: str        # pydantic-ai 的调用 id，恢复时要原样传回
    tool_name: str
    args: dict[str, Any]     # 原始参数，展示给审批人
    required: int            # 需要几个批准
    approvals: list[ApprovalRecord]

    @property
    def approved_by(self) -> set[str]:
        """已批准的人。用于双批去重与「不能自批」校验。"""
        return {a.user_id for a in self.approvals}

    @property
    def satisfied(self) -> bool:
        return len(self.approved_by) >= self.required
```

`satisfied` 用 `approved_by`（集合）而非 `len(approvals)` —— 同一人点两次不该凑够双批。

## 8. 超时兜底

待批任务卡在 `WAITING_INPUT` 需要出路。业界的做法是「外部调度器扫过期检查点」（LangGraph 没有内置 interrupt TTL），我们已有 `sweep_stale_tasks`。

**超时动作是拒绝，不是取消**：

```
WAITING_INPUT 超时 → 拿 ToolDenied("审批超时") 恢复 → 模型收到拒绝并自行收尾 → DONE
```

比转 `CANCELLED` 好在：模型能说明「因为没等到审批，PR 没有创建」，用户看到的是解释而非任务凭空消失。状态机里 `WAITING_INPUT → RUNNING` 这条边已经存在。

新增配置 `jobs_approval_timeout_minutes`，默认 1440（一天）。审批是人的动作，比 token 预算慢得多。

## 9. 完整流程

```
agent run
  ├─ 只读工具 → 正常执行
  └─ 配了审批的工具 → 抛 ApprovalRequired
       ↓
     run 正常结束，output 是 DeferredToolRequests
       ↓
     落库：pending_approval + history；任务转 WAITING_INPUT
       ↓
     定向 @ 审批人（原线程）
       ↓
  ┌────────────┴────────────┬──────────────┐
  ↓                         ↓              ↓
/approve（线程）         /deny（线程）    超时（巡检）
  ↓                         ↓              ↓
校验：非发起人 +          记拒绝理由      ToolDenied("审批超时")
     在审批人名单 +
     未重复批
  ↓
够数？ ──否──→ 继续等（记一条 approval）
  │是
  ↓
DeferredToolResults(approvals={tcid: ToolApproved(override_args=...)})
  + history
  ↓
gateway.run() 恢复 → 工具执行 → 任务转 RUNNING → DONE
```

工具执行前的最后一道：**恢复时要重查审批人是否仍在名单里**吗？不查 —— 批准是一个时间点的动作，事后名单变更不该追溯撤销已批准的操作。这与 GitHub 的 PR approval 语义一致（approve 后改 CODEOWNERS 不会撤销已有 approval）。

## 10. 审计

四眼原则要求记「谁请求、谁批准、原状态、应用的规则、理由」。复用 `POLICY_CHANGE` + `detail.event`（理由同 skill：`audit_logs.action` 是 Postgres 原生枚举，加成员要配 `ALTER TYPE` 迁移，漏了会让已升级的库炸）：

| event | detail |
|---|---|
| `approval_required` | `tool`, `args`, `required`, `approver_candidates`, `requester` |
| `approval_granted` | `tool`, `approver`, `override_args`（改过参数才有）, `progress`（如 `1/2`） |
| `approval_denied` | `tool`, `approver`, `reason` |
| `approval_timeout` | `tool`, `waited_minutes` |
| `approval_rejected_self` | `tool`, `attempted_by` —— 发起人试图自批，**这条最要紧** |

`approval_rejected_self` 单独记：它是 SoD 被触发的证据，安全审计要能查「有没有人试过绕过四眼」。

审批记在**频道**作用域（不是 `GLOBAL_SCOPE`）—— 它是这个频道里发生的事。

## 11. 改动清单

| 层 | 文件 | 动作 |
|---|---|---|
| domain | `models/approval.py` | 新增 `PendingApproval` / `ApprovalRecord` / `ApprovalDecision` |
| domain | `models/policy.py` | `PermissionPolicy` 加 `approval_required_tools` / `approver_ids` |
| domain | `models/channel.py` | `ChannelInstance` 加 `default_owner_id` |
| domain | `repositories/checkpoint.py` | 加待批载荷的读写方法 |
| domain | `ports/llm.py` | `run()` 返回值要能带待批清单；加 `deferred_results` 入参 |
| infra | `orm/checkpoint.py` | 加两列 |
| infra | `orm/policy.py` | 加两列（JSON 字符串，对齐 `allowed_tools` 先例） |
| infra | `orm/channel.py` | 加一列 |
| infra | `repositories/checkpoint.py`、`policy.py`、`channel.py` | mapper |
| infra | `llm/gateway.py` | `output_type=[str, DeferredToolRequests]`；工具包装层抛 `ApprovalRequired` |
| infra | `tools/registry.py` | 按 `approval_required_tools` 包一层审批闸 |
| app | `approval.py` | 新增 `ApprovalService`：解析决定、校验、去重、恢复 |
| app | `agent/runtime.py` | 待批时落库并转 `WAITING_INPUT` |
| app | `agent/context.py` | `ContextBundle` 带 `approval_required_tools` |
| app | `router.py` | `/approve` `/deny` 分支 + 初次通知 |
| app | `orchestrator.py` | 建任务时填 `owner_id`；巡检加审批超时分流 |
| — | `container.py` / `config.py` / migration | 装配 |
| adapters | `admin/policy.py` | 两个新字段的读写 |
| adapters | `admin/approval.py` | 只读待批列表端点 |
| web | `PolicyPage` / 新 `ApprovalPage` | 配置 + 只读列表 |
| 守卫 | `test_admin_routes` / `test_orm_registry` | 路由表、（表名不变，加列无需改） |

## 12. 测试策略

| 覆盖 | 要点 |
|---|---|
| **发起人不能自批** | 即便他在 `approver_ids` 里也拒绝；且记 `approval_rejected_self` |
| **审批人未配置 → 拒绝** | 不放宽（对齐 `allowed_tools` 语义） |
| 三级 fallback | `owner_id` 优先于 `approver_ids`；都没配则拒 |
| **双批去重** | 同一人点两次只算一次；两个不同人才够数 |
| 批准后执行 | 工具真跑了，且此前完成的工具不重放 |
| 拒绝后不执行 | 理由回灌，模型能收尾 |
| 改参数 | `override_args` 真的生效（实测支持） |
| 只读工具不受影响 | 同一轮里照常执行 |
| 超时 | 转 `ToolDenied`，任务能走到 DONE（不是卡死也不是 CANCELLED） |
| 与检查点共存 | 待批状态不被当成检查点落库（`_dangling > 0`） |
| 审计完整性 | 五种 event 各一条，字段齐全 |
| 向后兼容 | 不配 `approval_required_tools` 时行为完全不变 |
| 端到端 | 真 SQLite：撞审批 → 通知 → `/approve` → 执行 → DONE |

三处最容易静默退化：**自批校验**（漏了四眼原则就没了）、**双批去重**（用 `len()` 而非集合会让一人凑双批）、**审批人未配置的默认行为**（默认放宽是最危险的退化）。

## 13. 实施顺序

1. domain model + policy/channel 扩字段 + migration
2. `PendingApproval` 存储（checkpoint 表扩列）
3. gateway 审批闸 + `DeferredToolRequests` 输出
4. `ApprovalService`（校验 + 去重 + 恢复）
5. runtime 待批落库 + router 审批分支与通知
6. 巡检超时分流
7. admin 端点 + 控制台只读列表
8. 端到端 + 验收标准逐条过

## 14. 边界与已知限制

| 项 | 说明 |
|---|---|
| **审批人无角色系统** | 只有 id 列表。平滑升级路径见 §4.5 |
| **控制台不能放行** | `actor` 不可信（§6.4）。要放行必须先做用户模型 |
| **无审批人轮换** | 业界建议（防橡皮章），团队规模不到 |
| **无紧急绕过** | 业界建议有，但需先定义「什么算紧急」 |
| **同线程单待批** | `thread_ref` 作绑定键的代价 |
| **批准不追溯撤销** | 名单变更不影响已批准的操作（§9） |
| **平台按钮未做** | 靠打字，见 §6.5 |
| **模型主动提问未做** | `WAITING_INPUT` 只用于待批，不用于「等用户补充信息」 |
| **MCP 工具可配但风险不可控** | 外部 server 的工具行为我们不掌握，配审批只能拦住调用、不能保证它内部不做别的 |
