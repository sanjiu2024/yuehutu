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
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from ..core import db, events
from . import platform_errors

NOT_READY_CODE = 'xianyu_not_ready'
NOT_READY_MESSAGE = '协议层尚未就绪'

#: 「登录态过期」的统一提示词。协议层抛 `SessionExpiredError` 时用它改写异常文案，
#: 这样上层（chats.py 的 `str(value)` warning、这里的失败信封）不必各自认一遍类型。
SESSION_EXPIRED_MARK = '登录态已过期，需要重新登录'

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

    失败信封在这里补一次人话（`platform_errors.decorate`，只给**真机见过的**
    平台错误码加解释，认不出就不动）。放在这个唯一的出口上，是因为平台拒绝会
    从二十几个服务各自的 `_call` 里冒出来：逐处补必然漏，漏掉的那处用户就只能
    看到「用户没有权限」然后白扫一次码。`decorate` **不改 `code`**，所以
    HTTP 状态码不受影响。
    """
    if is_failure(result):
        return FAIL_STATUS.get(str(result.get('code')), 400), platform_errors.decorate(result)
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
    """写一条 operation_logs。`kind` 是任务卡里的叫法，等价于表里的 `action`。

    **故意不校验账号是否存在**：审计行是「我们尝试做过什么」的记录，不是「账号存在」的证明。
    真实的写路径上账号不存在会先被 `_client` 拦成 404 `not_found`，所以不会产生行；
    闸门探针故意用不存在的 `9999999999999` 时会在库里留下 `success=0` +
    detail「账号 … 不存在」的行 —— 这是探针留痕，不是业务写操作（曾经试过在这里按
    `accounts` 表挡掉，结果把「商品存在但账号行缺失」这类正常调用也一起挡了，故回退）。
    """
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

    顺带把 **account_id 写在客户端上**（`persist_account_id`）：mtop 会把自己的
    签名令牌 `_m_h5_tk` 续期写回库，写哪一行只有服务层知道。协议层不认识业务
    账号表，所以这个信息必须由这里注入。

    **只加不改语义**：服务层没就绪 / 客户端不支持钩子 / 钩子自己抛异常，都只是少收
    几条私信，绝不允许影响 `get_client` 的返回值。
    """
    try:
        if hasattr(client, 'persist_account_id'):
            client.persist_account_id = str(account_id)
    except Exception:  # noqa: BLE001 - 打标记失败不能影响拿客户端
        pass
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


_loop_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None


def shared_loop() -> asyncio.AbstractEventLoop:
    """进程级唯一的事件循环（在一个 daemon 线程里 `run_forever`）。

    **为什么不能每次 `asyncio.run()`**：IM 会话是「一个 WebSocket + 一个读循环任务」，
    它的生命周期长过多半个进程。以前每次 `call_method` 都 `asyncio.run()` 一个临时
    loop，函数一返回 loop 就关，挂在它上面的读循环随之被销毁、连接断掉 —— 下一个
    请求抛 `no close frame received or sent`。真机表现就是「同步会话只进会话不进
    消息」、`ensure_im` 建好会话后立刻变成死连接。

    懒建：第一次用到时才起线程，测试里纯同步假实现根本不会碰它。
    """
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name='xywb-asyncio', daemon=True,
            )
            thread.start()
            _loop = loop
        return _loop


async def _resolve(value: Any) -> Any:
    """把任意 awaitable 收敛成协程，好交给共享循环跑。"""
    return await value


def run_awaitable(value: Any) -> Any:
    """跑一个可能是协程的返回值，返回其结果。

    服务层是同步的，但协议层与编排层有 `async def` 方法。三种情况：
    - 不是 awaitable（测试里的同步假实现）-> 原样返回
    - 普通情况 -> 丢进**共享事件循环**（见 `shared_loop`）同步等结果
    - 已经站在共享循环里（协程中同步调用服务层）-> 阻塞等自己会死锁，
      退回「另开一个临时 loop」的老路
    """
    if not inspect.isawaitable(value):
        return value
    loop = shared_loop()
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _resolve(value)).result()
    return asyncio.run_coroutine_threadsafe(_resolve(value), loop).result()


def _is_session_expired(exc: Any) -> bool:
    """这个异常是不是协议层的「登录态过期」（`SessionExpiredError`）。

    三路判断，从硬到软（协议层可能还没就绪，也可能把异常包了一层）：
    1. 懒导入 `xianyu.client`，`isinstance(exc, client.SessionExpiredError)`；
    2. MRO 里有没有 `SessionExpiredError` 这个名字（测试里的假实现、被包装过的异常）；
    3. `xianyu.risk.is_session_expired(str(exc))` 文案兜底。
    """
    # 协议层**明确声明**「这次不是登录态过期」的异常要先放行：`ItemStatsUnavailable`
    # 会把平台原文带在消息里（`seller.pc.*` 族对 web Cookie 天然回 `Session过期`），
    # 走第 3 路文案兜底会把它误判成掉线，于是好账号被 `mark_need_relogin()`。
    # 类型声明优先于文案搜索（按 MRO 认名，子类/包装过也算）。
    try:
        if any(klass.__name__ == 'ItemStatsUnavailable' for klass in type(exc).__mro__):
            return False
    except Exception:  # noqa: BLE001 - 怪异元类，继续往下判
        pass
    client_mod = protocol_module('client')
    cls = getattr(client_mod, 'SessionExpiredError', None) if client_mod is not None else None
    try:
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    except Exception:  # noqa: BLE001 - 假类型/怪异元类，继续往下判
        pass
    try:
        if any(klass.__name__ == 'SessionExpiredError' for klass in type(exc).__mro__):
            return True
    except Exception:  # noqa: BLE001
        pass
    risk_mod = protocol_module('risk')
    check = getattr(risk_mod, 'is_session_expired', None) if risk_mod is not None else None
    try:
        return bool(callable(check) and check(str(exc)))
    except Exception:  # noqa: BLE001
        return False


def _expired_text(value: Any) -> str:
    """异常原文，且不重复带 `SESSION_EXPIRED_MARK` 前缀。"""
    text = str(value)
    prefix = f'{SESSION_EXPIRED_MARK}: '
    return text[len(prefix):] if text.startswith(prefix) else text


def _note_session_expired(client: Any, exc: BaseException) -> None:
    """协议层报「登录态过期」时的回写路径（B2 的根因就在缺这一段）。

    **为什么必须在这里做**：`xianyu.client` 抛出 `SessionExpiredError` 之后，
    过去没有任何生产代码捕获它 —— 统一降级成 `protocol_error`，库里 `login_state`
    还是 `active`，前端永远显示「已登录」，用户只能自己猜到该重新扫码。

    两件事，都吞异常（回写失败绝不能盖掉真正的错误）：
    1. `store.mark_need_relogin(account_id, 原因)` —— 账号页据此亮「需重新登录」；
    2. `events.publish('accounts.changed', …)` —— 前端不用刷新就能看到状态变化。

    另外把原因**写回异常文本**（不换类型、不丢原文）：`call_method` 的调用方
    （如 `chats._push_message`）会 `str(value)` 拼 warning，原样透出才查得下去。
    """
    original = str(exc)
    if not original.startswith(f'{SESSION_EXPIRED_MARK}: '):
        try:
            exc.args = (f'{SESSION_EXPIRED_MARK}: {original}',)
        except Exception:  # noqa: BLE001 - 有些异常对象不让改 args，那就只丢前缀
            pass

    account_id = str(getattr(client, 'cookie_id', '') or getattr(client, 'account_id', '') or '')
    store = protocol_module('store')
    mark = getattr(store, 'mark_need_relogin', None) if store is not None else None
    if account_id and callable(mark):
        try:
            mark(account_id, f'{SESSION_EXPIRED_MARK}: {original}')
        except Exception as write_exc:  # noqa: BLE001
            logger.warning(f'[{account_id}] 回写 need_relogin 失败: {write_exc}')
    publish('accounts.changed', {
        'account_id': account_id,
        'reason': 'session_expired',
        'login_state': getattr(store, 'STATE_NEED_RELOGIN', 'need_relogin')
        if store is not None else 'need_relogin',
        'message': SESSION_EXPIRED_MARK,
    })


def call_method(client: Any, names: Sequence[str], *args: Any, **kwargs: Any) -> Tuple[str, Any]:
    """按候选名字依次找方法调用。

    返回 (状态, 结果)，状态 ∈ {'ok','missing','error'}。
    `missing` 表示协议层还没实现这个方法（当作未就绪），`error` 是调用抛异常。

    **协程必须跑完再算 ok**：协议层方法大量是 `async def`，只 `return fn(...)` 会把
    一个还没执行的协程当成成功结果返回 —— 调用方看到 ok 却没发生任何事（假成功），
    协程还会因为没人 await 而泄漏。

    `error` 分支额外识别「登录态过期」：先回写账号状态（见 `_note_session_expired`），
    再把异常原样返回 —— 状态码与文案由 `call_failed` 给，这里不吞任何信息。
    """
    for name in names:
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                return CALL_OK, run_awaitable(fn(*args, **kwargs))
            except Exception as exc:
                if _is_session_expired(exc):
                    _note_session_expired(client, exc)
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
    """把 call_method 的非 ok 状态翻成失败信封（ok 时返回 None）。

    「登录态过期」单独给 `verification_required`（409）与 `login_state: need_relogin`：
    前端据此弹「重新扫码」，而不是像通用 `protocol_error` 那样被当成偶发网络错误、
    让用户一直重试一个已经死掉的 Cookie。
    """
    if status == CALL_OK:
        return None
    if status == CALL_MISSING:
        return not_ready('协议层尚未实现该方法')
    if _is_session_expired(value):
        return fail(
            f'账号{SESSION_EXPIRED_MARK}: {_expired_text(value)}',
            'verification_required',
            login_state='need_relogin',
        )
    return fail(f'协议层调用失败: {value}', 'protocol_error')
