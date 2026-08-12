import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'

import { memoryApi } from '@/api'
import { ApiError } from '@/api/client'
import type { Memory, MemoryType } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import {
  MEMORY_SOURCE_OPTIONS,
  MEMORY_TYPE_OPTIONS,
  MemorySourceTag,
  MemoryTypeTag,
} from '@/components/tags'

/** 表格筛选用的是 {value,text}，Select 要的是 {value,label} —— 转一次。 */
const MEMORY_TYPE_SELECT = MEMORY_TYPE_OPTIONS.map((o) => ({ value: o.value, label: o.text }))
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatFromNow, formatTime } from '@/lib/format'

type FormValues = { content: string; type?: MemoryType; user_id?: string }

/**
 * 频道记忆。可增、可改、可删 —— 记忆会被后续任务检索复用，故错的记忆要能改或删，
 * 重要的背景要能手工补进去，不必等它自己从对话里学。
 *
 * 编辑走 PATCH 而非「删了重建」：后者会让 id 变、写入时间被重置，审计里也看不出
 * 是同一条的演进。改内容会触发后端重算向量。
 *
 * 有意不提供改「可见性」：把 private 改成 channel 等于把本不该进频道记忆的内容
 * 放出去，属权限变更而非内容编辑，后端也不接受这个字段。
 */
export function MemoryPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  // null 表示新建，有值表示在编辑那一条 —— 单个状态同时表达「开没开」与「改哪条」，
  // 比两个 state（open + editing）少一种自相矛盾的组合。
  const [editing, setEditing] = useState<Memory | null>(null)
  const [open, setOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [form] = Form.useForm<FormValues>()

  const state = useAsync(
    useCallback((signal: AbortSignal) => memoryApi.list(channelId, signal), [channelId]),
    [channelId],
  )

  const openCreate = () => {
    setEditing(null)
    setOpen(true)
  }

  const openEdit = (entry: Memory) => {
    setEditing(entry)
    setOpen(true)
    // Modal 用了 destroyOnHidden，字段要在打开后再填
    form.setFieldsValue({ content: entry.content, type: entry.type })
  }

  const submit = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      return // 校验未过，AntD 已在字段下给出提示
    }
    setSubmitting(true)
    try {
      if (editing) {
        await memoryApi.update(editing.id, { content: values.content, type: values.type })
        message.success('已更新')
      } else {
        await memoryApi.create(channelId, values)
        message.success('已写入')
      }
      setOpen(false)
      form.resetFields()
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : editing ? '更新失败' : '写入失败')
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
      // 与下面的「来源用户」是两回事：这一列答「这条是谁写下的」。
      // 蒸馏产出与管理台写入的 source_user_id 都是 null，只有这列能区分它们。
      title: '产生方式',
      dataIndex: 'source',
      width: 120,
      filters: MEMORY_SOURCE_OPTIONS,
      onFilter: (v, r) => r.source === v,
      render: (v: Memory['source']) => <MemorySourceTag value={v} />,
    },
    {
      title: '来源用户',
      dataIndex: 'source_user_id',
      width: 130,
      render: (v: string | null) =>
        v ? (
          <Typography.Text className="mono">{v}</Typography.Text>
        ) : (
          <Tooltip title="没有关联到具体用户。自动蒸馏与控制台写入都是这样，要看是谁写的请参考「产生方式」。">
            <Typography.Text type="secondary">—</Typography.Text>
          </Tooltip>
        ),
    },
    {
      title: '索引',
      dataIndex: 'embedding_ref',
      width: 80,
      align: 'center',
      // 没建索引的条目不参与语义检索，只能靠「按时间倒序取最近若干条」被捞到。
      // 这一列让「为什么这条明明相关却没被引用」有个可查的答案。
      render: (v: string | null) =>
        v ? (
          <Tooltip title="已建向量索引，参与语义检索">
            <Typography.Text type="success">已建</Typography.Text>
          </Tooltip>
        ) : (
          <Tooltip title="未建向量索引（未配 embedding 或写入失败），只能经时间倒序被检索到">
            <Typography.Text type="secondary">未建</Typography.Text>
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
      width: 90,
      align: 'right',
      render: (_, r) => (
        <Space size={0}>
          <Tooltip title="修改内容或类型">
            <Button
              type="text"
              size="small"
              icon={<EditOutlined />}
              onClick={() => openEdit(r)}
            />
          </Tooltip>
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
        </Space>
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
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
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
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
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
        title={editing ? '修改记忆' : '手工写入记忆'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void submit()}
        confirmLoading={submitting}
        okText={editing ? '保存' : '写入'}
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="content"
            label="内容"
            rules={[{ required: true, message: '内容不能为空' }]}
            extra={
              editing
                ? '改动内容会让后端按新文本重算向量索引。'
                : '写成脱离上下文也能读懂的完整陈述句，它会被后续任务直接引用。'
            }
          >
            <Input.TextArea
              rows={5}
              placeholder="例如：这个频道的部署走 GitHub Actions，不要建议手工 scp。"
            />
          </Form.Item>
          <Form.Item
            name="type"
            label="类型"
            extra="留空按「背景知识」处理。"
          >
            <Select allowClear placeholder="背景知识" options={MEMORY_TYPE_SELECT} />
          </Form.Item>
          {/* 来源用户只在新建时可填：它记的是「这条源自谁的话」，
              是写入那一刻的事实，事后改没有意义。 */}
          {!editing && (
            <Form.Item
              name="user_id"
              label="来源用户 ID"
              extra="可留空。填了便于日后追溯这条源自谁的发言。"
            >
              <Input placeholder="U0123456789" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </>
  )
}
