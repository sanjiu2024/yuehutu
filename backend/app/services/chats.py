"""会话与消息服务：会话列表、消息收发、人工接管、AI 开关。

关键语义（任务卡明确要求）：
- **人工接管**：`send_manual()` 落一条 `direction='out', sender='human'` 的消息，
  再尽力推给协议层；同时把会话切到 `mode='manual'`，AI 不再插话。
- **AI 开关**：`set_ai_enabled(conv, True)` -> `mode='auto'`；`False` -> `mode='manual'`。
  `mode='off'` 表示连 AI 带自动回复全关（人工也不自动发），保留给「静默」场景。
- **AI 回复走懒导入**：`backend.app.agents.runtime.reply_for_message` 未就绪时只落消息、
  不调 AI，绝不让会话服务因为编排层还没写完而报错。
- **编排层是 async**：`runtime.reply_for_message(conversation_id, message_id)` 是协程
  （只生成、不发送）。这里统一用 `common.run_awaitable()` 跑它：没有事件循环就直接
  `asyncio.run`，已经在 async 路由里就另开线程跑，避免 "loop already running"。
- **AI 草稿**：`ai_draft()` 只生成建议文本、**不落库不发送**，给人工回复台用。
- **买家来信入口**：`handle_incoming()` 是唯一入口（`POST /api/chats/incoming` 与
  IM 入站分发都汇到它）。带 `msg_id` 时**幂等**：同一平台消息 id 重复送达只落一次、
  不会再触发一次 AI（去重标记落 `messages.meta.external_id`，不动 SCHEMA）。
- **IM 入站分发**：协议层 `xianyu.im.ImSession` 只提供 `on_message()` 回调点，
  由服务层 `attach_im_session()` 注入 `im_handler_for()` —— 协议层不 import services。
  注册是**幂等**的（同一会话同一账号只挂一份 handler），否则一封来信会触发多次 AI。
- **AI 回复真的发出去**：AI 回复落库后由 `deliver_ai_reply()` 经协议层
  `send_message` 发回闲鱼，并把结果如实回写 `messages.sent_ok` + 失败原因；
  `mode != 'auto'`（人工接管/关闭）**绝不自动发送**。
"""

from __future__ import annotations

import importlib
import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from loguru import logger

from ..core import db
from . import common

MODES = ('auto', 'manual', 'off')

#: 取不到平台会话 id（cid）时的结构化 code 与原因文本。
#: cid 只跟着 IM 帧进 `messages.meta`；HTTP 手动灌进来的来信、或历史数据没有 cid。
MISSING_CID_CODE = 'missing_cid'
MISSING_CID_MESSAGE = '无法确定平台会话 id（cid），消息仅本地留存、未发送'

#: 账号级开关关掉 AI 时的结构化 code（任务 B3）。
#: 判据是 `accounts.enabled` 与 `accounts.auto_reply` —— 这两个开关以前只在
#: `main.py:105`（决定要不要挂 IM 入站）被读，业务层根本不看，所以「关了自动回复
#: AI 照样开口」。注意 `enabled=0` 也会拦住 AI：账号没启用就当作不该自动发言。
ACCOUNT_OFF_CODE = 'account_auto_reply_off'

#: 非 auto 模式下的**结构化**拒发原因（任务 C5）。
#: 以前这个 code 只存在于 `deliver_ai_reply` 的 `mode != 'auto'` 分支里，而它唯一的
#: 生产调用方位于 `handle_incoming` 的 `mode == 'auto'` 分支内 -> 生产不可达。
#: 现在判据提到分支外，`POST /api/chats/incoming` 在 manual/off 会话上也会带回这个 code。
MODE_NOT_AUTO_CODE = 'mode_not_auto'

#: 「不是买家在说话」的消息类型：协议层认不出的帧会给 `[系统消息]` / `[卡片消息]`
#: 摘要（订单/物流提示、商品卡、小程序…）。这些帧现在**照旧落库**（以前是被静默丢掉，
#: 界面上就成了「(空消息)」），但绝不能触发 AI 自动回复 —— 对着一张商品卡开口说话
#: 就是自言自语，还会白烧一次大模型调用。旧项目 `XianyuAutoAsync.py:9918-9953`
#: 对同类帧也是判系统消息后直接 return。
SYSTEM_MSG_TYPES = ('system', 'card')

#: 命中 `SYSTEM_MSG_TYPES` 时给调用方的结构化拒发原因（和 `MODE_NOT_AUTO_CODE` 同形）。
SYSTEM_MESSAGE_CODE = 'system_message'

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


#: `messages.meta` 里承载媒体信息的键：图片地址列表 `images`、单张 `image_url`、
#: 语音 `audio_url`、语音时长 `audio_duration`、消息类型 `msg_type`。
#: 图片/语音**只走 meta**（SCHEMA 冻结，不加列），落库与透出都从这里读写。
MEDIA_META_KEYS = ('images', 'image_url', 'image', 'msg_type', 'audio_url', 'audio_duration')

#: 语音时长 > 这个数就当毫秒 —— **只是兜底**，真正生效的是协议层同名常量。
#: 保留一个本地默认值是为了「协议层整个装不上」时 `_row_to_message` 还能出数。
AUDIO_MS_THRESHOLD = 300


def audio_seconds(value: Any) -> Optional[float]:
    """语音时长统一成**秒**；取不到返回 None（前端「时长未知就不写数字」）。

    **换算只有一份实现**：协议层 `backend/app/xianyu/im.py::audio_seconds`。这里刻意
    不自己算 —— 以前两层各写一套阈值（协议层把 400 当 400 秒、这里当 0.4 秒），
    同一条语音在消息摘要和播放器上会显示成相差 1000 倍的两个数。
    """
    module = common.protocol_module('im')
    convert = getattr(module, 'audio_seconds', None)
    if callable(convert):
        return convert(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > AUDIO_MS_THRESHOLD:
        number = number / 1000.0
    return round(number, 1)


def _parse_meta(raw: Any) -> Dict[str, Any]:
    """`messages.meta` 是 JSON 文本 -> dict；解析不了返回空 dict（**绝不抛**）。"""
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or '').strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def meta_images(meta: Dict[str, Any]) -> List[str]:
    """从 meta 里取图片地址列表：`images` 优先，单张 `image_url`/`image` 兜底。

    协议层（task-43）还没落地时 meta 里只有单张 `image_url` —— 两种形状都要认，
    否则前端拿不到缩略图。
    """
    meta = meta or {}
    raw = meta.get('images')
    if isinstance(raw, (list, tuple)):
        candidates: Sequence[Any] = raw
    elif isinstance(raw, str) and raw.strip():
        candidates = [raw]
    else:
        candidates = []
    urls: List[str] = []
    for value in candidates:
        url = str(value or '').strip()
        if url and url not in urls:
            urls.append(url)
    for key in ('image_url', 'image'):
        url = str(meta.get(key) or '').strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _media_placeholder(meta: Dict[str, Any]) -> str:
    """正文为空时给一句可读占位（**绝不落空串**）：有图 `[图片]`、有语音 `[语音]`。"""
    if meta_images(meta):
        return '[图片]'
    if str((meta or {}).get('audio_url') or '').strip():
        return '[语音]'
    return ''


def _display_msg_type(column: Any, meta: Dict[str, Any], images: Sequence[str], audio_url: str) -> str:
    """给前端的消息类型：有语音就是 voice、有图就是 image，其余信 meta / 列值，退回 text。

    **语音优先于图片**：真机语音帧里可能带一张 jpg 封面，先判图会把语音消息显示成图片。
    命名跟协议层（`backend/app/xianyu/im.py`）和 `messages.msg_type` 列一致用 `voice`，
    历史写进去的 `audio` 也统一归成 `voice`（库里只该有一种写法）。
    """
    if audio_url:
        return 'voice'
    if images:
        return 'image'
    value = str((meta or {}).get('msg_type') or column or '').strip().lower()
    if value == 'audio':
        return 'voice'
    return value or 'text'


def _row_to_message(row: Any) -> Dict[str, Any]:
    """一行 `messages` -> 给 API/前端的消息 dict。

    除原有字段外**新增**（都是加字段，不删不改原有语义）：
    `images`（图片地址列表）、`msg_type`（image/audio/文本/卡片…）、`audio_url`、
    `audio_duration`（秒，未知为 null）、`ai_role`/`ai_model`/`ai_elapsed_ms`（AI 来源标记）。
    """
    msg = dict(row)
    if msg.get('sent_ok') is not None:
        msg['sent_ok'] = bool(msg['sent_ok'])
    meta = _parse_meta(msg.get('meta'))
    images = meta_images(meta)
    audio_url = str(meta.get('audio_url') or '').strip()
    msg['images'] = images
    msg['msg_type'] = _display_msg_type(msg.get('msg_type'), meta, images, audio_url)
    msg['audio_url'] = audio_url
    msg['audio_duration'] = audio_seconds(meta.get('audio_duration'))
    sender = str(msg.get('sender') or '')
    if sender.startswith(AI_ROLE_PREFIX):
        msg['ai_role'] = sender[len(AI_ROLE_PREFIX):]
    else:
        # `ai:<role>` 之外还认 meta（AI 回复落库时两个地方都写，老数据只有 meta 也得认）
        msg['ai_role'] = str(meta.get('ai_role') or '').strip()
    msg['ai_model'] = str(meta.get('ai_model') or meta.get('model') or '').strip()
    elapsed = meta.get('ai_elapsed_ms', meta.get('elapsed_ms'))
    try:
        msg['ai_elapsed_ms'] = int(elapsed) if elapsed not in (None, '') else None
    except (TypeError, ValueError):
        msg['ai_elapsed_ms'] = None
    return msg


def _find_conversation(conversation_id: Any) -> Optional[Any]:
    text = str(conversation_id)
    row = db.query_one('SELECT * FROM conversations WHERE id = ?', (text,)) if text.isdigit() else None
    return row


# --------------------------------------------------------------------------- 幂等

#: 平台侧消息 id 存在 `messages.meta` 里的键名。
#: 用 meta 而不是新列，是为了不改 SCHEMA —— 幂等只需要「查得到」，不需要索引。
EXTERNAL_ID_KEY = 'external_id'


def default_agent_role(item_id: Any) -> str:
    """新会话该用哪个角色：有商品上下文 -> `item`（商品专家），没有 -> `cs`（客服）。

    **不要在建会话时再用它了**（任务 A2）：`role_for_conversation()` 的规则 1 是
    「`agent_role` 是合法角色就直接用它」，而这里返回的 `item` / `cs` 恰好都是合法角色 ——
    只要建会话时预写死，规则 1 就恒命中，`bargain` / `vision`（规则 2/3）**永远选不上**。
    现在 `ensure_conversation()` 默认写空 `agent_role`，让规则真跑；
    这个函数只保留给「确实要人工钉死角色」的调用方当兜底用。
    """
    return 'item' if str(item_id or '').strip() else 'cs'


def peer_nickname_candidate(nick: Any, *, buyer_id: Any = '', my_nick: Any = '') -> str:
    """挑出**能当买家昵称**的串，挑不出返回空串。

    丢掉四类：空串、纯数字（平台给未设置昵称的账号发的占位名）、等于 buyer_id 的、
    等于**账号自己昵称**的（真机上 `reminderTitle` 会是发最后一条消息那个人的昵称，
    自己发的就是自己的）。
    """
    nick = str(nick or '').strip()
    if not nick or nick.isdigit():
        return ''
    if str(buyer_id or '').strip() and nick == str(buyer_id).strip():
        return ''
    if str(my_nick or '').strip() and nick == str(my_nick).strip():
        return ''
    return nick


def _row_text(row: Any, key: str) -> str:
    """安全读一行里的字符串字段（`db.query_one` 回的是 `sqlite3.Row`，没有 `.get()`）。"""
    if row is None:
        return ''
    try:
        return str(row[key] or '').strip()
    except (IndexError, KeyError, TypeError):
        return ''


def _account_nickname(account_id: Any) -> str:
    """账号**自己**的昵称（登录后落在 `accounts.nickname`），用来识别库里的脏昵称。"""
    row = db.query_one('SELECT nickname FROM accounts WHERE id = ?', (str(account_id or ''),))
    return _row_text(row, 'nickname')


def _safe_peer_nickname(nick: Any, current: Any, my_nick: Any = '') -> str:
    """算出「该往 `peer_nickname` 写什么」，返回空串表示**不写**。

    只在两种情况下认新昵称：库里是空的，或库里存的正是账号自己的昵称（历史脏数据 ——
    老版本把 `reminderTitle` 无条件当买家名，用户库里现在整列都是他自己的名字）。
    库里已经是别的名字时**保留**：宁可留一个不完美的真买家名，也不要被来路不明的串冲掉。
    """
    nick = str(nick or '').strip()
    if not nick:
        return ''
    current = str(current or '').strip()
    if not current or current == nick:
        return nick
    if str(my_nick or '').strip() and current == str(my_nick).strip():
        return nick
    return ''


def apply_peer_nickname(conversation_id: Any, nick: Any, my_nick: Any = '') -> str:
    """按 `_safe_peer_nickname` 的规则写库，返回**真正写进去**的值（没写=空串）。

    这是「永不用自己的昵称覆盖买家名」的唯一落库口：IM 同步与 IM 入站都走它。
    """
    try:
        conv_id = int(conversation_id)
    except (TypeError, ValueError):
        return ''
    row = db.query_one('SELECT peer_nickname FROM conversations WHERE id = ?', (conv_id,))
    if row is None:
        return ''
    current = _row_text(row, 'peer_nickname')
    target = _safe_peer_nickname(nick, current, my_nick=my_nick)
    if not target or target == current:
        return ''
    db.execute('UPDATE conversations SET peer_nickname = ? WHERE id = ?', (target, conv_id))
    return target


def find_incoming_by_external_id(conversation_id: Any, external_id: str) -> Optional[Dict[str, Any]]:
    """按平台消息 id 找**已落库的买家消息**；没有就返回 None。

    只认 `direction='in'`：同一条 msg_id 被回显成出站消息时不算重复来信。
    """
    external_id = str(external_id or '')
    if not external_id:
        return None
    rows = db.query(
        "SELECT * FROM messages WHERE conversation_id = ? AND direction = 'in' "
        "AND meta LIKE ? ORDER BY id ASC",
        (str(conversation_id), f'%{external_id}%'),
    )
    for row in rows:
        try:
            stored = json.loads(row['meta'] or '{}') or {}
        except (TypeError, ValueError):
            continue
        # LIKE 只是粗筛（`abc` 会命中 `xabc` / 正文里出现的同一个词），最终以 meta 为准
        if str(stored.get(EXTERNAL_ID_KEY) or '') == external_id:
            return _row_to_message(row)
    return None


# --------------------------------------------------------------------------- 会话


def ensure_conversation(
    account_id: str,
    peer_user_id: str,
    peer_nickname: str = '',
    item_id: str = '',
    mode: str = 'auto',
    agent_role: str = '',
) -> Dict[str, Any]:
    """按 (account_id, peer_user_id) upsert 会话，返回会话 dict（非信封）。

    `agent_role` 默认 **空串**（任务 A2）：空串不是合法角色，
    `agents.roles.role_for_conversation()` 的规则 1 会跳过它，改由消息内容决定
    `vision` / `bargain` / `cs` / `item`。只有调用方**明确**传入合法角色时才钉死
    （人工在 UI 上选了角色）。老代码在 `handle_incoming` / `dispatch_im_message` 里
    预写 `default_agent_role(item_id)`（`item` / `cs`），导致规则 2/3 永远不可达。
    """
    row = db.query_one(
        'SELECT * FROM conversations WHERE account_id = ? AND peer_user_id = ?',
        (account_id, peer_user_id),
    )
    if row is None:
        db.execute(
            'INSERT INTO conversations(account_id, peer_user_id, peer_nickname, item_id, mode, agent_role) '
            'VALUES(?,?,?,?,?,?)',
            (account_id, peer_user_id, peer_nickname or '', item_id or '', mode if mode in MODES else 'auto', agent_role or ''),
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


def purge_account_data(account_id: Any) -> Dict[str, int]:
    """删除某账号的**全部会话与其消息**，返回 `{"conversations": n, "messages": n}`。

    为什么单独放在这里：`xianyu.store.delete_account()` 只清
    `account_credentials` / `risk_state` / `risk_events` / `accounts`，
    既没有 `ON DELETE CASCADE` 也没有清 `conversations` / `messages` ——
    删号后残留的会话会继续出现在对话列表里（审计 docs/audit/chats.md §10）。
    删除账号的完整清理 = `store.delete_account()` + `chats.purge_account_data()`。

    幂等：账号没有数据时返回 `{"conversations": 0, "messages": 0}`，不抛异常；
    账号不存在也返回 0/0（调用方不需要先判存在）。
    """
    account_id = str(account_id or '')
    if not account_id:
        return {'conversations': 0, 'messages': 0}

    # `db.execute()` 对 DELETE 的返回值不可靠（`lastrowid or rowcount` 会拿到陈旧
    # lastrowid，见 core/db.py:265-267），所以条数先查再删。
    row = db.query_one('SELECT COUNT(*) AS c FROM conversations WHERE account_id = ?', (account_id,))
    conversations = int(row['c']) if row else 0
    row = db.query_one(
        'SELECT COUNT(*) AS c FROM messages WHERE conversation_id IN '
        '(SELECT id FROM conversations WHERE account_id = ?)',
        (account_id,),
    )
    messages = int(row['c']) if row else 0

    if conversations or messages:
        with db.tx():
            # 先消息后会话：外键只声明在 messages.conversation_id 一侧，
            # 反过来删会话会把 messages 留成孤儿。
            db.execute(
                'DELETE FROM messages WHERE conversation_id IN '
                '(SELECT id FROM conversations WHERE account_id = ?)',
                (account_id,),
            )
            db.execute('DELETE FROM conversations WHERE account_id = ?', (account_id,))

    logger.info(
        'purge_account_data: account={} conversations={} messages={}',
        account_id,
        conversations,
        messages,
    )
    return {'conversations': conversations, 'messages': messages}


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
            'SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1',
            (conv['id'],),
        )
        if last is None:
            conv['last_message'] = None
        else:
            # 摘要也走 `_row_to_message`：task-44 之前落库的「空正文 + meta 里有图片地址」
            # 消息在列表里会显示成「（暂无消息）」，跟气泡里的缩略图自相矛盾。
            summary = _row_to_message(last)
            if not str(summary.get('content') or '').strip():
                summary['content'] = _media_placeholder(_parse_meta(summary.get('meta')))
            conv['last_message'] = summary
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

    # 只有媒体、没有文字的消息（买家发图/发语音）不能落空串：前端会显示成「(空消息)」。
    # 落一句 `[图片]` / `[语音]` 占位，会话列表的最后一条摘要也才有东西可显示。
    content = str(content or '')
    if not content.strip():
        content = _media_placeholder(payload_meta) or content

    message_id = db.execute(
        'INSERT INTO messages(conversation_id, direction, sender, content, msg_type, item_id, meta, sent_ok) '
        'VALUES(?,?,?,?,?,?,?,?)',
        (
            row['id'], direction, sender, content,
            str(payload_meta.get('msg_type') or 'text').strip().lower() or 'text',
            item_id or row['item_id'] or '',
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


def _platform_cid(conversation: Any) -> str:
    """尽力取平台会话 id（cid）；**取不到就返回空串**，绝不拿别的 id 冒充。

    `conversations` 表没有 cid 列 —— cid 只出现在 IM 帧里，跟着消息落进 `messages.meta`。

    这里原来取不到就 `return peer_user_id`，是「人工回复发到错误会话」的根因：
    cid 是 **IM 会话 id**，`peer_user_id` 是 **买家 user id**，两者不是一回事
    （审计 docs/audit/chats.md §5 实测：打桩收到的第一个位置参数是 `audit_peer`）。
    拿买家 id 当会话 id 发出去，等于把卖家自己回的话投进一个平台不认识的会话，
    本地还记成 `sent: true`。现在**取不到就承认取不到**，由调用方给出 `missing_cid`。
    """
    try:
        row = db.query_one(
            "SELECT meta FROM messages WHERE conversation_id = ? AND meta LIKE '%cid%' "
            "ORDER BY id DESC LIMIT 1",
            (conversation['id'],),
        )
        raw = row['meta'] if row is not None else None
        if raw:
            meta = json.loads(raw) if isinstance(raw, str) else raw
            cid = str((meta or {}).get('cid') or '')
            if cid:
                return cid
    except Exception as exc:  # noqa: BLE001 - 取不到 cid 不该让发送整条失败
        logger.debug(f'从 messages.meta 取 cid 失败: {exc}')
    return ''


def _push_message(client: Any, conversation: Any, text: str, cid: str = '') -> Optional[str]:
    """把人工回复推给协议层；返回 `None` 表示成功，否则返回失败原因文本。

    真客户端签名是 `send_message(cid, text, toid=None)`（`xianyu/client.py:345`）。
    这里原来传的是 `(account_id, peer_user_id, text)` —— 三个位置全错位，
    等于把**账号 id** 当会话、把**买家 id** 当消息正文发出去；人工接管看起来「发出去了」，
    实际发的内容和对象都是错的。现在按真签名传 cid + 正文。

    `cid` 必须由调用方从 IM 帧里拿到（`_platform_cid`）；空 cid **不发送**，
    返回原因文本，绝不退回 `peer_user_id` 冒充。
    """
    cid = str(cid or '') or _platform_cid(conversation)
    if not cid:
        return MISSING_CID_MESSAGE
    status, value = common.call_method(client, ('send_message',), cid, text)
    if status == common.CALL_OK and not (isinstance(value, dict) and value.get('success') is False):
        return None
    if status == common.CALL_MISSING:
        return '协议层尚未实现发送消息，消息仅本地留存'
    return f'协议层发送失败，消息仅本地留存: {value}'


#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = 'confirmation_required'


def _truthy(value: Any) -> bool:
    """只有字面量 `True` / `'true'` / `1` 才算确认，别的（含 `'false'`、`'yes'`）都不算。

    与 `services/items.py::_truthy`、`services/orders.py::_truthy`、
    `mcp_server/client.py::_truthy` 同一口径 —— 三条路（HTTP / 主管 AI / MCP）
    必须对「什么算点头」有完全一样的判断。
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value == 1


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰库、不碰账号、不碰平台。"""
    if _truthy(confirm):
        return None
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交到闲鱼',
        CONFIRM_CODE,
    )


def send_manual(conversation_id: Any, text: str, *, confirm: Any = None) -> Any:
    """人工接管：卖家自己回消息。

    **这是真发消息**（买家会收到），所以第一道闸门是确认（`confirm=true`）：
    确认 → 字段校验 → 建客户端，与其它写接口同口径。
    ``direction='out'`` 那条入口（卖家已在别处发过、由调用方断言）在路由层显式传
    ``confirm=True``，见 `backend/app/api/chats.py`。

    - 落 `messages`（direction=out, sender=human），未送达时 `sent_ok=0` + `meta.send_error`
    - 尽力调协议层真正发出去；协议层未就绪 / 取不到 cid -> **不发送**，
      消息仍留库但标注未送达，并给出结构化 `code`
    - 会话切到 `manual`（AI 不再插话），并记 operation_logs

    返回形状与 `deliver_ai_reply` 对齐：`{'message','sent','code','mode','conversation_id'}`
    （`sent=False` 时多一个 `'warning'` 说明原因）。故意**不是**失败信封：
    消息确实已经留在本地库里，HTTP 层报 4xx/5xx 会让前端丢掉「已留库、未送达」这个信息。
    """
    problem = _confirm_gate('给买家发消息', confirm)
    if problem is not None:
        return problem
    row = _find_conversation(conversation_id)
    if row is None:
        return common.fail(f'会话不存在: {conversation_id}', 'not_found')
    text = (text or '').strip()
    if not text:
        return common.fail('消息内容不能为空', 'invalid_fields')
    conversation = _row_to_conversation(row)

    # cid 先算：它是**会话自己的固有属性**（这条会话有没有平台上下文），
    # 与协议层此刻是否就绪无关。先判它，才能让 `missing_cid` 稳定可达、
    # 也不会为了「一条本来就发不出去的消息」去构造协议层客户端。
    cid = _platform_cid(conversation)
    warning: Optional[str] = None
    sent = False
    if not cid:
        code = MISSING_CID_CODE
        warning = MISSING_CID_MESSAGE
    else:
        client, err = common.get_client(conversation['account_id'])
        if client is None:
            code = str((err or {}).get('code') or common.NOT_READY_CODE)
            warning = '协议层尚未就绪，消息仅本地留存'
        else:
            warning = _push_message(client, conversation, text, cid=cid)
            sent = warning is None
            code = 'ok' if sent else 'send_failed'

    # 未送达必须留痕：光有一条 sent_ok=0 的 out 消息，谁也说不清是「没发」还是「发失败」。
    message = append_message(
        row['id'], 'human', text, sent_ok=sent, mark_unread=False,
        meta=None if sent else {'send_error': warning},
    )
    if common.is_failure(message):
        return message

    if conversation['mode'] != 'manual':
        db.execute('UPDATE conversations SET mode = ? WHERE id = ?', ('manual', row['id']))
        common.publish('chats.mode_changed', {'conversation_id': row['id'], 'mode': 'manual'})

    common.log_operation(
        conversation['account_id'], 'send_manual', conversation['item_id'] or '',
        sent, {'conversation_id': row['id'], 'sent': sent, 'code': code, 'warning': warning},
    )
    result: Dict[str, Any] = {
        'message': message, 'sent': sent, 'code': code, 'mode': 'manual',
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


def _account_reply_gate(account_id: Any) -> Optional[Dict[str, Any]]:
    """账号级 AI 开关判据（任务 B3）；返回结构化拒绝信封或 None（放行）。

    `accounts.enabled=0`（账号未启用）或 `accounts.auto_reply=0`（用户关了自动回复）
    时**不生成也不发送** AI 回复 —— 人工回复不受影响（`send_manual` 不查这里）。

    **账号行不存在时不拦**：服务层会被测试/脚本直接用 `ensure_conversation` 建会话，
    没有 `accounts` 行不等于「用户关了开关」；真机里账号一定在库里（C3 已校验入站账号）。
    """
    account_id = str(account_id or '')
    if not account_id:
        return None
    row = db.query_one('SELECT enabled, auto_reply FROM accounts WHERE id = ?', (account_id,))
    if row is None:
        return None
    if not int(row['enabled'] or 0):
        return {
            'success': False,
            'code': ACCOUNT_OFF_CODE,
            'message': f'账号 {account_id} 未启用（enabled=0），AI 不自动回复',
        }
    if not int(row['auto_reply'] or 0):
        return {
            'success': False,
            'code': ACCOUNT_OFF_CODE,
            'message': f'账号 {account_id} 已关闭自动回复（auto_reply=0），AI 不自动回复',
        }
    return None


def _account_exists(account_id: Any) -> bool:
    """账号是否在 `accounts` 表里（任务 C3：不存在就不许建会话）。"""
    account_id = str(account_id or '')
    if not account_id:
        return False
    return db.query_one('SELECT 1 AS ok FROM accounts WHERE id = ?', (account_id,)) is not None


def _ai_role_of(reply: Optional[Dict[str, Any]], conversation: Dict[str, Any]) -> str:
    """AI 消息该挂哪个 sender 角色。

    编排层返回的 `reply['role']` 是**真正跑出来的角色**（vision/bargain/cs/item），
    优先用它；取不到再退回会话的 `agent_role`，最后退回 `item`。
    A2 之前这里只看会话的 `agent_role`，所以角色改造后消息 sender 会一直显示旧角色。
    """
    for candidate in (
        (reply or {}).get('role'),
        conversation.get('agent_role'),
    ):
        role = str(candidate or '').strip()
        if role:
            return role
    return 'item'


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
    started = time.monotonic()
    try:
        result = _normalize_ai_result(common.run_awaitable(handler(int(conversation['id']), int(message_id))))
    except Exception as exc:  # AI 出错绝不能影响买家消息落库
        return {'success': False, 'reply': '', 'message': f'AI 回复失败: {exc}', 'code': 'ai_error'}
    # 真实耗时由这里量（编排层只回 model/usage，不回耗时）：前端 AI 徽章显示它。
    result.setdefault('elapsed_ms', int((time.monotonic() - started) * 1000))
    return result


def _ai_message_meta(reply: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """AI 消息的来源标记（写进 `messages.meta`，前端气泡上的「AI」徽章读它）。

    拿不到就**不写**：宁可徽章只写「AI」，也不要编一个模型名出来。
    """
    reply = reply or {}
    meta: Dict[str, Any] = {'ai_generated': True}
    model = str(reply.get('model') or reply.get('ai_model') or '').strip()
    if model:
        meta['ai_model'] = model
    elapsed = reply.get('elapsed_ms')
    if elapsed not in (None, ''):
        meta['ai_elapsed_ms'] = elapsed
    role = str(reply.get('role') or '').strip()
    if role:
        meta['ai_role'] = role
    return meta


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
    msg_id: str = '',
    meta: Optional[Dict[str, Any]] = None,
) -> Any:
    """买家来信入口：落消息 -> 若会话是 auto 且 AI 可用，生成并落一条 AI 回复。

    - `msg_id`（平台消息 id）给了就**幂等**：同一条消息重复送达不会重复落库、
      也不会重复触发 AI，返回里带 `duplicate: true` 和已存在的那条消息。
      去重标记写进 `messages.meta.external_id`，**不新增列**。
    - 编排层（`backend.app.agents.runtime.reply_for_message`，async）未就绪时只落买家消息。
    - **要新建会话时必须先确认账号存在**（任务 C3）：`account_id='nope_acc'` 以前
      会 200 并凭空建出一个「幽灵会话」，出现在对话列表里。
    - 不生成 AI 时也会带回**结构化原因**（`ai_result.code`）：
      `mode_not_auto`（人工接管/关闭）或 `account_auto_reply_off`（账号级开关）。
    """
    if _find_conversation(conversation_id) is None:
        if not (account_id and peer_user_id):
            return common.fail(f'会话不存在: {conversation_id}', 'not_found')
        if not _account_exists(account_id):
            return common.fail(f'账号不存在: {account_id}', 'not_found')
        # 不预写死 agent_role（任务 A2）：留给 roles 的规则 2/3 去选 vision / bargain。
        conversation = ensure_conversation(
            account_id, peer_user_id, peer_nickname, item_id,
        )
    else:
        conversation = get_conversation(conversation_id)

    duplicate = find_incoming_by_external_id(conversation['id'], msg_id)
    if duplicate is not None:
        return {
            'message': duplicate,
            'ai_reply': None,
            'ai_result': None,
            'mode': conversation['mode'],
            'duplicate': True,
        }

    incoming_meta = dict(meta or {})
    if msg_id:
        incoming_meta[EXTERNAL_ID_KEY] = str(msg_id)
    incoming = append_message(
        conversation['id'], 'buyer', content, item_id=item_id,
        meta=incoming_meta or None,
    )
    if common.is_failure(incoming):
        return incoming

    mode = str(conversation.get('mode') or 'auto')
    # 系统/卡片消息落库但不该让 AI 开口（见 `SYSTEM_MSG_TYPES`）。判据放在 `mode` 分支
    # 之外，和 `MODE_NOT_AUTO_CODE` 同一个理由：`POST /api/chats/incoming` 也要能拿到
    # 这个 code，而不是只在 IM 入站路径上生效。
    incoming_type = str(incoming_meta.get('msg_type') or '').strip().lower()
    system_like = incoming_type in SYSTEM_MSG_TYPES
    # 判据提在 `mode == 'auto'` 分支**外面**（任务 C5）：以前 `mode_not_auto` 只写在
    # `deliver_ai_reply` 里，而它唯一的生产调用方在 auto 分支内 -> 那个 code 生产不可达。
    if mode != 'auto':
        ai_result: Optional[Dict[str, Any]] = {
            'success': False,
            'code': MODE_NOT_AUTO_CODE,
            'message': f'会话处于 {mode} 模式，AI 不自动回复',
        }
    elif system_like:
        ai_result = {
            'success': False,
            'code': SYSTEM_MESSAGE_CODE,
            'message': '这条不是买家说的话（系统/卡片消息），AI 不自动回复',
        }
    else:
        ai_result = _account_reply_gate(conversation.get('account_id'))

    reply_info: Optional[Dict[str, Any]] = None
    ai_message = None
    ai_send: Optional[Dict[str, Any]] = None
    if mode == 'auto' and ai_result is None:
        # 编排层签名：async reply_for_message(conversation_id, message_id) —— 只生成不发送
        reply = _ai_reply(conversation, incoming['id'])
        if reply is not None:
            reply_info = reply
            reply_text = str(reply.get('reply') or '') if reply.get('success') else ''
            if reply_text:
                ai_message = append_message(
                    conversation['id'], f'ai:{_ai_role_of(reply, conversation)}',
                    reply_text, ai_generated=True, sent_ok=False, mark_unread=False,
                    meta=_ai_message_meta(reply),
                )
                common.publish('chats.ai_reply', {
                    'conversation_id': conversation['id'], 'content': reply_text,
                })
                # 生成即投递：AI 回复必须真的发回闲鱼，而不是停在库里 sent_ok=0。
                cid = str(incoming_meta.get('cid') or '')
                ai_send = deliver_ai_reply(
                    conversation['id'], ai_message['id'], reply_text, cid=cid,
                )
                refreshed = ai_send.pop('message_row', None)
                if refreshed is not None:
                    ai_message = refreshed
        # AI 真跑了就用它的结构化结果；AI 压根没生成（编排层未就绪）时保留开关判据。
        if reply_info is not None:
            ai_result = reply_info

    return {
        'message': incoming,
        'ai_reply': ai_message,
        'ai_result': ai_result,
        'ai_send': ai_send,
        'mode': conversation['mode'],
        'duplicate': False,
    }


# --------------------------------------------------------------------------- AI 回复投递


def _set_message_meta(message_id: Any, key: str, value: Any) -> None:
    """改 `messages.meta` 里的一个键（值给空串就删掉这个键）。不动 SCHEMA。"""
    row = db.query_one('SELECT meta FROM messages WHERE id = ?', (message_id,))
    if row is None:
        return
    try:
        stored = json.loads(row['meta'] or '{}') or {}
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    if value in (None, ''):
        stored.pop(key, None)
    else:
        stored[key] = value
    db.execute('UPDATE messages SET meta = ? WHERE id = ?', (json.dumps(stored, ensure_ascii=False), message_id))


def _account_in_risk_cooldown(account_id: str) -> bool:
    """协议层内存风控锁是否锁着这个账号（cookie_id 就是 account_id）。

    协议层只在 mtop 链路开头查锁，`send_message` 不走 mtop —— 服务层不先查一遍，
    冷却期内会继续往外发消息，把风控从 60s 拖到更长的档位。
    """
    if not account_id:
        return False
    module = common.protocol_module('risk')
    registry = getattr(module, 'registry', None) if module is not None else None
    guard = registry.get(account_id) if registry is not None else None
    return bool(guard is not None and guard.is_blocked)


def _looks_like_risk_error(value: Any) -> bool:
    module = common.protocol_module('risk')
    checker = getattr(module, 'is_risk_control_error', None) if module is not None else None
    return bool(callable(checker) and checker(str(value)))


def _finish_ai_send(
    conversation: Dict[str, Any],
    message_id: Any,
    *,
    sent: bool,
    code: str,
    message: str,
    risk: bool = False,
) -> Dict[str, Any]:
    """把发送结果如实落库 + 留痕，并返回投递结果。

    失败**不许静默**：AI 回复停在 `sent_ok=0` 而不写原因，UI 上就是
    「AI 好像回了、买家又说没收到」这类最难查的问题。
    """
    account_id = str(conversation.get('account_id') or '')
    message_row: Optional[Dict[str, Any]] = None
    if message_id not in (None, '', 0):
        db.execute('UPDATE messages SET sent_ok = ? WHERE id = ?', (1 if sent else 0, message_id))
        _set_message_meta(message_id, 'send_error', '' if sent else message)
        row = db.query_one('SELECT * FROM messages WHERE id = ?', (message_id,))
        if row is not None:
            message_row = _row_to_message(row)

    common.log_operation(
        account_id, 'ai_reply_send', str(conversation.get('item_id') or ''), sent,
        {
            'conversation_id': conversation.get('id'),
            'message_id': message_id,
            'code': code,
            'message': message,
        },
    )
    if risk:
        common.add_risk_event(account_id, 'ai_reply_blocked', message or code)
    return {'sent': sent, 'code': code, 'message': message, 'message_row': message_row}


def deliver_ai_reply(
    conversation_id: Any,
    message_id: Any,
    text: str,
    *,
    cid: str = '',
) -> Dict[str, Any]:
    """把一条已落库的 AI 回复真的发回闲鱼，并如实回写结果。

    返回 `{'sent','code','message','message_row'}`（`message_row` 是刷新后的消息行，
    调用方用它覆盖落库时那条 `sent_ok=False` 的旧快照）。

    不发送的情况都必须留下原因：`mode != 'auto'`（人工接管 / 关闭）、
    账号级开关关掉（`enabled=0` / `auto_reply=0`，任务 B3）、
    缺平台会话 id（HTTP 手动灌入的来信没有 cid）、账号正在风控冷却中。

    这是投递侧的第二道账号级闸门：即使调用方绕开 `handle_incoming` 直接投递，
    账号开关关掉时也不许发出去。
    """
    row = _find_conversation(conversation_id)
    if row is None:
        return {'sent': False, 'code': 'not_found', 'message': f'会话不存在: {conversation_id}',
                'message_row': None}
    conversation = _row_to_conversation(row)
    account_id = str(conversation.get('account_id') or '')
    text = str(text or '').strip()

    if conversation.get('mode') != 'auto':
        return _finish_ai_send(
            conversation, message_id, sent=False, code=MODE_NOT_AUTO_CODE,
            message=f'会话处于 {conversation.get("mode")} 模式，AI 回复不自动发送',
        )
    gate = _account_reply_gate(account_id)
    if gate is not None:
        return _finish_ai_send(
            conversation, message_id, sent=False,
            code=str(gate.get('code') or ACCOUNT_OFF_CODE),
            message=str(gate.get('message') or '账号级开关已关闭 AI 回复，未发送'),
        )
    if not text:
        return _finish_ai_send(
            conversation, message_id, sent=False, code='empty_text', message='AI 回复为空，未发送',
        )
    if not str(cid or ''):
        return _finish_ai_send(
            conversation, message_id, sent=False, code=MISSING_CID_CODE,
            message='缺少平台会话 id（cid），AI 回复未发送',
        )
    if _account_in_risk_cooldown(account_id):
        return _finish_ai_send(
            conversation, message_id, sent=False, code='risk_cooldown', risk=True,
            message='账号处于风控冷却中，AI 回复未发送（不自动重试）',
        )

    client, err = common.get_client(account_id)
    if client is None:
        failure = err or common.not_ready()
        return _finish_ai_send(
            conversation, message_id, sent=False,
            code=str(failure.get('code') or 'xianyu_not_ready'),
            message=str(failure.get('message') or '协议层尚未就绪，AI 回复未发送'),
        )

    # 真客户端签名是 send_message(cid, text, toid=None)，toid 省略时=cid。
    status, value = common.call_method(client, ('send_message',), cid, text)
    if status == common.CALL_OK and not (isinstance(value, dict) and value.get('success') is False):
        return _finish_ai_send(conversation, message_id, sent=True, code='ok', message='')

    if status == common.CALL_MISSING:
        code, message = 'xianyu_not_ready', '协议层尚未实现 send_message，AI 回复未发送'
    else:
        code, message = 'protocol_error', f'AI 回复发送失败: {value}'
    risk = _looks_like_risk_error(value)
    if risk:
        code = 'risk_blocked'
    return _finish_ai_send(
        conversation, message_id, sent=False, code=code, message=message, risk=risk,
    )


# --------------------------------------------------------------------------- IM 入站分发

#: account_id -> 入站 handler。按账号缓存同一函数对象，保证 attach 幂等（见下）。
_IM_HANDLERS: Dict[str, Any] = {}


def im_handler_for(account_id: str):
    """造一个「一帧 IM 推送 -> 落库 + 触发 AI」的回调，交给协议层注册。

    回调必须是**无状态、不抛异常**的：它跑在 IM 读循环里，抛异常会被协议层吞掉
    （见 `xianyu.im.ImSession.dispatch`），但不能因此让后续消息也收不到。

    按 account_id 缓存**同一个函数对象**：回调无状态，而 `ImSession.on_message`
    按对象去重 —— 每次 attach 都造新闭包的话，同一会话会被挂上多份 handler，
    一封来信就会触发多次 AI。
    """
    account_id = str(account_id or '')
    handler = _IM_HANDLERS.get(account_id)
    if handler is None:
        def handler(payload: Any) -> Any:  # noqa: F811 - 有意复用上面查到的名字
            return dispatch_im_message(account_id, payload)

        _IM_HANDLERS[account_id] = handler
    return handler


#: 平台**例行**推送的帧类型（同步位点 `/s/sync`、商品草稿推送 `/s/vulcan`、心跳回帧）。
#: 真机实测它们永远不含买家消息，日志按 DEBUG 记（见 `_warn_unparsed_frame`）。
NON_MESSAGE_FRAME_TYPES = ('/s/sync', '/s/vulcan', '')


def _warn_unparsed_frame(im_module: Any, payload: Any, reason: str) -> None:
    """认不出的入站帧留一条 WARNING：type + 截断 body。

    宁可漏落一条买家消息，也不要凭空造出 buyer_id 为空的会话；但**静默跳过是不行的**
    —— 真机上唯一的线索就是这条日志（首次上线只能靠它反推真实帧形状）。
    """
    summarize = getattr(im_module, 'frame_summary', None) if im_module is not None else None
    info: Dict[str, Any] = {}
    if callable(summarize):
        try:
            info = summarize(payload) or {}
        except Exception:  # noqa: BLE001 - 记日志本身不许把读循环搞挂
            info = {}
    if not info:
        info = {'type': type(payload).__name__, 'body': repr(payload)[:200]}
    # 平台例行推送按 DEBUG 记：真机上每 30 秒一次心跳回帧、每分钟左右一次同步包，
    # 全是 WARNING 会把真正的「认不出的买家消息」淹掉（首上线唯一的线索就是那条）。
    if str(info.get('type') or '') in NON_MESSAGE_FRAME_TYPES:
        logger.debug(
            'IM 例行帧（{}）: type={} body={}',
            reason, info.get('type', ''), info.get('body', ''),
        )
        return
    logger.warning(
        'IM 入站帧已跳过（{}）: type={} body={}',
        reason, info.get('type', ''), info.get('body', ''),
    )


#: 图片地址在 IM 帧里可能用的键名（真机形状未定，见 docs/audit/chats.md §11）。
IMAGE_KEYS = ('image_url', 'imageUrl', 'image', 'picUrl', 'pic_url', 'itemImage', 'imageUrls')

#: 只认图片扩展名，避免把帧里随便一个 `url` 字段当成图片地址。
_IMAGE_URL_RE = re.compile(r'^https?://\S+\.(?:jpe?g|png|webp|gif|bmp)(?:\?\S*)?$', re.I)


def _image_value(value: Any) -> str:
    """把 `IMAGE_KEYS` 命中位置的值解释成图片地址（字符串 / 字符串列表 / 对象 url）。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ''
        if text[:1] in ('{', '['):
            try:
                return _first_image_url(json.loads(text))
            except (TypeError, ValueError):
                return ''
        return text if _IMAGE_URL_RE.match(text) else ''
    if isinstance(value, dict):
        for key in ('url', 'src', 'image_url', 'imageUrl'):
            if key in value:
                found = _image_value(value[key])
                if found:
                    return found
        return ''
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _image_value(item)
            if found:
                return found
        return ''
    return ''


def _first_image_url(payload: Any, depth: int = 6) -> str:
    """从一帧原始推送里尽力抠出图片地址；找不到返回空串。

    IM 帧的真实形状没有文档（`parse_incoming_message()` 也只回文本字段），
    所以这里做**有界**递归：只认 `IMAGE_KEYS` 里的键，且值必须是 http(s) 图片地址。
    宁可返回空串（vision 不触发），也不要把无关的 url 当成商品图送进模型。
    """
    if depth <= 0:
        return ''
    if isinstance(payload, dict):
        for key in IMAGE_KEYS:
            if key in payload:
                found = _image_value(payload[key])
                if found:
                    return found
        for value in payload.values():
            found = _first_image_url(value, depth - 1)
            if found:
                return found
        return ''
    if isinstance(payload, (list, tuple)):
        for value in payload:
            found = _first_image_url(value, depth - 1)
            if found:
                return found
        return ''
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ''
        if text[:1] in ('{', '['):
            try:
                return _first_image_url(json.loads(text), depth - 1)
            except (TypeError, ValueError):
                return ''
        return text if _IMAGE_URL_RE.match(text) else ''
    return ''


#: 一次回填最多拉多少会话、每个会话最多拉多少条消息（防止一次点「同步」打出几十个请求）。
IM_SYNC_MAX_CONVERSATIONS = 50
IM_SYNC_MESSAGE_LIMIT = 20


def _stored_external_id(conversation_id: Any, external_id: str, direction: str = '') -> bool:
    """这条平台消息 id 是否已经落过库（对**两个方向**都判，回填时用）。"""
    params: List[Any] = [str(conversation_id), f'%{external_id}%']
    clause = ''
    if direction:
        clause = ' AND direction = ?'
        params.append(direction)
    rows = db.query(
        f'SELECT meta FROM messages WHERE conversation_id = ? AND meta LIKE ?{clause}',
        params,
    )
    for row in rows:
        try:
            stored = json.loads(row['meta'] or '{}') or {}
        except (TypeError, ValueError):
            continue
        if str(stored.get(EXTERNAL_ID_KEY) or '') == external_id:
            return True
    return False


def sync_im_conversations(
    account_id: str,
    limit: int = IM_SYNC_MAX_CONVERSATIONS,
    message_limit: int = IM_SYNC_MESSAGE_LIMIT,
) -> Dict[str, Any]:
    """把平台上的**历史会话与最近消息回填进本地库**（幂等）。

    为什么必须有它：IM 入站只处理「正在发生」的推送帧，进程启动之前就存在于平台上
    的会话**永远不会自己进库** —— 用户看到的现象就是「我账号上面的消息一个没读取
    过来」（真机实测：账号有历史会话，而本地 `conversations` 表 0 行）。

    去重口径（重复点「同步会话」不会把消息刷成一片重复）：
    - 会话按 `(account_id, peer_user_id)` upsert（`ensure_conversation`）；
    - 消息按平台 msg id 存进 `meta.external_id`，插入前先查一次，
      出站回显（`is_self`）与入站来信都查，避免同一条被记两遍。

    返回 `{'account_id','scanned','conversations','conversations_new','messages',
    'messages_skipped','skipped','nicknames_fixed'}`，单个会话拉消息失败时把原因收进
    `warnings` 而不是整个失败（会话已经入库了，如实报出来比丢掉更有用）。失败时返回统一信封。

    昵称口径（任务 21）：`peer_nickname` **只允许是买家的昵称**。
    - 会话条目里的 `buyer_name` 由协议层确认过发送者不是自己，拿不到就是空串；
    - 优先用**买家发来的消息**里的昵称；
    - 已有会话只在「库里空着」或「库里存的正是账号自己的昵称」时才覆盖，
      `nicknames_fixed` 报出这次修正了几条（用户库里现在这一列全是自己的名字）。
    """
    account_id = str(account_id or '')
    if not account_id:
        return common.fail('缺少 account_id', 'invalid_fields')
    client, failure = common.get_client(account_id)
    if failure is not None:
        return failure

    limit = max(1, min(int(limit or IM_SYNC_MAX_CONVERSATIONS), 200))
    message_limit = max(0, min(int(message_limit or IM_SYNC_MESSAGE_LIMIT), 100))

    status, value = common.call_method(client, ('fetch_conversations',), limit=limit)
    envelope = common.call_failed(status, value)
    if envelope is not None:
        return envelope
    rows = common.extract_list(value, 'userConvs', 'conversations')

    conversations_new = 0
    messages_new = 0
    messages_skipped = 0
    skipped = 0
    warnings: List[str] = []
    # 账号自己的昵称：会话列表里那个「客户名」整列变成用户自己，就是没有它做比对。
    my_nick = _account_nickname(account_id)
    # 会话 id -> 候选买家昵称（先放会话条目里的，再被**买家消息**里的覆盖 = 后者更可信）
    nick_candidates: Dict[int, str] = {}

    for raw in rows:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        buyer_id = str(raw.get('buyer_id') or '')
        if not buyer_id:
            # 宁可少一条会话，也不要凭空造一个 buyer_id 为空的会话（与入站同一口径）。
            skipped += 1
            continue

        existed = db.query_one(
            'SELECT id FROM conversations WHERE account_id = ? AND peer_user_id = ?',
            (account_id, buyer_id),
        ) is not None
        candidate_nick = peer_nickname_candidate(
            raw.get('buyer_name'), buyer_id=buyer_id, my_nick=my_nick,
        )
        conversation = ensure_conversation(
            account_id,
            buyer_id,
            # 新会话才在建立时写昵称；**已存在**的会话一律走下面的 `apply_peer_nickname`
            # （只有「空着」或「存的是账号自己的昵称」时才覆盖），免得一个来路不明的名字
            # 把库里正确的买家名冲掉。
            peer_nickname='' if existed else candidate_nick,
            item_id=str(raw.get('item_id') or ''),
        )
        if existed and candidate_nick:
            nick_candidates[int(conversation['id'])] = candidate_nick
        if not existed:
            conversations_new += 1

        unread = int(raw.get('unread') or 0)
        if unread > int(conversation.get('unread') or 0):
            db.execute('UPDATE conversations SET unread = ? WHERE id = ?',
                       (unread, conversation['id']))

        cid = str(raw.get('cid') or '')
        if not (cid and message_limit):
            continue

        status, value = common.call_method(
            client, ('fetch_messages',), cid, limit=message_limit,
        )
        envelope = common.call_failed(status, value)
        if envelope is not None:
            warnings.append(f'会话 {buyer_id} 的历史消息拉取失败: {envelope.get("message")}')
            continue

        items = [one for one in common.extract_list(value, 'messages') if isinstance(one, dict)]
        # 平台按时间倒序回，落库按时间正序，消息列表读起来才是对话顺序。
        try:
            items.sort(key=lambda one: int(one.get('time') or 0))
        except (TypeError, ValueError):
            pass

        for item in items:
            content = str(item.get('content') or '')
            images = [str(url) for url in (item.get('images') or []) if url]
            audio_url = str(item.get('audio_url') or '').strip()
            if not content and not images and not audio_url:
                continue
            msg_id = str(item.get('msg_id') or '')
            if msg_id and _stored_external_id(conversation['id'], msg_id):
                messages_skipped += 1
                continue
            meta: Dict[str, Any] = {'source': 'im', 'cid': cid}
            if msg_id:
                meta[EXTERNAL_ID_KEY] = msg_id
            if images:
                # 图片地址必须落进 meta（角色判定靠它选 vision，见任务 A2；前端缩略图靠 images）。
                meta['images'] = images
                meta['image_url'] = images[0]
            if audio_url:
                meta['audio_url'] = audio_url
            if item.get('audio_duration') not in (None, ''):
                meta['audio_duration'] = item.get('audio_duration')
            if str(item.get('msg_type') or '').strip():
                meta['msg_type'] = str(item.get('msg_type')).strip()
            is_self = bool(item.get('is_self'))
            # 昵称：只有**买家发来的消息**才作数（`sender_nick` 已由协议层确认发送者不是
            # 自己）。自己发的消息里的昵称反过来用来学习「账号自己的昵称」，这样下一次同步
            # 就能认出库里存的到底是不是自己的名字。
            if is_self:
                my_nick = my_nick or str(item.get('sender_nick_raw') or '').strip()
            else:
                buyer_nick = peer_nickname_candidate(
                    item.get('sender_nick'), buyer_id=buyer_id, my_nick=my_nick,
                )
                if buyer_nick:
                    # 消息按时间正序，后赋值的 = 更新的那条买家消息
                    nick_candidates[int(conversation['id'])] = buyer_nick
            item_id = str(item.get('item_id') or raw.get('item_id') or '')
            result = append_message(
                conversation['id'],
                'human' if is_self else 'buyer',
                content,
                item_id=item_id,
                meta=meta,
                sent_ok=True if is_self else None,
                # 回填的是历史消息，不能把未读数再加一遍（unread 已按平台 redPoint 对齐）。
                mark_unread=False,
            )
            if isinstance(result, dict) and result.get('id'):
                messages_new += 1

    # 修正库里**已有的错昵称**（用户现在这一列全是自己的名字，就是老代码写的）。
    # 只认「空着」或「存的正是账号自己昵称」的，别把真买家名覆盖掉。
    nicknames_fixed = 0
    for conv_id, nick in nick_candidates.items():
        if apply_peer_nickname(conv_id, nick, my_nick=my_nick):
            nicknames_fixed += 1

    summary: Dict[str, Any] = {
        'account_id': account_id,
        'scanned': len(rows),
        'conversations_new': conversations_new,
        'messages': messages_new,
        'messages_skipped': messages_skipped,
        'skipped': skipped,
        'nicknames_fixed': nicknames_fixed,
    }
    if warnings:
        summary['warnings'] = warnings
    logger.info(
        'sync_im_conversations: account={} scanned={} new_conversations={} new_messages={} '
        'nicknames_fixed={}',
        account_id, len(rows), conversations_new, messages_new, nicknames_fixed,
    )
    return summary


def dispatch_im_message(account_id: str, payload: Any) -> Optional[Any]:
    """一帧 IM 推送 -> `handle_incoming`。

    不是买家文本消息（心跳/协议回帧/字段不全）时返回 None —— 宁可漏落一条，
    也不要凭空造出一个 buyer_id 为空的会话。
    """
    im_module = common.protocol_module('im')
    parse = getattr(im_module, 'parse_incoming_message', None) if im_module is not None else None
    if not callable(parse):
        _warn_unparsed_frame(im_module, payload, '协议层 IM 未就绪')
        return None
    parsed = parse(payload)
    if not parsed:
        _warn_unparsed_frame(im_module, payload, '不是可处理的买家文本消息')
        return None

    buyer_id = str(parsed.get('buyer_id') or '')
    content = str(parsed.get('content') or '')
    # 图片/语音消息**可能没有文字**（买家只发一张图）：只要三者有一个就不算空帧。
    # 用 `.get(...) or []` 这种防御写法 —— 协议层（task-43）还没落地时这些键根本不存在。
    images = [str(url).strip() for url in (parsed.get('images') or []) if str(url or '').strip()]
    audio_url = str(parsed.get('audio_url') or '').strip()
    audio_duration = parsed.get('audio_duration')
    msg_type = str(parsed.get('msg_type') or '').strip()
    if not (account_id and buyer_id and (content or images or audio_url)):
        _warn_unparsed_frame(im_module, payload, '缺少 account_id/buyer_id/正文或媒体')
        return None

    item_id = str(parsed.get('item_id') or '')
    # 不预写死 agent_role（任务 A2）：留给 roles 的规则 2/3 去选 vision / bargain。
    conversation = ensure_conversation(account_id, buyer_id, item_id=item_id)
    # 昵称：入站帧里的昵称是**发送者**的，先按「不是账号自己」过滤；只在这个会话还没有
    # 买家名（或库里存的是账号自己）时才写，绝不拿自己的昵称当客户名。
    my_nick = _account_nickname(account_id)
    inbound_nick = peer_nickname_candidate(
        parsed.get('sender_nick'), buyer_id=buyer_id, my_nick=my_nick,
    )
    if inbound_nick:
        written = apply_peer_nickname(conversation['id'], inbound_nick, my_nick=my_nick)
        if written:
            conversation['peer_nickname'] = written
    incoming_meta: Dict[str, Any] = {
        'source': 'im',
        'cid': str(parsed.get('cid') or ''),
    }
    # 图片地址必须透传（任务 A2）：`roles.role_for_conversation()` 靠
    # `messages.meta.image_url` 才会选 `vision`，`runtime` 也靠它塞 image_url 消息块。
    # 协议层（task-43）给了 `images` 列表就用它，否则从**原始帧**里再抠一次。
    if images:
        incoming_meta['images'] = images
    image_url = str(parsed.get('image_url') or parsed.get('image') or '') or _first_image_url(payload)
    if not image_url and images:
        image_url = images[0]
    if image_url:
        incoming_meta['image_url'] = image_url
    if audio_url:
        incoming_meta['audio_url'] = audio_url
    if audio_duration not in (None, ''):
        incoming_meta['audio_duration'] = audio_duration
    if msg_type:
        incoming_meta['msg_type'] = msg_type
    return handle_incoming(
        conversation['id'], content,
        item_id=item_id,
        msg_id=str(parsed.get('msg_id') or ''),
        meta=incoming_meta,
    )


#: session 上记「已给哪些账号挂过 handler」的属性名（不依赖 on_message 的去重语义）。
ATTACHED_ACCOUNTS_KEY = '_xianyu_ai_attached_accounts'


def attach_im_session(session: Any, account_id: str) -> Any:
    """把服务层入站 handler 注册到一条 `ImSession` 上（**幂等**）。

    协议层只负责「收到非配对帧就调回调」，业务归属（account_id）由服务层带上 ——
    这是「协议层不 import services」的反向注入点。同一个会话重复 attach（例如
    `im_service.start()` 与客户端钩子各来一次）只保留一份 handler。
    """
    register = getattr(session, 'on_message', None)
    if not callable(register):
        return session

    account_id = str(account_id or '')
    attached = getattr(session, ATTACHED_ACCOUNTS_KEY, None)
    if not isinstance(attached, set):
        attached = set()
        try:
            setattr(session, ATTACHED_ACCOUNTS_KEY, attached)
        except Exception:  # noqa: BLE001 - 挂不上属性（__slots__ 会话）时退回 on_message 去重
            attached = None
    if attached is not None:
        if account_id in attached:
            return session
        attached.add(account_id)

    register(im_handler_for(account_id))
    return session


def send_from_args(conversation_id: Any, content: str, *, confirm: Any = None) -> Any:
    """工具 `chats_send` 的 `content` → `send_manual(conversation_id, text, confirm=...)`。

    主管 AI 要给买家发消息也必须显式 `confirm=true`（工具规格里已进 `required`）。
    """
    return send_manual(conversation_id, content, confirm=confirm)

