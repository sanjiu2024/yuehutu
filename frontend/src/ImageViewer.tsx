/**
 * 通用看图器（全站唯一一份，商品图 / 会话图片共用）。
 *
 * 为什么要有它：用户反馈「没法放大图片」—— 原来的大图预览只能按窗口大小等比显示，
 * 小图永远那么小、细节看不清。这里补上真正的缩放：
 *   - 滚轮缩放（鼠标位置为中心）、双击在「适应窗口 / 200%」之间切换
 *   - 放大后可拖动平移，`适应` 一键回到初始大小
 *   - 百分比显示的是**相对原图像素**的比例（1:1 = 100%），不是相对窗口
 *   - ← / → 换图、Esc 关闭、+ / - 缩放、0 复位
 *
 * 约定：
 *   - 只吃 `urls`，不关心图片从哪来（商品图床 / IM CDN / 本地 blob 都能显示）。
 *   - 样式写在 `imageviewer.css`，前缀 `iv-`，颜色只用 `styles.css` 里的冻结令牌。
 *   - 不引第三方依赖（打包要离线可用）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { IconChevronLeft, IconChevronRight, IconClose, IconCopy, IconExternal, IconMaximize, IconMinus, IconPlus } from './icons'
import { useToast } from './components'
import './imageviewer.css'

export interface ImageViewerProps {
  urls: string[]
  /** 从第几张开始（0 起）。 */
  start?: number
  title?: string
  /** 标题下面的一行说明，比如「买家发来的图片」。 */
  note?: string
  onClose: () => void
}

const ZOOM_MIN = 1
const ZOOM_MAX = 8
const ZOOM_STEP = 0.35

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

export function ImageViewer({ urls, start = 0, title = '查看图片', note, onClose }: ImageViewerProps) {
  const toast = useToast()
  const list = useMemo(() => (urls || []).filter(Boolean), [urls])
  const [index, setIndex] = useState(() => clamp(start, 0, Math.max(0, list.length - 1)))
  /** 1 = 适应窗口；>1 = 放大倍数（相对「适应窗口」那一档）。 */
  const [zoom, setZoom] = useState(1)
  /** 适应窗口时图片相对原图像素的比例，加载完成后才知道（用于显示真实百分比）。 */
  const [baseScale, setBaseScale] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [failed, setFailed] = useState(false)
  const [dragging, setDragging] = useState(false)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)
  const url = list[index] || ''

  const reset = useCallback(() => {
    setZoom(1)
    setOffset({ x: 0, y: 0 })
  }, [])

  const go = useCallback(
    (delta: number) => {
      if (list.length < 2) return
      setIndex((current) => (current + delta + list.length) % list.length)
      reset()
      setFailed(false)
    },
    [list.length, reset],
  )

  // 换图后重新算 baseScale
  useEffect(() => {
    setBaseScale(1)
  }, [index, url])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      } else if (event.key === 'ArrowLeft') {
        go(-1)
      } else if (event.key === 'ArrowRight') {
        go(1)
      } else if (event.key === '+' || event.key === '=') {
        setZoom((z) => clamp(z + ZOOM_STEP, ZOOM_MIN, ZOOM_MAX))
      } else if (event.key === '-' || event.key === '_') {
        setZoom((z) => clamp(z - ZOOM_STEP, ZOOM_MIN, ZOOM_MAX))
      } else if (event.key === '0') {
        reset()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go, onClose, reset])

  // 缩放归 1 时把平移也归零，避免「看着是适应窗口、其实图被拖偏了」
  useEffect(() => {
    if (zoom <= 1.001) setOffset({ x: 0, y: 0 })
  }, [zoom])

  const onWheel = (event: React.WheelEvent) => {
    if (!url) return
    const factor = event.deltaY < 0 ? 1 + ZOOM_STEP : 1 / (1 + ZOOM_STEP)
    setZoom((z) => clamp(z * factor, ZOOM_MIN, ZOOM_MAX))
  }

  const onDoubleClick = () => {
    setZoom((z) => (z > 1.001 ? 1 : 2))
  }

  const onMouseDown = (event: React.MouseEvent) => {
    if (zoom <= 1.001) return
    event.preventDefault()
    dragRef.current = { x: event.clientX, y: event.clientY, ox: offset.x, oy: offset.y }
    setDragging(true)
  }

  const onMouseMove = (event: React.MouseEvent) => {
    const drag = dragRef.current
    if (!drag) return
    const stage = stageRef.current
    const limitX = stage ? (stage.clientWidth * (zoom - 1)) / 2 : 4000
    const limitY = stage ? (stage.clientHeight * (zoom - 1)) / 2 : 4000
    setOffset({
      x: clamp(drag.ox + (event.clientX - drag.x), -limitX, limitX),
      y: clamp(drag.oy + (event.clientY - drag.y), -limitY, limitY),
    })
  }

  const endDrag = () => {
    dragRef.current = null
    setDragging(false)
  }

  const onImgLoad = (event: React.SyntheticEvent<HTMLImageElement>) => {
    const img = event.currentTarget
    if (!img.naturalWidth) return
    const shown = img.getBoundingClientRect().width
    setBaseScale(shown > 0 ? shown / img.naturalWidth : 1)
    setFailed(false)
  }

  const percent = Math.round(zoom * baseScale * 100)
  const fitToWindow = () => setZoom(1)
  const actualSize = () => setZoom(baseScale > 0 ? clamp(1 / baseScale, ZOOM_MIN, ZOOM_MAX) : 1)

  const copyUrl = async () => {
    if (!url) return
    try {
      await navigator.clipboard.writeText(url)
      toast.success('图片地址已复制')
    } catch {
      toast.error('复制失败，请手动选中地址')
    }
  }

  const openExternal = () => {
    if (!url) return
    // 交给系统浏览器打开原图：WebView2 里 window.open 会走系统默认浏览器，
    // 拿不到返回值也不影响主界面。
    window.open(url, '_blank', 'noopener,noreferrer')
  }

  if (!list.length) return null

  return (
    <div className="iv-mask" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="iv" role="dialog" aria-modal="true" aria-label={title}>
        <div className="iv-head">
          <div className="iv-head-text">
            <span className="iv-title ellipsis" title={title}>
              {title}
            </span>
            {note && <span className="iv-note">{note}</span>}
          </div>
          <span className="iv-count">
            {index + 1} / {list.length}
          </span>
          <button className="iv-icon-btn" onClick={onClose} title="关闭（Esc）" aria-label="关闭">
            <IconClose size={16} />
          </button>
        </div>

        <div
          className={`iv-stage${dragging ? ' dragging' : ''}${zoom > 1.001 ? ' zoomed' : ''}`}
          ref={stageRef}
          onWheel={onWheel}
          onDoubleClick={onDoubleClick}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
          onMouseUp={endDrag}
          onMouseLeave={endDrag}
        >
          {failed ? (
            <div className="iv-err">这张图打不开了（地址失效或图床不回源）</div>
          ) : (
            <img
              src={url}
              alt={title}
              draggable={false}
              className="iv-img"
              style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }}
              onLoad={onImgLoad}
              onError={() => setFailed(true)}
            />
          )}

          {list.length > 1 && (
            <>
              <button className="iv-nav prev" onClick={() => go(-1)} title="上一张（←）" aria-label="上一张">
                <IconChevronLeft size={20} />
              </button>
              <button className="iv-nav next" onClick={() => go(1)} title="下一张（→）" aria-label="下一张">
                <IconChevronRight size={20} />
              </button>
            </>
          )}
        </div>

        <div className="iv-foot">
          <div className="iv-tools">
            <button className="iv-icon-btn" onClick={() => setZoom((z) => clamp(z - ZOOM_STEP, ZOOM_MIN, ZOOM_MAX))} title="缩小（-）" aria-label="缩小">
              <IconMinus size={16} />
            </button>
            <span className="iv-percent" title="相对原图像素的比例">
              {percent}%
            </span>
            <button className="iv-icon-btn" onClick={() => setZoom((z) => clamp(z + ZOOM_STEP, ZOOM_MIN, ZOOM_MAX))} title="放大（+）" aria-label="放大">
              <IconPlus size={16} />
            </button>
            <button className="iv-text-btn" onClick={fitToWindow} title="适应窗口（0）">
              <IconMaximize size={15} />
              适应
            </button>
            <button className="iv-text-btn" onClick={actualSize} title="按原图像素显示（1:1）">
              1:1
            </button>
          </div>
          <div className="iv-tools">
            <button className="iv-icon-btn" onClick={copyUrl} title="复制图片地址" aria-label="复制图片地址">
              <IconCopy size={16} />
            </button>
            <button className="iv-icon-btn" onClick={openExternal} title="用系统浏览器打开原图" aria-label="用系统浏览器打开原图">
              <IconExternal size={16} />
            </button>
          </div>
        </div>

        {list.length > 1 && (
          <div className="iv-thumbs">
            {list.map((item, i) => (
              <button
                key={`${item}-${i}`}
                className={`iv-thumb${i === index ? ' active' : ''}`}
                onClick={() => {
                  setIndex(i)
                  reset()
                  setFailed(false)
                }}
                title={`第 ${i + 1} 张`}
              >
                <img src={item} alt={`第 ${i + 1} 张`} />
              </button>
            ))}
          </div>
        )}

        <div className="iv-hint">滚轮缩放 · 放大后拖动平移 · 双击放大/还原 · ←/→ 换图 · Esc 关闭</div>
      </div>
    </div>
  )
}

export default ImageViewer
