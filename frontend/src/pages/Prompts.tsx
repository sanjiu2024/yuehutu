/**
 * 提示词：global / account / item 三级提示词的展示与编辑。
 *
 * 三级是**拼接**而不是替换 —— 全局最先拼、商品最后拼也最优先，所以列表按「生效顺序」分组
 * （全局 → 账号 → 商品），并把规则原文摆在页面上：不写清楚，用户很容易以为写商品级会「覆盖」全局。
 */

import { useEffect, useMemo, useState } from 'react'
import { ApiError, api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatTime, roleName } from '../format'
import {
  IconClock,
  IconEdit,
  IconInfo,
  IconPlus,
  IconPrompts,
  IconRefresh,
  IconSearch,
  IconTag,
  IconTrash,
} from '../icons'
import type { Account, AiPrompt, Item, ItemListResult, PromptListResult } from '../types'
import './prompts.css'

interface ScopeMeta {
  value: string
  label: string
  /** 拼接顺序：1 = 最先拼、优先级最低；3 = 最后拼、优先级最高。 */
  order: number
  desc: string
  /** 组头标签的配色 class（自带 pr- 前缀，不吃 styles.css）。 */
  chip: string
}

/** 数组顺序就是拼接顺序，页面上也按这个顺序渲染分组，避免「看着像覆盖」。 */
const SCOPES: ScopeMeta[] = [
  {
    value: 'global',
    label: '全局',
    order: 1,
    desc: '对所有账号、所有商品生效，最先拼接、优先级最低',
    chip: 'pr-scope-global',
  },
  {
    value: 'account',
    label: '账号',
    order: 2,
    desc: '只对某个账号下的全部会话生效，拼在全局之后',
    chip: 'pr-scope-account',
  },
  {
    value: 'item',
    label: '商品',
    order: 3,
    desc: '只对某个商品生效，最后拼接、优先级最高',
    chip: 'pr-scope-item',
  },
]

const SCOPE_LABEL: Record<string, string> = { global: '全局', account: '账号', item: '商品' }

/** 服务端没返回 roles 时的兜底（沿用原页面的顺序；服务端给了就以服务端为准）。 */
const ROLE_FALLBACK = ['master', 'item', 'cs', 'bargain', 'cross', 'vision']

interface PromptForm {
  id?: number
  scope: string
  /** scope=account 时是账号 id，scope=item 时是商品 item_id */
  ownerId: string
  role: string
  name: string
  content: string
  priority: string
  enabled: boolean
}

/** 行内错误：键与后端 invalid_fields 的字段名保持一致，方便日后接结构化错误。 */
interface FieldErrors {
  scope?: string
  account_id?: string
  item_id?: string
  role?: string
  content?: string
  priority?: string
}

const EMPTY_FORM: PromptForm = {
  scope: 'global',
  ownerId: '',
  role: '',
  name: '',
  content: '',
  priority: '0',
  enabled: true,
}

function isEnabled(prompt: AiPrompt): boolean {
  return prompt.enabled === undefined ? true : Boolean(prompt.enabled)
}

function formFromPrompt(prompt: AiPrompt): PromptForm {
  return {
    id: prompt.id,
    scope: prompt.scope || 'global',
    // 后端把 owner 放在 account_id 或 item_id 上，编辑时要放回对应的输入框
    ownerId: prompt.scope === 'item' ? prompt.item_id || '' : prompt.scope === 'account' ? prompt.account_id || '' : '',
    role: prompt.role || '',
    name: prompt.name || '',
    content: prompt.content || '',
    priority: String(prompt.priority ?? 0),
    enabled: isEnabled(prompt),
  }
}

function sameForm(a: PromptForm, b: PromptForm): boolean {
  return (
    a.scope === b.scope &&
    a.ownerId === b.ownerId &&
    a.role === b.role &&
    a.name === b.name &&
    a.content === b.content &&
    a.priority === b.priority &&
    a.enabled === b.enabled
  )
}

/**
 * 本地校验。后端 `PromptValidationError` 会报 scope / account_id / item_id / role，
 * 这里把能在本地拦的一律拦下来：能不进一次 400，就不进。
 */
function validateForm(draft: PromptForm): FieldErrors {
  const errors: FieldErrors = {}
  if (draft.scope === 'account' && !draft.ownerId) errors.account_id = '请选择这条提示词作用到哪个账号'
  if (draft.scope === 'item' && !draft.ownerId) errors.item_id = '请选择这条提示词作用到哪个商品'
  if (!draft.content.trim()) errors.content = '提示词内容不能为空'
  const priority = draft.priority.trim()
  if (!priority) errors.priority = '优先级不能为空，可以是 0'
  else if (!/^[+-]?\d+$/.test(priority)) errors.priority = '优先级必须是整数（后端按 int 解析）'
  return errors
}

/**
 * 把后端 invalid_fields 的 message 拆回字段。
 *
 * 为什么只能靠文案：`api.ts` 的 ApiError 只保留 message/code/status，结构化 `invalid_fields`
 * 在 handleResponse 里被丢掉了（见交付报告里的接口缺口，不改 api.ts）。好在这几个字段的理由
 * 是后端自己写死的、字段名就在文案里（`必须提供 item_id` / `必须提供 account_id`），
 * 于是按字段名就近归位；拆不出来的部分照旧在表单顶部显示完整原文。
 */
function splitServerFields(message: string): FieldErrors {
  const errors: FieldErrors = {}
  for (const raw of message.split(/[；;]/)) {
    const text = raw.trim()
    if (!text) continue
    if (text.includes('account_id')) errors.account_id = text
    else if (text.includes('item_id')) errors.item_id = text
    else if (text.includes('scope')) errors.scope = text
    else if (text.includes('role') || text.includes('角色')) errors.role = text
  }
  return errors
}

export function PromptsPage() {
  const toast = useToast()
  // 后端返回 {items, scopes, roles}，不是裸数组
  const prompts = useAsync<PromptListResult>(() => api.get<PromptListResult>('/api/agents/prompts'), [], true, [
    'prompts.changed',
  ])
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const items = useAsync<ItemListResult>(() => api.get<ItemListResult>('/api/items', { limit: 300 }), [], true, [
    'items.updated',
  ])

  const [form, setForm] = useState<PromptForm | null>(null)
  /** 每次换编辑对象都 +1，用 key 强制弹窗重建，避免草稿串台。 */
  const [editorKey, setEditorKey] = useState(0)
  /** 编辑器是否还有未保存改动（由弹窗回调，用于「切换编辑对象」前二次确认）。 */
  const [dirty, setDirty] = useState(false)
  const [pendingForm, setPendingForm] = useState<PromptForm | null>(null)
  const [removing, setRemoving] = useState<AiPrompt | null>(null)
  const [busy, setBusy] = useState(false)
  const [togglingId, setTogglingId] = useState<number | null>(null)
  const [scopeFilter, setScopeFilter] = useState('')
  const [keyword, setKeyword] = useState('')
  const [onlyEnabled, setOnlyEnabled] = useState(false)

  const list = prompts.data?.items ?? []
  const accountList = accounts.data ?? []
  const itemList = items.data?.items ?? []

  /** 角色下拉以服务端 roles 为准，读不到才退回本地常量。 */
  const roleOptions = useMemo(() => {
    const fromServer = prompts.data?.roles ?? []
    return fromServer.length > 0 ? fromServer : ROLE_FALLBACK
  }, [prompts.data])

  const term = keyword.trim().toLowerCase()

  const visible = useMemo(
    () =>
      list.filter((prompt) => {
        if (scopeFilter && prompt.scope !== scopeFilter) return false
        if (onlyEnabled && !isEnabled(prompt)) return false
        if (!term) return true
        return [prompt.name, prompt.content, prompt.role, prompt.item_id, prompt.account_id].some((value) =>
          String(value ?? '').toLowerCase().includes(term),
        )
      }),
    [list, scopeFilter, onlyEnabled, term],
  )

  const counts = useMemo(() => {
    const map: Record<string, number> = { all: list.length }
    for (const scope of SCOPES) map[scope.value] = list.filter((prompt) => prompt.scope === scope.value).length
    return map
  }, [list])

  /** 按生效顺序分组，组内 priority 大的在前（与后端挑选用同一条排序规则）。 */
  const grouped = useMemo(
    () =>
      SCOPES.map((meta) => ({
        meta,
        items: visible
          .filter((prompt) => prompt.scope === meta.value)
          .sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0)),
      })),
    [visible],
  )

  /** 「继续编辑其它提示词」下拉的数据源：排除当前这条。 */
  const siblings = useMemo(() => {
    const currentId = form?.id
    return list
      .filter((prompt) => prompt.id !== currentId)
      .map((prompt) => ({
        id: prompt.id,
        label: `${SCOPE_LABEL[prompt.scope || 'global']}｜${prompt.name || '（未命名提示词）'}`,
      }))
  }, [list, form])

  /** 有未保存改动时先确认，再换编辑对象 —— 不能把草稿悄悄丢掉。 */
  const openEditor = (next: PromptForm) => {
    if (form && dirty) {
      setPendingForm(next)
      return
    }
    setForm(next)
    setEditorKey((n) => n + 1)
    setDirty(false)
  }

  const applyPending = () => {
    if (!pendingForm) return
    setForm(pendingForm)
    setEditorKey((n) => n + 1)
    setDirty(false)
    setPendingForm(null)
  }

  const closeEditor = () => {
    setDirty(false)
    setForm(null)
  }

  const reloadAll = () => {
    prompts.reload()
    accounts.reload()
    items.reload()
  }

  const doRemove = async () => {
    if (!removing?.id) return
    setBusy(true)
    try {
      await api.del(`/api/agents/prompts/${removing.id}`)
      toast.success('已删除')
      setRemoving(null)
      prompts.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const toggleEnabled = async (prompt: AiPrompt, enabled: boolean) => {
    if (!prompt.id) return
    setTogglingId(prompt.id)
    try {
      // 后端收真 bool（prompts.py 里 1 if enabled else 0）
      await api.put(`/api/agents/prompts/${prompt.id}`, { enabled })
      prompts.reload()
      toast.success(enabled ? '已启用' : '已停用')
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setTogglingId(null)
    }
  }

  /** 提示词作用对象的显示名：后端把 owner 放在 account_id 或 item_id 上。 */
  const scopeName = (prompt: AiPrompt): string => {
    if (prompt.scope === 'global') return '全部账号与商品'
    if (prompt.scope === 'account') {
      const id = prompt.account_id || ''
      const account = accountList.find((a) => a.id === id)
      if (!account) return id || '（未指定账号）'
      return account.nickname ? `${account.nickname}（${id}）` : id
    }
    const id = prompt.item_id || ''
    const item = itemList.find((i) => i.item_id === id)
    if (!item) return id || '（未指定商品）'
    return item.title ? `${item.title}（${id}）` : id
  }

  const filtered = Boolean(scopeFilter || term || onlyEnabled)

  return (
    <div className="pr-page">
      <div className="pr-head">
        <div className="pr-head-main">
          <div className="pr-title">
            <IconPrompts size={16} />
            提示词
          </div>
          <div className="pr-sub">三级提示词按生效顺序排列：全局 → 账号 → 商品；越具体越靠后，也越优先。</div>
        </div>
        <div className="pr-head-actions">
          <button className="btn" onClick={reloadAll} disabled={prompts.loading}>
            <IconRefresh size={14} /> 刷新
          </button>
          <button className="btn primary" onClick={() => openEditor({ ...EMPTY_FORM })}>
            <IconPlus size={14} /> 新建提示词
          </button>
        </div>
      </div>

      <div className="pr-note">
        <span className="pr-note-icon">
          <IconInfo size={15} />
        </span>
        <div className="pr-note-text">
          三级提示词是<strong>拼接</strong>，不是替换：<strong>全局 → 账号 → 商品</strong> 依次追加到角色基础提示词后面，
          越具体的越靠后、优先级越高；同一层级里先看 role 是否精确命中当前 AI 角色，命中者优先，
          再比 <strong>priority（数字大的生效）</strong>。
        </div>
      </div>

      <div className="pr-toolbar">
        <div className="pr-seg">
          <button
            type="button"
            className={scopeFilter === '' ? 'pr-seg-btn active' : 'pr-seg-btn'}
            onClick={() => setScopeFilter('')}
          >
            全部 <span className="pr-seg-count">{counts.all}</span>
          </button>
          {SCOPES.map((scope) => (
            <button
              type="button"
              key={scope.value}
              className={scopeFilter === scope.value ? 'pr-seg-btn active' : 'pr-seg-btn'}
              onClick={() => setScopeFilter(scope.value)}
            >
              {scope.order}. {scope.label} <span className="pr-seg-count">{counts[scope.value]}</span>
            </button>
          ))}
        </div>
        <label className="pr-search">
          <IconSearch size={14} />
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜标题 / 内容 / 角色"
          />
        </label>
        <span className="pr-switch-line">
          <Switch checked={onlyEnabled} onChange={setOnlyEnabled} label="只看启用" />
        </span>
        <span className="grow" />
        <span className="dim">
          显示 {visible.length} / {list.length} 条
        </span>
      </div>

      <AsyncBox
        loading={prompts.loading}
        error={prompts.error}
        notReady={prompts.notReady}
        onRetry={prompts.reload}
      >
        {list.length === 0 ? (
          <div className="pr-empty">
            <span className="pr-empty-icon">
              <IconPrompts size={22} />
            </span>
            <div className="pr-empty-title">还没有提示词</div>
            <div className="pr-empty-desc">新建一条全局提示词，告诉 AI 你是谁、怎么说话、哪些话不能说。</div>
            <div className="pr-empty-actions">
              <button className="btn primary" onClick={() => openEditor({ ...EMPTY_FORM })}>
                <IconPlus size={14} /> 新建提示词
              </button>
            </div>
          </div>
        ) : visible.length === 0 ? (
          <div className="pr-empty">
            <span className="pr-empty-icon">
              <IconSearch size={22} />
            </span>
            <div className="pr-empty-title">没有匹配的提示词</div>
            <div className="pr-empty-desc">换个关键词，或把级别筛选调回「全部」。</div>
            <div className="pr-empty-actions">
              <button
                className="btn"
                onClick={() => {
                  setScopeFilter('')
                  setKeyword('')
                  setOnlyEnabled(false)
                }}
              >
                清除筛选条件
              </button>
            </div>
          </div>
        ) : (
          <div className="pr-groups">
            {grouped.map((group) => (
              <section className="pr-card" key={group.meta.value}>
                <div className="pr-card-head">
                  <div className="pr-card-title">
                    <span className={`pr-scope-chip ${group.meta.chip}`}>
                      <span className="pr-scope-order">{group.meta.order}</span>
                      {group.meta.label}级
                    </span>
                    <span className="pr-scope-desc">{group.meta.desc}</span>
                  </div>
                  <div className="pr-card-head-side">
                    <span className="pr-count">{group.items.length} 条</span>
                    <button
                      className="btn sm ghost"
                      onClick={() => openEditor({ ...EMPTY_FORM, scope: group.meta.value })}
                    >
                      <IconPlus size={13} /> 添加
                    </button>
                  </div>
                </div>
                <div className={group.items.length > 0 ? 'pr-card-body pr-list' : 'pr-card-body'}>
                  {group.items.length === 0 ? (
                    <div className="pr-group-empty">
                      <IconInfo size={14} />
                      <span>
                        该级别还没有提示词{filtered ? '（也可能被上面的筛选条件挡住了）' : ''}
                      </span>
                    </div>
                  ) : (
                    group.items.map((prompt) => (
                      <article className={isEnabled(prompt) ? 'pr-item' : 'pr-item pr-item-off'} key={prompt.id}>
                        <div className="pr-item-head">
                          <div className="pr-item-name">
                            <span className="pr-item-title">{prompt.name || '（未命名提示词）'}</span>
                            <Badge tone="muted">{prompt.role ? roleName(prompt.role) : '所有角色'}</Badge>
                            <Badge tone={Number(prompt.priority) > 0 ? 'ok' : 'muted'}>
                              priority {Number(prompt.priority) || 0}
                            </Badge>
                            {!isEnabled(prompt) && <Badge tone="warn">已停用</Badge>}
                          </div>
                          <div className="pr-item-actions">
                            <Switch
                              checked={isEnabled(prompt)}
                              disabled={togglingId === prompt.id}
                              onChange={(next) => toggleEnabled(prompt, next)}
                              label={isEnabled(prompt) ? '已启用' : '已停用'}
                            />
                            <button className="btn sm ghost" onClick={() => openEditor(formFromPrompt(prompt))}>
                              <IconEdit size={13} /> 编辑
                            </button>
                            <button className="btn sm danger" onClick={() => setRemoving(prompt)}>
                              <IconTrash size={13} /> 删除
                            </button>
                          </div>
                        </div>
                        <div className="pr-item-meta">
                          <span className="pr-meta">
                            <IconTag size={12} /> 作用于 {scopeName(prompt)}
                          </span>
                          <span className="pr-meta">
                            <IconClock size={12} /> 更新于 {formatTime(prompt.updated_at)}
                          </span>
                          <span className="pr-meta">{prompt.content ? `${prompt.content.length} 字` : '空内容'}</span>
                        </div>
                        <div className="pr-item-content">{prompt.content || '（空内容）'}</div>
                      </article>
                    ))
                  )}
                </div>
              </section>
            ))}
          </div>
        )}
      </AsyncBox>

      {form && (
        <PromptModal
          key={editorKey}
          form={form}
          accounts={accountList}
          items={itemList}
          roles={roleOptions}
          siblings={siblings}
          onDirtyChange={setDirty}
          onSwitchTo={(promptId) => {
            const target = list.find((prompt) => prompt.id === promptId)
            if (target) openEditor(formFromPrompt(target))
          }}
          onClose={closeEditor}
          onSaved={() => {
            closeEditor()
            prompts.reload()
          }}
        />
      )}

      {/* 有未保存改动时点了另一条的「编辑」：先确认再换人 */}
      {pendingForm && (
        <ConfirmDialog
          title="放弃未保存的修改？"
          danger
          confirmText="放弃并切换"
          message={<>当前这条提示词还有未保存的改动，切换到另一条之后这些改动会丢失。</>}
          onCancel={() => setPendingForm(null)}
          onConfirm={applyPending}
        />
      )}

      {removing && (
        <ConfirmDialog
          title="删除提示词"
          danger
          busy={busy}
          confirmText="确认删除"
          message={
            <>
              确定删除 <strong>{removing.name || '(未命名)'}</strong> 吗？删除后 AI 不再使用这条规则。
            </>
          }
          onCancel={() => setRemoving(null)}
          onConfirm={doRemove}
        />
      )}
    </div>
  )
}

/* ---------------- 提示词编辑器 ---------------- */

function PromptModal({
  form,
  accounts,
  items,
  roles,
  siblings,
  onClose,
  onSaved,
  onSwitchTo,
  onDirtyChange,
}: {
  form: PromptForm
  accounts: Account[]
  items: Item[]
  roles: string[]
  siblings: Array<{ id: number; label: string }>
  onClose: () => void
  onSaved: () => void
  onSwitchTo: (promptId: number) => void
  onDirtyChange: (dirty: boolean) => void
}) {
  const toast = useToast()
  const [draft, setDraft] = useState<PromptForm>(form)
  const [errors, setErrors] = useState<FieldErrors>({})
  /** 后端失败原文：结构化 invalid_fields 拿不到（见 splitServerFields 注释），原文必须让用户看到。 */
  const [serverError, setServerError] = useState('')
  const [saving, setSaving] = useState(false)
  const [askDiscard, setAskDiscard] = useState(false)
  const isEdit = form.id !== undefined
  const dirty = !sameForm(draft, form)

  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])

  // 卸载时清掉脏标记，否则父级会一直以为编辑器还开着
  useEffect(() => () => onDirtyChange(false), [onDirtyChange])

  // 浏览器级兜底：刷新 / 关标签页 / 外链跳转时弹原生确认（不动 App.tsx 做路由拦截）
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  /** 改哪个字段就清哪个字段的行内错误：错误要跟着输入消失，不能留在屏幕上误导。 */
  const patch = (next: Partial<PromptForm>, clear?: Array<keyof FieldErrors>) => {
    setDraft((prev) => ({ ...prev, ...next }))
    if (clear && clear.length > 0) {
      setErrors((prev) => {
        const kept = { ...prev }
        for (const key of clear) delete kept[key]
        return kept
      })
    }
  }

  const requestClose = () => {
    if (saving) return
    if (dirty) {
      setAskDiscard(true)
      return
    }
    onClose()
  }

  const save = async () => {
    const found = validateForm(draft)
    setErrors(found)
    if (Object.keys(found).length > 0) {
      setServerError('')
      return
    }
    setSaving(true)
    setServerError('')
    try {
      // 后端 create/update 从 `account_id` / `item_id` 取 owner（agents.py:106/134），
      // 标题字段名是 `name`，不是 `title`。
      const payload: Record<string, unknown> = {
        scope: draft.scope,
        role: draft.role || '',
        name: draft.name.trim() || '未命名提示词',
        content: draft.content,
        priority: Number(draft.priority.trim()),
        enabled: draft.enabled,
      }
      if (draft.scope === 'account') payload.account_id = draft.ownerId
      else if (draft.scope === 'item') payload.item_id = draft.ownerId

      if (isEdit) await api.put(`/api/agents/prompts/${form.id}`, payload)
      else await api.post('/api/agents/prompts', payload)
      toast.success(isEdit ? '已保存' : '已创建')
      onSaved()
    } catch (err) {
      // 后端原文放表单顶部；弹窗正文可能已经滚下去，再补一句短提示（不重复原文）
      toast.error('保存失败，请看表单顶部的提示')
      setServerError(errorText(err))
      if (err instanceof ApiError && err.code === 'invalid_fields') setErrors(splitServerFields(err.message))
    } finally {
      setSaving(false)
    }
  }

  // 二次确认弹窗放在 Modal 外面（同级），避免被 .modal-mask 的 backdrop-filter
  // 变成 fixed 定位的包含块后裁剪在弹窗内部。
  return (
    <>
      <Modal
        title={isEdit ? '编辑提示词' : '新建提示词'}
        onClose={requestClose}
        wide
      footer={
        <>
          <button className="btn ghost" onClick={requestClose} disabled={saving}>
            取消
          </button>
          <button className="btn primary" onClick={save} disabled={saving}>
            {saving ? '保存中…' : isEdit ? '保存修改' : '创建提示词'}
          </button>
        </>
      }
    >
      {serverError && (
        <div className="pr-servererr">
          <Notice kind="error" onClose={() => setServerError('')}>
            {serverError}
          </Notice>
        </div>
      )}

      <div className="pr-form-grid">
        <div className="pr-form-left">
          <div className="pr-field">
            <span className="pr-label">
              作用级别 <span className="pr-req">*</span>
            </span>
            <select
              value={draft.scope}
              onChange={(e) => patch({ scope: e.target.value, ownerId: '' }, ['scope', 'account_id', 'item_id'])}
            >
              {SCOPES.map((scope) => (
                <option key={scope.value} value={scope.value}>
                  {scope.order}. {scope.label}级 —— {scope.desc}
                </option>
              ))}
            </select>
            <span className="pr-tip">级别决定拼接顺序：全局最先拼，商品最后拼也最优先。</span>
            {errors.scope && <div className="pr-err">{errors.scope}</div>}
          </div>

          {draft.scope === 'account' && (
            <div className="pr-field">
              <span className="pr-label">
                作用账号 <span className="pr-req">*</span>
              </span>
              <select value={draft.ownerId} onChange={(e) => patch({ ownerId: e.target.value }, ['account_id'])}>
                <option value="">请选择账号</option>
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.nickname || account.id}
                  </option>
                ))}
              </select>
              {accounts.length === 0 && <span className="pr-tip">还没读到账号，先去「账号」页扫码登录。</span>}
              {errors.account_id && <div className="pr-err">{errors.account_id}</div>}
            </div>
          )}

          {draft.scope === 'item' && (
            <div className="pr-field">
              <span className="pr-label">
                作用商品 <span className="pr-req">*</span>
              </span>
              <select value={draft.ownerId} onChange={(e) => patch({ ownerId: e.target.value }, ['item_id'])}>
                <option value="">请选择商品</option>
                {items.map((item) => (
                  <option key={`${item.account_id}-${item.item_id}`} value={item.item_id}>
                    {item.title || item.item_id}
                  </option>
                ))}
              </select>
              {items.length === 0 && <span className="pr-tip">还没读到商品，先去「商品」页同步一次。</span>}
              {errors.item_id && <div className="pr-err">{errors.item_id}</div>}
            </div>
          )}

          <div className="pr-field">
            <span className="pr-label">限定角色</span>
            <select value={draft.role} onChange={(e) => patch({ role: e.target.value }, ['role'])}>
              <option value="">所有角色（通用）</option>
              {roles.map((role) => (
                <option key={role} value={role}>
                  {roleName(role)}
                </option>
              ))}
            </select>
            <span className="pr-tip">留空 = 对所有 AI 角色生效；同一级别里写了具体角色的那条更先被选中。</span>
            {errors.role && <div className="pr-err">{errors.role}</div>}
          </div>

          <div className="pr-field">
            <span className="pr-label">标题</span>
            <input
              type="text"
              value={draft.name}
              onChange={(e) => patch({ name: e.target.value })}
              placeholder="例如：议价底线"
            />
            <span className="pr-tip">同一级别、同一对象、同一角色下，同名 = 同一条（再保存是更新，不会新增）。</span>
          </div>

          <div className="pr-field">
            <span className="pr-label">
              优先级 <span className="pr-req">*</span>
            </span>
            <input
              className="pr-input-sm"
              type="number"
              step={1}
              value={draft.priority}
              onChange={(e) => patch({ priority: e.target.value }, ['priority'])}
            />
            <span className="pr-tip">同一级别里数字大的先生效，可以是负数或 0。</span>
            {errors.priority && <div className="pr-err">{errors.priority}</div>}
          </div>

          <div className="pr-field">
            <span className="pr-label">状态</span>
            <Switch
              checked={draft.enabled}
              onChange={(next) => patch({ enabled: next })}
              label={draft.enabled ? '已启用' : '已停用'}
            />
            <span className="pr-tip">停用后这条提示词仍留在库里，只是不再拼进发给模型的提示词。</span>
          </div>

          {siblings.length > 0 && (
            <div className="pr-switchto">
              <span className="pr-label">继续编辑其它提示词</span>
              <select
                value=""
                onChange={(e) => {
                  const id = Number(e.target.value)
                  if (id) onSwitchTo(id)
                }}
              >
                <option value="">选择一条…</option>
                {siblings.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.label}
                  </option>
                ))}
              </select>
              <span className="pr-tip">有未保存的改动时会先确认，再切换。</span>
            </div>
          )}
        </div>

        <div className="pr-form-right">
          <div className="pr-field">
            <span className="pr-label">
              提示词内容 <span className="pr-req">*</span>
            </span>
            <textarea
              className="pr-textarea"
              rows={10}
              value={draft.content}
              onChange={(e) => patch({ content: e.target.value }, ['content'])}
              placeholder={'例如：\n你是专业、热情的闲鱼卖家。\n绝不承诺包邮以外的运费优惠。\n低于 50 元不议价。'}
            />
            <div className="pr-charcount">
              <span>
                已输入 {draft.content.length} 字
                {draft.content ? ` · ${draft.content.split('\n').length} 行` : ''}
              </span>
            </div>
            {errors.content && <div className="pr-err">{errors.content}</div>}
            <span className="pr-tip">
              后端对提示词长度没有上限，所以这里不设字数限制；但这段文字会拼进系统提示词，越长越占模型的上下文预算。
            </span>
          </div>
        </div>
      </div>

      </Modal>

      {askDiscard && (
        <ConfirmDialog
          title="放弃未保存的修改？"
          danger
          confirmText="放弃修改"
          message={<>这条提示词还有未保存的改动，关闭后改动会丢失。</>}
          onCancel={() => setAskDiscard(false)}
          onConfirm={() => {
            setAskDiscard(false)
            onClose()
          }}
        />
      )}
    </>
  )
}
