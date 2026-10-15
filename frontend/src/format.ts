/** 展示层小工具：时间/金额/角色名/JSON 美化。 */

/** 解析后端三种时间形态（unix 秒/毫秒、UTC 无时区字符串、已格式化字符串）；认不出返回 null。 */
function toDate(value?: string | number | null): Date | null {
  if (value === undefined || value === null || value === '') return null
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
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatTime(value?: string | number | null): string {
  if (value === undefined || value === null || value === '') return '-'
  const date = toDate(value)
  if (!date) return String(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/**
 * 「人话」相对时间：刚刚 / 12 分钟前 / 3 小时前 / 昨天 14:30 / 09-28 14:30 / 2025-09-28 14:30。
 *
 * 为什么要有：列表里每一行都铺 `2026-10-16 01:53:14` 这 19 个字符，卖家扫「这条多久前的」
 * 还得自己减一次 —— 后台类产品的基线（千牛、有赞）都是相对时间 + 悬浮看全量。
 * **只用于列表/气泡这类「看新旧」的位置**，详情、审计、导出仍用 `formatTime` 全量。
 * 解析不出来就回落 `formatTime`（不猜、不画 1970）；未来时间（本机时钟漂移）也回落全量。
 */
export function formatRelativeTime(value?: string | number | null, now: Date = new Date()): string {
  const date = toDate(value)
  if (!date) return formatTime(value)
  const diff = now.getTime() - date.getTime()
  if (diff < 0) return formatTime(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  const dayStamp = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  if (dayStamp(date) === dayStamp(now)) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (dayStamp(date) === dayStamp(new Date(now.getTime() - 86_400_000))) return `昨天 ${clock}`
  if (date.getFullYear() === now.getFullYear()) return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`
  return `${dayStamp(date)} ${clock}`
}

export function formatClock(value?: string | number | null): string {
  const full = formatTime(value)
  if (full === '-') return full
  const parts = full.split(' ')
  return parts.length > 1 ? parts[1] : full
}

/**
 * 金额统一出口：千分位 + 固定 2 位 + 前缀 ¥；空值与非数字一律 `-`。
 * 非数字不再把原值当金额吐出来（既没 ¥ 也没对齐），原值请配 `moneyTitle()` 放进 title 排查。
 */
export function formatMoney(value?: number | string | null): string {
  if (value === undefined || value === null || value === '') return '-'
  const num = Number(typeof value === 'string' ? value.replace(/,/g, '').trim() : value)
  if (!Number.isFinite(num)) return '-'
  return `¥${num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** 非数字金额的原值（供 title 排查）；能正常格式化的返回 `''`。 */
export function moneyTitle(value?: number | string | null): string {
  if (value === undefined || value === null || value === '') return ''
  const num = Number(typeof value === 'string' ? value.replace(/,/g, '').trim() : value)
  return Number.isFinite(num) ? '' : String(value)
}

/**
 * 交易组（Orders/Refunds）的金额口径：平台原样的**元字符串**（`"39.90"`），**不能 /100**。
 * 契约见 `docs/contracts/trade.md`。整数元字符串（`"39"`）同样合法，不误伤。
 * 契约破了只 console.warn（线上不能因为一个异常金额白屏），并返回 null 让调用方画 `-`。
 */
export function parseYuanAmountString(value: unknown): number | null {
  if (value === undefined || value === null || value === '') return null
  const raw = String(value).trim()
  const num = Number(raw)
  if (!Number.isFinite(num)) {
    console.warn(`[xywb] 交易金额不是能解析的元字符串：${raw}（契约见 docs/contracts/trade.md，金额必须是元、不能 /100）`)
    return null
  }
  if (raw.includes('/')) {
    console.warn(`[xywb] 交易金额疑似带「/100」的加工痕迹：${raw}（平台给的就是元字符串，再除一次会小 100 倍）`)
    return null
  }
  return num
}

/**
 * 推广组（Promotion）的金额口径：平台指标里的金额单位是**分**，这里换算成元。
 * 非整数分按契约破了处理：console.warn + 返回 null，调用方画 `-`（不猜、不四舍五入掩盖）。
 */
export function fenToYuan(value: unknown): number | null {
  if (value === undefined || value === null || value === '') return null
  const num = Number(value)
  if (!Number.isFinite(num)) {
    console.warn(`[xywb] 推广金额不是数字：${String(value)}（平台指标金额单位是分）`)
    return null
  }
  if (!Number.isInteger(num)) {
    console.warn(`[xywb] 推广金额不是整数分：${String(value)}（分是整数，小数说明口径串了，可能是元）`)
    return null
  }
  return num / 100
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
