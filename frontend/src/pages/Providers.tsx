/**
 * AI 配置：统一 API Key 管理（提供者卡片）+ 角色 → 提供者/模型映射。
 *
 * 安全铁律：后端从不下发明文 Key（只给 `api_key_masked` / `has_key`），
 * 所以这里既不回填、也不把 Key 写进任何 DOM 属性；编辑表单的 Key 输入框永远是空的。
 *
 * 提示词分两层，别搞混：
 *  - **角色基础提示词**（`ai_roles.system_prompt`）在每张角色卡里展开改；
 *  - **三级提示词**（`ai_prompts` 的 global / account / item）归「提示词」页，这里只给跳转入口。
 * 两者在运行时是**拼接**关系（基础提示词在前，三级依次追加）。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Field, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatTime, roleName, truncate } from '../format'
import {
  IconAgents,
  IconCheck,
  IconCheckCircle,
  IconClose,
  IconEdit,
  IconKey,
  IconLink,
  IconPlus,
  IconRefresh,
  IconTrash,
  IconWifi,
  IconXCircle,
} from '../icons'
import type { AiRole, Provider, ProviderTest } from '../types'
import './providers.css'

const ROLES = ['master', 'item', 'cs', 'bargain', 'cross', 'vision']

interface ProviderForm {
  id?: number
  name: string
  base_url: string
  api_key: string
  default_model: string
  enabled: boolean
}

const EMPTY_FORM: ProviderForm = {
  name: '',
  base_url: 'https://api.deepseek.com/v1',
  api_key: '',
  default_model: '',
  enabled: true,
}

interface ProviderErrors {
  name?: string
  base_url?: string
  api_key?: string
}

/**
 * 就近校验：名称必填；base_url 必填且必须是 http(s) 地址；新建时 Key 必填。
 * 编辑时 Key 留空是**合法**的（表示不修改），所以只在新建时要求。
 */
function validateProviderForm(draft: ProviderForm, isEdit: boolean): ProviderErrors {
  const errors: ProviderErrors = {}
  if (!draft.name.trim()) errors.name = '请填写名称'
  const url = draft.base_url.trim()
  if (!url) errors.base_url = '请填写接口地址'
  else if (!/^https?:\/\//i.test(url)) errors.base_url = '接口地址必须以 http:// 或 https:// 开头'
  if (!isEdit && !draft.api_key.trim()) errors.api_key = '新建时必须填写 API Key'
  return errors
}

/**
 * 后端 `crypto.mask()` 解密失败时返回字面量 `<无法解密>` —— 那不是掩码，
 * 当密钥状态显示会误导用户，所以只认真正含 `*` 的掩码。
 */
function keyMasked(provider: Provider): string {
  const masked = provider.api_key_masked || ''
  return masked.includes('*') ? masked : ''
}

/** 后端 `/test` 还会回 `usage`（`types.ts` 的 `ProviderTest` 没声明），本地补上以便展示 token 用量。 */
interface TestUsage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

type TestOutcome = ProviderTest & { usage?: TestUsage }

export function ProvidersPage() {
  const toast = useToast()
  const providers = useAsync<Provider[]>(() => api.get<Provider[]>('/api/providers'), [], true, ['providers.changed'])
  const roles = useAsync<AiRole[]>(() => api.get<AiRole[]>('/api/agents/roles'), [], true, ['providers.changed'])

  const [tab, setTab] = useState<'providers' | 'roles'>('providers')
  const [form, setForm] = useState<ProviderForm | null>(null)
  const [removing, setRemoving] = useState<Provider | null>(null)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [testResult, setTestResult] = useState<Record<number, TestOutcome>>({})
  const [busy, setBusy] = useState(false)

  const list = providers.data ?? []
  const roleList = roles.data ?? []

  const defaultProvider = useMemo(() => list.find((p) => bool(p.is_default)), [list])

  // 编辑弹窗要显示「已配置 / 未配置 + 掩码」，但**不能**把这些值放进表单 state ——
  // 表单一旦持有 Key，就总有被回填进 DOM 的风险。
  const editingProvider = useMemo(
    () => (form?.id === undefined ? null : list.find((p) => p.id === form.id) ?? null),
    [form, list],
  )

  const doRemove = async () => {
    if (!removing) return
    setBusy(true)
    try {
      await api.del(`/api/providers/${removing.id}`)
      toast.success('已删除提供者')
      setRemoving(null)
      providers.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const test = async (provider: Provider) => {
    setTestingId(provider.id)
    // 先清掉上一次的结果，否则旧的成功/失败信息会和这次请求的加载态叠在一起，
    // 用户看不出到底哪一次是当前的。
    setTestResult((prev) => {
      const next = { ...prev }
      delete next[provider.id]
      return next
    })
    try {
      const result = await api.post<TestOutcome>(`/api/providers/${provider.id}/test`)
      setTestResult((prev) => ({ ...prev, [provider.id]: result }))
      if (result?.ok) {
        const parts = ['连接成功']
        if (typeof result.latency_ms === 'number') parts.push(`耗时 ${result.latency_ms}ms`)
        if (result.model) parts.push(result.model)
        toast.success(parts.join(' · '))
      } else {
        // 失败原因**原样**透传（后端 message 里有上游报错细节，改写会让人查不下去）
        toast.error(result?.error || '连接测试失败')
      }
    } catch (err) {
      const message = errorText(err)
      setTestResult((prev) => ({ ...prev, [provider.id]: { ok: false, error: message } }))
      toast.error(message)
    } finally {
      setTestingId(null)
    }
  }

  const setDefault = async (provider: Provider) => {
    try {
      await api.post(`/api/providers/${provider.id}/default`)
      toast.success(`已将「${provider.name || '(未命名)'}」设为默认`)
      providers.reload()
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  const toggleEnabled = async (provider: Provider, enabled: boolean) => {
    try {
      // 后端把 enabled 当 0/1 存，沿用既有形状（不要改成布尔）
      await api.put(`/api/providers/${provider.id}`, { enabled: enabled ? 1 : 0 })
      providers.reload()
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  const refreshAll = () => {
    providers.reload()
    roles.reload()
  }

  const openCreate = () => setForm({ ...EMPTY_FORM })

  return (
    <div className="pv-page">
      <div className="pv-head">
        <div>
          <div className="pv-title">AI 配置 · API Key 统一管理</div>
          <div className="pv-sub">
            所有 AI 角色共用这里的 Key，你只需填一次；再按角色指定不同的提供者与模型。
          </div>
        </div>
        <div className="pv-head-actions">
          <button className="btn sm" onClick={refreshAll}>
            <IconRefresh size={14} /> 刷新
          </button>
          <button className="btn primary" onClick={openCreate}>
            <IconPlus size={14} /> 添加 API Key
          </button>
        </div>
      </div>

      <Notice kind="info">
        Key 由后端加密保存，页面上永远不回显明文，只能看到「已配置 / 未配置」。
        {defaultProvider && (
          <>
            {' '}当前默认提供者：<strong>{defaultProvider.name || '(未命名)'}</strong>。
          </>
        )}
      </Notice>

      <div className="pv-toolbar">
        <div className="pv-seg" role="tablist">
          <button
            role="tab"
            aria-selected={tab === 'providers'}
            className={`pv-seg-btn${tab === 'providers' ? ' active' : ''}`}
            onClick={() => setTab('providers')}
          >
            <IconKey size={14} /> 模型服务（{list.length}）
          </button>
          <button
            role="tab"
            aria-selected={tab === 'roles'}
            className={`pv-seg-btn${tab === 'roles' ? ' active' : ''}`}
            onClick={() => setTab('roles')}
          >
            <IconAgents size={14} /> 角色绑定（{ROLES.length}）
          </button>
        </div>
        <span className="pv-flex1" />
        <span className="pv-toolbar-hint">
          {tab === 'providers' ? `共 ${list.length} 个模型服务` : '共 6 个角色，未指定提供者的跟随默认'}
        </span>
      </div>

      {tab === 'providers' ? (
        <AsyncBox
          loading={providers.loading}
          error={providers.error}
          notReady={providers.notReady}
          onRetry={providers.reload}
        >
          {list.length === 0 ? (
            <div className="pv-card">
              <div className="pv-card-body">
                {/* 不用 AsyncBox 的 empty= 属性、也不用 Empty 组件：它们内部会渲染 emoji，与本设计冲突 */}
                <div className="pv-empty">
                  <span className="pv-empty-icon">
                    <IconKey size={22} />
                  </span>
                  <div className="pv-empty-title">还没有配置 API Key</div>
                  <div className="pv-empty-desc">
                    点右上角「添加 API Key」接入第一个 OpenAI 兼容接口（DeepSeek、通义千问等），AI 角色才能开始工作。
                  </div>
                  <button className="btn primary" onClick={openCreate}>
                    <IconPlus size={14} /> 添加第一个 API Key
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="pv-grid">
              {list.map((provider) => (
                <ProviderCard
                  key={provider.id}
                  provider={provider}
                  testing={testingId === provider.id}
                  result={testResult[provider.id]}
                  onTest={() => test(provider)}
                  onSetDefault={() => setDefault(provider)}
                  onToggleEnabled={(next) => toggleEnabled(provider, next)}
                  onEdit={() => setForm(toForm(provider))}
                  onRemove={() => setRemoving(provider)}
                />
              ))}
            </div>
          )}
        </AsyncBox>
      ) : (
        <RolesPanel
          roles={roleList}
          providers={list}
          defaultProvider={defaultProvider ?? null}
          loading={roles.loading}
          error={roles.error}
          notReady={roles.notReady}
          onReload={roles.reload}
        />
      )}

      {form && (
        <ProviderModal
          form={form}
          keyProvider={editingProvider}
          onClose={() => setForm(null)}
          onSaved={() => {
            setForm(null)
            providers.reload()
          }}
        />
      )}

      {removing && (
        <ConfirmDialog
          title="删除提供者"
          danger
          busy={busy}
          confirmText="确认删除"
          message={
            <>
              确定删除 <strong>{removing.name || '(未命名)'}</strong> 吗？
              <br />
              该 provider 被删后，绑定它的 AI 角色会回退到默认 provider。
            </>
          }
          onCancel={() => setRemoving(null)}
          onConfirm={doRemove}
        />
      )}
    </div>
  )
}

/** 编辑态表单初值：**不含 Key** —— 后端只给掩码，回填明文是不可能的，也是不该做的。 */
function toForm(provider: Provider): ProviderForm {
  return {
    id: provider.id,
    name: provider.name || '',
    base_url: provider.base_url || '',
    api_key: '',
    default_model: provider.default_model || '',
    enabled: bool(provider.enabled),
  }
}

/* ---------------- 模型服务卡片 ---------------- */

function ProviderCard({
  provider,
  testing,
  result,
  onTest,
  onSetDefault,
  onToggleEnabled,
  onEdit,
  onRemove,
}: {
  provider: Provider
  testing: boolean
  result: TestOutcome | undefined
  onTest: () => void
  onSetDefault: () => void
  onToggleEnabled: (next: boolean) => void
  onEdit: () => void
  onRemove: () => void
}) {
  const isDefault = bool(provider.is_default)
  const enabled = bool(provider.enabled)
  const hasKey = bool(provider.has_key)
  const masked = keyMasked(provider)

  return (
    <article className={`pv-card${isDefault ? ' pv-card-default' : ''}`}>
      <div className="pv-card-head">
        <span className="pv-card-title" title={provider.name || '(未命名)'}>
          <IconKey size={15} />
          <span className="pv-ellipsis">{provider.name || '(未命名)'}</span>
        </span>
        {isDefault && (
          <span className="pv-default-chip" title="默认提供者：未单独指定提供者的角色都用它">
            <IconCheck size={12} /> 默认
          </span>
        )}
        <span className="pv-flex1" />
        <span className={`pv-state${enabled ? ' on' : ''}`}>{enabled ? '已启用' : '已停用'}</span>
        <Switch checked={enabled} onChange={onToggleEnabled} />
      </div>

      <div className="pv-card-body">
        <div className="pv-kv">
          <div className="pv-kv-k">接口地址</div>
          <div className="pv-kv-v pv-mono">{provider.base_url || '-'}</div>

          <div className="pv-kv-k">默认模型</div>
          <div className="pv-kv-v pv-mono">
            {provider.default_model || <span className="pv-dim">未指定</span>}
          </div>

          <div className="pv-kv-k">API Key</div>
          <div className="pv-kv-v pv-key-state">
            {hasKey ? (
              <span className="pv-key-ok">
                <IconCheck size={13} /> 已配置
              </span>
            ) : (
              <span className="pv-key-miss">
                <IconClose size={13} /> 未配置
              </span>
            )}
            {masked && (
              <span className="pv-mask pv-mono" title="后端只回掩码，明文不可查看">
                {masked}
              </span>
            )}
          </div>
        </div>

        {result && <TestResult result={result} />}
      </div>

      <div className="pv-card-foot">
        <button className="btn sm" disabled={testing} onClick={onTest}>
          {testing ? (
            <span className="pv-spin">
              <IconRefresh size={14} />
            </span>
          ) : (
            <IconWifi size={14} />
          )}
          {testing ? '测试中…' : '测试连接'}
        </button>
        <button className="btn sm" disabled={isDefault} onClick={onSetDefault}>
          <IconCheck size={14} /> {isDefault ? '已是默认' : '设为默认'}
        </button>
        <span className="pv-flex1" />
        <button className="btn sm ghost" onClick={onEdit}>
          <IconEdit size={14} /> 编辑
        </button>
        <button className="btn sm danger" onClick={onRemove}>
          <IconTrash size={14} /> 删除
        </button>
        {/* 低调提示：测试会真花钱，别让人连点 */}
        <div className="pv-cost-hint">「测试连接」会真实调用一次模型，产生少量 token 费用。</div>
      </div>
    </article>
  )
}

/**
 * 测试结果：成功给延迟 / 模型名 / 返回样例；失败把后端 message **原样**贴出来。
 * 失败文案不做任何包裹、改写或省略 —— 上游报错细节是排查的唯一线索。
 */
function TestResult({ result }: { result: TestOutcome }) {
  if (result.ok) {
    const usage = result.usage
    const usageParts: string[] = []
    if (typeof usage?.prompt_tokens === 'number') usageParts.push(`输入 ${usage.prompt_tokens}`)
    if (typeof usage?.completion_tokens === 'number') usageParts.push(`输出 ${usage.completion_tokens}`)
    if (typeof usage?.total_tokens === 'number' && usageParts.length === 0) {
      usageParts.push(`合计 ${usage.total_tokens}`)
    }

    return (
      <div className="pv-test-result ok">
        <div className="pv-test-line">
          <IconCheckCircle size={14} />
          <span className="pv-test-text">
            连接成功
            {typeof result.latency_ms === 'number' ? ` · 延迟 ${result.latency_ms} ms` : ''}
            {result.model ? ` · 模型 ${result.model}` : ''}
          </span>
        </div>
        {usageParts.length > 0 && <div className="pv-test-meta">本次用量：{usageParts.join('，')} tokens</div>}
        {result.sample ? <div className="pv-test-sample pv-mono">{truncate(result.sample, 200)}</div> : null}
      </div>
    )
  }

  return (
    <div className="pv-test-result err">
      <div className="pv-test-line">
        <IconXCircle size={14} />
        <span className="pv-test-text">连接失败（以下为后端原始报错）</span>
      </div>
      {/* pre + 等宽：保留换行与上游 stack/JSON，不截断、不改写 */}
      <pre className="pv-test-raw pv-mono">{result.error ? result.error : '（后端未返回错误信息）'}</pre>
    </div>
  )
}

/* ---------------- 提供者表单 ---------------- */

function ProviderModal({
  form,
  keyProvider,
  onClose,
  onSaved,
}: {
  form: ProviderForm
  /** 编辑时用于显示「已配置 / 未配置」，只读展示，绝不喂给输入框 */
  keyProvider: Provider | null
  onClose: () => void
  onSaved: () => void
}) {
  const toast = useToast()
  const [draft, setDraft] = useState<ProviderForm>(form)
  const [errors, setErrors] = useState<ProviderErrors>({})
  const [attempted, setAttempted] = useState(false)
  const [saving, setSaving] = useState(false)
  const isEdit = form.id !== undefined
  const currentMask = keyProvider ? keyMasked(keyProvider) : ''

  const update = (patch: Partial<ProviderForm>) => {
    const next = { ...draft, ...patch }
    setDraft(next)
    // 点过一次保存之后才实时纠错：否则刚打开表单就满屏红字
    if (attempted) setErrors(validateProviderForm(next, isEdit))
  }

  const save = async () => {
    setAttempted(true)
    const found = validateProviderForm(draft, isEdit)
    setErrors(found)
    if (found.name || found.base_url || found.api_key) return

    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        default_model: draft.default_model.trim(),
        enabled: draft.enabled ? 1 : 0,
      }
      // 编辑时留空表示「不修改 Key」；后端同样只在显式给非空值时才覆盖，两边语义一致
      if (draft.api_key.trim()) payload.api_key = draft.api_key.trim()
      if (isEdit) await api.put(`/api/providers/${form.id}`, payload)
      else await api.post('/api/providers', payload)
      // 提交成功后立刻把 Key 从 state 里清掉（弹窗虽然会卸载，但这条保证不依赖调用方）
      setDraft((prev) => ({ ...prev, api_key: '' }))
      toast.success(isEdit ? '已保存' : '已添加')
      onSaved()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={isEdit ? '编辑提供者' : '添加 AI 提供者'}
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
      <Field label="名称 *" tip="随便起，比如 DeepSeek / 通义千问">
        <input
          type="text"
          value={draft.name}
          onChange={(e) => update({ name: e.target.value })}
          placeholder="DeepSeek"
        />
        {errors.name && <div className="pv-err">{errors.name}</div>}
      </Field>

      <Field label="接口地址 *" tip="OpenAI 兼容的 /v1 地址，末尾不要带 /chat/completions">
        <input
          type="text"
          value={draft.base_url}
          onChange={(e) => update({ base_url: e.target.value })}
          placeholder="https://api.deepseek.com/v1"
        />
        {errors.base_url && <div className="pv-err">{errors.base_url}</div>}
      </Field>

      <Field
        label={isEdit ? 'API Key' : 'API Key *'}
        tip={
          isEdit
            ? '后端只回掩码，输入框永远是空的；留空表示保持原 Key 不变'
            : '统一在这里管理，所有 AI 角色共用'
        }
      >
        {isEdit && (
          <div className="pv-modal-keystate">
            {bool(keyProvider?.has_key) ? (
              <span className="pv-key-ok">
                <IconCheck size={13} /> 已配置
              </span>
            ) : (
              <span className="pv-key-miss">
                <IconClose size={13} /> 未配置
              </span>
            )}
            {currentMask && <span className="pv-mask pv-mono">{currentMask}</span>}
            <span className="pv-dim">无法查看明文（后端不下发）</span>
          </div>
        )}
        <input
          type="password"
          value={draft.api_key}
          onChange={(e) => update({ api_key: e.target.value })}
          placeholder={isEdit ? '留空则保持原 Key 不变' : 'sk-…'}
          autoComplete="off"
        />
        {errors.api_key && <div className="pv-err">{errors.api_key}</div>}
      </Field>

      <Field label="默认模型" tip="如 deepseek-chat / gpt-4o-mini，可留空由后端决定">
        <input
          type="text"
          value={draft.default_model}
          onChange={(e) => update({ default_model: e.target.value })}
          placeholder="deepseek-chat"
        />
      </Field>

      <Switch checked={draft.enabled} onChange={(next) => update({ enabled: next })} label="启用该提供者" />
    </Modal>
  )
}

/* ---------------- 角色 → 提供者 / 模型映射 ---------------- */

function RolesPanel({
  roles,
  providers,
  defaultProvider,
  loading,
  error,
  notReady,
  onReload,
}: {
  roles: AiRole[]
  providers: Provider[]
  defaultProvider: Provider | null
  loading: boolean
  error: string
  notReady: boolean
  onReload: () => void
}) {
  // 后端只会返回已存在配置的角色；这里补齐全部 6 个，缺的角色按「跟随默认」渲染
  const merged = useMemo(() => {
    return ROLES.map((role) => {
      const found = roles.find((r) => r.role === role)
      return found ?? { role, provider_id: null, model: '', temperature: 0.7, system_prompt: '', enabled: true }
    })
  }, [roles])

  return (
    <div className="pv-card">
      <div className="pv-card-head">
        <span className="pv-card-title">
          <IconAgents size={15} /> 角色 → 提供者 / 模型
        </span>
        <span className="pv-sub">未指定提供者的角色跟随默认</span>
        <span className="pv-flex1" />
        <button className="btn sm" onClick={onReload}>
          <IconRefresh size={14} /> 刷新
        </button>
      </div>

      <div className="pv-card-body">
        <AsyncBox loading={loading} error={error} notReady={notReady} onRetry={onReload}>
          <div className="pv-grid">
            {merged.map((role) => (
              <RoleCard
                key={role.role}
                role={role}
                providers={providers}
                defaultProvider={defaultProvider}
                onSaved={onReload}
              />
            ))}
          </div>
        </AsyncBox>
      </div>

      <div className="pv-card-foot">
        <span className="pv-hint">
          角色基础提示词在每张角色卡里展开改；三级提示词（全局 / 账号 / 商品）在「提示词」页维护，两者是拼接关系。
        </span>
        <span className="pv-flex1" />
        <a className="pv-link" href="#/prompts">
          <IconLink size={13} /> 打开提示词页
        </a>
      </div>
    </div>
  )
}

/** 角色 temperature 的输入框文本：后端可能不返回，缺省按内置默认 0.7。 */
function roleTempText(value?: number): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '0.7'
}

function RoleCard({
  role,
  providers,
  defaultProvider,
  onSaved,
}: {
  role: AiRole
  providers: Provider[]
  defaultProvider: Provider | null
  onSaved: () => void
}) {
  const toast = useToast()
  const [providerId, setProviderId] = useState(role.provider_id ? String(role.provider_id) : '')
  const [model, setModel] = useState(role.model || '')
  const [temperature, setTemperature] = useState(roleTempText(role.temperature))
  const [systemPrompt, setSystemPrompt] = useState(role.system_prompt || '')
  const [saving, setSaving] = useState(false)
  // 用户一旦动过任何一个输入框，就别再让 WS 静默重拉把草稿冲掉
  const touched = useRef(false)
  const markTouched = () => {
    touched.current = true
  }

  // 静默重拉（WS）后后端值可能变了，同步回来；以后端为准 —— 但正在编辑的草稿不能被冲掉
  useEffect(() => {
    if (touched.current) return
    setProviderId(role.provider_id ? String(role.provider_id) : '')
    setModel(role.model || '')
    setTemperature(roleTempText(role.temperature))
    setSystemPrompt(role.system_prompt || '')
  }, [role])

  const boundProvider = providers.find((p) => p.id === role.provider_id) ?? null
  const followsDefault = !role.provider_id
  const defaultName = defaultProvider ? defaultProvider.name || '(未命名)' : '（未设置默认）'
  const tempValue = Number(temperature)
  const tempOk = temperature.trim() !== '' && Number.isFinite(tempValue) && tempValue >= 0 && tempValue <= 2
  const dirty =
    providerId !== (role.provider_id ? String(role.provider_id) : '') ||
    model !== (role.model || '') ||
    temperature !== roleTempText(role.temperature) ||
    systemPrompt !== (role.system_prompt || '')

  const save = async () => {
    if (!tempOk) {
      toast.error('temperature 必须是 0 ~ 2 之间的数字')
      return
    }
    setSaving(true)
    try {
      // provider_id 传 ''（而不是 null）：语义都是「解绑、跟随默认」，
      // 后端 `api/agents.py` 把 '' 归一化成 None，`agents/roles.py::update_role`
      // 只在 `provider_id` 这个键上把 None 当「显式解绑」（其它键的 null 仍算「没传」，
      // 否则 `float(None)` 会炸）。传 null 今天也能解绑，但 '' 是接口层写明的那条路。
      // 这四个键与改动前一致 —— 后端是部分更新，少传哪个键就等于那个设置项在 UI 上再也改不了
      const payload: Record<string, unknown> = {
        provider_id: providerId === '' ? '' : Number(providerId),
        model: model.trim(),
        temperature: tempValue,
        system_prompt: systemPrompt,
      }
      const saved = await api.put<AiRole>(`/api/agents/roles/${encodeURIComponent(role.role)}`, payload)
      const wantId = providerId === '' ? null : Number(providerId)
      const gotId = saved?.provider_id ?? null
      if (gotId !== wantId) {
        // 后端回了 200 却没改 —— 解绑的假成功已在 `agents/roles.py` 修掉，这里留作兜底：
        // 只要回读到的值和请求不一致，就不能假装保存成功
        toast.error(
          `「${roleName(role.role)}」的提供者没被改动，仍是 ${
            gotId === null ? '跟随默认' : `#${gotId}`
          }（后端返回与请求不一致）`,
        )
      } else {
        toast.success(`已保存「${roleName(role.role)}」`)
      }
      // 保存成功后放开草稿保护，让后续 WS 重拉重新以后端为准
      touched.current = false
      onSaved()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <article className="pv-role-card">
      <div className="pv-role-head">
        <span className="pv-role-name">{roleName(role.role)}</span>
        <span className="pv-role-code pv-mono">{role.role}</span>
        <span className="pv-flex1" />
        {followsDefault ? <span className="pv-chip">跟随默认</span> : <span className="pv-chip pv-chip-on">已指定</span>}
      </div>

      <div className="pv-role-body">
        <div className="pv-role-current">
          {followsDefault ? (
            <>
              使用默认提供者：<strong>{defaultName}</strong>
            </>
          ) : (
            <>
              绑定：
              <strong>
                {boundProvider ? boundProvider.name || `#${boundProvider.id}` : `#${role.provider_id}（已不存在）`}
              </strong>
              {boundProvider?.default_model ? (
                <span className="pv-dim"> · 提供者默认模型 {boundProvider.default_model}</span>
              ) : null}
            </>
          )}
        </div>

        <label className="pv-role-field">
          <span className="pv-role-label">提供者</span>
          <select
            value={providerId}
            onChange={(e) => {
              markTouched()
              setProviderId(e.target.value)
            }}
          >
            <option value="">跟随默认（{defaultName}）</option>
            {providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.name || `#${provider.id}`}
              </option>
            ))}
          </select>
        </label>

        <label className="pv-role-field">
          <span className="pv-role-label">模型</span>
          <input
            type="text"
            value={model}
            onChange={(e) => {
              markTouched()
              setModel(e.target.value)
            }}
            placeholder={
              boundProvider?.default_model ? `留空用 ${boundProvider.default_model}` : '留空用提供者默认模型'
            }
          />
        </label>

        <label className="pv-role-field">
          <span className="pv-role-label">temperature（0 ~ 2，越大越发散）</span>
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={temperature}
            onChange={(e) => {
              markTouched()
              setTemperature(e.target.value)
            }}
          />
          {!tempOk && <span className="pv-err">必须是 0 ~ 2 之间的数字</span>}
        </label>

        <details className="pv-role-more">
          <summary>
            角色基础提示词
            {systemPrompt.trim() ? (
              <span className="pv-dim">已自定义 {systemPrompt.length} 字</span>
            ) : (
              <span className="pv-dim">未自定义，用内置默认</span>
            )}
          </summary>
          <textarea
            className="pv-role-textarea"
            rows={5}
            value={systemPrompt}
            onChange={(e) => {
              markTouched()
              setSystemPrompt(e.target.value)
            }}
            placeholder="留空则使用内置默认角色提示词"
          />
          <span className="pv-hint">
            这里只改这个角色的基础提示词。三级提示词（全局 / 账号 / 商品）在
            <a className="pv-link" href="#/prompts">提示词页</a>
            维护，两者是<strong>拼接</strong>关系：它排在基础提示词之后。
          </span>
        </details>
      </div>

      <div className="pv-role-foot">
        <span className="pv-hint">更新于 {formatTime(role.updated_at)}</span>
        <span className="pv-flex1" />
        <button className="btn sm primary" onClick={save} disabled={saving || !dirty}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </article>
  )
}
