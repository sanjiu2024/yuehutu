"""小铺组（子账号管理 / 客服分流）只读接口。

官方页面：`/seller-account/sub-account`、`/im-cs-dispatch/customer-routing-service`。

**只读**：这两个页面在官方工作台里能改子账号、能改分流规则，本项目只复刻「看」——
本模块不提供任何 POST/PUT/PATCH/DELETE 路由（由 `test_shop_api.py` 的
`test_shop_routes_are_read_only` 钉住）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, shop
from .deps import current_user

router = APIRouter(prefix='/api/shop', tags=['shop'])


def _envelope(result: Any):
    """服务层结果 → HTTP。成功 200 直接给信封体，失败按 `code` 映射状态码。"""
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/sub-accounts')
def sub_accounts(account_id: str, page: str = Query(default=''),
                 size: str = Query(default=''), user: Any = Depends(current_user)):
    """子账号列表（只读）。`page`/`size` 是平台 `pageNo`/`pageSize`。"""
    return _envelope(shop.sub_accounts(account_id, page=page, size=size))


@router.get('/{account_id}/cs-routing')
def cs_routing(account_id: str, page: str = Query(default=''),
               size: str = Query(default=''), user: Any = Depends(current_user)):
    """客服分流配置（只读）。`page`/`size` 只作用于客服分组。"""
    return _envelope(shop.cs_routing(account_id, page=page, size=size))
