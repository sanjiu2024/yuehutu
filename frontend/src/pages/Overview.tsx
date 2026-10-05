/** 概览：账号状态、今日消息、待处理、风控状态。 */

import { useMemo } from 'react'
import { api } from '../api'
import { AsyncBox, Badge, Notice, Stat } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatTime, loginStateLabel, runtimeStateLabel } from '../format'
import type { Account, Conversation, ConversationListResult, RiskStatusAll } from '../types'

export function OverviewPage({ onGoto }: { onGoto: (page: string) => void }) {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [])
  // 后端把会话包在 {conversations, total} 里
  const conversations = useAsync<ConversationListResult>(
    () => api.get<ConversationListResult>('/api/chats/conversations', { limit: 200 }),
    [],
  )
  // 不带 account_id 时返回 {states, blocked(账号id数组), cooldown_steps, auto_slider, ...}
  const risk = useAsync<RiskStatusAll>(() => api.get<RiskStatusAll>('/api/risk/status'), [])

  const accountList = accounts.data ?? []
  const conversationList = conversations.data?.conversations ?? []
  const riskStates = risk.data?.states ?? []

  const stats = useMemo(() => {
    const online = accountList.filter((a) => loginStateLabel(a.login_state).tone === 'ok').length
    const needRelogin = accountList.filter((a) => loginStateLabel(a.login_state).tone === 'danger').length

    const startOfDay = new Date()
    startOfDay.setHours(0, 0, 0, 0)
    const todayMessages = conversationList.filter((c) => {
      if (!c.last_message_at) return false
      const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(c.last_message_at)
        ? c.last_message_at
        : c.last_message_at.replace(' ', 'T') + 'Z'
      const time = new Date(normalized).getTime()
      return !Number.isNaN(time) && time >= startOfDay.getTime()
    }).length

    const unread = conversationList.reduce((sum, c) => sum + (Number(c.unread) || 0), 0)
    const manual = conversationList.filter((c) => c.mode === 'manual').length

    return { online, needRelogin, todayMessages, unread, manual, total: accountList.length }
  }, [accountList, conversationList])

  const blockedIds = risk.data?.blocked ?? []
  const blockedCount = blockedIds.length

  const notReady = accounts.notReady || conversations.notReady || risk.notReady

  /** 会话列表里 last_message 是对象，取其中的正文。 */
  const lastMessageText = (conversation: Conversation): string => {
    const last = conversation.last_message
    if (last && typeof last === 'object') return last.content || '（暂无消息内容）'
    if (typeof last === 'string') return last
    return '（暂无消息内容）'
  }

  return (
    <div>
      {notReady && (
        <Notice kind="warn" onRetry={() => { accounts.reload(); conversations.reload(); risk.reload() }}>
          部分后端接口还没就绪（并行开发中），下面显示为 0 或「未就绪」属正常现象。
        </Notice>
      )}

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat
          label="闲鱼账号"
          value={accounts.loading ? '…' : stats.total}
          extra={
            <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>
              在线 {stats.online}
              {stats.needRelogin > 0 && <span style={{ color: 'var(--danger)' }}> · 需重登 {stats.needRelogin}</span>}
            </div>
          }
        />
        <Stat
          label="今日活跃会话"
          value={conversations.loading ? '…' : stats.todayMessages}
          extra={<div className="dim" style={{ fontSize: 12, marginTop: 2 }}>按最后消息时间统计</div>}
        />
        <Stat
          label="未读 / 待处理"
          value={conversations.loading ? '…' : stats.unread}
          tone={stats.unread > 0 ? 'var(--warn)' : undefined}
          extra={<div className="dim" style={{ fontSize: 12, marginTop: 2 }}>人工接管中 {stats.manual}</div>}
        />
        <Stat
          label="风控状态"
          value={blockedCount > 0 ? `${blockedCount} 个熔断` : '正常'}
          tone={blockedCount > 0 ? 'var(--danger)' : 'var(--ok)'}
          small
          extra={<div className="dim" style={{ fontSize: 12, marginTop: 2 }}>共 {riskStates.length} 个账号有记录</div>}
        />
      </div>

      <div className="split">
        <div className="left">
          <div className="card">
            <h3>
              账号状态
              <span className="hint">点击进入账号页管理</span>
              <span style={{ flex: 1 }} />
              <button className="btn sm" onClick={accounts.reload}>
                刷新
              </button>
            </h3>
            <AsyncBox
              loading={accounts.loading}
              error={accounts.error}
              notReady={accounts.notReady}
              empty={accountList.length === 0}
              emptyIcon="👤"
              emptyTitle="还没有绑定闲鱼账号"
              emptyDesc="去「账号」页扫码登录一个账号，商品和会话才会有数据。"
              onRetry={accounts.reload}
            >
              <div className="col">
                {accountList.map((account) => {
                  const login = loginStateLabel(account.login_state)
                  const runtime = runtimeStateLabel(account.runtime_state)
                  return (
                    <div key={account.id} className="row wrap" style={{ justifyContent: 'space-between' }}>
                      <div className="row">
                        <strong>{account.nickname || account.remark || account.id}</strong>
                        <Badge tone={login.tone}>{login.text}</Badge>
                        <Badge tone={runtime.tone}>{runtime.text}</Badge>
                        {!bool(account.enabled) && <Badge tone="muted">已停用</Badge>}
                      </div>
                      <button className="btn sm ghost" onClick={() => onGoto('accounts')}>
                        管理
                      </button>
                    </div>
                  )
                })}
              </div>
            </AsyncBox>
          </div>
        </div>

        <div className="right">
          <div className="card">
            <h3>
              风控状态
              <span style={{ flex: 1 }} />
              <button className="btn sm" onClick={risk.reload}>
                刷新
              </button>
            </h3>
            <AsyncBox
              loading={risk.loading}
              error={risk.error}
              notReady={risk.notReady}
              empty={riskStates.length === 0}
              emptyIcon="🛡"
              emptyTitle="暂无风控记录"
              emptyDesc="没有触发风控事件，账号运行正常。"
              onRetry={risk.reload}
            >
              <div className="col">
                {riskStates.map((item, index) => {
                  const isBlocked = blockedIds.includes(item.account_id || '')
                  return (
                    <div key={String(item.account_id ?? index)} className="row wrap" style={{ justifyContent: 'space-between' }}>
                      <span className="mono">{item.account_id || '全局'}</span>
                      <div className="row">
                        <Badge tone={isBlocked ? 'danger' : 'ok'}>{isBlocked ? '熔断中' : '正常'}</Badge>
                        <span className="dim" style={{ fontSize: 12 }}>
                          连续命中 {Number(item.consecutive_hits) || 0} 次
                        </span>
                      </div>
                    </div>
                  )
                })}
              </div>
              {blockedCount > 0 && (
                <div style={{ marginTop: 10 }}>
                  <button className="btn sm" onClick={() => onGoto('risk')}>
                    去风控页处理
                  </button>
                </div>
              )}
            </AsyncBox>
          </div>

          <div className="card">
            <h3>
              最近会话
              <span style={{ flex: 1 }} />
              <button className="btn sm" onClick={conversations.reload}>
                刷新
              </button>
            </h3>
            <AsyncBox
              loading={conversations.loading}
              error={conversations.error}
              notReady={conversations.notReady}
              empty={conversationList.length === 0}
              emptyIcon="💬"
              emptyTitle="还没有会话"
              emptyDesc="账号在线并收到买家消息后，会话会出现在这里。"
              onRetry={conversations.reload}
            >
              <div className="col">
                {conversationList.slice(0, 6).map((conversation) => (
                  <div key={conversation.id} className="row wrap" style={{ justifyContent: 'space-between' }}>
                    <div className="col" style={{ gap: 0, minWidth: 0 }}>
                      <strong className="ellipsis">{conversation.peer_nickname || conversation.peer_user_id || '买家'}</strong>
                      <span className="dim ellipsis" style={{ fontSize: 12 }}>
                        {lastMessageText(conversation)}
                      </span>
                    </div>
                    <span className="dim" style={{ fontSize: 11.5 }}>
                      {formatTime(conversation.last_message_at)}
                    </span>
                  </div>
                ))}
                <button className="btn sm ghost" onClick={() => onGoto('chats')}>
                  打开会话页
                </button>
              </div>
            </AsyncBox>
          </div>
        </div>
      </div>
    </div>
  )
}
