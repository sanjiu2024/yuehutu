/**
 * 营销页：官方营销微应用的四个接口（**全部只读**）。
 *
 * 后端契约（`backend/app/api/marketing.py`，逐字核对过，已冻结；路径名不变，参数已订正）：
 *  - `GET /api/marketing/{account_id}/items?item_id=<必填>&date_type=&date_range=`
 *      **单商品一屏指标，没有 page/size**（不是商品列表）。
 *      `date_type` ∈ `recent1d`(缺省) / `recent7d` / `recent30d` / `customDate`；
 *      `date_range` **仅 customDate 时填**，格式 `YYYYMMDD|YYYYMMDD`（竖线分隔）。
 *  - `GET /api/marketing/{account_id}/tasks?biz_scene=&query_scene=&page=&size=&item_ids=`
 *      `size` 1~50（缺省 10），`page` 从 1 开始，`item_ids` 逗号分隔。
 *  - `GET /api/marketing/{account_id}/price-competitiveness?biz_scene=&item_id=<必填>&sku_prices=&commission_rate=`
 *      `sku_prices` 是 **JSON 对象字符串** `{"<skuId>": <priceYuan>}`；`commission_rate` **仅** `TRAFFIC_ORDER_BOOST`；
 *      **响应值是字符串**（`trafficBoostStr` 之类），所以这个 tab 的指标按原文渲染，不做数字本地化。
 *  - `GET /api/marketing/{account_id}/records?task_id=<必填>&biz_scene=&query_scene=`
 *      **没有 page/size、没有时间范围**；`query_scene` 缺省 `ORDER_BOOST_OPERATION_RECORD`。
 * 统一信封 `{success:true,data:{...}}`，`data` 形状：
 *   `{account_id,kind,metrics,items|tasks|records,warnings[],unrecognized_keys[],source,fetched_at,page,page_size}`
 *
 * 六条不能破的口径：
 *  1. **平台没返回的一律显示 `-`，绝不补 0**（见 `count()` / `metric()`）。0 和「拿不到」是两件事。
 *  2. 「契约键缺失（`items` 是 `null`）」和「平台明确返回了空数组」是**两种**状态，文案必须分开
 *     （见 `ListBody`）—— 前者可能是没权限，后者才是真的没有。
 *  3. 平台新字段不许悄悄丢：不在已知列里的键单独成列，超出 6 个的收进「其它字段」，`title` 悬浮给全量。
 *     已知列只是**命名线索**，不是「平台一定有这些列」的断言。
 *  4. 本页没有任何写操作，页面上明写。
 *  5. **必填项没填就不发请求**（黄条说清缺什么）：后端对空必填回 400，白发一次只会把
 *     真正有用的信息淹掉。`sku_prices` 还多一道前端 `JSON.parse` 校验（必须对象、值必须是数字）。
 *  6. 后端对非法枚举/参数回的是**带候选值的中文 400**，原样显示（`errorText` 直接返回服务端
 *     `message`），不许吞成「请求失败」。
 *
 * 不轮询：刷新一律 `useAsync().reload()`；`useAsync` 第 4 参订阅 WS topic 做**静默**重拉。
 */

import { useState, type ReactNode } from 'react'
import { api } from '../api'
import { AsyncBox, Field, Notice, Stat } from '../components'
import { formatTime } from '../format'
import { useAsync, type AsyncState } from '../hooks/useAsync'
import { IconChart, IconRefresh } from '../icons'
import type { Account } from '../types'
import './marketing.css'

/** `/tasks` 的每页条数：缺省与后端一致（10），上限后端是 50。 */
const PAGE_SIZE_DEFAULT = 10
const PAGE_SIZE_MAX = 50

type TabKey = 'items' | 'tasks' | 'competitiveness' | 'records'

/** 一行原始数据：键名不猜，取不到就 `-`。 */
type Row = Record<string, unknown>

const TABS: Array<{ key: TabKey; label: string; tip: string }> = [
  { key: 'items', label: '商品指标', tip: '单个商品的一屏营销指标（必填商品 ID，只读）' },
  { key: 'tasks', label: '营销任务', tip: '营销任务列表与进行中数量（只读）' },
  { key: 'competitiveness', label: '价格竞争力', tip: '按商品看价格竞争力（必填商品 ID，只读）' },
  { key: 'records', label: '投放记录', tip: '某个任务的历史投放记录（必填任务 ID，只读）' },
]

const LIST_LABEL: Record<TabKey, string> = {
  items: '商品指标明细',
  tasks: '营销任务',
  competitiveness: '价格竞争力明细',
  records: '投放记录',
}

/* ------------------------------------------------------------------ 官方枚举（逐字抄后端） */

/** `/items` 的 `date_type`（`backend/app/xianyu/marketing.py:74 DATE_TYPES`）。 */
const DATE_TYPES: Array<{ value: string; label: string }> = [
  { value: 'recent1d', label: '最近 1 天（缺省）' },
  { value: 'recent7d', label: '最近 7 天' },
  { value: 'recent30d', label: '最近 30 天' },
  { value: 'customDate', label: '自定义区间（YYYYMMDD|YYYYMMDD）' },
]

/** `/tasks`、`/price-competitiveness`、`/records` 的 `biz_scene`（`BIZ_SCENES`，只有这 2 个值）。 */
const BIZ_SCENES: Array<{ value: string; label: string }> = [
  { value: 'TRAFFIC_ORDER_BOOST', label: 'TRAFFIC_ORDER_BOOST（流量加速，缺省）' },
  { value: 'TRAFFIC_DRIVEN_REPRICING', label: 'TRAFFIC_DRIVEN_REPRICING（流量调价）' },
]

/** `/tasks` 的 `query_scene` 全集（`QUERY_SCENES`，15 个值逐字抄）。 */
const QUERY_SCENES: string[] = [
  'DEFAULT',
  'OPPORTUNITY_ITEM_RECOMMEND',
  'DISCOUNT_BOOST_SET',
  'DISCOUNT_BOOSTING_TASK',
  'DISCOUNT_BOOSTED_TASK',
  'ORDER_BOOST_FIRST_PAGE_BOOSTING',
  'ORDER_BOOST_FIRST_PAGE_PAUSE',
  'ORDER_BOOST_SECOND_PAGE_BOOSTING',
  'ORDER_BOOST_SECOND_PAGE_CAN_SIGN_UP',
  'ORDER_BOOST_SET',
  'ORDER_BOOSTING_SET',
  'ORDER_BOOSTING_SET_PAUSE',
  'ADMITTANCE_CHECK',
  'ORDER_BOOST_OPERATION_RECORD',
  'ORDER_BOOST_SECOND_PAGE_CAN_SIGN_UP_FILTER',
]
const DEFAULT_QUERY_SCENE = 'ORDER_BOOST_SET'
const RECORD_QUERY_SCENE = 'ORDER_BOOST_OPERATION_RECORD'

const DATE_RANGE_PATTERN = /^\d{8}\|\d{8}$/

interface MarketingResult {
  account_id?: string
  kind?: string
  metrics?: Record<string, unknown> | null
  items?: Row[] | null
  tasks?: Row[] | null
  records?: Row[] | null
  warnings?: string[]
  unrecognized_keys?: string[]
  source?: string
  fetched_at?: string
  page?: number | null
  page_size?: number | null
  total?: number | null
  total_count?: number | null
  has_next?: boolean | null
}

/* ------------------------------------------------------------------ 取值口径 */

/** 计数：拿不到就是 `-`（不是 0）。 */
function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

/** 指标：数字按本地化显示（最多两位小数），非数字原样转文本，拿不到就是 `-`。 */
function metric(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '-'
  const num = Number(value)
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(num)) {
    return num.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  }
  return text(value)
}

/** 文本：空串与 null 一律 `-`，不留空白单元格。 */
function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const str = String(value).trim()
  return str === '' ? '-' : str
}

function isMissing(value: unknown): boolean {
  return value === null || value === undefined
}

/** 页头「更新于」：只有真拿到 `fetched_at` 才写时间。 */
function fetchedNote(value?: string): string {
  return value ? ` · 更新于 ${formatTime(value)}` : ''
}

/**
 * 把「即将发出的 query 对象」拼成给人看的串，显示在面板上。
 * 口径与 `api.ts buildUrl` 一致：**空值不发**（后端把空串当缺省）。
 */
function queryString(params: Record<string, string>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    const str = (value ?? '').trim()
    if (str === '') continue
    search.append(key, str)
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

/* ------------------------------------------------------------------ 契约键与列 */

/** 契约键（task 描述里冻结的那几个）。缺了就显示 `-`，不补 0。 */
const METRIC_KEYS: Record<TabKey, string[]> = {
  items: ['item_count'],
  tasks: ['running_count', 'total_count'],
  competitiveness: ['low_count', 'high_count', 'avg_score'],
  records: ['record_count'],
}

const METRIC_LABELS: Record<string, string> = {
  item_count: '商品数',
  running_count: '进行中任务',
  total_count: '任务总数',
  low_count: '低于竞品数',
  high_count: '高于竞品数',
  avg_score: '平均竞争力分',
  record_count: '投放记录数',
}

/**
 * 已知列：**命名线索**，不是断言。某列在所有行里都不存在时会被自动丢掉（不留一整列 `-`），
 * 平台真返回的其它键会补成列或进「其它字段」抽屉。
 */
const KNOWN_COLUMNS: Record<TabKey, Array<{ key: string; label: string }>> = {
  items: [
    { key: 'item_id', label: '商品 ID' },
    { key: 'title', label: '商品标题' },
    { key: 'price', label: '价格（元）' },
    { key: 'status', label: '状态' },
  ],
  tasks: [
    { key: 'task_id', label: '任务 ID' },
    { key: 'task_name', label: '任务名称' },
    { key: 'status', label: '状态' },
    { key: 'item_id', label: '商品 ID' },
  ],
  competitiveness: [
    { key: 'item_id', label: '商品 ID' },
    { key: 'title', label: '商品标题' },
    { key: 'score', label: '竞争力分' },
    { key: 'level', label: '档位' },
  ],
  records: [
    { key: 'record_id', label: '记录 ID' },
    { key: 'item_id', label: '商品 ID' },
    { key: 'status', label: '状态' },
    { key: 'gmt_create', label: '创建时间' },
  ],
}

/** 不在已知列里的键：先并入后端点名的 `unrecognized_keys`，再扫一遍真实数据。 */
function extraKeys(rows: Row[], knownKeys: string[], unrecognized: string[]): string[] {
  const known = new Set(knownKeys)
  const seen = new Set<string>()
  for (const key of unrecognized) {
    if (key && !known.has(key)) seen.add(key)
  }
  for (const row of rows) {
    for (const key of Object.keys(row || {})) {
      if (!known.has(key)) seen.add(key)
    }
  }
  return [...seen].sort()
}

/**
 * 列 = 真实存在的已知列 + 最多 6 个平台新键（单独成列更好读）+ 其余收进「其它字段」。
 * 这样平台改字段名时数据不会消失，也不会把表格撑成几十列。
 */
function buildColumns(
  rows: Row[],
  known: Array<{ key: string; label: string }>,
  unrecognized: string[],
): { columns: Array<{ key: string; label: string }>; extra: string[] } {
  const present = known.filter((col) => rows.some((row) => row?.[col.key] !== undefined))
  const others = extraKeys(rows, known.map((col) => col.key), unrecognized)
  const head = others.slice(0, 6).map((key) => ({ key, label: key }))
  return { columns: [...present, ...head], extra: others.slice(6) }
}

/** 某个 tab 对应的行数组：缺键时是 `null`，平台明说没有时是 `[]`。 */
function rowsOf(kind: TabKey, data: MarketingResult | null): Row[] | null | undefined {
  if (!data) return null
  if (kind === 'tasks') return data.tasks
  if (kind === 'records') return data.records
  return data.items
}

/* ------------------------------------------------------------------ 入参校验 */

/** `sku_prices` 前端校验：必须是 JSON 对象，且每个值都是数字。 */
type SkuCheck = { ok: true; json: string } | { ok: false; reason: string }

function checkSkuPrices(raw: string): SkuCheck {
  const trimmed = raw.trim()
  if (trimmed === '') return { ok: false, reason: '还没填 SKU 价格' }
  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch (err) {
    return { ok: false, reason: `不是合法 JSON：${err instanceof Error ? err.message : String(err)}` }
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, reason: '必须是 JSON 对象（键 = skuId，值 = 价格数字），例如 {"123": 9.9}' }
  }
  const entries = Object.entries(parsed as Record<string, unknown>)
  if (entries.length === 0) return { ok: false, reason: 'JSON 对象是空的，至少要有一个 skuId' }
  for (const [skuId, price] of entries) {
    if (typeof price !== 'number' || !Number.isFinite(price)) {
      return { ok: false, reason: `SKU「${skuId}」的价格必须是数字，现在是 ${JSON.stringify(price)}` }
    }
  }
  return { ok: true, json: JSON.stringify(parsed) }
}

/* ------------------------------------------------------------------ 小组件 */

function RefreshButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button className="btn sm" onClick={onClick} disabled={disabled}>
      <span className="mk-ico">
        <IconRefresh size={13} />
      </span>
      刷新
    </button>
  )
}

/** 平台提示：默认只露 2 条，其余折叠 —— 提示多的时候不该把内容挤下去。 */
function WarningList({ warnings }: { warnings: string[] }) {
  const [open, setOpen] = useState(false)
  if (warnings.length === 0) return null
  const shown = open ? warnings : warnings.slice(0, 2)
  return (
    <Notice kind="warn">
      <div>平台返回了 {warnings.length} 条提示：</div>
      <ul className="mk-warn-list">
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

/**
 * 指标卡：契约键**总是**渲染（缺了就显示 `-` + 「平台未返回」），
 * 平台多给的指标键追加在后面，一个都不丢。
 *
 * `asText`：接口 3 的返回值是**字符串**（`trafficBoostStr` 之类），按原文渲染，不做数字本地化。
 */
function MetricCards({
  kind,
  metrics,
  asText = false,
}: {
  kind: TabKey
  metrics?: Record<string, unknown> | null
  asText?: boolean
}) {
  const keys = METRIC_KEYS[kind]
  const render = (value: unknown) => (asText ? text(value) : metric(value))
  const extra = Object.keys(metrics || {})
    .filter((key) => !keys.includes(key))
    .sort()
  return (
    <>
      <div className="mk-cards">
        {keys.map((key) => (
          <Stat
            key={key}
            label={METRIC_LABELS[key] || key}
            value={render(metrics?.[key])}
            extra={isMissing(metrics?.[key]) ? <span className="dim">平台未返回</span> : undefined}
          />
        ))}
        {extra.map((key) => (
          <Stat key={key} label={key} value={render(metrics?.[key])} small extra={<span className="dim">平台附加指标</span>} />
        ))}
      </div>
      {asText && (
        <p className="mk-note">
          这个接口的返回值是<strong>字符串</strong>（官方 `trafficBoostStr` 之类），上面按原文显示，不做数字本地化。
        </p>
      )}
    </>
  )
}

/** `null`（缺键）和 `[]`（平台明说没有）是两件事，空态文案必须分开。 */
function ListBody({
  rows,
  columns,
  extra,
  kind,
}: {
  rows: Row[] | null | undefined
  columns: Array<{ key: string; label: string }>
  extra: string[]
  kind: TabKey
}) {
  const label = LIST_LABEL[kind]

  if (isMissing(rows)) {
    return (
      <div className="mk-empty">
        <Notice kind="warn">
          平台这次<strong>没有返回</strong>「{label}」列表（缺键或为 null）。这<strong>不等于</strong>「一条都没有」
          —— 可能是这个账号没有该营销微应用的权限，或者官方接口这次没带这个字段。别把它当成 0。
        </Notice>
      </div>
    )
  }

  if (!rows || rows.length === 0) {
    return (
      <div className="mk-empty">
        <span className="mk-empty-title">平台明确返回了空列表</span>
        <span className="mk-empty-desc">
          这个账号下当前没有{label}。要新建或修改营销内容，请去官方卖家工作台操作。
        </span>
      </div>
    )
  }

  if (columns.length === 0) {
    return (
      <Notice kind="warn">
        平台返回了 {rows.length} 条{label}，但每一条都没有可单独成列的字段。第一条原始内容：
        <code className="mk-raw">{JSON.stringify(rows[0])}</code>
      </Notice>
    )
  }

  return (
    <div className="table-wrap">
      <table className="mk-table">
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
                <td className="mk-extra">
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
    </div>
  )
}

/* ------------------------------------------------------------------ 面板 */

/**
 * 四个 tab 共用同一套渲染：指标卡 + 提示 + 三态表格 + （可选）分页 + 只读声明。
 * 差别只有「取哪个键当行数组」「有没有分页」「额外工具栏」，所以不复制四份。
 *
 * `ready=false` 时**不发请求**（`useAsync` 的 immediate=false），只显示 `hint` 说清缺什么。
 */
function ListPanel({
  kind,
  state,
  ready = true,
  hint,
  requestLine,
  page = 1,
  onPage,
  toolbar,
  showPager = false,
  metricAsText = false,
  children,
}: {
  kind: TabKey
  state: AsyncState<MarketingResult>
  ready?: boolean
  hint?: ReactNode
  requestLine?: string
  page?: number
  onPage?: (next: number) => void
  toolbar?: ReactNode
  showPager?: boolean
  metricAsText?: boolean
  /** 工具栏下面再补一行自定义输入（例如 `sku_prices` 的 JSON 文本框）。 */
  children?: ReactNode
}) {
  const data = state.data
  const rows = rowsOf(kind, data)
  const rowList = Array.isArray(rows) ? rows : []
  const { columns, extra } = buildColumns(rowList, KNOWN_COLUMNS[kind], data?.unrecognized_keys || [])
  const warnings = data?.warnings || []
  const unrecognized = data?.unrecognized_keys || []
  const size = Number(data?.page_size ?? PAGE_SIZE_DEFAULT)
  const hasNext = typeof data?.has_next === 'boolean' ? data.has_next : rowList.length >= (Number.isFinite(size) && size > 0 ? size : PAGE_SIZE_DEFAULT)

  return (
    <div className="card">
      <h3>
        {TABS.find((item) => item.key === kind)?.label || kind}
        <span className="hint">
          {data?.kind ? `kind=${data.kind}` : '按账号查'}
          {data?.source ? ` · 来源 ${data.source}` : ''}
          {fetchedNote(data?.fetched_at)}
        </span>
      </h3>

      <div className="mk-bar">
        {toolbar}
        <span className="mk-spacer" />
        <RefreshButton onClick={state.reload} disabled={state.loading || !ready} />
      </div>

      {requestLine && (
        <p className="mk-request dim mono">
          {/* 必填没填齐时这一行只是「拼出来的地址」，一个请求都没发 —— 不能写成「本次请求」，
              否则用户会以为后端已经返回了 400 */}
          {ready ? '本次请求：' : '将要请求（必填没填齐，还没发）：'}
          {requestLine}
        </p>
      )}

      {children}

      {!ready ? (
        <Notice kind="warn">{hint || '必填参数还没填齐，先不发请求。'}</Notice>
      ) : (
        <>
          <div className="mk-section-head">
            <span className="mk-section-title">
              <span className="mk-ico">
                <IconChart size={14} />
              </span>
              关键指标
            </span>
            <span className="dim">契约键缺失时显示 - ，不会用 0 顶替</span>
          </div>
          <MetricCards kind={kind} metrics={data?.metrics} asText={metricAsText} />

          <WarningList warnings={warnings} />

          {unrecognized.length > 0 && (
            <Notice kind="info">
              平台返回了契约里没写明的键：{unrecognized.join('、')}。它们已按原样列进表格（或收进「其它字段」列），没有丢弃。
            </Notice>
          )}

          <AsyncBox loading={state.loading} error={state.error} notReady={state.notReady} onRetry={state.reload}>
            <ListBody rows={rows} columns={columns} extra={extra} kind={kind} />

            {showPager && (
              <div className="mk-pager">
                <span className="dim">
                  第 {text(data?.page ?? page)} 页 · 本页 {count(rowList.length)} 条 · 共 {count(data?.total ?? data?.total_count)} 条
                </span>
                <span className="mk-spacer" />
                <button className="btn sm" disabled={state.loading || page <= 1} onClick={() => onPage?.(Math.max(1, page - 1))}>
                  上一页
                </button>
                <button className="btn sm" disabled={state.loading || !hasNext} onClick={() => onPage?.(page + 1)}>
                  下一页
                </button>
              </div>
            )}
          </AsyncBox>
        </>
      )}

      <Notice kind="info">
        只读页：这里<strong>不会</strong>创建或修改营销任务、也不会改商品价格 —— 要投放请去官方卖家工作台操作。
      </Notice>
    </div>
  )
}

/**
 * `/items`：**单商品一屏指标**（不是商品列表）。
 * 必填 `item_id`；`date_type` 四选一；`customDate` 才出现 `date_range`（`YYYYMMDD|YYYYMMDD`）。
 */
function ItemsPanel({ accountId }: { accountId: string }) {
  const [itemId, setItemId] = useState('')
  const [dateType, setDateType] = useState('recent1d')
  const [dateRange, setDateRange] = useState('')

  const trimmedId = itemId.trim()
  const trimmedRange = dateRange.trim()
  const isCustom = dateType === 'customDate'
  const rangeOk = !isCustom || DATE_RANGE_PATTERN.test(trimmedRange)
  const ready = trimmedId !== '' && rangeOk

  const query: Record<string, string> = { item_id: trimmedId, date_type: dateType }
  if (isCustom) query.date_range = trimmedRange

  const state = useAsync<MarketingResult>(
    () => api.get<MarketingResult>(`/api/marketing/${encodeURIComponent(accountId)}/items`, query),
    [accountId, trimmedId, dateType, trimmedRange],
    ready,
    ['items.updated'],
  )

  const hint = trimmedId === ''
    ? '商品 ID 是必填的（后端对空 item_id 直接回 400）。填上商品 ID 再查。'
    : '自定义区间要填成 YYYYMMDD|YYYYMMDD（例如 20260101|20260131）；非 customDate 时后端不接受 date_range。'

  return (
    <ListPanel
      kind="items"
      state={state}
      ready={ready}
      hint={hint}
      requestLine={`/api/marketing/${encodeURIComponent(accountId)}/items${queryString(query)}`}
      toolbar={
        <>
          <Field label="商品 ID（必填）" tip="官方键名是 itmId；单个商品，不是列表">
            <input type="text" value={itemId} placeholder="例如 123456789" onChange={(event) => setItemId(event.target.value)} />
          </Field>
          <Field label="时间范围" tip="recent1d 是官方缺省值">
            <select value={dateType} onChange={(event) => setDateType(event.target.value)}>
              {DATE_TYPES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
          {isCustom && (
            <Field label="自定义区间（YYYYMMDD|YYYYMMDD）" tip="官方 UI 只允许今天往前 90 天以内">
              <input type="text" value={dateRange} placeholder="20260101|20260131" onChange={(event) => setDateRange(event.target.value)} />
            </Field>
          )}
        </>
      }
    />
  )
}

/** `/tasks`：`biz_scene` + `query_scene` + `page` + `size`(1~50) + `item_ids`（逗号分隔）。 */
function TasksPanel({ accountId }: { accountId: string }) {
  const [bizScene, setBizScene] = useState('TRAFFIC_ORDER_BOOST')
  const [queryScene, setQueryScene] = useState(DEFAULT_QUERY_SCENE)
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(String(PAGE_SIZE_DEFAULT))
  const [itemIds, setItemIds] = useState('')

  const sizeNum = Number(size)
  const sizeOk = size.trim() !== '' && Number.isFinite(sizeNum) && sizeNum >= 1 && sizeNum <= PAGE_SIZE_MAX
  const ready = sizeOk

  const query: Record<string, string> = {
    biz_scene: bizScene,
    query_scene: queryScene,
    page: String(page),
    size: size.trim(),
    item_ids: itemIds.trim(),
  }

  const state = useAsync<MarketingResult>(
    () => api.get<MarketingResult>(`/api/marketing/${encodeURIComponent(accountId)}/tasks`, query),
    [accountId, bizScene, queryScene, page, size, itemIds],
    ready,
    ['items.updated'],
  )

  return (
    <ListPanel
      kind="tasks"
      state={state}
      ready={ready}
      hint={`每页条数只能是 1~${PAGE_SIZE_MAX} 的整数（官方缺省 ${PAGE_SIZE_DEFAULT}）。现在是「${size}」。`}
      requestLine={`/api/marketing/${encodeURIComponent(accountId)}/tasks${queryString(query)}`}
      page={page}
      onPage={setPage}
      showPager
      toolbar={
        <>
          <Field label="业务场景" tip="官方枚举只有这 2 个值">
            <select value={bizScene} onChange={(event) => setBizScene(event.target.value)}>
              {BIZ_SCENES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="查询场景" tip="官方 15 个 queryScene，逐字抄自协议层；非法值后端会回带候选值的中文 400">
            <select value={queryScene} onChange={(event) => setQueryScene(event.target.value)}>
              {QUERY_SCENES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </Field>
          <Field label="每页条数" tip={`1~${PAGE_SIZE_MAX}，官方缺省 ${PAGE_SIZE_DEFAULT}`}>
            <input
              type="number"
              min={1}
              max={PAGE_SIZE_MAX}
              value={size}
              onChange={(event) => {
                setSize(event.target.value)
                setPage(1)
              }}
            />
          </Field>
          <Field label="商品 ID（可选）" tip="逗号分隔，例如 123,456；留空表示不按商品过滤">
            <input
              type="text"
              value={itemIds}
              placeholder="123,456"
              onChange={(event) => {
                setItemIds(event.target.value)
                setPage(1)
              }}
            />
          </Field>
        </>
      }
    />
  )
}

/** `/records`：必填 `task_id`；`query_scene` 缺省 `ORDER_BOOST_OPERATION_RECORD`；**没有分页**。 */
function RecordsPanel({ accountId }: { accountId: string }) {
  const [taskId, setTaskId] = useState('')
  const [bizScene, setBizScene] = useState('TRAFFIC_ORDER_BOOST')
  const [queryScene, setQueryScene] = useState(RECORD_QUERY_SCENE)

  const trimmedTask = taskId.trim()
  const ready = trimmedTask !== ''

  const query: Record<string, string> = { task_id: trimmedTask, biz_scene: bizScene, query_scene: queryScene }

  const state = useAsync<MarketingResult>(
    () => api.get<MarketingResult>(`/api/marketing/${encodeURIComponent(accountId)}/records`, query),
    [accountId, trimmedTask, bizScene, queryScene],
    ready,
    ['items.updated'],
  )

  return (
    <ListPanel
      kind="records"
      state={state}
      ready={ready}
      hint="任务 ID 是必填的（后端对空 task_id 直接回 400）。任务 ID 可以从「营销任务」tab 的列表里拿。"
      requestLine={`/api/marketing/${encodeURIComponent(accountId)}/records${queryString(query)}`}
      toolbar={
        <>
          <Field label="任务 ID（必填）" tip="投放记录是按任务查的；官方缺省 queryScene 是 ORDER_BOOST_OPERATION_RECORD">
            <input type="text" value={taskId} placeholder="例如 987654321" onChange={(event) => setTaskId(event.target.value)} />
          </Field>
          <Field label="业务场景" tip="官方枚举只有这 2 个值">
            <select value={bizScene} onChange={(event) => setBizScene(event.target.value)}>
              {BIZ_SCENES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="查询场景" tip="这个接口官方只用一个值：ORDER_BOOST_OPERATION_RECORD">
            <input type="text" value={queryScene} onChange={(event) => setQueryScene(event.target.value)} />
          </Field>
        </>
      }
    />
  )
}

/**
 * `/price-competitiveness`：必填 `item_id`；`sku_prices` 是 JSON 对象字符串（前端先校验）；
 * `commission_rate` **仅** `TRAFFIC_ORDER_BOOST` 支持；响应值是**字符串**，按原文渲染。
 */
function CompetitivenessPanel({ accountId }: { accountId: string }) {
  const [itemId, setItemId] = useState('')
  const [bizScene, setBizScene] = useState('TRAFFIC_ORDER_BOOST')
  const [skuRaw, setSkuRaw] = useState('')
  const [commission, setCommission] = useState('')

  const trimmedId = itemId.trim()
  const sku = checkSkuPrices(skuRaw)
  const supportsCommission = bizScene === 'TRAFFIC_ORDER_BOOST'
  const commissionNum = Number(commission)
  const commissionOk = !supportsCommission || commission.trim() === '' || (Number.isFinite(commissionNum) && commissionNum >= 0)
  const ready = trimmedId !== '' && sku.ok && commissionOk

  const query: Record<string, string> = { biz_scene: bizScene, item_id: trimmedId }
  if (sku.ok) query.sku_prices = sku.json
  if (supportsCommission && commission.trim() !== '') query.commission_rate = commission.trim()

  const state = useAsync<MarketingResult>(
    () => api.get<MarketingResult>(`/api/marketing/${encodeURIComponent(accountId)}/price-competitiveness`, query),
    [accountId, bizScene, trimmedId, sku.ok ? sku.json : '', commission],
    ready,
    ['items.updated'],
  )

  const reasons: string[] = []
  if (trimmedId === '') reasons.push('商品 ID 是必填的（后端对空 item_id 直接回 400）')
  if (!sku.ok) reasons.push(`SKU 价格：${sku.reason}`)
  if (!commissionOk) reasons.push(`佣金比例必须是数字（现在是「${commission}」），而且只有 TRAFFIC_ORDER_BOOST 支持这个参数`)

  return (
    <ListPanel
      kind="competitiveness"
      state={state}
      ready={ready}
      metricAsText
      hint={<span>先补齐这些再查，不会白发一次请求：{reasons.join('；')}。</span>}
      requestLine={`/api/marketing/${encodeURIComponent(accountId)}/price-competitiveness${queryString(query)}`}
      toolbar={
        <>
          <Field label="商品 ID（必填）" tip="单个商品">
            <input type="text" value={itemId} placeholder="例如 123456789" onChange={(event) => setItemId(event.target.value)} />
          </Field>
          <Field label="业务场景" tip="commission_rate 只有 TRAFFIC_ORDER_BOOST 支持">
            <select value={bizScene} onChange={(event) => setBizScene(event.target.value)}>
              {BIZ_SCENES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
          {supportsCommission && (
            <Field label="佣金比例（可选）" tip="仅 TRAFFIC_ORDER_BOOST 支持；留空则不发这个参数">
              <input type="number" min={0} step="0.01" value={commission} onChange={(event) => setCommission(event.target.value)} />
            </Field>
          )}
        </>
      }
    >
      <div className="mk-field-wide">
        <Field
          label="SKU 价格（必填，JSON 对象）"
          tip='键 = skuId，值 = 价格（元，数字）。例如 {"123": 9.9}；前端会先 JSON.parse 校验'
        >
          <textarea
            className="mk-textarea"
            value={skuRaw}
            placeholder={'{"123": 9.9, "456": 19.9}'}
            onChange={(event) => setSkuRaw(event.target.value)}
          />
        </Field>
        <p className="mk-note">
          {sku.ok ? 'JSON 校验通过，会把规范化后的对象字符串发给后端。' : `暂不发请求：${sku.reason}。`}
        </p>
      </div>
    </ListPanel>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function MarketingPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [tab, setTab] = useState<TabKey>('items')

  /** 选中账号 = 用户选的优先，否则退回第一个（派生值，不用 useEffect 回写）。 */
  const accountId = picked || accounts.data?.[0]?.id || ''

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">营销</h2>
          <p className="page-desc">
            官方营销微应用：单商品指标、营销任务、价格竞争力与投放记录。按账号查，全部只读，不会向平台提交任何东西。
            「商品指标」「价格竞争力」要填商品 ID，「投放记录」要填任务 ID —— 缺必填项时不会发请求。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="mk-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="mk-bar">
          <Field label="账号" tip="营销数据是按账号查的，一个账号一套数据">
            <select
              value={accountId}
              onChange={(event) => setPicked(event.target.value)}
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
          <span className="mk-spacer" />
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
          <Notice kind="info">先在上方选一个账号：营销数据都是按账号查的，没有账号就没数据可看。</Notice>
        </div>
      ) : tab === 'items' ? (
        <ItemsPanel key={`items-${accountId}`} accountId={accountId} />
      ) : tab === 'tasks' ? (
        <TasksPanel key={`tasks-${accountId}`} accountId={accountId} />
      ) : tab === 'competitiveness' ? (
        <CompetitivenessPanel key={`cmp-${accountId}`} accountId={accountId} />
      ) : (
        <RecordsPanel key={`records-${accountId}`} accountId={accountId} />
      )}
    </div>
  )
}
