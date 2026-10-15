"""SQLite 访问层。

约定（沿用旧项目，别改）：
- 时间戳统一 `CURRENT_TIMESTAMP`（UTC），不要写本地时间，不要 `datetime('now','localtime')`。
- WAL 模式，允许后台线程与请求线程并发读。
- 所有表结构集中在 SCHEMA，`init_db()` 幂等。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '',
    avatar_url TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0,
    auto_reply INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_credentials (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    cookie TEXT DEFAULT '',
    xianyu_user_id TEXT DEFAULT '',
    login_state TEXT NOT NULL DEFAULT 'unknown',
    relogin_reason TEXT DEFAULT '',
    last_login_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    price REAL DEFAULT 0,
    original_price REAL DEFAULT 0,
    stock INTEGER DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unknown',
    category_id TEXT DEFAULT '',
    images TEXT DEFAULT '[]',
    description TEXT DEFAULT '',
    ai_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    synced_at TIMESTAMP,
    UNIQUE(account_id, item_id)
);

CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key TEXT DEFAULT '',
    default_model TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_roles (
    role TEXT PRIMARY KEY,
    provider_id INTEGER,
    model TEXT DEFAULT '',
    temperature REAL NOT NULL DEFAULT 0.7,
    system_prompt TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    account_id TEXT DEFAULT '',
    item_id TEXT DEFAULT '',
    name TEXT DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    peer_user_id TEXT NOT NULL,
    peer_nickname TEXT DEFAULT '',
    item_id TEXT DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'auto',
    agent_role TEXT NOT NULL DEFAULT 'item',
    unread INTEGER NOT NULL DEFAULT 0,
    last_message_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, peer_user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    sender TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    msg_type TEXT NOT NULL DEFAULT 'text',
    item_id TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    sent_ok INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER,
    role TEXT NOT NULL,
    provider_id INTEGER,
    model TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    order_id TEXT NOT NULL UNIQUE,
    item_id TEXT DEFAULT '',
    buyer_id TEXT DEFAULT '',
    amount REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown',
    raw TEXT DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    company TEXT DEFAULT '',
    tracking_no TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_state (
    account_id TEXT PRIMARY KEY,
    consecutive_hits INTEGER NOT NULL DEFAULT 0,
    blocked_until REAL NOT NULL DEFAULT 0,
    last_hit_at REAL NOT NULL DEFAULT 0,
    verification_type TEXT DEFAULT '',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    item_id TEXT DEFAULT '',
    action TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    detail TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 商品数据的后台缓存（第七批）：平台这个接口只能按商品逐个查，页面点一下就是几十连击
-- mtop、很容易把账号打到风控。所以改成后台慢慢拉，拉到的存这里，页面只读缓存。
-- 拿不到的指标存 NULL（**不许存 0**）：前端显示「-」，0 和「没拿到」是两件事。
CREATE TABLE IF NOT EXISTS item_stats_cache (
    account_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    exposure REAL,
    visits REAL,
    visitors REAL,
    wants REAL,
    orders REAL,
    buyers REAL,
    paid_orders REAL,
    source TEXT NOT NULL DEFAULT 'platform',
    error TEXT DEFAULT '',
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_item_stats_cache_account ON item_stats_cache(account_id, fetched_at);

-- 主管 AI 的对话（第七批）：以前只存在浏览器内存里，刷新就没了。落库后可以回看、
-- 可以查「这次回答用的哪个模型、花了多久、调了哪些工具」。
CREATE TABLE IF NOT EXISTS agent_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread TEXT NOT NULL DEFAULT 'master',
    account_id TEXT DEFAULT '',
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '[]',
    provider_id INTEGER,
    model TEXT DEFAULT '',
    latency_ms INTEGER DEFAULT 0,
    tokens INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_chat_thread ON agent_chat_messages(thread, id);

-- 写操作幂等键（第 59 批）：请求带 `Idempotency-Key` 时，同一个键 + 同一个请求指纹
-- 只打平台一次；重复请求回放上次的响应（响应里带 `idempotent_replay`）。
-- `fingerprint` 是「账号 + 动作 + 载荷」的 sha256：同一个键配不同载荷算**冲突**，不是重放。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    response TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """返回当前线程的连接（自动建目录、开 WAL、开外键）。"""
    config.ensure_dirs()
    conn = sqlite3.connect(db_path or str(config.DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def get_conn() -> sqlite3.Connection:
    """返回当前线程的连接。

    连接是按线程缓存的，所以「换库路径」必须对所有线程都生效：
    共享事件循环线程（`services.common.shared_loop` 的 `xywb-asyncio`）活得很久，
    只重置当前线程的连接不够——它会把上一个数据目录的连接一直用下去，
    于是编排层读的是旧库（表现为「会话不存在」）。这里比对路径，对不上就重连。
    """
    path = str(config.DB_PATH)
    conn = getattr(_local, 'conn', None)
    if conn is not None and getattr(_local, 'db_path', None) != path:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        conn = None
    if conn is None:
        conn = connect()
        _local.conn = conn
        _local.db_path = path
    return conn


@contextmanager
def tx():
    """事务上下文：成功提交，异常回滚。"""
    conn = get_conn()
    conn.execute('BEGIN')
    try:
        yield conn
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise


def query(sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    return list(get_conn().execute(sql, tuple(params)))


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    cur = get_conn().execute(sql, tuple(params))
    return cur.lastrowid or cur.rowcount


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    get_conn().executemany(sql, [tuple(x) for x in seq])


def reset_connection() -> None:
    """丢弃当前线程的连接（测试里换库路径后必须调用）。"""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
        _local.conn = None
        _local.db_path = None


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def init_db() -> None:
    """建表 + 灌默认设置（幂等）。"""
    config.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for key, value in config.DEFAULT_SETTINGS.items():
            conn.execute(
                'INSERT INTO settings(key, value) VALUES(?, ?) '
                'ON CONFLICT(key) DO NOTHING',
                (key, value),
            )
    finally:
        conn.close()
