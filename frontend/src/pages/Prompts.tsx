/** 提示词：global / account / item 三级提示词编辑（priority 越大越优先）。 */

import { useMemo, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatTime, roleName } from '../format'
import type { Account, AiPrompt, Item, ItemListResult, PromptListResult } from '../types'

const SCOPES = [
  { value: 'global', label: '全局', tone: 'ok' as const, desc: '对所有账号、所有商品生效，优先级最低' },
  { value: 'account', label: '账号', tone: 'warn' as const, desc: '只对某个账号下的所有会话生效' },
  { value: 'item', label: '商品', tone: 'danger' as const, desc: '只对某个商品生效，优先级最高' },
]

const ROLE_OPTIONS = ['', 'master', 'item', 'cs', 'bargain', 'cross', 'vision']

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

const EMPTY: PromptForm = {
  scope: 'global',
  ownerId: '',
  role: '',
  name: '',
  content: '',
  priority: '0',
  enabled: true,
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
  const [removing, setRemoving] = useState<AiPrompt | null>(null)
  const [scopeFilter, setScopeFilter] = useState('')
  const [busy, setBusy] = useState(false)

  const list = prompts.data?.items ?? []
  const accountList = accounts.data ?? []
  const itemList = items.data?.items ?? []

  const grouped = useMemo(() => {
    const filtered = scopeFilter ? list.filter((p) => p.scope === scopeFilter) : list
    return SCOPES.map((scope) => ({
      scope: scope.value,
      label: scope.label,
      tone: scope.tone,
      desc: scope.desc,
      items: filtered
        .filter((p) => p.scope === scope.value)
        .sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0)),
    }))
  }, [list, scopeFilter])

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
    try {
      // 后端收真 bool（prompts.py 里 1 if enabled else 0）
      await api.put(`/api/agents/prompts/${prompt.id}`, { enabled })
      prompts.reload()
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  /** 提示词作用对象的显示名：后端把 owner 放在 account_id 或 item_id 上。 */
  const scopeName = (prompt: AiPrompt): string => {
    if (prompt.scope === 'global') return '全部'
    if (prompt.scope === 'account') {
      const account = accountList.find((a) => a.id === prompt.account_id)
      return account ? account.nickname || account.id : prompt.account_id || '（未指定）'
    }
    const item = itemList.find((i) => i.item_id === prompt.item_id)
    return item ? item.title || item.item_id : prompt.item_id || '（未指定）'
  }

  return (
    <div>
      <Notice kind="info">
        <strong>三级是拼接，不是替换</strong>：商品 &gt; 账号 &gt; 全局 三级各挑出命中该角色的一条，依次接到「AI
        配置」里那个角色的系统提示词后面，一起发给模型。同一级里如果有好几条：先看 <strong>角色是否精确匹配</strong>
        （写了具体角色的优先，通用那条靠后），角色同样精确时再比 <strong>priority（越大越优先）</strong>，都相同则取最新的一条。
        同一级下名称不同的多条可以共存，名称相同则是同一条（重复保存会更新它）。
      </Notice>

      <div className="toolbar">
        <select value={scopeFilter} onChange={(e) => setScopeFilter(e.target.value)} style={{ width: 160 }}>
          <option value="">全部级别</option>
          {SCOPES.map((scope) => (
            <option key={scope.value} value={scope.value}>
              {scope.label}级
            </option>
          ))}
        </select>
        <button className="btn primary" onClick={() => setForm({ ...EMPTY })}>
          ＋ 新建提示词
        </button>
        <span className="spacer" />
        <span className="dim" style={{ fontSize: 12 }}>共 {list.length} 条</span>
        <button className="btn" onClick={prompts.reload}>
          刷新
        </button>
      </div>

      <AsyncBox
        loading={prompts.loading}
        error={prompts.error}
        notReady={prompts.notReady}
        empty={list.length === 0}
        emptyIcon="📝"
        emptyTitle="还没有提示词"
        emptyDesc="新建一条全局提示词，告诉 AI 你是谁、怎么说话、哪些话不能说。"
        onRetry={prompts.reload}
      >
        {grouped.map((group) => (
          <div className="card" key={group.scope}>
            <h3>
              <Badge tone={group.tone}>{group.label}级</Badge>
              <span className="hint">{group.desc}</span>
              <span style={{ flex: 1 }} />
              <span className="dim" style={{ fontSize: 12 }}>{group.items.length} 条</span>
            </h3>
            {group.items.length === 0 ? (
              <div className="dim" style={{ fontSize: 12.5, padding: '6px 0' }}>
                暂无{group.label}级提示词
              </div>
            ) : (
              <div className="col">
                {group.items.map((prompt) => (
                  <div
                    key={prompt.id ?? `${prompt.scope}-${prompt.account_id}-${prompt.item_id}-${prompt.name}`}
                    style={{ border: '1px solid var(--border-soft)', borderRadius: 'var(--radius-sm)', padding: 12 }}
                  >
                    <div className="row wrap">
                      <strong>{prompt.name || '(未命名提示词)'}</strong>
                      <Badge tone="muted">priority {Number(prompt.priority) || 0}</Badge>
                      {prompt.role && <Badge tone="muted">{roleName(prompt.role)}</Badge>}
                      <span className="dim" style={{ fontSize: 11.5 }}>作用于：{scopeName(prompt)}</span>
                      <span className="spacer" />
                      <Switch checked={prompt.enabled === undefined ? true : Boolean(prompt.enabled)} onChange={(next) => toggleEnabled(prompt, next)} />
                      <button
                        className="btn sm ghost"
                        onClick={() =>
                          setForm({
                            id: prompt.id,
                            scope: prompt.scope || 'global',
                            ownerId: prompt.scope === 'item' ? prompt.item_id || '' : prompt.account_id || '',
                            role: prompt.role || '',
                            name: prompt.name || '',
                            content: prompt.content || '',
                            priority: String(prompt.priority ?? 0),
                            enabled: prompt.enabled === undefined ? true : Boolean(prompt.enabled),
                          })
                        }
                      >
                        编辑
                      </button>
                      <button className="btn sm danger" onClick={() => setRemoving(prompt)}>
                        删除
                      </button>
                    </div>
                    <div className="prompt-preview">{prompt.content || '（空内容）'}</div>
                    <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
                      更新于 {formatTime(prompt.updated_at)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </AsyncBox>

      {form && (
        <PromptModal
          form={form}
          accounts={accountList}
          items={itemList}
          onClose={() => setForm(null)}
          onSaved={() => {
            setForm(null)
            prompts.reload()
          }}
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

/* ---------------- 提示词表单 ---------------- */

function PromptModal({
  form,
  accounts,
  items,
  onClose,
  onSaved,
}: {
  form: PromptForm
  accounts: Account[]
  items: Item[]
  onClose: () => void
  onSaved: () => void
}) {
  const toast = useToast()
  const [draft, setDraft] = useState<PromptForm>(form)
  const [saving, setSaving] = useState(false)
  const isEdit = form.id !== undefined

  const save = async () => {
    if (!draft.content.trim()) {
      toast.info('提示词内容不能为空')
      return
    }
    if (draft.scope !== 'global' && !draft.ownerId) {
      toast.info(draft.scope === 'account' ? '请选择要作用到哪个账号' : '请选择要作用到哪个商品')
      return
    }
    setSaving(true)
    try {
      // 后端 create/update 从 `account_id` / `item_id` 取 owner（agents.py:93/115），
      // 标题字段名是 `name`，不是 `title`。
      const payload: Record<string, unknown> = {
        scope: draft.scope,
        role: draft.role || '',
        name: draft.name.trim() || '未命名提示词',
        content: draft.content,
        priority: Number(draft.priority) || 0,
        enabled: draft.enabled,
      }
      if (draft.scope === 'account') payload.account_id = draft.ownerId
      else if (draft.scope === 'item') payload.item_id = draft.ownerId

      if (isEdit) await api.put(`/api/agents/prompts/${form.id}`, payload)
      else await api.post('/api/agents/prompts', payload)
      toast.success(isEdit ? '已保存' : '已创建')
      onSaved()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={isEdit ? '编辑提示词' : '新建提示词'}
      onClose={onClose}
      wide
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
      <div className="split">
        <div className="left">
          <Field label="作用级别 *">
            <select value={draft.scope} onChange={(e) => setDraft({ ...draft, scope: e.target.value, ownerId: '' })}>
              {SCOPES.map((scope) => (
                <option key={scope.value} value={scope.value}>
                  {scope.label}级 —— {scope.desc}
                </option>
              ))}
            </select>
          </Field>

          {draft.scope === 'account' && (
            <Field label="账号 *">
              <select value={draft.ownerId} onChange={(e) => setDraft({ ...draft, ownerId: e.target.value })}>
                <option value="">请选择账号</option>
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.nickname || account.id}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {draft.scope === 'item' && (
            <Field label="商品 *">
              <select value={draft.ownerId} onChange={(e) => setDraft({ ...draft, ownerId: e.target.value })}>
                <option value="">请选择商品</option>
                {items.map((item) => (
                  <option key={`${item.account_id}-${item.item_id}`} value={item.item_id}>
                    {item.title || item.item_id}
                  </option>
                ))}
              </select>
            </Field>
          )}

          <Field label="限定角色" tip="留空表示对所有 AI 角色生效">
            <select value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
              {ROLE_OPTIONS.map((role) => (
                <option key={role || 'any'} value={role}>
                  {role ? roleName(role) : '所有角色'}
                </option>
              ))}
            </select>
          </Field>

          <Field label="标题">
            <input
              type="text"
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              placeholder="例如：议价底线"
            />
          </Field>

          <Field label="优先级" tip="同一级别内数字越大越优先，可以为负">
            <input
              type="number"
              value={draft.priority}
              onChange={(e) => setDraft({ ...draft, priority: e.target.value })}
              style={{ width: 120 }}
            />
          </Field>

          <Switch checked={draft.enabled} onChange={(next) => setDraft({ ...draft, enabled: next })} label="启用" />
        </div>

        <div className="right">
          <Field label="提示词内容 *">
            <textarea
              value={draft.content}
              onChange={(e) => setDraft({ ...draft, content: e.target.value })}
              rows={16}
              placeholder={'例如：\n你是专业、热情的闲鱼卖家。\n绝不承诺包邮以外的运费优惠。\n低于 50 元不议价。'}
            />
          </Field>
          <div className="dim" style={{ fontSize: 11.5 }}>
            共 {draft.content.length} 字
          </div>
        </div>
      </div>
    </Modal>
  )
}
