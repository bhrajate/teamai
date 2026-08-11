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
  Switch,
  Table,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'

import { tagApi } from '@/api'
import { ApiError } from '@/api/client'
import type { Tag } from '@/api/types'
import { EntityId } from '@/components/EntityId'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'

type FormValues = {
  name: string
  instruction: string
  role?: string
  output_style?: string
  created_by?: string
}

/**
 * 标签模板：把「以什么角色、按什么风格、遵循什么指令」存成一份可复用的配置，
 * 在频道里激活后由 AgentRuntime 读取。
 */
export function TagPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [open, setOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  /** 正在切换的标签 id，用来只给那一行的开关转圈。 */
  const [toggling, setToggling] = useState<string | null>(null)
  const [form] = Form.useForm<FormValues>()

  const state = useAsync(
    useCallback((signal: AbortSignal) => tagApi.list(channelId, signal), [channelId]),
    [channelId],
  )

  const create = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSubmitting(true)
    try {
      await tagApi.create(channelId, values)
      message.success('已创建')
      setOpen(false)
      form.resetFields()
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '创建失败')
    } finally {
      setSubmitting(false)
    }
  }

  const toggle = async (tag: Tag, active: boolean) => {
    setToggling(tag.id)
    try {
      await tagApi.setActive(channelId, tag.id, active)
      message.success(active ? `已激活「${tag.name}」` : `已停用「${tag.name}」`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '切换失败')
    } finally {
      setToggling(null)
    }
  }

  const remove = async (tag: Tag) => {
    try {
      await tagApi.remove(channelId, tag.id)
      message.success('已删除')
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '删除失败')
    }
  }

  const columns: ColumnsType<Tag> = [
    {
      title: '名称',
      dataIndex: 'name',
      width: 170,
      // 不显示 shared 字段：它在后端除存取之外从未被读作任何条件（默认 true
      // 且无处可改），摆出来会暗示一种不存在的「跨频道共享」能力。
      render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
    },
    {
      title: '指令',
      dataIndex: 'instruction',
      render: (v: string) => (
        <Typography.Paragraph
          className="wrap-anywhere"
          style={{ margin: 0 }}
          ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
        >
          {v}
        </Typography.Paragraph>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      width: 120,
      render: (v: string | null) =>
        v ?? <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: '输出风格',
      dataIndex: 'output_style',
      width: 120,
      render: (v: string | null) =>
        v ?? <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: 'ID',
      dataIndex: 'id',
      width: 130,
      render: (v: string) => <EntityId id={v} />,
    },
    {
      title: '启用',
      dataIndex: 'active',
      width: 80,
      align: 'center',
      render: (v: boolean, r) => (
        <Switch checked={v} loading={toggling === r.id} onChange={(next) => void toggle(r, next)} />
      ),
    },
    {
      key: 'action',
      width: 56,
      align: 'right',
      render: (_, r) => (
        <Popconfirm
          title={`删除「${r.name}」？`}
          description="删除会写审计，且不可恢复。"
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => void remove(r)}
        >
          <Button type="text" size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title="标签模板"
        description="预设的角色、风格与指令。激活后本频道的回复都按它执行，可随时停用。"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              新建标签
            </Button>
          </Space>
        }
      />

      <AsyncBoundary state={state} skeletonRows={5}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock
                description="还没有标签模板"
                action={
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
                    新建一个
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
                pagination={{ pageSize: 20, hideOnSinglePage: true }}
              />
            </Card>
          )
        }
      </AsyncBoundary>

      <Modal
        title="新建标签模板"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void create()}
        confirmLoading={submitting}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
        width={560}
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '名称不能为空' }]}
            extra="在频道里用这个名字激活它。"
          >
            <Input placeholder="例如：代码审查" />
          </Form.Item>
          <Form.Item
            name="instruction"
            label="指令"
            rules={[{ required: true, message: '指令不能为空' }]}
            extra="激活后附加到系统提示里。没有指令的标签等于空壳，故必填。"
          >
            <Input.TextArea
              rows={4}
              placeholder="例如：逐文件审查改动，先指出正确性问题，再谈可读性。不要重复代码本身。"
            />
          </Form.Item>
          <Form.Item name="role" label="角色" extra="可留空。">
            <Input placeholder="例如：资深后端工程师" />
          </Form.Item>
          <Form.Item name="output_style" label="输出风格" extra="可留空。">
            <Input placeholder="例如：要点式，每条一行" />
          </Form.Item>
          <Form.Item name="created_by" label="创建人 ID" extra="可留空，填了会记进审计。">
            <Input placeholder="U0123456789" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}
