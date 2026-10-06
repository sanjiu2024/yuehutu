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
        // 静默重拉也要收掉 loading：首屏请求还在飞的时候来一个 WS 事件，会先把上一次
        // 那一跑 cancel 掉（它的 finally 直接 return，不再收 loading），而静默这一跑
        // 自己不置 loading —— 结果就是列表永久转圈、真正的错误被盖住。静默刷新完成
        // 意味着数据已经到位，把 loading 收掉总是对的（值本来就相同的话 React 会跳过）。
        setLoading(false)
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
