"""`services/stats_worker.py` + `item_stats` 的 `source='cache'` 契约测试。

**为什么要这一组**：商品数据改成「后台慢慢拉、页面读缓存」之后，有两个地方一写错
用户就遭殃 —— ①后台把平台**没给的指标写成 0**（用户以为商品真没人看）；
②拉失败的商品**不推进游标**（每轮都卡在同一件，缓存永远是空的）。
另外风控是这套东西的命门：被熔断的账号一件都不许拉，拉到一半被风控必须立刻收手。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_stats_worker.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import db  # noqa: E402
from backend.app.services import item_stats, stats_worker  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'worker_acc'
OTHER = 'worker_other'
ITEM_1 = 'w_item_1'
ITEM_2 = 'w_item_2'
ITEM_3 = 'w_item_3'


class RiskControlError(RuntimeError):
    """与协议层同名：worker 按**类名**认风控，不看文案。"""


class SessionExpiredError(RuntimeError):
    """与协议层同名。"""


class ItemStatsUnavailable(RuntimeError):
    """与协议层同名：平台没给数据（不是风控）。"""


class FakeClient:
    """假协议层：`payloads[item_id]` 是返回值，`errors[item_id]` 非空则抛出。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[str] = []

    async def item_stats(self, item_id: str = '') -> Any:
        self.calls.append(item_id)
        exc = self.errors.get(item_id)
        if exc is not None:
            raise exc
        return self.payloads.get(item_id)


def install_fake_protocol(client: FakeClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, enabled: int = 1, login_state: str = 'active',
                 cookie: str = 'cookie-x') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,1)', (account_id, '后台拉取账号', enabled))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def seed_item(item_id: str, account_id: str = ACC, status: str = 'on_sale') -> None:
    db.execute('INSERT OR REPLACE INTO items(account_id, item_id, title, status) '
               'VALUES(?,?,?,?)', (account_id, item_id, item_id, status))


def cache_row(item_id: str, account_id: str = ACC) -> Any:
    return db.query_one('SELECT * FROM item_stats_cache WHERE account_id = ? AND item_id = ?',
                        (account_id, item_id))


def set_fetched_at(item_id: str, stamp: str, account_id: str = ACC) -> None:
    db.execute('UPDATE item_stats_cache SET fetched_at = ? WHERE account_id = ? AND item_id = ?',
               (stamp, account_id, item_id))


def pull(limit: int = 5) -> Dict[str, Any]:
    return asyncio.run(stats_worker.pull_once(limit_per_account=limit, pause=0))


class StatsWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fake_protocol()
        for table in ('item_stats_cache', 'items', 'account_credentials', 'accounts', 'risk_state'):
            db.execute(f'DELETE FROM {table}')

    # -- ① 一轮只拉 N 件 -------------------------------------------------

    def test_pull_once_respects_limit_per_account(self):
        seed_account()
        for item_id in (ITEM_1, ITEM_2, ITEM_3):
            seed_item(item_id)
        fake = FakeClient({ITEM_1: {'exposure': 1}, ITEM_2: {'exposure': 2},
                           ITEM_3: {'exposure': 3}})
        install_fake_protocol(fake)

        report = pull(limit=2)
        self.assertEqual(report['pulled'], 2, report)
        self.assertEqual(len(fake.calls), 2, f'只该拉 2 件: {fake.calls}')
        self.assertEqual(report['accounts'][ACC]['pulled'], 2)

    def test_only_enabled_active_accounts_are_pulled(self):
        """没启用 / 登录态不对的账号，后台一件都不拉。"""
        seed_account(ACC)
        seed_account(OTHER, enabled=0)
        seed_item(ITEM_1)
        seed_item(ITEM_2, account_id=OTHER)
        fake = FakeClient({ITEM_1: {'exposure': 1}, ITEM_2: {'exposure': 2}})
        install_fake_protocol(fake)

        report = pull(limit=5)
        self.assertEqual(list(report['accounts']), [ACC], report)
        self.assertEqual(fake.calls, [ITEM_1])

    # -- ② 最久没拉的优先 ------------------------------------------------

    def test_stalest_items_are_pulled_first(self):
        seed_account()
        for item_id in (ITEM_1, ITEM_2, ITEM_3):
            seed_item(item_id)
        # ITEM_1 很早拉过、ITEM_2 刚拉过、ITEM_3 从没拉过
        stats_worker.write_cache(ACC, ITEM_1, {'exposure': 1}, item_stats.SOURCE_PLATFORM)
        stats_worker.write_cache(ACC, ITEM_2, {'exposure': 2}, item_stats.SOURCE_PLATFORM)
        set_fetched_at(ITEM_1, '2020-01-01 00:00:00')
        fake = FakeClient({ITEM_3: {'exposure': 3}, ITEM_1: {'exposure': 11}})
        install_fake_protocol(fake)

        pull(limit=2)
        self.assertEqual(sorted(fake.calls), sorted([ITEM_3, ITEM_1]),
                         f'从没拉过的 + 最久没拉的优先: {fake.calls}')

    def test_off_shelf_items_are_not_pulled(self):
        seed_account()
        seed_item(ITEM_1, status='off_shelf')
        seed_item(ITEM_2)
        fake = FakeClient({ITEM_1: {'exposure': 1}, ITEM_2: {'exposure': 2}})
        install_fake_protocol(fake)

        pull(limit=5)
        self.assertEqual(fake.calls, [ITEM_2], '只拉在售商品')

    # -- ③ 失败写 error，且下一轮不卡住 ----------------------------------

    def test_failure_is_cached_as_error_and_cursor_advances(self):
        seed_account()
        seed_item(ITEM_1)
        seed_item(ITEM_2)
        fake = FakeClient({ITEM_2: {'exposure': 22}}, errors={ITEM_1: ItemStatsUnavailable('平台没给')})
        install_fake_protocol(fake)

        first = pull(limit=1)
        self.assertEqual((first['pulled'], first['failed']), (0, 1), first)
        row = cache_row(ITEM_1)
        self.assertEqual(row['source'], 'error')
        self.assertIn('平台没给', row['error'])
        self.assertIsNone(row['exposure'], '失败行的指标必须全 NULL，不许写 0')

        second = pull(limit=1)
        self.assertEqual(fake.calls, [ITEM_1, ITEM_2], f'第二轮该轮到 ITEM_2: {fake.calls}')
        self.assertEqual(second['pulled'], 1)

    # -- ④⑤ 风控 ---------------------------------------------------------

    def test_blocked_account_pulls_nothing(self):
        seed_account()
        seed_item(ITEM_1)
        db.execute('INSERT OR REPLACE INTO risk_state(account_id, blocked_until) VALUES(?,?)',
                   (ACC, time.time() + 600))
        fake = FakeClient({ITEM_1: {'exposure': 1}})
        install_fake_protocol(fake)

        report = pull(limit=5)
        self.assertEqual(fake.calls, [], '熔断中的账号一件都不许拉')
        self.assertEqual(report['accounts'][ACC]['stopped'], 'risk')
        self.assertEqual(report['pulled'], 0)

    def test_risk_error_mid_pull_stops_the_account(self):
        seed_account()
        for item_id in (ITEM_1, ITEM_2, ITEM_3):
            seed_item(item_id)
        fake = FakeClient({ITEM_1: {'exposure': 1}},
                          errors={ITEM_2: RiskControlError('被风控了')})
        install_fake_protocol(fake)

        report = pull(limit=3)
        self.assertEqual(fake.calls, [ITEM_1, ITEM_2], f'风控后不许继续拉: {fake.calls}')
        self.assertEqual(report['accounts'][ACC]['stopped'], 'risk')
        self.assertEqual(report['pulled'], 1)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(cache_row(ITEM_3), None, 'ITEM_3 根本不该被调')

    def test_session_expired_records_error_and_continues(self):
        """单件登录态过期不该让整轮崩掉：记 error 后继续下一件。"""
        seed_account()
        seed_item(ITEM_1)
        seed_item(ITEM_2)
        fake = FakeClient({ITEM_2: {'exposure': 22}},
                          errors={ITEM_1: SessionExpiredError('会话过期')})
        install_fake_protocol(fake)

        report = pull(limit=5)
        self.assertEqual(sorted(fake.calls), sorted([ITEM_1, ITEM_2]))
        self.assertEqual(report['pulled'], 1)
        self.assertEqual(report['failed'], 1)
        self.assertEqual(report['accounts'][ACC]['stopped'], 'session')

    # -- ⑥ NULL 语义 -----------------------------------------------------

    def test_missing_metrics_are_stored_as_null_not_zero(self):
        seed_account()
        seed_item(ITEM_1)
        install_fake_protocol(FakeClient({ITEM_1: {'exposure': 5}}))

        pull(limit=1)
        row = cache_row(ITEM_1)
        self.assertEqual(row['exposure'], 5)
        for column in ('visits', 'visitors', 'wants', 'orders', 'buyers', 'paid_orders'):
            self.assertIsNone(row[column], f'{column} 平台没给，必须 NULL 而不是 0')
        self.assertEqual(row['source'], 'platform')

    def test_unparsable_payload_is_recorded_as_error(self):
        """平台回了个认不出的东西（含 None）→ 记 error，不许假装成功。"""
        seed_account()
        seed_item(ITEM_1)
        install_fake_protocol(FakeClient({ITEM_1: None}))

        report = pull(limit=1)
        self.assertEqual((report['pulled'], report['failed']), (0, 1), report)
        row = cache_row(ITEM_1)
        self.assertEqual(row['source'], 'error')
        self.assertTrue(row['error'])

    # -- ⑦ cache 模式零平台调用 ------------------------------------------

    def test_cache_mode_never_touches_platform(self):
        seed_account()
        seed_item(ITEM_1)
        seed_item(ITEM_2)
        stats_worker.write_cache(ACC, ITEM_1, {'exposure': 41, 'visits': 5, 'wants': 2},
                                 item_stats.SOURCE_PLATFORM)
        fake = FakeClient({ITEM_1: {'exposure': 1}})
        install_fake_protocol(fake)

        result = item_stats.account_stats(ACC, source='cache')
        self.assertNotIn('success', result, result)
        self.assertEqual(result['source'], 'cache')
        self.assertEqual(fake.calls, [], 'cache 模式一次平台调用都不许有')
        first = [i for i in result['items'] if i['item_id'] == ITEM_1][0]
        self.assertEqual((first['exposure'], first['visits'], first['wants']), (41, 5, 2))
        second = [i for i in result['items'] if i['item_id'] == ITEM_2][0]
        self.assertIsNone(second['exposure'], '缓存里没有的商品：平台指标为 null')

    def test_cache_mode_works_without_cookie(self):
        """读缓存不碰平台，所以没 Cookie 也该能用（跟 `source=local` 一样）。"""
        seed_account(cookie='', login_state='need_relogin')
        seed_item(ITEM_1)
        stats_worker.write_cache(ACC, ITEM_1, {'exposure': 7}, item_stats.SOURCE_PLATFORM)

        result = item_stats.account_stats(ACC, source='cache')
        self.assertEqual(result['source'], 'cache')
        self.assertEqual(result['items'][0]['exposure'], 7)

    def test_cache_mode_with_empty_cache_warns(self):
        seed_account()
        seed_item(ITEM_1)
        result = item_stats.account_stats(ACC, source='cache')
        self.assertEqual(result['source'], 'cache')
        self.assertIsNone(result['items'][0]['exposure'])
        self.assertTrue(any('后台还没有拉到商品数据' in w for w in result['warnings']),
                        result['warnings'])

    def test_cache_mode_reports_failed_rows(self):
        seed_account()
        seed_item(ITEM_1)
        stats_worker.write_cache(ACC, ITEM_1, None, item_stats.SOURCE_ERROR, '被风控了')
        result = item_stats.account_stats(ACC, source='cache')
        self.assertIsNone(result['items'][0]['exposure'])
        self.assertTrue(any('被风控了' in w for w in result['warnings']), result['warnings'])

    # -- ⑧ progress -------------------------------------------------------

    def test_progress_counts(self):
        seed_account()
        seed_item(ITEM_1)
        seed_item(ITEM_2)
        seed_item(ITEM_3, status='off_shelf')
        stats_worker.write_cache(ACC, ITEM_1, {'exposure': 1}, item_stats.SOURCE_PLATFORM)
        stats_worker.write_cache(ACC, ITEM_2, None, item_stats.SOURCE_ERROR, '失败')

        info = stats_worker.progress(ACC)
        self.assertEqual(info['total'], 2, 'total 只数在售件')
        self.assertEqual(info['cached'], 1)
        self.assertEqual(info['failed'], 1)
        self.assertTrue(info['last_at'], info)
        self.assertEqual(stats_worker.progress('nobody')['total'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
