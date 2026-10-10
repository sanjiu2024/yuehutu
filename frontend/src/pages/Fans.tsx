/**
 * 数据组 → 粉丝数据（**只读**）。
 *
 * 契约见 `docs/contracts/datacenter.md`（task-52 冻结）：
 *  - `GET /api/datacenter/{account_id}/fans?start=&end=`（`YYYYMMDD`，缺省后端补今天往前 7 天）
 *  - 6 个指标：`fans_total / fans_new / fans_lost / fans_active / fans_orders / fans_buyers`，
 *    外加 `insights`（粉丝画像列表，认不出为 null）。`null` 一律 `-**，绝不补 0。
 *  - 无权限 ≠ 掉线：只给 null + 中文 warning，不提示重新扫码。
 *
 * 真机现状（账号 2217970177348）：粉丝接口无权限 ⇒ 指标全 null + insights null。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconClock, IconRefresh, IconUsers } from '../icons'
import type { Account } from '../types'
import { daysAgo, isRangeOk, num, pct, realRangeText, type MetricDef } from './datacenterShared'
import './datacenter.css'

const FANS_METRICS: MetricDef[] = [
  { key: 'fans_total', label: '粉丝总数', tip: '当前累计粉丝数' },
  { key: 'fans_new', label: '新增粉丝', tip: '区间内新增的粉丝数' },
  { key: 'fans_lost', label: '流失粉丝', tip: '区间内取消关注 / 流失的粉丝数' },
  { key: 'fans_active', label: '活跃粉丝', tip: '区间内有互动的粉丝数' },
  { key: 'fans_orders', label: '粉丝订单', tip: '粉丝产生的订单数' },
  { key: 'fans_buyers', label: '粉丝买家', tip: '下单过的粉丝数' },
]

/** 画像项：形状未冻结（真机无权限拿不到），原样渲染常见键，其余合并成一行说明。 */
type InsightRow = Record<string, unknown>

interface FansEnvelope {
  account_id?: string
  kind?: string
  period?: { from?: string; to?: string } | null
  metrics?: Record<string, unknown> | null
  begin_time?: unknown
  real_date_range?: unknown
  warnings?: string[]
  unrecognized_keys?: string[]
  insights?: InsightRow[] | null
  fetched_at?: string
  source?: string
}

function MetricCards({ metrics, defs }: { metrics: Record<string, unknown> | null | undefined; defs: MetricDef[] }) {
  return (
    <div className="dc-grid">
      {defs.map((def) => {
        const raw = metrics ? metrics[def.key] : undefined
        const shown = (def.format || num)(raw)
        const missing = raw === null || raw === undefined
        return (
          <div className="dc-card" key={def.key} title={def.tip || def.label}>
            <div className="dc-card-label">{def.label}</div>
            <div className={`dc-card-value${missing ? ' dc-missing' : ''}`}>{shown}</div>
          </div>
        )
      })}
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

/** 画像列头：从第一行里挑出的键，按出现顺序。 */
function insightKeys(rows: InsightRow[]): string[] {
  const first = rows[0]
  if (!first) return []
  return Object.keys(first)
}

function insightText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (Array.isArray(value)) return value.length ? value.map((v) => String(v)).join('、') : '-'
  if (typeof value === 'object') return JSON.stringify(value)
  if (typeof value === 'number') return pct(value)
  return String(value)
}

function Insights({ insights }: { insights: InsightRow[] | null | undefined }) {
  if (insights === null || insights === undefined) {
    return (
      <Notice kind="warn">
        平台这次<strong>没有返回</strong>粉丝画像（<code>insights</code> 为 null）。这不代表「没有画像」——
        可能是账号没有数据权限，也可能是平台没提供。
      </Notice>
    )
  }
  if (insights.length === 0) {
    return (
      <div className="sh-empty">
        <span className="sh-empty-ico">
          <IconUsers size={24} />
        </span>
        <div className="sh-empty-title">这个区间没有粉丝画像</div>
        <div className="sh-empty-desc">平台明确返回了空列表。换个更长的时间区间也许会有数据。</div>
      </div>
    )
  }
  const keys = insightKeys(insights)
  return (
    <div className="table-wrap">
      <table className="sh-table">
        <thead>
          <tr>{keys.map((k) => <th key={k}>{k}</th>)}</tr>
        </thead>
        <tbody>
          {insights.map((row, idx) => (
            <tr key={idx}>
              {keys.map((k) => (
                <td key={k}>{insightText(row?.[k])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function FansPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [start, setStart] = useState(() => daysAgo(6))
  const [end, setEnd] = useState(() => daysAgo(0))
  const [submitted, setSubmitted] = useState(() => ({ start: daysAgo(6), end: daysAgo(0) }))

  const accountId = picked || accounts.data?.[0]?.id || ''
  const rangeOk = isRangeOk(submitted.start, submitted.end)

  const data = useAsync<FansEnvelope>(
    () =>
      api.get<FansEnvelope>(`/api/datacenter/${encodeURIComponent(accountId)}/fans`, {
        start: submitted.start,
        end: submitted.end,
      }),
    [accountId, submitted.start, submitted.end],
    Boolean(accountId) && rangeOk,
  )

  const applyRange = (nextStart: string, nextEnd: string) => {
    setStart(nextStart)
    setEnd(nextEnd)
    setSubmitted({ start: nextStart, end: nextEnd })
  }

  const quick = (days: number) => applyRange(daysAgo(days), daysAgo(0))
  const warnings = data.data?.warnings || []

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">粉丝数据</h2>
          <p className="page-desc">
            粉丝总数、增减、活跃与粉丝成交，附平台画像。只读 —— 拿不到的指标显示「-」，不会用 0 冒充。
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
            <label className="dc-label" htmlFor="fans-account">
              账号
            </label>
            <select
              id="fans-account"
              value={accountId}
              onChange={(e) => setPicked(e.target.value)}
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
            <label className="dc-label" htmlFor="fans-start">
              开始日期
            </label>
            <input
              id="fans-start"
              className="dc-input"
              value={start}
              onChange={(e) => setStart(e.target.value.replace(/\D/g, '').slice(0, 8))}
              placeholder="YYYYMMDD"
              inputMode="numeric"
            />
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="fans-end">
              结束日期
            </label>
            <input
              id="fans-end"
              className="dc-input"
              value={end}
              onChange={(e) => setEnd(e.target.value.replace(/\D/g, '').slice(0, 8))}
              placeholder="YYYYMMDD"
              inputMode="numeric"
            />
          </div>
          <div className="dc-quick">
            <button type="button" className="btn sm" onClick={() => quick(6)}>
              近 7 天
            </button>
            <button type="button" className="btn sm" onClick={() => quick(29)}>
              近 30 天
            </button>
            <button
              type="button"
              className="btn sm"
              onClick={() => applyRange(start, end)}
              disabled={!isRangeOk(start, end)}
            >
              应用区间
            </button>
          </div>
        </div>
        {!isRangeOk(start, end) && (
          <Notice kind="warn">日期要用 8 位数字（例如 20261004），且开始日期不能晚于结束日期。</Notice>
        )}
      </div>

      <div className="card">
        <div className="dc-section-title">
          <span className="dc-ico">
            <IconChart size={15} />
          </span>
          粉丝指标
          <span className="dc-muted">
            {data.data?.period?.from && data.data?.period?.to
              ? `区间 ${data.data.period.from} ~ ${data.data.period.to}`
              : ''}
          </span>
        </div>

        <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
          {rangeOk && (
            <>
              <MetricCards metrics={data.data?.metrics} defs={FANS_METRICS} />
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

      <div className="card">
        <div className="dc-section-title">
          粉丝画像
          <span className="dc-muted">平台返回什么就显示什么</span>
        </div>
        <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
          {rangeOk && <Insights insights={data.data?.insights} />}
        </AsyncBox>
      </div>
    </div>
  )
}
