/** 与 docs/CONTRACTS.md 第 2/3 节对齐的数据类型。字段全部按后端可能缺省处理（可选）。 */

export interface Envelope<T = unknown> {
  success: boolean
  data?: T
  message?: string
  code?: string
}

export interface Account {
  id: string
  nickname?: string
  avatar_url?: string
  remark?: string
  enabled?: number | boolean
  auto_reply?: number | boolean
  login_state?: string
  runtime_state?: string
  created_at?: string
  updated_at?: string
}

export interface QrStart {
  ticket: string
  qr_image_b64: string
  expires_in?: number
}

export type QrState = 'pending' | 'scanned' | 'confirmed' | 'expired' | 'failed'

export interface QrPoll {
  state: QrState
  account_id?: string
  message?: string
}

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
  images?: string
  description?: string
  ai_enabled?: number | boolean
  updated_at?: string
  synced_at?: string
}

export interface Conversation {
  id: number
  account_id?: string
  peer_user_id?: string
  peer_nickname?: string
  item_id?: string
  mode?: string
  agent_role?: string
  unread?: number
  last_message_at?: string
  created_at?: string
  last_message?: string
}

export interface Message {
  id: number
  conversation_id?: number
  direction?: string
  sender?: string
  content?: string
  msg_type?: string
  item_id?: string
  meta?: string
  sent_ok?: number
  created_at?: string
}

export interface AgentMessage {
  id: number
  conversation_id?: number
  from_agent?: string
  to_agent?: string
  kind?: string
  topic?: string
  payload?: string
  created_at?: string
}

export interface AgentRun {
  id: number
  conversation_id?: number
  role?: string
  provider_id?: number
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
  provider_id?: number | null
  model?: string | null
  temperature?: number
  system_prompt?: string
  enabled?: number | boolean
  updated_at?: string
}

export interface AiPrompt {
  id: number
  role?: string
  scope?: string
  account_id?: string | null
  item_id?: string | null
  name?: string
  content?: string
  priority?: number
  enabled?: number | boolean
  updated_at?: string
}

export interface Provider {
  id: number
  name?: string
  base_url?: string
  api_key?: string
  default_model?: string
  enabled?: number | boolean
  is_default?: number | boolean
  created_at?: string
  updated_at?: string
}

export interface ProviderTest {
  ok: boolean
  latency_ms?: number
  model?: string
  error?: string
}

export interface RiskStatus {
  account_id?: string
  consecutive_hits?: number
  blocked_until?: number
  last_hit_at?: number
  verification_type?: string
  [key: string]: unknown
}

export interface RiskEvent {
  id: number
  account_id?: string
  kind?: string
  message?: string
  created_at?: string
}

export interface MasterChatReply {
  reply?: string
  tool_calls?: unknown[]
}

export interface OperationLog {
  id?: number
  account_id?: string
  item_id?: string
  action?: string
  success?: number | boolean
  detail?: string
  created_at?: string
}

export interface SystemInfo {
  version?: string
  python?: string
  platform?: string
  app_dir?: string
  db_path?: string
  frontend_dist?: string
  frontend_built?: boolean
}

export interface WsEvent {
  topic: string
  payload: Record<string, unknown>
  ts?: number
}
