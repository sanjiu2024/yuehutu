"""店铺资料服务：只读详情 + 昵称 / 简介 / 头像三条写接口。

口径（与其它服务一致）：

- 三步顺序 **确认 → 字段校验 → 建客户端**。字段校验是纯本地的、免费，而且掉线时
  也该先说「昵称不能少于4个字符」，而不是「去重新扫码」。
- 平台没给的字段一律 `None`（前端显示「-」），**不补 0、不猜**。
- 平台拒绝走 502 `protocol_error` + `platform_errors`，**不碰登录态**（无权限 ≠ 掉线）。
- 写操作成功才落 `operation_logs` 并发 `shop.changed` 事件。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..xianyu import shop_profile as proto
from . import common
from .trade import _client, _stamp, _text

CONFIRM_CODE = 'confirmation_required'

PROFILE_FIELDS = (
    'user_nick',
    'shop_desc',
    'user_avatar',
    'industry',
    'alipay_account',
    'business_type',
    'pro_shop_valid_time',
)

WARN_DETAIL_EMPTY = '平台没有返回店铺资料（响应里没有任何已知字段），这不是「没有店铺」'
WARN_PLATFORM_MESSAGE = '平台没有明确说成功，但也（在没有失败码的情况下）没有说不成功'


def _truthy(value: Any) -> bool:
    """只有 True / 'true' / 1 / '1' 算点头（与 items / item_templates / shop 同口径）。"""
    if value is True:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() == 'true' or value.strip() == '1'
    return False


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    if _truthy(confirm):
        return None
    return common.fail(
        '这是写操作，会真的改动闲鱼上的店铺资料，必须显式 confirm=true',
        CONFIRM_CODE, action=action,
    )


def _platform_failure(value: Any, api: str) -> Dict[str, Any]:
    return common.fail(
        f'平台接口失败: {proto.failure_reason(value) or "平台没有说明原因"}',
        'protocol_error',
        platform_errors={'api': api, 'message': proto.failure_reason(value) or None},
    )


def _value_message(value: Any) -> str:
    """客户端层失败时的文案（平台信封为空时只剩它）。"""
    if isinstance(value, dict):
        message = _text(value.get('message'))
        if message:
            return message
        ret = value.get('ret')
        if isinstance(ret, list) and ret:
            return _text(ret[0])
    return _text(value)


def _read(account_id: str, api: str, data: Dict[str, Any]) -> Any:
    """跑一个只读接口，返回 `(value, failure)`。"""
    client, failure = _client(account_id)
    if failure:
        return None, failure
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if status != common.CALL_OK:
        return None, common.call_failed(status, value)
    return value, None


def get_profile(account_id: str) -> Dict[str, Any]:
    value, failure = _read(account_id, proto.DETAIL_API, {})
    if failure:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, proto.DETAIL_API)
    parsed = proto.parse_profile(value)
    warnings: List[Dict[str, Any]] = []
    if not any(key in parsed for key in ('user_nick', 'shop_desc', 'user_avatar')):
        warnings.append({'code': 'detail_empty', 'message': WARN_DETAIL_EMPTY})
    payload: Dict[str, Any] = {'account_id': account_id}
    for field in PROFILE_FIELDS:
        payload[field] = parsed.get(field)
    payload['raw'] = parsed.get('raw')
    payload['warnings'] = warnings
    payload['api'] = proto.DETAIL_API
    return _stamp(payload)


def _submit(account_id: str, action: str, api: str, data: Dict[str, Any],
            extra: Dict[str, Any], audit: str) -> Dict[str, Any]:
    client, failure = _client(account_id)
    if failure:
        return failure
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if status != common.CALL_OK:
        # 注意：log_operation 的 `action` 列取 `kind or action`，所以这里**不能**传 kind='shop'
        # —— 传了会把审计名覆盖成 'shop'，操作日志里就看不出改的是昵称还是简介。
        common.log_operation(account_id, audit, None, False, _value_message(value))
        return common.call_failed(status, value)
    if not proto.is_success(value):
        common.log_operation(account_id, audit, None, False, proto.failure_reason(value))
        return _platform_failure(value, api)
    payload: Dict[str, Any] = {'account_id': account_id, 'action': action}
    payload.update(extra)
    payload['saved'] = True
    payload['api'] = api
    payload['warnings'] = []
    common.log_operation(account_id, audit, None, True, action)
    common.publish('shop.changed', {'account_id': account_id, 'action': action})
    return _stamp(payload)


def _write(account_id: str, action: str, fields: Dict[str, Any], extra: Dict[str, Any],
           audit: str) -> Dict[str, Any]:
    try:
        api, data = proto.request_of(action, confirm=True, **fields)
    except proto.UnknownApi as exc:
        return common.fail(str(exc), 'invalid_fields')
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _submit(account_id, action, api, data, extra, audit)


def update_nick(account_id: str, nick: Any, confirm: Any = None) -> Dict[str, Any]:
    problem = _confirm_gate('nick', confirm)
    if problem:
        return problem
    return _write(account_id, 'nick', {'nick': nick}, {'nick': _text(nick).strip()}, 'shop_profile_nick')


def update_desc(account_id: str, desc: Any, confirm: Any = None) -> Dict[str, Any]:
    problem = _confirm_gate('desc', confirm)
    if problem:
        return problem
    return _write(account_id, 'desc', {'desc': desc}, {'desc': _text(desc).strip()}, 'shop_profile_desc')


def update_avatar(account_id: str, avatar_url: Any, confirm: Any = None) -> Dict[str, Any]:
    problem = _confirm_gate('avatar', confirm)
    if problem:
        return problem
    return _write(
        account_id, 'avatar', {'avatar_url': avatar_url},
        {'avatar_url': _text(avatar_url).strip()}, 'shop_profile_avatar',
    )
