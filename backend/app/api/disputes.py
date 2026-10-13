# -*- coding: utf-8 -*-
"""纠纷协商接口（官方闲鱼卖家工作台「投诉 → 联系纠纷小二」）。

官方把这块挂在投诉详情页，弹一个面板：拉历史留言、发文字、发图举证、催办、标记已读。
我们单开 `/api/disputes`：路径里的 `{conversation_id}` 是**纠纷会话 id**（平台 `conversationId`，
由 `GET …/conversation` 拿到的那个），不是买家 IM 会话（那条走 `/api/im`）。

**写操作全部要显式 `confirm=true`**：未确认时一个平台请求都不会发（由 `test_disputes_api.py` 钉住）。
`confirm` **原样**交服务层判（不做 `bool()`）——`bool('false')` 是 `True`，
会把用户明确的拒绝当成同意（第 38 批那条真缺陷的教训）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, disputes
from .deps import current_user

router = APIRouter(prefix='/api/disputes', tags=['disputes'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/conversation')
def get_conversation(account_id: str, order_id: str = Query(default=''),
                     refund_id: str = Query(default=''), dispute_id: str = Query(default=''),
                     user: Any = Depends(current_user)):
    """取（或创建）纠纷会话（只读）。三个单号至少给一个。

    平台没给 `conversationId` 就如实 502 —— 拿不到会话 id，后面每一步都做不了。
    """
    return _envelope(disputes.conversation(account_id, order_id, refund_id, dispute_id))


@router.get('/{account_id}/{conversation_id}/messages')
def list_messages(account_id: str, conversation_id: str, begin_time: str = Query(default=''),
                  user: Any = Depends(current_user)):
    """拉历史留言（只读）。`begin_time` 传上一页返回的 `current_time`。

    平台没给列表键时 `messages` 是 `null` **+ warning**（`[]` 才是「真的没有留言」）。
    """
    return _envelope(disputes.history(account_id, conversation_id, begin_time))


@router.get('/{account_id}/{conversation_id}/unread')
def list_unread(account_id: str, conversation_id: str,
                user: Any = Depends(current_user)):
    """未读留言（只读）。平台没给列表键时 `messages` 是 `null` + warning。"""
    return _envelope(disputes.unread(account_id, conversation_id))


@router.post('/{account_id}/{conversation_id}/messages')
def send_message(account_id: str, conversation_id: str,
                 body: Dict[str, Any] = Body(default={}),
                 user: Any = Depends(current_user)):
    """发一条文字留言（写，`confirm=true` 才发）。"""
    return _envelope(disputes.send_text(account_id, conversation_id, body.get('text'),
                                        confirm=body.get('confirm')))


@router.post('/{account_id}/{conversation_id}/messages/image')
def send_message_image(account_id: str, conversation_id: str,
                       body: Dict[str, Any] = Body(default={}),
                       user: Any = Depends(current_user)):
    """发一张图举证（写，`confirm=true` 才发）。

    `url` 得是平台能取到的地址（本机上传接口 `POST /api/uploads/images` 给的就是）。
    """
    return _envelope(disputes.send_image(
        account_id, conversation_id, body.get('url'),
        ext=body.get('ext'), width=body.get('width'), height=body.get('height'),
        alt_text=body.get('alt_text'), thumb_url=body.get('thumb_url'),
        send_key=body.get('send_key'), confirm=body.get('confirm')))


@router.post('/{account_id}/{conversation_id}/urge')
def urge(account_id: str, conversation_id: str,
         body: Dict[str, Any] = Body(default={}),
         user: Any = Depends(current_user)):
    """催办（写，`confirm=true` 才发）：发一句固定文案的文字留言。"""
    return _envelope(disputes.urge(account_id, conversation_id, confirm=body.get('confirm')))


@router.post('/{account_id}/{conversation_id}/mark-read')
def mark_read(account_id: str, conversation_id: str,
              body: Dict[str, Any] = Body(default={}),
              user: Any = Depends(current_user)):
    """把某条留言标记已读（写，`confirm=true` 才发）。`sequence` 必须是整数。"""
    return _envelope(disputes.mark_read(account_id, conversation_id, body.get('msg_id'),
                                        body.get('sequence'), confirm=body.get('confirm')))
