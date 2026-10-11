# -*- coding: utf-8 -*-
"""交易组**写**路由（发货 / 改价 / 退款处理 / 评价 / 投诉处理）。

八个 POST，全部是写操作：

- 每个 body 都带 `confirm`（默认 false）→ 未确认时服务层直接返回 `confirmation_required`（400），
  **一个平台请求都不发**；
- body 用 `Dict[str, Any] = Body(default={})` 收，字段校验交给服务层出统一信封
  （用 FastAPI 的必填字段/`ge` 校验会先返回 422 的裸 body）；
- 写接口的 mtop 名与参数形状来自官方 bundle 静态反解，**未经真机验证**（见
  `backend/app/xianyu/trade_write.py` 与 `docs/contracts/trade_write.md`）。

本模块自身不发起任何网络请求：真实调用只发生在服务层确认之后。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..services import common, trade_write
from .deps import current_user

router = APIRouter(prefix='/api/trade', tags=['trade'])


def _envelope(result):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.post('/{account_id}/orders/ship')
def ship_offline(
    account_id: str,
    body: Dict[str, Any] = Body(default={}, description='{trade_ids, mail_no, cp_code, address_id, confirm}'),
    user=Depends(current_user),
):
    """订单发货（自己联系物流）：`mtop.taobao.idle.logistics.merchant.consign.offline`。"""
    return _envelope(trade_write.ship_offline(
        account_id,
        body.get('trade_ids'),
        body.get('mail_no'),
        body.get('cp_code'),
        body.get('address_id'),
        confirm=bool(body.get('confirm')),
    ))


@router.post('/{account_id}/orders/{order_id}/ship-dummy')
def ship_dummy(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(default={}, description='{trade_text, pic_urls, confirm}'),
    user=Depends(current_user),
):
    """无需物流发货（虚拟发货）：`mtop.taobao.idle.logistics.merchant.consign.dummy`。"""
    return _envelope(trade_write.ship_dummy(
        account_id,
        order_id,
        body.get('trade_text', ''),
        body.get('pic_urls') or (),
        confirm=bool(body.get('confirm')),
    ))


@router.post('/{account_id}/orders/{order_id}/adjust-price')
def adjust_price(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(default={}, description='{modify_fee, new_transport_fee, confirm}（单位：元）'),
    user=Depends(current_user),
):
    """订单改价：`mtop.taobao.idle.trade.merchant.user.adjust.price`（平台口径是分，服务层收元）。"""
    return _envelope(trade_write.adjust_price(
        account_id,
        order_id,
        body.get('modify_fee'),
        body.get('new_transport_fee'),
        confirm=bool(body.get('confirm')),
    ))


@router.get('/{account_id}/refunds/{refund_id}/verify-token')
def refund_verify_token(
    account_id: str,
    refund_id: str,
    user=Depends(current_user),
):
    """退款凭证（**只读**）：`mtop.idle.alipay.verify.url.query` → `{verify_url, token}`。

    为什么是 GET：它只查不改（不改变任何平台状态、不动钱），所以**没有 `confirm` 闸门** ——
    confirm 只钉有副作用的那一步。拿到的 `token` 是「同意退款」要回传的 `auth_token`，
    `verify_url` 是官方让用户完成支付宝身份验证的页面地址。
    """
    return _envelope(trade_write.refund_verify_token(account_id, refund_id))


@router.post('/{account_id}/refunds/{refund_id}/agree')
def refund_agree(
    account_id: str,
    refund_id: str,
    body: Dict[str, Any] = Body(default={}, description='{auth_token, confirm}'),
    user=Depends(current_user),
):
    """同意退款：`mtop.taobao.idle.merchant.refund.agree.refund`（`auth_token` 必须原样回传）。"""
    return _envelope(trade_write.refund_agree(
        account_id,
        refund_id,
        body.get('auth_token'),
        confirm=bool(body.get('confirm')),
    ))


@router.post('/{account_id}/refunds/{refund_id}/refuse')
def refund_refuse(
    account_id: str,
    refund_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{order_id, refuse_reason_id, proof_desc, proof_urls, negotiation, confirm}',
    ),
    user=Depends(current_user),
):
    """拒绝退款：`mtop.taobao.idle.merchant.refund.refuse`。"""
    return _envelope(trade_write.refund_refuse(
        account_id,
        refund_id,
        body.get('order_id'),
        body.get('refuse_reason_id'),
        body.get('proof_desc', ''),
        body.get('proof_urls') or (),
        body.get('negotiation'),
        confirm=bool(body.get('confirm')),
    ))


@router.post('/{account_id}/evaluations')
def rate_create(
    account_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{trade_ids, feedback, rate, image_urls, anonymous, confirm}',
    ),
    user=Depends(current_user),
):
    """卖家评价：`mtop.taobao.idle.merchant.rate.create`（`rate` 不需要等级时传 -1）。"""
    return _envelope(trade_write.rate_create(
        account_id,
        body.get('trade_ids'),
        body.get('feedback'),
        body.get('rate', -1),
        body.get('image_urls') or (),
        bool(body.get('anonymous')),
        confirm=bool(body.get('confirm')),
    ))


@router.post('/{account_id}/complaints/{complain_id}/refuse')
def complain_refuse(
    account_id: str,
    complain_id: str,
    body: Dict[str, Any] = Body(default={}, description='{proof_desc, proof_urls, confirm}'),
    user=Depends(current_user),
):
    """投诉拒绝：`mtop.taobao.idle.cco.shop.complain.refuse`。"""
    return _envelope(trade_write.complain_refuse(
        account_id,
        complain_id,
        body.get('proof_desc', ''),
        body.get('proof_urls') or (),
        confirm=bool(body.get('confirm')),
    ))


@router.post('/{account_id}/complaints/{complain_id}/proof')
def complain_proof(
    account_id: str,
    complain_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{proof_desc, proof_urls, kind, confirm}，kind 见协议层 COMPLAIN_PROOF_APIS',
    ),
    user=Depends(current_user),
):
    """投诉举证：`complain.submit.proof` / `.active.proof` / `.passive.proof` 三选一。"""
    return _envelope(trade_write.complain_proof(
        account_id,
        complain_id,
        body.get('proof_desc', ''),
        body.get('proof_urls') or (),
        body.get('kind') or 'sellerSubmitProof',
        confirm=bool(body.get('confirm')),
    ))
