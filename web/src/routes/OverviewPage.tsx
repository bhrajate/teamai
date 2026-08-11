import { ArrowRightOutlined } from '@ant-design/icons'
import {
  App,
  Card,
  Col,
  Descriptions,
  Flex,
  Progress,
  Row,
  Space,
  Statistic,
  Switch,
  Tooltip,
  Typography,
} from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { budgetApi, channelApi, taskApi } from '@/api'
import type { Budget, Task } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary } from '@/components/states'
import { BudgetStateTag, PlatformTag, TaskStatusTag } from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatCompact, formatNumber, formatTime, percent } from '@/lib/format'

/** 「在动的」任务 —— 概览要突出这几个，终态任务翻列表就行。 */
const LIVE = new Set(['PENDING', 'RUNNING', 'WAITING_INPUT', 'PAUSED'])

export function OverviewPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [saving, setSaving] = useState(false)

  const channel = useAsync(
    useCallback((signal: AbortSignal) => channelApi.get(channelId, signal), [channelId]),
    [channelId],
  )
  const tasks = useAsync(
    useCallback((signal: AbortSignal) => taskApi.list(channelId, signal), [channelId]),
    [channelId],
  )
  const budget = useAsync(
    useCallback((signal: AbortSignal) => budgetApi.get(channelId, signal), [channelId]),
    [channelId],
  )

  const toggle = async (
    field: 'ambient_enabled' | 'cross_channel_learning',
    value: boolean,
  ) => {
    setSaving(true)
    try {
      await channelApi.update(channelId, { [field]: value })
      message.success(value ? '已开启' : '已关闭')
      channel.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHeader
        title="频道概览"
        description="这个频道的身份、开关与当前用量。开关立即生效，且都会写审计。"
      />

      <AsyncBoundary state={channel} skeletonRows={4}>
        {(c) => (
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={14}>
              <Card title="身份">
                <Descriptions column={1} size="small" colon={false}>
                  <Descriptions.Item label="平台">
                    <Space>
                      <PlatformTag value={c.platform} />
                      <Typography.Text className="mono">{c.channel_id}</Typography.Text>
                    </Space>
                  </Descriptions.Item>
                  <Descriptions.Item label="工作区">
                    <Typography.Text className="mono">{c.workspace_id}</Typography.Text>
                  </Descriptions.Item>
                  <Descriptions.Item label="实例 ID">
                    <EntityId id={c.id} tail={10} />
                  </Descriptions.Item>
                  <Descriptions.Item label="Agent 身份">
                    <EntityId id={c.agent_identity} tail={10} />
                  </Descriptions.Item>
                  <Descriptions.Item label="创建时间">
                    <Typography.Text type="secondary">{formatTime(c.created_at)}</Typography.Text>
                  </Descriptions.Item>
                </Descriptions>
              </Card>
            </Col>

            <Col xs={24} lg={10}>
              <Card title="行为开关">
                <Flex vertical gap={18}>
                  <Flex justify="space-between" align="flex-start" gap={12}>
                    <div>
                      <Typography.Text strong>主动介入</Typography.Text>
                      <Typography.Paragraph
                        type="secondary"
                        style={{ margin: '2px 0 0', fontSize: 13 }}
                      >
                        允许在无人 @ 的情况下开口，例如提醒沉寂的线程。误报比漏报更伤，建议先在小范围频道试。
                      </Typography.Paragraph>
                    </div>
                    <Switch
                      checked={c.ambient_enabled}
                      loading={saving}
                      onChange={(v) => void toggle('ambient_enabled', v)}
                    />
                  </Flex>

                  <Flex justify="space-between" align="flex-start" gap={12}>
                    <div>
                      <Typography.Text strong>跨频道学习</Typography.Text>
                      <Typography.Paragraph
                        type="secondary"
                        style={{ margin: '2px 0 0', fontSize: 13 }}
                      >
                        允许检索其他频道的记忆。默认关闭 —— 频道间隔离是默认约定,开了等于让本频道读到别处的对话内容。
                      </Typography.Paragraph>
                    </div>
                    <Switch
                      checked={c.cross_channel_learning}
                      loading={saving}
                      onChange={(v) => void toggle('cross_channel_learning', v)}
                    />
                  </Flex>
                </Flex>
              </Card>
            </Col>

            <Col xs={24} lg={14}>
              <TaskSummary state={tasks} channelId={channelId} />
            </Col>

            <Col xs={24} lg={10}>
              <BudgetSummary state={budget} channelId={channelId} />
            </Col>
          </Row>
        )}
      </AsyncBoundary>
    </>
  )
}

function TaskSummary({
  state,
  channelId,
}: {
  state: ReturnType<typeof useAsync<Task[]>>
  channelId: string
}) {
  return (
    <Card
      title="任务"
      extra={
        <Link to={`/channels/${channelId}/tasks`}>
          全部 <ArrowRightOutlined />
        </Link>
      }
    >
      <AsyncBoundary state={state} skeletonRows={2}>
        {(list) => {
          const live = list.filter((t) => LIVE.has(t.status))
          const failed = list.filter((t) => t.status === 'FAILED')
          return (
            <Row gutter={16}>
              <Col span={8}>
                <Statistic title="总数" value={list.length} />
              </Col>
              <Col span={8}>
                <Statistic
                  title="进行中"
                  value={live.length}
                  valueStyle={live.length ? { color: 'var(--ant-color-primary)' } : undefined}
                />
              </Col>
              <Col span={8}>
                <Statistic
                  title="失败"
                  value={failed.length}
                  valueStyle={failed.length ? { color: 'var(--ant-color-error)' } : undefined}
                />
              </Col>
              {live.length > 0 && (
                <Col span={24} style={{ marginBlockStart: 16 }}>
                  <Space wrap size={[6, 6]}>
                    {live.slice(0, 6).map((t) => (
                      <Tooltip key={t.id} title={t.intent}>
                        <span>
                          <TaskStatusTag value={t.status} />
                        </span>
                      </Tooltip>
                    ))}
                    {live.length > 6 && (
                      <Typography.Text type="secondary">等 {live.length} 个</Typography.Text>
                    )}
                  </Space>
                </Col>
              )}
            </Row>
          )
        }}
      </AsyncBoundary>
    </Card>
  )
}

function BudgetSummary({
  state,
  channelId,
}: {
  state: ReturnType<typeof useAsync<Budget>>
  channelId: string
}) {
  return (
    <Card
      title="预算"
      extra={
        <Link to={`/channels/${channelId}/budget`}>
          配置 <ArrowRightOutlined />
        </Link>
      }
    >
      <AsyncBoundary
        state={state}
        skeletonRows={2}
        treatNotFoundAsEmpty
        emptyDescription="尚未配置预算"
        emptyAction={<Link to={`/channels/${channelId}/budget`}>去配置</Link>}
      >
        {(b) => {
          const pct = percent(b.used_tokens, b.token_limit)
          return (
            <Flex vertical gap={12}>
              <Flex justify="space-between" align="center">
                <Statistic
                  title="已用 token"
                  value={formatCompact(b.used_tokens)}
                  suffix={
                    <Typography.Text type="secondary" style={{ fontSize: 14 }}>
                      / {formatCompact(b.token_limit)}
                    </Typography.Text>
                  }
                />
                <BudgetStateTag value={b.state} />
              </Flex>
              <Progress
                percent={Math.min(pct, 100)}
                // 超 90% 转红：耗尽会让频道停在 EXHAUSTED，得提前看见
                status={b.state === 'EXHAUSTED' ? 'exception' : pct >= 90 ? 'exception' : 'active'}
                showInfo={false}
              />
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                剩余 {formatNumber(b.remaining)}，已用 {pct}%
              </Typography.Text>
            </Flex>
          )
        }}
      </AsyncBoundary>
    </Card>
  )
}
