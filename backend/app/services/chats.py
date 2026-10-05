"""会话与消息服务：会话列表、消息收发、人工接管、AI 开关。

关键语义（任务卡明确要求）：
- **人工接管**：`send_manual()` 落一条 `direction='out', sender='human'` 的消息，
  再尽力推给协议层；同时把会话切到 `mode='manual'`，AI 不再插话。
- **AI 开关**：`set_ai_enabled(conv, True)` -> `mode='auto'`；`False` -> `mode='manual'`。
  `mode='off'` 表示连 AI 带自动回复全关（人工也不自动发），保留给「静默」场景。
- **AI 回复走懒导入**：`backend.app.agents.runtime.reply_for_message` 未就绪时只落消息、
  不调 AI，绝不让会话服务因为编排层还没写完而报错。
- **编排层是 async**：`runtime.reply_for_message(conversation_id, message_id)` 是协程
  （只生成、不发送）。这里统一用 `_run_awaitable()` 跑它：没有事件循环就直接
  `asyncio.run`，已经在 async 路由里就另开线程跑，避免 "loop already running"。
- **AI 草稿**：`ai_draft()` 只生成建议文本、**不落库不发送**，给人工回复台用。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import inspect
from typing import Any, Dict, List, Optional, Sequence

from ..core import db
from . import common

MODES = ('auto', 'manual', 'off')

SENDER_BY_ROLE = {
    'buyer': ('in', 'buyer'),
    'user': ('in', 'buyer'),
    'human': ('out', 'human'),
    'seller': ('out', 'human'),
}
AI_ROLE_PREFIX = 'ai:'


def _row_to_conversation(row: Any) -> Dict[str, Any]:
    conv = dict(row)
    conv['ai_enabled'] = conv.get('mode') == 'auto'
    conv['unread'] = int(conv.get('unread') or 0)
    return conv


def _row_to_message(row: Any) -> Dict[str, Any]:
    msg = dict(row)
    if msg.get('sent_ok') is not None:
        msg['sent_ok'] = bool(msg['sent_ok'])
    return msg


def _find_conversation(conversation_id: Any) -> Optional[Any]:
    text = str(conversation_id)
    row = db.query_one('SELECT * FROM conversations WHERE id = ?', (text,)) if text.isdigit() else None
    return row


# --------------------------------------------------------------------------- 会话


def ensure_conversation(
    account_id: str,
    peer_user_id: str,
    peer_nickname: str = '',
    item_id: str = '',
    mode: str = 'auto',
    agent_role: str = 'item',
) -> Dict[str, Any]:
    """按 (account_id, peer_user_id) upsert 会话，返回会话 dict（非信封）。"""
    row = db.query_one(
        'SELECT * FROM conversations WHERE account_id = ? AND peer_user_id = ?',
        (account_id, peer_user_id),
    )
    if row is None:
        db.execute(
            'INSERT INTO conversations(account_id, peer_user_id, peer_nickname, item_id, mode, agent_role) '
            'VALUES(?,?,?,?,?,?)',
            (account_id, peer_user_id, peer_nickname or '', item_id or '', mode if mode in MODES else 'auto', agent_role or 'item'),
        )
        row = db.query_one(
            'SELECT * FROM conversations WHERE account_id = ? AND peer_user_id = ?',
            (account_id, peer_user_id),
        )
    else:
        updates: Dict[str, Any] = {}
        if peer_nickname and peer_nickname != row['peer_nickname']:
            updates['peer_nickname'] = peer_nickname
        if item_id and item_id != row['item_id']:
            updates['item_id'] = item_id
        if updates:
            sets = ', '.join(f'{k} = ?' for k in updates)
            db.execute(f'UPDATE conversations SET {sets} WHERE id = ?', list(updates.values()) + [row['id']])
            row = db.query_one('SELECT * FROM conversations WHERE id = ?', (row['id'],))
    return _row_to_conversation(row)


def list_conversations(
    account_id: Optional[str] = None,
    keyword: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 100), 500))
    where: List[str] = []
    params: List[Any] = []
    if account_id:
        where.append('account_id = ?')
        params.append(account_id)
    if mode:
        where.append('mode = ?')
        params.append(mode)
    if keyword:
        where.append('(peer_nickname LIKE ? OR peer_user_id LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like, like])
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    rows = db.query(
        f'SELECT * FROM conversations{clause} ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC LIMIT ?',
        params + [limit],
    )
    conversations = []
    for row in rows:
        conv = _row_to_conversation(row)
        last = db.query_one(
            'SELECT content, created_at, direction, sender FROM messages WHERE conversation_id = ? '
            'ORDER BY id DESC LIMIT 1',
            (conv['id'],),
        )
        conv['last_message'] = dict(last) if last is not None else None
        conversations.append(conv)
    return {'conversations': conversations, 'total': len(conversations)}


def get_conversation(conversation_id: Any) -> Any:
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    return _row_to_conversation(row)


# --------------------------------------------------------------------------- 消息


def get_messages(conversation_id: Any, page: int = 1, size: int = 50) -> Any:
    if _find_conversation(conversation_id) is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 50), 500))
    total_row = db.query_one(
        'SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?', (str(conversation_id),),
    )
    total = int(total_row['n']) if total_row else 0
    rows = db.query(
        'SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ? OFFSET ?',
        (str(conversation_id), size, (page - 1) * size),
    )
    messages = [_row_to_message(r) for r in reversed(rows)]
    return {'messages': messages, 'total': total, 'page': page, 'size': size,
            'pages': (total + size - 1) // size}


def append_message(
    conversation_id: Any,
    role: str,
    content: str,
    *,
    ai_generated: bool = False,
    item_id: str = '',
    meta: Optional[Dict[str, Any]] = None,
    sent_ok: Optional[bool] = None,
    mark_unread: bool = True,
) -> Any:
    """落一条消息。

    `role` 认三种写法：`buyer`（买家来信）、`human`（人工发出）、`ai:<role>`（AI 发出）。
    返回消息 dict（失败时返回信封）。
    """
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    role = (role or '').strip() or 'buyer'
    if role in SENDER_BY_ROLE:
        direction, sender = SENDER_BY_ROLE[role]
    elif role.startswith(AI_ROLE_PREFIX) or role in ('ai', 'assistant'):
        direction, sender = 'out', role if role.startswith(AI_ROLE_PREFIX) else 'ai:item'
    else:
        direction, sender = 'out', role

    if ai_generated and not sender.startswith(AI_ROLE_PREFIX) and sender != 'ai':
        sender = f'ai:{sender}'

    import json as _json
    payload_meta = dict(meta or {})
    if ai_generated:
        payload_meta.setdefault('ai_generated', True)

    message_id = db.execute(
        'INSERT INTO messages(conversation_id, direction, sender, content, item_id, meta, sent_ok) '
        'VALUES(?,?,?,?,?,?,?)',
        (
            row['id'], direction, sender, content or '', item_id or row['item_id'] or '',
            _json.dumps(payload_meta, ensure_ascii=False),
            None if sent_ok is None else (1 if sent_ok else 0),
        ),
    )
    db.execute(
        'UPDATE conversations SET last_message_at = CURRENT_TIMESTAMP WHERE id = ?', (row['id'],),
    )
    if direction == 'in' and mark_unread:
        db.execute('UPDATE conversations SET unread = unread + 1 WHERE id = ?', (row['id'],))

    message = db.query_one('SELECT * FROM messages WHERE id = ?', (message_id,))
    result = _row_to_message(message)
    common.publish('chats.message', {
        'conversation_id': row['id'], 'message_id': message_id,
        'direction': direction, 'sender': sender, 'content': content or '',
    })
    return result


def _push_message(client: Any, conversation: Any, text: str) -> Optional[str]:
    """尽力把人工回复推给协议层；返回警告文本或 None。"""
    status, value = common.call_method(
        client, ('send_message', 'send_im', 'send_text', 'send'),
        conversation['account_id'], conversation['peer_user_id'], text,
    )
    if status == common.CALL_OK:
        return None
    if status == common.CALL_MISSING:
        return '协议层尚未实现发送消息，消息仅本地留存'
    return f'协议层发送失败，消息仅本地留存: {value}'


def send_manual(conversation_id: Any, text: str) -> Any:
    """人工接管：卖家自己回消息。

    - 落 `messages`（direction=out, sender=human）
    - 尽力调协议层真正发出去；协议层未就绪 -> 消息仍落库，`sent: false` + warning
    - 会话切到 `manual`（AI 不再插话），并记 operation_logs
    """
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    text = (text or '').strip()
    if not text:
        return common.fail('消息内容不能为空', 'invalid_fields')
    conversation = _row_to_conversation(row)

    client, _err = common.get_client(conversation['account_id'])
    warning: Optional[str] = None
    sent = False
    if client is None:
        warning = '协议层尚未就绪，消息仅本地留存'
    else:
        warning = _push_message(client, conversation, text)
        sent = warning is None

    message = append_message(row['id'], 'human', text, sent_ok=sent, mark_unread=False)
    if common.is_failure(message):
        return message

    if conversation['mode'] != 'manual':
        db.execute('UPDATE conversations SET mode = ? WHERE id = ?', ('manual', row['id']))
        common.publish('chats.mode_changed', {'conversation_id': row['id'], 'mode': 'manual'})

    common.log_operation(
        conversation['account_id'], 'send_manual', conversation['item_id'] or '',
        True, {'conversation_id': row['id'], 'sent': sent, 'warning': warning},
    )
    result: Dict[str, Any] = {
        'message': message, 'sent': sent, 'mode': 'manual',
        'conversation_id': row['id'],
    }
    if warning:
        result['warning'] = warning
    return result


def set_ai_enabled(conversation_id: Any, enabled: bool) -> Any:
    """人工接管开关：True -> mode='auto'（AI 可以回），False -> mode='manual'（只有人工）。"""
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    mode = 'auto' if enabled else 'manual'
    db.execute('UPDATE conversations SET mode = ? WHERE id = ?', (mode, row['id']))
    common.log_operation(
        row['account_id'], 'set_ai_enabled', row['item_id'] or '',
        True, {'conversation_id': row['id'], 'mode': mode},
    )
    common.publish('chats.mode_changed', {'conversation_id': row['id'], 'mode': mode})
    return {'conversation_id': row['id'], 'mode': mode, 'ai_enabled': mode == 'auto'}


def set_mode(conversation_id: Any, mode: str) -> Any:
    if mode not in MODES:
        return common.fail(f'mode 必须是 {"/".join(MODES)} 之一', 'invalid_fields')
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    db.execute('UPDATE conversations SET mode = ? WHERE id = ?', (mode, row['id']))
    common.log_operation(
        row['account_id'], 'set_mode', row['item_id'] or '', True,
        {'conversation_id': row['id'], 'mode': mode},
    )
    common.publish('chats.mode_changed', {'conversation_id': row['id'], 'mode': mode})
    return {'conversation_id': row['id'], 'mode': mode, 'ai_enabled': mode == 'auto'}


def mark_read(conversation_id: Any) -> Any:
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    db.execute('UPDATE conversations SET unread = 0 WHERE id = ?', (row['id'],))
    return {'conversation_id': row['id'], 'unread': 0}


# --------------------------------------------------------------------------- AI 自动回复


def _load_runtime():
    """懒导入编排层；未就绪返回 None（调用方只落消息、不调 AI）。"""
    try:
        return importlib.import_module('backend.app.agents.runtime')
    except ImportError:
        return None
    except Exception:  # 编排层内部依赖缺失也当作未就绪
        return None


def _run_awaitable(value: Any) -> Any:
    """跑一个可能是协程的返回值，返回其结果。

    编排层的 `reply_for_message` 是 `async def`。三种情况：
    - 不是 awaitable（测试里的同步假实现）-> 原样返回
    - 没有正在跑的事件循环 -> `asyncio.run()`
    - 已经在 async 路由的事件循环里 -> 另开线程跑，避免 "loop already running"
    """
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)  # type: ignore[arg-type]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, value).result()


def _normalize_ai_result(raw: Any) -> Dict[str, Any]:
    """把编排层的返回统一成 `{'success', 'reply', 'role', ...}`。

    编排层真实形状是 `{'ok': bool, 'reply': str, 'role': ..., 'error': ...}`；
    这里同时容忍 `{'success': ...}` 与纯字符串。
    """
    if isinstance(raw, str):
        return {'success': True, 'reply': raw, 'role': 'item'}
    if not isinstance(raw, dict):
        return {'success': False, 'reply': '', 'message': f'AI 返回格式无法识别: {raw!r}'}

    result = dict(raw)
    if 'success' not in result:
        if 'ok' in result:
            result['success'] = bool(result.get('ok'))
        else:
            # 既没 ok 也没 success：有正文就算成功（兼容简单假实现）
            result['success'] = bool(result.get('reply') or result.get('text') or result.get('content'))
    if not result['success']:
        result.setdefault('reply', '')
        result.setdefault('message', str(result.get('error') or 'AI 回复生成失败'))
    else:
        result.setdefault('reply', str(result.get('text') or result.get('content') or ''))
    return result


def _ai_reply(conversation: Dict[str, Any], message_id: Any) -> Optional[Dict[str, Any]]:
    """调编排层生成回复（只生成、不发送）。编排层未就绪返回 None。"""
    runtime = _load_runtime()
    if runtime is None:
        return None
    handler = getattr(runtime, 'reply_for_message', None)
    if not callable(handler):
        return None
    try:
        return _normalize_ai_result(_run_awaitable(handler(int(conversation['id']), int(message_id))))
    except Exception as exc:  # AI 出错绝不能影响买家消息落库
        return {'success': False, 'reply': '', 'message': f'AI 回复失败: {exc}', 'code': 'ai_error'}


def ai_draft(conversation_id: Any, message_id: Any = None) -> Any:
    """CONTRACTS §3：`POST /api/chats/{id}/ai-draft` -> `{draft}`。

    **只生成建议、不发送、也不落库** —— 人工回复台把它填进输入框，卖家确认后再 send。
    没给 message_id 时用该会话最后一条买家消息。
    """
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    conversation = _row_to_conversation(row)

    if message_id in (None, '', 0):
        last = db.query_one(
            "SELECT * FROM messages WHERE conversation_id = ? AND direction = 'in' "
            'ORDER BY id DESC LIMIT 1',
            (str(row['id']),),
        )
        if last is None:
            return common.fail('该会话还没有买家消息，无法生成草稿', 'invalid_fields')
        message_id = last['id']

    reply = _ai_reply(conversation, message_id)
    if reply is None:
        return common.not_ready('AI 编排层尚未就绪，无法生成草稿')
    if not reply.get('success'):
        return common.fail(str(reply.get('message') or 'AI 草稿生成失败'), 'ai_error')

    draft = str(reply.get('reply') or '')
    if not draft:
        return common.fail('AI 没有给出建议内容', 'ai_error')
    return {
        'draft': draft,
        'conversation_id': int(row['id']),
        'message_id': int(message_id),
        'role': reply.get('role') or conversation.get('agent_role') or 'item',
    }


def handle_incoming(
    conversation_id: Any,
    content: str,
    *,
    item_id: str = '',
    peer_nickname: str = '',
    account_id: str = '',
    peer_user_id: str = '',
) -> Any:
    """买家来信入口：落消息 -> 若会话是 auto 且 AI 可用，生成并落一条 AI 回复。

    编排层（`backend.app.agents.runtime.reply_for_message`，async）未就绪时只落买家消息。
    """
    if _find_conversation(conversation_id) is None:
        if not (account_id and peer_user_id):
            return common.fail(f'会话不存在: {conversation_id}', 'not_found')
        conversation = ensure_conversation(account_id, peer_user_id, peer_nickname, item_id)
    else:
        conversation = get_conversation(conversation_id)

    incoming = append_message(conversation['id'], 'buyer', content, item_id=item_id)
    if common.is_failure(incoming):
        return incoming

    reply_info: Optional[Dict[str, Any]] = None
    ai_message = None
    if conversation['mode'] == 'auto':
        # 编排层签名：async reply_for_message(conversation_id, message_id) —— 只生成不发送
        reply = _ai_reply(conversation, incoming['id'])
        if reply is not None:
            reply_info = reply
            reply_text = str(reply.get('reply') or '') if reply.get('success') else ''
            if reply_text:
                ai_message = append_message(
                    conversation['id'], f'ai:{conversation.get("agent_role") or "item"}',
                    reply_text, ai_generated=True, sent_ok=False, mark_unread=False,
                )
                common.publish('chats.ai_reply', {
                    'conversation_id': conversation['id'], 'content': reply_text,
                })

    return {
        'message': incoming,
        'ai_reply': ai_message,
        'ai_result': reply_info,
        'mode': conversation['mode'],
    }
