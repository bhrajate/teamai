import { Select, Space, Typography } from 'antd'
import { useCallback } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import { channelApi } from '@/api'
import { PlatformTag } from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'

/**
 * 顶栏的频道切换器。
 *
 * 切换时保留当前所在的资源页 —— 在「审计」页换频道，应该还停在审计页，
 * 而不是被弹回概览。运维排查通常是「同一个视角横向比几个频道」。
 */
export function ChannelSwitcher() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { channelId } = useParams<{ channelId: string }>()

  const list = useAsync(useCallback((signal: AbortSignal) => channelApi.list(signal), []), [])

  const onChange = (next: string) => {
    const tail = channelId ? (pathname.split(`/channels/${channelId}`)[1] ?? '') : ''
    navigate(`/channels/${next}${tail}`)
  }

  const options = (list.data ?? []).map((c) => ({
    value: c.id,
    label: (
      <Space size={6}>
        <PlatformTag value={c.platform} />
        <span className="mono">{c.channel_id}</span>
      </Space>
    ),
    // Select 的搜索按这个字段匹配，故把可搜的信息都塞进来
    title: `${c.platform} ${c.channel_id} ${c.workspace_id} ${c.id}`,
  }))

  return (
    <Space size={10}>
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
        频道
      </Typography.Text>
      <Select
        value={channelId}
        onChange={onChange}
        options={options}
        loading={list.loading}
        showSearch
        // 默认按 label 匹配，但 label 是 ReactNode 匹配不了，故改按 title
        optionFilterProp="title"
        placeholder={list.error ? '频道列表加载失败' : '选择频道'}
        style={{ minWidth: 280 }}
        status={list.error ? 'warning' : undefined}
        notFoundContent={list.loading ? '加载中…' : '还没有频道实例'}
      />
    </Space>
  )
}
