import { Button, Result } from 'antd'
import { Link } from 'react-router-dom'

export function NotFoundPage() {
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="地址可能敲错了，或者这个频道实例已经被移除。"
      extra={
        <Link to="/channels">
          <Button type="primary">回到频道列表</Button>
        </Link>
      }
    />
  )
}
