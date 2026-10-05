/** 风控：熔断状态、冷却阶梯、风险事件、自动滑块开关。 */

import { useEffect, useMemo, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Notice, Stat, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatTime, loginStateLabel } from '../format'
import type { Account, RiskEvent, RiskStatus } from '../types'

const DEFAULT_STEPS = '60,180,300,600,1200'

/** 把 blocked_until（秒或毫秒时间戳）换算成剩余秒数。 */
function remainingSeconds(value?: number | string | null): number {
  const raw = Number(value)
  if (!raw) return 0
  const ms = raw > 1e12 ? raw : raw * 1000
  return Math.max(0, Math.round((ms - Date.now()) / 1000))
}

function formatDuration(seconds: number): string {
  if (seconds <= 0) return '已结束'
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

function parseSteps(text: string): number[] {
  return text
    .split(/[,\s]+/)
    .map((piece) => Number(piece.trim()))
    .filter((value) => Number.isFinite(value) && value > 0)
}

export function RiskPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [])
  const risk = useAsync<RiskStatus | RiskStatus[]>(() => api.get<RiskStatus | RiskStatus[]>('/api/risk/status'), [])
  const events = useAsync<RiskEvent[]>(() => api.get<RiskEvent[]>('/api/risk/events', { limit: 100 }), [])
  const settings = useAsync<Record<string, string>>(() => api.get<Record<string, string>>('/api/settings'), [])

  const [, setNow] = useState(Date.now())
  const [clearing, setClearing] = useState<RiskStatus | null>(null)
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
    if (data['risk.cooldown_steps']) setStepsText(String(data['risk.cooldown_steps']))
    if (data['risk.auto_slider'] !== undefined) setAutoSlider(String(data['risk.auto_slider']) === 'true')
  }, [settings.data])

  const riskItems = useMemo<RiskStatus[]>(() => {
    const data = risk.data
    if (!data) return []
    if (Array.isArray(data)) return data
    return [data]
  }, [risk.data])

  const accountList = accounts.data ?? []
  const nicknameOf = (accountId?: string | null) => {
    const account = accountList.find((a) => a.id === accountId)
    return account ? account.nickname || account.id : accountId || '未知账号'
  }

  const blocked = riskItems.filter((item) => remainingSeconds(item.blocked_until) > 0)
  const eventList = events.data ?? []
  const steps = parseSteps(stepsText)

  const saveSettings = async () => {
    setSavingSettings(true)
    try {
      await api.put('/api/settings', {
        'risk.cooldown_steps': stepsText.trim() || DEFAULT_STEPS,
        'risk.auto_slider': autoSlider ? 'true' : 'false',
      })
      toast.success('风控设置已保存')
      settings.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSavingSettings(false)
    }
  }

  const doClear = async () => {
    if (!clearing) return
    setBusy(true)
    try {
      await api.post(`/api/risk/${encodeURIComponent(clearing.account_id || '')}/clear`)
      toast.success('已解除该账号的熔断')
      setClearing(null)
      risk.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      {(risk.notReady || events.notReady) && (
        <Notice kind="warn" onRetry={() => { risk.reload(); events.reload() }}>
          风控接口还没就绪，下方数据可能为空。设置项（冷却阶梯、自动滑块）仍可保存。
        </Notice>
      )}

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label="熔断中账号" value={blocked.length} tone={blocked.length > 0 ? 'var(--danger)' : 'var(--ok)'} small />
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
                  const isBlocked = left > 0
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
                        <button className="btn sm" disabled={!item.account_id} onClick={() => setClearing(item)}>
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
                          <Badge tone="danger">{event.event_type || event.type || '风险'}</Badge>
                        </td>
                        <td className="dim" style={{ fontSize: 12 }}>{event.detail || event.message || '-'}</td>
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
                  setStepsText(settings.data?.['risk.cooldown_steps'] ? String(settings.data['risk.cooldown_steps']) : DEFAULT_STEPS)
                  setAutoSlider(String(settings.data?.['risk.auto_slider'] ?? 'false') === 'true')
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
              确定要解除 <strong>{nicknameOf(clearing.account_id)}</strong> 的熔断吗？
              <br />
              解除后 AI 会立即恢复对该账号的自动回复，若账号仍被闲鱼限制，可能再次触发风控。
            </>
          }
          onCancel={() => setClearing(null)}
          onConfirm={doClear}
        />
      )}
    </div>
  )
}
