import { ReloadOutlined } from '@ant-design/icons'
import {
  Card,
  Descriptions,
  Drawer,
  Segmented,
  Space,
  Table,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'
import { Button } from 'antd'

import { auditApi } from '@/api'
import type { AuditLog } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import {
  AUDIT_ACTION_OPTIONS,
  AUDIT_RESULT_OPTIONS,
  AuditActionTag,
  AuditResultTag,
} from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatFromNow, formatNumber, formatTime } from '@/lib/format'

/** 后端 limit 默认 100。给几档常用值，不做无上限翻页 —— 审计是只增表。 */
const LIMITS = [100, 200, 500]

export function AuditPage() {
  const channelId = useChannelId()
  const [limit, setLimit] = useState(200)
  const [detail, setDetail] = useState<AuditLog | null>(null)

  const state = useAsync(
    useCallback(
      (signal: AbortSignal) => auditApi.list(channelId, limit, signal),
      [channelId, limit],
    ),
    [channelId, limit],
  )

  const columns: ColumnsType<AuditLog> = [
    {
      title: '时间',
      dataIndex: 'ts',
      width: 120,
      sorter: (a, b) => a.ts.localeCompare(b.ts),
      defaultSortOrder: 'descend',
      render: (v: string) => (
        <Tooltip title={formatTime(v)}>
          <Typography.Text type="secondary">{formatFromNow(v)}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: '动作',
      dataIndex: 'action',
      width: 120,
      filters: AUDIT_ACTION_OPTIONS,
      onFilter: (v, r) => r.action === v,
      render: (v: AuditLog['action']) => <AuditActionTag value={v} />,
    },
    {
      title: '结果',
      dataIndex: 'result',
      width: 90,
      filters: AUDIT_RESULT_OPTIONS,
      onFilter: (v, r) => r.result === v,
      render: (v: AuditLog['result']) => <AuditResultTag value={v} />,
    },
    {
      title: '用户',
      dataIndex: 'user_id',
      width: 140,
      render: (v: string | null) =>
        v ? (
          <Typography.Text className="mono">{v}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">系统</Typography.Text>
        ),
    },
    {
      title: 'token',
      dataIndex: 'tokens_consumed',
      width: 100,
      align: 'right',
      sorter: (a, b) => a.tokens_consumed - b.tokens_consumed,
      render: (v: number) =>
        v > 0 ? (
          <Typography.Text className="mono">{formatNumber(v)}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: '关联任务',
      dataIndex: 'task_id',
      width: 130,
      render: (v: string | null) =>
        v ? <EntityId id={v} /> : <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      key: 'action-col',
      width: 70,
      align: 'right',
      render: (_, r) => (
        <Button type="link" size="small" onClick={() => setDetail(r)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title="审计"
        description="每个动作留一条记录，仅追加不可改。工具被拒、预算耗尽这类事件都在这里。"
        extra={
          <Space>
            <Segmented
              value={limit}
              onChange={(v) => setLimit(v as number)}
              options={LIMITS.map((n) => ({ label: `近 ${n} 条`, value: n }))}
            />
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
          </Space>
        }
      />

      <AsyncBoundary state={state} skeletonRows={8}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock description="这个频道还没有审计记录" />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table
                rowKey="id"
                columns={columns}
                dataSource={list}
                size="middle"
                pagination={{ pageSize: 30, showSizeChanger: true, hideOnSinglePage: true }}
              />
            </Card>
          )
        }
      </AsyncBoundary>

      {/* size 而非 width：v6 起 width 已废弃 */}
      <Drawer title="审计详情" open={detail !== null} onClose={() => setDetail(null)} size={520}>
        {detail && (
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="时间">{formatTime(detail.ts)}</Descriptions.Item>
            <Descriptions.Item label="动作">
              <AuditActionTag value={detail.action} />
            </Descriptions.Item>
            <Descriptions.Item label="结果">
              <AuditResultTag value={detail.result} />
            </Descriptions.Item>
            <Descriptions.Item label="用户">
              {detail.user_id ?? <Typography.Text type="secondary">系统</Typography.Text>}
            </Descriptions.Item>
            <Descriptions.Item label="消耗 token">
              {formatNumber(detail.tokens_consumed)}
            </Descriptions.Item>
            <Descriptions.Item label="关联任务">
              {detail.task_id ? (
                <EntityId id={detail.task_id} tail={12} />
              ) : (
                <Typography.Text type="secondary">—</Typography.Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="记录 ID">
              <EntityId id={detail.id} tail={12} />
            </Descriptions.Item>
            <Descriptions.Item label="detail">
              {/* detail 是自由 dict，形状随动作而异，故原样出 JSON 而不逐字段渲染 */}
              <Typography.Text className="mono wrap-anywhere" style={{ fontSize: 12 }}>
                {JSON.stringify(detail.detail, null, 2)}
              </Typography.Text>
            </Descriptions.Item>
          </Descriptions>
        )}
      </Drawer>
    </>
  )
}
