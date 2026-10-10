/**
 * 商品组 → 运费模版（官方 `post-temple` 页，**只读**）。
 *
 * 契约见 `docs/contracts/item_templates.md`（task-53 冻结）：
 *  - `GET /api/item-templates/{account_id}?page=&size=&template_name=`（列表）
 *  - `GET /api/item-templates/{account_id}/{template_id}`（详情，平台返回空 → 404）
 *  - 平台没有「发布模板」这个功能：官方 `post-temple` 是**运费模版管理**，这里如实命名。
 *
 * 三条口径：
 *  1. `total` / `has_next` 平台没给时是 **null**，显示「-」而不是 0 / false ——
 *     「一个模版都没有」和「平台没告诉我」必须能分辨。
 *  2. 无权限 ≠ 掉线：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 是缺站点 CODE 头，后端归 502，不提示重新扫码。
 *  3. 本页无写操作（创建 / 编辑 / 删除模版一律不做）。
 *
 * 真机现状（账号 2217970177348）：运费模版数 0，`query` 返回 `data:[]` + `totalCount:0`。
 */

import { useState } from 'react'
import { api } from '../api'
import { AsyncBox, Notice } from '../components'
import { formatTime } from '../format'
import { useAsync } from '../hooks/useAsync'
import { IconRefresh, IconSearch } from '../icons'
import type { Account } from '../types'
import './shop.css'

const PAGE_SIZE = 20

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

export function ItemTemplatesPage() {
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [picked, setPicked] = useState('')
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [appliedName, setAppliedName] = useState('')

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

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-main">
          <h2 className="page-title">运费模版</h2>
          <p className="page-desc">
            官方「运费模版管理」。只读 —— 平台没有「发布模板」功能，创建 / 编辑模版请去官方卖家工作台。
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
          emptyDesc="平台明确返回了空列表。要新建运费模版请去官方卖家工作台的「运费模版管理」。"
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
                  </tr>
                </thead>
                <tbody>
                  {templates.map((row, idx) => {
                    const others = rawOthers(row)
                    return (
                      <tr key={row.template_id || idx}>
                        <td>{row.template_name || '-'}</td>
                        <td className="sh-mono">{row.template_id || '-'}</td>
                        <td className="sh-extra">
                          {others ? <span title={others}>{others}</span> : '-'}
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
            只读页：不会创建 / 编辑 / 删除任何运费模版，也不向平台写入。数据更新于
            {data.data?.fetched_at ? formatTime(data.data.fetched_at) : ' -'}
          </div>
        </AsyncBox>
      </div>
    </div>
  )
}
