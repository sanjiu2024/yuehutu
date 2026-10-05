/**
 * 与**后端真实实现**对齐的数据类型（不是 CONTRACTS.md 的草案）。
 *
 * 对齐来源（逐一读过源码）：
 *   backend/app/api/accounts.py, items.py, chats.py, agents.py, providers.py, risk.py
 *   backend/app/services/{items,chats,risk_service,common}.py
 *   backend/app/agents/{roles,prompts,runtime,llm}.py
 *   backend/app/xianyu/{store,login}.py
 *
 * 三条容易踩的坑，这里显式记录：
 *  1. 列表接口一半是**裸数组**、一半是**包一层对象**。包对象的在类型里用 XxxPage 表示。
 *  2. 布尔字段后端给的是真 bool（`enabled`/`auto_reply`/`sent_ok`），不是 0/1。
 *  3. `agent_messages.payload`、`items.images` 后端已 JSON 解码，前端拿到的是对象/数组，不是字符串。
 */

export interface Envelope<T = unknown> {
  success: boolean
  data?: T
  message?: string
  code?: string
}

/* ------------------------------------------------------------------ 账号 */

export interface Account {
  id: string
  nickname?: string
  avatar_url?: string
  remark?: string
  enabled?: boolean
  auto_reply?: boolean
  /** unknown | active | need_relogin */
  login_state?: string
  /** running | stopped | risk */
  runtime_state?: string
  xianyu_user_id?: string
  relogin_reason?: string
  has_cookie?: boolean
  has_x5sec?: boolean
  /** 以下三个由 accounts.py 从风控注册表附加 */
  blocked?: boolean
  remaining_seconds?: number
  consecutive_hits?: number
  created_at?: string
  updated_at?: string
}

/** `POST /api/accounts/qr/start` 与 `/{id}/relogin` 都返回这个形状。 */
export interface QrStart {
  ticket: string
  /** 原始 base64 PNG，**不带** `data:image/png;base64,` 前缀。 */
  qr_image_b64: string
  expires_in?: number
  /** 仅 relogin 时附带 */
  id?: string
  login_state?: string
}

/** 后端 `_to_contract_state` 的实际取值。 */
export type QrState = 'pending' | 'scanned' | 'confirmed' | 'expired' | 'failed'

export interface QrPoll {
  state: QrState
  account_id?: string
  message?: string
}

export interface CaptchaResult {
  id: string
  started: boolean
  message?: string
}

/* ------------------------------------------------------------------ 商品 */

export interface Item {
  id?: number
  account_id: string
  item_id: string
  title?: string
  price?: number
  original_price?: number
  stock?: number
  status?: string
  category_id?: string
  /** 后端已 JSON 解码成数组 */
  images?: string[]
  description?: string
  ai_enabled?: boolean
  updated_at?: string
  synced_at?: string | null
}

/** 包一层对象的列表返回（items / chats / prompts / risk events 都是这种）。 */
export interface Paged<T> {
  total?: number
  page?: number
  size?: number
  pages?: number
  /** 具体列表键名各接口不同，见各专用类型。 */
  [key: string]: unknown
}

export interface ItemListResult {
  items: Item[]
  total: number
  page: number
  size: number
  pages: number
}

export interface ItemStats {
  total: number
  by_status?: Record<string, number>
  on_sale?: number
  by_account?: Record<string, number>
  inventory_value?: number
}

export interface OperationLog {
  id?: number
  account_id?: string
  item_id?: string
  action?: string
  success?: boolean
  detail?: string
  created_at?: string
}

export interface BatchResult {
  total: number
  ok: number
  failed: Array<{ id: string; message: string }>
  status?: string
}

/* ------------------------------------------------------------------ 会话 */

export interface LastMessage {
  content?: string
  created_at?: string
  direction?: string
  sender?: string
}

export interface Conversation {
  id: number
  account_id?: string
  peer_user_id?: string
  peer_nickname?: string
  item_id?: string
  /** auto | manual | off */
  mode?: string
  agent_role?: string
  unread?: number
  /** 后端由 mode 派生的便捷字段 */
  ai_enabled?: boolean
  last_message_at?: string
  created_at?: string
  /** **对象**，不是字符串 */
  last_message?: LastMessage | null
}

export interface ConversationListResult {
  conversations: Conversation[]
  total: number
}

export interface Message {
  id: number
  conversation_id?: number
  /** in | out */
  direction?: string
  /** buyer | human | ai:<role> */
  sender?: string
  content?: string
  msg_type?: string
  item_id?: string
  meta?: string
  sent_ok?: boolean | null
  created_at?: string
}

export interface MessageListResult {
  messages: Message[]
  total: number
  page: number
  size: number
  pages: number
}

export interface SendResult {
  message?: Message
  sent?: boolean
  mode?: string
  conversation_id?: number
  /** 协议层未就绪导致没真发出去时才有 */
  warning?: string
}

export interface ModeResult {
  conversation_id: number
  mode: string
  ai_enabled: boolean
}

/** `POST /api/chats/classify` → cs.suggest 的结果（「AI 帮我拟一条」用它）。 */
export interface ClassifyResult {
  intent?: string
  confidence?: number
  replies?: string[]
  quick_replies?: string[]
  [key: string]: unknown
}

/* ------------------------------------------------------------------ 多 AI */

export interface AgentMessage {
  id: number
  conversation_id?: number | null
  /** 形如 "item:1047949964912" | "cross" | "master" */
  from_agent?: string
  to_agent?: string
  /** ask | answer | notify */
  kind?: string
  topic?: string
  /** 后端已 JSON 解码成对象 */
  payload?: Record<string, unknown> | null
  created_at?: string
}

export interface AgentRun {
  id: number
  conversation_id?: number | null
  role?: string
  provider_id?: number | null
  model?: string
  prompt_tokens?: number
  completion_tokens?: number
  latency_ms?: number
  status?: string
  error?: string
  created_at?: string
}

export interface AiRole {
  role: string
  label?: string
  description?: string
  provider_id?: number | null
  model?: string
  temperature?: number
  system_prompt?: string
  /** 后端给的内置默认提示词，用于「恢复默认」 */
  default_prompt?: string
  enabled?: boolean
  updated_at?: string | null
}

export interface AiPrompt {
  id: number
  role?: string
  /** global | account | item */
  scope?: string
  /** scope=account 时有效，否则是空串 */
  account_id?: string
  /** scope=item 时有效，否则是空串 */
  item_id?: string
  name?: string
  content?: string
  /** 同一级别内数字越大越优先 */
  priority?: number
  enabled?: boolean
  updated_at?: string
}

export interface PromptListResult {
  items: AiPrompt[]
  scopes: string[]
  roles: string[]
}

export interface MasterChatReply {
  reply?: string
  tool_calls?: unknown[]
  role?: string
  model?: string
  provider_id?: number | null
}

export interface PromptPreview {
  role: string
  prompt: string
}

/* ------------------------------------------------------------------ 提供者 */

export interface Provider {
  id: number
  name?: string
  base_url?: string
  default_model?: string
  enabled?: boolean
  is_default?: boolean
  /** 后端只回掩码，绝不回明文 Key */
  api_key_masked?: string
  has_key?: boolean
  created_at?: string
  updated_at?: string
}

export interface ProviderTest {
  ok: boolean
  latency_ms?: number
  model?: string
  error?: string
  sample?: string
}

/* ------------------------------------------------------------------ 风控 */

/** risk_state 单行（risk_service._row_to_state）。 */
export interface RiskState {
  account_id?: string
  consecutive_hits?: number
  /** epoch 秒（float） */
  blocked_until?: number
  last_hit_at?: number
  verification_type?: string
  updated_at?: string
  blocked?: boolean
  remaining_seconds?: number
}

/** 带 account_id 的 status 分支。 */
export interface RiskStatusOne {
  account_id: string
  state: RiskState
  cooldown_steps: number[]
  auto_slider: boolean
  blocked: boolean
}

/** 不带 account_id 的 status 分支 —— 注意 `blocked` 这里是**账号 id 数组**。 */
export interface RiskStatusAll {
  states: RiskState[]
  blocked: string[]
  cooldown_steps: number[]
  auto_slider: boolean
  refresh_interval_seconds: number
}

export interface RiskEvent {
  id: number
  account_id?: string
  /** hit | clear_refused | ... */
  kind?: string
  message?: string
  created_at?: string
}

export interface RiskEventListResult {
  events: RiskEvent[]
  total: number
}

export interface RiskSettings {
  auto_slider: boolean
  cooldown_steps: number[]
  refresh_interval_seconds: number
}

export interface ClearResult {
  account_id: string
  cleared: boolean
  state: RiskState
}

/* ------------------------------------------------------------------ 系统 */

export interface SystemInfo {
  version?: string
  python?: string
  platform?: string
  app_dir?: string
  db_path?: string
  frontend_dist?: string
  frontend_built?: boolean
  /** 是否为打包后的 exe 运行 */
  frozen?: boolean
}

export interface WsEvent {
  topic: string
  payload: Record<string, unknown>
  ts?: number
}
