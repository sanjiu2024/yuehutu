/** 会话：左列表右聊天窗；人工接管输入框 + 本会话 AI 开关 + 消息流。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, errorText, isNotReady } from '../api'
import { AsyncBox, Badge, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import { formatClock, formatTime, modeLabel, senderLabel, truncate } from '../format'
import { subscribeTopics } from '../ws'
import type {
  Account,
  ClassifyResult,
  Conversation,
  ConversationListResult,
  Message,
  MessageListResult,
  ModeResult,
  SendResult,
} from '../types'

const MODES: Array<{ value: string; label: string; desc: string }> = [
  { value: 'auto', label: 'AI 自动回复', desc: '买家消息由 AI 处理，你可以随时插话' },
  { value: 'manual', label: '人工接管', desc: 'AI 让位，只有你发的消息会发出去' },
  { value: 'off', label: 'AI 关闭', desc: 'AI 完全不参与这个会话' },
]

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

export function ChatsPage() {
  const toast = useToast()
  const accounts = useAsync<Account[]>(() => api.get<Account[]>('/api/accounts'), [])
  const [accountId, setAccountId] = useState('')
  const [modeFilter, setModeFilter] = useState('')
  const [activeId, setActiveId] = useState<number | null>(null)

  const conversations = useAsync<ConversationListResult>(
    () =>
      api.get<ConversationListResult>('/api/chats/conversations', {
        account_id: accountId || undefined,
        mode: modeFilter || undefined,
        limit: 200,
      }),
    [accountId, modeFilter],
  )

  const list = conversations.data?.conversations ?? []
  const active = useMemo(() => list.find((c) => c.id === activeId) ?? null, [list, activeId])

  // 选中第一条
  useEffect(() => {
    if (activeId === null && list.length > 0) setActiveId(list[0].id)
    if (activeId !== null && list.length > 0 && !list.some((c) => c.id === activeId)) setActiveId(list[0].id)
  }, [list, activeId])

  // WS 实时刷新。后端真实 topic 是 `chats.message` / `chats.mode_changed` /
  // `chats.ai_reply` / `risk.changed` / `items.updated`。
  useEffect(() => {
    return subscribeTopics(
      ['chats.message', 'chats.mode_changed', 'chats.ai_reply', 'items.updated', 'risk.changed'],
      (event) => {
        conversations.reload()
        if (event.topic === 'chats.message') {
          const payload = event.payload as { conversation_id?: number }
          if (payload?.conversation_id && payload.conversation_id === activeId) {
            window.dispatchEvent(new CustomEvent('xywb:refresh-messages'))
          }
        }
      },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  const setMode = async (conversation: Conversation, mode: string) => {
    try {
      const result = await api.post<ModeResult>(`/api/chats/${conversation.id}/mode`, { mode })
      toast.success(`已切换为「${modeLabel(result?.mode || mode).text}」`)
      conversations.reload()
    } catch (err) {
      toast.error(errorText(err))
    }
  }

  // 把**平台上已经存在**的历史会话拉回本地。IM 入站只收「正在发生」的推送，
  // 进程启动前就有的会话不会自己进库 —— 不点这个按钮，界面就一直是空的。
  const [syncing, setSyncing] = useState(false)
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
      }>('/api/chats/sync', { account_id: accountId, limit: 50, message_limit: 20 })
      toast.success(
        `同步完成：扫描 ${result?.scanned ?? 0} 个会话，新增 ${result?.conversations_new ?? 0} 个会话、` +
          `${result?.messages ?? 0} 条消息`,
      )
      conversations.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="toolbar">
        <select value={accountId} onChange={(e) => setAccountId(e.target.value)} style={{ width: 180 }}>
          <option value="">全部账号</option>
          {(accounts.data ?? []).map((account) => (
            <option key={account.id} value={account.id}>
              {account.nickname || account.id}
            </option>
          ))}
        </select>
        <select value={modeFilter} onChange={(e) => setModeFilter(e.target.value)} style={{ width: 150 }}>
          <option value="">全部模式</option>
          {MODES.map((mode) => (
            <option key={mode.value} value={mode.value}>
              {mode.label}
            </option>
          ))}
        </select>
        <button className="btn" onClick={conversations.reload}>
          刷新
        </button>
        <button className="btn" onClick={syncHistory} disabled={syncing}>
          {syncing ? '同步中…' : '同步历史会话'}
        </button>
        <span className="spacer" />
        <span className="dim" style={{ fontSize: 12 }}>
          共 {list.length} 个会话
        </span>
      </div>

      <div className="chat-layout" style={{ flex: 1, minHeight: 0 }}>
        <div className="chat-list">
          <AsyncBox
            loading={conversations.loading}
            error={conversations.error}
            notReady={conversations.notReady}
            empty={list.length === 0}
            emptyIcon="💬"
            emptyTitle="没有会话"
            emptyDesc="账号在线并收到买家消息后，会话会出现在这里；平台上已有的历史会话请点上面「同步历史会话」。"
            onRetry={conversations.reload}
          >
            {list.map((conversation) => {
              const mode = modeLabel(conversation.mode)
              return (
                <div
                  key={conversation.id}
                  className={`chat-list-item${conversation.id === activeId ? ' active' : ''}`}
                  onClick={() => setActiveId(conversation.id)}
                >
                  <div className="top">
                    <span className="name">{conversation.peer_nickname || conversation.peer_user_id || '买家'}</span>
                    {Number(conversation.unread) > 0 && <span className="nav-badge">{conversation.unread}</span>}
                  </div>
                  <div className="preview">{lastMessageText(conversation)}</div>
                  <div className="row" style={{ marginTop: 5, gap: 6 }}>
                    <Badge tone={mode.tone}>{mode.text}</Badge>
                    {conversation.item_id && (
                      <span className="mono dim" style={{ fontSize: 10.5 }}>
                        {truncate(conversation.item_id, 14)}
                      </span>
                    )}
                    <span className="dim" style={{ fontSize: 10.5, marginLeft: 'auto' }}>
                      {formatClock(conversation.last_message_at)}
                    </span>
                  </div>
                </div>
              )
            })}
          </AsyncBox>
        </div>

        <div className="chat-panel">
          {active ? (
            <ChatWindow key={active.id} conversation={active} onModeChange={setMode} onRefreshList={conversations.reload} />
          ) : (
            <div className="empty" style={{ margin: 'auto' }}>
              <div className="big">💬</div>
              <div className="title">选择左侧的一个会话</div>
              <div className="desc">
                你可以看到完整消息流，
                <br />
                随时接管自己回消息，或把会话交回给 AI。
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ---------------- 单个会话窗口 ---------------- */

function ChatWindow({
  conversation,
  onModeChange,
  onRefreshList,
}: {
  conversation: Conversation
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

  const messages = useAsync<MessageListResult>(
    () => api.get<MessageListResult>(`/api/chats/${conversation.id}/messages`, { limit: 200 }),
    [conversation.id, tick],
  )

  const list = messages.data?.messages ?? []

  // 滚到底部
  useEffect(() => {
    const node = streamRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [list.length])

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
        const result = await api.post<{ draft?: string }>(
          `/api/chats/${conversation.id}/ai-draft`,
          {},
        )
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

  const mode = modeLabel(conversation.mode)

  return (
    <>
      <div className="chat-head">
        <span className="who">{conversation.peer_nickname || conversation.peer_user_id || '买家'}</span>
        <Badge tone={mode.tone}>{mode.text}</Badge>
        {conversation.item_id && <span className="mono dim" style={{ fontSize: 11 }}>商品 {conversation.item_id}</span>}
        <span style={{ flex: 1 }} />
        <div className="row" style={{ gap: 6 }}>
          {MODES.map((option) => (
            <button
              key={option.value}
              className={`btn sm${conversation.mode === option.value ? ' primary' : ''}`}
              disabled={modeBusy || conversation.mode === option.value}
              title={option.desc}
              onClick={() => switchMode(option.value)}
            >
              {option.label}
            </button>
          ))}
          <button className="btn sm ghost" onClick={refresh}>
            刷新
          </button>
        </div>
      </div>

      <div className="chat-stream" ref={streamRef}>
        {messages.notReady && <Notice kind="warn">会话消息接口还没就绪，消息流暂时无法显示。</Notice>}
        {messages.error && <Notice kind="error" onRetry={refresh}>{messages.error}</Notice>}
        {messages.loading && list.length === 0 && (
          <div className="loading-box">
            <span className="spinner" />
            <span>加载消息…</span>
          </div>
        )}
        {!messages.loading && !messages.notReady && !messages.error && list.length === 0 && (
          <div className="empty">
            <div className="big">🗨</div>
            <div className="title">还没有消息</div>
            <div className="desc">买家发来消息后会出现在这里。</div>
          </div>
        )}
        {list.map((message) => {
          const isOut = message.direction === 'out'
          const isHuman = message.sender === 'human'
          return (
            <div key={message.id} className={`msg ${isOut ? 'out' : 'in'}${isHuman ? ' human' : ''}`}>
              <div className="meta">
                {senderLabel(message.sender)} · {formatTime(message.created_at)}
              </div>
              <div className="bubble">{message.content || '(空消息)'}</div>
              {message.sent_ok === false && <div className="failed">发送失败</div>}
            </div>
          )
        })}
      </div>

      <div className="chat-compose">
        <div className="compose-tools">
          <Switch
            checked={conversation.mode === 'auto'}
            disabled={modeBusy}
            onChange={(next) => switchMode(next ? 'auto' : 'manual')}
            label={conversation.mode === 'auto' ? '本会话 AI 自动回复：开' : '本会话 AI 自动回复：关（人工接管中）'}
          />
          <span className="spacer" />
          <span className="dim" style={{ fontSize: 11.5 }}>
            {conversation.mode === 'auto'
              ? '你发的消息也会直接发给买家，AI 不会抢话'
              : 'AI 不会自动回复，你发的内容会直接发给买家'}
          </span>
        </div>
        <textarea
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
        <div className="compose-tools">
          <button className="btn primary" onClick={send} disabled={sending || !input.trim()}>
            {sending ? '发送中…' : '发送'}
          </button>
          <button className="btn" onClick={draft} disabled={drafting}>
            {drafting ? '生成中…' : 'AI 帮我拟一条'}
          </button>
          <span className="spacer" />
          <span className="dim" style={{ fontSize: 11.5 }}>Ctrl+Enter 发送</span>
        </div>
      </div>
    </>
  )
}
