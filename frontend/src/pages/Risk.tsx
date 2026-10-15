/** 风控：熔断状态、冷却阶梯、风险事件、自动滑块开关。
 *
 * 后端真实形状（services/risk_service.py）：
 *  - `GET /api/risk/status`（不带 account_id）→
 *      `{states: [...], blocked: [账号id...], cooldown_steps: [...], auto_slider, refresh_interval_seconds}`
 *    注意 `blocked` 是**账号 id 数组**（带 account_id 查询时它才是 bool）。
 *  - `GET /api/risk/events` → `{events: [...], total}`，事件字段只有 `kind` / `message`。
 *  - 设置在 `GET|PUT /api/risk/settings`（**不是** `/api/settings`）。
 *  - 清除冷却 `POST /api/risk/{account_id}/clear`：**不带任何 body 字段**。
 *    后端已加固（api/risk.py:65-76、tests/test_api_hardening.py），
 *    请求体里的 `{verified: true}` 不再算凭证 —— 凭证只能由服务端滑块任务 `mark_verified()` 写下。
 *    再传 `verified` 只会被忽略，传了等于自欺，所以这里传空对象。
 *
 * 冷却剩余时间**只能**由后端字段推算：`state.blocked_until`（epoch 秒）与
 * `state.remaining_seconds`（后端算好的整数秒）。前端每秒只做**本地重绘**，不发任何请求。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Field, Notice, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatDuration, formatTime, loginStateLabel } from '../format'
import {
  IconCheckCircle,
  IconClock,
  IconInfo,
  IconRefresh,
  IconShield,
  IconUsers,
  IconWarn,
} from '../icons'
import type { Account, ClearResult, RiskEvent, RiskEventListResult, RiskSettings, RiskStatusAll, RiskState } from '../types'
import './risk.css'

const DEFAULT_STEPS = '60,180,300,600,1200'
/** 层级太多会撑爆卡片，也不符合真实阶梯的用途。 */
const MAX_STEPS = 12
const MAX_STEP_SECONDS = 86400

/** 冷却分段（用于分段格子 stepper 与连续进度条）。 */
interface CooldownSegment {
  key: string
  /** 这一段从第几秒开始（倒序推进：越靠右的段落越先走完）。 */
  from: number
  fill: number
  state: 'done' | 'active' | 'todo'
}

interface CooldownView {
  total: number
  remaining: number
  /** 已完成比例 0~1，用于连续进度条宽度。 */
  ratio: number
  segments: CooldownSegment[]
}

/** 把 blocked_until（epoch 秒）换算成剩余秒数；容忍毫秒时间戳。 */
function remainingFrom(blockedUntil: number | undefined, remainingSeconds: number | undefined): number {
  const remote = Math.max(0, Math.round(Number(remainingSeconds) || 0))
  const until = Number(blockedUntil)
  if (until > 0) {
    const ms = until > 1e12 ? until : until * 1000
    // 本地推算可能因为「客户端时钟比服务端快」或「这行数据已经陈旧」而算出 0；
    // 后端给的 remaining_seconds 是权威值，取两者较大者，免得明明还在冷却却显示 0 秒。
    return Math.max(0, Math.max(Math.round((ms - Date.now()) / 1000), remote))
  }
  // 没有 blocked_until 时退回后端算好的秒数（同样不轮询，只是本地递减）
  return remote
}

/** 后端阶梯可能比本地输入框更新，优先用 /api/risk/status 回传的那一份。 */
function effectiveSteps(local: number[], remote?: number[]): number[] {
  if (local.length) return local
  return Array.isArray(remote) ? remote.filter((value) => Number(value) > 0) : []
}

function parseSteps(text: string): number[] {
  return text
    .split(/[,\s，、]+/)
    .map((piece) => Number(piece.trim()))
    .filter((value) => Number.isFinite(value) && value > 0)
}

/** 冷却进度：把「已过时间」折算成分段格子 + 连续进度条。 */
function buildCooldown(
  remaining: number,
  hits: number,
  steps: number[],
  remoteRemaining: number | undefined,
): CooldownView {
  let total = 0
  if (steps.length) {
    const index = Math.min(Math.max(hits - 1, 0), steps.length - 1)
    total = steps[index]
  }
  if (total <= 0) total = Math.max(remaining, Number(remoteRemaining) || 0, 1)
  const left = Math.min(Math.max(remaining, 0), total)

  let segments: CooldownSegment[] = []
  if (steps.length) {
    let cursor = 0
    segments = steps.map((seconds, index) => {
      const from = total - cursor - seconds
      cursor += seconds
      const fill = left <= 0 ? 0 : left >= from + seconds ? 1 : left <= from ? 0 : (left - from) / seconds
      return {
        key: `${seconds}-${index}`,
        from,
        fill,
        state: fill >= 1 ? 'done' : fill > 0 ? 'active' : 'todo',
      } as CooldownSegment
    })
  } else {
    // 没有阶梯（设置接口未就绪）时退化成一段，仍然显示真实的后端剩余时间
    segments = [{ key: 'single', from: 0, fill: total > 0 ? left / total : 0, state: left > 0 ? 'active' : 'todo' }]
  }

  return { total, remaining: left, ratio: total > 0 ? Math.min(1, left / total) : 0, segments }
}

/** 账号集合里可能同时存在「风控行」与「没有风控行」的账号，这里统一成一行。 */
interface AccountRow {
  accountId: string
  name: string
  state?: RiskState
  hits: number
  remaining: number
  blocked: boolean
  /** 后端算好的剩余秒数（本地倒计时到 0 后仍可作参照）。 */
  remoteRemaining?: number
}

export function RiskPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const risk = useAsync<RiskStatusAll>(() => api.get<RiskStatusAll>('/api/risk/status'), [], true, [
    'risk.changed',
    'risk.cleared',
  ])
  const events = useAsync<RiskEventListResult>(
    () => api.get<RiskEventListResult>('/api/risk/events', { limit: 100 }),
    [],
    true,
    ['risk.changed', 'risk.cleared'],
  )
  const settings = useAsync<RiskSettings>(() => api.get<RiskSettings>('/api/risk/settings'), [])

  const [nowMs, setNowMs] = useState(() => Date.now())
  const [clearing, setClearing] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [stepsText, setStepsText] = useState(DEFAULT_STEPS)
  const [autoSlider, setAutoSlider] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)

  // ---------------------------------------------------------------- 本地倒计时
  // 只在「有熔断账号」时跑；它**绝不发请求**，每秒只把 nowMs 推一格触发本地重绘。
  const activeUntil = useRef<Map<string, number>>(new Map())
  const timerRef = useRef<number | null>(null)

  const stopTimer = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  useEffect(
    () => () => {
      // 卸载必须清掉定时器，否则组件走了表还在跑
      stopTimer()
      activeUntil.current.clear()
    },
    [],
  )

  // 后端设置同步到表单（保存后 WS 静默重拉也会回到这里）
  useEffect(() => {
    const data = settings.data
    if (!data) return
    if (Array.isArray(data.cooldown_steps) && data.cooldown_steps.length) {
      setStepsText(data.cooldown_steps.join(','))
    }
    if (data.auto_slider !== undefined) setAutoSlider(Boolean(data.auto_slider))
  }, [settings.data])

  const settingsSynced = Boolean(settings.data)

  const riskItems = useMemo(() => risk.data?.states ?? [], [risk.data])
  const blockedIds = useMemo(() => risk.data?.blocked ?? [], [risk.data])
  const accountList = useMemo(() => accounts.data ?? [], [accounts.data])
  const eventList = events.data?.events ?? []

  const draftSteps = useMemo(() => parseSteps(stepsText), [stepsText])
  const steps = useMemo(() => effectiveSteps(draftSteps, risk.data?.cooldown_steps), [draftSteps, risk.data])

  const nicknameOf = (accountId?: string | null): string => {
    const account = accountList.find((a) => a.id === accountId)
    if (account) return account.nickname || account.id
    return accountId || '未知账号'
  }

  // 以「账号」为主轴，合并 /api/accounts 与 /api/risk/status，两边缺一个都不漏账号
  const rows = useMemo<AccountRow[]>(() => {
    const byId = new Map<string, AccountRow>()
    accountList.forEach((account) => {
      byId.set(account.id, {
        accountId: account.id,
        name: account.nickname || account.id,
        hits: Number(account.consecutive_hits) || 0,
        remaining: 0,
        blocked: bool(account.blocked),
        remoteRemaining: Number(account.remaining_seconds) || 0,
      })
    })
    riskItems.forEach((state, index) => {
      const accountId = String(state.account_id ?? `#${index}`)
      const current = byId.get(accountId)
      const remaining = remainingFrom(state.blocked_until, state.remaining_seconds)
      byId.set(accountId, {
        accountId,
        name: current?.name ?? nicknameOf(state.account_id),
        state,
        hits: Number(state.consecutive_hits) || 0,
        remaining,
        blocked: Boolean(state.blocked) || remaining > 0,
        remoteRemaining: Number(state.remaining_seconds) || 0,
      })
    })
    const list = Array.from(byId.values())
    // 熔断的排前面，其次按剩余时间长的排前面
    list.sort((a, b) => Number(b.blocked) - Number(a.blocked) || b.remaining - a.remaining)
    return list
  }, [accountList, riskItems])

  // 把仍在冷却的账号登记进本地倒计时；注册放在 effect 里，渲染过程不产生副作用
  const countdownKey = rows
    .filter((row) => row.blocked)
    .map((row) => `${row.accountId}:${Number(row.state?.blocked_until) || 0}:${row.remoteRemaining || 0}`)
    .join('|')

  useEffect(() => {
    activeUntil.current.clear()
    rows.forEach((row) => {
      if (!row.blocked) return
      // blocked_until 是 epoch **秒**（后端 time.time() 写下的），本地基准统一换成毫秒，
      // 否则下面拿它和 Date.now() 相减会算出约 -1.7e9 秒，倒计时恒显示 0 秒、定时器一秒就自停。
      const rawUntil = Number(row.state?.blocked_until) || 0
      const untilMs = rawUntil > 0 ? (rawUntil > 1e12 ? rawUntil : rawUntil * 1000) : 0
      // 本地基准已经过期（时钟偏移 / 陈旧数据）时退回「现在 + 后端给的剩余秒数」，
      // 与 remainingFrom() 的兜底口径一致，否则注册进来的基准一出生就是过期的。
      const base = untilMs > Date.now() ? untilMs : Date.now() + (row.remoteRemaining || 0) * 1000
      activeUntil.current.set(row.accountId, base)
    })
    if (activeUntil.current.size === 0) {
      stopTimer()
      return
    }
    if (timerRef.current === null) {
      timerRef.current = window.setInterval(() => {
        setNowMs(Date.now())
        // 全部归零就停表：没有倒计时可跑时不留空转的定时器
        const stillCounting = Array.from(activeUntil.current.values()).some((until) => until > Date.now())
        if (!stillCounting) stopTimer()
      }, 1000)
    }
    // rows 每秒都会因为 nowMs 变新，但 countdownKey 只在真正熔断集合变化时才变 —— 用 key 当依赖避免每秒重建定时器
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [countdownKey])

  /**
   * 渲染期把「本地基准时刻」换算成剩余秒数。
   * 必须读 `nowMs`：`rows` 的 useMemo 只依赖 [accountList, riskItems]，
   * 后端不重拉时它不会重算，若直接显示 `row.remaining` 倒计时就是死的。
   */
  const localRemaining = (row: AccountRow): number => {
    const until = activeUntil.current.get(row.accountId)
    if (!until) return row.remaining
    return Math.max(0, Math.round((until - nowMs) / 1000))
  }

  const blockedRows = rows.filter((row) => row.blocked)
  const blockedCount = Math.max(blockedIds.length, blockedRows.length)
  const cooldownRank = blockedRows.reduce((max, row) => Math.max(max, row.hits), 0)

  const stepErrors = useMemo(() => {
    const errors: string[] = []
    if (draftSteps.length === 0) {
      errors.push('至少填一个正整数（单位秒），用逗号分隔，例如 60,180,300。')
    }
    if (draftSteps.length > MAX_STEPS) {
      errors.push(`最多 ${MAX_STEPS} 级，当前填了 ${draftSteps.length} 级。`)
    }
    if (draftSteps.some((step) => step > MAX_STEP_SECONDS)) {
      errors.push(`单级最长 ${MAX_STEP_SECONDS} 秒（24 小时），超过的档位不会被后端采用。`)
    }
    if (draftSteps.some((step, index) => index > 0 && step <= draftSteps[index - 1])) {
      errors.push('阶梯应从短到长递增，否则冷却不会越来越久。')
    }
    return errors
  }, [draftSteps])

  const settingsDirty = useMemo(() => {
    const data = settings.data
    if (!data) return false
    const remoteSteps = Array.isArray(data.cooldown_steps) ? data.cooldown_steps : []
    const stepsChanged = remoteSteps.length !== draftSteps.length || remoteSteps.some((value, index) => value !== draftSteps[index])
    return stepsChanged || Boolean(data.auto_slider) !== autoSlider
  }, [settings.data, draftSteps, autoSlider])

  useEffect(() => {
    if (!settingsDirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [settingsDirty])

  const resetForm = () => {
    const data = settings.data
    setStepsText(data?.cooldown_steps?.length ? data.cooldown_steps.join(',') : DEFAULT_STEPS)
    setAutoSlider(Boolean(data?.auto_slider))
  }

  const saveSettings = async () => {
    if (stepErrors.length > 0) return
    setSavingSettings(true)
    try {
      await api.put('/api/risk/settings', { cooldown_steps: draftSteps, auto_slider: autoSlider })
      toast.success('风控设置已保存')
      settings.reload()
      risk.reload()
    } catch (err) {
      // 后端原文照实展示，前面只加一句「哪一步失败了」
      toast.error(`保存风控设置失败：${errorText(err)}`)
    } finally {
      setSavingSettings(false)
    }
  }

  /**
   * 解除熔断。**凭证只能由服务端写**：真的划完滑块、后端 captcha 任务确认拿到
   * `x5sec` 之后才会写下验证凭证；这里不再自证 `{verified:true}`（后端已不接受，
   * 那是让前端替后端盖章，等于没门槛）。没验证过就会被 409 拒掉，前端如实报出来。
   */
  const doClear = async () => {
    if (!clearing) return
    setBusy(true)
    try {
      await api.post<ClearResult>(`/api/risk/${encodeURIComponent(clearing)}/clear`, {})
      toast.success(`已解除「${nicknameOf(clearing)}」的熔断`)
      setClearing(null)
      risk.reload()
      events.reload()
      accounts.reload()
    } catch (err) {
      toast.error(`解除熔断失败：${errorText(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const eventTone = (kind?: string): string => {
    if (kind === 'verified') return 'ok'
    if (kind === 'clear_refused') return 'warn'
    return 'danger'
  }

  const eventLabel = (event: RiskEvent): string => {
    switch (event.kind) {
      case 'hit':
        return '命中风控'
      case 'verified':
        return '人工验证通过'
      case 'clear_refused':
        return '解除被拒'
      default:
        return event.kind || '风险'
    }
  }

  /** 倒计时数字与档位说明（「冷却剩余」列）。 */
  const renderRemaining = (view: CooldownView) => (
    <div className="rk-cd">
      <div className="rk-cd-head">
        <span className="rk-cd-num">{formatDuration(view.remaining)}</span>
        <span className="rk-cd-text">本档共 {formatDuration(view.total)}</span>
      </div>
    </div>
  )

  /** 连续进度条 + 分段格子（「冷却进度」列）。 */
  const renderProgress = (row: AccountRow, view: CooldownView) => (
    <div className="rk-cd">
      <div
        className="rk-bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={view.total}
        aria-valuenow={view.remaining}
        aria-label={`${row.name} 冷却剩余`}
      >
        <div className="rk-bar-fill" style={{ width: `${Math.round(view.ratio * 100)}%` }} />
      </div>
      <div className="rk-stepper">
        {view.segments.map((segment, index) => {
          const seconds = steps[index] ?? view.total
          return (
            <span
              key={segment.key}
              className={`rk-seg ${segment.state}`}
              title={`第 ${index + 1} 级 ${formatDuration(seconds)}（从 ${formatDuration(Math.max(segment.from, 0))} 起）`}
            >
              {segment.state === 'done' && <IconCheckCircle size={12} />}
              <span>{view.segments.length > 6 ? index + 1 : formatDuration(seconds)}</span>
              {segment.fill > 0 && segment.fill < 1 && (
                <i className="rk-seg-fill" style={{ width: `${Math.round(segment.fill * 100)}%` }} />
              )}
            </span>
          )
        })}
      </div>
    </div>
  )

  return (
    <div className="rk-page">
      <div className="rk-head">
        <div>
          <div className="rk-title">风控</div>
          <div className="rk-sub">
            熔断状态、冷却阶梯、风险事件。倒计时由后端返回的 <span className="mono">blocked_until</span> /
            <span className="mono"> remaining_seconds</span> 在本地推算，页面不做任何轮询。
          </div>
        </div>
        <div className="rk-head-actions">
          <button
            className="btn sm"
            onClick={() => {
              risk.reload()
              events.reload()
              accounts.reload()
              settings.reload()
            }}
          >
            <IconRefresh size={14} />
            刷新
          </button>
        </div>
      </div>

      {(risk.notReady || events.notReady || settings.notReady || accounts.notReady) && (
        <Notice
          kind="warn"
          onRetry={() => {
            risk.reload()
            events.reload()
            settings.reload()
            accounts.reload()
          }}
        >
          风控接口还没就绪，下方数据可能为空。设置项（冷却阶梯、自动滑块）仍可保存。
        </Notice>
      )}

      <div className="rk-stat-grid">
        <div className={`rk-stat${blockedCount > 0 ? ' danger' : ' ok'}`}>
          <div className="rk-stat-label">熔断中账号</div>
          <div className="rk-stat-value">{blockedCount}</div>
          <div className="rk-stat-extra">{blockedCount > 0 ? 'AI 已暂停自动回复' : '当前没有账号被熔断'}</div>
        </div>
        <div className="rk-stat">
          <div className="rk-stat-label">有风控记录</div>
          <div className="rk-stat-value">{riskItems.length}</div>
          <div className="rk-stat-extra">含已恢复正常的账号</div>
        </div>
        <div className={`rk-stat${eventList.length > 0 ? ' warn' : ''}`}>
          <div className="rk-stat-label">风险事件（最近 100 条）</div>
          <div className="rk-stat-value">{eventList.length}</div>
          <div className="rk-stat-extra">后端共 {events.data?.total ?? 0} 条</div>
        </div>
        <div className={`rk-stat${autoSlider ? ' warn' : ''}`}>
          <div className="rk-stat-label">自动滑块</div>
          <div className="rk-stat-value rk-stat-text">{autoSlider ? '已开启' : '已关闭'}</div>
          <div className="rk-stat-extra">触发验证时自动尝试完成滑块</div>
        </div>
        <div className="rk-stat">
          <div className="rk-stat-label">最高已用档位</div>
          <div className="rk-stat-value">{cooldownRank > 0 ? `${cooldownRank} 次` : '—'}</div>
          <div className="rk-stat-extra">熔断账号里最大的连续命中数</div>
        </div>
      </div>

      <div className="rk-cols">
        <div className="rk-col-main">
          <div className="rk-card">
            <div className="rk-card-head">
              <div className="rk-card-title">
                <IconShield size={16} />
                冷却与账号状态
              </div>
              <div className="rk-toolbar">
                <span className="rk-dim">共 {rows.length} 个账号</span>
              </div>
            </div>
            <AsyncBox loading={risk.loading} error={risk.error} notReady={risk.notReady} onRetry={risk.reload} empty={rows.length === 0} emptyTitle="还没有账号" emptyDesc="先在账号页扫码登录一个账号，这里才会出现风控状态。">
              {(
                <div className="rk-card-body">
                  <table className="rk-table">
                    <thead>
                      <tr>
                        <th className="rk-nowrap">账号</th>
                        <th className="rk-nowrap">状态</th>
                        <th className="rk-nowrap">冷却剩余</th>
                        <th>冷却进度</th>
                        <th className="rk-nowrap">连续命中</th>
                        <th className="rk-nowrap">最近命中</th>
                        <th className="rk-nowrap">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => {
                        const verification = row.state?.verification_type
                        const cd = buildCooldown(localRemaining(row), row.hits, steps, row.remoteRemaining)
                        return (
                          <tr key={row.accountId} className={row.blocked ? 'rk-row-blocked' : undefined}>
                            <td>
                              <div className="rk-acct">
                                <span className="rk-acct-name">{row.name}</span>
                                <span className="rk-acct-id mono">{row.accountId}</span>
                              </div>
                            </td>
                            <td className="rk-nowrap">
                              <span className={`rk-badge ${row.blocked ? 'danger' : 'ok'}`}>{row.blocked ? '熔断中' : '正常'}</span>
                              {verification && <span className="rk-badge warn">{verification}</span>}
                            </td>
                            <td className="rk-nowrap">
                              {row.blocked ? (
                                renderRemaining(cd)
                              ) : (
                                <span className="rk-cd-text">{row.state || row.hits ? '无冷却' : '没有风控记录'}</span>
                              )}
                            </td>
                            <td>{row.blocked ? renderProgress(row, cd) : <span className="rk-dim">—</span>}</td>
                            <td className="rk-nowrap mono">{row.hits}</td>
                            <td className="rk-nowrap rk-dim">{formatTime(row.state?.last_hit_at)}</td>
                            <td className="rk-nowrap">
                              <button className="btn sm" disabled={!row.accountId} onClick={() => setClearing(row.accountId)}>
                                解除熔断
                              </button>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </AsyncBox>
            <div className="rk-card-foot">
              <div className="rk-hint">
                <IconInfo size={14} />
                「解除熔断」只在确认该账号已通过人工验证后使用；后端不接受前端自证的 verified，未验证会被拒。
              </div>
            </div>
          </div>

          <div className="rk-card">
            <div className="rk-card-head">
              <div className="rk-card-title">
                <IconWarn size={16} />
                风控事件
              </div>
              <div className="rk-toolbar">
                <span className="rk-dim">最近 {eventList.length} 条</span>
              </div>
            </div>
            <AsyncBox
              loading={events.loading}
              error={events.error}
              notReady={events.notReady}
              onRetry={events.reload}
              empty={eventList.length === 0}
              emptyTitle="没有风控事件"
              emptyDesc="账号触发风控、被拒解除、验证通过时都会记在这里。"
            >
              {(
                <div className="rk-scroll">
                  <table className="rk-table">
                    <thead>
                      <tr>
                        <th className="rk-nowrap">时间</th>
                        <th className="rk-nowrap">账号</th>
                        <th className="rk-nowrap">类型</th>
                        <th className="rk-nowrap">状态</th>
                        <th>说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {eventList.map((event, index) => (
                        <tr key={event.id ?? index}>
                          <td className="rk-nowrap rk-dim">{formatTime(event.created_at)}</td>
                          <td>{nicknameOf(event.account_id)}</td>
                          <td className="rk-nowrap">
                            <span className={`rk-badge ${eventTone(event.kind)}`}>{eventLabel(event)}</span>
                          </td>
                          <td className="rk-nowrap rk-dim mono">{event.kind || '未知'}</td>
                          <td className="rk-msg">{event.message || '-'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </AsyncBox>
            <div className="rk-card-foot">
              <div className="rk-hint">
                <IconClock size={14} />
                列表通过 WebSocket 事件静默刷新，不需要手动轮询。
              </div>
            </div>
          </div>
        </div>

        <div className="rk-col-side">
          <div className="rk-card">
            <div className="rk-card-head">
              <div className="rk-card-title">
                <IconClock size={16} />
                冷却阶梯
              </div>
              {settingsDirty && <span className="rk-badge warn">未保存</span>}
            </div>
            <div className="rk-card-body">
              <div className="rk-hint rk-mb">每次命中风控后，账号会被暂停越来越久。逗号分隔，单位秒。</div>
              <Field label="阶梯（秒）" tip="例如 60,180,300,600,1200">
                <input
                  type="text"
                  value={stepsText}
                  onChange={(event) => setStepsText(event.target.value)}
                  className="mono rk-input"
                  placeholder="60,180,300,600,1200"
                />
              </Field>
              {stepErrors.length > 0 ? (
                stepErrors.map((message) => (
                  <div className="rk-err" key={message}>
                    {message}
                  </div>
                ))
              ) : (
                <div className="rk-chips">
                  {draftSteps.map((step, index) => (
                    <span className="rk-chip" key={`${step}-${index}`}>
                      第 {index + 1} 级 · {formatDuration(step)}
                    </span>
                  ))}
                </div>
              )}

              <div className="rk-split-line" />

              {/* 不用共享 Switch：它的 .check 样式归 styles.css（正在重写），本页自带一份 */}
              <label className="rk-switch">
                <input type="checkbox" checked={autoSlider} onChange={(event) => setAutoSlider(event.target.checked)} />
                自动滑块验证（触发验证时自动尝试完成）
              </label>
              <div className="rk-hint rk-mt">
                开启后可能提升账号被判定为机器的风险，建议先用小号试。
              </div>
            </div>
            <div className="rk-card-foot">
              <div className="rk-actions">
                <button className="btn primary" onClick={saveSettings} disabled={savingSettings || stepErrors.length > 0}>
                  {savingSettings ? '保存中…' : '保存设置'}
                </button>
                <button className="btn ghost" onClick={resetForm} disabled={savingSettings || !settingsSynced}>
                  还原
                </button>
                {settingsDirty && <span className="rk-dim">有未保存的改动</span>}
              </div>
            </div>
          </div>

          <div className="rk-card">
            <div className="rk-card-head">
              <div className="rk-card-title">
                <IconUsers size={16} />
                账号登录状态
              </div>
            </div>
            <div className="rk-card-body">
              <div className="rk-hint rk-mb">登录态异常是触发风控的常见前兆。</div>
              {accountList.length === 0 ? (
                <div className="rk-empty">
                  <span className="rk-empty-icon">
                    <IconUsers size={22} />
                  </span>
                  <div className="rk-empty-title">还没有账号</div>
                  <div className="rk-empty-desc">登录一个账号后这里会显示它的登录态与风控状态。</div>
                </div>
              ) : (
                <div className="rk-acct-list">
                  {accountList.map((account) => {
                    const login = loginStateLabel(account.login_state)
                    return (
                      <div className="rk-acct-row" key={account.id}>
                        <span className="rk-acct-name">{account.nickname || account.id}</span>
                        <span className="rk-acct-tags">
                          <span className={`rk-badge ${login.tone}`}>{login.text}</span>
                          {!bool(account.enabled) && <span className="rk-badge muted">已停用</span>}
                          {blockedIds.includes(account.id) && <span className="rk-badge danger">熔断中</span>}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
            <div className="rk-card-foot">
              <div className="rk-hint">
                <IconInfo size={14} />
                {risk.data?.refresh_interval_seconds
                  ? `后端风控刷新间隔 ${risk.data.refresh_interval_seconds} 秒；本页不轮询，靠 WS 事件刷新。`
                  : '本页不轮询，靠 WS 事件刷新。'}
              </div>
            </div>
          </div>
        </div>
      </div>

      {clearing && (
        <ConfirmDialog
          title="解除熔断"
          danger
          busy={busy}
          confirmText="确认解除"
          message={
            <>
              确定要解除 <strong>{nicknameOf(clearing)}</strong> 的熔断吗？
              <br />
              这表示你已确认该账号通过了人工验证。后端只认服务端写下的验证凭证，若还没验证过，请求会被拒绝并显示后端原文。
              <br />
              解除后 AI 会立即恢复自动回复，若账号仍被闲鱼限制，可能再次触发风控。
            </>
          }
          onCancel={() => setClearing(null)}
          onConfirm={doClear}
        />
      )}
    </div>
  )
}
