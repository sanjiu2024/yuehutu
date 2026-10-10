/**
 * 数据组 → 客服数据（**只读**）。
 *
 * 契约见 `docs/contracts/datacenter.md`（task-52 冻结）：
 *  - `GET /api/datacenter/{account_id}/customer-service?start=&end=`（`YYYYMMDD`，缺省后端补今天往前 7 天）
 *  - 6 个指标：询单人数 / 询单量 / 3 分钟响应率 / 平均响应时长(秒) / 未回复 / 满意度。
 *    `null` 一律 `-**，绝不补 0。
 *  - 无权限 ≠ 掉线：只给 null + 中文 warning，不提示重新扫码。
 *
 * 真机现状（账号 2217970177348）：客服接口无权限 ⇒ 指标全 null。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconClock, IconRefresh } from '../icons'
import type { Account } from '../types'
import { daysAgo, isRangeOk, num, pct, realRangeText, secs, type MetricDef } from './datacenterShared'
import './datacenter.css'

const CS_METRICS: MetricDef[] = [
  { key: 'inquiry_visitors', label: '询单人数', tip: '发起过咨询的独立买家数' },
  { key: 'inquiry_count', label: '询单量', tip: '咨询消息总条数 / 会话数（平台口径）' },
  { key: 'reply_rate_3min', label: '3 分钟响应率', format: pct, tip: '3 分钟内回复的比例（平台原样百分数）' },
  { key: 'avg_reply_seconds', label: '平均响应时长', format: secs, tip: '首次回复的平均耗时（秒）' },
  { key: 'unreplied_count', label: '未回复', tip: '截至统计时仍未回复的咨询数' },
  { key: 'satisfaction', label: '满意度', format: pct, tip: '买家评价满意度（平台原样，可能是百分数或分数）' },
]

interface CsEnvelope {
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

export function CsDataPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [start, setStart] = useState(() => daysAgo(6))
  const [end, setEnd] = useState(() => daysAgo(0))
  const [submitted, setSubmitted] = useState(() => ({ start: daysAgo(6), end: daysAgo(0) }))

  const accountId = picked || accounts.data?.[0]?.id || ''
  const rangeOk = isRangeOk(submitted.start, submitted.end)

  const data = useAsync<CsEnvelope>(
    () =>
      api.get<CsEnvelope>(`/api/datacenter/${encodeURIComponent(accountId)}/customer-service`, {
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
          <h2 className="page-title">客服数据</h2>
          <p className="page-desc">
            询单量、响应率、平均响应时长与满意度。只读 —— 拿不到的指标显示「-」，不会用 0 冒充。
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
            <label className="dc-label" htmlFor="csd-account">
              账号
            </label>
            <select
              id="csd-account"
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
            <label className="dc-label" htmlFor="csd-start">
              开始日期
            </label>
            <input
              id="csd-start"
              className="dc-input"
              value={start}
              onChange={(e) => setStart(e.target.value.replace(/\D/g, '').slice(0, 8))}
              placeholder="YYYYMMDD"
              inputMode="numeric"
            />
          </div>
          <div className="dc-field">
            <label className="dc-label" htmlFor="csd-end">
              结束日期
            </label>
            <input
              id="csd-end"
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
          客服指标
          <span className="dc-muted">
            {data.data?.period?.from && data.data?.period?.to
              ? `区间 ${data.data.period.from} ~ ${data.data.period.to}`
              : ''}
          </span>
        </div>

        <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
          {rangeOk && (
            <>
              <MetricCards metrics={data.data?.metrics} defs={CS_METRICS} />
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
