# -*- coding: utf-8 -*-
"""交易组只读服务（订单 / 退款 / 评价 / 投诉 / 退货地址）。

**只读**：这一层只调用 `backend/app/xianyu/trade.py` 里声明的查询接口，任何写接口
（发货、同意退款、评价、改价、设置默认地址……）都不在这里出现，协议层的
`assert_read_only()` 还会再拦一道。

**真机口径（2026-10-10，账号 2217970177348，原始响应 `logs/seller_discover/trade_probe*.json`）**：

- `orders` / `refunds` / `evaluations` 三个接口用本项目 Cookie 就能拿到真数据；
- `complaints` / `addresses` 真机回 `NO_PERMISSION::用户没有权限` /
  `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`（卖家工作台会话才有权限），
  服务层如实翻成 `platform_error` 信封，**不返回空列表假装成功**；
- 金额是真机给的**字符串**（`"39.90"`），原样透传，不做单位换算；
- 平台没给的字段是 `None`，**不用 0 顶替**。

**每个字段的来源路径**（协议层归一化后同名）：

- 订单：`data.module.items[].commonData.{orderId,itemId,orderStatus,createTime,
  paySuccessTime,consignTime,finishTime,inRefund,sellerRateStatus}`、
  `buyerInfoVO.*`、`itemVO.{title,itemPicUrl,itemInfoLines}`、
  `priceVO.{auctionPrice,buyNum,postFee,discountFee,confirmFee,refundFee,totalPrice}`、
  `columnVOList[]`、`rightVO.btnList[]`
- 退款：`data.data.items[].refundInfoVO.{refundId,refundStatus,reason,csStatus,gmtCreate,
  refundType,refundAccept,goodsNeeds,inSale}`、`commonData.{orderStatus,refundStatus,
  orderSimpleRemark}`、`priceVO.{refundFee,auctionPrice,buyNum}`、顶层 `data.data.ext.totalRefundFee`
- 评价：`data.module.items[].merchantCommonData.*`、`merchantBuyerVO.*`、`merchantItemVO.*`、
  `rateItemVOList[].{rateId,rate,main,feedBack,buyerMainRateLevel,gmtCreate,illegal,
  pictCdnUrlList,raterHeadImg,serviceTags,seller}`
- 页签计数（`?include_counts=1`）：`data.module.countInfoList[].{count,queryCode}`
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from ..core import db
from ..xianyu import trade as trade_proto
from . import common

MAX_PAGE_SIZE = 50


def _text(value: Any) -> str:
    if value is None:
        return ''
    return value if isinstance(value, str) else str(value)


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload.setdefault('source', 'platform')
    payload.setdefault('fetched_at', _now())
    return payload


# --------------------------------------------------------------------------- 前置检查


def _account_exists(account_id: str) -> bool:
    row = db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,))
    return bool(row)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """没有 Cookie / 已判定需要重登 —— 直接 409，一个平台请求都不发。"""
    row = db.query_one(
        'SELECT cookie, login_state FROM account_credentials WHERE account_id = ?',
        (account_id,),
    )
    if not row or not _text(row['cookie']).strip():
        return common.fail(
            '账号尚未登录或 Cookie 已失效，请先扫码登录',
            'verification_required',
            login_state=_text(row['login_state']) if row else 'unknown',
        )
    if _text(row['login_state']) == 'need_relogin':
        return common.fail(
            '账号登录态已过期，需要重新扫码登录',
            'verification_required',
            login_state='need_relogin',
        )
    return None


def _client(account_id: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    if not _account_exists(account_id):
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    client, problem = common.get_client(account_id)
    if client is None:
        return None, problem or common.not_ready()
    return client, None


def _page(page: Any, size: Any) -> Tuple[Optional[int], Optional[int], Optional[Dict[str, Any]]]:
    try:
        page_no = int(page)
        page_size = int(size)
    except (TypeError, ValueError):
        return None, None, common.fail('分页参数必须是整数', 'invalid_fields')
    if page_no < 1:
        return None, None, common.fail('页码必须从 1 开始', 'invalid_fields')
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        return None, None, common.fail(f'每页条数必须在 1~{MAX_PAGE_SIZE} 之间', 'invalid_fields')
    return page_no, page_size, None


# --------------------------------------------------------------------------- 调用


def _call(client: Any, api: str, data: Any, version: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """调一次协议层 `client.mtop`，把三种失败都翻成带 `platform_errors.explain` 的信封。

    `common.call_method` 走的是全项目统一路径：协程在共享事件循环里跑完、登录态过期
    会回写 `account_credentials.login_state`。
    """
    try:
        trade_proto.assert_read_only(api)
    except trade_proto.ReadOnlyViolation as exc:
        logger.error(f'[trade] 只读守卫拦截: {exc}')
        return None, common.fail(str(exc), 'bad_request')

    status, value = common.call_method(client, ('mtop',), api, data, v=version)
    if status != common.CALL_OK:
        failure = common.call_failed(status, value) or common.fail('协议层调用失败', 'protocol_error')
        failure.setdefault('platform_errors', {
            'api': api,
            'explain': _text(value) or '协议层未提供该方法',
        })
        return None, failure

    explain = trade_proto.envelope_error(value)
    if explain:
        return None, common.fail(
            f'平台接口失败: {explain}',
            'protocol_error',
            platform_errors={
                'api': api,
                'explain': explain,
                'message': trade_proto.envelope_message(value),
            },
        )
    return value, None


def _list(account_id: str, builder, version: str,
          parse, page: Any = 1, size: Any = 10) -> Dict[str, Any]:
    """分页列表的公共骨架。

    `builder` 是**函数**（`trade_proto.order_list_request` 之类），必须在 `_page`
    校验通过之后再调用：页码来自查询串，直接先构造请求会在 `int('abc')` 上抛 500。
    """
    page_no, page_size, failure = _page(page, size)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = builder(page_no, page_size)
    raw, failure = _call(client, api, data, version)
    if failure:
        return failure
    payload = parse(raw)
    payload['account_id'] = account_id
    payload['page'] = page_no
    payload['page_size'] = page_size
    payload['api'] = api
    return _stamp(payload)


# --------------------------------------------------------------------------- 对外接口


def orders(account_id: str, page: Any = 1, size: Any = 10,
           include_counts: bool = False) -> Dict[str, Any]:
    """订单列表（真机 SUCCESS）。

    `include_counts=True` 时额外调一次 `order.count` 拿页签计数（多一次平台请求，
    默认不调）。
    """
    result = _list(account_id, trade_proto.order_list_request,
                   trade_proto.ORDER_LIST_VERSION, trade_proto.parse_orders, page, size)
    if common.is_failure(result) or not include_counts:
        return result
    client, failure = _client(account_id)
    if failure:
        return failure
    counts, failure = _counts(client, 'ORDER')
    if failure:
        return failure
    result['counts'] = counts['counts']
    result['tabs'] = counts['tabs']
    return result


def _counts(client: Any, query_type: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """页签计数（`ORDER` / `REFUND` / `RATE`，真机 `countInfoList`）。"""
    api, data = trade_proto.order_count_request(query_type)
    raw, failure = _call(client, api, data, trade_proto.ORDER_COUNT_VERSION)
    if failure:
        return {}, failure
    payload = trade_proto.parse_order_counts(raw)
    payload['query_type'] = data.get('queryType')
    payload['api'] = api
    return payload, None


def refunds(account_id: str, page: Any = 1, size: Any = 10) -> Dict[str, Any]:
    """退款列表（真机 SUCCESS，容器是 `data.data`）。"""
    return _list(account_id, trade_proto.refund_list_request,
                 trade_proto.REFUND_LIST_VERSION, trade_proto.parse_refunds, page, size)


def evaluations(account_id: str, page: Any = 1, size: Any = 10) -> Dict[str, Any]:
    """评价列表（真机 SUCCESS；本项目账号的 `rateItemVOList` 目前都是空数组）。"""
    return _list(account_id, trade_proto.rate_list_request,
                 trade_proto.RATE_LIST_VERSION, trade_proto.parse_evaluations, page, size)


def complaints(account_id: str, page: Any = 1, size: Any = 10) -> Dict[str, Any]:
    """投诉列表。真机 `NO_PERMISSION::用户没有权限` → 返回 platform_error 信封。"""
    return _list(account_id, trade_proto.complain_list_request,
                 trade_proto.COMPLAIN_LIST_VERSION, trade_proto.parse_complaints, page, size)


def addresses(account_id: str) -> Dict[str, Any]:
    """退货地址列表。真机 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` → 返回 platform_error 信封。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = trade_proto.address_list_request()
    raw, failure = _call(client, api, data, trade_proto.ADDRESS_LIST_VERSION)
    if failure:
        return failure
    payload = trade_proto.parse_addresses(raw)
    payload['account_id'] = account_id
    payload['api'] = api
    return _stamp(payload)


# --------------------------------------------------------------------------- 退款处理（只读）


def _warn(payload: Dict[str, Any], code: str, message: str) -> None:
    payload.setdefault('warnings', []).append({'code': code, 'message': message})


def _one(account_id: str, api: str, data: Any, version: str, parse, **extra: Any) -> Dict[str, Any]:
    """单条只读的公共骨架（列表类走 `_list`）。字段校验必须已经在这一步之前做完。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    raw, failure = _call(client, api, data, version)
    if failure:
        return failure
    payload = parse(raw)
    payload.setdefault('warnings', [])
    payload['account_id'] = account_id
    payload['api'] = api
    for key, value in extra.items():
        payload[key] = value
    return _stamp(payload)


def refund_refuse_reasons(account_id: str, refund_id: Any, order_id: Any = '',
                          refuse_reason_id: Any = '') -> Dict[str, Any]:
    """拒绝退款的原因列表（`refund.refuse.render`）。

    这是「拒绝退款」真正需要的接口：以前界面让用户手抄 `refuseReasonList[].refuseReasonId`，
    等于把平台内部 id 丢给用户。官方把 `reasonId == 20653007` 那条置灰（只能在手机 App 操作），
    我们同样在响应里标 `app_only` 并给出中文 `app_only_hint`。
    """
    if not _text(refund_id):
        return common.fail('退款单号不能为空', 'invalid_fields')
    api, data = trade_proto.refund_refuse_render_request(refund_id, refuse_reason_id)
    payload = _one(account_id, api, data, trade_proto.REFUND_REFUSE_RENDER_VERSION,
                   trade_proto.parse_refuse_render,
                   refund_id=_text(refund_id), order_id=_text(order_id) or None)
    if common.is_failure(payload):
        return payload
    if not payload.get('refuse_reasons_found'):
        _warn(payload, 'refund_refuse_reasons_missing',
              '平台没有返回拒绝原因列表（可能改了字段名）——先别提交拒绝。')
    return payload


def refund_detail(account_id: str, refund_id: Any, order_id: Any = '') -> Dict[str, Any]:
    """退款详情（`refund.detail`）：官方按 `data.data.components[].render` 挑组件渲染。"""
    if not _text(refund_id):
        return common.fail('退款单号不能为空', 'invalid_fields')
    api, data = trade_proto.refund_detail_request(refund_id, order_id)
    payload = _one(account_id, api, data, trade_proto.REFUND_DETAIL_VERSION,
                   trade_proto.parse_refund_detail,
                   refund_id=_text(refund_id), order_id=_text(order_id) or None)
    if common.is_failure(payload):
        return payload
    if not payload.get('components_found'):
        _warn(payload, 'refund_detail_components_missing',
              '平台没有返回详情组件（可能改了字段名）。')
    return payload


def postage_refund_detail(account_id: str, order_id: Any, refund_id: Any = '',
                          refund_postage_type: Any = '') -> Dict[str, Any]:
    """邮费退款详情（`postage.refund.detail.query`，响应 `data.module`）。"""
    if not _text(order_id):
        return common.fail('订单号不能为空', 'invalid_fields')
    api, data = trade_proto.postage_refund_detail_request(order_id, refund_id, refund_postage_type)
    payload = _one(account_id, api, data, trade_proto.POSTAGE_REFUND_DETAIL_VERSION,
                   trade_proto.parse_postage_refund_detail,
                   order_id=_text(order_id), refund_id=_text(refund_id) or None)
    if common.is_failure(payload):
        return payload
    if not payload.get('detail_found'):
        _warn(payload, 'postage_refund_detail_missing',
              '平台没有返回邮费退款详情（可能改了字段名）。')
    return payload


def postage_refund_refuse_reasons(account_id: str, order_id: Any, refund_id: Any = '',
                                  refund_postage_type: Any = '',
                                  default_reason: Any = '') -> Dict[str, Any]:
    """邮费退款拒绝原因（`postage.refund.refuse.reason.query`）：`data.module.reasons` 是字符串数组。"""
    if not _text(order_id):
        return common.fail('订单号不能为空', 'invalid_fields')
    api, data = trade_proto.postage_refund_refuse_reasons_request(
        order_id, refund_id, refund_postage_type, default_reason)
    payload = _one(account_id, api, data, trade_proto.POSTAGE_REFUND_REFUSE_REASONS_VERSION,
                   trade_proto.parse_postage_refuse_reasons,
                   order_id=_text(order_id), refund_id=_text(refund_id) or None)
    if common.is_failure(payload):
        return payload
    if not payload.get('reasons_found'):
        _warn(payload, 'postage_refund_reasons_missing',
              '平台没有返回拒绝原因列表（可能改了字段名）——先别提交拒绝。')
    return payload
