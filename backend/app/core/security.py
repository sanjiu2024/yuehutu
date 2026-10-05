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

from loguru import logger

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
    """确保存在管理员，并且**凭据文件里的密码一定和库里的 hash 对得上**。

    返回 (username, password)；password 为空表示这次没有新生成密码。

    自愈逻辑（修 docs/PACKAGING.md 6.5）：历史上跑测试只把 DB_PATH 指到临时目录、
    没改 CRED_PATH，于是临时库里的随机密码被写进真实凭据文件，而真实库里的 hash
    还是旧的 → `POST /api/auth/desktop` 永远 500「管理员账号异常」，用户看到的是
    「窗口开了但没登录」。这里在验不过时**重新生成一对并同时落库 + 落盘**，两边
    自愈；不去信任那个来历不明的文件内容（它可能是别的库的密码）。
    """
    row = db.query_one('SELECT id, username, password_hash FROM users WHERE is_admin = 1 LIMIT 1')
    cred_path = config.CRED_PATH
    if row is not None:
        if cred_path.exists():
            try:
                data = json.loads(cred_path.read_text('utf-8'))
            except (OSError, ValueError):
                data = {}
            username = data.get('username') or row['username']
            password = data.get('password') or ''
            if password and verify_password(password, row['password_hash']):
                return username, ''
        return _reset_admin_password(int(row['id']), row['username'])
    username = 'admin'
    password = secrets.token_urlsafe(9)
    db.execute(
        'INSERT INTO users(username, password_hash, is_admin) VALUES(?, ?, 1)',
        (username, hash_password(password)),
    )
    _write_credentials(username, password)
    return username, password


def _write_credentials(username: str, password: str) -> None:
    config.ensure_dirs()
    config.CRED_PATH.write_text(
        json.dumps({'username': username, 'password': password}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _reset_admin_password(user_id: int, username: str) -> Tuple[str, str]:
    """凭据文件缺失或与库不一致时，重生成一对一致的（库 + 文件同时更新）。"""
    password = secrets.token_urlsafe(9)
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(password), user_id))
    _write_credentials(username, password)
    logger.warning('管理员凭据与数据库不一致，已重置并写回 {}', config.CRED_PATH)
    return username, password


def desktop_user() -> Optional[Dict[str, Any]]:
    """桌面外壳自动登录：确保管理员存在且凭据一致，然后直接返回该用户。

    比「`ensure_admin()` 拿明文密码再 `authenticate()`」稳：后者在「密码与库一致、
    无需重置」时拿不到明文（返回空串）就必然 401/500，正是 PACKAGING.md 6.5 的表象。
    """
    ensure_admin()
    row = db.query_one('SELECT id, username, is_admin FROM users WHERE is_admin = 1 LIMIT 1')
    if row is None:
        return None
    return {'id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])}


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
