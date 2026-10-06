/** WebSocket 客户端：ws://127.0.0.1:<port>/api/ws?token=<token>
 *
 * 后端事件格式（backend/app/core/events.py）：
 *   {"topic": "...", "payload": {...}, "ts": 1700000000.0}
 * 断线自动重连（指数退避，上限 15s）；未登录/无 token 时不连接。
 */

import { getToken } from './api'
import type { WsEvent } from './types'

type Listener = (event: WsEvent) => void
type StatusListener = (connected: boolean) => void

export class WsClient {
  private socket: WebSocket | null = null
  private listeners = new Set<Listener>()
  private statusListeners = new Set<StatusListener>()
  private retry = 0
  private timer: number | null = null
  private closedByUs = false
  private connected = false

  /** 连接状态变化的唯一出口：先比后发，避免同一状态重复通知。 */
  private setConnected(next: boolean): void {
    if (this.connected === next) return
    this.connected = next
    this.statusListeners.forEach((fn) => fn(next))
  }

  connect(): void {
    const token = getToken()
    if (!token || this.socket) return
    this.closedByUs = false
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${scheme}://${location.host}/api/ws?token=${encodeURIComponent(token)}`
    let socket: WebSocket
    try {
      socket = new WebSocket(url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.socket = socket

    socket.onopen = () => {
      this.retry = 0
      this.setConnected(true)
      // 后端 receive_text() 等待心跳；定期发个占位消息保活
      this.timer = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send('ping')
      }, 25000)
    }

    socket.onmessage = (raw) => {
      try {
        const event = JSON.parse(String(raw.data)) as WsEvent
        if (!event || typeof event.topic !== 'string') return
        this.listeners.forEach((fn) => fn(event))
      } catch {
        /* 非 JSON 消息忽略 */
      }
    }

    socket.onclose = () => {
      this.setConnected(false)
      this.cleanup()
      if (!this.closedByUs) this.scheduleReconnect()
    }

    socket.onerror = () => {
      /* onclose 会接着触发 */
    }
  }

  private cleanup(): void {
    if (this.timer !== null) {
      window.clearInterval(this.timer)
      this.timer = null
    }
    this.socket = null
  }

  private scheduleReconnect(): void {
    this.retry += 1
    const delay = Math.min(15000, 1000 * 2 ** Math.min(this.retry, 4))
    window.setTimeout(() => this.connect(), delay)
  }

  disconnect(): void {
    this.closedByUs = true
    this.setConnected(false)
    this.cleanup()
  }

  isConnected(): boolean {
    return this.connected
  }

  /** 订阅连接状态；订阅时立刻回灌当前状态，调用方不必自己再问一次。 */
  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener)
    listener(this.connected)
    return () => {
      this.statusListeners.delete(listener)
    }
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }
}

export const wsClient = new WsClient()

/** 订阅若干 topic；返回取消订阅函数。 */
export function subscribeTopics(topics: string[], listener: Listener): () => void {
  return wsClient.subscribe((event) => {
    if (topics.length === 0 || topics.includes(event.topic)) listener(event)
  })
}
