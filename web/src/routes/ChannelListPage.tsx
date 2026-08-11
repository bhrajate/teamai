import { ReloadOutlined, RightOutlined } from '@ant-design/icons'
import { Button, Card, Flex, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { channelApi } from '@/api'
import type { Channel } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { PlatformTag } from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { formatFromNow, formatTime } from '@/lib/format'

/**
 * 频道实例总览。控制台的落地页 —— 其余页面都要先有 channel_instance_id。
 *
 * 频道由平台事件自动创建（ChannelService.get_or_create），控制台里只读，
 * 故本页没有「新建」按钮：想多一个频道，去 Slack/飞书把机器人拉进群。
 */
export function ChannelListPage() {
  const state = useAsync(useCallback((signal: AbortSignal) => channelApi.list(signal), []), [])

  const columns: ColumnsType<Channel> = [
    {
      title: '平台',
      dataIndex: 'platform',
      width: 96,
      filters: [
        { text: 'Slack', value: 'slack' },
        { text: '飞书', value: 'feishu' },
      ],
      onFilter: (v, r) => r.platform === v,
      render: (v: string) => <PlatformTag value={v} />,
    },
    {
      title: '频道',
      dataIndex: 'channel_id',
      render: (v: string, r) => (
        <Space direction="vertical" size={0}>
          <Link to={`/channels/${r.id}`}>
            <Typography.Text strong className="mono">
              {v}
            </Typography.Text>
          </Link>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            工作区 <span className="mono">{r.workspace_id}</span>
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '实例 ID',
      dataIndex: 'id',
      width: 150,
      render: (v: string) => <EntityId id={v} />,
    },
    {
      title: '开关',
      key: 'flags',
      width: 190,
      render: (_, r) => (
        <Space size={4} wrap>
          {r.ambient_enabled ? (
            <Tooltip title="允许在无人 @ 时主动开口">
              <Tag color="lime">主动介入</Tag>
            </Tooltip>
          ) : null}
          {r.cross_channel_learning ? (
            <Tooltip title="允许检索其他频道的记忆">
              <Tag color="purple">跨频道学习</Tag>
            </Tooltip>
          ) : null}
          {!r.ambient_enabled && !r.cross_channel_learning ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              均关闭
            </Typography.Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: '策略',
      dataIndex: 'policy_id',
      width: 110,
      render: (v: string | null) =>
        v ? (
          <Tag color="blue">已配置</Tag>
        ) : (
          <Tooltip title="未配策略时按默认白名单执行">
            <Tag>未配置</Tag>
          </Tooltip>
        ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 130,
      // id 是 ULID，字典序即时间序，故直接按 id 排比解析日期便宜
      sorter: (a, b) => a.id.localeCompare(b.id),
      defaultSortOrder: 'descend',
      render: (v: string) => (
        <Tooltip title={formatTime(v)}>
          <Typography.Text type="secondary">{formatFromNow(v)}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      key: 'action',
      width: 60,
      align: 'right',
      render: (_, r) => (
        <Link to={`/channels/${r.id}`} aria-label="进入频道">
          <Button type="text" size="small" icon={<RightOutlined />} />
        </Link>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title="频道实例"
        description="每个频道一个共享实例，团队全员共用同一份记忆与预算。实例由平台事件自动创建，此处只读。"
        extra={
          <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
            刷新
          </Button>
        }
      />

      <AsyncBoundary state={state} skeletonRows={6}>
        {(channels) =>
          channels.length === 0 ? (
            <Card>
              <EmptyBlock
                description={
                  <Flex vertical align="center" gap={4}>
                    <Typography.Text>还没有频道实例</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      把机器人拉进 Slack 或飞书的群，在群里 @ 它一次即会建实例
                    </Typography.Text>
                  </Flex>
                }
              />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table
                rowKey="id"
                columns={columns}
                dataSource={channels}
                pagination={false}
                size="middle"
              />
            </Card>
          )
        }
      </AsyncBoundary>
    </>
  )
}
