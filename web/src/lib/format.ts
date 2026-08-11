import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import relativeTime from 'dayjs/plugin/relativeTime'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

/**
 * 后端一律给 ISO 8601 带时区（serializers 里全走 `.isoformat()`，
 * 且模型的 datetime 都是 UTC aware），dayjs 解析后按本机时区显示。
 */
export function formatTime(iso: string): string {
  return dayjs(iso).format('YYYY-MM-DD HH:mm:ss')
}

export function formatDate(iso: string): string {
  return dayjs(iso).format('YYYY-MM-DD')
}

/** 「3 分钟前」。列表里比绝对时间好扫，绝对时间放 tooltip。 */
export function formatFromNow(iso: string): string {
  return dayjs(iso).fromNow()
}

/** 千分位。token 数动辄六七位，不分组读不出量级。 */
export function formatNumber(n: number): string {
  return n.toLocaleString('zh-CN')
}

/** 大数收成 1.2M / 34.5K，用在卡片标题这类窄位置。 */
export function formatCompact(n: number): string {
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

/** 百分比，无上限截断 —— 超额时要能看出超了多少。 */
export function percent(used: number, total: number): number {
  if (total <= 0) return 0
  return Math.round((used / total) * 100)
}

/** ULID 太长，列表里只显示尾段，完整值放 tooltip 与复制按钮。 */
export function shortId(id: string, tail = 6): string {
  const [prefix, rest] = id.split('_')
  if (!rest) return id
  return `${prefix}_…${rest.slice(-tail)}`
}
