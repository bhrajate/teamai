import { ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Checkbox, Flex, Space, Tag, Typography } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { skillApi } from '@/api'
import { ApiError } from '@/api/client'
import { PageHeader } from '@/components/PageHeader'
import { AsyncBoundary, EmptyBlock } from '@/components/states'
import { useAsync } from '@/hooks/useAsync'
import { useChannelId } from '@/hooks/useChannelId'

/**
 * 频道启用哪些技能。
 *
 * 技能本身在全局技能库里维护（`/skills`），这里只管勾选。勾中即授权 ——
 * 不需要再去权限策略页补什么，模型就能载入它。但技能正文里提到的**工具**仍受
 * 策略管制：技能说「用 github 工具查 PR」而该频道没授权 github，模型就调不到。
 *
 * 全局停用的技能仍然显示勾选状态（而不是把勾去掉），否则管理员会以为关联关系
 * 丢了 —— 库里那行还在，重新勾一次是空操作。
 */
export function ChannelSkillPage() {
  const channelId = useChannelId()
  const { message } = App.useApp()
  const [checked, setChecked] = useState<string[] | null>(null)
  const [saving, setSaving] = useState(false)

  const state = useAsync(
    useCallback((signal: AbortSignal) => skillApi.forChannel(channelId, signal), [channelId]),
    [channelId],
  )

  // 取回后把勾选灌进本地 state。改频道时 useAsync 会先清掉 data，
  // 故这里也要跟着回到 null，否则会把上一个频道的勾选显示在新频道上。
  useEffect(() => {
    setChecked(state.data ? state.data.enabled_ids : null)
  }, [state.data])

  const save = async () => {
    if (checked === null) return
    setSaving(true)
    try {
      const r = await skillApi.setForChannel(channelId, checked)
      // 用后端返回的集合而非本地的：不存在的 id 会被静默丢弃，
      // 拿返回值才能让界面收敛到真实状态
      setChecked(r.enabled_ids)
      message.success(`已启用 ${r.enabled_ids.length} 个技能`)
      state.reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const dirty =
    checked !== null &&
    state.data !== undefined &&
    (checked.length !== state.data.enabled_ids.length ||
      checked.some((id) => !state.data!.enabled_ids.includes(id)))

  return (
    <>
      <PageHeader
        title="技能"
        description="勾选这个频道可用的技能。模型会看到勾中技能的名称与描述，判断相关时自行载入完整说明。技能在技能库里统一维护，改动对所有启用频道即时生效。"
        extra={
          <Space>
            <Link to="/skills">
              <Button>去技能库</Button>
            </Link>
            <Button icon={<ReloadOutlined />} onClick={state.reload} loading={state.loading}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              onClick={save}
              loading={saving}
              disabled={!dirty}
            >
              保存
            </Button>
          </Space>
        }
      />

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        title="勾中即授权"
        description="不需要在权限策略里额外授权技能。但技能正文里提到的工具仍受策略管制 —— 若技能要用 github 工具而这个频道没授权它，模型仍然调不到。"
      />

      <AsyncBoundary state={state} skeletonRows={5}>
        {(data) =>
          data.skills.length === 0 ? (
            <Card>
              <EmptyBlock
                description={
                  <Space direction="vertical">
                    <Typography.Text>技能库里还没有技能。</Typography.Text>
                    <Link to="/skills">去技能库新建一个</Link>
                  </Space>
                }
              />
            </Card>
          ) : (
            <Card>
              <Checkbox.Group
                value={checked ?? []}
                onChange={(v) => setChecked(v as string[])}
                style={{ display: 'block' }}
              >
                <Flex vertical gap={14}>
                  {data.skills.map((s) => (
                    <Checkbox key={s.id} value={s.id} disabled={!s.enabled}>
                      <Space direction="vertical" size={0}>
                        <Space size={8}>
                          <Typography.Text strong>{s.name}</Typography.Text>
                          {!s.enabled && <Tag color="orange">已全局停用</Tag>}
                        </Space>
                        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                          {s.description}
                        </Typography.Text>
                      </Space>
                    </Checkbox>
                  ))}
                </Flex>
              </Checkbox.Group>
            </Card>
          )
        }
      </AsyncBoundary>
    </>
  )
}
