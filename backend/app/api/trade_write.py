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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
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
        confirm=body.get('confirm'),
    ))


# --------------------------------------------------------------------------- 退货地址（写）
# 官方卖家工作台「小铺 → 退货地址」页的四个写接口（证据见 `xianyu/address_write.py`）。
# 注意路径顺序：这一族用 `addresses` 段，与上面 `orders` / `refunds` 段不重叠。


@router.post('/{account_id}/addresses')
def address_add(
    account_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{contact_name, mobile_phone, province_name, city_name, district_name,'
                    ' detail_address, area_id?, default_addr?, confirm}',
    ),
    user=Depends(current_user),
):
    """新增退货地址：`mtop.alibaba.idle.seller.platform.merchant.delivery.address.add`。"""
    return _envelope(trade_write.address_add(
        account_id,
        body.get('contact_name'),
        body.get('mobile_phone'),
        body.get('province_name'),
        body.get('city_name'),
        body.get('district_name'),
        body.get('detail_address'),
        body.get('area_id'),
        body.get('default_addr'),
        # 原样交给服务层判（`_truthy`：只有 True / 'true' / 1 算点头）——
        # 这里绝不能写成 `bool(...)`，否则 `"confirm": "false"` 会被当成同意。
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/addresses/{contact_id}/update')
def address_update(
    account_id: str,
    contact_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{contact_name, mobile_phone, province_name, city_name, district_name,'
                    ' detail_address, area_id?, default_addr?, confirm}',
    ),
    user=Depends(current_user),
):
    """修改退货地址：`…delivery.address.update`。"""
    return _envelope(trade_write.address_update(
        account_id,
        contact_id,
        body.get('contact_name'),
        body.get('mobile_phone'),
        body.get('province_name'),
        body.get('city_name'),
        body.get('district_name'),
        body.get('detail_address'),
        body.get('area_id'),
        body.get('default_addr'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/addresses/{contact_id}/delete')
def address_delete(
    account_id: str,
    contact_id: str,
    body: Dict[str, Any] = Body(default={}, description='{confirm}'),
    user=Depends(current_user),
):
    """删除退货地址：`…delivery.address.delete`（不可撤销）。"""
    return _envelope(trade_write.address_delete(
        account_id, contact_id, confirm=body.get('confirm')))


@router.post('/{account_id}/addresses/{contact_id}/default')
def address_set_default(
    account_id: str,
    contact_id: str,
    body: Dict[str, Any] = Body(default={}, description='{confirm}'),
    user=Depends(current_user),
):
    """设为默认退货地址：`…delivery.address.set.default`。"""
    return _envelope(trade_write.address_set_default(
        account_id, contact_id, confirm=body.get('confirm')))


@router.get('/{account_id}/orders/{order_id}/modify-address')
def order_modify_address(
    account_id: str,
    order_id: str,
    user=Depends(current_user),
):
    """买家改地址申请（**只读**）：`mtop.idle.merchant.order.get.modify.address.info`。

    为什么是 GET：只查不改，所以**没有 `confirm` 闸门**。返回里 `can_operate` 是服务层
    算好的（`status == 'WAITING'` 且平台给了 `deliver_id`），同意/拒绝都要先看它。
    隐私号订单会额外调一次 `mtop.idle.trade.order.detail.privacy.address` 换明文，
    解不开就是 `decrypted: null` + 一条 warning，**不编明文**。
    """
    return _envelope(trade_write.order_address_info(account_id, order_id))


@router.post('/{account_id}/orders/{order_id}/modify-address/agree')
def order_modify_address_agree(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(default={}, description='{deliver_id, confirm}'),
    user=Depends(current_user),
):
    """同意买家改地址：`…merchant.order.address.modify.agree`。**会真的改订单收货地址**。"""
    return _envelope(trade_write.order_address_agree(
        account_id, order_id, body.get('deliver_id'), confirm=body.get('confirm')))


@router.post('/{account_id}/orders/{order_id}/modify-address/refuse')
def order_modify_address_refuse(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(default={}, description='{deliver_id, confirm}'),
    user=Depends(current_user),
):
    """拒绝买家改地址：`…merchant.order.address.modify.refuse`。"""
    return _envelope(trade_write.order_address_refuse(
        account_id, order_id, body.get('deliver_id'), confirm=body.get('confirm')))


# --------------------------------------------------------------------------- 关单 / 延时 / 提醒（写）
# 第三十八批：官方订单页的另外几个动作。证据见 `xianyu/trade_write.py` 顶部常量注释。
# `confirm` 一律**原样**交服务层判 —— 路由层不许写 `bool(...)`，因为 `bool('false')` 是 True，
# 那会把写闸门强制成「已确认」（第三十八批就是这么抓到 8 处 + `security_write.py` 3 处的）。


@router.get('/{account_id}/orders/{order_id}/close-reasons')
def order_close_reasons(
    account_id: str,
    order_id: str,
    user=Depends(current_user),
):
    """关单原因候选（**只读**，没有 `confirm`）：`…trade.order.close.reason.get`。

    官方关单弹窗先调它，把 `data.closeReasons`（字符串数组）直接当选项。平台没给列表键时
    `close_reasons` 是 `null` + 一条中文 warning，**不替你编原因**。
    """
    return _envelope(trade_write.close_reasons(account_id, order_id))


@router.post('/{account_id}/orders/{order_id}/close')
def order_close(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(default={}, description='{close_reason, confirm}'),
    user=Depends(current_user),
):
    """关闭订单：`…trade.merchant.close.by.seller` v2.0。**不可撤销**。

    `close_reason` 必填（官方也是必选），候选来自上面那个只读接口。官方原话提醒：
    「确认关闭后，请确保已和买家协商达成一致，否则您将面临投诉或赔偿」。
    """
    return _envelope(trade_write.close_order(
        account_id, order_id, body.get('close_reason'), confirm=body.get('confirm')))


@router.post('/{account_id}/orders/{order_id}/delay')
def order_delay_confirm(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(
        default={}, description='{days, order_ids?, confirm}，days ∈ 3/5/7/10'),
    user=Depends(current_user),
):
    """延长收货时间：`…trade.merchant.batch.delay.confirm`（`delayTime` 平台要**秒**）。

    `order_ids` 不给就只处理路径里那一个订单；给了就是批量（收数组，也收逗号串）。
    """
    return _envelope(trade_write.delay_confirm(
        account_id, body.get('order_ids') or order_id, body.get('days'),
        confirm=body.get('confirm')))


@router.post('/{account_id}/orders/{order_id}/remind')
def order_remind_confirm(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(
        default={}, description='{order_ids?, remind_all?, confirm}'),
    user=Depends(current_user),
):
    """提醒买家确认收货：`…trade.merchant.batch.remind.confirm`。"""
    return _envelope(trade_write.remind_confirm(
        account_id, body.get('order_ids') or order_id, body.get('remind_all', False),
        confirm=body.get('confirm')))


@router.post('/{account_id}/refunds/{refund_id}/postage-pay')
def refund_postage_pay(
    account_id: str,
    refund_id: str,
    body: Dict[str, Any] = Body(
        default={}, description='{order_id, pay_type?, amount?, confirm}'),
    user=Depends(current_user),
):
    """支付运费退款：`mtop.taobao.idle.merchant.postage.refund.pay` —— **真的付钱**。

    `pay_type` 取 `alipay`（默认）/ `deposit`；`amount` 不参与请求，只进审计与返回值，
    方便事后对账（调用方应当先把它念给用户看再确认）。
    """
    return _envelope(trade_write.postage_refund_pay(
        account_id, body.get('order_id'), refund_id, body.get('pay_type') or 'alipay',
        body.get('amount'), confirm=body.get('confirm')))


@router.post('/{account_id}/complaints/{complain_id}/revoke')
def complain_revoke(
    account_id: str,
    complain_id: str,
    body: Dict[str, Any] = Body(default={}, description='{confirm}'),
    user=Depends(current_user),
):
    """撤销投诉申请：`mtop.taobao.idle.cco.shop.complain.apply.revoke`。"""
    return _envelope(trade_write.complain_revoke(
        account_id, complain_id, confirm=body.get('confirm')))


# --------------------------------------------------------------------------- 赔付 / 维权（第四十五批）
# 平台侧三条（证据与偏移见 `backend/app/xianyu/compensate.py` 顶部注释）：
#   · `…compensate.service.detail.query` v1.0 —— 只读，且官方带 `valueType:'string'`
#   · `…compensate.service.refuse.render` v1.0 —— 只读，给拒绝原因候选
#   · `…compensate.service.refuse` v1.0 —— 写，**凭证必填**、**不可撤销**
# 同族的 `…compensate.service.pay`（真付钱，要支付宝 authToken）**故意不做**。


@router.get('/{account_id}/orders/{order_id}/compensations/{service_code}')
def compensate_detail(
    account_id: str,
    order_id: str,
    service_code: str,
    user=Depends(current_user),
):
    """赔付详情（**只读**，没有 `confirm`）：`…compensate.service.detail.query`。

    平台没给 `module` 时 `found=false` + 一条中文 warning，金额等字段一律 `null`，
    **不替你编一笔赔付出来**。
    """
    return _envelope(trade_write.compensate_detail(account_id, order_id, service_code))


@router.get('/{account_id}/compensations/{service_code}/reasons')
def compensate_reasons(
    account_id: str,
    service_code: str,
    user=Depends(current_user),
):
    """拒绝赔付的原因候选（**只读**）：`…compensate.service.refuse.render`。

    平台没给 `reasonChoices` 时是 `null` + warning（**不是空数组**）。
    """
    return _envelope(trade_write.compensate_refuse_form(account_id, service_code))


@router.post('/{account_id}/orders/{order_id}/compensations/{service_code}/refuse')
def compensate_refuse(
    account_id: str,
    order_id: str,
    service_code: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{reason_choices, reason_desc?, proof_urls, is_manual?, confirm}'),
    user=Depends(current_user),
):
    """拒绝赔付：`…compensate.service.refuse` —— **不可撤销，且凭证必填**。

    官方原话：不传凭证时提示「请上传凭证」；拒绝说明最多 200 字。
    `reason_choices` 收数组也收逗号串，`proof_urls` 至少一张图片 URL。
    """
    return _envelope(trade_write.compensate_refuse(
        account_id, order_id, service_code, body.get('reason_choices'),
        body.get('reason_desc') or '', body.get('proof_urls'),
        body.get('is_manual', False), confirm=body.get('confirm')))


@router.post('/{account_id}/orders/{order_id}/memo')
def add_memo(
    account_id: str,
    order_id: str,
    body: Dict[str, Any] = Body(default={}, description='{memo, memo_group?, confirm}'),
    user=Depends(current_user),
):
    """订单备注：`mtop.taobao.idle.merchant.add.memo`。

    **这条备注不对买家展示**（官方 placeholder 原话），只是卖家自己的标记；
    `memo_group` 是 1~5 的整数（官方五张旗标图，没有文字名）。
    校验提示逐字照抄官方（非空 / 换行 / emoji / 白名单），上限 50 字。
    """
    return _envelope(trade_write.add_memo(
        account_id, order_id, body.get('memo'), body.get('memo_group', 1),
        confirm=body.get('confirm')))
