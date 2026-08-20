import {
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
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
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'

import { skillApi } from '@/api'
import { ApiError } from '@/api/client'
import type { Skill } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'

import { SkillFileDrawer } from '@/routes/SkillFileDrawer'

/**
 * 全局技能库。
 *
 * 技能与标签的分工是**触发方式**：标签由人打 `/名字` 触发，技能由模型看清单后
 * 自行判断相关再载入。所以技能是全局定义一份、各频道勾选启用（频道页在
 * `/channels/:id/skills`），而标签是按频道各配一份。
 *
 * 描述字段最要紧：它是模型判断「该不该用这个技能」的唯一依据，且每次调用都常驻
 * 系统提示词，故限 200 字 —— 详细步骤写正文里。
 *
 * 改完即时生效，不需要重启 worker（技能每次调用从库里读）。
 */
export function SkillPage() {
  const { message } = App.useApp()
  const [form] = Form.useForm<SkillForm>()
  const [editing, setEditing] = useState<Skill | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [filesOf, setFilesOf] = useState<Skill | null>(null)

  const state = useAsync(
    useCallback((signal: AbortSignal) => skillApi.list(signal), []),
    [],
  )

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ name: '', description: '', content: '' })
    setModalOpen(true)
  }

  const openEdit = (skill: Skill) => {
    setEditing(skill)
    form.resetFields()
    form.setFieldsValue({
      name: skill.name,
      description: skill.description,
      content: skill.content,
    })
    setModalOpen(true)
  }

  const toggleEnabled = async (skill: Skill, enabled: boolean) => {
    try {
      await skillApi.update(skill.id, { enabled })
      message.success(`${enabled ? '启用' : '停用'} ${skill.name}`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    }
  }

  const remove = async (skill: Skill) => {
    try {
      await skillApi.remove(skill.id)
      message.success(`已删除 ${skill.name}`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '删除失败')
    }
  }

  const save = async () => {
    let values: SkillForm
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    try {
      if (editing) {
        await skillApi.update(editing.id, values)
      } else {
        await skillApi.create(values)
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

  const columns: ColumnsType<Skill> = [
    {
      title: '名称',
      dataIndex: 'name',
      width: 200,
      render: (v: string) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{v}</Typography.Text>
          <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
            load_skill("{v}")
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: '描述（常驻系统提示词）',
      dataIndex: 'description',
      render: (v: string) => <Typography.Text>{v}</Typography.Text>,
    },
    {
      title: '附带文件',
      dataIndex: 'files',
      width: 120,
      render: (files: Skill['files'], r) => (
        <Button size="small" icon={<FileTextOutlined />} onClick={() => setFilesOf(r)}>
          {files.length > 0 ? `${files.length} 个` : '管理'}
        </Button>
      ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      width: 90,
      render: (v: boolean, r) => (
        <Tooltip title={v ? '已启用，勾选了它的频道即时可用' : '已全局停用，所有频道都不可用'}>
          <Switch checked={v} size="small" onChange={(next) => toggleEnabled(r, next)} />
        </Tooltip>
      ),
    },
    {
      title: '',
      key: 'actions',
      width: 100,
      render: (_: unknown, r) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Popconfirm
            title={`删除 ${r.name}？`}
            description="附带文件与所有频道的启用记录会一并清掉。"
            onConfirm={() => remove(r)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title="技能库"
        description="技能是给 AI 的做事规范，全局维护一份，在各频道的「技能」页勾选启用。模型按描述自行判断是否相关，相关时才载入正文 —— 所以描述要写清适用场景，步骤写在正文里。改完即时生效，无需重启。"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              新增技能
            </Button>
          </Space>
        }
      />

      <AsyncBoundary state={state} skeletonRows={4}>
        {(list) =>
          list.length === 0 ? (
            <Card>
              <EmptyBlock description="还没有技能。点右上角新增一个，然后去频道的「技能」页勾选启用。" />
            </Card>
          ) : (
            <Card styles={{ body: { padding: 0 } }}>
              <Table rowKey="id" columns={columns} dataSource={list} size="middle" pagination={false} />
            </Card>
          )
        }
      </AsyncBoundary>

      <Modal
        title={editing ? `编辑 ${editing.name}` : '新增技能'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={save}
        okText={editing ? '保存' : '创建'}
        okButtonProps={{ loading: saving }}
        cancelText="取消"
        width={720}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="name"
            label="名称"
            tooltip="小写字母、数字、连字符。模型要照这个名字调 load_skill，故不能有空格与大写。"
            rules={[
              { required: true, message: '请输入名称' },
              { pattern: /^[a-z0-9-]+$/, message: '只允许小写字母、数字与连字符' },
            ]}
          >
            <Input placeholder="code-review" />
          </Form.Item>
          <Form.Item
            name="description"
            label="描述"
            tooltip="模型判断「这件事该不该用这个技能」的唯一依据，每次调用都常驻系统提示词，故限 200 字。写清适用场景，不要写步骤。"
            rules={[
              { required: true, message: '请输入描述' },
              { max: 200, message: '不超过 200 字' },
            ]}
          >
            <Input placeholder="按团队 Go 规范审查 PR，产出分级问题清单" showCount maxLength={200} />
          </Form.Item>
          <Form.Item
            name="content"
            label="正文（操作说明）"
            tooltip="模型判断相关后经 load_skill 取回的完整说明。支持 Markdown，长度不限。"
            rules={[{ required: true, message: '请输入正文' }]}
          >
            <Input.TextArea
              rows={12}
              placeholder={'# 审查步骤\n\n1. 先看 diff 的整体结构\n2. ...'}
              style={{ fontFamily: 'var(--mono, monospace)' }}
            />
          </Form.Item>
          <Flex>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              正文只在模型载入该技能时才进入上下文，所以可以写长；描述是常驻的，必须短。
            </Typography.Text>
          </Flex>
        </Form>
      </Modal>

      <SkillFileDrawer
        skill={filesOf}
        onClose={() => {
          setFilesOf(null)
          state.reload()
        }}
      />
    </>
  )
}

type SkillForm = { name: string; description: string; content: string }
