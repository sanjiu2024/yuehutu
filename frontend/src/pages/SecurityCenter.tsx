import { useMemo, useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice, Stat } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconExternal, IconInfo, IconRefresh, IconShield } from '../icons'
import { formatTime } from '../format'
import type { Account } from '../types'
import './security-center.css'

/**
 * 真机结构（2026-10-10 打包版取证）：PC 文案不在顶层，而是在
 * `viewConfig.viewConfig.{punishTitleFromPc,punishReasonFromPc,punishResultFromPc,operationSuggestFromPc}`；
 * 顶层 `punishReason` 是 null，所以只读顶层会显示成 `-`。
 * 官方申诉入口在 `viewConfig.appealConfig.appealUrl`（`canAppeal:false` 时为空串）。
 */
type PcConfig = {
  contentType?: unknown
  punishTitleFromPc?: string
  punishReasonFromPc?: string
  punishResultFromPc?: string
  operationSuggestFromPc?: string
}
type Violation = {
  punishId?: number
  punishTitle?: string
  punishReason?: string
  punishResult?: string
  punishTime?: number
  point?: number
  status?: string | number
  appealTimeLimit?: string
  action?: { actionType?: string; actionUrl?: string; actionTypes?: { linkTitle?: string; linkUrl?: string }[] }
  viewConfig?: {
    viewConfig?: PcConfig
    appealConfig?: { canAppeal?: boolean; appealUrl?: string }
  }
}
type Result = {
  score?: any
  violations?: { data?: Violation[]; totalCount?: number }
  source?: string
  fetched_at?: string
}

function when(ms: unknown): string {
  if (!ms) return '-'
  const n = Number(ms)
  if (!Number.isFinite(n) || n <= 0) return '-'
  const d = new Date(n)
  const pad = (v: number) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function scoreOf(score: any): string {
  const value = score?.data?.totalScore ?? score?.totalScore
  return value === null || value === undefined ? '-' : String(value)
}

/** 平台对同一件事给两套字段：移动端那套常为 null，PC 文案在 `*FromPc` 里（真机取证）。 */
function pick(...values: (string | null | undefined)[]): string {
  for (const value of values) {
    const str = String(value ?? '').trim()
    if (str) return str
  }
  return '-'
}

/** 官方申诉入口：只打开链接，工作台自己绝不提交申诉。 */
function appealUrl(row: Violation): string {
  const official = row.viewConfig?.appealConfig
  if (official && official.canAppeal !== false && official.appealUrl) return String(official.appealUrl).trim()
  return String(row.action?.actionTypes?.[0]?.linkUrl || row.action?.actionUrl || '').trim()
}

export function SecurityCenterPage() {
  const [picked, setPicked] = useState('')
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const accountId = picked || accounts.data?.[0]?.id || ''
  const data = useAsync<Result>(
    () => api.get(`/api/security-center/${encodeURIComponent(accountId)}`),
    [accountId], Boolean(accountId), ['accounts.changed'])
  const score = data.data?.score
  const rows = useMemo(() => data.data?.violations?.data || [], [data.data])
  const total = data.data?.violations?.totalCount ?? rows.length
  const lastChange = score?.data?.lastChangedScoreRecord

  return <>
    <section className="page-head">
      <div><h3><IconShield size={18} /> 安全中心</h3><p>闲气值与违规记录（本页只读；申诉提交要显式确认）</p></div>
      <button className="btn sm" onClick={data.reload} disabled={data.loading}><IconRefresh size={14} /> 刷新</button>
    </section>

    <section className="card sc-account">
      <label>账号<select value={accountId} onChange={(e) => setPicked(e.target.value)}>
        {accounts.data?.map((a) => <option key={a.id} value={a.id}>{a.nickname || a.id}</option>)}
      </select></label>
      <span className="sc-hint">安全中心按账号查，一个账号一套数据</span>
    </section>

    <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} empty={!data.data} onRetry={data.reload} emptyTitle="安全中心暂无数据" emptyDesc="平台没有返回可识别的安全中心数据" emptyIcon="">
      <div className="sc-grid">
        <Stat label="当前闲气值" value={scoreOf(score)} small={false} extra={lastChange?.changeTypeName || ''} />
        <Stat label="违规记录" value={String(total ?? '-')} extra="平台返回的全部记录" />
        <Stat label="最近变动" value={when(lastChange?.gmtCreate ? Date.parse(String(lastChange.gmtCreate).replace(' ', 'T')) : undefined)} extra={`${lastChange?.realChangeScore ?? '-'} 分`} />
      </div>

      <div className="card">
        <div className="sc-table-head">违规记录 · 共 {total ?? '-'} 条</div>
        <div className="table-wrap"><table>
          <thead><tr><th>违规名称</th><th>违规编号</th><th>违规原因</th><th>违规影响</th><th>扣分</th><th>违规时间</th><th>申诉截止</th><th>操作</th></tr></thead>
          <tbody>{rows.map((row) => {
            const content = row.viewConfig?.viewConfig
            const url = appealUrl(row)
            return <tr key={String(row.punishId)}>
              <td><div className="sc-name">{pick(row.punishTitle, content?.punishTitleFromPc)}</div></td>
              <td className="sc-sub">{row.punishId ?? '-'}</td>
              <td className="sc-reason" title={content?.operationSuggestFromPc || ''}>{pick(row.punishReason, content?.punishReasonFromPc)}</td>
              <td className="sc-sub">{pick(row.punishResult, content?.punishResultFromPc)}</td>
              <td>{row.point === null || row.point === undefined ? '-' : `${row.point} 分`}</td>
              <td className="sc-sub">{when(row.punishTime)}</td>
              <td className="sc-sub">{row.appealTimeLimit || '-'}</td>
              <td>{url
                ? <button className="btn sm" onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}>
                    <IconExternal size={13} /> {row.action?.actionTypes?.[0]?.linkTitle || row.action?.actionType || '去申诉'}
                  </button>
                : <span className="sc-sub">-</span>}</td>
            </tr>
          })}</tbody>
        </table></div>
        {rows.length === 0 && <div className="sc-empty">平台没有返回违规记录</div>}
      </div>

      <Notice kind="info"><IconInfo size={15} /> 本页只读，页面上没有提交入口 —— 想自己在官方页面提交就点「去申诉」。申诉<b>也做成了接口</b>（<code>POST /api/security-center/&#123;account_id&#125;/appeal/submit</code> 等三条，AI 侧也能调），但它们<b>必须显式 <code>confirm</code></b>：不确认一个平台请求都不会发。数据更新于 {formatTime(data.data?.fetched_at)}</Notice>
    </AsyncBox>
  </>
}
