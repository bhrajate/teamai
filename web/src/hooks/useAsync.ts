import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '@/api/client'
import { onTokenChange } from '@/lib/auth'

export type AsyncState<T> = {
  data: T | undefined
  loading: boolean
  error: ApiError | undefined
  /** 手动重取。改完数据后调它刷新，不必自己搬状态。 */
  reload: () => void
}

/**
 * 取数原语：并发去重 + 卸载后不 setState + 令牌变更自动重取。
 *
 * 没上 TanStack Query 之类：本控制台的页面都是「进来取一次、改完重取」，
 * 没有跨页缓存复用的需求，一个 hook 足够，且少一层依赖。
 *
 * `deps` 变化即重取。传函数进来时务必自己 useCallback，否则每次渲染都是新函数，
 * 会打成取数死循环。
 */
export function useAsync<T>(
  fn: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiError | undefined>(undefined)
  const [nonce, setNonce] = useState(0)

  // 前一次请求的控制器。deps 变化时先中止旧的，避免旧响应后到覆盖新数据。
  const inflight = useRef<AbortController | undefined>(undefined)

  const reload = useCallback(() => setNonce((n) => n + 1), [])

  // deps 摊进下面那个 useEffect 的依赖数组，故长度必须每次渲染都一样 ——
  // 变长会让 React 把不同位置的依赖两两错配比较，重取时机变得不可预期，
  // 且 React 只在 dev 下警告。这里显式拦一道，把问题指到调用点。
  const depsLength = useRef(deps.length)
  if (import.meta.env.DEV && depsLength.current !== deps.length) {
    throw new Error(
      `useAsync 的 deps 长度从 ${depsLength.current} 变成了 ${deps.length}。` +
        '它必须是定长数组 —— 条件式地增删依赖项会让重取时机不可预期。' +
        '要按条件取数就传固定长度的 deps，在 fn 内部分支。',
    )
  }

  useEffect(() => {
    inflight.current?.abort()
    const ctrl = new AbortController()
    inflight.current = ctrl

    let alive = true
    setLoading(true)
    setError(undefined)

    fn(ctrl.signal)
      .then((value) => {
        if (!alive) return
        setData(value)
      })
      .catch((err: unknown) => {
        // 主动中止不是失败，静默 —— 否则切频道时会闪一下错误态
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (!alive) return
        setError(err instanceof ApiError ? err : new ApiError(0, String(err)))
        setData(undefined)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })

    return () => {
      alive = false
      ctrl.abort()
    }
    // fn 由调用方保证稳定（useCallback），故不入依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  // 在「设置」页填完令牌后，各页面应自动恢复，不必手动刷浏览器
  useEffect(() => onTokenChange(reload), [reload])

  return { data, loading, error, reload }
}
