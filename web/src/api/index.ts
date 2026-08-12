/**
 * 按资源分组的 API 调用，与 `adapters/admin/` 下的模块一一对应。
 *
 * 分组名即后端文件名，从而「后端加了个端点，前端该加在哪」不必想。
 */

import { request } from '@/api/client'
import type {
  AmbientRule,
  AuditLog,
  Budget,
  BudgetPeriod,
  Channel,
  Interaction,
  Memory,
  Policy,
  Tag,
  Task,
} from '@/api/types'

/** admin/channel.py */
export const channelApi = {
  list: (signal?: AbortSignal) => request<Channel[]>('/channels', { signal }),

  get: (id: string, signal?: AbortSignal) => request<Channel>(`/channels/${id}`, { signal }),

  /** 只传要改的开关；未传的字段后端不动。 */
  update: (
    id: string,
    body: { ambient_enabled?: boolean; cross_channel_learning?: boolean; actor?: string },
  ) => request<Channel>(`/channels/${id}`, { method: 'PATCH', body }),
}

/** admin/task.py */
export const taskApi = {
  list: (channelId: string, signal?: AbortSignal) =>
    request<Task[]>(`/channels/${channelId}/tasks`, { signal }),
}

/** admin/memory.py */
export const memoryApi = {
  list: (channelId: string, signal?: AbortSignal) =>
    request<Memory[]>(`/channels/${channelId}/memories`, { signal }),

  create: (channelId: string, body: { content: string; user_id?: string }) =>
    request<Memory>(`/channels/${channelId}/memories`, { method: 'POST', body }),

  /** 删除按条目 id，不带频道 —— 后端路由就是 /memories/{entry_id}。 */
  remove: (entryId: string, actor?: string) =>
    request<{ status: string }>(`/memories/${entryId}`, {
      method: 'DELETE',
      query: { actor },
    }),
}

/** admin/budget.py。未配额度时 GET 返回 404，调用方按 isNotFound 分支处理。 */
export const budgetApi = {
  get: (channelId: string, signal?: AbortSignal) =>
    request<Budget>(`/channels/${channelId}/budget`, { signal }),

  set: (channelId: string, body: { token_limit: number; period: BudgetPeriod }) =>
    request<Budget>(`/channels/${channelId}/budget`, { method: 'PUT', body }),
}

/** admin/policy.py。同上，未配策略时 GET 也是 404。 */
export const policyApi = {
  get: (channelId: string, signal?: AbortSignal) =>
    request<Policy>(`/channels/${channelId}/policy`, { signal }),

  set: (
    channelId: string,
    body: { allowed_tools: string[]; ambient_rules: AmbientRule[]; actor?: string },
  ) => request<Policy>(`/channels/${channelId}/policy`, { method: 'PUT', body }),

  /** 已注册的工具名。硬编码一份会随 build_tools() 增删而漂移。 */
  tools: (signal?: AbortSignal) => request<string[]>('/tools', { signal }),
}

/** admin/audit.py */
export const auditApi = {
  list: (channelId: string, limit = 200, signal?: AbortSignal) =>
    request<AuditLog[]>(`/channels/${channelId}/audit`, { query: { limit }, signal }),
}

/**
 * admin/interaction.py。只读 —— 记录由 AgentRuntime 执行时产生，
 * 清理走 worker 的保留期巡检，故这里没有 create/remove。
 */
export const interactionApi = {
  /** 频道最近若干条，时间倒序。后端 limit 上限 200（MAX_LIMIT），超了报 422。 */
  list: (channelId: string, limit = 50, signal?: AbortSignal) =>
    request<Interaction[]>(`/channels/${channelId}/interactions`, { query: { limit }, signal }),

  /** 某任务的完整往返，时间正序。重试与多阶段任务会有多条，故不返回单条。 */
  listByTask: (taskId: string, signal?: AbortSignal) =>
    request<Interaction[]>(`/tasks/${taskId}/interactions`, { signal }),

  // 后端还有 GET /interactions/{id}，这里没接：列表已经带回全部字段，
  // 抽屉直接用手上的行即可，再打一次是多余往返。要做单条深链时再补。
}

/** admin/tag.py */
export const tagApi = {
  list: (channelId: string, signal?: AbortSignal) =>
    request<Tag[]>(`/channels/${channelId}/tags`, { signal }),

  create: (
    channelId: string,
    body: {
      name: string
      instruction: string
      role?: string
      output_style?: string
      created_by?: string
    },
  ) => request<Tag>(`/channels/${channelId}/tags`, { method: 'POST', body }),

  /** 频道 id 入路径是后端要求的：据此确认标签确实属于该频道。 */
  setActive: (channelId: string, tagId: string, active: boolean) =>
    request<Tag>(`/channels/${channelId}/tags/${tagId}`, { method: 'PATCH', body: { active } }),

  remove: (channelId: string, tagId: string, actor?: string) =>
    request<{ status: string }>(`/channels/${channelId}/tags/${tagId}`, {
      method: 'DELETE',
      query: { actor },
    }),
}

/** admin/__init__.py 的 /health。匿名可打，用来判后端是否在线。 */
export const healthApi = {
  check: (signal?: AbortSignal) => request<{ status: string }>('/health', { signal }),
}
