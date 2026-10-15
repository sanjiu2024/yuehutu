"""通知中心接口：读（频道 + 未读数）+ 写（标记一条已读）。

官方页面：`/notification-center`。HTTP 路径是 **`/api/notifications/{account_id}`**
（复数，与前端页面语义一致；模块名保持 `notify.py`，main.py 按模块名挂载）。

- `GET  /api/notifications/{account_id}` —— 只读：频道树 + 未读总数
  （通知条目本身由 IM 长连接推送，REST 没有列表接口）。
- `POST /api/notifications/{account_id}/read` —— **写**：把一条通知标记为已读
  （`mtop.taobao.idlemessage.pc.notifys.read`，真机请求原文
  `logs/seller_discover/notify_capture2.json:93-95`）。body：
  `{"message_id": "...", "confirm": true}`。

`confirm` **原样**交服务层判（路由层不做 `bool()`）—— `bool('false')` 是 `True`，
会把用户明确的拒绝当成同意（第 38 批那条真缺陷的教训）。未确认时服务层第一行就返回
400 `confirmation_required`，**一个平台请求都不发**。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, notify
from .deps import current_user

router = APIRouter(prefix='/api/notifications', tags=['notify'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}')
def notifications(account_id: str, page: str = Query('', description='页码，从 1 开始'),
                  size: str = Query('', description='每页条数，1~100'),
                  user: Any = Depends(current_user)):
    """通知频道 + 未读数（只读；条目本身走 IM 长连接，REST 无列表接口）。"""
    return _envelope(notify.notifications(account_id, page, size))


@router.post('/{account_id}/read')
def mark_read(account_id: str, body: Dict[str, Any] = Body(default={}),
              user: Any = Depends(current_user)):
    """把**一条**通知标记为已读（写，`confirm=true` 才发）。

    `message_id` 取自通知条目（真机样例 `4338118620042.PNM`）；平台没给回执时
    返回 `saved=false` + warning，**不声称已读**。
    """
    return _envelope(notify.mark_read(account_id, body.get('message_id'),
                                      confirm=body.get('confirm')))
