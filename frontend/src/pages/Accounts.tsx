/**
 * 账号：扫码登录（二维码 base64 图片）、账号卡片、状态、手动验证。
 *
 * 字段来源与后端契约（重做 UI 时不动）：
 *  - `GET /api/accounts` → `Account[]`（`login_state` / `runtime_state` / `blocked` / `remaining_seconds` …）；
 *  - 「重新登录」`POST /api/accounts/{id}/relogin`，**新二维码内联返回**；
 *  - 「扫码添加」`POST /api/accounts/qr/start`；两者都用 `GET /api/accounts/qr/{ticket}` 轮询；
 *  - 「刷新资料」`POST /api/accounts/{id}/refresh-profile`，**成功与否看 `refreshed`**，不是看昵称有没有值；
 *  - 「手动验证」`POST /api/accounts/{id}/captcha`（异步触发，立刻返回）。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { api, errorText, isNotReady } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, Stat, Switch, useToast } from '../components'
import {
  IconCheck,
  IconCheckCircle,
  IconClock,
  IconClose,
  IconEdit,
  IconKey,
  IconQr,
  IconRefresh,
  IconSearch,
  IconShield,
  IconTrash,
  IconWarn,
  IconXCircle,
} from '../icons'
import { useAsync } from '../hooks/useAsync'
import { bool, formatDuration, formatTime, loginStateLabel, runtimeStateLabel } from '../format'
import type { Account, CaptchaResult, QrPoll, QrStart, QrState } from '../types'
import './accounts.css'

const QR_STATE_TEXT: Record<QrState, string> = {
  pending: '请用闲鱼 App 扫描二维码',
  scanned: '已扫码，请在手机上确认登录',
  confirmed: '登录成功！',
  expired: '二维码已过期，请重新获取',
  failed: '登录失败，请重新获取二维码',
}

/** 扫码三步：等待扫码 → 手机确认 → 登录完成。 */
const QR_STEPS = ['等待扫码', '手机确认', '登录完成'] as const

function qrStepIndex(state: QrState): number {
  if (state === 'pending') return 0
  if (state === 'scanned') return 1
  if (state === 'confirmed') return 2
  return -1 // expired / failed：整条流程作废，不标进度
}

/**
 * 账号头像。
 *
 * `avatar_url` 大概率拿不到（闲鱼资料接口未就绪时后端宁缺勿错，会下发空串），
 * 所以必须有个**不塌陷**的兜底：昵称/备注/账号 ID 首字母。加载失败也走同一兜底，
 * 不能让浏览器破图图标占着位置。
 */
function AccountAvatar({ account, size = 44 }: { account: Account; size?: number }) {
  // 记「失败的那个 URL」而不是布尔值：刷新资料拿到新 URL 后要重新试着加载，
  // 否则一次失败就把这个账号永久钉在首字母兜底上，必须整页刷新才恢复。
  const [failedUrl, setFailedUrl] = useState('')
  const nickname = (account.nickname || '').trim()
  const letter = (nickname[0] || (account.remark || '')[0] || account.id?.[0] || '?').toUpperCase()
  const style: CSSProperties = { width: size, height: size, fontSize: Math.round(size * 0.4) }
  const title = nickname || account.remark || account.id
  const url = account.avatar_url || ''
  if (url && url !== failedUrl) {
    return (
      <span className="ac-avatar" style={style} title={title}>
        <img src={url} alt={title} loading="lazy" onError={() => setFailedUrl(url)} />
      </span>
    )
  }
  return (
    <span className="ac-avatar" style={style} title={title} aria-hidden>
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
  const [keyword, setKeyword] = useState('')

  const list = accounts.data ?? []

  const filtered = useMemo(() => {
    const q = keyword.trim().toLowerCase()
    if (!q) return list
    return list.filter((account) =>
      [account.nickname, account.remark, account.id, account.xianyu_user_id]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q)),
    )
  }, [list, keyword])

  const metrics = useMemo(() => {
    const online = list.filter((a) => loginStateLabel(a.login_state).tone === 'ok').length
    const needRelogin = list.filter((a) => loginStateLabel(a.login_state).tone === 'danger').length
    const running = list.filter((a) => a.runtime_state === 'running').length
    const blocked = list.filter((a) => bool(a.blocked)).length
    return { online, needRelogin, running, blocked, total: list.length }
  }, [list])

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
    <div className="ac-page">
      <div className="ac-head">
        <div className="ac-head-text">
          <h2>账号</h2>
          <p>
            扫码登录闲鱼账号；每个账号可以独立开关「启用」与「AI 自动回复」，登录态和运行态都在这里看。
          </p>
        </div>
        <div className="ac-head-actions">
          <button className="btn primary" onClick={() => setQrOpen(true)}>
            <IconQr size={16} /> 扫码添加账号
          </button>
          <button className="btn" onClick={accounts.reload} disabled={accounts.loading}>
            <IconRefresh size={15} /> 刷新
          </button>
        </div>
      </div>

      <div className="grid grid-4 ac-metrics">
        <Stat
          label="账号总数"
          value={accounts.loading ? '…' : metrics.total}
          extra={<span className="ac-metric-hint">已绑定到本机</span>}
        />
        <Stat
          label="登录正常"
          value={accounts.loading ? '…' : metrics.online}
          tone={metrics.online > 0 ? 'var(--ok)' : undefined}
          extra={
            <span className="ac-metric-hint">
              {metrics.needRelogin > 0 ? `需重新登录 ${metrics.needRelogin} 个` : '全部登录态正常'}
            </span>
          }
        />
        <Stat
          label="运行中"
          value={accounts.loading ? '…' : metrics.running}
          extra={<span className="ac-metric-hint">IM 已连上、正在收消息</span>}
        />
        <Stat
          label="风控熔断"
          value={accounts.loading ? '…' : metrics.blocked}
          tone={metrics.blocked > 0 ? 'var(--danger)' : 'var(--ok)'}
          extra={<span className="ac-metric-hint">{metrics.blocked > 0 ? '建议先去风控页看原因' : '没有账号被熔断'}</span>}
        />
      </div>

      <div className="ac-toolbar">
        <span className="ac-search">
          <IconSearch size={15} />
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="按昵称 / 备注 / 账号 ID 筛选"
            aria-label="筛选账号"
          />
          {keyword && (
            <button type="button" className="ac-search-clear" onClick={() => setKeyword('')} aria-label="清空筛选">
              <IconClose size={13} />
            </button>
          )}
        </span>
        <span className="ac-toolbar-count">
          共 <b>{list.length}</b> 个账号{keyword ? `，匹配 ${filtered.length} 个` : ''}
        </span>
      </div>

      <AsyncBox
        loading={accounts.loading}
        error={accounts.error}
        notReady={accounts.notReady}
        onRetry={accounts.reload}
        empty={list.length === 0 || filtered.length === 0}
        emptyFiltered={list.length > 0 && filtered.length === 0}
        onClearFilters={() => setKeyword('')}
        emptyTitle={list.length === 0 ? '还没有闲鱼账号' : '没有匹配的账号'}
        emptyDesc={list.length === 0 ? '点右上角「扫码添加账号」，用手机闲鱼 App 扫一下二维码就能绑定；建议先在手机上关掉「二次验证」以免频繁重登。' : '换个关键词试试，或清空筛选。'}
        emptyAction={list.length === 0 ? (
          <button className="btn primary sm" onClick={() => setQrOpen(true)}>
            <IconQr size={14} /> 扫码添加账号
          </button>
        ) : undefined}
      >
        {(
          <div className="ac-grid">
            {filtered.map((account) => {
              const login = loginStateLabel(account.login_state)
              const runtime = runtimeStateLabel(account.runtime_state)
              const rowBusy = busyId === account.id
              const blocked = bool(account.blocked)
              const name = account.nickname || account.remark || '(未命名)'
              return (
                <div key={account.id} className={`ac-card${blocked ? ' blocked' : ''}`}>
                  <div className="ac-card-head">
                    <AccountAvatar account={account} />
                    <div className="ac-card-id">
                      <div className="ac-card-name" title={name}>
                        {name}
                      </div>
                      <div className="ac-card-sub mono" title={account.id}>
                        {account.id}
                      </div>
                    </div>
                    <div className="ac-card-badges">
                      <Badge tone={login.tone}>{login.text}</Badge>
                      <Badge tone={runtime.tone}>{runtime.text}</Badge>
                    </div>
                  </div>

                  <div className="ac-facts">
                    <span className="ac-fact">
                      <span className="ac-fact-k">闲鱼用户 ID</span>
                      <span className="ac-fact-v mono">{account.xianyu_user_id || '未获取'}</span>
                    </span>
                    <span className="ac-fact">
                      <span className="ac-fact-k">备注</span>
                      <span className="ac-fact-v" title={account.remark || ''}>
                        {account.remark || '-'}
                      </span>
                    </span>
                    <span className="ac-fact">
                      <span className="ac-fact-k">最近更新</span>
                      <span className="ac-fact-v">{formatTime(account.updated_at)}</span>
                    </span>
                    <span className="ac-fact">
                      <span className="ac-fact-k">凭据</span>
                      <span className="ac-fact-v ac-chips">
                        <span className={`ac-chip${account.has_cookie ? ' ok' : ' off'}`}>
                          {account.has_cookie ? 'Cookie 已存' : '无 Cookie'}
                        </span>
                        <span className={`ac-chip${account.has_x5sec ? ' ok' : ' off'}`}>
                          {account.has_x5sec ? 'x5sec 已存' : '无 x5sec'}
                        </span>
                      </span>
                    </span>
                  </div>

                  {blocked && (
                    <Notice kind="warn">
                      风控熔断中：连续命中 {Number(account.consecutive_hits) || 0} 次
                      {typeof account.remaining_seconds === 'number' && account.remaining_seconds > 0
                        ? `，约 ${formatDuration(account.remaining_seconds)} 后自动解除`
                        : '，暂时不会再向平台发请求'}
                      。
                      <button className="ac-inline-link" type="button" onClick={() => captcha(account)}>
                        手动验证
                      </button>
                    </Notice>
                  )}

                  {login.tone === 'danger' && account.relogin_reason && (
                    <div className="ac-reason">
                      <IconWarn size={13} /> {account.relogin_reason}
                    </div>
                  )}

                  <div className="ac-toggles">
                    <Switch
                      checked={bool(account.enabled)}
                      disabled={rowBusy}
                      label="启用该账号"
                      onChange={(next) => patchAccount(account, { enabled: next })}
                    />
                    <Switch
                      checked={bool(account.auto_reply)}
                      disabled={rowBusy}
                      label="AI 自动回复"
                      onChange={(next) => patchAccount(account, { auto_reply: next })}
                    />
                    {!bool(account.enabled) && <Badge tone="muted">已停用</Badge>}
                  </div>

                  <div className="ac-actions">
                    <button className="btn sm" disabled={rowBusy} onClick={() => setReloginTarget(account)}>
                      <IconKey size={14} /> 重新登录
                    </button>
                    <button
                      className="btn sm"
                      disabled={rowBusy}
                      onClick={() => refreshProfile(account)}
                      title="从闲鱼重新拉取昵称与头像"
                    >
                      <IconRefresh size={14} /> 刷新资料
                    </button>
                    <button
                      className="btn sm"
                      disabled={rowBusy}
                      onClick={() => captcha(account)}
                      title="打开浏览器窗口手动完成滑块验证"
                    >
                      <IconShield size={14} /> 手动验证
                    </button>
                    <button className="btn sm ghost" disabled={rowBusy} onClick={() => setEditing(account)}>
                      <IconEdit size={14} /> 编辑
                    </button>
                    <button className="btn sm danger" disabled={rowBusy} onClick={() => setRemoving(account)}>
                      <IconTrash size={14} /> 删除
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </AsyncBox>

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
  const [left, setLeft] = useState<number | null>(null)
  const pollRef = useRef<number | null>(null)
  const doneRef = useRef(false)
  // 连续失败计数。轮询要跑满二维码有效期（默认每 2 秒一次 ≈ 150 次），中间
  // 偶发一次网络抖动/后端瞬时 5xx 就弹错并停轮询的话，用户明明扫上了也白扫 ——
  // 一次抖动不该让整次登录失败，连续失败才判死。
  const failRef = useRef(0)
  const MAX_POLL_FAILURES = 5
  // 回调存进 ref：父组件传进来的是行内箭头函数（每次重渲染都是新引用），
  // 直接放进轮询 effect 的依赖里会让「toast / accounts.changed 推送到来」就重建
  // interval —— 每 2 秒的节奏被打断、还会立刻多发一次请求。
  const onCloseRef = useRef(onClose)
  const onSuccessRef = useRef(onSuccess)
  useEffect(() => {
    onCloseRef.current = onClose
    onSuccessRef.current = onSuccess
  }, [onClose, onSuccess])

  const start = useCallback(async () => {
    setStarting(true)
    setError('')
    setQr(null)
    setState('pending')
    setMessage('')
    setLeft(null)
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

    // 二维码作废/确认/判死后要**马上停**：只置 doneRef 的话 interval 还活着，
    // 会继续每 2 秒拿一个死 ticket 打 `GET /api/accounts/qr/{ticket}`，直到关弹窗。
    const stop = () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
      pollRef.current = null
    }

    // 这张二维码作废的标记：点「重新获取二维码」后 start() 会把 doneRef 重置为 false，
    // 上一张 ticket 的在途响应若晚到，会把**新**二维码盖上「已过期」并停掉新轮询。
    let cancelled = false

    const poll = async () => {
      if (cancelled || doneRef.current) return
      try {
        const result = await api.get<QrPoll>(`/api/accounts/qr/${encodeURIComponent(ticket)}`)
        if (cancelled || doneRef.current) return
        failRef.current = 0
        setState(result.state)
        if (result.message) setMessage(result.message)
        if (result.state === 'confirmed') {
          doneRef.current = true
          stop()
          onSuccessRef.current()
          window.setTimeout(() => onCloseRef.current(), 900)
        } else if (result.state === 'expired' || result.state === 'failed') {
          doneRef.current = true
          stop()
        }
      } catch (err) {
        if (cancelled || doneRef.current) return
        failRef.current += 1
        if (failRef.current < MAX_POLL_FAILURES) {
          // 还没到判死线：继续下一轮，只是提示一下，不清空二维码也别停轮询。
          setError(`状态查询暂时失败，正在重试（第 ${failRef.current} 次）：${errorText(err)}`)
          return
        }
        doneRef.current = true
        stop()
        setError(errorText(err))
      }
    }

    void poll()
    pollRef.current = window.setInterval(poll, 2000)
    return () => {
      cancelled = true
      stop()
    }
  }, [qr?.ticket])

  // 倒计时：只用后端给的 expires_in 起算，不猜。
  useEffect(() => {
    if (typeof qr?.expires_in !== 'number') {
      setLeft(null)
      return
    }
    setLeft(qr.expires_in)
    const timer = window.setInterval(() => {
      setLeft((value) => (value === null || value <= 1 ? 0 : value - 1))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [qr])

  const bad = state === 'expired' || state === 'failed'
  const confirmed = state === 'confirmed'
  const step = qrStepIndex(state)
  const tone = confirmed ? 'ok' : bad ? 'danger' : 'warn'
  const counting = typeof left === 'number' && left > 0 && !bad && !confirmed

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
            <IconRefresh size={15} /> {starting ? '获取中…' : '重新获取二维码'}
          </button>
        </>
      }
    >
      {error && <Notice kind="error">{error}</Notice>}

      {starting && !qr && (
        <div className="ac-qr-loading">
          <span className="spinner" />
          <span>正在向闲鱼请求二维码…</span>
        </div>
      )}

      {qr && (
        <div className="ac-qr">
          <div className="ac-qr-left">
            <div className={`ac-qr-frame${confirmed ? ' ok' : ''}${bad ? ' bad' : ''}`}>
              {qr.qr_image_b64 ? (
                <img
                  src={
                    qr.qr_image_b64.startsWith('data:')
                      ? qr.qr_image_b64
                      : `data:image/png;base64,${qr.qr_image_b64}`
                  }
                  alt="闲鱼登录二维码"
                />
              ) : (
                <span className="ac-qr-noframe">
                  <IconWarn size={26} />
                  <span>后端没有返回二维码图片</span>
                </span>
              )}
              {bad && (
                <span className="ac-qr-mask bad">
                  <IconXCircle size={30} />
                  <strong>{state === 'expired' ? '二维码已过期' : '登录失败'}</strong>
                  <span>点下方「重新获取二维码」</span>
                </span>
              )}
              {confirmed && (
                <span className="ac-qr-mask ok">
                  <IconCheckCircle size={30} />
                  <strong>登录成功</strong>
                  <span>正在返回账号列表…</span>
                </span>
              )}
            </div>
            {counting && (
              <span className="ac-count">
                <IconClock size={14} /> 剩余 {left} 秒
              </span>
            )}
            {typeof left === 'number' && left === 0 && !bad && !confirmed && (
              <span className="ac-count warn">二维码应已过期，建议重新获取</span>
            )}
          </div>

          <div className="ac-qr-right">
            <ol className="ac-steps">
              {QR_STEPS.map((label, index) => (
                <li
                  key={label}
                  className={`ac-step${step === index ? ' active' : ''}${step > index ? ' done' : ''}${
                    step < 0 ? ' void' : ''
                  }`}
                >
                  <span className="ac-step-dot">{step > index ? <IconCheck size={13} /> : index + 1}</span>
                  <span className="ac-step-label">{label}</span>
                </li>
              ))}
            </ol>

            <div className="ac-qr-status">
              <Badge tone={tone}>{QR_STATE_TEXT[state] || state}</Badge>
              {message && <span className="ac-qr-msg">{message}</span>}
            </div>

            <div className="ac-qr-tips">
              <span className="ac-qr-tip">
                <IconQr size={14} /> 手机闲鱼 → 我的 → 右上角设置 → 扫码登录
              </span>
              <span className="ac-qr-tip">
                <IconClock size={14} /> 二维码有效期 {qr.expires_in ? `${qr.expires_in} 秒` : '约 2 分钟'}
              </span>
              {bad ? (
                <span className="ac-qr-tip warn">
                  <IconWarn size={14} /> 连续失败就先在账号页点「手动验证」过掉滑块，再回来重新扫码
                </span>
              ) : (
                <span className="ac-qr-tip">
                  <IconCheck size={14} /> 扫码后需要在手机上点「确认登录」，电脑这边会自动跳转
                </span>
              )}
            </div>
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
      <div className="ac-kv">
        <span className="ac-kv-k">账号 ID</span>
        <span className="ac-kv-v mono">{account.id}</span>
        <span className="ac-kv-k">闲鱼用户 ID</span>
        <span className="ac-kv-v mono">{account.xianyu_user_id || '未获取'}</span>
        <span className="ac-kv-k">最近更新</span>
        <span className="ac-kv-v">{formatTime(account.updated_at)}</span>
      </div>

      <Field label="备注" tip="只影响本机显示，不会改平台上的昵称">
        <input
          type="text"
          value={remark}
          onChange={(e) => setRemark(e.target.value)}
          placeholder="给这个账号起个好记的名字"
        />
      </Field>

      <div className="ac-edit-switches">
        <Switch checked={enabled} onChange={setEnabled} label="启用该账号" />
        <Switch checked={autoReply} onChange={setAutoReply} label="允许 AI 自动回复" />
      </div>
    </Modal>
  )
}
