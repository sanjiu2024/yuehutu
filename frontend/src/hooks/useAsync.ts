/** 数据加载 hook：把「后端还没实现」和「真的出错」区分开，页面永不白屏。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { errorText, isNotReady } from '../api'

export interface AsyncState<T> {
  data: T | null
  loading: boolean
  /** 可展示的中文错误；后端未实现时为空（用 notReady 表示）。 */
  error: string
  /** 后端路由缺失 / 服务没起 —— 显示「功能未就绪」空态。 */
  notReady: boolean
  reload: () => void
  setData: (value: T | null) => void
}

export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = [], immediate = true): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(immediate)
  const [error, setError] = useState('')
  const [notReady, setNotReady] = useState(false)
  const [tick, setTick] = useState(0)
  const alive = useRef(true)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    if (!immediate && tick === 0) return
    let cancelled = false
    setLoading(true)
    setError('')
    setNotReady(false)
    loaderRef
      .current()
      .then((result) => {
        if (cancelled || !alive.current) return
        setData(result)
      })
      .catch((err) => {
        if (cancelled || !alive.current) return
        if (isNotReady(err)) {
          setNotReady(true)
          setData(null)
        } else {
          setError(errorText(err))
        }
      })
      .finally(() => {
        if (cancelled || !alive.current) return
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, immediate])

  const reload = useCallback(() => setTick((n) => n + 1), [])
  return { data, loading, error, notReady, reload, setData }
}
