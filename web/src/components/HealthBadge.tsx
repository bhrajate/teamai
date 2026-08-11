import { Badge, Tooltip } from 'antd'
import { useCallback, useEffect, useState } from 'react'

import { healthApi } from '@/api'

/**
 * 后端在线指示。/health 匿名可打，故它能区分「后端没起」与「令牌不对」——
 * 这两种都会让页面一片空白，但解法完全不同。
 */
export function HealthBadge() {
  const [ok, setOk] = useState<boolean | undefined>(undefined)

  const ping = useCallback(async (signal: AbortSignal) => {
    try {
      const r = await healthApi.check(signal)
      setOk(r.status === 'ok')
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      setOk(false)
    }
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    void ping(ctrl.signal)
    // 30s 一次。再密没意义 —— 这只是个状态灯，真正的失败由各页面自己报
    const timer = window.setInterval(() => void ping(ctrl.signal), 30_000)
    return () => {
      ctrl.abort()
      window.clearInterval(timer)
    }
  }, [ping])

  const meta =
    ok === undefined
      ? { status: 'default' as const, text: '检测中', tip: '正在探测后端' }
      : ok
        ? { status: 'success' as const, text: '在线', tip: '/api/health 正常' }
        : { status: 'error' as const, text: '离线', tip: '打不通 /api/health，确认 web 进程已启动' }

  return (
    <Tooltip title={meta.tip}>
      <Badge status={meta.status} text={<span style={{ fontSize: 13 }}>{meta.text}</span>} />
    </Tooltip>
  )
}
