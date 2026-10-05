/** 主管 AI：和 master 角色对话（POST /api/agents/master/chat）。 */

import { useEffect, useRef, useState } from 'react'
import { api, errorText } from '../api'
import { Badge, Notice, useToast } from '../components'
import { formatTime, roleName } from '../format'

interface ChatEntry {
  id: number
  role: 'user' | 'assistant'
  content: string
  toolCalls?: unknown[]
  at: string
  error?: boolean
}

const QUICK_PROMPTS = [
  '现在有几个账号在线？',
  '帮我看看今天有哪些买家在问价',
  '把在售商品的标题统一加上「全新」前缀',
  '哪些商品卖得好？给我个建议',
  '现在风控状态怎么样？',
]

let seq = 0

export function MasterPage() {
  const toast = useToast()
  const [entries, setEntries] = useState<ChatEntry[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const streamRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = streamRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [entries.length])

  const send = async (text?: string) => {
    const content = (text ?? input).trim()
    if (!content || sending) return
    const now = new Date().toISOString()
    setEntries((prev) => [...prev, { id: ++seq, role: 'user', content, at: now }])
    setInput('')
    setSending(true)
    try {
      const result = await api.post<{ reply?: string; tool_calls?: unknown[] }>('/api/agents/master/chat', { message: content })
      setEntries((prev) => [
        ...prev,
        {
          id: ++seq,
          role: 'assistant',
          content: result?.reply || '(主管 AI 没有返回内容)',
          toolCalls: result?.tool_calls,
          at: new Date().toISOString(),
        },
      ])
    } catch (err) {
      const message = errorText(err)
      setEntries((prev) => [
        ...prev,
        {
          id: ++seq,
          role: 'assistant',
          content: `调用主管 AI 失败：${message}`,
          at: new Date().toISOString(),
          error: true,
        },
      ])
      toast.error(message)
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="master-layout" style={{ height: '100%' }}>
      <Notice kind="info">
        主管 AI（角色 <span className="mono">master</span>）能调用工具帮你干活：查账号、改商品、看风控、发消息。
        它需要先在「AI 配置」里指定可用的模型。
      </Notice>

      <div className="master-stream" ref={streamRef}>
        {entries.length === 0 && (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="big">🧠</div>
            <div className="title">和主管 AI 说点什么</div>
            <div className="desc">
              它是你唯一的对话入口，会把任务派给各个商品的 AI。
              <br />
              试试下面的快捷问题，或直接在下方输入。
            </div>
            <div className="row wrap" style={{ justifyContent: 'center', marginTop: 14, gap: 8 }}>
              {QUICK_PROMPTS.map((prompt) => (
                <button key={prompt} className="btn sm" onClick={() => void send(prompt)}>
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        )}

        {entries.map((entry) => (
          <div key={entry.id} className={`master-msg ${entry.role}`}>
            <div className="meta dim" style={{ fontSize: 11, padding: '0 3px' }}>
              {entry.role === 'user' ? '我' : roleName('master')} · {formatTime(entry.at)}
            </div>
            <div className="bubble" style={entry.error ? { borderColor: 'var(--danger)' } : undefined}>
              {entry.content}
              {entry.toolCalls && entry.toolCalls.length > 0 && (
                <div className="tool-calls">
                  <div className="dim">调用了 {entry.toolCalls.length} 个工具：</div>
                  {entry.toolCalls.map((call, index) => {
                    const record = (call ?? {}) as Record<string, unknown>
                    const name = String(record.name ?? record.tool ?? record.function ?? `tool_${index + 1}`)
                    return (
                      <div key={index} className="tc">
                        <Badge tone="muted">{name}</Badge>{' '}
                        <span className="mono">{JSON.stringify(record.arguments ?? record.args ?? '')}</span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </div>
        ))}

        {sending && (
          <div className="master-msg assistant">
            <div className="bubble row" style={{ gap: 8 }}>
              <span className="spinner" />
              <span>主管 AI 思考中…</span>
            </div>
          </div>
        )}
      </div>

      <div className="chat-compose" style={{ borderTop: '1px solid var(--border-soft)', borderRadius: 'var(--radius)' }}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="告诉主管 AI 你想做什么，例如「把标题带‘旧’的商品下架」…"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <div className="compose-tools">
          <button className="btn primary" onClick={() => void send()} disabled={sending || !input.trim()}>
            {sending ? '发送中…' : '发送'}
          </button>
          <button className="btn" onClick={() => setEntries([])} disabled={entries.length === 0}>
            清空对话
          </button>
          <span className="spacer" />
          <span className="dim" style={{ fontSize: 11.5 }}>Ctrl+Enter 发送 · 对话不会保存到后端</span>
        </div>
      </div>
    </div>
  )
}
