/**
 * 交易 → **退货地址**（列表只读 + 四个写入口，全部二次确认）。契约 `docs/contracts/trade.md` §2.6。
 *
 *  1. **没有真机样本** —— 真机上平台回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` → 后端 502。
 *     bundle 里有**两套读法**，后端两套都认：只读页那套
 *     `{addressId,name,phoneNum,address,defaultAddr}` → `address_id / name / phone / address / is_default`；
 *     写页编辑弹窗那套 `{contactId,contactName,mobilePhone,provinceName,cityName,districtName,detailAddress}`
 *     → `contact_id / contact_name / mobile_phone / province_name / city_name / district_name /
 *     detail_address`（**平台给了才有这些键**，没给就不出现，绝不造）。读不到一律 `-`。
 *  2. `is_default` 是**平台只在给 bool 时才有值**的字段：给 `null` 显示 `-`，不能写成「否」。
 *  3. 这个 ret **不写 `need_relogin`** —— 无权限 ≠ 掉线，不要引导用户去重新扫码。
 *  4. 新增 / 编辑 / 删除 / 设为默认**已经实现**，全部走
 *     `POST /api/trade/{account_id}/addresses[...]`，body 一律带字面量 `confirm: true`，
 *     UI 侧还有一道 `ConfirmDialog danger`。写请求只在用户点完确认之后才发出去。
 *  5. **诚实边界**：官方新增/编辑表单里还有「所在地区」级联（`division`，`{label,value}` 数组）
 *     和一个隐藏的 `areaId`（区级区域码）。我们没有区域码来源，**不伪造**：
 *     只发省 / 市 / 区三个**名字**，`area_id` 留空就**根本不发这个键**。
 *     平台若因此拒绝，错误原文会原样显示 —— 不会把失败说成「已成功」。
 */

import { useState } from 'react'
import { api, errorText } from '../api'
import { Badge, ConfirmDialog, Field, Modal, Notice, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { IconEdit, IconPlus, IconShield, IconTrash } from '../icons'
import {
  AccountBar,
  TradeHead,
  TradeListView,
  dayOf,
  ellipsis,
  isNeedRelogin,
  isPermissionDenied,
  money,
  useTradeAccounts,
  when,
  type TradeColumn,
  type TradeEnvelope,
} from './Orders'

interface TradeAddress {
  address_id?: string | null
  /** 写接口的路径参数（后端把平台地址 ID 映射成 contact_id）。 */
  contact_id?: string | null
  name?: string | null
  phone?: string | null
  address?: string | null
  is_default?: boolean | null
  /** 平台原样透传里可能还有别的键，详情区会原样展示。 */
  [key: string]: unknown
}

interface AddressesResult extends TradeEnvelope<TradeAddress> {
  addresses?: TradeAddress[]
}

interface AddressWriteResult {
  action?: string | null
  contact_id?: string | null
  api?: string | null
  warnings?: Array<{ code?: string | null; message?: string | null }> | null
}

const MAX_NAME = 15
const MAX_DETAIL = 100
const ACTIONS: Record<WriteAction, string> = {
  create: '新增',
  update: '编辑',
  delete: '删除',
  default: '设为默认',
}

type WriteAction = 'create' | 'update' | 'delete' | 'default'

interface AddressForm {
  contact_name: string
  mobile_phone: string
  province_name: string
  city_name: string
  district_name: string
  detail_address: string
  area_id: string
  default_addr: boolean
}

const EMPTY_FORM: AddressForm = {
  contact_name: '',
  mobile_phone: '',
  province_name: '',
  city_name: '',
  district_name: '',
  detail_address: '',
  area_id: '',
  default_addr: false,
}

interface WriteTarget {
  action: WriteAction
  row?: TradeAddress
  form?: AddressForm
}

/** 唯一写出口：`confirm: true` 是字面量，调用方没有任何办法绕过它。 */
async function postAddress(path: string, body: Record<string, unknown>) {
  return api.post<AddressWriteResult>(path, { ...body, confirm: true })
}

function normalizeMobile(value: string): string {
  return value.replace(/[\s-]/g, '')
}

function contactIdOf(row: TradeAddress): string {
  return String(row.contact_id ?? row.address_id ?? '').trim()
}

/** 只发真名；`area_id` 留空**不发这个键**（不伪造区域码）。 */
function formToBody(form: AddressForm): Record<string, unknown> {
  const body: Record<string, unknown> = {
    contact_name: form.contact_name.trim(),
    mobile_phone: normalizeMobile(form.mobile_phone),
    province_name: form.province_name.trim(),
    city_name: form.city_name.trim(),
    district_name: form.district_name.trim(),
    detail_address: form.detail_address.trim(),
    default_addr: form.default_addr,
  }
  const area = form.area_id.trim()
  if (area) body.area_id = area
  return body
}

/** 前端先挡一遍（后端也会校验），返回缺什么/错什么，按钮据此禁用。 */
function validate(form: AddressForm): string[] {
  const problems: string[] = []
  const name = form.contact_name.trim()
  if (!name) problems.push('收件人 contact_name')
  else if (name.length > MAX_NAME) problems.push(`收件人 contact_name 超过 ${MAX_NAME} 字`)

  const phone = normalizeMobile(form.mobile_phone)
  if (!phone) problems.push('手机号 mobile_phone')
  else if (!/^\d{11}$/.test(phone)) problems.push('手机号 mobile_phone 要是 11 位数字（去空格/横线后）')

  if (!form.province_name.trim()) problems.push('省 province_name')
  if (!form.city_name.trim()) problems.push('市 city_name')
  if (!form.district_name.trim()) problems.push('区/县 district_name')

  const detail = form.detail_address.trim()
  if (!detail) problems.push('详细地址 detail_address')
  else if (detail.length > MAX_DETAIL) problems.push(`详细地址 detail_address 超过 ${MAX_DETAIL} 字`)
  return problems
}

/** 把这次真正要发的 body 摊平成人能核对的样子（含 confirm）。 */
function factsOf(path: string, body: Record<string, unknown>): string[] {
  const facts = [`接口 ${path}`, 'account_id 见上方已选账号']
  for (const [key, value] of Object.entries(body)) {
    facts.push(`${key} = ${value === '' ? '（空）' : JSON.stringify(value)}`)
  }
  facts.push('confirm = true')
  return facts
}

export function ReturnAddressesPage() {
  const { accounts, picked, setPicked, accountId } = useTradeAccounts()
  const [page, setPage] = useState(1)
  const pageSize = 10

  const toast = useToast()
  const [target, setTarget] = useState<WriteTarget | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notes, setNotes] = useState<string[]>([])

  const state = useAsync<AddressesResult>(
    () => api.get<AddressesResult>(`/api/trade/${encodeURIComponent(accountId)}/addresses`, { page, size: pageSize }),
    [accountId, page],
    Boolean(accountId),
    ['accounts.changed'],
  )

  const data = state.data
  const rows = data?.addresses ?? []
  const permission = isPermissionDenied(state.error)
  const relogin = isNeedRelogin(state.error)

  const form = target?.form ?? null
  const isForm = target?.action === 'create' || target?.action === 'update'
  const problems = form ? validate(form) : []

  function openCreate() {
    setError('')
    setTarget({ action: 'create', form: { ...EMPTY_FORM } })
  }

  function openEdit(row: TradeAddress) {
    setError('')
    // 平台可能给**两套键**：只读页那套把省/市/区与详细地址**合并**成一条 `address` 串
    // （拆不出可靠的省市区 ⇒ 不猜，留空让用户重填）；写页那套是逐个字段
    // （`provinceName`/`cityName`/…，后端原样透传成 `province_name`/`city_name`/…）。
    // 有逐字段的就预填，没有就留空 —— 不猜、也不自己拼地址串。
    const field = (key: string): string => {
      const value = row[key]
      if (typeof value === 'string') return value
      return value == null ? '' : String(value)
    }
    setTarget({
      action: 'update',
      row,
      form: {
        ...EMPTY_FORM,
        contact_name: field('contact_name') || String(row.name ?? ''),
        mobile_phone: field('mobile_phone') || String(row.phone ?? ''),
        province_name: field('province_name'),
        city_name: field('city_name'),
        district_name: field('district_name'),
        detail_address: field('detail_address'),
        default_addr: row.is_default === true,
      },
    })
  }

  function openConfirm(row: TradeAddress, action: 'delete' | 'default') {
    setError('')
    setTarget({ action, row })
  }

  function close() {
    if (busy) return
    setTarget(null)
    setConfirming(false)
    setError('')
  }

  function patchForm(patch: Partial<AddressForm>) {
    setTarget((prev) => (prev?.form ? { ...prev, form: { ...prev.form, ...patch } } : prev))
  }

  /** 这次要发的路径与 body（确认框与提交用的是同一份计算，不允许两处不一致）。 */
  function requestOf(current: WriteTarget): { path: string; body: Record<string, unknown> } | null {
    const base = `/api/trade/${encodeURIComponent(accountId)}`
    if (current.action === 'create' && current.form) {
      return { path: `${base}/addresses`, body: formToBody(current.form) }
    }
    const id = current.row ? contactIdOf(current.row) : ''
    if (!id) return null
    const own = `${base}/addresses/${encodeURIComponent(id)}`
    if (current.action === 'update' && current.form) return { path: `${own}/update`, body: formToBody(current.form) }
    if (current.action === 'delete') return { path: `${own}/delete`, body: {} }
    if (current.action === 'default') return { path: `${own}/default`, body: {} }
    return null
  }

  const request = target ? requestOf(target) : null

  async function submit() {
    if (!target || !request) return
    setBusy(true)
    setError('')
    try {
      const result = await postAddress(request.path, request.body)
      const warnings = (result?.warnings ?? [])
        .map((item) => String(item?.message ?? item?.code ?? '').trim())
        .filter(Boolean)
      setNotes(warnings)
      const id = result?.contact_id ? `（平台 ID ${result.contact_id}）` : ''
      const verb = target.action === 'delete' ? '已删除' : target.action === 'default' ? '已设为默认' : target.action === 'update' ? '已更新' : '已新增'
      toast.success(`退货地址${verb}${id}`)
      setBusy(false)
      setTarget(null)
      setConfirming(false)
      state.reload()
    } catch (err) {
      // 失败原样显示后端 message（含平台原文），弹窗不关，方便复制
      const text = errorText(err)
      setError(text)
      setBusy(false)
      toast.error(text)
    }
  }

  const columns: Array<TradeColumn<TradeAddress>> = [
    {
      key: 'address_id',
      label: '地址 ID',
      sortValue: (row) => {
        const n = Number(row.address_id)
        return Number.isFinite(n) ? n : null
      },
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-mono">{money(row.address_id)}</span>
          {row.is_default === true
            ? <Badge tone="ok">默认地址</Badge>
            : row.is_default === false
              ? <span className="tr-sub">非默认</span>
              : <span className="tr-sub">默认标记 -</span>}
        </div>
      ),
    },
    {
      key: 'name',
      label: '收件人',
      sortValue: (row) => String(row.name ?? ''),
      render: (row) => (
        <div className="tr-cell">
          <span className="tr-name" title={String(row.name ?? '')}>{ellipsis(row.name ?? '', 16) || '-'}</span>
          <span className="tr-sub">{row.phone ? String(row.phone) : '电话 -'}</span>
        </div>
      ),
    },
    {
      key: 'address',
      label: '详细地址',
      sortValue: (row) => String(row.address ?? ''),
      render: (row) => (
        <span className="tr-name" style={{ maxWidth: '40ch' }} title={String(row.address ?? '')}>
          {ellipsis(row.address ?? '', 48) || '-'}
        </span>
      ),
    },
    {
      key: 'default',
      label: '是否默认',
      sortValue: (row) => (row.is_default === null || row.is_default === undefined ? null : row.is_default ? 1 : 0),
      render: (row) => (
        <Badge tone={row.is_default === true ? 'ok' : 'muted'}>
          {row.is_default === true ? '是' : row.is_default === false ? '否' : '-'}
        </Badge>
      ),
    },
    {
      key: 'time',
      label: '更新时间',
      sortValue: (row) => {
        const n = Number(row.gmt_modified ?? row.gmt_create ?? row.create_time)
        return Number.isFinite(n) && n > 0 ? n : null
      },
      render: (row) => (
        <span>{when(row.gmt_modified ?? row.gmt_create ?? row.create_time)}</span>
      ),
    },
    {
      key: 'write',
      label: '操作',
      render: (row) => {
        const id = contactIdOf(row)
        const disabled = !accountId || !id
        return (
          <span className="tr-writes" onClick={(event) => event.stopPropagation()}>
            <button
              className="btn sm"
              disabled={disabled}
              title={disabled ? '这条没有地址 ID，改不了' : '编辑这条退货地址'}
              onClick={() => openEdit(row)}
            >
              <IconEdit size={13} /> 编辑
            </button>
            <button
              className="btn sm"
              disabled={disabled || row.is_default === true}
              title={row.is_default === true ? '已经是默认地址' : '设为默认退货地址'}
              onClick={() => openConfirm(row, 'default')}
            >
              设为默认
            </button>
            <button
              className="btn sm danger"
              disabled={disabled}
              title={disabled ? '这条没有地址 ID，删不了' : '删除这条退货地址'}
              onClick={() => openConfirm(row, 'delete')}
            >
              <IconTrash size={13} /> 删除
            </button>
          </span>
        )
      },
    },
  ]

  return (
    <>
      <TradeHead
        icon={<IconShield size={18} />}
        title="退货地址"
        desc={
          <>
            卖家退货收货地址 · 列表只读，新增 / 编辑 / 删除 / 设为默认都要二次确认
            {data?.fetched_at ? ` · 更新于 ${when(data.fetched_at)}` : ''}
            {data?.api ? ` · 平台接口 ${data.api}` : ''}
          </>
        }
        onRefresh={state.reload}
        loading={state.loading}
      />

      <AccountBar
        accounts={accounts}
        picked={picked}
        setPicked={(id) => { setPicked(id); setPage(1) }}
        extra={
          <button className="btn primary" disabled={!accountId} onClick={openCreate} title={accountId ? '新增退货地址' : '先选账号'}>
            <IconPlus size={14} /> 新增地址
          </button>
        }
      />

      {!accountId && <Notice kind="info">先在上方选一个账号，没有账号就没退货地址可看。</Notice>}

      <Notice kind="info">
        写操作（新增 / 编辑 / 删除 / 设为默认）只发省 / 市 / 区三个<strong>名字</strong>。
        官方表单里还有一个「所在地区」级联（<code>division</code>）和一个隐藏的 <code>areaId</code>（区级区域码），
        我们<strong>没有区域码来源，不伪造</strong>：<code>area_id</code> 留空就根本不发这个键。
        平台要是因此拒绝，下面会原样显示它的错误原文 —— 不会说成「已成功」。
      </Notice>

      {notes.length > 0 && (
        <Notice kind="warn" onClose={() => setNotes([])}>
          平台回执里有提示：{notes.join('；')}
        </Notice>
      )}

      {permission && (
        <Notice kind="warn">
          平台回了 <code>FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问</code>，这个账号读不到退货地址。
          这是平台给的业务结果，<strong>不是掉线、不用重新扫码</strong> ——
          要能读得换一个有卖家工作台会话的账号。下面照实显示错误，不会拿空列表冒充「没有地址」。
        </Notice>
      )}
      {relogin && !permission && (
        <Notice kind="warn">
          这个账号的登录态过期了，平台不让查退货地址 —— 这不是接口坏掉。
          <a className="btn sm" href="#/accounts" style={{ marginLeft: 8 }}>去账号页重新扫码</a>
        </Notice>
      )}

      <TradeListView<TradeAddress>
        rows={rows}
        columns={columns}
        loading={state.loading}
        error={state.error}
        notReady={state.notReady}
        onRetry={state.reload}
        rowKey={(row, index) => String(row.address_id ?? index)}
        emptyTitle="这个账号还没有退货地址"
        emptyDesc="平台一条地址都没返回。拿不到的字段显示 -，不会用「否」顶替没给的默认标记。"
        detail={(row) => <AddressDetail row={row} />}
        statusFilter={{
          label: '是否默认',
          options: ['默认地址', '非默认'],
          value: (row) => (row.is_default === true ? '默认地址' : row.is_default === false ? '非默认' : ''),
        }}
        keywordOf={(row) => [row.address_id, row.name, row.phone, row.address].filter(Boolean).join(' ')}
        dateOf={(row) => dayOf(row.gmt_modified ?? row.gmt_create ?? row.create_time)}
        page={page}
        pageSize={pageSize}
        total={data?.paging?.total_count ?? null}
        hasPrev={page > 1}
        hasNext={Number(data?.paging?.next_page ?? 0) > page}
        onPage={setPage}
      />

      {target && isForm && form && (
        <Modal
          title={target.action === 'create' ? '新增退货地址' : '编辑退货地址'}
          onClose={close}
          wide
          footer={
            <>
              <span className="ra-problems">
                {problems.length > 0 ? `还缺：${problems.join('、')}` : '字段齐了，下一步是二次确认'}
              </span>
              <span className="grow" />
              <button className="btn ghost" onClick={close} disabled={busy}>取消</button>
              <button
                className="btn primary"
                disabled={busy || problems.length > 0}
                onClick={() => { setError(''); setConfirming(true) }}
              >
                下一步：确认提交
              </button>
            </>
          }
        >
          <div className="ra-form">
            <Field label="收件人 contact_name" tip={`必填，≤${MAX_NAME} 字`}>
              <input
                value={form.contact_name}
                maxLength={MAX_NAME}
                onChange={(event) => patchForm({ contact_name: event.target.value })}
              />
            </Field>
            <Field label="手机号 mobile_phone" tip="必填，11 位数字（空格与横线会被去掉）">
              <input
                value={form.mobile_phone}
                onChange={(event) => patchForm({ mobile_phone: event.target.value })}
              />
            </Field>
            <Field label="省 province_name" tip="必填">
              <input value={form.province_name} onChange={(event) => patchForm({ province_name: event.target.value })} />
            </Field>
            <Field label="市 city_name" tip="必填">
              <input value={form.city_name} onChange={(event) => patchForm({ city_name: event.target.value })} />
            </Field>
            <Field label="区/县 district_name" tip="必填">
              <input value={form.district_name} onChange={(event) => patchForm({ district_name: event.target.value })} />
            </Field>
            <Field label="区级区域码 area_id" tip="可选。平台要的是区级区域码，我们没有来源，留空就不发这个键">
              <input value={form.area_id} onChange={(event) => patchForm({ area_id: event.target.value })} />
            </Field>
            <div className="ra-form-wide">
              <Field label="详细地址 detail_address" tip={`必填，≤${MAX_DETAIL} 字（不含省市区）`}>
                <input
                  value={form.detail_address}
                  maxLength={MAX_DETAIL}
                  onChange={(event) => patchForm({ detail_address: event.target.value })}
                />
              </Field>
            </div>
            <div className="ra-form-wide ra-check">
              <label className="ra-check-label">
                <input
                  type="checkbox"
                  checked={form.default_addr}
                  onChange={(event) => patchForm({ default_addr: event.target.checked })}
                />
                <span>设为默认退货地址（default_addr）</span>
              </label>
            </div>
          </div>

          {target.action === 'update' && target.row && (
            <p className="ra-orig">
              平台返回的原始地址（只读，不参与提交）：<code>{String(target.row.address ?? '-')}</code>
              <br />
              {form.province_name || form.city_name || form.district_name || form.detail_address
                ? '平台这次也给了拆开的省 / 市 / 区与详细地址，已经替你填进表单了 —— 提交前请核对一遍。'
                : '平台把省 / 市 / 区和详细地址合并成一条字符串返回，我们拆不出可靠的省市区，所以不替你猜 —— 请重新填写。'}
            </p>
          )}

          {error && <Notice kind="error">{error}</Notice>}
        </Modal>
      )}

      {target && request && (confirming || !isForm) && (
        <ConfirmDialog
          danger
          busy={busy}
          title={`${ACTIONS[target.action]}退货地址`}
          confirmText={busy ? '提交中…' : `确认${ACTIONS[target.action]}`}
          onCancel={close}
          onConfirm={submit}
          message={
            <>
              <p>
                会<strong>真的</strong>改动闲鱼上的退货地址，{target.action === 'delete' ? '删除' : target.action === 'default' ? '设为默认' : '提交'}
                之后<strong>不可撤销</strong>。账号 <code>{accountId}</code>。
              </p>
              <ul className="ra-facts">
                {factsOf(request.path, request.body).map((fact) => <li key={fact}><code>{fact}</code></li>)}
              </ul>
              <p>提交前请核对上面的最终 JSON（<code>confirm: true</code> 就是这一条发出的）。</p>
              {error && <Notice kind="error">{error}</Notice>}
            </>
          }
        />
      )}
    </>
  )
}

function AddressDetail({ row }: { row: TradeAddress }) {
  const known = new Set(['address_id', 'contact_id', 'name', 'phone', 'address', 'is_default'])
  const extras = Object.keys(row).filter((key) => !known.has(key))
  return (
    <div className="tr-detail">
      <div className="tr-detail-grid">
        <Field label="收件信息">
          <span>{money(row.name)}</span>
          <span className="tr-sub">{row.phone ? String(row.phone) : '电话 -'}</span>
          <span className="tr-sub">{money(row.address)}</span>
        </Field>
        <Field label="标识">
          <span>地址 ID <b className="tr-mono">{money(row.address_id)}</b></span>
          <span className="tr-sub">contact_id <b className="tr-mono">{money(row.contact_id)}</b></span>
          <span className="tr-sub">
            是否默认：
            {row.is_default === true ? '是'
              : row.is_default === false ? '否'
                : '平台没给这个键，显示 -（不是「否」）'}
          </span>
        </Field>
      </div>
      {extras.length > 0 && (
        <div className="tr-kv" style={{ marginTop: 8 }}>
          {extras.map((key) => {
            const value = row[key]
            const text = value === null || value === undefined
              ? '-'
              : typeof value === 'object' ? JSON.stringify(value) : String(value)
            return <span key={key}>{key} <b>{text}</b></span>
          })}
        </div>
      )}
    </div>
  )
}
