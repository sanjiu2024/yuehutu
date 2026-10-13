"""子账号管理服务：岗位只读 + 新建 / 删除 / 改手机号 / 改岗位 / 改密码。

口径：

- 三步顺序 **确认 → 字段校验 → 建客户端**；改密码/新建多一步「取平台公钥并加密」，
  它排在**建客户端之后**（必须真发一次 `sub.account.sub.pk`），但密码策略校验仍在最前。
- **密码明文只在本机进程内出现**：`encryptedPassword` 由服务端用平台公钥做
  RSAES-PKCS1-v1_5 加密（纯 Python，不引新依赖），日志、返回值、事件里都不带密码。
- 平台没给的字段一律 `None`；平台拒绝走 502 `protocol_error` + `platform_errors`，不碰登录态。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..xianyu import sub_account as proto
from . import common
from .trade import _client, _stamp, _text

CONFIRM_CODE = 'confirmation_required'

WARN_ROLES_MISSING = '平台没有返回岗位列表（响应里没有岗位数组），这不等于「没有岗位」'
WARN_NO_SUB_ID = '平台没有回显新建子账号的 id，请重新拉一次列表确认'

PUBLIC_KEY_FAILED = '平台没有返回密码加密用的公钥（sub.account.sub.pk），这次没有提交任何东西'


def _truthy(value: Any) -> bool:
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
        '这是写操作，会真的改动闲鱼上的子账号，必须显式 confirm=true',
        CONFIRM_CODE, action=action,
    )


def _value_message(value: Any) -> str:
    if isinstance(value, dict):
        message = _text(value.get('message'))
        if message:
            return message
        ret = value.get('ret')
        if isinstance(ret, list) and ret:
            return _text(ret[0])
    return _text(value)


def _platform_failure(value: Any, api: str) -> Dict[str, Any]:
    reason = proto.failure_reason(value)
    return common.fail(
        f'平台接口失败: {reason or "平台没有说明原因"}',
        'protocol_error',
        platform_errors={'api': api, 'message': reason or None},
    )


def _call_api(client: Any, api: str, data: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if status != common.CALL_OK:
        return None, common.call_failed(status, value)
    return value, None


def list_roles(account_id: str) -> Dict[str, Any]:
    """岗位列表（只读）。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = proto.roles_request_of()
    value, failure = _call_api(client, api, data)
    if failure:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, api)
    parsed = proto.parse_roles(value)
    warnings: List[Dict[str, Any]] = []
    if parsed['roles'] is None:
        warnings.append({'code': 'roles_missing', 'message': WARN_ROLES_MISSING})
    payload: Dict[str, Any] = {
        'account_id': account_id,
        'roles': parsed['roles'],
        'total': parsed['total'],
        'warnings': warnings,
        'api': api,
    }
    return _stamp(payload)


def _public_key(client: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    api, data = proto.public_key_request()
    value, failure = _call_api(client, api, data)
    if failure:
        return None, failure
    if not proto.is_success(value):
        return None, _platform_failure(value, api)
    parsed = proto.parse_public_key_response(value)
    if not parsed['public_key']:
        return None, common.fail(PUBLIC_KEY_FAILED, 'protocol_error',
                                 platform_errors={'api': api, 'message': None})
    return parsed, None


def _encrypt(password: Any, parsed: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        return proto.encrypt_password(password, parsed['public_key']), None
    except proto.EncryptionError as exc:
        return None, common.fail(f'密码加密失败：{exc}', 'protocol_error',
                                 platform_errors={'api': proto.SUB_PK_API, 'message': str(exc)})


def _submit(account_id: str, action: str, api: str, data: Dict[str, Any],
            extra: Dict[str, Any], audit: str) -> Dict[str, Any]:
    client, failure = _client(account_id)
    if failure:
        return failure
    value, failure = _call_api(client, api, data)
    if failure:
        common.log_operation(account_id, audit, None, False, _value_message(failure))
        return failure
    if not proto.is_success(value):
        common.log_operation(account_id, audit, None, False, proto.failure_reason(value))
        return _platform_failure(value, api)
    payload: Dict[str, Any] = {'account_id': account_id, 'action': action, 'api': api, 'saved': True}
    payload.update(extra)
    payload.setdefault('warnings', [])
    # 不传 kind='shop'：log_operation 的 action 列取 `kind or action`，传了会盖掉审计名。
    common.log_operation(account_id, audit, None, True, action)
    common.publish('shop.changed', {'account_id': account_id, 'action': action})
    return _stamp(payload)


def create(account_id: str, nick: Any, mobile: Any, role_ids: Any, password: Any,
           custom_name: Any = None, ext: Any = None, confirm: Any = None) -> Dict[str, Any]:
    """新建子账号。密码在本机加密后提交，返回值里不含密码。"""
    problem = _confirm_gate('create', confirm)
    if problem:
        return problem
    try:
        clean_nick = _text(nick).strip()
        if not clean_nick:
            raise ValueError('昵称不能为空')
        clean_mobile = proto.normalize_mobile(mobile)
        clean_roles = proto.normalize_role_ids(role_ids)
        clean_password = proto.check_password(password)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    client, failure = _client(account_id)
    if failure:
        return failure
    parsed, failure = _public_key(client)
    if failure:
        return failure
    encrypted, failure = _encrypt(clean_password, parsed)
    if failure:
        return failure
    try:
        api, data = proto.create_request(
            clean_nick, clean_mobile, clean_roles, encrypted, parsed['crypto_key'],
            custom_name=custom_name, ext=ext, confirm=True,
        )
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call_api(client, api, data)
    if failure:
        common.log_operation(account_id, 'sub_account_create', None, False,
                             _value_message(failure))
        return failure
    if not proto.is_success(value):
        common.log_operation(account_id, 'sub_account_create', None, False,
                             proto.failure_reason(value))
        return _platform_failure(value, api)
    payload = proto.business(value)
    sub_id = payload.get('subId') or payload.get('subAccountId') or payload.get('id')
    warnings: List[Dict[str, Any]] = []
    if sub_id is None:
        warnings.append({'code': 'no_sub_id', 'message': WARN_NO_SUB_ID})
    common.log_operation(account_id, 'sub_account_create', None, True, clean_nick)
    common.publish('shop.changed', {'account_id': account_id, 'action': 'create'})
    result: Dict[str, Any] = {
        'account_id': account_id,
        'action': 'create',
        'api': api,
        'saved': True,
        'nick': clean_nick,
        'mobile': clean_mobile,
        'role_ids': clean_roles,
        'sub_id': sub_id,
        'warnings': warnings,
    }
    return _stamp(result)


def delete(account_id: str, sub_id: Any, confirm: Any = None) -> Dict[str, Any]:
    problem = _confirm_gate('delete', confirm)
    if problem:
        return problem
    try:
        api, data = proto.delete_request(sub_id, confirm=True)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _submit(account_id, 'delete', api, data,
                   {'sub_id': proto.normalize_sub_id(sub_id)}, 'sub_account_delete')


def update_mobile(account_id: str, sub_id: Any, mobile: Any, confirm: Any = None) -> Dict[str, Any]:
    problem = _confirm_gate('update_mobile', confirm)
    if problem:
        return problem
    try:
        api, data = proto.mobile_request(sub_id, mobile, confirm=True)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _submit(account_id, 'update_mobile', api, data,
                   {'sub_id': proto.normalize_sub_id(sub_id), 'mobile': data['mobilePhone']},
                   'sub_account_mobile')


def update_roles(account_id: str, sub_id: Any, role_ids: Any, confirm: Any = None) -> Dict[str, Any]:
    problem = _confirm_gate('update_roles', confirm)
    if problem:
        return problem
    try:
        api, data = proto.roles_request(sub_id, role_ids, confirm=True)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _submit(account_id, 'update_roles', api, data,
                   {'sub_id': proto.normalize_sub_id(sub_id), 'role_ids': data['roleIds']},
                   'sub_account_roles')


def update_password(account_id: str, sub_id: Any, password: Any, confirm: Any = None) -> Dict[str, Any]:
    """改子账号密码。密码只在本机进程内出现，返回值里不含密码。"""
    problem = _confirm_gate('update_password', confirm)
    if problem:
        return problem
    try:
        clean_sub_id = proto.normalize_sub_id(sub_id)
        clean_password = proto.check_password(password)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    client, failure = _client(account_id)
    if failure:
        return failure
    parsed, failure = _public_key(client)
    if failure:
        return failure
    encrypted, failure = _encrypt(clean_password, parsed)
    if failure:
        return failure
    try:
        api, data = proto.password_request(clean_sub_id, encrypted, parsed['crypto_key'], confirm=True)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')
    return _submit(account_id, 'update_password', api, data,
                   {'sub_id': clean_sub_id}, 'sub_account_password')
