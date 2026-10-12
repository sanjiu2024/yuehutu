/**
 * 小铺 → 客服分流（**只读 + 写**）。
 *
 * 契约：只读见 `docs/contracts/shop.md`（task-51 冻结），写见 `docs/contracts/shop_write.md`。
 *
 * 只读部分：
 *  - `GET /api/shop/{account_id}/cs-routing?page=&size=`（page/size 只作用于客服分组）
 *  - `routing`：真机只有 `service_time: {start, end}`；`offline_message` / `service_range`
 *    为 `null` + 中文 warning。**整个 `routingRules` 没返回时是 `null`**，不返回 `{}` 冒充。
 *  - `groups` / `groups_total`：真机 `[]` / `0`（平台真值，不是「没权限」）。
 *  - `agents`：键不存在 → `null`（**不是 `[]`**）——「缺键 ≠ 没有客服」。
 *
 * 写部分（三条 POST，全部 `confirm: true` **字面量**，全部二次确认）：
 *  - `POST /api/shop/{account_id}/cs-routing/rule`：保存分流规则。
 *  - `POST /api/shop/{account_id}/cs-groups`：新建（`operate_type='create'`）/ 修改（`'update'`）。
 *  - `POST /api/shop/{account_id}/cs-groups/delete`：删除分组（**不可撤销**）。
 *
 * 五条口径：
 *  1. **`null` 显示 `-`，绝不补 0 / 空数组**。0 是真实值；缺键与空数组文案分开。
 *  2. **无权限 ≠ 掉线**：不会把账号打成 `need_relogin`，别提示用户重新扫码。
 *  3. **平台没回显就不冒充回执**：写接口的 `rule` / `group_id` 可能是 `null`（配一条
 *     warning），此时页面显示「平台没有回显」，**不拿我们发出去的内容充当平台回执**。
 *  4. **枚举中文名只在本地映射**（后端只给英文枚举值），未知值回落显示英文原文；
 *     `repeat_customer` 是**枚举字符串**不是布尔。
 *  5. 失败原样显示后端 `message`（含平台原文），弹窗不关，方便复制。
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, ConfirmDialog, Field, Modal, Notice, useToast } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import {
  IconEdit,
  IconInfo,
  IconPlus,
  IconRefresh,
  IconSend,
  IconTrash,
  IconUser,
  IconUsers,
} from '../icons'
import type { Account } from '../types'
import './shop.css'

const PAGE_SIZE = 20

/* ------------------------------------------------------------------ 枚举（本地中文映射） */

/** 分流方式：后端只给英文枚举值，中文名只在这里做。 */
const DISTRIBUTION_METHODS = [
  'LOW_SATURATION_FIRST',
  'LOW_DAILY_COUNT_FIRST',
  'LAST_SERVED_AGENT',
  'COMBINED_LOW_LOAD',
] as const

const DISTRIBUTION_METHOD_LABELS: Record<string, string> = {
  LOW_SATURATION_FIRST: '优先分给饱和度低的客服',
  LOW_DAILY_COUNT_FIRST: '优先分给当日接待量少的客服',
  LAST_SERVED_AGENT: '优先分给最久没接待的客服',
  COMBINED_LOW_LOAD: '综合低负载',
}

/** 重复进线（老客）策略：和分流方式是同一套枚举值。 */
const REPEAT_CUSTOMER_LABELS: Record<string, string> = {
  LOW_SATURATION_FIRST: '老客：优先分给饱和度低的客服',
  LOW_DAILY_COUNT_FIRST: '老客：优先分给当日接待量少的客服',
  LAST_SERVED_AGENT: '老客：优先分给最久没接待的客服',
  COMBINED_LOW_LOAD: '老客：综合低负载',
}

const OFFLINE_MESSAGES = ['MAIN_ACCOUNT', 'SPECIFIED_ACCOUNT'] as const

const OFFLINE_MESSAGE_LABELS: Record<string, string> = {
  MAIN_ACCOUNT: '留给主账号',
  SPECIFIED_ACCOUNT: '转给指定账号',
}

/** 分组服务范围：官方写侧就是小写；`DEFAULT` 只在读侧见过，这里不给选项。 */
const GROUP_MODES = ['unspecified', 'specified'] as const

const GROUP_MODE_LABELS: Record<string, string> = {
  unspecified: '不指定客服（unspecified）',
  specified: '指定客服类型（specified）',
}

const GROUP_NAME_MAX = 30

/** 未知枚举值回落显示英文原文，绝不猜中文。 */
function enumLabel(table: Record<string, string>, value: unknown): string {
  const key = value === null || value === undefined ? '' : String(value).trim()
  if (key === '') return '-'
  return table[key] || key
}

function enumOptions(table: Record<string, string>, values: readonly string[]) {
  return values.map((value) => ({ value, label: `${enumLabel(table, value)}（${value}）` }))
}

/* ------------------------------------------------------------------ 通用小工具 */

function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num.toLocaleString('zh-CN')
}

function text(value: unknown): string {
  if (value === null || value === undefined) return '-'
  const str = String(value).trim()
  return str === '' ? '-' : str
}

/** 表单用：缺值给空串（不是 `-`），否则会把 `-` 当真值发出去。 */
function str0(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value).trim()
}

function boolText(value: unknown): string {
  if (value === true) return '是'
  if (value === false) return '否'
  return '-'
}

function isMissing(value: unknown): boolean {
  return value === null || value === undefined
}

function jsonOf(value: unknown): string {
  if (value === undefined) return '-'
  try {
    const out = JSON.stringify(value)
    return out === undefined ? '-' : out
  } catch {
    return '-'
  }
}

function stringsOf(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item)).filter((item) => item.trim() !== '')
}

/* ------------------------------------------------------------------ 类型 */

interface ServiceTime {
  start?: unknown
  end?: unknown
}

interface Routing {
  raw?: Record<string, unknown> | null
  service_time?: ServiceTime | null
  offline_message?: unknown
  service_range?: unknown
}

/** 客服分组一行：形状未冻结，原样渲染常见键，其余塞进「其它字段」。 */
type GroupRow = Record<string, unknown>

/** 客服列表一行：同上。 */
type AgentRow = Record<string, unknown>

interface CsRoutingResult {
  account_id?: string
  routing?: Routing | null
  groups?: GroupRow[] | null
  groups_total?: number | null
  agents?: AgentRow[] | null
  warnings?: string[]
  fetched_at?: string
  source?: string
}

interface RuleForm {
  distribution_method: string
  repeat_customer: string
  offline_message: string
  specified_account_id: string
  specified_account_nick: string
  start_time: string
  end_time: string
}

interface GroupForm {
  operate_type: 'create' | 'update'
  group_id: string
  group_name: string
  mode: string
  specified_type: string
  specified_type_name: string
  type_data: string
  customer_uids: string
}

/** 待确认的写操作：`body` 里**不含** `confirm`，它由唯一写出口补上。 */
interface Pending {
  label: string
  title: string
  confirmText: string
  path: string
  body: Record<string, unknown>
  done: string
}

interface WriteReport {
  label: string
  path: string
  body: Record<string, unknown>
  result: Record<string, unknown>
}

/* ------------------------------------------------------------------ 唯一写出口 */

/**
 * 唯一写出口：`confirm: true` 是**字面量**，而且写在 `...body` **之后**，
 * 所以调用方再怎么传也覆盖不掉它。想发写请求只能经过这里。
 */
async function postShopWrite<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return api.post<T>(path, { ...body, confirm: true })
}

/* ------------------------------------------------------------------ 表单 → 请求体 */

/** 把最终要发的 body 摊成清单，二次确认框里逐条列出来。 */
function factsOf(body: Record<string, unknown>): string[] {
  const lines = Object.entries(body).map(([key, value]) => `${key} = ${jsonOf(value)}`)
  lines.push('confirm = true')
  return lines
}

function ruleDraft(form: RuleForm): { body: Record<string, unknown>; problems: string[] } {
  const problems: string[] = []
  if (!form.distribution_method) problems.push('分配方式 distribution_method')
  if (!form.repeat_customer) problems.push('重复进线策略 repeat_customer')
  if (!form.offline_message) problems.push('离线消息处理方式 offline_message')

  const body: Record<string, unknown> = {
    distribution_method: form.distribution_method,
    repeat_customer: form.repeat_customer,
    offline_message: form.offline_message,
  }

  if (form.offline_message === 'SPECIFIED_ACCOUNT') {
    const id = form.specified_account_id.trim()
    const nick = form.specified_account_nick.trim()
    if (!id) problems.push('指定账号 ID specified_account_id')
    if (!nick) problems.push('指定账号昵称 specified_account_nick')
    body.specified_account_id = id
    body.specified_account_nick = nick
  }

  const start = form.start_time.trim()
  const end = form.end_time.trim()
  if (start || end) {
    if (!start || !end) problems.push('服务时间（开始与结束要么都填、要么都不填）')
    else body.service_time = { start_time: start, end_time: end }
  }

  return { body, problems }
}

function groupDraft(form: GroupForm): { body: Record<string, unknown>; problems: string[] } {
  const problems: string[] = []
  const name = form.group_name.trim()
  if (!name) problems.push('分组名 group_name')
  else if (name.length > GROUP_NAME_MAX) problems.push(`分组名不能超过 ${GROUP_NAME_MAX} 个字`)
  if (!form.mode) problems.push('服务范围 mode')

  const key = form.group_id.trim()
  if (form.operate_type === 'update' && !key) problems.push('分组 ID group_id（修改必填）')

  const body: Record<string, unknown> = {
    operate_type: form.operate_type,
    group_name: name,
    mode: form.mode,
  }
  if (form.operate_type === 'update') body.group_id = key

  if (form.mode === 'specified') {
    // 官方缺省 type=ORDER：留空就不发这个键，让平台用它自己的缺省值，不替平台编枚举。
    const type = form.specified_type.trim()
    const typeName = form.specified_type_name.trim()
    if (type) body.specified_type = type
    if (typeName) body.specified_type_name = typeName
    const raw = form.type_data.trim()
    if (raw) {
      let parsed: unknown
      try {
        parsed = JSON.parse(raw)
      } catch {
        problems.push('type_data 不是合法 JSON')
        parsed = undefined
      }
      if (parsed !== undefined) {
        if (Array.isArray(parsed) && parsed.length > 0) body.type_data = parsed
        else problems.push('type_data 必须是**非空 JSON 数组**（平台只认数组）')
      }
    }
  }

  const uids = form.customer_uids
    .split(/[\s,，;；]+/)
    .map((uid) => uid.trim())
    .filter((uid) => uid !== '')
  if (uids.length > 0) body.customer_uids = uids

  return { body, problems }
}

/* ------------------------------------------------------------------ 只读块 */

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

function RoutingPanel({ routing }: { routing: Routing | null | undefined }) {
  if (isMissing(routing)) {
    return (
      <Notice kind="warn">
        平台这次<strong>没有返回</strong>分流规则（整个 <code>routingRules</code> 缺失）。这不代表「没配分流」——
        可能是账号对客服分流业务没有权限。
      </Notice>
    )
  }

  const st = routing?.service_time
  const rows: Array<{ k: string; v: string }> = [
    { k: '服务时间', v: st ? `${text(st.start)} ~ ${text(st.end)}` : '-' },
    { k: '离线消息', v: enumLabel(OFFLINE_MESSAGE_LABELS, routing?.offline_message) },
    { k: '服务范围', v: text(routing?.service_range) },
  ]

  return (
    <div className="sh-rules">
      {rows.map((row) => (
        <div className="sh-rule" key={row.k}>
          <span className="sh-rule-k">{row.k}</span>
          <span className="sh-rule-v">{row.v}</span>
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ 分组 / 客服 */

const GROUP_COLUMNS: Array<{ key: string; label: string }> = [
  { key: 'groupName', label: '分组名' },
  { key: 'groupId', label: '分组 ID' },
  { key: 'customerCount', label: '客服数' },
]

const AGENT_COLUMNS: Array<{ key: string; label: string }> = [
  { key: 'customerName', label: '客服昵称' },
  { key: 'customerId', label: '客服 ID' },
  { key: 'roleName', label: '角色' },
  { key: 'status', label: '状态' },
]

function extraKeys(rows: Array<Record<string, unknown>>, known: string[]): string[] {
  const set = new Set(known)
  const seen = new Set<string>()
  for (const row of rows) {
    for (const key of Object.keys(row || {})) {
      if (!set.has(key)) seen.add(key)
    }
  }
  return [...seen].sort()
}

function RowTable({
  rows,
  columns,
  emptyTitle,
  emptyDesc,
  missingHint,
  renderActions,
}: {
  rows: Array<Record<string, unknown>> | null | undefined
  columns: Array<{ key: string; label: string }>
  emptyTitle: string
  emptyDesc: string
  missingHint: string
  renderActions?: (row: Record<string, unknown>, index: number) => ReactNode
}) {
  if (isMissing(rows)) {
    return (
      <Notice kind="warn">
        {missingHint}（缺键 —— <strong>不等于</strong>「一个都没有」，可能是没权限）。
      </Notice>
    )
  }
  if (!rows || rows.length === 0) {
    return (
      <div className="sh-empty">
        <span className="sh-empty-ico">
          <IconUser size={24} />
        </span>
        <div className="sh-empty-title">{emptyTitle}</div>
        <div className="sh-empty-desc">{emptyDesc}</div>
      </div>
    )
  }
  const extra = extraKeys(rows, columns.map((c) => c.key))
  return (
    <div className="table-wrap">
      <table className="sh-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.key}>{col.label}</th>
            ))}
            {extra.length > 0 && <th>其它字段</th>}
            {renderActions && <th>操作</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => (
            <tr key={idx}>
              {columns.map((col) => (
                <td key={col.key}>{text(row?.[col.key])}</td>
              ))}
              {extra.length > 0 && (
                <td className="sh-extra">
                  {(() => {
                    const pairs = extra
                      .filter((k) => row?.[k] !== undefined)
                      .map((k) => `${k}=${text(row?.[k])}`)
                    return pairs.length ? <span title={pairs.join(' · ')}>{pairs.join(' · ')}</span> : '-'
                  })()}
                </td>
              )}
              {renderActions && (
                <td className="cs-actions">
                  <span className="cs-writes" onClick={(event) => event.stopPropagation()}>
                    {renderActions(row, idx)}
                  </span>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** 分组行里的 id：列表键名未冻结，两个常见写法都认，都没有就空串（按钮会禁用）。 */
function groupIdOf(row: GroupRow): string {
  return str0(row?.groupId) || str0(row?.group_id)
}

function groupNameOf(row: GroupRow): string {
  return str0(row?.groupName) || str0(row?.group_name)
}

function groupFormFrom(row: GroupRow | null): GroupForm {
  if (!row) {
    return {
      operate_type: 'create',
      group_id: '',
      group_name: '',
      mode: '',
      specified_type: '',
      specified_type_name: '',
      type_data: '',
      customer_uids: '',
    }
  }
  const rawMode = str0(row.mode)
  const mode = (GROUP_MODES as readonly string[]).includes(rawMode) ? rawMode : ''
  const rawTypeData = row.typeData ?? row.type_data
  const rawUids = row.customerUids ?? row.customer_uids
  return {
    operate_type: 'update',
    group_id: groupIdOf(row),
    group_name: groupNameOf(row),
    // 读侧不一定回显 mode：**不猜**，留空让用户自己选（否则可能把 specified 悄悄改成 unspecified）。
    mode,
    specified_type: str0(row.specifiedType ?? row.specified_type),
    specified_type_name: str0(row.specifiedTypeName ?? row.specified_type_name),
    type_data: Array.isArray(rawTypeData) && rawTypeData.length > 0 ? JSON.stringify(rawTypeData) : '',
    customer_uids: Array.isArray(rawUids) ? rawUids.map((uid) => String(uid)).join(', ') : '',
  }
}

/** 规则表单初值：只填读侧**确实回显**的值，其余留空让用户选，不替平台猜。 */
function ruleFormFrom(routing: Routing | null | undefined): RuleForm {
  const raw = routing?.raw || null
  const rawMethod = str0(raw?.distributionMethod ?? raw?.distribution_method)
  const rawRepeat = str0(raw?.repeatCustomer ?? raw?.repeat_customer)
  const rawOffline = str0(routing?.offline_message)
  const st = routing?.service_time
  return {
    distribution_method: (DISTRIBUTION_METHODS as readonly string[]).includes(rawMethod) ? rawMethod : '',
    repeat_customer: (DISTRIBUTION_METHODS as readonly string[]).includes(rawRepeat) ? rawRepeat : '',
    offline_message: (OFFLINE_MESSAGES as readonly string[]).includes(rawOffline) ? rawOffline : '',
    specified_account_id: '',
    specified_account_nick: '',
    start_time: str0(st?.start),
    end_time: str0(st?.end),
  }
}

/* ------------------------------------------------------------------ 写结果 */

function WriteResultCard({ report }: { report: WriteReport }) {
  const result = report.result
  const warnings = stringsOf(result.warnings)
  const rows: Array<{ k: string; v: string }> = [
    { k: '接口', v: str0(result.api) || report.path },
    { k: '平台动作', v: text(result.action) },
    { k: '平台已保存 saved', v: boolText(result.saved) },
  ]
  if ('deleted' in result) rows.push({ k: '平台已删除 deleted', v: boolText(result.deleted) })
  if ('group_id' in result) {
    rows.push({
      k: '分组 ID',
      v: isMissing(result.group_id) ? '平台没有回显分组 ID（见下方提示）' : text(result.group_id),
    })
  }
  if ('rule' in result) {
    rows.push({
      k: '平台回显规则',
      v: isMissing(result.rule) ? '平台没有回显规则（见下方提示）' : jsonOf(result.rule),
    })
  }
  rows.push({ k: '本地发出的字段', v: jsonOf(report.body) })

  return (
    <div className="card">
      <div className="sh-section-title">
        <span className="sh-ico">
          <IconInfo size={15} />
        </span>
        上次提交结果：{report.label}
      </div>
      <div className="sh-rules">
        {rows.map((row) => (
          <div className="sh-rule" key={row.k}>
            <span className="sh-rule-k">{row.k}</span>
            <span className="sh-rule-v cs-break">{row.v}</span>
          </div>
        ))}
      </div>
      {warnings.length > 0 && (
        <div className="sh-warnings cs-section">
          {warnings.map((warning, idx) => (
            <Notice key={idx} kind="warn">
              {warning}
            </Notice>
          ))}
        </div>
      )}
      <div className="sh-muted cs-section">
        上面「平台已保存」只看平台有没有报错；平台没回显的内容一律不当作回执。
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ 表单弹窗 */

function RuleFormModal({
  form,
  problems,
  busy,
  onChange,
  onClose,
  onNext,
}: {
  form: RuleForm
  problems: string[]
  busy: boolean
  onChange: (patch: Partial<RuleForm>) => void
  onClose: () => void
  onNext: () => void
}) {
  return (
    <Modal
      wide
      title="修改分流规则"
      onClose={() => !busy && onClose()}
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn danger" onClick={onNext} disabled={busy || problems.length > 0}>
            下一步：确认提交
          </button>
        </>
      }
    >
      <div className="cs-form">
        <Field label="分配方式 distribution_method">
          <select
            value={form.distribution_method}
            onChange={(e) => onChange({ distribution_method: e.target.value })}
            disabled={busy}
          >
            <option value="">请选择…</option>
            {enumOptions(DISTRIBUTION_METHOD_LABELS, DISTRIBUTION_METHODS).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="重复进线策略 repeat_customer" tip="枚举字符串，不是布尔值">
          <select
            value={form.repeat_customer}
            onChange={(e) => onChange({ repeat_customer: e.target.value })}
            disabled={busy}
          >
            <option value="">请选择…</option>
            {enumOptions(REPEAT_CUSTOMER_LABELS, DISTRIBUTION_METHODS).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="离线消息处理方式 offline_message">
          <select
            value={form.offline_message}
            onChange={(e) => onChange({ offline_message: e.target.value })}
            disabled={busy}
          >
            <option value="">请选择…</option>
            {enumOptions(OFFLINE_MESSAGE_LABELS, OFFLINE_MESSAGES).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Field>

        {form.offline_message === 'SPECIFIED_ACCOUNT' && (
          <>
            <Field label="指定账号 ID specified_account_id" tip="平台要的是账号 ID，纯数字会被转成整数">
              <input
                value={form.specified_account_id}
                onChange={(e) => onChange({ specified_account_id: e.target.value })}
                placeholder="例如 2201234567890"
                disabled={busy}
              />
            </Field>
            <Field label="指定账号昵称 specified_account_nick" tip="与 ID 成对必填">
              <input
                value={form.specified_account_nick}
                onChange={(e) => onChange({ specified_account_nick: e.target.value })}
                placeholder="账号昵称"
                disabled={busy}
              />
            </Field>
          </>
        )}

        <Field label="服务时间 service_time.start_time" tip="可选：要么都填、要么都不填">
          <input
            type="time"
            value={form.start_time}
            onChange={(e) => onChange({ start_time: e.target.value })}
            disabled={busy}
          />
        </Field>
        <Field label="服务时间 service_time.end_time">
          <input
            type="time"
            value={form.end_time}
            onChange={(e) => onChange({ end_time: e.target.value })}
            disabled={busy}
          />
        </Field>
      </div>

      {problems.length > 0 && <div className="cs-problems">还缺：{problems.join('、')}</div>}
      <div className="sh-muted cs-section">
        读侧契约只回显 <code>service_time</code> 与 <code>offline_message</code>；另外几个枚举平台没回显，
        所以默认留空，必须自己选 —— 我们不替你猜平台当前值。
      </div>
    </Modal>
  )
}

function GroupFormModal({
  form,
  problems,
  busy,
  onChange,
  onClose,
  onNext,
}: {
  form: GroupForm
  problems: string[]
  busy: boolean
  onChange: (patch: Partial<GroupForm>) => void
  onClose: () => void
  onNext: () => void
}) {
  const editing = form.operate_type === 'update'
  return (
    <Modal
      wide
      title={editing ? '修改客服分组' : '新建客服分组'}
      onClose={() => !busy && onClose()}
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button className="btn danger" onClick={onNext} disabled={busy || problems.length > 0}>
            下一步：确认提交
          </button>
        </>
      }
    >
      <div className="cs-form">
        {editing && (
          <Field label="分组 ID group_id" tip="修改时必须带；来自列表回显">
            <input
              value={form.group_id}
              onChange={(e) => onChange({ group_id: e.target.value })}
              placeholder="分组 ID"
              disabled={busy}
            />
          </Field>
        )}

        <Field label="分组名 group_name" tip={`最多 ${GROUP_NAME_MAX} 个字`}>
          <input
            value={form.group_name}
            onChange={(e) => onChange({ group_name: e.target.value })}
            placeholder="分组名"
            disabled={busy}
          />
        </Field>

        <Field label="服务范围 mode" tip="原样透传，不做大小写转换">
          <select value={form.mode} onChange={(e) => onChange({ mode: e.target.value })} disabled={busy}>
            <option value="">请选择…</option>
            {enumOptions(GROUP_MODE_LABELS, GROUP_MODES).map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </Field>

        {form.mode === 'specified' && (
          <>
            <Field label="指定类型 specified_type" tip="留空就用平台缺省值 ORDER">
              <input
                value={form.specified_type}
                onChange={(e) => onChange({ specified_type: e.target.value })}
                placeholder="ORDER"
                disabled={busy}
              />
            </Field>
            <Field label="指定类型名称 specified_type_name" tip="可选">
              <input
                value={form.specified_type_name}
                onChange={(e) => onChange({ specified_type_name: e.target.value })}
                placeholder="类型名称"
                disabled={busy}
              />
            </Field>
            <Field label="type_data" tip="非空 JSON 数组；留空就不发这个键">
              <textarea
                rows={3}
                value={form.type_data}
                onChange={(e) => onChange({ type_data: e.target.value })}
                placeholder='["…"]'
                disabled={busy}
              />
            </Field>
          </>
        )}

        <Field label="customer_uids" tip="客服 ID，用逗号或空格分隔；留空就不发这个键">
          <input
            value={form.customer_uids}
            onChange={(e) => onChange({ customer_uids: e.target.value })}
            placeholder="例如 2201, 2202"
            disabled={busy}
          />
        </Field>
      </div>

      {problems.length > 0 && <div className="cs-problems">还缺：{problems.join('、')}</div>}
      <div className="sh-muted cs-section">
        平台只给英文枚举值与类型码，我们没有它们的完整取值表：<code>specified_type</code> 留空就让平台用缺省值，
        <code>type_data</code> 必须是合法 JSON 数组，其余一律原样透传，不替平台编值。
      </div>
    </Modal>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function CsRoutingPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [page, setPage] = useState(1)

  const toast = useToast()
  const [ruleForm, setRuleForm] = useState<RuleForm | null>(null)
  const [groupForm, setGroupForm] = useState<GroupForm | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [busy, setBusy] = useState(false)
  const [writeError, setWriteError] = useState('')
  const [last, setLast] = useState<WriteReport | null>(null)

  const accountId = picked || accounts.data?.[0]?.id || ''
  const base = `/api/shop/${encodeURIComponent(accountId)}`

  const data = useAsync<CsRoutingResult>(
    () => api.get<CsRoutingResult>(`/api/shop/${encodeURIComponent(accountId)}/cs-routing`, { page, size: PAGE_SIZE }),
    [accountId, page],
    Boolean(accountId),
    ['shop.changed'],
  )

  const warnings = data.data?.warnings || []
  const groupsTotal = data.data?.groups_total

  const ruleDraftNow = ruleForm ? ruleDraft(ruleForm) : null
  const groupDraftNow = groupForm ? groupDraft(groupForm) : null

  /* ---- 打开表单 ---- */

  function openRuleForm() {
    setWriteError('')
    setRuleForm(ruleFormFrom(data.data?.routing))
  }

  function openGroupForm(row: GroupRow | null) {
    setWriteError('')
    setGroupForm(groupFormFrom(row))
  }

  /* ---- 表单 → 待确认 ---- */

  function nextRule() {
    if (!ruleForm || !ruleDraftNow) return
    setWriteError('')
    setPending({
      label: '保存分流规则',
      title: '确认保存分流规则',
      confirmText: '确认提交到闲鱼',
      path: `${base}/cs-routing/rule`,
      body: ruleDraftNow.body,
      done: '分流规则已提交',
    })
  }

  function nextGroup() {
    if (!groupForm || !groupDraftNow) return
    const editing = groupForm.operate_type === 'update'
    setWriteError('')
    setPending({
      label: editing ? '修改客服分组' : '新建客服分组',
      title: editing ? '确认修改客服分组' : '确认新建客服分组',
      confirmText: '确认提交到闲鱼',
      path: `${base}/cs-groups`,
      body: groupDraftNow.body,
      done: editing ? '客服分组已提交修改' : '客服分组已提交新建',
    })
  }

  function askDelete(row: GroupRow) {
    const groupId = groupIdOf(row)
    if (!groupId) return
    setWriteError('')
    setPending({
      label: `删除客服分组「${groupNameOf(row) || groupId}」`,
      title: '确认删除客服分组',
      confirmText: '确认删除（不可撤销）',
      path: `${base}/cs-groups/delete`,
      body: { group_id: groupId },
      done: '客服分组已提交删除',
    })
  }

  /* ---- 提交（唯一出口的唯一调用点）---- */

  async function submit() {
    if (!pending || busy) return
    setBusy(true)
    setWriteError('')
    try {
      const result = await postShopWrite<Record<string, unknown>>(pending.path, pending.body)
      setLast({ label: pending.label, path: pending.path, body: pending.body, result })
      toast.success(pending.done)
      setPending(null)
      setRuleForm(null)
      setGroupForm(null)
      data.reload()
    } catch (err) {
      // 失败原样显示后端 message（含平台原文），弹窗**不关**，方便复制。
      const message = errorText(err)
      setWriteError(message)
      toast.error(message)
    } finally {
      setBusy(false)
    }
  }

  function cancelPending() {
    if (busy) return
    setPending(null)
    setWriteError('')
  }

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">客服分流</h2>
          <p className="page-desc">
            分流规则、客服分组与客服列表。可以在这里改分流规则、新建 / 修改 / 删除客服分组 ——
            每个写操作都要二次确认，会<strong>真的</strong>改动闲鱼上的配置。
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
            <label className="sh-label" htmlFor="cs-account">
              账号
            </label>
            <select
              id="cs-account"
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
          <Notice kind="info">先在上方选一个账号：客服分流是按账号查的，没有账号就没数据可看。</Notice>
        </div>
      ) : (
        <>
          <div className="card">
            <div className="sh-section-title">
              <span className="sh-ico">
                <IconSend size={15} />
              </span>
              分流规则
              <span className="sh-spacer" />
              <button className="btn sm" onClick={openRuleForm} disabled={data.loading}>
                <span className="sh-ico">
                  <IconEdit size={13} />
                </span>
                修改分流规则
              </button>
            </div>
            <AsyncBox loading={data.loading} error={data.error} notReady={data.notReady} onRetry={data.reload}>
              <RoutingPanel routing={data.data?.routing} />
            </AsyncBox>
          </div>

          <div className="card">
            <div className="sh-section-title">
              <span className="sh-ico">
                <IconUsers size={15} />
              </span>
              客服分组
              <span className="sh-count">{isMissing(groupsTotal) ? '共 - 个' : `共 ${count(groupsTotal)} 个`}</span>
              <span className="sh-spacer" />
              <button className="btn primary sm" onClick={() => openGroupForm(null)} disabled={data.loading}>
                <span className="sh-ico">
                  <IconPlus size={13} />
                </span>
                新建分组
              </button>
            </div>
            <RowTable
              rows={data.data?.groups}
              columns={GROUP_COLUMNS}
              emptyTitle="这个账号下没有客服分组"
              emptyDesc="平台明确返回了空列表。可以用右上角「新建分组」建一个。"
              missingHint="平台这次没有返回客服分组列表"
              renderActions={(row) => {
                const groupId = groupIdOf(row)
                return (
                  <>
                    <button className="btn sm" onClick={() => openGroupForm(row)} disabled={!groupId}>
                      <span className="sh-ico">
                        <IconEdit size={13} />
                      </span>
                      编辑
                    </button>
                    <button className="btn sm danger" onClick={() => askDelete(row)} disabled={!groupId}>
                      <span className="sh-ico">
                        <IconTrash size={13} />
                      </span>
                      删除
                    </button>
                    {!groupId && <span className="sh-muted">这条没有分组 ID，改不了</span>}
                  </>
                )
              }}
            />
          </div>

          {last && <WriteResultCard report={last} />}

          <div className="card">
            <div className="sh-section-title">客服列表</div>
            <RowTable
              rows={data.data?.agents}
              columns={AGENT_COLUMNS}
              emptyTitle="这个账号下没有客服"
              emptyDesc="平台明确返回了空列表。要添加客服请去官方卖家工作台。"
              missingHint="平台这次没有返回客服列表"
            />

            {warnings.length > 0 && (
              <div className="sh-warnings sh-section">
                {warnings.map((w, idx) => (
                  <Notice key={idx} kind="warn">
                    {w}
                  </Notice>
                ))}
              </div>
            )}

            <div className="sh-note">
              <span className="sh-ico">
                <IconInfo size={14} />
              </span>
              客服列表只读；改分流规则 / 分组请用上面的按钮，每个写操作都会先弹确认框。数据更新于
              {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
            </div>
          </div>
        </>
      )}

      {ruleForm && ruleDraftNow && (
        <RuleFormModal
          form={ruleForm}
          problems={ruleDraftNow.problems}
          busy={busy}
          onChange={(patch) => setRuleForm((prev) => (prev ? { ...prev, ...patch } : prev))}
          onClose={() => setRuleForm(null)}
          onNext={nextRule}
        />
      )}

      {groupForm && groupDraftNow && (
        <GroupFormModal
          form={groupForm}
          problems={groupDraftNow.problems}
          busy={busy}
          onChange={(patch) => setGroupForm((prev) => (prev ? { ...prev, ...patch } : prev))}
          onClose={() => setGroupForm(null)}
          onNext={nextGroup}
        />
      )}

      {pending && (
        <ConfirmDialog
          danger
          busy={busy}
          title={pending.title}
          confirmText={pending.confirmText}
          onCancel={cancelPending}
          onConfirm={submit}
          message={
            <>
              <p>
                这一条会<strong>真的</strong>改动闲鱼上的客服分流配置（不是只改本地）。账号：
                <code>{accountId}</code>
              </p>
              <p>{pending.label}，本次提交的字段如下：</p>
              <ul className="cs-facts">
                {factsOf(pending.body).map((line) => (
                  <li key={line}>
                    <code>{line}</code>
                  </li>
                ))}
              </ul>
              {pending.body.operate_type === 'delete' || pending.path.endsWith('/delete') ? (
                <p>删除分组<strong>不可撤销</strong>，平台侧立刻生效。</p>
              ) : null}
              {writeError && (
                <div className="cs-section">
                  <Notice kind="error">
                    平台/后端原样返回：<code className="cs-break">{writeError}</code>
                    <div className="sh-muted">弹窗没有关，可以复制上面的报错；也可以改完再试。</div>
                  </Notice>
                </div>
              )}
            </>
          }
        />
      )}
    </div>
  )
}
