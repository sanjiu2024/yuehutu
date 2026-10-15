"""物流族服务：快递公司列表 / 运单号认公司（只读）+ 修改运单号（重发，写）。

协议层见 `xianyu/logistics.py`（官方 bundle 静态取证，零平台请求）。这里只做三件事：
拼请求、把平台返回翻成人能看的结果、把写操作的三步顺序钉死。

**三步顺序（写路径）**：确认 → 字段校验 → 建客户端。
- 确认在最前：未确认时连字段错都不该报，否则会误导用户「填对了就能发」；
- 字段校验在建客户端之前：纯本地、免费，账号掉线时也该先说「请输入快递单号」。

**只读路径的平台拒绝不做降级**：这一族只有三个接口，`companies` 被拒时如实 502、
由 `services/platform_errors.py` 统一补人话；把「没权限」装成「没有快递公司」是假数据。

**诚实边界**：一次真机写请求都没发过（红线不允许为了验证去改你平台上的物流单号）。
两个只读接口的形状来自 bundle，也尚未真机跑过。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..xianyu import division as division_proto
from ..xianyu import logistics as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读服务里的私有助手（同包内，避免两套判定漂移）
from .trade import _client

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = common.CONFIRM_CODE

#: 平台没给列表键时的 warning（与「确实没有快递公司」区分开）。
WARN_LIST_KEY_MISSING = {
    'code': 'list_key_missing',
    'message': '平台这一次没有返回快递公司列表字段（items）。下面的空列表不等于「一家快递公司都没有」，'
               '别当成 0 家 —— 点「重试」再看一次。',
}

#: 运单号认不出公司时的 warning（不猜公司）。
WARN_NO_MATCH = {
    'code': 'no_company_matched',
    'message': '这个单号没有识别出快递公司（平台没回 unionCode）。要么手工选一家，要么核对单号有没有抄错。',
}

#: 平台没回显快递公司编码时的 warning（回执里本来就没有业务 id，不代表失败）。
WARN_NOT_ECHOED = {
    'code': 'not_echoed',
    'message': '平台确认收到了这次修改，但没有回显快递公司编码（官方回执里也没有 id）。'
               '是否生效请点「刷新」看平台当前值。',
}


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload.setdefault('source', 'platform')
    payload.setdefault('fetched_at', _now())
    return payload


#: 判据单点：services/common.confirm_truthy（宽松档，全仓唯一一份实现）。
_truthy = common.confirm_truthy


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰账号、不碰平台。"""
    return common.confirm_required(action, confirm)


def _missing(label: str, value: Any) -> Optional[Dict[str, Any]]:
    if _text(value):
        return None
    return common.fail(f'{label}不能为空', 'invalid_fields')


def _call_failure(call_status: str, value: Any, api: str) -> Dict[str, Any]:
    """协议层没调通（方法缺失 / 抛异常 / 登录态过期）时的失败信封。"""
    failure = common.call_failed(call_status, value) or common.fail('协议层调用失败', 'protocol_error')
    failure.setdefault('platform_errors', {
        'api': api,
        'explain': str(value) or '协议层未提供该方法',
    })
    return failure


def _platform_failure(value: Any, api: str, fallback: str) -> Dict[str, Any]:
    """平台回了信封但业务体不算成功 ⇒ 502，把平台原文透出去。

    **不判登录态**：无权限 / 参数不对都不等于掉线，别让用户白扫一次码。
    """
    reason = proto.envelope_error(value) or fallback
    logger.warning(f'[logistics] 平台拒绝 {api}: {reason}')
    return common.fail(
        f'平台接口失败: {reason}',
        'protocol_error',
        platform_errors={
            'api': api,
            'message': proto.envelope_message(value),
            'explain': reason,
        },
    )


def _call(account_id: str, api: str, data: Optional[Dict[str, Any]],
          value_type: Optional[str] = None) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """发一次 mtop 调用，返回 `(客户端返回值, 失败信封)`；成功时第二个是 `None`。

    账号/凭据检查只有一份：复用只读交易服务里的 `_client` ——
    **账号不存在 → 404 `not_found`**、**没有 Cookie 或已判定要重登 → 409 `verification_required`**，
    两种都是本地判定，**一个平台请求都不发**。
    """
    client, failure = _client(account_id)
    if client is None:
        return None, failure
    # value_type 只有区域码那一族要带（平台按 valueType 解析业务体）；不传就不发这个键
    extra = {'value_type': value_type} if value_type else {}
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION, **extra)
    if status != common.CALL_OK:
        return None, _call_failure(status, value, api)
    return value, None


# --------------------------------------------------------------------------- 只读


def companies(account_id: str) -> Dict[str, Any]:
    """快递公司列表（`com.taobao.idle.logistics.new.get.companies`，官方**不带 `data`**）。

    平台明确返回空数组时就是「一家都没有」；连 `items` 键都没给时给空列表 **+ warning**。
    """
    api, data = proto.companies_request()
    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回快递公司列表')

    parsed = proto.parse_companies(value)
    rows = parsed['companies']
    warnings: List[Dict[str, Any]] = []
    if rows is None:
        warnings.append(dict(WARN_LIST_KEY_MISSING))
        rows = []
    return _stamp({
        'account_id': account_id,
        'api': api,
        'companies': rows,
        'count': len(rows),
        'found_key': parsed['found_key'],
        'default_code': proto.DEFAULT_COMPANY_CODE,
        'warnings': warnings,
    })


def guess_mailno(account_id: str, mail_no: Any) -> Dict[str, Any]:
    """运单号认公司（`mtop.taobao.idle.logistics.guess.mailno`）。

    认不出时 `cp_code` / `cp_name` 都是 `null` + 一条 warning —— **不猜公司**。
    """
    try:
        api, data = proto.guess_request(mail_no)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有识别出快递公司')

    parsed = proto.parse_guess(value)
    warnings: List[Dict[str, Any]] = []
    if not parsed['cp_code']:
        warnings.append(dict(WARN_NO_MATCH))
    return _stamp({
        'account_id': account_id,
        'api': api,
        'mail_no': data['mailNo'],
        'cp_code': parsed['cp_code'],
        'cp_name': parsed['cp_name'],
        'matched': bool(parsed['cp_code']),
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- 写


def resend_consign(account_id: str, order_id: Any, mail_no: Any, cp_code: Any = '',
                   confirm: Any = None) -> Dict[str, Any]:
    """修改运单号 / 重发（`mtop.taobao.idle.logistics.merchant.consign.resend`）。

    `cp_code` 留空时用官方默认值 `OTHER`。成功才写操作日志 + 发
    `orders.shipment_updated`（订单页据此静默重拉）。
    """
    problem = _confirm_gate('修改运单号', confirm)
    if problem is not None:
        return problem
    try:
        api, data = proto.resend_request(order_id, mail_no, cp_code, confirm=confirm)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有接受这次修改')

    parsed = proto.parse_resend(value)
    warnings: List[Dict[str, Any]] = []
    if not parsed['echoed']:
        warnings.append(dict(WARN_NOT_ECHOED))
    if parsed['saved']:
        common.log_operation(
            account_id, 'logistics_resend', data['tradeId'], True,
            {'mail_no': data['mailNo'], 'cp_code': data['cpCode']},
        )
        common.publish('orders.shipment_updated', {
            'order_id': data['tradeId'], 'company': data['cpCode'], 'tracking_no': data['mailNo'],
        })
    return _stamp({
        'account_id': account_id,
        'action': 'resend',
        'api': api,
        'order_id': data['tradeId'],
        'mail_no': data['mailNo'],
        'cp_code': data['cpCode'],
        'saved': parsed['saved'],
        'echoed': parsed['echoed'],
        'platform_message': parsed['platform_message'],
        'submitted': {'tradeId': data['tradeId'], 'mailNo': data['mailNo'], 'cpCode': data['cpCode']},
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- READ-COVERAGE-61
def unconsign_detail(account_id: str, order_id: Any) -> Dict[str, Any]:
    """未发货凭证详情（mtop.taobao.idle.merchant.unconsign.detail，只读）。

    凭证文本 / 图片平台没给就是 null（前端显示「-」），**不补空串、不补空数组**。
    """
    try:
        api, data = proto.unconsign_detail_request(order_id)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回发货凭证')

    parsed = proto.parse_unconsign_detail(value)
    warnings: List[Dict[str, Any]] = []
    if parsed['content'] is None and parsed['image_list'] is None:
        warnings.append({
            'code': 'no_evidence',
            'message': '平台没有返回这张凭证的内容或图片（可能这一单本来就没有上传凭证），'
                       '不代表「凭证是空的」—— 请点「刷新」再看一次。',
        })
    return _stamp({
        'account_id': account_id,
        'api': api,
        'order_id': data['orderId'],
        'content': parsed['content'],
        'image_list': parsed['image_list'],
        'raw': parsed['raw'],
        'warnings': warnings,
    })


def delivery_place_detail(account_id: str) -> Dict[str, Any]:
    """发货地详情（mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query，只读）。

    **形状未真机取证**，所以 detail 是平台业务体的原样透出；平台没给就是 null + warning。
    """
    api, data = proto.delivery_place_detail_request()
    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回发货地信息')

    parsed = proto.parse_delivery_place_detail(value)
    warnings: List[Dict[str, Any]] = []
    if parsed['detail'] is None:
        warnings.append({
            'code': 'no_detail',
            'message': '平台没有返回发货地详情字段（形状未取证），请点「刷新」重试。',
        })
    return _stamp({
        'account_id': account_id,
        'api': api,
        'detail': parsed['detail'],
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- READ-COVERAGE-61
#: 平台没给 `divisions` 键时的 warning（与「这个上级真的没有下级」区分开）。
WARN_DIVISIONS_KEY_MISSING = {
    'code': 'list_key_missing',
    'message': '平台这一次没有返回区域码列表字段（divisions）。下面的空列表不等于「这个上级没有下级」，'
               '别当成 0 条 —— 点「重试」再看一次。',
}


def divisions(account_id: str, token: Any = '', page_index: Any = '') -> Dict[str, Any]:
    """区域码（`mtop.cainiao.geography.frontend.divisionsinfo.get`，只读，第 61 批）。

    `token` 留空 → 根 `'CN'`（真机 31 个省）；`page_index` 留空 → 1。
    参数不合格（页码不是正整数 / token 超长）→ 400 `invalid_fields`，**一个平台请求都不发**。

    平台**没给** `divisions` 键时给空列表 + warning（与「这个上级确实没有下级」区分开）。
    返回里带 `leaf_level`（官方口径 `level >= 4` 即叶子）—— 前端据此收级联；
    **不要用 `is_leaf`（平台 `divisionLeaf`）当终止判据**，真机在区级上它是 `false`。
    """
    try:
        api, data = division_proto.divisions_request(token, page_index)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, value_type=division_proto.VALUE_TYPE)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回区域码')

    parsed = division_proto.parse_divisions(value)
    rows = parsed['divisions']
    warnings: List[Dict[str, Any]] = []
    if rows is None:
        warnings.append(dict(WARN_DIVISIONS_KEY_MISSING))
        rows = []
    return _stamp({
        'account_id': account_id,
        'api': api,
        'token': data['divisionToken'],
        'parent_token': parsed['parent_token'],
        'divisions': rows,
        'count': len(rows),
        'total_count': parsed['total_count'],
        'total_page': parsed['total_page'],
        'page_index': parsed['page_index'],
        'page_size': parsed['page_size'],
        'leaf_level': division_proto.LEAF_LEVEL,
        'warnings': warnings,
    })

