"""超级擦亮只读服务：首页、账户余额、日期指标。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ..core import db
from ..xianyu import client as client_mod
from . import common


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
