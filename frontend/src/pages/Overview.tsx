/**
 * 概览：一屏看清「账号在线 / 今日会话 / 待人工接管 / 风控熔断」，并把**需要人处理的事**顶到最前面。
 *
 * 数据来源（重做 UI 时不动，均为 topic 订阅实时刷新，无轮询）：
 *  - `GET /api/accounts`（`accounts.changed`）
 *  - `GET /api/chats/conversations?limit=200`（`chats.message` / `chats.mode_changed` / `chats.ai_reply`）
 *  - `GET /api/risk/status`（`risk.changed` / `risk.cleared`）
 *
 * 已知后端字段缺口（本页只能如实降级，见页面注释）：
 *  - 没有「今日消息条数」聚合，只能用会话的 `last_message_at` 数出「今日活跃会话」；
 *  - 没有历史趋势/环比数据，所以指标卡只给绝对值和构成说明，不编造涨跌。
 */

import { useMemo, type ReactNode } from 'react'
import { api } from '../api'
import { AsyncBox, Badge, Notice } from '../components'
import {
  IconArrowRight,
  IconBox,
  IconChart,
  IconChat,
  IconCheckCircle,
  IconClock,
  IconKey,
  IconRefresh,
  IconShield,
  IconUser,
  IconUsers,
  IconWarn,
  IconWifi,
} from '../icons'
import { useAsync } from '../hooks/useAsync'
import { bool, formatDuration, formatTime, loginStateLabel, modeLabel, runtimeStateLabel, truncate } from '../format'
import type { Account, Conversation, ConversationListResult, RiskStatusAll } from '../types'
import './overview.css'

type TodoItem = {
  key: string
  icon: ReactNode
  title: string
  desc: string
  page: string
  tone: 'warn' | 'danger' | 'info'
}

/** 会话里 `last_message` 后端给的是对象（chats.py:116-121），不是字符串。 */
function lastMessageText(conversation: Conversation): string {
  const last = conversation.last_message
  if (last && typeof last === 'object') return last.content || '（暂无消息内容）'
  if (typeof last === 'string') return last
  return '（暂无消息内容）'
}

/** 买家名兜底：昵称为空时显示「买家 <id>」，不显示空白。 */
function peerName(conversation: Conversation): string {
  const nickname = (conversation.peer_nickname || '').trim()
  if (nickname) return nickname
  const userId = (conversation.peer_user_id || '').trim()
  return userId ? `买家 ${userId}` : '买家'
}

function accountName(account: Account): string {
  return account.nickname || account.remark || account.id
}

export function OverviewPage({ onGoto }: { onGoto: (page: string) => void }) {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  // 后端把会话包在 {conversations, total} 里
  const conversations = useAsync<ConversationListResult>(
    () => api.get<ConversationListResult>('/api/chats/conversations', { limit: 200 }),
    [],
    true,
    ['chats.message', 'chats.mode_changed', 'chats.ai_reply'],
  )
  // 不带 account_id 时返回 {states, blocked(账号id数组), cooldown_steps, auto_slider, ...}
  const risk = useAsync<RiskStatusAll>(() => api.get<RiskStatusAll>('/api/risk/status'), [], true, [
    'risk.changed',
    'risk.cleared',
  ])

  const accountList = accounts.data ?? []
  const conversationList = conversations.data?.conversations ?? []
  const riskStates = risk.data?.states ?? []
  const blockedIds = risk.data?.blocked ?? []

  const stats = useMemo(() => {
    const online = accountList.filter((a) => loginStateLabel(a.login_state).tone === 'ok').length
    const needRelogin = accountList.filter((a) => loginStateLabel(a.login_state).tone === 'danger')
    const running = accountList.filter((a) => a.runtime_state === 'running').length

    const startOfDay = new Date()
    startOfDay.setHours(0, 0, 0, 0)
    const activeToday = conversationList.filter((c) => {
      if (!c.last_message_at) return false
      const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(c.last_message_at)
        ? c.last_message_at
        : c.last_message_at.replace(' ', 'T') + 'Z'
      const time = new Date(normalized).getTime()
      return !Number.isNaN(time) && time >= startOfDay.getTime()
    }).length

    const unread = conversationList.reduce((sum, c) => sum + (Number(c.unread) || 0), 0)
    const manual = conversationList.filter((c) => c.mode === 'manual')
    const auto = conversationList.filter((c) => c.mode === 'auto').length
    const blockedAccounts = accountList.filter((a) => blockedIds.includes(a.id) || bool(a.blocked))

    return {
      online,
      needRelogin,
      running,
      activeToday,
      unread,
      manual,
      auto,
      blockedAccounts,
      total: accountList.length,
    }
  }, [accountList, conversationList, blockedIds])

  const blockedCount = stats.blockedAccounts.length
  const notReady = accounts.notReady || conversations.notReady || risk.notReady

  const recent = useMemo(
    () =>
      [...conversationList]
        .sort((a, b) => String(b.last_message_at || '').localeCompare(String(a.last_message_at || '')))
        .slice(0, 6),
    [conversationList],
  )

  /** 「需要处理的事」：把三件真正需要人动手的事合成一条待办流，点击直达对应页面。 */
  const todos = useMemo<TodoItem[]>(() => {
    const items: TodoItem[] = []

    for (const conversation of stats.manual.slice(0, 5)) {
      items.push({
        key: `conv-${conversation.id}`,
        icon: <IconChat size={16} />,
        title: `${peerName(conversation)} 已切到人工接管`,
        desc: lastMessageText(conversation),
        page: 'chats',
        tone: 'warn',
      })
    }

    for (const account of stats.needRelogin) {
      items.push({
        key: `acct-${account.id}`,
        icon: <IconKey size={16} />,
        title: `${accountName(account)} 登录态异常`,
        desc: account.relogin_reason || '需要重新扫码登录，否则收不到新消息。',
        page: 'accounts',
        tone: 'danger',
      })
    }

    for (const account of stats.blockedAccounts) {
      const remaining = Number(account.remaining_seconds) || 0
      items.push({
        key: `block-${account.id}`,
        icon: <IconShield size={16} />,
        title: `${accountName(account)} 风控熔断中`,
        desc:
          `连续命中 ${Number(account.consecutive_hits) || 0} 次` +
          (remaining > 0 ? `，约 ${formatDuration(remaining)} 后自动解除` : '，暂时不会再向平台发请求'),
        page: 'risk',
        tone: 'danger',
      })
    }

    return items
  }, [stats])

  const refreshAll = () => {
    accounts.reload()
    conversations.reload()
    risk.reload()
  }

  return (
    <div className="ov-page">
      <div className="ov-head">
        <div className="ov-head-text">
          <h2>
            <IconChart size={18} /> 概览
          </h2>
          <p>账号在线情况、今天的会话、待人工接管的对话和风控熔断，一眼看完；有需要处理的会置顶在下面。</p>
        </div>
        <button className="btn" onClick={refreshAll} disabled={accounts.loading || conversations.loading || risk.loading}>
          <IconRefresh size={15} /> 刷新全部
        </button>
      </div>

      {notReady && (
        <Notice kind="warn" onRetry={refreshAll}>
          部分后端接口还没就绪（并行开发中），下面显示为 0 或「未就绪」属正常现象。
        </Notice>
      )}

      <div className="ov-metrics">
        <button className="ov-metric" type="button" onClick={() => onGoto('accounts')}>
          <span className="ov-metric-icon ok">
            <IconUsers size={18} />
          </span>
          <span className="ov-metric-body">
            <span className="ov-metric-label">账号在线</span>
            <span className="ov-metric-value">
              {accounts.loading ? '…' : stats.online}
              <span className="ov-metric-total">/ {stats.total}</span>
            </span>
            <span className={`ov-metric-hint${stats.needRelogin.length > 0 ? ' danger' : ''}`}>
              {stats.total === 0
                ? '还没有绑定账号'
                : stats.needRelogin.length > 0
                  ? `需重新登录 ${stats.needRelogin.length} 个`
                  : `运行中 ${stats.running} 个 · 登录态都正常`}
            </span>
          </span>
        </button>

        <button className="ov-metric" type="button" onClick={() => onGoto('chats')}>
          <span className="ov-metric-icon ask">
            <IconChat size={18} />
          </span>
          <span className="ov-metric-body">
            <span className="ov-metric-label">今日活跃会话</span>
            <span className="ov-metric-value">{conversations.loading ? '…' : stats.activeToday}</span>
            <span className="ov-metric-hint">
              共 {conversationList.length} 个会话 · 未读 {stats.unread} 条
            </span>
          </span>
        </button>

        <button className={`ov-metric${stats.manual.length > 0 ? ' alert' : ''}`} type="button" onClick={() => onGoto('chats')}>
          <span className="ov-metric-icon warn">
            <IconUser size={18} />
          </span>
          <span className="ov-metric-body">
            <span className="ov-metric-label">待人工接管</span>
            <span className="ov-metric-value">{conversations.loading ? '…' : stats.manual.length}</span>
            <span className="ov-metric-hint">
              {stats.manual.length > 0 ? '这些会话 AI 不会自动回，需要你处理' : `AI 自动回复中 ${stats.auto} 个`}
            </span>
          </span>
        </button>

        <button className={`ov-metric${blockedCount > 0 ? ' alert' : ''}`} type="button" onClick={() => onGoto('risk')}>
          <span className={`ov-metric-icon ${blockedCount > 0 ? 'danger' : 'ok'}`}>
            <IconShield size={18} />
          </span>
          <span className="ov-metric-body">
            <span className="ov-metric-label">风控状态</span>
            <span className="ov-metric-value">{risk.loading ? '…' : blockedCount > 0 ? `${blockedCount} 个熔断` : '正常'}</span>
            <span className="ov-metric-hint">
              {blockedCount > 0 ? '点这里去风控页看冷却进度' : `共 ${riskStates.length} 个账号有风控记录`}
            </span>
          </span>
        </button>
      </div>

      <div className="ov-split">
        <div className="ov-col">
          <section className="ov-card">
            <header className="ov-card-head">
              <h3>
                <IconWarn size={15} /> 需要处理的事
                {todos.length > 0 && <Badge tone="warn">{todos.length}</Badge>}
              </h3>
              <span className="ov-card-hint">点了直接跳过去</span>
            </header>
            {todos.length === 0 ? (
              <div className="ov-empty">
                <span className="ov-empty-icon ok">
                  <IconCheckCircle size={22} />
                </span>
                <span className="ov-empty-title">暂时没有需要你处理的事</span>
                <span className="ov-empty-desc">
                  {conversationList.length === 0
                    ? '还没有会话：账号在线并收到买家消息后，这里会出现需要人工跟进的对话。'
                    : '没有人工接管的会话、没有登录态异常的账号、也没有账号被风控熔断。'}
                </span>
              </div>
            ) : (
              <div className="ov-todos">
                {todos.map((todo) => (
                  <button key={todo.key} type="button" className={`ov-todo ${todo.tone}`} onClick={() => onGoto(todo.page)}>
                    <span className={`ov-todo-icon ${todo.tone}`}>{todo.icon}</span>
                    <span className="ov-todo-body">
                      <span className="ov-todo-title">{todo.title}</span>
                      <span className="ov-todo-desc" title={todo.desc}>
                        {todo.desc}
                      </span>
                    </span>
                    <IconArrowRight size={15} />
                  </button>
                ))}
              </div>
            )}
          </section>

          <section className="ov-card">
            <header className="ov-card-head">
              <h3>
                <IconClock size={15} /> 最近会话
              </h3>
              <button className="btn sm ghost" onClick={() => onGoto('chats')}>
                打开会话页
              </button>
            </header>
            <AsyncBox
              loading={conversations.loading}
              error={conversations.error}
              notReady={conversations.notReady}
              onRetry={conversations.reload}
            >
              {recent.length === 0 ? (
                <div className="ov-empty">
                  <span className="ov-empty-icon">
                    <IconChat size={22} />
                  </span>
                  <span className="ov-empty-title">还没有会话</span>
                  <span className="ov-empty-desc">
                    账号在线并收到买家消息后，会话会出现在这里；平台上已有的历史会话可以在会话页点「同步历史会话」拉回来。
                  </span>
                </div>
              ) : (
                <div className="ov-list">
                  {recent.map((conversation) => {
                    const mode = modeLabel(conversation.mode)
                    return (
                      <button
                        key={conversation.id}
                        type="button"
                        className="ov-conv"
                        onClick={() => onGoto('chats')}
                        title={lastMessageText(conversation)}
                      >
                        <span className="ov-conv-main">
                          <span className="ov-conv-top">
                            <strong>{peerName(conversation)}</strong>
                            <span className="ov-conv-time">{formatTime(conversation.last_message_at)}</span>
                          </span>
                          <span className="ov-conv-text">{lastMessageText(conversation)}</span>
                          {conversation.item_id && (
                            <span className="ov-conv-item">
                              <IconBox size={12} /> {truncate(conversation.item_id, 18)}
                            </span>
                          )}
                        </span>
                        <span className="ov-conv-side">
                          <Badge tone={mode.tone}>{mode.text}</Badge>
                          {(Number(conversation.unread) || 0) > 0 && (
                            <span className="ov-conv-unread">{Number(conversation.unread)}</span>
                          )}
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}
            </AsyncBox>
          </section>
        </div>

        <div className="ov-col">
          <section className="ov-card">
            <header className="ov-card-head">
              <h3>
                <IconWifi size={15} /> 账号状态
              </h3>
              <button className="btn sm ghost" onClick={() => onGoto('accounts')}>
                管理账号
              </button>
            </header>
            <AsyncBox loading={accounts.loading} error={accounts.error} notReady={accounts.notReady} onRetry={accounts.reload}>
              {accountList.length === 0 ? (
                <div className="ov-empty">
                  <span className="ov-empty-icon">
                    <IconUser size={22} />
                  </span>
                  <span className="ov-empty-title">还没有绑定闲鱼账号</span>
                  <span className="ov-empty-desc">去「账号」页扫码登录一个账号，商品和会话才会有数据。</span>
                </div>
              ) : (
                <div className="ov-list">
                  {accountList.map((account) => {
                    const login = loginStateLabel(account.login_state)
                    const runtime = runtimeStateLabel(account.runtime_state)
                    return (
                      <button key={account.id} type="button" className="ov-acct" onClick={() => onGoto('accounts')}>
                        <span className="ov-acct-main">
                          <strong>{accountName(account)}</strong>
                          <span className="ov-acct-id mono">{account.id}</span>
                        </span>
                        <span className="ov-acct-badges">
                          <Badge tone={login.tone}>{login.text}</Badge>
                          <Badge tone={runtime.tone}>{runtime.text}</Badge>
                          {!bool(account.enabled) && <Badge tone="muted">已停用</Badge>}
                        </span>
                      </button>
                    )
                  })}
                </div>
              )}
            </AsyncBox>
          </section>

          <section className="ov-card">
            <header className="ov-card-head">
              <h3>
                <IconShield size={15} /> 风控状态
              </h3>
              <button className="btn sm ghost" onClick={() => onGoto('risk')}>
                去风控页
              </button>
            </header>
            <AsyncBox loading={risk.loading} error={risk.error} notReady={risk.notReady} onRetry={risk.reload}>
              {riskStates.length === 0 ? (
                <div className="ov-empty">
                  <span className="ov-empty-icon ok">
                    <IconCheckCircle size={22} />
                  </span>
                  <span className="ov-empty-title">暂无风控记录</span>
                  <span className="ov-empty-desc">没有触发风控事件，账号运行正常。</span>
                </div>
              ) : (
                <div className="ov-list">
                  {riskStates.map((item, index) => {
                    const isBlocked = blockedIds.includes(item.account_id || '')
                    return (
                      <div key={String(item.account_id ?? index)} className="ov-risk">
                        <span className="mono ov-risk-id">{item.account_id || '全局'}</span>
                        <span className="ov-risk-side">
                          <Badge tone={isBlocked ? 'danger' : 'ok'}>{isBlocked ? '熔断中' : '正常'}</Badge>
                          <span className="ov-risk-hits">连续命中 {Number(item.consecutive_hits) || 0} 次</span>
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </AsyncBox>
          </section>
        </div>
      </div>
    </div>
  )
}
