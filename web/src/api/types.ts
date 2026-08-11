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
  | 'policy_change'
  | 'budget_change'
  | 'ambient_trigger'
export type AuditResult = 'SUCCESS' | 'FAILURE' | 'DENIED' | 'PAUSED'

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
  source_user_id: string | null
  created_at: string
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
