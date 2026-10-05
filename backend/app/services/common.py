"""业务服务层公共设施。

三件事：
1. **懒导入协议层**：`backend.app.xianyu.client` / `.login` 可能在并行开发中还没写完，
   所以只在真正要用的时候 import，`ImportError` 时返回统一结果
   `{"success": false, "message": "协议层尚未就绪", "code": "xianyu_not_ready"}`。
   这样业务层的测试可以完全脱离协议层跑。
2. **操作日志**：所有写操作落 `operation_logs`（列名 `action`，兼容 `kind` 关键字）。
3. **事件推送**：状态变化 `events.publish(topic, payload)`，前端靠它实时刷新。

topic 清单（前端订阅用，改这里要同步 CONTRACTS）：
- items.synced / items.updated / items.batch_done / items.status_changed / items.published
- chats.message / chats.mode_changed / chats.ai_reply
- orders.synced / orders.shipment_updated / orders.delivered
- risk.changed
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import inspect
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import db, events

NOT_READY_CODE = 'xianyu_not_ready'
NOT_READY_MESSAGE = '协议层尚未就绪'

# code -> HTTP 状态码（路由层把服务返回的失败信封翻成语义状态码）
FAIL_STATUS: Dict[str, int] = {
    'bad_request': 400,
    'invalid_fields': 400,
    'not_found': 404,
    'verification_required': 409,
    'conflict': 409,
    NOT_READY_CODE: 503,
    'protocol_error': 502,
}


# --------------------------------------------------------------------------- 信封


def not_ready(message: str = '', **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        'success': False,
        'message': message or NOT_READY_MESSAGE,
        'code': NOT_READY_CODE,
    }
    payload.update(extra)
    return payload


def fail(message: str, code: str = 'bad_request', **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {'success': False, 'message': message, 'code': code}
    payload.update(extra)
    return payload


def is_failure(result: Any) -> bool:
    return isinstance(result, dict) and result.get('success') is False


def respond(result: Any) -> Tuple[int, Any]:
    """把服务层返回值翻成 (HTTP 状态码, 响应体)。

    - 失败信封原样透出，状态码按 code 映射
    - 其它一律套 `{"success": true, "data": ...}`
    """
    if is_failure(result):
        return FAIL_STATUS.get(str(result.get('code')), 400), result
    return 200, {'success': True, 'data': result}


# --------------------------------------------------------------------------- 日志与事件


def log_operation(
    account_id: str,
    action: str,
    item_id: str = '',
    success: bool = True,
    detail: Any = '',
    kind: Optional[str] = None,
) -> int:
    """写一条 operation_logs。`kind` 是任务卡里的叫法，等价于表里的 `action`。"""
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    return db.execute(
        'INSERT INTO operation_logs(account_id, item_id, action, success, detail) '
        'VALUES(?,?,?,?,?)',
        (account_id or '', item_id or '', kind or action or '', 1 if success else 0, detail or ''),
    )


def publish(topic: str, payload: Optional[Dict[str, Any]] = None) -> None:
    try:
        events.publish(topic, payload or {})
    except Exception:  # pragma: no cover - 推送失败绝不能影响业务写入
        pass


def add_risk_event(account_id: str, kind: str, message: str = '') -> int:
    return db.execute(
        'INSERT INTO risk_events(account_id, kind, message) VALUES(?,?,?)',
        (account_id or '', kind, message or ''),
    )


def get_setting(key: str, default: str = '') -> str:
    row = db.query_one('SELECT value FROM settings WHERE key = ?', (key,))
    return row['value'] if row is not None and row['value'] is not None else default


def set_setting(key: str, value: Any) -> None:
    db.execute(
        'INSERT INTO settings(key, value, updated_at) VALUES(?,?,CURRENT_TIMESTAMP) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
        (key, '' if value is None else str(value)),
    )


def truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


# --------------------------------------------------------------------------- 协议层适配


def protocol_module(name: str):
    """懒导入 `backend.app.xianyu.<name>`；不存在或不可用时返回 None。"""
    try:
        return importlib.import_module(f'backend.app.xianyu.{name}')
    except ImportError:
        return None
    except Exception:  # 协议层内部依赖缺失（playwright 等）也当作未就绪
        return None


def _cookie_for(account_id: str) -> str:
    """从库里取出该账号的 Cookie 串。

    懒导入：`services` 在最底层，而 `xianyu.store` 依赖 `core.db`，模块级导入会把
    「协议层没装」这种情况变成 import 期的循环/失败，而不是 `not_ready` 信封。
    """
    try:
        store = protocol_module('store')
        if store is None:
            return ''
        return str(store.get_cookie(account_id) or '')
    except Exception:
        return ''


def _bind_im_service(account_id: str, client: Any) -> Any:
    """给刚构造出来的协议层客户端挂 IM 会话钩子，返回 client 本身。

    这是 IM 入站链路的接线点（详见 `services/im_service.py` 的模块说明）：协议层
    `_ensure_im()` 建好会话后回调钩子，服务层据此注册入站 handler；没有这一步，
    买家真的说话时帧会落到一个空 handler 列表上。

    **只加不改语义**：服务层没就绪 / 客户端不支持钩子 / 钩子自己抛异常，都只是少收
    几条私信，绝不允许影响 `get_client` 的返回值。
    """
    try:
        module = importlib.import_module('backend.app.services.im_service')
        bind = getattr(module, 'bind', None)
        if callable(bind):
            bind(account_id, client)
    except Exception:  # noqa: BLE001 - 接线失败不能影响拿客户端
        pass
    return client


def get_client(account_id: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """拿协议层客户端。返回 (client, None) 或 (None, 失败信封)。

    **必须先用 account_id 取 cookie**：`XianyuClient.__init__(self, cookie_str, ...)`
    收的是 Cookie 串、且要求里面含 `unb`，直接把 account_id 当 cookie 传进去必然抛
    「Cookie 里缺少 unb」。这条断链会让同步/发布/改价/上下架/发消息全部静默退化成
    「只写本地库」，所以这里是唯一正确的构造顺序：

        account_id -> store.get_cookie(account_id) -> XianyuClient(cookie_str)

    协议层另有可选的 `client.py::get_client(account_id)` 工厂，存在就优先用（测试与
    未来的连接池会注入它）。
    """
    module = protocol_module('client')
    if module is None:
        return None, not_ready()
    factory = getattr(module, 'get_client', None)
    if callable(factory):
        try:
            return _bind_im_service(account_id, factory(account_id)), None
        except Exception as exc:
            return None, not_ready(f'协议层客户端创建失败: {exc}')
    cls = getattr(module, 'XianyuClient', None)
    if cls is None:
        return None, not_ready('协议层缺少 XianyuClient')
    cookie = _cookie_for(account_id)
    if not cookie:
        return None, not_ready(f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录')
    try:
        return _bind_im_service(account_id, cls(cookie)), None
    except Exception as exc:
        return None, not_ready(f'协议层客户端创建失败: {exc}')


CALL_OK = 'ok'
CALL_MISSING = 'missing'
CALL_ERROR = 'error'


def run_awaitable(value: Any) -> Any:
    """跑一个可能是协程的返回值，返回其结果。

    服务层是同步的，但协议层与编排层有 `async def` 方法。三种情况：
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


def call_method(client: Any, names: Sequence[str], *args: Any, **kwargs: Any) -> Tuple[str, Any]:
    """按候选名字依次找方法调用。

    返回 (状态, 结果)，状态 ∈ {'ok','missing','error'}。
    `missing` 表示协议层还没实现这个方法（当作未就绪），`error` 是调用抛异常。

    **协程必须跑完再算 ok**：协议层方法大量是 `async def`，只 `return fn(...)` 会把
    一个还没执行的协程当成成功结果返回 —— 调用方看到 ok 却没发生任何事（假成功），
    协程还会因为没人 await 而泄漏。
    """
    for name in names:
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                return CALL_OK, run_awaitable(fn(*args, **kwargs))
            except Exception as exc:
                return CALL_ERROR, exc
    return CALL_MISSING, None


def extract_list(result: Any, *keys: str) -> List[Any]:
    """从协议层返回值里抠出列表（容忍 dict 包一层 / 直接 list）。"""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys or ('items', 'list', 'data', 'orders', 'conversations', 'result'):
            value = result.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = extract_list(value, *keys)
                if nested:
                    return nested
    return []


def extract_id(result: Any, *keys: str) -> str:
    """从协议层返回值里抠出 item_id / order_id 之类的标识。"""
    keys = keys or ('item_id', 'id')
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if value not in (None, ''):
                return str(value)
        for key in ('data', 'result', 'item'):
            nested = result.get(key)
            found = extract_id(nested, *keys) if isinstance(nested, dict) else ''
            if found:
                return found
    return ''


def call_failed(status: str, value: Any) -> Optional[Dict[str, Any]]:
    """把 call_method 的非 ok 状态翻成失败信封（ok 时返回 None）。"""
    if status == CALL_OK:
        return None
    if status == CALL_MISSING:
        return not_ready('协议层尚未实现该方法')
    return fail(f'协议层调用失败: {value}', 'protocol_error')
