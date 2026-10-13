/**
 * 数据组 → 商品数据（**只读**）。
 *
 * 这一页对应官方卖家工作台的 `/seller-data/commodity`（官方菜单树里我们唯一还没复刻的叶子页）。
 *
 * 契约（`docs/contracts/datacenter.md`）：
 *  - `GET /api/datacenter/{account_id}/commodity`
 *      `?start=&end=&item_id=&item_name=&page=&size=&order_by=&order=`
 *  - `start`/`end` 是 `YYYYMMDD`；官方这一页默认是**今天**（不是近 7 天），这里跟着官方走。
 *  - `items` 是「一行一个商品」，`columns` 是平台给的指标列定义（`{name,title,type}`）。
 *  - `total` / `pages` 认不出就是 `null`，不补 0；`items === null` 表示**平台没返回行**，
 *    与「真的没有商品」是两件事，文案必须分开。
 *
 * 三条不能破的口径：
 *  1. 平台没给的值显示 `-`，**绝不补 0**（0 是「真的没人看」，null 是「没取到」）。
 *  2. 无权限 ≠ 掉线：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 只给 null + 中文 warning，不提示重新扫码。
 *  3. 本页无写操作，刷新一律走 `reload()`，不轮询（只订阅 `accounts.changed`）。
 *
 * 官方渲染规则（从官方 bundle 抄的，不是我们发明的）：**第一行该列的值 < 1 时，
 * 整列渲染成「原样数字 + %」**（官方不做乘 100）。这里照抄，并在列头提示里说明。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconClock, IconImage, IconRefresh, IconSearch, IconSort } from '../icons'
import type { Account } from '../types'
import { daysAgo, isRangeOk, num, realRangeText } from './datacenterShared'
import './commodity.css'

const PAGE_SIZES = [10, 20, 50] as const
/** 商品信息列固定在最左（官方 `fixed: 'left'`）。 */
const INFO_KEY = 'accountName'

interface ColumnDef {
  name?: string
  title?: string
  type?: string
}

type Row = Record<string, unknown>

interface CommodityEnvelope {
  account_id?: string
  kind?: string
  period?: { from?: string; to?: string } | null
  begin_time?: unknown
  real_date_range?: unknown
  items?: Row[] | null
  columns?: ColumnDef[] | null
  total?: number | null
  page?: number
  size?: number
  pages?: number | null
  warnings?: string[]
  unrecognized_keys?: string[]
  fetched_at?: string
  source?: string
}

/** 单元格文字：`null`/缺失 → `-`；对象原样 JSON；其余 `String()`。 */
function cellText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/**
 * 官方规则：第一行该列的值 `< 1` 时整列补 `%`（原样数字，不乘 100）。
 *
 * 只在「值是数字」且「第一行该列是能读成数字的 < 1」时才补，避免把 `undefined < 1`
 * 这种 JS 强转怪癖带进来。
 */
function isPercentColumn(firstRow: Row | undefined, name: string): boolean {
  const first = firstRow ? firstRow[name] : undefined
  if (first === null || first === undefined || first === '') return false
  const n = Number(first)
  return Number.isFinite(n) && n < 1
}

function Thumb({ url, alt }: { url: string; alt: string }) {
  const [broken, setBroken] = useState(false)
  if (!url || broken) {
    return (
      <span className="cm-thumb cm-thumb-none" title="这个商品没有图片">
        <IconImage size={16} />
      </span>
    )
  }
  return (
    <img
      className="cm-thumb"
      src={url}
      alt={alt}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setBroken(true)}
    />
  )
}

function InfoCell({ row }: { row: Row }) {
  const title = cellText(row.itmName)
  const id = cellText(row.itmId)
  const url = typeof row.itmPicUrl === 'string' ? row.itmPicUrl : ''
  return (
    <div className="cm-info">
      <Thumb url={url} alt={title === '-' ? '商品图片' : title} />
      <div className="cm-info-text">
        <div className="cm-info-title" title={title}>
          {title}
        </div>
        <div className="cm-info-id">商品 ID {id}</div>
      </div>
    </div>
  )
}

function RealRangeNote({ real, begin }: { real: unknown; begin: unknown }) {
  const text = realRangeText(real, begin)
  if (!text) return null
  return (
    <div className="dc-realrange">
      <span className="dc-ico">
        <IconClock size={13} />
      </span>
      {text}
    </div>
  )
}

function Unrecognized({ keys }: { keys: string[] | null | undefined }) {
  if (!keys || keys.length === 0) return null
  return (
    <Notice kind="warn">
      有 {keys.length} 个平台字段没能对上已知指标名（{keys.slice(0, 8).join('、')}
      {keys.length > 8 ? ' …' : ''}）。这不是错误，是平台用了没预料到的字段名 —— 这些指标显示为「-」。
    </Notice>
  )
}

export function CommodityDataPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')

  // 官方这一页默认「今天」。
  const [start, setStart] = useState(() => daysAgo(0))
  const [end, setEnd] = useState(() => daysAgo(0))
  const [itemId, setItemId] = useState('')
  const [itemName, setItemName] = useState('')
  const [size, setSize] = useState<number>(20)
  const [page, setPage] = useState(1)
  const [sort, setSort] = useState<{ field: string; order: 'asc' | 'desc' } | null>(null)
  const [applied, setApplied] = useState(() => ({
    start: daysAgo(0),
    end: daysAgo(0),
    itemId: '',
    itemName: '',
  }))

  const accountId = picked || accounts.data?.[0]?.id || ''
  const rangeOk = isRangeOk(applied.start, applied.end)

  const data = useAsync<CommodityEnvelope>(
    () =>
      api.get<CommodityEnvelope>(`/api/datacenter/${encodeURIComponent(accountId)}/commodity`, {
        start: applied.start,
        end: applied.end,
        item_id: applied.itemId,
        item_name: applied.itemName,
        page,
        size,
        order_by: sort ? sort.field : '',
        order: sort ? sort.order : '',
      }),
    [accountId, applied.start, applied.end, applied.itemId, applied.itemName, page, size, sort?.field, sort?.order],
    Boolean(accountId) && rangeOk,
  )

  const apply = (next?: Partial<{ start: string; end: string; itemId: string; itemName: string }>) => {
    const merged = {
      start: next?.start ?? start,
      end: next?.end ?? end,
      itemId: next?.itemId ?? itemId,
      itemName: next?.itemName ?? itemName,
    }
    setApplied(merged)
    setPage(1)
  }

  const quick = (days: number) => {
    const from = daysAgo(days)
    setStart(from)
    setEnd(daysAgo(0))
    apply({ start: from, end: daysAgo(0) })
  }

  const toggleSort = (name: string) => {
    setPage(1)
    setSort((prev) => {
      if (!prev || prev.field !== name) return { field: name, order: 'asc' }
      if (prev.order === 'asc') return { field: name, order: 'desc' }
      return null
    })
  }

  const items = data.data?.items
  const rows = Array.isArray(items) ? items : []
  const firstRow = rows[0]
  const platformColumns = Array.isArray(data.data?.columns) ? data.data!.columns! : null
  const columnsReturnedEmpty = platformColumns !== null && platformColumns.length === 0
  // 平台没给列定义（或给了空数组）时，退回「按第一行的键显示」并明确告知，而不是假装知道列名。
  const columns: ColumnDef[] =
    platformColumns && platformColumns.length > 0
      ? platformColumns
      : firstRow
        ? Object.keys(firstRow)
            .filter((key) => key !== INFO_KEY)
            .map((key) => ({ name: key, title: key }))
        : []

  const total = typeof data.data?.total === 'number' ? data.data.total : null
  const pages = typeof data.data?.pages === 'number' ? data.data.pages : null
  const warnings = data.data?.warnings || []

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">商品数据</h2>
          <p className="page-desc">
            一行一个商品的经营指标（官方「商品数据」页）。只读 —— 拿不到的指标显示「-」，不会用 0 冒充。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="dc-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="dc-bar">
          <div className="dc-field">
            <label className="dc-label" htmlFor="cm-account">
              账号
            </label>
            <select
              id="cm-account"
              value={accountId}
              onChange={(e) => {
                setPicked(e.target.value)
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
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="cm-start">
              开始日期
            </label>
            <input
              id="cm-start"
              className="dc-input"
              value={start}
              onChange={(e) => setStart(e.target.value.replace(/\D/g, '').slice(0, 8))}
              placeholder="YYYYMMDD"
              inputMode="numeric"
            />
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="cm-end">
              结束日期
            </label>
            <input
              id="cm-end"
              className="dc-input"
              value={end}
              onChange={(e) => setEnd(e.target.value.replace(/\D/g, '').slice(0, 8))}
              placeholder="YYYYMMDD"
              inputMode="numeric"
            />
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="cm-item-id">
              商品 ID
            </label>
            <input
              id="cm-item-id"
              className="dc-input"
              value={itemId}
              onChange={(e) => setItemId(e.target.value.trim())}
              placeholder="精确匹配，可留空"
            />
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="cm-item-name">
              商品标题
            </label>
            <input
              id="cm-item-name"
              className="dc-input"
              value={itemName}
              onChange={(e) => setItemName(e.target.value)}
              placeholder="模糊匹配，可留空"
            />
          </div>
          <div className="dc-quick">
            <button type="button" className="btn sm" onClick={() => quick(0)}>
              今天
            </button>
            <button type="button" className="btn sm" onClick={() => quick(6)}>
              近 7 天
            </button>
            <button type="button" className="btn sm" onClick={() => quick(29)}>
              近 30 天
            </button>
            <button
              type="button"
              className="btn sm primary"
              onClick={() => apply()}
              disabled={!isRangeOk(start, end)}
            >
              <span className="dc-ico">
                <IconSearch size={13} />
              </span>
              查询
            </button>
          </div>
        </div>
        {!isRangeOk(start, end) && (
          <Notice kind="warn">日期要用 8 位数字（例如 20261013），且开始日期不能晚于结束日期。</Notice>
        )}
      </div>

      <div className="card">
        <div className="dc-section-title">
          <span className="dc-ico">
            <IconChart size={15} />
          </span>
          商品指标
          <span className="dc-muted">
            {data.data?.period?.from && data.data?.period?.to
              ? `区间 ${data.data.period.from} ~ ${data.data.period.to}`
              : ''}
          </span>
          <span className="cm-count">
            {total === null ? '共 - 条' : `共 ${num(total)} 条`}
            {pages === null ? '' : ` · 第 ${page} / ${pages} 页`}
          </span>
        </div>

        <AsyncBox
          loading={data.loading}
          error={data.error}
          notReady={data.notReady}
          empty={Array.isArray(items) && items.length === 0}
          emptyTitle="这个区间没有商品数据"
          emptyDesc="平台真的返回了空列表 —— 换个区间或清掉筛选再试。"
          onRetry={data.reload}
        >
          {rangeOk && (
            <>
              {items === null && (
                <Notice kind="warn">
                  平台没有返回商品行（`items`）。这**不是**「一件商品都没有」，而是这一列数据没取到 ——
                  常见原因是这个账号没有「数据」权限，或平台侧服务繁忙。别把它当成 0。
                </Notice>
              )}
              {items !== null && columnsReturnedEmpty && (
                <Notice kind="warn">
                  平台返回的指标列定义是空的（`columns: []`），下面按第一行实际出现的字段名显示 ——
                  这些列头是字段名，不是官方中文标题。
                </Notice>
              )}
              {items !== null && platformColumns === null && (
                <Notice kind="warn">
                  平台没有返回指标列定义（`columns`），下面按第一行实际出现的字段名显示 ——
                  这些列头是字段名，不是官方中文标题。
                </Notice>
              )}

              {rows.length > 0 && (
                <div className="table-wrap cm-wrap">
                  <table className="cm-table">
                    <thead>
                      <tr>
                        <th className="cm-th-info">商品信息</th>
                        {columns.map((col) => {
                          const name = String(col.name || '')
                          const percent = isPercentColumn(firstRow, name)
                          const active = sort && sort.field === name
                          return (
                            <th key={name} className="cm-th">
                              <button
                                type="button"
                                className={`cm-sort${active ? ' active' : ''}`}
                                onClick={() => toggleSort(name)}
                                title={`按「${col.title || name}」排序${percent ? '（平台原样数字 + %）' : ''}`}
                              >
                                <span className="cm-th-text">{col.title || name}</span>
                                <span className={`cm-sort-ico${active ? ' active' : ''}`}>
                                  <IconSort size={13} />
                                </span>
                              </button>
                              {active && (
                                <span className="cm-sort-order">{sort!.order === 'asc' ? '升序' : '降序'}</span>
                              )}
                            </th>
                          )
                        })}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, idx) => (
                        <tr key={String(row.itmId ?? idx)}>
                          <td className="cm-td-info">
                            <InfoCell row={row} />
                          </td>
                          {columns.map((col) => {
                            const name = String(col.name || '')
                            const value = row[name]
                            const missing = value === null || value === undefined || value === ''
                            const text = cellText(value)
                            const shown = isPercentColumn(firstRow, name) && !missing ? `${text}%` : text
                            return (
                              <td key={name} className={`cm-td${missing ? ' cm-missing' : ''}`}>
                                {shown}
                              </td>
                            )
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="cm-pager">
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                >
                  上一页
                </button>
                <button
                  type="button"
                  className="btn sm"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={pages !== null ? page >= pages : rows.length < size}
                >
                  下一页
                </button>
                <div className="dc-field cm-size">
                  <label className="dc-label" htmlFor="cm-size">
                    每页
                  </label>
                  <select
                    id="cm-size"
                    value={size}
                    onChange={(e) => {
                      setSize(Number(e.target.value))
                      setPage(1)
                    }}
                  >
                    {PAGE_SIZES.map((n) => (
                      <option key={n} value={n}>
                        {n} 条
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <RealRangeNote real={data.data?.real_date_range} begin={data.data?.begin_time} />
              {warnings.length > 0 && (
                <div className="dc-warnings">
                  {warnings.map((w, idx) => (
                    <Notice key={idx} kind="warn">
                      {w}
                    </Notice>
                  ))}
                </div>
              )}
              <Unrecognized keys={data.data?.unrecognized_keys} />
            </>
          )}
        </AsyncBox>

        <div className="dc-note">
          <span className="dc-ico">
            <IconClock size={13} />
          </span>
          只读页：不会改任何数据，也不向平台写入。数据更新于
          {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
        </div>
      </div>
    </div>
  )
}
