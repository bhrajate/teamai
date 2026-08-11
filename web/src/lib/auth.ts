/**
 * Admin API 令牌的本地存放。
 *
 * 放 localStorage 而不是打进构建产物：产物是静态文件，任何访客都能下载，
 * 令牌写进去等于公开。也不做登录态 —— 后端是单一共享令牌（见 admin/auth.py），
 * 没有用户概念，此处只是「把令牌记在这台机器上」。
 *
 * 这意味着同机器的其他人能从 devtools 读到它。对内网管理后台是可接受的折中；
 * 要按人区分权限，得先在后端引入会话与用户模型。
 */

const KEY = 'teamai.admin_token'

/** 令牌变更要让已挂载的组件重新取数，故自己广播一个事件。 */
const EVENT = 'teamai:token-change'

export function getToken(): string {
  try {
    return localStorage.getItem(KEY) ?? ''
  } catch {
    // 隐私模式下 localStorage 可能抛异常，降级成「没有令牌」
    return ''
  }
}

export function setToken(token: string): void {
  try {
    const trimmed = token.trim()
    if (trimmed) localStorage.setItem(KEY, trimmed)
    else localStorage.removeItem(KEY)
  } catch {
    // 存不下就算了，本次会话仍能用（request 每次都读，读不到即匿名）
  }
  window.dispatchEvent(new Event(EVENT))
}

export function onTokenChange(fn: () => void): () => void {
  window.addEventListener(EVENT, fn)
  // storage 事件只在**其他**标签页改动时触发，用来同步多开的标签页
  window.addEventListener('storage', fn)
  return () => {
    window.removeEventListener(EVENT, fn)
    window.removeEventListener('storage', fn)
  }
}
