/**
 * 资质页：超级擦亮的资质管理（5 个只读接口 + 2 个**写**接口）。
 *
 * 后端契约（`backend/app/api/qualification.py`）：
 *  - `GET  /api/qualification/{account_id}/detail`
 *  - `GET  /api/qualification/{account_id}/submitted-records?page=&size=`
 *  - `GET  /api/qualification/{account_id}/categories`
 *  - `GET  /api/qualification/{account_id}/templates`
 *  - `GET  /api/qualification/{account_id}/reusable`
 *  - `POST /api/qualification/{account_id}/packages/submit`  body `{"confirm": true, "request": {...}}`
 *  - `POST /api/qualification/{account_id}/oss-credential`    body `{"confirm": true, "request": {...}}`
 *
 * 三条不能破的口径：
 *  1. **读**：平台没返回的一律显示 `-`，绝不用 0 顶替；「缺键 / null」与「明确空数组」是两种状态，
 *     文案分开（见 `DetailBody` / `ListPanel`）。无权限时说人话，不说「0 条」。
 *  2. **写**：两个写接口的 `request` **字段名没有 bundle 证据**（服务层原样透传），所以这里
 *     **不编字段名做表单** —— 只收「原始 JSON 文本」，并强制「勾选确认 + 二次确认弹窗」才提交。
 *     写操作默认收起，展开也不会误触。
 *  3. 平台新字段不许悄悄丢：列表列按真实返回的键动态生成，超出的收进「其它字段」。
 *
 * 不轮询：刷新一律 `useAsync().reload()`；`useAsync` 第 4 参订阅 WS topic 做**静默**重拉。
 */

import { useMemo, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Field, Notice, Pager, useToast } from '../components'
import { formatTime } from '../format'
import { useAsync, type AsyncState } from '../hooks/useAsync'
import { IconRefresh, IconShield, IconUpload } from '../icons'
import type { Account } from '../types'
import './qualification.css'

const PAGE_SIZE = 20

/** 后端返回的 `data` 形状没有冻结：键名不猜，取不到就 `-`。 */
type QlResult = Record<string, unknown>
type Row = Record<string, unknown>

type TabKey = 'detail' | 'submitted' | 'categories' | 'templates' | 'reusable'

const TABS: Array<{ key: TabKey; label: string; tip: string }> = [
  { key: 'detail', label: '资质详情', tip: '账号当前的资质状态与平台返回的全部字段（只读）' },
  { key: 'submitted', label: '已提交记录', tip: '历史提交记录（只读）' },
  { key: 'categories', label: '可申请类目', tip: '平台允许申请资质的类目（只读）' },
  { key: 'templates', label: '资质模板', tip: '平台提供的资质模板（只读）' },
  { key: 'reusable', label: '可复用材料', tip: '可复用的资质材料（只读）' },
]

const LIST_LABEL: Record<Exclude<TabKey, 'detail'>, string> = {
  submitted: '已提交记录',
  categories: '可申请类目',
  templates: '资质模板',
  reusable: '可复用材料',
}

/**
 * 列表键的候选名：后端 `data` 里到底叫 `records` 还是 `items` 没有冻结，
 * 所以按候选顺序找**第一个真数组**；一个都没有就老实说「缺键」，而不是显示空表。
 */
const LIST_KEYS: Record<Exclude<TabKey, 'detail'>, string[]> = {
  submitted: ['records', 'submitted_records', 'items', 'list'],
  categories: ['categories', 'items', 'list'],
  templates: ['templates', 'items', 'list'],
  reusable: ['reusable', 'items', 'list'],
}

/* ------------------------------------------------------------------ 取值口径 */

/** 计数：拿不到就是 `-`（不是 0）。 */
function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

/** 文本：空串与 null 一律 `-`，不留空白单元格。 */
function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  const str = String(value).trim()
  return str === '' ? '-' : str
}

function isMissing(value: unknown): boolean {
  return value === null || value === undefined
}

/** 页头「更新于」：只有真拿到 `fetched_at` 才写时间。 */
function fetchedNote(data: QlResult | null): string {
  const value = data?.fetched_at
  return typeof value === 'string' && value ? ` · 更新于 ${formatTime(value)}` : ''
}

function pick(data: QlResult | null, keys: string[]): unknown {
  for (const key of keys) {
    const value = data?.[key]
    if (value !== undefined && value !== null) return value
  }
  return null
}

function pickWarnings(data: QlResult | null): string[] {
  const raw = data?.warnings
  return Array.isArray(raw) ? raw.map((item) => String(item)) : []
}

/** 把 `data` 里第一个真数组挑出来；挑不到返回 `null`（= 缺键，不是空）。 */
function firstArray(data: QlResult | null, keys: string[]): { key: string; rows: Row[] } | null {
  if (!data) return null
  for (const key of keys) {
    const value = data[key]
    if (Array.isArray(value)) return { key, rows: value as Row[] }
  }
  return null
}

/** 列表列：按真实返回的键动态生成（前 8 个单独成列，其余进「其它字段」）。 */
function columnsOf(rows: Row[], max = 8): { columns: Array<{ key: string; label: string }>; extra: string[] } {
  const seen = new Set<string>()
  for (const row of rows) {
    for (const key of Object.keys(row || {})) seen.add(key)
  }
  const all = [...seen].sort()
  return { columns: all.slice(0, max).map((key) => ({ key, label: key })), extra: all.slice(max) }
}

/** 把任意嵌套结构摊平成「字段 / 值」两列，不做字段名猜测。 */
function flatten(value: unknown, prefix = ''): Array<{ k: string; v: string }> {
  if (value === null || value === undefined) return [{ k: prefix || '（空）', v: '-' }]
  if (typeof value !== 'object') return [{ k: prefix || '（值）', v: text(value) }]
  if (Array.isArray(value)) {
    if (value.length === 0) return [{ k: prefix || '（数组）', v: '（平台返回了空数组）' }]
    return value.flatMap((item, index) => flatten(item, `${prefix}[${index}]`))
  }
  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) return [{ k: prefix || '（对象）', v: '（平台返回了空对象）' }]
  return entries.flatMap(([key, item]) => flatten(item, prefix ? `${prefix}.${key}` : key))
}

/* ------------------------------------------------------------------ 小组件 */

function RefreshButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button className="btn sm" onClick={onClick} disabled={disabled}>
      <span className="ql-ico">
        <IconRefresh size={13} />
      </span>
      刷新
    </button>
  )
}

/** 平台提示：默认只露 2 条，其余折叠。 */
function WarningList({ warnings }: { warnings: string[] }) {
  const [open, setOpen] = useState(false)
  if (warnings.length === 0) return null
  const shown = open ? warnings : warnings.slice(0, 2)
  return (
    <Notice kind="warn">
      <div>平台返回了 {warnings.length} 条提示：</div>
      <ul className="ql-warn-list">
        {shown.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
      {warnings.length > 2 && (
        <button className="btn sm ghost" onClick={() => setOpen((value) => !value)}>
          {open ? '收起' : `展开其余 ${warnings.length - 2} 条`}
        </button>
      )}
    </Notice>
  )
}

/* ------------------------------------------------------------------ 只读区 */

/** 资质详情：形状未冻结 ⇒ 按平台返回的键逐条列出；`null` 时说人话（多半是没权限）。 */
function DetailBody({ data }: { data: QlResult | null }) {
  if (data === null) {
    return (
      <div className="ql-empty">
        <Notice kind="warn">
          平台<strong>没有返回</strong>资质详情。这<strong>不等于</strong>「这个账号没有资质」—— 更可能是
          该账号在官方工作台里看不到「资质管理」，也就是没有这块权限。请先用官方账号确认一次。
        </Notice>
      </div>
    )
  }

  const rows = flatten(data, '').filter((item) => item.k !== 'warnings')

  if (rows.length === 0) {
    return (
      <div className="ql-empty">
        <span className="ql-empty-title">平台返回了一个空对象</span>
        <span className="ql-empty-desc">既没有字段也没有报错，说明官方接口这次什么都没给。</span>
      </div>
    )
  }

  return (
    <>
      <Notice kind="info">
        平台返回的字段名没有冻结，所以这里<strong>按原样逐字段列出</strong>（不做字段名猜测）。拿不到的显示 - ，不用 0 顶替。
      </Notice>
      <div className="table-wrap">
        <table className="ql-table">
          <thead>
            <tr>
              <th>字段</th>
              <th>值</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                <td className="ql-key">{row.k}</td>
                <td>{row.v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

/** 列表三态：`null`（缺键）说明为什么可能看不到；`[]` 是平台明说没有。 */
function ListBody({
  rows,
  columns,
  extra,
  label,
  dataKeys,
}: {
  rows: Row[]
  columns: Array<{ key: string; label: string }>
  extra: string[]
  label: string
  dataKeys: string[]
}) {
  if (rows.length === 0) {
    return (
      <div className="ql-empty">
        <span className="ql-empty-title">平台明确返回了空列表</span>
        <span className="ql-empty-desc">这个账号当前没有{label}。要申请或修改资质请去官方卖家工作台操作。</span>
      </div>
    )
  }

  if (columns.length === 0) {
    return (
      <Notice kind="warn">
        平台返回了 {rows.length} 条{label}，但每一条都没有可单独成列的字段。第一条原始内容：
        <code className="ql-raw">{JSON.stringify(rows[0])}</code>
      </Notice>
    )
  }

  return (
    <div className="table-wrap">
      <table className="ql-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key}>{col.label}</th>
            ))}
            {extra.length > 0 && <th>其它字段</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((col) => (
                <td key={col.key}>{text(row?.[col.key])}</td>
              ))}
              {extra.length > 0 && (
                <td className="ql-extra">
                  {(() => {
                    const pairs = extra
                      .filter((key) => row?.[key] !== undefined)
                      .map((key) => `${key}=${text(row?.[key])}`)
                    return pairs.length > 0 ? <span title={pairs.join(' · ')}>{pairs.join(' · ')}</span> : '-'
                  })()}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="dim ql-keys-note">
        后端 `data` 里这次实际给的键：{dataKeys.length > 0 ? dataKeys.join('、') : '（一个都没有）'}
      </p>
    </div>
  )
}

function ListPanel({
  kind,
  state,
  page = 1,
  onPage,
  showPager = false,
}: {
  kind: Exclude<TabKey, 'detail'>
  state: AsyncState<QlResult>
  page?: number
  onPage?: (next: number) => void
  showPager?: boolean
}) {
  const data = state.data
  const label = LIST_LABEL[kind]
  const found = firstArray(data, LIST_KEYS[kind])
  const rows = found?.rows || []
  const { columns, extra } = columnsOf(rows)
  const warnings = pickWarnings(data)
  const dataKeys = data ? Object.keys(data).sort() : []
  const sizeRaw = Number(pick(data, ['page_size', 'size']) ?? PAGE_SIZE)
  const size = Number.isFinite(sizeRaw) && sizeRaw > 0 ? sizeRaw : PAGE_SIZE
  const hasNextRaw = pick(data, ['has_next', 'has_next_page'])
  const hasNext = typeof hasNextRaw === 'boolean' ? hasNextRaw : rows.length >= size

  return (
    <div className="card">
      <h3>
        {label}
        <span className="hint">
          {data?.source ? `来源 ${String(data.source)}` : '按账号查'}
          {fetchedNote(data)}
        </span>
      </h3>

      <div className="ql-bar">
        <span className="dim">
          本页 {count(found ? rows.length : null)} 条 · 共 {count(pick(data, ['total', 'total_count']))} 条
        </span>
        <span className="ql-spacer" />
        <RefreshButton onClick={state.reload} disabled={state.loading} />
      </div>

      <WarningList warnings={warnings} />

      <AsyncBox loading={state.loading} error={state.error} notReady={state.notReady} onRetry={state.reload} empty={!isMissing(data) && found !== null && rows.length === 0} emptyTitle="平台返回了 0 条" emptyDesc="这次是真的没有数据（不是没拿到字段），换个账号或稍后刷新看看。">
        {isMissing(data) ? (
          <div className="ql-empty">
            <Notice kind="warn">
              平台<strong>没有返回</strong>「{label}」数据。这<strong>不等于</strong>「一条都没有」—— 可能是
              该账号没有资质管理权限，或者官方接口这次没带这个字段。
            </Notice>
          </div>
        ) : found === null ? (
          <div className="ql-empty">
            <Notice kind="warn">
              平台返回里<strong>找不到</strong>「{label}」的列表键（试过：{LIST_KEYS[kind].join('、')}）。
              后端 `data` 这次实际给的键是：{dataKeys.length > 0 ? dataKeys.join('、') : '（一个都没有）'}。
              这不是「0 条」，是这块数据没拿到。
            </Notice>
          </div>
        ) : (
          <ListBody rows={rows} columns={columns} extra={extra} label={label} dataKeys={dataKeys} />
        )}

        {showPager && found !== null && (
          <Pager
            page={page}
            pageSize={size}
            total={typeof found === 'number' ? found : undefined}
            loading={state.loading}
            hasNext={hasNext}
            onPage={(next) => onPage?.(next)}
            note={<>第 {text(pick(data, ['page']) ?? page)} 页</>}
          />
        )}
      </AsyncBox>

      <Notice kind="info">只读区：这里不会向平台提交任何资质材料。</Notice>
    </div>
  )
}

/* ------------------------------------------------------------------ 写区（危险） */

type WriteKind = 'packages' | 'oss'

const WRITE_META: Record<WriteKind, { path: string; label: string; desc: string }> = {
  packages: {
    path: 'packages/submit',
    label: '提交资质材料包',
    desc: '把一份资质材料包提交给闲鱼审核。提交后平台侧就会真的开始审核，本地无法撤回。',
  },
  oss: {
    path: 'oss-credential',
    label: '申请 OSS 上传凭证',
    desc: '向平台申请一个上传凭证（用于把资质图片传到闲鱼的对象存储）。这一步也会真的打到平台。',
  },
}

/** 解析原始 JSON：必须是对象；解析失败把原因原样回显给用户。 */
function parseRequest(raw: string): { value: Record<string, unknown> | null; error: string } {
  const trimmed = raw.trim()
  if (!trimmed) return { value: null, error: '请先填入要提交的 JSON 对象' }
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch (err) {
    return { value: null, error: `JSON 解析失败：${errorText(err)}` }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { value: null, error: '必须是 JSON 对象（以 { 开头、以 } 结尾）' }
  }
  return { value: parsed as Record<string, unknown>, error: '' }
}

/**
 * 写操作面板：**默认收起**。
 *
 * 为什么用原始 JSON 文本框而不是表单：两个写接口的 `request` 字段名**没有 bundle 证据**，
 * 服务层原样透传。编字段名做表单 = 编数据，提交上去只会被平台拒绝或写错东西，
 * 所以这里只负责「把你确认过的 JSON 原样发出去」。
 */
function WritePanel({ accountId, kind }: { accountId: string; kind: WriteKind }) {
  const meta = WRITE_META[kind]
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [raw, setRaw] = useState('{}')
  const [acked, setAcked] = useState(false)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<unknown>(null)

  const parsed = useMemo(() => parseRequest(raw), [raw])
  const ready = parsed.value !== null && acked && !busy

  const submit = async () => {
    if (!parsed.value) return
    setBusy(true)
    setError('')
    try {
      const data = await api.post<unknown>(
        `/api/qualification/${encodeURIComponent(accountId)}/${meta.path}`,
        { confirm: true, request: parsed.value },
      )
      setResult(data === undefined ? { success: true } : data)
      setAsking(false)
      toast.success(`${meta.label}：平台已接受这次请求`)
    } catch (err) {
      setError(errorText(err))
      setAsking(false)
      toast.error(`${meta.label}失败`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`ql-write${open ? ' open' : ''}`}>
      <div className="ql-write-head">
        <span className="ql-ico">
          <IconShield size={15} />
        </span>
        <div className="ql-write-title">
          <span className="ql-write-name">{meta.label}</span>
          <span className="ql-write-desc">{meta.desc}</span>
        </div>
        <span className="ql-spacer" />
        <button
          className="btn sm"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          disabled={!accountId}
        >
          {open ? '收起' : '展开'}
        </button>
      </div>

      {open && (
        <div className="ql-write-body">
          <Notice kind="warn">
            这个按钮会<strong>真的向闲鱼提交资质材料</strong>。写接口的 `request` 字段名没有 bundle 证据
            （后端原样透传），所以这里<strong>不提供字段表单</strong> —— 请自己粘贴确认过的 JSON 对象。
          </Notice>

          <Field
            label="request（原始 JSON 对象）"
            tip="会原样放进 POST body 的 request 字段。这里不猜字段名、不做校验以外的任何加工。"
          >
            <textarea
              className="ql-textarea"
              rows={8}
              spellCheck={false}
              value={raw}
              onChange={(event) => setRaw(event.target.value)}
              placeholder='在此粘贴要提交的 JSON 对象，例如 {"...": "..."}'
            />
          </Field>

          {parsed.error && <Notice kind="error">{parsed.error}</Notice>}

          <label className="ql-ack">
            <input type="checkbox" checked={acked} onChange={(event) => setAcked(event.target.checked)} />
            <span>我已确认这份内容是真的要提交给闲鱼的资质材料，愿意承担平台侧审核后果</span>
          </label>

          <div className="ql-bar">
            <span className="dim">
              提交目标：POST /api/qualification/{accountId || '-'}/{meta.path}
            </span>
            <span className="ql-spacer" />
            <button className="btn danger" disabled={!ready} onClick={() => setAsking(true)}>
              <span className="ql-ico">
                <IconUpload size={13} />
              </span>
              {meta.label}
            </button>
          </div>

          {error && (
            <Notice kind="error" onRetry={() => setError('')}>
              {error}
            </Notice>
          )}

          {result !== null && (
            <>
              <div className="ql-section-title">平台返回</div>
              <pre className="ql-pre">{JSON.stringify(result, null, 2)}</pre>
            </>
          )}
        </div>
      )}

      {asking && (
        <ConfirmDialog
          danger
          busy={busy}
          title={`确认${meta.label}？`}
          confirmText="确认提交到闲鱼"
          message={
            <>
              这会<strong>真的向闲鱼提交资质材料</strong>，不是本地演示。提交后平台侧会开始审核，
              本地<strong>无法撤回</strong>。
              <br />
              账号：{accountId || '-'}
              <br />
              目标：POST /api/qualification/{accountId || '-'}/{meta.path}
            </>
          }
          onConfirm={submit}
          onCancel={() => setAsking(false)}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function QualificationPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [tab, setTab] = useState<TabKey>('detail')
  const [page, setPage] = useState(1)

  /** 选中账号 = 用户选的优先，否则退回第一个（派生值，不用 useEffect 回写）。 */
  const accountId = picked || accounts.data?.[0]?.id || ''

  const detail = useAsync<QlResult>(
    () => api.get<QlResult>(`/api/qualification/${encodeURIComponent(accountId)}/detail`),
    [accountId],
    Boolean(accountId) && tab === 'detail',
    ['accounts.changed'],
  )

  const submitted = useAsync<QlResult>(
    () =>
      api.get<QlResult>(`/api/qualification/${encodeURIComponent(accountId)}/submitted-records`, {
        page,
        size: PAGE_SIZE,
      }),
    [accountId, page],
    Boolean(accountId) && tab === 'submitted',
    ['accounts.changed'],
  )

  const categories = useAsync<QlResult>(
    () => api.get<QlResult>(`/api/qualification/${encodeURIComponent(accountId)}/categories`),
    [accountId],
    Boolean(accountId) && tab === 'categories',
    ['accounts.changed'],
  )

  const templates = useAsync<QlResult>(
    () => api.get<QlResult>(`/api/qualification/${encodeURIComponent(accountId)}/templates`),
    [accountId],
    Boolean(accountId) && tab === 'templates',
    ['accounts.changed'],
  )

  const reusable = useAsync<QlResult>(
    () => api.get<QlResult>(`/api/qualification/${encodeURIComponent(accountId)}/reusable`),
    [accountId],
    Boolean(accountId) && tab === 'reusable',
    ['accounts.changed'],
  )

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">资质管理</h2>
          <p className="page-desc">
            超级擦亮的资质详情、已提交记录、可申请类目、模板与可复用材料；底部另有两个<strong>会真的提交到闲鱼</strong>
            的写操作，默认收起。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="ql-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="ql-bar">
          <Field label="账号" tip="资质是账号级的，一个账号一套资质数据">
            <select
              value={accountId}
              onChange={(event) => {
                setPicked(event.target.value)
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
          </Field>
          <span className="ql-spacer" />
        </div>
        {accounts.notReady && <Notice kind="warn">账号接口还没就绪，暂时选不了账号；页面其余部分可正常使用。</Notice>}
        {accounts.error && (
          <Notice kind="error" onRetry={accounts.reload}>
            {accounts.error}
          </Notice>
        )}
      </div>

      <div className="card">
        <div className="tabs" role="tablist">
          {TABS.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={tab === item.key}
              className={`tab${tab === item.key ? ' active' : ''}`}
              title={item.tip}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {!accountId ? (
        <div className="card">
          <Notice kind="info">先在上方选一个账号：资质是账号级的，没有账号就没数据可看。</Notice>
        </div>
      ) : tab === 'detail' ? (
        <div className="card">
          <h3>
            资质详情
            <span className="hint">平台返回的全部字段{fetchedNote(detail.data)}</span>
          </h3>
          <div className="ql-bar">
            <span className="dim">形状未冻结 ⇒ 按平台返回的键原样列出</span>
            <span className="ql-spacer" />
            <RefreshButton onClick={detail.reload} disabled={detail.loading} />
          </div>
          <WarningList warnings={pickWarnings(detail.data)} />
          <AsyncBox loading={detail.loading} error={detail.error} notReady={detail.notReady} onRetry={detail.reload}>
            <DetailBody data={detail.data} />
          </AsyncBox>
        </div>
      ) : tab === 'submitted' ? (
        <ListPanel key={`submitted-${accountId}`} kind="submitted" state={submitted} page={page} onPage={setPage} showPager />
      ) : tab === 'categories' ? (
        <ListPanel key={`categories-${accountId}`} kind="categories" state={categories} />
      ) : tab === 'templates' ? (
        <ListPanel key={`templates-${accountId}`} kind="templates" state={templates} />
      ) : (
        <ListPanel key={`reusable-${accountId}`} kind="reusable" state={reusable} />
      )}

      <div className="card">
        <div className="ql-section-title">写操作（危险）</div>
        <Notice kind="warn">
          下面两块<strong>会真的打到闲鱼平台</strong>，不是本地演示。默认收起；展开后还要勾选确认，
          再经过一次二次确认弹窗才会提交。只读区的五个接口永远不会写任何东西。
        </Notice>
        {accountId ? (
          <>
            <WritePanel accountId={accountId} kind="packages" />
            <WritePanel accountId={accountId} kind="oss" />
          </>
        ) : (
          <p className="dim">先选一个账号，写操作才能用。</p>
        )}
      </div>
    </div>
  )
}
