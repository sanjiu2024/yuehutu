/** 应用外壳：左侧导航 + 顶部栏 + 9 个页面；未登录时显示登录页。 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, clearToken, errorText, getToken, isNotReady, setToken, setUnauthorizedHandler } from './api'
import { Badge, ErrorBoundary, Notice, ToastProvider, useToast } from './components'
import { wsClient } from './ws'
import { OverviewPage } from './pages/Overview'
import { AccountsPage } from './pages/Accounts'
import { ItemsPage } from './pages/Items'
import { ChatsPage } from './pages/Chats'
import { AgentsPage } from './pages/Agents'
import { MasterPage } from './pages/Master'
import { ProvidersPage } from './pages/Providers'
import { PromptsPage } from './pages/Prompts'
import { RiskPage } from './pages/Risk'
import type { SystemInfo } from './types'

interface NavItem {
  key: string
  label: string
  icon: string
  desc: string
}

const NAV: NavItem[] = [
  { key: 'overview', label: '概览', icon: '📊', desc: '账号状态、今日消息、风控一览' },
  { key: 'accounts', label: '账号', icon: '👤', desc: '扫码登录、账号状态与手动验证' },
  { key: 'items', label: '商品', icon: '📦', desc: '商品列表、编辑、批量改价与上下架' },
  { key: 'chats', label: '会话', icon: '💬', desc: '买家会话与人工接管' },
  { key: 'agents', label: 'AI 沟通', icon: '🤝', desc: 'AI 之间在聊什么' },
  { key: 'master', label: '主管 AI', icon: '🧠', desc: '和主管 AI 对话派活' },
  { key: 'providers', label: 'AI 配置', icon: '🔑', desc: 'API Key、模型与角色映射' },
  { key: 'prompts', label: '提示词', icon: '📝', desc: '全局 / 账号 / 商品三级提示词' },
  { key: 'risk', label: '风控', icon: '🛡', desc: '熔断状态、冷却阶梯与自动滑块' },
]

const PAGE_TITLES: Record<string, string> = Object.fromEntries(NAV.map((item) => [item.key, item.label]))

function readHash(): string {
  const raw = window.location.hash.replace(/^#\/?/, '')
  return PAGE_TITLES[raw] ? raw : 'overview'
}

export default function App() {
  return (
    <ToastProvider>
      {/* 外壳自身崩了也不能白屏 */}
      <ErrorBoundary>
        <Shell />
      </ErrorBoundary>
    </ToastProvider>
  )
}

function Shell() {
  const [page, setPage] = useState<string>(() => readHash())
  const [authed, setAuthed] = useState<boolean>(() => Boolean(getToken()))
  const [connected, setConnected] = useState(false)
  const [systemInfo, setSystemInfo] = useState<SystemInfo | null>(null)
  const [unread, setUnread] = useState(0)

  // hash 路由
  useEffect(() => {
    const onHash = () => setPage(readHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const goto = useCallback((next: string) => {
    window.location.hash = `#/${next}`
    setPage(next)
  }, [])

  // 401 → 回到登录页
  useEffect(() => {
    setUnauthorizedHandler(() => setAuthed(false))
    return () => setUnauthorizedHandler(null)
  }, [])

  // 健康检查 + 系统信息（无鉴权，用于判断后端是否在线）
  useEffect(() => {
    let alive = true
    api
      .raw<SystemInfo>('/api/system/info', { anonymous: true })
      .then((info) => {
        if (alive) setSystemInfo(info)
      })
      .catch(() => {
        api
          .raw<{ ok?: boolean; version?: string }>('/api/health', { anonymous: true })
          .then((health) => {
            if (alive && health) setSystemInfo({ version: health.version, frontend_built: true })
          })
          .catch(() => undefined)
      })
    return () => {
      alive = false
    }
  }, [])

  // WebSocket 连接状态 + 未读数
  useEffect(() => {
    if (!authed) return
    wsClient.connect()
    const timer = window.setInterval(() => setConnected(wsClient.isConnected()), 2000)
    const unsubscribe = wsClient.subscribe((event) => {
      if (event.topic === 'chat.message' || event.topic === 'message') {
        const payload = event.payload as { direction?: string }
        if (payload?.direction !== 'out') setUnread((n) => n + 1)
      }
    })
    return () => {
      window.clearInterval(timer)
      unsubscribe()
    }
  }, [authed])

  // 切换页面时清零未读角标
  useEffect(() => {
    if (page === 'chats') setUnread(0)
  }, [page])

  const nav = useMemo(
    () =>
      NAV.map((item) => ({
        ...item,
        badge: item.key === 'chats' ? unread : 0,
      })),
    [unread],
  )

  if (!authed) {
    return <LoginPage onSuccess={() => setAuthed(true)} />
  }

  const current = NAV.find((item) => item.key === page) ?? NAV[0]

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <h1>闲鱼 AI 工作台</h1>
          <div className="sub">Xianyu AI Workbench</div>
        </div>
        <nav className="nav">
          {nav.map((item) => (
            <button
              key={item.key}
              className={`nav-item${item.key === page ? ' active' : ''}`}
              onClick={() => goto(item.key)}
              title={item.desc}
            >
              <span className="nav-icon">{item.icon}</span>
              <span>{item.label}</span>
              {item.badge > 0 && <span className="nav-badge">{item.badge > 99 ? '99+' : item.badge}</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>
            <span className={`ws-dot${connected ? ' on' : ''}`} />
            {connected ? '实时连接已建立' : '实时连接未建立'}
          </div>
          <div style={{ marginTop: 4 }}>
            {systemInfo?.version ? `版本 ${systemInfo.version}` : '版本未知'}
            {systemInfo?.frozen ? ' · 打包版' : ''}
          </div>
          <button
            className="btn sm ghost"
            style={{ marginTop: 8 }}
            onClick={() => {
              clearToken()
              wsClient.disconnect()
              setAuthed(false)
            }}
          >
            退出登录
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <h2>{current.label}</h2>
          <span className="dim" style={{ fontSize: 12 }}>
            {current.desc}
          </span>
          <span className="spacer" />
          <Badge tone={connected ? 'ok' : 'muted'}>{connected ? '实时在线' : '离线'}</Badge>
        </header>
        <div className="content">
          {/* key=page 让切页面时错误边界自动复位；否则一个页面崩了会一直卡在错误态 */}
          <ErrorBoundary key={page} onReset={() => goto(page)}>
            {page === 'overview' && <OverviewPage onGoto={goto} />}
            {page === 'accounts' && <AccountsPage />}
            {page === 'items' && <ItemsPage />}
            {page === 'chats' && <ChatsPage />}
            {page === 'agents' && <AgentsPage />}
            {page === 'master' && <MasterPage />}
            {page === 'providers' && <ProvidersPage />}
            {page === 'prompts' && <PromptsPage />}
            {page === 'risk' && <RiskPage />}
          </ErrorBoundary>
        </div>
      </main>
    </div>
  )
}

/* ---------------- 登录页 ---------------- */

function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const toast = useToast()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [hint, setHint] = useState('')
  const [systemInfo, setSystemInfo] = useState<SystemInfo | null>(null)

  // 桌面外壳会自己写入 token；这里只处理「还没登录」的情况
  useEffect(() => {
    if (getToken()) onSuccess()
  }, [onSuccess])

  useEffect(() => {
    let alive = true
    api
      .raw<SystemInfo>('/api/system/info', { anonymous: true })
      .then((info) => {
        if (alive) setSystemInfo(info)
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [])

  const submit = async () => {
    if (!username.trim() || !password) {
      setError('请输入用户名和密码')
      return
    }
    setBusy(true)
    setError('')
    try {
      const result = await api.raw<{ token?: string }>('/api/auth/login', {
        method: 'POST',
        body: { username: username.trim(), password },
        anonymous: true,
      })
      if (result?.token) {
        setToken(result.token)
        toast.success('登录成功')
        onSuccess()
      } else {
        setError('后端没有返回登录令牌')
      }
    } catch (err) {
      if (isNotReady(err)) {
        setHint('后端服务可能还没启动，或接口还没就绪。请确认程序正在运行。')
      }
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-logo">🐟</div>
        <h1>闲鱼 AI 工作台</h1>
        <div className="sub">登录后即可管理账号、商品与 AI</div>

        {error && <Notice kind="error">{error}</Notice>}
        {hint && <Notice kind="warn">{hint}</Notice>}

        <div className="field">
          <label>用户名</label>
          <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label>密码</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && void submit()}
          />
        </div>
        <button className="btn primary block" onClick={submit} disabled={busy}>
          {busy ? '登录中…' : '登录'}
        </button>
        <div className="dim" style={{ fontSize: 11.5, marginTop: 12, lineHeight: 1.7 }}>
          默认账号 admin，初始密码见 README。
          <br />
          如果是桌面版，打开程序时会自动登录。
          {systemInfo?.version ? `（后端版本 ${systemInfo.version}）` : ''}
        </div>
      </div>
    </div>
  )
}
