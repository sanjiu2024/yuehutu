"""账号与 Cookie 的落库读写。

只做「账号 ↔ 凭证 ↔ 风控状态」的持久化，不含业务规则。上层的协议模块调这里，
API 层也调这里，保证只有一处 SQL。

**字段命名的一处偏差要记牢**：task 描述里说「登录态过期置
``runtime_state='need_relogin'``」，但 ``core/db.py`` 的实际 schema 里没有
``runtime_state`` 这一列 —— 账号的登录态在 ``account_credentials.login_state``。
这里按**实际 schema** 写（``login_state``），否则运行时会
``no such column: runtime_state``。已同步给 lead。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..core import db
from . import cookies as cookie_utils

# login_state 的取值。unknown=还没登录过，active=可用，need_relogin=登录态过期。
STATE_UNKNOWN = 'unknown'
STATE_ACTIVE = 'active'
STATE_NEED_RELOGIN = 'need_relogin'


def _now() -> str:
    """UTC 时间戳字符串。

    与 schema 的 ``CURRENT_TIMESTAMP`` 保持同一种格式（UTC，无时区后缀），
    避免同一列里混着两种时间基准 —— 前端按字符串排序时就会错乱。
    """
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

def upsert_account(account_id: str, nickname: str = '', avatar_url: str = '',
                   xianyu_user_id: str = '', cookie: str = '',
                   login_state: str = STATE_ACTIVE, remark: str = '') -> None:
    """写入/更新账号与其凭证。已存在时**不覆盖 enabled/auto_reply**。

    为什么刻意不覆盖这两个开关：登录/重新登录流程会调到这里，如果顺手把
    ``enabled`` 重置成默认值，用户「登录前先停用的账号」会在登录后被自动
    启用，然后立刻开始自动回复 —— 这是用户明确不希望发生的事。
    """
    existing = db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,))
    if existing is None:
        db.execute(
            'INSERT INTO accounts (id, nickname, avatar_url, remark, enabled, auto_reply,'
            ' created_at, updated_at) VALUES (?, ?, ?, ?, 0, 1, ?, ?)',
            (account_id, nickname, avatar_url, remark, _now(), _now()),
        )
    else:
        db.execute(
            'UPDATE accounts SET nickname = ?, avatar_url = ?, updated_at = ? WHERE id = ?',
            (nickname, avatar_url, _now(), account_id),
        )

    cred = db.query_one('SELECT account_id FROM account_credentials WHERE account_id = ?',
                        (account_id,))
    if cred is None:
        db.execute(
            'INSERT INTO account_credentials (account_id, cookie, xianyu_user_id, login_state,'
            ' relogin_reason, last_login_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (account_id, cookie, xianyu_user_id, login_state, '', _now(), _now()),
        )
    else:
        db.execute(
            'UPDATE account_credentials SET cookie = ?, xianyu_user_id = ?, login_state = ?,'
            ' relogin_reason = ?, last_login_at = ?, updated_at = ? WHERE account_id = ?',
            (cookie, xianyu_user_id, login_state, '', _now(), _now(), account_id),
        )


def list_accounts() -> List[Dict[str, Any]]:
    """账号列表。**不返回 cookie 全文**，只返回是否持有与登录态。"""
    rows = db.query(
        'SELECT a.id, a.nickname, a.avatar_url, a.remark, a.enabled, a.auto_reply,'
        ' a.created_at, a.updated_at, c.login_state, c.xianyu_user_id, c.cookie,'
        ' c.relogin_reason'
        ' FROM accounts a LEFT JOIN account_credentials c ON c.account_id = a.id'
        ' ORDER BY a.created_at'
    )
    result = []
    for row in rows:
        item = db.row_to_dict(row)
        cookie = item.pop('cookie', '') or ''
        item['enabled'] = bool(item.get('enabled'))
        item['auto_reply'] = bool(item.get('auto_reply'))
        item['has_cookie'] = bool(cookie)
        item['has_x5sec'] = cookie_utils.has_x5sec(cookie)
        item['login_state'] = item.get('login_state') or STATE_UNKNOWN
        result.append(item)
    return result


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    for item in list_accounts():
        if item['id'] == account_id:
            return item
    return None


def get_cookie(account_id: str) -> str:
    row = db.query_one('SELECT cookie FROM account_credentials WHERE account_id = ?',
                       (account_id,))
    return (row['cookie'] if row else '') or ''


def set_cookie(account_id: str, cookie: str, xianyu_user_id: str = '',
               login_state: str = STATE_ACTIVE) -> None:
    """只更新 Cookie 与登录态（验证成功后调用）。"""
    db.execute(
        'UPDATE account_credentials SET cookie = ?, xianyu_user_id = ?, login_state = ?,'
        ' relogin_reason = ?, last_login_at = ?, updated_at = ? WHERE account_id = ?',
        (cookie, xianyu_user_id, login_state, '', _now(), _now(), account_id),
    )


def set_login_state(account_id: str, state: str, reason: str = '') -> None:
    """置登录态。登录态过期走这里 —— **不是风控**，不动 risk_state。"""
    db.execute(
        'UPDATE account_credentials SET login_state = ?, relogin_reason = ?, updated_at = ?'
        ' WHERE account_id = ?',
        (state, reason, _now(), account_id),
    )


def mark_need_relogin(account_id: str, reason: str = '') -> None:
    set_login_state(account_id, STATE_NEED_RELOGIN, reason)


def delete_account(account_id: str) -> bool:
    """删账号。凭证靠外键 ON DELETE CASCADE 一起删。

    风控状态/事件是独立表、没有外键，必须显式清 —— 否则同一账号重新登录后
    会继承上一轮的风控冷却，用户会觉得「刚重登就被限流」。
    """
    existing = db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,))
    if existing is None:
        return False
    with db.tx():
        db.execute('DELETE FROM account_credentials WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM risk_state WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM risk_events WHERE account_id = ?', (account_id,))
        db.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
    return True


def set_enabled(account_id: str, enabled: bool) -> None:
    db.execute('UPDATE accounts SET enabled = ?, updated_at = ? WHERE id = ?',
               (1 if enabled else 0, _now(), account_id))


def update_account(account_id: str, enabled=None, auto_reply=None, remark=None) -> None:
    """局部更新账号开关与备注（契约 §3 的 ``PATCH /api/accounts/{id}``）。

    只写传进来的字段：前端一次可能只改一个开关，用整行 UPDATE 会把另外两个
    字段覆盖成调用方没传的值（例如把 ``auto_reply`` 从 0 悄悄改回 1）。
    """
    fields, params = [], []
    if enabled is not None:
        fields.append('enabled = ?')
        params.append(1 if enabled else 0)
    if auto_reply is not None:
        fields.append('auto_reply = ?')
        params.append(1 if auto_reply else 0)
    if remark is not None:
        fields.append('remark = ?')
        params.append(str(remark))
    if not fields:
        return
    fields.append('updated_at = ?')
    params.extend([_now(), account_id])
    db.execute(f'UPDATE accounts SET {", ".join(fields)} WHERE id = ?', tuple(params))


# ---------------------------------------------------------------------------
# 风控状态
# ---------------------------------------------------------------------------

def save_risk_state(account_id: str, consecutive_hits: int, blocked_until: float,
                    last_hit_at: float, verification_type: str = '') -> None:
    """把内存里的熔断状态落库。

    为什么内存里已经有一份还要落库：进程重启后内存计数归零，用户会看到
    「刚重启就又开始撞风控」。落库后启动时能恢复冷却窗口。
    """
    row = db.query_one('SELECT account_id FROM risk_state WHERE account_id = ?',
                       (account_id,))
    if row is None:
        db.execute(
            'INSERT INTO risk_state (account_id, consecutive_hits, blocked_until,'
            ' last_hit_at, verification_type, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
            (account_id, consecutive_hits, blocked_until, last_hit_at,
             verification_type, _now()),
        )
    else:
        db.execute(
            'UPDATE risk_state SET consecutive_hits = ?, blocked_until = ?, last_hit_at = ?,'
            ' verification_type = ?, updated_at = ? WHERE account_id = ?',
            (consecutive_hits, blocked_until, last_hit_at, verification_type,
             _now(), account_id),
        )


def load_risk_state(account_id: str) -> Optional[Dict[str, Any]]:
    row = db.query_one('SELECT * FROM risk_state WHERE account_id = ?', (account_id,))
    return db.row_to_dict(row) if row else None


def log_risk_event(account_id: str, kind: str, message: str = '') -> None:
    db.execute('INSERT INTO risk_events (account_id, kind, message) VALUES (?, ?, ?)',
               (account_id, kind, str(message)[:2000]))


def recent_risk_events(account_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = db.query(
        'SELECT * FROM risk_events WHERE account_id = ? ORDER BY id DESC LIMIT ?',
        (account_id, max(1, int(limit))),
    )
    return db.rows_to_dicts(rows)


def log_operation(account_id: str, action: str, success: bool = True,
                  detail: Any = '', item_id: str = '') -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    db.execute(
        'INSERT INTO operation_logs (account_id, item_id, action, success, detail)'
        ' VALUES (?, ?, ?, ?, ?)',
        (account_id, item_id, action, 1 if success else 0, str(detail)[:4000]),
    )
