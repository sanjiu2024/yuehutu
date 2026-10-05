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

熔断不只活在这份内存里：每次真实命中都会通过 ``set_hit_handler`` 钩子交给服务层
落进 ``risk_state`` 表（UI 风控页读的就是那张表），反向由服务层的
``mark_verified`` / ``clear_cooldown`` 调 ``registry.reset()`` 复位内存计数。
两边不同步时，用户会看到「风控页永远没命中，但协议层默默被拦」。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

from loguru import logger

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


# --------------------------------------------------------------------------- 落库钩子
#
# 协议层命中风控时，这次熔断必须让服务层（`risk_state` 表 / UI 风控页）也知道，
# 否则用户看到的是假的平静。但协议层**不许 import services**：`xianyu -> services
# -> xianyu` 会成环，而且这层要能脱离数据库单独测。所以只暴露一个钩子，落库实现
# 由服务层在 import 时注册（见 `backend/app/services/risk_service.py` 底部）。
_hit_handler: Optional[Callable[[str, str, int], None]] = None
_hit_handler_lock = threading.Lock()


def set_hit_handler(handler: Optional[Callable[[str, str, int], None]]) -> None:
    """注册「协议层命中风控」的回调；``None`` 表示摘掉。

    回调签名 ``(cookie_id, reason, cooldown_seconds)``，必须是同步、快速的本地
    写库动作。
    """
    global _hit_handler
    with _hit_handler_lock:
        _hit_handler = handler


def _notify_hit(cookie_id: str, reason: str, cooldown: int) -> None:
    """把一次真实熔断交给已注册的回调。

    回调抛异常只记日志：落库失败不该让 mtop 调用跟着炸 —— 请求已经被拦下了，
    这时候再抛一个「落库失败」的异常只会掩盖真正的风控错误。
    """
    handler = _hit_handler
    if handler is None:
        return
    try:
        handler(cookie_id, reason, cooldown)
    except Exception as exc:  # noqa: BLE001 - 见上：故意吞掉，只留日志
        logger.warning(f'[{cookie_id}] 风控命中落库失败(熔断本身已生效): {exc}')


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
        # 通知放在锁外：回调要写库/发事件，占着 guard 锁会让并发请求全排队。
        # 只对**真实熔断**通知（登录态过期在 `RiskRegistry.trip` 就已经返回 0，
        # 走不到这里），所以服务层的 consecutive_hits 与内存阶梯一一对应。
        _notify_hit(self.cookie_id, self.last_reason, int(cooldown))
        return int(cooldown)

    def reset(self) -> None:
        """一次成功调用后清零。连续命中的「连续」就是靠这里断开的。"""
        with self._lock:
            self.consecutive_hits = 0
            self.blocked_until = 0.0
            self.last_reason = ''

    def block_for(self, seconds: float, hits: int = 0, reason: str = '') -> None:
        """把内存里的锁恢复到「还剩 seconds 秒」的状态（不累加、不通知）。

        进程重启后内存锁就没了，而 `risk_state.blocked_until` 还在 —— 不给
        这里回灌，UI 说「冷却中」后端却照样往闲鱼发请求，两边又各说各话。
        """
        with self._lock:
            self.consecutive_hits = max(0, int(hits))
            self.last_reason = reason or ''
            self.blocked_until = max(self.blocked_until, time.time() + max(0.0, float(seconds)))

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

    def block_for(self, cookie_id: str, seconds: float, hits: int = 0, reason: str = '') -> None:
        """把一个账号的锁回灌成「还剩 seconds 秒」（启动时从库里恢复用）。"""
        if not cookie_id or float(seconds or 0) <= 0:
            return
        self.get(cookie_id).block_for(seconds, hits, reason)

    def status(self) -> dict:
        with self._lock:
            guards = list(self._guards.values())
        return {g.cookie_id: g.status() for g in guards}


# 全局注册表。按 cookie_id 分桶，所以多账号不会互相牵连。
registry = RiskRegistry()
