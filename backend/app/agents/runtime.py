"""多 AI 运行时：买家消息 → 角色 AI 回复、主管 AI 对话、跨商品协查。

这是「多 AI 编排」的执行层：

    reply_for_message()  买家消息进来 → 挑角色（item/cs/bargain/vision）→ 生成回复
    run_master()         卖家跟主管 AI 说话 → 主管 AI 调工具干活（可多轮 tool calling）
    cross_ask()          跨商品协查：买家在 A 商品里问 B 商品 → 去问 B 的商品 AI → 汇总回来

跨商品协议的每一次投递都写 `agent_messages`（kind=ask|answer|notify），前端「AI 之间沟通」页直接读它。
深度上限 2、单跳超时 3s（`docs/CONTRACTS.md` §4）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from ..core import db, events
from . import llm, prompts, roles as roles_mod, tools as tools_mod

MAX_CROSS_DEPTH = 2
CROSS_TIMEOUT = 3.0
HISTORY_LIMIT = 12
CROSS_MAX_TARGETS = 3


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _loads(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or '')
    except (TypeError, ValueError):
        return default


def record_agent_message(
    from_agent: str,
    to_agent: str,
    kind: str,
    *,
    topic: str = '',
    payload: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[int] = None,
) -> int:
    """落一条 AI 之间的消息，并推事件给前端。返回行 id。"""
    message_id = int(db.execute(
        'INSERT INTO agent_messages(conversation_id, from_agent, to_agent, kind, topic, payload) '
        'VALUES(?,?,?,?,?,?)',
        (conversation_id, from_agent, to_agent, kind, topic, _dumps(payload or {})),
    ))
    events.publish('agent.message', {
        'id': message_id,
        'conversation_id': conversation_id,
        'from': from_agent,
        'to': to_agent,
        'kind': kind,
        'topic': topic,
    })
    return message_id


def recent_agent_messages(limit: int = 100, conversation_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """AI 之间沟通的记录（新的在前），前端「AI 沟通」页用。"""
    sql = 'SELECT * FROM agent_messages'
    params: List[Any] = []
    if conversation_id is not None:
        sql += ' WHERE conversation_id = ?'
        params.append(int(conversation_id))
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    rows = list(db.query(sql, params))
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item['payload'] = _loads(item.get('payload'), {})
        out.append(item)
    return out


def _conversation(conversation_id: int):
    return db.query_one('SELECT * FROM conversations WHERE id = ?', (int(conversation_id),))


def _message(message_id: int):
    return db.query_one('SELECT * FROM messages WHERE id = ?', (int(message_id),))


def _item_context(item_id: Optional[str]) -> str:
    if not item_id:
        return ''
    row = db.query_one(
        'SELECT item_id, title, price, original_price, stock, status, description FROM items WHERE item_id = ? '
        'ORDER BY id DESC LIMIT 1',
        (str(item_id),),
    )
    if row is None:
        return ''
    parts = [f"商品标题：{row['title'] or '（未填）'}", f"当前售价：{row['price']} 元"]
    if row['original_price']:
        parts.append(f"原价：{row['original_price']} 元")
    parts.append(f"库存：{row['stock']}")
    if row['description']:
        parts.append(f"商品描述：{str(row['description'])[:500]}")
    return '【你正在服务的商品】\n' + '\n'.join(parts)


def _history(conversation_id: int, limit: int = HISTORY_LIMIT) -> List[Dict[str, str]]:
    rows = db.query(
        'SELECT direction, sender, content FROM messages WHERE conversation_id = ? '
        'AND msg_type != \'system\' ORDER BY id DESC LIMIT ?',
        (int(conversation_id), int(limit)),
    )
    out: List[Dict[str, str]] = []
    for row in reversed(list(rows)):
        who = '买家' if row['direction'] == 'in' else ('卖家' if row['sender'] == 'human' else '我方AI')
        content = str(row['content'] or '').strip()
        if content:
            out.append({'role': 'user' if row['direction'] == 'in' else 'assistant',
                        'content': f'{who}：{content}'})
    return out


def _temperature(role: str) -> float:
    return roles_mod.role_temperature(role)


# --------------------------------------------------------------------------
# 商品匹配（跨商品协查用）
# --------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r'[\s,，。；;、!！?？:：/\\|()（）\[\]【】"\'`~\-—_+*]+')


def _keywords(text: str) -> List[str]:
    tokens = [t for t in _TOKEN_SPLIT.split(str(text or '')) if len(t) >= 2]
    return tokens


def _ngrams(text: str, n: int = 2) -> set:
    """去空白后的字符 n-gram。中文没有空格，靠 n-gram 才能匹配「牛仔裤」这种子串。"""
    s = re.sub(r'\s+', '', str(text or ''))
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


# 命中多少个 2-gram 才算「买家在问这个商品」，太低会把通用词（全新、包邮）算进来
_MATCH_MIN_SCORE = 2

# 这些词几乎每个商品都有，不能作为「买家在问这个商品」的依据。
# 成色词（九成新/八成新…）尤其重要：买家一句「是九成新吗」本身不含任何商品名，
# 如果只从**标题**里剔停用词、不剔问题里的，标题里的「九成新」两个 2-gram 会凑满
# `_MATCH_MIN_SCORE`，于是对一句纯问成色的消息发起跨商品协查（误命中多个商品）。
_MATCH_STOPWORDS = {
    '全新', '包邮', '正品', '现货', '闲置', '专柜', '全新未', '可小刀', '支持', '价格', '便宜',
    '还有', '多少', '钱吗', '有货', '库存', '发发', '发货',
    '九成新', '八成新', '七成新', '六成新', '五成新', '几成新', '成色', '成新', '新旧', '二手',
}


def _clean_for_match(text: str) -> str:
    """两侧（问题与标题）都必须用同一套停用词清洗，否则通用词会变成命中。"""
    cleaned = str(text or '').lower()
    for word in _MATCH_STOPWORDS:
        cleaned = cleaned.replace(word.lower(), '')
    return cleaned


def match_items(question: str, exclude_item_id: Optional[str] = None,
                candidate_items: Optional[List[str]] = None,
                limit: int = CROSS_MAX_TARGETS) -> List[Dict[str, Any]]:
    """在 `items` 表里找买家可能问到的**其它**商品。

    给了 `candidate_items` 就按 id 精确查；否则按标题关键词在问题里的命中数排序。
    """
    exclude = str(exclude_item_id or '')
    if candidate_items:
        ids = [str(i) for i in candidate_items if str(i) and str(i) != exclude]
        if not ids:
            return []
        placeholders = ','.join('?' for _ in ids)
        rows = db.query(
            f'SELECT account_id, item_id, title, price, stock, status FROM items '
            f'WHERE item_id IN ({placeholders})',
            ids,
        )
        return [dict(r) for r in rows if str(r['item_id']) != exclude][:limit]

    cleaned_question = _clean_for_match(question)
    if not cleaned_question.strip():
        return []
    q_grams = _ngrams(cleaned_question)
    scored: List[tuple] = []
    for row in db.query('SELECT account_id, item_id, title, price, stock, status FROM items ORDER BY id DESC LIMIT 500'):
        if str(row['item_id']) == exclude:
            continue
        title = str(row['title'] or '')
        if not title.strip():
            continue
        cleaned = _clean_for_match(title)
        hits = len(_ngrams(cleaned) & q_grams)
        if hits >= _MATCH_MIN_SCORE:
            scored.append((hits, len(title), dict(row)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [item for _, _, item in scored[:limit]]


# --------------------------------------------------------------------------
# 角色 AI 回复
# --------------------------------------------------------------------------

def _messages_for(role: str, conv: Any, content: str, message: Any) -> List[Dict[str, Any]]:
    system = prompts.system_prompt(
        role,
        account_id=conv['account_id'] if conv is not None else None,
        item_id=conv['item_id'] if conv is not None else None,
    )
    messages: List[Dict[str, Any]] = [{'role': 'system', 'content': system}]
    if conv is not None:
        messages.extend(_history(int(conv['id'])))
    extra = _item_context(conv['item_id'] if conv is not None else None)
    if extra:
        messages.append({'role': 'system', 'content': extra})

    image_url = ''
    if message is not None:
        meta = _loads(message['meta'], {}) or {}
        image_url = str(meta.get('image_url') or meta.get('image') or '')
    if role == 'vision' and image_url:
        messages.append({
            'role': 'user',
            'content': [
                {'type': 'text', 'text': content or '看看这张图'},
                {'type': 'image_url', 'image_url': {'url': image_url}},
            ],
        })
    else:
        messages.append({'role': 'user', 'content': content})
    return messages


async def reply_for_message(conversation_id: int, message_id: int) -> Dict[str, Any]:
    """给一条买家消息生成 AI 回复。

    **只生成、不发送** —— 发送由业务层（chats 工作流）决定，这样「人工接管」才拦得住。
    返回 `{'ok', 'reply', 'role', 'conversation_id', 'message_id', 'provider_id', 'model', 'usage'}`。
    """
    conv = _conversation(conversation_id)
    if conv is None:
        return {'ok': False, 'error': f'会话 {conversation_id} 不存在'}
    message = _message(message_id)
    if message is None:
        return {'ok': False, 'error': f'消息 {message_id} 不存在'}

    role = roles_mod.role_for_conversation(conv, message)
    content = str(message['content'] or '').strip()

    # 买家在 A 商品里问到 B 商品 → 先协查，把答复作为上下文
    cross_note = ''
    if role == 'item' and content:
        candidates = match_items(content, exclude_item_id=conv['item_id'])
        if candidates:
            try:
                cross_note = await cross_ask(
                    f"item:{conv['item_id']}" if conv['item_id'] else 'item',
                    content,
                    exclude_item_id=conv['item_id'],
                    conversation_id=int(conv['id']),
                    candidate_items=[c['item_id'] for c in candidates],
                )
            except Exception as exc:  # 协查失败不该让买家收不到回复
                cross_note = ''
                events.publish('agent.cross_failed', {'conversation_id': int(conv['id']), 'error': str(exc)})

    messages = _messages_for(role, conv, content, message)
    if cross_note:
        messages.append({
            'role': 'system',
            'content': f'【跨商品协查结果，来自店内其它商品 AI】\n{cross_note}\n'
                       f'请用一句话把买家关心的事说清楚，不要提到「协查」「其它 AI」这些内部说法。',
        })

    try:
        result = await llm.chat(
            messages,
            role=role,
            temperature=_temperature(role),
            conversation_id=int(conv['id']),
        )
    except llm.LLMError as exc:
        events.publish('agent.reply_failed', {'conversation_id': int(conv['id']), 'role': role, 'error': str(exc)})
        return {'ok': False, 'error': str(exc), 'role': role, 'conversation_id': int(conv['id'])}

    return {
        'ok': True,
        'reply': result.get('text', ''),
        'role': role,
        'conversation_id': int(conv['id']),
        'message_id': int(message_id),
        'provider_id': result.get('provider_id'),
        'model': result.get('model'),
        'usage': result.get('usage', {}),
        'cross_used': bool(cross_note),
    }


# --------------------------------------------------------------------------
# 主管 AI
# --------------------------------------------------------------------------

def _parse_tool_call(raw: Any) -> Optional[Dict[str, Any]]:
    """兼容 OpenAI 的 tool_calls 结构，也兼容测试里直接给 {'name','args'} 的简写。"""
    if not isinstance(raw, dict):
        return None
    if raw.get('name'):
        args = raw.get('args', raw.get('arguments', {}))
        return {'id': raw.get('id') or f"call_{raw['name']}", 'name': str(raw['name']), 'args': args}
    fn = raw.get('function') or {}
    name = fn.get('name')
    if not name:
        return None
    args = fn.get('arguments', {})
    if isinstance(args, str):
        try:
            args = json.loads(args or '{}')
        except (TypeError, ValueError):
            args = {}
    return {'id': raw.get('id') or f'call_{name}', 'name': str(name), 'args': args or {}}


async def run_master(user_text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """主管 AI 回合：卖家说一句话，它理解意图、调工具、把结果说清楚。

    最多 `tools.MAX_TOOL_ROUNDS` 轮工具调用。返回 `{'ok','reply','tool_calls','role','provider_id','model'}`。
    """
    context = dict(context or {})
    conversation_id = context.get('conversation_id')
    system = prompts.system_prompt('master')
    messages: List[Dict[str, Any]] = [{'role': 'system', 'content': system}]
    for turn in context.get('history') or []:
        if isinstance(turn, dict) and turn.get('role') and turn.get('content') is not None:
            messages.append({'role': str(turn['role']), 'content': str(turn['content'])})
    if conversation_id:
        conv = _conversation(int(conversation_id))
        if conv is not None:
            messages.extend(_history(int(conv['id']), limit=6))
    messages.append({'role': 'user', 'content': str(user_text or '')})

    executed: List[Dict[str, Any]] = []
    last_text = ''
    for _round in range(tools_mod.MAX_TOOL_ROUNDS):
        try:
            result = await llm.chat(
                messages,
                role='master',
                temperature=_temperature('master'),
                tools=tools_mod.MASTER_TOOLS,
                conversation_id=int(conversation_id) if conversation_id else None,
            )
        except llm.LLMError as exc:
            return {'ok': False, 'error': str(exc), 'role': 'master', 'tool_calls': executed}

        last_text = result.get('text', '') or last_text
        calls = [_parse_tool_call(c) for c in (result.get('tool_calls') or [])]
        calls = [c for c in calls if c]
        if not calls:
            return {
                'ok': True,
                'reply': last_text,
                'tool_calls': executed,
                'role': 'master',
                'provider_id': result.get('provider_id'),
                'model': result.get('model'),
                'usage': result.get('usage', {}),
            }

        messages.append({'role': 'assistant', 'content': result.get('text') or '', 'tool_calls': result.get('tool_calls')})
        for call in calls:
            output = await tools_mod.call_tool(call['name'], call['args'])
            executed.append({'name': call['name'], 'args': call['args'], 'result': output})
            messages.append({
                'role': 'tool',
                'tool_call_id': call['id'],
                'name': call['name'],
                'content': _dumps(output)[:4000],
            })
        events.publish('agent.master.tools', {'count': len(calls), 'names': [c['name'] for c in calls]})

    return {
        'ok': True,
        'reply': last_text or '（工具调用轮数已达上限，先停一下）',
        'tool_calls': executed,
        'role': 'master',
        'truncated': True,
    }


# --------------------------------------------------------------------------
# 跨商品协查
# --------------------------------------------------------------------------

async def _ask_item_ai(item: Dict[str, Any], question: str, *, depth: int,
                       conversation_id: Optional[int]) -> str:
    """向某个商品的 AI 提问（跨商品链路的一跳）。"""
    item_id = str(item['item_id'])
    account_id = str(item.get('account_id') or '')
    system = prompts.system_prompt('item', account_id=account_id, item_id=item_id)
    context = _item_context(item_id)
    messages = [{'role': 'system', 'content': system}]
    if context:
        messages.append({'role': 'system', 'content': context})
    messages.append({
        'role': 'system',
        'content': '【来自店内另一个商品 AI 的协查】买家在别的商品会话里问到了你这件商品，'
                   '请只针对你这件商品，用一句话给出准确答复（价格/成色/库存/能否发）。',
    })
    messages.append({'role': 'user', 'content': question})

    record_agent_message(
        'cross', f'item:{item_id}', 'ask',
        topic='other_item_info',
        payload={'question': question, 'depth': depth, 'item_id': item_id},
        conversation_id=conversation_id,
    )
    try:
        result = await asyncio.wait_for(
            llm.chat(messages, role='item', temperature=_temperature('item'),
                     timeout=CROSS_TIMEOUT, conversation_id=conversation_id),
            timeout=CROSS_TIMEOUT,
        )
        answer = str(result.get('text') or '').strip()
    except (asyncio.TimeoutError, llm.LLMError, Exception) as exc:  # noqa: B014 - 任何失败都要回一条
        answer = ''
        record_agent_message(
            f'item:{item_id}', 'cross', 'notify',
            topic='other_item_info',
            payload={'error': f'{type(exc).__name__}: {exc}', 'item_id': item_id},
            conversation_id=conversation_id,
        )
    if answer:
        record_agent_message(
            f'item:{item_id}', 'cross', 'answer',
            topic='other_item_info',
            payload={'answer': answer, 'item_id': item_id, 'title': item.get('title', '')},
            conversation_id=conversation_id,
        )
    return answer


async def _summarize(question: str, answers: List[Dict[str, Any]], *, conversation_id: Optional[int]) -> str:
    """cross 角色把各商品 AI 的答复汇总成一句话。LLM 挂了就退化成拼接。"""
    if not answers:
        return ''
    joined = '\n'.join(
        f"- 《{a['title'] or a['item_id']}》（{a['item_id']}）：{a['answer']}" for a in answers
    )
    fallback = '；'.join(f"《{a['title'] or a['item_id']}》{a['answer']}" for a in answers)
    messages = [
        {'role': 'system', 'content': prompts.system_prompt('cross')},
        {'role': 'user', 'content': f'买家的问题：{question}\n\n各商品 AI 的答复：\n{joined}\n\n'
                                    f'请汇总成一段可以直接回复买家的话，不要提到内部协查过程。'},
    ]
    try:
        result = await asyncio.wait_for(
            llm.chat(messages, role='cross', temperature=_temperature('cross'),
                     timeout=CROSS_TIMEOUT, conversation_id=conversation_id),
            timeout=CROSS_TIMEOUT,
        )
        text = str(result.get('text') or '').strip()
        return text or fallback
    except Exception:
        return fallback


async def cross_ask(
    from_role: str,
    question: str,
    exclude_item_id: Optional[str] = None,
    *,
    conversation_id: Optional[int] = None,
    depth: int = 1,
    candidate_items: Optional[List[str]] = None,
) -> str:
    """跨商品协查：`from_role` 问「买家问的这件别的商品怎么回事」，返回可直接用的答复文本。

    链路：记录 ask → 匹配商品 → 向 `item:<id>` 发 ask → 收 answer → 汇总 answer 回 `from_role`。
    每次投递都写 `agent_messages`。深度超过 `MAX_CROSS_DEPTH` 抛 `ValueError`。
    """
    depth = int(depth or 1)
    if depth > MAX_CROSS_DEPTH:
        raise ValueError(f'跨商品协查深度超限：{depth} > {MAX_CROSS_DEPTH}')
    if depth < 1:
        raise ValueError('跨商品协查深度必须 >= 1')

    question = str(question or '').strip()
    record_agent_message(
        str(from_role), 'cross', 'ask',
        topic='other_item_info',
        payload={'question': question, 'candidate_items': candidate_items or [], 'depth': depth},
        conversation_id=conversation_id,
    )

    targets = match_items(question, exclude_item_id=exclude_item_id, candidate_items=candidate_items)
    if not targets:
        text = '店里没找到对应的商品，麻烦把商品名或链接发我一下，我帮你确认。'
        record_agent_message(
            'cross', str(from_role), 'notify',
            topic='other_item_info',
            payload={'reason': 'no_match', 'question': question},
            conversation_id=conversation_id,
        )
        return text

    answers: List[Dict[str, Any]] = []
    for item in targets:
        # 再往下一跳就超过深度上限了：不再递归，直接说明
        if depth + 1 > MAX_CROSS_DEPTH:
            break
        answer = await _ask_item_ai(item, question, depth=depth + 1, conversation_id=conversation_id)
        if answer:
            answers.append({'item_id': str(item['item_id']), 'title': item.get('title', ''), 'answer': answer})

    if not answers:
        text = '这件商品我这边暂时问不到准确信息，我先帮你记下，稍后让老板确认。'
    else:
        text = await _summarize(question, answers, conversation_id=conversation_id)

    record_agent_message(
        'cross', str(from_role), 'answer',
        topic='other_item_info',
        payload={'answer': text, 'question': question, 'depth': depth,
                 'answered_items': [a['item_id'] for a in answers]},
        conversation_id=conversation_id,
    )
    return text


def recent_runs(limit: int = 100, role: Optional[str] = None) -> List[Dict[str, Any]]:
    return llm.recent_runs(limit=limit, role=role)


def elapsed_ms(started: float) -> int:
    return int((time.time() - started) * 1000)


__all__ = [
    'CROSS_TIMEOUT',
    'MAX_CROSS_DEPTH',
    'cross_ask',
    'match_items',
    'recent_agent_messages',
    'recent_runs',
    'record_agent_message',
    'reply_for_message',
    'run_master',
]
