"""风控服务：熔断状态、风控事件、冷却阶梯、自动滑块、清除冷却。

**清除冷却的硬规则**（任务卡与 CONTRACTS §3 都写了）：只有人工验证成功后才允许清。
这里把「验证成功」落成两个可判定的事实：
1. 调用方显式传 `verified=True`（例如验证窗口回调里确认通过）；
2. 或 `risk_state.verification_type` 非空 —— 协议层 `captcha` 完成后写下的验证凭证。

两者都没有时返回 `{"success": false, "code": "verification_required"}`（HTTP 409），
并把这次尝试记进 `risk_events`，避免出现「冷却被随手清掉」这种线上事故。

冷却阶梯取自 settings `risk.cooldown_steps`（默认 `60,180,300,600,1200` 秒），
与旧项目 `utils/risk_control.py` 的实测值一致；自动滑块默认**关**
（settings `risk.auto_slider`，旧项目实测 37 次 0 成功）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..core import db
from . import common

DEFAULT_STEPS = [60, 180, 300, 600, 1200]
SETTING_AUTO_SLIDER = 'risk.auto_slider'
SETTING_COOLDOWN_STEPS = 'risk.cooldown_steps'
SETTING_REFRESH_INTERVAL = 'risk.refresh_interval_seconds'


# --------------------------------------------------------------------------- 冷却阶梯


def cooldown_steps() -> List[int]:
    """冷却阶梯（秒）。解析失败时回落到默认值，绝不抛异常给调用方。"""
    raw = common.get_setting(SETTING_COOLDOWN_STEPS, '')
    steps: List[int] = []
    for chunk in (raw or '').split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(float(chunk))
        except ValueError:
            continue
        if value > 0:
            steps.append(value)
    return steps or list(DEFAULT_STEPS)


def cooldown_for(hits: int) -> int:
    """第 hits 次命中对应的冷却秒数（超出阶梯取最后一档）。"""
    steps = cooldown_steps()
    index = max(0, min(int(hits or 1) - 1, len(steps) - 1))
    return steps[index]


def _row_to_state(row: Any) -> Dict[str, Any]:
    state = dict(row)
    now = time.time()
    blocked_until = float(state.get('blocked_until') or 0)
    state['blocked'] = blocked_until > now
    state['remaining_seconds'] = int(max(0, blocked_until - now))
    state['last_hit_at'] = float(state.get('last_hit_at') or 0)
    state['consecutive_hits'] = int(state.get('consecutive_hits') or 0)
    return state


def _ensure_state(account_id: str) -> Any:
    row = db.query_one('SELECT * FROM risk_state WHERE account_id = ?', (account_id,))
    if row is None:
        db.execute('INSERT INTO risk_state(account_id) VALUES(?)', (account_id,))
        row = db.query_one('SELECT * FROM risk_state WHERE account_id = ?', (account_id,))
    return row


# --------------------------------------------------------------------------- 读


def status(account_id: Optional[str] = None) -> Dict[str, Any]:
    """读 risk_state。给 account_id 返回单个账号，否则返回全部账号 + 汇总。"""
    if account_id:
        state = _row_to_state(_ensure_state(account_id))
        return {'account_id': account_id, 'state': state, 'cooldown_steps': cooldown_steps(),
                'auto_slider': auto_slider_enabled(), 'blocked': state['blocked']}

    rows = db.query('SELECT * FROM risk_state ORDER BY updated_at DESC')
    states = [_row_to_state(r) for r in rows]
    return {
        'states': states,
        'blocked': [s['account_id'] for s in states if s['blocked']],
        'cooldown_steps': cooldown_steps(),
        'auto_slider': auto_slider_enabled(),
        'refresh_interval_seconds': _refresh_interval(),
    }


def events(limit: int = 100, account_id: Optional[str] = None) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 100), 1000))
    if account_id:
        rows = db.query(
            'SELECT * FROM risk_events WHERE account_id = ? ORDER BY id DESC LIMIT ?',
            (account_id, limit),
        )
    else:
        rows = db.query('SELECT * FROM risk_events ORDER BY id DESC LIMIT ?', (limit,))
    return {'events': [dict(r) for r in rows], 'total': len(rows)}


def is_blocked(account_id: str) -> bool:
    """给协议层调用：这个账号当前是否在冷却中。"""
    row = db.query_one('SELECT blocked_until FROM risk_state WHERE account_id = ?', (account_id,))
    if row is None:
        return False
    return float(row['blocked_until'] or 0) > time.time()


def auto_slider_enabled() -> bool:
    return common.truthy(common.get_setting(SETTING_AUTO_SLIDER, 'false'), False)


def _refresh_interval() -> int:
    try:
        return int(float(common.get_setting(SETTING_REFRESH_INTERVAL, '1200') or 1200))
    except ValueError:
        return 1200


def settings() -> Dict[str, Any]:
    return {
        'auto_slider': auto_slider_enabled(),
        'cooldown_steps': cooldown_steps(),
        'refresh_interval_seconds': _refresh_interval(),
    }


# --------------------------------------------------------------------------- 写


def set_auto_slider(enabled: bool) -> Dict[str, Any]:
    """自动滑块开关。默认关；打开只影响协议层的 captcha 策略。"""
    value = common.truthy(enabled, False)
    common.set_setting(SETTING_AUTO_SLIDER, 'true' if value else 'false')
    common.publish('risk.changed', {'auto_slider': value})
    return {'auto_slider': value}


def _parse_steps(value: Any) -> List[int]:
    """把用户给的阶梯（list 或 '60,180'）解析成正整数列表；非法项跳过，不抛异常。"""
    raw_items = value if isinstance(value, (list, tuple)) else str(value).split(',')
    steps: List[int] = []
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        try:
            number = int(float(text))
        except (TypeError, ValueError):
            continue
        if number > 0:
            steps.append(number)
    return steps


def update_settings(fields: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(fields, dict):
        return common.fail('settings 必须是对象', 'bad_request')
    if 'auto_slider' in fields:
        set_auto_slider(fields['auto_slider'])
    if 'cooldown_steps' in fields:
        value = fields['cooldown_steps']
        if not isinstance(value, (list, tuple)) and not str(value).strip():
            steps = cooldown_steps()          # 空值 = 保持当前阶梯
        else:
            steps = _parse_steps(value) or list(DEFAULT_STEPS)
        common.set_setting(SETTING_COOLDOWN_STEPS, ','.join(str(s) for s in steps))
    if 'refresh_interval_seconds' in fields:
        try:
            interval = int(float(fields['refresh_interval_seconds']))
        except (TypeError, ValueError):
            return common.fail('refresh_interval_seconds 必须是数字', 'invalid_fields')
        if interval <= 0:
            return common.fail('refresh_interval_seconds 必须为正数', 'invalid_fields')
        common.set_setting(SETTING_REFRESH_INTERVAL, interval)
    common.publish('risk.changed', {'settings': settings()})
    return settings()


def record_hit(account_id: str, kind: str = 'hit', message: str = '') -> Dict[str, Any]:
    """协议层命中风控时调用：连续命中 +1，按阶梯设置 blocked_until。"""
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    row = _ensure_state(account_id)
    hits = int(row['consecutive_hits'] or 0) + 1
    now = time.time()
    blocked_until = now + cooldown_for(hits)
    db.execute(
        'UPDATE risk_state SET consecutive_hits = ?, blocked_until = ?, last_hit_at = ?, updated_at = CURRENT_TIMESTAMP '
        'WHERE account_id = ?',
        (hits, blocked_until, now, account_id),
    )
    common.add_risk_event(account_id, kind, message or f'第 {hits} 次命中，冷却 {cooldown_for(hits)} 秒')
    common.publish('risk.changed', {'account_id': account_id, 'consecutive_hits': hits, 'blocked_until': blocked_until})
    return {'account_id': account_id, 'consecutive_hits': hits, 'blocked_until': blocked_until,
            'cooldown_seconds': cooldown_for(hits)}


def mark_verified(account_id: str, verification_type: str = 'slider') -> Dict[str, Any]:
    """协议层人工验证成功后调用，写下验证凭证（clear_cooldown 认这个）。"""
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    _ensure_state(account_id)
    db.execute(
        'UPDATE risk_state SET verification_type = ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?',
        (verification_type or 'slider', account_id),
    )
    common.add_risk_event(account_id, 'verified', f'人工验证成功（{verification_type or "slider"}）')
    common.publish('risk.changed', {'account_id': account_id, 'verified': True})
    return {'account_id': account_id, 'verification_type': verification_type or 'slider'}


def clear_cooldown(account_id: str, verified: bool = False, verification_type: str = '') -> Any:
    """清除冷却 —— **只有人工验证成功后才允许**。

    - `verified=True`（调用方确认验证通过），或
    - `risk_state.verification_type` 非空（协议层 captcha 已写下凭证）

    否则返回 `verification_required`（HTTP 409），并记一条 risk_events。
    """
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    row = _ensure_state(account_id)
    stored_type = str(row['verification_type'] or '')
    if not verified and not stored_type:
        common.add_risk_event(account_id, 'clear_refused', '未检测到人工验证成功，拒绝清除冷却')
        common.log_operation(account_id, 'clear_cooldown', '', False, '未检测到人工验证成功')
        return common.fail('未检测到人工验证成功，拒绝清除冷却', 'verification_required')

    db.execute(
        'UPDATE risk_state SET consecutive_hits = 0, blocked_until = 0, last_hit_at = 0, '
        'verification_type = ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?',
        (verification_type or stored_type or 'slider', account_id),
    )
    common.add_risk_event(account_id, 'cleared', '人工验证成功，冷却已清除')
    common.log_operation(account_id, 'clear_cooldown', '', True, {'verification_type': verification_type or stored_type})
    common.publish('risk.changed', {'account_id': account_id, 'cleared': True})
    return {'account_id': account_id, 'cleared': True, 'state': _row_to_state(_ensure_state(account_id))}
