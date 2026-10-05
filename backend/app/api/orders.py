"""订单路由（薄层）。

- 任务卡：`GET /api/orders`、`GET /api/orders/{id}`、`POST /api/orders/sync`、
  `PUT /api/orders/{id}/shipment`、`GET /api/orders/stats`
- CONTRACTS §3：`GET /api/orders`、`POST /api/orders/sync`、`POST /api/orders/{id}/deliver`、
  `GET /api/orders/{id}/logistics`
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, orders
from .deps import current_user

router = APIRouter(prefix='/api/orders', tags=['orders'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


@router.get('')
@router.get('/')
def list_orders(
    account_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    limit: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    user=Depends(current_user),
):
    if limit:
        size = max(1, min(int(limit), 500))
        if offset:
            page = int(offset) // size + 1
    return _envelope(orders.list_orders(account_id=account_id, status=status, page=page, size=size, keyword=q))


@router.get('/stats')
def order_stats(account_id: Optional[str] = Query(default=None), user=Depends(current_user)):
    return _envelope(orders.order_stats(account_id=account_id))


@router.post('/sync')
def sync_orders(payload: Dict[str, Any] = Body(default={}), user=Depends(current_user)):
    account_id = str((payload or {}).get('account_id') or '')
    return _envelope(orders.sync_orders(account_id))


@router.get('/{order_id}/logistics')
def get_logistics(order_id: str, user=Depends(current_user)):
    return _envelope(orders.get_logistics(order_id))


@router.put('/{order_id}/shipment')
def update_shipment(order_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """body: {company, tracking_no}（也认 courier / tracking_number）"""
    company = str(payload.get('company') or payload.get('courier') or payload.get('express') or '')
    tracking_no = str(payload.get('tracking_no') or payload.get('tracking_number') or payload.get('waybill') or '')
    return _envelope(orders.update_shipment(order_id, company, tracking_no))


@router.post('/{order_id}/deliver')
def deliver(order_id: str, user=Depends(current_user)):
    """CONTRACTS §3 的标记发货（不带物流单号时用）。"""
    return _envelope(orders.deliver(order_id))


@router.get('/{order_id}')
def get_order(order_id: str, user=Depends(current_user)):
    return _envelope(orders.get_order(order_id))
