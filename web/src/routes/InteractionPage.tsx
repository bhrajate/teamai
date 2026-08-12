import { ReloadOutlined } from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Collapse,
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
import { Link, useSearchParams } from 'react-router-dom'

import { interactionApi } from '@/api'
import type { Interaction } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import {
  INTERACTION_RESULT_OPTIONS,
  InteractionResultTag,
  ModelLevelTag,
} from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatFromNow, formatNumber, formatTime } from '@/lib/format'

/** 200 是后端 MAX_LIMIT，再大后端直接 422。 */
const LIMITS = [50, 100, 200]

const TASK_PARAM = 'task'

/**
 * Agent 交互记录。只读 —— 记录由 AgentRuntime 执行时写入，清理走 worker 的
 * 保留期巡检，故不给人工增删入口。
 *
 * 与「审计」页的分工：审计答「发生了什么动作」，这里答「模型当时看到了什么、
 * 回了什么、烧了多少 token」。排查一个错误回答要的是后者。
 *
 * `?task=<id>` 时切到单任务视图（走 listByTask，时间正序），从任务页跳进来
 * 看一件活的完整往返 —— 重试与多阶段任务会有多条。
 */
export function InteractionPage() {
  const channelId = useChannelId()
  const [params, setParams] = useSearchParams()
  const [limit, setLimit] = useState(50)
  const [detail, setDetail] = useState<Interaction | null>(null)

  const taskId = params.get(TASK_PARAM) ?? undefined

  const state = useAsync(
    // deps 定长（useAsync 要求),故分支写在 fn 内部而不是条件式增删依赖
    useCallback(
      (signal: AbortSignal) =>
        taskId
          ? interactionApi.listByTask(taskId, signal)
          : interactionApi.list(channelId, limit, signal),
      [channelId, limit, taskId],
    ),
    [channelId, limit, taskId],
  )

  const clearTaskFilter = () => {
    const next = new URLSearchParams(params)
    next.delete(TASK_PARAM)
    setParams(next, { replace: true })
  }

  const columns: ColumnsType<Interaction> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 110,
      sorter: (a, b) => a.created_at.localeCompare(b.created_at),
      // 单任务视图按时间正序读才是「一次往返」，频道视图要最近的在上
      defaultSortOrder: taskId ? 'ascend' : 'descend',
      render: (v: string) => (
        <Tooltip title={formatTime(v)}>
          <Typography.Text type="secondary">{formatFromNow(v)}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: '结果',
      dataIndex: 'result',
      width: 90,
      filters: INTERACTION_RESULT_OPTIONS,
      onFilter: (v, r) => r.result === v,
      render: (v: Interaction['result']) => <InteractionResultTag value={v} />,
    },
    {
      title: '模型',
      dataIndex: 'model_level',
      width: 190,
      render: (v: string, r) => (
        <Space direction="vertical" size={0}>
          <ModelLevelTag value={v} />
          {/* 实际生效的模型 ID：降级到备用模型时与档位配置不一致，成本按这个归因 */}
          {r.model_id && (
            <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
              {r.model_id}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: 'token',
      dataIndex: 'tokens_total',
      width: 120,
      align: 'right',
      sorter: (a, b) => a.tokens_total - b.tokens_total,
      // 分项进 tooltip：输入输出单价差数倍，核成本时要分开看
      render: (v: number, r) => (
        <Tooltip title={`输入 ${formatNumber(r.tokens_in)} / 输出 ${formatNumber(r.tokens_out)}`}>
          <Typography.Text className="mono">{formatNumber(v)}</Typography.Text>
        </Tooltip>
      ),
    },
    {
      title: '发起人',
      dataIndex: 'requester_id',
      width: 140,
      render: (v: string | null) =>
        v ? (
          <Typography.Text className="mono">{v}</Typography.Text>
        ) : (
          <Typography.Text type="secondary">系统</Typography.Text>
        ),
    },
    {
      title: '任务',
      dataIndex: 'task_id',
      width: 130,
      render: (v: string) => <EntityId id={v} />,
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

  /**
   * 表尾合计要对齐 token 那一列，故从 columns 推位置而不写死数字 ——
   * 写死的话加一列或调顺序就会错位，且不会报错。
   *
   * `+1` 是 expandable 额外插在最前的展开列：它占一个 <td>，但不在 columns 里。
   */
  const tokenColumn = columns.findIndex((c) => 'dataIndex' in c && c.dataIndex === 'tokens_total')
  const tokenCellIndex = tokenColumn + 1

  return (
    <>
      <PageHeader
        title="交互记录"
        description="每次调模型留一条：完整的系统提示词、用户输入、模型响应与 token 分项。回答不对时来这里复现当时的输入。"
        extra={
          <Space>
            {/* 单任务视图取的是该任务全部往返，条数由任务决定，limit 档位无意义 */}
            {!taskId && (
              <Segmented
                value={limit}
                onChange={(v) => setLimit(v as number)}
                options={LIMITS.map((n) => ({ label: `近 ${n} 条`, value: n }))}
              />
            )}
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
          </Space>
        }
      />

      {taskId && (
        <Alert
          type="info"
          showIcon
          style={{ marginBlockEnd: 16 }}
          title={
            <Space size={6} wrap>
              <span>只看任务</span>
              <EntityId id={taskId} tail={10} />
              <span>的完整往返，按时间正序。</span>
            </Space>
          }
          action={
            <Button size="small" type="link" onClick={clearTaskFilter}>
              看全频道
            </Button>
          }
        />
      )}

      <AsyncBoundary state={state} skeletonRows={8}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock
                description={
                  taskId
                    ? '这个任务还没有交互记录。它可能尚未被 worker 执行。'
                    : '这个频道还没有交互记录。在群里 @ 机器人问一句试试。'
                }
                action={
                  taskId ? (
                    <Button type="link" onClick={clearTaskFilter}>
                      看全频道
                    </Button>
                  ) : undefined
                }
              />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table
                // 两个视图的默认排序方向相反，而 defaultSortOrder 只在挂载时读一次。
                // 不给 key 的话切回频道视图会留着 ascend，变成最旧的在上。
                key={taskId ?? 'channel'}
                rowKey="id"
                columns={columns}
                dataSource={list}
                size="middle"
                pagination={{ pageSize: 20, showSizeChanger: true, hideOnSinglePage: true }}
                expandable={{ expandedRowRender: (r) => <RowSummary record={r} /> }}
                summary={(rows) => (
                  <TokenTotal
                    rows={rows as readonly Interaction[]}
                    leading={tokenCellIndex}
                    trailing={columns.length - tokenColumn - 1}
                  />
                )}
              />
            </Card>
          )
        }
      </AsyncBoundary>

      <InteractionDrawer
        record={detail}
        onClose={() => setDetail(null)}
        channelId={channelId}
        viewingTaskId={taskId}
      />
    </>
  )
}

/**
 * 展开行：问句与回答的摘要，各截三行。
 *
 * 只出这两段而不带系统提示词 —— 后者动辄上千字且每条大同小异，铺在表格里
 * 会把「这次问了什么」淹掉。全文都在抽屉里。
 */
function RowSummary({ record }: { record: Interaction }) {
  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Field label="用户输入" text={record.user_prompt} />
      <Field label="模型响应" text={record.response} />
      {record.error && (
        <Alert type="error" showIcon title={record.error} style={{ marginBlockStart: 2 }} />
      )}
    </Space>
  )
}

function Field({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Typography.Text>
      {text ? (
        <Typography.Paragraph
          style={{ margin: '2px 0 0', fontSize: 13 }}
          ellipsis={{ rows: 3, tooltip: false }}
        >
          {text}
        </Typography.Paragraph>
      ) : (
        <Typography.Paragraph type="secondary" style={{ margin: '2px 0 0', fontSize: 13 }}>
          （空）
        </Typography.Paragraph>
      )}
    </div>
  )
}

/**
 * 表尾合计。按当前页而非全量求和 —— 与上方表格里看到的行对得上，
 * 翻页时数字跟着变是预期的。
 *
 * `leading` / `trailing` 是 token 列前后各有多少个单元格，由调用方从 columns 算，
 * 从而加列时不必回来改这里。
 */
function TokenTotal({
  rows,
  leading,
  trailing,
}: {
  rows: readonly Interaction[]
  leading: number
  trailing: number
}) {
  const total = rows.reduce((sum, r) => sum + r.tokens_total, 0)
  const inTotal = rows.reduce((sum, r) => sum + r.tokens_in, 0)
  const outTotal = rows.reduce((sum, r) => sum + r.tokens_out, 0)

  return (
    <Table.Summary fixed>
      <Table.Summary.Row>
        <Table.Summary.Cell index={0} colSpan={leading}>
          <Typography.Text type="secondary">本页 {rows.length} 条合计</Typography.Text>
        </Table.Summary.Cell>
        <Table.Summary.Cell index={leading} align="right">
          <Tooltip title={`输入 ${formatNumber(inTotal)} / 输出 ${formatNumber(outTotal)}`}>
            <Typography.Text strong className="mono">
              {formatNumber(total)}
            </Typography.Text>
          </Tooltip>
        </Table.Summary.Cell>
        <Table.Summary.Cell index={leading + 1} colSpan={trailing} />
      </Table.Summary.Row>
    </Table.Summary>
  )
}

function InteractionDrawer({
  record,
  onClose,
  channelId,
  /** 当前已经在看哪个任务。等于本条记录的任务时，跳转链接指向当前 URL，点了没反应，故不出。 */
  viewingTaskId,
}: {
  record: Interaction | null
  onClose: () => void
  channelId: string
  viewingTaskId?: string
}) {
  return (
    // 比别处的抽屉宽一档：提示词是长文本，窄了每行只剩十几个字，读不动
    <Drawer title="交互详情" open={record !== null} onClose={onClose} size={720}>
      {record && (
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="时间">{formatTime(record.created_at)}</Descriptions.Item>
            <Descriptions.Item label="结果">
              <InteractionResultTag value={record.result} />
            </Descriptions.Item>
            <Descriptions.Item label="模型">
              <Space>
                <ModelLevelTag value={record.model_level} />
                <Typography.Text className="mono">{record.model_id || '—'}</Typography.Text>
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="token">
              <Typography.Text className="mono">
                {formatNumber(record.tokens_total)}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ marginInlineStart: 8, fontSize: 12 }}>
                输入 {formatNumber(record.tokens_in)} · 输出 {formatNumber(record.tokens_out)}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="发起人">
              {record.requester_id ?? <Typography.Text type="secondary">系统</Typography.Text>}
            </Descriptions.Item>
            <Descriptions.Item label="线程">
              <Typography.Text className="mono wrap-anywhere">{record.thread_ref}</Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="任务">
              <Space size={8} wrap>
                <EntityId id={record.task_id} tail={12} />
                {/* 跳到单任务视图：一件活重试过或分多阶段时，这里能看全 */}
                {viewingTaskId !== record.task_id && (
                  <Link
                    to={`/channels/${channelId}/interactions?${TASK_PARAM}=${record.task_id}`}
                    onClick={onClose}
                  >
                    看该任务全部往返
                  </Link>
                )}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="记录 ID">
              <EntityId id={record.id} tail={12} />
            </Descriptions.Item>
            <Descriptions.Item label="context_refs">
              {/* 引用而非快照：记忆条目 id、线程历史条数等。形状随调用路径而异，故出 JSON */}
              {Object.keys(record.context_refs).length === 0 ? (
                <Typography.Text type="secondary">—</Typography.Text>
              ) : (
                <Typography.Text className="mono wrap-anywhere" style={{ fontSize: 12 }}>
                  {JSON.stringify(record.context_refs, null, 2)}
                </Typography.Text>
              )}
            </Descriptions.Item>
          </Descriptions>
          {record.error && (
            <Alert type="error" showIcon title="执行报错" description={record.error} />
          )}

          {/* 三段全文。响应与问句默认展开，系统提示词默认收起 —— 它最长且每条
              大同小异，展开会把真正要看的两段顶出屏幕。 */}
          <Collapse
            defaultActiveKey={['response', 'user']}
            items={[
              {
                key: 'response',
                label: '模型响应',
                extra: <CopyIcon text={record.response} />,
                children: <LongText text={record.response} />,
              },
              {
                key: 'user',
                label: '用户输入',
                extra: <CopyIcon text={record.user_prompt} />,
                children: <LongText text={record.user_prompt} />,
              },
              {
                key: 'system',
                label: '系统提示词',
                extra: <CopyIcon text={record.system_prompt} />,
                children: <LongText text={record.system_prompt} />,
              },
            ]}
          />
        </Space>
      )}
    </Drawer>
  )
}

/** 全文块。限高滚动而非无限拉长：抽屉里三段并列，任一段撑满都会让其余两段找不着。 */
function LongText({ text }: { text: string }) {
  if (!text) return <Typography.Text type="secondary">（空）</Typography.Text>
  return (
    <Typography.Text
      className="wrap-anywhere"
      style={{ display: 'block', maxHeight: 360, overflow: 'auto', fontSize: 13 }}
    >
      {text}
    </Typography.Text>
  )
}

/** 折叠面板 extra 里的复制图标。stopPropagation 否则点它会连带折叠面板。 */
function CopyIcon({ text }: { text: string }) {
  if (!text) return null
  return (
    <span onClick={(e) => e.stopPropagation()}>
      <Typography.Text copyable={{ text, tooltips: ['复制全文', '已复制'] }} />
    </span>
  )
}
