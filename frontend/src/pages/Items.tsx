/** 商品：列表 + 单条编辑 + 批量选择后批量改价/改标题/改描述 + 上下架 + 发布。 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type CSSProperties,
  type Dispatch,
  type DragEvent,
  type ReactNode,
  type SetStateAction,
} from 'react'
import { api, errorText, getToken, uploadImage, type ImageInput } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatMoney, formatTime, itemStatusLabel, parseImages, truncate } from '../format'
import type { Account, BatchResult, Item, ItemListResult, OperationLog } from '../types'

/**
 * 把批量接口的逐条结果翻成一句人话。
 *
 * 后端约定（`backend/app/services/items.py` 的 `_batch_summary`）：
 *   ok / synced = 本地已改 **且** 已推到闲鱼 —— 协议层没就绪时**恒为 0**；
 *   warnings    = 本地改了但没推出去（**不能当成功吞掉**，这就是本次修的缺陷）；
 *   failed      = 完全没改（商品不存在 / 字段非法 / 跨账号）。
 * 后端对批量结果是 200 + 逐条成败，HTTP 200 不代表改成功，必须看 body。
 * `label` 只影响提示前缀（批量 / 单个），单条接口返回的是同样形状的逐条汇总。
 */
function showBatchNotice(
  toast: { success: (text: string) => void; error: (text: string) => void; info: (text: string) => void },
  result: BatchResult | undefined,
  verb: string,
  label = '批量',
): void {
  const ok = Number(result?.ok) || 0
  const warnings = result?.warnings ?? []
  const failed = result?.failed ?? []
  const noop = Number(result?.noop) || 0
  const parts: string[] = []
  if (ok > 0) parts.push(`已${verb}并同步 ${ok} 个`)
  if (warnings.length > 0) {
    parts.push(`${warnings.length} 个仅本地生效（未同步到闲鱼：${warnings[0]?.message ?? '未知原因'}）`)
  }
  if (failed.length > 0) parts.push(`${failed.length} 个没成功（${failed[0]?.message ?? '未知原因'}）`)
  // `noop` 是后端明确「这次没有实际改动」（例如只传了表示不改图的空图片列表）：
  // 不能并进 `ok` 报「已修改并同步」，那是在骗用户。
  if (noop > 0) parts.push(`${noop} 个没有需要改动的内容`)
  const text = `${label}${verb}：${parts.join('；') || `没有可处理的商品（共 ${Number(result?.total) || 0} 个）`}`
  if (failed.length > 0) toast.error(text)
  else if (warnings.length > 0) toast.info(text)
  else if (ok > 0) toast.success(text)
  else toast.info(text)
}

/** 把多次单条调用（批量上下架是逐条打接口）的返回合并成一个批量结果。 */
function mergeBatch(): BatchResult & { warnings: NonNullable<BatchResult['warnings']> } {
  return { total: 0, ok: 0, synced: 0, local_only: 0, warnings: [], failed: [] }
}

/**
 * 单条 `/edit` 的返回形状与批量**不一样**：
 *   `{ item, synced, updated_fields, warning? }`（`backend/app/services/items.py` 的 `update_item`）
 * 而批量形状是 `{ total, ok, warnings, failed }`。
 * 直接把它当 `BatchResult` 读会 `ok=undefined→0`、`warnings=undefined→[]`，于是
 * `warning` 被吞掉，还错报「商品修改：没有可处理的商品（共 0 个）」。
 * 这里折成批量形状：`synced=false` 时把 `warning` 记成一条 `not_pushed`。
 */
function singleEditAsBatch(
  result:
    | { item?: Item; synced?: boolean; warning?: string; updated_fields?: string[]; noop?: boolean }
    | undefined,
): BatchResult & { warnings: NonNullable<BatchResult['warnings']> } {
  const merged = mergeBatch()
  merged.total = 1
  if (result?.noop) {
    // 后端 task-26 起可以返回 `noop: true`（这次没有任何实际改动）。
    // 不能落进下面的 synced 分支 —— 那会报「已修改并同步 1 个」，而实际一个字都没改。
    merged.noop = 1
    return merged
  }
  if (result?.synced) {
    merged.ok = 1
    merged.synced = 1
    return merged
  }
  merged.warnings.push({
    id: result?.item?.item_id ?? '',
    code: 'not_pushed',
    message: result?.warning || '改动仅本地生效，未同步到闲鱼',
  })
  merged.local_only = 1
  return merged
}

/* ---------------- 列表缩略图 / 库存口径 ---------------- */

/** 列表缩略图边长（固定尺寸，有没有图片行高都一样，不会因为缺图塌陷）。 */
const LIST_THUMB_SIZE = 46

const listThumbStyle: CSSProperties = {
  width: LIST_THUMB_SIZE,
  height: LIST_THUMB_SIZE,
  flex: 'none',
  display: 'block',
  borderRadius: 6,
  objectFit: 'cover',
  background: 'rgba(255,255,255,.05)',
  border: '1px solid rgba(255,255,255,.12)',
}

/**
 * 商品缩略图（主图 = `images[0]`）。
 * 无图或加载失败都退回同尺寸占位块 —— 绝不显示浏览器的破图图标。
 */
function ItemThumb({ url, alt }: { url?: string; alt: string }) {
  const [failed, setFailed] = useState(false)
  if (!url || failed) {
    return (
      <span
        style={{
          ...listThumbStyle,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 18,
          color: 'rgba(255,255,255,.26)',
        }}
        title={failed ? '图片加载失败' : '没有图片'}
      >
        🖼
      </span>
    )
  }
  return (
    <img
      src={url}
      alt={alt}
      loading="lazy"
      style={listThumbStyle}
      onError={() => setFailed(true)}
    />
  )
}

/**
 * 库存显示口径：后端 `stock = -1` 表示**未知/未同步**（不是「缺货」，也不能当 0），
 * 必须显示成 `-`；`>= 0` 才是平台真实库存。
 */
function stockText(stock: number | null | undefined): { text: string; unknown: boolean } {
  if (stock === null || stock === undefined) return { text: '-', unknown: true }
  const value = Number(stock)
  if (!Number.isFinite(value) || value < 0) return { text: '-', unknown: true }
  return { text: String(value), unknown: false }
}

/**
 * `items.images` → 有序 URL 列表。
 *
 * 约定是库里**只存 URL 字符串**（对象形状只是编辑请求的传输形状、不该落库）。这里
 * 仍然多兜一层 `{url}` 对象：万一哪天库列里混进了对象，`String(obj)` 会变成
 * `"[object Object]"`，缩略图和 `<img src>` 会一起废掉 —— 三行容错换掉一次回归。
 */
function itemImageUrls(images?: unknown): string[] {
  if (!Array.isArray(images)) {
    return parseImages(images as string | string[] | null | undefined)
  }
  return images
    .map((entry) => {
      if (typeof entry === 'string') return entry.trim()
      if (entry && typeof entry === 'object') {
        const url = (entry as { url?: unknown }).url
        return typeof url === 'string' ? url.trim() : ''
      }
      return ''
    })
    .filter(Boolean)
}

export function ItemsPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  const [accountId, setAccountId] = useState('')
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<Item | null>(null)
  const [publishOpen, setPublishOpen] = useState(false)
  const [batchOpen, setBatchOpen] = useState(false)
  const [logsOpen, setLogsOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirmAction, setConfirmAction] = useState<{ title: string; message: ReactNode; run: () => Promise<void> } | null>(null)

  const accountList = accounts.data ?? []

  const items = useAsync<ItemListResult>(
    () =>
      api.get<ItemListResult>('/api/items', {
        account_id: accountId || undefined,
        status: status || undefined,
        q: query || undefined,
        limit: 500,
      }),
    [accountId, status, query],
    true,
    // 发布/编辑/上下架/同步成功后后端会发 items.updated，静默重拉列表
    ['items.updated'],
  )

  const list = items.data?.items ?? []

  const selectedItems = useMemo(() => list.filter((item) => selected.has(item.item_id)), [list, selected])
  const selectedAccounts = useMemo(() => new Set(selectedItems.map((item) => item.account_id)), [selectedItems])
  const mixedAccounts = selectedAccounts.size > 1

  const allChecked = list.length > 0 && list.every((item) => selected.has(item.item_id))

  const toggleAll = () => {
    setSelected(allChecked ? new Set() : new Set(list.map((item) => item.item_id)))
  }

  const toggleOne = (itemId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  const sync = async () => {
    if (!accountId) {
      toast.info('请先在上方选择要同步的账号')
      return
    }
    setBusy(true)
    try {
      // 同步是同步阻塞完成的（不是后台任务），返回值里就有条数；
      // 再用 items.updated 事件触发静默重拉，不靠「稍后自己刷新」蒙用户。
      const result = await api.post<{ count?: number }>(`/api/items/${encodeURIComponent(accountId)}/sync`)
      toast.success(`同步完成，拉取到 ${Number(result?.count) || 0} 个商品`)
      items.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const setShelf = (item: Item, action: 'delist' | 'relist') => {
    setConfirmAction({
      title: action === 'delist' ? '下架商品' : '重新上架',
      message: (
        <>
          确定要{action === 'delist' ? '下架' : '上架'} <strong>{truncate(item.title || item.item_id, 40)}</strong> 吗？
        </>
      ),
      run: async () => {
        const verb = action === 'delist' ? '下架' : '上架'
        // 单条上下架也返回 {ok, warnings, failed}：协议层没推成功时是个 warning，
        // 直接报「已下架」会让用户以为闲鱼上真的下架了。
        const result = await api.post<BatchResult>(
          `/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}/${action}`,
        )
        showBatchNotice(toast, result, verb, '商品')
        items.reload()
      },
    })
  }

  const batchShelf = (action: 'delist' | 'relist') => {
    const targets = selectedItems
    setConfirmAction({
      title: action === 'delist' ? '批量下架' : '批量上架',
      message: (
        <>
          将对 <strong>{targets.length}</strong> 个商品执行{action === 'delist' ? '下架' : '上架'}操作。
          {targets.length > 5 && <div className="dim" style={{ marginTop: 6 }}>批量操作会逐个请求，可能需要几秒。</div>}
        </>
      ),
      run: async () => {
        const verb = action === 'delist' ? '下架' : '上架'
        const merged = mergeBatch()
        for (const item of targets) {
          merged.total += 1
          try {
            // 单条接口也返回逐条汇总（ok/warnings/failed）：协议层没推成功时 ok=0、
            // warning 在 warnings 里 —— HTTP 200 只代表请求到了，不代表真的推到闲鱼。
            const result = await api.post<BatchResult>(
              `/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}/${action}`,
            )
            merged.ok += Number(result?.ok) || 0
            merged.warnings.push(...(result?.warnings ?? []))
            merged.failed.push(...(result?.failed ?? []))
          } catch (err) {
            merged.failed.push({ id: item.item_id, message: errorText(err) })
          }
        }
        merged.synced = merged.ok
        merged.local_only = merged.warnings.length
        showBatchNotice(toast, merged, verb)
        setSelected(new Set())
        items.reload()
      },
    })
  }

  return (
    <div>
      <div className="toolbar">
        <select value={accountId} onChange={(e) => { setAccountId(e.target.value); setSelected(new Set()) }} style={{ width: 190 }}>
          <option value="">全部账号</option>
          {accountList.map((account) => (
            <option key={account.id} value={account.id}>
              {account.nickname || account.id}
            </option>
          ))}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 130 }}>
          <option value="">全部状态</option>
          <option value="onsale">在售</option>
          <option value="offline">已下架</option>
          <option value="sold">已售出</option>
        </select>
        <input
          type="search"
          placeholder="搜索标题 / 商品 ID"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ width: 200 }}
        />
        <button className="btn" onClick={items.reload}>
          刷新
        </button>
        <span className="spacer" />
        <button className="btn" onClick={sync} disabled={busy || !accountId} title={!accountId ? '请先选择账号' : '从闲鱼拉取在售商品'}>
          同步商品
        </button>
        <button className="btn" onClick={() => setLogsOpen(true)} disabled={!accountId}>
          操作日志
        </button>
        <button className="btn primary" onClick={() => setPublishOpen(true)} disabled={!accountId} title={!accountId ? '请先选择账号' : ''}>
          ＋ 发布商品
        </button>
      </div>

      {selected.size > 0 && (
        <div className="card" style={{ borderColor: 'var(--accent)' }}>
          <div className="row wrap">
            <strong>已选 {selected.size} 个商品</strong>
            {mixedAccounts && <Badge tone="warn">跨账号，批量接口只支持单账号</Badge>}
            <span className="spacer" />
            <button className="btn sm" onClick={() => setSelected(new Set())}>
              取消选择
            </button>
            <button className="btn sm primary" disabled={mixedAccounts} onClick={() => setBatchOpen(true)}>
              批量修改
            </button>
            <button className="btn sm" disabled={mixedAccounts} onClick={() => batchShelf('relist')}>
              批量上架
            </button>
            <button className="btn sm" disabled={mixedAccounts} onClick={() => batchShelf('delist')}>
              批量下架
            </button>
          </div>
          {mixedAccounts && (
            <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>
              提示：批量接口路径含 account_id，一次只能对一个账号的商品批量操作。请先在左上角筛选到单个账号。
            </div>
          )}
        </div>
      )}

      <div className="card">
        <h3>
          商品列表
          <span className="hint">共 {list.length} 条</span>
        </h3>
        <AsyncBox
          loading={items.loading}
          error={items.error}
          notReady={items.notReady}
          empty={list.length === 0}
          emptyIcon="📦"
          emptyTitle="没有商品"
          emptyDesc={accountId ? '点「同步商品」从闲鱼拉取在售商品入库。' : '先选择一个账号，或点「同步商品」。'}
          onRetry={items.reload}
        >
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 36 }}>
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} />
                  </th>
                  <th>商品</th>
                  <th className="nowrap">价格</th>
                  <th className="nowrap">库存</th>
                  <th className="nowrap">状态</th>
                  <th className="nowrap">AI</th>
                  <th className="nowrap">更新时间</th>
                  <th className="nowrap">操作</th>
                </tr>
              </thead>
              <tbody>
                {list.map((item) => {
                  const state = itemStatusLabel(item.status)
                  const checked = selected.has(item.item_id)
                  const imageList = itemImageUrls(item.images)
                  const stock = stockText(item.stock)
                  return (
                    <tr key={`${item.account_id}-${item.item_id}`} className={checked ? 'selected' : ''}>
                      <td>
                        <input type="checkbox" checked={checked} onChange={() => toggleOne(item.item_id)} />
                      </td>
                      <td style={{ maxWidth: 340 }}>
                        <div className="row" style={{ gap: 10, alignItems: 'flex-start', flexWrap: 'nowrap' }}>
                          <ItemThumb url={imageList[0]} alt={item.title || item.item_id} />
                          <div style={{ minWidth: 0 }}>
                            <div className="ellipsis" title={item.title}>
                              {item.title || '(无标题)'}
                            </div>
                            <div className="mono dim" style={{ fontSize: 11 }}>
                              {item.item_id} · {item.account_id}
                            </div>
                            {imageList.length > 1 && (
                              <div className="dim" style={{ fontSize: 11 }}>
                                共 {imageList.length} 张图
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="num nowrap">
                        {formatMoney(item.price)}
                        {item.original_price ? (
                          <div className="dim" style={{ fontSize: 11, textDecoration: 'line-through' }}>
                            {formatMoney(item.original_price)}
                          </div>
                        ) : null}
                      </td>
                      <td className="num nowrap" title={stock.unknown ? '未同步（平台没返回库存）' : ''}>
                        {stock.text}
                      </td>
                      <td className="nowrap">
                        <Badge tone={state.tone}>{state.text}</Badge>
                      </td>
                      <td className="nowrap">
                        <Badge tone={bool(item.ai_enabled) ? 'ok' : 'muted'}>{bool(item.ai_enabled) ? '开' : '关'}</Badge>
                      </td>
                      <td className="nowrap dim" style={{ fontSize: 12 }}>
                        {formatTime(item.updated_at)}
                      </td>
                      <td className="nowrap">
                        <div className="row" style={{ gap: 5 }}>
                          <button className="btn sm" onClick={() => setEditing(item)}>
                            编辑
                          </button>
                          {itemStatusLabel(item.status).text === '在售' ? (
                            <button className="btn sm ghost" onClick={() => setShelf(item, 'delist')}>
                              下架
                            </button>
                          ) : (
                            <button className="btn sm ghost" onClick={() => setShelf(item, 'relist')}>
                              上架
                            </button>
                          )}
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

      {editing && (
        <EditItemModal
          item={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            items.reload()
          }}
        />
      )}

      {batchOpen && (
        <BatchEditModal
          items={selectedItems}
          onClose={() => setBatchOpen(false)}
          onDone={() => {
            setBatchOpen(false)
            setSelected(new Set())
            items.reload()
          }}
        />
      )}

      {publishOpen && (
        <PublishItemModal
          accountId={accountId}
          onClose={() => setPublishOpen(false)}
          onDone={() => {
            setPublishOpen(false)
            items.reload()
          }}
        />
      )}

      {logsOpen && <OperationLogsModal accountId={accountId} onClose={() => setLogsOpen(false)} />}

      {confirmAction && (
        <ConfirmDialog
          title={confirmAction.title}
          message={confirmAction.message}
          confirmText="确认执行"
          danger
          busy={busy}
          onCancel={() => setConfirmAction(null)}
          onConfirm={async () => {
            setBusy(true)
            try {
              await confirmAction.run()
              setConfirmAction(null)
            } catch (err) {
              toast.error(errorText(err))
            } finally {
              setBusy(false)
            }
          }}
        />
      )}
    </div>
  )
}

/* ---------------- 图片上传区（拖拽 / 点击 / 粘贴 + 排序） ---------------- */

/** 刚上传图片的真实尺寸，按 URL 记（URL 在数组里是唯一键）。 */
type ImageSizes = Record<string, { width: number; height: number }>

/**
 * 有序 URL 列表 + 已知尺寸 → 提交给后端的 `images` 数组。
 *
 * **有尺寸的传对象，没尺寸的传字符串**：
 * 平台 `imageInfoDOList` 的 `widthSize`/`heightSize` 要真实尺寸；上传接口给了就带上，
 * 库里读出来的历史图没有尺寸 —— 那就交给协议层填占位 `'0'`（它明确拒绝硬编码 1254，
 * 因为那是对每一张图撒谎），前端不编。
 */
function imagePayload(urls: string[], sizes: ImageSizes): ImageInput[] {
  return urls.map((url) => {
    const size = sizes[url]
    return size ? { url, width: size.width, height: size.height } : url
  })
}

const IMAGE_CELL_SIZE = 96
const imageCellStyle: CSSProperties = {
  position: 'relative',
  width: IMAGE_CELL_SIZE,
  height: IMAGE_CELL_SIZE,
  flex: 'none',
  borderRadius: 8,
  overflow: 'hidden',
  border: '1px solid rgba(255,255,255,.14)',
  background: 'rgba(255,255,255,.04)',
  cursor: 'grab',
}

const imageCellImgStyle: CSSProperties = { width: '100%', height: '100%', objectFit: 'cover', display: 'block' }

const thumbDeleteStyle: CSSProperties = {
  position: 'absolute',
  top: 4,
  right: 4,
  width: 20,
  height: 20,
  padding: 0,
  lineHeight: '18px',
  borderRadius: '50%',
  border: 'none',
  cursor: 'pointer',
  background: 'rgba(0,0,0,.66)',
  color: '#fff',
  fontSize: 13,
  fontWeight: 700,
}

const dropZoneBaseStyle: CSSProperties = {
  border: '1.5px dashed rgba(255,255,255,.22)',
  borderRadius: 10,
  padding: '14px 16px',
  textAlign: 'center',
  fontSize: 12.5,
  lineHeight: 1.9,
  cursor: 'pointer',
  color: 'rgba(255,255,255,.6)',
  background: 'rgba(255,255,255,.02)',
}

const dropZoneActiveStyle: CSSProperties = {
  ...dropZoneBaseStyle,
  borderColor: 'var(--accent)',
  background: 'rgba(59,130,246,.14)',
  color: '#dbeafe',
}

/** 上传进度条的槽（底条），填充部分按百分比宽度画。 */
const progressTrackStyle: CSSProperties = {
  position: 'absolute',
  left: 0,
  right: 0,
  bottom: 0,
  height: 4,
  background: 'rgba(0,0,0,.5)',
}

// 只放后端真的收的四种（`backend/app/services/uploads.py:44` ALLOWED_EXTENSIONS）。
// 故意不把 gif/bmp/avif/heic 写进来：放行它们只会让用户白等一次上传再收到
// 「格式不支持」，不如当场说清楚。
const IMAGE_EXT = /\.(png|jpe?g|webp)$/i
// 与后端 MAX_UPLOAD_BYTES 对齐（`backend/app/services/uploads.py:48`，10MB）。
const MAX_IMAGE_BYTES = 10 * 1024 * 1024

/** 从后端给的 `local_path` 里取出文件名，喂给 `GET /api/uploads/local/{name}`。 */
function localFileName(localPath?: string | null): string {
  if (!localPath) return ''
  const parts = String(localPath).split(/[\\/]/)
  return parts[parts.length - 1] || ''
}

interface UploadTask {
  key: string
  name: string
  /** 本地 object URL，上传中就靠它显示预览（服务端地址还没拿到）。 */
  preview: string
  percent: number
  error: string
}

/**
 * 商品图片编辑区。
 *
 * 契约（Lead 定，不得改）：
 *  - 上传：`POST /api/uploads/images`，multipart 字段 `file`（+ 可选 `account_id`）
 *    → `{url, file_id, width, height, name, local_path}`；
 *  - 保存时把图片数组放进既有编辑请求体的 `images`，**第一张是主图**。数组里
 *    **刚上传的图是 `{url,width,height}` 对象**（上传接口回了真实尺寸），
 *    **库里读出来的历史图是裸 URL 字符串**（没有尺寸，不瞎编）；混合数组后端认。
 *    （形状由调用方用 `imagePayload()` 拼，本组件只负责「顺序 + 每张的真实尺寸」。）
 *
 * 交互：
 *  - 拖入（dragenter/dragover/drop，进入时整块高亮）、点击选择、Ctrl+V 粘贴；
 *  - 缩略图网格，单张删除，**拖动排序**（HTML5 draggable + drop 到目标格，第一张即主图）；
 *  - 每张上传独立显示真实进度（XHR 的 upload.onprogress），失败的只标自己那一张，
 *    不影响已经传好的图；只要还有图在传，调用方就禁用「保存」。
 *  - 「每行一个 URL」纯文本模式保留为兼容入口（分段控件切换），用于手工粘贴平台地址。
 */
function ImageField({
  accountId,
  urls,
  setUrls,
  onUploadingChange,
  onUploaded,
}: {
  accountId: string
  /** 只存 http(s) URL 的**有序**列表：渲染、去重、排序、删除都在这一层做。 */
  urls: string[]
  /** 用父级的 setState 直通，多个上传并发追加时不会丢结果（不做陈旧闭包）。 */
  setUrls: Dispatch<SetStateAction<string[]>>
  onUploadingChange: Dispatch<SetStateAction<number>>
  /** 上传成功时把真实尺寸报给父级，父级据此把这张图拼成 `{url,width,height}`。 */
  onUploaded?: (url: string, size: { width: number; height: number }) => void
}) {
  const toast = useToast()
  const [mode, setMode] = useState<'upload' | 'text'>('upload')
  const [dragOver, setDragOver] = useState(false)
  const [tasks, setTasks] = useState<UploadTask[]>([])
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)
  const [text, setText] = useState('')
  const [broken, setBroken] = useState<Record<string, boolean>>({})
  // 刚上传的图拿到的是闲鱼图床 URL；图床偶尔不能立刻回源。后端为这种情况留了
  // `GET /api/uploads/local/{name}`（落盘原图，`<img>` 带不了 Bearer 头，所以它也收 `?token=`）。
  // 这里记下 url → 本地文件名，远程图拉不到时退回来源本地，最后才认「打不开」。
  const [localNames, setLocalNames] = useState<Record<string, string>>({})
  const [srcOverride, setSrcOverride] = useState<Record<string, string>>({})
  const inputRef = useRef<HTMLInputElement>(null)
  // dragenter/dragleave 会随子元素冒泡反复触发，用计数抵消，避免高亮闪烁。
  const depthRef = useRef(0)

  // 只上报**还在传**的张数给父级（决定「保存」是否禁用）。
  // 传失败的张数不能算进去 —— 否则用户一旦有一张图传失败，保存就被永久锁死。
  const inFlight = tasks.filter((task) => !task.error).length
  useEffect(() => {
    onUploadingChange(inFlight)
  }, [inFlight, onUploadingChange])

  // 兜底：编辑弹窗开着时，把整个文档的文件投放默认行为掐掉。
  // 否则用户把文件掉到虚线框外面一点点，浏览器就会直接跳到那个文件（本地路径），
  // 单页应用被卸载 —— 用户看到的是「白屏/文件内容」，比丢一张图严重得多。
  useEffect(() => {
    const swallow = (event: Event) => event.preventDefault()
    window.addEventListener('dragover', swallow)
    window.addEventListener('drop', swallow)
    return () => {
      window.removeEventListener('dragover', swallow)
      window.removeEventListener('drop', swallow)
    }
  }, [])

  const addUrls = useCallback(
    (incoming: string[]) => {
      setUrls((prev) => [...prev, ...incoming.filter((url) => !prev.includes(url))])
    },
    [setUrls],
  )

  const uploadOne = useCallback(
    async (file: File) => {
      const key = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      const preview = URL.createObjectURL(file)
      setTasks((prev) => [...prev, { key, name: file.name || '图片', preview, percent: 0, error: '' }])
      try {
        const result = await uploadImage(file, {
          accountId: accountId || undefined,
          onProgress: (percent) => setTasks((prev) => prev.map((task) => (task.key === key ? { ...task, percent } : task))),
        })
        if (!result?.url) throw new Error('上传完成了，但后端没有返回图片地址')
        addUrls([result.url])
        // 把真实尺寸报给父级：平台 imageInfoDOList 的 widthSize/heightSize 就
        // 不用退化成占位 '0'。**拿不到就不报** —— 宁可让协议层去填 '0'（它明确
        // 拒绝硬编码 1254，因为那是对每张图撒谎），也不能在这里编一个尺寸。
        const width = Number(result.width)
        const height = Number(result.height)
        if (onUploaded && Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
          onUploaded(result.url, { width, height })
        }
        // 记下本地副本名，远程 URL 拉不到时可以退回它
        const localName = localFileName(result.local_path)
        if (localName) setLocalNames((prev) => ({ ...prev, [result.url as string]: localName }))
        setTasks((prev) => prev.filter((task) => task.key !== key))
        URL.revokeObjectURL(preview)
      } catch (err) {
        // 单张失败只落在这一张上：已传好的图原样保留，其余并发的上传继续跑。
        setTasks((prev) => prev.map((task) => (task.key === key ? { ...task, error: errorText(err) } : task)))
      }
    },
    [accountId, addUrls, onUploaded],
  )

  const pickFiles = (files: FileList | File[] | null | undefined) => {
    if (!files) return
    const all = Array.from(files)
    const images = all.filter((file) => file.type.startsWith('image/') || IMAGE_EXT.test(file.name))
    const skipped = all.length - images.length
    if (skipped > 0) toast.info(`已跳过 ${skipped} 个不是图片的文件`)
    if (images.length === 0) {
      if (all.length === 0) toast.info('没有读到文件，试试直接点这个区域选择图片')
      return
    }
    // 超限的当场拦下：服务端上限 10MB，与其让它传完再被退回，不如现在就说清楚。
    const tooBig = images.filter((file) => file.size > MAX_IMAGE_BYTES)
    const accepted = images.filter((file) => file.size <= MAX_IMAGE_BYTES)
    if (tooBig.length > 0) {
      const names = tooBig.map((file) => `${file.name}（${(file.size / 1024 / 1024).toFixed(1)}MB）`).join('、')
      toast.error(`超过 10MB 的图片传不了：${truncate(names, 60)}`)
    }
    if (accepted.length === 0) return
    // 并发上传：每张各自成败，不排队、不互相阻塞。
    for (const file of accepted) void uploadOne(file)
  }

  const removeTask = (key: string) => {
    setTasks((prev) => {
      const target = prev.find((task) => task.key === key)
      if (target) URL.revokeObjectURL(target.preview)
      return prev.filter((task) => task.key !== key)
    })
  }

  /** 把第 from 张移到 to 位置（第一张 = 主图）。 */
  const reorder = (from: number, to: number) => {
    if (from === to || from < 0 || to < 0) return
    setUrls((prev) => {
      if (from >= prev.length || to >= prev.length) return prev
      const next = prev.slice()
      const [moved] = next.splice(from, 1)
      next.splice(to, 0, moved)
      return next
    })
  }

  const onWrapperDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    depthRef.current = 0
    setDragOver(false)
    // 「每行一个 URL」模式下不走拖拽上传：那时用户是在编辑文本，
    // 收下图片只会让 textarea 里的内容和真正要保存的 urls 对不上。
    if (mode !== 'upload') return
    // 缩略图之间的「排序拖动」没有 files（数据在 text/plain 里），别当成文件投放。
    if (event.dataTransfer.files && event.dataTransfer.files.length > 0) pickFiles(event.dataTransfer.files)
  }

  const onPaste = (event: ClipboardEvent<HTMLDivElement>) => {
    // 文本模式下 textarea 才是粘贴目标 —— 这里一旦 preventDefault，
    // 用户往 textarea 里粘一行 URL 就会发现「一个字都没进去」。
    if (mode !== 'upload') return
    const files = event.clipboardData?.files
    if (files && files.length > 0) {
      event.preventDefault()
      pickFiles(files)
      return
    }
    // 从别处复制来的图片地址也直接收下
    const raw = event.clipboardData?.getData('text') || ''
    const found = raw
      .split(/\s+/)
      .map((part) => part.trim())
      .filter((part) => /^https?:\/\//i.test(part))
    if (found.length > 0) {
      event.preventDefault()
      addUrls(found)
      toast.success(`已添加 ${found.length} 个图片地址`)
    }
  }

  const failedTasks = tasks.filter((task) => task.error)

  /**
   * 缩略图加载失败时的两级退让：先试后端的本地副本，再认「打不开」。
   * 顺序不能反 —— 远程图床 URL 才是真正要提交给平台的地址，本地副本只是预览兜底。
   */
  const onImageError = (url: string) => {
    const name = localNames[url]
    if (name && !srcOverride[url]) {
      const token = getToken()
      const query = token ? `?token=${encodeURIComponent(token)}` : ''
      setSrcOverride((prev) => ({ ...prev, [url]: `/api/uploads/local/${encodeURIComponent(name)}${query}` }))
      return
    }
    setBroken((prev) => ({ ...prev, [url]: true }))
  }

  return (
    <div
      onDragEnter={(event) => {
        // 文本模式下没有拖拽上传，别让虚线框高亮（那会儿虚线框根本没渲染）
        if (mode !== 'upload') return
        // 缩略图之间的「排序拖动」不是「拖文件进来」，别让虚线框跟着高亮喊「松手就开始上传」
        if (dragIndex !== null) return
        if (!event.dataTransfer.types.includes('Files')) return
        event.preventDefault()
        depthRef.current += 1
        setDragOver(true)
      }}
      onDragOver={(event) => {
        if (mode !== 'upload') return
        if (dragIndex !== null) return
        if (!event.dataTransfer.types.includes('Files')) return
        event.preventDefault()
        event.dataTransfer.dropEffect = 'copy'
        setDragOver(true)
      }}
      onDragLeave={(event) => {
        if (depthRef.current <= 0) return
        event.preventDefault()
        depthRef.current -= 1
        if (depthRef.current <= 0) {
          depthRef.current = 0
          setDragOver(false)
        }
      }}
      onDrop={onWrapperDrop}
      onPaste={onPaste}
    >
      <div className="row" style={{ marginBottom: 8 }}>
        <button
          type="button"
          className={`btn sm${mode === 'upload' ? ' primary' : ''}`}
          onClick={() => setMode('upload')}
        >
          拖拽上传
        </button>
        <button
          type="button"
          className={`btn sm${mode === 'text' ? ' primary' : ''}`}
          onClick={() => {
            if (mode !== 'text') setText(urls.join('\n'))
            setMode('text')
          }}
          style={{ marginLeft: -1 }}
          title="兼容入口：直接粘贴图片 URL"
        >
          每行一个 URL
        </button>
        <span className="spacer" />
        <span className="dim" style={{ fontSize: 11.5 }}>
          共 {urls.length} 张{urls.length > 0 ? ' · 第一张是主图' : ''}
        </span>
      </div>

      {mode === 'upload' ? (
        <>
          <div
            tabIndex={0}
            style={dragOver ? dropZoneActiveStyle : dropZoneBaseStyle}
            onClick={(event) => {
              // 点在缩略图上不触发选文件（缩略图有自己的删除/排序交互）
              if ((event.target as HTMLElement).closest('[data-thumb]')) return
              inputRef.current?.click()
            }}
          >
            {dragOver ? (
              <strong>松手就开始上传</strong>
            ) : (
              <>
                <div>
                  <strong>把图片拖进来</strong>，或点击这里选择文件
                </div>
                <div style={{ fontSize: 11.5 }}>
                  也可以先点一下这里再按 Ctrl+V 粘贴 · 支持 png / jpg / webp / gif，可一次多张
                </div>
              </>
            )}
          </div>

          <input
            ref={inputRef}
            type="file"
            multiple
            accept="image/*"
            style={{ display: 'none' }}
            onChange={(event) => {
              pickFiles(event.target.files)
              event.target.value = ''
            }}
          />

          {(urls.length > 0 || tasks.length > 0) && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginTop: 12 }}>
              {urls.map((url, index) => (
                <div
                  key={`${index}-${url}`}
                  data-thumb
                  draggable
                  title="按住拖动可调整顺序（第一张是主图）"
                  onDragStart={(event) => {
                    event.stopPropagation()
                    setDragIndex(index)
                    event.dataTransfer.effectAllowed = 'move'
                    event.dataTransfer.setData('text/plain', String(index))
                  }}
                  onDragEnter={(event) => event.stopPropagation()}
                  onDragOver={(event) => {
                    event.preventDefault()
                    event.stopPropagation()
                    event.dataTransfer.dropEffect = 'move'
                    setOverIndex(index)
                  }}
                  onDragLeave={(event) => {
                    event.stopPropagation()
                    setOverIndex((prev) => (prev === index ? null : prev))
                  }}
                  onDrop={(event) => {
                    event.preventDefault()
                    event.stopPropagation()
                    const raw = event.dataTransfer.getData('text/plain')
                    const from = /^\d+$/.test(raw) ? Number(raw) : dragIndex
                    if (from !== null) reorder(from, index)
                    setDragIndex(null)
                    setOverIndex(null)
                  }}
                  onDragEnd={() => {
                    setDragIndex(null)
                    setOverIndex(null)
                  }}
                  style={{
                    ...imageCellStyle,
                    opacity: dragIndex === index ? 0.4 : 1,
                    outline: overIndex === index && dragIndex !== null && dragIndex !== index ? '2px solid var(--accent)' : 'none',
                    outlineOffset: 1,
                  }}
                >
                  <img
                    src={srcOverride[url] || url}
                    alt={`图片 ${index + 1}`}
                    loading="lazy"
                    style={imageCellImgStyle}
                    onError={() => onImageError(url)}
                  />
                  {broken[url] && (
                    <span
                      style={{
                        position: 'absolute',
                        inset: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        background: 'rgba(0,0,0,.6)',
                        color: '#fca5a5',
                        fontSize: 11.5,
                      }}
                    >
                      图片打不开
                    </span>
                  )}
                  {index === 0 && (
                    <span
                      style={{
                        position: 'absolute',
                        left: 4,
                        bottom: 4,
                        padding: '1px 6px',
                        borderRadius: 4,
                        fontSize: 10.5,
                        background: 'rgba(59,130,246,.9)',
                        color: '#fff',
                      }}
                    >
                      主图
                    </span>
                  )}
                  <span
                    style={{
                      position: 'absolute',
                      right: 4,
                      bottom: 4,
                      fontSize: 10.5,
                      color: 'rgba(255,255,255,.75)',
                    }}
                  >
                    {index + 1}
                  </span>
                  <button
                    type="button"
                    style={thumbDeleteStyle}
                    title="删除这张"
                    onClick={(event) => {
                      event.stopPropagation()
                      setUrls((prev) => prev.filter((_, i) => i !== index))
                    }}
                  >
                    ×
                  </button>
                </div>
              ))}

              {tasks.map((task) => (
                <div key={task.key} data-thumb style={imageCellStyle}>
                  <img src={task.preview} alt={task.name} style={{ ...imageCellImgStyle, opacity: task.error ? 0.35 : 0.55 }} />
                  {task.error ? (
                    <span
                      style={{
                        position: 'absolute',
                        inset: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        background: 'rgba(0,0,0,.55)',
                        color: '#fca5a5',
                        fontSize: 11.5,
                      }}
                    >
                      上传失败
                    </span>
                  ) : (
                    <>
                      <div style={progressTrackStyle}>
                        <div
                          style={{
                            height: '100%',
                            width: `${task.percent}%`,
                            background: 'var(--accent)',
                            transition: 'width .15s',
                          }}
                        />
                      </div>
                      <span
                        style={{
                          position: 'absolute',
                          left: 4,
                          bottom: 6,
                          fontSize: 10.5,
                          color: 'rgba(255,255,255,.9)',
                        }}
                      >
                        {task.percent}%
                      </span>
                    </>
                  )}
                  <button
                    type="button"
                    style={thumbDeleteStyle}
                    title={task.error ? '移除这张失败的图片' : '取消这张上传'}
                    onClick={(event) => {
                      event.stopPropagation()
                      removeTask(task.key)
                    }}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          {failedTasks.map((task) => (
            <Notice key={task.key} kind="error">
              <strong>{task.name}</strong> 上传失败：{task.error}
            </Notice>
          ))}
        </>
      ) : (
        <textarea
          value={text}
          onChange={(event) => {
            setText(event.target.value)
            setUrls(
              event.target.value
                .split('\n')
                .map((line) => line.trim())
                .filter(Boolean),
            )
          }}
          rows={6}
          placeholder={'https://…（每行一个，第一行是主图）'}
          className="mono"
        />
      )}
    </div>
  )
}

/* ---------------- 单条编辑 ---------------- */

function EditItemModal({ item, onClose, onSaved }: { item: Item; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [title, setTitle] = useState(item.title || '')
  const [price, setPrice] = useState(item.price !== undefined && item.price !== null ? String(item.price) : '')
  // `stock < 0` 是后端的「未知」哨兵值（首次入库 / 平台列表没给库存），**不是**真实库存。
  // 千万不能把它预填进输入框：用户随手一保存就把 -1 发回去，后端
  // `normalize_fields` 会以「库存不能为负」返回 400，用户看到的是莫名其妙的报错。
  // 未知就留空 —— 留空等于「这次不改库存」。
  const [stock, setStock] = useState(
    item.stock !== undefined && item.stock !== null && Number(item.stock) >= 0 ? String(item.stock) : '',
  )
  const [description, setDescription] = useState(item.description || '')
  const [images, setImages] = useState<string[]>(() => itemImageUrls(item.images))
  /**
   * 刚上传图片的真实尺寸（URL → {width,height}）。
   *
   * 库里的 `items.images` **只存 URL 字符串**，历史图没有尺寸；用户这次新传的图
   * 上传接口回了尺寸，就记在这儿 —— 提交时 `imagePayload()` 把有尺寸的拼成
   * `{url,width,height}` 对象、其余保持字符串。两者混在一个数组里后端认。
   */
  const [imageSizes, setImageSizes] = useState<ImageSizes>({})
  /** 有过上传/删除/排序才提交 `images` —— 否则没动图片的编辑会把平台主图清掉。 */
  const [imagesTouched, setImagesTouched] = useState(false)
  const [uploadingCount, setUploadingCount] = useState(0)
  const [aiEnabled, setAiEnabled] = useState(bool(item.ai_enabled))
  const [saving, setSaving] = useState(false)

  const applyImages: Dispatch<SetStateAction<string[]>> = useCallback((updater) => {
    setImagesTouched(true)
    setImages((prev) => (typeof updater === 'function' ? updater(prev) : updater))
  }, [])

  /** 上传成功：记下真实尺寸（只有上传路径会调它，粘贴 URL 拿不到尺寸，如实留空）。 */
  const rememberSize = useCallback((url: string, size: { width: number; height: number }) => {
    setImageSizes((prev) => ({ ...prev, [url]: size }))
  }, [])

  const busy = saving || uploadingCount > 0

  const save = async () => {
    if (uploadingCount > 0) {
      toast.info('还有图片正在上传，等传完再保存')
      return
    }
    // 后端会把 `images` 翻成平台的 `imageInfoDOList`，**空数组会被拒**
    // （商品图不允许清空）。在本地就拦下来，别让用户白等一次往返。
    if (imagesTouched && images.length === 0) {
      toast.error('商品至少要留一张图：不能把所有图片都删掉再保存')
      return
    }
    setSaving(true)
    try {
      const patch: Record<string, unknown> = { title, description }
      if (price !== '') patch.price = Number(price)
      if (stock !== '') patch.stock = Number(stock)
      // 契约：`images` 数组里，**刚上传的图带真实尺寸**（`{url,width,height}`）、
      // **历史图是裸 URL 字符串**（库里只有地址），第一张是主图（后端转成平台
      // imageInfoDOList，混合数组后端认）。只有用户真的动过图片才带上这个字段
      // —— 协议层对 `[]`/`None` 直接抛错，没动图就不该有这个键。
      if (imagesTouched) patch.images = imagePayload(images, imageSizes)
      // 单条编辑返回 {item, synced, updated_fields, warning?, local_only_fields?, noop?}
      // （**不是**批量形状）：ok 才算真的改到闲鱼上了；只有 warning 时是「本地已改、
      // 未同步」，不能报「已保存」了事，也绝不能因为形状不对报「共 0 个」。
      const result = await api.post<{
        item?: Item
        synced?: boolean
        warning?: string
        updated_fields?: string[]
        local_only_fields?: string[]
        noop?: boolean
      }>(
        `/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}/edit`,
        patch,
      )
      showBatchNotice(toast, singleEditAsBatch(result), '修改', '商品')
      onSaved()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`编辑商品 · ${truncate(item.title || item.item_id, 30)}`}
      onClose={onClose}
      wide
      footer={
        <>
          <span className="dim" style={{ fontSize: 12, marginRight: 'auto' }}>
            {uploadingCount > 0 ? `${uploadingCount} 张图片正在上传，传完才能保存` : ''}
          </span>
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button className="btn primary" onClick={save} disabled={busy}>
            {saving ? '保存中…' : uploadingCount > 0 ? '等待上传…' : '保存'}
          </button>
        </>
      }
    >
      <div className="split">
        <div className="left">
          <Field label="标题">
            <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <div className="row" style={{ gap: 12 }}>
            <div style={{ flex: 1 }}>
              <Field label="价格（元）">
                <input type="number" step="0.01" min="0" value={price} onChange={(e) => setPrice(e.target.value)} />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="库存" tip="留空 = 未同步/不改动（平台没给库存时是 -1）">
                <input type="number" min="0" value={stock} onChange={(e) => setStock(e.target.value)} />
              </Field>
            </div>
          </div>
          <Field label="商品 ID" tip="只读">
            <input type="text" value={item.item_id} readOnly className="mono" />
          </Field>
          <Switch checked={aiEnabled} onChange={setAiEnabled} label="允许该商品的 AI 自动回复（仅本地标记）" />
        </div>
        <div className="right">
          <Field label="商品图片" tip="拖拽 / 点击 / 粘贴上传，第一张是主图">
            <ImageField
              accountId={item.account_id}
              urls={images}
              setUrls={applyImages}
              onUploadingChange={setUploadingCount}
              onUploaded={rememberSize}
            />
          </Field>
          <Field label="描述">
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={8} />
          </Field>
        </div>
      </div>
    </Modal>
  )
}

/* ---------------- 批量修改 ---------------- */

type PriceMode = 'set' | 'delta' | 'percent'
type TitleMode = 'set' | 'prefix' | 'suffix' | 'replace'
type DescMode = 'replace' | 'append' | 'prepend'

function BatchEditModal({
  items,
  onClose,
  onDone,
}: {
  items: Item[]
  onClose: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const [priceOn, setPriceOn] = useState(false)
  const [priceMode, setPriceMode] = useState<PriceMode>('set')
  const [priceValue, setPriceValue] = useState('')
  const [titleOn, setTitleOn] = useState(false)
  const [titleMode, setTitleMode] = useState<TitleMode>('prefix')
  const [titleValue, setTitleValue] = useState('')
  const [descOn, setDescOn] = useState(false)
  const [descMode, setDescMode] = useState<DescMode>('append')
  const [descValue, setDescValue] = useState('')
  const [saving, setSaving] = useState(false)

  const accountId = items[0]?.account_id ?? ''

  const preview = (item: Item): string => {
    const parts: string[] = []
    if (priceOn && priceValue !== '') {
      const value = Number(priceValue)
      const base = Number(item.price) || 0
      const next = priceMode === 'set' ? value : priceMode === 'delta' ? base + value : base * (1 + value / 100)
      parts.push(`价格 ${formatMoney(base)} → ${formatMoney(Math.max(0, next))}`)
    }
    if (titleOn && titleValue) {
      if (titleMode === 'prefix') parts.push(`标题加前缀「${titleValue}」`)
      else if (titleMode === 'suffix') parts.push(`标题加后缀「${titleValue}」`)
      else if (titleMode === 'set') parts.push(`标题 → ${truncate(titleValue, 24)}`)
      else parts.push(`标题替换「${titleValue.split('=>')[0] || ''}」`)
    }
    if (descOn && descValue) {
      parts.push(`描述${descMode === 'append' ? '追加' : descMode === 'prepend' ? '前置' : '替换'}`)
    }
    return parts.length ? parts.join('；') : '不修改'
  }

  /** 只用来判断「用户到底勾了没有」，不参与请求体构造。 */
  const hasSelection = (): boolean => {
    if (priceOn && priceValue !== '' && !Number.isNaN(Number(priceValue))) return true
    if (titleOn && titleValue.trim()) return true
    if (descOn && descValue.trim()) return true
    return false
  }

  /**
   * 把 UI 上的「百分比 / 查找替换 / 追加前置」这些**后端不认**的意图，
   * 逐条算成后端真正支持的字段（`price` / `title` / `description`）。
   *
   * 后端 `services/items.py:48 PATCH_KEYS` 只认
   * `price_delta|price_set|title_tpl|title_prefix|title_suffix|stock_set`，
   * 其余字段名会被 `normalize_fields` 判为「不支持的字段」**整批 400**。
   * 百分比与查找替换没有对应的补丁键，所以在客户端按每条商品的当前值算好。
   */
  const patchFor = (item: Item): Record<string, unknown> => {
    const patch: Record<string, unknown> = {}
    if (priceOn && priceValue !== '') {
      const value = Number(priceValue)
      if (!Number.isNaN(value)) {
        const base = Number(item.price) || 0
        const next = priceMode === 'set' ? value : priceMode === 'delta' ? base + value : base * (1 + value / 100)
        patch.price = Math.max(0, Math.round(next * 100) / 100)
      }
    }
    if (titleOn && titleValue) {
      const current = item.title || ''
      if (titleMode === 'set') patch.title = titleValue
      else if (titleMode === 'prefix') patch.title = `${titleValue}${current}`
      else if (titleMode === 'suffix') patch.title = `${current}${titleValue}`
      else {
        const [from, to] = titleValue.split('=>')
        const find = (from || '').trim()
        patch.title = find ? current.split(find).join((to || '').trim()) : current
      }
    }
    if (descOn && descValue) {
      const current = item.description || ''
      if (descMode === 'replace') patch.description = descValue
      else if (descMode === 'append') patch.description = `${current}${descValue}`
      else patch.description = `${descValue}${current}`
    }
    return patch
  }

  const submit = async () => {
    if (!hasSelection()) {
      toast.info('请至少勾选一项要修改的内容')
      return
    }
    setSaving(true)
    try {
      const perItem = items.map((item) => ({ item, patch: patchFor(item) }))
      const first = JSON.stringify(perItem[0]?.patch ?? {})
      const uniform = perItem.every((entry) => JSON.stringify(entry.patch) === first)

      if (uniform) {
        // 所有商品的最终值一致 → 走一次批量接口。
        // 路径里的 accountId 必须带上：后端按 `(account_id, item_id)` 配对校验，
        // 不属于该账号的 item_id 会逐条进 failed（account_mismatch）。
        // 注意：后端对批量结果是 200 + {total, ok, warnings[], failed[]}（**逐条**成败），
        // 一条都没改成功时 HTTP 仍是 200；而且协议层没就绪时「本地改了」只算
        // warning、**ok=0** —— 不看返回值就报「已批量修改 N 个」会让用户以为
        // 改到闲鱼上了，实际一个都没推出去。
        const result = await api.post<BatchResult>(
          `/api/items/${encodeURIComponent(accountId)}/batch-edit`,
          {
            item_ids: items.map((item) => item.item_id),
            patch: perItem[0].patch,
          },
        )
        showBatchNotice(toast, result, '修改')
      } else {
        // 每条商品的最终值不同（百分比/查找替换）→ 逐条走单条编辑接口。
        // 单条接口同样返回 {ok, warnings, failed}：协议层没推成功时 ok=0，
        // 只有 warning —— 不能按「请求没抛异常」就计成功。
        const merged = mergeBatch()
        for (const entry of perItem) {
          merged.total += 1
          try {
            const result = await api.post<{ item?: Item; synced?: boolean; warning?: string }>(
              `/api/items/${encodeURIComponent(entry.item.account_id)}/${encodeURIComponent(entry.item.item_id)}/edit`,
              entry.patch,
            )
            const one = singleEditAsBatch(result)
            merged.ok += one.ok
            merged.warnings.push(...one.warnings)
            merged.failed.push(...one.failed)
          } catch (err) {
            merged.failed.push({ id: entry.item.item_id, message: errorText(err) })
          }
        }
        merged.synced = merged.ok
        merged.local_only = merged.warnings.length
        showBatchNotice(toast, merged, '修改')
      }
      onDone()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={`批量修改 · ${items.length} 个商品`}
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button className="btn primary" onClick={submit} disabled={saving}>
            {saving ? '提交中…' : `应用到 ${items.length} 个商品`}
          </button>
        </>
      }
    >
      <Notice kind="info">
        批量接口一次只作用于一个账号（<span className="mono">{accountId}</span>）。未勾选的字段不会被修改。
      </Notice>

      <div className="card">
        <h3>
          <Switch checked={priceOn} onChange={setPriceOn} />
          价格
        </h3>
        <div className="row wrap">
          <select value={priceMode} onChange={(e) => setPriceMode(e.target.value as PriceMode)} style={{ width: 160 }} disabled={!priceOn}>
            <option value="set">设为固定价</option>
            <option value="delta">加减固定金额</option>
            <option value="percent">按百分比调整</option>
          </select>
          <input
            type="number"
            step="0.01"
            value={priceValue}
            onChange={(e) => setPriceValue(e.target.value)}
            disabled={!priceOn}
            placeholder={priceMode === 'set' ? '例如 99.9' : priceMode === 'delta' ? '例如 -10 或 5' : '例如 -10 表示降 10%'}
            style={{ width: 240 }}
          />
          <span className="dim" style={{ fontSize: 12 }}>
            {priceMode === 'set' ? '所有选中商品统一改为该价格' : priceMode === 'delta' ? '正数加价、负数减价' : '正数涨价、负数降价（%）'}
          </span>
        </div>
      </div>

      <div className="card">
        <h3>
          <Switch checked={titleOn} onChange={setTitleOn} />
          标题
        </h3>
        <div className="row wrap">
          <select value={titleMode} onChange={(e) => setTitleMode(e.target.value as TitleMode)} style={{ width: 160 }} disabled={!titleOn}>
            <option value="prefix">加前缀</option>
            <option value="suffix">加后缀</option>
            <option value="set">统一替换</option>
            <option value="replace">查找替换</option>
          </select>
          <input
            type="text"
            value={titleValue}
            onChange={(e) => setTitleValue(e.target.value)}
            disabled={!titleOn}
            placeholder={titleMode === 'replace' ? '旧文字=>新文字' : '输入要加/替换的文字'}
            style={{ flex: 1, minWidth: 200 }}
          />
        </div>
      </div>

      <div className="card">
        <h3>
          <Switch checked={descOn} onChange={setDescOn} />
          描述
        </h3>
        <div className="row" style={{ marginBottom: 8 }}>
          <select value={descMode} onChange={(e) => setDescMode(e.target.value as DescMode)} style={{ width: 160 }} disabled={!descOn}>
            <option value="append">追加到末尾</option>
            <option value="prepend">插入到开头</option>
            <option value="replace">整体替换</option>
          </select>
        </div>
        <textarea
          value={descValue}
          onChange={(e) => setDescValue(e.target.value)}
          disabled={!descOn}
          rows={4}
          placeholder="要写入的描述内容"
        />
      </div>

      <div className="card">
        <h3>修改预览（前 5 条）</h3>
        <div className="col">
          {items.slice(0, 5).map((item) => (
            <div key={item.item_id} className="row" style={{ justifyContent: 'space-between', fontSize: 12.5 }}>
              <span className="ellipsis" style={{ maxWidth: '45%' }}>
                {truncate(item.title || item.item_id, 32)}
              </span>
              <span className="dim">{preview(item)}</span>
            </div>
          ))}
          {items.length > 5 && <div className="dim" style={{ fontSize: 12 }}>…还有 {items.length - 5} 个</div>}
        </div>
      </div>
    </Modal>
  )
}

/* ---------------- 发布商品 ---------------- */

function PublishItemModal({
  accountId,
  onClose,
  onDone,
}: {
  accountId: string
  onClose: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const [title, setTitle] = useState('')
  const [price, setPrice] = useState('')
  const [originalPrice, setOriginalPrice] = useState('')
  const [stock, setStock] = useState('1')
  const [categoryId, setCategoryId] = useState('')
  const [description, setDescription] = useState('')
  const [images, setImages] = useState<string[]>([])
  /** 发布路径的图**全是这次新传的**，所以每张都能带真实尺寸（不走历史图那条路）。 */
  const [imageSizes, setImageSizes] = useState<ImageSizes>({})
  const [uploadingCount, setUploadingCount] = useState(0)
  const [saving, setSaving] = useState(false)

  const rememberSize = useCallback((url: string, size: { width: number; height: number }) => {
    setImageSizes((prev) => ({ ...prev, [url]: size }))
  }, [])

  const submit = async () => {
    if (!title.trim()) {
      toast.info('请填写商品标题')
      return
    }
    if (uploadingCount > 0) {
      toast.info('还有图片正在上传，等传完再发布')
      return
    }
    // 后端会把 `images` 翻成平台的 `imageInfoDOList`，空数组会被拒 —— 发布必须有图。
    if (images.length === 0) {
      toast.info('请先至少上传一张商品图片')
      return
    }
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        title: title.trim(),
        description,
        category_id: categoryId || undefined,
        // 契约：第一张是主图；新传的图带真实尺寸 {url,width,height}，没有尺寸的用裸 URL。
        images: imagePayload(images, imageSizes),
      }
      if (price !== '') payload.price = Number(price)
      if (originalPrice !== '') payload.original_price = Number(originalPrice)
      if (stock !== '') payload.stock = Number(stock)
      await api.post(`/api/items/${encodeURIComponent(accountId)}/publish`, payload)
      toast.success('发布请求已提交')
      onDone()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title="发布新商品"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button className="btn primary" onClick={submit} disabled={saving || uploadingCount > 0}>
            {saving ? '发布中…' : uploadingCount > 0 ? '等待上传…' : '发布'}
          </button>
        </>
      }
    >
      <Notice kind="info">
        将发布到账号 <span className="mono">{accountId}</span>。闲鱼对发布频率敏感，建议一次只发一个。
      </Notice>
      <div className="split">
        <div className="left">
          <Field label="标题 *">
            <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="例如：全新未拆封 蓝牙耳机" />
          </Field>
          <div className="row" style={{ gap: 12 }}>
            <div style={{ flex: 1 }}>
              <Field label="售价（元）">
                <input type="number" step="0.01" min="0" value={price} onChange={(e) => setPrice(e.target.value)} />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="原价（元）">
                <input type="number" step="0.01" min="0" value={originalPrice} onChange={(e) => setOriginalPrice(e.target.value)} />
              </Field>
            </div>
          </div>
          <div className="row" style={{ gap: 12 }}>
            <div style={{ flex: 1 }}>
              <Field label="库存">
                <input type="number" min="0" value={stock} onChange={(e) => setStock(e.target.value)} />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="分类 ID" tip="可留空">
                <input type="text" value={categoryId} onChange={(e) => setCategoryId(e.target.value)} />
              </Field>
            </div>
          </div>
        </div>
        <div className="right">
          <Field label="商品图片" tip="拖拽 / 点击 / 粘贴上传，第一张是主图">
            <ImageField
              accountId={accountId}
              urls={images}
              setUrls={setImages}
              onUploadingChange={setUploadingCount}
              onUploaded={rememberSize}
            />
          </Field>
          <Field label="描述">
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={8} />
          </Field>
        </div>
      </div>
    </Modal>
  )
}

/* ---------------- 操作日志 ---------------- */

function OperationLogsModal({ accountId, onClose }: { accountId: string; onClose: () => void }) {
  const logs = useAsync<{ logs: OperationLog[] }>(
    () => api.get<{ logs: OperationLog[] }>(`/api/items/${encodeURIComponent(accountId)}/operation-logs`, { limit: 100 }),
    [accountId],
    true,
    ['items.updated'],
  )
  const list = logs.data?.logs ?? []

  return (
    <Modal
      title={`操作日志 · ${accountId}`}
      onClose={onClose}
      wide
      footer={
        <button className="btn ghost" onClick={onClose}>
          关闭
        </button>
      }
    >
      <AsyncBox
        loading={logs.loading}
        error={logs.error}
        notReady={logs.notReady}
        empty={list.length === 0}
        emptyIcon="🧾"
        emptyTitle="暂无操作日志"
        onRetry={logs.reload}
      >
        <div className="table-wrap scroll-60">
          <table>
            <thead>
              <tr>
                <th className="nowrap">时间</th>
                <th className="nowrap">动作</th>
                <th className="nowrap">结果</th>
                <th>商品</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {list.map((log, index) => (
                <tr key={log.id ?? index}>
                  <td className="nowrap dim" style={{ fontSize: 12 }}>
                    {formatTime(log.created_at)}
                  </td>
                  <td className="nowrap">{log.action || '-'}</td>
                  <td className="nowrap">
                    <Badge tone={bool(log.success) ? 'ok' : 'danger'}>{bool(log.success) ? '成功' : '失败'}</Badge>
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {log.item_id || '-'}
                  </td>
                  <td className="dim" style={{ fontSize: 12, maxWidth: 300 }}>
                    {truncate(log.detail || '', 90)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBox>
    </Modal>
  )
}
