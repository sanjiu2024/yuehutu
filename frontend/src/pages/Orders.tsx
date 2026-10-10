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
 * 只读：五个端点都没有写方法（后端 405 锁死），本组页面不提供任何写操作入口。
 */

import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { api, ApiError } from '../api'
import { AsyncBox, Badge, Field, Notice } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatTime } from '../format'
import { IconChevronDown, IconChevronRight, IconRefresh, IconShield } from '../icons'
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

/** 平台的 `buttons` —— 只渲染文案，**不绑任何点击**（写接口后端 405）。 */
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
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="订单"
        desc={
          <>
            已卖出的订单与状态 · 只读
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar accounts={accounts} picked={picked} setPicked={(id) => { setPicked(id); setPage(1) }} />

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
          <span className="tr-sub">只显示文案、点不动 —— 本项目只读，后端写接口一律 405。</span>
        </Field>
      )}
    </div>
  )
}
