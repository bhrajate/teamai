import { DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useState } from 'react'

import { skillApi } from '@/api'
import { ApiError } from '@/api/client'
import type { Skill, SkillFileSummary } from '@/api/types'
import { EmptyBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'

/** 单文件上限，与后端 domain/models/skill.py 的 FILE_MAX_BYTES 对齐。 */
const FILE_MAX_BYTES = 64 * 1024

/** 按 UTF-8 字节算，与后端一致 —— 按字符算会让中文文档「看着没超却被拒」。 */
function byteLength(s: string): number {
  return new TextEncoder().encode(s).length
}

function humanSize(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`
}

/**
 * 技能的附带文件管理。
 *
 * 文件是渐进式披露的第 3 级：模型载入技能后先看到文件清单（路径、大小、用途），
 * 需要某个文件时再调 read_skill_file 取内容。所以「用途」这一栏是模型判断
 * 要不要读它的依据，不是给人看的备注。
 *
 * 文件一律是文本且**只读** —— 脚本对模型也只是可读的源码，本项目不执行它。
 */
export function SkillFileDrawer({ skill, onClose }: { skill: Skill | null; onClose: () => void }) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FileForm>()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  const skillId = skill?.id ?? ''
  const state = useAsync(
    useCallback(
      async (signal: AbortSignal) => {
        if (!skillId) return [] as SkillFileSummary[]
        // 列表里的文件摘要跟着 skill 一起来，重取 skill 即可拿到最新的
        const all = await skillApi.list(signal)
        return all.find((s) => s.id === skillId)?.files ?? []
      },
      [skillId],
    ),
    [skillId],
  )

  const openCreate = () => {
    setEditingId(null)
    form.resetFields()
    form.setFieldsValue({ path: '', description: '', content: '' })
    setModalOpen(true)
  }

  const openEdit = async (file: SkillFileSummary) => {
    setEditingId(file.id)
    form.resetFields()
    try {
      // 列表只有摘要，内容要单取
      const full = await skillApi.getFile(skillId, file.id)
      form.setFieldsValue({
        path: full.path,
        description: full.description,
        content: full.content,
      })
      setModalOpen(true)
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '读取文件失败')
    }
  }

  const remove = async (file: SkillFileSummary) => {
    try {
      await skillApi.removeFile(skillId, file.id)
      message.success(`已删除 ${file.path}`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '删除失败')
    }
  }

  const save = async () => {
    let values: FileForm
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    try {
      if (editingId) {
        await skillApi.updateFile(skillId, editingId, values)
      } else {
        await skillApi.createFile(skillId, values)
      }
      message.success(editingId ? '已保存' : '已添加')
      setModalOpen(false)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const columns: ColumnsType<SkillFileSummary> = [
    {
      title: '路径',
      dataIndex: 'path',
      render: (v: string) => <Typography.Text className="mono">{v}</Typography.Text>,
    },
    { title: '用途（模型据此决定是否读取）', dataIndex: 'description' },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 90,
      render: (v: number) => <Typography.Text type="secondary">{humanSize(v)}</Typography.Text>,
    },
    {
      title: '',
      key: 'actions',
      width: 100,
      render: (_: unknown, r) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Popconfirm title={`删除 ${r.path}？`} onConfirm={() => remove(r)}>
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <>
      <Drawer
        title={skill ? `${skill.name} 的附带文件` : ''}
        open={skill !== null}
        onClose={onClose}
        /* size 而非 width：v6 起 width 已废弃（同 AuditPage 的抽屉） */
        size={760}
        extra={
          <Button type="primary" size="small" icon={<PlusOutlined />} onClick={openCreate}>
            添加文件
          </Button>
        }
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          title="文件是按需读取的"
          description="模型载入技能时只看到下面这份清单（路径、大小、用途），需要某个文件时才会取它的内容。所以「用途」要写清这个文件是干什么的。文件一律是文本，且只读 —— 脚本对模型也只是可读的源码，不会被执行。"
        />
        {(state.data ?? []).length === 0 ? (
          <EmptyBlock description="还没有附带文件。参考文档、配置样例、脚本源码都可以放这里。" />
        ) : (
          <Table
            rowKey="id"
            columns={columns}
            dataSource={state.data ?? []}
            size="small"
            pagination={false}
            loading={state.loading}
          />
        )}
      </Drawer>

      <Modal
        title={editingId ? '编辑文件' : '添加文件'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={save}
        okText={editingId ? '保存' : '添加'}
        okButtonProps={{ loading: saving }}
        cancelText="取消"
        width={720}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="path"
            label="路径"
            tooltip="模型照这个路径调 read_skill_file。可以带目录，如 docs/checklist.md。"
            rules={[
              { required: true, message: '请输入路径' },
              {
                pattern: /^[A-Za-z0-9_./-]+$/,
                message: '只允许字母、数字、下划线、连字符、点与斜杠',
              },
              {
                validator: (_, v: string) =>
                  !v || (!v.startsWith('/') && !v.endsWith('/') && !v.split('/').includes('..'))
                    ? Promise.resolve()
                    : Promise.reject(new Error('不能以 / 开头或结尾，也不能含 .. 段')),
              },
            ]}
          >
            <Input placeholder="checklist.md" />
          </Form.Item>
          <Form.Item
            name="description"
            label="用途"
            tooltip="模型在读之前只看到这句话，据此判断值不值得读。"
            rules={[{ required: true, message: '请输入用途' }]}
          >
            <Input placeholder="Go 代码审查的逐项检查清单" />
          </Form.Item>
          <Form.Item
            name="content"
            label="内容"
            rules={[
              { required: true, message: '请输入内容' },
              {
                validator: (_, v: string) =>
                  byteLength(v ?? '') <= FILE_MAX_BYTES
                    ? Promise.resolve()
                    : Promise.reject(
                        new Error(
                          `${humanSize(byteLength(v))} 超出上限 64 KB（按 UTF-8 字节算，一个汉字 3 字节）`,
                        ),
                      ),
              },
            ]}
          >
            <Input.TextArea
              rows={14}
              placeholder="文本内容。参考文档、配置样例、脚本源码都可以。"
              style={{ fontFamily: 'var(--mono, monospace)' }}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

type FileForm = { path: string; description: string; content: string }
