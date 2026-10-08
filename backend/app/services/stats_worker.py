"""商品数据后台慢慢拉（增量 worker）。

**为什么要这个模块**：平台的商品数据接口只能**按商品逐个查**（`client.item_stats(item_id)`），
一个 45 件商品的账号点一次页面就是 45 连击 mtop —— 真机已复现 `stock_refresh_stopped=risk`。
所以改成：后台每轮只拉一小撮、把结果写进 `item_stats_cache`，页面读缓存
（`item_stats.account_stats(..., source='cache')`，**零平台调用**）。

三条设计原则：

1. **风控优先**：账号在冷却里就一件都不拉；拉到一半抛 `RiskControlError` 立刻收手
   （剩下的商品留到下一轮，比「拉满 45 件然后被熔断」划算）。
2. **失败也要推进游标**：拉失败的商品写一行 `source='error'` + 原因 + 指标全 NULL。
   否则「最久没拉的优先」每轮都会挑回同一件失败商品，永远卡在它身上。
3. **拿不到就是 NULL**：平台没返回的指标存 NULL，绝不补 0（`0` 和「平台没给」
   在前端是两回事，硬塞 0 会让用户以为商品真没人看）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from ..core import db
from . import common, item_stats, risk_service

#: 单轮单账号默认拉几件（生产节奏由调用方/Lead 定，这里只给保守默认值）
DEFAULT_LIMIT = 5

#: `pull_once` 返回里 `stopped` 的取值
STOP_RISK = 'risk'
STOP_SESSION = 'session'
#: 商品数据接口这次就是不可用（`ItemStatsUnavailable`：会话拿不到这个接口族 /
#: 接口下线 / 响应认不出）。**这是系统性的，不是某一件事的问题** —— 真机实测
#: 每账号每轮 5 次调用全部以 `接口不可用: MTOP 请求异常:` 失败，`stopped` 还是 None，
#: 于是每 5 分钟重复 10 次注定失败的 mtop，把风控计数越推越高
#: （`risk_state.consecutive_hits` 从 1 涨到 2）。所以一件失败就收手，剩下的下一轮再说。
STOP_UNAVAILABLE = 'unavailable'

#: 只有这些账号会被后台拉：启用 + 有 Cookie + 登录态正常
ACTIVE_ACCOUNTS_SQL = """
SELECT a.id AS id
  FROM accounts a
  JOIN account_credentials c ON c.account_id = a.id
 WHERE a.enabled = 1
   AND COALESCE(c.cookie, '') <> ''
   AND c.login_state = 'active'
 ORDER BY a.id
"""

#: 「最久没拉过 / 从没拉过」的在售商品：没缓存行的 `fetched_at` 当成空串排最前
STALE_ITEMS_SQL = """
SELECT i.item_id AS item_id
  FROM items i
  LEFT JOIN item_stats_cache c
         ON c.account_id = i.account_id AND c.item_id = i.item_id
 WHERE i.account_id = ?
   AND i.status = 'on_sale'
 ORDER BY COALESCE(c.fetched_at, '') ASC, i.item_id ASC
 LIMIT ?
"""

UPSERT_SQL = """
INSERT OR REPLACE INTO item_stats_cache
    (account_id, item_id, exposure, visits, visitors, wants, orders, buyers, paid_orders,
     source, error, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
"""


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _number(value: Any) -> Optional[float]:
    """数字原样过；**认不出就是 None**（`0` 是平台真说的 0，要留着）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _error_kind(exc: BaseException) -> str:
    """按**类名**认异常，不 import 协议层。

    这样测试注入的假协议层只要类名对得上就能测，也避免服务层依赖
    `backend/app/xianyu/client.py`（协议层没装时 worker 也得能 import）。
    """
    names = {klass.__name__ for klass in type(exc).__mro__}
    if 'RiskControlError' in names:
        return 'risk'
    if 'SessionExpiredError' in names:
        return 'session'
    if 'ItemStatsUnavailable' in names:
        return 'unavailable'
    return 'other'


def active_accounts() -> List[str]:
    return [_text(row['id']) for row in db.query(ACTIVE_ACCOUNTS_SQL)]


def stale_items(account_id: str, limit: int) -> List[str]:
    return [_text(row['item_id'])
            for row in db.query(STALE_ITEMS_SQL, (account_id, max(0, int(limit))))]


def write_cache(account_id: str, item_id: str, metrics: Optional[Dict[str, Any]],
                source: str, error: str = '') -> None:
    """把一件商品的结果写进缓存（`INSERT OR REPLACE`：一件商品永远只有一行）。"""
    values = metrics or {}
    db.execute(UPSERT_SQL, (
        account_id, item_id,
        _number(values.get('exposure')), _number(values.get('visits')),
        _number(values.get('visitors')), _number(values.get('wants')),
        _number(values.get('orders')), _number(values.get('buyers')),
        _number(values.get('paid_orders')),
        source, _text(error)[:300],
    ))


async def pull_once(limit_per_account: int = DEFAULT_LIMIT, pause: float = 0.0) -> Dict[str, Any]:
    """跑一轮：每个可用账号挑「最久没拉」的最多 N 件在售商品，逐个写缓存。

    `pause` 是**每件之间**的间隔（测试传 0，生产由调用方定，用来摊开 mtop 请求）。
    """
    limit = max(0, int(limit_per_account or 0))
    report: Dict[str, Any] = {
        'accounts': {}, 'pulled': 0, 'failed': 0, 'paused': bool(pause),
    }
    for account_id in active_accounts():
        stat = {'pulled': 0, 'failed': 0, 'stopped': None}
        report['accounts'][account_id] = stat
        if limit == 0:
            continue
        if risk_service.is_blocked(account_id):
            # 冷却中：一件都不拉（这时候调平台只会把冷却时间拉长）
            stat['stopped'] = STOP_RISK
            continue
        client, _failure = common.get_client(account_id)
        if client is None:
            stat['stopped'] = STOP_SESSION
            continue
        for item_id in stale_items(account_id, limit):
            try:
                payload = await client.item_stats(item_id)
            except Exception as exc:  # noqa: BLE001 —— 单件失败不许崩掉整轮
                kind = _error_kind(exc)
                stat['failed'] += 1
                write_cache(account_id, item_id, None, item_stats.SOURCE_ERROR,
                            _text(exc) or type(exc).__name__)
                if kind == 'risk':
                    # 被风控：本账号立刻收手，但这一件也要落一行 error —— 否则下一轮
                    # 第一个挑中的还是它，白撞一次风控。
                    stat['stopped'] = STOP_RISK
                    break
                if kind in ('session', 'unavailable'):
                    # 系统性问题：换个商品重试不会有不同结果，只是多打几次 mtop。
                    stat['stopped'] = STOP_SESSION if kind == 'session' else STOP_UNAVAILABLE
                    break
                if pause:
                    await asyncio.sleep(pause)
            metrics = item_stats._normalize_platform(payload)
            if not metrics:
                # 平台没给可识别的指标（含「返回 None」）→ 记失败 + 真实原因
                stat['failed'] += 1
                write_cache(account_id, item_id, None, item_stats.SOURCE_ERROR,
                            item_stats.WARN_PLATFORM_UNPARSED)
            else:
                stat['pulled'] += 1
                write_cache(account_id, item_id, metrics, item_stats.SOURCE_PLATFORM)
            if pause:
                await asyncio.sleep(pause)
        report['pulled'] += stat['pulled']
        report['failed'] += stat['failed']
    return report


def _count(sql: str, params: Tuple[Any, ...]) -> int:
    row = db.query_one(sql, params)
    if row is None:
        return 0
    return int(row['n'] or 0)


def progress(account_id: str) -> Dict[str, Any]:
    """这个账号拉到哪了：`(在售件数, 已缓存, 失败, 最近一次成功时间)`。"""
    account = _text(account_id).strip()
    total = _count("SELECT COUNT(*) AS n FROM items WHERE account_id = ? AND status = 'on_sale'",
                   (account,))
    cached = _count("SELECT COUNT(*) AS n FROM item_stats_cache "
                    "WHERE account_id = ? AND source <> ?", (account, item_stats.SOURCE_ERROR))
    failed = _count("SELECT COUNT(*) AS n FROM item_stats_cache "
                    "WHERE account_id = ? AND source = ?", (account, item_stats.SOURCE_ERROR))
    last = db.query_one("SELECT MAX(fetched_at) AS last_at FROM item_stats_cache "
                        "WHERE account_id = ? AND source <> ?",
                        (account, item_stats.SOURCE_ERROR))
    return {
        'account_id': account,
        'total': total,
        'cached': cached,
        'failed': failed,
        'last_at': (_text(last['last_at']) or None) if last is not None else None,
    }
