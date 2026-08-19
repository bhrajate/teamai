/**
 * 与后端 `adapters/admin/serializers.py` 逐字段对应。
 *
 * ⚠️ 改这里必须同步改那边，反之亦然。后端路由返回 `dict[str, Any]`，
 * OpenAPI schema 里没有响应形状，故这层对齐只能靠人守 —— 字段名写错不会有
 * 编译错误，只会在页面上显示 undefined。
 *
 * 各 union 的取值抄自 domain/models/ 里的 Enum，取 `.value`。
 */

/** domain/models/task.py TaskStatus */
export type TaskStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'WAITING_INPUT'
  | 'DONE'
  | 'FAILED'
  | 'CANCELLED'
  | 'PAUSED'

/** domain/models/memory.py MemoryType */
export type MemoryType = 'BACKGROUND_KNOWLEDGE' | 'PREFERENCE' | 'DECISION' | 'FACT'

/**
 * domain/models/memory.py MemorySource —— 记忆的产生方式。
 *
 * 与 `source_user_id` 是两件事：那个答「哪个用户的话变成了这条」，本字段答
 * 「这条是谁写下的」。蒸馏产出与管理台人工写入的 source_user_id 都是 null，
 * 只有这个字段能区分它们。
 */
export type MemorySource = 'DISTILLED' | 'MANUAL' | 'EDITED'

/** domain/models/budget.py */
export type BudgetScope = 'ORGANIZATION' | 'CHANNEL'
export type BudgetPeriod = 'DAILY' | 'WEEKLY' | 'MONTHLY'
export type BudgetState = 'ACTIVE' | 'EXHAUSTED'

/** domain/models/audit.py */
export type AuditAction =
  | 'task_create'
  | 'task_transition'
  | 'tool_call'
  | 'tool_denied'
  | 'memory_store'
  | 'memory_delete'
  // 系统从对话窗口蒸馏出记忆。与 memory_store 分开：后者是人显式写入。
  | 'memory_distill'
  // 人工修改已有记忆（原地改，保留 id 与 created_at）
  | 'memory_edit'
  | 'policy_change'
  | 'budget_change'
  | 'ambient_trigger'
export type AuditResult = 'SUCCESS' | 'FAILURE' | 'DENIED' | 'PAUSED'

/**
 * domain/models/interaction.py InteractionResult
 *
 * 与 AuditResult 取值相近但不是同一个枚举：这里没有 DENIED（工具被拒记在审计），
 * 且用 DONE 而非 SUCCESS。别把两者的 Tag 组件混用。
 */
export type InteractionResult = 'DONE' | 'PAUSED' | 'FAILED'

/** 平台标识。adapters/ 下每个子包一个，`CONNECTOR_BUILDERS` 决定实际启用哪些。 */
export type Platform = 'slack' | 'feishu'

export type Channel = {
  id: string
  /** 未来接新平台时后端会给出新值，故留宽而不收成 Platform */
  platform: Platform | string
  channel_id: string
  workspace_id: string
  agent_identity: string
  ambient_enabled: boolean
  cross_channel_learning: boolean
  policy_id: string | null
  created_at: string
}

export type Memory = {
  id: string
  channel_instance_id: string
  content: string
  type: MemoryType
  /** 哪个用户的话变成了这条。蒸馏与管理台写入都是 null，故区分来源要看 source。 */
  source_user_id: string | null
  /** 这条是谁写下的 */
  source: MemorySource
  /** 有值即已建向量索引。null 表示这条不参与语义检索，只走时间倒序回落。 */
  embedding_ref: string | null
  /**
   * 取代本条的记忆 id。非 null 即表示本条已不是现行事实、不再参与检索，
   * 但仍留在库里 —— 排查「机器人为什么这么说」时要看得到被取代的版本。
   */
  superseded_by: string | null
  superseded_at: string | null
  created_at: string
}

/**
 * 一条疑似与待写入内容冲突的现行记忆。
 *
 * 形状由 `adapters/admin/memory.py` 的 `_conflict_detail` 决定，
 * 由 `tests/unit/test_admin_memory_conflict.py::test_409的body形状` 锁住。
 */
export type MemoryConflict = {
  entry: Memory
  /**
   * 余弦相似度 [0,1]。**null 表示走的是字面比对兜底**（未配 embedding），
   * 那时没有相似度可言 —— 后端刻意给 null 而不是编一个数，页面据此显示
   * 「字面重复」而不是一个假的百分比。
   */
  score: number | null
}

/**
 * embedder 的装配状态（`GET /api/embedding`）。
 *
 * `available` 为假时记忆能力有三层降级，见 `infrastructure/llm/embedding.py` 的
 * `_DEGRADED_CONSEQUENCES`：语义检索关闭、手工写入的冲突检查退化为字面比对、
 * 蒸馏的去重与取代对旧记忆基本失效。第三层最贵 —— 它让记忆库持续劣化，而且要
 * 几周才从回答质量上看出来。
 */
export type EmbeddingState = {
  available: boolean
  /** 配的是哪个模型。换过模型而没重建索引时向量是旧模型算的，对账查不出来。 */
  model: string | null
  dimensions: number
}

/** 手工写入撞上冲突时 409 的 detail。 */
export type MemoryConflictDetail = {
  message: string
  /**
   * 冲突检查是否处于降级状态（未配 embedding，只能查字面重复）。
   * 必须显示给录入人：不说的话「只报了这几条」会被读成「确认只有这几条」。
   */
  degraded: boolean
  conflicts: MemoryConflict[]
}

/**
 * 从 `ApiError.body` 里认出记忆冲突的 detail。
 *
 * 放在 types 而不是 client：`ApiError` 是全站通用的失败载体，不该认识
 * 「记忆冲突」这个领域概念（见 api/client.ts 里 `body` 字段的说明）。
 */
export function readMemoryConflictDetail(body: unknown): MemoryConflictDetail | null {
  if (!body || typeof body !== 'object' || !('detail' in body)) return null
  const d = (body as { detail: unknown }).detail
  if (!d || typeof d !== 'object' || !('conflicts' in d)) return null
  const { conflicts, message, degraded } = d as Record<string, unknown>
  if (!Array.isArray(conflicts)) return null
  return {
    message: typeof message === 'string' ? message : '',
    degraded: degraded === true,
    conflicts: conflicts as MemoryConflict[],
  }
}

export type Budget = {
  id: string
  scope: BudgetScope
  token_limit: number
  period: BudgetPeriod
  used_tokens: number
  remaining: number
  state: BudgetState
}

export type AmbientRule = {
  trigger: string
  params: Record<string, unknown>
  action: string
}

export type Policy = {
  id: string
  channel_instance_id: string
  allowed_tools: string[]
  ambient_rules: AmbientRule[]
  updated_at: string
}

export type AuditLog = {
  id: string
  ts: string
  channel_instance_id: string
  user_id: string | null
  action: AuditAction
  task_id: string | null
  tokens_consumed: number
  result: AuditResult
  detail: Record<string, unknown>
}

/**
 * 一次 Agent 调用的完整留痕。
 *
 * 三段文本（system_prompt / user_prompt / response)后端有意不截断 —— 这个
 * 资源的用途就是复现「模型当时看到了什么」。故列表页只出摘要，全文进抽屉。
 */
export type Interaction = {
  id: string
  task_id: string
  channel_instance_id: string
  thread_ref: string
  requester_id: string | null
  user_prompt: string
  system_prompt: string
  response: string
  model_level: string
  /** 实际生效的模型 ID。light 档降级到备用模型时与配置里的主模型不同。 */
  model_id: string
  tokens_in: number
  tokens_out: number
  /** 后端由 tokens_in + tokens_out 算出（模型上的 property），非独立存储字段。 */
  tokens_total: number
  result: InteractionResult
  error: string | null
  /** 引用而非内容快照：记忆条目 id、线程历史条数等，形状随调用路径而异。 */
  context_refs: Record<string, unknown>
  created_at: string
}

export type Task = {
  id: string
  channel_instance_id: string
  intent: string
  status: TaskStatus
  tag_name: string | null
  model_level: string
  requester_id: string | null
  created_at: string
  updated_at: string
}

export type Tag = {
  id: string
  channel_instance_id: string
  name: string
  instruction: string
  role: string | null
  output_style: string | null
  /**
   * ⚠️ 当前无实际作用。后端 `TagTemplate.shared` 默认 true，除存取之外从未被
   * 读作任何条件，也没有端点能改它。故界面上不呈现 —— 摆出来会暗示一种
   * 不存在的「跨频道共享」能力。后端真正实现共享语义后再接。
   */
  shared: boolean
  active: boolean
  created_by: string | null
}

/**
 * domain/models/mcp.py McpServer —— MCP server 配置。
 *
 * ⚠️ headers 在后端已被脱敏：任何响应里都只有值为 `***` 的占位（真值不离开
 * 后端），创建/更新时提交 `***` 表示保留原值、空串表示删除该键。
 */
export type McpServer = {
  id: string
  channel_instance_id: string
  name: string
  url: string
  headers: Record<string, string>
  enabled: boolean
  /** worker 启动时的连接失败快照，非实时探活。null 表示最近一次启动连上了（或从未装载）。 */
  last_error: string | null
  created_at: string
  updated_at: string
}

/** 连接测试的结果。tools 为 server 暴露的工具名（原名，不带前缀）。 */
export type McpTestResult = {
  tools: string[]
}
