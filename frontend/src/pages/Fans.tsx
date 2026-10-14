/**
 * 数据组 → 粉丝数据（**只读**）。
 *
 * 契约见 `docs/contracts/datacenter.md`（task-52 冻结）：
 *  - `GET /api/datacenter/{account_id}/fans?start=&end=`（`YYYYMMDD`，缺省后端补今天往前 7 天）
 *  - 6 个指标：`fans_total / fans_new / fans_lost / fans_active / fans_orders / fans_buyers`，
 *    外加 `insights_breakdown`（第 51 批：性别 / 粉丝类型 / 省份 / 八大人群四组具名列表，
 *    认不出为 null）与旧字段 `insights`（认不出为 null）。`null` 一律 `-`，绝不补 0。
 *  - 无权限 ≠ 掉线：只给 null + 中文 warning，不提示重新扫码。
 *
 * 真机现状（账号 2217970177348）：粉丝接口无权限 ⇒ 指标全 null + 画像为 null。
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

/** 画像分组（第 51 批冻结）：官方页面渲染的就是这四组具名列表。 */
interface FansInsightItem {
  name: string
  ratio: number | null
  ratio_text: string | null
  raw_label: string | null
}

interface FansInsightGroup {
  key: string
  title: string
  platform_key: string
  items: FansInsightItem[]
}

interface FansBreakdown {
  groups: Record<string, FansInsightGroup>
  raw_keys: string[]
}

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
  insights_breakdown?: FansBreakdown | null
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

/** 占比显示：平台给了文字就原样用（`'12.34%'`），否则按 0~1 换算；`null` → `-`。 */
function ratioText(item: FansInsightItem): string {
  if (item.ratio_text) return item.ratio_text
  if (item.ratio === null || item.ratio === undefined) return '-'
  const n = Number(item.ratio)
  if (!Number.isFinite(n)) return '-'
  return `${(n * 100).toFixed(2)}%`
}

/** 一组画像：名字 + 占比条 + 占比文字。比例为 `null` 时条是空的、文字是 `-`（不补 0）。 */
function InsightGroupBlock({ group }: { group: FansInsightGroup }) {
  return (
    <div className="dc-fans-group">
      <div className="dc-fans-group-title">
        {group.title}
        <span className="dc-muted">{group.items.length} 项</span>
      </div>
      <div className="dc-fans-rows">
        {group.items.map((item, idx) => (
          <div className="dc-fans-row" key={`${item.name}-${idx}`}>
            <span className="dc-fans-name" title={item.raw_label || item.name}>
              {item.name}
            </span>
            <span className="dc-fans-bar" aria-hidden="true">
              <span
                className="dc-fans-bar-fill"
                style={{
                  width:
                    item.ratio === null || item.ratio === undefined
                      ? '0%'
                      : `${Math.min(100, Math.max(0, Number(item.ratio) * 100))}%`,
                }}
              />
            </span>
            <span className={`dc-fans-ratio${item.ratio === null ? ' dc-missing' : ''}`}>{ratioText(item)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** 粉丝画像分组（第 51 批）：官方页面就是这四组；认不出分组时诚实说明，不编数据。 */
function Breakdown({ breakdown }: { breakdown: FansBreakdown | null | undefined }) {
  if (breakdown === null || breakdown === undefined) {
    return (
      <Notice kind="warn">
        平台这次<strong>没有返回</strong>粉丝画像分组（性别 / 粉丝类型 / 省份 / 八大人群）。这不代表「没有粉丝」——
        可能是账号没有「数据」权限，也可能是平台改了字段名。
      </Notice>
    )
  }
  const groups = Object.values(breakdown.groups || {})
  if (groups.length === 0) {
    return (
      <div className="sh-empty">
        <span className="sh-empty-ico">
          <IconUsers size={24} />
        </span>
        <div className="sh-empty-title">这个区间没有粉丝画像</div>
        <div className="sh-empty-desc">平台明确返回了空分组。换个更长的时间区间也许会有数据。</div>
      </div>
    )
  }
  return (
    <>
      <div className="dc-fans-groups">
        {groups.map((group) => (
          <InsightGroupBlock key={group.key} group={group} />
        ))}
      </div>
      <div className="dc-note">
        每组按占比从高到低排；平台没给比例的项排最后并显示「-」（不补 0）。平台字段名：
        {breakdown.raw_keys.join('、')}
      </div>
    </>
  )
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
          <span className="dc-ico">
            <IconUsers size={15} />
          </span>
          粉丝画像
          <span className="dc-muted">性别 / 粉丝类型 / 省份 / 八大人群（平台分组，原样渲染）</span>
        </div>
        <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
          {rangeOk && <Breakdown breakdown={data.data?.insights_breakdown} />}
        </AsyncBox>
        {rangeOk && data.data?.insights ? (
          <details className="dc-extra">
            <summary>平台另外还给了一段没有名字的画像列表（{data.data.insights.length} 项）</summary>
            <Insights insights={data.data.insights} />
          </details>
        ) : null}
      </div>
    </div>
  )
}
