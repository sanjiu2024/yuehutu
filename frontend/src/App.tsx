/** 应用外壳：左侧导航 + 顶部栏 + 10 个页面；未登录时显示登录页。 */

import { useCallback, useEffect, useState } from 'react'
import { api, clearToken, errorText, getToken, isNotReady, setToken, setUnauthorizedHandler } from './api'
import {
  Badge,
  BrandLogo,
  ErrorBoundary,
  Notice,
  ThemeToggle,
  ToastProvider,
  applyTheme,
  readTheme,
  useToast,
  type ThemeName,
} from './components'
import { ICONS, IconLogout, type IconName } from './icons'
import { wsClient } from './ws'
import { OverviewPage } from './pages/Overview'
import { AccountsPage } from './pages/Accounts'
import { ItemsPage } from './pages/Items'
import { ChatsPage } from './pages/Chats'
import { OrdersPage } from './pages/Orders'
import { RefundsPage } from './pages/Refunds'
import { EvaluationsPage } from './pages/Evaluations'
import { ComplaintsPage } from './pages/Complaints'
import { ReturnAddressesPage } from './pages/ReturnAddresses'
import { SubAccountsPage } from './pages/SubAccounts'
import { CsRoutingPage } from './pages/CsRouting'
import { DataOverviewPage } from './pages/DataOverview'
import { FansPage } from './pages/Fans'
import { CsDataPage } from './pages/CsData'
import { ItemTemplatesPage } from './pages/ItemTemplates'
import { NotificationsPage } from './pages/Notifications'
import { FinancePage } from './pages/Finance'
import { PromotionPage } from './pages/Promotion'
import { MarketingPage } from './pages/Marketing'
import { QualificationPage } from './pages/Qualification'
import { SecurityCenterPage } from './pages/SecurityCenter'
import { AgentsPage } from './pages/Agents'
import { MasterPage } from './pages/Master'
import { ProvidersPage } from './pages/Providers'
import { PromptsPage } from './pages/Prompts'
import { RiskPage } from './pages/Risk'
import type { SystemInfo } from './types'

interface NavItem {
  key: string
  label: string
  icon: IconName
  desc: string
  /** 该页自己画了页头（标题 + 说明 + 页级按钮）时置 true：顶栏就不再重复一遍标题与说明。 */
  ownHeader?: boolean
}

/**
 * 10 个入口平铺一列太长、也看不出彼此关系，按用途分成 5 组。
 * 分组只影响侧栏排版，`key` 与路由一一对应，**顺序/分组都不改变 hash 行为**。
 */
const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: '总览',
    items: [
      {
        key: 'overview',
        label: '概览',
        icon: 'overview',
        desc: '账号状态、今日消息、风控一览',
        ownHeader: true,
      },
    ],
  },
  {
    title: '运营',
    items: [
      { key: 'accounts', label: '账号', icon: 'user', desc: '扫码登录、账号状态与手动验证', ownHeader: true },
      { key: 'items', label: '商品', icon: 'box', desc: '商品列表、编辑、批量改价与上下架' },
      { key: 'chats', label: '会话', icon: 'chat', desc: '买家会话与人工接管' },
    ],
  },
  {
    title: '商品',
    items: [
      { key: 'item-templates', label: '运费模版', icon: 'box', desc: '运费模版增删改与给商品设置运费（写操作要二次确认）' },
    ],
  },
  {
    title: '交易',
    items: [
      { key: 'orders', label: '订单', icon: 'tag', desc: '买家订单、发货、改价与退款（写操作要二次确认）' },
      { key: 'refunds', label: '退款', icon: 'refresh', desc: '退款申请与处理进度' },
      { key: 'evaluations', label: '评价', icon: 'checkCircle', desc: '买家评价与回复' },
      { key: 'complaints', label: '投诉', icon: 'warn', desc: '平台投诉与处理状态' },
      { key: 'return-addresses', label: '退货地址', icon: 'link', desc: '退货收货地址管理' },
    ],
  },
  {
    title: '小铺',
    items: [
      { key: 'sub-accounts', label: '子账号', icon: 'user', desc: '子账号与权限' },
      { key: 'cs-routing', label: '客服分流', icon: 'send', desc: '客服分组与分流规则' },
    ],
  },
  {
    title: '数据',
    items: [
      { key: 'data-overview', label: '数据总览', icon: 'chart', desc: '曝光、访问、询单与支付' },
      { key: 'fans', label: '粉丝数据', icon: 'users', desc: '粉丝增长与活跃' },
      { key: 'cs-data', label: '客服数据', icon: 'send', desc: '询单响应与服务质量' },
    ],
  },
  {
    title: '消息',
    items: [
      { key: 'notifications', label: '通知中心', icon: 'bell', desc: '平台通知与待办提醒' },
    ],
  },
  {
    title: '推广',
    items: [
      { key: 'promotion', label: '超级擦亮', icon: 'chart', desc: '余额、指标、计划概览与投放管理（写操作要二次确认）' },
      { key: 'marketing', label: '营销', icon: 'sparkle', desc: '商品指标、营销任务、价格竞争力与操作记录（只读）' },
      { key: 'qualification', label: '资质管理', icon: 'database', desc: '擦亮资质、类目与模板（提交材料要二次确认）' },
      { key: 'security-center', label: '安全中心', icon: 'shield', desc: '闲气值、违规记录与申诉入口（提交申诉要二次确认）' },
    ],
  },
  {
    title: '财务',
    items: [
      {
        key: 'finance',
        label: '财务',
        icon: 'chart',
        desc: '收入账单、支出账单、申请发票与开票信息（只读）',
      },
    ],
  },
  {
    title: 'AI',
    items: [
      { key: 'agents', label: 'AI 沟通', icon: 'agents', desc: 'AI 之间在聊什么' },
      { key: 'master', label: '主管 AI', icon: 'master', desc: '和主管 AI 对话派活' },
      { key: 'providers', label: 'AI 配置', icon: 'key', desc: 'API Key、模型与角色映射' },
      { key: 'prompts', label: '提示词', icon: 'prompts', desc: '全局 / 账号 / 商品三级提示词' },
    ],
  },
  {
    title: '系统',
    items: [
      {
        key: 'risk',
        label: '风控',
        icon: 'shield',
        desc: '熔断状态、冷却阶梯与自动滑块',
        ownHeader: true,
      },
    ],
  },
]

const NAV: NavItem[] = NAV_GROUPS.flatMap((group) => group.items)

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
  const [theme, setTheme] = useState<ThemeName>(() => readTheme())

  const changeTheme = useCallback((next: ThemeName) => {
    setTheme(next)
    applyTheme(next)
  }, [])

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

  // 401 时回到登录页
  useEffect(() => {
    setUnauthorizedHandler(() => setAuthed(false))
    return () => setUnauthorizedHandler(null)
  }, [])

  // 健康检查 + 系统信息（无鉴权，用于判断后端是否在线）
  useEffect(() => {
    let alive = true
    const viaHealth = () =>
      api
        .raw<{ ok?: boolean; version?: string }>('/api/health', { anonymous: true })
        .then((health) => {
          if (alive && health) setSystemInfo({ version: health.version, frontend_built: true })
        })
        .catch(() => undefined)

    // `/api/system/info` 是要鉴权的，匿名打必然 401 —— 没 token 时直接问 /api/health
    if (getToken()) {
      api
        .raw<SystemInfo>('/api/system/info')
        .then((info) => {
          if (alive) setSystemInfo(info)
        })
        .catch(() => viaHealth())
    } else {
      viaHealth()
    }
    return () => {
      alive = false
    }
  }, [])

  // WebSocket 连接状态 + 未读数
  useEffect(() => {
    if (!authed) return
    wsClient.connect()
    // 连接状态由 ws.ts 在 open/close 时推过来，不再每 2 秒问一次 isConnected()
    const offStatus = wsClient.onStatus(setConnected)
    const unsubscribe = wsClient.subscribe((event) => {
      if (event.topic === 'chat.message' || event.topic === 'message') {
        const payload = event.payload as { direction?: string }
        if (payload?.direction !== 'out') setUnread((n) => n + 1)
      }
    })
    return () => {
      offStatus()
      unsubscribe()
    }
  }, [authed])

  // 切换页面时清零未读角标
  useEffect(() => {
    if (page === 'chats') setUnread(0)
  }, [page])

  if (!authed) {
    return <LoginPage onSuccess={() => setAuthed(true)} theme={theme} onThemeChange={changeTheme} />
  }

  const current = NAV.find((item) => item.key === page) ?? NAV[0]

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-logo">
            <BrandLogo size={20} />
          </span>
          <div className="brand-text">
            <h1>闲鱼 AI 工作台</h1>
            <div className="sub">Xianyu AI Workbench</div>
          </div>
        </div>

        <nav className="nav">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.title}>
              <div className="nav-group-title">{group.title}</div>
              {group.items.map((item) => {
                const badge = item.key === 'chats' ? unread : 0
                const Icon = ICONS[item.icon]
                return (
                  <button
                    key={item.key}
                    className={`nav-item${item.key === page ? ' active' : ''}`}
                    onClick={() => goto(item.key)}
                    title={item.desc}
                  >
                    <span className="nav-icon">
                      <Icon size={17} />
                    </span>
                    <span>{item.label}</span>
                    {badge > 0 && <span className="nav-badge">{badge > 99 ? '99+' : badge}</span>}
                  </button>
                )
              })}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="sidebar-status">
            <span className={`ws-dot${connected ? ' on' : ''}`} />
            {connected ? '实时连接已建立' : '实时连接未建立'}
          </div>
          <div className="sidebar-meta">
            {systemInfo?.version ? `版本 ${systemInfo.version}` : '版本未知'}
            {systemInfo?.frozen ? ' · 打包版' : ''}
          </div>
          <button
            className="btn sm ghost btn-with-icon"
            onClick={() => {
              clearToken()
              wsClient.disconnect()
              setAuthed(false)
            }}
          >
            <IconLogout size={14} />
            <span>退出登录</span>
          </button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          {/* 概览 / 账号 / 风控 这三页自己画了页头（标题 + 说明 + 页级按钮），
              顶栏再写一遍就成了同一句话出现两次 —— 那三页顶栏只留状态与主题开关。 */}
          {!current.ownHeader && (
            <div className="topbar-head">
              <h2>{current.label}</h2>
              <span className="page-desc">{current.desc}</span>
            </div>
          )}
          <span className="spacer" />
          <div className="topbar-actions">
            <Badge tone={connected ? 'ok' : 'muted'}>{connected ? '实时在线' : '离线'}</Badge>
            <ThemeToggle theme={theme} onChange={changeTheme} />
          </div>
        </header>
        <div className="content">
          {/* key=page 让切页面时错误边界自动复位；否则一个页面崩了会一直卡在错误态 */}
          <ErrorBoundary key={page} onReset={() => goto(page)}>
            {page === 'overview' && <OverviewPage onGoto={goto} />}
            {page === 'accounts' && <AccountsPage />}
            {page === 'items' && <ItemsPage />}
            {page === 'chats' && <ChatsPage />}
            {page === 'orders' && <OrdersPage />}
            {page === 'refunds' && <RefundsPage />}
            {page === 'evaluations' && <EvaluationsPage />}
            {page === 'complaints' && <ComplaintsPage />}
            {page === 'return-addresses' && <ReturnAddressesPage />}
            {page === 'sub-accounts' && <SubAccountsPage />}
            {page === 'cs-routing' && <CsRoutingPage />}
            {page === 'data-overview' && <DataOverviewPage />}
            {page === 'fans' && <FansPage />}
            {page === 'cs-data' && <CsDataPage />}
            {page === 'item-templates' && <ItemTemplatesPage />}
            {page === 'notifications' && <NotificationsPage />}
            {page === 'finance' && <FinancePage />}
            {page === 'promotion' && <PromotionPage />}
            {page === 'marketing' && <MarketingPage />}
            {page === 'qualification' && <QualificationPage />}
            {page === 'security-center' && <SecurityCenterPage />}
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

function LoginPage({
  onSuccess,
  theme,
  onThemeChange,
}: {
  onSuccess: () => void
  theme: ThemeName
  onThemeChange: (next: ThemeName) => void
}) {
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
      <div className="login-theme">
        <ThemeToggle theme={theme} onChange={onThemeChange} />
      </div>
      <div className="login-card">
        <div className="login-logo">
          <BrandLogo size={32} />
        </div>
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
        <div className="login-foot">
          默认账号 admin，初始密码见 README。
          <br />
          如果是桌面版，打开程序时会自动登录。
          {systemInfo?.version ? `（后端版本 ${systemInfo.version}）` : ''}
        </div>
      </div>
    </div>
  )
}
