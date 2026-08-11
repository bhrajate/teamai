import { ApiOutlined, KeyOutlined, SaveOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Col, Flex, Form, Input, Row, Typography } from 'antd'
import { useState } from 'react'

import { healthApi } from '@/api'
import { ApiError } from '@/api/client'
import { PageHeader } from '@/components/PageHeader'
import { getToken, setToken } from '@/lib/auth'

/**
 * 令牌设置。
 *
 * 令牌存 localStorage 而非打进构建产物 —— 产物是静态文件，任何访客都能下载，
 * 令牌写进去等于公开（详见 lib/auth.ts）。
 */
export function SettingsPage() {
  const { message } = App.useApp()
  const [form] = Form.useForm<{ token: string }>()
  const [pinging, setPinging] = useState(false)

  const save = () => {
    setToken(form.getFieldValue('token') ?? '')
    // setToken 会广播事件，各页面的 useAsync 收到后自动重取，不必刷浏览器
    message.success('已保存到本机')
  }

  const ping = async () => {
    setPinging(true)
    try {
      const r = await healthApi.check()
      message.success(`后端在线：${r.status}`)
    } catch (err) {
      message.error(err instanceof ApiError ? err.detail : '打不通后端')
    } finally {
      setPinging(false)
    }
  }

  return (
    <>
      <PageHeader
        title="设置"
        description="Admin API 的访问令牌只存在这台机器的浏览器里，不随构建产物分发。"
      />

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={13}>
          <Card title="Admin API 令牌">
            <Form
              form={form}
              layout="vertical"
              requiredMark={false}
              initialValues={{ token: getToken() }}
            >
              <Form.Item
                name="token"
                label="令牌"
                extra="与后端 .env 里的 ADMIN_API_TOKEN 一致。后端没配这一项时留空即可。"
              >
                <Input.Password
                  prefix={<KeyOutlined />}
                  placeholder="留空表示匿名访问"
                  autoComplete="off"
                />
              </Form.Item>

              <Flex gap={8}>
                <Button type="primary" icon={<SaveOutlined />} onClick={save}>
                  保存
                </Button>
                <Button icon={<ApiOutlined />} loading={pinging} onClick={() => void ping()}>
                  测试连通
                </Button>
              </Flex>
            </Form>
          </Card>
        </Col>

        <Col xs={24} lg={11}>
          <Card title="说明">
            <Flex vertical gap={12}>
              <Alert
                type="warning"
                showIcon
                title="这不是登录"
                description="后端是单一共享令牌，没有用户概念，同机器的其他人能从 devtools 里读到它。要按人区分权限，得先在后端引入会话与用户模型。"
              />
              <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 13 }}>
                「测试连通」打的是 <Typography.Text code>/api/health</Typography.Text>，
                该端点有意不校验令牌，好让探针与{' '}
                <Typography.Text code>make verify-*</Typography.Text> 能匿名访问。
                所以它只能说明后端在跑，不能说明令牌对不对 —— 令牌是否有效，看别的页面能否取到数据。
              </Typography.Paragraph>
            </Flex>
          </Card>
        </Col>
      </Row>
    </>
  )
}
