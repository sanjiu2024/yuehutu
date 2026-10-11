/**
 * 交易 → **退货地址**（只读）。契约 `docs/contracts/trade.md` §2.6。
 *
 *  1. **没有真机样本** —— 真机上平台回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` → 后端 502。
 *     字段按 bundle 读法 `{addressId,name,phoneNum,address,defaultAddr}` 映射成
 *     `address_id / name / phone / address / is_default`，读不到就 `-`。
 *  2. `is_default` 是**平台只在给 bool 时才有值**的字段：给 `null` 显示 `-`，不能写成「否」。
 *  3. 这个 ret **不写 `need_relogin`** —— 无权限 ≠ 掉线，不要引导用户去重新扫码。
 *  4. 新增 / 编辑 / 删除退货地址**没有实现**（本页只读）：这两件事都要先有证据 ——
 *     既要能从官方 bundle 里挖出写接口名与入参，也要确认它不碰风控敏感路径。
 *     在那之前宁可只读，也不猜一个接口去改用户的收货地址。
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
  useTradeAccounts,
  when,
  type TradeColumn,
  type TradeEnvelope,
} from './Orders'

interface TradeAddress {
  address_id?: string | null
  name?: string | null
  phone?: string | null
  address?: string | null
  is_default?: boolean | null
  /** 平台原样透传里可能还有别的键，详情区会原样展示。 */
  [key: string]: unknown
}

interface AddressesResult extends TradeEnvelope<TradeAddress> {
  addresses?: TradeAddress[]
}

export function ReturnAddressesPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const pageSize = 10

  const state = useAsync<AddressesResult>(
    () => api.get<AddressesResult>(`/api/trade/${encodeURIComponent(accountId)}/addresses`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.addresses ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)

  const columns: Array<TradeColumn<TradeAddress>> = [
    {
      key: 'address_id',
      label: '地址 ID',
      sortValue: (row) => {
        const n = Number(row.address_id)
        return Number.isFinite(n) ? n : null
      },
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-mono">{money(row.address_id)}</span>
          {row.is_default === true
            ? <Badge tone="ok">默认地址</Badge>
            : row.is_default === false
              ? <span className="tr-sub">非默认</span>
              : <span className="tr-sub">默认标记 -</span>}
        </div>
      ),
    },
    {
      key: 'name',
      label: '收件人',
      sortValue: (row) => String(row.name ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-name" title={String(row.name ?? '')}>{ellipsis(row.name ?? '', 16) || '-'}</span>
          <span className="tr-sub">{row.phone ? String(row.phone) : '电话 -'}</span>
        </div>
      ),
    },
    {
      key: 'address',
      label: '详细地址',
      sortValue: (row) => String(row.address ?? ''),
      render: (row) => (
        <span className="tr-name" style={{ maxWidth: '40ch' }} title={String(row.address ?? '')}>
          {ellipsis(row.address ?? '', 48) || '-'}
        </span>
      ),
    },
    {
      key: 'default',
      label: '是否默认',
      sortValue: (row) => (row.is_default === null || row.is_default === undefined ? null : row.is_default ? 1 : 0),
      render: (row) => (
        <Badge tone={row.is_default === true ? 'ok' : 'muted'}>
          {row.is_default === true ? '是' : row.is_default === false ? '否' : '-'}
        </Badge>
      ),
    },
    {
      key: 'time',
      label: '更新时间',
      sortValue: (row) => {
        const n = Number(row.gmt_modified ?? row.gmt_create ?? row.create_time)
        return Number.isFinite(n) && n > 0 ? n : null
      },
      render: (row) => (
        <span>{when(row.gmt_modified ?? row.gmt_create ?? row.create_time)}</span>
      ),
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="退货地址"
        desc={
          <>
            卖家退货收货地址 · 只读，新增 / 修改地址请去平台
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar accounts={accounts} picked={picked} setPicked={(id) => { setPicked(id); setPage(1) }} />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没退货地址可看。</Notice>}
      {permission && (
        <Notice kind="warn">
          平台回了 <code>FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问</code>，这个账号读不到退货地址。
          这是平台给的业务结果，<strong>不是掉线、不用重新扫码</strong> ——
          要能读得换一个有卖家工作台会话的账号。下面照实显示错误，不会拿空列表冒充「没有地址」。
        </Notice>
      )}
      {relogin && !permission && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查退货地址 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}

      <TradeListView<TradeAddress>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(row.address_id ?? index)}
        emptyTitle="这个账号还没有退货地址"
        emptyDesc="平台一条地址都没返回。拿不到的字段显示 -，不会用「否」顶替没给的默认标记。"
        detail={(row) => <AddressDetail row={row} />}
        statusFilter={{
          label: '是否默认',
          options: ['默认地址', '非默认'],
          value: (row) => (row.is_default === true ? '默认地址' : row.is_default === false ? '非默认' : ''),
        }}
        keywordOf={(row) => [row.address_id, row.name, row.phone, row.address].filter(Boolean).join(' ')}
        dateOf={(row) => dayOf(row.gmt_modified ?? row.gmt_create ?? row.create_time)}
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

function AddressDetail({ row }: { row: TradeAddress }) {
  const known = new Set(['address_id', 'name', 'phone', 'address', 'is_default'])
  const extras = Object.keys(row).filter((key) => !known.has(key))
  return (
    <div className="tr-detail">
      <div className="tr-detail-grid">
        <Field label="收件信息">
          <span>{money(row.name)}</span>
          <span className="tr-sub">{row.phone ? String(row.phone) : '电话 -'}</span>
          <span className="tr-sub">{money(row.address)}</span>
        </Field>
        <Field label="标识">
          <span>地址 ID <b className="tr-mono">{money(row.address_id)}</b></span>
          <span className="tr-sub">
            是否默认：
            {row.is_default === true ? '是'
              : row.is_default === false ? '否'
                : '平台没给这个键，显示 -（不是「否」）'}
          </span>
        </Field>
      </div>
      {extras.length > 0 && (
        <div className="tr-kv" style={{ marginTop: 8 }}>
          {extras.map((key) => {
            const value = row[key]
            const text = value === null || value === undefined
              ? '-'
              : typeof value === 'object' ? JSON.stringify(value) : String(value)
            return <span key={key}>{key} <b>{text}</b></span>
          })}
        </div>
      )}
    </div>
  )
}
