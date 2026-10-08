/**
 * 财务页：收入账单 / 支出账单 / 申请发票 / 开票信息（**只读**）。
 *
 * 后端契约（`backend/app/api/finance.py`，形状由 task-48 冻结，前端不得改）：
 *  - `GET /api/finance/{account_id}/income?from=YYYYMM&to=YYYYMM&biz_type=&page=1&size=20`
 *  - `GET /api/finance/{account_id}/expense?`（同形状）
 *  - `GET /api/finance/{account_id}/invoices?page=1&size=20&biz_types=a,b`
 *  - `GET /api/finance/{account_id}/purchaser`
 * 统一信封 `{success:true,data:{...}}`；失败 `{success:false,code,message}`。
 *
 * 两条不能破的口径：
 *  1. **数字拿不到一律显示 `-`，绝不补 0**。0.00 是真实金额（比如这个月没收入），
 *     拿不到和 0 是两件事，混在一起用户会以为账对不上。见 `money()` / `count()`。
 *  2. **本页没有任何写操作**。开票申请在官方工作台提交，这里只读 —— 页面上明写。
 *
 * 不轮询：刷新一律走 `useAsync().reload()`（全站约定，唯一允许的轮询在账号页扫码）。
 *
 * 样式来自同目录 `finance.css`（前缀 `fn-`），只依赖 `styles.css` 的令牌变量与
 * `.card`/`.tabs`/`.tab`/`.btn`/`.badge`/`.kv`/`.table-wrap` 这些稳定公共 class。
 */

import { useMemo, useState } from 'react'
import { api } from '../api'
import { AsyncBox, Badge, Field, Notice, Stat } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconClock, IconInfo, IconRefresh, IconTag } from '../icons'
import type { Account } from '../types'
import './finance.css'

/** 分页大小：与后端默认一致，写死成常量避免两处各写一个数。 */
const PAGE_SIZE = 20

type TabKey = 'income' | 'expense' | 'invoice' | 'purchaser'

const TABS: Array<{ key: TabKey; label: string; tip: string }> = [
  { key: 'income', label: '收入账单', tip: '按账单周期汇总的收入流水（只读）' },
  { key: 'expense', label: '支出账单', tip: '平台服务费等扣费流水（只读）' },
  { key: 'invoice', label: '申请发票', tip: '可开票金额与已申请记录。本页只读，不提交开票申请' },
  { key: 'purchaser', label: '开票信息', tip: '发票抬头、纳税类型与支持的票种（只读）' },
]

interface BizCategory {
  code?: string
  name?: string
  children?: Array<{ code?: string; name?: string }>
}

interface BillRow {
  bill_cycle?: string
  bill_no?: string
  biz_name?: string
  biz_type?: string
  amount?: number | null
  income?: number | null
  income_refund?: number | null
  expense?: number | null
  expense_refund?: number | null
  currency?: string
  agg_counts?: number | null
}

interface BillSummary {
  currency?: string
  income?: number | null
  income_refund?: number | null
  expense?: number | null
  expense_refund?: number | null
  net_income?: number | null
  net_expense?: number | null
  agg_counts?: number | null
}

interface BillResult {
  account_id?: string
  bill_code?: string
  bill_name?: string
  period?: { from?: string; to?: string }
  biz_type?: string
  summary?: BillSummary | null
  bills?: BillRow[]
  total?: number | null
  has_next?: boolean
  page?: number | null
  size?: number | null
  biz_categories?: BizCategory[]
  fetched_at?: string
  source?: string
}

interface InvoiceBill {
  id?: string
  bill_cycle?: string
  item_name?: string
  can_invoice_amount?: number | null
  invoice_amount_with_tax?: number | null
  unable_amount?: number | null
  tax_rate?: number | string | null
  company_name?: string
  company_tax_no?: string
  company_code?: string
  status?: string
  deal_status?: string
  choice_type?: string
  gmt_create?: string
}

interface InvoiceGroup {
  biz_type?: string
  biz_type_name?: string
  total_amount?: number | null
  bills?: InvoiceBill[]
}

/**
 * `requests` 在 task-48 的契约里只写了 `[...]`（形状未冻结），所以这里**只读几个
 * 大概率存在的键**，取不到就显示 `-` —— 不猜结构、不造字段，等形状定下来再收紧。
 */
type InvoiceRequestRow = Record<string, unknown>

interface InvoiceResult {
  account_id?: string
  groups?: InvoiceGroup[]
  total_amount?: number | null
  total_count?: number | null
  page?: number | null
  size?: number | null
  has_next?: boolean
  requests?: InvoiceRequestRow[]
  request_total?: number | null
  fetched_at?: string
  source?: string
}

interface PurchaserResult {
  account_id?: string
  purchaser_name?: string
  purchaser_type?: string
  support_invoice_types?: string[]
  fetched_at?: string
  source?: string
}

/** 当前月份 `YYYYMM`：账期输入的默认值。 */
function currentMonth(): string {
  const now = new Date()
  return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}`
}

/** 账期必须是合法的 `YYYYMM`。不合法就不发请求，免得后端回一句用户看不懂的 400。 */
function isMonth(value: string): boolean {
  if (!/^\d{6}$/.test(value)) return false
  const month = Number(value.slice(4, 6))
  return month >= 1 && month <= 12
}

/**
 * 金额：**拿不到就是 `-`，绝不补 0**。拿得到时固定两位小数（财务数字不对齐没法看）。
 */
function money(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 计数：同 `money()`，拿不到就是 `-`。 */
function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

/** 文本：空串与 null 一律显示 `-`，不留空白单元格。 */
function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const str = String(value).trim()
  return str === '' ? '-' : str
}

/** 页头「更新于」：只有真拿到 `fetched_at` 才显示，否则不写时间。 */
function fetchedNote(value?: string): string {
  return value ? ` · 更新于 ${formatTime(value)}` : ''
}

/** 只读提示：四个 tab 共用同一句话，避免每个 tab 各写一版。 */
function ReadOnlyNotice() {
  return (
    <Notice kind="info">
      只读页：这里<strong>不会</strong>提交开票申请、也不会改平台上的任何数据 —— 要开票请去官方卖家工作台操作。
    </Notice>
  )
}

function RefreshButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button className="btn sm" onClick={onClick} disabled={disabled}>
      <span className="fn-ico">
        <IconRefresh size={13} />
      </span>
      刷新
    </button>
  )
}

/* ------------------------------------------------------------------ 账单（收入 / 支出） */

function BillPanel({ kind, accountId }: { kind: 'income' | 'expense'; accountId: string }) {
  const isIncome = kind === 'income'
  const [from, setFrom] = useState(currentMonth())
  const [to, setTo] = useState(currentMonth())
  const [bizType, setBizType] = useState('')
  const [page, setPage] = useState(1)

  const periodOk = isMonth(from) && isMonth(to)

  const bills = useAsync<BillResult>(
    () =>
      api.get<BillResult>(`/api/finance/${encodeURIComponent(accountId)}/${kind}`, {
        from,
        to,
        biz_type: bizType,
        page,
        size: PAGE_SIZE,
      }),
    [accountId, kind, from, to, bizType, page],
    // 账期没填对就不发请求：后端只会回一句参数校验失败，不如就地告诉他哪里不对。
    periodOk,
  )

  const data = bills.data
  const summary = data?.summary ?? null
  const rows = data?.bills ?? []
  const categories = data?.biz_categories ?? []

  /** 金额列取哪种口径：收入页看 `income`，支出页看 `expense`；后端另有通用 `amount` 兜底。 */
  const amountOf = (row: BillRow) => (isIncome ? row.income ?? row.amount : row.expense ?? row.amount)
  const refundOf = (row: BillRow) => (isIncome ? row.income_refund : row.expense_refund)

  return (
    <div className="card">
      <h3>
        {isIncome ? '收入账单' : '支出账单'}
        <span className="hint">
          {text(data?.bill_name)} · 账期 {text(data?.period?.from)} ~ {text(data?.period?.to)}
          {fetchedNote(data?.fetched_at)}
        </span>
      </h3>

      <div className="fn-bar">
        <Field label="起始账期" tip="格式 YYYYMM，例如 202610">
          <input
            type="text"
            value={from}
            maxLength={6}
            placeholder="YYYYMM"
            onChange={(e) => {
              setFrom(e.target.value.trim())
              setPage(1)
            }}
            className={isMonth(from) ? '' : 'fn-invalid'}
          />
        </Field>
        <Field label="结束账期" tip="格式 YYYYMM；和起始账期一起决定统计区间">
          <input
            type="text"
            value={to}
            maxLength={6}
            placeholder="YYYYMM"
            onChange={(e) => {
              setTo(e.target.value.trim())
              setPage(1)
            }}
            className={isMonth(to) ? '' : 'fn-invalid'}
          />
        </Field>
        <Field label="业务大类" tip="来自平台返回的账单大类；「全部」= 不筛">
          <select
            value={bizType}
            onChange={(e) => {
              setBizType(e.target.value)
              setPage(1)
            }}
          >
            <option value="">全部</option>
            {categories.map((category) => {
              const code = category.code ?? ''
              const children = category.children ?? []
              if (children.length === 0) {
                return (
                  <option key={code || category.name} value={code}>
                    {text(category.name)}
                  </option>
                )
              }
              return (
                <optgroup key={code || category.name} label={text(category.name)}>
                  <option value={code}>{text(category.name)}（全部）</option>
                  {children.map((child) => (
                    <option key={child.code || child.name} value={child.code ?? ''}>
                      {text(child.name)}
                    </option>
                  ))}
                </optgroup>
              )
            })}
          </select>
        </Field>
        <span className="fn-spacer" />
        <RefreshButton onClick={bills.reload} disabled={bills.loading || !periodOk} />
      </div>

      {!periodOk && (
        <Notice kind="warn">账期要填 6 位数字的「年 + 月」，例如 202610。填对了才会去查账单。</Notice>
      )}

      <div className="fn-cards">
        {isIncome ? (
          <>
            <Stat label="收入金额合计" value={money(summary?.income)} extra={`币种 ${text(summary?.currency)}`} />
            <Stat label="退款金额合计" value={money(summary?.income_refund)} />
          </>
        ) : (
          <>
            <Stat label="扣费金额合计" value={money(summary?.expense)} extra={`币种 ${text(summary?.currency)}`} />
            <Stat label="退款金额合计" value={money(summary?.expense_refund)} />
          </>
        )}
        <Stat label="流水笔数" value={count(summary?.agg_counts)} small />
      </div>

      <AsyncBox
        loading={bills.loading}
        error={bills.error}
        notReady={bills.notReady}
        empty={periodOk && rows.length === 0}
        emptyTitle="这个账期没有账单流水"
        emptyDesc="换个账期或把「业务大类」切回「全部」再看看。金额拿不到时后端会给 null，这里一律显示 - 而不是 0。"
        emptyIcon="chart"
        onRetry={bills.reload}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>账单周期</th>
                <th>业务大类</th>
                <th className="num">{isIncome ? '收入合计（元）' : '扣费合计（元）'}</th>
                <th className="num">退款合计（元）</th>
                <th className="num">流水笔数</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={row.bill_no || `${row.bill_cycle}-${index}`}>
                  <td className="nowrap">{text(row.bill_cycle)}</td>
                  <td>{text(row.biz_name)}</td>
                  <td className="num mono">{money(amountOf(row))}</td>
                  <td className="num mono">{money(refundOf(row))}</td>
                  <td className="num mono">{count(row.agg_counts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="fn-pager">
          <span className="dim">
            第 {text(data?.page ?? page)} 页 · 本页 {rows.length} 条 · 共 {count(data?.total)} 条
          </span>
          <span className="fn-spacer" />
          <button className="btn sm" disabled={bills.loading || page <= 1} onClick={() => setPage((n) => Math.max(1, n - 1))}>
            上一页
          </button>
          <button
            className="btn sm"
            disabled={bills.loading || !data?.has_next}
            onClick={() => setPage((n) => n + 1)}
          >
            下一页
          </button>
        </div>
      </AsyncBox>

      <ReadOnlyNotice />
    </div>
  )
}

/* ------------------------------------------------------------------ 申请发票 */

function InvoicePanel({ accountId }: { accountId: string }) {
  const [page, setPage] = useState(1)
  const invoices = useAsync<InvoiceResult>(
    () =>
      api.get<InvoiceResult>(`/api/finance/${encodeURIComponent(accountId)}/invoices`, {
        page,
        size: PAGE_SIZE,
      }),
    [accountId, page],
  )

  const data = invoices.data
  const groups = data?.groups ?? []
  const requests = data?.requests ?? []

  /** 把「按业务类型分组」摊平成表格行，同时记住它属于哪个业务类型。 */
  const rows = useMemo(
    () => groups.flatMap((group) => (group.bills ?? []).map((bill) => ({ group, bill }))),
    [groups],
  )

  return (
    <div className="card">
      <h3>
        申请发票
        <span className="hint">
          可开票金额按业务类型汇总{fetchedNote(data?.fetched_at)}
        </span>
      </h3>

      <div className="fn-bar">
        <span className="fn-legend">
          这里的金额是<strong>平台算出来的可开票额度</strong>，本页不提交申请。
        </span>
        <span className="fn-spacer" />
        <RefreshButton onClick={invoices.reload} disabled={invoices.loading} />
      </div>

      <div className="fn-section-head">
        <span className="fn-section-title">
          <span className="fn-ico">
            <IconTag size={14} />
          </span>
          待申请发票
        </span>
      </div>

      <AsyncBox
        loading={invoices.loading}
        error={invoices.error}
        notReady={invoices.notReady}
        empty={rows.length === 0}
        emptyTitle="现在没有可开票的账单"
        emptyDesc="有可开票账单时，平台会在这里按业务类型列出来。"
        emptyIcon="tag"
        onRetry={invoices.reload}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>业务类型</th>
                <th className="num">可开票金额（元）</th>
                <th>所属公司</th>
                <th className="num">税率</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ group, bill }, index) => (
                <tr key={bill.id || `${group.biz_type}-${index}`}>
                  <td>
                    <div className="cell-main">{text(group.biz_type_name ?? group.biz_type)}</div>
                    {bill.item_name ? <div className="cell-sub">{bill.item_name}</div> : null}
                  </td>
                  <td className="num mono">{money(bill.can_invoice_amount)}</td>
                  <td>{text(bill.company_name)}</td>
                  <td className="num mono" title="平台返回的原始税率值，前端不做换算">
                    {text(bill.tax_rate)}
                  </td>
                  <td>
                    {bill.status ? <Badge tone="muted">{String(bill.status)}</Badge> : <span className="dim">-</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="fn-total">
          <span className="fn-total-label">
            <span className="fn-ico">
              <IconChart size={14} />
            </span>
            开票金额合计
          </span>
          <span className="fn-total-value mono">{money(data?.total_amount)}</span>
          <span className="dim">共 {count(data?.total_count)} 条</span>
        </div>

        <div className="fn-pager">
          <span className="dim">第 {text(data?.page ?? page)} 页</span>
          <span className="fn-spacer" />
          <button
            className="btn sm"
            disabled={invoices.loading || page <= 1}
            onClick={() => setPage((n) => Math.max(1, n - 1))}
          >
            上一页
          </button>
          <button
            className="btn sm"
            disabled={invoices.loading || !data?.has_next}
            onClick={() => setPage((n) => n + 1)}
          >
            下一页
          </button>
        </div>
      </AsyncBox>

      <div className="fn-section-head">
        <span className="fn-section-title">
          <span className="fn-ico">
            <IconClock size={14} />
          </span>
          已申请发票
        </span>
        <span className="fn-section-desc">共 {count(data?.request_total)} 条</span>
      </div>

      {requests.length === 0 ? (
        <p className="dim">还没有申请记录。</p>
      ) : (
        <div className="fn-list">
          {requests.map((request, index) => (
            <div className="fn-list-row" key={String(request.id ?? index)}>
              <span className="fn-list-main">
                {text(request.biz_type_name ?? request.biz_type ?? request.item_name ?? request.bill_cycle)}
              </span>
              <span className="mono">{money(request.can_invoice_amount ?? request.amount ?? request.invoice_amount_with_tax)}</span>
              <span className="dim">{text(request.status ?? request.deal_status)}</span>
            </div>
          ))}
        </div>
      )}

      <ReadOnlyNotice />
    </div>
  )
}

/* ------------------------------------------------------------------ 开票信息 */

function PurchaserPanel({ accountId }: { accountId: string }) {
  const purchaser = useAsync<PurchaserResult>(
    () => api.get<PurchaserResult>(`/api/finance/${encodeURIComponent(accountId)}/purchaser`),
    [accountId],
  )

  const data = purchaser.data
  const types = data?.support_invoice_types ?? []

  return (
    <div className="card">
      <h3>
        开票信息
        <span className="hint">发票抬头与支持的票种{fetchedNote(data?.fetched_at)}</span>
      </h3>

      <div className="fn-bar">
        <span className="fn-legend">这些是账号在平台上登记的抬头信息，本页只读。</span>
        <span className="fn-spacer" />
        <RefreshButton onClick={purchaser.reload} disabled={purchaser.loading} />
      </div>

      <AsyncBox
        loading={purchaser.loading}
        error={purchaser.error}
        notReady={purchaser.notReady}
        onRetry={purchaser.reload}
      >
        <div className="kv">
          <div>
            <span className="k">发票抬头</span>
            <span className="v">{text(data?.purchaser_name)}</span>
          </div>
          <div>
            <span className="k">纳税类型</span>
            <span className="v">{text(data?.purchaser_type)}</span>
          </div>
          <div>
            <span className="k">支持票种</span>
            <span className="v">
              {types.length === 0 ? (
                <span className="dim">-</span>
              ) : (
                <span className="fn-tags">
                  {types.map((item) => (
                    <span className="fn-tag" key={item}>
                      {item}
                    </span>
                  ))}
                </span>
              )}
            </span>
          </div>
        </div>
      </AsyncBox>

      <ReadOnlyNotice />
    </div>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function FinancePage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [tab, setTab] = useState<TabKey>('income')

  /**
   * 选中账号 = 「用户选的」优先，否则退回第一个账号。
   * 不用 `useEffect` 回写 state：账号列表是异步来的，回写会让首帧多渲染一次，
   * 而且用户切到「（未选）」时会被悄悄改回去 —— 派生值更诚实。
   */
  const accountId = picked || accounts.data?.[0]?.id || ''

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">财务</h2>
          <p className="page-desc">
            收入 / 支出账单、可开票金额与发票抬头。数据按账号查，只读，不会向平台提交任何东西。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="fn-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="fn-bar">
          <Field label="账号" tip="账单与发票都是按账号查的，一个账号一套数据">
            <select value={accountId} onChange={(e) => setPicked(e.target.value)} disabled={accounts.loading}>
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
          <span className="fn-spacer" />
        </div>
        {accounts.notReady && (
          <Notice kind="warn">
            <span className="fn-ico">
              <IconInfo size={14} />
            </span>
            账号接口还没就绪，暂时选不了账号；页面其余部分可正常使用。
          </Notice>
        )}
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
          <Notice kind="info">先在上方选一个账号：账单和发票都是按账号查的，没有账号就没数据可看。</Notice>
        </div>
      ) : tab === 'income' ? (
        <BillPanel key="income" kind="income" accountId={accountId} />
      ) : tab === 'expense' ? (
        <BillPanel key="expense" kind="expense" accountId={accountId} />
      ) : tab === 'invoice' ? (
        <InvoicePanel accountId={accountId} />
      ) : (
        <PurchaserPanel accountId={accountId} />
      )}
    </div>
  )
}

/**
 * 只保留具名导出：本仓 10 个页面（含本页）都是 `export function XxxPage()`，
 * `App.tsx` 也一律按具名 import —— 多一个默认导出只会让人以为有两种用法。
 */
