import { ClockCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Descriptions, Flex, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback } from 'react'

import { approvalApi } from '@/api'
import type { PendingApproval } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatFromNow, formatTime } from '@/lib/format'

/**
 * 待审批操作。**只读** —— 这里看不到批准按钮，是有意的。
 *
 * Admin API 只有一个共享令牌，操作者身份是前端随便填的字符串；而审批的审计链
 * 不该建在不可信字段上。放行要回频道线程里打 `/approve`，那里的用户 id 是平台
 * 签过名的。所以本页的用途是「看见有东西在等批 + 看全参数」，然后去线程里批。
 *
 * 四眼原则：发起人不能批准自己的操作，故「发起人」这一列同时也是「不能批的人」。
 */
export function ApprovalPage() {
  const channelId = useChannelId()

  const state = useAsync(
    useCallback((signal: AbortSignal) => approvalApi.list(channelId, signal), [channelId]),
    [channelId],
  )

  const columns: ColumnsType<PendingApproval> = [
    {
      title: '工具',
      dataIndex: 'tool_name',
      width: 170,
      render: (v: string, r) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong className="mono">
            {v}
          </Typography.Text>
          {r.required > 1 && (
            <Tooltip title="四眼原则：需要两位不同的审批人各批一次">
              <Tag color="orange">需 {r.required} 人 · {r.progress}</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '参数',
      dataIndex: 'args',
      render: (args: Record<string, unknown>) => (
        <Descriptions size="small" column={1} colon={false} style={{ maxWidth: 460 }}>
          {Object.entries(args).map(([k, v]) => (
            <Descriptions.Item key={k} label={<Typography.Text type="secondary">{k}</Typography.Text>}>
              <Typography.Text className="mono wrap-anywhere" style={{ fontSize: 12 }}>
                {typeof v === 'string' ? v : JSON.stringify(v)}
              </Typography.Text>
            </Descriptions.Item>
          ))}
        </Descriptions>
      ),
    },
    {
      title: '发起人',
      dataIndex: 'requester_id',
      width: 150,
      render: (v: string) => (
        <Tooltip title="发起人不能批准自己的操作（四眼原则）">
          <Typography.Text className="mono">{v}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: '该谁批',
      dataIndex: 'owner_id',
      width: 150,
      render: (v: string | null) =>
        v ? (
          <Typography.Text className="mono">{v}</Typography.Text>
        ) : (
          <Tooltip title="任务没有指定负责人，由权限策略里的频道审批人批准">
            <Typography.Text type="secondary">频道审批人</Typography.Text>
          </Tooltip>
        ),
    },
    {
      title: '已批',
      dataIndex: 'approved_by',
      width: 130,
      render: (v: string[]) =>
        v.length ? (
          <Space size={4} wrap>
            {v.map((u) => (
              <Tag key={u} color="green">
                {u}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: '等了多久',
      dataIndex: 'created_at',
      width: 120,
      render: (v: string) => (
        <Tooltip title={formatTime(v)}>
          <Space size={4}>
            <ClockCircleOutlined />
            <Typography.Text type="secondary">{formatFromNow(v)}</Typography.Text>
          </Space>
        </Tooltip>
      ),
    },
    {
      title: '任务',
      dataIndex: 'task_id',
      width: 130,
      render: (v: string) => <EntityId id={v} />,
    },
  ]

  return (
    <>
      <PageHeader
        title="待审批"
        description="需要人工批准才能执行的工具调用。批准要回频道线程里操作 —— 这里只能查看。"
        extra={
          <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
            刷新
          </Button>
        }
      />

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        title="批准要在频道线程里做"
        description={
          <Flex vertical gap={4}>
            <Typography.Text>
              在对应线程回复 <Typography.Text code>/approve</Typography.Text> 放行，
              <Typography.Text code>/deny 理由</Typography.Text> 拒绝；
              改参数后放行用 <Typography.Text code>/approve key=值</Typography.Text>。
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              控制台不提供批准按钮：这里的操作者身份来自共享的 Admin 令牌，无法确认是谁，
              而审批必须能追溯到具体的人。频道里的用户身份由平台签名，可信。
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              发起人不能批准自己的操作。超过配置的等待时长（默认一天）后按拒绝处理。
            </Typography.Text>
          </Flex>
        }
      />

      <AsyncBoundary state={state} skeletonRows={4}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock description="当前没有等待审批的操作。需要审批的工具在「权限策略」页配置。" />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table
                rowKey="task_id"
                columns={columns}
                dataSource={list}
                size="middle"
                pagination={false}
              />
            </Card>
          )
        }
      </AsyncBoundary>
    </>
  )
}
