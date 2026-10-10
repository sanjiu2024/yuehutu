"""通知中心只读接口。

官方页面：`/notification-center`。HTTP 路径是 **`/api/notifications/{account_id}`**
（复数，与前端页面语义一致；模块名保持 `notify.py`，main.py 按模块名挂载）。

**只读**：官方能「全部标记已读」，本项目只复刻「看」——不提供任何写路由
（由 `test_notify_api.py` 的 `test_notify_routes_are_read_only` 钉住）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
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
