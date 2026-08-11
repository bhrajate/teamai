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
import { MemoryPage } from '../src/routes/MemoryPage'
import { NotFoundPage } from '../src/routes/NotFoundPage'
import { OverviewPage } from '../src/routes/OverviewPage'
import { PolicyPage } from '../src/routes/PolicyPage'
import { SettingsPage } from '../src/routes/SettingsPage'
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
  ['AuditPage', AuditPage, `/channels/${CH}/audit`],
  ['SettingsPage', SettingsPage, '/settings'],
  ['NotFoundPage', NotFoundPage, '/nope'],
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

  console.log(failed === 0 ? `\n全部 ${CASES.length} 个页面渲染通过` : `\n${failed} 个页面失败`)
  process.exit(failed === 0 ? 0 : 1)
}

main()
