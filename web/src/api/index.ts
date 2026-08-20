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
  EmbeddingState,
  Interaction,
  McpServer,
  McpTestResult,
  ChannelSkills,
  Memory,
  MemoryType,
  PendingApproval,
  Policy,
  Skill,
  SkillFile,
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
  /**
   * 默认只返回现行事实。`includeSuperseded` 连已被取代的一起取 —— 排查
   * 「这条事实之前是什么」时要用，日常看「现在记着什么」不该开。
   */
  list: (channelId: string, signal?: AbortSignal, includeSuperseded = false) => {
    const qs = includeSuperseded ? '?include_superseded=true' : ''
    return request<Memory[]>(`/channels/${channelId}/memories${qs}`, { signal })
  },

  /**
   * 手工写入。写前后端会查冲突 —— 库里有疑似说同一件事的现行记忆时抛
   * **409**，`ApiError.body` 里带候选列表（用 `readMemoryConflictDetail` 读）。
   *
   * 撞上 409 后要么给 `supersede_id`（取代那条），要么给 `force`（明确要并列
   * 存在）。两者不能同时给，后端报 400 —— 它们表达的是相反的意图。
   *
   * 没有单独的「预检」端点：409 已经把候选带回来了，再打一次是多余往返。
   */
  create: (
    channelId: string,
    body: {
      content: string
      type?: MemoryType
      user_id?: string
      /** 取代这条既有记忆（新写一条 + 给旧条目打 superseded_by）。 */
      supersede_id?: string
      /** 跳过冲突检查，与已有记忆并列写入。 */
      force?: boolean
    },
  ) => request<Memory>(`/channels/${channelId}/memories`, { method: 'POST', body }),

  /**
   * 改内容与/或类型。原地改，保留 id 与 created_at。
   *
   * 这是「这条写错了」的路径。「事实变了」由蒸馏的 UPDATE 动作走 supersede ——
   * 新写一条、旧条目标记 superseded_by，两条都留着。改内容会触发向量重算。
   */
  update: (entryId: string, body: { content?: string; type?: MemoryType; actor?: string }) =>
    request<Memory>(`/memories/${entryId}`, { method: 'PATCH', body }),

  /** 删除按条目 id，不带频道 —— 后端路由就是 /memories/{entry_id}。 */
  remove: (entryId: string, actor?: string) =>
    request<{ status: string }>(`/memories/${entryId}`, {
      method: 'DELETE',
      query: { actor },
    }),

  /**
   * embedder 是否可用。不可用时记忆能力有三层降级（语义检索关闭、写入的冲突检查
   * 退化为字面比对、蒸馏的去重对旧记忆失效），记忆页据此挂提示。
   *
   * 受令牌保护而非挂在匿名的 `/health` 上：「这个部署有没有配 embedding」是运营
   * 信息。所以这里不能像 healthApi 那样当探针用。
   */
  embedding: (signal?: AbortSignal) => request<EmbeddingState>('/embedding', { signal }),
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

  /**
   * ⚠️ 配了 `approval_required_tools` 却不给 `approver_ids` 会被后端拒（422）：
   * 那些工具将永远无法执行。反过来「只配审批人」是允许的。
   */
  set: (
    channelId: string,
    body: {
      allowed_tools: string[]
      ambient_rules: AmbientRule[]
      approval_required_tools?: Record<string, number>
      approver_ids?: string[]
      actor?: string
    },
  ) => request<Policy>(`/channels/${channelId}/policy`, { method: 'PUT', body }),

  /** 已注册的工具名。硬编码一份会随 build_tools() 增删而漂移。 */
  tools: (signal?: AbortSignal) => request<string[]>('/tools', { signal }),
}

/**
 * admin/approval.py —— 待审批操作。**只读**。
 *
 * 没有 approve/deny 方法，这是有意的：Admin API 只有一个共享令牌，actor 是
 * 前端随便填的，而审批的审计链不该建在不可信字段上。放行要回频道线程里打
 * `/approve`（那里的 user_id 是平台签过名的）。
 */
export const approvalApi = {
  list: (channelId: string, signal?: AbortSignal) =>
    request<PendingApproval[]>(`/channels/${channelId}/approvals`, { signal }),
}

/** admin/audit.py */
export const auditApi = {
  list: (channelId: string, limit = 200, signal?: AbortSignal) =>
    request<AuditLog[]>(`/channels/${channelId}/audit`, { query: { limit }, signal }),

  /**
   * 全局资源的变更流水（目前只有技能库：技能是全局定义的，改正文这个动作
   * 没有频道可归属）。
   *
   * 专门端点而非 `/channels/global/audit` —— 后端用哪个哨兵值存这些记录是它的
   * 内部约定，不该在前端再写一份。
   */
  listGlobal: (limit = 200, signal?: AbortSignal) =>
    request<AuditLog[]>('/audit/global', { query: { limit }, signal }),
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

/** admin/mcp.py —— MCP server 管理。 */
export const mcpApi = {
  list: (channelId: string, signal?: AbortSignal) =>
    request<McpServer[]>(`/channels/${channelId}/mcp-servers`, { signal }),

  create: (channelId: string, body: { name: string; url: string; headers?: Record<string, string>; enabled?: boolean }) =>
    request<McpServer>(`/channels/${channelId}/mcp-servers`, { method: 'POST', body }),

  update: (channelId: string, serverId: string, body: Partial<Pick<McpServer, 'name' | 'url' | 'headers' | 'enabled'>>) =>
    request<McpServer>(`/channels/${channelId}/mcp-servers/${serverId}`, { method: 'PUT', body }),

  remove: (channelId: string, serverId: string) =>
    request<{ ok: boolean }>(`/channels/${channelId}/mcp-servers/${serverId}`, { method: 'DELETE' }),

  /** 连接测试：传 url + headers，成功返回该 server 暴露的工具名。 */
  test: (channelId: string, body: { url: string; headers?: Record<string, string> }) =>
    request<McpTestResult>(`/channels/${channelId}/mcp-servers/test`, { method: 'POST', body }),
}

/**
 * admin/skill.py —— 技能管理。
 *
 * 本控制台里唯一的**全局**资源：技能定义一份，各频道勾选启用。故这里的路径
 * 分两组 —— `/skills*` 是全局库，`/channels/{id}/skills` 是某频道的启用集合。
 *
 * 改动即时生效，不需要重启 worker（对比 mcpApi：那边要重启才装载）。
 */
export const skillApi = {
  /** 全局库，带正文与文件摘要。 */
  list: (signal?: AbortSignal) => request<Skill[]>('/skills', { signal }),

  create: (body: {
    name: string
    description: string
    content: string
    enabled?: boolean
    actor?: string
  }) => request<Skill>('/skills', { method: 'POST', body }),

  /** 只传要改的字段；未传的后端不动。 */
  update: (
    skillId: string,
    body: Partial<Pick<Skill, 'name' | 'description' | 'content' | 'enabled'>> & {
      actor?: string
    },
  ) => request<Skill>(`/skills/${skillId}`, { method: 'PUT', body }),

  remove: (skillId: string, actor?: string) =>
    request<{ ok: boolean }>(`/skills/${skillId}`, { method: 'DELETE', query: { actor } }),

  // ---- 附带文件 ----

  /** 单个文件，**带内容**。列表只给摘要，编辑时经此单取。 */
  getFile: (skillId: string, fileId: string, signal?: AbortSignal) =>
    request<SkillFile>(`/skills/${skillId}/files/${fileId}`, { signal }),

  /**
   * 新建文件。后端校验路径形态与 64 KB 上限（按 UTF-8 字节），
   * 同技能内路径重复报 409。
   */
  createFile: (
    skillId: string,
    body: { path: string; description: string; content: string; actor?: string },
  ) => request<SkillFile>(`/skills/${skillId}/files`, { method: 'POST', body }),

  updateFile: (
    skillId: string,
    fileId: string,
    body: Partial<Pick<SkillFile, 'path' | 'description' | 'content'>> & { actor?: string },
  ) => request<SkillFile>(`/skills/${skillId}/files/${fileId}`, { method: 'PUT', body }),

  removeFile: (skillId: string, fileId: string, actor?: string) =>
    request<{ ok: boolean }>(`/skills/${skillId}/files/${fileId}`, {
      method: 'DELETE',
      query: { actor },
    }),

  // ---- 频道启用 ----

  /** 全局库 + 该频道勾选状态，一次取回。 */
  forChannel: (channelId: string, signal?: AbortSignal) =>
    request<ChannelSkills>(`/channels/${channelId}/skills`, { signal }),

  /**
   * 覆盖式设置该频道启用的技能：传最终应启用的完整 id 列表。
   *
   * 不存在的 id 会被后端静默丢弃，返回的 `enabled_ids` 才是实际生效的集合
   * （管理页的勾选基于上一次拉到的列表，期间有人删了技能时会有幽灵 id）。
   */
  setForChannel: (channelId: string, skillIds: string[], actor?: string) =>
    request<{ enabled_ids: string[] }>(`/channels/${channelId}/skills`, {
      method: 'PUT',
      body: { skill_ids: skillIds, actor },
    }),
}
