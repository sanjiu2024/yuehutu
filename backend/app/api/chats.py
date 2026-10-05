"""会话/消息路由（薄层）。

同时提供两套路径：
- 任务卡：`GET /api/chats`、`GET /api/chats/{id}/messages`、`POST /api/chats/{id}/send`、
  `PUT /api/chats/{id}/ai`
- CONTRACTS §3（正式）：`GET /api/chats/conversations`、`GET /api/chats/{id}/messages`、
  `POST /api/chats/{id}/send`、`POST /api/chats/{id}/mode`、
  `POST /api/chats/{id}/ai-draft`（只生成建议草稿、不发送）

`POST /{id}/mode` 是契约名，`PUT /{id}/ai` 是等价别名（{enabled} -> mode）。
`POST /classify` 是给人工回复台的额外辅助路由（意图 + 快捷回复）。

人工接管语义见 `services.chats.send_manual` / `set_ai_enabled`。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import chats, common, cs
from .deps import current_user

router = APIRouter(prefix='/api/chats', tags=['chats'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------- 会话列表


@router.get('')
@router.get('/')
def list_conversations(
    account_id: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    mode: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user=Depends(current_user),
):
    return _envelope(chats.list_conversations(account_id=account_id, keyword=keyword, mode=mode, limit=limit))


@router.get('/conversations')
def list_conversations_contract(
    account_id: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    mode: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user=Depends(current_user),
):
    """CONTRACTS §3 的正式路径。"""
    return _envelope(chats.list_conversations(account_id=account_id, keyword=keyword, mode=mode, limit=limit))


# --------------------------------------------------------------------------- 客服辅助


@router.post('/classify')
def classify(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """给人工回复台：一段买家消息 -> 意图 + 候选快捷回复。"""
    return _envelope(cs.suggest(str(payload.get('text') or payload.get('content') or '')))


# --------------------------------------------------------------------------- 单会话


@router.get('/{conversation_id}')
def get_conversation(conversation_id: int, user=Depends(current_user)):
    return _envelope(chats.get_conversation(conversation_id))


@router.get('/{conversation_id}/messages')
def get_messages(
    conversation_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    limit: Optional[int] = Query(default=None),
    before_id: Optional[int] = Query(default=None),
    user=Depends(current_user),
):
    if limit:
        size = max(1, min(int(limit), 500))
    return _envelope(chats.get_messages(conversation_id, page=page, size=size))


@router.post('/{conversation_id}/send')
def send_manual(conversation_id: int, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """人工回复（接管）。body: {content} 或 {text}"""
    text = str(payload.get('content') or payload.get('text') or '')
    return _envelope(chats.send_manual(conversation_id, text))


@router.put('/{conversation_id}/ai')
def set_ai(conversation_id: int, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """任务卡的 AI 开关别名：{enabled: true/false}（true -> auto，false -> manual）。

    契约名是 `POST /{id}/mode`；这条保留为等价别名，方便早期前端调用。
    """
    enabled = common.truthy(payload.get('enabled', True), True)
    return _envelope(chats.set_ai_enabled(conversation_id, enabled))


@router.post('/{conversation_id}/ai-draft')
def ai_draft(conversation_id: int, payload: Optional[Dict[str, Any]] = Body(default=None), user=Depends(current_user)):
    """CONTRACTS §3：生成 AI 建议草稿（**只生成、不发送、不落库**）-> `{draft}`。

    人工回复台把它填进输入框，卖家确认后再走 `/send`。
    body 可选 `{message_id}`；不给就用该会话最后一条买家消息。
    """
    message_id = (payload or {}).get('message_id')
    return _envelope(chats.ai_draft(conversation_id, message_id))


@router.post('/{conversation_id}/mode')
def set_mode(conversation_id: int, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """CONTRACTS §3：{mode: auto|manual|off}"""
    return _envelope(chats.set_mode(conversation_id, str(payload.get('mode') or '')))


@router.post('/{conversation_id}/read')
def mark_read(conversation_id: int, user=Depends(current_user)):
    return _envelope(chats.mark_read(conversation_id))
