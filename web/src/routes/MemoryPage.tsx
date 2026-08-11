import { DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'

import { memoryApi } from '@/api'
import { ApiError } from '@/api/client'
import type { Memory } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { MEMORY_TYPE_OPTIONS, MemoryTypeTag } from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatFromNow, formatTime } from '@/lib/format'

type FormValues = { content: string; user_id?: string }

/**
 * 频道记忆。可增可删 —— 记忆会被后续任务检索复用，故错的记忆要能删掉，
 * 重要的背景要能手工补进去，不必等它自己从对话里学。
 *
 * 类型（背景知识/偏好/决策/事实）由后端在写入时判定，故新建表单不给选。
 */
export function MemoryPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [open, setOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm<FormValues>()

  const state = useAsync(
    useCallback((signal: AbortSignal) => memoryApi.list(channelId, signal), [channelId]),
    [channelId],
  )

  const create = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      return // 校验未过，AntD 已在字段下给出提示
    }
    setSubmitting(true)
    try {
      await memoryApi.create(channelId, values)
      message.success('已写入')
      setOpen(false)
      form.resetFields()
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '写入失败')
    } finally {
      setSubmitting(false)
    }
  }

  const remove = async (id: string) => {
    try {
      await memoryApi.remove(id)
      message.success('已删除')
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '删除失败')
    }
  }

  const columns: ColumnsType<Memory> = [
    {
      title: '内容',
      dataIndex: 'content',
      render: (v: string) => (
        <Typography.Paragraph
          className="wrap-anywhere"
          style={{ margin: 0 }}
          ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
        >
          {v}
        </Typography.Paragraph>
      ),
    },
    {
      title: '类型',
      dataIndex: 'type',
      width: 110,
      filters: MEMORY_TYPE_OPTIONS,
      onFilter: (v, r) => r.type === v,
      render: (v: Memory['type']) => <MemoryTypeTag value={v} />,
    },
    {
      title: '来源',
      dataIndex: 'source_user_id',
      width: 140,
      render: (v: string | null) =>
        v ? (
          <Typography.Text className="mono">{v}</Typography.Text>
        ) : (
          <Tooltip title="没有来源用户，通常是系统或管理台写入">
            <Typography.Text type="secondary">系统</Typography.Text>
          </Tooltip>
        ),
    },
    {
      title: 'ID',
      dataIndex: 'id',
      width: 130,
      render: (v: string) => <EntityId id={v} />,
    },
    {
      title: '写入时间',
      dataIndex: 'created_at',
      width: 120,
      sorter: (a, b) => a.created_at.localeCompare(b.created_at),
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
        <Popconfirm
          title="删除这条记忆？"
          description="删除会写审计，且不可恢复。"
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => void remove(r.id)}
        >
          <Button type="text" size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title="频道记忆"
        description="Agent 从对话中积累的知识，后续任务会检索复用。频道间默认隔离，除非开了跨频道学习。"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              手工写入
            </Button>
          </Space>
        }
      />

      <AsyncBoundary state={state} skeletonRows={6}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock
                description="还没有记忆条目"
                action={
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
                    手工写入一条
                  </Button>
                }
              />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table
                rowKey="id"
                columns={columns}
                dataSource={list}
                size="middle"
                pagination={{ pageSize: 20, hideOnSinglePage: true, showSizeChanger: true }}
              />
            </Card>
          )
        }
      </AsyncBoundary>

      <Modal
        title="手工写入记忆"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void create()}
        confirmLoading={submitting}
        okText="写入"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="content"
            label="内容"
            rules={[{ required: true, message: '内容不能为空' }]}
            extra="类型由后端判定，不必在此选择。"
          >
            <Input.TextArea
              rows={5}
              placeholder="例如：这个频道的部署走 GitHub Actions，不要建议手工 scp。"
            />
          </Form.Item>
          <Form.Item name="user_id" label="来源用户 ID" extra="可留空。填了会记进来源，便于日后追溯。">
            <Input placeholder="U0123456789" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
