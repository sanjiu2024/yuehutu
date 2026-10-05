/**
 * 统一 API 层。
 *
 * 约定（docs/CONTRACTS.md 第 3 节）：
 *   成功 {"success":true,"data":...}
 *   失败 {"success":false,"message":"...","code":"..."}
 * 鉴权：除 /api/auth/* 与 /api/health 外都要 Authorization: Bearer <token>，
 * token 从 localStorage["xywb_token"] 读（桌面外壳自动写入）。
 *
 * 后端路由可能尚未实现（并行开发中）—— 404/网络失败一律转成友好中文错误，
 * 由调用方渲染成提示条，绝不白屏。
 */

import type { Envelope } from './types'

export const TOKEN_KEY = 'xywb_token'

/** 后端路由还没写出来时给的提示（前端页面仍可用，只是数据为空）。 */
export const NOT_READY_HINT = '该功能的后端接口还没就绪，请稍后再试或联系管理员'

export class ApiError extends Error {
  code: string
  status: number
  /** 后端未实现 / 网络不通时为 true，页面据此显示「未就绪」而不是报错红条。 */
  notReady: boolean

  constructor(message: string, code = 'error', status = 0, notReady = false) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.notReady = notReady
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

/** 401 时通知外壳（App 注册回调后统一跳登录/提示）。 */
type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler
}

function friendlyMessage(status: number, message: string, code: string): string {
  if (status === 401 || code === 'unauthorized') return '登录状态已失效，请重新登录'
  if (status === 403) return '没有权限执行该操作'
  if (status === 422) return '参数校验失败：' + message
  if (status >= 500) return '后端内部错误：' + message
  return message || `请求失败（HTTP ${status}）`
}

interface RequestOptions {
  method?: string
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  /** 跳过 Authorization 头（/api/auth/*、/api/health）。 */
  anonymous?: boolean
  signal?: AbortSignal
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  let url = path
  if (query) {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue
      params.append(key, String(value))
    }
    const qs = params.toString()
    if (qs) url += (url.includes('?') ? '&' : '?') + qs
  }
  return url
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, anonymous = false, signal } = options
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (!anonymous) {
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw err
    throw new ApiError('无法连接后端服务，请确认程序是否正在运行', 'network_error', 0, true)
  }

  const text = await response.text()
  let envelope: Envelope<T> | null = null
  if (text) {
    try {
      envelope = JSON.parse(text) as Envelope<T>
    } catch {
      envelope = null
    }
  }

  if (response.status === 401) {
    clearToken()
    onUnauthorized?.()
    throw new ApiError('登录状态已失效，请重新登录', 'unauthorized', 401)
  }

  if (!response.ok) {
    // FastAPI 的默认错误处理器返回 {"detail": "..."}，**没有** success 字段；
    // 后端业务失败走 common.respond，一定带 success/code。据此区分
    // 「路由根本不存在」与「路由在、但这单业务失败了（如账号不存在）」。
    const isFrameworkError = !envelope || typeof envelope.success !== 'boolean'
    const rawMessage =
      envelope?.message ||
      (envelope as unknown as { detail?: string } | null)?.detail ||
      response.statusText ||
      ''
    const code = envelope?.code || `http_${response.status}`
    // 后端尚未实现该路由时 FastAPI 直接 404/405；这类才算「未就绪」。
    const notReady = isFrameworkError && (response.status === 404 || response.status === 405 || response.status === 501)
    throw new ApiError(friendlyMessage(response.status, rawMessage, code), code, response.status, notReady)
  }

  if (envelope && envelope.success === false) {
    const code = envelope.code || 'error'
    throw new ApiError(friendlyMessage(response.status, envelope.message || '', code), code, response.status)
  }

  if (envelope && 'data' in envelope) return envelope.data as T
  // 少数接口（如 /api/auth/logout）成功时不带 data
  return undefined as T
}

export const api = {
  get: <T>(path: string, query?: RequestOptions['query'], signal?: AbortSignal) =>
    request<T>(path, { method: 'GET', query, signal }),
  post: <T>(path: string, body?: unknown, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'POST', body, query }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  raw: request,
}

/** 把任意异常转成能显示给用户的中文文案。 */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message || '未知错误'
  return String(err ?? '未知错误')
}

/** 后端未实现/网络不通 —— 页面用它决定显示「未就绪」空态而非错误。 */
export function isNotReady(err: unknown): boolean {
  return err instanceof ApiError && err.notReady
}
