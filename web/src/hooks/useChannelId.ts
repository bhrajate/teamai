import { useParams } from 'react-router-dom'

/**
 * 取路由里的 channelId。
 *
 * 路由表保证 `/channels/:channelId/*` 下必有此参数，故这里断言非空 ——
 * 若为空说明路由配错了，早崩比在页面上发出 `/channels/undefined/tasks`
 * 这种请求好定位。
 */
export function useChannelId(): string {
  const { channelId } = useParams<{ channelId: string }>()
  if (!channelId) throw new Error('路由缺少 channelId 参数')
  return channelId
}
