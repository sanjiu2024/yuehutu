/**
 * 交易 → **评价**（只读）。契约 `docs/contracts/trade.md` §2.4。
 *
 * 两条容易写错的地方：
 *  1. 列表项本身是**订单**，评价挂在 `has_rate` + `rates[]` 上，不是列表项就是一条评价。
 *  2. 平台空数组时回的是 `has_rate:false` + `rates:null` —— `rates` 是 null 而不是 `[]`，
 *     所以「有没有评价」只能看 `rates === null`，**绝不能写成「0 条评价」**。
 *
 * ⚠️ `rates[]` 目前**没有真机样本**（只读过 bundle 读法），所以渲染全部走防御式取值：
 * 拿不到就显示 `-`，绝不补 0、绝不猜默认星级。
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
  pick,
  useTradeAccounts,
  when,
  type TradeColumn,
  type TradeEnvelope,
} from './Orders'

interface RateItem {
  rate_id?: string | null
  rate?: string | null
  main?: string | null
  feedback?: string | null
  level?: string | number | null
  create_time?: unknown
  illegal?: unknown
  pictures?: unknown
  service_tags?: unknown
  rater_avatar?: string | null
  seller?: unknown
}

interface EvalBuyer {
  user_id?: string | null
  nick?: string | null
  name?: string | null
}

interface TradeEvaluation {
  order_id?: string | null
  item_id?: string | null
  order_status?: string | null
  seller_rate_status?: unknown
  has_rate?: boolean | null
  rates?: RateItem[] | null
  buyer?: EvalBuyer
  item?: { title?: string | null; pic_url?: string | null; info_lines?: unknown }
  times?: {
    create_time?: unknown
    pay_success_time?: unknown
    consign_time?: unknown
    finish_time?: unknown
  }
}

interface EvaluationsResult extends TradeEnvelope<TradeEvaluation> {
  evaluations?: TradeEvaluation[]
}

/** 好/中/差评只认平台自己给的文字，认不出就保持中性 —— 不猜星级。 */
function toneForMain(main: unknown): 'ok' | 'warn' | 'danger' | 'muted' {
  const text = String(main ?? '')
  if (text.includes('差')) return 'danger'
  if (text.includes('中')) return 'warn'
  if (text.includes('好')) return 'ok'
  return 'muted'
}

/** 图片数组：不是数组就当没有。 */
function pictureList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object') {
        const record = item as Record<string, unknown>
        const url = record.url ?? record.pic_url ?? record.image
        return typeof url === 'string' ? url : ''
      }
      return ''
    })
    .filter(Boolean)
}

function tagList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object') {
        const record = item as Record<string, unknown>
        const text = record.name ?? record.tag ?? record.value ?? record.content
        return typeof text === 'string' ? text : ''
      }
      return ''
    })
    .filter(Boolean)
}

export function EvaluationsPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const pageSize = 10

  const state = useAsync<EvaluationsResult>(
    () => api.get<EvaluationsResult>(`/api/trade/${encodeURIComponent(accountId)}/evaluations`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.evaluations ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)

  const columns: Array<TradeColumn<TradeEvaluation>> = [
    {
      key: 'order_id',
      label: '订单',
      sortValue: (row) => (row.order_id ? Number(row.order_id) || null : null),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-mono">{money(row.order_id)}</span>
          <span className="tr-sub">{when(row.times?.finish_time)}</span>
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
      key: 'rate',
      label: '评价',
      // 平台没回评价时 rates 是 null，sortValue 也必须是 null（排在最后），不能是 0
      sortValue: (row) => (Array.isArray(row.rates) ? row.rates.length : null),
      render: (row) => {
        if (!Array.isArray(row.rates)) {
          return (
            <div className="tr-cell">
              <span className="tr-name">-</span>
              <span className="tr-sub">{row.has_rate ? '平台标了有评价，但没给内容（-）' : '平台回：这条订单没有评价'}</span>
            </div>
          )
        }
        if (row.rates.length === 0) {
          return (
            <div className="tr-cell">
              <span className="tr-name">-</span>
              <span className="tr-sub">平台回了空数组，没有评价内容</span>
            </div>
          )
        }
        const first = row.rates[0]
        return (
          <div className="tr-cell">
            <span className="tr-name" title={String(first.feedback ?? first.main ?? '')}>
              {ellipsis(first.feedback ?? first.main ?? '', 24) || '-'}
            </span>
            <span className="tr-chips">
              <Badge tone={toneForMain(first.main)}>{money(first.main)}</Badge>
              <span className="tr-sub">{row.rates.length > 1 ? `共 ${row.rates.length} 条` : when(first.create_time)}</span>
            </span>
          </div>
        )
      },
    },
    {
      key: 'status',
      label: '订单状态',
      sortValue: (row) => String(row.order_status ?? ''),
      render: (row) => <span className="tr-sub">{money(row.order_status)}</span>,
    },
    {
      key: 'time',
      label: '交易完成时间',
      sortValue: (row) => {
        const n = Number(row.times?.finish_time)
        return Number.isFinite(n) && n > 0 ? n : null
      },
      render: (row) => <span>{when(row.times?.finish_time)}</span>,
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="评价"
        desc={
          <>
            买家对已完成订单的评价 · 只读，回复评价在平台操作
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar accounts={accounts} picked={picked} setPicked={(id) => { setPicked(id); setPage(1) }} />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没评价可看。</Notice>}
      {relogin && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查评价 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}
      {permission && (
        <Notice kind="warn">
          平台不给这个账号开「评价」权限（<code>NO_PERMISSION</code>）。这不是掉线，<strong>不用重新扫码</strong>
          —— 要开通得换卖家工作台会话。
        </Notice>
      )}

      <TradeListView<TradeEvaluation>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(row.order_id ?? index)}
        emptyTitle="这个账号还没有可查的评价"
        emptyDesc="平台一条记录都没返回。没有评价的订单显示 -，不会写成 0 条。"
        detail={(row) => <EvalDetail row={row} />}
        statusFilter={{
          label: '订单状态',
          options: Array.from(new Set(rows.map((row) => String(row.order_status ?? '')).filter(Boolean))),
          value: (row) => String(row.order_status ?? ''),
        }}
        keywordOf={(row) => [row.order_id, row.item_id, row.item?.title, row.buyer?.nick, row.buyer?.name,
          ...(Array.isArray(row.rates)
            ? row.rates.flatMap((rate) => [rate.main, rate.feedback])
            : [])]
          .filter(Boolean).join(' ')}
        dateOf={(row) => dayOf(row.times?.finish_time ?? row.times?.create_time)}
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

function EvalDetail({ row }: { row: TradeEvaluation }) {
  const rates = Array.isArray(row.rates) ? row.rates : null
  return (
    <div className="tr-detail">
      <div className="tr-detail-grid">
        <Field label="交易时间">
          <span>下单 {when(row.times?.create_time)}</span>
          <span className="tr-sub">付款 {when(row.times?.pay_success_time)}</span>
          <span className="tr-sub">发货 {when(row.times?.consign_time)}</span>
          <span className="tr-sub">完成 {when(row.times?.finish_time)}</span>
        </Field>
        <Field label="买家">
          <span className="tr-name">{money(row.buyer?.nick ?? row.buyer?.name)}</span>
          <span className="tr-sub">ID {money(row.buyer?.user_id)}</span>
          <span className="tr-sub">卖家评价状态 {money(pick(row, 'seller_rate_status'))}</span>
        </Field>
      </div>

      {rates === null ? (
        <Notice kind="info">
          平台对这条订单回的 <code>rates</code> 是 <code>null</code>（没有评价内容）。
          这里不会用「0 条评价」代替 —— 那是编出来的数字。
        </Notice>
      ) : rates.length === 0 ? (
        <Notice kind="info">平台回了一个空的 <code>rates</code> 数组：这条订单没有评价。</Notice>
      ) : (
        rates.map((rate, index) => (
          <div className="tr-detail" key={String(rate.rate_id ?? index)}>
            <div className="tr-chips">
              <span className="tr-rate">{money(rate.main)}</span>
              <Badge tone={toneForMain(rate.main)}>{money(rate.main)}</Badge>
              <span className="tr-sub">等级 {money(rate.level)}</span>
              <span className="tr-sub">{when(rate.create_time)}</span>
              {rate.illegal !== undefined && rate.illegal !== null
                ? <span className="tr-sub">违规标记 {String(rate.illegal)}</span>
                : <span className="tr-sub">违规标记 -</span>}
            </div>
            <p className="tr-sub" style={{ margin: '6px 0 0' }}>
              {rate.feedback ? String(rate.feedback) : '平台没给文字内容（-）'}
            </p>
            {tagList(rate.service_tags).length > 0 && (
              <div className="tr-chips" style={{ marginTop: 6 }}>
                {tagList(rate.service_tags).map((tag) => <Badge key={tag} tone="muted">{tag}</Badge>)}
              </div>
            )}
            {pictureList(rate.pictures).length > 0 && (
              <div className="tr-pics">
                {pictureList(rate.pictures).map((url) => <img key={url} src={url} alt="" loading="lazy" />)}
              </div>
            )}
            <div className="tr-kv" style={{ marginTop: 6 }}>
              <span>评价 ID <b>{money(rate.rate_id)}</b></span>
              <span>买家给的分 <b>{money(rate.rate)}</b></span>
              <span>卖家内容 <b>{money(pick(rate, 'seller'))}</b></span>
            </div>
          </div>
        ))
      )}
    </div>
  )
}
