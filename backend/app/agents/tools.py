"""工具定义与执行 —— 主管 AI 的 function-calling 工具集。

**这份文件是唯一工具真相**（`docs/CONTRACTS.md` §5）：`mcp_server/server.py` 直接
`from backend.app.agents.tools import TOOL_SPECS`，两边名字与 schema 必须一致。

- `TOOL_SPECS`：**全集**（19 个业务工具 + `master_chat`），MCP 侧暴露这套。
- `MASTER_TOOLS`：给主管 AI 自己用的子集 = 全集去掉 `master_chat`
  （`master_chat` 是「把卖家的话转给主管 AI」，在主管 AI 的 tool 集里会递归）。

schema 格式照抄 OpenAI function calling：
    {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
注意参数键叫 **`parameters`**（MCP 侧暴露 `tools/list` 时自己映射成 `inputSchema`）。

执行入口：`async call_tool(name, args) -> dict`。
读类工具直接查库（不依赖其它工作流），写类工具**懒导入** `backend.app.services.*`，
模块还没写完时返回 `{'ok': False, 'error': ...}` 而不是抛异常 —— 并行开发期间不许互相卡死。
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Dict, List, Optional

from ..core import db, events

TOOL_SPECS: List[Dict[str, Any]] = [
    # ---------------- 账号 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'accounts_list',
            'description': '列出所有闲鱼账号及登录状态。想知道「有哪些号、哪个掉线了」时用。',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'accounts_set_enabled',
            'description': '启用或停用某个闲鱼账号（停用后不再自动回复）。用户说「把某个号关掉/打开」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'enabled': {'type': 'boolean', 'description': 'true=启用，false=停用'},
                },
                'required': ['account_id', 'enabled'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'accounts_relogin',
            'description': '触发某个账号重新扫码登录。账号掉线、Cookie 失效时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '闲鱼账号 id'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 商品 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'items_list',
            'description': '查询在售商品列表，可按账号、状态过滤，或按关键词搜标题。用户问「我有哪些商品/某商品在不在」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '只看这个账号的商品，可省略'},
                    'status': {'type': 'string', 'description': '商品状态，例如 on_sale / sold / delisted，可省略'},
                    'q': {'type': 'string', 'description': '标题关键词，可省略'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_get',
            'description': '查单个商品的完整信息（价格、库存、描述、图片）。要改价或回答买家细节前先用它。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '商品所属账号 id'},
                    'item_id': {'type': 'string', 'description': '商品 id'},
                },
                'required': ['account_id', 'item_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_edit',
            'description': '修改单个商品的价格/标题/描述/库存/图片。用户明确要求改某个商品时用；只传要改的字段。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '商品所属账号 id'},
                    'item_id': {'type': 'string', 'description': '商品 id'},
                    'price': {'type': 'number', 'description': '新价格（元），可省略'},
                    'title': {'type': 'string', 'description': '新标题，可省略'},
                    'description': {'type': 'string', 'description': '新描述，可省略'},
                    'stock': {'type': 'integer', 'description': '新库存，可省略'},
                    'images': {'type': 'array', 'items': {'type': 'string'}, 'description': '图片 URL 列表，可省略'},
                },
                'required': ['account_id', 'item_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_publish',
            'description': '发布一个新商品到闲鱼。用户给了标题和价格让你上架时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '发布到哪个账号'},
                    'title': {'type': 'string', 'description': '商品标题'},
                    'price': {'type': 'number', 'description': '售价（元）'},
                    'description': {'type': 'string', 'description': '商品描述，可省略'},
                    'images': {'type': 'array', 'items': {'type': 'string'}, 'description': '图片 URL 列表，可省略'},
                    'category_id': {'type': 'string', 'description': '类目 id，可省略'},
                },
                'required': ['account_id', 'title', 'price'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_batch_edit',
            'description': '批量改价/改标题。用户说「把某几个商品都降 10 块」这类批量操作时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '商品所属账号 id'},
                    'item_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '要改的商品 id 列表'},
                    'patch': {
                        'type': 'object',
                        'description': '改动内容，例如 {"price_delta": -10} 或 {"price_set": 99} 或 {"title_tpl": "..."}',
                    },
                },
                'required': ['account_id', 'item_ids', 'patch'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_sync',
            'description': '从闲鱼拉取某个账号的在售商品并入库。用户说「同步一下商品」时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '要同步的账号 id'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 聊天 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'chats_list',
            'description': '列出买家会话（谁在问、哪个商品、什么模式）。用户问「有谁在问/有什么新消息」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '只看这个账号的会话，可省略'},
                    'mode': {'type': 'string', 'description': 'auto / manual / off，可省略'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'chats_read',
            'description': '读某个会话的聊天记录。要了解上下文、或用户问「他刚才说了什么」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'conversation_id': {'type': 'integer', 'description': '会话 id'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'required': ['conversation_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'chats_send',
            'description': '以卖家身份给买家发一条消息。用户说「回复他/告诉他…」时用；发送前先想清楚措辞。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'conversation_id': {'type': 'integer', 'description': '会话 id'},
                    'content': {'type': 'string', 'description': '要发送的文本'},
                },
                'required': ['conversation_id', 'content'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'chats_set_mode',
            'description': '切换某个会话的回复模式：auto=AI 自动回，manual=只给建议要人工确认，off=不回复。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'conversation_id': {'type': 'integer', 'description': '会话 id'},
                    'mode': {'type': 'string', 'enum': ['auto', 'manual', 'off'], 'description': '目标模式'},
                },
                'required': ['conversation_id', 'mode'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 订单 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'orders_list',
            'description': '查订单列表（可按账号、状态过滤）。用户问「今天卖了几单/某单什么状态」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '只看这个账号的订单，可省略'},
                    'status': {'type': 'string', 'description': '订单状态，可省略'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'orders_deliver',
            'description': '给某个订单发货（需要快递公司 + 运单号）。用户明确要求发货时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'order_id': {'type': 'string', 'description': '订单号'},
                    'company': {'type': 'string', 'description': '快递公司，例如 顺丰'},
                    'tracking_no': {'type': 'string', 'description': '运单号'},
                },
                'required': ['order_id', 'company', 'tracking_no'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 风控 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'risk_status',
            'description': '查账号风控状态（是否被限流/需要人工验证）。用户问「为什么没自动回复了」时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '账号 id，可省略则返回全部'}},
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'risk_clear',
            'description': '清除某个账号的风控熔断（**只有人工验证成功后**才能用）。用户说「我已经验证过了」时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '账号 id'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 提供者 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'providers_list',
            'description': '列出已配置的 AI 提供者（名字、模型、是否默认）。**不返回 api_key**。',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'providers_upsert',
            'description': '新增或更新一个 AI 提供者（API Key 统一管理）。用户给了新的 base_url/key 时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '要更新的提供者 id；省略则新增'},
                    'name': {'type': 'string', 'description': '显示名，例如 智谱GLM'},
                    'base_url': {'type': 'string', 'description': 'OpenAI 兼容地址，例如 https://open.bigmodel.cn/api/paas/v4'},
                    'api_key': {'type': 'string', 'description': 'API Key'},
                    'default_model': {'type': 'string', 'description': '默认模型名，例如 glm-4-flash'},
                    'enabled': {'type': 'boolean', 'description': '是否启用，默认 true'},
                    'is_default': {'type': 'boolean', 'description': '是否设为默认提供者'},
                },
                'required': ['name', 'base_url'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 主管 AI（只给 MCP 侧） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'master_chat',
            'description': '把卖家（你）的一句话转给这个软件的主管 AI，由它理解意图并调用工具干活。这是 DSH 里指挥本软件的总入口。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string', 'description': '要对主管 AI 说的话'},
                    'conversation_id': {'type': 'integer', 'description': '可选，绑定的买家会话 id'},
                },
                'required': ['message'],
                'additionalProperties': False,
            },
        },
    },
]

# 主管 AI 自己的 tool 集：去掉 master_chat（否则会递归调自己）
MASTER_TOOLS: List[Dict[str, Any]] = [
    spec for spec in TOOL_SPECS if spec['function']['name'] != 'master_chat'
]

TOOL_NAMES: List[str] = [spec['function']['name'] for spec in TOOL_SPECS]

# 一次主管 AI 回合里最多执行多少次工具调用（防止模型死循环）
MAX_TOOL_ROUNDS = 4


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    for spec in TOOL_SPECS:
        if spec['function']['name'] == name:
            return spec
    return None


def _require(args: Dict[str, Any], *names: str) -> Optional[str]:
    missing = [n for n in names if args.get(n) in (None, '')]
    return f'缺少参数: {", ".join(missing)}' if missing else None


def _rows(sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    return [dict(r) for r in db.query(sql, params)]


# --------------------------------------------------------------------------
# 读类工具：直接查库，不依赖其它工作流
# --------------------------------------------------------------------------

def _accounts_list(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = db.query(
        'SELECT a.id, a.nickname, a.remark, a.enabled, a.auto_reply, a.updated_at, '
        'c.login_state, c.relogin_reason, c.last_login_at '
        'FROM accounts a LEFT JOIN account_credentials c ON c.account_id = a.id '
        'ORDER BY a.id'
    )
    return {'ok': True, 'accounts': [dict(r) for r in rows]}


def _items_list(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = 'SELECT account_id, item_id, title, price, stock, status, ai_enabled, updated_at FROM items WHERE 1=1'
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND account_id = ?'
        params.append(str(args['account_id']))
    if args.get('status'):
        sql += ' AND status = ?'
        params.append(str(args['status']))
    if args.get('q'):
        sql += ' AND title LIKE ?'
        params.append(f"%{args['q']}%")
    sql += ' ORDER BY updated_at DESC LIMIT ?'
    params.append(int(args.get('limit') or 20))
    return {'ok': True, 'items': _rows(sql, params)}


def _items_get(args: Dict[str, Any]) -> Dict[str, Any]:
    row = db.query_one(
        'SELECT * FROM items WHERE account_id = ? AND item_id = ?',
        (str(args['account_id']), str(args['item_id'])),
    )
    if row is None:
        return {'ok': False, 'error': f'没找到商品 {args["item_id"]}'}
    item = dict(row)
    try:
        item['images'] = json.loads(item.get('images') or '[]')
    except (ValueError, TypeError):
        item['images'] = []
    return {'ok': True, 'item': item}


def _chats_list(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = ('SELECT c.id, c.account_id, c.peer_nickname, c.item_id, c.mode, c.agent_role, '
           'c.unread, c.last_message_at FROM conversations c WHERE 1=1')
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND c.account_id = ?'
        params.append(str(args['account_id']))
    if args.get('mode'):
        sql += ' AND c.mode = ?'
        params.append(str(args['mode']))
    sql += " ORDER BY COALESCE(c.last_message_at, '') DESC, c.id DESC LIMIT ?"
    params.append(int(args.get('limit') or 20))
    return {'ok': True, 'conversations': _rows(sql, params)}


def _chats_read(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get('limit') or 20)
    rows = db.query(
        'SELECT id, direction, sender, content, msg_type, item_id, created_at FROM messages '
        'WHERE conversation_id = ? ORDER BY id DESC LIMIT ?',
        (int(args['conversation_id']), limit),
    )
    return {'ok': True, 'messages': [dict(r) for r in reversed(rows)]}


def _orders_list(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = 'SELECT order_id, account_id, item_id, buyer_id, amount, status, created_at FROM orders WHERE 1=1'
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND account_id = ?'
        params.append(str(args['account_id']))
    if args.get('status'):
        sql += ' AND status = ?'
        params.append(str(args['status']))
    sql += ' ORDER BY created_at DESC LIMIT ?'
    params.append(int(args.get('limit') or 20))
    return {'ok': True, 'orders': _rows(sql, params)}


def _risk_status(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = ('SELECT account_id, consecutive_hits, blocked_until, last_hit_at, verification_type, updated_at '
           'FROM risk_state WHERE 1=1')
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND account_id = ?'
        params.append(str(args['account_id']))
    rows = _rows(sql, params)
    import time as _time

    now = _time.time()
    for row in rows:
        row['blocked'] = float(row.get('blocked_until') or 0) > now
    return {'ok': True, 'risk': rows}


def _providers_list(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = db.query(
        'SELECT id, name, base_url, default_model, enabled, is_default, updated_at FROM providers ORDER BY id'
    )
    return {'ok': True, 'providers': [dict(r) for r in rows]}


def _providers_upsert(args: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = args.get('id')
    fields = {
        'name': str(args.get('name') or '').strip(),
        'base_url': str(args.get('base_url') or '').strip().rstrip('/'),
        'default_model': str(args.get('default_model') or '').strip(),
        'enabled': 1 if args.get('enabled', True) else 0,
        'is_default': 1 if args.get('is_default') else 0,
    }
    if provider_id:
        current = db.query_one('SELECT * FROM providers WHERE id = ?', (int(provider_id),))
        if current is None:
            return {'ok': False, 'error': f'提供者 #{provider_id} 不存在'}
        api_key = str(args.get('api_key') or current['api_key'] or '')
        if not fields['name']:
            fields['name'] = current['name']
        if not fields['base_url']:
            fields['base_url'] = current['base_url']
        db.execute(
            'UPDATE providers SET name=?, base_url=?, api_key=?, default_model=?, enabled=?, is_default=?, '
            'updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (fields['name'], fields['base_url'], api_key, fields['default_model'],
             fields['enabled'], fields['is_default'], int(provider_id)),
        )
    else:
        api_key = str(args.get('api_key') or '')
        if not fields['name'] or not fields['base_url']:
            return {'ok': False, 'error': '新增提供者必须给 name 和 base_url'}
        provider_id = int(db.execute(
            'INSERT INTO providers(name, base_url, api_key, default_model, enabled, is_default) '
            'VALUES(?,?,?,?,?,?)',
            (fields['name'], fields['base_url'], api_key, fields['default_model'],
             fields['enabled'], fields['is_default']),
        ))
    if fields['is_default']:
        db.execute('UPDATE providers SET is_default = 0 WHERE id != ?', (int(provider_id),))
    return {'ok': True, 'provider_id': int(provider_id), 'updated': True}


# --------------------------------------------------------------------------
# 写类工具：懒导入 services（并行开发期间可能还没写完）
# --------------------------------------------------------------------------

def _services():
    """懒导入业务层。返回 (module, error)。"""
    import importlib

    try:
        return importlib.import_module('backend.app.services'), None
    except ImportError as exc:
        return None, str(exc)


async def _dispatch_service(fn_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    services, error = _services()
    if services is None:
        return {'ok': False, 'error': f'业务模块还没就绪（backend.app.services 不可用: {error}）'}
    target = getattr(services, fn_name, None)
    if target is None:
        return {'ok': False, 'error': f'业务模块还没实现 {fn_name}()'}
    import inspect

    result = target(**args) if not inspect.iscoroutinefunction(target) else await target(**args)
    if isinstance(result, dict) and 'ok' in result:
        return result
    return {'ok': True, 'result': result}


async def _accounts_set_enabled(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = str(args['account_id'])
    enabled = 1 if args.get('enabled') else 0
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return {'ok': False, 'error': f'账号 {account_id} 不存在'}
    db.execute('UPDATE accounts SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
               (enabled, account_id))
    events.publish('accounts.changed', {'account_id': account_id, 'enabled': bool(enabled)})
    return {'ok': True, 'account_id': account_id, 'enabled': bool(enabled)}


async def _risk_clear(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = str(args['account_id'])
    db.execute(
        'UPDATE risk_state SET consecutive_hits = 0, blocked_until = 0, verification_type = \'\', '
        'updated_at = CURRENT_TIMESTAMP WHERE account_id = ?',
        (account_id,),
    )
    events.publish('risk.cleared', {'account_id': account_id})
    return {'ok': True, 'account_id': account_id}


async def _master_chat(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import runtime  # 懒导入，避免 runtime <-> tools 循环

    result = await runtime.run_master(
        str(args['message']),
        context={'conversation_id': args.get('conversation_id')} if args.get('conversation_id') else None,
    )
    return {'ok': True, 'reply': result.get('reply', ''), 'tool_calls': result.get('tool_calls', [])}


# 工具名 -> 执行函数（参数已校验）
_HANDLERS = {
    'accounts_list': _accounts_list,
    'accounts_set_enabled': _accounts_set_enabled,
    'accounts_relogin': lambda a: _dispatch_service('accounts_relogin', a),
    'items_list': _items_list,
    'items_get': _items_get,
    'items_edit': lambda a: _dispatch_service('items_edit', a),
    'items_publish': lambda a: _dispatch_service('items_publish', a),
    'items_batch_edit': lambda a: _dispatch_service('items_batch_edit', a),
    'items_sync': lambda a: _dispatch_service('items_sync', a),
    'chats_list': _chats_list,
    'chats_read': _chats_read,
    'chats_send': lambda a: _dispatch_service('chats_send', a),
    'chats_set_mode': lambda a: _dispatch_service('chats_set_mode', a),
    'orders_list': _orders_list,
    'orders_deliver': lambda a: _dispatch_service('orders_deliver', a),
    'risk_status': _risk_status,
    'risk_clear': _risk_clear,
    'providers_list': _providers_list,
    'providers_upsert': _providers_upsert,
    'master_chat': _master_chat,
}


async def call_tool(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一个工具。永远返回 dict（`{'ok': True/False, ...}`），不抛业务异常。"""
    args = dict(args or {})
    spec = get_tool(name)
    if spec is None:
        return {'ok': False, 'error': f'未知工具: {name}'}
    required = (spec['function']['parameters'] or {}).get('required') or []
    missing = _require(args, *required)
    if missing:
        return {'ok': False, 'error': missing}
    handler = _HANDLERS.get(name)
    if handler is None:  # pragma: no cover - 防漏注册
        return {'ok': False, 'error': f'工具 {name} 没有实现'}
    try:
        result = handler(args)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as exc:  # 工具失败要回给模型，不能炸掉整个回合
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


__all__ = ['MASTER_TOOLS', 'MAX_TOOL_ROUNDS', 'TOOL_NAMES', 'TOOL_SPECS', 'call_tool', 'get_tool']
