/** 账号：扫码登录（二维码 base64 图片）、账号列表、状态、手动验证。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatTime, loginStateLabel, runtimeStateLabel } from '../format'
import type { Account, QrPoll, QrStart, QrState } from '../types'

const QR_STATE_TEXT: Record<QrState, string> = {
  pending: '请用闲鱼 App 扫描二维码',
  scanned: '已扫码，请在手机上确认登录',
  confirmed: '登录成功！',
  expired: '二维码已过期，请重新获取',
  failed: '登录失败，请重新获取二维码',
}

export function AccountsPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [])
  const [qrOpen, setQrOpen] = useState(false)
  const [editing, setEditing] = useState<Account | null>(null)
  const [removing, setRemoving] = useState<Account | null>(null)
  const [busyId, setBusyId] = useState('')
  const [busy, setBusy] = useState(false)

  const list = accounts.data ?? []

  const patchAccount = async (account: Account, patch: Partial<Account>) => {
    setBusyId(account.id)
    try {
      await api.patch(`/api/accounts/${encodeURIComponent(account.id)}`, patch)
      toast.success('已更新')
      accounts.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  const relogin = async (account: Account) => {
    setBusyId(account.id)
    try {
      await api.post(`/api/accounts/${encodeURIComponent(account.id)}/relogin`)
      toast.success('已触发重新登录，请留意扫码窗口')
      accounts.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  const captcha = async (account: Account) => {
    setBusyId(account.id)
    try {
      await api.post(`/api/accounts/${encodeURIComponent(account.id)}/captcha`)
      toast.success('已打开人工验证窗口，请在弹出的浏览器里完成滑块')
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  const doRemove = async () => {
    if (!removing) return
    setBusy(true)
    try {
      await api.del(`/api/accounts/${encodeURIComponent(removing.id)}`)
      toast.success('账号已删除')
      setRemoving(null)
      accounts.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="toolbar">
        <button className="btn primary" onClick={() => setQrOpen(true)}>
          ＋ 扫码添加账号
        </button>
        <span className="spacer" />
        <button className="btn" onClick={accounts.reload}>
          刷新
        </button>
      </div>

      <div className="card">
        <h3>
          账号列表
          <span className="hint">共 {list.length} 个</span>
        </h3>
        <AsyncBox
          loading={accounts.loading}
          error={accounts.error}
          notReady={accounts.notReady}
          empty={list.length === 0}
          emptyIcon="👤"
          emptyTitle="还没有闲鱼账号"
          emptyDesc="点上方「扫码添加账号」，用闲鱼 App 扫码即可绑定。"
          onRetry={accounts.reload}
        >
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>账号</th>
                  <th>登录状态</th>
                  <th>运行状态</th>
                  <th className="nowrap">启用</th>
                  <th className="nowrap">自动回复</th>
                  <th>备注</th>
                  <th className="nowrap">操作</th>
                </tr>
              </thead>
              <tbody>
                {list.map((account) => {
                  const login = loginStateLabel(account.login_state)
                  const runtime = runtimeStateLabel(account.runtime_state)
                  const rowBusy = busyId === account.id
                  return (
                    <tr key={account.id}>
                      <td>
                        <div className="row">
                          <strong>{account.nickname || '(未命名)'}</strong>
                        </div>
                        <div className="mono dim">{account.id}</div>
                      </td>
                      <td>
                        <Badge tone={login.tone}>{login.text}</Badge>
                      </td>
                      <td>
                        <Badge tone={runtime.tone}>{runtime.text}</Badge>
                      </td>
                      <td>
                        <Switch
                          checked={bool(account.enabled)}
                          disabled={rowBusy}
                          onChange={(next) => patchAccount(account, { enabled: next ? 1 : 0 })}
                        />
                      </td>
                      <td>
                        <Switch
                          checked={bool(account.auto_reply)}
                          disabled={rowBusy}
                          onChange={(next) => patchAccount(account, { auto_reply: next ? 1 : 0 })}
                        />
                      </td>
                      <td className="dim">{account.remark || '-'}</td>
                      <td className="nowrap">
                        <div className="row" style={{ gap: 5 }}>
                          <button className="btn sm" disabled={rowBusy} onClick={() => relogin(account)}>
                            重新登录
                          </button>
                          <button className="btn sm" disabled={rowBusy} onClick={() => captcha(account)} title="打开浏览器窗口手动完成滑块验证">
                            手动验证
                          </button>
                          <button className="btn sm ghost" onClick={() => setEditing(account)}>
                            编辑
                          </button>
                          <button className="btn sm danger" onClick={() => setRemoving(account)}>
                            删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </AsyncBox>
      </div>

      {qrOpen && (
        <QrLoginModal
          onClose={() => setQrOpen(false)}
          onSuccess={() => {
            accounts.reload()
          }}
        />
      )}

      {editing && (
        <EditAccountModal
          account={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            accounts.reload()
          }}
        />
      )}

      {removing && (
        <ConfirmDialog
          title="删除账号"
          danger
          busy={busy}
          confirmText="确认删除"
          message={
            <>
              确定删除账号 <strong>{removing.nickname || removing.id}</strong> 吗？
              <br />
              该账号的登录凭据会一并删除，需要重新扫码才能恢复。
            </>
          }
          onCancel={() => setRemoving(null)}
          onConfirm={doRemove}
        />
      )}
    </div>
  )
}

/* ---------------- 扫码登录 ---------------- */

function QrLoginModal({ onClose, onSuccess }: { onClose: () => void; onSuccess: () => void }) {
  const [qr, setQr] = useState<QrStart | null>(null)
  const [state, setState] = useState<QrState>('pending')
  const [message, setMessage] = useState('')
  const [starting, setStarting] = useState(true)
  const [error, setError] = useState('')
  const pollRef = useRef<number | null>(null)
  const doneRef = useRef(false)

  const start = useCallback(async () => {
    setStarting(true)
    setError('')
    setQr(null)
    setState('pending')
    setMessage('')
    doneRef.current = false
    try {
      const result = await api.post<QrStart>('/api/accounts/qr/start')
      setQr(result)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setStarting(false)
    }
  }, [])

  useEffect(() => {
    void start()
  }, [start])

  // 轮询扫码状态
  useEffect(() => {
    if (!qr?.ticket || doneRef.current) return
    const ticket = qr.ticket

    const poll = async () => {
      try {
        const result = await api.get<QrPoll>(`/api/accounts/qr/${encodeURIComponent(ticket)}`)
        if (doneRef.current) return
        setState(result.state)
        if (result.message) setMessage(result.message)
        if (result.state === 'confirmed') {
          doneRef.current = true
          onSuccess()
          window.setTimeout(onClose, 900)
        } else if (result.state === 'expired' || result.state === 'failed') {
          doneRef.current = true
        }
      } catch (err) {
        if (doneRef.current) return
        doneRef.current = true
        setError(errorText(err))
      }
    }

    void poll()
    pollRef.current = window.setInterval(poll, 2000)
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [qr?.ticket, onClose, onSuccess])

  const tone = state === 'confirmed' ? 'ok' : state === 'expired' || state === 'failed' ? 'danger' : 'warn'

  return (
    <Modal
      title="扫码添加闲鱼账号"
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            关闭
          </button>
          <button className="btn primary" onClick={() => void start()} disabled={starting}>
            {starting ? '获取中…' : '重新获取二维码'}
          </button>
        </>
      }
    >
      {error && <Notice kind="error">{error}</Notice>}

      {starting && !qr && (
        <div className="loading-box">
          <span className="spinner" />
          <span>正在向闲鱼请求二维码…</span>
        </div>
      )}

      {qr && (
        <div className="qr-box">
          {qr.qr_image_b64 ? (
            <img
              src={qr.qr_image_b64.startsWith('data:') ? qr.qr_image_b64 : `data:image/png;base64,${qr.qr_image_b64}`}
              alt="闲鱼登录二维码"
            />
          ) : (
            <Notice kind="warn">后端没有返回二维码图片，请点「重新获取二维码」。</Notice>
          )}
          <Badge tone={tone}>{QR_STATE_TEXT[state] || state}</Badge>
          {message && <div className="dim" style={{ fontSize: 12 }}>{message}</div>}
          <div className="dim" style={{ fontSize: 12, textAlign: 'center', lineHeight: 1.8 }}>
            打开手机闲鱼 → 我的 → 设置 → 扫码登录
            <br />
            二维码有效期 {qr.expires_in ? `${qr.expires_in} 秒` : '约 2 分钟'}，过期请重新获取
          </div>
        </div>
      )}
    </Modal>
  )
}

/* ---------------- 编辑账号 ---------------- */

function EditAccountModal({
  account,
  onClose,
  onSaved,
}: {
  account: Account
  onClose: () => void
  onSaved: () => void
}) {
  const toast = useToast()
  const [remark, setRemark] = useState(account.remark || '')
  const [enabled, setEnabled] = useState(bool(account.enabled))
  const [autoReply, setAutoReply] = useState(bool(account.auto_reply))
  const [saving, setSaving] = useState(false)

  const save = async () => {
    setSaving(true)
    try {
      await api.patch(`/api/accounts/${encodeURIComponent(account.id)}`, {
        remark,
        enabled: enabled ? 1 : 0,
        auto_reply: autoReply ? 1 : 0,
      })
      toast.success('已保存')
      onSaved()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`编辑账号 · ${account.nickname || account.id}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button className="btn primary" onClick={save} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <div className="kv" style={{ marginBottom: 14 }}>
        <span className="k">账号 ID</span>
        <span className="v mono">{account.id}</span>
        <span className="k">最近更新</span>
        <span className="v">{formatTime(account.updated_at)}</span>
      </div>
      <div className="field">
        <label>备注</label>
        <input type="text" value={remark} onChange={(e) => setRemark(e.target.value)} placeholder="给这个账号起个好记的名字" />
      </div>
      <div className="row" style={{ gap: 20, marginTop: 6 }}>
        <Switch checked={enabled} onChange={setEnabled} label="启用该账号" />
        <Switch checked={autoReply} onChange={setAutoReply} label="允许 AI 自动回复" />
      </div>
    </Modal>
  )
}
