import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AppLayout } from '@/components/AppLayout'
import { darkTheme, lightTheme } from '@/theme'

import { AuditPage } from '@/routes/AuditPage'
import { BudgetPage } from '@/routes/BudgetPage'
import { ChannelListPage } from '@/routes/ChannelListPage'
import { InteractionPage } from '@/routes/InteractionPage'
import { MemoryPage } from '@/routes/MemoryPage'
import { NotFoundPage } from '@/routes/NotFoundPage'
import { OverviewPage } from '@/routes/OverviewPage'
import { PolicyPage } from '@/routes/PolicyPage'
import { SettingsPage } from '@/routes/SettingsPage'
import { TagPage } from '@/routes/TagPage'
import { TaskPage } from '@/routes/TaskPage'

const DARK_KEY = 'teamai.dark'

/** 首次访问跟随系统偏好，之后记住手动选择。 */
function initialDark(): boolean {
  try {
    const saved = localStorage.getItem(DARK_KEY)
    if (saved !== null) return saved === '1'
  } catch {
    // 隐私模式读不到，跟随系统
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}

export function App() {
  const [dark, setDark] = useState(initialDark)

  useEffect(() => {
    try {
      localStorage.setItem(DARK_KEY, dark ? '1' : '0')
    } catch {
      // 存不下不影响本次会话
    }
    // 让原生控件（滚动条、表单）跟着切，否则深色下会露出白色滚动条
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
  }, [dark])

  return (
    <ConfigProvider locale={zhCN} theme={dark ? darkTheme : lightTheme}>
      {/* AntApp 提供 message/modal 的静态上下文，从而能吃到上面的主题 */}
      <AntApp>
        <BrowserRouter>
          <Routes>
            <Route element={<AppLayout dark={dark} onToggleDark={() => setDark((v) => !v)} />}>
              <Route index element={<Navigate to="/channels" replace />} />
              <Route path="/channels" element={<ChannelListPage />} />
              <Route path="/channels/:channelId" element={<OverviewPage />} />
              <Route path="/channels/:channelId/tasks" element={<TaskPage />} />
              <Route path="/channels/:channelId/memories" element={<MemoryPage />} />
              <Route path="/channels/:channelId/budget" element={<BudgetPage />} />
              <Route path="/channels/:channelId/policy" element={<PolicyPage />} />
              <Route path="/channels/:channelId/tags" element={<TagPage />} />
              <Route path="/channels/:channelId/interactions" element={<InteractionPage />} />
              <Route path="/channels/:channelId/audit" element={<AuditPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  )
}
