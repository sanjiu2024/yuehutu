"""本地账号、会话与密钥。

设计要点（沿用旧项目踩过的坑）：
- 会话**落库**（`sessions` 表），内存只做缓存 → 重启不掉登录。
- 空闲 24h 滑动续期、绝对 30 天上限；每次 touch 间隔小于 5 分钟不写库，省 IO。
- 首次运行自动创建 admin；密码写 `data/desktop_credentials.json`，桌面外壳用它自动登录。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from . import config, db

TOUCH_INTERVAL = 300.0
_cache: Dict[str, Dict[str, Any]] = {}


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 120_000)
    return f'pbkdf2_sha256${salt}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, _ = stored.split('$', 2)
    except ValueError:
        return False
    if algo != 'pbkdf2_sha256':
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def ensure_admin() -> Tuple[str, str]:
    """确保存在管理员。返回 (username, password)（仅在首次创建时生成随机密码）。"""
    row = db.query_one('SELECT id, username FROM users WHERE is_admin = 1 LIMIT 1')
    cred_path = config.CRED_PATH
    if row is not None:
        if cred_path.exists():
            try:
                data = json.loads(cred_path.read_text('utf-8'))
                return data.get('username', row['username']), data.get('password', '')
            except (OSError, ValueError):
                pass
        return row['username'], ''
    username = 'admin'
    password = secrets.token_urlsafe(9)
    db.execute(
        'INSERT INTO users(username, password_hash, is_admin) VALUES(?, ?, 1)',
        (username, hash_password(password)),
    )
    config.ensure_dirs()
    cred_path.write_text(
        json.dumps({'username': username, 'password': password}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return username, password


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    row = db.query_one('SELECT * FROM users WHERE username = ?', (username,))
    if row is None or not verify_password(password, row['password_hash']):
        return None
    db.execute('UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE id = ?', (row['id'],))
    return {'id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])}


def issue_token(user_id: int, token: Optional[str] = None) -> str:
    token = token or secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        'INSERT INTO sessions(token, user_id, created_at, last_seen, expires_at) VALUES(?,?,?,?,?)',
        (token, user_id, now, now, now + config.SESSION_ABSOLUTE_SECONDS),
    )
    _cache[token] = {'user_id': user_id, 'last_seen': now, 'expires_at': now + config.SESSION_ABSOLUTE_SECONDS}
    return token


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """校验会话；内存 miss 时回库兜底，并按滑动窗口续期。"""
    if not token:
        return None
    now = time.time()
    entry = _cache.get(token)
    if entry is None:
        row = db.query_one('SELECT * FROM sessions WHERE token = ?', (token,))
        if row is None:
            return None
        entry = {'user_id': row['user_id'], 'last_seen': row['last_seen'], 'expires_at': row['expires_at']}
        _cache[token] = entry
    if now > entry['expires_at'] or now - entry['last_seen'] > config.SESSION_IDLE_SECONDS:
        delete_token(token)
        return None
    if now - entry['last_seen'] > TOUCH_INTERVAL:
        entry['last_seen'] = now
        db.execute('UPDATE sessions SET last_seen = ? WHERE token = ?', (now, token))
    user = db.query_one('SELECT id, username, is_admin FROM users WHERE id = ?', (entry['user_id'],))
    if user is None:
        delete_token(token)
        return None
    return {'id': user['id'], 'username': user['username'], 'is_admin': bool(user['is_admin'])}


def delete_token(token: str) -> None:
    _cache.pop(token, None)
    db.execute('DELETE FROM sessions WHERE token = ?', (token,))


def delete_user_tokens(user_id: int) -> None:
    for token, entry in list(_cache.items()):
        if entry.get('user_id') == user_id:
            _cache.pop(token, None)
    db.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))


def purge_expired() -> int:
    now = time.time()
    return db.execute(
        'DELETE FROM sessions WHERE expires_at < ? OR last_seen < ?',
        (now, now - config.SESSION_IDLE_SECONDS),
    )


def local_secret() -> str:
    """进程间共享的本地密钥（MCP 插件用它换取 token）。"""
    config.ensure_dirs()
    if config.SECRET_PATH.exists():
        secret = config.SECRET_PATH.read_text('utf-8').strip()
        if secret:
            return secret
    secret = secrets.token_urlsafe(32)
    config.SECRET_PATH.write_text(secret, encoding='utf-8')
    return secret


def write_mcp_token(token: str) -> None:
    config.ensure_dirs()
    config.MCP_TOKEN_PATH.write_text(token, encoding='utf-8')


def refresh_mcp_token() -> str:
    """每次后端启动换一个长期 MCP token 并写盘（MCP 插件读这个文件，不需要走登录流程）。

    单独一条 sessions 记录（user_id=admin），因此和网页登录互不干扰：网页登出不会
    让 DSH 里的插件掉线，反之亦然。
    """
    admin = db.query_one('SELECT id FROM users ORDER BY id LIMIT 1')
    if admin is None:
        return ''
    token = secrets.token_urlsafe(32)
    issue_token(int(admin['id']), token=token)
    write_mcp_token(token)
    return token
