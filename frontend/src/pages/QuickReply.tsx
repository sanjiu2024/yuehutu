/**
 * 「话术」页 —— 官方 IM 工作台的「快捷回复」（分组 + 短语）的增删改。
 *
 * 契约（后端由 Lead 实现，逐字照此写，前端不改后端）：
 *   GET  /api/quick-reply/{account_id}/groups
 *        -> {success, data:{account_id, groups:[{group_id, group_name, contents:[{content_id, content}]}] | null,
 *                           total, source, fetched_at, warnings[]}}
 *   POST /api/quick-reply/{account_id}/groups                        body {group_name, confirm}
 *   POST /api/quick-reply/{account_id}/groups/{group_id}             body {group_name, confirm}
 *   POST /api/quick-reply/{account_id}/groups/{group_id}/delete      body {confirm}
 *   POST /api/quick-reply/{account_id}/phrases                       body {group_id, content, confirm}
 *   POST /api/quick-reply/{account_id}/phrases/{content_id}          body {group_id, content, confirm}
 *   POST /api/quick-reply/{account_id}/phrases/{content_id}/delete   body {group_id, confirm}
 *
 * 三条不能破的口径：
 *   1. `groups === null`（平台没给列表字段）与 `groups === []`（真的没有话术）**必须分开提示**。
 *   2. 拿不到的字段一律显示 `-`，**不补 0**、不编中文。
 *   3. 写操作全部走唯一写出口 + Modal 填值 + ConfirmDialog(danger) 二次确认。
 */

import { useState, type ReactNode } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Empty, Field, Modal, Notice, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatTime } from '../format'
import {
  IconChat,
  IconEdit,
  IconPlus,
  IconRefresh,
  IconTrash,
} from '../icons'
import type { Account } from '../types'
import './quickreply.css'

/** 官方限制：分组名 1~10 字。 */
const GROUP_NAME_MAX = 10
/** 官方限制：短语内容 1~200 字。 */
const PHRASE_MAX = 200

interface QuickPhrase {
  content_id?: string | null
  content?: string | null
}

interface QuickGroup {
  group_id?: string | null
  group_name?: string | null
  /** 平台没给是 `null`（不是空数组）。 */
  contents?: QuickPhrase[] | null
}

interface QuickReplyResult {
  account_id?: string | null
  /** `null` = 平台没给列表字段；`[]` = 真的没有话术。两者不是一回事。 */
  groups?: QuickGroup[] | null
  total?: number | null
  source?: string | null
  fetched_at?: string | null
  warnings?: unknown
}

/** `null` / 缺失 / 空串一律 `-`，**不补 0**。 */
function plain(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  return String(value)
}

function warningLines(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object') {
        const record = item as Record<string, unknown>
        const code = record.code === null || record.code === undefined ? '' : String(record.code)
        const raw = record.message ?? record.msg
        const message = raw === null || raw === undefined ? '' : String(raw)
        return [code, message].filter(Boolean).join(' ')
      }
      return String(item ?? '')
    })
    .filter(Boolean)
}

function strId(value: unknown): string {
  return typeof value === 'string' && value ? value : ''
}

/**
 * **全文件唯一的写出口**：所有写操作都从这里走，`confirm: true` 只能是字面量，
 * 前端不做「默认执行」。本文件里真实发起写请求的地方只有这一处。
 */
function postQuickWrite(path: string, body: Record<string, unknown>) {
  return api.post<Record<string, unknown>>(path, { ...body, confirm: true })
}

type Draft =
  | { kind: 'group-create' }
  | { kind: 'group-rename'; groupId: string; groupName: string }
  | { kind: 'group-delete'; groupId: string; groupName: string; phraseCount: number | null }
  | { kind: 'phrase-create'; groupId: string; groupName: string }
  | { kind: 'phrase-edit'; groupId: string; contentId: string; groupName: string; content: string }
  | { kind: 'phrase-delete'; groupId: string; contentId: string; groupName: string; content: string }

interface Pending {
  title: string
  confirmText: string
  path: string
  body: Record<string, unknown>
  lines: Array<{ label: string; value: string }>
}

function draftTitle(draft: Draft): string {
  switch (draft.kind) {
    case 'group-create':
      return '新建话术分组'
    case 'group-rename':
      return '改分组名'
    case 'group-delete':
      return '删除话术分组'
    case 'phrase-create':
      return '给分组加一条短语'
    case 'phrase-edit':
      return '改短语内容'
    default:
      return '删除短语'
  }
}

export function QuickReplyPage() {
  const toast = useToast()
  const [picked, setPicked] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [groupName, setGroupName] = useState('')
  const [content, setContent] = useState('')
  const [failure, setFailure] = useState('')
  const [busy, setBusy] = useState(false)

  const accounts = useAsync<Account[]>(
    () => api.get<Account[]>('/api/accounts'),
    [],
    true,
    ['accounts.changed'],
  )
  // 派生，不用 effect 回写：选中的账号，否则第一个账号。
  const accountId = picked || accounts.data?.[0]?.id || ''

  const state = useAsync<QuickReplyResult>(
    () => api.get<QuickReplyResult>(`/api/quick-reply/${encodeURIComponent(accountId)}/groups`),
    [accountId],
    Boolean(accountId),
    // 实时刷新：订阅 topic，**不轮询**。
    ['accounts.changed'],
  )

  const base = `/api/quick-reply/${encodeURIComponent(accountId)}`
  const data = state.data
  const groups = data?.groups
  const warnings = warningLines(data?.warnings)

  function closeAll() {
    setDraft(null)
    setPending(null)
    setFailure('')
  }

  function openDraft(next: Draft) {
    setFailure('')
    setPending(null)
    if (next.kind === 'group-create') setGroupName('')
    else if (next.kind === 'group-rename') setGroupName(next.groupName)
    else if (next.kind === 'phrase-create') setContent('')
    else if (next.kind === 'phrase-edit') setContent(next.content)
    setDraft(next)
  }

  /** 校验 + 组装待确认的请求。校验不过就**不发请求**，只提示。 */
  function buildPending(): Pending | null {
    if (!draft) return null
    if (!accountId) {
      toast.info('先选一个账号')
      return null
    }
    const accountLine = { label: '账号', value: accountId }

    if (draft.kind === 'group-create' || draft.kind === 'group-rename') {
      const name = groupName.trim()
      if (!name) {
        toast.info('分组名不能为空')
        return null
      }
      if (name.length > GROUP_NAME_MAX) {
        toast.info(`分组名最多 ${GROUP_NAME_MAX} 个字，现在 ${name.length} 个 —— 超长的请求不会发出去`)
        return null
      }
      const lines = [accountLine, { label: 'group_name', value: name }]
      if (draft.kind === 'group-create') {
        return { title: '新建分组', confirmText: '新建分组', path: `${base}/groups`, body: { group_name: name }, lines }
      }
      return {
        title: '改分组名',
        confirmText: '改名',
        path: `${base}/groups/${encodeURIComponent(draft.groupId)}`,
        body: { group_name: name },
        lines: [accountLine, { label: 'group_id', value: draft.groupId }, { label: '原 group_name', value: plain(draft.groupName) }, { label: '新 group_name', value: name }],
      }
    }

    if (draft.kind === 'phrase-create' || draft.kind === 'phrase-edit') {
      const text = content.trim()
      if (!text) {
        toast.info('短语内容不能为空')
        return null
      }
      if (text.length > PHRASE_MAX) {
        toast.info(`短语最多 ${PHRASE_MAX} 个字，现在 ${text.length} 个 —— 超长的请求不会发出去`)
        return null
      }
      const lines = [accountLine, { label: 'group_id', value: draft.groupId }, { label: 'content', value: text }]
      if (draft.kind === 'phrase-create') {
        return { title: '新建短语', confirmText: '新建短语', path: `${base}/phrases`, body: { group_id: draft.groupId, content: text }, lines }
      }
      return {
        title: '改短语',
        confirmText: '保存修改',
        path: `${base}/phrases/${encodeURIComponent(draft.contentId)}`,
        body: { group_id: draft.groupId, content: text },
        lines: [accountLine, { label: 'group_id', value: draft.groupId }, { label: 'content_id', value: draft.contentId }, { label: '原 content', value: plain(draft.content) }, { label: '新 content', value: text }],
      }
    }

    if (draft.kind === 'group-delete') {
      return {
        title: '删除分组',
        confirmText: '删除分组',
        path: `${base}/groups/${encodeURIComponent(draft.groupId)}/delete`,
        body: {},
        lines: [accountLine, { label: 'group_id', value: draft.groupId }, { label: 'group_name', value: plain(draft.groupName) }, { label: '组内短语数', value: draft.phraseCount === null ? '平台没给 contents（null），不当作 0' : String(draft.phraseCount) }],
      }
    }

    return {
      title: '删除短语',
      confirmText: '删除短语',
      path: `${base}/phrases/${encodeURIComponent(draft.contentId)}/delete`,
      body: { group_id: draft.groupId },
      lines: [accountLine, { label: 'group_id', value: draft.groupId }, { label: 'content_id', value: draft.contentId }, { label: 'content', value: plain(draft.content) }],
    }
  }

  function next() {
    const built = buildPending()
    if (built) setPending(built)
  }

  async function run() {
    if (!pending) return
    setBusy(true)
    try {
      await postQuickWrite(pending.path, pending.body)
      toast.success(`${pending.confirmText}成功`)
      setPending(null)
      setDraft(null)
      setFailure('')
      state.reload()
    } catch (err) {
      // 失败：**弹窗不关**，把后端原话照实显示（confirmation_required / invalid_fields /
      // verification_required / not_found / protocol_error / xianyu_not_ready 的 message）。
      setPending(null)
      setFailure(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const isDelete = draft?.kind === 'group-delete' || draft?.kind === 'phrase-delete'
  const needsGroupName = draft?.kind === 'group-create' || draft?.kind === 'group-rename'
  const needsContent = draft?.kind === 'phrase-create' || draft?.kind === 'phrase-edit'
  // 409 verification_required / 503 xianyu_not_ready 都长这样：useAsync 只给文案，所以按文案判。
  // 这类不是「页面坏了」，是账号没登录 —— 必须给一条去账号页的路，不能让人卡在红框前。
  const relogin = /扫码|Cookie|登录态|未登录|登录已过期|失效/.test(state.error)

  let body: ReactNode = null
  if (!accountId) {
    body = (
      <Notice kind="info">
        还没有可用账号。先到「账号」页扫码登录，这里才有话术可看。
      </Notice>
    )
  } else {
    body = (
      <>
        <div className="qr-meta">
          <span className="qr-meta-item">账号 <b>{accountId}</b></span>
          <span className="qr-meta-item">平台返回 total <b>{plain(data?.total)}</b></span>
          <span className="qr-meta-item">数据来源 <b>{plain(data?.source)}</b></span>
          <span className="qr-meta-item">拉取于 <b>{data?.fetched_at ? formatTime(data.fetched_at) : '-'}</b></span>
        </div>

        {warnings.length > 0 ? (
          <Notice kind="warn">
            平台 / 后端给出的提示：
            <ul className="qr-warn">
              {warnings.map((warning, index) => (
                <li key={`${warning}-${index}`}>{warning}</li>
              ))}
            </ul>
          </Notice>
        ) : null}

        {groups === null || groups === undefined ? (
          <Notice kind="warn">
            <b>平台这次没给列表字段</b>（<code>groups</code> 是 <code>null</code>）。
            这跟「这个账号真的没有话术」<b>不是一回事</b>，所以这里既不显示空列表、也不会写「0 个」。
          </Notice>
        ) : groups.length === 0 ? (
          <Empty
            icon="chat"
            title="这个账号还没有任何话术分组"
            desc="平台明确回了一个空数组（不是 null），也就是真的没有。点右上角「新建分组」建第一个。"
            action={
              <button className="btn sm primary" onClick={() => openDraft({ kind: 'group-create' })}>
                <IconPlus size={13} /> 新建分组
              </button>
            }
          />
        ) : (
          <div className="qr-list">
            {groups.map((group, groupIndex) => {
              const groupId = strId(group.group_id)
              const name = group.group_name ?? ''
              const contents = Array.isArray(group.contents) ? group.contents : null
              return (
                <section className="card qr-group" key={groupId || `group-${groupIndex}`}>
                  <div className="qr-group-head">
                    <span className="qr-group-name" title={name || undefined}>{plain(group.group_name)}</span>
                    <span className="qr-group-count">
                      {contents === null
                        ? '短语：平台没给 contents（null）'
                        : `${contents.length} 条短语`}
                    </span>
                    <span className="qr-group-actions">
                      <button
                        className="btn sm"
                        disabled={!groupId}
                        title={groupId ? '改这个分组的名字' : '平台没给 group_id，改不了'}
                        onClick={() => openDraft({ kind: 'group-rename', groupId, groupName: name })}
                      >
                        <IconEdit size={13} /> 改名
                      </button>
                      <button
                        className="btn sm"
                        disabled={!groupId}
                        title={groupId ? '给这个分组加一条短语' : '平台没给 group_id，加不了'}
                        onClick={() => openDraft({ kind: 'phrase-create', groupId, groupName: name })}
                      >
                        <IconPlus size={13} /> 加短语
                      </button>
                      <button
                        className="btn sm danger"
                        disabled={!groupId}
                        title={groupId ? '删掉整个分组（含组内短语）' : '平台没给 group_id，删不了'}
                        onClick={() =>
                          openDraft({
                            kind: 'group-delete',
                            groupId,
                            groupName: name,
                            phraseCount: contents === null ? null : contents.length,
                          })
                        }
                      >
                        <IconTrash size={13} /> 删分组
                      </button>
                    </span>
                  </div>

                  {contents === null ? (
                    <p className="qr-na">
                      平台没给这个分组的 <code>contents</code>（是 <code>null</code>，不是空数组）——
                      所以这里不写「0 条短语」。
                    </p>
                  ) : contents.length === 0 ? (
                    <p className="qr-na">平台明确回了空数组：这个分组下确实一条短语都没有。</p>
                  ) : (
                    <ul className="qr-phrases">
                      {contents.map((phrase, phraseIndex) => {
                        const contentId = strId(phrase.content_id)
                        const text = phrase.content ?? ''
                        return (
                          <li className="qr-phrase" key={contentId || `phrase-${phraseIndex}`}>
                            <span className="qr-phrase-text" title={text || undefined}>
                              {plain(phrase.content)}
                            </span>
                            <span className="qr-phrase-actions">
                              <button
                                className="btn sm ghost"
                                disabled={!groupId || !contentId}
                                title={contentId ? '改这条短语' : '平台没给 content_id，改不了'}
                                onClick={() =>
                                  openDraft({ kind: 'phrase-edit', groupId, contentId, groupName: name, content: text })
                                }
                              >
                                <IconEdit size={13} /> 改
                              </button>
                              <button
                                className="btn sm ghost"
                                disabled={!groupId || !contentId}
                                title={contentId ? '删这条短语' : '平台没给 content_id，删不了'}
                                onClick={() =>
                                  openDraft({ kind: 'phrase-delete', groupId, contentId, groupName: name, content: text })
                                }
                              >
                                <IconTrash size={13} /> 删
                              </button>
                            </span>
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </section>
              )
            })}
          </div>
        )}
      </>
    )
  }

  return (
    <>
      <section className="qr-head">
        <div className="qr-head-text">
          <h3>
            <IconChat size={16} /> 话术
          </h3>
          <p className="qr-sub">
            官方 IM 工作台的「快捷回复」= 分组 + 短语。这里的增删改会<b>真的</b>改到闲鱼上的快捷回复
            （不是只改本地），每个动作都要二次确认。
          </p>
        </div>
        <div className="qr-actions">
          <select
            className="qr-select"
            aria-label="选择账号"
            value={accountId}
            onChange={(event) => setPicked(event.target.value)}
          >
            {accounts.data?.map((account) => (
              <option key={account.id} value={account.id}>
                {account.nickname || account.id}
              </option>
            ))}
            {!accountId ? <option value="">（没有可用账号）</option> : null}
          </select>
          <button className="btn sm" onClick={state.reload} disabled={!accountId || state.loading}>
            <IconRefresh size={13} /> {state.loading ? '刷新中…' : '刷新'}
          </button>
          <button
            className="btn sm primary"
            disabled={!accountId}
            title={accountId ? '新建一个话术分组' : '先选账号'}
            onClick={() => openDraft({ kind: 'group-create' })}
          >
            <IconPlus size={13} /> 新建分组
          </button>
        </div>
      </section>

      {relogin ? (
        <Notice kind="warn">
          这个账号还没登录（或 Cookie 已失效）：话术接口要带登录态才读得到，所以下面一个分组都拿不回来。
          先到「账号」页完成扫码登录，回来点一次「刷新」就好 —— 这不是页面坏了，也不是没有话术。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>
            去账号页重新扫码
          </a>
        </Notice>
      ) : null}

      <AsyncBox
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        empty={false}
      >
        {body}
      </AsyncBox>

      {draft ? (
        <Modal
          title={draftTitle(draft)}
          onClose={closeAll}
          footer={
            <>
              <button className="btn ghost" onClick={closeAll} disabled={busy}>取消</button>
              <button className={isDelete ? 'btn danger' : 'btn primary'} onClick={next} disabled={busy}>
                {isDelete ? '删除…' : '下一步：确认'}
              </button>
            </>
          }
        >
          {failure ? (
            <Notice kind="error" onClose={() => setFailure('')}>{failure}</Notice>
          ) : null}

          {needsGroupName ? (
            <Field
              label="分组名"
              tip={`官方限制 1 ~ ${GROUP_NAME_MAX} 个字；超长前端直接拦下，不发请求。`}
            >
              <input
                className="qr-input"
                value={groupName}
                maxLength={GROUP_NAME_MAX}
                placeholder="例如：发货时间"
                onChange={(event) => setGroupName(event.target.value)}
              />
              <div className="qr-count">
                {groupName.length} / {GROUP_NAME_MAX}
              </div>
            </Field>
          ) : null}

          {needsContent ? (
            <>
              <Field label="所属分组">
                <div className="qr-readonly">{plain('groupName' in draft ? draft.groupName : '')}</div>
              </Field>
              <Field
                label="短语内容"
                tip={`官方限制 1 ~ ${PHRASE_MAX} 个字；超长前端直接拦下，不发请求。`}
              >
                <textarea
                  className="qr-textarea"
                  value={content}
                  maxLength={PHRASE_MAX}
                  rows={4}
                  placeholder="例如：亲，今天下午 5 点前下单，当天就发货哦～"
                  onChange={(event) => setContent(event.target.value)}
                />
                <div className="qr-count">
                  {content.length} / {PHRASE_MAX}
                </div>
              </Field>
            </>
          ) : null}

          {draft.kind === 'group-delete' ? (
            <Notice kind="warn">
              要删的分组：<b>{plain(draft.groupName)}</b>（<code>group_id</code> {draft.groupId}）。
              {draft.phraseCount === null
                ? ' 平台没给这个分组的 contents（null），所以这里不写「0 条短语」—— 组里可能有短语，删分组会一起带走。'
                : ` 组里有 ${draft.phraseCount} 条短语，删分组会一起带走。`}
            </Notice>
          ) : null}

          {draft.kind === 'phrase-delete' ? (
            <Notice kind="warn">
              要删的短语：<b>{plain(draft.content)}</b>（<code>content_id</code> {draft.contentId}，
              属于分组「{plain(draft.groupName)}」）。
            </Notice>
          ) : null}

          <p className="qr-note">
            点下一步会先弹二次确认，确认框里会逐条列出提交值；真正的请求要带上
            <code> confirm: true</code>，缺它后端直接 400 <code>confirmation_required</code>，一个平台请求都不发。
          </p>
        </Modal>
      ) : null}

      {pending ? (
        <ConfirmDialog
          danger
          busy={busy}
          title={pending.title}
          confirmText={pending.confirmText}
          onCancel={() => setPending(null)}
          onConfirm={run}
          message={
            <>
              <p className="qr-note">
                <b>这会真的改动闲鱼上的快捷回复（不是只改本地）</b>，做完不能在这里撤销。
              </p>
              <div className="qr-lines">
                {pending.lines.map((line) => (
                  <div className="qr-line" key={line.label}>
                    <span>{line.label}</span>
                    <b>{line.value}</b>
                  </div>
                ))}
              </div>
              <p className="qr-note">
                提交时会带 <code>confirm: true</code>；缺 confirm 后端直接 400
                <code> confirmation_required</code>，一个平台请求都不发。
              </p>
            </>
          }
        />
      ) : null}
    </>
  )
}
