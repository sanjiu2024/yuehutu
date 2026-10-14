"""IM 工具族服务层：会话搜索 / 商品搜索 / 清未读。

口径与其它写一致：**确认 → 字段校验 → 建客户端**（确认最前、字段校验在建客户端之前）。
读接口平台没返回的字段一律 ``None``（前端显示「-」），不补 ``0`` / ``[]``。

清未读的诚实口径：官方这条接口**发完就不管**（不看响应），所以我们按「信封成功 = 平台已收到」
处理并同步把本地未读清零；平台明确回 ``success: false`` 时走 502 且**不动本地**；
平台没回回执字段时给 warning ``im_unread_unconfirmed``（不谎报「平台已确认」）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..core import db
from ..xianyu import im_tools as proto
from . import common
from .im_actions import (
    CONFIRM_CODE,
    MISSING_CID_CODE,
    MISSING_CID_MESSAGE,
    _call,
    _confirm_gate,
    _missing,
    _platform_failure,
    _resolve_session,
    _stamp,
    _text,
    _truthy,
)

TOPIC = 'chats.changed'

WARN_SESSIONS_MISSING = {
    'code': 'im_sessions_missing',
    'message': '平台没有返回会话列表字段（可能改了键名），这一页暂时显示不出来，请点「刷新」重试。',
}
WARN_TOTAL_MISSING = {
    'code': 'im_session_total_missing',
    'message': '平台没有返回会话总数，界面上不显示条数（不拿当前页条数冒充总数）。',
}
WARN_ITEMS_MISSING = {
    'code': 'im_items_missing',
    'message': '平台没有返回商品列表字段（可能改了键名），这一页暂时显示不出来，请换个关键词再试。',
}
WARN_UNCONFIRMED = {
    'code': 'im_unread_unconfirmed',
    'message': '平台这条接口没有回执字段（官方发完也不看响应），请求已被接受，但平台没有明确确认。',
}


def session_search(account_id: str, keyword: str = '', page: Any = 1,
                   size: Any = proto.PAGE_SIZE_DEFAULT) -> Any:
    """会话搜索（只读）。关键词为空也允许（官方搜索框允许空提交）。"""
    account_id = _text(account_id)
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    try:
        api, data, version = proto.session_search_request(keyword, page, size)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    error = proto.envelope_error(value)
    if error:
        return _platform_failure(value, api, error)
    parsed = proto.parse_session_search(value)

    warnings = []
    if not parsed['found']:
        warnings.append(WARN_SESSIONS_MISSING)
    elif parsed['sessions'] is None:
        warnings.append(WARN_SESSIONS_MISSING)
    if parsed['total'] is None:
        warnings.append(WARN_TOTAL_MISSING)

    return _stamp({
        'account_id': account_id,
        'keyword': _text(keyword),
        'page': data['pageNumber'],
        'size': data['pageSize'],
        'sessions': parsed['sessions'],
        'total': parsed['total'],
        'cur_page': parsed['cur_page'],
        'api': api,
        'warnings': warnings,
    })


def item_search(account_id: str, session_id: str = '', conversation_id: str = '',
                role: str = 'own', keyword: str = '', page: Any = 1,
                size: Any = proto.PAGE_SIZE_DEFAULT) -> Any:
    """商品搜索（只读）：官方 IM 页「我的宝贝 / TA的宝贝」抽屉。

    ``session_id`` 是平台 cid；只给本地会话行 id 时用 ``conversation_id`` 换（换不到 400
    ``missing_cid``，与 IM 其它接口同口径）。
    """
    account_id = _text(account_id)
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    resolved, failure = _resolve_session(account_id, session_id, conversation_id)
    if failure is not None:
        return failure
    try:
        api, data, version = proto.item_search_request(resolved, role, keyword, page, size)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    error = proto.envelope_error(value)
    if error:
        return _platform_failure(value, api, error)
    parsed = proto.parse_item_search(value)

    warnings = []
    if not parsed['found'] or parsed['items'] is None:
        warnings.append(WARN_ITEMS_MISSING)

    return _stamp({
        'account_id': account_id,
        'session_id': resolved,
        'role': data['searchItemRole'],
        'keyword': data['queryWord'],
        'page': data['pageNumber'],
        'size': data['pageSize'],
        'items': parsed['items'],
        'has_more': parsed['has_more'],
        'next_page': parsed['next_page'],
        'api': api,
        'warnings': warnings,
    })


def clear_unread(account_id: str, kind: str = 'session', confirm: Any = None) -> Any:
    """清未读（写）：``kind='session'`` 清买家会话、``'system'`` 清系统通知。

    成功后把本地 ``conversations.unread`` 清零（``kind='system'`` 不动本地：我们没把系统通知
    建模成会话行），并播 ``chats.changed`` 让界面刷新。
    """
    account_id = _text(account_id)
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    failure = _confirm_gate('清未读', confirm)
    if failure is not None:
        return failure
    try:
        resolved_kind = proto._unread_kind(kind)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    action = 'clear_session_unread' if resolved_kind == 'session' else 'clear_systems_unread'
    api, data, version = proto.request_of(action, confirm=True)

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    error = proto.envelope_error(value)
    if error:
        return _platform_failure(value, api, error)
    parsed = proto.parse_clean(value)
    if parsed['platform_success'] is False:
        return _platform_failure(value, api, parsed['platform_message'] or '平台返回 success=false')

    cleared: Optional[int] = None
    if resolved_kind == 'session':
        with db.get_conn() as conn:
            cursor = conn.execute('UPDATE conversations SET unread = 0 WHERE account_id = ? AND unread > 0',
                                  (account_id,))
            cleared = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None

    warnings = []
    if parsed['platform_success'] is None:
        warnings.append(WARN_UNCONFIRMED)

    result = _stamp({
        'account_id': account_id,
        'action': action,
        'kind': resolved_kind,
        'saved': True,
        'platform_success': parsed['platform_success'],
        'cleared': cleared,
        'api': api,
        'warnings': warnings,
    })
    common.log_operation(account_id, 'im_unread_clean', '', True,
                         {'kind': resolved_kind, 'cleared': cleared, 'api': api})
    common.publish(TOPIC, {'account_id': account_id, 'kind': resolved_kind, 'cleared': cleared})
    return result
