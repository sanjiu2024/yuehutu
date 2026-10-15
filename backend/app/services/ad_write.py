"""超级擦亮写服务：投放计划增删改停启 + 活动报名/领奖 + 账户流水操作。

红线（与 ``docs/contracts/ad_write.md`` 一一对应）：

- **不做充值 / 提现下单**：官方 bundle 里没有下单接口名（只有 ``.recharge.query`` /
  ``.withdraw.query`` 两个只读查询），所以 ``recharge`` / ``withdraw`` 在这里返回
  ``unsupported`` —— 绝不编接口名，更不会真调真钱接口。
- 平台没返回的字段一律 ``None``，**不补 0**（金额字段尤其不能瞎补）。
- 平台拒绝（无权限、参数被拒）原样透出为 ``protocol_error``，**无权限 ≠ 掉线**，
  不写 ``need_relogin``；只有协议层认出 ``SESSION_EXPIRED`` 才回写登录态。
- 每个写操作都要 ``confirm=True``，否则 ``confirm_required``，且**一次协议调用都不发**。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from ..core import db
from ..xianyu import ad_write as protocol
from . import common

#: 支持的写动作（顺序即 ``GET /actions`` 的展示顺序）
ACTIONS = tuple(protocol.WRITE_APIS)

#: 明确不做的动作：bundle 里查不到下单接口名。**这是结论，不是 TODO。**
UNSUPPORTED: Dict[str, str] = {
    'recharge': '官方 bundle 里没有充值下单接口（只有 mtop.taobao.idle.ad.bp.account.recharge.query '
                '这个只读查询），不编接口名、不代下单；充值请在闲鱼 App 内完成',
    'withdraw': '官方 bundle 里没有提现下单接口（只有 mtop.taobao.idle.ad.bp.account.withdraw.query '
                '这个只读查询），官方页面提现按钮的文案就是「请进入闲鱼App内操作」；提现请在闲鱼 App 内完成',
}


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """Cookie 缺失或已标记掉线 —— 与 ``services/promotion.py`` 同一套判定。"""
    row = db.query_one('SELECT cookie, login_state FROM account_credentials WHERE account_id = ?', (account_id,))
    if row is None or not _text(row['cookie']).strip():
        return common.fail(f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录',
                           'verification_required', login_state='need_relogin')
    if _text(row['login_state']).strip() == 'need_relogin':
        return common.fail(f'账号 {account_id} 登录态已过期，需要重新扫码登录',
                           'verification_required', login_state='need_relogin')
    return None


def _client(account_id: str):
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


def actions_catalog() -> Dict[str, Any]:
    """写接口目录：哪些能做、对应的 mtop 名是什么、哪些明确不做。"""
    return {
        'actions': [
            {'action': name, 'api': protocol.WRITE_APIS[name]['api'],
             'params': protocol.WRITE_APIS[name]['params'], 'confirm_required': True}
            for name in ACTIONS
        ],
        'unsupported': [
            {'action': name, 'reason': reason, 'confirm_required': True}
            for name, reason in UNSUPPORTED.items()
        ],
    }


def _write(account_id: str, action: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    """所有写操作的唯一出口：确认 -> 白名单 -> 账号 -> 协议层 -> 信封。"""
    problem = common.confirm_required(
        action, confirm, strict=True,
        message='写操作必须显式确认（confirm=true）后才会执行')
    if problem is not None:
        return problem
    if action in UNSUPPORTED:
        return common.fail(UNSUPPORTED[action], 'unsupported')
    if action not in protocol.WRITE_APIS:
        return common.fail(f'不支持的写操作 {action}', 'not_found')
    client, failure = _client(account_id)
    if failure:
        return failure
    body = payload if isinstance(payload, dict) else {}
    # 幂等（第 63 批，§9）：带 `Idempotency-Key` 时同键同载荷只打平台一次，重复请求回放上次
    # 响应（带 `idempotent_replay`）。指纹用**已确认、已过白名单**的 body 算。
    return common.idempotent_call(
        account_id, action, _write_subject(body), body,
        lambda: _write_once(client, account_id, action, body),
    )


def _write_subject(payload: Any) -> str:
    """幂等指纹里的「对象 id」：推广写都是按 campaignId / id 定位一条计划。"""
    if not isinstance(payload, dict):
        return ''
    for key in ('campaignId', 'campaign_id', 'id', 'groupId'):
        value = payload.get(key)
        if value not in (None, ''):
            return _text(value)
    return ''


def _write_once(client: Any, account_id: str, action: str, payload: Any) -> Dict[str, Any]:
    """真正打平台的那一次（幂等回放时不会走到这里）。"""
    status, value = common.call_method(protocol.WriteClient(client), (action,), payload, confirm=True)
    if status == common.CALL_ERROR and isinstance(value, protocol.WriteNotConfirmed):
        return common.fail('协议层拒绝了没有确认的写请求', 'confirm_required')
    failure = common.call_failed(status, value)
    if failure:
        return failure
    return {
        'account_id': account_id,
        'action': action,
        'api': protocol.WRITE_APIS[action]['api'],
        'confirmed': True,
        'result': value,
        'written_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'platform',
    }


def execute(account_id: str, action: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    """HTTP 层入口：按动作名分发。"""
    return _write(account_id, _text(action).strip(), payload, confirm)


# -- 每个动作一个显式函数（服务层 API 稳定，调用方不必记字符串） ------------------


def campaign_edit(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'campaign_edit', payload, confirm)


def campaign_delete(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'campaign_delete', payload, confirm)


def campaign_batch_delete(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'campaign_batch_delete', payload, confirm)


def campaign_pause(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'campaign_pause', payload, confirm)


def campaign_start(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'campaign_start', payload, confirm)


def campaign_start_before(account_id: str, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'campaign_start_before', None, confirm)


def item_campaign_renew(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'item_campaign_renew', payload, confirm)


def activity_apply(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'activity_apply', payload, confirm)


def activity_task_execute(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'activity_task_execute', payload, confirm)


def account_log_operates(account_id: str, payload: Any = None, confirm: Any = False) -> Dict[str, Any]:
    return _write(account_id, 'account_log_operates', payload, confirm)
