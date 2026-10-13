# -*- coding: utf-8 -*-
"""交易写接口（发货 / 改价 / 退款处理 / 评价 / 投诉处理）—— 协议层。

**接口来源（静态 bundle 反解，零风控）**：官方交易微应用
`https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3879584 B）。
下面每个 `api` / `v` / `data` 键名都是从这份 bundle 里逐条抄下来的，常量后注明
**字节偏移**（本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\trade_bundle\\main.js`，
`mtop_names.json` 是全部接口名清单，可用偏移复核原文）。

**全部未经真机验证**：写接口禁止对真实平台试打，参数形状只保证「与官方前端发出的完全一致」。

**确认闸门**：每个请求构造器都要 `confirm=True`，默认 False 时抛 `WriteNotConfirmed` ——
协议层没有任何默认执行的写入口。服务层再把它翻成 `confirmation_required` 失败信封。

**金额口径**：改价接口的单位是**分**（bundle 原文 `Math.round(100*价格).toString()`），
`yuan_to_fen()` 是全项目唯一一处元→分换算。

**与只读层的关系**：`backend/app/xianyu/trade.py` 的 `assert_read_only()` 只拦它自己那批
查询接口；本模块是唯一允许调用 `trade.WRITE_APIS` 的地方，且只在确认后构造请求。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .trade import body_of, decode_payload, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 订单发货（自己联系物流，填运单号）。bundle 偏移 2624879：
#: `{api:"...consign.offline",v:"1.0",data:{orderInfos:e,mailNo:t,cpCode:n,addressId:r}}`
SHIP_OFFLINE_API = 'mtop.taobao.idle.logistics.merchant.consign.offline'
SHIP_OFFLINE_VERSION = '1.0'

#: 无需物流发货（虚拟发货）。bundle 偏移 3085774：
#: `data:{orderId:t.orderId,tradeText:c,picList:JSON.stringify(r),newUnconsign:!0}`
SHIP_DUMMY_API = 'mtop.taobao.idle.logistics.merchant.consign.dummy'
SHIP_DUMMY_VERSION = '1.0'

#: 订单改价。bundle 偏移 2530439：
#: `data:{orderId:e,modifyFee:t,newTransportFee:n}`，两个费用都是 `Math.round(100*价格).toString()`（分）
ADJUST_PRICE_API = 'mtop.taobao.idle.trade.merchant.user.adjust.price'
ADJUST_PRICE_VERSION = '1.0'

#: 同意退款。bundle 偏移 2800533 / 2803954（两处调用点形状一致）：
#: `data:{refundId:e,authToken:t}`
REFUND_AGREE_API = 'mtop.taobao.idle.merchant.refund.agree.refund'
REFUND_AGREE_VERSION = '1.0'

#: 拒绝退款。bundle 偏移 2687681：
#: `data:{refundId,orderId,refuseReasonId,refuseProof:"{desc,proofMultiMediaList:[{mediaTypeEnum:"IMAGE",url}]}",
#: negotiationApply:"{negotiationRefundFee,idleNegotiationType}"}`（后两个是 JSON **字符串**，无内容时不传）
REFUND_REFUSE_API = 'mtop.taobao.idle.merchant.refund.refuse'
REFUND_REFUSE_VERSION = '1.0'

#: 卖家评价（bundle 里卖家侧唯一的评价写接口；没有单独的「回复评价」接口）。
#: bundle 偏移 2789045：`{api:"...rate.create",v:"1.0",data:e}`，调用点（偏移 2790105）构造的
#: `e = {tradeIdList, imageUrls, rate, feedback, anonymous}`，响应在 `data.module`。
RATE_CREATE_API = 'mtop.taobao.idle.merchant.rate.create'
RATE_CREATE_VERSION = '1.0'

#: 投诉拒绝（卖家举证反驳）。bundle 偏移 2827294：
#: `{api:"...complain.refuse",v:"1.0",needLoginPC:!0,data:{complainId,proofDesc,proofMediaList}}`，
#: `proofMediaList` 是 `JSON.stringify([{type:"image",url}])`
COMPLAIN_REFUSE_API = 'mtop.taobao.idle.cco.shop.complain.refuse'
COMPLAIN_REFUSE_VERSION = '1.0'

#: 投诉举证：同一个提交框按按钮 code 选三个接口之一。bundle 偏移 2832609 与 2834959。
COMPLAIN_PROOF_APIS: Dict[str, str] = {
    'sellerSubmitProof': 'mtop.taobao.idle.cco.shop.complain.submit.proof',
    'submitActiveProof': 'mtop.taobao.idle.cco.shop.complain.submit.active.proof',
    'submitPassiveProof': 'mtop.taobao.idle.cco.shop.complain.submit.passive.proof',
}
COMPLAIN_PROOF_VERSION = '1.0'

#: 退款凭证（支付宝身份验证的 verifyUrl + token）。bundle 偏移 **2801524**（同意退款弹窗）
#: 与 **2804850**（拒绝退款弹窗），两处参数逐字相同：
#: `{api:"mtop.idle.alipay.verify.url.query",v:"1.0",data:{bizId:refundId,scene:"REFUND_PC",
#: callBackUrl:"https://seller.goofish.com/?site=COMMONPRO#/seller-trade/refund-manage"}}`；
#: 响应读 `data.module.verifyUrl`（官方把它塞进 150px 高的 iframe）与 `data.module.token`
#: （原样传给 `refund.agree.refund` 的 `authToken`）。
#:
#: **它本身只查不写**（不改任何平台状态），所以**故意不放进 `WRITE_APIS`**、也不要 confirm；
#: 但它是「同意退款」的前置步骤，所以留在本模块。官方只在
#: `window.__PC_SELLER_WORKBENCH__.userInfo.isAdmin` 为真时走这条，非管理员态直接用空 token
#: 提交 —— 我们照实把平台的返回/报错透出来，不猜、不伪造 token。
VERIFY_URL_API = 'mtop.idle.alipay.verify.url.query'
VERIFY_URL_VERSION = '1.0'
VERIFY_SCENE_REFUND = 'REFUND_PC'
VERIFY_CALLBACK_URL = 'https://seller.goofish.com/?site=COMMONPRO#/seller-trade/refund-manage'

#: 关闭订单（卖家主动关单）。bundle 偏移 **2762506**：
#: `hO({api:"...trade.merchant.close.by.seller",v:"2.0",data:{tid:e,bizOrderId:e,closeReason:t}})`
#: —— 官方把**同一个订单号**同时塞进 `tid` 与 `bizOrderId`（`tid` 是历史字段，别自己编）。
#: 关单原因必须先选（官方提示「请选择订单取消原因~」），候选来自下面那个只读接口。
#: 官方文案：「确认关闭后，请确保已和买家协商达成一致，否则您将面临投诉或赔偿」。
CLOSE_BY_SELLER_API = 'mtop.taobao.idle.trade.merchant.close.by.seller'
CLOSE_BY_SELLER_VERSION = '2.0'

#: 关单原因候选（**只读**）。bundle 偏移 2585779：
#: `{api:"mtop.taobao.idle.trade.order.close.reason.get",v:"1.0",data:{bizOrderId}}`；
#: 消费点（偏移 2598721）读 `resp.data.closeReasons` —— **字符串数组**，官方直接
#: `map(r => ({label: r, value: r}))` 当选项，没有 code/desc 之分。
CLOSE_REASON_API = 'mtop.taobao.idle.trade.order.close.reason.get'
CLOSE_REASON_VERSION = '1.0'

#: 延长收货时间（批量）。bundle 偏移 **2758810**：
#: `hO({api:"...trade.merchant.batch.delay.confirm",v:"1.0",data:{orderIdList,delayTime}})`；
#: `delayTime` 是**秒**：官方 `60*(p||0)*60*24`，`p` 取 3/5/7/10（天）。
#: 响应读 `data.module.{totalNum,successNum,failOrderInfos[{orderId,errorMsg}]}`。
BATCH_DELAY_API = 'mtop.taobao.idle.trade.merchant.batch.delay.confirm'
BATCH_DELAY_VERSION = '1.0'
DELAY_DAYS: Tuple[int, ...] = (3, 5, 7, 10)
SECONDS_PER_DAY = 24 * 60 * 60

#: 提醒买家确认收货（批量）。bundle 偏移 **2023848**：
#: `hO({api:"...trade.merchant.batch.remind.confirm",v:"1.0",data:{orderIdList,remindAllOrder}})`。
BATCH_REMIND_API = 'mtop.taobao.idle.trade.merchant.batch.remind.confirm'
BATCH_REMIND_VERSION = '1.0'

#: 支付运费退款（卖家承担运费）。bundle 偏移 **2835556**：
#: `hO({api:"mtop.taobao.idle.merchant.postage.refund.pay",v:"1.0",
#:     ttid:"1@tbwang_mac_1.0.0#pc",data:{orderId,refundId,payType},needLoginPC:!0})`；
#: `payType` 官方默认 `'alipay'`（另一个取值是 `'deposit'`，界面文案走保证金）。
#: **这是真的付钱**，所以除了 confirm 之外还要求调用方把金额念清楚（见服务层）。
POSTAGE_PAY_API = 'mtop.taobao.idle.merchant.postage.refund.pay'
POSTAGE_PAY_VERSION = '1.0'
PAY_TYPES: Tuple[str, ...] = ('alipay', 'deposit')
PC_TTID = '1@tbwang_mac_1.0.0#pc'

#: 撤销投诉申请。bundle 偏移 **2814089**：
#: `hO({api:"mtop.taobao.idle.cco.shop.complain.apply.revoke",v:"1.0",
#:     data:{complainId},needLoginPC:!0})`；响应读 `data.success` + `data.failReason`
#: （与投诉拒绝同族，用 `complain_failure()` 判）。
COMPLAIN_REVOKE_API = 'mtop.taobao.idle.cco.shop.complain.apply.revoke'
COMPLAIN_REVOKE_VERSION = '1.0'

#: 批量类接口的单次订单数上限（官方界面按页勾选，一页最多 10~20；这里给足余量）。
MAX_ORDER_IDS = 50


#: 本模块声明的全部写接口（用于自检与文档；`trade.py` 的黑名单是它的超集）。
WRITE_APIS: Tuple[str, ...] = (
    SHIP_OFFLINE_API,
    SHIP_DUMMY_API,
    ADJUST_PRICE_API,
    REFUND_AGREE_API,
    REFUND_REFUSE_API,
    RATE_CREATE_API,
    COMPLAIN_REFUSE_API,
    CLOSE_BY_SELLER_API,
    BATCH_DELAY_API,
    BATCH_REMIND_API,
    POSTAGE_PAY_API,
    COMPLAIN_REVOKE_API,
) + tuple(COMPLAIN_PROOF_APIS.values())

#: 本模块里**只读**的接口（查退款凭证、查关单原因）。单独列出来，免得被误当成写接口
#: 加进黑名单，也免得有人给它加 confirm 闸门 —— 它们不改变任何平台状态。
READ_APIS: Tuple[str, ...] = (VERIFY_URL_API, CLOSE_REASON_API)


class WriteNotConfirmed(RuntimeError):
    """写接口没有显式确认。"""


def require_confirm(confirm: bool, action: str) -> None:
    """写操作闸门：未确认直接抛，绝不默认执行。"""
    if not confirm:
        raise WriteNotConfirmed(f'{action} 是写操作，需要显式确认（confirm=True）后才会提交')


# --------------------------------------------------------------------------- 取值工具


def _text(value: Any) -> str:
    """空值一律 ''，数字转字符串（订单号/退款号真机给 int，必须保精度）。"""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return ''


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _or_none(value: Any) -> Optional[Any]:
    """平台没给就是 None，**不用 0/'' 顶替**。"""
    if value is None or value == '' or value == [] or value == {}:
        return None
    return value


def _list_of(value: Any) -> List[Any]:
    """把 list/tuple/JSON 字符串统一成 list（空/认不出 → []）。"""
    if value is None or value == '':
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None and item != '']
    return [value]


def yuan_to_fen(amount: Any) -> str:
    """元 → 分（字符串）。平台口径：`Math.round(100*价格).toString()`。

    这是全项目唯一一处金额换算；服务层一律收「元」，换算只发生在这里。
    """
    if amount is None or amount == '':
        raise ValueError('金额不能为空')
    return str(int(round(float(amount) * 100)))


# --------------------------------------------------------------------------- 请求构造


def ship_offline_request(trade_ids: Sequence[Any], mail_no: Any, cp_code: Any, address_id: Any,
                         *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """发货（自己联系物流）。`orderInfos` 是 JSON **字符串**：`[{"tradeId": "..."}]`。"""
    require_confirm(confirm, '订单发货')
    order_infos = [{'tradeId': _text(trade_id)} for trade_id in _list_of(trade_ids)]
    return SHIP_OFFLINE_API, {
        'orderInfos': json.dumps(order_infos, ensure_ascii=False),
        'mailNo': _text(mail_no),
        'cpCode': _text(cp_code),
        'addressId': _int_or_none(address_id),
    }


def ship_dummy_request(order_id: Any, trade_text: Any = '', pic_urls: Any = (),
                       *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """无需物流发货。`picList` 是 JSON 字符串，`newUnconsign` 官方固定 true。"""
    require_confirm(confirm, '无需物流发货')
    return SHIP_DUMMY_API, {
        'orderId': _text(order_id),
        'tradeText': _text(trade_text),
        'picList': json.dumps([_text(url) for url in _list_of(pic_urls)], ensure_ascii=False),
        'newUnconsign': True,
    }


def adjust_price_request(order_id: Any, modify_fee: Any, new_transport_fee: Any,
                         *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """订单改价。入参是**元**，出参按平台口径换算成**分**的字符串。"""
    require_confirm(confirm, '订单改价')
    return ADJUST_PRICE_API, {
        'orderId': _text(order_id),
        'modifyFee': yuan_to_fen(modify_fee),
        'newTransportFee': yuan_to_fen(new_transport_fee),
    }


def refund_agree_request(refund_id: Any, auth_token: Any,
                         *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """同意退款。`authToken` 由平台在退款详情/渲染接口里下发，必须原样回传。"""
    require_confirm(confirm, '同意退款')
    return REFUND_AGREE_API, {
        'refundId': _text(refund_id),
        'authToken': _text(auth_token),
    }


def refund_refuse_request(refund_id: Any, order_id: Any, refuse_reason_id: Any,
                          proof_desc: Any = '', proof_urls: Any = (),
                          negotiation: Any = None,
                          *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """拒绝退款。

    `refuseReasonId` 来自 `refund.refuse.render` 下发的 `refuseReasonList[].refuseReasonId`；
    `refuseProof` / `negotiationApply` 是 JSON 字符串，**没内容就不传**（官方也是 `void 0`）。
    """
    require_confirm(confirm, '拒绝退款')
    data: Dict[str, Any] = {
        'refundId': _text(refund_id),
        'orderId': _text(order_id),
        'refuseReasonId': _text(refuse_reason_id),
    }
    urls = [_text(url) for url in _list_of(proof_urls)]
    if _text(proof_desc) or urls:
        data['refuseProof'] = json.dumps({
            'desc': _text(proof_desc),
            'proofMultiMediaList': [{'mediaTypeEnum': 'IMAGE', 'url': url} for url in urls],
        }, ensure_ascii=False)
    if isinstance(negotiation, dict) and negotiation:
        data['negotiationApply'] = json.dumps(negotiation, ensure_ascii=False)
    return REFUND_REFUSE_API, data


def verify_url_request(refund_id: Any) -> Tuple[str, Dict[str, Any]]:
    """退款凭证（**只读**）：拿 `verifyUrl` + `token`。

    刻意**没有 `confirm` 参数** —— 它只查不改，给只读接口加确认闸门只会稀释真正危险
    操作的信号（判据：confirm 只钉有副作用的那一步）。服务层同理不做确认检查。
    """
    return VERIFY_URL_API, {
        'bizId': _text(refund_id),
        'scene': VERIFY_SCENE_REFUND,
        'callBackUrl': VERIFY_CALLBACK_URL,
    }


def rate_create_request(trade_ids: Any, feedback: Any, rate: Any = -1, image_urls: Any = (),
                        anonymous: bool = False,
                        *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """卖家评价。`rate` 是评价等级（不需要等级时官方传 -1），`feedback` 是评价内容。"""
    require_confirm(confirm, '评价')
    return RATE_CREATE_API, {
        'tradeIdList': [_text(trade_id) for trade_id in _list_of(trade_ids)],
        'imageUrls': [_text(url) for url in _list_of(image_urls)],
        'rate': _int_or_none(rate) if _int_or_none(rate) is not None else -1,
        'feedback': _text(feedback),
        'anonymous': bool(anonymous),
    }


def _complain_data(complain_id: Any, proof_desc: Any, proof_urls: Any) -> Dict[str, Any]:
    urls = [_text(url) for url in _list_of(proof_urls)]
    return {
        'complainId': _text(complain_id),
        'proofDesc': _text(proof_desc),
        'proofMediaList': json.dumps([{'type': 'image', 'url': url} for url in urls],
                                     ensure_ascii=False),
    }


def complain_refuse_request(complain_id: Any, proof_desc: Any, proof_urls: Any,
                            *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """投诉拒绝（举证反驳）。"""
    require_confirm(confirm, '投诉拒绝')
    return COMPLAIN_REFUSE_API, _complain_data(complain_id, proof_desc, proof_urls)


def complain_proof_request(complain_id: Any, proof_desc: Any, proof_urls: Any,
                           kind: str = 'sellerSubmitProof',
                           *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """投诉举证。`kind` 取 `sellerSubmitProof` / `submitActiveProof` / `submitPassiveProof`。"""
    if kind not in COMPLAIN_PROOF_APIS:
        raise ValueError(f'未知的投诉举证类型: {kind}')
    require_confirm(confirm, '投诉举证')
    return COMPLAIN_PROOF_APIS[kind], _complain_data(complain_id, proof_desc, proof_urls)


def order_id_list(value: Any) -> List[str]:
    """把「订单号列表」统一成字符串列表（收数组，也收逗号/中文逗号串）。

    UI 传数组、MCP/AI 传逗号串，两条路都必须能用；去重保序、上限 `MAX_ORDER_IDS`。
    """
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace('，', ',').split(',')]
        items = [part for part in parts if part]
    else:
        items = [_text(item) for item in _list_of(value)]
    seen: List[str] = []
    for item in items:
        text = _text(item)
        if text and text not in seen:
            seen.append(text)
    if not seen:
        raise ValueError('订单号不能为空')
    if len(seen) > MAX_ORDER_IDS:
        raise ValueError(f'一次最多处理 {MAX_ORDER_IDS} 个订单')
    return seen


def close_order_request(order_id: Any, close_reason: Any,
                        *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """关闭订单。`tid` 与 `bizOrderId` 都是同一个订单号（官方就是这么发的）。"""
    require_confirm(confirm, '关闭订单')
    order = _text(order_id)
    if not order:
        raise ValueError('订单号不能为空')
    reason = _text(close_reason).strip()
    if not reason:
        raise ValueError('关闭原因不能为空（候选见 close_reason_request）')
    return CLOSE_BY_SELLER_API, {'tid': order, 'bizOrderId': order, 'closeReason': reason}


def close_reason_request(order_id: Any) -> Tuple[str, Dict[str, Any]]:
    """关单原因候选（只读，不要 confirm）。"""
    order = _text(order_id)
    if not order:
        raise ValueError('订单号不能为空')
    return CLOSE_REASON_API, {'bizOrderId': order}


def delay_confirm_request(order_ids: Any, days: Any,
                          *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """延长收货时间。`days` 只认官方那四档（3/5/7/10），`delayTime` 换算成秒。"""
    require_confirm(confirm, '延长收货时间')
    ids = order_id_list(order_ids)
    value = _int_or_none(days)
    if value not in DELAY_DAYS:
        raise ValueError('延长天数只能是 %s 之一' % '/'.join(str(day) for day in DELAY_DAYS))
    return BATCH_DELAY_API, {'orderIdList': ids, 'delayTime': value * SECONDS_PER_DAY}


def remind_confirm_request(order_ids: Any, remind_all: Any = False,
                           *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """提醒买家确认收货。`remindAllOrder` 官方是布尔。"""
    require_confirm(confirm, '提醒确认收货')
    ids = order_id_list(order_ids)
    flag = remind_all is True or _text(remind_all).lower() in ('true', '1')
    return BATCH_REMIND_API, {'orderIdList': ids, 'remindAllOrder': flag}


def postage_refund_pay_request(order_id: Any, refund_id: Any, pay_type: Any = 'alipay',
                               *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """支付运费退款（**真的付钱**）。`payType` 只认 `alipay` / `deposit`。"""
    require_confirm(confirm, '支付运费退款')
    order = _text(order_id)
    refund = _text(refund_id)
    if not order:
        raise ValueError('订单号不能为空')
    if not refund:
        raise ValueError('退款单号不能为空')
    kind = _text(pay_type).strip() or 'alipay'
    if kind not in PAY_TYPES:
        raise ValueError('付款方式只能是 %s 之一' % '/'.join(PAY_TYPES))
    return POSTAGE_PAY_API, {'orderId': order, 'refundId': refund, 'payType': kind}


def complain_revoke_request(complain_id: Any,
                            *, confirm: bool = False) -> Tuple[str, Dict[str, Any]]:
    """撤销投诉申请。"""
    require_confirm(confirm, '撤销投诉申请')
    complain = _text(complain_id)
    if not complain:
        raise ValueError('投诉单号不能为空')
    return COMPLAIN_REVOKE_API, {'complainId': complain}


# --------------------------------------------------------------------------- 响应解析

def parse_write_result(raw: Any) -> Dict[str, Any]:
    """写接口响应的公共部分：只透出平台**确实给了**的东西，缺就是 None。"""
    raw = decode_payload(raw)
    success = raw.get('success') if isinstance(raw, dict) else None
    return {
        'platform_success': success if isinstance(success, bool) else None,
        'platform_message': envelope_message(raw) or None,
        'platform_body': body_of(raw) or None,
    }


def parse_ship_result(raw: Any) -> Dict[str, Any]:
    """发货结果。bundle 读的是 `data.{totalNum,successNum}`（批量发货的计数）。"""
    payload = parse_write_result(raw)
    body = payload['platform_body'] if isinstance(payload['platform_body'], dict) else {}
    payload.update({
        'total_num': _int_or_none(body.get('totalNum')),
        'success_num': _int_or_none(body.get('successNum')),
        'fail_num': _int_or_none(body.get('failNum')),
    })
    return payload


def parse_rate_result(raw: Any) -> Dict[str, Any]:
    """评价结果。bundle 读的是 `data.module.{success,successOrderIds,failOrderInfos}`。"""
    payload = parse_write_result(raw)
    body = payload['platform_body'] if isinstance(payload['platform_body'], dict) else {}
    module = body.get('module') if isinstance(body.get('module'), dict) else body
    success = module.get('success') if isinstance(module, dict) else None
    payload.update({
        'rate_success': success if isinstance(success, bool) else None,
        'success_order_ids': _or_none(module.get('successOrderIds')) if isinstance(module, dict) else None,
        'fail_order_infos': _or_none(module.get('failOrderInfos')) if isinstance(module, dict) else None,
    })
    return payload


def parse_verify_result(raw: Any) -> Dict[str, Any]:
    """退款凭证（只读）的响应：`data.module.{verifyUrl, token}`。

    两处都**可能是空串**（平台对某些账号直接回空）—— 空串一律给 `None`，
    让上层显示「平台没给」，而不是拿 `''` 冒充一个能用的凭证。
    """
    payload = parse_write_result(raw)
    body = payload['platform_body'] if isinstance(payload['platform_body'], dict) else {}
    module = body.get('module') if isinstance(body.get('module'), dict) else body
    module = module if isinstance(module, dict) else {}
    payload.update({
        'verify_url': _text(module.get('verifyUrl')) or None,
        'token': _text(module.get('token')) or None,
        'scene': _text(module.get('scene')) or VERIFY_SCENE_REFUND,
    })
    return payload


def complain_failure(raw: Any) -> str:
    """投诉类接口的**业务级**失败：官方前端读的是 `data.success` + `data.failReason`。

    这类失败顶层 `success` 仍是 true，`envelope_error()` 抓不到，所以单独提供。
    """
    raw = decode_payload(raw)
    data = decode_payload(raw.get('data')) if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        return ''
    if data.get('success') is False:
        return _text(data.get('failReason')) or '平台拒绝了本次投诉操作'
    return ''


def parse_close_reasons(raw: Any) -> Dict[str, Any]:
    """关单原因候选。官方读 `data.closeReasons`，元素是**字符串**。

    认不出列表键一律 `None`（不是 `[]`）—— 「平台没给」和「平台给了空列表」是两件事。
    """
    raw = decode_payload(raw)
    data = decode_payload(raw.get('data')) if isinstance(raw, dict) else None
    if not isinstance(data, dict):
        data = raw if isinstance(raw, dict) else {}
    reasons = None
    for key in ('closeReasons', 'reasonList', 'reasons', 'list'):
        value = data.get(key)
        if isinstance(value, list):
            reasons = [_text(item) for item in value if _text(item)]
            break
    return {
        'close_reasons': reasons,
        'raw_keys': sorted(data.keys())[:20] if isinstance(data, dict) else [],
    }


def parse_batch_result(raw: Any) -> Dict[str, Any]:
    """批量写（延长收货 / 提醒收货）的结果：`data.module.{totalNum,successNum,failOrderInfos}`。

    官方在没有 module 时读不到计数 —— 那种情况一律 `None`，绝不补 0。
    """
    payload = parse_write_result(raw)
    body = payload['platform_body'] if isinstance(payload['platform_body'], dict) else {}
    module = body.get('module') if isinstance(body.get('module'), dict) else body
    module = module if isinstance(module, dict) else {}
    payload.update({
        'total_num': _int_or_none(module.get('totalNum')),
        'success_num': _int_or_none(module.get('successNum')),
        'fail_order_infos': _or_none(module.get('failOrderInfos')),
    })
    return payload
