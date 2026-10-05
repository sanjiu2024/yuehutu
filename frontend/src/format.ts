/** 展示层小工具：时间/金额/角色名/JSON 美化。 */

export function formatTime(value?: string | number | null): string {
  if (value === undefined || value === null || value === '') return '-'
  let date: Date
  if (typeof value === 'number') {
    // 后端 risk_state 用 unix 秒
    date = new Date(value < 1e12 ? value * 1000 : value)
  } else if (/^\d+(\.\d+)?$/.test(value)) {
    const num = Number(value)
    date = new Date(num < 1e12 ? num * 1000 : num)
  } else {
    // 数据库存的是 CURRENT_TIMESTAMP（UTC，无时区后缀），补 Z 再本地化
    const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : value.replace(' ', 'T') + 'Z'
    date = new Date(normalized)
  }
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

export function formatClock(value?: string | number | null): string {
  const full = formatTime(value)
  if (full === '-') return full
  const parts = full.split(' ')
  return parts.length > 1 ? parts[1] : full
}

export function formatMoney(value?: number | string | null): string {
  if (value === undefined || value === null || value === '') return '-'
  const num = Number(value)
  if (Number.isNaN(num)) return String(value)
  return `¥${num.toFixed(2)}`
}

/** 后端布尔字段是真 bool；同时容忍历史 0/1 与字符串。 */
export function bool(value: number | boolean | string | undefined | null): boolean {
  if (value === true || value === 1) return true
  if (value === '1' || value === 'true' || value === 'yes') return true
  return false
}

/**
 * 账号登录态 → 中文 + 颜色类名。
 * 后端实际取值（xianyu/store.py:23-25）：unknown | active | need_relogin。
 */
export function loginStateLabel(state?: string): { text: string; tone: string } {
  switch (state) {
    case 'active':
    case 'online':
    case 'ok':
    case 'valid':
      return { text: '已登录', tone: 'ok' }
    case 'need_relogin':
    case 'relogin':
    case 'expired':
      return { text: '需重新扫码', tone: 'danger' }
    case 'pending':
      return { text: '等待扫码', tone: 'warn' }
    case 'scanned':
      return { text: '已扫码待确认', tone: 'warn' }
    case 'failed':
      return { text: '登录失败', tone: 'danger' }
    case 'unknown':
    case undefined:
    case '':
      return { text: '未登录', tone: 'muted' }
    default:
      return { text: state, tone: 'muted' }
  }
}

/** 运行态：后端由账号是否启用 + 是否熔断派生。 */
export function runtimeStateLabel(state?: string): { text: string; tone: string } {
  switch (state) {
    case 'running':
    case 'online':
      return { text: '运行中', tone: 'ok' }
    case 'stopped':
    case 'offline':
      return { text: '已停止', tone: 'muted' }
    case 'risk':
    case 'blocked':
      return { text: '风控中', tone: 'danger' }
    case undefined:
    case '':
      return { text: '-', tone: 'muted' }
    default:
      return { text: state, tone: 'muted' }
  }
}

/**
 * 商品状态 → 中文 + 颜色类名。
 * 后端实际写入的两个值（items.py:370/374）：`on_sale` | `off_shelf`。
 */
export function itemStatusLabel(status?: string): { text: string; tone: string } {
  switch (status) {
    case 'on_sale':
    case 'onsale':
    case 'selling':
    case '在售':
      return { text: '在售', tone: 'ok' }
    case 'off_shelf':
    case 'offshelf':
    case 'offline':
    case 'delisted':
    case '已下架':
      return { text: '已下架', tone: 'muted' }
    case 'sold':
    case '已售出':
      return { text: '已售出', tone: 'warn' }
    case 'draft':
    case '草稿':
      return { text: '草稿', tone: 'muted' }
    case undefined:
    case '':
      return { text: '未知', tone: 'muted' }
    default:
      return { text: status, tone: 'muted' }
  }
}

/** 商品状态下拉框的选项（与后端写入值一致）。 */
export const ITEM_STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'on_sale', label: '在售' },
  { value: 'off_shelf', label: '已下架' },
  { value: 'sold', label: '已售出' },
  { value: 'draft', label: '草稿' },
]

export function modeLabel(mode?: string): { text: string; tone: string } {
  switch (mode) {
    case 'manual':
      return { text: '人工接管', tone: 'warn' }
    case 'off':
      return { text: 'AI 已关闭', tone: 'muted' }
    case 'auto':
      return { text: 'AI 自动回复', tone: 'ok' }
    default:
      return { text: mode || '未知', tone: 'muted' }
  }
}

/** 消息发送者 → 中文标签。sender 形如 buyer | ai:item | human */
export function senderLabel(sender?: string): string {
  if (!sender) return '未知'
  if (sender === 'buyer') return '买家'
  if (sender === 'human') return '我（人工）'
  if (sender.startsWith('ai:')) {
    const role = sender.slice(3)
    return `AI·${roleName(role)}`
  }
  return sender
}

export const ROLE_NAMES: Record<string, string> = {
  master: '主管 AI',
  item: '商品 AI',
  cs: '客服 AI',
  bargain: '议价 AI',
  cross: '跨商品协查 AI',
  vision: '识图 AI',
}

export function roleName(role?: string): string {
  if (!role) return '未知'
  return ROLE_NAMES[role] || role
}

/** agent_messages 里的 from/to 形如 "item:1047949964912" 或 "cross"。 */
export function agentLabel(agent?: string): string {
  if (!agent) return '未知'
  const [role, id] = agent.split(':')
  const base = roleName(role)
  if (!id) return base
  const short = id.length > 8 ? `…${id.slice(-6)}` : id
  return `${base}(${short})`
}

/** 从 "item:1047949964912" 里拆出角色与商品 id。 */
export function splitAgent(agent?: string): { role: string; id: string } {
  if (!agent) return { role: '', id: '' }
  const index = agent.indexOf(':')
  if (index < 0) return { role: agent, id: '' }
  return { role: agent.slice(0, index), id: agent.slice(index + 1) }
}

export function kindLabel(kind?: string): { text: string; tone: string } {
  switch (kind) {
    case 'ask':
      return { text: '提问', tone: 'ask' }
    case 'answer':
      return { text: '回答', tone: 'answer' }
    case 'notify':
      return { text: '通知', tone: 'notify' }
    default:
      return { text: kind || '消息', tone: 'muted' }
  }
}

/**
 * 美化 JSON。
 * 后端已把 `agent_messages.payload` JSON 解码成对象，所以这里**必须同时接受对象与字符串**。
 */
export function prettyJson(value?: unknown): string {
  if (value === undefined || value === null || value === '') return ''
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  const text = String(value)
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** 从 payload 里挑出最适合展示的一句话。payload 可能是对象或 JSON 字符串。 */
export function payloadSummary(payload?: unknown): string {
  if (payload === undefined || payload === null || payload === '') return ''
  let parsed: unknown = payload
  if (typeof payload === 'string') {
    try {
      parsed = JSON.parse(payload)
    } catch {
      return payload
    }
  }
  if (typeof parsed !== 'object' || parsed === null) return String(parsed)
  const record = parsed as Record<string, unknown>
  for (const key of ['question', 'answer', 'text', 'message', 'summary', 'reason', 'title', 'content']) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  const keys = Object.keys(record)
  if (keys.length === 0) return ''
  return keys.map((k) => `${k}=${JSON.stringify(record[k])}`).join('  ')
}

export function truncate(text: string, max = 60): string {
  return text.length > max ? text.slice(0, max) + '…' : text
}

/** 商品图片：后端已解码成数组，但历史数据可能是 JSON 串或逗号分隔。 */
export function parseImages(images?: string | string[] | null): string[] {
  if (!images) return []
  if (Array.isArray(images)) return images.map((v) => String(v)).filter(Boolean)
  const trimmed = images.trim()
  if (trimmed.startsWith('[')) {
    try {
      const parsed = JSON.parse(trimmed)
      if (Array.isArray(parsed)) return parsed.map((v) => String(v)).filter(Boolean)
    } catch {
      /* 落到下面的分隔解析 */
    }
  }
  return trimmed
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** 把「秒」格式化成「1 分 30 秒」。 */
export function formatDuration(seconds?: number | null): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  if (total <= 0) return '0 秒'
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = total % 60
  const parts: string[] = []
  if (hours) parts.push(`${hours} 小时`)
  if (minutes) parts.push(`${minutes} 分`)
  if (secs && !hours) parts.push(`${secs} 秒`)
  return parts.join(' ') || '0 秒'
}
