/** 账号：扫码登录（二维码 base64 图片）、账号列表、状态、手动验证。 */

import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import { api, errorText, isNotReady } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatTime, loginStateLabel, runtimeStateLabel } from '../format'
import type { Account, CaptchaResult, QrPoll, QrStart, QrState } from '../types'

const QR_STATE_TEXT: Record<QrState, string> = {
  pending: '请用闲鱼 App 扫描二维码',
  scanned: '已扫码，请在手机上确认登录',
  confirmed: '登录成功！',
  expired: '二维码已过期，请重新获取',
  failed: '登录失败，请重新获取二维码',
}

/**
 * 账号头像。
 *
 * `avatar_url` 大概率拿不到（闲鱼资料接口未就绪时后端宁缺勿错，会下发空串），
 * 所以必须有个**不塌陷**的兜底：昵称/账号 ID 首字母。加载失败也走同一兜底，
 * 不能让浏览器破图图标占着位置。
 */
function AccountAvatar({ account, size = 36 }: { account: Account; size?: number }) {
  const [failed, setFailed] = useState(false)
  const nickname = (account.nickname || '').trim()
  const letter = (nickname[0] || account.id?.[0] || '?').toUpperCase()
  const box: CSSProperties = {
    width: size,
    height: size,
    flex: 'none',
    borderRadius: '50%',
    overflow: 'hidden',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: Math.round(size * 0.42),
    fontWeight: 600,
    color: '#fff',
    background: 'linear-gradient(135deg, #3b82f6, #8b5cf6)',
    border: '1px solid rgba(255,255,255,.14)',
  }
  if (account.avatar_url && !failed) {
    return (
      <span style={box} title={nickname || account.id}>
        <img
          src={account.avatar_url}
          alt={nickname || account.id}
          loading="lazy"
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
          onError={() => setFailed(true)}
        />
      </span>
    )
  }
  return (
    <span style={box} title={nickname || account.id}>
      {letter}
    </span>
  )
}

/** 二维码有效期到期后自动重新拉一张（后端默认 300 秒）。 */
export function AccountsPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [qrOpen, setQrOpen] = useState(false)
  /** 点「重新登录」时带上账号，弹窗直接显示该账号的新二维码。 */
  const [reloginTarget, setReloginTarget] = useState<Account | null>(null)
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

  /**
   * 手动过验证。后端路由是 `POST /api/accounts/{id}/captcha`
   * （**不是** `/verify`），异步触发、立刻返回，真实结果写进操作日志。
   */
  const captcha = async (account: Account) => {
    setBusyId(account.id)
    try {
      const result = await api.post<CaptchaResult>(
        `/api/accounts/${encodeURIComponent(account.id)}/captcha`,
        { headless: false },
      )
      toast.success(result?.message || '已打开人工验证窗口，请在弹出的浏览器里完成滑块')
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  /**
   * 从平台重新拉这个账号的昵称/头像。
   *
   * 契约（backed by `backend/app/api/accounts.py:282`）：
   * `POST /api/accounts/{id}/refresh-profile` → `{account_id, nickname, avatar_url, refreshed}`
   * （拉失败时**仍然 200**，多给一个 `message`）。
   *
   * 所以判定成功与否要看 `refreshed`，不能只看 `nickname` 有没有值 ——
   * 平台就是没给昵称时 `refreshed` 也可能为 false，那时要说清楚是「没拉到」，
   * 而不是骗用户「已刷新」。
   */
  const refreshProfile = async (account: Account) => {
    setBusyId(account.id)
    try {
      const result = await api.post<{
        account_id?: string
        nickname?: string
        avatar_url?: string
        refreshed?: boolean
        message?: string
      }>(`/api/accounts/${encodeURIComponent(account.id)}/refresh-profile`)
      if (result?.refreshed && result.nickname) {
        toast.success(`已刷新资料：${result.nickname}`)
      } else if (result?.refreshed) {
        // 拿到了平台资料，但里面没有昵称（后端宁缺勿错下发空串）。
        toast.info(result.message || '已刷新，但平台这次没给昵称')
      } else {
        toast.error(result?.message || '没能从闲鱼拉到账号资料，可能是 Cookie 已失效，试试重新扫码登录')
      }
      accounts.reload()
    } catch (err) {
      if (isNotReady(err)) toast.error('「刷新资料」接口还没就绪，请稍后再试')
      else toast.error(errorText(err))
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
                        <div className="row" style={{ gap: 10, flexWrap: 'nowrap' }}>
                          <AccountAvatar account={account} />
                          <div style={{ minWidth: 0 }}>
                            <strong>{account.nickname || '(未命名)'}</strong>
                            <div className="mono dim">{account.id}</div>
                          </div>
                        </div>
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
                          onChange={(next) => patchAccount(account, { enabled: next })}
                        />
                      </td>
                      <td>
                        <Switch
                          checked={bool(account.auto_reply)}
                          disabled={rowBusy}
                          onChange={(next) => patchAccount(account, { auto_reply: next })}
                        />
                      </td>
                      <td className="dim">{account.remark || '-'}</td>
                      <td className="nowrap">
                        <div className="row" style={{ gap: 5 }}>
                          <button className="btn sm" disabled={rowBusy} onClick={() => setReloginTarget(account)}>
                            重新登录
                          </button>
                          <button
                            className="btn sm"
                            disabled={rowBusy}
                            onClick={() => refreshProfile(account)}
                            title="从闲鱼重新拉取昵称与头像"
                          >
                            刷新资料
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

      {reloginTarget && (
        <QrLoginModal
          reloginAccount={reloginTarget}
          onClose={() => setReloginTarget(null)}
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

/**
 * 扫码登录弹窗。
 *
 * 两种入口共用这一个组件：
 *  - 新增账号：`POST /api/accounts/qr/start` 拿一张新二维码；
 *  - 重新登录：`POST /api/accounts/{id}/relogin` —— 后端**直接把新二维码内联返回**
 *    （`{id, login_state, ticket, qr_image_b64, expires_in}`），所以不用再调一次 qr/start。
 *    两者拿到的 ticket 都用同一个 `GET /api/accounts/qr/{ticket}` 轮询。
 */
function QrLoginModal({
  reloginAccount,
  onClose,
  onSuccess,
}: {
  reloginAccount?: Account
  onClose: () => void
  onSuccess: () => void
}) {
  const [qr, setQr] = useState<QrStart | null>(null)
  const [state, setState] = useState<QrState>('pending')
  const [message, setMessage] = useState('')
  const [starting, setStarting] = useState(true)
  const [error, setError] = useState('')
  const pollRef = useRef<number | null>(null)
  const doneRef = useRef(false)
  // 连续失败计数。轮询要跑满二维码有效期（默认每 2 秒一次 ≈ 150 次），中间
  // 偶发一次网络抖动/后端瞬时 5xx 就弹错并停轮询的话，用户明明扫上了也白扫 ——
  // 一次抖动不该让整次登录失败，连续失败才判死。
  const failRef = useRef(0)
  const MAX_POLL_FAILURES = 5

  const start = useCallback(async () => {
    setStarting(true)
    setError('')
    setQr(null)
    setState('pending')
    setMessage('')
    doneRef.current = false
    failRef.current = 0
    try {
      if (reloginAccount) {
        // 后端在这里已经清掉旧 Cookie 并置 need_relogin，返回的新二维码可直接用
        const result = await api.post<QrStart>(
          `/api/accounts/${encodeURIComponent(reloginAccount.id)}/relogin`,
        )
        setQr(result)
      } else {
        const result = await api.post<QrStart>('/api/accounts/qr/start')
        setQr(result)
      }
    } catch (err) {
      setError(errorText(err))
    } finally {
      setStarting(false)
    }
  }, [reloginAccount])

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
        failRef.current = 0
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
        failRef.current += 1
        if (failRef.current < MAX_POLL_FAILURES) {
          // 还没到判死线：继续下一轮，只是提示一下，不清空二维码也别停轮询。
          setError(`状态查询暂时失败，正在重试（第 ${failRef.current} 次）：${errorText(err)}`)
          return
        }
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
      title={reloginAccount ? `重新登录 · ${reloginAccount.nickname || reloginAccount.id}` : '扫码添加闲鱼账号'}
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
        enabled,
        auto_reply: autoReply,
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
