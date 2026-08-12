import { ArrowRightOutlined, ReloadOutlined } from '@ant-design/icons'
import { Button, Card, Flex, Space, Table, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { taskApi } from '@/api'
import type { Task } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { ModelLevelTag, TASK_STATUS_OPTIONS, TaskStatusTag } from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatFromNow, formatTime } from '@/lib/format'

/**
 * 任务列表。只读 —— 任务的创建与流转都由平台消息驱动，控制台不提供人工改状态：
 * 状态机有合法迁移表（domain/models/task.py），绕过它改会把任务改成非法态。
 */
export function TaskPage() {
  const channelId = useChannelId()
  const state = useAsync(
    useCallback((signal: AbortSignal) => taskApi.list(channelId, signal), [channelId]),
    [channelId],
  )

  const columns: ColumnsType<Task> = [
    {
      title: '意图',
      dataIndex: 'intent',
      render: (v: string, r) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{v || '（未分类）'}</Typography.Text>
          {r.tag_name && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              标签 {r.tag_name}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      filters: TASK_STATUS_OPTIONS,
      onFilter: (v, r) => r.status === v,
      render: (v: Task['status']) => <TaskStatusTag value={v} />,
    },
    {
      title: '模型档位',
      dataIndex: 'model_level',
      width: 120,
      render: (v: string) => <ModelLevelTag value={v} />,
    },
    {
      title: '发起人',
      dataIndex: 'requester_id',
      width: 150,
      render: (v: string | null) =>
        v ? (
          <Typography.Text className="mono">{v}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: '任务 ID',
      dataIndex: 'id',
      width: 140,
      render: (v: string) => <EntityId id={v} />,
    },
    {
      title: '更新',
      dataIndex: 'updated_at',
      width: 120,
      sorter: (a, b) => a.updated_at.localeCompare(b.updated_at),
      defaultSortOrder: 'descend',
      render: (v: string) => (
        <Tooltip title={formatTime(v)}>
          <Typography.Text type="secondary">{formatFromNow(v)}</Typography.Text>
        </Tooltip>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title="任务"
        description="长任务由 worker 进程消费，完成后回帖到原线程。此处只读，状态流转由消息驱动。"
        extra={
          <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
            刷新
          </Button>
        }
      />

      <AsyncBoundary state={state} skeletonRows={6}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock description="这个频道还没有任务。在群里 @ 机器人派一件活试试。" />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table
                rowKey="id"
                columns={columns}
                dataSource={list}
                size="middle"
                pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: true }}
                expandable={{
                  // 创建时间不值得占一列，但排查时要看，收进展开行
                  expandedRowRender: (r) => (
                    <Flex justify="space-between" align="center" gap={16} wrap>
                      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                        创建于 {formatTime(r.created_at)}，最后更新 {formatTime(r.updated_at)}
                      </Typography.Text>
                      {/* 任务本身只有状态，答不了「为什么是这个结果」。那要看当时的提示词与响应。 */}
                      <Link to={`/channels/${channelId}/interactions?task=${r.id}`}>
                        看交互记录 <ArrowRightOutlined />
                      </Link>
                    </Flex>
                  ),
                }}
              />
            </Card>
          )
        }
      </AsyncBoundary>
    </>
  )
}
