"""风控熔断：按 cookie_id 独立计数的冷却阶梯。

两条铁律（写错会让整个功能不可用）：

1. **登录态过期不是风控**。``FAIL_SYS_SESSION_EXPIRED`` / ``Session过期``
   只说明 Cookie 该换了，账号本身没问题。如果把它当风控熔断，用户会看到
   「账号被限流，请等 20 分钟」——实际上重新登录一下就好了，白等 20 分钟。
   所以这两类标记走 ``is_session_expired``，调用方据此把账号置
   ``need_relogin``，**不进熔断**。

2. **按 cookie_id 独立计数**。旧项目早期用全局单例，一个账号撞风控导致
   所有账号一起冷却 —— 多账号用户直接瘫痪。这里每个 cookie_id 一个
   ``AccountGuard``，阶梯按各自连续命中次数走。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

# 连续命中时的冷却秒数阶梯。第 n 次命中取第 n 档，超出后固定用最后一档
# （1200s = 20min）。再往上加档没有意义：风控的惩罚窗口本身以小时计，
# 程序端只需要「别在惩罚期内继续撞」。
COOLDOWN_STEPS = (60, 180, 300, 600, 1200)

# 命中这些标记 = 需要熔断。
RISK_CONTROL_MARKERS = (
    'RGV587_ERROR',
    'FAIL_SYS_USER_VALIDATE',
    '哎哟喂',
    '被挤爆',
    'FAIL_SYS_FLOW_LIMIT',
    '请稍后重试',
    'SM::',
)

# 命中这些标记 = 只是登录态没了，**不许熔断**。
SESSION_EXPIRED_MARKERS = (
    'FAIL_SYS_SESSION_EXPIRED',
    'Session过期',
)


def _as_text(message) -> str:
    """把任意异常/响应/字符串压成可搜索的文本。

    风控标记可能藏在 ``ret`` 列表、异常 str、或 dict 的任意层，统一
    ``str()`` 一遍最省事；``None`` 要变成空串而不是 ``'None'``，否则
    ``'None'`` 里当然搜不到标记，逻辑虽然对但日志会误导。
    """
    if message is None:
        return ''
    return str(message)


def is_risk_control_error(message) -> bool:
    """消息里是否含风控标记。"""
    text = _as_text(message)
    return any(marker in text for marker in RISK_CONTROL_MARKERS)


def is_session_expired(message) -> bool:
    """消息是否表示登录态过期（**不是风控**，不熔断）。"""
    text = _as_text(message)
    return any(marker in text for marker in SESSION_EXPIRED_MARKERS)


class AccountGuard:
    """单个账号（cookie_id）的熔断器。"""

    def __init__(self, cookie_id: str):
        self.cookie_id = cookie_id
        self.consecutive_hits = 0
        self.blocked_until = 0.0
        self.last_hit_at = 0.0
        self.last_reason = ''
        self._lock = threading.Lock()

    @property
    def is_blocked(self) -> bool:
        return time.time() < self.blocked_until

    @property
    def remaining_seconds(self) -> int:
        remaining = self.blocked_until - time.time()
        return max(0, int(remaining))

    def trip(self, reason: str = '', min_cooldown: float = 0) -> int:
        """记一次命中，返回本次冷却秒数。

        ``min_cooldown`` 允许调用方指定「至少等这么久」——比如响应里明确
        写了 Retry-After。取 ``max`` 而不是覆盖，避免被服务端的短值把
        本地阶梯降级。
        """
        with self._lock:
            step = min(self.consecutive_hits, len(COOLDOWN_STEPS) - 1)
            cooldown = max(float(COOLDOWN_STEPS[step]), float(min_cooldown or 0))
            self.consecutive_hits += 1
            now = time.time()
            self.last_hit_at = now
            self.last_reason = reason or ''
            # 用 max 累加：已经因为更长的惩罚被 block 到 T，就不该被这次
            # 较短的冷却提前解封。
            self.blocked_until = max(self.blocked_until, now + cooldown)
            return int(cooldown)

    def reset(self) -> None:
        """一次成功调用后清零。连续命中的「连续」就是靠这里断开的。"""
        with self._lock:
            self.consecutive_hits = 0
            self.blocked_until = 0.0
            self.last_reason = ''

    def status(self) -> dict:
        return {
            'cookie_id': self.cookie_id,
            'blocked': self.is_blocked,
            'remaining_seconds': self.remaining_seconds,
            'consecutive_hits': self.consecutive_hits,
            'last_reason': self.last_reason,
        }


class RiskRegistry:
    """cookie_id -> AccountGuard 的注册表。

    ``trip(msg)`` 只接受风控消息；调用方若把登录态过期传进来，这里会**拒绝**
    并返回 0（不熔断），避免上层写错时静默地惩罚用户。
    """

    def __init__(self):
        self._guards: Dict[str, AccountGuard] = {}
        self._lock = threading.Lock()

    def get(self, cookie_id: str) -> AccountGuard:
        with self._lock:
            guard = self._guards.get(cookie_id)
            if guard is None:
                guard = AccountGuard(cookie_id)
                self._guards[cookie_id] = guard
            return guard

    def trip(self, cookie_id: str, message='', min_cooldown: float = 0) -> int:
        """按消息决定是否熔断；返回冷却秒数（0 = 没熔断）。

        登录态过期返回 0 且**不累加**计数：这是本模块最重要的一条分支。
        """
        if is_session_expired(message):
            return 0
        if not is_risk_control_error(message) and not message:
            # 无消息的显式 trip（调用方已经确认是风控）也要允许
            return self.get(cookie_id).trip('', min_cooldown)
        return self.get(cookie_id).trip(_as_text(message), min_cooldown)

    def reset(self, cookie_id: Optional[str] = None) -> None:
        """清空一个账号；``None`` 表示全部清空（退出登录/切换账号时用）。"""
        with self._lock:
            if cookie_id is None:
                self._guards.clear()
                return
            guard = self._guards.get(cookie_id)
        if guard is not None:
            guard.reset()

    def status(self) -> dict:
        with self._lock:
            guards = list(self._guards.values())
        return {g.cookie_id: g.status() for g in guards}


# 全局注册表。按 cookie_id 分桶，所以多账号不会互相牵连。
registry = RiskRegistry()
