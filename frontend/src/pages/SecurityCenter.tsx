import { useMemo, useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice, Stat } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconInfo, IconRefresh, IconShield } from '../icons'
import type { Account } from '../types'
import './security-center.css'

type Violation = {
  punishId?: number
  punishTitle?: string
  punishReason?: string
  punishResult?: string
  punishTime?: number
  point?: number
  status?: string | number
  appealTimeLimit?: string
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
      <div><h3><IconShield size={18} /> 安全中心</h3><p>闲气值与违规记录（只读，不会替你申诉）</p></div>
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
          <thead><tr><th>违规名称</th><th>违规编号</th><th>违规原因</th><th>违规影响</th><th>扣分</th><th>违规时间</th><th>申诉截止</th></tr></thead>
          <tbody>{rows.map((row) => <tr key={String(row.punishId)}>
            <td><div className="sc-name">{row.punishTitle || '-'}</div><div className="sc-sub">{row.status === null || row.status === undefined ? '' : `状态 ${row.status}`}</div></td>
            <td className="sc-sub">{row.punishId ?? '-'}</td>
            <td className="sc-reason">{row.punishReason || '-'}</td>
            <td className="sc-sub">{row.punishResult || '-'}</td>
            <td>{row.point === null || row.point === undefined ? '-' : `${row.point} 分`}</td>
            <td className="sc-sub">{when(row.punishTime)}</td>
            <td className="sc-sub">{row.appealTimeLimit || '-'}</td>
          </tr>)}</tbody>
        </table></div>
        {rows.length === 0 && <div className="sc-empty">平台没有返回违规记录</div>}
      </div>

      <Notice kind="info"><IconInfo size={15} /> 本页只读。要申诉请点官方入口自己提交 —— 工作台不会替你提交任何申诉或上传材料。数据更新于 {data.data?.fetched_at || '-'}</Notice>
    </AsyncBox>
  </>
}
