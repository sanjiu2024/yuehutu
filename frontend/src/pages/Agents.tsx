/** AI 沟通：角色总览（ai_roles）+ AI 之间的沟通记录（agent_messages）+ 调用流水（agent_runs）。
 *
 * 为什么不再用 Tab 把两块藏起来：这一页现在要同时回答「哪个角色开着、绑了什么模型」和
 * 「AI 之间聊了什么、调用有没有炸」，三块摊开比来回切换省事。
 *
 * 上一版的「协查链」聚合视图按 task-34 给的列清单换成了扁平表格（时间/发起方/接收方/
 * 类型/topic/payload）；跨商品协查这个信号没有丢，改成行内「跨商品」小标 + 本地筛选。
 *
 * 除角色开关写回后端外，这一页不发任何额外请求：所有统计与筛选都用已拿到的数据在本地算。
 */

import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, errorText } from '../api'
import { AsyncBox, Badge, Notice, Switch, useToast } from '../components'
import { useAsync } from '../hooks/useAsync'
import {
  IconAgents,
  IconBox,
  IconChart,
  IconChat,
  IconCheckCircle,
  IconEye,
  IconLink,
  IconMaster,
  IconRefresh,
  IconSettings,
  IconTag,
  IconUsers,
  IconXCircle,
} from '../icons'
import {
  agentLabel,
  formatTime,
  kindLabel,
  payloadSummary,
  prettyJson,
  roleName,
  splitAgent,
  truncate,
} from '../format'
import type { AgentMessage, AgentRun, AiRole } from '../types'
import './agents.css'

/** 卡片顺序固定成和后端 `agents/roles.py::ROLES` 一致：接口顺序变了卡片也不乱跳。 */
const ROLE_ORDER: readonly string[] = ['master', 'item', 'cs', 'bargain', 'cross', 'vision']

/** 角色职责文案（task-34 指定）；后端 description 只作兜底，接口缺字段时也不至于空白。 */
const ROLE_DUTY: Record<string, string> = {
  master: '主管 AI，唯一能调工具、直接与卖家对接的角色，把任务派给其它 AI。',
  item: '每个商品一个常驻 AI，按商品维度的话术回答买家。',
  cs: '通用客服：订单、物流、退换、售后政策。',
  bargain: '议价 AI：在卖家设定的底价与幅度内和买家谈价。',
  cross: '跨商品协查：买家在 A 商品里问 B 商品时，负责去问 B 的商品 AI 再汇总答复。',
  vision: '看图 AI：买家发图问货时负责识图作答。',
}

/** 跑失败的判定：status 不是 ok/success，或者后端写了 error 原文（结果可能是空的）。 */
function isRunFailed(run: AgentRun): boolean {
  const status = (run.status || '').toLowerCase()
  if (status && status !== 'ok' && status !== 'success') return true
  return Boolean(run.error)
}

/** 跨商品协查：either 走 cross 角色，either 两端挂着不同的商品 id。 */
function isCrossMessage(message: AgentMessage): boolean {
  const from = splitAgent(message.from_agent)
  const to = splitAgent(message.to_agent)
  if (from.role === 'cross' || to.role === 'cross') return true
  return Boolean(from.id && to.id && from.id !== to.id)
}

function RoleIcon({ role }: { role: string }) {
  switch (role) {
    case 'master':
      return <IconMaster size={16} />
    case 'item':
      return <IconBox size={16} />
    case 'cs':
      return <IconChat size={16} />
    case 'bargain':
      return <IconTag size={16} />
    case 'cross':
      return <IconLink size={16} />
    case 'vision':
      return <IconEye size={16} />
    default:
      return <IconAgents size={16} />
  }
}

/** 三个全局 AI 回复开关（第 55 批）。
 *
 * 以前 `ai.enabled` / `chat.auto_reply_scope` / `chat.human_takeover_seconds` 只存在于
 * `settings` 表与 `GET /api/settings` 的返回里，生产代码一次都没读过 —— 界面上没有入口，
 * 就算从接口写进去也毫无作用。现在后端真的按它们拦 AI，这里给一个能改的入口。
 *
 * 三个值都是字符串（`settings` 表就是 key/value 文本），保存时统一转成字符串发出去。
 */
function AutoReplySettingsCard() {
  const toast = useToast()
  const settings = useAsync<Record<string, string>>(() => api.get<Record<string, string>>('/api/settings'), [], true, [])
  const [loaded, setLoaded] = useState(false)
  const [enabled, setEnabled] = useState(true)
  const [scope, setScope] = useState('all')
  const [takeover, setTakeover] = useState('0')
  const [saving, setSaving] = useState(false)

  // 只灌一次草稿：之后以用户改的为准，别被静默重拉覆盖掉。
  useEffect(() => {
    const data = settings.data
    if (!data || loaded) return
    setEnabled(String(data['ai.enabled'] ?? 'true').trim().toLowerCase() !== 'false')
    setScope(String(data['chat.auto_reply_scope'] ?? 'all').trim().toLowerCase() || 'all')
    setTakeover(String(data['chat.human_takeover_seconds'] ?? '0').trim() || '0')
    setLoaded(true)
  }, [settings.data, loaded])

  async function save(): Promise<void> {
    setSaving(true)
    try {
      await api.put('/api/settings', {
        'ai.enabled': enabled ? 'true' : 'false',
        'chat.auto_reply_scope': scope,
        'chat.human_takeover_seconds': String(takeover || '0'),
      })
      toast.success('已保存：AI 自动回复按新开关执行')
      settings.reload()
    } catch (err) {
      toast.error(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="ag-card">
      <div className="ag-card-head">
        <div className="ag-card-title">
          <IconSettings size={14} /> AI 自动回复开关
        </div>
        <div className="ag-card-meta">
          {settings.loading ? '加载中…' : '只影响 AI 自动回复，你自己发的消息不受影响'}
        </div>
      </div>
      <div className="ag-card-body">
        <AsyncBox
          loading={settings.loading}
          error={settings.error}
          notReady={settings.notReady}
          onRetry={settings.reload}
        >
          <div className="ag-settings">
            <Switch
              checked={enabled}
              onChange={(next) => setEnabled(next)}
              label="AI 总开关（ai.enabled）：关掉之后 AI 不再自动回任何买家"
            />
            <label className="ag-setting-row">
              <span>自动回复范围（chat.auto_reply_scope）</span>
              <select value={scope} onChange={(event) => setScope(event.target.value)}>
                <option value="all">all：所有会话都自动回</option>
                <option value="items_only">items_only：只回挂着商品的会话</option>
                <option value="off">off：一律不自动回</option>
              </select>
            </label>
            <label className="ag-setting-row">
              <span>人工回复后不插话（chat.human_takeover_seconds，秒；0 = 不限制）</span>
              <input
                type="number"
                min={0}
                value={takeover}
                onChange={(event) => setTakeover(event.target.value)}
              />
            </label>
            <div className="ag-setting-actions">
              <button className="btn primary" onClick={save} disabled={saving || !loaded}>
                {saving ? '保存中…' : '保存'}
              </button>
              <span className="ag-setting-hint">
                关掉 AI 或把范围设成 off 之后，买家来信照旧落库、会话页照旧能人工回，只是 AI 不再开口。
              </span>
            </div>
          </div>
        </AsyncBox>
      </div>
    </div>
  )
}


export function AgentsPage() {
  const toast = useToast()
  const [kindFilter, setKindFilter] = useState('')
  const [topicFilter, setTopicFilter] = useState('')
  const [onlyCross, setOnlyCross] = useState(false)
  const [runRoleFilter, setRunRoleFilter] = useState('')
  const [onlyFailed, setOnlyFailed] = useState(false)
  /** 开关的乐观值：写回期间先按用户点的显示，接口回来或 WS 事件到了再对齐。 */
  const [pendingEnabled, setPendingEnabled] = useState<Record<string, boolean>>({})
  const [savingRole, setSavingRole] = useState('')

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
  const roles = useAsync<AiRole[]>(() => api.get<AiRole[]>('/api/agents/roles'), [], true, ['providers.changed'])

  // 新的在前：表是流水，最近发生的事应该在第一屏
  const messageList = useMemo(
    () => [...(messages.data ?? [])].sort((a, b) => (b.id ?? 0) - (a.id ?? 0)),
    [messages.data],
  )
  const runList = useMemo(() => [...(runs.data ?? [])].sort((a, b) => (b.id ?? 0) - (a.id ?? 0)), [runs.data])

  const roleList = useMemo(() => {
    const order = (role: string): number => {
      const index = ROLE_ORDER.indexOf(role)
      return index < 0 ? ROLE_ORDER.length : index
    }
    return [...(roles.data ?? [])].sort((a, b) => order(a.role) - order(b.role))
  }, [roles.data])

  const topics = useMemo(
    () => Array.from(new Set(messageList.map((item) => item.topic).filter((topic): topic is string => Boolean(topic)))),
    [messageList],
  )
  const runRoles = useMemo(
    () => Array.from(new Set(runList.map((item) => item.role).filter((role): role is string => Boolean(role)))),
    [runList],
  )

  const visibleMessages = useMemo(
    () =>
      messageList.filter((message) => {
        if (kindFilter && message.kind !== kindFilter) return false
        if (topicFilter && message.topic !== topicFilter) return false
        if (onlyCross && !isCrossMessage(message)) return false
        return true
      }),
    [messageList, kindFilter, topicFilter, onlyCross],
  )

  const visibleRuns = useMemo(
    () =>
      runList.filter((run) => {
        if (runRoleFilter && run.role !== runRoleFilter) return false
        if (onlyFailed && !isRunFailed(run)) return false
        return true
      }),
    [runList, runRoleFilter, onlyFailed],
  )

  const roleEnabled = (role: AiRole): boolean =>
    role.role in pendingEnabled ? pendingEnabled[role.role] : Boolean(role.enabled)
  const enabledRoles = roleList.filter(roleEnabled).length
  const failedRuns = runList.filter(isRunFailed).length
  const crossMessages = messageList.filter(isCrossMessage).length

  /** 后端回执或 WS 静默重拉已经反映出乐观值时，把临时覆盖删掉，免得两套状态长期漂移。 */
  useEffect(() => {
    if (!roles.data) return
    setPendingEnabled((prev) => {
      let dirty = false
      const next = { ...prev }
      for (const role of roles.data ?? []) {
        if (role.role in next && Boolean(role.enabled) === next[role.role]) {
          delete next[role.role]
          dirty = true
        }
      }
      return dirty ? next : prev
    })
  }, [roles.data])

  async function toggleRole(role: AiRole, next: boolean): Promise<void> {
    const key = role.role
    if (savingRole) return
    setPendingEnabled((prev) => ({ ...prev, [key]: next }))
    setSavingRole(key)
    try {
      const updated = await api.put<AiRole>(`/api/agents/roles/${encodeURIComponent(key)}`, {
        enabled: next ? 1 : 0,
      })
      if (updated && roles.data) {
        roles.setData(roles.data.map((item) => (item.role === key ? updated : item)))
      }
      toast.success(`已${next ? '启用' : '停用'}「${role.label || roleName(key)}」`)
    } catch (err) {
      // 写回失败必须把乐观值撤回：否则页面显示的是「已启用」，后端其实没改
      setPendingEnabled((prev) => {
        const copy = { ...prev }
        delete copy[key]
        return copy
      })
      toast.error(errorText(err))
    } finally {
      setSavingRole('')
    }
  }

  function reloadAll(): void {
    messages.reload()
    runs.reload()
    roles.reload()
  }

  const messageFiltered = kindFilter !== '' || topicFilter !== '' || onlyCross
  const runFiltered = runRoleFilter !== '' || onlyFailed

  return (
    <div className="ag-page">
      <div className="ag-head">
        <div>
          <div className="ag-title">
            <IconAgents size={16} /> AI 沟通
          </div>
          <div className="ag-sub">
            六个 AI 角色的启用状态与模型绑定、AI 之间的沟通记录，以及每次模型调用的用量与耗时。
          </div>
        </div>
        <div className="ag-head-actions">
          <button
            className="btn"
            onClick={reloadAll}
            disabled={messages.loading || runs.loading || roles.loading}
          >
            <IconRefresh size={14} /> 刷新
          </button>
        </div>
      </div>

      <div className="ag-stats">
        <StatBox
          label="已启用角色"
          value={`${enabledRoles} / ${roleList.length || ROLE_ORDER.length}`}
          tone="accent"
          hint="停用的角色不会被派活"
        />
        <StatBox
          label="AI 之间消息"
          value={messageList.length}
          hint={`其中跨商品协查 ${crossMessages} 条`}
        />
        <StatBox label="运行记录" value={runList.length} hint="后端一次最多返回 100 条" />
        <StatBox
          label="失败运行"
          value={failedRuns}
          tone={failedRuns > 0 ? 'danger' : 'ok'}
          hint={failedRuns > 0 ? 'status 非 ok 或带 error 原文' : '没有失败记录'}
        />
      </div>

      <AutoReplySettingsCard />

      <div className="ag-card">
        <div className="ag-card-head">
          <div className="ag-card-title">
            <IconUsers size={14} /> 角色总览
          </div>
          <div className="ag-card-meta">
            {roles.loading ? '加载中…' : `共 ${roleList.length} 个角色 · ${enabledRoles} 个已启用`}
          </div>
        </div>
        <div className="ag-card-body">
          <AsyncBox
            loading={roles.loading}
            error={roles.error}
            notReady={roles.notReady}
            onRetry={roles.reload}
          >
            {roleList.length === 0 ? (
              <AgEmpty
                icon={<IconUsers size={22} />}
                title="还没有角色配置"
                desc="后端 ai_roles 表为空时会自动补 6 行默认角色，稍后点「刷新」再看。"
              />
            ) : (
              <div className="ag-grid">
                {roleList.map((role) => (
                  <RoleCard
                    key={role.role}
                    role={role}
                    enabled={roleEnabled(role)}
                    saving={savingRole === role.role}
                    disabled={savingRole !== ''}
                    onToggle={toggleRole}
                  />
                ))}
              </div>
            )}
          </AsyncBox>
        </div>
      </div>

      <div className="ag-card">
        <div className="ag-card-head">
          <div className="ag-card-title">
            <IconChat size={14} /> AI 之间沟通记录
          </div>
          <div className="ag-card-meta">
            共 {messageList.length} 条
            {messageFiltered ? ` · 筛出 ${visibleMessages.length} 条` : ''}
          </div>
        </div>
        <div className="ag-card-body">
          <Notice kind="info">
            AI 之间的每次问询都落一条 from → to 消息。买家在 A 商品里问起 B 商品时会出现{' '}
            <span className="mono">item:A → cross → item:B</span> 这样的往返，标了「跨商品」的就是。
          </Notice>

          <div className="ag-toolbar ag-toolbar-gap">
            <label className="ag-field">
              <span>类型</span>
              <select className="ag-select" value={kindFilter} onChange={(e) => setKindFilter(e.target.value)}>
                <option value="">全部</option>
                <option value="ask">提问 ask</option>
                <option value="answer">回答 answer</option>
                <option value="notify">通知 notify</option>
              </select>
            </label>
            <label className="ag-field">
              <span>topic</span>
              <select
                className="ag-select"
                value={topicFilter}
                onChange={(e) => setTopicFilter(e.target.value)}
                disabled={topics.length === 0}
              >
                <option value="">全部</option>
                {topics.map((topic) => (
                  <option key={topic} value={topic}>
                    {topic}
                  </option>
                ))}
              </select>
            </label>
            <Switch checked={onlyCross} onChange={setOnlyCross} label="只看跨商品" />
            <span className="ag-spacer" />
            {messageFiltered && (
              <button
                className="btn sm ghost"
                onClick={() => {
                  setKindFilter('')
                  setTopicFilter('')
                  setOnlyCross(false)
                }}
              >
                清空筛选
              </button>
            )}
          </div>

          <AsyncBox
            loading={messages.loading}
            error={messages.error}
            notReady={messages.notReady}
            onRetry={messages.reload}
          >
            {visibleMessages.length === 0 ? (
              <AgEmpty
                icon={<IconChat size={22} />}
                title={messageList.length === 0 ? '暂无 AI 之间沟通记录' : '没有符合筛选条件的消息'}
                desc={
                  messageList.length === 0
                    ? '当买家在 A 商品里问起 B 商品，商品 A 的 AI 会去找 B 的 AI 问清楚，全过程记在这里。'
                    : '换个类型或 topic，或者清空筛选再看。'
                }
              />
            ) : (
              <div className="ag-scroll">
                <table className="ag-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>发起方</th>
                      <th>接收方</th>
                      <th>类型</th>
                      <th>会话</th>
                      <th>topic</th>
                      <th>payload 摘要</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleMessages.map((message) => {
                      const kind = kindLabel(message.kind)
                      const summary = payloadSummary(message.payload)
                      const raw = prettyJson(message.payload)
                      return (
                        <tr key={message.id}>
                          <td className="ag-nowrap dim">{formatTime(message.created_at)}</td>
                          <td className="ag-nowrap">{agentLabel(message.from_agent)}</td>
                          <td className="ag-nowrap">{agentLabel(message.to_agent)}</td>
                          <td className="ag-nowrap">
                            <Badge tone={kind.tone}>{kind.text}</Badge>
                            {isCrossMessage(message) && <span className="ag-cross">跨商品</span>}
                          </td>
                          <td className="ag-nowrap dim mono">
                            {message.conversation_id ? `#${message.conversation_id}` : '-'}
                          </td>
                          <td className="ag-nowrap dim">{message.topic || '-'}</td>
                          <td className="ag-payload">
                            {raw ? (
                              <details className="ag-raw">
                                <summary>{truncate(summary || raw, 90)}</summary>
                                <pre className="ag-raw-pre">{raw}</pre>
                              </details>
                            ) : (
                              <span className="dim">-</span>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </AsyncBox>
        </div>
      </div>

      <div className="ag-card">
        <div className="ag-card-head">
          <div className="ag-card-title">
            <IconChart size={14} /> AI 运行流水
          </div>
          <div className="ag-card-meta">
            共 {runList.length} 条
            {failedRuns > 0 ? ` · 失败 ${failedRuns} 条` : ''}
          </div>
        </div>
        <div className="ag-card-body">
          <div className="ag-toolbar">
            <label className="ag-field">
              <span>角色</span>
              <select
                className="ag-select"
                value={runRoleFilter}
                onChange={(e) => setRunRoleFilter(e.target.value)}
                disabled={runRoles.length === 0}
              >
                <option value="">全部</option>
                {runRoles.map((role) => (
                  <option key={role} value={role}>
                    {roleName(role)}（{role}）
                  </option>
                ))}
              </select>
            </label>
            <Switch checked={onlyFailed} onChange={setOnlyFailed} label="只看失败" />
            <span className="ag-spacer" />
            {runFiltered && (
              <button
                className="btn sm ghost"
                onClick={() => {
                  setRunRoleFilter('')
                  setOnlyFailed(false)
                }}
              >
                清空筛选
              </button>
            )}
          </div>

          <AsyncBox loading={runs.loading} error={runs.error} notReady={runs.notReady} onRetry={runs.reload}>
            {visibleRuns.length === 0 ? (
              <AgEmpty
                icon={onlyFailed && runList.length > 0 ? <IconCheckCircle size={22} /> : <IconChart size={22} />}
                title={
                  runList.length === 0
                    ? '暂无 AI 运行记录'
                    : onlyFailed
                      ? '没有失败的调用记录'
                      : '没有符合筛选条件的记录'
                }
                desc={
                  runList.length === 0
                    ? 'AI 每次回复买家都会记一条，含 token 用量与耗时。'
                    : '换个角色，或者清空筛选再看。'
                }
              />
            ) : (
              <div className="ag-scroll">
                <table className="ag-table">
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>角色</th>
                      <th>会话</th>
                      <th>模型服务 / 模型</th>
                      <th>Token（prompt / completion）</th>
                      <th>耗时</th>
                      <th>状态</th>
                      <th>错误</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleRuns.map((run) => {
                      const failed = isRunFailed(run)
                      const status = run.status || '未知'
                      return (
                        <tr key={run.id} className={failed ? 'ag-row-bad' : undefined}>
                          <td className="ag-nowrap dim">{formatTime(run.created_at)}</td>
                          <td className="ag-nowrap">
                            <Badge tone="muted">{roleName(run.role)}</Badge>
                          </td>
                          <td className="ag-nowrap dim mono">
                            {run.conversation_id ? `#${run.conversation_id}` : '-'}
                          </td>
                          <td className="ag-nowrap mono ag-small">
                            {run.provider_id ? `#${run.provider_id} · ` : ''}
                            {run.model || '未记录'}
                          </td>
                          <td className="ag-nowrap ag-num dim ag-small">
                            {run.prompt_tokens ?? 0} / {run.completion_tokens ?? 0}
                          </td>
                          <td className="ag-nowrap ag-num">
                            {typeof run.latency_ms === 'number' ? `${run.latency_ms} ms` : '-'}
                          </td>
                          <td className="ag-nowrap">
                            <Badge tone={failed ? 'danger' : status === 'ok' || status === 'success' ? 'ok' : 'muted'}>
                              {status}
                            </Badge>
                          </td>
                          <td className={failed ? 'ag-err' : 'dim ag-small'}>
                            {failed && <IconXCircle size={13} />}
                            {failed ? run.error || '（后端没有写错误详情）' : '-'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </AsyncBox>
        </div>
      </div>
    </div>
  )
}

/* ---------------- 页面内的小组件 ---------------- */

function StatBox({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint?: string
  tone?: 'ok' | 'danger' | 'accent'
}) {
  return (
    <div className="ag-stat">
      <div className="ag-stat-label">{label}</div>
      <div className={`ag-stat-value${tone ? ` is-${tone}` : ''}`}>{value}</div>
      {hint && <div className="ag-stat-hint">{hint}</div>}
    </div>
  )
}

/** 空态自己写：共享的 Empty / AsyncBox.empty 内部会渲染 emoji，全站禁 emoji。 */
function AgEmpty({ icon, title, desc }: { icon: ReactNode; title: string; desc: string }) {
  return (
    <div className="ag-empty">
      <span className="ag-empty-icon">{icon}</span>
      <div className="ag-empty-title">{title}</div>
      <div className="ag-empty-desc">{desc}</div>
    </div>
  )
}

function RoleCard({
  role,
  enabled,
  saving,
  disabled,
  onToggle,
}: {
  role: AiRole
  enabled: boolean
  saving: boolean
  disabled: boolean
  onToggle: (role: AiRole, next: boolean) => void
}) {
  const label = role.label || roleName(role.role)
  const duty = ROLE_DUTY[role.role] || role.description || ''
  return (
    <div className={`ag-card ag-role${enabled ? '' : ' is-off'}`}>
      <div className="ag-role-head">
        <span className="ag-role-icon">
          <RoleIcon role={role.role} />
        </span>
        <span className="ag-role-name">{label}</span>
        <span className="ag-role-key mono">{role.role}</span>
        <span className="grow" />
        {saving && <span className="ag-card-meta">保存中…</span>}
        <Switch
          checked={enabled}
          disabled={disabled}
          onChange={(next) => onToggle(role, next)}
          label={enabled ? '已启用' : '已停用'}
        />
      </div>
      <div className="ag-role-desc">{duty || '（后端没有给这个角色写说明）'}</div>
      <div className="ag-kv">
        <div className="ag-kv-k">模型服务</div>
        <div className="ag-kv-v">{role.provider_id ? `#${role.provider_id}` : '未绑定'}</div>
        <div className="ag-kv-k">模型</div>
        <div className="ag-kv-v mono">{role.model || '未指定'}</div>
        <div className="ag-kv-k">temperature</div>
        <div className="ag-kv-v">{role.temperature ?? '-'}</div>
        <div className="ag-kv-k">更新时间</div>
        <div className="ag-kv-v">{role.updated_at ? formatTime(role.updated_at) : '-'}</div>
      </div>
    </div>
  )
}
