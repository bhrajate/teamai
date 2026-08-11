import { Flex, Space, Typography } from 'antd'

/**
 * 页内标题区。左标题右操作，说明文字压在标题下。
 *
 * 不用 AntD 的 PageHeader（v5 起已移出主包）；这里只要这一种排布，
 * 自己写比引 @ant-design/pro-components 划算。
 */
export function PageHeader({
  title,
  description,
  extra,
}: {
  title: React.ReactNode
  description?: React.ReactNode
  extra?: React.ReactNode
}) {
  return (
    <Flex align="flex-start" justify="space-between" gap={16} style={{ marginBlockEnd: 20 }}>
      <div style={{ minWidth: 0 }}>
        <Typography.Title level={4} style={{ margin: 0, fontWeight: 600 }}>
          {title}
        </Typography.Title>
        {description && (
          <Typography.Paragraph type="secondary" style={{ margin: '6px 0 0', maxWidth: 720 }}>
            {description}
          </Typography.Paragraph>
        )}
      </div>
      {extra && <Space wrap>{extra}</Space>}
    </Flex>
  )
}
