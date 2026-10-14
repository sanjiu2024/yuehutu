"""IM 工具族接口（官方卖家工作台 IM 页的「会话搜索 / 商品抽屉 / 清未读」）。

与 `/api/im` 上已有的会话操作族共用前缀（同一套 `im` tag），但协议来源不同模块
（`xianyu/im_tools.py`）：

- `GET  /api/im/{account_id}/sessions/search` —— 会话搜索（只读，`keyword` 可为空）
- `GET  /api/im/{account_id}/item-search` —— 商品搜索（只读，`role` ∈ `own`（我的宝贝）/
  `other`（TA的宝贝）；`session_id` 或 `conversation_id` 二选一）
- `POST /api/im/{account_id}/unread/clean` —— 清未读（写，`confirm=true` 才发；
  `kind` ∈ `session`（买家会话）/ `system`（系统通知））

写操作的 `confirm` **原样**交服务层判（不做 `bool()`）；平台没返回的字段一律 `null`。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, im_tools
from .deps import current_user

router = APIRouter(prefix='/api/im', tags=['im'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/sessions/search')
def session_search(account_id: str, keyword: str = Query(default=''),
                   page: int = Query(default=1, ge=1),
                   size: int = Query(default=im_tools.proto.PAGE_SIZE_DEFAULT, ge=1,
                                     le=im_tools.proto.PAGE_SIZE_MAX),
                   user: Any = Depends(current_user)):
    """会话搜索（只读）。平台没返回列表键时 `sessions` 是 `null` + warning（不是空列表）。"""
    return _envelope(im_tools.session_search(account_id, keyword, page, size))


@router.get('/{account_id}/item-search')
def item_search(account_id: str, session_id: str = Query(default=''),
                conversation_id: str = Query(default=''),
                role: str = Query(default='own'),
                keyword: str = Query(default=''),
                page: int = Query(default=1, ge=1),
                size: int = Query(default=im_tools.proto.PAGE_SIZE_DEFAULT, ge=1,
                                  le=im_tools.proto.PAGE_SIZE_MAX),
                user: Any = Depends(current_user)):
    """商品搜索（只读）：官方 IM 页「我的宝贝 / TA的宝贝」两个 tab。

    只给本地会话行 id 时用 `conversation_id`，由服务层换成平台 cid（换不到 400 `missing_cid`）。
    """
    return _envelope(im_tools.item_search(account_id, session_id, conversation_id, role,
                                          keyword, page, size))


@router.post('/{account_id}/unread/clean')
def clear_unread(account_id: str, body: Dict[str, Any] = Body(default={}),
                 user: Any = Depends(current_user)):
    """清未读（写，`confirm=true` 才发）。

    官方这条接口发完就不看响应 ⇒ 信封成功即认为平台已收到，并同步清零本地未读；
    平台明确回 `success: false` 时 502 且不动本地；没回回执字段时带 warning
    `im_unread_unconfirmed`（不谎报「平台已确认」）。
    """
    return _envelope(im_tools.clear_unread(account_id, body.get('kind') or 'session',
                                           confirm=body.get('confirm')))
