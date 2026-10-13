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

import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Modal, Notice, useToast } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconInfo, IconPlus, IconRefresh, IconUser } from '../icons'
import type { Account } from '../types'

import './shop.css'
import './subaccounts.css'

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

/* ------------------------------------------------------- 写操作（第三十二批） */

interface RolesResult {
  account_id?: string
  roles?: Array<{ role_id?: unknown; role_name?: unknown; selected?: unknown }> | null
  total?: number | null
  warnings?: string[]
  fetched_at?: string
}

type WriteMode = 'create' | 'delete' | 'mobile' | 'roles' | 'password'

const WRITE_MODES: Array<{ value: WriteMode; label: string }> = [
  { value: 'create', label: '新建子账号' },
  { value: 'delete', label: '删除子账号' },
  { value: 'mobile', label: '改手机号' },
  { value: 'roles', label: '改岗位' },
  { value: 'password', label: '改密码' },
]

/** 官方 bundle 原文的正则（前端先挡一次，后端还会再挡）。 */
const MOBILE_RE = /^1[3-9]\d{9}$/
const PASSWORD_RE = /^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,15}$/

/** 确认框里密码只回显前 2 位。 */
function maskPassword(value: string): string {
  return `${value.slice(0, 2)}${'*'.repeat(Math.max(0, value.length - 2))}`
}

/** 唯一写出口：`confirm: true` 写在 `...body` 之后，调用方覆盖不了。 */
function postSubWrite<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return api.post<T>(path, { ...body, confirm: true })
}

/**
 * 子账号写操作面板。五个动作共用一个出口，都先弹 `ConfirmDialog danger`。
 *
 * 密码**明文发给本机后端**（加密在服务端做，见 `backend/app/services/sub_accounts.py`）：
 * 前端不碰公钥，也不在日志/确认框里回显完整密码。
 */
export function SubAccountWritePanel({
  accountId,
  onClose,
  onChanged,
}: {
  accountId: string
  onClose: () => void
  onChanged: () => void
}) {
  const toast = useToast()
  const [mode, setMode] = useState<WriteMode>('create')
  const [subId, setSubId] = useState('')
  const [nick, setNick] = useState('')
  const [mobile, setMobile] = useState('')
  const [customName, setCustomName] = useState('')
  const [password, setPassword] = useState('')
  const [roleIds, setRoleIds] = useState<number[]>([])
  const [problem, setProblem] = useState('')
  const [ask, setAsk] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const roles = useAsync<RolesResult>(
    () => api.get<RolesResult>(`/api/sub-accounts/${encodeURIComponent(accountId)}/roles`),
    [accountId],
    Boolean(accountId),
    ['shop.changed'],
  )

  const options = (roles.data?.roles || [])
    .map((row) => ({ id: Number(row.role_id), name: String(row.role_name ?? row.role_id ?? '') }))
    .filter((row) => Number.isFinite(row.id))

  function base(): string {
    return `/api/sub-accounts/${encodeURIComponent(accountId)}`
  }

  function path(): string {
    if (mode === 'create') return base()
    const id = encodeURIComponent(subId.trim())
    return `${base()}/${id}/${mode === 'mobile' ? 'mobile' : mode === 'roles' ? 'roles' : mode === 'password' ? 'password' : 'delete'}`
  }

  function body(): Record<string, unknown> {
    if (mode === 'create') {
      const payload: Record<string, unknown> = {
        nick: nick.trim(),
        mobile: mobile.trim(),
        role_ids: roleIds,
        password,
      }
      if (customName.trim()) payload.custom_name = customName.trim()
      return payload
    }
    if (mode === 'delete') return {}
    if (mode === 'mobile') return { mobile: mobile.trim() }
    if (mode === 'roles') return { role_ids: roleIds }
    return { password }
  }

  function validate(): string {
    if (mode !== 'create' && !subId.trim()) return '先填子账号 ID（可以从上面列表里复制）。'
    if (mode === 'create' || mode === 'mobile') {
      if (!MOBILE_RE.test(mobile.trim())) return '手机号要 11 位、以 1 开头（官方原文的正则）。'
    }
    if (mode === 'create') {
      if (!nick.trim()) return '昵称不能为空。'
    }
    if (mode === 'create' || mode === 'roles') {
      if (roleIds.length === 0) return '至少要选一个岗位。'
    }
    if (mode === 'create' || mode === 'password') {
      if (!PASSWORD_RE.test(password)) return '密码必须是字母和数字组合、长度 8~15 位（官方原文的正则）。'
    }
    return ''
  }

  async function submit() {
    setBusy(true)
    setError('')
    try {
      await postSubWrite<Record<string, unknown>>(path(), body())
      toast.success(`${WRITE_MODES.find((m) => m.value === mode)?.label || '操作'}成功`)
      setAsk(false)
      onChanged()
      onClose()
    } catch (err) {
      // 后端 message 原样显示，弹窗不关（方便复制）
      setError(errorText(err))
      setAsk(false)
    } finally {
      setBusy(false)
    }
  }

  const facts = Object.entries(body()).map(([key, value]) => {
    if (key === 'password') return { key, value: maskPassword(String(value)) }
    if (Array.isArray(value)) return { key, value: value.join(', ') }
    return { key, value: String(value) }
  })

  return (
    <>
      <Modal title="子账号写操作" onClose={onClose}>
        <div className="sa-form">
          <div className="sa-modes">
            {WRITE_MODES.map((item) => (
              <button
                key={item.value}
                className={`btn sm${mode === item.value ? ' primary' : ''}`}
                onClick={() => {
                  setMode(item.value)
                  setProblem('')
                  setError('')
                }}
                disabled={busy}
              >
                {item.label}
              </button>
            ))}
          </div>

          <Notice kind="warn">
            这里<strong>会真的</strong>改动闲鱼上的子账号，不可撤销。每个动作都会先弹一次确认，确认前不会向闲鱼发任何请求。
          </Notice>

          {mode !== 'create' && (
            <label className="sa-field">
              <span className="sh-label">子账号 ID（必填）</span>
              <input
                className="sa-input"
                value={subId}
                placeholder="从上面列表的「子账号 ID」列复制"
                onChange={(e) => {
                  setSubId(e.target.value)
                  setProblem('')
                }}
              />
            </label>
          )}

          {mode === 'create' && (
            <>
              <label className="sa-field">
                <span className="sh-label">昵称（必填）</span>
                <input
                  className="sa-input"
                  value={nick}
                  placeholder="子账号登录名"
                  onChange={(e) => {
                    setNick(e.target.value)
                    setProblem('')
                  }}
                />
              </label>
              <label className="sa-field">
                <span className="sh-label">备注名（可留空）</span>
                <input
                  className="sa-input"
                  value={customName}
                  placeholder="给自己看的备注"
                  onChange={(e) => setCustomName(e.target.value)}
                />
              </label>
            </>
          )}

          {(mode === 'create' || mode === 'mobile') && (
            <label className="sa-field">
              <span className="sh-label">手机号（必填）</span>
              <input
                className="sa-input"
                value={mobile}
                placeholder="11 位手机号"
                onChange={(e) => {
                  setMobile(e.target.value)
                  setProblem('')
                }}
              />
            </label>
          )}

          {(mode === 'create' || mode === 'password') && (
            <label className="sa-field">
              <span className="sh-label">密码（必填）</span>
              <input
                className="sa-input"
                type="password"
                value={password}
                placeholder="字母 + 数字，8~15 位"
                onChange={(e) => {
                  setPassword(e.target.value)
                  setProblem('')
                }}
              />
              <span className="sh-muted">
                密码只发给本机后端，由后端用平台公钥加密后提交；日志与响应里不会出现明文。
              </span>
            </label>
          )}

          {(mode === 'create' || mode === 'roles') && (
            <div className="sa-field">
              <span className="sh-label">岗位（必填，可多选）</span>
              {roles.notReady || roles.error ? (
                <Notice kind="warn">岗位列表没拿到（{roles.error || '接口未就绪'}），先去上面刷新一次再试。</Notice>
              ) : options.length === 0 ? (
                <Notice kind="warn">
                  平台这次没有返回任何岗位 —— 这<strong>不等于</strong>「一个岗位都没有」，可能只是没有权限。先去官方卖家工作台确认岗位配置。
                </Notice>
              ) : (
                <div className="sa-roles">
                  {options.map((option) => (
                    <label key={option.id} className="check">
                      <input
                        type="checkbox"
                        checked={roleIds.includes(option.id)}
                        onChange={(e) => {
                          setRoleIds((prev) =>
                            e.target.checked ? [...prev, option.id] : prev.filter((id) => id !== option.id),
                          )
                          setProblem('')
                        }}
                      />
                      {option.name}
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}

          {problem && <Notice kind="warn">{problem}</Notice>}
          {error && <Notice kind="error">{error}</Notice>}

          <div className="sa-actions">
            <button className="btn sm" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button
              className="btn primary sm"
              disabled={busy}
              onClick={() => {
                const bad = validate()
                if (bad) {
                  setProblem(bad)
                  return
                }
                setProblem('')
                setAsk(true)
              }}
            >
              下一步：确认
            </button>
          </div>
        </div>
      </Modal>

      {ask && (
        <ConfirmDialog
          danger
          busy={busy}
          title={`确认${WRITE_MODES.find((m) => m.value === mode)?.label || '操作'}`}
          confirmText="确认提交到闲鱼"
          message={
            <>
              这一条会<strong>真的</strong>改动闲鱼上的子账号，不可撤销。
              <br />
              账号：{accountId}
              <br />
              接口：<code>POST {path()}</code>
              <br />
              本次提交：
              {facts.length === 0 ? <span className="sa-fact">（无额外字段）</span> : null}
              {facts.map((fact) => (
                <span key={fact.key} className="sa-fact">
                  <code>{fact.key}</code> = {fact.value}
                </span>
              ))}
            </>
          }
          onConfirm={submit}
          onCancel={() => setAsk(false)}
        />
      )}
    </>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function SubAccountsPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [page, setPage] = useState(1)
  const [writeOpen, setWriteOpen] = useState(false)

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
            店铺子账号列表与可分配角色。表格只读；注册 / 删除 / 改手机号 / 改岗位 / 改密码走右上角「子账号写操作」，每个动作都要二次确认。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="sh-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
          <button className="btn primary sm" onClick={() => setWriteOpen(true)} disabled={!accountId}>
            <span className="sh-ico">
              <IconPlus size={13} />
            </span>
            子账号写操作
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
              表格本身只读（不会在这里改数据）；注册 / 删除 / 改手机号密码岗位都在「子账号写操作」里，每个动作都要二次确认。数据更新于
              {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
            </div>
          </div>

          <SidePanel data={data.data} />
        </>
      )}

      {writeOpen && accountId && (
        <SubAccountWritePanel accountId={accountId} onClose={() => setWriteOpen(false)} onChanged={data.reload} />
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
