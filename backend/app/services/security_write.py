# -*- coding: utf-8 -*-
"""安全中心**写**服务：申诉表单获取 / 提交申诉 / 催办。

红线（与 `docs/contracts/security_write.md` 一致）：
- 三个写动作都要 `confirm=True`，没确认直接返回 `confirmation_required`，**不发任何平台请求**；
- 平台没返回的字段一律 `None`，不用 `0`/`''`/`{}` 顶替；
- 失败信封原样透出（`platform_errors.explain`）；
- **无权限 ≠ 掉线**：这里不判定登录态，`need_relogin` 只由 `common._is_session_expired` 决定。

账号/凭据检查复用 `services/trade._client`（同包内唯一一份判定，避免两套漂移）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from ..xianyu import security_write as proto
from . import common
from .trade import _client, _stamp, _text

logger = logging.getLogger(__name__)

__all__ = [
    'CONFIRM_CODE',
    'appeal_form', 'appeal_submit', 'appeal_submit_cro', 'appeal_urge',
]

#: 未确认时返回的错误码（与 `services/trade_write.py` 保持一致）。
CONFIRM_CODE = 'confirmation_required'


def _confirm_required(action: str) -> Dict[str, Any]:
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交',
        CONFIRM_CODE,
    )


def _missing(*pairs: Tuple[str, Any]) -> Optional[Dict[str, Any]]:
    """必填校验：空串/纯空白/None 都算缺失。"""
    for label, value in pairs:
        if value is None or (isinstance(value, str) and not value.strip()):
            return common.fail(f'{label}不能为空', 'invalid_fields')
    return None


def _call(client: Any, api: str, data: Dict[str, Any],
          version: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """调一次 mtop（带站点 CODE 头）。

    安全中心这批接口不带 `idle_site_biz_code: COMMONPRO` 时服务端会回
    `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` / `data.errorMsg="站点CODE不能为空"`
    （真机事实）。头从客户端借，拿不到就 `None`（不补头、也不假装）。
    """
    headers = proto.site_headers(client)
    status, value = common.call_method(client, ('mtop',), api, data, v=version,
                                       extra_headers=headers)
    if status != common.CALL_OK:
        failure = common.call_failed(status, value) or common.fail('协议层调用失败', 'protocol_error')
        failure.setdefault('platform_errors', {
            'api': api,
            'explain': _text(value) or '协议层未提供该方法',
        })
        return value, failure
    explain = proto.envelope_error(value)
    if explain:
        logger.warning('安全中心写接口返回失败信封 api=%s explain=%s', api, explain)
        return value, common.fail(
            f'平台接口失败: {explain}',
            'protocol_error',
            platform_errors={'api': api, 'explain': explain, 'message': proto.envelope_message(value)},
        )
    return value, None


def _submit(account_id: str, action: str, audit_id: str,
            builder: Callable[[], Tuple[str, Dict[str, Any]]],
            parse: Callable[[Any], Dict[str, Any]],
            version: str,
            business: Optional[Callable[[Any], str]] = None) -> Dict[str, Any]:
    """写请求公共流程：取客户端 → 构造请求 → 调用 → 解析 → 记操作日志。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    try:
        api, data = builder()
    except (proto.WriteNotConfirmed, ValueError) as exc:
        return common.fail(
            str(exc),
            CONFIRM_CODE if isinstance(exc, proto.WriteNotConfirmed) else 'invalid_fields',
        )
    raw, failure = _call(client, api, data, version)
    if failure:
        common.log_operation(account_id, action, audit_id, False, failure.get('message') or '')
        return failure
    if business is not None:
        reason = business(raw)
        if reason:
            common.log_operation(account_id, action, audit_id, False, reason)
            return common.fail(
                f'平台接口失败: {reason}',
                'protocol_error',
                platform_errors={'api': api, 'explain': reason},
            )
    payload = parse(raw)
    payload['account_id'] = account_id
    payload['api'] = api
    payload['action'] = action
    common.log_operation(account_id, action, audit_id, True, data)
    return _stamp(payload)


def _urge_failure(raw: Any) -> str:
    """催办的官方判据是 `data` 为真（没有具体字段名）。平台没回 `data` 就是没成功。"""
    return '' if proto.parse_appeal_urge(raw).get('urged') else '平台没有返回催办结果'


# --------------------------------------------------------------------------- 对外接口


def appeal_form(account_id: str, form_id: Any) -> Dict[str, Any]:
    """获取申诉表单（**只读**，不带确认闸门）。

    返回 `form_data`：官方读 `data.formData`，平台没给就是 `None`。
    """
    failure = _missing(('表单 id', form_id))
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    api, data = proto.appeal_form_request(form_id)
    raw, failure = _call(client, api, data, proto.APPEAL_VERSION)
    if failure:
        common.log_operation(account_id, 'security_appeal_form', _text(form_id), False,
                             failure.get('message') or '')
        return failure
    payload = proto.parse_appeal_form(raw)
    payload['account_id'] = account_id
    payload['api'] = api
    payload['action'] = 'security_appeal_form'
    common.log_operation(account_id, 'security_appeal_form', _text(form_id), True, data)
    return _stamp(payload)


def appeal_submit(account_id: str, parameter: Any, *, confirm: bool = False) -> Dict[str, Any]:
    """提交申诉（安全中心 pc 弹窗）。

    `parameter` 是表单值：`dict` 会序列化成 JSON 字符串，`str` 原样透传。
    """
    if not confirm:
        return _confirm_required('提交申诉')
    failure = _missing(('申诉表单内容', parameter))
    if failure:
        return failure
    return _submit(
        account_id, 'security_appeal_submit', '',
        lambda: proto.appeal_submit_request(parameter, confirm=True),
        proto.parse_appeal_submit, proto.APPEAL_VERSION,
        business=proto.appeal_submit_failure,
    )


def appeal_submit_cro(account_id: str, punish_id: Any, proof_items: Any, *,
                      confirm: bool = False) -> Dict[str, Any]:
    """提交申诉（复核 cro 流程）：`parameter = JSON.stringify({punishId, proofItems})`。"""
    if not confirm:
        return _confirm_required('提交申诉（复核流程）')
    failure = _missing(('处罚单号', punish_id), ('申诉凭证', proof_items))
    if failure:
        return failure
    return _submit(
        account_id, 'security_appeal_submit_cro', _text(punish_id),
        lambda: proto.appeal_submit_cro_request(punish_id, proof_items, confirm=True),
        proto.parse_appeal_submit, proto.APPEAL_VERSION,
        business=proto.appeal_submit_failure,
    )


def appeal_urge(account_id: str, punish_id: Any, *, confirm: bool = False) -> Dict[str, Any]:
    """催办（加急处理）：`data:{punishId:Number(...)}`。"""
    if not confirm:
        return _confirm_required('申诉催办')
    failure = _missing(('处罚单号', punish_id))
    if failure:
        return failure
    return _submit(
        account_id, 'security_appeal_urge', _text(punish_id),
        lambda: proto.appeal_urge_request(punish_id, confirm=True),
        proto.parse_appeal_urge, proto.APPEAL_VERSION,
        business=_urge_failure,
    )
