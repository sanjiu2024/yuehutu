/**
 * 「交易」组公共外壳 + **订单页**。
 *
 * 注意：写范围只有 `frontend/src/pages/{Orders,Refunds,Evaluations,Complaints,ReturnAddresses,Notifications}.tsx`
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
 *    退款处理 / 运费退款 / 评价 / 投诉 / 关单 / 延长收货 / 提醒确认收货 / 撤销投诉共 13 个 POST，
 *    全部走本页的「交易写操作」面板，每个都要 `ConfirmDialog` 二次确认，且 `confirm` 只能传
 *    字面量 `true` —— 前端不做「默认执行」。未确认时后端返回 400 `confirmation_required`，
 *    一个平台请求都不发。
 */

import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { api, ApiError, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatTime } from '../format'
import {
  IconCheckCircle, IconChevronDown, IconChevronRight, IconClock, IconEdit, IconLink, IconRefresh,
  IconSend, IconShield, IconSparkle, IconWarn,
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
  // ---- 第三十八批：官方卖家工作台订单页的另外 5 个写操作 ----
  | 'close'
  | 'delay'
  | 'remind'
  | 'postage-pay'
  | 'complain-revoke'

type WriteGroup = 'order' | 'ship' | 'price' | 'refund' | 'rate' | 'complain'

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

/**
 * 延时 / 提醒这两个批量写操作：`order_ids` **留空就不发这个键**（后端用路径里那一个订单号），
 * 而不是发一个空数组 —— 平台侧空列表是「什么都没选」，跟「就用这一单」不是一回事。
 */
function withOrderIds(body: Record<string, unknown>, raw: string): Record<string, unknown> {
  const ids = splitList(raw)
  if (ids.length > 0) body.order_ids = ids
  return body
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
    // 延时 / 提醒这类批量写：平台回 `data.module.{totalNum,successNum,failOrderInfos}`，
    // 三个都可能是 null（不是 0）。失败明细只报条数，具体原因在返回值里。
    const failed = Array.isArray(data.fail_order_infos)
      ? String(data.fail_order_infos.length)
      : num(data.fail_num)
    parts.push(`平台回执：总数 ${num(data.total_num)} / 成功 ${num(data.success_num)} / 失败 ${failed}`)
  }
  if (data.rate_success !== undefined) parts.push(`平台评价结果：${data.rate_success ? '成功' : '失败'}`)
  if (data.pay_type !== undefined || data.amount !== undefined) {
    parts.push(`付款方式 ${String(data.pay_type ?? '-')} / 金额 ${num(data.amount)}`)
  }
  if (data.saved === false) parts.push('平台没有回显成功标记，请刷新核对')
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
  /** 延时天数（只认 3/5/7/10，官方那四档）。 */
  delayDays: string
  /** 运费退款的付款方式：alipay（默认）/ deposit。 */
  payType: string
  /** 提醒确认收货里的「全部提醒」开关。 */
  remindAll: boolean
}

interface WriteFormSpec {
  title: string
  action: string
  desc: ReactNode
  fields: WriteFieldSpec[]
  extras?: 'rate' | 'proof-kind' | 'close-reason' | 'delay-days' | 'pay-type' | 'remind-all'
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

  /* ---- 第三十八批：官方卖家工作台订单页的另外 5 个写操作（证据见 docs/contracts/trade_write.md）---- */

  close: {
    title: '关闭订单',
    action: '关闭订单',
    desc: (
      <>
        平台接口 <code>mtop.taobao.idle.trade.merchant.close.by.seller</code> v2.0。
        官方原文提醒：<strong>确认关闭后，请确保已和买家协商达成一致，否则您将面临投诉或赔偿</strong>。
        原因可以点上面的「拉取平台原因」拿，平台没给就手填一个（平台只要求非空）。
      </>
    ),
    fields: [
      { name: 'order_id', label: '订单号', required: true, placeholder: '订单号' },
      {
        name: 'close_reason',
        label: '关单原因',
        required: true,
        placeholder: '从平台原因里选，或自己填一个',
        hint: '原样发给平台；不确定就先拉一次平台原因，别自己编。',
      },
    ],
    extras: 'close-reason',
    path: (accountId, value) => idPath(accountId, value, 'order_id', 'orders/{id}/close'),
    body: (value) => ({ close_reason: value('close_reason').trim() }),
  },

  delay: {
    title: '延长收货时间',
    action: '延长收货',
    desc: (
      <>
        平台接口 <code>mtop.taobao.idle.trade.merchant.batch.delay.confirm</code>。官方文案：
        延长收货时间可以让买家有更多时间来「确认收货」，而不会急于去申请退款。
        天数平台只认 <strong>3 / 5 / 7 / 10</strong> 四档（换算成秒在后端做一次）。
      </>
    ),
    fields: [
      { name: 'order_id', label: '订单号（路径里那一个）', required: true, placeholder: '订单号' },
      {
        name: 'order_ids',
        label: '一起延时的其它订单号',
        multiline: true,
        placeholder: '留空就只处理上面那一个；也可以一行一个或逗号分隔',
        hint: '平台收的是 orderIdList，逗号串由后端拼；留空时后端用路径里那一个。',
      },
    ],
    extras: 'delay-days',
    path: (accountId, value) => idPath(accountId, value, 'order_id', 'orders/{id}/delay'),
    body: (value, extra) => withOrderIds({ days: extra.delayDays }, value('order_ids')),
  },

  remind: {
    title: '提醒买家确认收货',
    action: '提醒确认收货',
    desc: (
      <>
        平台接口 <code>mtop.taobao.idle.trade.merchant.batch.remind.confirm</code>。
        「全部提醒」是官方那个开关（<code>remindAllOrder</code>），默认不勾。
      </>
    ),
    fields: [
      { name: 'order_id', label: '订单号（路径里那一个）', required: true, placeholder: '订单号' },
      {
        name: 'order_ids',
        label: '一起提醒的其它订单号',
        multiline: true,
        placeholder: '留空就只提醒上面那一个；也可以一行一个或逗号分隔',
      },
    ],
    extras: 'remind-all',
    path: (accountId, value) => idPath(accountId, value, 'order_id', 'orders/{id}/remind'),
    body: (value, extra) => withOrderIds({ remind_all: extra.remindAll }, value('order_ids')),
  },

  'postage-pay': {
    title: '支付运费退款（真的付钱）',
    action: '支付运费退款',
    desc: (
      <>
        平台接口 <code>mtop.taobao.idle.merchant.postage.refund.pay</code>：
        <strong>这是真的从支付宝或保证金里扣钱</strong>，不是只改本地记录。
        「退款金额」只是给人看的（平台请求里不带它），别把它当成改价。
      </>
    ),
    fields: [
      { name: 'order_id', label: '订单号', required: true, placeholder: '订单号' },
      { name: 'refund_id', label: '退款单号', required: true, placeholder: '退款列表里的 refund_id' },
      { name: 'amount', label: '退款金额（元，只用于确认与审计）', placeholder: '可选，例如 12.00' },
    ],
    extras: 'pay-type',
    path: (accountId, value) => idPath(accountId, value, 'refund_id', 'refunds/{id}/postage-pay'),
    body: (value, extra) => {
      const body: Record<string, unknown> = {
        order_id: value('order_id').trim(),
        pay_type: extra.payType,
      }
      if (value('amount').trim()) body.amount = value('amount').trim()
      return body
    },
  },

  'complain-revoke': {
    title: '撤销投诉申请',
    action: '撤销投诉',
    desc: (
      <>
        平台接口 <code>mtop.taobao.idle.cco.shop.complain.apply.revoke</code>：撤销自己发起的那条投诉。
        平台以 <code>data.success</code> 判成败，失败原因取 <code>data.failReason</code>。
      </>
    ),
    fields: [
      { name: 'complain_id', label: '投诉单号', required: true, placeholder: '投诉单号' },
    ],
    path: (accountId, value) => idPath(accountId, value, 'complain_id', 'complaints/{id}/revoke'),
    body: () => ({}),
  },
}

const WRITE_TABS: Array<{ key: WriteGroup; label: string; icon: ReactNode }> = [
  { key: 'order', label: '订单处理', icon: <IconClock size={14} /> },
  { key: 'ship', label: '发货', icon: <IconSend size={14} /> },
  { key: 'price', label: '改价', icon: <IconEdit size={14} /> },
  { key: 'refund', label: '退款处理', icon: <IconCheckCircle size={14} /> },
  { key: 'rate', label: '评价', icon: <IconSparkle size={14} /> },
  { key: 'complain', label: '投诉处理', icon: <IconWarn size={14} /> },
]

const WRITE_MODES: Partial<Record<WriteGroup, Array<{ value: WriteKind; label: string }>>> = {
  order: [
    { value: 'close', label: '关闭订单' },
    { value: 'delay', label: '延长收货时间' },
    { value: 'remind', label: '提醒确认收货' },
  ],
  ship: [{ value: 'ship', label: '自己联系物流' }, { value: 'ship-dummy', label: '无需物流' }],
  refund: [
    { value: 'refund-agree', label: '同意退款' },
    { value: 'refund-refuse', label: '拒绝退款' },
    { value: 'postage-pay', label: '支付运费退款（真的付钱）' },
  ],
  complain: [
    { value: 'complain-refuse', label: '投诉拒绝' },
    { value: 'complain-proof', label: '投诉举证' },
    { value: 'complain-revoke', label: '撤销投诉申请' },
  ],
}

/** 没有子模式的 tab（改价 / 评价）点一下要直接切到它唯一的那个端点，不能留在上一个 kind。 */
const GROUP_DEFAULT_KIND: Record<WriteGroup, WriteKind> = {
  order: 'close',
  ship: 'ship',
  price: 'adjust-price',
  refund: 'refund-agree',
  rate: 'rate',
  complain: 'complain-refuse',
}

function groupOf(kind: WriteKind): WriteGroup {
  if (kind === 'close' || kind === 'delay' || kind === 'remind') return 'order'
  if (kind === 'ship' || kind === 'ship-dummy') return 'ship'
  if (kind === 'adjust-price') return 'price'
  if (kind === 'refund-agree' || kind === 'refund-refuse' || kind === 'postage-pay') return 'refund'
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
  const [delayDays, setDelayDays] = useState('3')
  const [payType, setPayType] = useState('alipay')
  const [remindAll, setRemindAll] = useState(false)
  // 关单原因（只读）：官方关单弹窗先调 `trade.order.close.reason.get` 把原因当选项。
  const [reasonsBusy, setReasonsBusy] = useState(false)
  const [reasonsError, setReasonsError] = useState('')
  // `null` = 平台没返回原因列表键（**不是「没有原因」**），`[]` = 平台明确回了一个空数组。
  const [reasons, setReasons] = useState<string[] | null>(null)
  const [reasonsFetched, setReasonsFetched] = useState(false)
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
    // 关单原因和订单号绑定：换动作就作废，免得把上一个订单的原因带过去。
    setReasons(null)
    setReasonsFetched(false)
    setReasonsError('')
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

  /**
   * 拉一次「关单原因」候选（`GET …/orders/{id}/close-reasons`，**只读**）。
   *
   * 三种结果必须分开说：拿到数组就是选项；拿到**空数组**是「平台明确说这个订单没有可选原因」；
   * 拿到 `null` 是「平台没返回原因列表键」—— 那时只能手填，而且要说清楚不是「没有原因」。
   */
  async function fetchCloseReasons() {
    const orderId = value('order_id').trim()
    if (!orderId) {
      setReasonsError('先填「订单号」，原因是按订单问的。')
      return
    }
    setReasonsBusy(true)
    setReasonsError('')
    try {
      const data = await api.get<Record<string, unknown>>(
        `/api/trade/${encodeURIComponent(accountId)}/orders/${encodeURIComponent(orderId)}/close-reasons`,
      )
      const list = Array.isArray(data?.close_reasons)
        ? data.close_reasons.map((item) => String(item)).filter(Boolean)
        : null
      setReasons(list)
      setReasonsFetched(true)
      if (list && list.length > 0 && !value('close_reason').trim()) {
        setValue('close_reason', list[0])
      }
    } catch (err) {
      setReasons(null)
      setReasonsFetched(false)
      setReasonsError(errorText(err) || '拉取关单原因失败，后端没有给出原因')
    }
    setReasonsBusy(false)
  }

  async function submit() {
    setAsking(false)
    setBusy(true)
    setError('')
    const outcome = await postWrite(
      spec.path(accountId, value),
      spec.body(value, { rate, anonymous, proofKind, delayDays, payType, remindAll }),
    )
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

            {spec.extras === 'close-reason' && (
              <div className="tr-field tr-write-reasons">
                <span>
                  平台关单原因
                  <code className="tr-write-name">trade.order.close.reason.get</code>
                </span>
                <span className="tr-write-inline">
                  <button className="btn ghost sm" onClick={fetchCloseReasons} disabled={reasonsBusy || busy}>
                    <IconRefresh size={14} /> {reasonsBusy ? '正在拉取…' : '拉取平台原因'}
                  </button>
                  <span className="tr-sub">只读接口，不会改动任何东西。</span>
                </span>
                {reasonsFetched && reasons && reasons.length > 0 && (
                  <select
                    aria-label="平台关单原因"
                    value={reasons.includes(value('close_reason')) ? value('close_reason') : ''}
                    onChange={(event) => setValue('close_reason', event.target.value)}
                  >
                    <option value="">（从平台原因里选一个）</option>
                    {reasons.map((item) => <option key={item} value={item}>{item}</option>)}
                  </select>
                )}
                {reasonsFetched && reasons && reasons.length === 0 && (
                  <Notice kind="warn">
                    平台明确回了一个<strong>空数组</strong>：这个订单现在没有可选原因。
                    要么先别关单，要么手填一个原因（平台只要求非空）。
                  </Notice>
                )}
                {reasonsFetched && reasons === null && (
                  <Notice kind="warn">
                    平台<strong>没有返回原因列表键</strong>（这<strong>不是</strong>「没有原因」）。
                    我们不替你编原因：可以手填一个，或者去官方卖家工作台看当前可选值。
                  </Notice>
                )}
                {reasonsError ? <Notice kind="warn">{reasonsError}</Notice> : null}
              </div>
            )}

            {spec.extras === 'delay-days' && (
              <label className="tr-field">
                <span>延长天数<code className="tr-write-name">days</code></span>
                <select value={delayDays} onChange={(event) => setDelayDays(event.target.value)}>
                  {['3', '5', '7', '10'].map((day) => (
                    <option key={day} value={day}>{day} 天</option>
                  ))}
                </select>
                <span className="tr-sub">平台只认这四档；换算成秒（×86400）在后端做一次。</span>
              </label>
            )}

            {spec.extras === 'pay-type' && (
              <label className="tr-field">
                <span>付款方式<code className="tr-write-name">pay_type</code></span>
                <select value={payType} onChange={(event) => setPayType(event.target.value)}>
                  <option value="alipay">alipay（支付宝，默认）</option>
                  <option value="deposit">deposit（保证金）</option>
                </select>
                <span className="tr-sub">这是真的扣款，不是只改本地记录。</span>
              </label>
            )}

            {spec.extras === 'remind-all' && (
              <label className="tr-field tr-write-check">
                <span>全部提醒<code className="tr-write-name">remind_all</code></span>
                <span className="tr-write-inline">
                  <input
                    type="checkbox"
                    checked={remindAll}
                    onChange={(event) => setRemindAll(event.target.checked)}
                  />
                  用平台的「全部提醒」开关
                </span>
                <span className="tr-sub">不勾就是只提醒上面填的订单。</span>
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
              {kind === 'close' && (
                <p>
                  官方原文提醒：<strong>确认关闭后，请确保已和买家协商达成一致，否则您将面临投诉或赔偿</strong>。
                </p>
              )}
              {kind === 'postage-pay' && (
                <p>
                  <strong>这一步是真的付钱</strong>：平台会从
                  {payType === 'deposit' ? '保证金' : '支付宝'}
                  里扣款给买家，不是只改本地记录。
                </p>
              )}
              <ul className="tr-write-facts">
                <li>接口 <code>{path}</code></li>
                {spec.fields.filter((field) => value(field.name).trim() !== '').map((field) => (
                  <li key={field.name}>
                    {field.label} <code>{field.name}</code>：{masked(field.name, value(field.name))}
                  </li>
                ))}
                {spec.extras === 'rate' && <li>rate：{rate} · anonymous：{anonymous ? '是' : '否'}</li>}
                {spec.extras === 'proof-kind' && <li>kind：{proofKind}</li>}
                {spec.extras === 'delay-days' && <li>days：{delayDays}（delayTime = {delayDays} × 86400 秒）</li>}
                {spec.extras === 'pay-type' && <li>pay_type：{payType}</li>}
                {spec.extras === 'remind-all' && <li>remind_all：{remindAll ? '是' : '否'}</li>}
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

/* ------------------------------------------------- 买家申请改收货地址审批（task-77） */

/**
 * `GET /api/trade/{account_id}/orders/{order_id}/modify-address` 的返回（契约已冻结）。
 *
 * 证据来自本地 trade bundle（`main.js`，**零平台请求**）：读接口是
 * `mtop.idle.merchant.order.get.modify.address.info`，响应 `data.module` =
 * `{modifyStatus, modifyAddressInfoVO, nowAddressInfoVO, modifyDeliverId}`。
 */
interface ModifyAddressData {
  order_id?: string | null
  /** 平台原文（`NONE` / `WAITING` / 其它原样透传）。**拿不到就是 `null`，不许编 'NONE'**。 */
  status?: string | null
  /** 只有 `status === 'WAITING'` 才是平台的 `modifyDeliverId`，否则 `null`。 */
  deliver_id?: string | null
  /** 契约：严格 `status === 'WAITING'`。 */
  can_operate?: boolean
  /** 平台 VO **原样透传**；平台没给是 `null`（不是 `{}`）。 */
  now_address?: Record<string, unknown> | null
  modify_address?: Record<string, unknown> | null
  /** 解密成功时的 `{name,suffix,phoneNumber,address}`，否则 `null`。 */
  decrypted?: {
    name?: string | null
    suffix?: string | null
    phone_number?: string | null
    address?: string | null
  } | null
  warnings?: unknown
  api?: string
  fetched_at?: string
}

/**
 * 官方工作台自己也有这道闸：bundle 偏移 3101000 处是 `"NOT_PAY"===s || "待付款"===s`，
 * 命中就**直接不查、不可操作**。我们跟着拦，并且把原因写给用户看。
 */
function isNotPaid(status: unknown): boolean {
  return status === 'NOT_PAY' || status === '待付款'
}

/**
 * 地址 VO 的**已知**键 → 中文。只认这几个 —— 平台可能给 `privacyFlag` / `namePlaintext`
 * 这类字段，也可能换一套，**没进这张表的全部丢进「其它字段」原样展示，绝不猜键名**。
 */
const ADDRESS_FIELDS: Array<{ keys: string[]; label: string }> = [
  { keys: ['name', 'namePlaintext'], label: '收件人' },
  { keys: ['phoneNumber', 'phone', 'mobile'], label: '电话' },
  { keys: ['address'], label: '地址' },
  { keys: ['suffix'], label: '地址后缀' },
  { keys: ['privacyFlag'], label: '隐私标记' },
  { keys: ['maskedName'], label: '脱敏姓名' },
]

/** 地址字段渲染：`null` / 缺失一律 `-`，布尔翻成是/否 —— **绝不把空值写成 0**。 */
function addressValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function normalizeWarnings(value: unknown): Array<{ code: string; message: string }> {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return { code: '', message: item }
      if (item && typeof item === 'object') {
        const record = item as Record<string, unknown>
        return { code: String(record.code ?? ''), message: String(record.message ?? record.msg ?? '') }
      }
      return { code: '', message: String(item ?? '') }
    })
    .filter((item) => item.code || item.message)
}

/** 一个地址 VO 的卡片：已知键 + 其余键进「其它字段」。`null` 如实说没给。 */
function AddressCard({ title, value }: { title: string; value: Record<string, unknown> | null | undefined }) {
  if (!value) {
    return (
      <div className="tr-addr-card">
        <div className="tr-addr-title">{title}</div>
        <p className="tr-sub">
          平台这一次<strong>没有</strong>传这个地址 —— 契约里它是 <code>null</code>（不是空对象）。
          显示 <code>-</code> 是实情，不会拿空对象或 0 填满。
        </p>
        <div className="tr-addr-rows">
          <span>收件人</span><b>-</b>
          <span>电话</span><b>-</b>
          <span>地址</span><b>-</b>
        </div>
      </div>
    )
  }

  const used = new Set<string>()
  const rows: Array<{ label: string; text: string }> = []
  for (const field of ADDRESS_FIELDS) {
    field.keys.forEach((key) => used.add(key))
    const hit = field.keys.find((key) => value[key] !== undefined)
    if (!hit) continue
    rows.push({ label: field.label, text: addressValue(value[hit]) })
  }
  const extras = Object.keys(value).filter((key) => !used.has(key))

  return (
    <div className="tr-addr-card">
      <div className="tr-addr-title">{title}</div>
      {rows.length === 0 ? (
        <p className="tr-sub">平台给了这个 VO，但已知键一个都没命中 —— 见下面的「其它字段」。</p>
      ) : (
        <div className="tr-addr-rows">
          {rows.map((row) => (
            <Fragment key={row.label}>
              <span>{row.label}</span>
              <b>{row.text}</b>
            </Fragment>
          ))}
        </div>
      )}
      {extras.length > 0 && (
        <div className="tr-addr-extra">
          <div className="tr-sub">其它字段（平台原样透传，键名不翻译）</div>
          <div className="tr-addr-rows">
            {extras.map((key) => (
              <Fragment key={key}>
                <span>{key}</span>
                <b>{addressValue(value[key])}</b>
              </Fragment>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * 改地址审批面板。先 GET 只读接口，`can_operate`（严格 `status === 'WAITING'`）才给同意/拒绝；
 * 两个写操作都走 `ConfirmDialog danger`，请求体里的 `confirm` 只能是**字面量 `true`**。
 */
export function ModifyAddressModal({ accountId, orderId, onClose, onChanged }: {
  accountId: string
  orderId: string
  onClose: () => void
  onChanged: () => void
}) {
  const toast = useToast()
  const [pending, setPending] = useState<'agree' | 'refuse' | null>(null)
  const [busy, setBusy] = useState(false)

  const state = useAsync<ModifyAddressData>(
    () => api.get<ModifyAddressData>(
      `/api/trade/${encodeURIComponent(accountId)}/orders/${encodeURIComponent(orderId)}/modify-address`,
    ),
    [accountId, orderId],
    Boolean(accountId && orderId),
    // 只读查询，没有对应的 WS topic；**不轮询**，只有用户点刷新或写完才重拉。
    [],
  )

  const info = state.data
  const status = info?.status ?? null
  const deliverId = typeof info?.deliver_id === 'string' && info.deliver_id ? info.deliver_id : null
  const canOperate = info?.can_operate === true && status === 'WAITING' && Boolean(deliverId)
  const warnings = normalizeWarnings(info?.warnings)

  async function submit(action: 'agree' | 'refuse') {
    if (!deliverId) {
      toast.error('平台没给 deliver_id，这次没法提交（拿不到就不编一个）')
      return
    }
    setBusy(true)
    try {
      // 本功能**唯一**的写出口：同意 / 拒绝共用它，`confirm: true` 是字面量，
      // 前端不做「默认执行」—— 用户必须先在 ConfirmDialog 上点确认。
      await api.post(
        `/api/trade/${encodeURIComponent(accountId)}/orders/${encodeURIComponent(orderId)}`
        + `/modify-address/${action}`,
        { deliver_id: deliverId, confirm: true },
      )
      toast.success(action === 'agree' ? '已同意修改地址' : '已拒绝修改地址')
      setPending(null)
      state.reload()
      onChanged()
    } catch (err) {
      // 未确认 400 confirmation_required / 字段缺失 400 invalid_fields / 掉线 409 /
      // 平台拒绝 502 protocol_error（带 platform_errors）。**无权限不改登录态**，
      // 所以这里只照实显示原因，不把用户赶去重新扫码。
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Modal
        wide
        title={`改地址审批 · 订单 ${orderId}`}
        onClose={onClose}
        footer={
          canOperate ? (
            <>
              <button className="btn ghost" onClick={onClose} disabled={busy}>关闭</button>
              <button
                className="btn danger"
                disabled={busy}
                title="拒绝买家修改收货地址：会真的改动平台数据，要二次确认"
                onClick={() => setPending('refuse')}
              >
                拒绝修改
              </button>
              <button
                className="btn danger"
                disabled={busy}
                title="同意买家修改收货地址：会真的改动平台数据，要二次确认"
                onClick={() => setPending('agree')}
              >
                同意修改
              </button>
            </>
          ) : (
            <button className="btn ghost" onClick={onClose}>关闭</button>
          )
        }
      >
        {state.loading && !info ? (
          <Notice kind="info">正在问平台：这个订单有没有待处理的改地址申请…</Notice>
        ) : null}

        {state.notReady ? (
          <Notice kind="warn">
            「改地址」这条只读接口还没就绪（后端还没注册路由，暂时 404）。
            这<b>不是</b>平台问题，等后端上线再回来看。
          </Notice>
        ) : null}

        {state.error ? (
          <Notice kind="error" onRetry={state.reload}>{state.error}</Notice>
        ) : null}

        {info ? (
          <>
            <div className="tr-addr-status">
              <span className="tr-sub">平台状态</span>
              <Badge tone={status === 'WAITING' ? 'warn' : 'muted'}>{status ?? '-'}</Badge>
              <span className="tr-sub">
                {status === null
                  ? '平台没回 modifyStatus —— 显示 - ，不编一个 NONE 出来'
                  : status === 'WAITING'
                    ? '买家有申请，等卖家处理'
                    : `平台原文 ${status}：不是 WAITING，没有待处理的申请`}
              </span>
            </div>

            <div className="tr-addr-status">
              <span className="tr-sub">deliver_id</span>
              <b className="tr-mono">{money(deliverId)}</b>
              <span className="tr-sub">
                只有 status 是 WAITING 时平台才会给这个值；其余情况是 null，显示 - 。
              </span>
            </div>

            {isNotPaid(status) ? (
              <Notice kind="warn">
                平台把这个订单标成未付款（<code>NOT_PAY</code> / 待付款），官方工作台在这档
                直接不查也不允许操作。
              </Notice>
            ) : null}

            {warnings.length > 0 ? (
              <Notice kind="warn">
                平台 / 后端给出的提示：
                <ul className="tr-addr-warn">
                  {warnings.map((warning, index) => (
                    <li key={`${warning.code}-${index}`}>
                      {warning.code ? <code>{warning.code}</code> : null}
                      {warning.message ? ` ${warning.message}` : ''}
                    </li>
                  ))}
                </ul>
              </Notice>
            ) : null}

            <div className="tr-addr-grid">
              <AddressCard title="现地址（平台当前收件地址）" value={info.now_address} />
              <AddressCard title="买家申请改成" value={info.modify_address} />
            </div>

            {info.decrypted ? (
              <div className="tr-addr-card">
                <div className="tr-addr-title">解密后的地址（平台明文）</div>
                <div className="tr-addr-rows">
                  <span>收件人</span><b>{addressValue(info.decrypted.name)}</b>
                  <span>地址后缀</span><b>{addressValue(info.decrypted.suffix)}</b>
                  <span>电话</span><b>{addressValue(info.decrypted.phone_number)}</b>
                  <span>地址</span><b>{addressValue(info.decrypted.address)}</b>
                </div>
              </div>
            ) : null}

            {!canOperate ? (
              <Notice kind="info">
                这个订单没有待处理的改地址申请（平台状态 {status ?? '-'}），所以没有同意 / 拒绝按钮。
                官方工作台也只在 <code>modifyStatus === 'WAITING'</code> 时才发两个地址和
                <code>modifyDeliverId</code>。
              </Notice>
            ) : null}

            <div className="tr-addr-status">
              <span className="tr-sub">平台接口 {money(info.api)}</span>
              <span className="tr-sub">拉取于 {when(info.fetched_at)}</span>
              <button className="btn sm ghost" onClick={state.reload} disabled={state.loading}>
                <IconRefresh size={13} /> {state.loading ? '刷新中…' : '重新查询'}
              </button>
            </div>
          </>
        ) : null}
      </Modal>

      {pending ? (
        <ConfirmDialog
          danger
          busy={busy}
          title={pending === 'agree' ? '确认同意买家修改收货地址？' : '确认拒绝买家修改收货地址？'}
          confirmText={pending === 'agree' ? '同意修改' : '拒绝修改'}
          onCancel={() => setPending(null)}
          onConfirm={() => submit(pending)}
          message={
            <>
              <p style={{ marginTop: 0 }}>
                订单 <b className="tr-mono">{orderId}</b> · deliver_id{' '}
                <b className="tr-mono">{money(deliverId)}</b>
              </p>
              <p>
                {pending === 'agree'
                  ? '同意后，平台会把订单的收货地址改成本次申请的地址，之后发货按新地址走。'
                  : '拒绝后，订单继续用原来的收货地址，买家需要自己再申请或联系你。'}
                这是<b>真的写平台数据</b>，做完不能在这里撤销。
              </p>
              <p className="tr-sub" style={{ marginBottom: 0 }}>
                请求会带 <code>{'{ deliver_id, confirm: true }'}</code>；缺 confirm 后端直接 400
                <code> confirmation_required</code>，一个平台请求都不发。
              </p>
            </>
          }
        />
      ) : null}
    </>
  )
}

export function OrdersPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const [write, setWrite] = useState<WriteDraft | null>(null)
  /** 正在看「买家申请改收货地址」的订单号；null 表示面板关着。 */
  const [modifyOrderId, setModifyOrderId] = useState<string | null>(null)
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
        const notPaid = isNotPaid(row.order_status)
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
            <button
              className="btn sm danger"
              disabled={!accountId || !orderId}
              title="订单处理：关闭订单 / 延长收货时间 / 提醒确认收货 —— 三个都会真的改动平台数据"
              onClick={() => setWrite({ kind: 'close', orderId })}
            >
              <IconClock size={13} /> 订单处理
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
            <button
              className="btn sm"
              disabled={!accountId || !orderId || notPaid}
              title={
                notPaid
                  ? '未付款订单连查都不查：官方工作台在 NOT_PAY / 待付款这一档直接不查、不可操作'
                  : row.buyer_apply_modify_address
                    ? '买家申请了修改收货地址，点这里看「现地址 / 申请改成」并同意或拒绝'
                    : '查看这个订单有没有待处理的改地址申请：先 GET 只读接口，只有平台状态是 WAITING 才出现同意 / 拒绝'
              }
              onClick={() => setModifyOrderId(orderId)}
            >
              <IconLink size={13} /> 改地址
            </button>
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
            已卖出的订单与状态 · 列表只读，写操作（关单 / 延时 / 提醒 / 发货 / 改价 / 退款 / 运费退款 /
            评价 / 投诉 / 撤销投诉）都要二次确认
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
            title={accountId ? '13 个写端点都在这个面板里，每次都要二次确认' : '先选一个账号'}
            onClick={() => setWrite({ kind: 'close' })}
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

      {modifyOrderId && accountId ? (
        <ModifyAddressModal
          accountId={accountId}
          orderId={modifyOrderId}
          onClose={() => setModifyOrderId(null)}
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
