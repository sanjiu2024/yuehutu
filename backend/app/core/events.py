"""进程内事件总线：后端各工作流 publish，WebSocket/SSE 订阅后推给前端。

只做进程内（桌面应用单进程），不引入 redis 之类依赖。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

_recent: Deque[Dict[str, Any]] = deque(maxlen=500)
_subscribers: List['asyncio.Queue[Dict[str, Any]]'] = []
_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """由 FastAPI 启动钩子调用，记录主事件循环。"""
    global _loop
    _loop = loop


def publish(topic: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """线程安全：任何线程都能发。没有订阅者时只留历史。"""
    event = {'topic': topic, 'payload': payload or {}, 'ts': time.time()}
    with _lock:
        _recent.append(event)
        queues = list(_subscribers)
    if not queues or _loop is None:
        return
    for queue in queues:
        try:
            _loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            # 事件循环已关闭（应用退出中），忽略
            pass


async def subscribe() -> 'asyncio.Queue[Dict[str, Any]]':
    queue: 'asyncio.Queue[Dict[str, Any]]' = asyncio.Queue(maxsize=1000)
    with _lock:
        _subscribers.append(queue)
    return queue


def unsubscribe(queue: 'asyncio.Queue[Dict[str, Any]]') -> None:
    with _lock:
        if queue in _subscribers:
            _subscribers.remove(queue)


def recent(limit: int = 100) -> List[Dict[str, Any]]:
    with _lock:
        return list(_recent)[-limit:]
