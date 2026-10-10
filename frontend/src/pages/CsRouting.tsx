/**
 * 小铺 → 客服分流（**只读**）。
 *
 * 契约见 `docs/contracts/shop.md`（task-51 冻结）：
 *  - `GET /api/shop/{account_id}/cs-routing?page=&size=`（page/size 只作用于客服分组）
 *
 * 响应三块：
 *  - `routing`：分流规则。真机只有 `service_time: {start, end}`；`offline_message` /
 *    `service_range` 为 `null` + 中文 warning。**整个 `routingRules` 没返回时是 `null`**，
 *    不返回 `{}` 冒充。
 *  - `groups` / `groups_total`：客服分组。真机 `[]` / `0`（平台真值，不是「没权限」）。
 *  - `agents`：客服列表。键不存在 → `null`（**不是 `[]`**）——「缺键 ≠ 没有客服」。
 *
 * 两条口径：
 *  1. **`null` 显示 `-` 或「无权限」，绝不补 0 / 空数组**。0 是真实值。
 *  2. **无权限 ≠ 掉线**：不会把账号打成 `need_relogin`，别提示用户重新扫码。
 *  3. **本页无写操作**：改分流规则 / 客服分组请去官方工作台。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconInfo, IconRefresh, IconSend, IconUser } from '../icons'
import type { Account } from '../types'
import './shop.css'

const PAGE_SIZE = 20

interface ServiceTime {
  start?: unknown
  end?: unknown
}

interface Routing {
  raw?: Record<string, unknown> | null
  service_time?: ServiceTime | null
  offline_message?: unknown
  service_range?: unknown
}

/** 客服分组一行：形状未冻结，原样渲染常见键，其余塞进「其它字段」。 */
type GroupRow = Record<string, unknown>

/** 客服列表一行：同上。 */
type AgentRow = Record<string, unknown>

interface CsRoutingResult {
  account_id?: string
  routing?: Routing | null
  groups?: GroupRow[] | null
  groups_total?: number | null
  agents?: AgentRow[] | null
  warnings?: string[]
  fetched_at?: string
  source?: string
}

function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const str = String(value).trim()
  return str === '' ? '-' : str
}

function isMissing(value: unknown): boolean {
  return value === null || value === undefined
}

function RefreshButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button className="btn sm" onClick={onClick} disabled={disabled}>
      <span className="sh-ico">
        <IconRefresh size={13} />
      </span>
      刷新
    </button>
  )
}

/* ------------------------------------------------------------------ 分流规则 */

function RoutingPanel({ routing }: { routing: Routing | null | undefined }) {
  if (isMissing(routing)) {
    return (
      <Notice kind="warn">
        平台这次<strong>没有返回</strong>分流规则（整个 <code>routingRules</code> 缺失）。这不代表「没配分流」——
        可能是账号对客服分流业务没有权限。
      </Notice>
    )
  }

  const st = routing?.service_time
  const rows: Array<{ k: string; v: string }> = [
    { k: '服务时间', v: st ? `${text(st.start)} ~ ${text(st.end)}` : '-' },
    { k: '离线消息', v: text(routing?.offline_message) },
    { k: '服务范围', v: text(routing?.service_range) },
  ]

  return (
    <div className="sh-rules">
      {rows.map((row) => (
        <div className="sh-rule" key={row.k}>
          <span className="sh-rule-k">{row.k}</span>
          <span className="sh-rule-v">{row.v}</span>
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ 分组 / 客服 */

const GROUP_COLUMNS: Array<{ key: string; label: string }> = [
  { key: 'groupName', label: '分组名' },
  { key: 'groupId', label: '分组 ID' },
  { key: 'customerCount', label: '客服数' },
]

const AGENT_COLUMNS: Array<{ key: string; label: string }> = [
  { key: 'customerName', label: '客服昵称' },
  { key: 'customerId', label: '客服 ID' },
  { key: 'roleName', label: '角色' },
  { key: 'status', label: '状态' },
]

function extraKeys(rows: Array<Record<string, unknown>>, known: string[]): string[] {
  const set = new Set(known)
  const seen = new Set<string>()
  for (const row of rows) {
    for (const key of Object.keys(row || {})) {
      if (!set.has(key)) seen.add(key)
    }
  }
  return [...seen].sort()
}

function RowTable({
  rows,
  columns,
  emptyTitle,
  emptyDesc,
  missingHint,
}: {
  rows: Array<Record<string, unknown>> | null | undefined
  columns: Array<{ key: string; label: string }>
  emptyTitle: string
  emptyDesc: string
  missingHint: string
}) {
  if (isMissing(rows)) {
    return (
      <Notice kind="warn">
        {missingHint}（缺键 —— <strong>不等于</strong>「一个都没有」，可能是没权限）。
      </Notice>
    )
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="sh-empty">
        <span className="sh-empty-ico">
          <IconUser size={24} />
        </span>
        <div className="sh-empty-title">{emptyTitle}</div>
        <div className="sh-empty-desc">{emptyDesc}</div>
      </div>
    )
  }
  const extra = extraKeys(rows, columns.map((c) => c.key))
  return (
    <div className="table-wrap">
      <table className="sh-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key}>{col.label}</th>
            ))}
            {extra.length > 0 && <th>其它字段</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => (
            <tr key={idx}>
              {columns.map((col) => (
                <td key={col.key}>{text(row?.[col.key])}</td>
              ))}
              {extra.length > 0 && (
                <td className="sh-extra">
                  {(() => {
                    const pairs = extra
                      .filter((k) => row?.[k] !== undefined)
                      .map((k) => `${k}=${text(row?.[k])}`)
                    return pairs.length ? <span title={pairs.join(' · ')}>{pairs.join(' · ')}</span> : '-'
                  })()}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function CsRoutingPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [page, setPage] = useState(1)

  const accountId = picked || accounts.data?.[0]?.id || ''

  const data = useAsync<CsRoutingResult>(
    () => api.get<CsRoutingResult>(`/api/shop/${encodeURIComponent(accountId)}/cs-routing`, { page, size: PAGE_SIZE }),
    [accountId, page],
    Boolean(accountId),
  )

  const warnings = data.data?.warnings || []
  const groupsTotal = data.data?.groups_total

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">客服分流</h2>
          <p className="page-desc">
            分流规则、客服分组与客服列表。只读 —— 改分流规则 / 分组请去官方卖家工作台操作。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="sh-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="sh-bar">
          <div className="sh-field">
            <label className="sh-label" htmlFor="cs-account">
              账号
            </label>
            <select
              id="cs-account"
              value={accountId}
              onChange={(e) => {
                setPicked(e.target.value)
                setPage(1)
              }}
              disabled={accounts.loading}
            >
              {accounts.data && accounts.data.length > 0 ? (
                accounts.data.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.nickname || account.id}
                  </option>
                ))
              ) : (
                <option value="">（还没有账号）</option>
              )}
            </select>
          </div>
          <span className="sh-spacer" />
          <RefreshButton onClick={data.reload} disabled={data.loading || !accountId} />
        </div>
        {accounts.notReady && <Notice kind="warn">账号接口还没就绪，暂时选不了账号。</Notice>}
        {accounts.error && (
          <Notice kind="error" onRetry={accounts.reload}>
            {accounts.error}
          </Notice>
        )}
      </div>

      {!accountId ? (
        <div className="card">
          <Notice kind="info">先在上方选一个账号：客服分流是按账号查的，没有账号就没数据可看。</Notice>
        </div>
      ) : (
        <>
          <div className="card">
            <div className="sh-section-title">
              <span className="sh-ico">
                <IconSend size={15} />
              </span>
              分流规则
            </div>
            <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
              <RoutingPanel routing={data.data?.routing} />
            </AsyncBox>
          </div>

          <div className="card">
            <div className="sh-section-title">
              客服分组
              <span className="sh-count">{isMissing(groupsTotal) ? '共 - 个' : `共 ${count(groupsTotal)} 个`}</span>
            </div>
            <RowTable
              rows={data.data?.groups}
              columns={GROUP_COLUMNS}
              emptyTitle="这个账号下没有客服分组"
              emptyDesc="平台明确返回了空列表。要新建分组请去官方卖家工作台。"
              missingHint="平台这次没有返回客服分组列表"
            />
          </div>

          <div className="card">
            <div className="sh-section-title">客服列表</div>
            <RowTable
              rows={data.data?.agents}
              columns={AGENT_COLUMNS}
              emptyTitle="这个账号下没有客服"
              emptyDesc="平台明确返回了空列表。要添加客服请去官方卖家工作台。"
              missingHint="平台这次没有返回客服列表"
            />

            {warnings.length > 0 && (
              <div className="sh-warnings sh-section">
                {warnings.map((w, idx) => (
                  <Notice key={idx} kind="warn">
                    {w}
                  </Notice>
                ))}
              </div>
            )}

            <div className="sh-note">
              <span className="sh-ico">
                <IconInfo size={14} />
              </span>
              只读页：这里<strong>不会</strong>改分流规则、客服分组或客服账号。数据更新于
              {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
