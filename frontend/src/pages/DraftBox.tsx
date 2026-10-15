/**
 * 草稿箱：官方草稿箱的只读列表 + 详情 + 删除（含批量删除）。
 *
 * 为什么挂在「商品」页第三 tab：官方客户端没有独立的「草稿箱」菜单，它藏在商品发布流程里，
 * 所以这里不新增侧栏项，只作为商品页的一个 tab。
 *
 * 两条硬规矩（来自审计结论 docs/audit/items.md）：
 * 1. 拿不到的值显示 `-`，**不补 0**。price / max_price / quantity / created_at 都可能是 null，
 *    把 null 显示成 0 会让用户以为「这草稿真的卖 0 块」，比留空危险得多。
 * 2. `fields` 是平台原样 payload，**键名没有任何证据**（契约明写），所以只做原样展示：
 *    不翻译键名、不猜字段含义、不把未知键藏起来。
 */

import { useEffect, useState } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Modal, Notice, Pager, useToast } from '../components'
import { IconBox, IconEye, IconRefresh, IconTrash } from '../icons'
import { useAsync } from '../hooks/useAsync'
import { formatMoney, formatTime, prettyJson, truncate } from '../format'
import './drafts.css'

/* ------------------------------------------------------------------ 契约类型 */

/** GET /api/drafts/{account_id} 的 items 条目。字段可能为 null 是契约明写的。 */
interface DraftRow {
  draft_id: string
  title?: string | null
  image_url?: string | null
  price?: number | null
  max_price?: number | null
  quantity?: number | null
  created_at?: string | null
}

interface DraftListResult {
  items: DraftRow[]
  total: number
  page: number
  size: number
  pages: number
  source?: string
  fetched_at?: string | null
  warnings?: unknown[]
}

/** GET /api/drafts/{account_id}/{draft_id}：fields 是平台原样 payload，也可能是 null。 */
interface DraftDetail {
  draft_id: string
  fields: Record<string, unknown> | null
  raw?: unknown
  fetched_at?: string | null
  warnings?: unknown[]
}

/** POST .../{draft_id}/delete 的返回。deleted=false 表示**没真的删掉**，要如实说。 */
interface DraftDeleteResult {
  draft_id: string
  deleted?: boolean
}

/** POST .../batch-delete 的返回。 */
interface DraftBatchResult {
  total: number
  ok: number
  failed?: Array<{ id?: string; code?: string; message?: string }>
  suc_count?: number
  fail_count?: number
  warnings?: unknown[]
}

/* -------------------------------------------------------------------- 写出口 */

/**
 * 全文件**唯一**写出口：`confirm: true` 是字面量，写在 `...body` 之后，
 * 所以调用方即使传 `confirm: false` 也覆盖不了 —— 二次确认不可能被绕过。
 */
function postDraftWrite<T>(path: string, body: Record<string, unknown>): Promise<T> {
  return api.post<T>(path, { ...body, confirm: true })
}

/* -------------------------------------------------------------------- 小工具 */

/** 展示后端 warnings 的任意形状：字符串 / {id,code,message} / 其他对象。 */
function warningText(entry: unknown): string {
  if (entry === null || entry === undefined) return ''
  if (typeof entry === 'string') return entry
  if (typeof entry === 'object') {
    const record = entry as Record<string, unknown>
    const parts = [record.id, record.code, record.message].map((v) => (typeof v === 'string' ? v : ''))
    const text = parts.filter(Boolean).join(' · ')
    return text || prettyJson(record)
  }
  return String(entry)
}

function warningList(warnings?: unknown[]): string[] {
  if (!Array.isArray(warnings)) return []
  return warnings.map(warningText).filter(Boolean)
}

/** 值原样：null 就显示 `null`（不显示 `-`，因为契约里 null 本身是有意义的信息）。 */
function valueText(value: unknown): string {
  if (value === null) return 'null'
  if (value === undefined) return 'undefined'
  if (typeof value === 'string') return value
  if (typeof value === 'object') return prettyJson(value)
  return String(value)
}

/** 价格：`max_price` 有值且与 price 不同才显示区间。任一为空则只显示有的那个，都没有就 `-`。 */
function priceText(row: DraftRow): string {
  const low = row.price === null || row.price === undefined ? null : Number(row.price)
  const high = row.max_price === null || row.max_price === undefined ? null : Number(row.max_price)
  if (low === null && high === null) return '-'
  if (low !== null && high !== null && !Number.isNaN(low) && !Number.isNaN(high) && low !== high) {
    return `${formatMoney(low)}～${formatMoney(high)}`
  }
  const only = low !== null ? low : high
  if (only === null || Number.isNaN(only)) return '-'
  return formatMoney(only)
}

/** 库存：null 一律 `-`，绝不 `Number(null) || 0` 补成 0。 */
function quantityText(row: DraftRow): string {
  if (row.quantity === null || row.quantity === undefined) return '-'
  return String(row.quantity)
}

/* ---------------------------------------------------------------------- 图片 */

function DraftThumb({ url, alt }: { url?: string | null; alt: string }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [url])
  const usable = Boolean(url) && !failed
  if (!usable) {
    return (
      <span className="dr-thumb dr-thumb-empty" title={url ? '图片加载失败' : '平台没有返回图片'}>
        <IconBox size={16} />
      </span>
    )
  }
  return (
    <img
      className="dr-thumb"
      src={String(url)}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  )
}

/* ------------------------------------------------------------------ 结果面板 */

/** 批量删除的失败项逐条显示：一条都不合并，否则用户不知道哪个草稿还在。 */
function BatchResultPanel({ report, onClose }: { report: DraftBatchResult; onClose: () => void }) {
  const failed = (report.failed || []).filter(Boolean)
  const warnings = warningList(report.warnings)
  return (
    <div className="dr-result">
      <div className="dr-result-head">
        <strong>
          批量删除结果：成功 {report.ok ?? report.suc_count ?? 0} / 失败{' '}
          {failed.length || report.fail_count || 0}（共 {report.total}）
        </strong>
        <button className="btn sm ghost" onClick={onClose}>
          关闭
        </button>
      </div>
      {failed.length > 0 && (
        <div className="dr-result-group">
          <div className="dr-result-title danger">下面这些「没有删掉」，平台上的草稿还在：</div>
          <ul className="dr-result-list">
            {failed.map((entry, index) => (
              <li key={`${entry.id || 'row'}-${index}`}>
                <span className="dr-result-id mono">{entry.id || '(无 id)'}</span>
                <span className="dr-result-code danger">{entry.code || 'unknown'}</span>
                <span className="dr-result-msg">{entry.message || '(后端没给原因)'}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="dr-result-group">
          <div className="dr-result-title warn">平台/后端提醒：</div>
          <ul className="dr-result-list">
            {warnings.map((text, index) => (
              <li key={index}>
                <span className="dr-result-msg">{text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ 详情弹窗 */

function DraftDetailModal({
  accountId,
  draft,
  onClose,
}: {
  accountId: string
  draft: DraftRow
  onClose: () => void
}) {
  const detail = useAsync<DraftDetail>(
    () => api.get<DraftDetail>(`/api/drafts/${encodeURIComponent(accountId)}/${encodeURIComponent(draft.draft_id)}`),
    [accountId, draft.draft_id],
  )
  const fields = detail.data?.fields ?? null
  const entries = fields && typeof fields === 'object' ? Object.entries(fields) : []
  const warnings = warningList(detail.data?.warnings)

  return (
    <Modal title={`草稿详情 ${draft.title ? truncate(String(draft.title), 24) : draft.draft_id}`} onClose={onClose}>
      <AsyncBox
        loading={detail.loading}
        error={detail.error}
        notReady={detail.notReady}
        onRetry={detail.reload}
        empty={false}
      >
        <div className="dr-detail">
          <div className="dr-detail-meta mono dim">
            草稿 ID {draft.draft_id}
            {detail.data?.fetched_at ? ` · 抓取于 ${formatTime(detail.data.fetched_at)}` : ''}
          </div>

          {warnings.length > 0 && (
            <div className="dr-warn">
              <div className="dr-warn-title">抓取时的提醒：</div>
              <ul>
                {warnings.map((text, index) => (
                  <li key={index}>{text}</li>
                ))}
              </ul>
            </div>
          )}

          {fields === null ? (
            <Notice kind="warn">
              平台这一次没有返回草稿内容（契约里它是 null，不是空对象）—— 说明这次抓取只拿到了草稿的列表信息，
              没有拿到它的字段明细。草稿本身还在，可以稍后重试；不要把它当成「空草稿」。
            </Notice>
          ) : (
            <>
              <div className="dr-detail-title">
                平台返回的原始字段（键名原样照抄，未翻译、未猜测含义）：
              </div>
              <div className="table-wrap">
                <table className="dr-table dr-kv">
                  <thead>
                    <tr>
                      <th>键</th>
                      <th>值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map(([key, value]) => (
                      <tr key={key}>
                        <td className="mono dr-key">{key}</td>
                        <td className={typeof value === 'object' && value !== null ? 'dr-val-block' : 'dr-val'}>
                          {typeof value === 'object' && value !== null ? (
                            <pre className="mono dr-pre">{prettyJson(value)}</pre>
                          ) : (
                            valueText(value)
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {entries.length === 0 && (
                <div className="dim dr-detail-meta">
                  平台返回的 fields 是个空对象（没有任何键），这是平台自己的返回，不是页面丢的。
                </div>
              )}
              <details className="dr-raw">
                <summary>完整 JSON（原样）</summary>
                <pre className="mono dr-pre">{prettyJson(fields)}</pre>
              </details>
            </>
          )}

          {detail.data?.raw !== undefined && detail.data?.raw !== null && (
            <details className="dr-raw">
              <summary>raw 原始响应（原样，供排查用）</summary>
              <pre className="mono dr-pre">{prettyJson(detail.data.raw)}</pre>
            </details>
          )}
        </div>
      </AsyncBox>
    </Modal>
  )
}

/* -------------------------------------------------------------------- 主组件 */

interface PendingDelete {
  title: string
  message: string
  confirmText: string
  run: () => Promise<void>
  error?: string
}

export function DraftBox({ accountId }: { accountId: string }) {
  const toast = useToast()
  const [page, setPage] = useState(1)
  const [size, setSize] = useState(20)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [detailFor, setDetailFor] = useState<DraftRow | null>(null)
  const [pending, setPending] = useState<PendingDelete | null>(null)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState<DraftBatchResult | null>(null)

  const drafts = useAsync<DraftListResult>(
    () =>
      api.get<DraftListResult>(`/api/drafts/${encodeURIComponent(accountId)}`, { page, size }),
    [accountId, page, size],
    Boolean(accountId),
    ['items.updated', 'accounts.changed'],
  )

  // 换账号：页码回第一页、勾选清空、上一轮的结果面板也得收掉，否则会把 A 账号的
  // 「已选 3 个」带到 B 账号上，用户一点批量删除就删错账号的草稿。
  useEffect(() => {
    setPage(1)
    setSelected(new Set())
    setReport(null)
  }, [accountId])

  const rows = drafts.data?.items || []
  const total = drafts.data?.total ?? rows.length
  const pages = drafts.data?.pages ?? 1
  const listWarnings = warningList(drafts.data?.warnings)
  const selectedRows = rows.filter((row) => selected.has(String(row.draft_id)))
  const allSelected = rows.length > 0 && selectedRows.length === rows.length

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(rows.map((row) => String(row.draft_id))))
  }

  function askDeleteOne(row: DraftRow) {
    const label = row.title ? `「${truncate(String(row.title), 20)}」` : `草稿 ${row.draft_id}`
    setPending({
      title: '删除草稿',
      confirmText: '确认删除',
      message: `会真的删除闲鱼上的草稿，不可撤销。\n\n${label}\n草稿 ID：${row.draft_id}`,
      run: async () => {
        const result = await postDraftWrite<DraftDeleteResult>(
          `/api/drafts/${encodeURIComponent(accountId)}/${encodeURIComponent(row.draft_id)}/delete`,
          {},
        )
        if (result && result.deleted === false) {
          // 后端明确说「没删掉」就不许报成功 —— 这正是历史上「本地改了就说成功」的坑。
          throw new Error(`后端返回 deleted=false：草稿 ${row.draft_id} 没有被真的删除，请刷新后再看`)
        }
        toast.success(`已删除草稿 ${row.draft_id}`)
        setSelected((prev) => {
          const next = new Set(prev)
          next.delete(String(row.draft_id))
          return next
        })
        drafts.reload()
      },
    })
  }

  function askDeleteBatch() {
    const ids = selectedRows.map((row) => String(row.draft_id))
    if (ids.length === 0) return
    setPending({
      title: `批量删除 ${ids.length} 个草稿`,
      confirmText: `删除这 ${ids.length} 个`,
      message:
        `会真的删除闲鱼上的草稿，不可撤销。\n\n共 ${ids.length} 个：\n` +
        ids.map((id) => `· ${id}`).join('\n'),
      run: async () => {
        const result = await postDraftWrite<DraftBatchResult>(
          `/api/drafts/${encodeURIComponent(accountId)}/batch-delete`,
          { draft_ids: ids },
        )
        const ok = result?.ok ?? result?.suc_count ?? 0
        const failed = (result?.failed || []).length || result?.fail_count || 0
        setReport(result)
        if (failed > 0) {
          toast.error(`删除完成：成功 ${ok} 个，失败 ${failed} 个 —— 失败的那些平台上的草稿还在，见下方明细`)
        } else {
          toast.success(`已删除 ${ok} 个草稿`)
        }
        setSelected(new Set())
        drafts.reload()
      },
    })
  }

  if (!accountId) {
    return (
      <Notice kind="info">
        草稿箱属于某个具体账号：请先在顶部选择账号，再回到这里查看它的草稿。
      </Notice>
    )
  }

  return (
    <div className="dr-wrap">
      <div className="toolbar dr-toolbar">
        <button className="btn sm" onClick={drafts.reload} disabled={drafts.loading}>
          <IconRefresh size={14} />
          刷新
        </button>
        <select
          className="dr-select"
          value={size}
          onChange={(event) => {
            setSize(Number(event.target.value))
            setPage(1)
            setSelected(new Set())
          }}
        >
          <option value={20}>每页 20</option>
          <option value={50}>每页 50</option>
          <option value={100}>每页 100</option>
        </select>
        <button className="btn sm danger" onClick={askDeleteBatch} disabled={selectedRows.length === 0}>
          <IconTrash size={14} />
          批量删除{selectedRows.length > 0 ? `（${selectedRows.length}）` : ''}
        </button>
        <span className="it-spacer" />
        <span className="dim dr-count">
          共 {total} 个草稿{drafts.data?.source ? ` · 来源 ${drafts.data.source}` : ''}
        </span>
      </div>

      {listWarnings.length > 0 && (
        <div className="dr-warn">
          <div className="dr-warn-title">后端/平台在这次拉取里的提醒：</div>
          <ul>
            {listWarnings.map((text, index) => (
              <li key={index}>{text}</li>
            ))}
          </ul>
        </div>
      )}

      {report && <BatchResultPanel report={report} onClose={() => setReport(null)} />}

      <AsyncBox
        loading={drafts.loading}
        error={drafts.error}
        notReady={drafts.notReady}
        onRetry={drafts.reload}
        empty={rows.length === 0}
        emptyIcon="box"
        emptyTitle="没有草稿"
        emptyDesc="这个账号的官方草稿箱里目前是空的；也可能是平台没返回草稿列表（见上方的提醒）。"
      >
        <div className="table-wrap">
          <table className="dr-table">
            <thead>
              <tr>
                <th className="dr-col-check">
                  <input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="全选" />
                </th>
                <th className="dr-col-img">图片</th>
                <th>标题</th>
                <th className="dr-col-id">草稿 ID</th>
                <th className="dr-col-price">价格</th>
                <th className="dr-col-qty">库存</th>
                <th className="dr-col-time">创建时间</th>
                <th className="dr-col-acts">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const id = String(row.draft_id)
                return (
                  <tr key={id}>
                    <td className="dr-col-check">
                      <input
                        type="checkbox"
                        checked={selected.has(id)}
                        onChange={() => toggle(id)}
                        aria-label={`选择草稿 ${id}`}
                      />
                    </td>
                    <td className="dr-col-img">
                      <DraftThumb url={row.image_url} alt={row.title ? String(row.title) : `草稿 ${id}`} />
                    </td>
                    <td className="dr-title" title={row.title ? String(row.title) : ''}>
                      {row.title ? truncate(String(row.title), 40) : <span className="dim">-</span>}
                    </td>
                    <td className="mono dr-col-id">{id}</td>
                    <td className="num nowrap">{priceText(row)}</td>
                    <td className="num nowrap">{quantityText(row)}</td>
                    <td className="nowrap">{formatTime(row.created_at)}</td>
                    <td className="dr-col-acts">
                      <div className="dr-acts">
                        <button className="btn sm ghost" onClick={() => setDetailFor(row)}>
                          <IconEye size={14} />
                          详情
                        </button>
                        <button className="btn sm danger" onClick={() => askDeleteOne(row)}>
                          <IconTrash size={14} />
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

        <Pager
          page={page}
          pageSize={size}
          total={pages > 0 ? pages * size : undefined}
          hasNext={pages > 0 && page < pages}
          onPage={setPage}
          note={
            <>
              第 {drafts.data?.page ?? page} / {Math.max(1, pages)} 页
              {drafts.data?.fetched_at && (
                <span className="dim dr-fetched"> 抓取于 {formatTime(drafts.data.fetched_at)}</span>
              )}
            </>
          }
        />
      </AsyncBox>

      {detailFor && (
        <DraftDetailModal accountId={accountId} draft={detailFor} onClose={() => setDetailFor(null)} />
      )}

      {pending && (
        <ConfirmDialog
          title={pending.title}
          danger
          busy={busy}
          confirmText={pending.confirmText}
          message={
            <>
              {pending.message.split('\n').map((line, index) => (
                <div key={index}>{line || '\u00a0'}</div>
              ))}
              {pending.error && <div className="dr-write-error">失败：{pending.error}</div>}
            </>
          }
          onCancel={() => setPending(null)}
          onConfirm={async () => {
            setBusy(true)
            try {
              await pending.run()
              setPending(null)
            } catch (err) {
              // 失败不关弹窗：后端原文（掉线 / 平台拒绝）留在框里，用户可以照着处理。
              const text = errorText(err)
              setPending((prev) => (prev ? { ...prev, error: text } : prev))
              toast.error(text)
            } finally {
              setBusy(false)
            }
          }}
        />
      )}

      <div className="dr-hint dim">
        草稿箱只提供「看列表 / 看详情 / 删除」：官方草稿不能从这里改价或直接发布，改内容请去闲鱼 App；
        这里的「详情」只展示平台返回的原始字段。
        {selectedRows.length > 0 && (
          <>
            {' '}
            当前已勾选 <Badge tone="warn">{selectedRows.length}</Badge> 个（只作用于本页看得见的草稿）。
          </>
        )}
      </div>
    </div>
  )
}
