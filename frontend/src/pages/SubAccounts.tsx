/**
 * 小铺 → 子账号管理（**只读**）。
 *
 * 契约见 `docs/contracts/shop.md`（task-51 冻结）：
 *  - `GET /api/shop/{account_id}/sub-accounts?page=&size=`
 *
 * 两条不能破的口径：
 *  1. **平台没给的字段是 `null`，一律显示 `-`，绝不补 0**。0 是真实值（该账号真机
 *     `total: 0` = 一个子账号都没有），和「平台没告诉我」是两件事。
 *  2. **无权限 ≠ 掉线**：`quota` / `roles` 对该账号是 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`
 *     （账号对这个业务没权限），后端给 `null` + 中文 warning，**不会**把账号打成
 *     `need_relogin`。页面必须把 warning 显示出来，别让用户以为要重新扫码。
 *  3. **本页没有任何写操作**。注册/删除子账号、改手机号/密码/角色在官方工作台做。
 *
 * `items[]` 是平台 `list` 原样透传（不重命名、不补字段）——真机该账号列表为空，
 * 行字段名没有真机样例，所以这里按 bundle 线索渲染常见列、其余塞进「原始字段」抽屉，
 * 宁可让用户看到平台给了什么，也不猜一个假的列名。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Badge, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconInfo, IconRefresh, IconUser } from '../icons'
import type { Account } from '../types'
import './shop.css'

const PAGE_SIZE = 20

/** 平台 `list` 里的一行：形状未冻结，只读几个 bundle 里出现过的键，其余原样保留。 */
type SubRow = Record<string, unknown>

interface SubAccountsResult {
  account_id?: string
  page?: number | null
  page_size?: number | null
  total?: number | null
  total_page?: number | null
  has_next_page?: boolean | null
  items?: SubRow[] | null
  quota?: Record<string, unknown> | null
  roles?: Array<{ roleId?: unknown; roleName?: unknown }> | null
  warnings?: string[]
  fetched_at?: string
  source?: string
}

/** 计数：拿不到就是 `-`。 */
function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

/** 文本：空串与 null 一律 `-`。 */
function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const str = String(value).trim()
  return str === '' ? '-' : str
}

/** 平台某键存在性无法分辨时，给一个可解释的空态而不是空表。 */
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

/**
 * 常见列（来自 bundle 线索 `subId` / `mobilePhone` / `roleId`）——**有就显示，没有显示 `-`**，
 * 不是「平台一定有这些列」的断言。
 */
const KNOWN_COLUMNS: Array<{ key: string; label: string }> = [
  { key: 'subId', label: '子账号 ID' },
  { key: 'mobilePhone', label: '手机号' },
  { key: 'subNick', label: '昵称' },
  { key: 'roleName', label: '角色' },
  { key: 'status', label: '状态' },
]

/** 把一行里**不在已知列**的键收进抽屉，避免平台新字段被悄悄丢掉。 */
function extraKeys(rows: SubRow[]): string[] {
  const known = new Set(KNOWN_COLUMNS.map((c) => c.key))
  const seen = new Set<string>()
  for (const row of rows) {
    for (const key of Object.keys(row || {})) {
      if (!known.has(key)) seen.add(key)
    }
  }
  return [...seen].sort()
}

/* ------------------------------------------------------------------ 页面 */

export function SubAccountsPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [page, setPage] = useState(1)

  /** 选中账号：用户选的优先，否则退回第一个（派生值，不用 useEffect 回写）。 */
  const accountId = picked || accounts.data?.[0]?.id || ''

  const data = useAsync<SubAccountsResult>(
    () => api.get<SubAccountsResult>(`/api/shop/${encodeURIComponent(accountId)}/sub-accounts`, { page, size: PAGE_SIZE }),
    [accountId, page],
    Boolean(accountId),
  )

  const rows = data.data?.items
  const extra = extraKeys(Array.isArray(rows) ? rows : [])
  const total = data.data?.total
  const hasNext = data.data?.has_next_page
  const warnings = data.data?.warnings || []

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">子账号管理</h2>
          <p className="page-desc">
            店铺子账号列表与可分配角色。只读 —— 注册 / 删除 / 改手机号密码请去官方卖家工作台操作。
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
            <label className="sh-label" htmlFor="sh-account">
              账号
            </label>
            <select id="sh-account" value={accountId} onChange={(e) => { setPicked(e.target.value); setPage(1) }} disabled={accounts.loading}>
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
          <Notice kind="info">先在上方选一个账号：子账号是按账号查的，没有账号就没数据可看。</Notice>
        </div>
      ) : (
        <>
          <div className="card">
            <div className="sh-head">
              <span className="sh-head-title">
                子账号
                <span className="sh-count">{isMissing(total) ? '共 - 个' : `共 ${count(total)} 个`}</span>
              </span>
            </div>

            {warnings.length > 0 && (
              <div className="sh-warnings">
                {warnings.map((w, idx) => (
                  <Notice key={idx} kind="warn">
                    {w}
                  </Notice>
                ))}
              </div>
            )}

            <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
              <ListBody items={rows} extra={extra} />
            </AsyncBox>

            {Array.isArray(rows) && rows.length > 0 && (
              <div className="sh-pager">
                <span className="sh-muted">
                  第 {page} 页{!isMissing(total) ? ` · 共 ${count(total)} 条` : ''}
                </span>
                <span className="sh-spacer" />
                <button className="btn sm" onClick={() => setPage((n) => Math.max(1, n - 1))} disabled={page <= 1 || data.loading}>
                  上一页
                </button>
                <button
                  className="btn sm"
                  onClick={() => setPage((n) => n + 1)}
                  disabled={data.loading || hasNext === false || (hasNext !== true && isMissing(total))}
                >
                  下一页
                </button>
              </div>
            )}

            <div className="sh-note">
              <span className="sh-ico">
                <IconInfo size={14} />
              </span>
              只读页：这里<strong>不会</strong>注册、删除子账号，也不会改手机号 / 密码 / 角色。数据更新于
              {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
            </div>
          </div>

          <SidePanel data={data.data} />
        </>
      )}
    </div>
  )
}

/** `null`（缺键）和 `[]`（平台明说没有）是两件事，空态文案必须分开。 */
function ListBody({ items, extra }: { items: SubRow[] | null | undefined; extra: string[] }) {
  if (isMissing(items)) {
    return (
      <div className="sh-empty">
        <Notice kind="warn">
          平台这次<strong>没有返回</strong>子账号列表（缺键）。这<strong>不等于</strong>「一个子账号都没有」——
          可能是账号对子账号业务没有权限，见上方说明。
        </Notice>
      </div>
    )
  }
  if (!items || items.length === 0) {
    return (
      <div className="sh-empty">
        <span className="sh-empty-ico">
          <IconUser size={26} />
        </span>
        <div className="sh-empty-title">这个账号下没有子账号</div>
        <div className="sh-empty-desc">平台明确返回了空列表。要添加子账号请去官方卖家工作台。</div>
      </div>
    )
  }
  return (
    <div className="table-wrap">
      <table className="sh-table">
        <thead>
          <tr>
            {KNOWN_COLUMNS.map((col) => (
              <th key={col.key}>{col.label}</th>
            ))}
            {extra.length > 0 && <th>其它字段</th>}
          </tr>
        </thead>
        <tbody>
          {items.map((row, idx) => (
            <tr key={idx}>
              {KNOWN_COLUMNS.map((col) => (
                <td key={col.key} className={col.key === 'mobilePhone' ? 'sh-mono' : undefined}>
                  {text(row?.[col.key])}
                </td>
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

/** 配额 / 角色：真机无权限 → `null`。此时显示「无权限」而不是 0 或空。 */
function SidePanel({ data }: { data: SubAccountsResult | null }) {
  const quota = data?.quota
  const roles = data?.roles
  return (
    <div className="sh-side">
      <div className="card">
        <div className="sh-side-title">子账号配额</div>
        {isMissing(quota) ? (
          <div className="sh-muted">无权限（平台未授权该账号查看配额）</div>
        ) : (
          <div className="sh-kv">
            {Object.entries(quota || {}).map(([k, v]) => (
              <div className="sh-kv-row" key={k}>
                <span className="sh-kv-k">{k}</span>
                <span className="sh-kv-v">{text(v)}</span>
              </div>
            ))}
            {Object.keys(quota || {}).length === 0 && <div className="sh-muted">平台返回了空配额对象</div>}
          </div>
        )}
      </div>
      <div className="card">
        <div className="sh-side-title">可分配角色</div>
        {isMissing(roles) ? (
          <div className="sh-muted">无权限（平台未授权该账号查看角色列表）</div>
        ) : roles && roles.length > 0 ? (
          <div className="sh-roles">
            {roles.map((role, idx) => (
              <Badge key={idx} tone="info">
                {text(role?.roleName ?? role?.roleId)}
              </Badge>
            ))}
          </div>
        ) : (
          <div className="sh-muted">平台返回了空角色列表</div>
        )}
      </div>
    </div>
  )
}
