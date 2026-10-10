/**
 * 交易 → **退款**（只读）。契约 `docs/contracts/trade.md` §2.3。
 *
 * 退款帧和订单帧有两个不一样的地方，写的时候别照抄订单页：
 *  1. 平台在退款帧里**根本不给** `name` / `phone` / `address`，一律显示 `-`（是实情，不是丢数据）。
 *  2. 顶层有 `total_refund_fee`（真机 `"468.90"`，**字符串**），是本页的汇总数字。
 *
 * 公共外壳与工具函数都在 `./Orders` —— 本次写范围里没有独立的公共文件可放。
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
  buttonsOf,
  dayOf,
  ellipsis,
  infoLines,
  isNeedRelogin,
  isPermissionDenied,
  money,
  num,
  pick,
  useTradeAccounts,
  when,
  type TradeColumn,
  type TradeEnvelope,
} from './Orders'

interface RefundBuyer {
  user_id?: string | null
  nick?: string | null
  avatar?: string | null
}

interface RefundPrice {
  auction_price?: string | null
  buy_num?: number | null
  refund_fee?: string | null
  refund_fee_view?: string | null
}

interface TradeRefund {
  refund_id?: string | null
  order_id?: string | null
  item_id?: string | null
  order_status?: string | null
  refund_status?: number | null
  refund_status_text?: string | null
  refund_type?: unknown
  refund_accept?: unknown
  cs_status?: unknown
  reason?: string | null
  goods_needs?: unknown
  in_sale?: unknown
  refund_time?: unknown
  remark?: string | null
  remark_rich_text?: unknown
  buyer?: RefundBuyer
  item?: { title?: string | null; pic_url?: string | null; info_lines?: unknown }
  price?: RefundPrice
  times?: { create_time?: unknown }
  buttons?: unknown[]
}

interface RefundsResult extends TradeEnvelope<TradeRefund> {
  refunds?: TradeRefund[]
  /** 顶层汇总，真机是字符串 `"468.90"`。拿不到是 `null` 不是 0。 */
  total_refund_fee?: string | null
}

export function RefundsPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const pageSize = 10

  const state = useAsync<RefundsResult>(
    () => api.get<RefundsResult>(`/api/trade/${encodeURIComponent(accountId)}/refunds`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.refunds ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)

  const columns: Array<TradeColumn<TradeRefund>> = [
    {
      key: 'refund_id',
      label: '退款单',
      sortValue: (row) => (row.refund_id ? Number(row.refund_id) || null : null),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-mono">{money(row.refund_id)}</span>
          <span className="tr-sub">订单 {money(row.order_id)}</span>
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
      sortValue: (row) => String(row.buyer?.nick ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-name" title={String(row.buyer?.nick ?? '')}>
            {ellipsis(row.buyer?.nick ?? '', 18) || '-'}
          </span>
          <span className="tr-sub">{row.buyer?.user_id ? `ID ${row.buyer.user_id}` : 'ID -'}</span>
        </div>
      ),
    },
    {
      key: 'status',
      label: '状态',
      sortValue: (row) => Number(row.refund_status ?? '') || 0,
      render: (row) => (
        <div className="tr-cell">
          <Badge tone={row.refund_status_text || row.order_status ? 'warn' : 'muted'}>
            {money(row.refund_status_text ?? row.order_status)}
          </Badge>
          <span className="tr-sub">
            状态码 {row.refund_status === null || row.refund_status === undefined ? '-' : row.refund_status}
          </span>
        </div>
      ),
    },
    {
      key: 'reason',
      label: '退款原因',
      sortValue: (row) => String(row.reason ?? ''),
      render: (row) => (
        <span className="tr-name" title={String(row.reason ?? '')}>
          {ellipsis(row.reason, 24) || '-'}
        </span>
      ),
    },
    {
      key: 'fee',
      label: '退款金额（元）',
      num: true,
      sortValue: (row) => {
        const value = row.price?.refund_fee ?? row.price?.refund_fee_view
        if (value === null || value === undefined || value === '') return null
        const n = Number(value)
        return Number.isFinite(n) ? n : null
      },
      render: (row) => (
        <span className="tr-money">{money(row.price?.refund_fee ?? row.price?.refund_fee_view)}</span>
      ),
    },
    {
      key: 'time',
      label: '申请时间',
      sortValue: (row) => {
        const n = Number(row.refund_time ?? row.times?.create_time)
        return Number.isFinite(n) && n > 0 ? n : null
      },
      render: (row) => <span>{when(row.refund_time ?? row.times?.create_time)}</span>,
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="退款"
        desc={
          <>
            售后退款单 · 只读，同意 / 拒绝退款一律在平台操作
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
          <span className="tr-note">
            退款总额 <b className="tr-money">{money(data?.total_refund_fee)}</b> 元
          </span>
        }
      />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没退款单可看。</Notice>}
      {relogin && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查退款单 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}
      {permission && (
        <Notice kind="warn">
          平台不给这个账号开「退款」权限（<code>NO_PERMISSION</code>）。这不是掉线，<strong>不用重新扫码</strong>
          —— 要开通得换卖家工作台会话。
        </Notice>
      )}

      <TradeListView<TradeRefund>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(row.refund_id ?? index)}
        emptyTitle="这个账号还没有退款单"
        emptyDesc="平台一条退款单都没返回。拿不到的金额一律显示 -，不会用 0 冒充。"
        detail={(row) => <RefundDetail row={row} />}
        statusFilter={{
          label: '状态',
          options: Array.from(new Set(rows
            .map((row) => String(row.refund_status_text ?? row.order_status ?? '')).filter(Boolean))),
          value: (row) => String(row.refund_status_text ?? row.order_status ?? ''),
        }}
        keywordOf={(row) => [row.refund_id, row.order_id, row.item_id, row.item?.title, row.buyer?.nick, row.reason]
          .filter(Boolean).join(' ')}
        dateOf={(row) => dayOf(row.refund_time ?? row.times?.create_time)}
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

function RefundDetail({ row }: { row: TradeRefund }) {
  const names = buttonsOf(row.buttons)
  const rich = Array.isArray(row.remark_rich_text) ? row.remark_rich_text : []
  return (
    <div className="tr-detail-grid">
      <Field label="买家（退款帧）">
        <span className="tr-name">{money(row.buyer?.nick)}</span>
        <span className="tr-sub">ID {money(row.buyer?.user_id)}</span>
        <span className="tr-sub">
          电话 / 地址：平台在退款帧里不给这些字段，显示 - 是实情，不是丢数据
        </span>
      </Field>
      <Field label="退款">
        <span>金额 <b className="tr-money">{money(row.price?.refund_fee)}</b> 元</span>
        <span className="tr-sub">平台展示值 {money(row.price?.refund_fee_view)}</span>
        <span className="tr-sub">单价 {money(row.price?.auction_price)} · 数量 {num(row.price?.buy_num)}</span>
        <span className="tr-sub">原因 {money(row.reason)}</span>
      </Field>
      <Field label="状态">
        <span>{money(row.refund_status_text ?? row.order_status)}</span>
        <span className="tr-sub">refund_type {money(pick(row, 'refund_type'))}</span>
        <span className="tr-sub">是否在售 {money(pick(row, 'in_sale'))}</span>
        <span className="tr-sub">客服状态 {money(pick(row, 'cs_status'))}</span>
      </Field>
      <Field label="备注">
        <span className="tr-sub">{row.remark ? row.remark : '平台没给备注（-）'}</span>
        {rich.length > 0 && (
          <span className="tr-sub">
            富文本：
            {rich.map((item, index) => {
              const text = typeof item === 'object' && item !== null
                ? String((item as Record<string, unknown>).content ?? '')
                : String(item ?? '')
              return text ? <span key={index}>{text} </span> : null
            })}
          </span>
        )}
      </Field>
      {names.length > 0 && (
        <Field label="平台给出的操作">
          <span className="tr-btns">
            {names.map((name) => <Badge key={name} tone="muted">{name}</Badge>)}
          </span>
          <span className="tr-sub">
            只显示文案、点不动 —— 同意 / 拒绝退款是写接口，本项目一律 405。
          </span>
        </Field>
      )}
    </div>
  )
}
