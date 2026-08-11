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

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
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

/** 把 FastAPI 的 detail 读成一句话。422 的 detail 是数组，得摊平。 */
function readDetail(body: unknown, status: number): string {
  if (typeof body === 'string' && body) return body
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail
    if (typeof d === 'string') return d
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

  if (!resp.ok) throw new ApiError(resp.status, readDetail(parsed, resp.status))
  return parsed as T
}
