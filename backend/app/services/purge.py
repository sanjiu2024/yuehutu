"""删号时的本地数据清理，以及「孤儿行」的自愈清理。

为什么有这个模块（用户真机提问：「删除的账号数据怎么还在？」）：
`xianyu/store.py::delete_account()` 只删 `accounts` / `account_credentials` / `risk_state` /
`risk_events`，`services/chats.py::purge_account_data()` 只删 `conversations` / `messages`。
于是 `items` / `item_stats_cache` / `operation_logs` / `orders` / `shipments` / 账号级
`ai_prompts` 全部留在库里，`account_id` 指向一个已经不存在的账号 —— 商品页照样把它们
列出来（真机上删掉的账号留下 39 件商品，用户看到的就是「删了账号商品还在」）。

两条口径：
1. **删号 = 删掉这台机器上属于它的所有本地行**。唯一例外是 `agent_chat_messages`
   （主管 AI 的对话，`thread='master'`，不属于任何账号）。重新扫码登录会重新同步回来。
2. **孤儿清理只按账号表判断**：`account_id` 不在 `accounts` 里才算孤儿。不猜、不按时间、
   不按前缀 —— 判断依据只有账号表本身，所以不会误删「刚刚还没同步完」的正常账号数据。
"""

from __future__ import annotations

from typing import Any, Dict, List

from loguru import logger

from ..core import db

# 带 account_id 的业务表（`conversations`/`messages` 归 chats 服务，见文件头）。
ACCOUNT_TABLES = ('items', 'item_stats_cache', 'operation_logs', 'orders', 'ai_prompts')


def _count(sql: str, params: tuple = ()) -> int:
    row = db.query_one(sql, params)
    return int(row['c']) if row else 0


def _purge_one(account_id: str) -> Dict[str, int]:
    """删掉一个账号在本地业务表里的所有行，返回各表条数。"""
    counts: Dict[str, int] = {}
    for table in ACCOUNT_TABLES:
        counts[table] = _count(
            f'SELECT COUNT(*) AS c FROM {table} WHERE account_id = ?', (account_id,))
    # 发货单挂在订单号上，不是 account_id：必须先按订单号删，否则会留孤儿。
    counts['shipments'] = _count(
        'SELECT COUNT(*) AS c FROM shipments WHERE order_id IN '
        '(SELECT order_id FROM orders WHERE account_id = ?)', (account_id,))

    if not any(counts.values()):
        return counts

    with db.tx():
        db.execute(
            'DELETE FROM shipments WHERE order_id IN '
            '(SELECT order_id FROM orders WHERE account_id = ?)', (account_id,))
        for table in ACCOUNT_TABLES:
            db.execute(f'DELETE FROM {table} WHERE account_id = ?', (account_id,))
    return counts


def purge_account_data(account_id: str) -> Dict[str, int]:
    """删号时调用：清掉该账号的商品/缓存/操作日志/订单/发货单/账号级提示词。

    会话与消息由 `services/chats.py::purge_account_data()` 负责（两份 SQL 会分叉，
    所以这里不重复实现）。幂等：再调一次全是 0。
    """
    if not account_id:
        return {table: 0 for table in ACCOUNT_TABLES} | {'shipments': 0}
    counts = _purge_one(str(account_id))
    if any(counts.values()):
        logger.info('purge_account_data: account={} {}', account_id, counts)
    return counts


def orphan_account_ids() -> List[str]:
    """找出「业务表里有数据、但账号表里没有」的 account_id（去重、排序）。"""
    ids: set = set()
    for table in ACCOUNT_TABLES + ('conversations',):
        rows = db.query(
            f"SELECT DISTINCT account_id AS a FROM {table} "
            "WHERE account_id IS NOT NULL AND account_id <> '' "
            'AND account_id NOT IN (SELECT id FROM accounts)',
        )
        ids.update(str(row['a']) for row in rows)
    return sorted(ids)


def purge_orphans() -> Dict[str, Any]:
    """启动时自愈：把孤儿账号残留的本地行清掉（含会话/消息）。

    返回 `{'accounts': {id: 各表条数}, 'purged_accounts': n, 'purged_rows': n}`。
    没有孤儿时不做任何写操作。
    """
    result: Dict[str, Any] = {'accounts': {}, 'purged_accounts': 0, 'purged_rows': 0}
    for account_id in orphan_account_ids():
        counts = _purge_one(account_id)
        try:
            from . import chats as chats_service

            chats_counts = chats_service.purge_account_data(account_id)
            counts.update({k: int(v) for k, v in dict(chats_counts).items()})
        except Exception as exc:  # noqa: BLE001 - 会话清理失败不该拦住其它表的清理
            logger.warning('purge_orphans: 清理 {} 的会话失败: {}', account_id, exc)
        result['accounts'][account_id] = counts
        result['purged_rows'] += sum(int(v) for v in counts.values())
    result['purged_accounts'] = len(result['accounts'])
    if result['purged_accounts']:
        logger.info(
            'purge_orphans: 清掉 {} 个已删除账号的残留本地数据（共 {} 行）: {}',
            result['purged_accounts'], result['purged_rows'], result['accounts'])
    return result
