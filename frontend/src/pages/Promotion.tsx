import { useCallback, useMemo, useState } from 'react'
import { api, ApiError, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Modal, Notice, Stat, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconChart, IconCopy, IconEdit, IconInfo, IconRefresh, IconShield } from '../icons'
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

/** 写接口目录（`GET /api/promotion/write/actions`）。 */
type WriteAction = { action: string; api: string; params?: boolean; confirm_required?: boolean }
type UnsupportedAction = { action: string; reason: string; confirm_required?: boolean }
type WriteCatalog = { actions?: WriteAction[]; unsupported?: UnsupportedAction[] }
type WriteResult = {
  action?: string; api?: string; confirmed?: boolean; result?: unknown
  written_at?: string; source?: string
}

const TABS = [
  { key: 'overview', label: '概览' },
  { key: 'campaigns', label: '擦亮计划' },
  { key: 'reports', label: '数据报表' },
  { key: 'account', label: '账户' },
  { key: 'write', label: '投放管理' },
] as const
type TabKey = (typeof TABS)[number]['key']

/**
 * 中文名与用途说明。
 *
 * 后端目录只给 `action` / mtop 名 / 是否有参数 —— **payload 字段名官方没有公开证据**，
 * 所以这里只翻译「动作名」（纯 UI 文案），绝不为任何动作编字段名、也不做字段表单。
 * 目录里出现没登记的新动作时回落到英文 action 名，不会因为少一条翻译就渲染不出来。
 */
const ACTION_LABELS: Record<string, string> = {
  campaign_edit: '编辑投放计划',
  campaign_delete: '删除投放计划',
  campaign_batch_delete: '批量删除投放计划',
  campaign_pause: '暂停投放计划',
  campaign_start: '启动投放计划',
  campaign_start_before: '启动前校验',
  item_campaign_renew: '商品计划续投',
  activity_apply: '活动报名',
  activity_task_execute: '活动任务领奖',
  account_log_operates: '账户流水操作',
  recharge: '充值',
  withdraw: '提现',
}

function label(action: string): string {
  return ACTION_LABELS[action] || action
}

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
  const toast = useToast()
  const [picked, setPicked] = useState('')
  const [tab, setTab] = useState<TabKey>('overview')
  const [from, setFrom] = useState(day(-6))
  const [to, setTo] = useState(day(0))

  // ---- 写操作（投放管理）状态 ----
  const [draft, setDraft] = useState<WriteAction | null>(null)
  const [jsonText, setJsonText] = useState('{}')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [lastError, setLastError] = useState('')
  const [lastResult, setLastResult] = useState<WriteResult | null>(null)

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
  // 写接口目录：只订阅 topic，不轮询
  const catalog = useAsync<WriteCatalog>(
    () => api.get<WriteCatalog>('/api/promotion/write/actions'),
    [], tab === 'write', ['promotion.changed', 'accounts.changed'])
  const active = { overview, campaigns, reports, account, write: catalog }[tab]
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

  const writeActions: WriteAction[] = catalog.data?.actions || []
  const unsupported: UnsupportedAction[] = catalog.data?.unsupported || []

  /** JSON 校验：必须是**对象**。非法就禁用按钮，绝不发出去。 */
  const jsonCheck = useMemo(() => {
    if (draft && draft.params === false) return { ok: true, reason: '' }
    const text = jsonText.trim()
    if (!text) return { ok: false, reason: '请求体不能为空；没有参数的动作也请填 {}' }
    try {
      const value = JSON.parse(text)
      if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        return { ok: false, reason: 'requestJson 必须是一个 JSON 对象，例如 {"campaignId": 123}' }
      }
      return { ok: true, reason: '' }
    } catch (err) {
      return { ok: false, reason: `JSON 语法错误：${(err as Error).message}` }
    }
  }, [draft, jsonText])

  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      toast.success(`已复制：${text}`)
    } catch {
      toast.error('复制失败，请手动选中复制')
    }
  }, [toast])

  const openDraft = useCallback((action: WriteAction) => {
    setDraft(action)
    setJsonText('{}')
    setConfirming(false)
    setLastError('')
  }, [])

  /** 执行写操作：`confirm` 只认字面量 true（后端也是这么判的）。 */
  const run = useCallback(async () => {
    if (!draft || !accountId) return
    setBusy(true)
    setLastError('')
    try {
      const payload = draft.params === false ? {} : JSON.parse(jsonText.trim())
      const result = await api.post<WriteResult>(
        `/api/promotion/write/${encodeURIComponent(accountId)}/${encodeURIComponent(draft.action)}`,
        { confirm: true, payload })
      setLastResult(result ?? null)
      setConfirming(false)
      setDraft(null)
      const detail = result?.result === null || result?.result === undefined
        ? '平台没有返回明细'
        : '平台已返回结果'
      toast.success(`「${label(draft.action)}」执行成功（${detail}）`)
      // 投放计划真的变了：重拉只读区块（不轮询，只在写成功后拉一次）
      overview.reload()
      campaigns.reload()
      reports.reload()
      account.reload()
    } catch (err) {
      // 失败原样显示后端 message（含平台原文），绝不显示成成功；保留 draft 让用户改完重试
      const message = errorText(err)
      const code = err instanceof ApiError ? err.code : ''
      setLastError(code ? `${message}（code=${code}）` : message)
      toast.error(`「${label(draft.action)}」执行失败：${message}`)
      setConfirming(false)
    } finally {
      setBusy(false)
    }
  }, [draft, accountId, jsonText, toast, overview, campaigns, reports, account])

  return <>
    <section className="page-head">
      <div><h3><IconChart size={18} /> 超级擦亮</h3><p>推广余额、擦亮计划与投放数据（数据只读；「投放管理」可写，需二次确认）</p></div>
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
      <Notice kind="info"><IconInfo size={15} /> 概览是只读的；创建/修改/暂停/删除投放计划在「投放管理」标签里，每个动作都要二次确认。充值、提现平台没有网页版下单接口，本工作台不支持。数据更新于 {formatTime(overview.data?.fetched_at)}</Notice>
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
      <Notice kind="info"><IconInfo size={15} /> 充值、提现、发票不在这里做 —— 平台没有网页版下单接口（提现按钮原文是「请进入闲鱼App内操作」）。账户流水操作在「投放管理」里，需要二次确认。</Notice>
    </AsyncBox>}

    {tab === 'write' && <>
      <AsyncBox loading={catalog.loading} error={catalog.error} notReady={catalog.notReady} empty={!catalog.data} onRetry={catalog.reload} emptyTitle="暂无写接口目录" emptyDesc="后端没有返回可用的写接口目录" emptyIcon="edit">
        <Notice kind="warn">
          <div className="pr-write-tip"><b>这些按钮会真的改动闲鱼上的投放计划，并可能产生花费。</b>每个动作都要过二次确认；成功后本工作台不能撤销，请核对好再点。</div>
          <div>请求体字段名官方 bundle 里没有公开证据，所以本页只收<b>原始 JSON</b>，不替你编字段名、也不做字段表单。</div>
        </Notice>

        <div className="card">
          <div className="pr-table-head">
            <IconShield size={13} /> 投放管理 · 共 {num(writeActions.length)} 个写动作 · 需要二次确认
          </div>
          <div className="table-wrap"><table>
            <thead><tr><th>动作</th><th>mtop 接口</th><th>请求参数</th><th>操作</th></tr></thead>
            <tbody>{writeActions.map((item) => <tr key={item.action}>
              <td>
                <div className="pr-name">{label(item.action)}</div>
                <div className="pr-sub mono">{item.action}</div>
              </td>
              <td>
                <div className="pr-api">
                  <span className="mono">{item.api}</span>
                  <button className="btn sm ghost" onClick={() => copy(item.api)} title="复制 mtop 接口名"><IconCopy size={13} /> 复制</button>
                </div>
              </td>
              <td className="pr-sub">
                {item.params === false
                  ? '无参数：官方请求 data 是空对象 {}'
                  : 'requestJson：原始 JSON 对象，字段名以平台为准'}
              </td>
              <td>
                <button className="btn danger sm" onClick={() => openDraft(item)} disabled={!accountId}>
                  <IconEdit size={13} /> {label(item.action)}
                </button>
              </td>
            </tr>)}</tbody>
          </table></div>
        </div>

        {unsupported.length > 0 && <div className="card">
          <div className="pr-table-head">本工作台不做的动作 · 不是 bug</div>
          <div className="pr-unsupported">
            {unsupported.map((item) => <Notice kind="warn" key={item.action}>
              <div><b>{label(item.action)}不支持 —— 这不是 bug。</b>平台没有网页版下单接口：官方账户页的提现按钮原文就是「请进入闲鱼App内操作」，充值同理。本工作台不代下单，请在闲鱼 App 内操作。</div>
              <div className="pr-sub">后端说明：{item.reason}</div>
            </Notice>)}
          </div>
        </div>}

        {lastResult && <div className="card">
          <div className="pr-table-head">最近一次写操作结果 · {lastResult.action ? label(lastResult.action) : '-'} · {formatTime(lastResult.written_at)}</div>
          <div className="pr-result">
            <div className="pr-sub">接口 <span className="mono">{lastResult.api || '-'}</span> · 账号 {accountName}</div>
            <div className="code-block mono">{JSON.stringify(lastResult.result ?? null, null, 2)}</div>
            <div className="pr-sub">平台没有返回内容时显示 null（不补 0）。</div>
          </div>
        </div>}

        {lastError && <div className="pr-err">
          <Notice kind="error" onClose={() => setLastError('')}>执行失败：{lastError}</Notice>
        </div>}
      </AsyncBox>
    </>}

    {draft && !confirming && <Modal
      title={`${label(draft.action)} · 填写 requestJson`}
      onClose={() => { setDraft(null); setLastError('') }}
      footer={<>
        <button className="btn ghost" onClick={() => setDraft(null)} disabled={busy}>取消</button>
        <button className="btn danger" onClick={() => setConfirming(true)} disabled={!jsonCheck.ok || !accountId}>
          <IconShield size={14} /> 下一步：二次确认
        </button>
      </>}>
      <div className="pr-sub">账号 {accountName}（{accountId || '未选账号'}）· mtop 接口 <span className="mono">{draft.api}</span></div>
      {draft.params === false
        ? <Notice kind="info">该动作官方请求 <span className="mono">data</span> 是空对象 <span className="mono">{'{}'}</span>，无需填写参数。</Notice>
        : <>
          <p>业务对象原样放进 <span className="mono">requestJson</span>：官方 bundle 没有公开字段名，本页不猜字段、不做表单，填错会被平台拒绝，错误原文会显示出来。</p>
          <textarea
            className={`pr-json mono${jsonCheck.ok ? '' : ' bad'}`}
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            spellCheck={false}
            placeholder='{"campaignId": 12345}'
            aria-label="requestJson 原始 JSON"
          />
          <div className={`pr-json-hint${jsonCheck.ok ? '' : ' bad'}`}>
            {jsonCheck.ok ? 'JSON 语法正确（本页只校验语法，不校验字段名）' : jsonCheck.reason}
          </div>
        </>}
      {lastError && <div className="pr-err"><Notice kind="error">上次执行失败：{lastError}</Notice></div>}
    </Modal>}

    {draft && confirming && <ConfirmDialog
      danger
      busy={busy}
      title={`确认执行「${label(draft.action)}」`}
      confirmText="确认执行"
      onCancel={() => setConfirming(false)}
      onConfirm={run}
      message={<>
        <div>这会<b>真的</b>改动闲鱼上的投放计划，并可能产生花费；成功后本工作台无法撤销。</div>
        <div className="pr-sub">账号：{accountName}（{accountId}）</div>
        <div className="pr-sub">接口：<span className="mono">{draft.api}</span></div>
        <div className="pr-preview mono">{draft.params === false ? '{}' : jsonText.trim()}</div>
      </>}
    />}
  </>
}
