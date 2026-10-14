# -*- coding: utf-8 -*-
"""交易组只读路由（订单 / 退款 / 评价 / 投诉 / 退货地址）。

五个 GET，全部只读；分页参数以字符串收，交给服务层校验成 `invalid_fields` 信封
（用 FastAPI 的 `ge/le` 会先返回 422 的裸 body，破坏统一信封）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, trade
from .deps import current_user

router = APIRouter(prefix='/api/trade', tags=['trade'])


def _envelope(result):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/orders')
def orders(
    account_id: str,
    page: str = Query('1', description='页码，从 1 开始'),
    size: str = Query('10', description='每页条数 1~50'),
    include_counts: bool = Query(False, description='额外调一次 order.count 取页签计数'),
    user=Depends(current_user),
):
    """订单列表（真机 `mtop.taobao.idle.trade.merchant.sold.get`）。"""
    return _envelope(trade.orders(account_id, page, size, include_counts))


@router.get('/{account_id}/refunds')
def refunds(
    account_id: str,
    page: str = Query('1', description='页码，从 1 开始'),
    size: str = Query('10', description='每页条数 1~50'),
    user=Depends(current_user),
):
    """退款列表（真机 `mtop.taobao.idle.merchant.refund.list`，响应容器 `data.data`）。"""
    return _envelope(trade.refunds(account_id, page, size))


@router.get('/{account_id}/evaluations')
def evaluations(
    account_id: str,
    page: str = Query('1', description='页码，从 1 开始'),
    size: str = Query('10', description='每页条数 1~50'),
    user=Depends(current_user),
):
    """评价列表（真机 `mtop.taobao.idle.merchant.rate.list`，评价在 `rateItemVOList`）。"""
    return _envelope(trade.evaluations(account_id, page, size))


@router.get('/{account_id}/complaints')
def complaints(
    account_id: str,
    page: str = Query('1', description='页码，从 1 开始'),
    size: str = Query('10', description='每页条数 1~50'),
    user=Depends(current_user),
):
    """投诉列表。真机 `NO_PERMISSION::用户没有权限`，会返回 platform_error 信封。"""
    return _envelope(trade.complaints(account_id, page, size))


@router.get('/{account_id}/addresses')
def addresses(account_id: str, user=Depends(current_user)):
    """退货地址列表。真机 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，会返回 platform_error 信封。"""
    return _envelope(trade.addresses(account_id))


@router.get('/{account_id}/refunds/{refund_id}/refuse-reasons')
def refund_refuse_reasons(
    account_id: str,
    refund_id: str,
    order_id: str = Query('', description='订单号（可选，只用于回显）'),
    refuse_reason_id: str = Query('', description='回填选中的原因 id（可选）'),
    user=Depends(current_user),
):
    """拒绝退款的原因列表（`mtop.taobao.idle.merchant.refund.refuse.render`）。

    官方把 `reasonId == 20653007` 那条置灰（只能在手机 App 操作）⇒ 响应里标 `app_only`。
    """
    return _envelope(trade.refund_refuse_reasons(account_id, refund_id, order_id, refuse_reason_id))


@router.get('/{account_id}/refunds/{refund_id}/detail')
def refund_detail(
    account_id: str,
    refund_id: str,
    order_id: str = Query('', description='订单号（可选）'),
    user=Depends(current_user),
):
    """退款详情（`mtop.taobao.idle.merchant.refund.detail`）：原样透传 `components[]`。"""
    return _envelope(trade.refund_detail(account_id, refund_id, order_id))


@router.get('/{account_id}/orders/{order_id}/postage-refund/detail')
def postage_refund_detail(
    account_id: str,
    order_id: str,
    refund_id: str = Query('', description='退款单号（可选）'),
    refund_postage_type: str = Query('', description='邮费退款类型（官方从 URL 上带过来）'),
    user=Depends(current_user),
):
    """邮费退款详情（`mtop.taobao.idle.merchant.postage.refund.detail.query`）。"""
    return _envelope(trade.postage_refund_detail(account_id, order_id, refund_id, refund_postage_type))


@router.get('/{account_id}/orders/{order_id}/postage-refund/refuse-reasons')
def postage_refund_refuse_reasons(
    account_id: str,
    order_id: str,
    refund_id: str = Query('', description='退款单号（可选）'),
    refund_postage_type: str = Query('', description='邮费退款类型（可选）'),
    default_reason: str = Query('', description='官方会把 URL 上的 defaultReason 带过来'),
    user=Depends(current_user),
):
    """邮费退款拒绝原因（`mtop.taobao.idle.merchant.postage.refund.refuse.reason.query`）。"""
    return _envelope(trade.postage_refund_refuse_reasons(
        account_id, order_id, refund_id, refund_postage_type, default_reason))
