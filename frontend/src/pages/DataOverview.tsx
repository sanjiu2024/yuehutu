/**
 * 数据组 → 数据总览（**只读**）。
 *
 * 契约见 `docs/contracts/datacenter.md`（task-52 冻结）：
 *  - `GET /api/datacenter/{account_id}/overview?start=&end=`（`YYYYMMDD`，缺省后端补今天往前 7 天）
 *  - 9 个指标：曝光/访问/浏览/在线商品/询单，`null` 一律 `-**，绝不补 0。
 *  - 无权限 ≠ 掉线：只给 null + 中文 warning，不提示重新扫码。
 *
 * 真机现状（账号 2217970177348）：数据总览接口无权限 ⇒ 指标全 null、warnings 说明原因。
 * 官方页面对照值（待有权限账号复验）：访问 124 / 曝光 1971 / 浏览 97 / 在线 45 / 询单 8 / 3 分钟响应率 33.33%。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconClock, IconRefresh } from '../icons'
import type { Account } from '../types'
import { daysAgo, isRangeOk, num, pct, realRangeText, type MetricDef } from './datacenterShared'
import './datacenter.css'

const OVERVIEW_METRICS: MetricDef[] = [
  { key: 'exposure', label: '曝光次数', tip: '商品在搜索 / 推荐里被展示的次数' },
  { key: 'exposure_visitors', label: '曝光人数', tip: '看到过曝光的独立买家数' },
  { key: 'visits', label: '商品访问次数', tip: '商品详情页被打开的次数' },
  { key: 'visit_visitors', label: '访问人数', tip: '打开过详情页的独立买家数' },
  { key: 'browse', label: '浏览次数', tip: '详情页有效浏览次数' },
  { key: 'browse_visitors', label: '浏览人数', tip: '有效浏览的独立买家数' },
  { key: 'online_items', label: '在线商品数', tip: '当前在架商品数量' },
  { key: 'inquiry_visitors', label: '询单人数', tip: '发起过咨询的独立买家数' },
  {
    key: 'inquiry_reply_rate_3min',
    label: '3 分钟询单响应率',
    format: pct,
    tip: '3 分钟内回复咨询的比例（平台原样百分数）',
  },
]

interface OverviewEnvelope {
  account_id?: string
  kind?: string
  period?: { from?: string; to?: string } | null
  metrics?: Record<string, unknown> | null
  begin_time?: unknown
  real_date_range?: unknown
  warnings?: string[]
  unrecognized_keys?: string[]
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

/* ------------------------------------------------------------------ *
 * 经营卡片（交易数据 / 经营概况 / 复购 / 退款概览）
 *
 * 官方页面把这一块做成四张卡，各自一个接口。我们照抄：
 *  - 一次一张，各查各的 ⇒ 一张没权限不会让整页空白；
 *  - 指标名与中文标题都以平台为准：`label` 为 null 时显示平台字段名，
 *    **绝不自己编中文**（平台新增指标时编出来的名字会骗人）；
 *  - 平台没给的值是 null，显示「-」，不用 0 冒充。
 * ------------------------------------------------------------------ */

interface CardMetric {
  key?: string
  label?: string | null
  value?: unknown
  ratio?: string | null
  ratio_format?: string | null
  cycle?: string | null
  last?: string | null
  platform_name?: string | null
}

interface CardPoint {
  date?: string | null
  value?: unknown
}

interface CardSeries {
  key?: string
  label?: string | null
  points?: CardPoint[] | null
}

interface CardEnvelope {
  kind?: string
  title?: string | null
  period?: { from?: string; to?: string } | null
  metrics?: CardMetric[] | null
  banner?: CardMetric[] | null
  series?: CardSeries[] | null
  promotion?: unknown
  extra?: Record<string, unknown> | null
  real_date_range?: unknown
  warnings?: string[]
  fetched_at?: string
}

const CARD_TITLES: Record<string, string> = {
  trade: '交易数据',
  seller: '经营概况',
  repurchase: '复购',
  refund: '退款概览',
}

/** 渲染顺序照官方页面：交易 → 经营概况 → 复购 → 退款概览。 */
const CARD_ORDER = ['trade', 'seller', 'repurchase', 'refund']

/** 指标的显示名：平台给了中文就用平台的，否则用平台字段名（不猜）。 */
function metricName(metric: CardMetric): string {
  return metric.label || metric.platform_name || metric.key || '（未命名字段）'
}

function metricValue(metric: CardMetric): string {
  const value = metric.value
  if (value === null || value === undefined || value === '') return '-'
  return typeof value === 'number' ? num(value) : String(value)
}

/** 折线：值不是数字的点跳过（留空档，不插 0 —— 插 0 会画出一条假的谷底）。 */
function sparklinePoints(points: CardPoint[] | null | undefined): string {
  const rows = points || []
  if (rows.length < 2) return ''
  const values = rows.map((p) => (typeof p.value === 'number' ? p.value : Number(p.value)))
  const usable = values.filter((v) => Number.isFinite(v))
  if (usable.length < 2) return ''
  const min = Math.min(...usable)
  const max = Math.max(...usable)
  const span = max - min || 1
  const step = 100 / (values.length - 1)
  return values
    .map((v, i) => (Number.isFinite(v) ? `${(i * step).toFixed(2)},${(28 - ((v - min) / span) * 26).toFixed(2)}` : ''))
    .filter(Boolean)
    .join(' ')
}

function CardBlock({ accountId, kind, start, end }: { accountId: string; kind: string; start: string; end: string }) {
  const card = useAsync<CardEnvelope>(
    () =>
      api.get<CardEnvelope>(`/api/datacenter/${encodeURIComponent(accountId)}/card/${kind}`, {
        start,
        end,
      }),
    [accountId, kind, start, end],
    Boolean(accountId),
    ['accounts.changed'],
  )
  const banner = card.data?.banner || []
  const series = card.data?.series || []
  const metrics = card.data?.metrics || []
  const extraKeys = Object.keys(card.data?.extra || {})

  return (
    <div className="dc-opcard">
      <div className="dc-opcard-head">
        <span className="dc-ico">
          <IconChart size={14} />
        </span>
        {card.data?.title || CARD_TITLES[kind] || kind}
        {card.data?.period?.from && card.data?.period?.to ? (
          <span className="dc-muted">
            {card.data.period.from} ~ {card.data.period.to}
          </span>
        ) : null}
      </div>
      <AsyncBox loading={card.loading} error={card.error} notReady={card.notReady} onRetry={card.reload}>
        {banner.length === 0 && series.length === 0 && metrics.length === 0 ? (
          <div className="dc-muted">这一张暂时没有数据。</div>
        ) : null}
        {banner.length > 0 && (
          <div className="dc-grid dc-grid-sm">
            {banner.map((metric, idx) => (
              <div className="dc-card" key={`${metric.key || idx}`} title={metric.key || ''}>
                <div className="dc-card-label">{metricName(metric)}</div>
                <div className={`dc-card-value${metric.value === null || metric.value === undefined ? ' dc-missing' : ''}`}>
                  {metricValue(metric)}
                </div>
                {(metric.ratio || metric.last || metric.cycle) && (
                  <div className="dc-card-sub">
                    {[metric.cycle, metric.ratio, metric.last].filter(Boolean).join(' · ')}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {series.map((line, idx) => (
          <div className="dc-line" key={`${line.key || idx}`}>
            <div className="dc-line-head">
              <span>{line.label || line.key}</span>
              <span className="dc-muted">
                {line.points && line.points.length > 0
                  ? `最新 ${metricValue({ value: line.points[line.points.length - 1]?.value })}`
                  : ''}
              </span>
            </div>
            {sparklinePoints(line.points) ? (
              <svg className="dc-spark" viewBox="0 0 100 30" preserveAspectRatio="none" role="img"
                   aria-label={`${line.label || line.key} 折线`}>
                <polyline points={sparklinePoints(line.points)} />
              </svg>
            ) : (
              <div className="dc-muted">折线点不够（平台只给了 {line.points?.length || 0} 个点）。</div>
            )}
          </div>
        ))}
        {metrics.length > 0 && (
          <div className="dc-metrics">
            {metrics.map((metric, idx) => (
              <div className="dc-metric-row" key={`${metric.key || idx}`}>
                <span className="dc-metric-name">{metricName(metric)}</span>
                <span className={metric.value === null || metric.value === undefined ? 'dc-missing' : ''}>
                  {metricValue(metric)}
                </span>
                {(metric.ratio || metric.cycle) && (
                  <span className="dc-muted">{[metric.cycle, metric.ratio].filter(Boolean).join(' · ')}</span>
                )}
              </div>
            ))}
          </div>
        )}
        {(card.data?.warnings || []).map((w, idx) => (
          <Notice key={idx} kind="warn">
            {w}
          </Notice>
        ))}
        {(extraKeys.length > 0 || Boolean(card.data?.promotion)) && (
          <details className="dc-extra">
            <summary>平台原始字段（不翻译、不补 0）</summary>
            <pre className="code-block">
              {JSON.stringify(
                { promotion: card.data?.promotion ?? null, extra: card.data?.extra ?? {} },
                null,
                2,
              )}
            </pre>
          </details>
        )}
      </AsyncBox>
    </div>
  )
}

export function DataOverviewPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [start, setStart] = useState(() => daysAgo(6))
  const [end, setEnd] = useState(() => daysAgo(0))
  const [submitted, setSubmitted] = useState(() => ({ start: daysAgo(6), end: daysAgo(0) }))

  const accountId = picked || accounts.data?.[0]?.id || ''
  const rangeOk = isRangeOk(submitted.start, submitted.end)

  const data = useAsync<OverviewEnvelope>(
    () =>
      api.get<OverviewEnvelope>(`/api/datacenter/${encodeURIComponent(accountId)}/overview`, {
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
          <h2 className="page-title">数据总览</h2>
          <p className="page-desc">
            曝光、访问、浏览、在线商品与询单响应。只读 —— 拿不到的指标显示「-」，不会用 0 冒充。
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
            <label className="dc-label" htmlFor="ov-account">
              账号
            </label>
            <select
              id="ov-account"
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
            <label className="dc-label" htmlFor="ov-start">
              开始日期
            </label>
            <input
              id="ov-start"
              className="dc-input"
              value={start}
              onChange={(e) => setStart(e.target.value.replace(/\D/g, '').slice(0, 8))}
              placeholder="YYYYMMDD"
              inputMode="numeric"
            />
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="ov-end">
              结束日期
            </label>
            <input
              id="ov-end"
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
          核心指标
          <span className="dc-muted">
            {data.data?.period?.from && data.data?.period?.to
              ? `区间 ${data.data.period.from} ~ ${data.data.period.to}`
              : ''}
          </span>
        </div>

        <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
          {rangeOk && (
            <>
              <MetricCards metrics={data.data?.metrics} defs={OVERVIEW_METRICS} />
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
            <IconChart size={15} />
          </span>
          经营卡片
          <span className="dc-muted">四张卡各查各的，跟着上面的区间走</span>
        </div>
        {accountId && rangeOk ? (
          <div className="dc-opcards">
            {CARD_ORDER.map((kind) => (
              <CardBlock
                key={kind}
                accountId={accountId}
                kind={kind}
                start={submitted.start}
                end={submitted.end}
              />
            ))}
          </div>
        ) : (
          <div className="dc-muted">先选一个账号、再给一个合法区间。</div>
        )}
        <div className="dc-note">
          <span className="dc-ico">
            <IconClock size={13} />
          </span>
          卡片里的指标名以平台为准：平台新增了我们没登记过的指标时，这里显示平台字段名，不会编一个中文名。
        </div>
      </div>
    </div>
  )
}
