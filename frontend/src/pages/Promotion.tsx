import { useMemo, useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice, Stat } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconInfo, IconRefresh } from '../icons'
import { formatTime } from '../format'
import type { Account } from '../types'
import './promotion.css'

type Period = { from?: string; to?: string }
type Overview = {
  home?: any; account?: any; metrics?: any; period?: Period; fetched_at?: string
}
type Campaigns = { campaigns?: any; period?: Period; fetched_at?: string }
type Reports = { plans?: any; items?: any; period?: Period; fetched_at?: string }
type AccountView = { account?: any; settle?: any; period?: Period; fetched_at?: string }

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'campaigns', label: '擦亮计划' },
  { key: 'reports', label: '数据报表' },
  { key: 'account', label: '账户' },
] as const
type TabKey = (typeof TABS)[number]['key']

function day(offset: number): string {
  const d = new Date()
  d.setDate(d.getDate() + offset)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
}

/** 平台指标里的金额单位是**分**；拿不到就显示「-」，绝不补 0。 */
function cents(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return (n / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** 计数与比率：拿不到显示「-」。 */
function num(value: unknown, digits = 0): string {
  if (value === null || value === undefined || value === '') return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return '-'
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

function metric(source: any, key: string): unknown {
  return source?.data?.[key] ?? source?.[key]
}

function StatusTag({ text }: { text: unknown }) {
  const label = text === null || text === undefined || text === '' ? '-' : String(text)
  return <span className="pr-tag">{label}</span>
}

export function PromotionPage() {
  const [picked, setPicked] = useState('')
  const [tab, setTab] = useState<TabKey>('overview')
  const [from, setFrom] = useState(day(-6))
  const [to, setTo] = useState(day(0))
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const accountId = picked || accounts.data?.[0]?.id || ''
  const range = `start=${from}&end=${to}`
  const overview = useAsync<Overview>(
    () => api.get(`/api/promotion/${encodeURIComponent(accountId)}/overview?${range}`),
    [accountId, range], Boolean(accountId) && tab === 'overview', ['accounts.changed'])
  const campaigns = useAsync<Campaigns>(
    () => api.get(`/api/promotion/${encodeURIComponent(accountId)}/campaigns?${range}&size=20`),
    [accountId, range], Boolean(accountId) && tab === 'campaigns', ['accounts.changed'])
  const reports = useAsync<Reports>(
    () => api.get(`/api/promotion/${encodeURIComponent(accountId)}/reports?${range}&size=20`),
    [accountId, range], Boolean(accountId) && tab === 'reports', ['accounts.changed'])
  const account = useAsync<AccountView>(
    () => api.get(`/api/promotion/${encodeURIComponent(accountId)}/account?${range}`),
    [accountId, range], Boolean(accountId) && tab === 'account', ['accounts.changed'])
  const active = { overview, campaigns, reports, account }[tab]
  const accountName = useMemo(
    () => accounts.data?.find((a) => a.id === accountId)?.nickname || accountId,
    [accounts.data, accountId])

  const home = overview.data?.home
  const card = (overview.data?.account || account.data?.account) as any
  const metrics = overview.data?.metrics
  const campaignRows: any[] = campaigns.data?.campaigns?.result || []
  const planRows: any[] = reports.data?.plans?.result || []
  const itemRows: any[] = reports.data?.items?.result || []
  const settle = account.data?.settle as any
  const balance = card?.totalUsableBalanceOfYuan ?? card?.cashBalanceOfYuan

  return <>
    <section className="page-head">
      <div><h3><IconChart size={18} /> 超级擦亮</h3><p>推广余额、擦亮计划与投放数据（只读）</p></div>
      <button className="btn sm" onClick={active.reload} disabled={active.loading}><IconRefresh size={14} /> 刷新</button>
    </section>

    <section className="card pr-bar">
      <label>账号<select value={accountId} onChange={(e) => setPicked(e.target.value)}>
        {accounts.data?.map((a) => <option key={a.id} value={a.id}>{a.nickname || a.id}</option>)}
      </select></label>
      <label>起始日期<input value={from} onChange={(e) => setFrom(e.target.value.trim())} placeholder="YYYYMMDD" /></label>
      <label>结束日期<input value={to} onChange={(e) => setTo(e.target.value.trim())} placeholder="YYYYMMDD" /></label>
      <span className="pr-hint">日期格式 YYYYMMDD，默认最近 7 天；{accountName}</span>
    </section>

    <nav className="tabs pr-tabs">{TABS.map((t) => (
      <button key={t.key} className={`tab${tab === t.key ? ' active' : ''}`} onClick={() => setTab(t.key)}>{t.label}</button>
    ))}</nav>

    {tab === 'overview' && <AsyncBox loading={overview.loading} error={overview.error} notReady={overview.notReady} empty={!overview.data} onRetry={overview.reload} emptyTitle="暂无推广数据" emptyDesc="平台没有返回可识别的只读推广数据" emptyIcon="">
      <div className="pr-grid">
        <Stat label="可用余额（元）" value={balance ?? '-'} />
        <Stat label="现金余额（元）" value={card?.cashBalanceOfYuan ?? '-'} />
        <Stat label="擦亮花费（元）" value={cents(metric(metrics, 'cost'))} />
        <Stat label="曝光数" value={num(metric(metrics, 'showPv'))} />
        <Stat label="点击数" value={num(metric(metrics, 'clickPv'))} />
        <Stat label="询单量" value={num(metric(metrics, 'inquiries'))} />
        <Stat label="下单笔数" value={num(metric(metrics, 'payPv'))} />
        <Stat label="点击率（%）" value={num(metric(metrics, 'ctr'), 2)} />
      </div>
      <div className="pr-grid pr-grid-4">
        <Stat label="擦亮中计划" value={num(home?.campaignInfo?.allCampaignCount)} />
        <Stat label="系统暂停" value={num(home?.campaignInfo?.systemPauseCampaignCount)} />
        <Stat label="手动暂停" value={num(home?.campaignInfo?.userPauseCampaignCount)} />
        <Stat label="结算金额（元）" value={settle?.totalAmountOfYuan ?? '-'} />
      </div>
      <Notice kind="info"><IconInfo size={15} /> 本页只读推广数据，不提供充值、提现、创建或修改投放计划。数据更新于 {formatTime(overview.data?.fetched_at)}</Notice>
    </AsyncBox>}

    {tab === 'campaigns' && <AsyncBox loading={campaigns.loading} error={campaigns.error} notReady={campaigns.notReady} empty={!campaigns.data} onRetry={campaigns.reload} emptyTitle="暂无擦亮计划" emptyDesc="这个区间里平台没有返回计划" emptyIcon="">
      <div className="card">
        <div className="pr-table-head">擦亮计划 · 共 {num(campaigns.data?.campaigns?.total)} 个 · 金额单位为元</div>
        <div className="table-wrap"><table>
          <thead><tr><th>计划</th><th>宝贝</th><th>类型</th><th>状态</th><th>投放日期</th><th>花费（元）</th><th>曝光</th><th>点击</th></tr></thead>
          <tbody>{campaignRows.map((row) => {
            const first = (row.entityList || [])[0] || {}
            const data = row.adDataDetailVO || {}
            return <tr key={String(row.campaignId)}>
              <td><div className="pr-name">{row.title || `计划 ${row.campaignId}`}</div><div className="pr-sub">{row.campaignId}</div></td>
              <td><div className="pr-item">{first.imgUrl ? <img src={first.imgUrl} alt="" /> : <span className="pr-noimg" />}<span>{(row.entityList || []).length > 1 ? `${(row.entityList || []).length} 个宝贝` : (first.title || '-')}</span></div></td>
              <td>{row.campaignTypeName || '-'}</td>
              <td><StatusTag text={row.statusDescCode || row.status} /></td>
              <td className="pr-sub">{row.startTime ? `${String(row.startTime).slice(0, 10)} 起` : '-'}<br />{row.endTime ? `${String(row.endTime).slice(0, 10)} 止` : '不限'}</td>
              <td>{cents(data.cost)}</td>
              <td>{num(data.showPv)}</td>
              <td>{num(data.clickPv)}</td>
            </tr>
          })}</tbody>
        </table></div>
      </div>
    </AsyncBox>}

    {tab === 'reports' && <AsyncBox loading={reports.loading} error={reports.error} notReady={reports.notReady} empty={!reports.data} onRetry={reports.reload} emptyTitle="暂无报表数据" emptyDesc="这个区间里平台没有返回报表" emptyIcon="">
      <div className="card">
        <div className="pr-table-head">计划报表 · 共 {num(reports.data?.plans?.total)} 条</div>
        <div className="table-wrap"><table>
          <thead><tr><th>计划 ID</th><th>花费（元）</th><th>曝光</th><th>点击</th><th>点击率（%）</th><th>询单</th><th>成交金额（元）</th></tr></thead>
          <tbody>{planRows.map((row) => {
            const data = row.idleAdGroupDataVO?.data || {}
            return <tr key={String(row.campaignId)}>
              <td>{row.campaignId}</td><td>{cents(data.cost)}</td><td>{num(data.showPv)}</td>
              <td>{num(data.clickPv)}</td><td>{num(data.ctr, 2)}</td><td>{num(data.inquiries)}</td>
              <td>{cents(data.payAmount)}</td>
            </tr>
          })}</tbody>
        </table></div>
      </div>
      <div className="card">
        <div className="pr-table-head">商品报表 · 共 {num(reports.data?.items?.total)} 条</div>
        <div className="table-wrap"><table>
          <thead><tr><th>商品</th><th>价格（元）</th><th>花费（元）</th><th>曝光</th><th>点击</th><th>点击率（%）</th><th>询单</th></tr></thead>
          <tbody>{itemRows.map((row) => {
            const data = row.adData || {}
            return <tr key={String(row.itemId)}>
              <td><div className="pr-item">{row.imgUrl ? <img src={row.imgUrl} alt="" /> : <span className="pr-noimg" />}<span>{row.title || row.itemId}</span></div></td>
              <td>{cents(row.price)}</td><td>{cents(data.cost)}</td><td>{num(data.showPv)}</td>
              <td>{num(data.clickPv)}</td><td>{num(data.ctr, 2)}</td><td>{num(data.inquiries)}</td>
            </tr>
          })}</tbody>
        </table></div>
      </div>
    </AsyncBox>}

    {tab === 'account' && <AsyncBox loading={account.loading} error={account.error} notReady={account.notReady} empty={!account.data} onRetry={account.reload} emptyTitle="暂无账户数据" emptyDesc="平台没有返回可识别的账户数据" emptyIcon="">
      <div className="pr-grid">
        <Stat label="可用余额（元）" value={card?.totalUsableBalanceOfYuan ?? '-'} />
        <Stat label="现金余额（元）" value={card?.cashBalanceOfYuan ?? '-'} />
        <Stat label="赠款余额（元）" value={card?.bonusBalanceOfYuan ?? '-'} />
        <Stat label="冻结余额（元）" value={card?.frozenBalanceOfYuan ?? '-'} />
      </div>
      <div className="pr-grid pr-grid-4">
        <Stat label="结算合计（元）" value={settle?.totalAmountOfYuan ?? '-'} />
        <Stat label="现金结算（元）" value={settle?.cashAmountOfYuan ?? '-'} />
        <Stat label="礼金（元）" value={settle?.giftAmountOfYuan ?? '-'} />
        <Stat label="红包（元）" value={settle?.redPacketAmountOfYuan ?? '-'} />
      </div>
      <Notice kind="info"><IconInfo size={15} /> 充值、提现、账户流水与发票都不在这里做 —— 这些要么涉及付款，要么需要平台写接口。</Notice>
    </AsyncBox>}
  </>
}
