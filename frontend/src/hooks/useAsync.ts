/** 数据加载 hook：把「后端还没实现」和「真的出错」区分开，页面永不白屏。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { errorText, isNotReady } from '../api'
import { subscribeTopics } from '../ws'

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

export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  immediate = true,
  /** 订阅这些 WebSocket topic；收到事件就**静默**重拉一次（不闪加载态、不丢已有数据）。 */
  topics: string[] = [],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(immediate)
  const [error, setError] = useState('')
  const [notReady, setNotReady] = useState(false)
  const [tick, setTick] = useState(0)
  const alive = useRef(true)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  // 这次重拉是不是由 WS 事件触发的（静默刷新不该把页面打回加载态）
  const silent = useRef(false)
  const topicKey = topics.join(',')

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    if (!topicKey) return
    return subscribeTopics(topicKey.split(','), () => {
      silent.current = true
      setTick((n) => n + 1)
    })
  }, [topicKey])

  useEffect(() => {
    if (!immediate && tick === 0) return
    let cancelled = false
    const isSilent = silent.current
    silent.current = false
    if (!isSilent) {
      setLoading(true)
      setError('')
      setNotReady(false)
    }
    loaderRef
      .current()
      .then((result) => {
        if (cancelled || !alive.current) return
        setData(result)
        // 静默刷新成功了就把上次的报错清掉
        if (isSilent) setError('')
      })
      .catch((err) => {
        if (cancelled || !alive.current) return
        if (isNotReady(err)) {
          setNotReady(true)
          if (!isSilent) setData(null)
        } else {
          // 静默刷新失败时保留旧数据，只把原因显示出来
          setError(errorText(err))
        }
      })
      .finally(() => {
        if (cancelled || !alive.current) return
        if (!isSilent) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, immediate, topicKey])

  const reload = useCallback(() => {
    silent.current = false
    setTick((n) => n + 1)
  }, [])
  return { data, loading, error, notReady, reload, setData }
}
