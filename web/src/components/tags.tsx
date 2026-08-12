import { Tag, Tooltip } from 'antd'

import type {
  AuditAction,
  AuditResult,
  BudgetState,
  InteractionResult,
  MemorySource,
  MemoryType,
  TaskStatus,
} from '@/api/types'

/**
 * 枚举值 → 中文标签 + 颜色。
 *
 * 全部映射集中在本文件：同一个状态在任务列表与审计详情里必须同色同名，
 * 分散写必然漂移。`satisfies Record<X, ...>` 让后端加了枚举值而这里漏补时
 * 直接编译报错，而不是在页面上显示英文原文。
 */

type Meta = { label: string; color: string }

const TASK_STATUS = {
  // 进行中的三态用暖色/主色，终态用灰或语义色 —— 扫列表时先看到「还在动的」
  PENDING: { label: '待执行', color: 'default' },
  RUNNING: { label: '执行中', color: 'processing' },
  WAITING_INPUT: { label: '待补充', color: 'warning' },
  DONE: { label: '已完成', color: 'success' },
  FAILED: { label: '失败', color: 'error' },
  CANCELLED: { label: '已取消', color: 'default' },
  PAUSED: { label: '已暂停', color: 'warning' },
} satisfies Record<TaskStatus, Meta>

const MEMORY_TYPE = {
  BACKGROUND_KNOWLEDGE: { label: '背景知识', color: 'blue' },
  PREFERENCE: { label: '偏好', color: 'purple' },
  DECISION: { label: '决策', color: 'geekblue' },
  FACT: { label: '事实', color: 'cyan' },
} satisfies Record<MemoryType, Meta>

/**
 * 记忆的产生方式。用无色/灰调而非彩色：它是审计维度而非状态，
 * 抢了类型标签（彩色）的视觉焦点反而妨碍扫读。
 */
const MEMORY_SOURCE = {
  DISTILLED: { label: '自动蒸馏', color: 'default' },
  MANUAL: { label: '人工写入', color: 'blue' },
  EDITED: { label: '蒸馏后修改', color: 'orange' },
} satisfies Record<MemorySource, Meta>

const AUDIT_ACTION = {
  task_create: { label: '创建任务', color: 'blue' },
  task_transition: { label: '任务流转', color: 'geekblue' },
  tool_call: { label: '调用工具', color: 'cyan' },
  // 拒绝与失败要一眼看见：这两类是排查时真正要找的
  tool_denied: { label: '工具被拒', color: 'volcano' },
  memory_store: { label: '写入记忆', color: 'purple' },
  memory_delete: { label: '删除记忆', color: 'magenta' },
  // 系统蒸馏与人工写入分开着色：排查「记忆库里怎么会有这条」时先要分清来源
  memory_distill: { label: '蒸馏记忆', color: 'blue' },
  memory_edit: { label: '修改记忆', color: 'orange' },
  policy_change: { label: '策略变更', color: 'orange' },
  budget_change: { label: '预算变更', color: 'gold' },
  ambient_trigger: { label: '主动介入', color: 'lime' },
} satisfies Record<AuditAction, Meta>

const AUDIT_RESULT = {
  SUCCESS: { label: '成功', color: 'success' },
  FAILURE: { label: '失败', color: 'error' },
  DENIED: { label: '被拒', color: 'volcano' },
  PAUSED: { label: '暂停', color: 'warning' },
} satisfies Record<AuditResult, Meta>

/**
 * 交互结果。与 AUDIT_RESULT 分开一张表而非复用：两者枚举不同（这里无 DENIED，
 * 且成功态叫 DONE），合用会让「后端加了枚举值这里漏补」失去编译期保护。
 * 颜色刻意与审计对齐 —— 同一次调用在两个页面里出现时该同色。
 */
const INTERACTION_RESULT = {
  DONE: { label: '完成', color: 'success' },
  PAUSED: { label: '暂停', color: 'warning' },
  FAILED: { label: '失败', color: 'error' },
} satisfies Record<InteractionResult, Meta>

const BUDGET_STATE = {
  ACTIVE: { label: '正常', color: 'success' },
  EXHAUSTED: { label: '已耗尽', color: 'error' },
} satisfies Record<BudgetState, Meta>

const PLATFORM: Record<string, Meta> = {
  slack: { label: 'Slack', color: 'magenta' },
  feishu: { label: '飞书', color: 'blue' },
}

/** 后端给了未知值时不要崩，原样显示灰底 —— 界面比字典先老。 */
function pick(map: Record<string, Meta>, key: string): Meta {
  return map[key] ?? { label: key, color: 'default' }
}

export const TaskStatusTag = ({ value }: { value: TaskStatus }) => {
  const m = pick(TASK_STATUS, value)
  return <Tag color={m.color}>{m.label}</Tag>
}

export const MemoryTypeTag = ({ value }: { value: MemoryType }) => {
  const m = pick(MEMORY_TYPE, value)
  return <Tag color={m.color}>{m.label}</Tag>
}

const MEMORY_SOURCE_HINT: Record<MemorySource, string> = {
  DISTILLED: '由模型从频道对话中自动提炼，未经人工确认',
  MANUAL: '由人经控制台或 Admin API 直接写入',
  EDITED: '原为自动蒸馏，之后被人工修正过',
}

export const MemorySourceTag = ({ value }: { value: MemorySource }) => {
  const m = pick(MEMORY_SOURCE, value)
  return (
    <Tooltip title={MEMORY_SOURCE_HINT[value] ?? value}>
      <Tag color={m.color}>{m.label}</Tag>
    </Tooltip>
  )
}

export const AuditActionTag = ({ value }: { value: AuditAction }) => {
  const m = pick(AUDIT_ACTION, value)
  // 英文原值放 tooltip：排查时要按它去 grep 后端日志
  return (
    <Tooltip title={value}>
      <Tag color={m.color}>{m.label}</Tag>
    </Tooltip>
  )
}

export const AuditResultTag = ({ value }: { value: AuditResult }) => {
  const m = pick(AUDIT_RESULT, value)
  return <Tag color={m.color}>{m.label}</Tag>
}

export const InteractionResultTag = ({ value }: { value: InteractionResult }) => {
  const m = pick(INTERACTION_RESULT, value)
  return <Tag color={m.color}>{m.label}</Tag>
}

export const BudgetStateTag = ({ value }: { value: BudgetState }) => {
  const m = pick(BUDGET_STATE, value)
  return <Tag color={m.color}>{m.label}</Tag>
}

export const PlatformTag = ({ value }: { value: string }) => {
  const m = pick(PLATFORM, value)
  return <Tag color={m.color}>{m.label}</Tag>
}

/** 模型档位。轻量/完整两档来自 config 的 model.* 分级。 */
export const ModelLevelTag = ({ value }: { value: string }) => {
  const full = value.toLowerCase() === 'full'
  return <Tag color={full ? 'purple' : 'default'}>{full ? '完整模型' : '轻量模型'}</Tag>
}

/** 任务状态的全部取值，供表格筛选器出选项。 */
export const TASK_STATUS_OPTIONS = Object.entries(TASK_STATUS).map(([value, m]) => ({
  value: value as TaskStatus,
  text: m.label,
}))

export const AUDIT_ACTION_OPTIONS = Object.entries(AUDIT_ACTION).map(([value, m]) => ({
  value: value as AuditAction,
  text: m.label,
}))

export const AUDIT_RESULT_OPTIONS = Object.entries(AUDIT_RESULT).map(([value, m]) => ({
  value: value as AuditResult,
  text: m.label,
}))

export const MEMORY_TYPE_OPTIONS = Object.entries(MEMORY_TYPE).map(([value, m]) => ({
  value: value as MemoryType,
  text: m.label,
}))

export const MEMORY_SOURCE_OPTIONS = Object.entries(MEMORY_SOURCE).map(([value, m]) => ({
  value: value as MemorySource,
  text: m.label,
}))

export const INTERACTION_RESULT_OPTIONS = Object.entries(INTERACTION_RESULT).map(([value, m]) => ({
  value: value as InteractionResult,
  text: m.label,
}))
