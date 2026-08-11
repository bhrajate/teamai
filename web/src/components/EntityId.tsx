import { CopyOutlined } from '@ant-design/icons'
import { App, Tooltip, Typography } from 'antd'

import { shortId } from '@/lib/format'

/**
 * ID 的统一呈现：等宽、缩略、点一下复制全值。
 *
 * ID 是 `<前缀>_<ULID>`（26 位),完整显示会把表格列挤爆，但排查时又必须能拿到
 * 全值 —— 故显示缩略、复制给全量。
 */
export function EntityId({ id, tail = 6 }: { id: string; tail?: number }) {
  const { message } = App.useApp()

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id)
      message.success('已复制')
    } catch {
      // http 下（非 localhost）clipboard API 不可用，此时提示手动选中
      message.warning('当前环境不允许自动复制，请手动选中')
    }
  }

  return (
    <Tooltip title={id}>
      <Typography.Text
        className="mono"
        style={{ cursor: 'pointer', userSelect: 'all' }}
        onClick={copy}
      >
        {shortId(id, tail)}
        <CopyOutlined style={{ marginInlineStart: 6, opacity: 0.45 }} />
      </Typography.Text>
    </Tooltip>
  )
}
