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

/**
 * 桌面外壳自动登录：把 URL 上的 `?desktop_token=` 收进 localStorage **并在首屏渲染前清掉**。
 *
 * 为什么必须走 URL 而不是让外壳 `window.evaluate_js` 写 localStorage：
 * pywebview 的 `events.loaded` 在**前端 bundle 执行完之后**才触发，而 `App` 的
 * `authed` 初值是在 bundle 执行时读 `getToken()` 的 —— 等外壳再写就晚了，
 * 界面已经渲染成登录页，用户看到「请登录」而不知道密码（实测打包产物就是这个症状）。
 * 查询串在文档解析前就存在，所以这里在模块加载时同步取一次。
 */
export function adoptDesktopToken(): void {
  try {
    const params = new URLSearchParams(window.location.search)
    const token = params.get('desktop_token')
    if (!token) return
    localStorage.setItem(TOKEN_KEY, token)
    params.delete('desktop_token')
    const query = params.toString()
    window.history.replaceState(
      null,
      '',
      `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`,
    )
  } catch {
    // 隐私模式 / 无 window：静默跳过，用户仍可手工登录
  }
}

// 模块副作用：任何页面 import 到 api.ts 就已经生效，早于 App 首屏渲染。
if (typeof window !== 'undefined') adoptDesktopToken()

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

/**
 * 把后端的失败信封翻成人话。
 *
 * 后端用语义状态码（`backend/app/services/common.py` 的 FAIL_STATUS）：
 *  - 503 `xianyu_not_ready` —— 协议层依赖不可用（闲鱼客户端没起来/没登录）
 *  - 409 `verification_required` —— 没检测到人工验证，拒绝清除冷却
 *  - 502 `protocol_error` / 模型调用失败 —— 上游没配合
 * 这些**都不是**「后端崩了」，要显示成可读的业务提示，绝不能只说「请求失败」。
 */
function friendlyMessage(status: number, message: string, code: string): string {
  if (status === 401 || code === 'unauthorized') return '登录状态已失效，请重新登录'
  if (status === 403) return '没有权限执行该操作'

  // 先按业务 code 判，比状态码更精确
  if (code === 'xianyu_not_ready') {
    return message || '闲鱼协议层还没就绪（客户端未启动或未登录），请先在「账号」页完成扫码登录'
  }
  if (code === 'verification_required') {
    return message || '还没检测到人工验证成功，暂时不能解除冷却'
  }
  if (code === 'protocol_error') {
    return message || '和闲鱼通信失败，请稍后重试'
  }
  if (code === 'ai_error') {
    return message || 'AI 调用失败，请到「AI 配置」检查 API Key 与模型'
  }
  if (code === 'not_found') return message || '没找到对应的数据'
  if (code === 'invalid_fields' || code === 'bad_request') return message || '参数不合法'

  if (status === 503) return message || '依赖的服务暂时不可用，请稍后重试'
  if (status === 502) return message || '上游服务返回异常，请稍后重试'
  if (status === 409) return message || '当前状态下不能执行该操作'
  if (status === 422) return '参数校验失败：' + (message || '请检查填写内容')
  if (status >= 500) return '后端内部错误：' + (message || `HTTP ${status}`)
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

/**
 * 把一次 HTTP 响应（fetch 或 XHR 拿到的）翻成 `T` 或抛出 `ApiError`。
 *
 * 抽出来是为了让 JSON 接口与 multipart 上传共用**同一套**信封/401/未就绪判定，
 * 免得上传路径自己再写一遍错误映射、两边判断不一致。
 */
function handleResponse<T>(
  status: number,
  statusText: string,
  text: string,
  ok: boolean,
  anonymous: boolean,
  sentToken: boolean,
): T {
  let envelope: Envelope<T> | null = null
  if (text) {
    try {
      envelope = JSON.parse(text) as Envelope<T>
    } catch {
      envelope = null
    }
  }

  if (status === 401) {
    // 只有「真的带了 token 出去的请求」才说明会话失效。
    // 匿名请求（anonymous: true）收到 401 只意味着「这个接口需要登录」，
    // 不能据此清掉用户的会话 —— 否则挂载时那次匿名的探测会把刚登录的用户立刻踢回登录页。
    if (!anonymous && sentToken) {
      clearToken()
      onUnauthorized?.()
    }
    throw new ApiError('登录状态已失效，请重新登录', 'unauthorized', 401)
  }

  if (!ok) {
    // FastAPI 的默认错误处理器返回 {"detail": "..."}，**没有** success 字段；
    // 后端业务失败走 common.respond，一定带 success/code。据此区分
    // 「路由根本不存在」与「路由在、但这单业务失败了（如账号不存在）」。
    // 注意：后端为了统一信封，会把**路由层**的 404/405 也套成信封，但给了
    // 可区分的 `code: 'route_not_found'`（见 backend/app/main.py 的 http_error）。
    const isFrameworkError = !envelope || typeof envelope.success !== 'boolean'
    const rawMessage =
      envelope?.message ||
      (envelope as unknown as { detail?: string } | null)?.detail ||
      statusText ||
      ''
    const code = envelope?.code || `http_${status}`
    // 后端尚未实现该路由时 FastAPI 直接 404/405；这类才算「未就绪」。
    const routeMissing = code === 'route_not_found'
    const notReady = (isFrameworkError || routeMissing)
      && (status === 404 || status === 405 || status === 501)
    throw new ApiError(friendlyMessage(status, rawMessage, code), code, status, notReady)
  }

  if (envelope && envelope.success === false) {
    const code = envelope.code || 'error'
    throw new ApiError(friendlyMessage(status, envelope.message || '', code), code, status)
  }

  if (envelope && 'data' in envelope) return envelope.data as T
  // 少数接口（如 /api/auth/logout）成功时不带 data
  return undefined as T
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, anonymous = false, signal } = options
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let sentToken = false
  if (!anonymous) {
    const token = getToken()
    if (token) {
      headers['Authorization'] = `Bearer ${token}`
      sentToken = true
    }
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
  return handleResponse<T>(response.status, response.statusText, text, response.ok, anonymous, sentToken)
}

/** `POST /api/uploads/images` 成功的返回。 */
export interface UploadedImage {
  /** 平台可用的 https 图片地址（提交编辑/发布时放进 `images` 的就是它）。 */
  url: string
  file_id?: string
  width?: number
  height?: number
  name?: string
  /** 后端本地落盘路径，仅调试用，不要展示给用户。 */
  local_path?: string
}

export interface UploadOptions {
  /** 可选：带上账号，后端据此决定图片落到哪个账号的目录/平台。 */
  accountId?: string
  /** 真实上传进度（0-100）。浏览器只在 XHR 上给得到 `upload.onprogress`。 */
  onProgress?: (percent: number) => void
  signal?: AbortSignal
}

/**
 * 编辑/发布请求体里 `images` 数组的一项。**同一个数组里两种都允许**：
 *
 * - **刚上传的图** → `{url, width, height}`：`uploadImage` 已经回了真实尺寸，
 *   平台 `imageInfoDOList` 的 `widthSize`/`heightSize` 就不用退化成占位 `'0'`；
 * - **库里读出来的历史图** → 裸 URL 字符串：库里只存了地址，没有尺寸。
 *   尺寸是**编不出来的**（协议层宁可填 `'0'` 也不硬编码 1254，因为那是撒谎），
 *   所以这里也绝不瞎猜，缺尺寸就老实传字符串。
 *
 * 后端 `_image_entry`（`backend/app/xianyu/client.py:1211`）两种形态都认，
 * 并且只有 **URL 字符串**会落进 `items.images` 列。
 */
export type ImageInput = string | { url: string; width?: number; height?: number }

/**
 * 上传单张图片到 `POST /api/uploads/images`（multipart，字段 `file`）。
 *
 * 为什么用 XHR 而不是 fetch：只有 `XMLHttpRequest.upload.onprogress` 能在**发送过程中**
 * 拿到已上传字节数，fetch 至今没有请求体进度。用户拖进几张图后需要看到进度条，
 * 否则不知道是卡住了还是在传。
 * 注意**不要**手写 `Content-Type` —— multipart 的 boundary 必须由浏览器生成。
 */
export function uploadImage(file: File, options: UploadOptions = {}): Promise<UploadedImage> {
  const { accountId, onProgress, signal } = options
  return new Promise<UploadedImage>((resolve, reject) => {
    const form = new FormData()
    form.append('file', file, file.name || 'image')
    if (accountId) form.append('account_id', accountId)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/uploads/images')
    const token = getToken()
    let sentToken = false
    if (token) {
      xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      sentToken = true
    }

    if (signal) {
      if (signal.aborted) {
        reject(new ApiError('上传已取消', 'aborted', 0))
        return
      }
      signal.addEventListener('abort', () => xhr.abort(), { once: true })
    }

    xhr.upload.onprogress = (event) => {
      if (!onProgress) return
      onProgress(event.lengthComputable && event.total > 0 ? Math.round((event.loaded / event.total) * 100) : 0)
    }
    xhr.onload = () => {
      try {
        resolve(
          handleResponse<UploadedImage>(
            xhr.status,
            xhr.statusText,
            xhr.responseText,
            xhr.status >= 200 && xhr.status < 300,
            false,
            sentToken,
          ),
        )
      } catch (err) {
        reject(err)
      }
    }
    xhr.onerror = () =>
      reject(new ApiError('图片上传失败：无法连接后端服务，请确认程序是否正在运行', 'network_error', 0, true))
    xhr.ontimeout = () => reject(new ApiError('图片上传超时，请重试', 'timeout', 0, true))
    xhr.onabort = () => reject(new ApiError('上传已取消', 'aborted', 0))

    xhr.send(form)
  })
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
