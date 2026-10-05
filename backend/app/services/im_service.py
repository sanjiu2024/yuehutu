"""IM 会话生命周期服务：把协议层的 `ImSession` 接到服务层的入站 handler 上。

**为什么需要这个模块**：`xianyu.client._ensure_im()` 建好 `ImSession` 之后，过去
没有任何生产代码注册入站 handler —— 买家真的说话时，帧被 `ImSession.dispatch`
投递给一个空 handler 列表，AI 永远不会开口（只有手动 POST `/api/chats/incoming`
才会回）。本模块补上这段接线：

    XianyuClient.on_im_session(hook)   # 协议层回调点
        -> im_service.bind()           # 服务层挂钩子（common.get_client 里也挂）
        -> chats.attach_im_session()   # 注入「一帧 -> 落库 + 触发 AI」handler

层次约束不变：协议层不许 import services，所以方向永远是「服务层挂上去」。
AI 回复的**发送**不在这里，而在 `services.chats.deliver_ai_reply()` —— 它跟入站
是同一条链路（`handle_incoming` 生成回复后立刻投递），本模块只保证会话活着、
handler 在位。

对外入口：
- `start(account_id)`：账号需要收私信时调用（返回信封；协议层未就绪 -> not_ready）
- `stop(account_id)`：关掉会话
- `current_session(account_id)` / `active_accounts()`：诊断用
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from . import chats, common

# account_id -> ImSession。跑在 IM 读循环里的 handler 需要它来定位「这条会话是谁的」。
_sessions: Dict[str, Any] = {}
# account_id -> hook。同一个 account 复用**同一个函数对象**：`XianyuClient.on_im_session`
# 按对象去重，若每次 bind 都造新闭包，同一客户端会被挂上好几份钩子。
_hooks: Dict[str, Any] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------- 会话表


def remember(account_id: str, session: Any) -> Any:
    with _lock:
        _sessions[str(account_id or '')] = session
    return session


def current_session(account_id: str) -> Optional[Any]:
    with _lock:
        return _sessions.get(str(account_id or ''))


def active_accounts() -> List[str]:
    with _lock:
        return sorted(_sessions)


def forget(account_id: str) -> Optional[Any]:
    with _lock:
        return _sessions.pop(str(account_id or ''), None)


# --------------------------------------------------------------------------- 接线


def _attach(account_id: str, session: Any) -> bool:
    """注册入站 handler。失败只记日志，不往上抛。

    「会话建好了但 handler 没挂上」比「建会话失败」更难发现，所以这里必须留痕。
    """
    try:
        chats.attach_im_session(session, account_id)
        return True
    except Exception as exc:  # noqa: BLE001 - 钩子路径不允许把异常带进建会话流程
        logger.warning(f'[{account_id}] 注册 IM 入站 handler 失败: {exc}')
        return False


def _hook_for(account_id: str):
    with _lock:
        hook = _hooks.get(account_id)
        if hook is None:
            def hook(session: Any) -> None:  # noqa: F811 - 有意复用上面查到的名字
                remember(account_id, session)
                _attach(account_id, session)
            _hooks[account_id] = hook
    return hook


def bind(account_id: str, client: Any) -> Any:
    """给协议层客户端挂「会话建好就注册 handler」的钩子，返回 client 本身。

    幂等：同一 client 重复 bind 只会留一份钩子（`on_im_session` 自己去重）。
    client 没有这个钩子（假客户端 / 旧客户端）就静默跳过 —— `bind` 不允许影响
    `common.get_client` 的结果。
    """
    account_id = str(account_id or '')
    register = getattr(client, 'on_im_session', None)
    if not callable(register):
        return client
    try:
        register(_hook_for(account_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'[{account_id}] 挂 IM 会话钩子失败: {exc}')
    return client


def ensure_session(account_id: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """拿到（必要时创建）账号的 IM 会话，并确保入站 handler 已注册。

    返回 `(session, None)` 或 `(None, 失败信封)` —— 失败一定是信封（含 code），
    调用方据此如实回话，不要把它变成「静默成功」。
    """
    account_id = str(account_id or '')
    if not account_id:
        return None, common.fail('缺少 account_id', 'invalid_fields')

    client, err = common.get_client(account_id)
    if client is None:
        return None, err or common.not_ready()
    bind(account_id, client)

    status, session = common.call_method(client, ('ensure_im', '_ensure_im'))
    if status != common.CALL_OK or session is None or common.is_failure(session):
        return None, common.call_failed(status, session) or common.fail(
            f'IM 会话创建失败: {session}', 'protocol_error'
        )

    remember(account_id, session)
    _attach(account_id, session)
    return session, None


def _is_alive(session: Any) -> bool:
    """会话是否还活着。没有 `connected` 属性的实现（旧假会话）按「活」处理。

    「按活处理」是有意的：宁可复用一条状态未知的会话，也不要因为读不出版本差异
    就无限重建 —— 重建在平台上等于多开一条入站连接。
    """
    connected = getattr(session, 'connected', None)
    return True if connected is None else bool(connected)


def start(account_id: str) -> Any:
    """账号需要收私信时调用：建会话 + 注册入站 handler。

    **幂等**：同一账号已经挂着一条活着的会话时直接复用，不重复建连。这个函数现在由
    API 层在「扫码登录成功 / 打开 enabled / relogin」三处调用，而这些动作用户会反复
    触发；每调一次就新建一条 WebSocket，平台上会留下多条入站连接（同一条买家消息
    收多份、AI 也回多份）。

    协议层未就绪（没有 Cookie、缺依赖）时返回 `not_ready` 信封而不是抛异常 ——
    调用点都在后台线程或 HTTP 路由里，抛出去只会变成一条没人看的 traceback。
    """
    account_id = str(account_id or '')
    if not account_id:
        return common.fail('缺少 account_id', 'invalid_fields')
    try:
        existing = current_session(account_id)
        if existing is not None and _is_alive(existing):
            _attach(account_id, existing)  # 幂等：会话还在，但 handler 未必挂上了
            return {
                'account_id': account_id,
                'attached': True,
                'connected': True,
                'reused': True,
            }
        if existing is not None:
            # 半死的旧会话（连上过又断线）：先摘掉，别让它继续占着 account_id
            stop(account_id)
        session, err = ensure_session(account_id)
        if session is None:
            return err or common.not_ready()
        return {
            'account_id': account_id,
            'attached': True,
            'connected': bool(getattr(session, 'connected', False)),
            'backfill': _backfill_history(account_id),
        }
    except Exception as exc:  # noqa: BLE001 - 后台挂载路径不许把异常抛给调用方
        logger.warning(f'[{account_id}] 挂载 IM 会话异常: {exc}')
        return common.fail(f'IM 入站挂载异常: {exc}', 'protocol_error')


def _backfill_history(account_id: str) -> Dict[str, Any]:
    """刚挂上入站连接时顺手把**历史会话**回填一次。

    入站 handler 只收「正在发生」的推送，进程启动前就存在的会话不会自己进库 ——
    真机表现就是「账号上的消息一个都没读取过来」。回填失败不影响挂载本身，
    原因照实塞进返回值里。
    """
    from . import chats  # 局部 import：避免服务层之间在模块级互相牵连
    try:
        result = chats.sync_im_conversations(account_id)
    except Exception as exc:  # noqa: BLE001 - 回填失败不该拖垮 IM 挂载
        logger.warning(f'[{account_id}] IM 历史会话回填异常: {exc}')
        return {'error': str(exc)}
    if isinstance(result, dict) and result.get('code'):
        logger.warning(f'[{account_id}] IM 历史会话回填失败: {result.get("message")}')
    return result if isinstance(result, dict) else {}


def stop(account_id: str) -> Any:
    """关掉账号的 IM 会话（并从会话表里摘掉）。"""
    session = forget(account_id)
    if session is None:
        return {'account_id': account_id, 'closed': False}
    close = getattr(session, 'close', None)
    if callable(close):
        try:
            common.run_awaitable(close())
        except Exception as exc:  # noqa: BLE001
            return common.fail(f'关闭 IM 会话失败: {exc}', 'protocol_error')
    return {'account_id': account_id, 'closed': True}
