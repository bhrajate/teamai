/**
 * fetch 封装：拼基址、带令牌、把各种失败归一成 ApiError。
 *
 * 全前端唯一的网络出口。这样「令牌怎么带」「错误怎么读」只有一处实现 ——
 * 后端把「同一个领域对象不许在多个路由里拼成不同形状」收在 serializers.py，
 * 这里对应的是「同一种失败不许在多个页面里各判一次」。
 */

import { getToken } from '@/lib/auth'

/** 留空即用相对路径 /api（dev 走 vite proxy，生产同源部署亦然）。 */
const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  /** 后端 detail 字段；FastAPI 的 HTTPException 与校验错误都落在这里。 */
  readonly detail: string
  /**
   * 原始响应体，未做任何解读。
   *
   * `detail` 是拍平成一句话的结果，够用于「弹个错误提示」，但有些失败自带调用方
   * 要用的结构化数据 —— 记忆写入的 409 会带一份候选冲突列表（见
   * `adapters/admin/memory.py` 的 `_conflict_detail`），页面靠它渲染选择界面。
   *
   * 刻意留成 `unknown` 并且不在这里加 `conflicts` 之类的取值器：本文件的职责是
   * 「把各种失败归一」，塞进领域概念就破了它自己的边界（见文件头）。要用的页面
   * 自己按类型收窄。
   */
  readonly body: unknown

  constructor(status: number, detail: string, body?: unknown) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.body = body
  }

  /** 401：令牌缺失或不对，调用方据此引导去「设置」页。 */
  get isUnauthorized(): boolean {
    return this.status === 401
  }

  /** 404 在本控制台是常态而非故障 —— 频道尚未配预算/策略时就是 404。 */
  get isNotFound(): boolean {
    return this.status === 404
  }
}

/**
 * 把 FastAPI 的 detail 读成一句话。422 的 detail 是数组，得摊平；
 * 结构化的 detail（记忆写入 409）是对象，取它的 message 字段。
 */
function readDetail(body: unknown, status: number): string {
  if (typeof body === 'string' && body) return body
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail
    if (typeof d === 'string') return d
    // 对象形态的 detail。不加这个分支的话，带结构化数据的失败会一路掉到底部的
    // 兜底文案「请求失败（HTTP 409）」—— 后端明明给了一句人话却显示不出来。
    if (d && typeof d === 'object' && !Array.isArray(d) && 'message' in d) {
      const msg = (d as { message: unknown }).message
      if (typeof msg === 'string' && msg) return msg
    }
    if (Array.isArray(d)) {
      const parts = d
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            const loc = 'loc' in item && Array.isArray(item.loc) ? item.loc.join('.') : ''
            return loc ? `${loc}: ${String(item.msg)}` : String(item.msg)
          }
          return String(item)
        })
        .filter(Boolean)
      if (parts.length) return parts.join('；')
    }
  }
  return `请求失败（HTTP ${status}）`
}

type Options = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  query?: Record<string, string | number | boolean | undefined>
  signal?: AbortSignal
}

export async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { method = 'GET', body, query, signal } = options

  let url = `${BASE}/api${path}`
  if (query) {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== '') qs.set(k, String(v))
    }
    if (qs.size) url += `?${qs}`
  }

  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let resp: Response
  try {
    resp = await fetch(url, {
      method,
      headers,
      signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  } catch (err) {
    // AbortError 要原样抛出：调用方靠它区分「组件卸载了」与「请求真失败了」，
    // 归一成 ApiError 会让已卸载的页面弹出无意义的错误提示。
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    // 网络层失败（后端没起、跨源被拦、DNS）拿不到 status，用 0 占位
    throw new ApiError(0, '连不上后端。确认 web 进程已启动，且跨源部署时已放行本站来源。')
  }

  if (resp.status === 204) return undefined as T

  const text = await resp.text()
  let parsed: unknown = text
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      // 非 JSON 响应（网关的 HTML 错误页）保持原文，交给 readDetail 兜底
    }
  }

  if (!resp.ok) throw new ApiError(resp.status, readDetail(parsed, resp.status), parsed)
  return parsed as T
}
