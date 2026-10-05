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

export function bool(value: number | boolean | undefined | null): boolean {
  return value === true || value === 1
}

/** 账号登录态 → 中文 + 颜色类名。 */
export function loginStateLabel(state?: string): { text: string; tone: string } {
  switch (state) {
    case 'online':
    case 'ok':
    case 'valid':
      return { text: '在线', tone: 'ok' }
    case 'pending':
      return { text: '等待扫码', tone: 'warn' }
    case 'scanned':
      return { text: '已扫码待确认', tone: 'warn' }
    case 'expired':
      return { text: '登录已过期', tone: 'danger' }
    case 'need_relogin':
    case 'relogin':
      return { text: '需重新扫码', tone: 'danger' }
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
    default:
      return { text: state || '未知', tone: 'muted' }
  }
}

export function itemStatusLabel(status?: string): { text: string; tone: string } {
  switch (status) {
    case 'onsale':
    case 'on_sale':
    case 'selling':
      return { text: '在售', tone: 'ok' }
    case 'offline':
    case 'delisted':
    case 'off':
      return { text: '已下架', tone: 'muted' }
    case 'sold':
      return { text: '已售出', tone: 'warn' }
    case 'draft':
      return { text: '草稿', tone: 'muted' }
    default:
      return { text: status || '未知', tone: 'muted' }
  }
}

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

export function prettyJson(value?: string | null): string {
  if (!value) return ''
  try {
    return JSON.stringify(JSON.parse(value), null, 2)
  } catch {
    return value
  }
}

/** 从 payload 里挑出最适合展示的一句话。 */
export function payloadSummary(payload?: string | null): string {
  if (!payload) return ''
  try {
    const parsed = JSON.parse(payload) as Record<string, unknown>
    for (const key of ['question', 'answer', 'text', 'message', 'summary', 'reason']) {
      const value = parsed[key]
      if (typeof value === 'string' && value.trim()) return value
    }
    const keys = Object.keys(parsed)
    if (keys.length === 0) return ''
    return keys.map((k) => `${k}=${JSON.stringify(parsed[k])}`).join('  ')
  } catch {
    return payload
  }
}

export function truncate(text: string, max = 60): string {
  return text.length > max ? text.slice(0, max) + '…' : text
}

/** 图片字段可能是 JSON 数组、逗号分隔或单个 URL。 */
export function parseImages(images?: string | null): string[] {
  if (!images) return []
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
