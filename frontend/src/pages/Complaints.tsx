/**
 * 交易 → **投诉**（只读）。契约 `docs/contracts/trade.md` §2.5。
 *
 * 这一页和前面几页有个本质区别，先写清楚免得后面踩坑：
 *  1. **没有真机样本** —— 真机上平台回 `NO_PERMISSION::用户没有权限` → 后端 502 `protocol_error`。
 *     所以元素是**原样透传**，字段长什么样契约没有保证，下面全部走防御式取值。
 *  2. 因为读不到数据，这里**绝不能**用空列表假装「没有投诉」：拿不到就是错误态，照实显示。
 *  3. 那个 ret **不写 `need_relogin`**，所以「去重新扫码」是错误引导 —— 这不是掉线。
 */

import { useState } from 'react'
import { api } from '../api'
import { Badge, Field, Notice } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconShield } from '../icons'
import {
  AccountBar,
  TradeHead,
  TradeListView,
  dayOf,
  ellipsis,
  isNeedRelogin,
  isPermissionDenied,
  money,
  num,
  useTradeAccounts,
  when,
  type TradeColumn,
  type TradeEnvelope,
} from './Orders'

/**
 * 平台原样透传，没有冻结的字段表 —— 用索引签名而不是编一套 interface。
 * 想读的字段一律经 `pick()` / 本地 `read()`，读不到给 `null`，渲染成 `-`。
 */
type RawComplaint = Record<string, unknown>

interface ComplaintsResult extends TradeEnvelope<RawComplaint> {
  complaints?: RawComplaint[]
  /** 状态计数，平台给什么键就是什么键，不猜中文。 */
  status_count_map?: Record<string, number | null> | null
}

function read(row: RawComplaint, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = row[key]
    if (value !== undefined && value !== null && value !== '') return value
  }
  return null
}

function toneForStatus(status: string): 'ok' | 'warn' | 'danger' | 'muted' {
  if (status.includes('完成') || status.includes('关闭') || status.includes('结束')) return 'ok'
  if (status.includes('处理') || status.includes('进行') || status.includes('待')) return 'warn'
  if (status.includes('失败') || status.includes('拒绝') || status.includes('超时')) return 'danger'
  return 'muted'
}

export function ComplaintsPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const pageSize = 10

  const state = useAsync<ComplaintsResult>(
    () => api.get<ComplaintsResult>(`/api/trade/${encodeURIComponent(accountId)}/complaints`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.complaints ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)
  const counts = data?.status_count_map && typeof data.status_count_map === 'object'
    ? Object.entries(data.status_count_map)
    : []

  const columns: Array<TradeColumn<RawComplaint>> = [
    {
      key: 'complaint_id',
      label: '投诉单',
      sortValue: (row) => {
        const value = read(row, 'complaint_id', 'id', 'case_id', 'cid')
        const n = Number(value)
        return Number.isFinite(n) ? n : value === null ? null : String(value)
      },
      render: (row) => {
        const id = read(row, 'complaint_id', 'id', 'case_id', 'cid')
        const order = read(row, 'order_id', 'oid')
        return (
          <div className="tr-cell">
            <span className="tr-mono">{money(id)}</span>
            <span className="tr-sub">订单 {money(order)}</span>
          </div>
        )
      },
    },
    {
      key: 'status',
      label: '状态',
      sortValue: (row) => String(read(row, 'status_text', 'status', 'complaint_status') ?? ''),
      render: (row) => {
        const text = String(read(row, 'status_text', 'status', 'complaint_status') ?? '')
        return (
          <div className="tr-cell">
            <Badge tone={text ? toneForStatus(text) : 'muted'}>{text || '-'}</Badge>
            <span className="tr-sub">状态码 {money(read(row, 'status_code', 'state'))}</span>
          </div>
        )
      },
    },
    {
      key: 'item',
      label: '商品 / 对方',
      sortValue: (row) => String(read(row, 'item_title', 'title', 'item_name') ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-name" title={String(read(row, 'item_title', 'title', 'item_name') ?? '')}>
            {ellipsis(String(read(row, 'item_title', 'title', 'item_name') ?? ''), 26) || '-'}
          </span>
          <span className="tr-sub">{money(read(row, 'buyer_nick', 'other_nick', 'nick'))}</span>
        </div>
      ),
    },
    {
      key: 'reason',
      label: '原因',
      sortValue: (row) => String(read(row, 'reason', 'complaint_reason', 'type', 'reason_text') ?? ''),
      render: (row) => {
        const text = String(read(row, 'reason', 'complaint_reason', 'type', 'reason_text') ?? '')
        return <span className="tr-name" title={text}>{text || '-'}</span>
      },
    },
    {
      key: 'money',
      label: '金额（元）',
      num: true,
      sortValue: (row) => {
        const value = read(row, 'amount', 'money', 'complaint_amount', 'refund_fee')
        if (value === null) return null
        const n = Number(value)
        return Number.isFinite(n) ? n : null
      },
      render: (row) => (
        <span className="tr-money">{money(read(row, 'amount', 'money', 'complaint_amount', 'refund_fee'))}</span>
      ),
    },
    {
      key: 'time',
      label: '时间',
      sortValue: (row) => {
        const n = Number(read(row, 'gmt_create', 'create_time', 'gmt_modified', 'apply_time'))
        return Number.isFinite(n) && n > 0 ? n : null
      },
      render: (row) => <span>{when(read(row, 'gmt_create', 'create_time', 'gmt_modified', 'apply_time'))}</span>,
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="投诉"
        desc={
          <>
            买家投诉工单 · 只读，处理投诉在平台操作
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar accounts={accounts} picked={picked} setPicked={(id) => { setPicked(id); setPage(1) }} />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没投诉工单可看。</Notice>}

      {permission && (
        <Notice kind="warn">
          平台回了 <code>NO_PERMISSION::用户没有权限</code>，这个账号读不到投诉工单。
          这是平台给的业务结果，<strong>不是掉线、不用重新扫码</strong> ——
          要能读得换一个有卖家工作台会话的账号。下面照实显示错误，不会拿空列表冒充「没有投诉」。
        </Notice>
      )}
      {relogin && !permission && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查投诉 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}

      {counts.length > 0 && (
        <section className="card tr-toolbar">
          <div className="tr-tabs">
            {counts.map(([key, value]) => (
              <div className="tr-tab" key={key} title={`平台原样返回的状态键 ${key}，不擅自翻译成中文`}>
                <span>{key}</span>
                <b>{num(value)}</b>
              </div>
            ))}
          </div>
        </section>
      )}

      <TradeListView<RawComplaint>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(read(row, 'complaint_id', 'id', 'case_id') ?? index)}
        emptyTitle="这个账号还没有投诉工单"
        emptyDesc="平台一条投诉都没返回。拿不到的金额一律显示 -，不会用 0 冒充。"
        detail={(row) => <ComplaintDetail row={row} />}
        statusFilter={{
          label: '状态',
          options: Array.from(new Set(rows
            .map((row) => String(read(row, 'status_text', 'status', 'complaint_status') ?? '')).filter(Boolean))),
          value: (row) => String(read(row, 'status_text', 'status', 'complaint_status') ?? ''),
        }}
        keywordOf={(row) => Object.values(row)
          .filter((value) => typeof value === 'string')
          .slice(0, 12)
          .join(' ')}
        dateOf={(row) => dayOf(read(row, 'gmt_create', 'create_time', 'gmt_modified'))}
        page={page}
        pageSize={pageSize}
        total={data?.paging?.total_count ?? null}
        hasPrev={page > 1}
        hasNext={Number(data?.paging?.next_page ?? 0) > page}
        onPage={setPage}
      />
    </>
  )
}

function ComplaintDetail({ row }: { row: RawComplaint }) {
  let raw = ''
  try {
    raw = JSON.stringify(row, null, 2)
  } catch {
    raw = ''
  }
  return (
    <div className="tr-detail">
      <div className="tr-detail-grid">
        <Field label="已识别的字段">
          <span>投诉单 <b>{money(read(row, 'complaint_id', 'id', 'case_id'))}</b></span>
          <span className="tr-sub">订单 {money(read(row, 'order_id', 'oid'))}</span>
          <span className="tr-sub">状态 {money(read(row, 'status_text', 'status', 'complaint_status'))}</span>
          <span className="tr-sub">时间 {when(read(row, 'gmt_create', 'create_time', 'gmt_modified'))}</span>
        </Field>
        <Field label="为什么这里显示原始 JSON" tip="">
          <span className="tr-sub">
            这个接口没有真机样本，契约只写了「元素原样透传」，字段表没冻结。
            下面是平台原样回给你的内容 —— 不替你猜字段名，也不删字段。
          </span>
        </Field>
      </div>
      <pre className="tr-raw">{raw || '平台没有返回任何内容'}</pre>
    </div>
  )
}
