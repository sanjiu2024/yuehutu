/**
 * 「交易」组公共外壳 + **订单页**。
 *
 * ⚠️ 写范围只有 `frontend/src/pages/{Orders,Refunds,Evaluations,Complaints,ReturnAddresses,Notifications}.tsx`
 * 与两个 css，**没有独立的公共文件可放**，所以本组 5 页共用的类型、工具函数与列表外壳全部从这里导出，
 * 其余四页 `import { ... } from './Orders'`。哪天允许建公共文件，把这一段整体搬走即可。
 *
 * 契约：`docs/contracts/trade.md`（形状已冻结）。必须守住的几条：
 *  1. **金额是平台原样的字符串**（`"39.90"`），**不能 /100**，也不要转数字再格式化。
 *  2. **平台没给的字段是 `null`，一律显示 `-`，禁止补 0** —— 0 是真实数值，不是「没有」。
 *  3. 分页键在信封顶层 `data.paging.{total_count,next_page}`，拿不到是 `null` 不是 `0`。
 *  4. 平台失败（含 `NO_PERMISSION` / `UNAUTHORIZED`）是 **502 `protocol_error`**，不是空列表；
 *     登录态问题是 **409 `verification_required`**，要去账号页重扫，且**不是**「接口坏了」。
 *  5. 接口只收 `page`/`size`/`include_counts`，**不收筛选与排序参数** ——
 *     所以状态/关键字/日期筛选和表头排序都是**客户端对当前页**做的，UI 上必须写清楚，
 *     不能假装成全库搜索。
 *
 * 6. **列表是只读的，写操作是另一组端点**（`docs/contracts/trade_write.md`）：发货 / 改价 /
 *    退款处理 / 评价 / 投诉共 8 个 POST，全部走本页的「交易写操作」面板，每个都要
 *    `ConfirmDialog` 二次确认，且 `confirm` 只能传字面量 `true` —— 前端不做「默认执行」。
 *    未确认时后端返回 400 `confirmation_required`，一个平台请求都不发。
 */

import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { api, ApiError, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatTime } from '../format'
import {
  IconCheckCircle, IconChevronDown, IconChevronRight, IconEdit, IconRefresh, IconSend,
  IconShield, IconSparkle, IconWarn,
} from '../icons'
import type { Account } from '../types'
import './trade.css'

/* ------------------------------------------------------------------ 公共类型与工具 */

/** 每个交易接口都回的顶层字段（`docs/contracts/trade.md` §1.2）。 */
export interface TradeEnvelope<T> {
  account_id?: string
  page?: number | null
  page_size?: number | null
  api?: string
  source?: string
  fetched_at?: string
  paging?: {
    total_count?: number | null
    next_page?: number | null
    last_end_row?: number | null
    server_time?: number | null
  } | null
  warnings?: string[]
  /** 各接口自己的列表键。 */
  orders?: T[]
  refunds?: T[]
  evaluations?: T[]
  complaints?: T[]
  addresses?: T[]
}

/** 金额：**原样显示**，null/空 → `-`。平台给的是字符串，任何数值加工都是错的。 */
export function money(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const s = String(value).trim()
  return s === '' ? '-' : s
}

/** 计数：只做千分位，拿不到 → `-`（不补 0）。 */
export function num(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)
  return n.toLocaleString('zh-CN')
}

/**
 * 时间：平台给过毫秒时间戳、秒时间戳、已格式化字符串三种。
 * 认不出来就原样显示，**绝不**因为解析失败把时间画成 1970。
 */
export function when(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const raw = String(value).trim()
  if (!/^\d+$/.test(raw)) return formatTime(raw)
  let n = Number(raw)
  if (!Number.isFinite(n) || n <= 0) return raw
  if (n < 1e12) n *= 1000
  if (n > 1e14) return raw
  return formatTime(n)
}

/** 长文本：省略靠 CSS 的 ellipsis，这里补 title 让悬浮能看到全名。 */
export function ellipsis(value: unknown, max = 60): string {
  if (value === null || value === undefined) return ''
  const s = String(value)
  return s.length > max ? `${s.slice(0, max)}…` : s
}

/** 读嵌套取值（`'item.title'`），取不到一律 null —— 调用方负责画 `-`。 */
export function pick(source: unknown, path: string): unknown {
  let cur: unknown = source
  for (const key of path.split('.')) {
    if (cur === null || cur === undefined || typeof cur !== 'object') return null
    cur = (cur as Record<string, unknown>)[key]
  }
  return cur ?? null
}

/** 平台的 `info_lines: [{key, value}]` —— 商品行下的次要信息。 */
export function infoLines(value: unknown): Array<{ key: string; value: string }> {
  if (!Array.isArray(value)) return []
  return value
    .filter((line): line is Record<string, unknown> => Boolean(line) && typeof line === 'object')
    .map((line) => ({ key: String(line.key ?? ''), value: String(line.value ?? '') }))
    .filter((line) => line.key || line.value)
}

/** 平台的 `buttons` —— 只渲染文案，**不绑任何点击**（文案与后端写端点没有契约对应关系）。 */
export function buttonsOf(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((btn) => (btn && typeof btn === 'object' ? String((btn as Record<string, unknown>).name ?? '') : ''))
    .filter(Boolean)
}

/** 取 `YYYY-MM-DD`，供日期筛选用；认不出返回 null（不参与筛选，也不会算出 1970）。 */
export function dayOf(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null
  const raw = String(value).trim()
  if (/^(\d{4})-(\d{2})-(\d{2})/.test(raw)) return raw.slice(0, 10)
  if (!/^\d+$/.test(raw)) return null
  let n = Number(raw)
  if (!Number.isFinite(n) || n <= 0) return null
  if (n < 1e12) n *= 1000
  if (n > 1e14) return null
  const d = new Date(n)
  if (Number.isNaN(d.getTime())) return null
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/**
 * 错误对象有两条来路：有人 `catch` 到的 `ApiError`，和 `useAsync` 已经把原因变成**中文文案字符串**
 * 的 `state.error`（`useAsync.ts:78` 只留 `errorText(err)`）。只按 `instanceof ApiError` 判
 * 会让「无权限 ≠ 掉线」这些提示**永远不出现** —— 页面上只会剩一句看不懂的错误。
 */
function messageOf(error: unknown): string {
  if (typeof error === 'string') return error
  if (error instanceof Error) return error.message
  return ''
}

export function isPermissionDenied(error: unknown): boolean {
  const message = messageOf(error)
  if (!message) return false
  // 有 code 时按契约收紧（无权限走 502 protocol_error）；没有 code 的纯文案只能按词判
  if (error instanceof ApiError && error.code !== 'protocol_error') return false
  return /权限|NO_PERMISSION|UNAUTHORIZED/.test(message)
}

export function isNeedRelogin(error: unknown): boolean {
  const message = messageOf(error)
  if (!message) return false
  if (error instanceof ApiError) return error.code === 'verification_required'
  return /扫码|Cookie|登录态|未登录|登录已过期/.test(message)
}

/* ------------------------------------------------------------------ 页头 / 小配件 */

export function TradeHead({ icon, title, desc, onRefresh, loading }: {
  icon: ReactNode
  title: string
  desc: ReactNode
  onRefresh: () => void
  loading?: boolean
}) {
  return (
    <section className="tr-head">
      <div>
        <h3>{icon}{title}</h3>
        <p>{desc}</p>
      </div>
      <button className="btn sm" onClick={onRefresh} disabled={loading}>
        <IconRefresh size={14} /> 刷新
      </button>
    </section>
  )
}

export function ReadOnlyTag() {
  return <span className="tr-readonly"><IconShield size={12} /> 只读</span>
}

/** 账号选择：与 Finance / Promotion 同一套「派生，不用 effect 回写」的写法。 */
export function useTradeAccounts() {
  const accounts = useAsync<Account[]>(
    () => api.get<Account[]>('/api/accounts'),
    [],
    true,
    ['accounts.changed'],
  )
  const [picked, setPicked] = useState('')
  const accountId = picked || accounts.data?.[0]?.id || ''
  return { accounts, picked, setPicked, accountId }
}

export function AccountBar({ accounts, picked, setPicked, extra }: {
  accounts: ReturnType<typeof useTradeAccounts>['accounts']
  picked: string
  setPicked: (id: string) => void
  extra?: ReactNode
}) {
  return (
    <section className="card tr-toolbar">
      <label className="tr-field">
        账号
        <select value={picked} onChange={(e) => setPicked(e.target.value)} disabled={accounts.loading}>
          {accounts.data?.length ? (
            accounts.data.map((account) => (
              <option key={account.id} value={account.id}>{account.nickname || account.id}</option>
            ))
          ) : (
            <option value="">（还没有账号）</option>
          )}
        </select>
      </label>
      <span className="tr-spacer" />
      {extra}
      <ReadOnlyTag />
    </section>
  )
}

/* ------------------------------------------------------------------ 列表外壳 */

export interface TradeColumn<T> {
  key: string
  label: string
  /** 数字/金额列：右对齐 + 等宽。 */
  num?: boolean
  /** 传了才可排序（平台没有排序参数，排序是客户端对**当前页**做的）。 */
  sortValue?: (row: T) => number | string | null | undefined
  render: (row: T) => ReactNode
}

export interface TradeListViewProps<T> {
  rows: T[] | undefined
  columns: Array<TradeColumn<T>>
  loading: boolean
  error: string
  notReady: boolean
  onRetry: () => void
  rowKey: (row: T, index: number) => string
  /** 平台确实一条都没有时的空态。 */
  emptyTitle: string
  emptyDesc: string
  /** 点击行展开的详情；不传则行不可展开。 */
  detail?: (row: T) => ReactNode
  /** 状态筛选（选项从当前页实际出现的状态里取）。 */
  statusFilter?: { label: string; options: string[]; value: (row: T) => string }
  /** 关键字筛选。 */
  keywordOf?: (row: T) => string
  /** 日期筛选（YYYYMMDD 输入，按行里的日期比较）。 */
  dateOf?: (row: T) => string | null
  page: number
  pageSize: number
  total?: number | null
  hasPrev: boolean
  hasNext: boolean
  onPage: (page: number) => void
  /** 表格上方的自定义区块（汇总卡、说明等）。 */
  toolbar?: ReactNode
}

type SortState = { key: string; dir: 'asc' | 'desc' } | null

export function TradeListView<T>(props: TradeListViewProps<T>) {
  const {
    rows, columns, loading, error, notReady, onRetry, rowKey,
    emptyTitle, emptyDesc, detail, statusFilter, keywordOf, dateOf,
    page, pageSize, total, hasPrev, hasNext, onPage, toolbar,
  } = props

  const [status, setStatus] = useState('')
  const [keyword, setKeyword] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [sort, setSort] = useState<SortState>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const source = rows ?? []
  const hasFilters = Boolean(statusFilter || keywordOf || dateOf)

  /** 客户端筛选：**只作用于当前页** —— 接口不收筛选参数，这是诚实的边界。 */
  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    const begin = normalizeDay(from)
    const end = normalizeDay(to)
    return source.filter((row) => {
      if (statusFilter && status && statusFilter.value(row) !== status) return false
      if (kw && keywordOf && !keywordOf(row).toLowerCase().includes(kw)) return false
      if (dateOf && (begin || end)) {
        const day = dateOf(row)
        if (!day) return false
        if (begin && day < begin) return false
        if (end && day > end) return false
      }
      return true
    })
  }, [source, status, keyword, from, to, statusFilter, keywordOf, dateOf])

  const sorted = useMemo(() => {
    if (!sort) return filtered
    const column = columns.find((item) => item.key === sort.key)
    if (!column?.sortValue) return filtered
    const get = column.sortValue
    const dir = sort.dir === 'asc' ? 1 : -1
    return [...filtered].sort((a, b) => {
      const va = get(a)
      const vb = get(b)
      // 平台经常给 null：排到最后，不要和 0、空串混在一起。
      if (va === null || va === undefined) return 1
      if (vb === null || vb === undefined) return -1
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir
      return String(va).localeCompare(String(vb), 'zh-CN') * dir
    })
  }, [filtered, sort, columns])

  const filtering = Boolean(status || keyword.trim() || from.trim() || to.trim())

  function toggleSort(column: TradeColumn<T>) {
    if (!column.sortValue) return
    setSort((prev) => (
      prev && prev.key === column.key
        ? prev.dir === 'asc'
          ? { key: column.key, dir: 'desc' }
          : null
        : { key: column.key, dir: 'asc' }
    ))
  }

  function clearFilters() {
    setStatus(''); setKeyword(''); setFrom(''); setTo('')
  }

  return (
    <>
      {toolbar}

      {hasFilters && (
        <div className="tr-bar">
          {statusFilter && (
            <label className="tr-field">
              {statusFilter.label}
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">全部</option>
                {statusFilter.options.map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </label>
          )}
          {keywordOf && (
            <label className="tr-field">
              关键字
              <input
                type="search"
                value={keyword}
                placeholder="单号 / 商品 / 买家"
                onChange={(e) => setKeyword(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }}
              />
            </label>
          )}
          {dateOf && (
            <>
              <label className="tr-field tr-date">
                开始日期
                <input type="text" value={from} maxLength={10} placeholder="YYYY-MM-DD"
                  onChange={(e) => setFrom(e.target.value.trim())} />
              </label>
              <label className="tr-field tr-date">
                结束日期
                <input type="text" value={to} maxLength={10} placeholder="YYYY-MM-DD"
                  onChange={(e) => setTo(e.target.value.trim())} />
              </label>
            </>
          )}
          <span className="tr-spacer" />
          <span className="tr-note">筛选与排序只作用于<strong>当前这一页</strong>：平台接口只收 page/size</span>
        </div>
      )}

      <AsyncBox
        loading={loading}
        error={error}
        notReady={notReady}
        empty={!loading && !error && source.length === 0}
        emptyTitle={emptyTitle}
        emptyDesc={emptyDesc}
        onRetry={onRetry}
        emptyIcon="chart"
      >
        {sorted.length === 0 ? (
          <div className="card">
            <div className="tr-empty">
              <p className="tr-empty-title">当前筛选没有匹配的记录</p>
              <p className="tr-empty-desc">
                这一页有 {num(source.length)} 条，但被状态/关键字/日期筛掉了 —— 筛选不会去平台上重新查。
              </p>
              <button className="btn sm" onClick={clearFilters}>清空筛选</button>
            </div>
          </div>
        ) : (
          <div className="card">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    {detail && <th className="tr-toggle-col" aria-label="展开" />}
                    {columns.map((column) => (
                      <th
                        key={column.key}
                        className={column.num ? 'num' : ''}
                        aria-sort={sort?.key === column.key ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                        title={column.sortValue ? '点击按当前页排序（升 / 降 / 取消）' : '这一列不可排序'}
                      >
                        <span
                          className={column.sortValue ? 'tr-th' : 'tr-th tr-th-static'}
                          onClick={() => toggleSort(column)}
                        >
                          {column.label}
                          {column.sortValue ? (
                            <span className="tr-sort">{sort?.key === column.key ? (sort.dir === 'asc' ? '↑' : '↓') : '↕'}</span>
                          ) : null}
                        </span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((row, index) => {
                    const key = rowKey(row, index)
                    const open = expanded === key
                    return (
                      <Fragment key={key}>
                        <tr
                          className={open ? 'selected' : ''}
                          onClick={detail ? () => setExpanded(open ? null : key) : undefined}
                          tabIndex={detail ? 0 : undefined}
                          onKeyDown={detail ? (event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault()
                              setExpanded(open ? null : key)
                            }
                          } : undefined}
                        >
                          {detail && (
                            <td className="tr-toggle-col">
                              {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
                            </td>
                          )}
                          {columns.map((column) => (
                            <td key={column.key} className={column.num ? 'num' : ''}>{column.render(row)}</td>
                          ))}
                        </tr>
                        {open && detail && (
                          <tr className="tr-detail-row">
                            <td colSpan={columns.length + 1}>
                              <div className="tr-detail">{detail(row)}</div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>

            <div className="tr-pager">
              <span className="tr-note">
                第 {num(page)} 页 · 每页 {num(pageSize)} 条 · 本页显示 {num(sorted.length)} 条
                {filtering ? `（本页共 ${num(source.length)} 条）` : ''}
                {' · '}平台返回总数 {num(total)}
              </span>
              <span className="tr-spacer" />
              <button className="btn sm" disabled={!hasPrev} onClick={() => onPage(page - 1)}>上一页</button>
              <button className="btn sm" disabled={!hasNext} onClick={() => onPage(page + 1)}>下一页</button>
            </div>
          </div>
        )}
      </AsyncBox>
    </>
  )
}

/** `YYYYMMDD` → `YYYY-MM-DD`；输入不合法就返回 ''（等于不筛）。 */
function normalizeDay(value: string): string {
  const raw = value.trim()
  if (raw === '') return ''
  if (/^\d{8}$/.test(raw)) return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw
  return ''
}

/** 登录态 / 权限两类失败的统一出口，避免每页各写一版提示。 */
export function TradeError({ error, notReady, onRetry }: {
  error: string
  notReady?: boolean
  onRetry?: () => void
}) {
  if (!error) return null
  return (
    <Notice kind={notReady ? 'warn' : 'error'} onRetry={onRetry}>
      {error}
    </Notice>
  )
}

/* ================================================================== 交易写操作 */

/**
 * 写操作类型 —— 与后端 8 个 POST **一一对应**（`docs/contracts/trade_write.md`）。
 * 字段名一律用后端契约里的真名，前端不发明字段、不改单位（改价传元，换算在后端做一次）。
 */
export type WriteKind =
  | 'ship'
  | 'ship-dummy'
  | 'adjust-price'
  | 'refund-agree'
  | 'refund-refuse'
  | 'rate'
  | 'complain-refuse'
  | 'complain-proof'

type WriteGroup = 'ship' | 'price' | 'refund' | 'rate' | 'complain'

/** 打开写面板时带的预填：订单行的「发货 / 改价」按钮会把 order_id 带进来。 */
export interface WriteDraft {
  kind: WriteKind
  orderId?: string
  refundId?: string
  complainId?: string
}

type WriteOutcome =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; message: string }

/**
 * 唯一的写请求出口。两条硬规矩都在这里：
 *  1. `confirm` 只能是**字面量 `true`** —— 这是后端 `confirmation_required` 闸门的钥匙，
 *     前端永远不替用户点这个头；
 *  2. 失败**原样**把后端 `message`（含平台原文）交回调用方，绝不吞错、绝不改写成成功。
 */
async function postWrite(path: string, body: Record<string, unknown>): Promise<WriteOutcome> {
  try {
    const data = await api.post<Record<string, unknown>>(path, { ...body, confirm: true })
    return { ok: true, data: data ?? {} }
  } catch (err) {
    return { ok: false, message: errorText(err) || '写请求失败，后端没有给出原因' }
  }
}

/** 多行 / 逗号分隔输入 → 数组（去空、保序）。 */
function splitList(value: string): string[] {
  return value.split(/[\n,，;；]+/).map((item) => item.trim()).filter(Boolean)
}

/** 确认框里回显令牌一类的敏感值：只留前 4 位。 */
function masked(name: string, value: string): string {
  if (/token|secret|cookie/i.test(name) && value.length > 4) return `${value.slice(0, 4)}…（${value.length} 位）`
  return ellipsis(value, 60)
}

/** 平台回执：**拿不到的字段显示 `-`，绝不补 0**。 */
function writeSummary(data: Record<string, unknown>): string {
  const parts: string[] = []
  if (data.total_num !== undefined || data.success_num !== undefined) {
    parts.push(`平台回执：总数 ${num(data.total_num)} / 成功 ${num(data.success_num)} / 失败 ${num(data.fail_num)}`)
  }
  if (data.rate_success !== undefined) parts.push(`平台评价结果：${data.rate_success ? '成功' : '失败'}`)
  return parts.join(' · ')
}

interface WriteFieldSpec {
  /** 真实字段名，原样进 body。 */
  name: string
  label: string
  required?: boolean
  placeholder?: string
  hint?: string
  multiline?: boolean
}

interface WriteExtra {
  rate: string
  anonymous: boolean
  proofKind: string
}

interface WriteFormSpec {
  title: string
  action: string
  desc: ReactNode
  fields: WriteFieldSpec[]
  extras?: 'rate' | 'proof-kind'
  path: (accountId: string, value: (name: string) => string) => string
  body: (value: (name: string) => string, extra: WriteExtra) => Record<string, unknown>
  /** 额外校验：返回非空文案表示不通过（必填项由 `required` 统一判）。 */
  check?: (value: (name: string) => string) => string
}

const PROOF_KINDS: Array<{ value: string; label: string }> = [
  { value: 'sellerSubmitProof', label: '卖家举证（默认）' },
  { value: 'submitActiveProof', label: '主动举证' },
  { value: 'submitPassiveProof', label: '被动举证' },
]

function tradePath(accountId: string, suffix: string): string {
  return `/api/trade/${encodeURIComponent(accountId)}${suffix}`
}

function idPath(accountId: string, value: (name: string) => string, name: string, suffix: string): string {
  return tradePath(accountId, `/${suffix.replace('{id}', encodeURIComponent(value(name)))}`)
}

const WRITE_SPECS: Record<WriteKind, WriteFormSpec> = {
  ship: {
    title: '订单发货（自己联系物流）',
    action: '发货',
    desc: <>平台接口 <code>mtop.taobao.idle.logistics.merchant.consign.offline</code>；一次可以提交多个订单号。</>,
    fields: [
      { name: 'trade_ids', label: '订单号列表', required: true, multiline: true, placeholder: '一行一个订单号', hint: '后端会拼成 orderInfos 的 JSON 字符串' },
      { name: 'mail_no', label: '运单号', required: true, placeholder: '如 SF1234567890' },
      { name: 'cp_code', label: '物流公司编码', required: true, placeholder: '如 SF / YTO / ZTO', hint: '平台编码，不是中文公司名' },
      { name: 'address_id', label: '发货地址 id', required: true, placeholder: '如 123456789', hint: '「退货地址」页里平台给的 address_id' },
    ],
    path: (accountId) => tradePath(accountId, '/orders/ship'),
    body: (value) => ({
      trade_ids: splitList(value('trade_ids')),
      mail_no: value('mail_no'),
      cp_code: value('cp_code'),
      address_id: value('address_id'),
    }),
  },
  'ship-dummy': {
    title: '无需物流发货（虚拟发货）',
    action: '无需物流发货',
    desc: <>平台接口 <code>mtop.taobao.idle.logistics.merchant.consign.dummy</code>；虚拟商品、当面交易用。</>,
    fields: [
      { name: 'order_id', label: '订单号', required: true, placeholder: '订单号' },
      { name: 'trade_text', label: '发货说明', placeholder: '可选，平台原文透传' },
      { name: 'pic_urls', label: '凭证图片 URL', multiline: true, placeholder: '一行一个 https 图片地址' },
    ],
    path: (accountId, value) => idPath(accountId, value, 'order_id', 'orders/{id}/ship-dummy'),
    body: (value) => ({
      trade_text: value('trade_text'),
      pic_urls: splitList(value('pic_urls')),
    }),
  },
  'adjust-price': {
    title: '订单改价',
    action: '改价',
    desc: <>平台接口 <code>mtop.taobao.idle.trade.merchant.user.adjust.price</code>；<strong>这里填元</strong>，后端换算成分（平台口径）。</>,
    fields: [
      { name: 'order_id', label: '订单号', required: true, placeholder: '订单号' },
      { name: 'modify_fee', label: '改后商品价格（元）', required: true, placeholder: '如 39.90' },
      { name: 'new_transport_fee', label: '改后运费（元）', required: true, placeholder: '如 0' },
    ],
    path: (accountId, value) => idPath(accountId, value, 'order_id', 'orders/{id}/adjust-price'),
    body: (value) => ({
      modify_fee: value('modify_fee'),
      new_transport_fee: value('new_transport_fee'),
    }),
    check: (value) => {
      const price = value('modify_fee').trim()
      if (price === '' || !Number.isFinite(Number(price))) return '改后商品价格必须是数字（元）'
      if (Number(price) <= 0) return '平台不接受 0 元的商品价格'
      const post = value('new_transport_fee').trim()
      if (post === '' || !Number.isFinite(Number(post))) return '改后运费必须是数字（元，可以是 0）'
      if (Number(post) < 0) return '改后运费不能为负'
      return ''
    },
  },
  'refund-agree': {
    title: '同意退款',
    action: '同意退款',
    desc: <>平台接口 <code>mtop.taobao.idle.merchant.refund.agree.refund</code>；<code>auth_token</code> 是平台下发的操作令牌，必须原样回传。退款列表里没有这个字段 —— 用下面的「获取凭证」向平台要一次（只读，不会改动任何东西）。</>,
    fields: [
      { name: 'refund_id', label: '退款单号', required: true, placeholder: '退款单号（退款列表里给）' },
      { name: 'auth_token', label: '平台操作令牌 auth_token', required: true, placeholder: '点下面的「获取凭证」自动填，也可以自己粘贴' },
    ],
    path: (accountId, value) => idPath(accountId, value, 'refund_id', 'refunds/{id}/agree'),
    body: (value) => ({ auth_token: value('auth_token') }),
  },
  'refund-refuse': {
    title: '拒绝退款',
    action: '拒绝退款',
    desc: <>平台接口 <code>mtop.taobao.idle.merchant.refund.refuse</code>；拒绝原因 id 来自平台只读的 <code>refund.refuse.render</code>。</>,
    fields: [
      { name: 'refund_id', label: '退款单号', required: true, placeholder: '退款单号' },
      { name: 'order_id', label: '订单号', required: true, placeholder: '订单号' },
      { name: 'refuse_reason_id', label: '拒绝原因 id', required: true, placeholder: 'refuseReasonList[].refuseReasonId' },
      { name: 'proof_desc', label: '拒绝说明', multiline: true, placeholder: '可选，会作为 refuseProof.desc 提交' },
      { name: 'proof_urls', label: '凭证图片 URL', multiline: true, placeholder: '一行一个 https 图片地址' },
    ],
    path: (accountId, value) => idPath(accountId, value, 'refund_id', 'refunds/{id}/refuse'),
    body: (value) => ({
      order_id: value('order_id'),
      refuse_reason_id: value('refuse_reason_id'),
      proof_desc: value('proof_desc'),
      proof_urls: splitList(value('proof_urls')),
    }),
  },
  rate: {
    title: '卖家评价',
    action: '评价',
    desc: <>平台接口 <code>mtop.taobao.idle.merchant.rate.create</code>；bundle 里卖家侧只有这一个评价写接口，没有单独的「回复评价」。</>,
    fields: [
      { name: 'trade_ids', label: '订单号列表', required: true, multiline: true, placeholder: '一行一个订单号' },
      { name: 'feedback', label: '评价内容', required: true, multiline: true, placeholder: '评价文案' },
      { name: 'image_urls', label: '评价图片 URL', multiline: true, placeholder: '一行一个 https 图片地址' },
    ],
    extras: 'rate',
    path: (accountId) => tradePath(accountId, '/evaluations'),
    body: (value, extra) => ({
      trade_ids: splitList(value('trade_ids')),
      feedback: value('feedback'),
      rate: Number(extra.rate),
      image_urls: splitList(value('image_urls')),
      anonymous: extra.anonymous,
    }),
  },
  'complain-refuse': {
    title: '投诉拒绝（举证反驳）',
    action: '投诉拒绝',
    desc: <>平台接口 <code>mtop.taobao.idle.cco.shop.complain.refuse</code>；举证图片会作为 proofMediaList 提交。</>,
    fields: [
      { name: 'complain_id', label: '投诉单号', required: true, placeholder: '投诉单号' },
      { name: 'proof_desc', label: '举证说明', placeholder: '可选' },
      { name: 'proof_urls', label: '举证图片 URL', required: true, multiline: true, placeholder: '一行一个 https 图片地址' },
    ],
    path: (accountId, value) => idPath(accountId, value, 'complain_id', 'complaints/{id}/refuse'),
    body: (value) => ({
      proof_desc: value('proof_desc'),
      proof_urls: splitList(value('proof_urls')),
    }),
  },
  'complain-proof': {
    title: '投诉举证',
    action: '投诉举证',
    desc: <>同一个提交框按平台按钮选三个接口之一（<code>submit.proof</code> / <code>.active.proof</code> / <code>.passive.proof</code>）。</>,
    fields: [
      { name: 'complain_id', label: '投诉单号', required: true, placeholder: '投诉单号' },
      { name: 'proof_desc', label: '举证说明', placeholder: '可选' },
      { name: 'proof_urls', label: '举证图片 URL', required: true, multiline: true, placeholder: '一行一个 https 图片地址' },
    ],
    extras: 'proof-kind',
    path: (accountId, value) => idPath(accountId, value, 'complain_id', 'complaints/{id}/proof'),
    body: (value, extra) => ({
      proof_desc: value('proof_desc'),
      proof_urls: splitList(value('proof_urls')),
      kind: extra.proofKind,
    }),
  },
}

const WRITE_TABS: Array<{ key: WriteGroup; label: string; icon: ReactNode }> = [
  { key: 'ship', label: '发货', icon: <IconSend size={14} /> },
  { key: 'price', label: '改价', icon: <IconEdit size={14} /> },
  { key: 'refund', label: '退款处理', icon: <IconCheckCircle size={14} /> },
  { key: 'rate', label: '评价', icon: <IconSparkle size={14} /> },
  { key: 'complain', label: '投诉处理', icon: <IconWarn size={14} /> },
]

const WRITE_MODES: Partial<Record<WriteGroup, Array<{ value: WriteKind; label: string }>>> = {
  ship: [{ value: 'ship', label: '自己联系物流' }, { value: 'ship-dummy', label: '无需物流' }],
  refund: [{ value: 'refund-agree', label: '同意退款' }, { value: 'refund-refuse', label: '拒绝退款' }],
  complain: [{ value: 'complain-refuse', label: '投诉拒绝' }, { value: 'complain-proof', label: '投诉举证' }],
}

/** 没有子模式的 tab（改价 / 评价）点一下要直接切到它唯一的那个端点，不能留在上一个 kind。 */
const GROUP_DEFAULT_KIND: Record<WriteGroup, WriteKind> = {
  ship: 'ship',
  price: 'adjust-price',
  refund: 'refund-agree',
  rate: 'rate',
  complain: 'complain-refuse',
}

function groupOf(kind: WriteKind): WriteGroup {
  if (kind === 'ship' || kind === 'ship-dummy') return 'ship'
  if (kind === 'adjust-price') return 'price'
  if (kind === 'refund-agree' || kind === 'refund-refuse') return 'refund'
  if (kind === 'rate') return 'rate'
  return 'complain'
}

/**
 * 交易写操作面板：8 个写端点收在一个弹窗里（评价页 / 投诉页不在写范围内，
 * 所以那两类操作以本面板的 tab 形式提供）。
 *
 * 每一次提交都要过 `ConfirmDialog`（危险色），确认文案写明「会真的改动闲鱼平台上的订单数据，
 * 不可撤销」；未确认时后端一个平台请求都不发。
 */
export function TradeWritePanel({ accountId, draft, onClose, onChanged }: {
  accountId: string
  draft: WriteDraft
  onClose: () => void
  onChanged: () => void
}) {
  const toast = useToast()
  const [kind, setKind] = useState<WriteKind>(draft.kind)
  const [values, setValues] = useState<Record<string, string>>(() => ({
    order_id: draft.orderId ?? '',
    trade_ids: draft.orderId ?? '',
    refund_id: draft.refundId ?? '',
    complain_id: draft.complainId ?? '',
  }))
  const [rate, setRate] = useState('-1')
  const [anonymous, setAnonymous] = useState(false)
  const [proofKind, setProofKind] = useState(PROOF_KINDS[0].value)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // 退款凭证（只读）：`auth_token` 不在退款列表帧里，只能向平台现问一次。
  const [verifyBusy, setVerifyBusy] = useState(false)
  const [verifyError, setVerifyError] = useState('')
  const [verifyInfo, setVerifyInfo] = useState<{ token: string; url: string } | null>(null)

  const spec = WRITE_SPECS[kind]
  const group = groupOf(kind)
  const modes = WRITE_MODES[group] ?? []
  const value = (name: string) => values[name] ?? ''
  const setValue = (name: string, next: string) => setValues((prev) => ({ ...prev, [name]: next }))

  const missing = spec.fields.filter((field) => field.required && value(field.name).trim() === '')
  const checkMessage = spec.check ? spec.check(value) : ''
  const blocked = missing.length > 0 || Boolean(checkMessage)
  const path = spec.path(accountId, value)

  function switchTo(next: WriteKind) {
    setKind(next)
    setError('')
  }

  /**
   * 向平台要一次退款凭证（`GET …/refunds/{id}/verify-token`，**只读**）。
   *
   * 官方前端在同意 / 拒绝退款弹窗打开时就查一次 `mtop.idle.alipay.verify.url.query`：
   * `data.module.token` 填进 `authToken`、`data.module.verifyUrl` 塞进 iframe 让用户过支付宝身份验证。
   * 这里照做，但把 iframe 换成一个链接（我们不嵌第三方页面）。
   * **不假装成功**：平台没给 token 就明说，不编一个。
   */
  async function fetchVerify() {
    const refundId = value('refund_id').trim()
    if (!refundId) {
      setVerifyError('先填「退款单号」，凭证是按单号问的。')
      return
    }
    setVerifyBusy(true)
    setVerifyError('')
    try {
      const data = await api.get<Record<string, unknown>>(
        `/api/trade/${encodeURIComponent(accountId)}/refunds/${encodeURIComponent(refundId)}/verify-token`,
      )
      const token = typeof data?.token === 'string' ? data.token : ''
      const url = typeof data?.verify_url === 'string' ? data.verify_url : ''
      setVerifyInfo({ token, url })
      if (token) {
        setValue('auth_token', token)
      } else {
        setVerifyError('平台这次没有返回凭证（官方只在管理员态才下发）。可以留空直接试，平台拒绝时会把原因写在错误里。')
      }
    } catch (err) {
      setVerifyInfo(null)
      setVerifyError(errorText(err) || '获取凭证失败，后端没有给出原因')
    }
    setVerifyBusy(false)
  }

  async function submit() {
    setAsking(false)
    setBusy(true)
    setError('')
    const outcome = await postWrite(spec.path(accountId, value), spec.body(value, { rate, anonymous, proofKind }))
    setBusy(false)
    if (!outcome.ok) {
      // 后端 message 原样展示（含平台原文）：既进 toast，也留在面板里方便复制。
      setError(outcome.message)
      toast.error(outcome.message)
      return
    }
    const summary = writeSummary(outcome.data)
    toast.success(`${spec.action}已提交${summary ? ` · ${summary}` : ''}`)
    onChanged()
    onClose()
  }

  return (
    <>
      <Modal
        wide
        title={`交易写操作 · ${spec.title}`}
        onClose={onClose}
        footer={
          <>
            <span className="tr-sub">
              写操作会改动闲鱼平台上的真实数据，每次都要二次确认。
            </span>
            <span className="tr-spacer" />
            <button className="btn ghost" onClick={onClose} disabled={busy}>取消</button>
            <button
              className="btn danger"
              disabled={blocked || busy}
              onClick={() => setAsking(true)}
              title={blocked ? '必填项还没填完' : '会弹出二次确认'}
            >
              <IconShield size={14} /> {spec.action}…
            </button>
          </>
        }
      >
        <div className="tr-write">
          <div className="tr-write-tabs">
            {WRITE_TABS.map((tab) => (
              <button
                key={tab.key}
                className={`tr-write-tab${tab.key === group ? ' active' : ''}`}
                onClick={() => switchTo(WRITE_MODES[tab.key]?.[0]?.value ?? GROUP_DEFAULT_KIND[tab.key])}
              >
                {tab.icon}{tab.label}
              </button>
            ))}
          </div>

          {modes.length > 1 && (
            <div className="tr-write-modes">
              {modes.map((mode) => (
                <label key={mode.value} className="tr-write-mode">
                  <input
                    type="radio"
                    name="tr-write-mode"
                    checked={kind === mode.value}
                    onChange={() => switchTo(mode.value)}
                  />
                  {mode.label}
                </label>
              ))}
            </div>
          )}

          <p className="tr-note">{spec.desc}</p>

          {error && <Notice kind="error">{error}</Notice>}

          <div className="tr-write-grid">
            {spec.fields.map((field) => (
              <label className="tr-field" key={field.name}>
                <span>
                  {field.label}
                  {field.required ? <b className="tr-write-req"> *</b> : null}
                  <code className="tr-write-name">{field.name}</code>
                </span>
                {field.multiline ? (
                  <textarea
                    rows={3}
                    value={value(field.name)}
                    placeholder={field.placeholder}
                    onChange={(event) => setValue(field.name, event.target.value)}
                  />
                ) : (
                  <input
                    type="text"
                    value={value(field.name)}
                    placeholder={field.placeholder}
                    onChange={(event) => setValue(field.name, event.target.value)}
                  />
                )}
                {field.hint ? <span className="tr-sub">{field.hint}</span> : null}
              </label>
            ))}

            {kind === 'refund-agree' && (
              <div className="tr-field tr-write-verify">
                <span>
                  退款凭证
                  <code className="tr-write-name">verify-token</code>
                </span>
                <span className="tr-write-inline">
                  <button className="btn ghost sm" onClick={fetchVerify} disabled={verifyBusy || busy}>
                    <IconRefresh size={14} /> {verifyBusy ? '正在获取…' : '获取凭证'}
                  </button>
                  <span className="tr-sub">
                    只读接口 <code>mtop.idle.alipay.verify.url.query</code>，不会改动任何东西。
                  </span>
                </span>
                {verifyInfo?.token ? (
                  <span className="tr-sub">
                    已填入令牌（{verifyInfo.token.slice(0, 4)}…，共 {verifyInfo.token.length} 字符）。
                  </span>
                ) : null}
                {verifyInfo?.url ? (
                  <span className="tr-sub">
                    平台要求先完成支付宝身份验证：
                    <a href={verifyInfo.url} target="_blank" rel="noreferrer">打开验证页</a>
                  </span>
                ) : null}
                {verifyError ? <Notice kind="warn">{verifyError}</Notice> : null}
              </div>
            )}

            {spec.extras === 'rate' && (
              <>
                <label className="tr-field">
                  <span>评价等级<code className="tr-write-name">rate</code></span>
                  <select value={rate} onChange={(event) => setRate(event.target.value)}>
                    <option value="-1">-1（不指定等级）</option>
                    {[1, 2, 3, 4, 5].map((star) => (
                      <option key={star} value={String(star)}>{star} 星</option>
                    ))}
                  </select>
                </label>
                <label className="tr-field tr-write-check">
                  <span>匿名<code className="tr-write-name">anonymous</code></span>
                  <span className="tr-write-inline">
                    <input
                      type="checkbox"
                      checked={anonymous}
                      onChange={(event) => setAnonymous(event.target.checked)}
                    />
                    匿名评价
                  </span>
                </label>
              </>
            )}

            {spec.extras === 'proof-kind' && (
              <label className="tr-field">
                <span>举证类型<code className="tr-write-name">kind</code></span>
                <select value={proofKind} onChange={(event) => setProofKind(event.target.value)}>
                  {PROOF_KINDS.map((item) => (
                    <option key={item.value} value={item.value}>{item.label}</option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {blocked && (
            <p className="tr-note tr-write-blocked">
              {missing.length > 0
                ? `还缺必填项：${missing.map((field) => field.label).join('、')}`
                : checkMessage}
            </p>
          )}
        </div>
      </Modal>

      {asking && (
        <ConfirmDialog
          danger
          busy={busy}
          title={`确认${spec.action}？`}
          confirmText={`确认${spec.action}`}
          onCancel={() => setAsking(false)}
          onConfirm={submit}
          message={
            <>
              <p style={{ marginTop: 0 }}>
                这次操作会<strong>真的改动闲鱼平台上的订单数据，不可撤销</strong>；成功后本页列表会自动刷新。
              </p>
              <ul className="tr-write-facts">
                <li>接口 <code>{path}</code></li>
                {spec.fields.filter((field) => value(field.name).trim() !== '').map((field) => (
                  <li key={field.name}>
                    {field.label} <code>{field.name}</code>：{masked(field.name, value(field.name))}
                  </li>
                ))}
                {spec.extras === 'rate' && <li>rate：{rate} · anonymous：{anonymous ? '是' : '否'}</li>}
                {spec.extras === 'proof-kind' && <li>kind：{proofKind}</li>}
              </ul>
              <p style={{ marginBottom: 0 }} className="tr-sub">
                提交时 <code>confirm</code> 是字面量 <code>true</code>；未确认时后端不会向平台发任何请求。
              </p>
            </>
          }
        />
      )}
    </>
  )
}

/* ================================================================== 订单页 */

interface OrderBuyer {
  user_id?: string | null
  nick?: string | null
  name?: string | null
  phone?: string | null
  address?: string | null
  avatar?: string | null
  is_privacy?: boolean | null
}
interface OrderItem {
  title?: string | null
  pic_url?: string | null
  info_lines?: unknown
}
interface OrderPrice {
  auction_price?: string | null
  buy_num?: number | null
  post_fee?: string | null
  discount_fee?: string | null
  confirm_fee?: string | null
  refund_fee?: string | null
  total_price?: string | null
}
interface TradeOrder {
  order_id?: string | null
  item_id?: string | null
  order_status?: string | null
  buyer?: OrderBuyer
  item?: OrderItem
  price?: OrderPrice
  times?: { create_time?: unknown; finish_time?: unknown }
  in_refund?: boolean | null
  buyer_apply_modify_address?: boolean | null
  consign_tips?: string[]
  buttons?: unknown[]
}

interface OrderTab { code: string; label: string | null; count: number | null }
interface OrdersResult extends TradeEnvelope<TradeOrder> {
  counts?: Record<string, number | null> | null
  tabs?: OrderTab[] | null
}

export function OrdersPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const [write, setWrite] = useState<WriteDraft | null>(null)
  const pageSize = 10

  const state = useAsync<OrdersResult>(
    () =>
      api.get<OrdersResult>(`/api/trade/${encodeURIComponent(accountId)}/orders`, {
        page,
        size: pageSize,
        // 页签计数要多打一次 `order.count`，只有第一页需要。
        include_counts: page === 1,
      }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.orders ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)

  /**
   * 页签只做**计数展示**，不做过滤：平台的 `queryCode`（TRADE_SUCCESS…）和列表里的
   * `order_status`（中文原文）之间没有契约保证的对应关系，而且接口不收状态参数。
   * 猜一个映射过去，点「交易成功」筛出一堆别的单子，比不给按钮更糟。
   */
  const tabs = data?.tabs ?? []

  const columns: Array<TradeColumn<TradeOrder>> = [
    {
      key: 'order_id',
      label: '订单',
      sortValue: (row) => toSortNumber(row.order_id),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-mono">{money(row.order_id)}</span>
          <span className="tr-sub">{when(row.times?.create_time)}</span>
        </div>
      ),
    },
    {
      key: 'item',
      label: '商品',
      sortValue: (row) => String(row.item?.title ?? ''),
      render: (row) => (
        <div className="tr-item">
          {row.item?.pic_url
            ? <img src={String(row.item.pic_url)} alt="" loading="lazy" />
            : <span className="tr-noimg" />}
          <div className="tr-item-text">
            <span className="tr-name" title={String(row.item?.title ?? '')}>
              {ellipsis(row.item?.title, 40) || '-'}
            </span>
            {infoLines(row.item?.info_lines).map((line, index) => (
              <span className="tr-sub" key={index}>
                {line.key}{line.key && line.value ? '：' : ''}{line.value}
              </span>
            ))}
          </div>
        </div>
      ),
    },
    {
      key: 'buyer',
      label: '买家',
      sortValue: (row) => String(row.buyer?.nick ?? row.buyer?.name ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-name" title={String(row.buyer?.nick ?? row.buyer?.name ?? '')}>
            {ellipsis(row.buyer?.nick ?? row.buyer?.name ?? '', 18) || '-'}
          </span>
          <span className="tr-sub">{row.buyer?.user_id ? `ID ${row.buyer.user_id}` : 'ID -'}</span>
        </div>
      ),
    },
    {
      key: 'status',
      label: '状态',
      sortValue: (row) => String(row.order_status ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <Badge tone={toneForOrder(row.order_status)}>{money(row.order_status)}</Badge>
          {row.in_refund ? <span className="tr-flag">退款中</span> : null}
          {row.buyer_apply_modify_address ? <span className="tr-flag">买家申请改地址</span> : null}
        </div>
      ),
    },
    {
      key: 'amount',
      label: '实付款（元）',
      num: true,
      sortValue: (row) => toSortNumber(row.price?.total_price ?? row.price?.auction_price),
      render: (row) => (
        <span className="tr-money">{money(row.price?.total_price ?? row.price?.auction_price)}</span>
      ),
    },
    {
      key: 'buttons',
      label: '平台给出的操作',
      render: (row) => {
        const names = buttonsOf(row.buttons)
        if (names.length === 0) return <span className="tr-sub">-</span>
        return <span className="tr-btns">{names.map((name) => <Badge key={name} tone="muted">{name}</Badge>)}</span>
      },
    },
    {
      key: 'write',
      label: '写操作',
      render: (row) => {
        const orderId = String(row.order_id ?? '')
        return (
          <span className="tr-writes" onClick={(event) => event.stopPropagation()}>
            <button
              className="btn sm danger"
              disabled={!accountId || !orderId}
              title={orderId ? '发货：会真的改动平台数据，要二次确认' : '这一行没有订单号，发不了货'}
              onClick={() => setWrite({ kind: 'ship', orderId })}
            >
              <IconSend size={13} /> 发货
            </button>
            <button
              className="btn sm danger"
              disabled={!accountId || !orderId}
              title="改价：会真的改动平台数据，要二次确认"
              onClick={() => setWrite({ kind: 'adjust-price', orderId })}
            >
              <IconEdit size={13} /> 改价
            </button>
            {row.in_refund ? (
              <button
                className="btn sm danger"
                disabled={!accountId}
                title="处理退款：平台这里只给了「退款中」标记，退款单号要填退款列表里的 refund_id"
                onClick={() => setWrite({ kind: 'refund-agree', orderId })}
              >
                <IconCheckCircle size={13} /> 退款
              </button>
            ) : null}
          </span>
        )
      },
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="订单"
        desc={
          <>
            已卖出的订单与状态 · 列表只读，写操作（发货 / 改价 / 退款 / 评价 / 投诉）都要二次确认
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar
        accounts={accounts}
        picked={picked}
        setPicked={(id) => { setPicked(id); setPage(1) }}
        extra={
          <button
            className="btn sm danger"
            disabled={!accountId}
            title={accountId ? '8 个写端点都在这个面板里，每次都要二次确认' : '先选一个账号'}
            onClick={() => setWrite({ kind: 'ship' })}
          >
            <IconShield size={14} /> 交易写操作
          </button>
        }
      />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没订单可看。</Notice>}
      {relogin && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查订单 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}
      {permission && (
        <Notice kind="warn">
          平台不给这个账号开「订单」权限（<code>NO_PERMISSION</code>）。这不是掉线，<strong>不用重新扫码</strong>
          —— 要开通得换卖家工作台会话。
        </Notice>
      )}

      <TradeListView<TradeOrder>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(row.order_id ?? index)}
        emptyTitle="这个账号还没有卖出订单"
        emptyDesc="平台一条订单都没返回。拿不到的数字一律显示 -，不会用 0 冒充。"
        detail={(row) => <OrderDetail row={row} />}
        statusFilter={{
          label: '状态',
          options: Array.from(new Set(rows.map((row) => String(row.order_status ?? '')).filter(Boolean))),
          value: (row) => String(row.order_status ?? ''),
        }}
        keywordOf={(row) => [row.order_id, row.item_id, row.item?.title, row.buyer?.nick, row.buyer?.name]
          .filter(Boolean).join(' ')}
        dateOf={(row) => dayOf(row.times?.create_time)}
        page={page}
        pageSize={pageSize}
        total={data?.paging?.total_count ?? null}
        hasPrev={page > 1}
        hasNext={Number(data?.paging?.next_page ?? 0) > page}
        onPage={setPage}
        toolbar={
          tabs.length > 0 ? (
            <section className="card">
              <div className="tr-head-row">
                <span className="tr-head-title">订单状态计数</span>
                <span className="tr-note">
                  只是计数，不能点：平台不按状态过滤订单（接口只收 page/size）
                </span>
              </div>
              <div className="tr-tabs">
                {tabs.map((item) => (
                  <div
                    className="tr-tab"
                    key={item.code}
                    title={item.label
                      ? `平台状态码 ${item.code}`
                      : `平台返回了未知状态码 ${item.code}，本项目不猜中文`}
                  >
                    <span>{item.label ?? `未知状态码 ${item.code}`}</span>
                    <b>{num(item.count)}</b>
                  </div>
                ))}
              </div>
            </section>
          ) : null
        }
      />

      {write && accountId ? (
        <TradeWritePanel
          accountId={accountId}
          draft={write}
          onClose={() => setWrite(null)}
          onChanged={state.reload}
        />
      ) : null}
    </>
  )
}

/** 排序用的数值：转不出来就返回 null（排到末尾），绝不返回 NaN。 */
function toSortNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function toneForOrder(status: string | null | undefined): 'ok' | 'warn' | 'danger' | 'muted' {
  const text = String(status ?? '')
  if (text.includes('关闭') || text.includes('退款')) return 'danger'
  if (text.includes('成功') || text.includes('完成')) return 'ok'
  if (text.includes('待') || text.includes('发货')) return 'warn'
  return 'muted'
}

function OrderDetail({ row }: { row: TradeOrder }) {
  const names = buttonsOf(row.buttons)
  return (
    <div className="tr-detail-grid">
      <Field label="买家">
        <span className="tr-name">{money(row.buyer?.nick ?? row.buyer?.name)}</span>
        <span className="tr-sub">{row.buyer?.phone ? `电话 ${row.buyer.phone}` : '电话 -'}</span>
        <span className="tr-sub">{row.buyer?.address ? `地址 ${row.buyer.address}` : '地址 -'}</span>
        {row.buyer?.is_privacy ? <span className="tr-flag">平台对买家信息做了隐私处理</span> : null}
      </Field>
      <Field label="金额（元，平台原样）">
        <span>成交价 <b className="tr-money">{money(row.price?.auction_price)}</b></span>
        <span className="tr-sub">运费 {money(row.price?.post_fee)} · 数量 {num(row.price?.buy_num)}</span>
        <span className="tr-sub">优惠 {money(row.price?.discount_fee)} · 确认收货 {money(row.price?.confirm_fee)}</span>
        <span className="tr-sub">退款 {money(row.price?.refund_fee)} · 合计 <b>{money(row.price?.total_price)}</b></span>
      </Field>
      <Field label="时间">
        <span>下单 {when(row.times?.create_time)}</span>
        <span className="tr-sub">完成 {when(row.times?.finish_time)}</span>
        <span className="tr-sub">商品 ID {money(row.item_id)}</span>
      </Field>
      <Field label="发货">
        {row.consign_tips?.length
          ? <span className="tr-sub">{row.consign_tips.join('；')}</span>
          : <span className="tr-sub">平台没给发货提示（-）</span>}
      </Field>
      {names.length > 0 && (
        <Field label="平台给出的操作">
          <span className="tr-btns">{names.map((name) => <Badge key={name} tone="muted">{name}</Badge>)}</span>
          <span className="tr-sub">
            只显示平台文案（与本项目的写端点没有契约对应关系）；要动平台数据请用这一行的
            「发货 / 改价」，或上方「交易写操作」面板 —— 每次都会二次确认。
          </span>
        </Field>
      )}
    </div>
  )
}
