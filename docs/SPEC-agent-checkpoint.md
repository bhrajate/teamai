# SPEC：Agent 检查点与失败续跑

> 状态：已实现
> 日期：2026-08-20
> 范围：`LLMGateway` 契约 + gateway 实现 + AgentRuntime + 超时巡检 + 新表 `task_checkpoints`

## 1. 需求

### 1.1 问题

`infrastructure/llm/gateway.py` 现在是 `await agent.run(...)` 单发，整个工具调用循环包在这一个 await 里。进程在第 3 次工具调用时崩掉，前两次的结果一并消失；超时巡检把卡住的 RUNNING 收敛成 `FAILED`，不重投、不续跑。对多轮工具任务，这意味着已花掉的 token 与已产生的外部副作用全部作废。

三个环节共同决定了「不可恢复」：队列 `BLPOP` 读即删（无 ack，崩溃时载荷已不在 Redis）、执行中途无任何状态落库、巡检只收敛不重投。

### 1.2 目标

任务在 worker 崩溃后能从**最近一个干净的轮边界**继续，已完成的工具不重跑。

### 1.3 非目标

- 队列 ack 改造 —— 独立议题，本方案用巡检驱动绕开它（见 §9）
- 工具幂等性 —— 独立议题，只标注风险（见 §12）
- 时间旅行式调试 —— 只留最新检查点
- 秒级恢复 —— 恢复延迟 = 巡检间隔
- 增量存历史 —— 全量存，理由见 §5.2

### 1.4 验收标准

1. 三轮工具任务在第二轮后崩溃，续跑只执行第三轮的工具
2. 续跑产出与不崩溃时一致
3. 同一段 token 不被扣两次，且崩溃段的消耗已计入
4. 检查点绝不落在带悬空工具调用的状态上
5. 超过续跑上限的任务收敛到 `FAILED`
6. 任务进终态时检查点被删除（同事务）
7. 不装配检查点仓储时行为与现状完全一致
8. `ModelRequestNode` 导入失效时立刻红（守卫测试）
9. `make check` + `npm run check` 全绿

## 2. 实测结论（pydantic-ai 2.25.0）

以下全部是探针脚本的实际输出，不是文档推断。方案的每处设计都指回这里的某一条。

### 2.1 序列化无损 ✅

```
消息条数: 6 → 往返后 6
ToolCallPart:   2 → 2
ToolReturnPart: 2 → 2
二次 dump 稳定: True
blob 大小: 3345 字节
```

`ModelMessagesTypeAdapter.dump_json` / `validate_json` 往返无损，二次 dump 逐字节相同。

检查点体积随工具轮数线性增长，实测每轮约 1.2 KB：

| 检查点 | 消息数 | 字节 |
|---|---|---|
| 1（1 轮工具） | 3 | 1417 |
| 2（2 轮工具） | 5 | 2610 |
| 3（3 轮工具） | 7 | 3804 |

十轮量级约 12 KB，`LargeBinary` 完全够用。

### 2.2 `ctx.state.message_history` 滞后一轮 ⚠️

两轮工具调用，逐节点观察：

| # | 节点 | 历史 n | 悬空 | 此刻 CALLS |
|---|---|---|---|---|
| 1 | UserPromptNode | 0 | 0 | `[]` |
| 2 | ModelRequestNode | 0 | 0 | `[]` |
| 3 | CallToolsNode | 2 | 1 | `[]` |
| 4 | ModelRequestNode | 2 | **1** | **`['ping(1)']`** |
| 5 | CallToolsNode | 4 | 1 | `['ping(1)']` |
| 6 | ModelRequestNode | 4 | **1** | **`['ping(1)','ping(2)']`** |
| 7 | CallToolsNode | 6 | 0 | `['ping(1)','ping(2)']` |
| 8 | End | 6 | 0 | `['ping(1)','ping(2)']` |

看第 4 行：工具**已执行**（`CALLS=['ping(1)']`），但 state 里 n=2、无任何 `ToolReturnPart`、悬空数为 1。那条待发的 `ModelRequest[ToolReturnPart]` 此刻不在 state 里 —— 它由节点自己持有。

这条实测推翻了「只看 `state.message_history` 就能找到干净边界」的直觉，是 §4.1 判据的直接依据。

### 2.3 两个落库判据实测

| 判据 | 为真且带悬空的节点数 | 结论 |
|---|---|---|
| A：历史里存在**任何** `ToolReturnPart` | **2** | 会落下带悬空调用的检查点 |
| B：**最后一条**消息含 `ToolReturnPart` | 0，但**从未为真** | 永远不落库 |

因 2.2 的滞后，在 state 视图里 `ToolReturnPart` 消息永远与下一条 `ModelResponse` 一起出现，中间那个干净瞬间观测不到。「悬空==0 且有工具结果」在整条 run 里只在节点 7、8 成立 —— 都是终点，对续跑无用。

### 2.4 悬空历史是硬错误 ✅

```
UserError: Cannot provide a new user prompt when the message history
contains unprocessed tool calls.
```

带未应答工具调用的历史 + 新 user prompt = 直接抛错。这既证明必须避免落这种检查点，也顺带定下续跑的调用方式（见 2.6）。

### 2.5 `ModelRequestNode.request` 携带待发的 `ToolReturnPart` ✅

三轮工具调用，只看 `ModelRequestNode`：

| 节点 | state_n | request 含 Ret | candidate_n | 悬空 | 可落库 |
|---|---|---|---|---|---|
| 2 | 0 | False | 1 | 0 | False |
| 4 | 2 | **True** | 3 | **0** | **True** |
| 6 | 4 | **True** | 5 | **0** | **True** |
| 8 | 6 | **True** | 7 | **0** | **True** |

`candidate = [*state.message_history, node.request]` 恰好在每个工具轮产出一个悬空为 0 的干净检查点。三轮工具 → 3 个检查点，形状：

```
检查点1: Requ[UserPrompt] Resp[Call] Requ[Ret:pong-1]
检查点2: … Resp[Call] Requ[Ret:pong-2]
检查点3: … Resp[Call] Requ[Ret:pong-3]
```

### 2.6 从中间检查点续跑，只跑剩余轮次 ✅

```
从检查点1 续跑（已完成1轮）: CALLS=['ping(2)','ping(3)'] 期望2次 ✅
从检查点2 续跑（已完成2轮）: CALLS=['ping(3)']           期望1次 ✅
从检查点3 续跑（已完成3轮）: CALLS=[]                     期望0次 ✅
```

续跑传 `prompt=None`（原始提问是历史第 1 条，往返后仍在）。

⚠️ **探针陷阱**：`FunctionModel` 的行为必须从传入历史推导，不能用自增计数器 —— 续跑时它是新实例，计数器归零会从第一轮重放，那测的是探针自己的 bug。我第一版探针就是这么错的，测试里要沿用「从历史推导」的写法。

### 2.7 usage 口径 ✅

```
不中断跑完 total_tokens = 247
从检查点1 续跑，传空 RunUsage() → 本段 total = 192
```

传空 `RunUsage()` 时 `run.usage` 表示**本段**用量；传上一段的 usage 对象则累计。另：2.25 里 `run.usage` 是 **property**，`r.usage()` 抛 `TypeError`（gateway 现有注释已记这件事）。

### 2.8 续跑的固有代价：input token 重复计费

续跑要把累积历史重新作为 input 发一遍，所以**各段之和 > 不崩溃时的总量**（实测 192 + 基数 > 247）。这是续跑机制本身的代价，换来的是不重跑工具，不是实现缺陷。

因此验收标准 1.4.3 的措辞是「同一段不被扣两次」，而**不是**「续跑总量等于不崩溃总量」—— 后者不可能成立。

## 3. 落库判据

### 3.1 判据

**在 `ModelRequestNode` 处判定**：

```python
candidate = [*ctx.state.message_history, node.request]
if dangling(candidate) == 0 and has_tool_return(candidate):
    落库
```

依据是 2.5：`node.request` 携带待发的 `ToolReturnPart`，把它拼上就得到干净的轮边界视图。对照 2.2 的表，这会在节点 4、6 各落一次 —— 正是想要的两个轮边界。

`dangling()` 按 `tool_call_id` 配对计数，不靠位置推断 —— 一轮里可能有多个并行工具调用。`has_tool_return()` 用 `isinstance(p, ToolReturnPart)`，**不用类名字符串比较**：后者会随 SDK 版本悄悄失效，而失效表现是「再也不落检查点」，没有任何报错。

### 3.2 判据 A 要作为反例固化进测试

2.3 实测判据 A（历史里存在任何 `ToolReturnPart`）会在 2 个节点落下带悬空调用的检查点，而 2.4 证明那种历史一旦用于续跑就是硬错误。测试里要显式锁住「A 与正确判据不等价」，防止日后有人「简化」成它。

### 3.3 纯文本轮次不落库

判据要求 `has_tool_return`，故纯对话任务不产生检查点，崩溃后从头重跑。代价只有 token、无副作用 —— 有意的取舍，避免为零收益的场景付 DB 写入。

### 3.4 去重

blob 与上次相同则跳过 DB 写。

### 3.5 写入频率

每个工具轮一次 DB 写。工具轮数极多的任务会放大写入，必要时可加「距上次落库不足 N 秒则跳过」，但先不做 —— 没有真实数据支撑这个阈值该取多少。

## 4. 领域模型与存储

### 4.1 领域模型

```python
# domain/models/checkpoint.py —— 仅标准库
@dataclass
class TaskCheckpoint:
    task_id: str
    messages: bytes          # 不透明 blob，domain 不解释
    tokens_used: int         # 跨段累计
    attempts: int = 0        # 已续跑次数
    created_at: datetime
    updated_at: datetime
```

`messages` 是 `bytes` 而非领域自己的消息模型，理由与 `ToolBundle` 一致 —— `domain/ports/tools.py` 已写明「若在领域层重新描述工具，就得由 infrastructure 翻译回 SDK 对象，翻译层一旦失真就会丢掉参数 schema」。消息历史同理，且失真后果更隐蔽：续跑时上下文少一段，模型照着残缺历史继续答，无任何报错。

### 4.2 表

```
task_checkpoints
  task_id      String(32)   PK
  messages     LargeBinary
  tokens_used  Integer
  attempts     Integer
  created_at   DateTime(tz)
  updated_at   DateTime(tz)
```

**主键是 `task_id`、覆盖写**：只会从最新的那个续跑。留历史要配 GC，换不来任何东西 —— 除了时间旅行调试，而那不是本次目标。

**不塞进 `tasks` 表**：blob 十轮量级 12 KB（2.1 实测），而 `tasks` 被列表端点与巡检反复扫。分表让热扫描保持便宜 —— 与 pgvector 那次 TOAST 的实测教训同理（单频道 7.5ms vs 全表 206ms），大字段与热扫描表要分开。

**不需要额外索引**：巡检先从 `tasks` 筛超时，再按 task_id 点查。

### 4.3 生命周期

任务进终态（DONE / FAILED / CANCELLED）时，**在同一事务内**删检查点。否则巡检要额外查任务状态才能判断该不该续跑，逻辑复杂一档；靠定时清理则会留一段「任务已完成、检查点还在」的窗口。

`PAUSED`（预算耗尽）**不删** —— 追加配额后应当从断点续跑而非重新开始。

## 5. 端口契约改动

这是最重的一处。现有契约是「发一次、拿结果，内部工具循环对上层不可见」，检查点必然打破它 —— 但只开放到「可以在中途被回调」这一层，节点数量、节点名称这些 SDK 细节仍不出 infrastructure。

```python
# domain/ports/llm.py
class CheckpointSink(Protocol):
    """节点边界的持久化回调。messages 对领域不透明。

    实现方须保证异常不外抛 —— gateway 会兜住并只记 warning，因为
    「检查点落不下」远好于「让一次正在成功的 run 失败」。
    """
    async def __call__(self, messages: bytes, tokens_total: int) -> None: ...


async def run(
    prompt: str,
    *,
    model_level: str,
    system_prompt: str = "",
    tools: ToolBundle | None = None,
    token_limit: int | None = None,
    history: bytes | None = None,                  # 新增：续跑起点
    on_checkpoint: CheckpointSink | None = None,   # 新增：中途回调
) -> LLMResult
```

两个参数都可选，现有调用点不改也能跑。

**`history` 非空时 `prompt` 被忽略** —— 原始提问就是历史第 1 条（2.1 证明往返后仍在）。这个取舍放在 gateway 内部而非让调用方传 `None`：2.4 实测传错会抛 `UserError`，那是 SDK 细节，不该泄到用例层。

## 6. 网关实现

### 6.1 主体

```python
async def run(self, prompt, *, model_level, system_prompt="", tools=None,
              token_limit=None, history=None, on_checkpoint=None) -> LLMResult:
    hist = ModelMessagesTypeAdapter.validate_json(history) if history else None
    agent = Agent(self._model(model_level),
                  instructions=system_prompt or None,
                  toolsets=[tools] if tools is not None else None)
    limits = UsageLimits(total_tokens_limit=max(token_limit, 1)) if token_limit is not None else None

    last_blob: bytes | None = None
    try:
        # 续跑时不能再给 user prompt（§2.4）；传空 RunUsage 让 usage 只计本段（§6.2）
        async with agent.iter(None if hist else prompt,
                              message_history=hist,
                              usage=RunUsage(),
                              usage_limits=limits) as run:
            async for node in run:
                if on_checkpoint is None or not isinstance(node, ModelRequestNode):
                    continue
                # 必须把 node.request 拼上：state 滞后一轮，待发的
                # ToolReturnPart 只由节点自己持有（§2.2 / §2.5）
                candidate = [*run.ctx.state.message_history, node.request]
                if _dangling(candidate) or not _has_tool_return(candidate):
                    continue
                blob = ModelMessagesTypeAdapter.dump_json(candidate)
                if blob == last_blob:
                    continue
                last_blob = blob
                try:
                    await on_checkpoint(blob, _total_tokens(run.usage))
                except Exception as exc:
                    # 落不下检查点不该毁掉正在成功的 run —— 最坏结果只是
                    # 崩溃后从更早的点重来
                    logger.warning(f"检查点持久化失败: {exc}")
        result = run.result
    except UsageLimitExceeded as exc:
        raise TokenBudgetExceeded(str(exc)) from exc
```

### 6.2 为什么传空 `RunUsage()`

`UsageLimits(total_tokens_limit=...)` 作用在传入的 usage 对象上。传累计值时，续跑段的上限会把前几段已花的算进去，语义变成「整个任务的总上限」；而预算控制器给的是**当前剩余配额**，两者对不上。传空实例则上限就是「本段最多花多少」，与剩余配额直接对应（2.7 实测本段从 0 计）。

总量由应用层 `base + segment` 算（§7.1）。

### 6.3 私有 API 依赖

`ModelRequestNode` 来自 `pydantic_ai._agent_graph` —— 私有模块。**必须加导入可用性的守卫测试**（§11），SDK 升级挪走它时立刻红，而不是等到线上「再也不落检查点」这种无声失效。

### 6.4 Plan B（若日后 `node.request` 不再带 `ToolReturnPart`）

改为在 `CallToolsNode` 处自行合成：从 `node.model_response` 取 `ToolCallPart` 列表，配合包一层 toolset（`_GracefulToolset` 已是 `WrapperToolset`，可在 `call_tool` 后记录结果）拿到返回值，自拼 `ModelRequest([ToolReturnPart...])` 追加。代码多一截但完全受控。

当前 2.5 实测 H1 成立，走 §6.1；这条留作 SDK 变更时的退路。

## 7. 运行时装配

### 7.1 续跑起点与增量计费

```python
async def _run_agent(self, task, bundle) -> StageResult:
    cp = await self._checkpoints.get(task.id) if self._checkpoints else None
    base = cp.tokens_used if cp else 0   # 前几段累计
    consumed = 0                          # 本段已计费的量

    async def sink(messages: bytes, seg_total: int) -> None:
        nonlocal consumed
        await self._checkpoints.upsert(task.id, messages, base + seg_total)
        # 每落一个检查点就补扣本轮增量。不等 run 结束一次扣完 ——
        # 那样 worker 崩溃时这一段的 token 永远不会被计费。
        if (delta := seg_total - consumed) > 0:
            await self._budget.consume(bundle.channel_instance_id, delta)
            consumed = seg_total

    remaining = await self._budget.remaining(task.channel_instance_id)
    llm = await self._gateway.run(
        self._compose_prompt(bundle),
        model_level=bundle.model_level,
        system_prompt=bundle.system_prompt,
        tools=self._tools.for_channel(bundle.allowed_tools, bundle.skills),
        token_limit=remaining,
        history=cp.messages if cp else None,
        on_checkpoint=sink if self._checkpoints else None,
    )
    # 收尾只补最后一个检查点之后的增量
    if (tail := llm.tokens - consumed) > 0:
        await self._budget.consume(task.channel_instance_id, tail)
```

三点要注意：

**`llm.tokens` 与 `consumed` 都是段内量纲**（因 §6.2 传了空 `RunUsage()`），所以 `tail = llm.tokens - consumed` 是对的，不需要减 `base`。

**`token_limit` 直接用 `remaining`**，不减 `base` —— 前几段在各自的检查点已经扣过了，`remaining` 已经反映了它们。

**`self._checkpoints` 可选注入**，不装配时整个检查点能力不出现（对齐 `interactions` 的现有写法）。

### 7.2 为什么是「每检查点计费」

上一版方案是「run 结束时按 `llm.tokens - base` 一次扣完」，它有个漏洞：崩溃段的 token 从未被 consume，崩一次就白花一次配额，且配额账面看不出来。

改成每检查点扣增量后：预算准确到最后一个检查点、崩溃不漏计、永不重复计费（靠 `consumed` 记账）。代价是预算表的写入频率跟着工具轮数走 —— 与 §3.5 的检查点写入同频，两者可以合并观察。

### 7.3 审计与交互记录

只在**最终完成**时写一次，不在每个检查点写 —— 否则同一任务在审计里出现多条 `task_transition`，成本统计被重复计入。

`AgentInteraction.context_refs` 加 `resume_count`，让「这个回答是续跑来的」可查。检查点本身不写审计：它是执行期状态，续跑次数看 `task_checkpoints.attempts`。

## 8. 终态清理

在 `TaskOrchestrator.transition()` 里，目标是终态时先删检查点、再更新任务，两者同一事务：

```python
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}

async def transition(self, task, to, actor):
    task.transition(to, actor)
    if to in _TERMINAL and self._checkpoints is not None:
        await self._checkpoints.delete(task.id)
    await self._repo.update(task)
    ...
```

放在 orchestrator 而非 runtime：**所有**终态迁移都经过它（含巡检判失败、用户取消），放 runtime 会漏掉那两条路径。

## 9. 续跑触发：不动队列

队列 ack 改造明确不做。`sweep_stale_tasks()` 已经在找超时的 RUNNING，把它分流：

```
RUNNING 超时：
  有检查点 且 attempts < 上限  → attempts++，重新入队，保持 RUNNING
  否则                        → FAILED（现状）
PENDING 超时：
  不变 → FAILED（还没开始跑，无检查点可言）
```

**这样绕开「无 ack」的关键**：检查点含完整历史，历史第 1 条就是原始 user prompt（2.1 证明往返后仍在）—— 载荷可以**纯从 DB 重建**，Redis 里丢掉的那条消息不再重要。

三处细节：

- `RUNNING → RUNNING` 不是合法迁移（状态机无自环），走 `transition()` 会抛 `InvalidTransition`。直接更新 `updated_at`，**别为此加自环** —— 那会引入观测不到的中间态，也会让「取消 → 重投」这类非法路径变得可能。
- **必须**刷新 `updated_at`，否则下一轮巡检立刻又捞出来，成死循环。这是本节最容易漏的一处：漏了不会报错，表现是同一任务被无限续跑。
- 重投时 `prompt` 传空串 —— worker 侧现有的 `payload.prompt or task.intent` 兜底正好接住。

`SweepReport` 加 `resumed` 字段 —— 现在有三种结局（续跑 / 收敛失败 / 推进失败），调用方要能区分。这与它当初为什么要带回 `failed` 是同一个理由：「一个都没卡住」和「找到 5 个全都处理失败」不能是同一个返回值。

新增配置 `jobs_max_resume_attempts`，默认 3。

## 10. 改动清单

| 层 | 文件 | 动作 |
|---|---|---|
| domain | `models/checkpoint.py` | 新增 `TaskCheckpoint`（仅标准库） |
| domain | `repositories/checkpoint.py` | 新增 ABC：`get` / `upsert` / `delete` / `bump_attempts` |
| domain | `ports/llm.py` | 加 `CheckpointSink` + `run()` 两参数 |
| domain | `models/__init__.py`、`repositories/__init__.py` | 导出 |
| infra | `orm/checkpoint.py` | `TaskCheckpointModel` |
| infra | `orm/__init__.py` | **必须补 import** —— 漏了表静默不建（`test_orm_registry` 守着） |
| infra | `repositories/checkpoint.py` | SQLAlchemy 实现 |
| infra | `llm/gateway.py` | `agent.run()` → `agent.iter()` + 节点回调 |
| app | `agent/runtime.py` | 续跑起点、sink、**增量计费** |
| app | `orchestrator.py` | 终态删检查点 + 巡检分流 + `SweepReport.resumed` |
| — | `container.py` | 装配 `checkpoint_repo`，传给 orchestrator 与 runtime；`JobScope` 也要带 |
| — | `config.py` | `jobs_max_resume_attempts` |
| — | `app/worker/main.py` | 巡检调用处传新参数、日志区分续跑与收敛 |
| — | migration | `task_checkpoints` 建表 |
| 守卫 | `test_orm_registry.py` | `EXPECTED_TABLES` 加 `task_checkpoints` |
| 守卫 | `test_repository_commit.py` | 新写方法必须 flush 不 commit |

`test_identity.py` 不用改 —— 主键复用 `task_id`，不引入新 id 前缀。

## 11. 测试策略

| 覆盖 | 要点 |
|---|---|
| 序列化往返 | dump → load → 工具结果仍在；二次 dump 逐字节相同 |
| **落库判据** | **三轮**工具调用（两轮测不出「从中间续跑」）；断言检查点数 == 工具轮数 |
| **判据 A 反例** | 固化「A 会落下带悬空调用的检查点」（§3.2），防止被简化成它 |
| 悬空绝不落库 | 遍历所有检查点，断言每个 `dangling == 0` |
| 并行工具调用 | 同一条消息里多个 `ToolCallPart`，验证配对按 `tool_call_id` 而非位置 |
| 续跑不重放 | 从中间检查点续跑，断言只跑剩余轮次；`FunctionModel` 行为**从历史推导**（§2.6 的陷阱） |
| 续跑传新 prompt | 断言抛 `UserError`（锁住 §5 「history 非空时忽略 prompt」的必要性） |
| **增量计费** | 断言 `consume` 的**调用序列**是增量而非总量；崩溃后同一段不被扣两次 |
| 续跑总量 | 断言续跑总量 **>** 不崩溃总量（§2.8，防止有人「修」成相等） |
| 回调容错 | sink 抛异常只记 warning，run 仍成功 |
| SDK 导入守卫 | `ModelRequestNode` 可从 `pydantic_ai._agent_graph` 导入（§6.3） |
| 巡检分流 | 有检查点→续跑、超上限→FAILED、`updated_at` 确实刷新、**不死循环** |
| 终态清理 | DONE / FAILED / CANCELLED 后检查点已删；PAUSED 后仍在 |
| 向后兼容 | 不装配 `checkpoint_repo` 时行为与现状一致 |
| 端到端 | 真 SQLite：强制中断 → 巡检 → 续跑 → 产出与不崩时一致 |

三处最容易静默退化，测试要写死：判据（退化成 A 会导致续跑硬错误）、增量计费（退化成扣总量会重复计费）、`updated_at` 刷新（漏了会无限续跑）。

## 12. 风险与边界

| 项 | 说明 |
|---|---|
| **幂等性** | 不做，只标注。内置工具里只有 `github.create_pr` 有真副作用。判据保证续跑时无悬空调用，模型是**重新决策**而非框架重放（2.4 证明框架直接拒绝悬空历史）—— 但它仍可能决定再调一次。真解法是幂等键，独立议题 |
| **私有 API** | `ModelRequestNode` 来自 `_agent_graph`，靠 §11 的导入守卫兜住 |
| **纯对话任务** | 不落检查点，崩溃后从头重跑（只花 token） |
| **恢复延迟** | = 巡检间隔，非秒级 |
| **input token 重复** | 续跑必然重发累积历史（§2.8）。这是机制代价，不是缺陷 |
| **全量存历史** | 每轮约 1.2 KB（2.1 实测）。十轮 12 KB 可接受；百轮量级要再评估增量存 |
| **多 worker 并发续跑** | **无防护**。扩到多副本前必须给续跑加租约 —— `memory_outbox.leased_until` 是现成先例 |
| **写入放大** | 每工具轮一次检查点写 + 一次预算写（§3.5 / §7.2） |

## 13. 实施顺序

> 已全部完成。验收标准 §1.4 九条逐条核对通过。


1. domain model + ORM + migration + repository（含真 SQL 测试）
2. `ports/llm.py` 契约 + gateway 改造（含判据单测与 A 的反例固化、导入守卫）
3. `runtime.py` 装配 + 增量计费（含预算准确性测试）
4. `orchestrator.py` 终态清理 + 巡检分流 + `SweepReport.resumed`
5. container / config / worker 装配
6. 端到端崩溃续跑测试
7. 全量验证（验收标准逐条过）

§2 的实测已覆盖全部前置假设，无需额外探针步骤。

## 14. 设计过程中被实测推翻的判断

留档，因为这几条都曾看起来很合理：

| # | 曾以为 | 实测结果 |
|---|---|---|
| 1 | 判据「最后一条消息含 `ToolReturnPart`」更安全 | **从未为真**，会导致永不落库（2.3）。正确判据必须把 `node.request` 拼上（2.5） |
| 2 | 只看 `ctx.state.message_history` 就能找到干净边界 | 它**滞后一轮**，待发的 `ToolReturnPart` 只由节点持有（2.2） |
| 3 | 续跑会「重放」悬空调用 | 框架**直接拒绝**悬空历史（`UserError`，2.4）。风险变成「模型自行决定再调一次」 |
| 4 | 计费用「`llm.tokens` 减基数」 | 有漏洞：崩溃段的 token 从未计入。改为每检查点扣增量（§7.1） |
| 5 | `run.usage()` 是方法 | 2.25 里是 **property**，调用抛 `TypeError`（2.7） |
| 6 | 续跑总量应等于不崩溃总量 | 不可能 —— input token 必然重复（2.8） |
