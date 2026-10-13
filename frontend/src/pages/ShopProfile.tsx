/**
 * 店铺资料（读 + 写）。
 *
 * 只读：`GET /api/shop/{account_id}/profile`
 * 写：`POST /api/shop/{account_id}/profile/{nick,desc,avatar}` —— 每个都要二次确认。
 *
 * 契约要点（照 docs/contracts/shop.md）：
 * - 平台没返回的字段一律 `null` ⇒ 界面显示「-」，**绝不补 0、绝不编值**。
 * - 昵称 4~12 字、头像必须 `http(s)://` 开头 —— 这是官方 bundle 里的原文校验，前端也先挡一次。
 * - 唯一写出口 `postProfileWrite()`：`confirm: true` 是字面量、写在 `...body` 之后，调用方覆盖不了。
 * - 成功后平台不会回显整份资料 ⇒ 重新拉一次只读接口，而不是拿本地值假装成功。
 */
import { useState } from 'react'

import { api, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Modal, Notice, useToast } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconEdit, IconImage, IconInfo, IconRefresh } from '../icons'
import type { Account } from '../types'

import './shop.css'
import './shopprofile.css'

interface ShopProfileResult {
  account_id?: string
  user_nick?: string | null
  shop_desc?: string | null
  user_avatar?: string | null
  industry?: string | null
  alipay_account?: string | null
  business_type?: string | null
  pro_shop_valid_time?: string | null
  warnings?: string[]
  source?: string
  fetched_at?: string
}

type WriteKind = 'nick' | 'desc' | 'avatar'

interface WriteSpec {
  title: string
  label: string
  hint: string
  body: (value: string) => Record<string, unknown>
  /** 返回非空文案表示不通过。 */
  check: (value: string) => string
  multiline?: boolean
}

const NICK_MIN = 4
const NICK_MAX = 12

const WRITE_SPECS: Record<WriteKind, WriteSpec> = {
  nick: {
    title: '改店铺昵称',
    label: '新昵称',
    hint: `官方限制 ${NICK_MIN}~${NICK_MAX} 个字，前后空格会被去掉。`,
    body: (value) => ({ nick: value.trim() }),
    check: (value) => {
      const text = value.trim()
      if (!text) return '昵称不能为空。'
      if (text.length < NICK_MIN) return `昵称不能少于 ${NICK_MIN} 个字符（官方原文）。`
      if (text.length > NICK_MAX) return `昵称不能超过 ${NICK_MAX} 个字符（官方原文）。`
      return ''
    },
  },
  desc: {
    title: '改店铺简介',
    label: '新简介',
    hint: '留空会被拦下：简介不能是空的（想清空请去官方卖家工作台）。',
    body: (value) => ({ desc: value.trim() }),
    check: (value) => (value.trim() ? '' : '简介不能为空。'),
    multiline: true,
  },
  avatar: {
    title: '改店铺头像',
    label: '头像图片地址',
    hint: '要一个 http(s) 开头的图片地址。本机不上传文件，只把这个地址交给平台。',
    body: (value) => ({ avatar_url: value.trim() }),
    check: (value) => (/^https?:\/\/\S+$/i.test(value.trim()) ? '' : '头像地址要以 http:// 或 https:// 开头。'),
  },
}

function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const raw = String(value).trim()
  return raw ? raw : '-'
}

function isMissing(value: unknown): boolean {
  return value === null || value === undefined
}

/** 唯一写出口：`confirm: true` 写在 `...body` 之后，调用方无法用 body 覆盖。 */
function postProfileWrite<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return api.post<T>(path, { ...body, confirm: true })
}

function RefreshButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button className="btn sm" onClick={onClick} disabled={disabled}>
      <span className="sh-ico">
        <IconRefresh size={13} />
      </span>
      刷新
    </button>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function ShopProfilePage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [draft, setDraft] = useState<{ kind: WriteKind; value: string } | null>(null)
  const [problem, setProblem] = useState('')
  const [askConfirm, setAskConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [writeError, setWriteError] = useState('')

  /** 选中账号：用户选的优先，否则退回第一个（派生值，不用 useEffect 回写）。 */
  const accountId = picked || accounts.data?.[0]?.id || ''

  const data = useAsync<ShopProfileResult>(
    () => api.get<ShopProfileResult>(`/api/shop/${encodeURIComponent(accountId)}/profile`),
    [accountId],
    Boolean(accountId),
    ['shop.changed', 'accounts.changed'],
  )

  const profile = data.data
  const warnings = profile?.warnings || []

  function openWrite(kind: WriteKind) {
    setDraft({ kind, value: '' })
    setProblem('')
    setWriteError('')
  }

  function closeWrite() {
    setDraft(null)
    setAskConfirm(false)
    setProblem('')
    setWriteError('')
  }

  async function submit() {
    if (!draft) return
    const spec = WRITE_SPECS[draft.kind]
    setBusy(true)
    setWriteError('')
    try {
      const path = `/api/shop/${encodeURIComponent(accountId)}/profile/${draft.kind}`
      await postProfileWrite<Record<string, unknown>>(path, spec.body(draft.value))
      toast.success(`${spec.title}成功`)
      setAskConfirm(false)
      closeWrite()
      data.reload()
    } catch (err) {
      // 后端 message 原样显示，弹窗不关（方便复制）
      setWriteError(errorText(err))
      setAskConfirm(false)
    } finally {
      setBusy(false)
    }
  }

  const spec = draft ? WRITE_SPECS[draft.kind] : null

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">店铺资料</h2>
          <p className="page-desc">
            官方「店铺资料」里的昵称 / 简介 / 头像。列表只读，写操作都要二次确认；确认前不会向闲鱼发任何请求。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="sh-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
        </div>
      </div>

      <div className="card">
        <div className="sh-bar">
          <div className="sh-field">
            <label className="sh-label" htmlFor="sp-account">
              账号
            </label>
            <select
              id="sp-account"
              value={accountId}
              onChange={(e) => setPicked(e.target.value)}
              disabled={accounts.loading}
            >
              {accounts.data && accounts.data.length > 0 ? (
                accounts.data.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.nickname || account.id}
                  </option>
                ))
              ) : (
                <option value="">（还没有账号）</option>
              )}
            </select>
          </div>
          <span className="sh-spacer" />
          <RefreshButton onClick={data.reload} disabled={data.loading || !accountId} />
        </div>
        {accounts.notReady && <Notice kind="warn">账号接口还没就绪，暂时选不了账号。</Notice>}
        {accounts.error && (
          <Notice kind="error" onRetry={accounts.reload}>
            {accounts.error}
          </Notice>
        )}
      </div>

      {!accountId ? (
        <div className="card">
          <Notice kind="info">先在上方选一个账号：店铺资料是按账号查的，没有账号就没数据可看。</Notice>
        </div>
      ) : (
        <div className="card">
          <div className="sh-head">
            <span className="sh-head-title">
              店铺资料
              <span className="sh-count">{isMissing(profile?.user_nick) ? '昵称 -' : text(profile?.user_nick)}</span>
            </span>
            <span className="sh-spacer" />
            <button className="btn sm" onClick={() => openWrite('nick')} disabled={data.loading}>
              <span className="sh-ico">
                <IconEdit size={13} />
              </span>
              改昵称
            </button>
            <button className="btn sm" onClick={() => openWrite('desc')} disabled={data.loading}>
              <span className="sh-ico">
                <IconEdit size={13} />
              </span>
              改简介
            </button>
            <button className="btn sm" onClick={() => openWrite('avatar')} disabled={data.loading}>
              <span className="sh-ico">
                <IconImage size={13} />
              </span>
              改头像
            </button>
          </div>

          {warnings.length > 0 && (
            <div className="sh-warnings">
              {warnings.map((w, idx) => (
                <Notice key={idx} kind="warn">
                  {w}
                </Notice>
              ))}
            </div>
          )}

          <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
            <div className="sp-body">
              <div className="sp-avatar">
                {profile?.user_avatar ? (
                  <img className="sp-avatar-img" src={profile.user_avatar} alt="店铺头像" />
                ) : (
                  <span className="sp-avatar-none">-</span>
                )}
                <span className="sp-avatar-hint">
                  {profile?.user_avatar ? '平台返回的头像地址' : '平台没有返回头像地址'}
                </span>
              </div>
              <div className="sp-grid">
                <div className="sp-row">
                  <span className="sp-k">昵称</span>
                  <span className="sp-v">{text(profile?.user_nick)}</span>
                </div>
                <div className="sp-row">
                  <span className="sp-k">简介</span>
                  <span className="sp-v sp-v-wrap">{text(profile?.shop_desc)}</span>
                </div>
                <div className="sp-row">
                  <span className="sp-k">行业</span>
                  <span className="sp-v">{text(profile?.industry)}</span>
                </div>
                <div className="sp-row">
                  <span className="sp-k">支付宝账号</span>
                  <span className="sp-v">{text(profile?.alipay_account)}</span>
                </div>
                <div className="sp-row">
                  <span className="sp-k">店铺类型</span>
                  <span className="sp-v">{text(profile?.business_type)}</span>
                </div>
                <div className="sp-row">
                  <span className="sp-k">专业卖家有效期</span>
                  <span className="sp-v">{text(profile?.pro_shop_valid_time)}</span>
                </div>
              </div>
            </div>

            <div className="sh-note">
              <span className="sh-ico">
                <IconInfo size={14} />
              </span>
              「-」= 平台这次没有返回这个值（<strong>不是 0、也不是空</strong>）。数据来源：
              {text(profile?.source)}，更新于 {profile?.fetched_at ? formatTime(profile.fetched_at) : ' -'}
            </div>
          </AsyncBox>
        </div>
      )}

      {draft && spec && (
        <Modal title={spec.title} onClose={closeWrite}>
          <div className="sp-form">
            <label className="sh-label" htmlFor="sp-write">
              {spec.label}
            </label>
            {spec.multiline ? (
              <textarea
                id="sp-write"
                className="sp-input"
                rows={4}
                value={draft.value}
                placeholder={spec.hint}
                onChange={(e) => {
                  setDraft({ ...draft, value: e.target.value })
                  setProblem('')
                }}
              />
            ) : (
              <input
                id="sp-write"
                className="sp-input"
                value={draft.value}
                placeholder={spec.hint}
                onChange={(e) => {
                  setDraft({ ...draft, value: e.target.value })
                  setProblem('')
                }}
              />
            )}
            <span className="sh-muted">{spec.hint}</span>
            {problem && <Notice kind="warn">{problem}</Notice>}
            {writeError && <Notice kind="error">{writeError}</Notice>}
            <div className="sp-actions">
              <button className="btn sm" onClick={closeWrite} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary sm"
                disabled={busy}
                onClick={() => {
                  const bad = spec.check(draft.value)
                  if (bad) {
                    setProblem(bad)
                    return
                  }
                  setProblem('')
                  setAskConfirm(true)
                }}
              >
                下一步：确认
              </button>
            </div>
          </div>
        </Modal>
      )}

      {askConfirm && draft && spec && (
        <ConfirmDialog
          danger
          busy={busy}
          title={`确认${spec.title}`}
          confirmText="确认提交到闲鱼"
          message={
            <>
              这一条会<strong>真的</strong>改动闲鱼上的店铺资料（不是只改本地），不可撤销。
              <br />
              账号：{accountId}
              <br />
              接口：<code>POST /api/shop/{accountId}/profile/{draft.kind}</code>
              <br />
              本次提交：
              {Object.entries(spec.body(draft.value)).map(([key, value]) => (
                <span key={key} className="sp-fact">
                  <code>{key}</code> = {String(value)}
                </span>
              ))}
            </>
          }
          onConfirm={submit}
          onCancel={() => setAskConfirm(false)}
        />
      )}
    </div>
  )
}
