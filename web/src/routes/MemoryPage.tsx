import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useState } from 'react'

import { memoryApi } from '@/api'
import { ApiError } from '@/api/client'
import type { EmbeddingState, Memory, MemoryConflictDetail, MemoryType } from '@/api/types'
import { readMemoryConflictDetail } from '@/api/types'
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
import { formatDate, formatFromNow, formatTime } from '@/lib/format'

type FormValues = { content: string; type?: MemoryType; user_id?: string }

/** 「都不取代，并列写入」的 radio 取值。用不可能与 ULID 相撞的字面量。 */
const WRITE_PARALLEL = '__parallel__'

/**
 * 撞上冲突后的选择界面。
 *
 * 为什么要人来选而不是自动取代：凭一句待写入的话判不出「这是新版本」还是
 * 「另一件事」，而错误取代会作废一条正确的记忆。理由同蒸馏侧不给 DELETE。
 *
 * **默认不预选任何一项。** 预选「取代最像的那条」等于替人做了那个判断，而这道
 * 界面存在的全部理由就是不替人做判断；预选「并列写入」则等于默认放行，那还不如
 * 不拦。故确认按钮在选出之前是禁用的。
 */
export function ConflictResolution({
  detail,
  content,
  value,
  onChange,
}: {
  detail: MemoryConflictDetail
  content: string
  value: string | null
  onChange: (v: string) => void
}) {
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        type="warning"
        showIcon
        title={detail.message}
        description={
          detail.degraded
            ? '配上 embedding 后这里能查出说法不同但意思相同的记忆。'
            : undefined
        }
      />

      <div>
        <Typography.Text type="secondary">你要写入的：</Typography.Text>
        <Typography.Paragraph className="wrap-anywhere" style={{ margin: '4px 0 0' }} strong>
          {content}
        </Typography.Paragraph>
      </div>

      <Radio.Group
        value={value ?? undefined}
        onChange={(e) => onChange(e.target.value as string)}
        style={{ width: '100%' }}
      >
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          {detail.conflicts.map((c) => (
            <Radio key={c.entry.id} value={c.entry.id} style={{ alignItems: 'flex-start' }}>
              <Space direction="vertical" size={2}>
                <Space size={6} wrap>
                  <Typography.Text type="secondary">取代这条</Typography.Text>
                  {/* 日期要显眼：它是判断哪条是现行说法的依据，也是 Agent 侧
                      裁决矛盾记忆用的同一个信号。 */}
                  <Tag>{formatDate(c.entry.created_at)}</Tag>
                  <MemoryTypeTag value={c.entry.type} />
                  {c.score === null ? (
                    <Tooltip title="未配 embedding，这条是按字面重复查出来的">
                      <Tag color="default">字面重复</Tag>
                    </Tooltip>
                  ) : (
                    <Tag color="orange">相似度 {Math.round(c.score * 100)}%</Tag>
                  )}
                </Space>
                <Typography.Text className="wrap-anywhere">{c.entry.content}</Typography.Text>
              </Space>
            </Radio>
          ))}
          <Radio value={WRITE_PARALLEL} style={{ alignItems: 'flex-start' }}>
            <Space direction="vertical" size={2}>
              <Typography.Text>都不取代，并列写入</Typography.Text>
              <Typography.Text type="secondary">
                两条会同时是现行记忆。说的确实是两件事时选这个。
              </Typography.Text>
            </Space>
          </Radio>
        </Space>
      </Radio.Group>
    </Space>
  )
}

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
  // 非 null 表示 Modal 处在「解决冲突」那一步。同时留着刚填的表单值：确认后要按
  // 它重发，而 Modal 用了 destroyOnHidden、表单实例这时已经不能指望。
  const [conflict, setConflict] = useState<{ detail: MemoryConflictDetail; values: FormValues } | null>(
    null,
  )
  const [resolution, setResolution] = useState<string | null>(null)
  // 取代之后那条会从默认列表消失（默认只给现行事实）。这个开关是这次一起加的：
  // 控制台第一次能发起取代，而在此之前它没有任何地方能看到被取代的条目 ——
  // 那等于加了一个结果不可见的操作。
  const [includeSuperseded, setIncludeSuperseded] = useState(false)
  const [form] = Form.useForm<FormValues>()

  const state = useAsync(
    useCallback(
      (signal: AbortSignal) => memoryApi.list(channelId, signal, includeSuperseded),
      [channelId, includeSuperseded],
    ),
    [channelId, includeSuperseded],
  )

  // embedder 装配状态。单独取而不并进 state：它与频道无关（切频道不必重取），
  // 而且取失败不该让整页进错误态 —— 那只是少一条提示。
  const [embedding, setEmbedding] = useState<EmbeddingState | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    void memoryApi
      .embedding(ctrl.signal)
      .then(setEmbedding)
      .catch(() => {
        // 静默：拿不到状态时不挂提示，比挂一条可能是错的提示好
      })
    return () => ctrl.abort()
  }, [])

  const closeModal = () => {
    setOpen(false)
    setConflict(null)
    setResolution(null)
  }

  const openCreate = () => {
    setEditing(null)
    setConflict(null)
    setResolution(null)
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
      closeModal()
      form.resetFields()
      state.reload()
    } catch (err) {
      // 409：库里有疑似说同一件事的现行记忆。不是失败，是要人做个决定 ——
      // 切到选择那一步，别弹错误提示（那会让人以为写不进去）。
      const detail = err instanceof ApiError && err.status === 409
        ? readMemoryConflictDetail(err.body)
        : null
      if (detail) {
        setConflict({ detail, values })
        setResolution(null)
        return
      }
      message.error(err instanceof ApiError ? err.detail : editing ? '更新失败' : '写入失败')
    } finally {
      setSubmitting(false)
    }
  }

  /** 按人选的方式重发写入。 */
  const submitResolved = async () => {
    if (!conflict || !resolution) return
    setSubmitting(true)
    try {
      const parallel = resolution === WRITE_PARALLEL
      await memoryApi.create(channelId, {
        ...conflict.values,
        // 两者互斥，后端同时收到会报 400
        ...(parallel ? { force: true } : { supersede_id: resolution }),
      })
      message.success(
        parallel
          ? '已并列写入'
          : '已取代旧记忆。旧的那条仍在库里，勾「含已取代」可以看到',
      )
      closeModal()
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
      render: (v: string, r: Memory) => (
        <Space direction="vertical" size={2}>
          {/* 被取代的条目必须一眼可辨：它们不参与检索，混在现行记忆里看会让人
              以为同一件事重复存了好几条。只在开了「含已取代」时才会出现。 */}
          {r.superseded_by && (
            <Tooltip
              title={
                r.superseded_at
                  ? `已于 ${formatTime(r.superseded_at)} 被取代，不再参与检索`
                  : '已被取代，不再参与检索'
              }
            >
              <Tag color="default">已取代</Tag>
            </Tooltip>
          )}
          <Typography.Paragraph
            className="wrap-anywhere"
            style={{ margin: 0 }}
            type={r.superseded_by ? 'secondary' : undefined}
            ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
          >
            {v}
          </Typography.Paragraph>
        </Space>
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
            {/* 默认列表只给现行事实。开这个才看得到被取代的版本 ——
                「这条事实之前是什么」是排查「机器人为什么这么说」的主要线索，
                而取代既可能来自蒸馏也可能来自这个页面上的手工取代。 */}
            <Tooltip title="连已被取代的历史版本一起显示。它们不参与检索，只供排查。">
              <Checkbox
                checked={includeSuperseded}
                onChange={(e) => setIncludeSuperseded(e.target.checked)}
              >
                含已取代
              </Checkbox>
            </Tooltip>
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              手工写入
            </Button>
          </Space>
        }
      />

      {/* 未配 embedding 时的降级提示。三层后果里第三层最要紧 —— 前两层是「这次
          差一点」，第三层是记忆库会持续劣化，而且要几周才从回答质量上看出来。
          此前这件事只在启动日志里出现一次，滚掉就没人知道了。 */}
      {embedding && !embedding.available && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBlockEnd: 16 }}
          title="未配置 embedding，记忆能力处于降级状态"
          description={
            <>
              <div>· 语义检索关闭，只能按时间倒序取最近若干条</div>
              <div>· 手工写入的冲突检查退化为字面比对，说法不同但意思相同的查不出来</div>
              <div>
                · 自动蒸馏的去重与取代对旧记忆基本失效 —— 比对候选退化成「最近 10
                条」，更早的矛盾记忆进不了比对，会持续并列堆积
              </div>
            </>
          }
        />
      )}

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
        title={conflict ? '这条可能和已有记忆重复' : editing ? '修改记忆' : '手工写入记忆'}
        open={open}
        // 冲突那一步的取消是「返回修改」，回到表单而不是关掉整个 Modal ——
        // 关掉会把刚写的内容一起丢掉，而人点它的意思通常是想改措辞再试。
        onCancel={() => {
          if (conflict) {
            setConflict(null)
            setResolution(null)
          } else {
            closeModal()
          }
        }}
        onOk={() => void (conflict ? submitResolved() : submit())}
        confirmLoading={submitting}
        okText={conflict ? '确认' : editing ? '保存' : '写入'}
        // 没选之前不许确认：这道界面的全部理由是让人做那个判断，
        // 给一个可点的默认等于替他做了
        okButtonProps={conflict ? { disabled: !resolution } : undefined}
        cancelText={conflict ? '返回修改' : '取消'}
        // destroyOnHidden 只在 Modal **关闭**时销毁子节点。切到冲突那一步时 Modal
        // 仍是开着的，所以表单实例与已填内容都还活着，「返回修改」原样回来。
        destroyOnHidden
        width={conflict ? 640 : undefined}
      >
        {conflict && (
          <ConflictResolution
            detail={conflict.detail}
            content={conflict.values.content}
            value={resolution}
            onChange={setResolution}
          />
        )}
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          // 冲突那一步藏起表单而不是卸载：内容要留着，「返回修改」时原样回来
          style={conflict ? { display: 'none' } : undefined}
        >
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
