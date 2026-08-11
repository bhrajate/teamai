import {
  AuditOutlined,
  BulbOutlined,
  DatabaseOutlined,
  DeploymentUnitOutlined,
  MoonOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  SunOutlined,
  TagsOutlined,
  ThunderboltOutlined,
  UnorderedListOutlined,
  WalletOutlined,
} from '@ant-design/icons'
import { Layout, Menu, Tooltip, Typography, theme } from 'antd'
import { useMemo } from 'react'
import { Link, Outlet, useLocation, useParams } from 'react-router-dom'

import { ChannelSwitcher } from '@/components/ChannelSwitcher'
import { HealthBadge } from '@/components/HealthBadge'

const { Sider, Header, Content } = Layout

/** 侧栏条目。key 即路由末段，与后端资源模块同名，便于对照。 */
const RESOURCE_ITEMS = [
  { key: '', icon: <DeploymentUnitOutlined />, label: '概览' },
  { key: 'tasks', icon: <UnorderedListOutlined />, label: '任务' },
  { key: 'memories', icon: <DatabaseOutlined />, label: '记忆' },
  { key: 'budget', icon: <WalletOutlined />, label: '预算' },
  { key: 'policy', icon: <SafetyCertificateOutlined />, label: '权限策略' },
  { key: 'tags', icon: <TagsOutlined />, label: '标签' },
  { key: 'audit', icon: <AuditOutlined />, label: '审计' },
]

export function AppLayout({ dark, onToggleDark }: { dark: boolean; onToggleDark: () => void }) {
  const { token } = theme.useToken()
  const { pathname } = useLocation()
  const { channelId } = useParams<{ channelId: string }>()

  /** 选中项由 URL 反推，从而刷新页面与前进后退都能对上。 */
  const selectedKey = useMemo(() => {
    if (pathname.startsWith('/settings')) return 'settings'
    if (!channelId) return 'channels'
    const tail = pathname.split(`/channels/${channelId}`)[1]?.replace(/^\//, '') ?? ''
    return `res:${tail}`
  }, [pathname, channelId])

  const items = useMemo(() => {
    const base = [
      {
        key: 'channels',
        icon: <ThunderboltOutlined />,
        label: <Link to="/channels">全部频道</Link>,
      },
    ]

    // 未选频道时不出资源组：那些路由都要 channelId，出来了点不动
    const resources = channelId
      ? [
          {
            type: 'group' as const,
            key: 'grp-resource',
            label: '当前频道',
            children: RESOURCE_ITEMS.map((it) => ({
              key: `res:${it.key}`,
              icon: it.icon,
              label: (
                <Link to={`/channels/${channelId}${it.key ? `/${it.key}` : ''}`}>{it.label}</Link>
              ),
            })),
          },
        ]
      : []

    return [
      ...base,
      ...resources,
      { type: 'divider' as const, key: 'div-1' },
      { key: 'settings', icon: <SettingOutlined />, label: <Link to="/settings">设置</Link> },
    ]
  }, [channelId])

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        width={232}
        breakpoint="lg"
        collapsedWidth={0}
        style={{ background: token.colorBgContainer, borderInlineEnd: `1px solid ${token.colorBorderSecondary}` }}
      >
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            paddingInline: 20,
          }}
        >
          <BulbOutlined style={{ color: token.colorPrimary, fontSize: 18 }} />
          <Typography.Text strong style={{ fontSize: 15, letterSpacing: 0.2 }}>
            TeamAI
          </Typography.Text>
        </div>

        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          items={items}
          style={{ borderInlineEnd: 0, paddingBlock: 4 }}
        />
      </Sider>

      <Layout>
        <Header
          style={{
            background: token.colorBgContainer,
            borderBlockEnd: `1px solid ${token.colorBorderSecondary}`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 16,
          }}
        >
          <ChannelSwitcher />
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <HealthBadge />
            <Tooltip title={dark ? '切到浅色' : '切到深色'}>
              <Typography.Link onClick={onToggleDark} style={{ display: 'flex' }}>
                {dark ? <SunOutlined /> : <MoonOutlined />}
              </Typography.Link>
            </Tooltip>
          </div>
        </Header>

        <Content style={{ padding: 24, maxWidth: 1360, width: '100%' }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
