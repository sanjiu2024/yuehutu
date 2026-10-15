/**
 * **通知中心**。契约 `docs/contracts/notify.md`。
 *
 * 这页的结构和别的页不一样，先把坑写在最前面：
 *  1. **通知中心没有列表接口** —— 条目由 IM / ACCS 长连接推送，所以 `GET /api/notifications/{account_id}`
 *     的 `items` **恒为 `[]`**。下面不会把它渲染成「你没有通知」，而是明说条目走另一条通道。
 *  2. `unread.total`：平台**明说 0 就照用 0**；键**不存在**是 `null` → 显示 `-`，**绝不补 0**。
 *  3. `channels` 是平台 `notifyInfoList` 原样透传，**字段表没冻结** → 防御式取值。
 *  4. 这个接口目前是**空操作**（只校验 + 回显），`page` / `size` 是合法入参，但翻页不会多出条目。
 */

import { useState } from 'react'
import { api } from '../api'
import { Badge, Notice } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconBell, IconRefresh, IconWifi } from '../icons'
import { formatRelativeTime, formatTime } from '../format'
import { TradeError, isNeedRelogin, isPermissionDenied, money, num, useTradeAccounts, when } from './Orders'
import './notifications.css'

interface NotificationsResult {
  account_id?: string
  channels?: unknown
  unread?: unknown
  items?: unknown
  page?: number
  page_size?: number
  warnings?: string[]
  fetched_at?: string
  source?: string
}

/** `channels` 可能是数组，也可能是 `{ notifyInfoList: [...] }`，都认。 */
function channelList(value: unknown): unknown[] {
  if (Array.isArray(value)) return value
  if (value && typeof value === 'object') {
    const inner = (value as Record<string, unknown>).notifyInfoList
    if (Array.isArray(inner)) return inner
  }
  return []
}

function text(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** 一条通知里挑得出的字段；一个都挑不出来就退回原始 JSON，不替平台猜字段名。 */
function readChannel(item: unknown): { title: string; body: string; time: string; kind: string } {
  if (!item || typeof item !== 'object') {
    return { title: text(item) || '-', body: '', time: '', kind: '' }
  }
  const record = item as Record<string, unknown>
  const title = text(record.title ?? record.msg_title ?? record.subject ?? record.type ?? record.msg_type)
  const body = text(record.content ?? record.body ?? record.desc ?? record.summary ?? record.text)
  const time = text(record.gmt_create ?? record.create_time ?? record.time ?? record.timestamp)
  const kind = text(record.biz_type_name ?? record.biz_type ?? record.category ?? record.channel)
  return { title, body, time, kind }
}

/** `unread` 是对象时取 `.total`，直接是数字时用数字，没有就是 null。 */
function unreadTotal(value: unknown): number | string | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string') return value.trim() === '' ? null : value
  if (typeof value === 'object') {
    const total = (value as Record<string, unknown>).total
    if (typeof total === 'number') return Number.isFinite(total) ? total : null
    if (typeof total === 'string') return total.trim() === '' ? null : total
    return null
  }
  return null
}

export function NotificationsPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const pageSize = 20

  const state = useAsync<NotificationsResult>(
    () => api.get<NotificationsResult>(`/api/notifications/${encodeURIComponent(accountId)}`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)
  const channels = channelList(data?.channels)
  const items = Array.isArray(data?.items) ? data.items : []
  const warnings = Array.isArray(data?.warnings) ? data.warnings : []
  const unread = unreadTotal(data?.unread)

  return (
    <>
      <section className="tr-head nt-head">
        <div>
          <h3><IconBell size={18} /> 通知中心</h3>
          <p>
            平台通知渠道与未读计数 · 只读
            {data?.fetched_at ? ` · 拉取于 ${when(data.fetched_at)}（UTC）` : ''}
            {data?.source ? ` · 来源 ${data.source}` : ''}
          </p>
        </div>
        <button className="btn sm" onClick={state.reload} disabled={state.loading || !accountId}>
          <IconRefresh size={14} /> {state.loading ? '刷新中…' : '刷新'}
        </button>
      </section>

      <section className="card nt-toolbar">
        <label className="nt-field">
          <span>账号</span>
          <select value={picked || accountId} onChange={(event) => { setPicked(event.target.value); setPage(1) }}>
            {!accounts.data?.length && <option value="">（还没有账号）</option>}
            {(accounts.data ?? []).map((account) => (
              <option key={account.id} value={account.id}>
                {account.nickname || account.id}
              </option>
            ))}
          </select>
        </label>
        <span className="nt-readonly"><IconWifi size={12} /> 只读</span>
      </section>

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没通知可看。</Notice>}
      {relogin && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查通知 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}
      {permission && (
        <Notice kind="warn">
          平台不给这个账号开通知权限。这不是掉线，<strong>不用重新扫码</strong>。
        </Notice>
      )}

      {state.loading && !data && <Notice kind="info">正在读取通知渠道…</Notice>}

      {!state.loading && state.error && (
        <TradeError error={state.error} notReady={state.notReady} onRetry={state.reload} />
      )}

      {!state.loading && !state.error && accountId && (
        <>
          <section className="nt-stats">
            <div className="card nt-stat">
              <span>未读通知</span>
              <b className={unread === null ? 'nt-na' : ''}>{unread === null ? '-' : num(unread)}</b>
              {unread === null && <em>平台没回 unread.total 这个键，显示 - 不补 0</em>}
              {unread === 0 && <em>平台明确回了 0，这就是真 0</em>}
            </div>
            <div className="card nt-stat">
              <span>通知渠道</span>
              <b className={channels.length === 0 ? 'nt-na' : ''}>
                {channels.length === 0 ? '-' : num(channels.length)}
              </b>
              <em>
                {channels.length === 0
                  ? '平台没回 notifyInfoList（缺键时是 [] + warning）'
                  : '平台 notifyInfoList 原样透传'}
              </em>
            </div>
            <div className="card nt-stat">
              <span>本接口返回的条目</span>
              <b className="nt-na">{num(items.length)}</b>
              <em>契约写明恒为 []：条目走 IM / ACCS 长连接，不经过这里</em>
            </div>
          </section>

          {warnings.length > 0 && (
            <Notice kind="info">
              平台/后端给出的提示：
              <ul className="nt-warn">
                {warnings.map((warning, index) => <li key={index}>{warning}</li>)}
              </ul>
            </Notice>
          )}

          <section className="card nt-panel">
            <div className="nt-panel-head">
              <h4>通知渠道</h4>
              <span className="nt-note">
                平台原样透传，字段表没冻结 —— 只展示拿得到的字段，拿不到显示 -
              </span>
            </div>

            {channels.length === 0 ? (
              <div className="nt-empty">
                <p className="nt-empty-title">平台没有返回任何通知渠道</p>
                <p className="nt-empty-desc">
                  这不是「系统很安静」，而是平台这一次没给 <code>notifyInfoList</code>。
                  缺键时后端会回 <code>[]</code> 并附一条中文 warning，上面的提示区会写明原因。
                </p>
              </div>
            ) : (
              <div className="nt-channels">
                {channels.map((item, index) => {
                  const channel = readChannel(item)
                  return (
                    <div className="nt-channel" key={index}>
                      <div className="nt-channel-head">
                        <strong title={channel.title}>{channel.title || '-'}</strong>
                        {channel.kind && <Badge tone="muted">{channel.kind}</Badge>}
                        <span className="nt-time" title={channel.time ? formatTime(when(channel.time)) : ''}>{channel.time ? formatRelativeTime(when(channel.time)) : ''}</span>
                      </div>
                      {channel.body && <p className="nt-body">{channel.body}</p>}
                      {!channel.body && !channel.title && <pre className="tr-raw">{text(item) || '-'}</pre>}
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          <section className="card nt-panel">
            <div className="nt-panel-head">
              <h4>通知条目</h4>
              <span className="nt-note">本接口不提供条目列表</span>
            </div>
            <div className="nt-empty">
              <p className="nt-empty-title">条目经 IM / ACCS 长连接推送，不走这个接口</p>
              <p className="nt-empty-desc">
                契约写明 <code>items</code> 恒为 <code>[]</code>。这里不会把它渲染成「你没有通知」——
                那会让真在等通知的人以为功能坏了。
              </p>
              <div className="nt-pager">
                <span className="nt-note">
                  第 {page} 页 · 每页 {pageSize} 条 · 本接口返回 {items.length} 条 ·
                  总数 -（契约没有给 total 字段，不拿 page_size 冒充）
                </span>
                <button className="btn sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
                <button
                  className="btn sm"
                  disabled
                  title="接口不返回下一页游标（items 恒为空），翻页不会多出条目"
                  onClick={() => setPage(page + 1)}
                >
                  下一页
                </button>
              </div>
            </div>
          </section>

          {data && !data.account_id && <Notice kind="info">后端没有回 account_id，这个账号可能没绑定成功。</Notice>}
          {data?.account_id && data.account_id !== accountId && (
            <Notice kind="warn">
              后端回的 account_id（{money(data.account_id)}）和当前选择（{money(accountId)}）不一致，请刷新确认。
            </Notice>
          )}
        </>
      )}
    </>
  )
}
