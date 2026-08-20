/**
 * 渲染冒烟：把每个页面在 Node 里服务端渲染一遍。
 *
 * `vite build` 只保证「编译过」，抓不到运行时问题 —— 缺失导出、循环导入、
 * 渲染期就抛的错，全都编译得过但一跑就白屏。这里用 renderToString 走一遍
 * 真实的组件树，把那类错误提前暴露。
 *
 * 用 MemoryRouter 而非 BrowserRouter：后者要 window.history，Node 里没有。
 * 各页面的取数会发 fetch 并失败，那是预期的 —— useAsync 把失败收进 state，
 * 首帧渲染的是骨架屏，不影响本探针的目的。
 *
 * 另有一段组件级用例（COMPONENT_CASES）：**只渲首帧就到不了的分支**在这里补。
 * 记忆写入的冲突界面要先撞上 409 才出现，页面首帧永远渲不到它 —— 而它是这个
 * 项目里最容易一改就白屏的地方（嵌套 Space + Radio + 条件渲染）。
 *
 *     npx vite-node scripts/smoke-render.tsx
 */

import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { renderToString } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { lightTheme } from '../src/theme'

import { AuditPage } from '../src/routes/AuditPage'
import { BudgetPage } from '../src/routes/BudgetPage'
import { ChannelListPage } from '../src/routes/ChannelListPage'
import { ChannelSkillPage } from '../src/routes/ChannelSkillPage'
import { InteractionPage } from '../src/routes/InteractionPage'
import { McpServerPage } from '../src/routes/McpServerPage'
import { ConflictResolution, MemoryPage } from '../src/routes/MemoryPage'
import { NotFoundPage } from '../src/routes/NotFoundPage'
import { OverviewPage } from '../src/routes/OverviewPage'
import { PolicyPage } from '../src/routes/PolicyPage'
import { SettingsPage } from '../src/routes/SettingsPage'
import { SkillPage } from '../src/routes/SkillPage'
import { TagPage } from '../src/routes/TagPage'
import { TaskPage } from '../src/routes/TaskPage'

const CH = 'ch_smoke'

/** 页面 → 它在路由表里的路径。路径要真实：各页面用 useChannelId 读参数。 */
const CASES: [string, React.ComponentType, string][] = [
  ['ChannelListPage', ChannelListPage, '/channels'],
  ['OverviewPage', OverviewPage, `/channels/${CH}`],
  ['TaskPage', TaskPage, `/channels/${CH}/tasks`],
  ['MemoryPage', MemoryPage, `/channels/${CH}/memories`],
  ['BudgetPage', BudgetPage, `/channels/${CH}/budget`],
  ['PolicyPage', PolicyPage, `/channels/${CH}/policy`],
  ['TagPage', TagPage, `/channels/${CH}/tags`],
  ['ChannelSkillPage', ChannelSkillPage, `/channels/${CH}/skills`],
  // McpServerPage 此前漏登记（这个 smoke 是唯一能抓渲染期崩溃的地方），一并补上
  ['McpServerPage', McpServerPage, `/channels/${CH}/mcp`],
  ['SkillPage', SkillPage, '/skills'],
  ['InteractionPage', InteractionPage, `/channels/${CH}/interactions`],
  // 单任务视图是另一条分支：走 listByTask、多出筛选条、排序反向
  ['InteractionPage(task)', InteractionPage, `/channels/${CH}/interactions?task=tsk_smoke`],
  ['AuditPage', AuditPage, `/channels/${CH}/audit`],
  // 全局变更视图是另一条分支：走 listGlobal、文案与空态都不同
  ['AuditPage(global)', AuditPage, `/channels/${CH}/audit?scope=global`],
  ['SettingsPage', SettingsPage, '/settings'],
  ['NotFoundPage', NotFoundPage, '/nope'],
]

/** 造一条候选冲突。`score` 为 null 即「字面比对兜底」那条路。 */
function conflictEntry(id: string, content: string, score: number | null) {
  return {
    entry: {
      id,
      channel_instance_id: CH,
      content,
      type: 'FACT' as const,
      source_user_id: null,
      source: 'DISTILLED' as const,
      embedding_ref: 'point-1',
      superseded_by: null,
      superseded_at: null,
      created_at: '2026-03-02T10:30:00+00:00',
    },
    score,
  }
}

/** 首帧到不了的分支。见文件头。 */
const COMPONENT_CASES: [string, React.ReactElement][] = [
  [
    'ConflictResolution',
    <ConflictResolution
      detail={{
        message: '发现 1 条疑似说同一件事的现行记忆。',
        degraded: false,
        conflicts: [conflictEntry('mem_old', '网关重试超时设为 3 秒', 0.93)],
      }}
      content="网关重试超时设为 5 秒"
      value={null}
      onChange={() => {}}
    />,
  ],
  [
    // 降级路径的形状不同：score 为 null，要显示「字面重复」而不是百分比。
    // Math.round(null * 100) 不会抛但会渲出「相似度 0%」，那是个会骗人的显示，
    // 故这条单独渲一遍。
    'ConflictResolution(degraded)',
    <ConflictResolution
      detail={{
        message: '发现 2 条字面重复的现行记忆。未配置 embedding，只能做字面比对。',
        degraded: true,
        conflicts: [
          conflictEntry('mem_a', '部署走 GitHub Actions', null),
          conflictEntry('mem_b', '部署走 GitHub Actions，不要手工 scp', null),
        ],
      }}
      content="部署走 GitHub Actions"
      value="mem_a"
      onChange={() => {}}
    />,
  ],
]

function main(): void {
  let failed = 0

  for (const [name, Page, path] of CASES) {
    try {
      const html = renderToString(
        <ConfigProvider locale={zhCN} theme={lightTheme}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/channels" element={<Page />} />
              <Route path="/channels/:channelId" element={<Page />} />
              <Route path="/channels/:channelId/:section" element={<Page />} />
              <Route path="/settings" element={<Page />} />
              <Route path="*" element={<Page />} />
            </Routes>
          </MemoryRouter>
        </ConfigProvider>,
      )

      // 渲染出空串说明组件没产出任何 DOM，等于白屏，与抛错同样是失败
      if (html.trim().length === 0) {
        console.error(`✗ ${name} 渲染结果为空`)
        failed += 1
        continue
      }
      console.log(`✓ ${name.padEnd(16)} ${String(html.length).padStart(6)} 字符`)
    } catch (err) {
      console.error(`✗ ${name} 渲染抛错:`)
      console.error(err instanceof Error ? (err.stack ?? err.message) : String(err))
      failed += 1
    }
  }

  for (const [name, element] of COMPONENT_CASES) {
    try {
      const html = renderToString(
        <ConfigProvider locale={zhCN} theme={lightTheme}>
          {element}
        </ConfigProvider>,
      )
      if (html.trim().length === 0) {
        console.error(`✗ ${name} 渲染结果为空`)
        failed += 1
        continue
      }
      console.log(`✓ ${name.padEnd(16)} ${String(html.length).padStart(6)} 字符`)
    } catch (err) {
      console.error(`✗ ${name} 渲染抛错:`)
      console.error(err instanceof Error ? (err.stack ?? err.message) : String(err))
      failed += 1
    }
  }

  const total = CASES.length + COMPONENT_CASES.length
  console.log(failed === 0 ? `\n全部 ${total} 个用例渲染通过` : `\n${failed} 个用例失败`)
  process.exit(failed === 0 ? 0 : 1)
}

main()
