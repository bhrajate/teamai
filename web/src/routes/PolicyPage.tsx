import { DeleteOutlined, PlusOutlined, SaveOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Flex,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Typography,
} from 'antd'
import { useCallback, useEffect, useState } from 'react'

import { policyApi } from '@/api'
import { ApiError } from '@/api/client'
import type { AmbientRule } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { ErrorBlock, LoadingBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatTime } from '@/lib/format'

/** 后端目前只注册了 thread_stale 一条规则（application/ambient.py 的 _handlers）。 */
const TRIGGERS = [{ label: '线程沉寂（thread_stale）', value: 'thread_stale' }]

const ACTIONS = [
  { label: '轻推提醒（nudge）', value: 'nudge' },
  { label: '仅记录（log）', value: 'log' },
]

/**
 * thread_stale 的两个参数。键名与 application/ambient.py 的 `_minutes(rule, ...)`
 * 一致，缺项即用后端默认值（idle 60 分钟、cooldown 取 idle 本身）。
 */
const IDLE_KEY = 'idle_minutes'
const COOLDOWN_KEY = 'cooldown_minutes'

/** 后端 `_candidates()` 用它当查询窗口，故填得比这更短本轮取不到。 */
const DEFAULT_IDLE_MINUTES = 60

/** 分钟数输入。清空即从 params 里删掉该键，让后端回落到自己的默认值。 */
function MinutesInput({
  value,
  onChange,
  placeholder,
}: {
  value: unknown
  onChange: (v: number | null) => void
  placeholder: string
}) {
  return (
    <InputNumber
      min={1}
      value={typeof value === 'number' ? value : null}
      onChange={onChange}
      placeholder={placeholder}
      style={{ width: 112 }}
    />
  )
}

/** 写入/删除单个 param。传 null 即删键，与 MinutesInput 的「清空」对应。 */
function setParam(
  rule: AmbientRule,
  key: string,
  value: number | null,
): Record<string, unknown> {
  if (value === null) {
    return Object.fromEntries(Object.entries(rule.params).filter(([k]) => k !== key))
  }
  return { ...rule.params, [key]: value }
}

export function PolicyPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [saving, setSaving] = useState(false)

  const [tools, setTools] = useState<string[]>([])
  const [rules, setRules] = useState<AmbientRule[]>([])
  /** 首次载入完成前不渲染表单，否则会闪一下空选项再跳成真值。 */
  const [ready, setReady] = useState(false)

  const policy = useAsync(
    useCallback((signal: AbortSignal) => policyApi.get(channelId, signal), [channelId]),
    [channelId],
  )
  const available = useAsync(useCallback((signal: AbortSignal) => policyApi.tools(signal), []), [])

  useEffect(() => {
    if (policy.data) {
      setTools(policy.data.allowed_tools)
      setRules(policy.data.ambient_rules)
      setReady(true)
    } else if (policy.error?.isNotFound) {
      // 未配过策略：空白名单起步。空白名单即「一个工具都不给」，这是安全的默认
      setTools([])
      setRules([])
      setReady(true)
    }
  }, [policy.data, policy.error])

  const save = async () => {
    setSaving(true)
    try {
      await policyApi.set(channelId, { allowed_tools: tools, ambient_rules: rules })
      message.success('已保存')
      policy.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const addRule = () =>
    setRules((prev) => [...prev, { trigger: 'thread_stale', params: {}, action: 'nudge' }])

  const patchRule = (index: number, patch: Partial<AmbientRule>) =>
    setRules((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)))

  const dropRule = (index: number) => setRules((prev) => prev.filter((_, i) => i !== index))

  // 404 是「还没配」，不是故障；其余错误照常报
  if (policy.error && !policy.error.isNotFound) {
    return (
      <>
        <PageHeader title="权限策略" />
        <ErrorBlock error={policy.error} onRetry={policy.reload} />
      </>
    )
  }

  if (!ready) {
    return (
      <>
        <PageHeader title="权限策略" />
        <LoadingBlock rows={6} />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="权限策略"
        description="控制这个频道能用哪些工具、在什么条件下主动开口。未授权的工具根本不会出现在发给模型的工具列表里。"
        extra={
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={saving}
            onClick={() => void save()}
          >
            保存
          </Button>
        }
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={11}>
          <Card title="工具白名单">
            {available.error ? (
              <Alert
                type="warning"
                showIcon
                title="拉不到工具清单"
                description={available.error.detail}
              />
            ) : available.loading ? (
              <LoadingBlock rows={3} />
            ) : (
              <Flex vertical gap={12}>
                <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 13 }}>
                  勾中的工具才对本频道可见。全不勾即完全禁用工具调用 —— 这也是未配策略时的默认。
                </Typography.Paragraph>
                <Checkbox.Group
                  value={tools}
                  onChange={(v) => setTools(v as string[])}
                  style={{ display: 'flex', flexDirection: 'column', gap: 10 }}
                >
                  {(available.data ?? []).map((name) => (
                    <Checkbox key={name} value={name}>
                      <Typography.Text className="mono">{name}</Typography.Text>
                    </Checkbox>
                  ))}
                </Checkbox.Group>
                {(available.data ?? []).length === 0 && (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="后端没注册任何工具"
                  />
                )}
                {/* 白名单里存在已下线的工具时要提示，否则这条配置会静默失效 */}
                {tools.filter((t) => !(available.data ?? []).includes(t)).length > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    title="白名单含未注册的工具"
                    description={`${tools
                      .filter((t) => !(available.data ?? []).includes(t))
                      .join('、')} 不在后端已注册的工具里，保存后不会生效。`}
                  />
                )}
              </Flex>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={13}>
          <Card
            title="主动介入规则"
            extra={
              <Button size="small" icon={<PlusOutlined />} onClick={addRule}>
                加一条
              </Button>
            }
          >
            <Alert
              type="info"
              showIcon
              style={{ marginBlockEnd: 16 }}
              title="还需频道开关配合"
              description="这些规则只在频道的「主动介入」开关打开时才起作用，两级都开才会真正发消息。"
            />

            {rules.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有规则，不会主动开口" />
            ) : (
              <Flex vertical gap={12}>
                {rules.map((rule, i) => (
                  <Card key={i} size="small" variant="outlined">
                    <Flex vertical gap={10}>
                      <Flex gap={8} align="center" wrap>
                        <Select
                          value={rule.trigger}
                          onChange={(v) => patchRule(i, { trigger: v })}
                          options={TRIGGERS}
                          style={{ minWidth: 210 }}
                        />
                        <Select
                          value={rule.action}
                          onChange={(v) => patchRule(i, { action: v })}
                          options={ACTIONS}
                          style={{ minWidth: 170 }}
                        />
                        <Button
                          type="text"
                          danger
                          size="small"
                          icon={<DeleteOutlined />}
                          onClick={() => dropRule(i)}
                          style={{ marginInlineStart: 'auto' }}
                        />
                      </Flex>

                      {rule.trigger === 'thread_stale' ? (
                        <Flex vertical gap={8}>
                          <Space size={8} wrap>
                            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                              沉寂超过
                            </Typography.Text>
                            <MinutesInput
                              value={rule.params[IDLE_KEY]}
                              onChange={(v) => patchRule(i, { params: setParam(rule, IDLE_KEY, v) })}
                              placeholder={`默认 ${DEFAULT_IDLE_MINUTES}`}
                            />
                            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                              分钟后提醒，同一任务至少间隔
                            </Typography.Text>
                            <MinutesInput
                              value={rule.params[COOLDOWN_KEY]}
                              onChange={(v) =>
                                patchRule(i, { params: setParam(rule, COOLDOWN_KEY, v) })
                              }
                              placeholder="同上"
                            />
                            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                              分钟
                            </Typography.Text>
                          </Space>

                          {/* 后端拿默认值当候选查询窗口，填更短的值不会更早触发。
                              不写出来的话，配了 10 分钟却等了一小时会以为是 bug。 */}
                          {typeof rule.params[IDLE_KEY] === 'number' &&
                            (rule.params[IDLE_KEY] as number) < DEFAULT_IDLE_MINUTES && (
                              <Typography.Text type="warning" style={{ fontSize: 12 }}>
                                低于 {DEFAULT_IDLE_MINUTES} 分钟的阈值不会更早触发 ——
                                巡检按默认值取候选任务，短于它的那部分要等下一轮。
                              </Typography.Text>
                            )}
                        </Flex>
                      ) : (
                        <Input
                          value={JSON.stringify(rule.params)}
                          disabled
                          addonBefore="params"
                          className="mono"
                        />
                      )}
                    </Flex>
                  </Card>
                ))}
              </Flex>
            )}

            {policy.data && (
              <Typography.Paragraph type="secondary" style={{ margin: '16px 0 0', fontSize: 13 }}>
                最后更新 {formatTime(policy.data.updated_at)}
              </Typography.Paragraph>
            )}
          </Card>
        </Col>
      </Row>
    </>
  )
}
