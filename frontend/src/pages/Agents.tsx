/** AI 沟通：agent_messages 时间线 —— 一眼看出「商品 A 的 AI 去问了商品 B 的 AI」。
 *
 * 后端落库的是扁平的 from→to 消息（kind = ask|answer|notify）。
 * 这里按 (conversation_id, topic) 聚合成「协查链」，把一次跨商品问询的
 * 完整往返画成一条链：item:A → cross → item:B → cross → item:A。
 */

import { useMemo, useState } from 'react'
import { api } from '../api'
import { AsyncBox, Badge, Notice, Switch } from '../components'
import { useAsync } from '../hooks/useAsync'
import { agentLabel, formatTime, kindLabel, payloadSummary, prettyJson, roleName } from '../format'
import type { AgentMessage, AgentRun } from '../types'

interface Chain {
  key: string
  conversationId?: number
  topic?: string
  messages: AgentMessage[]
  /** 参与的角色集合（不含 cross），用于判断是不是跨商品协查 */
  roles: string[]
  /** 涉及的商品 item_id */
  items: string[]
  startedAt?: string
}

function splitAgent(agent?: string): { role: string; itemId: string } {
  if (!agent) return { role: '', itemId: '' }
  const [role, itemId = ''] = agent.split(':')
  return { role, itemId }
}

export function AgentsPage() {
  const [kindFilter, setKindFilter] = useState('')
  const [onlyCross, setOnlyCross] = useState(false)
  const [showRaw, setShowRaw] = useState(false)
  const [tab, setTab] = useState<'timeline' | 'runs'>('timeline')

  const messages = useAsync<AgentMessage[]>(() => api.get<AgentMessage[]>('/api/agents/messages'), [], true, [
    'agent.message',
    'agent.cross_failed',
    'agent.reply_failed',
  ])
  const runs = useAsync<AgentRun[]>(() => api.get<AgentRun[]>('/api/agents/runs', { limit: 100 }), [], true, [
    'agent.message',
    'agent.reply_failed',
    'chats.ai_reply',
  ])

  const list = useMemo(() => {
    const raw = messages.data ?? []
    return [...raw].sort((a, b) => (a.id ?? 0) - (b.id ?? 0))
  }, [messages.data])

  const chains = useMemo<Chain[]>(() => {
    const groups = new Map<string, AgentMessage[]>()
    for (const message of list) {
      const key = `${message.conversation_id ?? 'x'}::${message.topic ?? ''}`
      const bucket = groups.get(key)
      if (bucket) bucket.push(message)
      else groups.set(key, [message])
    }
    const result: Chain[] = []
    groups.forEach((group, key) => {
      const items: string[] = []
      const roles = new Set<string>()
      for (const message of group) {
        for (const agent of [message.from_agent, message.to_agent]) {
          const { role, itemId } = splitAgent(agent)
          if (role && role !== 'cross' && role !== 'master') roles.add(role)
          if (itemId && !items.includes(itemId)) items.push(itemId)
        }
      }
      result.push({
        key,
        conversationId: group[0]?.conversation_id ?? undefined,
        topic: group[0]?.topic,
        messages: group,
        roles: [...roles],
        items,
        startedAt: group[0]?.created_at,
      })
    })
    // 新的在前
    return result.sort((a, b) => {
      const first = a.messages[0]?.id ?? 0
      const second = b.messages[0]?.id ?? 0
      return second - first
    })
  }, [list])

  const visibleChains = useMemo(() => {
    let result = chains
    if (kindFilter) {
      result = result.map((chain) => ({ ...chain, messages: chain.messages.filter((m) => m.kind === kindFilter) }))
        .filter((chain) => chain.messages.length > 0)
    }
    if (onlyCross) {
      // 跨商品协查：至少两个不同商品参与
      result = result.filter((chain) => chain.items.length >= 2 || chain.messages.some((m) => splitAgent(m.to_agent).role === 'cross'))
    }
    return result
  }, [chains, kindFilter, onlyCross])

  const crossCount = chains.filter(
    (chain) => chain.items.length >= 2 || chain.messages.some((m) => splitAgent(m.to_agent).role === 'cross'),
  ).length

  return (
    <div>
      <div className="tabs">
        <button className={`tab${tab === 'timeline' ? ' active' : ''}`} onClick={() => setTab('timeline')}>
          AI 之间沟通（{chains.length} 条链）
        </button>
        <button className={`tab${tab === 'runs' ? ' active' : ''}`} onClick={() => setTab('runs')}>
          AI 调用记录（{runs.data?.length ?? 0}）
        </button>
      </div>

      {tab === 'timeline' ? (
        <>
          <Notice kind="info">
            一次跨商品问询会显示成一条链：<span className="mono">商品A的AI → cross → 商品B的AI → cross → 商品A的AI</span>。
            标记为「跨商品」的链就是买家在 A 商品里问了 B 商品的事。
          </Notice>

          <div className="toolbar">
            <select value={kindFilter} onChange={(e) => setKindFilter(e.target.value)} style={{ width: 150 }}>
              <option value="">全部类型</option>
              <option value="ask">提问 ask</option>
              <option value="answer">回答 answer</option>
              <option value="notify">通知 notify</option>
            </select>
            <Switch checked={onlyCross} onChange={setOnlyCross} label="只看跨商品协查" />
            <Switch checked={showRaw} onChange={setShowRaw} label="显示原始 payload" />
            <span className="spacer" />
            <span className="dim" style={{ fontSize: 12 }}>共 {crossCount} 条跨商品链</span>
            <button className="btn" onClick={messages.reload}>
              刷新
            </button>
          </div>

          <AsyncBox
            loading={messages.loading}
            error={messages.error}
            notReady={messages.notReady}
            empty={visibleChains.length === 0}
            emptyIcon="🤝"
            emptyTitle={onlyCross ? '暂无跨商品协查记录' : '暂无 AI 之间沟通记录'}
            emptyDesc="当买家在 A 商品里问起 B 商品，商品 A 的 AI 会去找 B 的 AI 问清楚，全过程记录在这里。"
            onRetry={messages.reload}
          >
            <div className="timeline">
              {visibleChains.map((chain) => (
                <ChainCard key={chain.key} chain={chain} showRaw={showRaw} />
              ))}
            </div>
          </AsyncBox>
        </>
      ) : (
        <AsyncBox
          loading={runs.loading}
          error={runs.error}
          notReady={runs.notReady}
          empty={(runs.data ?? []).length === 0}
          emptyIcon="📊"
          emptyTitle="暂无 AI 调用记录"
          emptyDesc="AI 每次回复买家都会记一条，含 token 用量与耗时。"
          onRetry={runs.reload}
        >
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="nowrap">时间</th>
                  <th className="nowrap">角色</th>
                  <th>模型</th>
                  <th className="nowrap">状态</th>
                  <th className="nowrap">耗时</th>
                  <th className="nowrap">Token</th>
                  <th>错误</th>
                </tr>
              </thead>
              <tbody>
                {(runs.data ?? []).map((run) => (
                  <tr key={run.id}>
                    <td className="nowrap dim" style={{ fontSize: 12 }}>{formatTime(run.created_at)}</td>
                    <td className="nowrap">
                      <Badge tone="muted">{roleName(run.role)}</Badge>
                    </td>
                    <td className="mono" style={{ fontSize: 11.5 }}>{run.model || '-'}</td>
                    <td className="nowrap">
                      <Badge tone={run.status === 'ok' || run.status === 'success' ? 'ok' : 'danger'}>
                        {run.status || '未知'}
                      </Badge>
                    </td>
                    <td className="num nowrap">{run.latency_ms !== undefined ? `${run.latency_ms}ms` : '-'}</td>
                    <td className="num nowrap dim" style={{ fontSize: 11.5 }}>
                      {run.prompt_tokens ?? 0} / {run.completion_tokens ?? 0}
                    </td>
                    <td className="dim" style={{ fontSize: 11.5 }}>{run.error || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </AsyncBox>
      )}
    </div>
  )
}

/* ---------------- 一条协查链 ---------------- */

function ChainCard({ chain, showRaw }: { chain: Chain; showRaw: boolean }) {
  const isCross = chain.items.length >= 2 || chain.messages.some((m) => splitAgent(m.to_agent).role === 'cross')
  const asked = chain.messages.find((m) => m.kind === 'ask')
  const question = asked ? payloadSummary(asked.payload) : ''

  return (
    <div className="tl-item">
      <div className="tl-card">
        <div className="tl-head">
          <div className="tl-flow">
            {chain.messages.map((message, index) => (
              <span key={`hop-${message.id ?? index}`} className="row" style={{ gap: 7 }}>
                {index === 0 && <span>{agentLabel(message.from_agent)}</span>}
                <span className="tl-arrow">→</span>
                <span>{agentLabel(message.to_agent)}</span>
              </span>
            ))}
          </div>
          {isCross && <Badge tone="ask">跨商品</Badge>}
          {chain.topic && <Badge tone="muted">{chain.topic}</Badge>}
          {chain.conversationId !== undefined && (
            <span className="mono dim" style={{ fontSize: 11 }}>会话 #{chain.conversationId}</span>
          )}
          <span style={{ flex: 1 }} />
          <span className="dim" style={{ fontSize: 11.5 }}>{formatTime(chain.startedAt)}</span>
        </div>

        {question && (
          <div className="tl-body">
            <span className="dim">买家问题：</span>
            {question}
          </div>
        )}

        <div className="tl-pair" style={{ marginTop: 9 }}>
          {chain.messages.map((message) => {
            const kind = kindLabel(message.kind)
            const summary = payloadSummary(message.payload)
            const from = splitAgent(message.from_agent)
            const to = splitAgent(message.to_agent)
            return (
              <div key={message.id} className="row wrap" style={{ alignItems: 'flex-start', gap: 8, padding: '5px 0' }}>
                <Badge tone={kind.tone}>{kind.text}</Badge>
                <span className="row" style={{ gap: 6, fontWeight: 600, fontSize: 12.5 }}>
                  {agentLabel(message.from_agent)}
                  <span className="tl-arrow">→</span>
                  {agentLabel(message.to_agent)}
                </span>
                {from.itemId && to.itemId && from.itemId !== to.itemId && (
                  <span className="dim" style={{ fontSize: 11.5 }}>
                    （商品 {from.itemId.slice(-6)} 问商品 {to.itemId.slice(-6)}）
                  </span>
                )}
                <span className="dim" style={{ fontSize: 11.5, marginLeft: 'auto' }}>
                  {formatTime(message.created_at)}
                </span>
                {summary && (
                  <div style={{ flexBasis: '100%', fontSize: 12.5, color: 'var(--text-dim)', paddingLeft: 4 }}>
                    {summary}
                  </div>
                )}
                {showRaw && message.payload && <div className="code-block" style={{ flexBasis: '100%' }}>{prettyJson(message.payload)}</div>}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
