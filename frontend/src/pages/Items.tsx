/**
 * 商品：列表 + 商品数据 + 单条编辑 + 批量选择后批量改价/改标题/改描述/改库存 + 上下架 + 发布。
 *
 * 本文件的样式都在同目录的 `items.css`（`it-` 前缀），不碰共享的 styles.css / components.tsx。
 */

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
import {
  IconArrowRight,
  IconBox,
  IconChart,
  IconChevronDown,
  IconChevronLeft,
  IconChevronRight,
  IconClose,
  IconCopy,
  IconEdit,
  IconEye,
  IconImage,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSort,
  IconTrash,
  IconWarn,
} from '../icons'
import { useAsync } from '../hooks/useAsync'
import { bool, formatMoney, formatTime, itemStatusLabel, parseImages, truncate } from '../format'
import type { Account, BatchResult, Item, ItemListResult, OperationLog } from '../types'
import './items.css'

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

/* ---------------- 列表缩略图 / 大图查看 / 库存口径 ---------------- */

/**
 * 列表缩略图（主图 = `images[0]`），48×48 圆角。
 *
 * 三种状态都不塌陷，也绝不出现浏览器的破图图标：
 *   有图 → hover 放大（`.it-thumb:hover`）、点击开大图；
 *   无图 → 同尺寸占位图标（`IconImage`，不是 emoji）；
 *   加载失败 → 同上占位，title 说明原因。
 * 多于一张时右下角显示张数，提示「点开能翻」。
 */
function ItemThumb({ urls, alt, onOpen }: { urls: string[]; alt: string; onOpen: () => void }) {
  const [failed, setFailed] = useState(false)
  const url = urls[0]
  const usable = Boolean(url) && !failed
  return (
    <button
      type="button"
      className="it-thumb"
      title={
        usable
          ? urls.length > 1
            ? `点击查看大图（共 ${urls.length} 张）`
            : '点击查看大图'
          : failed
            ? '图片加载失败'
            : '没有图片'
      }
      onClick={(event) => {
        // 整行点击 = 选中，缩略图点击 = 看大图：各自 stopPropagation，
        // 否则用户想看大图却顺手把这一行选中了。
        event.stopPropagation()
        if (usable) onOpen()
      }}
    >
      {usable ? (
        <img src={url} alt={alt} loading="lazy" onError={() => setFailed(true)} />
      ) : (
        <span className="it-thumb-ph">
          <IconImage size={18} />
        </span>
      )}
      {urls.length > 1 && <span className="it-thumb-count">{urls.length}</span>}
    </button>
  )
}

/**
 * 大图查看：点击缩略图后看主图，多图可左右翻（键盘 ←/→ 翻页、Esc 关闭）。
 *
 * 这里不复用 components.tsx 的 `Modal`：那个是表单弹窗（固定 620/900px 宽、标题栏+底栏），
 * 看图要的是接近全屏的画布和翻页控件。本任务的写范围允许新增 `items.css`，
 * 所以用自带的 `.it-lightbox*` 一套，不去改共享组件。
 */
function ImageLightbox({
  urls,
  start = 0,
  title,
  onClose,
}: {
  urls: string[]
  start?: number
  title: string
  onClose: () => void
}) {
  const [index, setIndex] = useState(Math.min(Math.max(start, 0), Math.max(urls.length - 1, 0)))
  const [failed, setFailed] = useState(false)

  const go = useCallback(
    (step: number) => {
      if (urls.length < 2) return
      setFailed(false)
      setIndex((prev) => (prev + step + urls.length) % urls.length)
    },
    [urls.length],
  )

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      else if (event.key === 'ArrowLeft') go(-1)
      else if (event.key === 'ArrowRight') go(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go, onClose])

  const current = urls[index] || ''

  return (
    <div className="it-lightbox-mask" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="it-lightbox" role="dialog" aria-modal="true" aria-label="查看商品大图">
        <div className="it-lightbox-head">
          <span className="it-lightbox-title ellipsis" title={title}>
            {title}
          </span>
          <span className="it-lightbox-count">
            {index + 1} / {urls.length}
          </span>
          <button className="btn sm ghost" onClick={onClose} title="关闭（Esc）">
            <IconClose size={15} />
          </button>
        </div>
        <div className="it-lightbox-body">
          {current && !failed ? (
            <img src={current} alt={`${title} 第 ${index + 1} 张`} onError={() => setFailed(true)} />
          ) : (
            <div className="it-lightbox-err">{failed ? '这张图打不开了（地址失效或图床不回源）' : '没有图片可看'}</div>
          )}
          {urls.length > 1 && (
            <>
              <button className="btn it-lightbox-nav prev" onClick={() => go(-1)} title="上一张（←）">
                <IconChevronLeft size={18} />
              </button>
              <button className="btn it-lightbox-nav next" onClick={() => go(1)} title="下一张（→）">
                <IconChevronRight size={18} />
              </button>
            </>
          )}
        </div>
        {urls.length > 1 && (
          <div className="it-lightbox-foot">
            {urls.map((url, i) => (
              <button
                key={`${i}-${url}`}
                className={`it-lightbox-thumb${i === index ? ' active' : ''}`}
                onClick={() => {
                  setFailed(false)
                  setIndex(i)
                }}
                title={`第 ${i + 1} 张${i === 0 ? '（主图）' : ''}`}
              >
                <img src={url} alt={`第 ${i + 1} 张`} loading="lazy" />
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
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

/** 未知库存的通用说明（列表、弹窗、预览都用同一句，避免三处口径不一致）。 */
const UNKNOWN_STOCK_TIP = '未同步（平台没返回库存），显示 - 而不是 0'

/* ---------------- 表头点击排序 ---------------- */

/** 商品列表可排序的列。**必须与后端白名单同名字段一致**（`services/items.py::SORT_COLUMNS`）。 */
type ItemSortKey = 'updated' | 'price' | 'stock' | 'title' | 'status'
type SortOrder = 'asc' | 'desc'

/**
 * 点表头的方向规则：**同一列再点一下换方向**；换到别的列时给一个更常用的默认方向
 * （标题从 A→Z、价格/库存/时间从大到小）。用户要的就是「就点一下，可以切换」。
 */
function nextSort(current: ItemSortKey, order: SortOrder, key: ItemSortKey): { key: ItemSortKey; order: SortOrder } {
  if (current === key) return { key, order: order === 'asc' ? 'desc' : 'asc' }
  return { key, order: key === 'title' ? 'asc' : 'desc' }
}

/**
 * 可点击排序的表头。
 *
 * 商品列表由**后端**排序（`?sort=&order=`，白名单在 `services/items.py::SORT_COLUMNS`，
 * 前端只发字段名、不拼 SQL）；商品数据页一次返回全部商品，就在内存里排。
 * 两条路共用这一个表头组件，免得箭头、`aria-sort`、提示文案各写一套。
 */
function SortableTh({
  label,
  active,
  order,
  onToggle,
  width,
  numeric = false,
  title,
}: {
  label: string
  active: boolean
  order: SortOrder
  onToggle: () => void
  width?: number
  numeric?: boolean
  title?: string
}) {
  return (
    <th
      className={`nowrap it-th-sort${active ? ' active' : ''}${numeric ? ' num' : ''}`}
      style={width ? { width } : undefined}
      aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        className="it-sort-btn"
        onClick={onToggle}
        title={title ?? `按${label}排序（再点一下换个方向）`}
      >
        <span>{label}</span>
        <span className={`it-sort-mark${active ? (order === 'asc' ? ' asc' : ' desc') : ''}`} aria-hidden="true">
          {active ? <IconChevronDown size={12} /> : <IconSort size={12} />}
        </span>
      </button>
    </th>
  )
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
  /** 页内两个视图：商品列表 / 商品数据。用全局已有的 `.tabs/.tab` 样式，不引第三方 tab 组件。 */
  const [tab, setTab] = useState<'list' | 'data'>('list')
  const [accountId, setAccountId] = useState('')
  const [status, setStatus] = useState('')
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  /** 表头排序：默认「更新时间倒序」，点表头切换（同一列再点一下换方向）。 */
  const [sortKey, setSortKey] = useState<ItemSortKey>('updated')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<Item | null>(null)
  const [publishOpen, setPublishOpen] = useState(false)
  const [batchOpen, setBatchOpen] = useState(false)
  const [logsOpen, setLogsOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  /** 当前展开「更多」菜单的行 key（`account_id/item_id`），同一时间只开一个。 */
  const [menuFor, setMenuFor] = useState<string | null>(null)
  /** 大图查看：点缩略图后打开，多图可翻页。 */
  const [lightbox, setLightbox] = useState<{ urls: string[]; index: number; title: string } | null>(null)
  const [confirmAction, setConfirmAction] = useState<{ title: string; message: ReactNode; run: () => Promise<void> } | null>(null)

  const accountList = accounts.data ?? []

  // 输入框每敲一个字都发一次请求，列表就会在「加载中…」和内容之间来回闪；等 250ms 再查。
  // `queryInput` 只管显示，`query` 才是进请求的那个值。
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(queryInput), 250)
    return () => window.clearTimeout(timer)
  }, [queryInput])

  // 菜单开着时，点页面任何其他地方（含另一个菜单按钮）就收起：避免多个菜单叠在一起。
  useEffect(() => {
    if (!menuFor) return
    const close = () => setMenuFor(null)
    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [menuFor])

  const items = useAsync<ItemListResult>(
    () =>
      api.get<ItemListResult>('/api/items', {
        account_id: accountId || undefined,
        status: status || undefined,
        q: query || undefined,
        limit: 500,
        // 排序在后端做（`?sort=&order=` 走白名单），这样 500 条上限之外也是全局有序，
        // 不是「先截断再排序」。
        sort: sortKey,
        order: sortOrder,
      }),
    [accountId, status, query, sortKey, sortOrder],
    true,
    // 发布/编辑/上下架/同步成功后后端会发 items.updated，静默重拉列表
    ['items.updated'],
  )

  const list = items.data?.items ?? []
  /** 三个状态分区各有多少件（后端 `counts`，范围是账号 + 关键词，不含状态本身）。 */
  const counts = items.data?.counts
  /** 真总条数（可能大于本次返回的 `limit`，那时页面只显示了一部分）。 */
  const total = items.data?.total ?? list.length

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

  /** 复制商品 ID：http / 无权限环境下剪贴板可能不可用，失败就如实说，不假装成功。 */
  const copyId = async (itemId: string) => {
    try {
      await navigator.clipboard.writeText(itemId)
      toast.success(`已复制商品 ID：${itemId}`)
    } catch {
      toast.info(`复制失败，请手动选中：${itemId}`)
    }
  }

  /**
   * 删除商品记录。
   *
   * `DELETE /api/items/{account_id}/{item_id}` **只删本地记录**（平台侧没有删除接口），
   * 文案必须讲明白：闲鱼上架着的商品不会被自动下架，下次「同步商品」可能又拉回来。
   * 会话与消息不受影响 —— 买家当初问的是哪件商品，历史里还得查得到。
   */
  const removeItem = (item: Item) => {
    setConfirmAction({
      title: '删除商品',
      message: (
        <>
          确定要删除 <strong>{truncate(item.title || item.item_id, 40)}</strong> 吗？
          <div className="dim" style={{ marginTop: 6 }}>
            只删本地记录：闲鱼上架着的商品不会被自动下架，下次「同步商品」可能又拉回来。
          </div>
        </>
      ),
      run: async () => {
        try {
          await api.del(`/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}`)
          toast.success('已删除本地记录')
          setSelected((prev) => {
            const next = new Set(prev)
            next.delete(item.item_id)
            return next
          })
          items.reload()
        } catch (err) {
          toast.error(errorText(err))
        }
      },
    })
  }

  return (
    <div>
      <div className="toolbar">
        <select
          value={accountId}
          onChange={(e) => {
            setAccountId(e.target.value)
            setSelected(new Set())
          }}
          style={{ width: 180 }}
          title="商品按账号隔离：批量操作一次只能作用在一个账号上"
        >
          <option value="">全部账号</option>
          {accountList.map((account) => (
            <option key={account.id} value={account.id}>
              {account.nickname || account.id}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          style={{ width: 210 }}
          title="售罄（库存 0）和已卖出的商品都归在「已下架」里：一件卖完的商品挂在「在售」里既卖不出也点不动"
        >
          <option value="">全部状态{counts ? `（${counts.all}）` : ''}</option>
          <option value="onsale">在售{counts ? `（${counts.onsale}）` : ''}</option>
          <option value="offline">已下架 · 含售罄 / 已卖出{counts ? `（${counts.offline}）` : ''}</option>
          <option value="sold">已售出{counts ? `（${counts.sold}）` : ''}</option>
        </select>
        <label className="it-search" title="按标题或商品 ID 搜索">
          <IconSearch size={14} />
          <input
            type="search"
            placeholder="搜索标题 / 商品 ID"
            value={queryInput}
            onChange={(e) => setQueryInput(e.target.value)}
            style={{ width: 190 }}
          />
        </label>
        <button className="btn" onClick={items.reload} disabled={items.loading}>
          <span className="it-ico">
            <IconRefresh size={14} />
          </span>
          刷新
        </button>
        <span className="it-spacer" />
        <button className="btn" onClick={sync} disabled={busy || !accountId} title={!accountId ? '请先选择账号' : '从闲鱼拉取在售商品并入库'}>
          <span className="it-ico">
            <IconRefresh size={14} />
          </span>
          同步商品
        </button>
        <button className="btn" onClick={() => setLogsOpen(true)} disabled={!accountId} title={!accountId ? '请先选择账号' : '查看这个账号的操作日志'}>
          操作日志
        </button>
        <button className="btn primary" onClick={() => setPublishOpen(true)} disabled={!accountId} title={!accountId ? '请先选择账号' : '发布新商品到闲鱼'}>
          <span className="it-ico">
            <IconPlus size={14} />
          </span>
          发布商品
        </button>
      </div>

      <div className="tabs">
        <button className={`tab${tab === 'list' ? ' active' : ''}`} onClick={() => setTab('list')}>
          <span className="it-ico">
            <IconBox size={14} />
          </span>
          商品列表
        </button>
        <button className={`tab${tab === 'data' ? ' active' : ''}`} onClick={() => setTab('data')} title="曝光 / 浏览 / 下单等平台数据">
          <span className="it-ico">
            <IconChart size={14} />
          </span>
          商品数据
        </button>
      </div>

      {tab === 'data' && <ItemDataView accountId={accountId} />}

      {tab === 'list' && (<>

      {selected.size > 0 && (
        <div className="card it-selbar">
          <div className="row wrap">
            <strong>已选 {selected.size} 个商品</strong>
            {mixedAccounts && <Badge tone="warn">跨账号，批量接口只支持单账号</Badge>}
            <span className="it-spacer" />
            <button className="btn sm" onClick={() => setSelected(new Set())}>
              取消选择
            </button>
            <button className="btn sm primary" disabled={mixedAccounts} onClick={() => setBatchOpen(true)} title="可批量改价格 / 标题 / 描述 / 库存">
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
          <span className="hint">
            共 {total} 条
            {list.length < total ? ` · 只显示了前 ${list.length} 条（用搜索或状态筛选缩小范围）` : ''}
            {counts
              ? ` · 在售 ${counts.onsale} · 已下架/售罄/已卖出 ${counts.offline} · 已售出 ${counts.sold}`
              : ''}
            {' · 点表头排序'}
          </span>
        </h3>
        <AsyncBox
          loading={items.loading}
          error={items.error}
          notReady={items.notReady}
          empty={list.length === 0}
          emptyIcon="box"
          emptyTitle="没有商品"
          emptyDesc={accountId ? '点「同步商品」把闲鱼在售商品拉下来。' : '先在上方选择一个账号，或点「同步商品」。'}
          onRetry={items.reload}
        >
          <div className="table-wrap it-table-wrap">
            <table className="it-table">
              <thead>
                <tr>
                  <th style={{ width: 34 }}>
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} title="全选当前列表" />
                  </th>
                  <SortableTh
                    label="商品"
                    active={sortKey === 'title'}
                    order={sortOrder}
                    onToggle={() => {
                      const next = nextSort(sortKey, sortOrder, 'title')
                      setSortKey(next.key)
                      setSortOrder(next.order)
                    }}
                    title="按标题排序（再点一下换个方向）"
                  />
                  <SortableTh
                    label="价格"
                    numeric
                    width={96}
                    active={sortKey === 'price'}
                    order={sortOrder}
                    onToggle={() => {
                      const next = nextSort(sortKey, sortOrder, 'price')
                      setSortKey(next.key)
                      setSortOrder(next.order)
                    }}
                  />
                  <SortableTh
                    label="库存"
                    numeric
                    width={68}
                    active={sortKey === 'stock'}
                    order={sortOrder}
                    onToggle={() => {
                      const next = nextSort(sortKey, sortOrder, 'stock')
                      setSortKey(next.key)
                      setSortOrder(next.order)
                    }}
                    title="按库存排序（库存未知的排在最后）"
                  />
                  <SortableTh
                    label="状态"
                    width={78}
                    active={sortKey === 'status'}
                    order={sortOrder}
                    onToggle={() => {
                      const next = nextSort(sortKey, sortOrder, 'status')
                      setSortKey(next.key)
                      setSortOrder(next.order)
                    }}
                  />
                  <th className="nowrap" style={{ width: 52 }}>
                    AI
                  </th>
                  <SortableTh
                    label="更新时间"
                    width={132}
                    active={sortKey === 'updated'}
                    order={sortOrder}
                    onToggle={() => {
                      const next = nextSort(sortKey, sortOrder, 'updated')
                      setSortKey(next.key)
                      setSortOrder(next.order)
                    }}
                  />
                  <th className="nowrap" style={{ width: 164 }}>
                    操作
                  </th>
                </tr>
              </thead>
              <tbody>
                {list.map((item) => {
                  const state = itemStatusLabel(item.status)
                  const checked = selected.has(item.item_id)
                  const imageList = itemImageUrls(item.images)
                  const stock = stockText(item.stock)
                  const rowKey = `${item.account_id}-${item.item_id}`
                  const onsale = state.text === '在售'
                  // 售罄 = 平台还在架上但库存已经是 0。它被归在「已下架」分区里，
                  // 再挂一个「在售」标签会让人以为筛选坏了，所以单独标「售罄」。
                  const soldOut = onsale && Number(item.stock) === 0
                  return (
                    <tr key={rowKey} className={`it-row${checked ? ' selected' : ''}`}>
                      <td>
                        <input type="checkbox" checked={checked} onChange={() => toggleOne(item.item_id)} />
                      </td>
                      <td className="it-cell-item">
                        <div className="it-item">
                          <ItemThumb
                            urls={imageList}
                            alt={item.title || item.item_id}
                            onOpen={() => setLightbox({ urls: imageList, index: 0, title: item.title || item.item_id })}
                          />
                          <div className="it-item-main">
                            <button className="it-title" onClick={() => setEditing(item)} title="点击编辑这个商品">
                              {item.title || '(无标题)'}
                            </button>
                            <div className="it-meta mono">
                              {item.item_id} · {item.account_id}
                              {imageList.length > 1 ? ` · ${imageList.length} 张图` : ''}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="num nowrap">
                        <div>{formatMoney(item.price)}</div>
                        {item.original_price ? <div className="it-was">{formatMoney(item.original_price)}</div> : null}
                      </td>
                      <td
                        className={`num nowrap${stock.unknown ? ' it-unknown' : ''}`}
                        title={stock.unknown ? UNKNOWN_STOCK_TIP : `平台库存：${stock.text}`}
                      >
                        {stock.text}
                      </td>
                      <td className="nowrap">
                        {soldOut ? (
                          <Badge tone="warn">售罄</Badge>
                        ) : (
                          <Badge tone={state.tone}>{state.text}</Badge>
                        )}
                      </td>
                      <td className="nowrap">
                        <Badge tone={bool(item.ai_enabled) ? 'ok' : 'muted'}>{bool(item.ai_enabled) ? '开' : '关'}</Badge>
                      </td>
                      <td className="nowrap it-time">{formatTime(item.updated_at)}</td>
                      <td className="nowrap">
                        <div className="it-acts">
                          <button className="btn sm" onClick={() => setEditing(item)}>
                            编辑
                          </button>
                          <button className="btn sm ghost" onClick={() => setShelf(item, onsale ? 'delist' : 'relist')}>
                            {onsale ? '下架' : '上架'}
                          </button>
                          <button
                            className="btn sm ghost"
                            title="更多操作"
                            onClick={(event) => {
                              event.stopPropagation()
                              setMenuFor((prev) => (prev === rowKey ? null : rowKey))
                            }}
                          >
                            <IconChevronDown size={14} />
                          </button>
                        </div>
                        {/* 菜单做成「行内展开」而不是绝对定位浮层：表格容器是 overflow-x:auto，
                            浮层会被裁掉一半，展开在单元格里最稳。 */}
                        {menuFor === rowKey && (
                          <div className="it-menu" onClick={(event) => event.stopPropagation()}>
                            <button
                              disabled={imageList.length === 0}
                              title={imageList.length === 0 ? '这个商品没有图片' : '查看大图'}
                              onClick={() => {
                                setMenuFor(null)
                                setLightbox({ urls: imageList, index: 0, title: item.title || item.item_id })
                              }}
                            >
                              <IconEye size={13} /> 查看大图
                            </button>
                            <button
                              onClick={() => {
                                setMenuFor(null)
                                void copyId(item.item_id)
                              }}
                            >
                              <IconCopy size={13} /> 复制商品 ID
                            </button>
                            <button
                              className="danger"
                              onClick={() => {
                                setMenuFor(null)
                                removeItem(item)
                              }}
                            >
                              <IconTrash size={13} /> 删除
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="it-legend">
            库存 <code>-</code> = 未同步（平台没返回），不是 0 也不是缺货；<strong>售罄（库存 0）和已卖出的商品都在「已下架」分区</strong>里，
            那样好一起编辑；点标题进编辑，点缩略图看大图。
          </div>
        </AsyncBox>
      </div>
      </>)}

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

      {lightbox && (
        <ImageLightbox
          urls={lightbox.urls}
          start={lightbox.index}
          title={lightbox.title}
          onClose={() => setLightbox(null)}
        />
      )}

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

/* ---------------- 商品数据（曝光 / 浏览 / 下单等平台指标） ---------------- */

/** `GET /api/items/{account_id}/stats` 的返回行（types.ts 不在本任务写范围内，所以本地定义）。 */
interface ItemStatsRow {
  item_id: string
  title?: string
  images?: string[]
  price?: number | null
  status?: string
  exposure?: number | null
  visits?: number | null
  visitors?: number | null
  orders?: number | null
  buyers?: number | null
  paid_orders?: number | null
}

interface ItemStatsResult {
  items: ItemStatsRow[]
  updated_at?: string
  /** `local` = 只有本地入库数据（平台数据接口没接通）；其它值原样当平台数据。 */
  source?: string
  /** 拿不到平台数据时后端给的中文原因，原样展示，不自己编。 */
  warnings?: string[]
}

/**
 * 数据来源：与后端 `GET /api/items/{account_id}/stats?source=` 一一对应
 * （`auto` = 平台能用就用、用不了降级本地；`local` = 完全不碰平台；`platform` = 必须拿到平台数据）。
 * 后端**没有**时间范围参数，所以这里不做「近 N 天」筛选，免得点了没反应像假数据。
 */
const STATS_SOURCES: Array<{ value: 'auto' | 'local' | 'platform'; label: string; tip: string }> = [
  { value: 'auto', label: '自动', tip: '平台数据能用就用，用不了降级为本地统计并列出原因' },
  { value: 'local', label: '仅本地', tip: '完全不请求闲鱼平台，只看本地库（未登录也能用）' },
  { value: 'platform', label: '平台', tip: '必须拿到平台数据，拿不到会直接报错' },
]

/** 平台指标列：`key` 必须与后端返回字段名一致；写错了只会一直显示 -，不会崩。 */
const STATS_COLUMNS: Array<{ key: keyof ItemStatsRow; label: string }> = [
  { key: 'exposure', label: '曝光次数' },
  { key: 'visits', label: '浏览次数' },
  { key: 'visitors', label: '浏览人数' },
  { key: 'orders', label: '下单人数' },
  { key: 'buyers', label: '支付人数' },
  { key: 'paid_orders', label: '支付订单数' },
]

const STATS_MISSING_TIP = '平台没返回这个数值：按要求显示 - 而不是 0'

/**
 * 数值口径：拿不到（null / undefined / 空串 / 非数字 / 负数）一律显示 `-`，
 * **绝不显示 0 或假数据**。0 是有意义的真实值，所以只判「拿不到」，不判 falsy。
 */
function statValue(value: unknown): { text: string; unknown: boolean } {
  if (value === null || value === undefined || value === '') return { text: '-', unknown: true }
  const num = Number(value)
  if (!Number.isFinite(num) || num < 0) return { text: '-', unknown: true }
  return { text: num.toLocaleString('zh-CN'), unknown: false }
}

/**
 * 商品数据视图：曝光 / 浏览 / 下单等平台指标。
 *
 * 后端接口：`GET /api/items/{account_id}/stats`（实现在 `backend/app/api` 的 `item_stats.py`，
 * 返回 `{items, updated_at, source, warnings}`）。这里必须做到：**接口未就绪也能正常渲染页面**
 * （`AsyncBox` 的 notReady 分支），拿不到的数值一律 `-` 并标出原因，`source === 'local'` 时明说「平台数据未接通」。
 */
function ItemDataView({ accountId }: { accountId: string }) {
  /** 数据来源。后端目前只支持账号维度、**没有时间范围参数**，所以不做「近 N 天」。 */
  const [sourceMode, setSourceMode] = useState<'auto' | 'local' | 'platform'>('auto')
  const [lightbox, setLightbox] = useState<{ urls: string[]; index: number; title: string } | null>(null)
  /** 表内排序（这一页一次返回该账号全部商品，所以在内存里排，不用再打一次接口）。 */
  const [dataSort, setDataSort] = useState<{ key: keyof ItemStatsRow; order: SortOrder }>({
    key: 'exposure',
    order: 'desc',
  })
  const stats = useAsync<ItemStatsResult>(
    () => api.get<ItemStatsResult>(`/api/items/${encodeURIComponent(accountId)}/stats`, { source: sourceMode }),
    [accountId, sourceMode],
    Boolean(accountId),
    ['items.updated'],
  )

  const rows = stats.data?.items ?? []
  const source = String(stats.data?.source ?? '').toLowerCase()
  const warnings = stats.data?.warnings ?? []

  // 合计只累加「拿得到」的数值；一个都没拿到就显示 -，不用 0 假装统计过。
  const sums = useMemo(() => {
    const result: Record<string, { text: string; unknown: boolean; known: number }> = {}
    for (const column of STATS_COLUMNS) {
      let total = 0
      let known = 0
      for (const row of rows) {
        if (!statValue(row[column.key]).unknown) {
          total += Number(row[column.key])
          known += 1
        }
      }
      result[column.key as string] =
        known === 0
          ? { text: '-', unknown: true, known }
          : { text: total.toLocaleString('zh-CN'), unknown: false, known }
    }
    return result
  }, [rows])

  const anyMetric = useMemo(
    () => rows.some((row) => STATS_COLUMNS.some((column) => !statValue(row[column.key]).unknown)),
    [rows],
  )

  /** 只给「至少有一个商品拿得到值」的指标发汇总卡：4 张卡里 2 张永远是 `-` 只是噪音。 */
  const cardColumns = useMemo(
    () => STATS_COLUMNS.filter((column) => (sums[column.key as string]?.known ?? 0) > 0),
    [sums],
  )

  /** 内存排序：**拿不到的数值永远排在最后**（不管升序降序），否则点「曝光」先看到一堆 `-`。 */
  const sortedRows = useMemo(() => {
    const factor = dataSort.order === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      if (dataSort.key === 'title') {
        return String(a.title || a.item_id).localeCompare(String(b.title || b.item_id), 'zh-CN') * factor
      }
      const left = statValue(a[dataSort.key])
      const right = statValue(b[dataSort.key])
      if (left.unknown && right.unknown) return 0
      if (left.unknown) return 1
      if (right.unknown) return -1
      return (Number(a[dataSort.key]) - Number(b[dataSort.key])) * factor
    })
  }, [rows, dataSort])

  const toggleDataSort = (key: keyof ItemStatsRow) => {
    setDataSort((prev) =>
      prev.key === key
        ? { key, order: prev.order === 'asc' ? 'desc' : 'asc' }
        : { key, order: key === 'title' ? 'asc' : 'desc' },
    )
  }

  if (!accountId) {
    return (
      <div className="card">
        <h3>商品数据</h3>
        <Notice kind="info">先在上方选择一个账号：平台数据是按账号查的，选「全部账号」看不出是哪家的数据。</Notice>
      </div>
    )
  }

  return (
    <div className="card">
      <h3>
        商品数据
        <span className="hint">
          曝光 / 浏览 / 下单等平台指标
          {stats.data?.updated_at ? ` · 更新于 ${formatTime(stats.data.updated_at)}` : ''}
        </span>
      </h3>

      <div className="row wrap it-data-bar">
        <div className="it-seg" role="group" aria-label="数据来源">
          {STATS_SOURCES.map((item) => (
            <button
              key={item.value}
              className={`it-seg-item${sourceMode === item.value ? ' active' : ''}`}
              onClick={() => setSourceMode(item.value)}
              title={item.tip}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="it-legend">
          平台这个接口只给「累计」数据，不支持按近 1 天 / 7 天 / 30 天筛，所以这里没有时间范围选择。
        </span>
        <span className="it-spacer" />
        <button className="btn sm" onClick={stats.reload} disabled={stats.loading}>
          <span className="it-ico">
            <IconRefresh size={13} />
          </span>
          刷新
        </button>
      </div>

      {(source === 'local' || warnings.length > 0) && (
        <div className="it-notes">
          {source === 'local' && (
            <Notice kind="warn">平台数据接口未接通，仅显示本地数据：曝光 / 浏览 / 下单这些平台指标拿不到，一律显示 -。</Notice>
          )}
          {warnings.map((text, index) => (
            <Notice key={`${index}-${text}`} kind="warn">
              {text}
            </Notice>
          ))}
        </div>
      )}

      <AsyncBox
        loading={stats.loading}
        error={stats.error}
        notReady={stats.notReady}
        empty={rows.length === 0}
        emptyIcon="box"
        emptyTitle="还没有商品数据"
        emptyDesc="先在「商品列表」里点「同步商品」把闲鱼商品拉下来，再回来看数据。"
        onRetry={stats.reload}
      >
        {!anyMetric && (
          <Notice kind="info">这张表的平台指标现在都是 -：平台数据接口没返回数值（未接通或未登录），所以不用 0 顶替。</Notice>
        )}

        {cardColumns.length > 0 && (
          <>
            <div className="grid grid-4 it-sums">
              {cardColumns.map((column) => {
                const sum = sums[column.key as string]
                const active = dataSort.key === column.key
                return (
                  <button
                    type="button"
                    className={`stat it-sum-card${active ? ' active' : ''}`}
                    key={`sum-${column.key as string}`}
                    onClick={() => toggleDataSort(column.key)}
                    title={`${column.label}：点一下按这一列排序（再点一下换方向）`}
                  >
                    <div className="label">
                      {column.label}合计
                      <span className={`it-sort-mark${active ? (dataSort.order === 'asc' ? ' asc' : ' desc') : ''}`}>
                        {active ? <IconChevronDown size={12} /> : <IconSort size={12} />}
                      </span>
                    </div>
                    <div className={`value${sum.unknown ? ' it-unknown' : ''}`}>
                      {sum.text}
                    </div>
                    <div className="it-sum-note">
                      已汇总 {sum.known}/{rows.length} 个商品
                    </div>
                  </button>
                )
              })}
            </div>
            <div className="it-legend">
              只列出<strong>有数据</strong>的指标：平台这个接口只给曝光 / 浏览 / 想要，浏览人数 / 下单人数这类它根本没返回，
              所以不摆一堆永远是 <code>-</code> 的卡片。点卡片或表头可以排序。
            </div>
          </>
        )}

        <div className="table-wrap it-table-wrap">
          <table className="it-table">
            <thead>
              <tr>
                <SortableTh
                  label="商品"
                  active={dataSort.key === 'title'}
                  order={dataSort.order}
                  onToggle={() => toggleDataSort('title')}
                  title="按标题排序（再点一下换个方向）"
                />
                <SortableTh
                  label="价格"
                  numeric
                  width={92}
                  active={dataSort.key === 'price'}
                  order={dataSort.order}
                  onToggle={() => toggleDataSort('price')}
                />
                <th className="nowrap" style={{ width: 76 }}>
                  状态
                </th>
                {STATS_COLUMNS.map((column) => (
                  <SortableTh
                    key={column.key as string}
                    label={column.label}
                    numeric
                    width={92}
                    active={dataSort.key === column.key}
                    order={dataSort.order}
                    onToggle={() => toggleDataSort(column.key)}
                    title={`按${column.label}排序（拿不到的排在最后）`}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((row) => {
                const state = itemStatusLabel(row.status)
                const imageList = itemImageUrls(row.images)
                return (
                  <tr key={row.item_id} className="it-row">
                    <td className="it-cell-item">
                      <div className="it-item">
                        <ItemThumb
                          urls={imageList}
                          alt={row.title || row.item_id}
                          onOpen={() => setLightbox({ urls: imageList, index: 0, title: row.title || row.item_id })}
                        />
                        <div className="it-item-main">
                          <div className="it-title it-title-static ellipsis" title={row.title || row.item_id}>
                            {row.title || '(无标题)'}
                          </div>
                          <div className="it-meta mono">{row.item_id}</div>
                        </div>
                      </div>
                    </td>
                    <td className="num nowrap">{formatMoney(row.price)}</td>
                    <td className="nowrap">
                      <Badge tone={state.tone}>{state.text}</Badge>
                    </td>
                    {STATS_COLUMNS.map((column) => {
                      const value = statValue(row[column.key])
                      return (
                        <td
                          key={column.key as string}
                          className={`num nowrap${value.unknown ? ' it-unknown' : ''}`}
                          title={value.unknown ? STATS_MISSING_TIP : `${column.label}：${value.text}`}
                        >
                          {value.text}
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <div className="it-legend">
          <code>-</code> = 平台没返回这个数值（不是 0，也不是没有）；合计只累加拿得到的商品，覆盖不到的在卡片里标出「已汇总 N/M」。
        </div>
      </AsyncBox>

      {lightbox && (
        <ImageLightbox
          urls={lightbox.urls}
          start={lightbox.index}
          title={lightbox.title}
          onClose={() => setLightbox(null)}
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
        <span style={{ flex: 1 }} />
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
  /** 划线价（原价）。平台允许改，但它只影响展示，留空 = 不改。 */
  const [originalPrice, setOriginalPrice] = useState(
    item.original_price !== undefined && item.original_price !== null ? String(item.original_price) : '',
  )
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
  /** 平台没给库存（`stock < 0` / 缺失）→ 输入框留空，并解释为什么是空的。 */
  const stockUnknown =
    stock === '' &&
    (item.stock === undefined || item.stock === null || !Number.isFinite(Number(item.stock)) || Number(item.stock) < 0)

  const status = itemStatusLabel(item.status)
  /**
   * 已卖出的商品平台**不接受编辑**（服务层 `SOLD_WARNING` 闸门）：保存只写本地。
   * 这不是保存失败，但用户上一版看到的正是那句「仅本地生效」，所以这里必须提前讲清楚。
   */
  const sold = status.text === '已售出'
  /** 售罄（库存 0、还在架上）：改库存就能重新开卖，值得单独提一句。 */
  const soldOut = !sold && Number(item.stock) === 0

  /**
   * 真正的改动清单 —— 同时用于「改动预览」和「一个字都没改就不让保存」。
   *
   * 空输入框一律表示「这次不改」（价格 / 原价 / 库存都是这个口径），所以**不能**
   * 拿空串去比：那会把「没动价格」也算成改动，保存按钮永远是亮的。
   */
  const changes = useMemo(() => {
    const list: { label: string; from: string; to: string }[] = []
    const text = (value: unknown) => (value === undefined || value === null || value === '' ? '（空）' : String(value))
    if (title !== (item.title || '')) list.push({ label: '标题', from: text(item.title), to: text(title) })
    if (price.trim() !== '' && Number(price) !== Number(item.price)) {
      list.push({ label: '价格', from: text(item.price), to: text(price) })
    }
    if (originalPrice.trim() !== '' && Number(originalPrice) !== Number(item.original_price)) {
      list.push({ label: '原价', from: text(item.original_price), to: text(originalPrice) })
    }
    if (stock.trim() !== '' && Number(stock) !== Number(item.stock)) {
      list.push({ label: '库存', from: stockUnknown ? '未同步' : text(item.stock), to: text(stock) })
    }
    if (description !== (item.description || '')) {
      list.push({ label: '描述', from: text(item.description), to: text(description) })
    }
    if (imagesTouched) {
      list.push({ label: '图片', from: `${itemImageUrls(item.images).length} 张`, to: `${images.length} 张` })
    }
    if (aiEnabled !== bool(item.ai_enabled)) {
      list.push({ label: 'AI 自动回复', from: bool(item.ai_enabled) ? '开' : '关', to: aiEnabled ? '开' : '关' })
    }
    return list
  }, [title, price, originalPrice, stock, description, images, imagesTouched, aiEnabled, item, stockUnknown])

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
    if (changes.length === 0) {
      toast.info('没有任何改动，不用保存')
      return
    }
    setSaving(true)
    try {
      // **只发真的变了的字段**，口径与上面的 `changes` 完全一致。
      // 以前是无条件带 `title`/`description`、只要输入框非空就带 `price`/`original_price`/
      // `stock`：用户只改了个标题，返回的 `local_only_fields` 也会把 description/price/stock
      // 列出来（正是用户看到的那句「以下字段仅本地生效：description, price, stock, title」），
      // 而且 `original_price` 会被静默写成 0。
      const patch: Record<string, unknown> = {}
      if (title !== (item.title || '')) patch.title = title
      if (description !== (item.description || '')) patch.description = description
      if (price.trim() !== '' && Number(price) !== Number(item.price)) patch.price = Number(price)
      if (originalPrice.trim() !== '' && Number(originalPrice) !== Number(item.original_price)) {
        patch.original_price = Number(originalPrice)
      }
      if (stock.trim() !== '' && Number(stock) !== Number(item.stock)) patch.stock = Number(stock)
      // 契约：`images` 数组里，**刚上传的图带真实尺寸**（`{url,width,height}`）、
      // **历史图是裸 URL 字符串**（库里只有地址），第一张是主图（后端转成平台
      // imageInfoDOList，混合数组后端认）。只有用户真的动过图片才带上这个字段
      // —— 协议层对 `[]`/`None` 直接抛错，没动图就不该有这个键。
      if (imagesTouched) patch.images = imagePayload(images, imageSizes)
      // `ai_enabled` 平台没有这个字段（服务层按 `local_only` 处理），但它是**真会落库**的。
      // 以前这个开关从来没进过 patch：拨一下、保存、回来还是原样 —— 用户报的
      // 「改动不成功」有一半就是它。只有真的拨动过才带上，免得白报一条「仅本地生效」。
      if (aiEnabled !== bool(item.ai_enabled)) patch.ai_enabled = aiEnabled
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
      onSaved()
      if (result?.noop) {
        toast.info('没有检测到改动，闲鱼那边不用动')
      } else if (result?.synced) {
        toast.success('已保存并同步到闲鱼')
      } else if (sold) {
        // 用户最困惑的那条提示：说清楚「为什么弹」和「为什么闲鱼上没变」，
        // 而不是丢一句「仅本地生效」让人以为保存失败了。
        toast.info('本地已保存。这个商品在闲鱼上已卖出，平台不接受编辑，所以闲鱼上的信息不会变 —— 不是保存失败。')
      } else {
        showBatchNotice(toast, singleEditAsBatch(result), '修改', '商品')
      }
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
          <span className="dim it-edit-foot">
            {uploadingCount > 0
              ? `${uploadingCount} 张图片正在上传，传完才能保存`
              : sold
                ? '已卖出的商品闲鱼不接受编辑：保存只更新本地记录'
                : changes.length === 0
                  ? '还没有任何改动'
                  : `${changes.length} 处改动会提交到闲鱼`}
          </span>
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button
            className="btn primary"
            onClick={save}
            disabled={busy || changes.length === 0}
            title={
              // 顺序必须与上面 footer 的文案一致：先说「为什么现在不能按/按了会怎样」，
              // 再说「没改动」。反过来的话，一个没改动的已卖出商品按钮写着
              // 「保存到本地」、hover 却提示「先改点东西」，用户会以为两个地方打架。
              uploadingCount > 0
                ? '图片还在上传，传完才能保存'
                : sold
                  ? '闲鱼不接受编辑，只会更新本地记录'
                  : changes.length === 0
                    ? '先改点东西（留空的字段不会被改）'
                    : '保存并同步到闲鱼'
            }
          >
            {saving ? '保存中…' : uploadingCount > 0 ? '等待上传…' : sold ? '保存到本地' : '保存'}
          </button>
        </>
      }
    >
      {sold && (
        <Notice kind="warn">
          <div className="it-edit-sold">
            <IconWarn size={16} />
            <div>
              <strong>这个商品在闲鱼上已经卖出（{status.text}），平台不接受对已卖出商品的编辑。</strong>
              <div>
                所以保存后：本地记录会更新，<strong>闲鱼上的标题 / 价格 / 库存 / 描述不会变</strong> ——
                这就是上一次那句「仅本地生效」的意思，不是保存失败。
              </div>
              <div className="dim">
                真要改闲鱼上的信息，只能先在列表里点「上架」把它重新挂上去（平台不一定允许），或者复制一份重新发布。
              </div>
            </div>
          </div>
        </Notice>
      )}
      {soldOut && (
        <Notice kind="info">这个商品库存已经是 0（售罄）：填上新的库存就会重新变成可购买状态。</Notice>
      )}
      <div className="split">
        <div className="left">
          <Field label="标题">
            <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <div className="row" style={{ gap: 12 }}>
            <div style={{ flex: 1 }}>
              <Field label="价格（元）" tip="留空 = 不改动">
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  placeholder="例如 99.9"
                />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="原价（元）" tip="划线价，留空 = 不改动">
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={originalPrice}
                  onChange={(e) => setOriginalPrice(e.target.value)}
                  placeholder="可留空"
                />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="库存 / 数量（件）" tip="留空 = 不改动；填 0 就是把库存改成 0">
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={stock}
                  onChange={(e) => setStock(e.target.value)}
                  placeholder={stockUnknown ? '平台未同步' : '留空不改'}
                />
              </Field>
            </div>
          </div>
          {stockUnknown && (
            <Notice kind="info">
              平台没返回这个商品的库存（列表里显示 -），所以这里是空的。填上真实件数再保存，就会按这个数改库存。
            </Notice>
          )}
          <Field label="商品 ID" tip="只读">
            <input type="text" value={item.item_id} readOnly className="mono" />
          </Field>
          <Switch checked={aiEnabled} onChange={setAiEnabled} label="允许该商品的 AI 自动回复（仅本地标记，不同步到闲鱼）" />
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
      <div className="it-changes">
        <div className="it-changes-head">
          <IconEdit size={14} />
          改动预览
          <span className="dim">{changes.length === 0 ? '还没有任何改动' : `${changes.length} 处`}</span>
        </div>
        {changes.length === 0 ? (
          <div className="dim">价格 / 原价 / 库存留空表示「这次不改」；图片没动就不会重传，所以不会有意外改动。</div>
        ) : (
          <ul className="it-changes-list">
            {changes.map((change) => (
              <li key={change.label}>
                <span className="it-change-label">{change.label}</span>
                <span className="it-change-from" title={change.from}>
                  {truncate(change.from, 34)}
                </span>
                <IconArrowRight size={12} />
                <span className="it-change-to" title={change.to}>
                  {truncate(change.to, 34)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Modal>
  )
}

/* ---------------- 批量修改 ---------------- */

type PriceMode = 'set' | 'delta' | 'percent'
type TitleMode = 'set' | 'prefix' | 'suffix' | 'replace'
type DescMode = 'replace' | 'append' | 'prepend'
type StockMode = 'set' | 'increase' | 'decrease'

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
  const [stockOn, setStockOn] = useState(false)
  const [stockMode, setStockMode] = useState<StockMode>('set')
  const [stockValue, setStockValue] = useState('')
  const [saving, setSaving] = useState(false)

  const accountId = items[0]?.account_id ?? ''
  /** 库存未知（`stock < 0` 或缺失）的选中商品：加减库存只能按增量提交。 */
  const unknownStockItems = items.filter(
    (item) =>
      item.stock === undefined || item.stock === null || !Number.isFinite(Number(item.stock)) || Number(item.stock) < 0,
  )
  /** 已卖出的选中商品：平台不接受对已卖出商品的编辑，只会改本地（结果里逐条写「仅本地生效」）。 */
  const soldItems = items.filter((item) => itemStatusLabel(item.status).text === '已售出')

  const preview = (item: Item): string => {
    const parts: string[] = []
    if (priceOn && priceValue !== '') {
      const value = Number(priceValue)
      if (Number.isFinite(value)) {
        const base = Number(item.price) || 0
        const next = priceMode === 'set' ? value : priceMode === 'delta' ? base + value : base * (1 + value / 100)
        parts.push(`价格 ${formatMoney(base)} → ${formatMoney(Math.max(0, next))}`)
      }
    }
    if (stockOn && stockValue !== '') {
      const size = Math.abs(Math.round(Number(stockValue)))
      if (Number.isFinite(size)) {
        if (stockMode === 'set') parts.push(`库存 → ${size}`)
        else {
          const base = Number(item.stock)
          const known = item.stock !== undefined && item.stock !== null && Number.isFinite(base) && base >= 0
          parts.push(
            known
              ? `库存 ${base} → ${Math.max(0, base + (stockMode === 'increase' ? size : -size))}`
              : `库存未知，按${stockMode === 'increase' ? '增加' : '减少'} ${size} 提交`,
          )
        }
      }
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
    if (priceOn && priceValue !== '' && Number.isFinite(Number(priceValue))) return true
    if (titleOn && titleValue.trim()) return true
    if (descOn && descValue.trim()) return true
    if (stockOn && stockValue.trim() !== '' && Number.isFinite(Number(stockValue))) return true
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
    // `.trim()` 不能省：`Number(' ')` 是 0 且 `Number.isFinite(0)` 为真，
    // 价格框里只敲一个空格会静默把价格改成 0（库存分支早就 trim 了）。
    if (priceOn && priceValue.trim() !== '') {
      const value = Number(priceValue)
      // 必须用 `isFinite`：`Number('1e999')` 是 `Infinity`（不是 NaN），
      // `JSON.stringify(Infinity)` 会变成 `null`，后端现在会拿它当「没给数字」400。
      if (Number.isFinite(value)) {
        const base = Number(item.price) || 0
        const next = priceMode === 'set' ? value : priceMode === 'delta' ? base + value : base * (1 + value / 100)
        // 光校验输入不够：百分比模式下 `base * (1 + value / 100)` 自己会溢出
        // （`value=1e308` 是有限数，乘完就是 `Infinity`）。算出来的值也要守。
        if (Number.isFinite(next)) {
          patch.price = Math.max(0, Math.round(next * 100) / 100)
        }
      }
    }
    if (stockOn && stockValue !== '') {
      const size = Math.abs(Math.round(Number(stockValue)))
      // 同上：`1e999` 得 `Infinity`，`JSON.stringify` 会把它变成 `null`
      if (Number.isFinite(size)) {
        if (stockMode === 'set') {
          // 设为固定值：所有商品同一个数 —— 走后端已支持的 `stock_set`。
          patch.stock_set = Math.max(0, size)
        } else {
          const base = Number(item.stock)
          const known = item.stock !== undefined && item.stock !== null && Number.isFinite(base) && base >= 0
          if (known) {
            // 当前库存已知 → 直接算成绝对值（今天的后端就支持），不依赖 stock_delta。
            patch.stock = Math.max(0, base + (stockMode === 'increase' ? size : -size))
          } else {
            // 当前库存未知 → 只能提交增量（后端 task-35 的 `stock_delta`）。
            // 后端还没支持时它会逐条给出中文原因（例如「库存未知，已跳过」），
            // 我把那个原因原样展示，不自己编。
            patch.stock_delta = stockMode === 'increase' ? size : -size
          }
        }
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

      {soldItems.length > 0 && (
        <Notice kind="warn">
          选中的商品里有 {soldItems.length} 个<b>已经卖出</b>：平台不接受对已卖出商品的编辑，
          这些商品只会改本地记录（闲鱼上的信息不会变），结果里会逐条写成「仅本地生效」。这不是失败。
        </Notice>
      )}

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
          <Switch checked={stockOn} onChange={setStockOn} />
          库存 / 数量
        </h3>
        <div className="row wrap">
          <select
            value={stockMode}
            onChange={(e) => setStockMode(e.target.value as StockMode)}
            style={{ width: 160 }}
            disabled={!stockOn}
          >
            <option value="set">设为固定值</option>
            <option value="increase">增加</option>
            <option value="decrease">减少</option>
          </select>
          <input
            type="number"
            min="0"
            step="1"
            value={stockValue}
            onChange={(e) => setStockValue(e.target.value)}
            disabled={!stockOn}
            placeholder={stockMode === 'set' ? '例如 50' : '例如 5'}
            style={{ width: 140 }}
          />
          <span className="dim" style={{ fontSize: 12 }}>
            {stockMode === 'set'
              ? '所有选中商品统一改成这个库存数'
              : `在当前库存上加${stockMode === 'increase' ? '' : '减'}这个数量（不会低于 0）`}
          </span>
        </div>
        {stockOn && stockMode !== 'set' && unknownStockItems.length > 0 && (
          <div className="it-note">
            选中的商品里有 {unknownStockItems.length} 个库存未知（列表显示 -）：后端拿不到当前库存，
            <b>不能用「增加/减少」</b>（算出来的是编的数字），这些商品会被逐条拒掉并写明原因。
            想改它们请把上面切成「设为固定值」，或先点「同步商品」把库存取回来。
          </div>
        )}
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
  /** 点过「发布」之后才显示必填项红字：一打开弹窗就一片红很劝退。 */
  const [touched, setTouched] = useState(false)

  const rememberSize = useCallback((url: string, size: { width: number; height: number }) => {
    setImageSizes((prev) => ({ ...prev, [url]: size }))
  }, [])

  const submit = async () => {
    setTouched(true)
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

  const canPublish = Boolean(title.trim()) && images.length > 0 && uploadingCount === 0 && !saving

  return (
    <Modal
      title="发布新商品"
      onClose={onClose}
      wide
      footer={
        <>
          <span className="dim" style={{ fontSize: 12, marginRight: 'auto' }}>
            {uploadingCount > 0
              ? `${uploadingCount} 张图片正在上传，传完才能发布`
              : canPublish
                ? `将上传 ${images.length} 张图，第一张是主图`
                : '标题和至少一张图片是必填的'}
          </span>
          <button className="btn ghost" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button
            className="btn primary"
            onClick={submit}
            disabled={saving || uploadingCount > 0}
            title={canPublish ? '发布到闲鱼' : '先填标题并上传至少一张图片'}
          >
            {saving ? '发布中…' : uploadingCount > 0 ? '等待上传…' : '发布到闲鱼'}
          </button>
        </>
      }
    >
      <Notice kind="info">
        将发布到账号 <span className="mono">{accountId}</span>。闲鱼对发布频率敏感，建议一次只发一个。
      </Notice>

      {/* 买家视角预览：填的时候就能看到「主图 + 标题 + 价格」长什么样，别等发出去才发现写错。 */}
      <div className="it-hero">
        <div className="it-hero-thumb">
          {images[0] ? (
            <img src={images[0]} alt="主图预览" />
          ) : (
            <span className="it-hero-ph">
              <IconImage size={20} />
            </span>
          )}
        </div>
        <div className="it-hero-main">
          <div className="it-hero-title">{title.trim() || '还没填标题'}</div>
          <div className="it-hero-price">
            {price !== '' && !Number.isNaN(Number(price)) ? formatMoney(Number(price)) : '未定价'}
            {originalPrice !== '' && !Number.isNaN(Number(originalPrice)) ? (
              <span className="it-was">{formatMoney(Number(originalPrice))}</span>
            ) : null}
            {stock !== '' ? <span className="it-hero-stock">库存 {stock}</span> : null}
          </div>
          <div className="it-hero-note">这是买家大致会看到的样子：主图 + 标题 + 价格</div>
        </div>
      </div>

      <div className="split">
        <div className="left">
          <div className="it-sec-title">基本信息</div>
          <Field label="标题 *" tip="最多 60 个字：品牌 / 成色 / 规格写清楚">
            <input
              type="text"
              value={title}
              maxLength={80}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="例如：全新未拆封 蓝牙耳机 白色"
              className={touched && !title.trim() ? 'it-invalid' : ''}
            />
            <div className={`it-count${title.length > 60 ? ' it-count-over' : ''}`}>{title.length} / 60</div>
          </Field>
          {touched && !title.trim() && <div className="it-err">标题必须填：买家就是靠它搜到你的商品</div>}
          <div className="row" style={{ gap: 12 }}>
            <div style={{ flex: 1 }}>
              <Field label="售价（元）" tip="留空 = 让闲鱼自己定">
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  placeholder="0.00"
                />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="原价（元）" tip="会被划掉的价格，可留空">
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  value={originalPrice}
                  onChange={(e) => setOriginalPrice(e.target.value)}
                  placeholder="0.00"
                />
              </Field>
            </div>
            <div style={{ flex: 1 }}>
              <Field label="库存（件）" tip="默认 1">
                <input type="number" min="0" step="1" value={stock} onChange={(e) => setStock(e.target.value)} />
              </Field>
            </div>
          </div>
          <Field label="分类 ID" tip="可留空：不确定就交给平台默认分类">
            <input
              type="text"
              value={categoryId}
              onChange={(e) => setCategoryId(e.target.value)}
              placeholder="可留空"
            />
          </Field>
        </div>
        <div className="right">
          <div className="it-sec-title">
            图片与描述
            <span className="hint">{images.length > 0 ? `${images.length} 张，第一张是主图` : '至少要一张'}</span>
          </div>
          <Field label="商品图片 *" tip="拖拽 / 点击 / 粘贴上传；第一张是主图，可拖动排序">
            <ImageField
              accountId={accountId}
              urls={images}
              setUrls={setImages}
              onUploadingChange={setUploadingCount}
              onUploaded={rememberSize}
            />
          </Field>
          {touched && images.length === 0 && <div className="it-err">至少要上传一张商品图片，否则平台会拒绝发布</div>}
          <Field label="描述" tip="成色、规格、发货方式写清楚，能明显少些砍价和退货">
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={9}
              placeholder="例如：全新未拆封，支持验货，工作日当天发货。"
            />
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
        emptyIcon="box"
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
