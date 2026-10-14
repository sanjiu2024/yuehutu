/**
 * 会话：两栏布局（左 300px 会话列表 + 右会话面板）。
 *
 * 行为契约（重做 UI 时不动）：
 *  - 买家名兜底：`peer_nickname` 为空时显示「买家 <peer_user_id>」（后端宁缺勿错下发空串）；
 *  - 「同步历史会话」= `POST /api/chats/sync`（IM 只收「正在发生」的推送，不点这个按钮界面会是空的）；
 *  - 实时刷新一律走 WebSocket topic 订阅（`useAsync(..., topics)` + `xywb:refresh-messages`），**不轮询**；
 *  - 发送框 `Ctrl/Cmd+Enter` 发送，发送中禁用。
 *
 * task-44 起：
 *  - 图片消息出缩略图（点开用全站唯一一份 `ImageViewer`）、语音消息出播放器（原生 `<audio>`，
 *    控件自绘，不引依赖），AI 回复带角色/模型/耗时徽章；
 *  - 界面上**不再出现「(空消息)」**：正文为空时按消息类型给出诚实说明（见 `messageText`）；
 *  - 发图 / 发语音 / 打电话 本地做不到，只给「打开闲鱼官方页面」一个入口，
 *    拿不到真实商品 id 就**禁用**（不编地址，见 `officialItemUrl`）。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { api, errorText, isNotReady } from '../api'
import { AsyncBox, Badge, ConfirmDialog, Field, Modal, Switch, useToast } from '../components'
import {
  IconBox,
  IconChat,
  IconCheck,
  IconExternal,
  IconImage,
  IconInfo,
  IconRefresh,
  IconSend,
  IconShield,
  IconSparkle,
  IconTag,
  IconUser,
  IconUsers,
  IconWarn,
  IconWifi,
  IconXCircle,
} from '../icons'
import { ImageViewer } from '../ImageViewer'
import { useAsync } from '../hooks/useAsync'
import { formatClock, formatDuration, formatTime, modeLabel, roleName, senderLabel, truncate } from '../format'
import { subscribeTopics } from '../ws'
import type {
  Account,
  ClassifyResult,
  Conversation,
  ConversationListResult,
  Item,
  ItemListResult,
  Message,
  MessageListResult,
  ModeResult,
  SendResult,
} from '../types'
import './chats.css'

const MODES: Array<{ value: string; label: string; desc: string }> = [
  { value: 'auto', label: 'AI 自动回复', desc: '买家消息由 AI 处理，你可以随时插话' },
  { value: 'manual', label: '人工接管', desc: 'AI 让位，只有你发的消息会发出去' },
  { value: 'off', label: 'AI 关闭', desc: 'AI 完全不参与这个会话' },
]

/** 列表/面板里的实时刷新订阅的 topic（后端真实 topic 名）。 */
const LIVE_TOPICS = ['chats.message', 'chats.mode_changed', 'chats.ai_reply', 'items.updated', 'risk.changed']

/**
 * 会话级写操作（第 40 批）的返回形状。
 *
 * `is_in_black` **可能是 `null`**：平台没返回这个键（比如没权限）时后端不补 `false`
 * ——「平台没说」和「没拉黑」是两件事，界面上必须分开说。
 */
interface ImBlacklistResult {
  account_id: string
  session_id: string
  is_in_black: boolean | null
  show_black_list: boolean | null
  api: string
  warnings: string[]
  fetched_at: string
  source: string
}

interface ImActionResult {
  action: string
  saved: boolean
  deleted?: boolean
  item_id?: string
  target_uid?: string
  session_id?: string
  /** 会话接管族（第 43 批）才有：平台自己说的 `data.success`（三态，`null` = 平台没说）。 */
  platform_success?: boolean | null
  /** 重新发起成功时平台回显的 sessionId。 */
  returned_session_id?: string
  /** 只有「重新发起」成功才补的别名，便于前端说话。 */
  rejoined?: boolean
  warnings?: string[]
}

/** 接待状态的一项（官方三条：在线 / 小休 / 离线）。 */
interface ReceptionOption {
  status: string
  label: string
  description: string
}

/**
 * 客服接待状态（第 46 批）。
 *
 * `status` **可能是 `null`**：平台没返回或返回了认不出的值时后端给 `null`
 * ——「平台没说」绝不能显示成「离线」（那会让店主以为自己在离线）。
 */
interface ReceptionResult {
  account_id: string
  status: string | null
  raw_status: string | null
  options: ReceptionOption[]
  api: string
  warnings: string[]
  fetched_at: string
  source: string
}

/**
 * 客服列表（`GET /api/shop/{account_id}/cs-routing` 的 `data.agents`）。
 *
 * **`agents` 可能是 `null`**：平台没返回这个键时后端不补空数组 ——「平台没说」和
 * 「这个店没有客服」是两件事。转交需要一个真实的客服 uid，拿不到就不许编。
 */
interface CsAgent {
  customerUid?: string | number
  nick?: string
  customName?: string
  roleNames?: string[]
}

interface CsRoutingResult {
  agents?: CsAgent[] | null
  warnings?: string[]
}

/**
 * 买家显示名。
 *
 * `peer_nickname` 后端拿不到时会下发**空串**（宁缺勿错，不猜名字），
 * 这时显示「买家 <peer_user_id>」—— 比空白或错的名字有用，也方便对着闲鱼核对。
 */
function peerName(conversation: Conversation): string {
  const nickname = (conversation.peer_nickname || '').trim()
  if (nickname) return nickname
  const userId = (conversation.peer_user_id || '').trim()
  return userId ? `买家 ${userId}` : '买家'
}

/** 会话的 last_message 后端给的是**对象**（chats.py:116-121），不是字符串。 */
function lastMessageText(conversation: Conversation): string {
  const last = conversation.last_message
  if (last && typeof last === 'object') return last.content || '（暂无消息）'
  if (typeof last === 'string') return last
  return '（暂无消息）'
}

/** 取最后一条买家消息，用于「AI 帮我拟一条」时当上下文。 */
function lastInboundText(list: Message[]): string {
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const message = list[i]
    if (message.direction === 'in' && message.content) return message.content
  }
  return ''
}

/** 从 cs.suggest 的返回里收集所有可能的建议回复。 */
function collectSuggestions(result: ClassifyResult | null | undefined): string[] {
  if (!result) return []
  const out: string[] = []
  for (const key of ['replies', 'quick_replies', 'suggestions']) {
    const value = result[key]
    if (Array.isArray(value)) {
      for (const item of value) if (typeof item === 'string' && item.trim()) out.push(item)
    }
  }
  const single = result.reply || result.text
  if (typeof single === 'string' && single.trim()) out.push(single)
  return out
}

/* ---------------- 媒体消息（task-44） ---------------- */

/** 后端落库时给纯媒体消息的占位正文（`services/chats.py::_media_placeholder`）。 */
const MEDIA_PLACEHOLDERS = new Set(['[图片]', '[语音]'])

/** 消息里的图片地址（后端 task-44 起在 `messages` 响应里透出 `images`）。 */
function messageImages(message: Message): string[] {
  const raw = Array.isArray(message.images) ? message.images : []
  const out: string[] = []
  for (const url of raw) {
    const value = typeof url === 'string' ? url.trim() : ''
    if (value && !out.includes(value)) out.push(value)
  }
  return out
}

/**
 * 消息正文。
 *
 * 库里还有 task-44 之前的**空正文**历史消息，界面上不许再出现「(空消息)」这种糊弄字样：
 * 按消息类型给出诚实说明，图片/语音本身有载体时干脆不显示文字气泡。
 */
function messageText(message: Message): string {
  const content = (message.content || '').trim()
  const hasImages = messageImages(message).length > 0
  const hasAudio = Boolean((message.audio_url || '').trim())
  if (hasImages && (!content || MEDIA_PLACEHOLDERS.has(content))) return ''
  // 协议层给语音帧的正文是「[语音] N″」摘要（task-43），有播放器就不用再重复一行文字
  if (hasAudio && (!content || content.startsWith('[语音]'))) return ''
  if (content) return content
  const type = (message.msg_type || '').trim()
  if (type === 'image') return '（图片消息，但没拿到图片地址）'
  if (type === 'voice' || type === 'audio') return '（语音消息，但没拿到语音地址）'
  if (type && type !== 'text') return `（平台 ${type} 消息，没有正文）`
  return '（这条消息没有正文）'
}

/** AI 回复的徽章文案：角色 + 模型 + 真实耗时（后端 task-44 起透出 `ai_*`）。 */
function aiBadgeText(message: Message): string {
  const parts: string[] = [message.ai_role ? roleName(message.ai_role) : 'AI 回复']
  const model = (message.ai_model || '').trim()
  if (model) parts.push(model)
  const ms = Number(message.ai_elapsed_ms)
  if (Number.isFinite(ms) && ms > 0) {
    parts.push(ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${Math.round(ms)} 毫秒`)
  }
  return parts.join(' · ')
}

/**
 * 「打开闲鱼官方页面」的地址。
 *
 * 打电话 / 发图 / 发语音 本地做不到（要上传到闲鱼图床、走平台语音通道），
 * 所以只提供「去官方页面自己操作」这一个入口 —— 不假装能发。
 *
 * 地址只用**平台真实下发的商品 id** 拼平台自己的商品页
 * （`//www.goofish.com/item?id=<itemId>`，形状见 `backend/tests/test_xianyu_items.py:100`
 * 里平台真实下发的 `detailUrl`；`backend/app/xianyu/client.py:1345` 也把 `detailUrl`
 * 列为平台原始字段）。**拿不到商品 id 就返回空串 → 按钮禁用**，不编地址、不猜聊天页地址。
 */
function officialItemUrl(itemId?: string): string {
  const id = (itemId || '').trim()
  if (!id) return ''
  return `https://www.goofish.com/item?id=${encodeURIComponent(id)}`
}

/**
 * 图片缩略图。
 *
 * 闲鱼图床可能拦热链 / 图已过期，这时**不留破图**：给一块说明 + 一键用系统浏览器打开原图
 * （WebView2 里 `window.open` 走系统默认浏览器，同 `ImageViewer.tsx:157-162`）。
 */
function Thumb({ url, onOpen }: { url: string; onOpen: () => void }) {
  const [failed, setFailed] = useState(false)

  if (failed) {
    return (
      <button
        type="button"
        className="ch-thumb failed"
        onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}
        title="缩略图没加载出来，点一下用系统浏览器打开原图"
      >
        <span className="ch-thumb-fallback">
          <IconImage size={18} />
          <span>缩略图没加载出来</span>
          <span className="ch-thumb-fallback-tip">点开用浏览器看原图</span>
        </span>
      </button>
    )
  }

  return (
    <button type="button" className="ch-thumb" onClick={onOpen} title="点开大图（可滚轮缩放）">
      <img src={url} alt="会话图片" loading="lazy" onError={() => setFailed(true)} />
    </button>
  )
}

/** 语音时长文案：`formatDuration` 是取整到秒的，不足 1 秒会显示「0 秒」，这里说清楚。 */
function durationLabel(duration?: number | null): string {
  if (typeof duration !== 'number' || !Number.isFinite(duration) || duration <= 0) return ''
  if (duration < 1) return ' · 不到 1 秒'
  return ` · ${formatDuration(duration)}`
}

/** 语音消息播放器：原生 `<audio>` + 自绘控件（CSS 三角形/竖条，不用 emoji）。 */
function AudioMessage({ url, duration }: { url: string; duration?: number | null }) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [failed, setFailed] = useState(false)

  const toggle = () => {
    const node = audioRef.current
    if (!node) return
    setFailed(false)
    if (node.paused) {
      const started = node.play()
      // 地址失效 / 平台拦热链时 `play()` 会 reject —— 不许静默：给出可重试的失败态
      if (started && typeof started.catch === 'function') {
        started.catch(() => {
          setPlaying(false)
          setFailed(true)
        })
      }
    } else {
      node.pause()
    }
  }

  return (
    <span className={`ch-audio${failed ? ' failed' : ''}`}>
      <button
        type="button"
        className={`ch-audio-btn${playing ? ' playing' : ''}`}
        onClick={toggle}
        aria-label={playing ? '暂停语音' : '播放语音'}
        title={failed ? '播放失败，点一下重试' : playing ? '暂停' : '播放'}
      />
      <span className="ch-audio-label">
        {failed ? '语音播放失败，点播放键重试' : `语音消息${durationLabel(duration)}`}
      </span>
      <audio
        ref={audioRef}
        src={url}
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onError={() => {
          setPlaying(false)
          setFailed(true)
        }}
      />
    </span>
  )
}

/* ---------------- 展示小工具 ---------------- */

/** 稳定的头像底色：同一个买家每次进来颜色一致。 */
function avatarStyle(seed: string): CSSProperties {
  let hash = 0
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 31 + seed.charCodeAt(i)) % 360
  return { background: `linear-gradient(135deg, hsl(${hash} 62% 52%), hsl(${(hash + 46) % 360} 62% 42%))` }
}

function initialOf(name: string): string {
  const trimmed = name.trim()
  return (trimmed[0] || '买').toUpperCase()
}

function dayKey(value?: string | null): string {
  const full = formatTime(value)
  return full === '-' ? '' : full.slice(0, 10)
}

function dayLabel(key: string): string {
  if (!key) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = (date: Date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const now = new Date()
  if (key === stamp(now)) return '今天'
  if (key === stamp(new Date(now.getTime() - 86400000))) return '昨天'
  return key
}

function ModeIcon({ mode }: { mode: string }) {
  if (mode === 'auto') return <IconSparkle size={14} />
  if (mode === 'manual') return <IconUser size={14} />
  return <IconXCircle size={14} />
}

/* ---------------- 页面 ---------------- */

export function ChatsPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [], true, ['accounts.changed'])
  // 会话接口只下发 item_id，不给商品标题；这里拉一次商品表把它补成「商品短标题」。
  // 拉不到（接口未就绪 / 商品已删）就退回显示截断的 item_id，不影响会话本身。
  const items = useAsync<ItemListResult>(() => api.get<ItemListResult>('/api/items', { size: 200 }), [], true, [
    'items.updated',
  ])
  const [accountId, setAccountId] = useState('')
  const [modeFilter, setModeFilter] = useState('')
  const [activeId, setActiveId] = useState<number | null>(null)
  const [syncing, setSyncing] = useState(false)

  const conversations = useAsync<ConversationListResult>(
    () =>
      api.get<ConversationListResult>('/api/chats/conversations', {
        account_id: accountId || undefined,
        mode: modeFilter || undefined,
        limit: 200,
      }),
    [accountId, modeFilter],
    true,
    LIVE_TOPICS,
  )

  // 切账号/切模式筛选时先把旧数据清掉：否则列表 AsyncBox 的 `loading && !data`
  // 抑制会在新数据到达前继续显示**上一个账号**的会话（看着像串号）。
  // `setData` 是 useState 的 setter，引用稳定，可以安全进依赖。
  const resetConversations = conversations.setData
  useEffect(() => {
    resetConversations(null)
    setActiveId(null)
  }, [accountId, modeFilter, resetConversations])

  const list = conversations.data?.conversations ?? []
  const active = useMemo(() => list.find((c) => c.id === activeId) ?? null, [list, activeId])
  const unreadTotal = useMemo(() => list.reduce((sum, c) => sum + (Number(c.unread) || 0), 0), [list])

  const itemTitles = useMemo(() => {
    const map = new Map<string, string>()
    for (const item of (items.data?.items ?? []) as Item[]) {
      if (item.item_id) map.set(item.item_id, (item.title || '').trim())
    }
    return map
  }, [items.data])

  const accountNames = useMemo(() => {
    const map = new Map<string, string>()
    for (const account of accounts.data ?? []) map.set(account.id, account.nickname || account.id)
    return map
  }, [accounts.data])

  /** 商品短标题：优先真标题，其次截断的 item_id（缺字段时的诚实兜底）。 */
  const itemTitleOf = useCallback(
    (itemId?: string) => {
      const id = (itemId || '').trim()
      if (!id) return '未关联商品'
      const title = itemTitles.get(id)
      return title || `商品 ${truncate(id, 12)}`
    },
    [itemTitles],
  )

  // 选中第一条
  useEffect(() => {
    if (activeId === null && list.length > 0) setActiveId(list[0].id)
    if (activeId !== null && list.length > 0 && !list.some((c) => c.id === activeId)) setActiveId(list[0].id)
  }, [list, activeId])

  // 列表刷新由上面 useAsync 的 topics 负责（静默重拉，不闪加载态）；
  // 这里只负责把「当前会话来了新消息」转成消息流刷新事件。
  // 两个 topic 都订：AI 回复走 append_message 时会发 `chats.message`
  // （backend/app/services/chats.py:409），另外还会单独发 `chats.ai_reply`（:794）。
  // 两边都订阅，将来后端只改其中一条推送路径时消息流也不会漏刷新（代价：AI 回复时多刷一次）。
  useEffect(() => {
    return subscribeTopics(['chats.message', 'chats.ai_reply'], (event) => {
      const payload = event.payload as { conversation_id?: number }
      if (payload?.conversation_id && payload.conversation_id === activeId) {
        window.dispatchEvent(new CustomEvent('xywb:refresh-messages'))
      }
    })
  }, [activeId])

  const setMode = async (conversation: Conversation, mode: string) => {
    try {
      const result = await api.post<ModeResult>(`/api/chats/${conversation.id}/mode`, { mode })
      const next = result?.mode || mode
      toast.success(`已切换为「${modeLabel(next).text}」`)
      // 只改这一条会话的 mode，不整表 reload —— `reload()` 会把 loading 置真，
      // 列表整块闪成「加载中…」（App 里 useAsync 的静默重拉语义才是对的）。
      // 注意 `setData` 只收值、不收 updater（hooks/useAsync.ts:15）。
      const current = conversations.data
      if (current) {
        conversations.setData({
          ...current,
          conversations: current.conversations.map((item) =>
            item.id === conversation.id ? { ...item, mode: next, ai_enabled: next === 'auto' } : item,
          ),
        })
      }
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  // 把**平台上已经存在**的历史会话拉回本地。IM 入站只收「正在发生」的推送，
  // 进程启动前就有的会话不会自己进库 —— 不点这个按钮，界面就一直是空的。
  const syncHistory = async () => {
    if (!accountId) {
      toast.error('先在左边选一个账号，再同步历史会话')
      return
    }
    setSyncing(true)
    try {
      const result = await api.post<{
        scanned?: number
        conversations_new?: number
        messages?: number
        messages_skipped?: number
        skipped?: number
        nicknames_fixed?: number
      }>('/api/chats/sync', { account_id: accountId, limit: 50, message_limit: 20 })
      const fixed = Number(result?.nicknames_fixed) || 0
      toast.success(
        `同步完成：扫描 ${result?.scanned ?? 0} 个会话，新增 ${result?.conversations_new ?? 0} 个会话、` +
          `${result?.messages ?? 0} 条消息` +
          (fixed > 0 ? `，修正 ${fixed} 个买家名` : ''),
      )
      conversations.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div className="ch-page">
      <div className="ch-toolbar">
        <select className="ch-select" aria-label="账号筛选" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
          <option value="">全部账号</option>
          {(accounts.data ?? []).map((account) => (
            <option key={account.id} value={account.id}>
              {account.nickname || account.id}
            </option>
          ))}
        </select>
        <select className="ch-select" aria-label="接待模式筛选" value={modeFilter} onChange={(e) => setModeFilter(e.target.value)}>
          <option value="">全部模式</option>
          {MODES.map((mode) => (
            <option key={mode.value} value={mode.value}>
              {mode.label}
            </option>
          ))}
        </select>
        <button className="btn" onClick={conversations.reload}>
          <IconRefresh size={15} /> 刷新
        </button>
        <button className="btn" onClick={syncHistory} disabled={syncing}>
          <IconChat size={15} /> {syncing ? '同步中…' : '同步历史会话'}
        </button>
        <span className="ch-toolbar-count">
          共 <b>{list.length}</b> 个会话
          {unreadTotal > 0 && <> · 未读 <b>{unreadTotal}</b></>}
        </span>
      </div>

      <div className="ch-layout">
        <div className="ch-list">
          <div className="ch-list-head">
            <IconChat size={14} />
            <span>会话</span>
            <span className="ch-list-count">{list.length} 个</span>
          </div>
          <div className="ch-list-body">
            <AsyncBox
              loading={conversations.loading && !conversations.data}
              error={conversations.error}
              notReady={conversations.notReady}
              onRetry={conversations.reload}
            >
              {list.length === 0 ? (
                <ChatsEmpty hasAccount={Boolean(accountId)} syncing={syncing} onSync={syncHistory} />
              ) : (
                list.map((conversation) => {
                  const mode = modeLabel(conversation.mode)
                  const name = peerName(conversation)
                  const unread = Number(conversation.unread) || 0
                  return (
                    <button
                      key={conversation.id}
                      type="button"
                      className={`ch-item${conversation.id === activeId ? ' active' : ''}`}
                      onClick={() => setActiveId(conversation.id)}
                      aria-current={conversation.id === activeId}
                    >
                      <span className="ch-item-avatar" style={avatarStyle(conversation.peer_user_id || name)} aria-hidden>
                        {initialOf(name)}
                      </span>
                      <span className="ch-item-body">
                        <span className="ch-item-top">
                          <span className="ch-item-name" title={name}>
                            {name}
                          </span>
                          <span className="ch-item-time">{formatClock(conversation.last_message_at)}</span>
                        </span>
                        <span className="ch-item-preview">{lastMessageText(conversation)}</span>
                        <span className="ch-item-meta">
                          <Badge tone={mode.tone}>{mode.text}</Badge>
                          <span className="ch-item-item" title={conversation.item_id || ''}>
                            {itemTitleOf(conversation.item_id)}
                          </span>
                          {unread > 0 && <span className="ch-item-unread">{unread > 99 ? '99+' : unread}</span>}
                        </span>
                      </span>
                    </button>
                  )
                })
              )}
            </AsyncBox>
          </div>
        </div>

        <div className="ch-panel">
          {active ? (
            <ChatWindow
              key={active.id}
              conversation={active}
              accountName={accountNames.get(active.account_id || '') || active.account_id || ''}
              itemTitle={itemTitleOf(active.item_id)}
              onModeChange={setMode}
              onRefreshList={conversations.reload}
            />
          ) : (
            <div className="ch-empty">
              <span className="ch-empty-icon">
                <IconChat size={22} />
              </span>
              <span className="ch-empty-title">选择左侧的一个会话</span>
              <span className="ch-empty-desc">
                你可以看到完整消息流，随时接管自己回消息，或把会话交回给 AI。
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ---------------- 空态 ---------------- */

function ChatsEmpty({
  hasAccount,
  syncing,
  onSync,
}: {
  hasAccount: boolean
  syncing: boolean
  onSync: () => void
}) {
  return (
    <div className="ch-empty">
      <span className="ch-empty-icon">
        <IconChat size={22} />
      </span>
      <span className="ch-empty-title">{hasAccount ? '这个账号还没有会话' : '还没有会话'}</span>
      <span className="ch-empty-desc">
        {hasAccount
          ? '账号在线并收到买家消息后，会话会出现在这里；平台上已有的历史会话请点下面的「同步历史会话」。'
          : '先在上面选一个账号，再点「同步历史会话」把平台上的会话拉回来。'}
      </span>
      <span className="ch-empty-actions">
        <button className="btn primary sm" onClick={onSync} disabled={syncing || !hasAccount}>
          <IconRefresh size={14} /> {syncing ? '同步中…' : '同步历史会话'}
        </button>
      </span>
    </div>
  )
}

function MessageSkeleton() {
  return (
    <div className="ch-skel" aria-hidden>
      {[0, 1, 2, 3].map((index) => (
        <div key={index} className={`ch-skel-row${index % 3 === 2 ? ' out' : ''}`}>
          <span className="ch-skel-dot" />
          <span className="ch-skel-lines">
            <span className="ch-skel-line" style={{ width: 130 + ((index * 41) % 110) }} />
            <span className="ch-skel-line short" />
          </span>
        </div>
      ))}
    </div>
  )
}

/* ---------------- 单个会话窗口 ---------------- */

function ChatWindow({
  conversation,
  accountName,
  itemTitle,
  onModeChange,
  onRefreshList,
}: {
  conversation: Conversation
  accountName: string
  itemTitle: string
  onModeChange: (conversation: Conversation, mode: string) => void
  onRefreshList: () => void
}) {
  const toast = useToast()
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [modeBusy, setModeBusy] = useState(false)
  const streamRef = useRef<HTMLDivElement>(null)
  const [tick, setTick] = useState(0)
  /** 点开的图片（全站唯一一份看图器：滚轮缩放 / 换图 / 看原图）。 */
  const [viewer, setViewer] = useState<{ urls: string[]; start: number; note: string } | null>(null)

  const messages = useAsync<MessageListResult>(
    () => api.get<MessageListResult>(`/api/chats/${conversation.id}/messages`, { limit: 200 }),
    [conversation.id, tick],
  )

  const list = messages.data?.messages ?? []
  const name = peerName(conversation)
  /**
   * 拉黑状态（第 40 批）。
   *
   * 用 `conversation_id`（本地行 id）而不是平台 cid —— `conversations` 表里没有 cid 列，
   * 前端拿不到，由后端从最新一条消息的 `meta.cid` 解析。
   */
  const blacklist = useAsync<ImBlacklistResult>(
    () =>
      api.get<ImBlacklistResult>(`/api/im/${conversation.account_id}/blacklist`, {
        conversation_id: conversation.id,
      }),
    [conversation.id, conversation.account_id, tick],
    true,
    ['chats.changed'],
  )
  const [imBusy, setImBusy] = useState('')
  /** 需要二次确认的会话级写操作：拉黑 / 解除（第 40 批）+ 转交 / 结束 / 重新发起（第 43 批）
   *  + 接待状态（第 46 批）。 */
  const [confirmKind, setConfirmKind] = useState<
    '' | 'add' | 'remove' | 'transfer' | 'leave' | 'rejoin' | 'card' | 'reception'
  >('')
  const [cardOpen, setCardOpen] = useState(false)
  const [cardItemId, setCardItemId] = useState('')
  /** 转交客服的选人弹窗 + 选中的客服 uid（`targetUid` 只能来自客服列表，不猜）。 */
  const [transferOpen, setTransferOpen] = useState(false)
  const [targetUid, setTargetUid] = useState('')
  /**
   * 可转交的客服列表（第 43 批）。
   *
   * 懒加载（`immediate=false`）：只有用户点「转交客服」才去查 —— 每开一个会话就打一次
   * 平台接口没有意义。订阅 `shop.changed`：小铺页改了客服分组后这里静默重拉。
   */
  const agents = useAsync<CsRoutingResult>(
    () => api.get<CsRoutingResult>(`/api/shop/${conversation.account_id}/cs-routing`),
    [conversation.account_id],
    false,
    ['shop.changed'],
  )
  /** `null` = 平台没返回客服列表（不是「没有客服」）；`[]` = 真的一个都没有。 */
  const agentList = agents.data?.agents ?? null
  /** `true`/`false`/`null`（平台没说）/`undefined`（还没查出来）。 */
  const blackState = blacklist.data ? blacklist.data.is_in_black : undefined
  /**
   * 接待状态（第 46 批）。
   *
   * 账号级、不跟会话走：官方接口只认账号，所以用 `conversation.account_id`。
   * 与拉黑状态一样订阅 `chats.changed` 静默重拉（**不轮询**）。
   */
  const reception = useAsync<ReceptionResult>(
    () => api.get<ReceptionResult>(`/api/im/${conversation.account_id}/reception`),
    [conversation.account_id, tick],
    true,
    ['chats.changed'],
  )
  /** 待确认的接待状态切换（`null` = 没在确认）。 */
  const [receptionTarget, setReceptionTarget] = useState<ReceptionOption | null>(null)
  const receptionStatus = reception.data?.status ?? null
  const receptionOptions = reception.data?.options ?? []
  const receptionText = (status: string | null) => {
    const hit = receptionOptions.find((option) => option.status === status)
    return hit ? hit.label : status || ''
  }
  /** 用户是不是停在底部（往上翻历史时别把他拽回来）。 */
  const stickRef = useRef(true)

  const scrollToBottom = useCallback(() => {
    const node = streamRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [])

  // 滚到底部
  useEffect(() => {
    scrollToBottom()
  }, [list.length, scrollToBottom])

  // 图片是异步加载的：加载完会把消息流撑高，最新一条被顶出视野。
  // 用捕获阶段监听（img 的 load 不冒泡），且只在用户本来就在底部时跟着走。
  useEffect(() => {
    const node = streamRef.current
    if (!node) return
    const onLoad = () => {
      if (stickRef.current) scrollToBottom()
    }
    node.addEventListener('load', onLoad, true)
    return () => node.removeEventListener('load', onLoad, true)
  }, [conversation.id, scrollToBottom])

  // WS 推送时刷新本会话消息
  useEffect(() => {
    const handler = () => setTick((n) => n + 1)
    window.addEventListener('xywb:refresh-messages', handler)
    return () => window.removeEventListener('xywb:refresh-messages', handler)
  }, [])

  const refresh = useCallback(() => setTick((n) => n + 1), [])

  const send = async () => {
    const content = input.trim()
    if (!content) return
    setSending(true)
    try {
      const result = await api.post<SendResult>(`/api/chats/${conversation.id}/send`, { content })
      setInput('')
      // 后端在协议层未就绪时仍会落库本地消息，但 sent=false + warning
      if (result && result.sent === false) {
        toast.error(result.warning || '消息已记录，但没能发给买家（协议层未就绪）')
      }
      refresh()
      onRefreshList()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSending(false)
    }
  }

  /**
   * 「AI 帮我拟一条」。
   *
   * 首选 `POST /api/chats/{id}/ai-draft`（真 AI 生成，只生成不发送、不落库）。
   * 若 AI 编排层还没就绪（后端返回 `xianyu_not_ready`），退回到
   * `POST /api/chats/classify`（客服意图分类 + 快捷回复建议，纯规则、不花钱），
   * 至少让用户拿到一条可用的话术，而不是干瞪眼。
   */
  const draft = async () => {
    setDrafting(true)
    try {
      try {
        const result = await api.post<{ draft?: string }>(`/api/chats/${conversation.id}/ai-draft`, {})
        if (result?.draft) {
          setInput(result.draft)
          toast.info('AI 已拟好草稿，确认后再发送')
          return
        }
      } catch (err) {
        if (!isNotReady(err)) throw err
      }

      // 兜底：规则版建议回复
      const text = input.trim() || lastInboundText(list) || '你好'
      const result = await api.post<ClassifyResult>('/api/chats/classify', { text })
      const suggestions = collectSuggestions(result)
      if (suggestions.length > 0) {
        setInput(suggestions[0])
        toast.info(`AI 暂不可用，已用规则话术填入（意图：${result?.intent || '未知'}），确认后再发送`)
      } else {
        toast.info('暂时没有可用的建议回复')
      }
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setDrafting(false)
    }
  }

  const switchMode = async (mode: string) => {
    setModeBusy(true)
    try {
      await onModeChange(conversation, mode)
    } finally {
      setModeBusy(false)
    }
  }

  /**
   * 会话级写操作的**唯一出口**。
   *
   * 三件事一次做对：① 一定带 `confirm: true`（后端只认显式确认，`'false'` 这种字符串会被
   * 判成没确认）；② 一律带 `conversation_id`，平台 cid 由后端解析；③ 失败原样把后端
   * 的中文说明弹出来（平台无权限 ≠ 掉线，不能笼统说「请重新登录」）。
   */
  const runImWrite = async (path: string, body: Record<string, unknown>, okText: string) => {
    setImBusy(path)
    try {
      const result = await api.post<ImActionResult>(path, { ...body, confirm: true })
      if (result && result.saved === false) {
        toast.error((result.warnings || [])[0] || '平台没有确认这次操作')
      } else {
        toast.success(okText)
      }
      setConfirmKind('')
      setCardOpen(false)
      setTransferOpen(false)
      setReceptionTarget(null)
      refresh()
      onRefreshList()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setImBusy('')
    }
  }

  const imBase = `/api/im/${conversation.account_id}`

  const toggleBlacklist = () => {
    if (blackState === true) {
      void runImWrite(`${imBase}/blacklist/remove`, { conversation_id: conversation.id }, '已解除拉黑')
    } else {
      void runImWrite(`${imBase}/blacklist`, { conversation_id: conversation.id }, '已拉黑这个买家')
    }
  }

  const openCard = () => {
    // 默认填当前会话关联的商品；没有就留空，让用户自己填（不编 id）
    setCardItemId((conversation.item_id || '').trim())
    setCardOpen(true)
  }

  const sendCard = () => {
    const itemId = cardItemId.trim()
    if (!itemId) return
    void runImWrite(
      `${imBase}/cards`,
      { conversation_id: conversation.id, item_id: itemId },
      '商品卡片已发出',
    )
  }

  /* ---- 客服接待状态（第 46 批）：在线 / 小休 / 离线 ---- */

  /** 点一个接待状态：先弹二次确认（真发平台），确认后才走唯一写出口。 */
  const askReception = (option: ReceptionOption) => {
    if (option.status === receptionStatus) return
    setReceptionTarget(option)
    setConfirmKind('reception')
  }

  const submitReception = () => {
    const target = receptionTarget
    if (!target) return
    void runImWrite(
      `${imBase}/reception`,
      { status: target.status },
      `接待状态已切到「${target.label}」`,
    )
  }

  /* ---- 客服会话接管（第 43 批）：转交 / 结束 / 重新发起 ---- */

  const agentLabel = (agent: CsAgent): string => {
    const label = (agent.customName || agent.nick || '').trim()
    const uid = String(agent.customerUid ?? '').trim()
    if (label && uid) return `${label}（${uid}）`
    return label || uid || '未命名客服'
  }

  const openTransfer = () => {
    setTransferOpen(true)
    // 懒加载：第一次点开才查客服列表（查过就不重复打平台接口）
    if (!agents.data && !agents.loading) agents.reload()
  }

  const submitTransfer = () => {
    const uid = targetUid.trim()
    if (!uid) return
    void runImWrite(
      `${imBase}/sessions/transfer`,
      { conversation_id: conversation.id, target_uid: uid },
      '会话已转交',
    )
  }

  const confirmSessionAction = () => {
    if (confirmKind === 'transfer') {
      submitTransfer()
      return
    }
    if (confirmKind === 'leave') {
      void runImWrite(`${imBase}/sessions/leave`, { conversation_id: conversation.id }, '会话已结束')
      return
    }
    if (confirmKind === 'rejoin') {
      void runImWrite(`${imBase}/sessions/rejoin`, { conversation_id: conversation.id }, '已重新发起聊天')
      return
    }
    if (confirmKind === 'card') {
      sendCard()
      return
    }
    if (confirmKind === 'reception') {
      submitReception()
    }
  }

  /** 二次确认框的文案：六种会话级写操作各自把后果说清楚。 */
  const confirmSpec = (): { title: string; message: string; ok: string; danger: boolean } => {
    if (confirmKind === 'remove') {
      return {
        title: `解除对「${name}」的拉黑？`,
        message: '解除后对方可以重新给你发消息，会真的改平台上的拉黑名单。',
        ok: '解除拉黑',
        danger: false,
      }
    }
    if (confirmKind === 'transfer') {
      const agent = (agentList || []).find(
        (item) => String(item.customerUid ?? '') === targetUid.trim(),
      )
      return {
        title: '转交这个会话？',
        message: `会把和「${name}」的会话转交给 ${
          agent ? agentLabel(agent) : targetUid.trim()
        }，转交后由对方接待；会真的改平台上的会话归属。`,
        ok: '转交',
        danger: false,
      }
    }
    if (confirmKind === 'leave') {
      return {
        title: `结束和「${name}」的会话？`,
        message: '结束后买家不能再给你发消息，你也不能再回复；会真的改平台上的会话状态。',
        ok: '结束会话',
        danger: true,
      }
    }
    if (confirmKind === 'rejoin') {
      return {
        title: `重新发起和「${name}」的聊天？`,
        message: '会重新发起和这个买家的聊天，对方会收到新会话；会真的改平台上的会话状态。',
        ok: '重新发起',
        danger: false,
      }
    }
    if (confirmKind === 'card') {
      return {
        title: `给「${name}」发商品卡片？`,
        message: `会往这个会话里发一条商品卡片（商品 id ${cardItemId.trim() || '（空）'}），对方立刻能看到，发出去不能撤回。`,
        ok: '发送',
        danger: false,
      }
    }
    if (confirmKind === 'reception') {
      const target = receptionTarget
      return {
        title: target ? `把接待状态切到「${target.label}」？` : '切换接待状态？',
        message: target
          ? `${target.description}。这是整个账号的接待状态（所有会话一起变），会真的改平台上的设置。`
          : '会真的改平台上的接待状态。',
        ok: '切换',
        danger: target?.status !== 'ONLINE',
      }
    }
    return {
      title: `拉黑「${name}」？`,
      message: '拉黑后对方不能再给你发消息，会真的改平台上的拉黑名单。',
      ok: '拉黑',
      danger: true,
    }
  }

  const blackHint =
    blacklist.error
      ? `拉黑状态没查到：${blacklist.error}`
      : blackState === true
        ? '这个买家已在你的拉黑名单里'
        : blackState === false
          ? '这个买家没有被拉黑'
          : blacklist.loading
            ? '正在查拉黑状态…'
            : '平台没有返回拉黑状态（可能这个账号没有该权限），仍可以尝试拉黑'

  /** 接待状态的悬浮说明（`null` 时把平台原样值也写出来 —— 不当作「离线」）。 */
  const receptionHint = reception.error
    ? `接待状态没查到：${reception.error}`
    : receptionStatus
      ? `${receptionText(receptionStatus)}${
          receptionOptions.find((option) => option.status === receptionStatus)?.description
            ? `：${receptionOptions.find((option) => option.status === receptionStatus)?.description}`
            : ''
        }`
      : reception.loading
        ? '正在查接待状态…'
        : `平台没有返回接待状态${
            reception.data?.raw_status ? `（原样值：${reception.data.raw_status}）` : ''
          }`

  const mode = modeLabel(conversation.mode)
  const confirmView = confirmSpec()
  // 发图/发语音/打电话 只能在闲鱼官方页面做；没有真实商品 id 就禁用（不编地址）。
  const officialUrl = officialItemUrl(conversation.item_id)
  const openOfficial = () => {
    if (!officialUrl) return
    // WebView2 里 window.open 会走系统默认浏览器（同 ImageViewer.tsx:161 的做法）
    window.open(officialUrl, '_blank', 'noopener,noreferrer')
  }

  return (
    <>
      <div className="ch-head">
        <div className="ch-head-id">
          <span className="ch-head-avatar" style={avatarStyle(conversation.peer_user_id || name)} aria-hidden>
            {initialOf(name)}
          </span>
          <div className="ch-head-main">
            <div className="ch-head-line">
              <strong className="ch-head-name" title={name}>
                {name}
              </strong>
              <Badge tone={mode.tone}>{mode.text}</Badge>
            </div>
            <div className="ch-head-sub">
              <span className="mono ch-sub-item" title={conversation.peer_user_id || ''}>
                {conversation.peer_user_id || '未知买家 ID'}
              </span>
              <span className="ch-sub-item" title={conversation.item_id || ''}>
                <IconBox size={13} /> {itemTitle}
              </span>
              {accountName && (
                <span className="ch-sub-item" title={accountName}>
                  <IconUser size={13} /> {accountName}
                </span>
              )}
              {(blacklist.data || blacklist.error) && (
                <span
                  className={`ch-sub-item${blackState === true ? ' ch-sub-warn' : ''}`}
                  title={blackHint}
                >
                  <IconShield size={13} />
                  {blacklist.error
                    ? '拉黑状态没查到'
                    : blackState === true
                      ? '已拉黑'
                      : blackState === false
                        ? '未拉黑'
                        : '拉黑状态：平台未返回'}
                </span>
              )}
              <span
                className={`ch-sub-item${
                  receptionStatus && receptionStatus !== 'ONLINE' ? ' ch-sub-warn' : ''
                }`}
                title={receptionHint}
              >
                <IconWifi size={13} />
                接待：
                {reception.error
                  ? '没查到'
                  : receptionStatus
                    ? receptionText(receptionStatus)
                    : reception.loading
                      ? '查询中'
                      : '平台未返回'}
              </span>
            </div>
          </div>
        </div>
        <div className="ch-head-actions">
          <div className="ch-seg" role="group" aria-label="AI 模式">
            {MODES.map((option) => (
              <button
                key={option.value}
                type="button"
                className={`ch-seg-btn${conversation.mode === option.value ? ' active' : ''}`}
                disabled={modeBusy || conversation.mode === option.value}
                title={option.desc}
                onClick={() => void switchMode(option.value)}
              >
                <ModeIcon mode={option.value} />
                {option.label}
              </button>
            ))}
          </div>
          <button
            className={`btn sm${blackState === true ? ' ghost' : ' danger'}`}
            onClick={() => setConfirmKind(blackState === true ? 'remove' : 'add')}
            disabled={Boolean(imBusy)}
            title={blackHint}
          >
            <IconShield size={14} /> {blackState === true ? '解除拉黑' : '拉黑买家'}
          </button>
          <button
            className="btn sm ghost"
            onClick={openCard}
            disabled={Boolean(imBusy)}
            title="把这个商品以卡片形式发给买家（会真的发一条消息）"
          >
            <IconTag size={14} /> 发商品卡片
          </button>
          <button
            className="btn sm ghost"
            onClick={openTransfer}
            disabled={Boolean(imBusy)}
            title="把这个会话转交给其他客服接待（会真的改平台上的会话归属）"
          >
            <IconUsers size={14} /> 转交客服
          </button>
          <button
            className="btn sm ghost"
            onClick={() => setConfirmKind('leave')}
            disabled={Boolean(imBusy)}
            title="结束和这个买家的会话（结束后买家不能再发消息）"
          >
            <IconXCircle size={14} /> 结束会话
          </button>
          <button
            className="btn sm ghost"
            onClick={() => setConfirmKind('rejoin')}
            disabled={Boolean(imBusy)}
            title="重新发起和这个买家的聊天"
          >
            <IconChat size={14} /> 重新发起
          </button>
          {/* 接待状态（第 46 批）：账号级开关，选一个就弹二次确认（唯一写出口带 confirm: true） */}
          <select
            className="ch-im-select ch-im-reception"
            aria-label="切换接待状态"
            title={receptionHint}
            value={receptionStatus || ''}
            disabled={Boolean(imBusy) || reception.loading || receptionOptions.length === 0}
            onChange={(event) => {
              const option = receptionOptions.find((item) => item.status === event.target.value)
              if (option) askReception(option)
            }}
          >
            {!receptionStatus && <option value="">接待状态：没查到</option>}
            {receptionOptions.map((option) => (
              <option key={option.status} value={option.status}>
                接待：{option.label}
              </option>
            ))}
          </select>
          <button className="btn sm ghost" onClick={refresh}>
            <IconRefresh size={14} /> 刷新
          </button>
          <button
            className="btn sm ghost"
            onClick={openOfficial}
            disabled={!officialUrl}
            title={
              officialUrl
                ? '在闲鱼官方页面里自己发图 / 发语音 / 打电话（打开的是该商品的官方页面）'
                : '这个会话没有商品 id，拿不到官方页面地址（不猜地址）'
            }
          >
            <IconExternal size={14} /> 打开闲鱼官方页面
          </button>
        </div>
      </div>

      {/* 接待状态读失败或平台没返回时，把后端的中文原话摆出来（不静默、也不当成「离线」） */}
      {(reception.error || (reception.data && !receptionStatus)) && (
        <p className="ch-im-hint ch-im-warn ch-reception-hint">
          {reception.error
            ? `接待状态读失败：${reception.error}`
            : `平台没有返回接待状态${
                reception.data?.raw_status ? `（原样值：${reception.data.raw_status}）` : ''
              }，这里不当作「离线」。`}
        </p>
      )}

      <div
        className="ch-stream"
        ref={streamRef}
        onScroll={() => {
          const node = streamRef.current
          if (node) stickRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80
        }}
      >
        {messages.notReady && (
          <div className="ch-empty">
            <span className="ch-empty-icon">
              <IconInfo size={22} />
            </span>
            <span className="ch-empty-title">消息接口还没就绪</span>
            <span className="ch-empty-desc">后端会话消息接口尚未可用，消息流暂时无法显示。</span>
          </div>
        )}
        {messages.error && (
          <div className="ch-empty">
            <span className="ch-empty-icon">
              <IconWarn size={22} />
            </span>
            <span className="ch-empty-title">消息加载失败</span>
            <span className="ch-empty-desc">{messages.error}</span>
            <span className="ch-empty-actions">
              <button className="btn sm" onClick={refresh}>
                <IconRefresh size={14} /> 重试
              </button>
            </span>
          </div>
        )}
        {messages.loading && list.length === 0 && <MessageSkeleton />}
        {!messages.loading && !messages.notReady && !messages.error && list.length === 0 && (
          <div className="ch-empty">
            <span className="ch-empty-icon">
              <IconChat size={22} />
            </span>
            <span className="ch-empty-title">还没有消息</span>
            <span className="ch-empty-desc">买家发来消息后会出现在这里；你也可以在下面主动回一条。</span>
          </div>
        )}
        {list.map((message, index) => {
          const isOut = message.direction === 'out'
          const isHuman = message.sender === 'human'
          // `ai:<role>` 是主判据；后端 task-44 起还会用 `meta.ai_role` 兜底老数据
          const isAi = Boolean(
            (message.sender && message.sender.startsWith('ai:')) || (message.ai_role || '').trim(),
          )
          const failed = message.sent_ok === false
          const images = messageImages(message)
          const audioUrl = (message.audio_url || '').trim()
          const text = messageText(message)
          const aiBadge = isAi ? aiBadgeText(message) : ''
          const key = dayKey(message.created_at)
          const showDay = key !== dayKey(list[index - 1]?.created_at)
          return (
            <div key={message.id} style={{ display: 'contents' }}>
              {showDay && key && <div className="ch-day">{dayLabel(key)}</div>}
              <div
                className={`ch-msg ${isOut ? 'out' : 'in'}${isHuman ? ' human' : ''}${failed ? ' failed' : ''}`}
              >
                <div className="ch-msg-meta">
                  {images.length > 0 && <IconImage size={12} />}
                  <span className={`ch-sender-tag${isHuman ? ' human' : isAi ? ' ai' : ''}`}>
                    {senderLabel(message.sender)}
                  </span>
                  <span>{formatTime(message.created_at)}</span>
                </div>
                {images.length > 0 && (
                  <div className={`ch-msg-media${images.length > 1 ? ' multi' : ''}`}>
                    {images.map((url, imageIndex) => (
                      <Thumb
                        key={`${message.id}-img-${imageIndex}`}
                        url={url}
                        onOpen={() =>
                          setViewer({
                            urls: images,
                            start: imageIndex,
                            note: isOut ? '我发出的图片' : '买家发来的图片',
                          })
                        }
                      />
                    ))}
                  </div>
                )}
                {audioUrl && <AudioMessage url={audioUrl} duration={message.audio_duration} />}
                {text && <div className="ch-msg-bubble">{text}</div>}
                {aiBadge && (
                  <div className="ch-ai-badge" title={`本次回复：${aiBadge}`}>
                    <IconSparkle size={12} /> {aiBadge}
                  </div>
                )}
                {failed && (
                  <div className="ch-msg-failed">
                    <IconWarn size={13} /> 发送失败：没有真正发给买家
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div className="ch-compose">
        <div className="ch-compose-top">
          <Switch
            checked={conversation.mode === 'auto'}
            disabled={modeBusy}
            onChange={(next) => void switchMode(next ? 'auto' : 'manual')}
            label={
              conversation.mode === 'auto'
                ? '本会话 AI 自动回复：开'
                : conversation.mode === 'off'
                  ? '本会话 AI 自动回复：已关闭'
                  : '本会话 AI 自动回复：关（人工接管中）'
            }
          />
          <span className="ch-compose-hint">
            {conversation.mode === 'auto'
              ? '你发的消息也会直接发给买家，AI 不会抢话'
              : conversation.mode === 'off'
                ? '这个会话的 AI 完全关闭了，只有你发的消息会发出去'
                : 'AI 不会自动回复，你发的内容会直接发给买家'}
          </span>
        </div>
        <textarea
          className="ch-textarea"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="在这里自己回消息，按 Ctrl+Enter 发送…"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <div className="ch-compose-bottom">
          <button className="btn primary" onClick={send} disabled={sending || !input.trim()}>
            <IconSend size={15} /> {sending ? '发送中…' : '发送'}
          </button>
          <button className="btn" onClick={draft} disabled={drafting}>
            <IconSparkle size={15} /> {drafting ? '生成中…' : 'AI 帮我拟一条'}
          </button>
          <span className="ch-compose-tip">
            <span className="ch-kbd">Ctrl</span> + <span className="ch-kbd">Enter</span> 发送 · 发图 / 发语音 / 打电话
            请用上方「打开闲鱼官方页面」
          </span>
        </div>
      </div>

      {viewer && (
        <ImageViewer
          urls={viewer.urls}
          start={viewer.start}
          title={name || '查看图片'}
          note={viewer.note}
          onClose={() => setViewer(null)}
        />
      )}

      {transferOpen && (
        <Modal
          title="转交会话"
          onClose={() => setTransferOpen(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setTransferOpen(false)}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={() => setConfirmKind('transfer')}
                disabled={Boolean(imBusy) || !targetUid.trim()}
              >
                <IconUsers size={14} /> 转交
              </button>
            </>
          }
        >
          {agents.notReady && (
            <p className="ch-im-hint ch-im-warn">客服列表接口还没就绪，暂时拿不到可转交的客服。</p>
          )}
          {agents.error && <p className="ch-im-hint ch-im-warn">客服列表加载失败：{agents.error}</p>}
          {agents.loading && <p className="ch-im-hint">正在拉取客服列表…</p>}
          {!agents.loading && !agents.error && !agents.notReady && agentList === null && (
            <p className="ch-im-hint ch-im-warn">
              平台没有返回客服列表（可能这个账号没有该权限），拿不到可转交的客服 uid。
            </p>
          )}
          {agentList && agentList.length === 0 && (
            <p className="ch-im-hint ch-im-warn">
              这个店还没有客服账号，先到「小铺 · 客服分流」里加客服分组。
            </p>
          )}
          {agentList && agentList.length > 0 && (
            <Field label="转交给哪个客服" tip="uid 直接取自客服列表，不猜">
              <select
                className="ch-im-select"
                value={targetUid}
                onChange={(e) => setTargetUid(e.target.value)}
              >
                <option value="">请选择客服</option>
                {agentList.map((agent, index) => (
                  <option key={`${String(agent.customerUid ?? '')}-${index}`} value={String(agent.customerUid ?? '')}>
                    {agentLabel(agent)}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <p className="ch-im-hint">
            转交后由对方接待这个买家；点「转交」还会再弹一次确认框，确认后才真的发到平台。
          </p>
        </Modal>
      )}

      {cardOpen && (
        <Modal
          title="发商品卡片"
          onClose={() => setCardOpen(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setCardOpen(false)}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={() => setConfirmKind('card')}
                disabled={Boolean(imBusy) || !cardItemId.trim()}
              >
                <IconCheck size={14} /> 发送
              </button>
            </>
          }
        >
          <Field label="商品 id" tip="默认填当前会话关联的商品；也可以改成别的商品 id（不猜 id）">
            <input
              type="text"
              value={cardItemId}
              onChange={(e) => setCardItemId(e.target.value)}
              placeholder="例如 812345678901"
            />
          </Field>
          <p className="ch-im-hint">发出去就是给对方的一条真实消息，发完不能撤回。</p>
        </Modal>
      )}

      {/*
        确认框必须放在**所有弹窗之后**：两者都是 `position: fixed` 且 z-index 相同，
        同级元素里后渲染的盖在上面。放在前面时，「转交会话」弹窗会盖住确认框 ——
        用户点「转交」后什么都看不到（第 43 批界面取证抓到的真缺陷）。
      */}
      {confirmKind && (
        <ConfirmDialog
          title={confirmView.title}
          message={confirmView.message}
          confirmText={confirmView.ok}
          danger={confirmView.danger}
          busy={Boolean(imBusy)}
          onConfirm={
            confirmKind === 'add' || confirmKind === 'remove' ? toggleBlacklist : confirmSessionAction
          }
          onCancel={() => setConfirmKind('')}
        />
      )}
    </>
  )
}
