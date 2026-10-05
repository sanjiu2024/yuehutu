"""WebSocket 推送：把事件总线的消息实时推给前端。"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core import events, security

router = APIRouter(tags=['ws'])


@router.websocket('/api/ws')
async def ws_endpoint(websocket: WebSocket):
    token = websocket.query_params.get('token', '')
    if security.verify_token(token) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = await events.subscribe()

    async def pump():
        while True:
            event = await queue.get()
            await websocket.send_json(event)

    task = asyncio.create_task(pump())
    try:
        while True:
            # 前端心跳/占位消息；收不到就当作断开
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        events.unsubscribe(queue)
