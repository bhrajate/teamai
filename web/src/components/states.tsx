import { ApiOutlined, LockOutlined, ReloadOutlined } from '@ant-design/icons'
import { Button, Empty, Result, Skeleton, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'

import type { ApiError } from '@/api/client'

/**
 * 三种非正常态的统一呈现：加载中、出错、空。
 *
 * 集中在这里而非各页面自判，原因与 tags.tsx 相同 —— 尤其 401：
 * 十个页面各写一遍「去设置令牌」的引导，措辞必然不一致。
 */

export function LoadingBlock({ rows = 4 }: { rows?: number }) {
  return <Skeleton active paragraph={{ rows }} />
}

/**
 * 错误态。401 与连不上后端各给专门文案 —— 这两种最常见，且解法完全不同：
 * 前者去填令牌，后者去起进程。
 */
export function ErrorBlock({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  if (error.isUnauthorized) {
    return (
      <Result
        icon={<LockOutlined />}
        status="warning"
        title="需要 Admin API 令牌"
        subTitle="后端配了 ADMIN_API_TOKEN，浏览器这边还没有。在设置页填入后即可访问。"
        extra={
          <Link to="/settings">
            <Button type="primary">去设置</Button>
          </Link>
        }
      />
    )
  }

  if (error.status === 0) {
    return (
      <Result
        icon={<ApiOutlined />}
        status="error"
        title="连不上后端"
        subTitle={error.detail}
        extra={
          <Space direction="vertical" align="center">
            <Typography.Text type="secondary">
              本地调试先跑 <Typography.Text code>make run-web</Typography.Text>
            </Typography.Text>
            {onRetry && (
              <Button icon={<ReloadOutlined />} onClick={onRetry}>
                重试
              </Button>
            )}
          </Space>
        }
      />
    )
  }

  return (
    <Result
      status="error"
      title={`请求失败（HTTP ${error.status}）`}
      subTitle={error.detail}
      extra={
        onRetry && (
          <Button icon={<ReloadOutlined />} onClick={onRetry}>
            重试
          </Button>
        )
      }
    />
  )
}

export function EmptyBlock({
  description,
  action,
}: {
  description: React.ReactNode
  action?: React.ReactNode
}) {
  return (
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description}>
      {action}
    </Empty>
  )
}

/**
 * 取数三态的收口：加载中出骨架、出错出错误块、有数据交给 children。
 *
 * 404 单独放行（`treatNotFoundAsEmpty`）：频道没配预算/策略时后端就返回 404，
 * 那不是故障，页面该显示「还没配，去配一个」而不是红色报错。
 */
export function AsyncBoundary<T>({
  state,
  children,
  skeletonRows,
  treatNotFoundAsEmpty,
  emptyDescription,
  emptyAction,
}: {
  state: { data: T | undefined; loading: boolean; error: ApiError | undefined; reload: () => void }
  children: (data: T) => React.ReactNode
  skeletonRows?: number
  treatNotFoundAsEmpty?: boolean
  emptyDescription?: React.ReactNode
  emptyAction?: React.ReactNode
}) {
  if (state.loading && state.data === undefined) return <LoadingBlock rows={skeletonRows} />

  if (state.error) {
    if (treatNotFoundAsEmpty && state.error.isNotFound) {
      return <EmptyBlock description={emptyDescription ?? '尚未配置'} action={emptyAction} />
    }
    return <ErrorBlock error={state.error} onRetry={state.reload} />
  }

  if (state.data === undefined) return <LoadingBlock rows={skeletonRows} />
  return <>{children(state.data)}</>
}
