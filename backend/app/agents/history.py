"""AI 对话落库：`agent_chat_messages` 表的读写。

以前「主管 AI」页的对话只活在浏览器内存里（页面上就写着「对话只保存在本页，刷新即丢失」），
而 `agent_runs` 里的调用记录界面上又看不到 —— 用户问「ai 的会话记录呢」就是这个缺口。

这张表把一整个回合都存下来：用户说了什么、AI 回了什么、调了哪些工具、用的哪个模型、
花了多久、多少 token、失败原因是什么。失败**也要落库**（`status='error'` + `error`），
否则用户看不到「它其实已经改了什么 / 为什么失败」。

    append(thread, role, content, ...) -> int          写一行，返回新行 id
    list_messages(thread='master', limit=50, before_id=None)
                                                        -> {'items', 'total', 'has_more'}
    clear(thread='master') -> int                      删掉该 thread 的全部行，返回删除行数

表结构（`backend/app/core/db.py`）：`id, thread, account_id, role, content, tool_calls,
provider_id, model, latency_ms, tokens, status, error, created_at`；`tool_calls` 存 JSON 字符串。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..core import db

log = logging.getLogger(__name__)

DEFAULT_THREAD = 'master'
MAX_LIMIT = 500


def _dump_tool_calls(tool_calls: Any) -> str:
    """工具调用统一存 JSON 字符串；拿不到就存 '[]'，不编。

    **必须是 JSON 数组**：`'{}'` / `'null'` / `'123'` 都是合法 JSON 但读回来会被
    `_row_to_message` 归一成 `[]` —— 写进去的是「有工具调用」，读出来是「没有」，
    写读不对称。这类值一律告警并按空数组存（复审 L1）。
    """
    if tool_calls is None or tool_calls == '':
        return '[]'
    if isinstance(tool_calls, str):
        try:
            parsed = json.loads(tool_calls)
        except (TypeError, ValueError):
            log.warning('tool_calls 不是合法 JSON，按空数组存：%.120s', tool_calls)
            return '[]'
        if not isinstance(parsed, list):
            log.warning('tool_calls 是合法 JSON 但不是数组，按空数组存：%.120s', tool_calls)
            return '[]'
        return tool_calls
    if isinstance(tool_calls, (list, tuple)):
        try:
            return json.dumps(list(tool_calls), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            log.warning('tool_calls 无法序列化，按空数组存：%r', type(tool_calls))
            return '[]'
    log.warning('tool_calls 不是数组也不是字符串，按空数组存：%r', type(tool_calls))
    return '[]'


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _row_to_message(row: Any) -> Dict[str, Any]:
    item = db.row_to_dict(row) or {}
    try:
        calls = json.loads(item.get('tool_calls') or '[]')
    except (TypeError, ValueError):
        calls = []
    item['tool_calls'] = calls if isinstance(calls, list) else []
    return item


def append(
    thread: str = DEFAULT_THREAD,
    role: str = 'user',
    content: str = '',
    *,
    account_id: str = '',
    tool_calls: Any = None,
    provider_id: Optional[int] = None,
    model: str = '',
    latency_ms: int = 0,
    tokens: int = 0,
    status: str = 'ok',
    error: str = '',
) -> int:
    """写一行对话，返回新行 id。

    可选字段拿不到就留空/0（`provider_id` 存 NULL），不要编造。
    """
    pid: Optional[int]
    try:
        pid = None if provider_id in (None, '') else int(provider_id)
    except (TypeError, ValueError):
        pid = None
    return db.execute(
        'INSERT INTO agent_chat_messages(thread, account_id, role, content, tool_calls, '
        'provider_id, model, latency_ms, tokens, status, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (
            str(thread or DEFAULT_THREAD),
            str(account_id or ''),
            str(role or 'user'),
            str(content or ''),
            _dump_tool_calls(tool_calls),
            pid,
            str(model or ''),
            _as_int(latency_ms),
            _as_int(tokens),
            str(status or 'ok'),
            str(error or ''),
        ),
    )


def count(thread: str = DEFAULT_THREAD) -> int:
    """该 thread 的总行数。"""
    row = db.query_one('SELECT COUNT(*) AS n FROM agent_chat_messages WHERE thread = ?',
                       (str(thread or DEFAULT_THREAD),))
    return int(row['n']) if row is not None else 0


def list_messages(
    thread: str = DEFAULT_THREAD,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> Dict[str, Any]:
    """按 id **升序**返回一页（最新的在最后），供聊天界面直接追加渲染。

    `before_id` 给「向上翻历史」用：只取 id 比它小的行。`total` 是该 thread 的总数
    （不随 `before_id` 变），`has_more` 表示这一页之前还有没有更早的行。
    """
    thread = str(thread or DEFAULT_THREAD)
    # `limit=0` 不能被 `0 or 50` 吃成默认 50（复审 M4）：下限是 1 条。
    raw = 50 if limit is None else _as_int(limit, 50)
    size = max(1, min(raw, MAX_LIMIT))
    sql = 'SELECT * FROM agent_chat_messages WHERE thread = ?'
    params: List[Any] = [thread]
    if before_id not in (None, ''):
        cursor = _as_int(before_id, 0)
        if cursor > 0:
            sql += ' AND id < ?'
            params.append(cursor)
        else:
            # `before_id='abc'` 过去会被 `_as_int` 变成 0，于是 `id < 0` 静默返回空页 ——
            # 用户看到的是「没有更早的消息了」，而不是「你这个游标传错了」（复审 L2）。
            log.warning('before_id 不是正整数，忽略这个游标：%r', before_id)
    # 先倒序取「最近的 size 条」，再翻回升序 —— 这样 limit 卡的是最新的一页。
    rows = list(db.query(sql + ' ORDER BY id DESC LIMIT ?', params + [size]))
    items = [_row_to_message(row) for row in reversed(rows)]
    has_more = False
    if items:
        has_more = db.query_one(
            'SELECT 1 FROM agent_chat_messages WHERE thread = ? AND id < ? LIMIT 1',
            (thread, int(items[0]['id'])),
        ) is not None
    return {'items': items, 'total': count(thread), 'has_more': has_more}


def clear(thread: str = DEFAULT_THREAD) -> int:
    """清空该 thread 的对话，返回删掉的行数。

    这里**不能**用 `db.execute()` 的返回值：它是 `cur.lastrowid or cur.rowcount`，而 DELETE
    不会重置 lastrowid —— 同一个连接上会拿到上一次 INSERT 的 id（实测返回 8 / 42 这种假数字）。
    所以走 `db.tx()` 直接读游标的真实 `rowcount`，同样参数化。
    """
    with db.tx() as conn:
        cur = conn.execute('DELETE FROM agent_chat_messages WHERE thread = ?',
                           (str(thread or DEFAULT_THREAD),))
        return int(cur.rowcount)


__all__ = ['DEFAULT_THREAD', 'MAX_LIMIT', 'append', 'clear', 'count', 'list_messages']
