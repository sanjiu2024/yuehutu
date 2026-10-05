/** AI 配置：providers 增删改（统一 API Key 管理）、测试连接、设为默认；角色 → 模型映射。 */

import { useEffect, useMemo, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatTime, roleName } from '../format'
import type { AiRole, Provider, ProviderTest } from '../types'

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

/** 只显示 Key 的头尾，避免整串暴露在界面上。 */
function maskKey(key?: string): string {
  if (!key) return '未设置'
  if (key.length <= 10) return '••••••'
  return `${key.slice(0, 4)}••••••${key.slice(-4)}`
}

export function ProvidersPage() {
  const toast = useToast()
  const providers = useAsync<Provider[]>(() => api.get<Provider[]>('/api/providers'), [])
  const roles = useAsync<AiRole[]>(() => api.get<AiRole[]>('/api/agents/roles'), [])

  const [tab, setTab] = useState<'providers' | 'roles'>('providers')
  const [form, setForm] = useState<ProviderForm | null>(null)
  const [removing, setRemoving] = useState<Provider | null>(null)
  const [testingId, setTestingId] = useState<number | null>(null)
  const [testResult, setTestResult] = useState<Record<number, ProviderTest>>({})
  const [busy, setBusy] = useState(false)

  const list = providers.data ?? []
  const roleList = roles.data ?? []

  const defaultProvider = useMemo(() => list.find((p) => bool(p.is_default)), [list])

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
    try {
      const result = await api.post<ProviderTest>(`/api/providers/${provider.id}/test`)
      setTestResult((prev) => ({ ...prev, [provider.id]: result }))
      if (result?.ok) toast.success(`连接成功${result.latency_ms ? `，耗时 ${result.latency_ms}ms` : ''}`)
      else toast.error(`连接失败：${result?.error || '未知原因'}`)
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
      toast.success(`已将「${provider.name}」设为默认`)
      providers.reload()
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  const toggleEnabled = async (provider: Provider, enabled: boolean) => {
    try {
      await api.put(`/api/providers/${provider.id}`, { enabled: enabled ? 1 : 0 })
      providers.reload()
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  return (
    <div>
      <div className="tabs">
        <button className={`tab${tab === 'providers' ? ' active' : ''}`} onClick={() => setTab('providers')}>
          API 提供者（{list.length}）
        </button>
        <button className={`tab${tab === 'roles' ? ' active' : ''}`} onClick={() => setTab('roles')}>
          角色 → 模型映射（{roleList.length || ROLES.length}）
        </button>
      </div>

      {tab === 'providers' ? (
        <>
          <Notice kind="info">
            所有 AI 角色共用这里的 API Key。你只需填一次，再在「角色 → 模型映射」里给不同角色指定不同模型。
            {defaultProvider && (
              <>
                {' '}当前默认：<strong>{defaultProvider.name}</strong>
              </>
            )}
          </Notice>

          <div className="toolbar">
            <button className="btn primary" onClick={() => setForm({ ...EMPTY_FORM })}>
              ＋ 添加提供者
            </button>
            <span className="spacer" />
            <button className="btn" onClick={() => { providers.reload(); roles.reload() }}>
              刷新
            </button>
          </div>

          <div className="card">
            <AsyncBox
              loading={providers.loading}
              error={providers.error}
              notReady={providers.notReady}
              empty={list.length === 0}
              emptyIcon="🔑"
              emptyTitle="还没有配置 AI 提供者"
              emptyDesc="添加一个 OpenAI 兼容的 API（如 DeepSeek、OpenAI、通义），AI 才能工作。"
              onRetry={providers.reload}
            >
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>接口地址</th>
                      <th>API Key</th>
                      <th>默认模型</th>
                      <th className="nowrap">状态</th>
                      <th className="nowrap">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((provider) => {
                      const result = testResult[provider.id]
                      return (
                        <tr key={provider.id}>
                          <td>
                            <div className="row">
                              <strong>{provider.name || '(未命名)'}</strong>
                              {bool(provider.is_default) && <Badge tone="ok">默认</Badge>}
                            </div>
                            {result && (
                              <div style={{ fontSize: 11.5, marginTop: 2 }}>
                                {result.ok ? (
                                  <span style={{ color: 'var(--ok)' }}>
                                    连通 ✓{result.latency_ms ? ` ${result.latency_ms}ms` : ''}
                                    {result.model ? ` · ${result.model}` : ''}
                                  </span>
                                ) : (
                                  <span style={{ color: 'var(--danger)' }}>失败：{result.error || '未知'}</span>
                                )}
                              </div>
                            )}
                          </td>
                          <td className="mono" style={{ fontSize: 11.5, maxWidth: 240, wordBreak: 'break-all' }}>
                            {provider.base_url || '-'}
                          </td>
                          <td className="mono" style={{ fontSize: 11.5 }}>
                            {maskKey(provider.api_key)}
                          </td>
                          <td className="mono" style={{ fontSize: 11.5 }}>
                            {provider.default_model || '-'}
                          </td>
                          <td className="nowrap">
                            <Switch checked={bool(provider.enabled)} onChange={(next) => toggleEnabled(provider, next)} />
                          </td>
                          <td className="nowrap">
                            <div className="row" style={{ gap: 5 }}>
                              <button className="btn sm" disabled={testingId === provider.id} onClick={() => test(provider)}>
                                {testingId === provider.id ? '测试中…' : '测试连接'}
                              </button>
                              <button className="btn sm" disabled={bool(provider.is_default)} onClick={() => setDefault(provider)}>
                                设为默认
                              </button>
                              <button
                                className="btn sm ghost"
                                onClick={() =>
                                  setForm({
                                    id: provider.id,
                                    name: provider.name || '',
                                    base_url: provider.base_url || '',
                                    api_key: '',
                                    default_model: provider.default_model || '',
                                    enabled: bool(provider.enabled),
                                  })
                                }
                              >
                                编辑
                              </button>
                              <button className="btn sm danger" onClick={() => setRemoving(provider)}>
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
        </>
      ) : (
        <RolesPanel roles={roleList} providers={list} loading={roles.loading} error={roles.error} notReady={roles.notReady} onReload={roles.reload} />
      )}

      {form && (
        <ProviderModal
          form={form}
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
              确定删除 <strong>{removing.name}</strong> 吗？
              <br />
              使用该提供者的角色会回落到默认提供者。
            </>
          }
          onCancel={() => setRemoving(null)}
          onConfirm={doRemove}
        />
      )}
    </div>
  )
}

/* ---------------- 提供者表单 ---------------- */

function ProviderModal({ form, onClose, onSaved }: { form: ProviderForm; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [draft, setDraft] = useState<ProviderForm>(form)
  const [saving, setSaving] = useState(false)
  const isEdit = form.id !== undefined

  const save = async () => {
    if (!draft.name.trim()) {
      toast.info('请填写名称')
      return
    }
    if (!draft.base_url.trim()) {
      toast.info('请填写接口地址')
      return
    }
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        default_model: draft.default_model.trim(),
        enabled: draft.enabled ? 1 : 0,
      }
      // 编辑时留空表示「不修改 Key」
      if (draft.api_key.trim()) payload.api_key = draft.api_key.trim()
      if (isEdit) await api.put(`/api/providers/${form.id}`, payload)
      else await api.post('/api/providers', payload)
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
        <input type="text" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      </Field>
      <Field label="接口地址 *" tip="OpenAI 兼容的 /v1 地址，末尾不要带 /chat/completions">
        <input
          type="text"
          value={draft.base_url}
          onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
          placeholder="https://api.deepseek.com/v1"
        />
      </Field>
      <Field
        label={isEdit ? 'API Key（留空表示不修改）' : 'API Key *'}
        tip="统一在这里管理，所有 AI 角色共用"
      >
        <input
          type="password"
          value={draft.api_key}
          onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
          placeholder={isEdit ? '留空则保持原 Key 不变' : 'sk-…'}
          autoComplete="off"
        />
      </Field>
      <Field label="默认模型" tip="如 deepseek-chat / gpt-4o-mini，可留空由后端决定">
        <input
          type="text"
          value={draft.default_model}
          onChange={(e) => setDraft({ ...draft, default_model: e.target.value })}
          placeholder="deepseek-chat"
        />
      </Field>
      <Switch checked={draft.enabled} onChange={(next) => setDraft({ ...draft, enabled: next })} label="启用该提供者" />
    </Modal>
  )
}

/* ---------------- 角色映射 ---------------- */

function RolesPanel({
  roles,
  providers,
  loading,
  error,
  notReady,
  onReload,
}: {
  roles: AiRole[]
  providers: Provider[]
  loading: boolean
  error: string
  notReady: boolean
  onReload: () => void
}) {
  // 后端可能只返回已配置的角色，这里补齐全部 6 个角色
  const merged = useMemo(() => {
    return ROLES.map((role) => {
      const found = roles.find((r) => r.role === role)
      return found ?? { role, provider_id: null, model: '', temperature: 0.7, system_prompt: '', enabled: 1 }
    })
  }, [roles])

  return (
    <div className="card">
      <h3>
        角色 → 模型映射
        <span className="hint">未指定则使用默认提供者</span>
        <span style={{ flex: 1 }} />
        <button className="btn sm" onClick={onReload}>
          刷新
        </button>
      </h3>
      <AsyncBox loading={loading} error={error} notReady={notReady} onRetry={onReload}>
        <div className="col">
          {merged.map((role) => (
            <RoleRow key={role.role} role={role} providers={providers} onSaved={onReload} />
          ))}
        </div>
      </AsyncBox>
    </div>
  )
}

function RoleRow({ role, providers, onSaved }: { role: AiRole; providers: Provider[]; onSaved: () => void }) {
  const toast = useToast()
  const [providerId, setProviderId] = useState(role.provider_id ? String(role.provider_id) : '')
  const [model, setModel] = useState(role.model || '')
  const [temperature, setTemperature] = useState(String(role.temperature ?? 0.7))
  const [systemPrompt, setSystemPrompt] = useState(role.system_prompt || '')
  const [expanded, setExpanded] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setProviderId(role.provider_id ? String(role.provider_id) : '')
    setModel(role.model || '')
    setTemperature(String(role.temperature ?? 0.7))
    setSystemPrompt(role.system_prompt || '')
  }, [role])

  const save = async () => {
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        provider_id: providerId === '' ? null : Number(providerId),
        model: model.trim(),
        temperature: Number(temperature) || 0,
        system_prompt: systemPrompt,
      }
      await api.put(`/api/agents/roles/${encodeURIComponent(role.role)}`, payload)
      toast.success(`已保存「${roleName(role.role)}」配置`)
      onSaved()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ border: '1px solid var(--border-soft)', borderRadius: 'var(--radius-sm)', padding: 12 }}>
      <div className="row wrap">
        <strong style={{ minWidth: 110 }}>{roleName(role.role)}</strong>
        <span className="mono dim" style={{ fontSize: 11.5 }}>{role.role}</span>
        <span className="spacer" />
        <Badge tone={bool(role.enabled) ? 'ok' : 'muted'}>{bool(role.enabled) ? '启用' : '停用'}</Badge>
        <button className="btn sm ghost" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : '展开'}
        </button>
      </div>

      <div className="row wrap" style={{ marginTop: 9, gap: 10 }}>
        <select value={providerId} onChange={(e) => setProviderId(e.target.value)} style={{ width: 180 }}>
          <option value="">使用默认提供者</option>
          {providers.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name}
            </option>
          ))}
        </select>
        <input
          type="text"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="模型名（留空用提供者默认）"
          style={{ width: 220 }}
        />
        <input
          type="number"
          step="0.1"
          min="0"
          max="2"
          value={temperature}
          onChange={(e) => setTemperature(e.target.value)}
          style={{ width: 90 }}
          title="temperature 0~2"
        />
        <button className="btn primary sm" onClick={save} disabled={saving}>
          {saving ? '保存中…' : '保存'}
        </button>
        <span className="dim" style={{ fontSize: 11.5 }}>
          更新于 {formatTime(role.updated_at)}
        </span>
      </div>

      {expanded && (
        <div style={{ marginTop: 10 }}>
          <Field label="角色系统提示词" tip="会被「提示词」页里的三级提示词覆盖/追加">
            <textarea value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} rows={4} />
          </Field>
        </div>
      )}
    </div>
  )
}
