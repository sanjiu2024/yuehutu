"""数据组只读接口：数据总览 / 粉丝数据 / 客服数据。

三个 GET，参数只有 `start` / `end`（`YYYYMMDD`，缺省最近 7 天）。响应统一走
`common.respond`：成功 `{"success": true, "data": {...}}`，失败
`{"success": false, "message": "...", "code": "..."}`（`verification_required` 时另带
`login_state: need_relogin`）。**指标缺失时是 `null`，不是 0**，前端显示「-」。

与 `backend/app/api/promotion.py` 同形态（最薄的转发层，不含业务判断）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, datacenter
from .deps import current_user

router = APIRouter(prefix='/api/datacenter', tags=['datacenter'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/overview')
def overview(
    account_id: str,
    start: str = Query('', description='YYYYMMDD'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    return _envelope(datacenter.overview(account_id, start, end))


@router.get('/{account_id}/fans')
def fans(
    account_id: str,
    start: str = Query('', description='YYYYMMDD'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    return _envelope(datacenter.fans(account_id, start, end))


@router.get('/{account_id}/customer-service')
def customer_service(
    account_id: str,
    start: str = Query('', description='YYYYMMDD'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    return _envelope(datacenter.customer_service(account_id, start, end))
