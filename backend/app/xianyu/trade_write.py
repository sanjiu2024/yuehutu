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

#: 本模块声明的全部写接口（用于自检与文档；`trade.py` 的黑名单是它的超集）。
WRITE_APIS: Tuple[str, ...] = (
    SHIP_OFFLINE_API,
    SHIP_DUMMY_API,
    ADJUST_PRICE_API,
    REFUND_AGREE_API,
    REFUND_REFUSE_API,
    RATE_CREATE_API,
    COMPLAIN_REFUSE_API,
) + tuple(COMPLAIN_PROOF_APIS.values())


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
