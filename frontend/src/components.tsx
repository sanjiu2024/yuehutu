/** 共享 UI 组件（原生 CSS，无第三方库）。 */

import {
  Component,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ErrorInfo,
  type ReactNode,
} from 'react'

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
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 留在控制台便于排查，但不打扰用户
    console.error('[xywb] 页面渲染出错:', error, info.componentStack)
  }

  reset = () => {
    this.setState({ error: null })
    this.props.onReset?.()
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="empty" style={{ padding: '48px 20px' }}>
        <div style={{ fontSize: 34, marginBottom: 8 }}>⚠️</div>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>这个页面出了点问题</div>
        <div className="dim" style={{ fontSize: 12.5, maxWidth: 520, margin: '0 auto 14px' }}>
          不影响其它页面。可以点下面的按钮重试，或换到左侧其它菜单。
        </div>
        <div className="code-block" style={{ maxWidth: 640, margin: '0 auto 14px', textAlign: 'left' }}>
          {String(error?.message || error)}
        </div>
        <div className="row" style={{ justifyContent: 'center' }}>
          <button className="btn primary" onClick={this.reset}>
            重试
          </button>
          <button className="btn" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </div>
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
  const icon = kind === 'error' ? '⚠' : kind === 'warn' ? '!' : 'i'
  return (
    <div className={`notice ${kind}`}>
      <span>{icon}</span>
      <div className="grow">{children}</div>
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
  )
}

/* ---------------- 空态 / 加载 ---------------- */

export function Empty({
  icon = '📭',
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
      <div className="big">{icon}</div>
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
 * 统一的「加载 / 出错 / 未就绪 / 空」渲染器。
 * 后端接口还没写出来时显示友好提示，绝不白屏。
 */
export function AsyncBox({
  loading,
  error,
  notReady,
  empty,
  emptyTitle = '暂无数据',
  emptyDesc,
  emptyIcon = '📭',
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
  onRetry?: () => void
  children: ReactNode
}) {
  if (loading) return <Loading />
  if (notReady)
    return (
      <Notice kind="warn" onRetry={onRetry}>
        该功能的后端接口还没就绪（并行开发中），页面其余部分可正常使用。
      </Notice>
    )
  if (error)
    return (
      <Notice kind="error" onRetry={onRetry}>
        {error}
      </Notice>
    )
  if (empty) return <Empty icon={emptyIcon} title={emptyTitle} desc={emptyDesc} action={onRetry ? <button className="btn sm" onClick={onRetry}>重新加载</button> : undefined} />
  return <>{children}</>
}

/* ---------------- 弹窗 ---------------- */

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? ' wide' : ''}`}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="btn sm ghost" onClick={onClose}>
            ✕
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
  return (
    <Modal
      title={title}
      onClose={onCancel}
      footer={
        <>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>
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

  const push = useCallback((kind: ToastItem['kind'], text: string) => {
    const id = Date.now() + Math.random()
    setItems((prev) => [...prev, { id, kind, text }])
    window.setTimeout(() => setItems((prev) => prev.filter((item) => item.id !== id)), kind === 'error' ? 6000 : 3600)
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
      <div className="toast-host">
        {items.map((item) => (
          <div key={item.id} className={`toast ${item.kind}`}>
            {item.text}
          </div>
        ))}
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
  return (
    <div className="field">
      <label>{label}</label>
      {children}
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
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={`value${small ? ' small' : ''}`} style={tone ? { color: tone } : undefined}>
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
