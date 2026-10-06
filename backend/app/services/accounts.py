"""账号资料服务：把昵称/头像从平台拉到并落库。

**为什么需要这个模块**：`backend/app/xianyu/client.py:598 user_profile()` 与
`parse_user_profile()` 早就写好了，但全仓只有一个调用点 ——
`backend/app/xianyu/login.py:389`（扫码登录成功那一瞬）。用 Cookie 导入的账号、
续期恢复的账号、以及历史上没走 `_fill_profile` 的账号，`accounts.nickname`
永远是空串，账号页就显示成 `(未命名)`。

另外 `login.py` 的成功载荷只回 `nickname`、没有 `avatar_url`（`login.py:397-403`），
所以头像在扫码登录路径上也永远是空的 —— 本模块是头像唯一的落地通道。

三条硬规则：

1. **绝不让拉资料失败影响其它接口**：`refresh_profile` 不抛异常，失败只记日志、
   返回 `refreshed=False`。账号列表/启用/启动路径都调它，任何一处抛出去就是
   「平台抖一下，账号页整个 500」。
2. **窄写回填**：走 `store.update_account_profile`（只 UPDATE nickname/avatar_url），
   **不能用 `upsert_account`** —— 那是整体写，会抹掉 cookie/enabled。
3. **必须用共享事件循环**：`common.call_method` 内部走 `common.run_awaitable`
   → `common.shared_loop()`。**不许 `asyncio.run` 新 loop**：协议客户端/IM 会话是
   长生命周期对象，临时 loop 一关就会把挂在它上面的任务一起销毁（详见
   `common.shared_loop` 的注释）。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterable

from loguru import logger

from ..xianyu import store
from . import common

#: 同一账号两次「非强制」拉取的最小间隔（秒）。30 分钟：昵称/头像几乎不变，
#: 而每一次拉取都是一次真实 mtop 请求（会消耗风控配额）。
PROFILE_THROTTLE_SECONDS = 1800

#: account_id -> 上次尝试时间（**记在调用前**：失败也要节流，否则列表接口每次
#: 被刷新都会拿失败账号去撞一次平台）。
_last_attempt: Dict[str, float] = {}
#: 正在后台刷新中的账号，避免列表接口连续被轮询时重复起线程。
_inflight: set = set()
_lock = threading.Lock()


def _profile_of(account_id: str) -> Dict[str, str]:
    account = store.get_account(account_id) or {}
    return {
        'account_id': account_id,
        'nickname': str(account.get('nickname') or ''),
        'avatar_url': str(account.get('avatar_url') or ''),
    }


def _result(account_id: str, refreshed: bool, message: str = '') -> Dict[str, Any]:
    out: Dict[str, Any] = _profile_of(account_id)
    out['refreshed'] = refreshed
    if message:
        out['message'] = message
    return out


def _throttled(account_id: str, force: bool) -> bool:
    """还在节流窗口内就不许拉。`force=True`（用户点了刷新按钮）一律放行。"""
    if force:
        return False
    last = _last_attempt.get(account_id)
    return last is not None and (time.time() - last) < PROFILE_THROTTLE_SECONDS


def forget_profile_cache(account_id: str) -> None:
    """忘掉节流记录。

    删号时用（同一个 id 重新导入不该继承上一轮的 30 分钟窗口）；
    `account_id` 传空表示清空全部 —— 测试夹具用，避免用例之间互相节流。
    """
    with _lock:
        if not account_id:
            _last_attempt.clear()
            _inflight.clear()
            return
        _last_attempt.pop(account_id, None)
        _inflight.discard(account_id)


def refresh_profile(account_id: str, force: bool = False) -> Dict[str, Any]:
    """拉一次账号资料并落库。**永不抛异常**。

    返回 `{account_id, nickname, avatar_url, refreshed[, message]}`；`nickname`
    /`avatar_url` 是**落库之后**的真实值（所以即使这次被节流跳过，调用方拿到的
    也是库里现有的资料，可以直接回给前端）。
    """
    account_id = str(account_id or '').strip()
    if not account_id:
        return {'account_id': '', 'nickname': '', 'avatar_url': '',
                'refreshed': False, 'message': '缺少 account_id'}

    with _lock:
        if _throttled(account_id, force):
            return _result(
                account_id, False,
                f'距上次拉取不足 {int(PROFILE_THROTTLE_SECONDS / 60)} 分钟，已跳过（force=true 可强制）')
        _last_attempt[account_id] = time.time()

    client, err = common.get_client(account_id)
    if client is None:
        detail = ''
        if isinstance(err, dict):
            detail = str(err.get('message') or '')
        return _result(account_id, False, detail or '协议层尚未就绪，无法拉取账号资料')

    # 走 call_method（不是直接 fn()）：它内部用共享事件循环等协程跑完，并且顺手
    # 处理「登录态过期」→ 回写 need_relogin + 发事件（task-17 的接线）。
    status, value = common.call_method(client, ('user_profile',))
    if status != common.CALL_OK:
        if status == common.CALL_MISSING:
            return _result(account_id, False, '协议层未实现 user_profile')
        logger.warning(f'[{account_id}] 拉取账号资料失败（不影响列表/启动）: {value}')
        return _result(account_id, False, f'拉取账号资料失败: {value}')

    profile = value if isinstance(value, dict) else {}
    nickname = str(profile.get('nickname') or '').strip()
    avatar_url = str(profile.get('avatar_url') or '').strip()
    if not nickname and not avatar_url:
        # 平台返回空资料（接口改版/降级）——窄写函数本来就不会覆盖非空值，
        # 这里只是别把「什么都没拉到」记成已刷新。
        return _result(account_id, False, '平台未返回昵称/头像')

    before = _profile_of(account_id)
    store.update_account_profile(account_id, nickname=nickname, avatar_url=avatar_url)
    after = _result(account_id, True)
    if after['nickname'] != before['nickname'] or after['avatar_url'] != before['avatar_url']:
        # 前端账号页订阅了 accounts.changed：不发这条，用户得手动刷新才看得到新昵称。
        common.publish('accounts.changed', {
            'account_id': account_id, 'reason': 'profile_refreshed',
            'nickname': after['nickname'], 'avatar_url': after['avatar_url'],
        })
    logger.info(
        f'[{account_id}] 账号资料已更新 nickname={after["nickname"]!r} '
        f'has_avatar={bool(after["avatar_url"])}'
    )
    return after


def refresh_missing_profiles(accounts: Iterable[Dict[str, Any]]) -> int:
    """给「缺昵称」的账号各起一个后台刷新线程，返回起了几个。

    **必须后台做**：`user_profile` 是一次真 mtop 往返，同步等会把
    `GET /api/accounts` 拖到秒级。这里只负责挑账号 + 起线程，重活与节流都在
    `refresh_profile` 里 —— 连续轮询列表不会重复拉（节流 + `_inflight` 双保险）。
    """
    spawned = 0
    for account in accounts:
        account_id = str(account.get('id') or '').strip()
        if not account_id or str(account.get('nickname') or '').strip():
            continue
        # 没有 Cookie 的账号拉不到资料，白白起线程还会在日志里刷「未就绪」。
        if not account.get('has_cookie'):
            continue
        if account.get('login_state') != store.STATE_ACTIVE:
            continue
        with _lock:
            if account_id in _inflight or _throttled(account_id, False):
                continue
            _inflight.add(account_id)

        def worker(target: str = account_id) -> None:
            try:
                refresh_profile(target)
            except Exception as exc:  # noqa: BLE001 - 守护线程里的异常没人接
                logger.warning(f'[{target}] 后台刷新账号资料异常: {exc}')
            finally:
                with _lock:
                    _inflight.discard(target)

        threading.Thread(target=worker, name=f'profile-refresh-{account_id}',
                         daemon=True).start()
        spawned += 1
    return spawned
