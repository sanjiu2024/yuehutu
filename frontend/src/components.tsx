/** 共享 UI 组件（原生 CSS，无第三方库）。
 *
 * task-31 起：**全站不再使用 emoji 作为图标**，一律走 `./icons` 里的 SVG。
 * 导出名与 props 签名保持向后兼容（只新增，不删改），因为 9 个页面都在用。
 */

import {
  cloneElement,
  Component,
  createContext,
  isValidElement,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ErrorInfo,
  type ReactElement,
  type ReactNode,
} from 'react'
import {
  ICONS,
  IconBox,
  IconChart,
  IconCheck,
  IconCheckCircle,
  IconChat,
  IconClock,
  IconClose,
  IconInfo,
  IconKey,
  IconMoon,
  IconSearch,
  IconSettings,
  IconShield,
  IconSun,
  IconUser,
  IconWarn,
  IconXCircle,
  Logo,
  type IconName,
} from './icons'

/* ---------------- 图标解析 ---------------- */

/**
 * 把「图标名」解析成 `IconName`。
 *
 * `Empty.icon` / `AsyncBox.emptyIcon` 的 props 类型是 `string`（历史上有页面传过 emoji），
 * 认不出来的名字一律回落兜底图标 —— 所以**任何页面都不可能再渲染出 emoji**。
 * 9 个页面现已全部改成传 `IconName`（`"box"`、`"user"`、`"chat"`…），
 * 因此这里不再保留 emoji 别名表：全站源码与产物里都不留 emoji 字形。
 */
function resolveIcon(name: string | undefined, fallback: IconName = 'box'): IconName {
  if (name && name in ICONS) return name as IconName
  if (name) {
    // 拼错的图标名以前会静默变成盒子图标，排查时很难定位，这里留一条线索
    console.warn(`[xywb] 未知图标名「${name}」，已回落为「${fallback}」。可用图标见 icons.tsx 的 ICONS。`)
  }
  return fallback
}

/** 按名字渲染一个图标（供 `Empty` / `AsyncBox` 这类「图标名是 string」的场合用）。 */
function NamedIcon({ name, size = 24 }: { name?: string; size?: number }) {
  const Icon = ICONS[resolveIcon(name)]
  return <Icon size={size} />
}

/* ---------------- 错误边界 ---------------- */

/**
 * 兜底错误边界。
 *
 * 硬要求是「任何 404/失败都要显示友好提示，绝不白屏」——接口层已经用 AsyncBox 兜住了，
 * 但渲染期抛错（后端返回了预期外的形状、undefined 取属性等）会直接炸掉整棵树变白屏。
 * 这里把整页包住，至少让用户看到发生了什么、能重试。
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; onReset?: () => void },
  { error: Error | null; showDetail: boolean }
> {
  state: { error: Error | null; showDetail: boolean } = { error: null, showDetail: false }

  /** 报错原文只给「反馈问题」用；复制失败（无剪贴板权限）静默忽略，不二次打扰用户 */
  copy = (text: string) => {
    try {
      void navigator.clipboard?.writeText(text)
    } catch {
      /* 忽略：用户仍可手动选中复制 */
    }
  }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 留在控制台便于排查，但不打扰用户
    console.error('[xywb] 页面渲染出错:', error, info.componentStack)
  }

  reset = () => {
    this.setState({ error: null, showDetail: false })
    this.props.onReset?.()
  }

  render() {
    const { error, showDetail } = this.state
    if (!error) return this.props.children
    // 默认只给人话：英文 JS 报错铺在页面正中只会让卖家更慌，也帮不上排查（第 58 批 UI 审查第 8 条）
    const raw = String(error?.stack || error?.message || error)
    return (
      <div className="empty" style={{ padding: '48px 20px' }}>
        <div className="empty-icon">
          <IconXCircle size={26} />
        </div>
        <div className="title">这个页面出了点问题</div>
        <div className="desc">不影响其它页面。可以点下面的按钮重试，或换到左侧其它菜单；错误已记到控制台。</div>
        <div className="row" style={{ justifyContent: 'center', marginBottom: 10 }}>
          <button className="btn primary" onClick={this.reset}>
            重试
          </button>
          <button className="btn" onClick={() => window.location.reload()}>
            刷新页面
          </button>
          <button
            className="btn ghost"
            onClick={() => this.setState({ showDetail: !showDetail })}
            aria-expanded={showDetail}
          >
            {showDetail ? '收起报错详情' : '查看报错详情'}
          </button>
        </div>
        {showDetail && (
          <div style={{ maxWidth: 640, margin: '0 auto', textAlign: 'left' }}>
            <div className="code-block" style={{ maxHeight: 180, overflow: 'auto', marginBottom: 8 }}>
              {raw}
            </div>
            <div className="row" style={{ justifyContent: 'flex-end' }}>
              <button className="btn sm" onClick={() => this.copy(raw)}>
                复制错误信息
              </button>
            </div>
          </div>
        )}
      </div>
    )
  }
}

/* ---------------- 徽标 ---------------- */

export function Badge({ tone = 'muted', children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>
}

/* ---------------- 提示条 ---------------- */

export function Notice({
  kind = 'info',
  children,
  onRetry,
  onClose,
}: {
  kind?: 'info' | 'warn' | 'error'
  children: ReactNode
  onRetry?: () => void
  onClose?: () => void
}) {
  const Icon = kind === 'error' ? IconXCircle : kind === 'warn' ? IconWarn : IconInfo
  return (
    <div className={`notice ${kind}`}>
      <span className="notice-icon">
        <Icon size={14} />
      </span>
      <div className="notice-body">{children}</div>
      {(onRetry || onClose) && (
        <div className="notice-actions">
          {onRetry && (
            <button className="btn sm ghost" onClick={onRetry}>
              重试
            </button>
          )}
          {onClose && (
            <button className="btn sm ghost" onClick={onClose}>
              关闭
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/* ---------------- 空态 / 加载 / 骨架 ---------------- */

export function Empty({
  icon = 'box',
  title,
  desc,
  action,
}: {
  icon?: string
  title: string
  desc?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <NamedIcon name={icon} size={26} />
      </div>
      <div className="title">{title}</div>
      {desc && <div className="desc">{desc}</div>}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  )
}

export function Loading({ text = '加载中…' }: { text?: string }) {
  return (
    <div className="loading-box">
      <span className="spinner" />
      <span>{text}</span>
    </div>
  )
}

/**
 * 骨架屏。比转圈的 spinner 更好：位置固定、不跳动，用户能预期内容结构。
 *
 * @example <Skeleton lines={3} />                三行文字
 * @example <Skeleton width={160} height={14} />  单个标题块
 * @example <Skeleton className="skeleton-block" /> 整块占位
 */
export function Skeleton({
  lines = 1,
  width,
  height = 12,
  radius,
  className,
}: {
  lines?: number
  width?: number | string
  height?: number
  radius?: number
  className?: string
}) {
  const extra = className ? ` ${className}` : ''
  if (lines <= 1) {
    return (
      <span
        className={`skeleton skeleton-text${extra}`}
        style={{ width, height, borderRadius: radius }}
      />
    )
  }
  return (
    <div className={`skeleton-group${extra}`}>
      {Array.from({ length: lines }, (_, index) => (
        <span key={index} className="skeleton skeleton-text" style={{ height, borderRadius: radius }} />
      ))}
    </div>
  )
}

/**
 * 统一的「加载 / 出错 / 未就绪 / 空」渲染器。
 * 空态只有三种固定形态（真空 / 筛选没命中 / 未就绪），页面只传文案，不要各页自己拼。
 * 首屏用骨架屏；已有内容时的刷新保留旧内容 + 顶部细进度条（stale-while-revalidate），不整块闪白。
 */
export function AsyncBox({
  loading,
  error,
  notReady,
  empty,
  emptyTitle,
  emptyDesc,
  emptyIcon = 'box',
  emptyFiltered = false,
  onClearFilters,
  emptyAction,
  onRetry,
  children,
}: {
  loading: boolean
  error: string
  notReady: boolean
  empty?: boolean
  emptyTitle?: string
  emptyDesc?: ReactNode
  emptyIcon?: string
  /** 空数组且当前带筛选条件：套用「筛选没命中」的固定文案与一键清空动作。 */
  emptyFiltered?: boolean
  onClearFilters?: () => void
  /** 页面自带的空态主操作（如「扫码添加账号」）。给了就优先于内置的「清空筛选 / 重新加载」。 */
  emptyAction?: ReactNode
  onRetry?: () => void
  children: ReactNode
}) {
  const lastContent = useRef<ReactNode>(null)
  useEffect(() => {
    if (!loading && !error && !notReady && !empty) lastContent.current = children
  }, [loading, error, notReady, empty, children])

  if (loading) {
    const stale = lastContent.current
    // 首屏没有旧内容 → 骨架屏（位置固定、不跳动）；刷新 → 保留旧内容，只在顶部压一条细进度条。
    if (!stale) return <Skeleton lines={4} />
    return (
      <div className="async-refreshing" aria-busy="true">
        <div className="async-progress" role="progressbar" aria-label="正在刷新" />
        {stale}
      </div>
    )
  }
  if (notReady)
    return (
      <Notice kind="warn" onRetry={onRetry}>
        这个功能当前版本还不能用，后续更新会补上。你可以先用左侧其它菜单，或稍后再回来看看。
      </Notice>
    )
  if (error)
    return (
      <Notice kind="error" onRetry={onRetry}>
        {error}
      </Notice>
    )
  if (empty)
    return (
      <Empty
        icon={emptyIcon}
        title={emptyTitle ?? (emptyFiltered ? '没有符合条件的记录' : '暂无数据')}
        desc={emptyDesc ?? (emptyFiltered ? '换个关键词或放宽筛选条件试试。' : undefined)}
        action={
          emptyAction ? (
            emptyAction
          ) : emptyFiltered && onClearFilters ? (
            <button className="btn sm" onClick={onClearFilters}>
              清空筛选
            </button>
          ) : onRetry ? (
            <button className="btn sm" onClick={onRetry}>
              重新加载
            </button>
          ) : undefined
        }
      />
    )
  return <>{children}</>
}

/* ---------------- 分页条 ---------------- */

const DEFAULT_PAGE_SIZES = [10, 20, 50]

/**
 * 统一分页条：上一页 / 下一页 + 页码区间 + 每页条数 + 跳页。
 * 通用电商后台基线（Ant Design、千牛）都长这样，不要再各页自己拼「上一页/下一页」两个按钮。
 * 给 total 才能画页码区间与跳页；只有 has_next 的接口就只画上一页/下一页。
 */
export function Pager({
  page,
  pageSize,
  total,
  onPage,
  onPageSize,
  loading = false,
  hasNext,
  pageSizes = DEFAULT_PAGE_SIZES,
  note,
}: {
  page: number
  pageSize: number
  total?: number
  onPage: (next: number) => void
  onPageSize?: (next: number) => void
  loading?: boolean
  hasNext?: boolean
  pageSizes?: number[]
  note?: ReactNode
}) {
  const [jump, setJump] = useState('')
  const pageCount =
    typeof total === 'number' && Number.isFinite(total) && total >= 0 && pageSize > 0
      ? Math.max(1, Math.ceil(total / pageSize))
      : undefined
  const nums: (number | '…')[] = []
  if (pageCount !== undefined && pageCount > 1) {
    const from = Math.max(1, page - 2)
    const to = Math.min(pageCount, page + 2)
    if (from > 1) {
      nums.push(1)
      if (from > 2) nums.push('…')
    }
    for (let i = from; i <= to; i += 1) nums.push(i)
    if (to < pageCount) {
      if (to < pageCount - 1) nums.push('…')
      nums.push(pageCount)
    }
  }
  const jumpTo = (raw: string) => {
    const n = Math.floor(Number(raw))
    if (!Number.isFinite(n) || n < 1) return
    onPage(pageCount !== undefined ? Math.min(pageCount, n) : n)
    setJump('')
  }
  return (
    <div className="pager">
      {note && <span className="pager-note">{note}</span>}
      <span className="pager-spacer" />
      {onPageSize && (
        <label className="pager-size">
          每页
          <select value={pageSize} disabled={loading} onChange={(e) => onPageSize(Number(e.target.value))}>
            {pageSizes.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          条
        </label>
      )}
      <button type="button" className="btn sm" disabled={loading || page <= 1} onClick={() => onPage(Math.max(1, page - 1))}>
        上一页
      </button>
      {nums.map((n, i) =>
        n === '…' ? (
          <span key={`gap-${i}`} className="pager-gap">
            …
          </span>
        ) : (
          <button
            key={n}
            type="button"
            className={`btn sm pager-num${n === page ? ' active' : ''}`}
            aria-current={n === page ? 'page' : undefined}
            disabled={loading}
            onClick={() => onPage(n)}
          >
            {n}
          </button>
        ),
      )}
      <button
        type="button"
        className="btn sm"
        disabled={loading || (pageCount !== undefined ? page >= pageCount : hasNext === false)}
        onClick={() => onPage(page + 1)}
      >
        下一页
      </button>
      {pageCount !== undefined && pageCount > 1 && (
        <span className="pager-jump">
          跳至
          <input
            className="pager-jump-input"
            value={jump}
            inputMode="numeric"
            aria-label="跳转到第几页"
            onChange={(e) => setJump(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') jumpTo(jump)
            }}
            onBlur={() => jump !== '' && jumpTo(jump)}
          />
          页
        </span>
      )}
    </div>
  )
}

/* ---------------- 弹窗 ---------------- */

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
  initialFocusRef,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
  /** 打开时优先聚焦的元素；危险确认框传「取消」按钮，让默认焦点落在安全项上 */
  initialFocusRef?: { current: HTMLElement | null }
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // 弹窗打开期间锁住背景滚动：长列表页面在弹窗里滚不动、背景却在跑，很容易滚错
  useEffect(() => {
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [])

  // 标题是 ReactNode，不能直接塞 aria-label，改用 aria-labelledby 指向标题元素
  const titleId = useId()

  // 键盘可达性：打开时把焦点收进弹窗、关闭后还给原来的元素。
  // 弹窗内部自己 autoFocus 了输入框时不抢焦点（React 的 autoFocus 在提交阶段已生效）。
  const dialogRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const node = dialogRef.current
    if (initialFocusRef?.current) initialFocusRef.current.focus()
    else if (node && !node.contains(document.activeElement)) node.focus()

    // Tab 循环限制在弹窗内（第 58 批 UI 审查第 1 条）：
    // 否则键盘用户能把焦点走到弹窗背后的按钮上再按空格 —— 危险操作确认框最怕这个。
    const onTab = (event: KeyboardEvent) => {
      if (event.key !== 'Tab' || !node) return
      const focusables = Array.from(
        node.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      )
      if (!focusables.length) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (!active || !node.contains(active)) {
        event.preventDefault()
        first.focus()
        return
      }
      if (event.shiftKey && active === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    window.addEventListener('keydown', onTab, true)
    return () => {
      window.removeEventListener('keydown', onTab, true)
      if (previous && previous.isConnected) previous.focus()
    }
  }, [initialFocusRef])

  return (
    <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        className={`modal${wide ? ' wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div className="modal-head">
          <h3 className="modal-title" id={titleId}>{title}</h3>
          <span className="grow" />
          <button className="modal-close" onClick={onClose} aria-label="关闭" title="关闭">
            <IconClose size={16} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}

export function ConfirmDialog({
  title,
  message,
  confirmText = '确认',
  danger,
  onConfirm,
  onCancel,
  busy,
}: {
  title: string
  message: ReactNode
  confirmText?: string
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
}) {
  // 危险确认框默认聚焦「取消」（安全项）：Enter 不该直接把不可逆操作执行掉
  const cancelRef = useRef<HTMLButtonElement>(null)
  return (
    <Modal
      title={title}
      onClose={onCancel}
      initialFocusRef={cancelRef}
      footer={
        <>
          <button ref={cancelRef} className="btn ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button className={`btn ${danger ? 'danger' : 'primary'}`} onClick={onConfirm} disabled={busy}>
            {busy ? '处理中…' : confirmText}
          </button>
        </>
      }
    >
      <div style={{ fontSize: 13.5, lineHeight: 1.7 }}>{message}</div>
    </Modal>
  )
}

/* ---------------- Toast ---------------- */

export interface ToastItem {
  id: number
  kind: 'success' | 'error' | 'info'
  text: string
}

interface ToastApi {
  success: (text: string) => void
  error: (text: string) => void
  info: (text: string) => void
}

const ToastContext = createContext<ToastApi>({
  success: () => undefined,
  error: () => undefined,
  info: () => undefined,
})

export function useToast(): ToastApi {
  return useContext(ToastContext)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((item) => item.id !== id))
  }, [])

  const push = useCallback((kind: ToastItem['kind'], text: string) => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev, { id, kind, text }])
    // 失败提示**不自动消失**（第 58 批 UI 审查第 9 条）：6 秒后消失，用户回来只看到一切正常，
    // 正是「以为操作成功了」的直接来源；改成常驻 + 手动关闭。
    if (kind !== 'error') {
      window.setTimeout(() => setItems((prev) => prev.filter((item) => item.id !== id)), 3600)
    }
  }, [])

  const api = useMemo<ToastApi>(
    () => ({
      success: (text: string) => push('success', text),
      error: (text: string) => push('error', text),
      info: (text: string) => push('info', text),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* 读屏可达：失败用 role=alert（立即播报），其余 role=status（第 58 批 UI 审查第 9 条） */}
      <div className="toast-host">
        {items.map((item) => {
          const Icon = item.kind === 'error' ? IconXCircle : item.kind === 'success' ? IconCheckCircle : IconInfo
          return (
            <div
              key={item.id}
              className={`toast ${item.kind}`}
              role={item.kind === 'error' ? 'alert' : 'status'}
            >
              <span className="toast-icon">
                <Icon size={15} />
              </span>
              <span className="toast-text">{item.text}</span>
              {item.kind === 'error' && (
                <button className="toast-close" onClick={() => dismiss(item.id)} aria-label="关闭提示" title="关闭">
                  <IconClose size={14} />
                </button>
              )}
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

/* ---------------- 小工具组件 ---------------- */

export function Field({
  label,
  tip,
  children,
}: {
  label: string
  tip?: ReactNode
  children: ReactNode
}) {
  // 标签与控件关联：`<label>` 原来是**兄弟节点**，点标签不会聚焦输入框，
  // 读屏也只会念「组合框」而不说这个框是干什么的（`<select>` 没有 placeholder
  // 可退，问题最明显）。这里只在 children 是**单个**还带 `id` 的元素时补上
  // `id`/`htmlFor`，其余形状（多个元素、已是别的包裹层）行为不变。
  const id = useId()
  const single = isValidElement(children) ? (children as ReactElement<{ id?: string }>) : null
  const linked = single && !single.props.id ? cloneElement(single, { id }) : null
  return (
    <div className="field">
      <label htmlFor={linked ? id : undefined}>{label}</label>
      {linked ?? children}
      {tip && <span className="tip">{tip}</span>}
    </div>
  )
}

export function Stat({
  label,
  value,
  tone,
  small,
  extra,
}: {
  label: string
  value: ReactNode
  tone?: string
  small?: boolean
  extra?: ReactNode
}) {
  // tone 允许直接传 CSS 变量名（'var(--ok)'）或颜色值，保持既有行为不变
  const valueStyle: CSSProperties | undefined = tone ? { color: tone } : undefined
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={`value${small ? ' small' : ''}`} style={valueStyle}>
        {value}
      </div>
      {extra}
    </div>
  )
}

/** 开关：接受 0/1 或 boolean。 */
export function Switch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label?: string
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  )
}

/* ---------------- 主题 ---------------- */

/** 主题只认两套：深色（默认）与浅色。 */
export type ThemeName = 'dark' | 'light'

const THEME_KEY = 'xywb_theme'

/** 读持久化的主题；任何异常都回落深色，绝不因为读不到偏好就白屏。 */
export function readTheme(): ThemeName {
  try {
    return window.localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

/** 应用主题：写 `document.documentElement.dataset.theme`，并持久化。 */
export function applyTheme(theme: ThemeName) {
  document.documentElement.dataset.theme = theme
  try {
    window.localStorage.setItem(THEME_KEY, theme)
  } catch {
    // 隐私模式下 localStorage 可能不可写；主题只在本次会话生效即可
  }
}

/** 启动时调用：把持久化的主题贴到 <html> 上。 */
export function initTheme(): ThemeName {
  const theme = readTheme()
  document.documentElement.dataset.theme = theme
  return theme
}

/** 深/浅色切换按钮（分段控件）。 */
export function ThemeToggle({ theme, onChange }: { theme: ThemeName; onChange: (next: ThemeName) => void }) {
  return (
    <div className="seg" role="group" aria-label="主题">
      <button
        type="button"
        className={`seg-btn${theme === 'dark' ? ' active' : ''}`}
        onClick={() => onChange('dark')}
        title="深色主题"
        aria-pressed={theme === 'dark'}
      >
        <IconMoon size={14} />
        <span>深色</span>
      </button>
      <button
        type="button"
        className={`seg-btn${theme === 'light' ? ' active' : ''}`}
        onClick={() => onChange('light')}
        title="浅色主题"
        aria-pressed={theme === 'light'}
      >
        <IconSun size={14} />
        <span>浅色</span>
      </button>
    </div>
  )
}

/** 工作台 Logo（登录页与侧栏共用）。 */
export function BrandLogo({ size = 30 }: { size?: number }) {
  return <Logo size={size} />
}

/* 顺手再导出几个常用图标，省得各页面为了一个图标去 import 一堆 */
export { IconBox, IconChart, IconChat, IconCheck, IconClock, IconKey, IconSearch, IconSettings, IconShield, IconUser }
export type { IconName }
