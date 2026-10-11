/**
 * 商品组 → 运费模版（官方 `post-temple` 页）。
 *
 * 契约见 `docs/contracts/item_templates.md`：
 *  - 读：`GET /api/item-templates/{account_id}?page=&size=&template_name=`、`GET /api/item-templates/{account_id}/{template_id}`
 *  - 写：`POST /api/item-templates/{account_id}/create`、`/{account_id}/{template_id}/modify`、
 *        `/{account_id}/{template_id}/delete`、`/{account_id}/unbind`
 *  字段名逐字照官方 bundle（`seller-item@0.0.20`，偏移见 `backend/app/xianyu/item_templates.py`）。
 *
 * 四条口径：
 *  1. `total` / `has_next` 平台没给时是 **null**，显示「-」而不是 0 / false ——
 *     「一个模版都没有」和「平台没告诉我」必须能分辨。
 *  2. 无权限 ≠ 掉线：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 是缺站点 CODE 头，后端归 502，不提示重新扫码。
 *  3. 写操作**全部**要过二次确认；未确认时后端一个平台请求都不发（有测试钉着）。
 *  4. 平台没有「发布模板」这个功能：官方 `post-temple` 是**运费模版管理**，这里如实命名。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, ConfirmDialog, Modal, Notice, useToast } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconEdit, IconPlus, IconRefresh, IconSearch, IconTrash } from '../icons'
import type { Account } from '../types'
import './shop.css'
import './itemtemplates.css'

const PAGE_SIZE = 20
const NAME_MAX = 30

interface TemplateRow {
  template_id?: string
  template_name?: string
  raw?: Record<string, unknown> | null
}

interface TemplateListEnvelope {
  account_id?: string
  templates?: TemplateRow[] | null
  count?: number | null
  total?: number | null
  page?: number | null
  page_size?: number | null
  has_next?: boolean | null
  warnings?: string[]
  fetched_at?: string
  source?: string
}

/** 一条运费明细。字段名与官方一致（snake_case 是后端入参，官方 camelCase 由协议层翻译）。 */
interface DetailRow {
  receive_division_name: string
  first: string
  first_price: string
  excess: string
  excess_price: string
}

interface FormFields {
  template_name: string
  item_prov: string
  item_city: string
  valuation: string
  is_default: boolean
  details: DetailRow[]
  version: string
}

interface FormState {
  kind: 'create' | 'modify'
  /** 编辑时是被改的那一行（要拿 template_id）。 */
  row: TemplateRow
  fields: FormFields
}

type UnbindMode = 'free_ship' | 'template' | 'fixed'

interface UnbindState {
  itemIds: string
  mode: UnbindMode
  templateId: string
  postPrice: string
}

type Pending =
  | { kind: 'create' }
  | { kind: 'modify' }
  | { kind: 'delete'; row: TemplateRow }
  | { kind: 'unbind' }

function emptyDetail(): DetailRow {
  return { receive_division_name: '', first: '1', first_price: '', excess: '1', excess_price: '' }
}

function emptyForm(): FormFields {
  return {
    template_name: '',
    item_prov: '',
    item_city: '',
    valuation: '',
    is_default: false,
    details: [emptyDetail()],
    version: '',
  }
}

function emptyUnbind(): UnbindState {
  return { itemIds: '', mode: 'free_ship', templateId: '', postPrice: '' }
}

/** 已证实的两个字段优先展示，其余键合并进「其它字段」列。 */
function rawOthers(row: TemplateRow): string {
  const raw = row.raw
  if (!raw) return ''
  const skip = new Set(['templateId', 'templateName'])
  const pairs: string[] = []
  for (const key of Object.keys(raw)) {
    if (!skip.has(key)) {
      const v = raw[key]
      pairs.push(`${key}=${v === null || v === undefined ? '-' : String(v)}`)
    }
  }
  return pairs.join(' · ')
}

function numOrNull(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)
  return n.toLocaleString('zh-CN')
}

function text(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/** 商品 ID 允许逗号 / 顿号 / 空格 / 换行分隔 —— 从表格里复制一列粘进来是常见操作。 */
function splitItemIds(raw: string): string[] {
  return raw
    .split(/[\s,，、;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** 表单 → 后端入参：空字段一律不发（官方发的是 undefined，不是空串）。 */
function formBody(fields: FormFields): Record<string, unknown> {
  const details = fields.details
    .filter((d) => d.receive_division_name.trim())
    .map((d) => ({
      receive_division_name: d.receive_division_name.trim(),
      first: d.first.trim(),
      first_price: d.first_price.trim(),
      excess: d.excess.trim(),
      excess_price: d.excess_price.trim(),
    }))
  const body: Record<string, unknown> = {
    template_name: fields.template_name.trim(),
    is_default: fields.is_default,
    details,
  }
  if (fields.item_prov.trim()) body.item_prov = fields.item_prov.trim()
  if (fields.item_city.trim()) body.item_city = fields.item_city.trim()
  if (fields.valuation.trim()) body.valuation = fields.valuation.trim()
  if (fields.version.trim()) body.version = fields.version.trim()
  return body
}

function detailSummary(details: DetailRow[]): string {
  const rows = details.filter((d) => d.receive_division_name.trim())
  if (rows.length === 0) return '（没有明细）'
  return rows
    .map(
      (d) =>
        `${d.receive_division_name.trim()}：首件 ${d.first || '1'} 件 ${d.first_price || '-'} 元，` +
        `续件 ${d.excess || '1'} 件 ${d.excess_price || '-'} 元`,
    )
    .join('；')
}

export function ItemTemplatesPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [appliedName, setAppliedName] = useState('')

  const [form, setForm] = useState<FormState | null>(null)
  const [unbind, setUnbind] = useState<UnbindState | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')

  const accountId = picked || accounts.data?.[0]?.id || ''

  const data = useAsync<TemplateListEnvelope>(
    () =>
      api.get<TemplateListEnvelope>(`/api/item-templates/${encodeURIComponent(accountId)}`, {
        page,
        size: PAGE_SIZE,
        ...(appliedName ? { template_name: appliedName } : {}),
      }),
    [accountId, page, appliedName],
    Boolean(accountId),
  )

  const warnings = data.data?.warnings || []
  const templates = data.data?.templates
  const hasPrev = page > 1
  // total/has_next 平台没给时是 null：只有明确 false 才禁用「下一页」。
  const hasNext = data.data?.has_next === true
  const countText = numOrNull(data.data?.count)
  const totalText = numOrNull(data.data?.total)

  const applySearch = () => {
    setAppliedName(search.trim())
    setPage(1)
  }

  const openCreate = () => {
    setProblem('')
    setForm({ kind: 'create', row: { template_id: '' }, fields: emptyForm() })
  }

  const openEdit = (row: TemplateRow) => {
    const raw = (row.raw || {}) as Record<string, unknown>
    setProblem('')
    setForm({
      kind: 'modify',
      row,
      fields: {
        template_name: text(row.template_name),
        item_prov: text(raw.itemProv),
        item_city: text(raw.itemCity),
        valuation: text(raw.valuation),
        is_default: raw.isDefault === true || raw.isDefault === 1,
        details: [emptyDetail()],
        // 修改必须带 version（官方两者都发）。列表里拿不到就留空，让用户自己填。
        version: text(raw.version),
      },
    })
  }

  /** 写请求唯一出口：`confirm` 是字面量 true，避免任何路径漏掉闸门。 */
  const post = async (path: string, body: Record<string, unknown>) => {
    setBusy(true)
    setProblem('')
    try {
      await api.post(path, { ...body, confirm: true })
      return true
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setProblem(message)
      toast.error(message)
      return false
    } finally {
      setBusy(false)
    }
  }

  const submitForm = async () => {
    if (!form) return
    const path =
      form.kind === 'create'
        ? `/api/item-templates/${encodeURIComponent(accountId)}/create`
        : `/api/item-templates/${encodeURIComponent(accountId)}/${encodeURIComponent(
            text(form.row.template_id),
          )}/modify`
    const ok = await post(path, formBody(form.fields))
    if (ok) {
      toast.success(form.kind === 'create' ? '模版已创建' : '模版已修改')
      setPending(null)
      setForm(null)
      data.reload()
    } else {
      setPending(null)
    }
  }

  const submitDelete = async () => {
    if (!pending || pending.kind !== 'delete') return
    const id = text(pending.row.template_id)
    const ok = await post(
      `/api/item-templates/${encodeURIComponent(accountId)}/${encodeURIComponent(id)}/delete`,
      {},
    )
    if (ok) {
      toast.success('模版已删除')
      setPending(null)
      data.reload()
    } else {
      setPending(null)
    }
  }

  const submitUnbind = async () => {
    if (!unbind) return
    const body: Record<string, unknown> = {
      item_ids: splitItemIds(unbind.itemIds),
      mode: unbind.mode,
    }
    if (unbind.mode === 'template') body.template_id = unbind.templateId.trim()
    if (unbind.mode === 'fixed') body.post_price = Number(unbind.postPrice)
    const ok = await post(`/api/item-templates/${encodeURIComponent(accountId)}/unbind`, body)
    if (ok) {
      toast.success('已提交给闲鱼')
      setPending(null)
      setUnbind(null)
      data.reload()
    } else {
      setPending(null)
    }
  }

  // -- 本地校验（能在本地拦住的就别发给后端） --------------------------

  const formProblem = (() => {
    if (!form) return ''
    const name = form.fields.template_name.trim()
    if (!name) return '模版名称不能为空'
    if (name.length > NAME_MAX) return `模版名称最多 ${NAME_MAX} 个字，现在 ${name.length} 个`
    if (form.fields.valuation.trim() && !Number.isFinite(Number(form.fields.valuation))) {
      return '计价方式必须是数字'
    }
    return ''
  })()

  const unbindProblem = (() => {
    if (!unbind) return ''
    if (splitItemIds(unbind.itemIds).length === 0) return '至少要填一个商品 ID'
    if (unbind.mode === 'template' && !unbind.templateId.trim()) return '选「用模版」时必须选一个模版'
    if (unbind.mode === 'fixed') {
      const price = Number(unbind.postPrice)
      if (!unbind.postPrice.trim()) return '固定运费要填金额'
      if (!Number.isFinite(price) || price <= 0) return '固定运费必须是大于 0 的数字（单位：元）'
    }
    return ''
  })()

  const editable = (templates || []).filter((row) => text(row.template_id))

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">运费模版</h2>
          <p className="page-desc">
            官方「运费模版管理」。可以新建 / 修改 / 删除模版，也可以直接给商品设置运费 ——
            每个写操作都会先弹一次确认，确认前不会向闲鱼发任何请求。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn sm" onClick={accounts.reload} disabled={accounts.loading}>
            <span className="sh-ico">
              <IconRefresh size={13} />
            </span>
            刷新账号
          </button>
          <button className="btn primary sm" onClick={openCreate} disabled={!accountId}>
            <span className="sh-ico">
              <IconPlus size={13} />
            </span>
            新建模版
          </button>
        </div>
      </div>

      <div className="card">
        <div className="sh-bar">
          <div className="sh-field">
            <label className="sh-label" htmlFor="tpl-account">
              账号
            </label>
            <select
              id="tpl-account"
              value={accountId}
              onChange={(e) => {
                setPicked(e.target.value)
                setPage(1)
              }}
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
          <div className="sh-field">
            <label className="sh-label" htmlFor="tpl-search">
              模版名称
            </label>
            <input
              id="tpl-search"
              className="sh-input"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') applySearch()
              }}
              placeholder="按名称搜索，回车生效"
            />
          </div>
          <button type="button" className="btn sm" onClick={applySearch}>
            <span className="sh-ico">
              <IconSearch size={13} />
            </span>
            搜索
          </button>
          <span className="sh-spacer" />
          <span className="sh-count">
            {totalText === '-' ? `本页 ${countText} 个` : `共 ${totalText} 个 · 本页 ${countText} 个`}
          </span>
          <button className="btn sm" onClick={data.reload} disabled={data.loading || !accountId}>
            <span className="sh-ico">
              <IconRefresh size={13} />
            </span>
            刷新
          </button>
        </div>
        {appliedName && (
          <Notice kind="info">
            正在按名称「{appliedName}」筛选。
            <button className="link" onClick={() => { setSearch(''); setAppliedName(''); setPage(1) }}>
              清除筛选
            </button>
          </Notice>
        )}
      </div>

      <div className="card">
        <div className="sh-section-title">
          运费模版
          <span className="sh-count">{`第 ${page} 页`}</span>
        </div>

        <AsyncBox
          loading={data.loading}
          error={data.error}
          notReady={data.notReady}
          empty={!templates || templates.length === 0}
          emptyTitle="这个账号还没有运费模版"
          emptyDesc="平台明确返回了空列表。点右上角「新建模版」建一个，或者去官方卖家工作台。"
          onRetry={data.reload}
        >
          {templates && templates.length > 0 && (
            <div className="table-wrap">
              <table className="sh-table">
                <thead>
                  <tr>
                    <th>模版名称</th>
                    <th>模版 ID</th>
                    <th>其它字段</th>
                    <th className="tpl-ops-head">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {templates.map((row, idx) => {
                    const others = rawOthers(row)
                    const id = text(row.template_id)
                    return (
                      <tr key={id || idx}>
                        <td>{row.template_name || '-'}</td>
                        <td className="sh-mono">{id || '-'}</td>
                        <td className="sh-extra">
                          {others ? <span title={others}>{others}</span> : '-'}
                        </td>
                        <td className="tpl-ops">
                          <button className="btn sm" disabled={!id} onClick={() => openEdit(row)}>
                            <span className="sh-ico">
                              <IconEdit size={13} />
                            </span>
                            编辑
                          </button>
                          <button
                            className="btn danger sm"
                            disabled={!id}
                            onClick={() => {
                              setProblem('')
                              setPending({ kind: 'delete', row })
                            }}
                          >
                            <span className="sh-ico">
                              <IconTrash size={13} />
                            </span>
                            删除
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          {warnings.length > 0 && (
            <div className="sh-warnings sh-section">
              {warnings.map((w, idx) => (
                <Notice key={idx} kind="warn">
                  {w}
                </Notice>
              ))}
            </div>
          )}

          {(hasPrev || hasNext) && (
            <div className="sh-pager">
              <button className="btn sm" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={!hasPrev || data.loading}>
                上一页
              </button>
              <button className="btn sm" onClick={() => setPage((p) => p + 1)} disabled={!hasNext || data.loading}>
                下一页
              </button>
            </div>
          )}

          <div className="sh-note">
            写操作（新建 / 修改 / 删除 / 设置运费）都要二次确认，确认前不会向平台发请求。数据更新于
            {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
          </div>
        </AsyncBox>
      </div>

      <div className="card">
        <div className="sh-section-title">
          给商品设置运费
          <span className="sh-count">和官方「设置运费」同一个入口</span>
        </div>
        <p className="tpl-hint">
          从「商品」页复制商品 ID 粘进来（逗号或换行分隔都行），选一种运费方式。提交后会真的改到闲鱼上。
        </p>
        {unbind === null ? (
          <button className="btn sm" disabled={!accountId} onClick={() => { setProblem(''); setUnbind(emptyUnbind()) }}>
            <span className="sh-ico">
              <IconPlus size={13} />
            </span>
            设置商品运费
          </button>
        ) : (
          <div className="tpl-form">
            <div className="field">
              <label htmlFor="tpl-items">商品 ID</label>
              <textarea
                id="tpl-items"
                className="tpl-items"
                value={unbind.itemIds}
                onChange={(e) => setUnbind({ ...unbind, itemIds: e.target.value })}
                placeholder={'例如 1085757639387, 108581070822\n支持逗号 / 换行分隔，一次可以填多个'}
              />
              <span className="tip">
                已识别 {splitItemIds(unbind.itemIds).length} 个商品
              </span>
            </div>

            <div className="field">
              <label>运费方式</label>
              <div className="tpl-seg">
                {(
                  [
                    ['free_ship', '包邮'],
                    ['template', '用已有模版'],
                    ['fixed', '固定运费'],
                  ] as [UnbindMode, string][]
                ).map(([value, label]) => (
                  <label key={value} className={`tpl-seg-item${unbind.mode === value ? ' on' : ''}`}>
                    <input
                      type="radio"
                      name="tpl-mode"
                      checked={unbind.mode === value}
                      onChange={() => setUnbind({ ...unbind, mode: value })}
                    />
                    {label}
                  </label>
                ))}
              </div>
            </div>

            {unbind.mode === 'template' && (
              <div className="field">
                <label htmlFor="tpl-pick">选模版</label>
                <select
                  id="tpl-pick"
                  value={unbind.templateId}
                  onChange={(e) => setUnbind({ ...unbind, templateId: e.target.value })}
                >
                  <option value="">（选一个）</option>
                  {editable.map((row) => (
                    <option key={text(row.template_id)} value={text(row.template_id)}>
                      {row.template_name || text(row.template_id)}
                    </option>
                  ))}
                </select>
                {editable.length === 0 && <span className="tip">这个账号还没有模版，先新建一个。</span>}
              </div>
            )}

            {unbind.mode === 'fixed' && (
              <div className="field">
                <label htmlFor="tpl-price">固定运费（元）</label>
                <input
                  id="tpl-price"
                  className="sh-input"
                  value={unbind.postPrice}
                  onChange={(e) => setUnbind({ ...unbind, postPrice: e.target.value })}
                  placeholder="例如 8.8"
                />
                <span className="tip">按元填，提交时后端会换算成平台要的「分」。</span>
              </div>
            )}

            {unbindProblem && <Notice kind="warn">{unbindProblem}</Notice>}
            {problem && <Notice kind="error">{problem}</Notice>}

            <div className="tpl-actions">
              <button className="btn ghost" onClick={() => { setUnbind(null); setProblem('') }} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={Boolean(unbindProblem) || busy}
                onClick={() => setPending({ kind: 'unbind' })}
              >
                下一步：确认
              </button>
            </div>
          </div>
        )}
      </div>

      {form && (
        <Modal
          title={form.kind === 'modify' ? '编辑运费模版' : '新建运费模版'}
          onClose={() => { setForm(null); setProblem('') }}
          wide
          footer={
            <>
              <button className="btn ghost" onClick={() => { setForm(null); setProblem('') }} disabled={busy}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={Boolean(formProblem) || busy}
                onClick={() => setPending({ kind: form.kind })}
              >
                下一步：确认
              </button>
            </>
          }
        >
          <div className="tpl-form">
            <Notice kind="info">
              字段名逐字照官方页面。留空的选填项不会提交（官方发的是 undefined，不是空串）。
            </Notice>

            <div className="field">
              <label htmlFor="tpl-name">模版名称 *</label>
              <input
                id="tpl-name"
                className="sh-input"
                value={form.fields.template_name}
                onChange={(e) => setForm({ ...form, fields: { ...form.fields, template_name: e.target.value } })}
                placeholder="例如 默认运费模版"
              />
              <span className="tip">
                {form.fields.template_name.trim().length} / {NAME_MAX} 个字
              </span>
            </div>

            <div className="tpl-cols">
              <div className="field">
                <label htmlFor="tpl-prov">发货省</label>
                <input
                  id="tpl-prov"
                  className="sh-input"
                  value={form.fields.item_prov}
                  onChange={(e) => setForm({ ...form, fields: { ...form.fields, item_prov: e.target.value } })}
                  placeholder="例如 浙江省"
                />
              </div>
              <div className="field">
                <label htmlFor="tpl-city">发货市</label>
                <input
                  id="tpl-city"
                  className="sh-input"
                  value={form.fields.item_city}
                  onChange={(e) => setForm({ ...form, fields: { ...form.fields, item_city: e.target.value } })}
                  placeholder="例如 杭州市"
                />
              </div>
              <div className="field">
                <label htmlFor="tpl-valuation">计价方式</label>
                <input
                  id="tpl-valuation"
                  className="sh-input"
                  value={form.fields.valuation}
                  onChange={(e) => setForm({ ...form, fields: { ...form.fields, valuation: e.target.value } })}
                  placeholder="留空 = 不提交"
                />
              </div>
            </div>

            <label className="check">
              <input
                type="checkbox"
                checked={form.fields.is_default}
                onChange={(e) => setForm({ ...form, fields: { ...form.fields, is_default: e.target.checked } })}
              />
              设为默认模版
            </label>

            {form.kind === 'modify' && (
              <div className="field">
                <label htmlFor="tpl-version">版本号</label>
                <input
                  id="tpl-version"
                  className="sh-input"
                  value={form.fields.version}
                  onChange={(e) => setForm({ ...form, fields: { ...form.fields, version: e.target.value } })}
                  placeholder="平台返回的 version，官方修改时会带上"
                />
                <span className="tip">列表里没返回 version 就留空 —— 后端会把这一项整个不提交。</span>
              </div>
            )}

            <div className="tpl-details-head">
              <span>运费明细</span>
              <button
                className="btn sm"
                onClick={() =>
                  setForm({ ...form, fields: { ...form.fields, details: [...form.fields.details, emptyDetail()] } })
                }
              >
                <span className="sh-ico">
                  <IconPlus size={13} />
                </span>
                加一行
              </button>
            </div>
            <div className="tpl-details">
              <div className="tpl-detail-row tpl-detail-head">
                <span>收货地区</span>
                <span>首件</span>
                <span>首费（元）</span>
                <span>续件</span>
                <span>续费（元）</span>
                <span />
              </div>
              {form.fields.details.map((row, idx) => {
                const patch = (part: Partial<DetailRow>) => {
                  const next = [...form.fields.details]
                  next[idx] = { ...row, ...part }
                  setForm({ ...form, fields: { ...form.fields, details: next } })
                }
                return (
                  <div className="tpl-detail-row" key={idx}>
                    <input
                      className="sh-input"
                      value={row.receive_division_name}
                      onChange={(e) => patch({ receive_division_name: e.target.value })}
                      placeholder="例如 全国"
                    />
                    <input className="sh-input" value={row.first} onChange={(e) => patch({ first: e.target.value })} />
                    <input
                      className="sh-input"
                      value={row.first_price}
                      onChange={(e) => patch({ first_price: e.target.value })}
                      placeholder="8"
                    />
                    <input className="sh-input" value={row.excess} onChange={(e) => patch({ excess: e.target.value })} />
                    <input
                      className="sh-input"
                      value={row.excess_price}
                      onChange={(e) => patch({ excess_price: e.target.value })}
                      placeholder="3"
                    />
                    <button
                      className="btn ghost sm"
                      disabled={form.fields.details.length <= 1}
                      onClick={() =>
                        setForm({
                          ...form,
                          fields: { ...form.fields, details: form.fields.details.filter((_, i) => i !== idx) },
                        })
                      }
                    >
                      删
                    </button>
                  </div>
                )
              })}
            </div>

            {formProblem && <Notice kind="warn">{formProblem}</Notice>}
            {problem && <Notice kind="error">{problem}</Notice>}
          </div>
        </Modal>
      )}

      {pending?.kind === 'create' && form && (
        <ConfirmDialog
          title="确认新建运费模版"
          danger
          busy={busy}
          confirmText="确认提交到闲鱼"
          onCancel={() => setPending(null)}
          onConfirm={submitForm}
          message={
            <>
              <p>
                这会<b>真的</b>在闲鱼上新建一个运费模版，账号 <b>{accountId}</b>。
              </p>
              <p>
                名称：<b>{form.fields.template_name.trim()}</b>
                {form.fields.is_default ? '（设为默认）' : ''}
                <br />
                发货地：{[form.fields.item_prov, form.fields.item_city].filter(Boolean).join(' ') || '不提交'}
                <br />
                明细：{detailSummary(form.fields.details)}
              </p>
            </>
          }
        />
      )}

      {pending?.kind === 'modify' && form && (
        <ConfirmDialog
          title="确认修改运费模版"
          danger
          busy={busy}
          confirmText="确认提交到闲鱼"
          onCancel={() => setPending(null)}
          onConfirm={submitForm}
          message={
            <>
              <p>
                这会<b>真的</b>改动闲鱼上的模版 <b>{text(form.row.template_id)}</b>（账号 {accountId}）。
              </p>
              <p>
                名称：<b>{form.fields.template_name.trim()}</b>
                <br />
                明细：{detailSummary(form.fields.details)}
                <br />
                版本号：{form.fields.version.trim() || '（不提交）'}
              </p>
            </>
          }
        />
      )}

      {pending?.kind === 'delete' && (
        <ConfirmDialog
          title="确认删除运费模版"
          danger
          busy={busy}
          confirmText="确认删除"
          onCancel={() => setPending(null)}
          onConfirm={submitDelete}
          message={
            <>
              <p>
                这会**真的**从闲鱼上删掉模版{' '}
                <b>{pending.row.template_name || text(pending.row.template_id)}</b>
                （{text(pending.row.template_id)}）。<b>删除不可撤销。</b>
              </p>
              <p>正在用这个模版的商品可能会受影响，删之前先确认没有商品在用。</p>
            </>
          }
        />
      )}

      {pending?.kind === 'unbind' && unbind && (
        <ConfirmDialog
          title="确认设置运费"
          danger
          busy={busy}
          confirmText="确认提交到闲鱼"
          onCancel={() => setPending(null)}
          onConfirm={submitUnbind}
          message={
            <>
              <p>
                这会**真的**改动闲鱼上这些商品的运费设置，账号 <b>{accountId}</b>。
              </p>
              <p>
                商品：{splitItemIds(unbind.itemIds).join('、')}
                <br />
                方式：
                {unbind.mode === 'free_ship'
                  ? '包邮'
                  : unbind.mode === 'template'
                    ? `用模版 ${unbind.templateId.trim()}`
                    : `固定运费 ${unbind.postPrice.trim()} 元`}
              </p>
            </>
          }
        />
      )}
    </div>
  )
}
