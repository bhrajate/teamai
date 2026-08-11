import { SaveOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Flex,
  Form,
  InputNumber,
  Progress,
  Row,
  Segmented,
  Statistic,
  Typography,
} from 'antd'
import { useCallback, useEffect, useState } from 'react'

import { budgetApi } from '@/api'
import { ApiError } from '@/api/client'
import type { BudgetPeriod } from '@/api/types'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary } from '@/components/states'
import { BudgetStateTag } from '@/components/tags'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'
import { formatNumber, percent } from '@/lib/format'

const PERIODS: { label: string; value: BudgetPeriod }[] = [
  { label: '每日', value: 'DAILY' },
  { label: '每周', value: 'WEEKLY' },
  { label: '每月', value: 'MONTHLY' },
]

/**
 * 频道预算。
 *
 * 保存走 PUT，后端 `configure_channel_quota` 原地改上限与周期，用量不清零；
 * 新上限高于已用量时会把 EXHAUSTED 放回 ACTIVE。
 */
export function BudgetPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<{ token_limit: number; period: BudgetPeriod }>()

  const state = useAsync(
    useCallback((signal: AbortSignal) => budgetApi.get(channelId, signal), [channelId]),
    [channelId],
  )

  // 取到既有配置后回填表单，避免用户在空表单上重填一遍
  useEffect(() => {
    if (state.data) {
      form.setFieldsValue({
        token_limit: state.data.token_limit,
        period: state.data.period,
      })
    } else if (state.error?.isNotFound) {
      form.setFieldsValue({ token_limit: 1_000_000, period: 'MONTHLY' })
    }
  }, [state.data, state.error, form])

  const save = async () => {
    let values: { token_limit: number; period: BudgetPeriod }
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    try {
      await budgetApi.set(channelId, values)
      message.success('已保存')
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const exhausted = state.data?.state === 'EXHAUSTED'

  return (
    <>
      <PageHeader
        title="预算"
        description="按频道核算 token 配额。用尽后频道暂停响应，等到下个周期由 worker 的定时任务重置。"
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={13}>
          <Card title="当前用量">
            <AsyncBoundary
              state={state}
              skeletonRows={3}
              treatNotFoundAsEmpty
              emptyDescription="这个频道还没有配预算，右侧填一个上限即可启用"
            >
              {(b) => {
                const pct = percent(b.used_tokens, b.token_limit)
                return (
                  <Flex vertical gap={16}>
                    {exhausted && (
                      <Alert
                        type="error"
                        showIcon
                        title="配额已耗尽"
                        description="频道当前不响应新请求。等下个周期自动重置，或在右侧调高上限。"
                      />
                    )}
                    <Row gutter={16}>
                      <Col span={8}>
                        <Statistic title="已用" value={formatNumber(b.used_tokens)} />
                      </Col>
                      <Col span={8}>
                        <Statistic title="剩余" value={formatNumber(b.remaining)} />
                      </Col>
                      <Col span={8}>
                        <Statistic title="上限" value={formatNumber(b.token_limit)} />
                      </Col>
                    </Row>
                    <Progress
                      percent={Math.min(pct, 100)}
                      status={exhausted || pct >= 90 ? 'exception' : 'active'}
                      format={() => `${pct}%`}
                    />
                    <Descriptions column={1} size="small" colon={false}>
                      <Descriptions.Item label="状态">
                        <BudgetStateTag value={b.state} />
                      </Descriptions.Item>
                      <Descriptions.Item label="周期">
                        {PERIODS.find((p) => p.value === b.period)?.label ?? b.period}
                      </Descriptions.Item>
                      <Descriptions.Item label="范围">
                        {b.scope === 'CHANNEL' ? '本频道' : '整个组织'}
                      </Descriptions.Item>
                    </Descriptions>
                  </Flex>
                )
              }}
            </AsyncBoundary>
          </Card>
        </Col>

        <Col xs={24} lg={11}>
          <Card title="配置">
            <Alert
              type="info"
              showIcon
              style={{ marginBlockEnd: 16 }}
              title="保存即原地生效"
              description="只改上限与周期，已用量与本周期起点都保留。若当前是耗尽状态且新上限高于已用量，会自动恢复为正常。"
            />
            <Form form={form} layout="vertical" requiredMark={false}>
              <Form.Item
                name="token_limit"
                label="token 上限"
                rules={[
                  { required: true, message: '请填上限' },
                  {
                    type: 'number',
                    min: 1,
                    message: '必须为正整数',
                  },
                ]}
              >
                {/* 显式给泛型 number：不写的话 TS 会从 min={1} 推成字面量类型 1，
                    parser 的返回值随之被要求是 1。 */}
                <InputNumber<number>
                  style={{ width: '100%' }}
                  min={1}
                  step={100_000}
                  // 千分位。六七位数字不分组容易多打一个零
                  formatter={(v) => (v ? formatNumber(Number(v)) : '')}
                  parser={(v) => Number((v ?? '').replace(/[^\d]/g, ''))}
                />
              </Form.Item>
              <Form.Item name="period" label="重置周期">
                <Segmented options={PERIODS} block />
              </Form.Item>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                onClick={() => void save()}
                block
              >
                保存
              </Button>
              <Typography.Paragraph
                type="secondary"
                style={{ margin: '12px 0 0', fontSize: 13 }}
              >
                周期重置由 worker 进程的定时任务执行。worker 没跑的话，耗尽的频道不会自动恢复。
              </Typography.Paragraph>
            </Form>
          </Card>
        </Col>
      </Row>
    </>
  )
}
