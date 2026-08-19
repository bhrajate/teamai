import {
  ApiOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  App,
  Button,
  Card,
  Flex,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'

import { mcpApi } from '@/api'
import { ApiError } from '@/api/client'
import type { McpServer } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'

/**
 * 频道 MCP server 管理。
 *
 * 配置单位是频道（对齐 policy/预算），重启 worker 后生效 —— 连接与工具装载在
 * worker 启动时发生，这里只管把配置落库。hostname 规则：小写字母/数字/连字符，
 * 它会拼进工具名前缀 `mcp__<name>__`。
 *
 * 凭据说明：headers 的真值不离开后端，任何响应都是 `***` 占位（后端脱敏）。
 * 因此列表里只读展示、编辑只在已有键上回显占位、提交空串删除该键。
 */
export function McpServerPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [form] = Form.useForm<McpServerForm>()
  const [editing, setEditing] = useState<McpServer | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  const [testState, setTestState] = useState<{ loading: boolean; tools: string[] | null; detail: string | null }>({
    loading: false,
    tools: null,
    detail: null,
  })

  const state = useAsync(
    useCallback((signal: AbortSignal) => mcpApi.list(channelId, signal), [channelId]),
    [channelId],
  )

  const headers = Form.useWatch('headers', form) as HeaderRow[] | undefined

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ name: '', url: '', headers: [{}] })
    setTestState({ loading: false, tools: null, detail: null })
    setModalOpen(true)
  }

  const openEdit = (server: McpServer) => {
    setEditing(server)
    form.resetFields()
    form.setFieldsValue({
      name: server.name,
      url: server.url,
      headers: server.headers && Object.keys(server.headers).length ? Object.keys(server.headers).map((k) => ({ key: k, value: '***' })) : [{}],
    })
    setTestState({ loading: false, tools: null, detail: null })
    setModalOpen(true)
  }

  const toggleEnabled = async (server: McpServer, enabled: boolean) => {
    try {
      await mcpApi.update(channelId, server.id, { enabled })
      message.success(`${enabled ? '启用' : '停用'} ${server.name}`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    }
  }

  const remove = async (server: McpServer) => {
    try {
      await mcpApi.remove(channelId, server.id)
      message.success(`已删除 ${server.name}`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '删除失败')
    }
  }

  const testConnection = async () => {
    try {
      await form.validateFields(['url'])
    } catch {
      return
    }
    setTestState({ loading: true, tools: null, detail: null })
    try {
      const url = form.getFieldValue('url') as string
      const hs = (form.getFieldValue('headers') as HeaderRow[] | undefined) ?? []
      const headers = Object.fromEntries(
        hs.filter((h) => h.key && h.value && h.value !== '***').map((h) => [h.key as string, h.value as string]),
      )
      const r = await mcpApi.test(channelId, { url, headers })
      setTestState({ loading: false, tools: r.tools, detail: null })
    } catch (err) {
      setTestState({
        loading: false,
        tools: null,
        detail: err instanceof ApiError ? err.detail : '连接失败',
      })
    }
  }

  const save = async () => {
    let values: McpServerForm
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    const headers = Object.fromEntries(
      (values.headers ?? []).filter((h) => h.key).map((h) => [h.key as string, h.value ?? '']),
    )
    const body = { name: values.name, url: values.url, headers }
    setSaving(true)
    try {
      if (editing) {
        await mcpApi.update(channelId, editing.id, body)
      } else {
        await mcpApi.create(channelId, body)
      }
      message.success(editing ? '已保存' : '已创建')
      setModalOpen(false)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const columns: ColumnsType<McpServer> = [
    {
      title: '名称',
      dataIndex: 'name',
      render: (v: string) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{v}</Typography.Text>
          <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
            mcp__{v}__
          </Typography.Text>
        </Space>
      ),
    },
    { title: '端点', dataIndex: 'url', render: (v: string) => <Typography.Text className="mono">{v}</Typography.Text> },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: 90,
      render: (v: boolean, r) => (
        <Tooltip title={v ? '已启用，重启 worker 后装载' : '已停用，重启后不会装载'}>
          <Switch checked={v} size="small" onChange={(next) => toggleEnabled(r, next)} />
        </Tooltip>
      ),
    },    {
      title: '最近错误',
      dataIndex: 'last_error',
      width: 220,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v}>
            <Tag color="red" style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {v}
            </Tag>
          </Tooltip>
        ) : (
          <Typography.Text type="secondary">—</Typography.Text>
        ),
    },
    {
      title: '',
      key: 'actions',
      width: 140,
      render: (_: unknown, r) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Popconfirm
            title={`删除 ${r.name}？`}
            description="工具会在下次重启后消失，该频道策略里残留的 mcp__ 条目会被忽略。"
            onConfirm={() => remove(r)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const testResult =
    testState.tools != null ? (
      <Flex vertical gap={4}>
        <Typography.Text type="success">连接成功，{testState.tools.length} 个工具：</Typography.Text>
        <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
          {testState.tools.join('、')}
        </Typography.Text>
      </Flex>
    ) : testState.detail ? (
      <Typography.Text type="danger">{testState.detail}</Typography.Text>
    ) : null

  return (
    <>
      <PageHeader
        title="MCP 服务器"
        description="为这个频道挂载外部 MCP server（streamable HTTP）。保存后重启 worker 生效，其工具以 mcp__<名称>__ 前缀进入本频道工具白名单。"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新增服务器
            </Button>
          </Space>
        }
      />

      <AsyncBoundary state={state} skeletonRows={4}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock description="还没有配置 MCP server。点右上角新增一个，重启 worker 后在权限策略里勾选启用它的工具。" />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table rowKey="id" columns={columns} dataSource={list} size="middle" pagination={false} />
            </Card>
          )
        }
      </AsyncBoundary>

      <Modal
        title={editing ? `编辑 ${editing.name}` : '新增 MCP 服务器'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={save}
        okText={editing ? '保存' : '创建'}
        okButtonProps={{ loading: saving }}
        cancelText="取消"
        width={620}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="name"
            label="名称"
            tooltip="小写字母、数字、连字符。会拼进工具名前缀 mcp__<名称>__，创建后不可改（改名会让白名单条目失效）。"
            rules={[
              { required: true, message: '请输入名称' },
              { pattern: /^[a-z0-9-]+$/, message: '只允许小写字母、数字与连字符' },
            ]}
          >
            <Input placeholder="github" disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="url"
            label="端点 URL"
            rules={[
              { required: true, message: '请输入端点 URL' },
              { pattern: /^https?:\/\//, message: '必须是 http(s) 端点' },
            ]}
          >
            <Input placeholder="https://mcp.example.com/github" />
          </Form.Item>
          <Form.Item label="请求头">
            <Flex vertical gap={8}>
              {(headers ?? []).map((_row, i) => (
                <Space key={i} align="center">
                  <Form.Item name={['headers', i, 'key']} noStyle rules={[{ required: true, message: '键不能为空' }]}>
                    <Input placeholder="Header 键" style={{ width: 200 }} />
                  </Form.Item>
                  <Form.Item name={['headers', i, 'value']} noStyle>
                    <Input placeholder="值（留空删除该键）" style={{ width: 220 }} />
                  </Form.Item>
                  <Button
                    danger
                    size="small"
                    icon={<DeleteOutlined />}
                    onClick={() => {
                      const next = (form.getFieldValue('headers') as HeaderRow[]).filter((_, idx) => idx !== i)
                      form.setFieldsValue({ headers: next })
                    }}
                  />
                </Space>
              ))}
              <Space>
                <Button
                  size="small"
                  type="dashed"
                  icon={<PlusOutlined />}
                  onClick={() => form.setFieldsValue({ headers: [...(form.getFieldValue('headers') ?? []), {}] })}
                >
                  加一行
                </Button>
                <Button size="small" onClick={testConnection} loading={testState.loading} icon={<ApiOutlined />}>
                  测试连接
                </Button>
              </Space>
            </Flex>
          </Form.Item>
          {testResult}
        </Form>
      </Modal>
    </>
  )
}

type HeaderRow = { key: string; value?: string }
type McpServerForm = { name: string; url: string; headers?: HeaderRow[] }
