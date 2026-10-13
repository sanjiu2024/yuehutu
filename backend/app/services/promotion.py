"""超级擦亮只读服务：首页、账户余额、日期指标、资金明细。

资金明细（流水 / 充值详情 / 提现详情）也全是**只读查询**：官方 bundle 里不存在
「发起充值 / 发起提现」的 mtop 接口，钱的实际划转在支付宝侧完成。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ..core import db
from ..xianyu import client as client_mod
from . import common

# 官方「资金明细」页的流水类型筛选（bundle 常量 KX），0=全部。
OPERATE_TYPES = {0: '全部', 1: '现金', 2: '礼金', 3: '红包'}

# 状态短标签取自官方 bundle 的枚举表（长文案是官方写死的，我们只给短标签；
# 未知状态码一律 None，不猜）。
RECHARGE_STATUS = {'ING': '支付中', 'SUCCESS': '充值成功', 'FAIL': '充值失败'}
WITHDRAW_STATUS = {'ING': '提现中', 'SUCCESS': '提现成功', 'FAIL': '提现失败'}


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    row = db.query_one('SELECT cookie, login_state FROM account_credentials WHERE account_id = ?', (account_id,))
    if row is None or not _text(row['cookie']).strip():
        return common.fail(f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录', 'verification_required', login_state='need_relogin')
    if _text(row['login_state']).strip() == 'need_relogin':
        return common.fail(f'账号 {account_id} 登录态已过期，需要重新扫码登录', 'verification_required', login_state='need_relogin')
    return None


def _client(account_id: str):
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


def _date_pair(start: str, end: str):
    today = datetime.now().date()
    default_end = today
    default_start = today - timedelta(days=6)
    a = _text(start).strip() or default_start.strftime('%Y%m%d')
    b = _text(end).strip() or default_end.strftime('%Y%m%d')
    if len(a) != 8 or len(b) != 8 or not a.isdigit() or not b.isdigit() or a > b:
        return None, common.fail('日期必须是 YYYYMMDD，且开始日期不能晚于结束日期', 'invalid_fields')
    return (a, b), None


def _call(client: Any, method: str, *args: Any):
    status, value = common.call_method(client, (method,), *args)
    if status == common.CALL_OK:
        return value, None
    failure = common.call_failed(status, value)
    if failure:
        return None, failure
    return None, common.fail(_text(value) or '推广平台接口调用失败', 'protocol_error')


def overview(account_id: str, start: str = '', end: str = '') -> Dict[str, Any]:
    dates, failure = _date_pair(start, end)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    home, failure = _call(client, 'ad_home')
    if failure:
        return failure
    card, failure = _call(client, 'ad_account_card')
    if failure:
        return failure
    member, failure = _call(client, 'ad_member_data', dates[0], dates[1])
    if failure:
        return failure
    return {'account_id': account_id, 'period': {'from': dates[0], 'to': dates[1]},
            'home': home, 'account': card, 'metrics': member,
            'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'source': 'platform'}


def _page(start: str, end: str, page: str, size: str):
    """区间 + 分页一起校验；分页只做上限保护，避免一次拉爆平台接口。"""
    dates, failure = _date_pair(start, end)
    if failure:
        return None, failure
    try:
        page_no = max(1, int(_text(page).strip() or 1))
        page_size = int(_text(size).strip() or 20)
    except (TypeError, ValueError):
        return None, common.fail('分页参数必须是整数', 'invalid_fields')
    if page_size < 1 or page_size > 50:
        return None, common.fail('每页条数必须在 1~50 之间', 'invalid_fields')
    return (dates[0], dates[1], page_no, page_size), None


def campaigns(account_id: str, start: str = '', end: str = '',
              page: str = '', size: str = '') -> Dict[str, Any]:
    """擦亮计划列表（只读）。金额字段单位是**分**，原样返回，由前端换算。"""
    params, failure = _page(start, end, page, size)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(client, 'ad_campaigns', params[0], params[1], params[2], params[3])
    if failure:
        return failure
    return {'account_id': account_id, 'period': {'from': params[0], 'to': params[1]},
            'page': params[2], 'page_size': params[3], 'campaigns': payload,
            'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'source': 'platform'}


def reports(account_id: str, start: str = '', end: str = '',
            page: str = '', size: str = '') -> Dict[str, Any]:
    """计划报表 + 商品报表（两个平台接口，都是只读）。"""
    params, failure = _page(start, end, page, size)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    plans, failure = _call(client, 'ad_campaign_metrics', params[0], params[1], params[2], params[3])
    if failure:
        return failure
    items, failure = _call(client, 'ad_item_metrics', params[0], params[1], params[2], params[3])
    if failure:
        return failure
    return {'account_id': account_id, 'period': {'from': params[0], 'to': params[1]},
            'page': params[2], 'page_size': params[3], 'plans': plans, 'items': items,
            'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'source': 'platform'}


def account(account_id: str, start: str = '', end: str = '') -> Dict[str, Any]:
    """账户视图：余额卡片 + 区间结算（金额是**元**字符串）。"""
    dates, failure = _date_pair(start, end)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    card, failure = _call(client, 'ad_account_card')
    if failure:
        return failure
    settle, failure = _call(client, 'ad_settle', dates[0], dates[1])
    if failure:
        return failure
    return {'account_id': account_id, 'period': {'from': dates[0], 'to': dates[1]},
            'account': card, 'settle': settle,
            'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), 'source': 'platform'}


def _int_arg(label: str, value: Any, allowed=None):
    """整数参数校验（空值给 0）；返回 ``(值, 失败)``。"""
    raw = _text(value).strip()
    if not raw:
        return 0, None
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None, common.fail(f'{label}必须是整数', 'invalid_fields')
    if number < 0:
        return None, common.fail(f'{label}不能是负数', 'invalid_fields')
    if allowed is not None and number not in allowed:
        return None, common.fail(f'{label}只能是 {"/".join(str(x) for x in allowed)}', 'invalid_fields')
    return number, None


def _stamp() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def funds(account_id: str, start: str = '', end: str = '', page: str = '', size: str = '',
          account_type: str = '', operate_type: str = '') -> Dict[str, Any]:
    """资金流水明细（只读）。平台没给列表键时 ``funds=None`` + 中文 warning，不造数据。"""
    params, failure = _page(start, end, page, size)
    if failure:
        return failure
    account_type_value, failure = _int_arg('账号类型', account_type)
    if failure:
        return failure
    operate_type_value, failure = _int_arg('流水类型', operate_type, allowed=sorted(OPERATE_TYPES))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(client, 'ad_account_log', params[0], params[1], params[2], params[3],
                             account_type_value, operate_type_value)
    if failure:
        return failure
    warnings = []
    entries = payload.get('result') if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        entries = None
        warnings.append('平台没有返回流水列表（result），这一页当空处理，请点「刷新」重试')
    total = payload.get('total') if isinstance(payload, dict) else None
    if not isinstance(total, int) or isinstance(total, bool):
        total = None
    return {'account_id': account_id, 'period': {'from': params[0], 'to': params[1]},
            'page': params[2], 'page_size': params[3],
            'account_type': account_type_value, 'operate_type': operate_type_value,
            'operate_type_text': OPERATE_TYPES.get(operate_type_value),
            'funds': entries, 'total': total, 'warnings': warnings,
            'fetched_at': _stamp(), 'source': 'platform'}


def fund_operations(account_id: str, account_type: str = '') -> Dict[str, Any]:
    """账号类型下平台允许的操作项（官方用它决定流水行给不给详情入口）。"""
    account_type_value, failure = _int_arg('账号类型', account_type)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(client, 'ad_log_operates', account_type_value)
    if failure:
        return failure
    operations = payload.get('result') if isinstance(payload, dict) else None
    warnings = []
    if not isinstance(operations, list):
        operations = None
        warnings.append('平台没有返回可操作项列表（result）')
    return {'account_id': account_id, 'account_type': account_type_value,
            'operations': operations, 'warnings': warnings,
            'fetched_at': _stamp(), 'source': 'platform'}


def _order_detail(account_id: str, order_id: str, method: str, status_key: str,
                  labels: Dict[str, str], kind: str) -> Dict[str, Any]:
    order = _text(order_id).strip()
    if not order:
        return common.fail(f'{kind}订单号不能为空', 'invalid_fields')
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(client, method, order)
    if failure:
        return failure
    detail = payload if isinstance(payload, dict) else None
    warnings = [] if detail is not None else [f'平台没有返回这条{kind}单的详情']
    status = _text(detail.get(status_key)).strip() if detail else ''
    return {'account_id': account_id, 'order_id': order, 'detail': detail,
            'status': status or None, 'status_text': labels.get(status) if status else None,
            'warnings': warnings, 'fetched_at': _stamp(), 'source': 'platform'}


def recharge_detail(account_id: str, order_id: str) -> Dict[str, Any]:
    """充值详情（只读）。状态短标签来自官方枚举表，未知状态给 null。"""
    return _order_detail(account_id, order_id, 'ad_recharge_detail', 'rechargeStatus',
                         RECHARGE_STATUS, '充值')


def withdraw_detail(account_id: str, order_id: str) -> Dict[str, Any]:
    """提现详情（只读）。状态短标签来自官方枚举表，未知状态给 null。"""
    return _order_detail(account_id, order_id, 'ad_withdraw_detail', 'status',
                         WITHDRAW_STATUS, '提现')

