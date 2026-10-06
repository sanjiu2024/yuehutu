/**
 * 主管 AI：和 master 角色对话（POST /api/agents/master/chat）。
 *
 * 样式全部来自同目录 `master.css`（前缀 `ms-`）：`styles.css` 正在被重写，
 * 这里只依赖 `.btn`/`.row`/`.col`/`.grow`/`.dim`/`.mono`/`.ellipsis` 这几个稳定公共 class。
 * 对话记录只活在本页 state 里（后端没有会话持久化），刷新即丢。
 */

import { useEffect, useRef, useState } from 'react'
import { api, errorText } from '../api'
import { ConfirmDialog, Notice } from '../components'
import { formatTime, prettyJson, roleName } from '../format'
import { IconChevronDown, IconChevronRight, IconCopy, IconMaster, IconSend, IconTrash } from '../icons'
import './master.css'

interface ChatEntry {
  id: number
  role: 'user' | 'assistant'
  content: string
  toolCalls?: unknown[]
  at: string
  /** 这条用户消息没发出去（后端报错）——必须让用户看见，不能假装已送达。 */
  failed?: boolean
}

/** 后端 `{name, args, result}` 的形状（runtime.py:429）；同时容忍 OpenAI 原始结构。 */
interface ToolCallView {
  name: string
  args: unknown
  result: unknown
  hasResult: boolean
}

const SAMPLE_QUESTIONS = [
  '今天有哪些商品需要调价？',
  '现在有几个账号在线？',
  '哪些商品卖得好？给我个建议',
  '现在风控状态怎么样？',
]

let seq = 0

/** 后端 tool_calls 已经是展开后的结构，但历史/上游可能给 OpenAI 原始形状，两种都认。 */
function readToolCall(raw: unknown, index: number): ToolCallView {
  const record: Record<string, unknown> = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const fn: Record<string, unknown> =
    record.function && typeof record.function === 'object' ? (record.function as Record<string, unknown>) : {}
  const name = String(record.name ?? record.tool ?? fn.name ?? `tool_${index + 1}`)
  let args: unknown = record.args ?? record.arguments ?? fn.arguments ?? {}
  if (typeof args === 'string') {
    // OpenAI 把 arguments 传成 JSON 字符串；解析失败就原样展示，不吞内容
    try {
      args = JSON.parse(args)
    } catch {
      /* 保留原始字符串 */
    }
  }
  const hasResult = 'result' in record || 'output' in record || 'response' in record
  return { name, args, result: record.result ?? record.output ?? record.response, hasResult }
}

/**
 * 只按 ``` 围栏切分正文，不引 markdown 库（超出写入范围）。
 * 行内反引号、表格等一律按纯文本显示。
 */
function splitFences(text: string): Array<{ code: boolean; lang: string; text: string }> {
  const parts: Array<{ code: boolean; lang: string; text: string }> = []
  let buffer: string[] = []
  let code = false
  let lang = ''
  const flush = () => {
    const body = buffer.join('\n')
    if (body.trim() !== '' || code) parts.push({ code, lang, text: body })
    buffer = []
  }
  for (const line of text.split('\n')) {
    const fence = /^\s*```(.*)$/.exec(line)
    if (fence) {
      flush()
      if (!code) lang = fence[1].trim()
      code = !code
      continue
    }
    buffer.push(line)
  }
  flush()
  return parts
}

/** 剪贴板在非安全上下文/权限被拒时不可用：静默失败，绝不打断对话。 */
async function copyText(text: string): Promise<boolean> {
  try {
    const clipboard = navigator.clipboard
    if (clipboard && typeof clipboard.writeText === 'function') {
      await clipboard.writeText(text)
      return true
    }
  } catch {
    /* 静默兜底 */
  }
  return false
}

function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      className="btn sm ghost ms-copy"
      title={label}
      onClick={() => {
        void copyText(text).then((ok) => {
          if (ok) setCopied(true)
        })
      }}
    >
      <IconCopy size={13} />
      {copied ? '已复制' : label}
    </button>
  )
}

function CodeBlock({ text, lang }: { text: string; lang?: string }) {
  return (
    <div className="ms-code-wrap">
      <div className="ms-code-bar">
        {lang ? <span className="ms-code-lang mono">{lang}</span> : <span />}
        <CopyButton text={text} />
      </div>
      <pre className="ms-code mono">{text}</pre>
    </div>
  )
}

/** AI 正文：普通段落 pre-wrap，``` 围栏用等宽代码块。 */
function MessageBody({ text }: { text: string }) {
  return (
    <>
      {splitFences(text).map((part, index) =>
        part.code ? (
          <CodeBlock key={index} text={part.text} lang={part.lang} />
        ) : (
          <div key={index} className="ms-text">
            {part.text}
          </div>
        ),
      )}
    </>
  )
}

function ToolCallList({ calls }: { calls: unknown[] }) {
  const views = calls.map(readToolCall)
  const preview = views
    .slice(0, 3)
    .map((view) => view.name)
    .join('、')
  return (
    <details className="ms-tools">
      <summary className="ms-tools-head">
        <IconChevronRight size={14} className="ms-tools-arrow" />
        <span className="ms-tools-count">调用了 {views.length} 个工具</span>
        <span className="ms-tools-names mono">
          {preview}
          {views.length > 3 ? ` 等 ${views.length} 个` : ''}
        </span>
      </summary>
      <div className="ms-tools-body">
        {views.map((view, index) => {
          const argsText = prettyJson(view.args) || '{}'
          const resultText = view.hasResult ? prettyJson(view.result) || '(空)' : ''
          const bundle = view.hasResult
            ? `${view.name}\n入参：\n${argsText}\n返回：\n${resultText}`
            : `${view.name}\n入参：\n${argsText}`
          return (
            <div className="ms-tool" key={`${view.name}-${index}`}>
              <div className="ms-tool-head">
                <span className="ms-chip mono">{view.name}</span>
                <span className="grow" />
                <CopyButton text={bundle} label="复制调用" />
              </div>
              <div className="ms-tool-label">入参</div>
              <pre className="ms-code mono">{argsText}</pre>
              {view.hasResult && (
                <>
                  <div className="ms-tool-label">返回</div>
                  <pre className="ms-code mono">{resultText}</pre>
                </>
              )}
            </div>
          )
        })}
      </div>
    </details>
  )
}

export function MasterPage() {
  const [entries, setEntries] = useState<ChatEntry[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  /** 后端返回的原始错误 message，原样展示，不美化 */
  const [failedText, setFailedText] = useState('')
  const [askClear, setAskClear] = useState(false)
  const [atBottom, setAtBottom] = useState(true)
  const streamRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const scrollToBottom = (smooth: boolean) => {
    const node = streamRef.current
    if (!node) return
    node.scrollTo({ top: node.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }

  // 贴底时跟随新消息；用户手动往上翻（atBottom=false）时不抢滚动位置
  useEffect(() => {
    if (atBottom) scrollToBottom(false)
  }, [entries, sending, atBottom])

  // 输入框高度自适应，封顶 160px（再高就内部滚动）
  useEffect(() => {
    const node = inputRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, 160)}px`
  }, [input])

  const onStreamScroll = () => {
    const node = streamRef.current
    if (!node) return
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight
    setAtBottom(distance < 48)
  }

  const send = async (raw?: string, retryId?: number) => {
    const content = (raw ?? input).trim()
    if (!content || sending) return
    const userEntryId = retryId ?? ++seq
    setSending(true)
    setFailedText('')
    if (retryId === undefined) {
      const entry: ChatEntry = { id: userEntryId, role: 'user', content, at: new Date().toISOString() }
      setEntries((prev) => [...prev, entry])
    } else {
      // 重发：原地复活那条失败的用户消息，不新增气泡
      setEntries((prev) =>
        prev.map((item) => (item.id === retryId ? { ...item, failed: false, at: new Date().toISOString() } : item)),
      )
    }
    setInput('')
    try {
      const result = await api.post<{ reply?: string; tool_calls?: unknown[] }>('/api/agents/master/chat', { message: content })
      setEntries((prev) => [
        ...prev,
        {
          id: ++seq,
          role: 'assistant',
          content: result?.reply?.trim() || '(主管 AI 没有返回内容)',
          toolCalls: result?.tool_calls,
          at: new Date().toISOString(),
        },
      ])
    } catch (err) {
      const message = errorText(err)
      setFailedText(message)
      // 把那条用户消息标成未送达，避免用户以为已经发出去了
      setEntries((prev) => prev.map((item) => (item.id === userEntryId ? { ...item, failed: true } : item)))
    } finally {
      setSending(false)
    }
  }

  const pickSample = (text: string) => {
    setInput(text)
    inputRef.current?.focus()
  }

  return (
    <div className="ms-page">
      <div className="ms-head">
        <div>
          <div className="ms-title">主管 AI</div>
          <div className="ms-sub">
            唯一能调用工具的角色：查账号、改商品、看风控、发消息，并把活派给各个商品的 AI。
            它需要先在「AI 配置」里指定可用的模型。
          </div>
        </div>
        <div className="ms-head-actions">
          <button className="btn sm" onClick={() => setAskClear(true)} disabled={entries.length === 0}>
            <IconTrash size={14} />
            清空对话
          </button>
        </div>
      </div>

      <div className="ms-card ms-chat">
        <div className="ms-card-head">
          <div className="ms-card-title">
            <IconMaster size={15} />
            对话
          </div>
          <span className="ms-chat-tip">对话只保存在本页，刷新即丢失</span>
        </div>

        <div className="ms-stream-wrap">
          <div className="ms-stream ms-scroll" ref={streamRef} onScroll={onStreamScroll}>
            {entries.length === 0 && (
              <div className="ms-empty">
                <span className="ms-empty-icon">
                  <IconMaster size={22} />
                </span>
                <div className="ms-empty-title">和主管 AI 说点什么</div>
                <div className="ms-empty-desc">
                  比如「今天有哪些商品需要调价」。它会自己挑工具去查、去改，再把结果讲清楚。
                  <br />
                  每次对话都会真实调用大模型。
                </div>
                <div className="ms-samples">
                  {SAMPLE_QUESTIONS.map((question) => (
                    <button key={question} className="btn sm ms-sample" onClick={() => pickSample(question)}>
                      {question}
                    </button>
                  ))}
                </div>
                <div className="ms-empty-note">点示例只会填进输入框，确认后再自己按发送</div>
              </div>
            )}

            {entries.map((entry) => (
              <div key={entry.id} className={`ms-msg ${entry.role}${entry.failed ? ' failed' : ''}`}>
                <div className="ms-bubble">
                  {entry.role === 'assistant' ? (
                    <MessageBody text={entry.content} />
                  ) : (
                    <div className="ms-text">{entry.content}</div>
                  )}
                  {entry.toolCalls && entry.toolCalls.length > 0 && <ToolCallList calls={entry.toolCalls} />}
                  {entry.failed && (
                    <div className="ms-retry">
                      <button className="btn sm" onClick={() => void send(entry.content, entry.id)} disabled={sending}>
                        重发这条
                      </button>
                    </div>
                  )}
                </div>
                {/* 时间戳放在气泡下方（规范要求），淡色 12px */}
                <div className="ms-meta">
                  {entry.role === 'user' ? '我' : roleName('master')} · {formatTime(entry.at)}
                  {entry.failed && <span className="ms-failed-tag">未送达</span>}
                </div>
              </div>
            ))}

            {sending && (
              <div className="ms-msg assistant">
                <div className="ms-bubble ms-thinking">
                  <span className="ms-dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <span>主管 AI 思考中…</span>
                </div>
                <div className="ms-meta">{roleName('master')} · 正在回复</div>
              </div>
            )}
          </div>

          {!atBottom && (
            <button className="ms-jump" onClick={() => scrollToBottom(true)} title="回到最新">
              <IconChevronDown size={16} />
            </button>
          )}
        </div>

        <div className="ms-compose">
          {failedText && (
            <Notice kind="error" onClose={() => setFailedText('')}>
              {failedText}
            </Notice>
          )}
          <textarea
            ref={inputRef}
            className="ms-input"
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="告诉主管 AI 你想做什么，例如「把标题带『旧』的商品下架」…（Ctrl+Enter 发送）"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault()
                void send()
              }
            }}
          />
          <div className="ms-compose-foot">
            <span className="ms-hint">Enter 换行 · Ctrl+Enter 发送 · 每次对话都会真实调用大模型并计费</span>
            <span className="grow" />
            <button className="btn primary" onClick={() => void send()} disabled={sending || !input.trim()}>
              <IconSend size={14} />
              {sending ? '发送中…' : '发送'}
            </button>
          </div>
        </div>
      </div>

      {askClear && (
        <ConfirmDialog
          title="清空对话"
          message="只清空本页显示的消息，后端没有保存过这段对话。已发出的请求无法撤回。"
          confirmText="清空"
          onConfirm={() => {
            setEntries([])
            setFailedText('')
            setAskClear(false)
          }}
          onCancel={() => setAskClear(false)}
        />
      )}
    </div>
  )
}
