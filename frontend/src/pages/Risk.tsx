/** 风控：熔断状态、冷却阶梯、风险事件、自动滑块开关。
 *
 * 后端真实形状（services/risk_service.py）：
 *  - `GET /api/risk/status`（不带 account_id）→
 *      `{states: [...], blocked: [账号id...], cooldown_steps: [...], auto_slider, refresh_interval_seconds}`
 *    注意 `blocked` 是**账号 id 数组**（带 account_id 查询时它才是 bool）。
 *  - `GET /api/risk/events` → `{events: [...], total}`，事件字段只有 `kind` / `message`。
 *  - 设置在 `GET|PUT /api/risk/settings`（**不是** `/api/settings`）。
 *  - 清除冷却 `POST /api/risk/{account_id}/clear` 必须带 `verified: true`，
 *    否则后端判「未检测到人工验证成功」返回 409。
 */

import { useEffect, useMemo, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Notice, Stat, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatDuration, formatTime, loginStateLabel } from '../format'
import type { Account, ClearResult, RiskEventListResult, RiskSettings, RiskStatusAll } from '../types'

const DEFAULT_STEPS = '60,180,300,600,1200'

/** 把 blocked_until（epoch 秒）换算成剩余秒数。 */
function remainingSeconds(value?: number | null): number {
  const raw = Number(value)
  if (!raw) return 0
  // 后端给的是秒；容忍毫秒时间戳
  const ms = raw > 1e12 ? raw : raw * 1000
  return Math.max(0, Math.round((ms - Date.now()) / 1000))
}

function parseSteps(text: string): number[] {
  return text
    .split(/[,\s]+/)
    .map((piece) => Number(piece.trim()))
    .filter((value) => Number.isFinite(value) && value > 0)
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

  const [, setNow] = useState(Date.now())
  const [clearing, setClearing] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [stepsText, setStepsText] = useState(DEFAULT_STEPS)
  const [autoSlider, setAutoSlider] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)

  // 每秒刷新倒计时
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  // 后端设置同步到表单
  useEffect(() => {
    const data = settings.data
    if (!data) return
    if (Array.isArray(data.cooldown_steps) && data.cooldown_steps.length) {
      setStepsText(data.cooldown_steps.join(','))
    }
    if (data.auto_slider !== undefined) setAutoSlider(Boolean(data.auto_slider))
  }, [settings.data])

  const riskItems = risk.data?.states ?? []
  const blockedIds = risk.data?.blocked ?? []
  const steps = parseSteps(stepsText)

  const accountList = accounts.data ?? []
  const nicknameOf = (accountId?: string | null) => {
    const account = accountList.find((a) => a.id === accountId)
    return account ? account.nickname || account.id : accountId || '未知账号'
  }

  const eventList = events.data?.events ?? []

  const saveSettings = async () => {
    if (steps.length === 0) return
    setSavingSettings(true)
    try {
      await api.put('/api/risk/settings', {
        cooldown_steps: steps,
        auto_slider: autoSlider,
      })
      toast.success('风控设置已保存')
      settings.reload()
      risk.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSavingSettings(false)
    }
  }

  /**
   * 解除熔断。**凭证只能由服务端写**：真的划完滑块、后端 captcha 任务确认拿到
   * `x5sec` 之后才会写下验证凭证；这里不再自证 `{verified:true}`（那是让前端
   * 替后端盖章，等于没门槛）。没验证过就会被 409 拒掉，前端如实报出来。
   */
  const doClear = async () => {
    if (!clearing) return
    setBusy(true)
    try {
      await api.post<ClearResult>(`/api/risk/${encodeURIComponent(clearing)}/clear`, {})
      toast.success('已解除该账号的熔断')
      setClearing(null)
      risk.reload()
      events.reload()
      accounts.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const blockedCount = useMemo(() => blockedIds.length, [blockedIds])

  return (
    <div>
      {(risk.notReady || events.notReady || settings.notReady) && (
        <Notice
          kind="warn"
          onRetry={() => {
            risk.reload()
            events.reload()
            settings.reload()
          }}
        >
          风控接口还没就绪，下方数据可能为空。设置项（冷却阶梯、自动滑块）仍可保存。
        </Notice>
      )}

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label="熔断中账号" value={blockedCount} tone={blockedCount > 0 ? 'var(--danger)' : 'var(--ok)'} small />
        <Stat label="有风控记录" value={riskItems.length} small />
        <Stat label="风险事件" value={eventList.length} small />
        <Stat
          label="自动滑块"
          value={autoSlider ? '已开启' : '已关闭'}
          tone={autoSlider ? 'var(--warn)' : undefined}
          small
          extra={<div className="dim" style={{ fontSize: 12, marginTop: 2 }}>触发验证时自动尝试完成滑块</div>}
        />
      </div>

      <div className="split">
        <div className="left">
          <div className="card">
            <h3>
              账号熔断状态
              <span style={{ flex: 1 }} />
              <button className="btn sm" onClick={risk.reload}>
                刷新
              </button>
            </h3>
            <AsyncBox
              loading={risk.loading}
              error={risk.error}
              notReady={risk.notReady}
              empty={riskItems.length === 0}
              emptyIcon="🛡"
              emptyTitle="没有风控记录"
              emptyDesc="账号运行正常，没有触发任何风控。"
              onRetry={risk.reload}
            >
              <div className="col">
                {riskItems.map((item, index) => {
                  const left = remainingSeconds(item.blocked_until)
                  const isBlocked = Boolean(item.blocked) || left > 0
                  const hits = Number(item.consecutive_hits) || 0
                  const stepIndex = Math.min(Math.max(hits - 1, 0), Math.max(steps.length - 1, 0))
                  return (
                    <div
                      key={String(item.account_id ?? index)}
                      style={{
                        border: `1px solid ${isBlocked ? 'var(--danger)' : 'var(--border-soft)'}`,
                        borderRadius: 'var(--radius-sm)',
                        padding: 12,
                      }}
                    >
                      <div className="row wrap">
                        <strong>{nicknameOf(item.account_id)}</strong>
                        <Badge tone={isBlocked ? 'danger' : 'ok'}>{isBlocked ? '熔断中' : '正常'}</Badge>
                        {item.verification_type && <Badge tone="warn">{item.verification_type}</Badge>}
                        <span className="spacer" />
                        {isBlocked && (
                          <span style={{ color: 'var(--danger)', fontSize: 12.5 }}>
                            剩余 {formatDuration(left)}
                          </span>
                        )}
                      </div>
                      <div className="row wrap" style={{ marginTop: 8, gap: 14, fontSize: 12.5 }}>
                        <span className="dim">
                          连续命中 <strong style={{ color: hits > 0 ? 'var(--warn)' : undefined }}>{hits}</strong> 次
                        </span>
                        {steps.length > 0 && (
                          <span className="dim">
                            当前档位 第 {stepIndex + 1} 级 / {steps.length} 级（{formatDuration(steps[stepIndex])}）
                          </span>
                        )}
                        <span className="dim">最近命中 {formatTime(item.last_hit_at)}</span>
                      </div>
                      <div className="row" style={{ marginTop: 9 }}>
                        <button className="btn sm" disabled={!item.account_id} onClick={() => setClearing(item.account_id || null)}>
                          解除熔断
                        </button>
                        <span className="dim" style={{ fontSize: 11.5, marginLeft: 8 }}>
                          仅在你确认账号已恢复正常后使用
                        </span>
                      </div>
                    </div>
                  )
                })}
              </div>
            </AsyncBox>
          </div>

          <div className="card">
            <h3>
              风险事件
              <span className="hint">最近的触发记录</span>
              <span style={{ flex: 1 }} />
              <button className="btn sm" onClick={events.reload}>
                刷新
              </button>
            </h3>
            <AsyncBox
              loading={events.loading}
              error={events.error}
              notReady={events.notReady}
              empty={eventList.length === 0}
              emptyIcon="📋"
              emptyTitle="暂无风险事件"
              onRetry={events.reload}
            >
              <div className="table-wrap scroll-60">
                <table>
                  <thead>
                    <tr>
                      <th className="nowrap">时间</th>
                      <th>账号</th>
                      <th className="nowrap">类型</th>
                      <th>说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {eventList.map((event, index) => (
                      <tr key={event.id ?? index}>
                        <td className="nowrap dim" style={{ fontSize: 12 }}>{formatTime(event.created_at)}</td>
                        <td>{nicknameOf(event.account_id)}</td>
                        <td className="nowrap">
                          <Badge tone="danger">{event.kind || '风险'}</Badge>
                        </td>
                        <td className="dim" style={{ fontSize: 12 }}>{event.message || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </AsyncBox>
          </div>
        </div>

        <div className="right">
          <div className="card">
            <h3>冷却阶梯</h3>
            <div className="dim" style={{ fontSize: 12, marginBottom: 10 }}>
              每次命中风控后，账号会被暂停越来越久。逗号分隔，单位秒。
            </div>
            <Field label="阶梯（秒）" tip="例如 60,180,300,600,1200">
              <input type="text" value={stepsText} onChange={(e) => setStepsText(e.target.value)} className="mono" />
            </Field>
            {steps.length > 0 ? (
              <div className="row wrap" style={{ gap: 6, margin: '4px 0 12px' }}>
                {steps.map((step, index) => (
                  <span key={`${step}-${index}`} className="step-chip">
                    第 {index + 1} 级 · {formatDuration(step)}
                  </span>
                ))}
              </div>
            ) : (
              <Notice kind="warn">阶梯格式无法识别，请用逗号分隔的正整数。</Notice>
            )}

            <Switch
              checked={autoSlider}
              onChange={setAutoSlider}
              label="自动滑块验证（触发验证时自动尝试完成）"
            />
            <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>
              开启后可能提升账号被判定为机器的风险，建议先用小号试。
            </div>

            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn primary" onClick={saveSettings} disabled={savingSettings || steps.length === 0}>
                {savingSettings ? '保存中…' : '保存设置'}
              </button>
              <button
                className="btn ghost"
                onClick={() => {
                  const data = settings.data
                  setStepsText(data?.cooldown_steps?.length ? data.cooldown_steps.join(',') : DEFAULT_STEPS)
                  setAutoSlider(Boolean(data?.auto_slider))
                }}
                disabled={savingSettings}
              >
                还原
              </button>
            </div>
          </div>

          <div className="card">
            <h3>账号登录状态</h3>
            <div className="dim" style={{ fontSize: 12, marginBottom: 8 }}>
              登录态异常是触发风控的常见前兆。
            </div>
            <AsyncBox
              loading={accounts.loading}
              error={accounts.error}
              notReady={accounts.notReady}
              empty={accountList.length === 0}
              emptyIcon="👤"
              emptyTitle="还没有账号"
              onRetry={accounts.reload}
            >
              <div className="col">
                {accountList.map((account) => {
                  const login = loginStateLabel(account.login_state)
                  return (
                    <div key={account.id} className="row wrap" style={{ justifyContent: 'space-between' }}>
                      <span>{account.nickname || account.id}</span>
                      <div className="row" style={{ gap: 6 }}>
                        <Badge tone={login.tone}>{login.text}</Badge>
                        {!bool(account.enabled) && <Badge tone="muted">已停用</Badge>}
                        {blockedIds.includes(account.id) && <Badge tone="danger">熔断中</Badge>}
                      </div>
                    </div>
                  )
                })}
              </div>
            </AsyncBox>
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
              这表示你已确认该账号通过了人工验证。解除后 AI 会立即恢复自动回复，若账号仍被闲鱼限制，可能再次触发风控。
            </>
          }
          onCancel={() => setClearing(null)}
          onConfirm={doClear}
        />
      )}
    </div>
  )
}
