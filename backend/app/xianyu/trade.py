# -*- coding: utf-8 -*-
"""交易组只读接口（订单 / 退款 / 评价 / 投诉 / 退货地址）。

**接口来源（静态 CDN 反解，零风控）**：官方交易微应用
`https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3879584 B）里
每个接口的 `api` / `v` / `data` 参数对象都是硬编码的，本模块逐条照抄。

**真机证据（2026-10-10，账号 2217970177348，只读探针）**：原始响应落在
`logs/seller_discover/trade_probe.json` / `trade_probe2.json`，键名摘要落在
`trade_keys.txt`。实测结论：

======================  ==========================================  ============================
接口                    真机结果                                    响应容器
======================  ==========================================  ============================
order.count             SUCCESS                                     ``data.module.countInfoList``
sold.get                SUCCESS                                     ``data.module.items``
refund.list             SUCCESS                                     ``data.data.items`` ← 双层
rate.list               SUCCESS                                     ``data.module.items``
complain.list           ``NO_PERMISSION::用户没有权限``              取不到样本
address.list.query      ``FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问``  取不到样本
======================  ==========================================  ============================

**为什么有的接口没权限**：本项目用的是买家侧（www.goofish.com）Cookie，投诉与退货
地址属于卖家工作台会话（`seller.goofish.com`）的能力，与 task-36「商品数据接口」
同一个成因。拿不到就如实报 `platform_errors.explain`，**不猜字段、不用 0 顶替**。

**只读边界**：本模块只声明查询接口；同一个 bundle 里的写接口（发货、同意退款、
评价、改价、设置默认地址……）全部列进 `WRITE_APIS`，`assert_read_only()` 会在调用
前把它们拦下来。协议层不提供任何写交易数据的入口。

**金额单位**：真机给的是**字符串**（`"39.90"` / `"468.90"`），既不是分也不是浮点，
本模块一律**原样透传**，不做除法也不做 `float()`（那会引入二进制浮点误差，且与
平台展示口径不一致）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- 接口常量

ORDER_COUNT_API = 'mtop.taobao.idle.merchant.order.count'
ORDER_COUNT_VERSION = '1.0'
ORDER_LIST_API = 'mtop.taobao.idle.trade.merchant.sold.get'
ORDER_LIST_VERSION = '1.0'
ORDER_DETAIL_API = 'mtop.taobao.idle.trade.merchant.full.info'
ORDER_DETAIL_VERSION = '1.0'
REFUND_LIST_API = 'mtop.taobao.idle.merchant.refund.list'
REFUND_LIST_VERSION = '1.0'

#: 退款处理页四条只读接口（bundle `idle-seller-trade/0.0.26/js/main.js` 静态取证）。
#: `refund.refuse.render`：官方「拒绝原因」弹窗的数据源（**这是「拒绝退款」界面真正需要的**，
#: 我们以前让用户手抄 `refuseReasonList[].refuseReasonId`，那是把平台内部 id 丢给用户）。
REFUND_REFUSE_RENDER_API = 'mtop.taobao.idle.merchant.refund.refuse.render'
REFUND_REFUSE_RENDER_VERSION = '1.0'
#: `refund.detail`：退款详情，官方按 `data.data.components[].render` 挑组件渲染。
REFUND_DETAIL_API = 'mtop.taobao.idle.merchant.refund.detail'
REFUND_DETAIL_VERSION = '1.0'
#: `postage.refund.detail.query`：邮费退款详情（响应 `data.module`）。
POSTAGE_REFUND_DETAIL_API = 'mtop.taobao.idle.merchant.postage.refund.detail.query'
POSTAGE_REFUND_DETAIL_VERSION = '1.0'
#: `postage.refund.refuse.reason.query`：邮费退款拒绝原因，响应 `data.module.reasons`（**字符串数组**）。
POSTAGE_REFUND_REFUSE_REASONS_API = 'mtop.taobao.idle.merchant.postage.refund.refuse.reason.query'
POSTAGE_REFUND_REFUSE_REASONS_VERSION = '1.0'

#: 官方把这条拒绝原因置灰（hover 提示「扫码去APP操作」，只给手机端入口）：
#: 命中这些 `reasonId` 的原因我们同样置灰，并在响应里给出 `app_only` / `app_only_hint`。
APP_ONLY_REASON_IDS: Tuple[str, ...] = ('20653007',)
APP_ONLY_REASON_HINT = '这条原因只能在手机闲鱼 App 里操作（官方页面把它置灰并给了扫码入口）。'

#: 官方退款详情组件名（`components[].render`），只用于给界面提示，**不参与解析判断**。
REFUND_DETAIL_RENDERS: Tuple[str, ...] = (
    'nodeStatusInfo',
    'refundStatusInfo',
    'bottomBar',
    'progressDetail',
    'basicRefundInfo',
    'postageRefundInfo',
    'refundDescribe',
    'logisticsInfo',
)
RATE_LIST_API = 'mtop.taobao.idle.merchant.rate.list'
RATE_LIST_VERSION = '1.0'
COMPLAIN_LIST_API = 'mtop.taobao.idle.cco.shop.complain.list'
COMPLAIN_LIST_VERSION = '1.0'
ADDRESS_LIST_API = 'mtop.alibaba.idle.seller.platform.merchant.delivery.address.list.query'
ADDRESS_LIST_VERSION = '1.0'

#: `order.count` 的 `queryType`（真机三个页签共用同一个接口）。
ORDER_COUNT_TYPES = ('ORDER', 'REFUND', 'RATE')

#: 真机 `countInfoList[].queryCode` → 中文页签名（bundle 里页面自己维护的文案表）。
ORDER_COUNT_LABELS: Dict[str, str] = {
    'ALL': '全部',
    'NOT_PAY': '待付款',
    'NOT_SHIP': '待发货',
    'SHIPPED': '已发货',
    'TRADE_SUCCESS': '交易成功',
    'TRADE_CLOSED': '交易关闭',
    'REFUND': '退款中',
    'NOT_SHIP_EXPIRED': '发货超时',
    'NOT_SHIP_ABOUT_TO_EXPIRE': '即将超时',
}

#: 只读边界：这些是同一个 bundle 里的**写**接口，协议层绝不调用。
WRITE_APIS = (
    'mtop.taobao.idle.merchant.refund.agree.refund',
    'mtop.taobao.idle.merchant.refund.refuse',
    'mtop.taobao.idle.merchant.postage.refund.pay',
    'mtop.taobao.idle.merchant.postage.refund.refuse',
    'mtop.taobao.idle.merchant.rate.create',
    'mtop.taobao.idle.merchant.dispute.create',
    'mtop.taobao.idle.cco.shop.complain.refuse',
    'mtop.taobao.idle.cco.shop.complain.apply.revoke',
    'mtop.taobao.idle.cco.shop.complain.submit.proof',
    'mtop.taobao.idle.trade.merchant.close.by.seller',
    'mtop.taobao.idle.trade.merchant.user.adjust.price',
    'mtop.taobao.idle.trade.merchant.batch.delay.confirm',
    'mtop.taobao.idle.trade.merchant.batch.remind.confirm',
    'mtop.alibaba.idle.seller.platform.merchant.delivery.address.add',
    'mtop.alibaba.idle.seller.platform.merchant.delivery.address.update',
    'mtop.alibaba.idle.seller.platform.merchant.delivery.address.delete',
    'mtop.alibaba.idle.seller.platform.merchant.delivery.address.set.default',
    'mtop.alibaba.idle.seller.platform.merchant.delivery.place.info.set',
    'mtop.alibaba.idle.seller.platform.sms.sms.send',
    # 下面 4 个是 `trade_write.py`（写接口协议层）用到的，原先漏登记 —— 黑名单必须是
    # 写协议层的**超集**，否则只读层可能悄悄调用它们。`test_trade_write_api.py` 有一条
    # 断言钉住这个超集关系，漏一个就红。
    'mtop.taobao.idle.logistics.merchant.consign.offline',
    'mtop.taobao.idle.logistics.merchant.consign.dummy',
    'mtop.taobao.idle.cco.shop.complain.submit.active.proof',
    'mtop.taobao.idle.cco.shop.complain.submit.passive.proof',
    # 第三十八批（订单页写能力补全）新实现的 5 个写接口：黑名单是写协议层的超集，
    # 漏一个 `test_trade_write_api.py` 的断言就红。
    'mtop.taobao.idle.merchant.postage.refund.pay',
    'mtop.taobao.idle.cco.shop.complain.apply.revoke',
    # 第四十九批（订单备注）：同样是写协议层的超集要求，漏一个断言就红。
    'mtop.taobao.idle.merchant.add.memo',
)


class ReadOnlyViolation(RuntimeError):
    """试图调用交易写接口。"""


def assert_read_only(api: str) -> str:
    """只读守卫：写接口一律拒绝（返回原 api，方便内联使用）。"""
    if api in WRITE_APIS:
        raise ReadOnlyViolation(f'协议层是只读的，禁止调用写接口: {api}')
    return api


# --------------------------------------------------------------------------- 取值工具


def _text(value: Any) -> str:
    """空值一律 ''，数字转字符串（订单号/商品号真机给 int，必须保精度）。"""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return ''


def _or_none(value: Any) -> Optional[Any]:
    """真机缺字段就是缺字段 —— 返回 None，**不用 0/'' 顶替**。"""
    if value is None or value == '' or value == [] or value == {}:
        return None
    return value


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(source: Any, *keys: str) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        if source.get(key) is not None:
            return source[key]
    return None


def decode_payload(raw: Any) -> Any:
    """`valueType:"string"` 的接口可能把业务体塞成 JSON 字符串，统一解一层。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def envelope_error(raw: Any) -> str:
    """平台信封里是否失败：返回失败文案（'' 表示成功）。

    真机失败样例：`complain.list` → `NO_PERMISSION::用户没有权限`；
    `address.list.query` → `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。
    """
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return ''
    if raw.get('success') is False:
        message = _text(raw.get('message'))
        ret = raw.get('ret')
        if not message and isinstance(ret, list) and ret:
            message = _text(ret[0])
        return message or '平台接口返回失败'
    return ''


def envelope_message(raw: Any) -> str:
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return ''
    return _text(raw.get('message')) or _text(_first(raw.get('ret') or [], 0))


def body_of(raw: Any) -> Dict[str, Any]:
    """取出业务体。

    真机容器有三种：``data.module``（sold.get / rate.list / order.count）、
    ``data.data``（refund.list，双层）、``data`` 本身（address.list.query 是数组）。
    """
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return {}
    data = decode_payload(raw.get('data'))
    if isinstance(data, dict):
        for key in ('module', 'data'):
            nested = decode_payload(data.get(key))
            if isinstance(nested, dict):
                return nested
        return data
    return {}


def _outer_data(raw: Any) -> Any:
    """信封顶层的 `data` 原样（可能是 dict，也可能直接是 list）。"""
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return None
    return decode_payload(raw.get('data'))


def items_of(raw: Any) -> List[Any]:
    """列表项：``module.items`` / ``data.items`` / `data` 本身是数组。"""
    items = body_of(raw).get('items')
    if isinstance(items, list):
        return items
    data = _outer_data(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get('items'), list):
        return data['items']
    return []


def paging_of(raw: Any) -> Dict[str, Any]:
    """分页/服务器时间。

    真机的落点不统一：`sold.get` 的 `totalCount` / `nextPage` / `lastEndRow` 在
    `data.module` 里（2026-10-15 实测同一账号 `total_count=15`，见 `logs/order_sync57/`），
    refund.list / rate.list 的在**信封顶层**（`data` 的兄弟键）—— 所以下面把 raw /
    `body_of(raw)` / 外层 `data` 三处合并着找；三处都没有才返回 None（**不猜**）。
    """
    raw = decode_payload(raw)
    outer = _outer_data(raw)
    sources = [raw if isinstance(raw, dict) else {}, body_of(raw)]
    if isinstance(outer, dict):
        sources.append(outer)
    merged: Dict[str, Any] = {}
    for source in sources:
        for key in ('totalCount', 'nextPage', 'lastEndRow', 'serverTime'):
            if source.get(key) is not None:
                merged[key] = source[key]
    return {
        'total_count': _int_or_none(merged.get('totalCount')),
        'next_page': merged.get('nextPage') if merged.get('nextPage') is not None else None,
        'last_end_row': _int_or_none(merged.get('lastEndRow')),
        'server_time': _or_none(merged.get('serverTime')),
    }


# --------------------------------------------------------------------------- 请求构造


def order_count_request(query_type: str = 'ORDER') -> Tuple[str, Dict[str, Any]]:
    """页签计数。真机 `data:{queryType:"ORDER"}` → `countInfoList[{count,queryCode}]`。"""
    query = _text(query_type).upper() or 'ORDER'
    if query not in ORDER_COUNT_TYPES:
        query = 'ORDER'
    return assert_read_only(ORDER_COUNT_API), {'queryType': query}


def order_list_request(page: int = 1, size: int = 10, order_ids: str = '',
                       query_code: str = '', search_param: Any = None) -> Tuple[str, Dict[str, Any]]:
    """订单列表。真机 `{pageNumber, rowsPerPage, orderIds, queryCode, orderSearchParam}`。"""
    data: Dict[str, Any] = {'pageNumber': int(page), 'rowsPerPage': int(size)}
    if order_ids:
        data['orderIds'] = order_ids
    if query_code:
        data['queryCode'] = query_code
    if search_param:
        data['orderSearchParam'] = json.dumps(search_param, ensure_ascii=False)
    return assert_read_only(ORDER_LIST_API), data


def refund_list_request(page: int = 1, size: int = 10, search_param: Any = None) -> Tuple[str, Dict[str, Any]]:
    """退款列表。真机 `{pageNumber, rowsPerPage, queryType:"refund", refundSearchParam}`。"""
    data: Dict[str, Any] = {
        'pageNumber': int(page),
        'rowsPerPage': int(size),
        'queryType': 'refund',
    }
    if search_param:
        data['refundSearchParam'] = json.dumps(search_param, ensure_ascii=False)
    return assert_read_only(REFUND_LIST_API), data




def refund_refuse_render_request(refund_id: Any, refuse_reason_id: Any = '') -> Tuple[str, Dict[str, Any]]:
    """拒绝原因弹窗的数据源。官方 `{refundId, refuseReasonId}`（后者是回填选中项，可省）。"""
    data: Dict[str, Any] = {'refundId': _text(refund_id)}
    if _text(refuse_reason_id):
        data['refuseReasonId'] = _text(refuse_reason_id)
    return assert_read_only(REFUND_REFUSE_RENDER_API), data


def refund_detail_request(refund_id: Any, order_id: Any = '') -> Tuple[str, Dict[str, Any]]:
    """退款详情。官方 `{orderId, refundId}`。"""
    return assert_read_only(REFUND_DETAIL_API), {
        'orderId': _text(order_id),
        'refundId': _text(refund_id),
    }


def postage_refund_detail_request(order_id: Any, refund_id: Any = '',
                                  refund_postage_type: Any = '') -> Tuple[str, Dict[str, Any]]:
    """邮费退款详情。官方把 URL 上的 `refundPostageType` 一起带上。"""
    data: Dict[str, Any] = {'orderId': _text(order_id)}
    if _text(refund_id):
        data['refundId'] = _text(refund_id)
    if _text(refund_postage_type):
        data['refundPostageType'] = _text(refund_postage_type)
    return assert_read_only(POSTAGE_REFUND_DETAIL_API), data


def postage_refund_refuse_reasons_request(order_id: Any, refund_id: Any = '',
                                          refund_postage_type: Any = '',
                                          default_reason: Any = '') -> Tuple[str, Dict[str, Any]]:
    """邮费退款拒绝原因。官方 `prefetch: true`，`{orderId, refundId, refundPostageType, defaultReason}`。"""
    data: Dict[str, Any] = {'orderId': _text(order_id)}
    if _text(refund_id):
        data['refundId'] = _text(refund_id)
    if _text(refund_postage_type):
        data['refundPostageType'] = _text(refund_postage_type)
    if _text(default_reason):
        data['defaultReason'] = _text(default_reason)
    return assert_read_only(POSTAGE_REFUND_REFUSE_REASONS_API), data
def rate_list_request(page: int = 1, size: int = 10) -> Tuple[str, Dict[str, Any]]:
    """评价列表。真机带不带 `queryType` 返回体一致（都是待评价/已评价订单 + rateItemVOList）。"""
    return assert_read_only(RATE_LIST_API), {
        'pageNumber': int(page),
        'rowsPerPage': int(size),
        'queryType': 'RATE',
    }


def complain_list_request(page: int = 1, size: int = 10, order_id: str = '',
                          status: str = '') -> Tuple[str, Dict[str, Any]]:
    """投诉列表。真机 `data:{pageNum, pageSize, orderId, complainStatus}`（注意是 pageNum）。"""
    data: Dict[str, Any] = {'pageNum': int(page), 'pageSize': int(size)}
    if order_id:
        data['orderId'] = order_id
    if status:
        data['complainStatus'] = status
    return assert_read_only(COMPLAIN_LIST_API), data


def address_list_request() -> Tuple[str, Any]:
    """退货地址列表。真机**不传 data**，响应 `data` 直接是数组。"""
    return assert_read_only(ADDRESS_LIST_API), None


# --------------------------------------------------------------------------- 归一化


def _buyer_of(source: Any, *keys: str) -> Optional[Dict[str, Any]]:
    vo = _first(source, *(keys or ('buyerInfoVO', 'merchantBuyerVO')))
    if not isinstance(vo, dict):
        return None
    return {
        'user_id': _text(_first(vo, 'buyerId', 'userId')) or None,
        'nick': _text(_first(vo, 'userNick', 'nick')) or None,
        'name': _text(vo.get('name')) or None,
        'phone': _text(vo.get('phone')) or None,
        'address': _text(vo.get('address')) or None,
        'avatar': _text(_first(vo, 'userIcon', 'avatar')) or None,
        'is_privacy': vo.get('isPrivacy') if isinstance(vo.get('isPrivacy'), bool) else None,
    }


def _item_of(source: Any, *keys: str) -> Optional[Dict[str, Any]]:
    vo = _first(source, *(keys or ('itemVO', 'merchantItemVO')))
    if not isinstance(vo, dict):
        return None
    lines = vo.get('itemInfoLines')
    info_lines = []
    if isinstance(lines, list):
        for line in lines:
            if isinstance(line, dict):
                info_lines.append({'key': _text(line.get('key')) or None,
                                   'value': _text(line.get('value')) or None})
    return {
        'title': _text(vo.get('title')) or None,
        'pic_url': _text(vo.get('itemPicUrl')) or None,
        'info_lines': info_lines or None,
    }


def _buttons_of(source: Any) -> Optional[List[Dict[str, Any]]]:
    right = source.get('rightVO') if isinstance(source, dict) else None
    buttons = right.get('btnList') if isinstance(right, dict) else None
    if not isinstance(buttons, list):
        return None
    out = []
    for button in buttons:
        if not isinstance(button, dict):
            continue
        event = button.get('clickEvent') if isinstance(button.get('clickEvent'), dict) else {}
        payload = event.get('data') if isinstance(event.get('data'), dict) else {}
        out.append({
            'name': _text(button.get('name')) or None,
            'code': _text(_first(button, 'code', 'tradeAction')) or None,
            'disabled': button.get('buttonDisable') if isinstance(button.get('buttonDisable'), bool) else None,
            'action_type': _text(event.get('type')) or None,
            'url': _text(payload.get('url')) or None,
            'action_code': _text(payload.get('code')) or None,
        })
    return out or None


def _times_of(common: Any) -> Dict[str, Any]:
    common = common if isinstance(common, dict) else {}
    return {
        'create_time': _text(common.get('createTime')) or None,
        'pay_time': _text(common.get('paySuccessTime')) or None,
        'consign_time': _text(common.get('consignTime')) or None,
        'finish_time': _text(common.get('finishTime')) or None,
    }


def normalize_order(item: Any) -> Dict[str, Any]:
    """订单项。真机字段：buyerInfoVO / commonData / itemVO / priceVO / columnVOList / rightVO。"""
    item = item if isinstance(item, dict) else {}
    common = item.get('commonData') if isinstance(item.get('commonData'), dict) else {}
    price = item.get('priceVO') if isinstance(item.get('priceVO'), dict) else {}
    columns = []
    for column in item.get('columnVOList') or []:
        if not isinstance(column, dict):
            continue
        values = []
        for content in column.get('contentVOList') or []:
            if isinstance(content, dict):
                key = content.get('key') if isinstance(content.get('key'), dict) else {}
                values.append({'text': _text(key.get('text')) or None})
        columns.append({'name': _text(column.get('name')) or None,
                        'num': _int_or_none(column.get('num')),
                        'values': values or None})
    tips = common.get('consignTipsText')
    consign_tips = [_text(tip.get('text')) for tip in tips or []
                    if isinstance(tip, dict) and _text(tip.get('text'))]
    # 订单备注（卖家自己的标记，不对买家展示）：官方写回读 `merchantCommonData.{memo,memoGroup}`
    # （交易 bundle 偏移 2069748），列表行里也有落在 `commonData` 的；两处都认，缺就是 None。
    merchant = item.get('merchantCommonData') if isinstance(item.get('merchantCommonData'), dict) else {}
    memo = common.get('memo') if common.get('memo') is not None else merchant.get('memo')
    memo_group = common.get('memoGroup') if common.get('memoGroup') is not None else merchant.get('memoGroup')
    return {
        'order_id': _text(common.get('orderId')) or None,
        'item_id': _text(common.get('itemId')) or None,
        'order_status': _text(common.get('orderStatus')) or None,
        'buyer': _buyer_of(item),
        'item': _item_of(item),
        'price': {
            'auction_price': _or_none(price.get('auctionPrice')),
            'buy_num': _int_or_none(price.get('buyNum')),
            'post_fee': _or_none(price.get('postFee')),
            'discount_fee': _or_none(price.get('discountFee')),
            'confirm_fee': _or_none(price.get('confirmFee')),
            'refund_fee': _or_none(price.get('refundFee')),
            'total_price': _or_none(price.get('totalPrice')),
        },
        'times': _times_of(common),
        'in_refund': common.get('inRefund') if isinstance(common.get('inRefund'), bool) else None,
        'seller_rate_status': _int_or_none(common.get('sellerRateStatus')),
        'buyer_apply_modify_address': (common.get('buyerApplyModifyAddress')
                                       if isinstance(common.get('buyerApplyModifyAddress'), bool) else None),
        'consign_tips': consign_tips or None,
        'memo': _text(memo) or None,
        'memo_group': _int_or_none(memo_group),
        'columns': columns or None,
        'buttons': _buttons_of(item),
    }


def normalize_refund(item: Any) -> Dict[str, Any]:
    """退款项。真机容器是 `data.data.items`，字段在 commonData / refundInfoVO / priceVO。"""
    item = item if isinstance(item, dict) else {}
    common = item.get('commonData') if isinstance(item.get('commonData'), dict) else {}
    info = item.get('refundInfoVO') if isinstance(item.get('refundInfoVO'), dict) else {}
    price = item.get('priceVO') if isinstance(item.get('priceVO'), dict) else {}
    rich = common.get('orderSimpleRemarkRichText')
    rich_text = []
    if isinstance(rich, dict):
        for node in rich.get('data') or []:
            if isinstance(node, dict):
                rich_text.append({'content': _text(node.get('content')) or None,
                                  'type': _text(node.get('type')) or None})
    return {
        'refund_id': _text(info.get('refundId')) or None,
        'order_id': _text(common.get('orderId')) or None,
        'item_id': _text(common.get('itemId')) or None,
        'order_status': _text(common.get('orderStatus')) or None,
        'refund_status': _int_or_none(common.get('refundStatus')),
        'refund_status_text': _text(info.get('refundStatus')) or None,
        'refund_type': _int_or_none(info.get('refundType')),
        'refund_accept': info.get('refundAccept') if isinstance(info.get('refundAccept'), bool) else None,
        'cs_status': _text(info.get('csStatus')) or None,
        'reason': _text(info.get('reason')) or None,
        'goods_needs': _int_or_none(info.get('goodsNeeds')),
        'in_sale': info.get('inSale') if isinstance(info.get('inSale'), bool) else None,
        'refund_time': _text(info.get('gmtCreate')) or None,
        'remark': _text(common.get('orderSimpleRemark')) or None,
        'remark_rich_text': rich_text or None,
        'buyer': _buyer_of(item),
        'item': _item_of(item),
        'price': {
            'auction_price': _or_none(price.get('auctionPrice')),
            'buy_num': _int_or_none(price.get('buyNum')),
            'refund_fee': _or_none(price.get('refundFee')),
            'refund_fee_view': _or_none(price.get('refundFeeView')),
        },
        'times': _times_of(common),
        'buttons': _buttons_of(item),
    }


def normalize_rate_record(record: Any) -> Dict[str, Any]:
    """一条评价（真机在 `items[].rateItemVOList[]`，本项目账号暂无评价故为空数组）。"""
    record = record if isinstance(record, dict) else {}
    pictures = record.get('pictCdnUrlList')
    return {
        'rate_id': _text(record.get('rateId')) or None,
        'rate': _text(record.get('rate')) or None,
        'main': _text(record.get('main')) or None,
        'feedback': _text(record.get('feedBack')) or None,
        'level': _text(record.get('buyerMainRateLevel')) or None,
        'create_time': _text(record.get('gmtCreate')) or None,
        'illegal': record.get('illegal') if isinstance(record.get('illegal'), bool) else None,
        'rater_avatar': _text(record.get('raterHeadImg')) or None,
        'pictures': [str(url) for url in pictures or [] if url] or None,
        'service_tags': record.get('serviceTags') if isinstance(record.get('serviceTags'), list) else None,
        'seller': record.get('seller') if isinstance(record.get('seller'), dict) else None,
    }


def normalize_evaluation(item: Any) -> Dict[str, Any]:
    """评价页的一条订单（真机 `data.module.items`，字段是 merchant* 前缀）。"""
    item = item if isinstance(item, dict) else {}
    common = item.get('merchantCommonData') if isinstance(item.get('merchantCommonData'), dict) else {}
    records = item.get('rateItemVOList')
    rates = [normalize_rate_record(record) for record in records or [] if isinstance(record, dict)]
    return {
        'order_id': _text(common.get('orderId')) or None,
        'item_id': _text(common.get('itemId')) or None,
        'order_status': _text(common.get('orderStatus')) or None,
        'seller_rate_status': _int_or_none(common.get('sellerRateStatus')),
        'has_rate': bool(rates),
        'rates': rates or None,
        'buyer': _buyer_of(item, 'merchantBuyerVO'),
        'item': _item_of(item, 'merchantItemVO'),
        'times': _times_of(common),
    }


def normalize_address(item: Any) -> Dict[str, Any]:
    """退货地址。

    **bundle 里有两处读法，所以这里两套键都认**（真机 `address.list.query` 对我们无权限、
    拿不到样本，因此不猜哪一套是真的）：

    - 只读页的 `formatter`：`{addressId, name, phoneNum, address, defaultAddr}`
      —— `address` 是省市区+详细地址拼好的一整串；
    - 写页的编辑弹窗（偏移 3780900~3782550）：`editForm.setValues({...e,
      division:[e.provinceName, e.cityName, e.districtName], defaultAddr:…})`，即按
      `contactId` / `contactName` / `mobilePhone` / `provinceName` / `cityName` /
      `districtName` / `detailAddress` / `defaultAddr` **逐个字段**读
      （`address-add-modal` 的 schema 也是这几个名字）。

    前 5 个键永远存在（缺失给 `None`）；后一套**平台给了才出现**，空值不出现 ——
    绝不凭空造字段。UI 有就能预填编辑表单，没有就照旧让用户重填。
    """
    item = item if isinstance(item, dict) else {}
    out: Dict[str, Any] = {
        'address_id': _text(_first(item, 'addressId', 'contactId', 'id')) or None,
        'name': _text(_first(item, 'name', 'contactName')) or None,
        'phone': _text(_first(item, 'phoneNum', 'phone', 'mobilePhone')) or None,
        'address': _text(item.get('address')) or None,
        'is_default': item.get('defaultAddr') if isinstance(item.get('defaultAddr'), bool) else None,
    }
    per_field = {
        'contact_id': _first(item, 'contactId'),
        'contact_name': _first(item, 'contactName'),
        'mobile_phone': _first(item, 'mobilePhone'),
        'province_name': _first(item, 'provinceName'),
        'city_name': _first(item, 'cityName'),
        'district_name': _first(item, 'districtName'),
        'detail_address': _first(item, 'detailAddress'),
    }
    for key, value in per_field.items():
        text = _text(value).strip()
        if text:
            out[key] = text
    return out


def normalize_complaint(item: Any) -> Dict[str, Any]:
    """投诉项。

    **样本缺失**：真机返回 `NO_PERMISSION::用户没有权限`，拿不到任何一条投诉，因此这里
    **不做字段猜测** —— 原样透传平台给的键值（`json` 可序列化的浅拷贝），由调用方
    自行解释。宁可透传也不编字段名。
    """
    if not isinstance(item, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in item.items():
        out[str(key)] = value
    return out


def parse_order_counts(raw: Any) -> Dict[str, Any]:
    """`order.count` 响应 → `{'counts': {code: count}, 'tabs': [{code,label,count}]}`。

    真机 `countInfoList[{count, queryCode}]`（**不是** bundle 里的 title/number/key，
    那是前端自己的文案对象）。
    """
    body = body_of(raw)
    counts: Dict[str, Optional[int]] = {}
    tabs: List[Dict[str, Any]] = []
    for row in body.get('countInfoList') or []:
        if not isinstance(row, dict):
            continue
        code = _text(row.get('queryCode'))
        if not code:
            continue
        count = _int_or_none(row.get('count'))
        counts[code] = count
        tabs.append({'code': code, 'label': ORDER_COUNT_LABELS.get(code) or None, 'count': count})
    return {'counts': counts, 'tabs': tabs}


def parse_orders(raw: Any) -> Dict[str, Any]:
    """订单列表响应 → 归一化列表 + 分页。"""
    return {
        'orders': [normalize_order(item) for item in items_of(raw)],
        'paging': paging_of(raw),
    }


def parse_refunds(raw: Any) -> Dict[str, Any]:
    """退款列表响应 → 归一化列表 + 分页 + `ext.totalRefundFee`（真机有）。"""
    body = body_of(raw)
    ext = body.get('ext') if isinstance(body.get('ext'), dict) else {}
    return {
        'refunds': [normalize_refund(item) for item in items_of(raw)],
        'total_refund_fee': _or_none(ext.get('totalRefundFee')),
        'paging': paging_of(raw),
    }


def parse_evaluations(raw: Any) -> Dict[str, Any]:
    """评价列表响应 → 归一化列表 + 分页。"""
    return {
        'evaluations': [normalize_evaluation(item) for item in items_of(raw)],
        'paging': paging_of(raw),
    }


def parse_complaints(raw: Any) -> Dict[str, Any]:
    """投诉列表响应 → 透传项 + `statusCountMap`（bundle 里前端读的就是它）。"""
    body = body_of(raw)
    status_map = body.get('statusCountMap')
    return {
        'complaints': [normalize_complaint(item) for item in items_of(raw)],
        'status_count_map': status_map if isinstance(status_map, dict) else None,
        'paging': paging_of(raw),
    }


def parse_addresses(raw: Any) -> Dict[str, Any]:
    """退货地址响应。真机无权限；bundle 证据是 `data` 直接为数组。"""
    return {'addresses': [normalize_address(item) for item in items_of(raw)]}


def _is_app_only(reason_id: Any) -> bool:
    return _text(reason_id) in APP_ONLY_REASON_IDS


def normalize_refuse_reason(entry: Any) -> Dict[str, Any]:
    """一条拒绝原因：`{refuse_reason_id, reason_id, reason_name, app_only}`。

    `refuse_reason_id` 是**要提交给平台的那个 id**；`reason_id` 只用来判断「官方置灰」。
    """
    reason_id = _or_none(_first(entry, 'reasonId', 'reason_id'))
    return {
        'refuse_reason_id': _or_none(_first(entry, 'refuseReasonId', 'refuse_reason_id')),
        'reason_id': reason_id,
        'reason_name': _or_none(_first(entry, 'reasonName', 'reason_name')),
        'app_only': _is_app_only(reason_id),
        'raw': entry,
    }


def parse_refuse_render(raw: Any) -> Dict[str, Any]:
    """拒绝原因弹窗：`data.data.{refuseReasonList, refuseNegotiation, refuseProof, refundFee}`。"""
    body = body_of(raw)
    reasons_raw = _first(body, 'refuseReasonList', 'refuse_reason_list', 'reasons')
    reasons = None
    if isinstance(reasons_raw, list):
        reasons = [normalize_refuse_reason(x) for x in reasons_raw]
    return {
        'refuse_reasons': reasons,
        'refuse_reasons_found': isinstance(reasons_raw, list),
        'refuse_negotiation': _or_none(_first(body, 'refuseNegotiation', 'refuse_negotiation')),
        'refuse_proof': _or_none(_first(body, 'refuseProof', 'refuse_proof')),
        'refund_fee': _or_none(_first(body, 'refundFee', 'refund_fee')),
        'app_only_reason_ids': list(APP_ONLY_REASON_IDS),
        'app_only_hint': APP_ONLY_REASON_HINT,
        'body_keys': sorted(str(k) for k in body),
    }


def parse_refund_detail(raw: Any) -> Dict[str, Any]:
    """退款详情：官方按 `data.data.components[].render` 挑组件渲染。

    我们**原样透传组件列表**（每项带 `render` 与原始内容）并给出 `component_renders`，
    不把组件内部字段猜成我们的字段名。
    """
    body = body_of(raw)
    components_raw = _first(body, 'components', 'componentList', 'component_list')
    components = None
    if isinstance(components_raw, list):
        components = [
            {'render': _or_none(_first(x, 'render', 'type')), 'raw': x}
            if isinstance(x, dict) else {'render': None, 'raw': x}
            for x in components_raw
        ]
    renders = [c['render'] for c in (components or []) if c.get('render')]
    return {
        'components': components,
        'components_found': isinstance(components_raw, list),
        'component_renders': renders,
        'known_renders': list(REFUND_DETAIL_RENDERS),
        'body_keys': sorted(str(k) for k in body),
    }


def parse_postage_refund_detail(raw: Any) -> Dict[str, Any]:
    """邮费退款详情（响应 `data.module`）：原样透传 + 两个已知布尔的安全归一。"""
    body = body_of(raw)
    processing = _first(body, 'existProcessingCompensate', 'exist_processing_compensate')
    return {
        'detail': body or None,
        'detail_found': bool(body),
        'exist_processing_compensate': None if processing is None else (
            processing if isinstance(processing, bool) else _text(processing).lower() == 'true'
        ),
        'body_keys': sorted(str(k) for k in body),
    }


def parse_postage_refuse_reasons(raw: Any) -> Dict[str, Any]:
    """邮费退款拒绝原因：响应 `data.module.reasons`，**元素是字符串**（官方直接当 label/value 用）。"""
    body = body_of(raw)
    reasons_raw = _first(body, 'reasons', 'reasonList', 'reason_list')
    reasons = None
    if isinstance(reasons_raw, list):
        reasons = [_text(x) for x in reasons_raw]
    return {
        'reasons': reasons,
        'reasons_found': isinstance(reasons_raw, list),
        'body_keys': sorted(str(k) for k in body),
    }
