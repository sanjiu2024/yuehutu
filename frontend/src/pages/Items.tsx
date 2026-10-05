/** 商品：列表 + 单条编辑 + 批量选择后批量改价/改标题/改描述 + 上下架 + 发布。 */

import { useMemo, useState, type ReactNode } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { bool, formatMoney, formatTime, itemStatusLabel, parseImages, truncate } from '../format'
import type { Account, Item, ItemListResult, OperationLog } from '../types'

export function ItemsPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [])
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
      await api.post(`/api/items/${encodeURIComponent(accountId)}/sync`)
      toast.success('已触发同步，稍后刷新查看')
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
        await api.post(`/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}/${action}`)
        toast.success(action === 'delist' ? '已下架' : '已上架')
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
        let ok = 0
        const failed: string[] = []
        for (const item of targets) {
          try {
            await api.post(`/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}/${action}`)
            ok += 1
          } catch (err) {
            failed.push(`${item.item_id}: ${errorText(err)}`)
          }
        }
        if (failed.length === 0) toast.success(`已处理 ${ok} 个商品`)
        else toast.error(`成功 ${ok} 个，失败 ${failed.length} 个：${failed[0]}`)
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
                  return (
                    <tr key={`${item.account_id}-${item.item_id}`} className={checked ? 'selected' : ''}>
                      <td>
                        <input type="checkbox" checked={checked} onChange={() => toggleOne(item.item_id)} />
                      </td>
                      <td style={{ maxWidth: 320 }}>
                        <div className="ellipsis" title={item.title}>
                          {item.title || '(无标题)'}
                        </div>
                        <div className="mono dim" style={{ fontSize: 11 }}>
                          {item.item_id} · {item.account_id}
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
                      <td className="num">{item.stock ?? '-'}</td>
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

/* ---------------- 单条编辑 ---------------- */

function EditItemModal({ item, onClose, onSaved }: { item: Item; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [title, setTitle] = useState(item.title || '')
  const [price, setPrice] = useState(item.price !== undefined && item.price !== null ? String(item.price) : '')
  const [stock, setStock] = useState(item.stock !== undefined && item.stock !== null ? String(item.stock) : '')
  const [description, setDescription] = useState(item.description || '')
  const [images, setImages] = useState(parseImages(item.images).join('\n'))
  const [aiEnabled, setAiEnabled] = useState(bool(item.ai_enabled))
  const [saving, setSaving] = useState(false)

  const save = async () => {
    setSaving(true)
    try {
      const patch: Record<string, unknown> = { title, description }
      if (price !== '') patch.price = Number(price)
      if (stock !== '') patch.stock = Number(stock)
      const imageList = images.split('\n').map((s) => s.trim()).filter(Boolean)
      if (imageList.length) patch.images = imageList
      await api.post(`/api/items/${encodeURIComponent(item.account_id)}/${encodeURIComponent(item.item_id)}/edit`, patch)
      toast.success('已保存')
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
              <Field label="库存">
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
          <Field label="描述">
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={8} />
          </Field>
          <Field label="图片链接" tip="每行一个 URL">
            <textarea value={images} onChange={(e) => setImages(e.target.value)} rows={5} />
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
        // 所有商品的最终值一致 → 走一次批量接口
        await api.post(`/api/items/${encodeURIComponent(accountId)}/batch-edit`, {
          item_ids: items.map((item) => item.item_id),
          patch: perItem[0].patch,
        })
        toast.success(`已批量修改 ${items.length} 个商品`)
      } else {
        // 每条商品的最终值不同（百分比/替换）→ 逐条走单条编辑接口
        let ok = 0
        const failed: string[] = []
        for (const entry of perItem) {
          try {
            await api.post(
              `/api/items/${encodeURIComponent(entry.item.account_id)}/${encodeURIComponent(entry.item.item_id)}/edit`,
              entry.patch,
            )
            ok += 1
          } catch (err) {
            failed.push(`${entry.item.item_id}: ${errorText(err)}`)
          }
        }
        if (failed.length === 0) toast.success(`已修改 ${ok} 个商品`)
        else toast.error(`成功 ${ok} 个，失败 ${failed.length} 个：${failed[0]}`)
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
  const [images, setImages] = useState('')
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    if (!title.trim()) {
      toast.info('请填写商品标题')
      return
    }
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        title: title.trim(),
        description,
        category_id: categoryId || undefined,
        images: images.split('\n').map((s) => s.trim()).filter(Boolean),
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
          <button className="btn primary" onClick={submit} disabled={saving}>
            {saving ? '发布中…' : '发布'}
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
          <Field label="描述">
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={8} />
          </Field>
          <Field label="图片链接" tip="每行一个 URL">
            <textarea value={images} onChange={(e) => setImages(e.target.value)} rows={4} />
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
