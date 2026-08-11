import { theme, type ThemeConfig } from 'antd'

/**
 * 设计基调：克制的靛紫主色 + 偏大的圆角 + 收窄的字号层级。
 *
 * 不用 AntD 默认蓝：这套界面要长时间盯着看审计与任务列表，默认蓝在大面积
 * 表格里偏跳；靛紫饱和度低一档，配灰阶更稳。
 *
 * 字体栈显式列出中文字族：控制台是中文界面，缺了 PingFang / 微软雅黑
 * 这类回落，Linux 与 Windows 上会掉进衬线字体，字重与行高全乱。
 */
const FONT_STACK = [
  '-apple-system',
  'BlinkMacSystemFont',
  '"Segoe UI"',
  'Roboto',
  '"PingFang SC"',
  '"Hiragino Sans GB"',
  '"Microsoft YaHei"',
  '"Source Han Sans SC"',
  '"Noto Sans CJK SC"',
  '"Helvetica Neue"',
  'sans-serif',
].join(', ')

const MONO_STACK = [
  '"SFMono-Regular"',
  'ui-monospace',
  'Menlo',
  'Consolas',
  '"JetBrains Mono"',
  '"Cascadia Code"',
  'monospace',
].join(', ')

export const BRAND = '#5b5bd6'

/** 主色与语义色。深浅两套主题共用，避免品牌色在切换时跳变。 */
const sharedTokens = {
  colorPrimary: BRAND,
  colorInfo: BRAND,
  colorSuccess: '#12a594',
  colorWarning: '#ffb224',
  colorError: '#e5484d',
  colorLink: BRAND,

  fontFamily: FONT_STACK,
  fontFamilyCode: MONO_STACK,
  fontSize: 14,

  borderRadius: 8,
  borderRadiusLG: 12,
  borderRadiusSM: 6,

  controlHeight: 36,
  wireframe: false,
} satisfies ThemeConfig['token']

/** 组件级微调。只改与「密集数据界面」直接相关的几处，不做全面覆盖。 */
const components: ThemeConfig['components'] = {
  Layout: {
    // 侧栏与内容区靠色差分层，不画边框 —— 边框在窄屏上会显得局促
    bodyBg: 'transparent',
    headerHeight: 56,
    headerPadding: '0 20px',
  },
  Menu: {
    itemHeight: 38,
    itemMarginInline: 8,
    itemBorderRadius: 8,
    // 选中项只靠底色区分，不要左侧竖条：竖条与圆角同时出现会互相打架
    activeBarWidth: 0,
    activeBarBorderWidth: 0,
  },
  Table: {
    headerBorderRadius: 0,
    cellPaddingBlock: 12,
    // 表头比正文低一档对比，让数据本身更跳
    headerSplitColor: 'transparent',
  },
  Card: {
    paddingLG: 20,
  },
  Statistic: {
    contentFontSize: 26,
  },
  Descriptions: {
    itemPaddingBottom: 12,
  },
  Tag: {
    borderRadiusSM: 6,
  },
}

export const lightTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    ...sharedTokens,
    colorBgLayout: '#f6f6f9',
    colorBgContainer: '#ffffff',
    colorBorderSecondary: '#ececf2',
  },
  components,
}

export const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    ...sharedTokens,
    // 纯黑会让卡片与背景失去层次，取一档偏冷的深灰
    colorBgLayout: '#131318',
    colorBgContainer: '#1b1b21',
    colorBorderSecondary: '#2a2a33',
  },
  components,
}
