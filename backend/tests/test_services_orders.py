"""订单服务测试：列表/单条/统计、同步（协议层懒导入）、发货、物流、operation_logs。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_services_orders.py"

**不打真实闲鱼平台**：协议层用 sys.modules 注入假模块。
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import config, db, events  # noqa: E402
from backend.app.services import common, orders  # noqa: E402

CLIENT_MODULE = 'backend.app.xianyu.client'


class FakeClient:
    """假协议层：只记调用，不联网。"""

    def __init__(self, account_id='acc1'):
        self.account_id = account_id
        self.calls = []
        self.logistics_calls = 0

    def list_orders(self, account_id):
        self.calls.append(('list_orders', account_id))
        return {
            'orders': [
                {'order_id': 'O1', 'item_id': 'I1', 'buyer_id': 'b1', 'amount': 99.5, 'status': 'paid'},
                {'order_id': 'O2', 'item_id': 'I2', 'buyer_id': 'b2', 'total_fee': '20', 'status': 'shipped'},
            ]
        }

    def ship_order(self, account_id, order_id, company, tracking_no):
        self.calls.append(('ship_order', account_id, order_id, company, tracking_no))
        return {'ok': True}

    def query_logistics(self, account_id, order_id, tracking_no):
        self.logistics_calls += 1
        return [{'time': '2026-01-01', 'desc': '已揽收'}]


def install_client(client) -> None:
    module = types.ModuleType(CLIENT_MODULE)
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    sys.modules[CLIENT_MODULE] = module


def install_boom_client(message: str) -> None:
    module = types.ModuleType(CLIENT_MODULE)

    class BoomClient:
        def __init__(self, account_id):
            raise RuntimeError(message)

    module.XianyuClient = BoomClient  # type: ignore[attr-defined]
    sys.modules[CLIENT_MODULE] = module


def block_client() -> None:
    """sys.modules 里放 None -> import 抛 ImportError（协议层还没写完）。"""
    sys.modules[CLIENT_MODULE] = None  # type: ignore[assignment]


def clear_client() -> None:
    sys.modules.pop(CLIENT_MODULE, None)


class OrdersServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        clear_client()
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.execute('DELETE FROM shipments')
        db.execute('DELETE FROM orders')
        db.execute('DELETE FROM operation_logs')
        clear_client()
        self.client = FakeClient()

    def seed(self, order_id='O1', account='acc1', amount=100.0, status='paid',
             item_id='I1', buyer_id='b1'):
        db.execute(
            'INSERT INTO orders(account_id, order_id, item_id, buyer_id, amount, status) VALUES(?,?,?,?,?,?)',
            (account, order_id, item_id, buyer_id, amount, status),
        )
        return order_id

    # ------------------------------------------------------------------ 读

    def test_list_orders_filters_and_pagination(self):
        self.seed('O1', status='paid', amount=10)
        self.seed('O2', status='shipped', amount=20)
        self.seed('O3', account='acc2', status='paid', amount=30)

        self.assertEqual(orders.list_orders()['total'], 3)
        self.assertEqual(orders.list_orders(account_id='acc1')['total'], 2)
        self.assertEqual(orders.list_orders(status='paid')['total'], 2)

        page = orders.list_orders(page=1, size=2)
        self.assertEqual(len(page['orders']), 2)
        self.assertEqual(page['pages'], 2)

        found = orders.list_orders(keyword='O3')
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['orders'][0]['order_id'], 'O3')

    def test_get_order_with_shipment(self):
        self.seed('O1')
        order = orders.get_order('O1')
        self.assertEqual(order['order_id'], 'O1')
        self.assertIsNone(order['shipment'])
        self.assertEqual(order['shipments'], [])

        orders.update_shipment('O1', '顺丰', 'SF123')
        order = orders.get_order('O1')
        self.assertEqual(order['status'], 'shipped')
        self.assertEqual(order['shipment']['tracking_no'], 'SF123')
        self.assertEqual(len(order['shipments']), 1)

    def test_get_order_missing(self):
        result = orders.get_order('NOPE')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_order_stats(self):
        self.seed('O1', status='paid', amount=10.5)
        self.seed('O2', status='wait_ship', amount=20)
        self.seed('O3', status='shipped', amount=30)
        stats = orders.order_stats()
        self.assertEqual(stats['total'], 3)
        self.assertEqual(stats['amount'], 60.5)
        self.assertEqual(stats['pending'], 2)
        self.assertEqual(stats['shipped'], 1)
        self.assertEqual(stats['by_status']['paid'], 1)

        orders.update_shipment('O1', '中通', 'ZT1')
        self.assertEqual(orders.order_stats()['shipments'], 1)

    # ------------------------------------------------------------------ 同步

    def test_sync_orders_with_fake_protocol(self):
        install_client(self.client)
        result = orders.sync_orders('acc1')
        self.assertNotIn('success', result)
        self.assertEqual(result['count'], 2)
        self.assertEqual(orders.list_orders(account_id='acc1')['total'], 2)
        # total_fee 字符串也能转 float
        self.assertEqual(orders.get_order('O2')['amount'], 20.0)

        # 再同步是幂等 upsert
        result = orders.sync_orders('acc1')
        self.assertEqual(result['count'], 2)
        self.assertEqual(orders.list_orders(account_id='acc1')['total'], 2)

    def test_sync_orders_requires_account(self):
        result = orders.sync_orders('')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_request')

    def test_sync_orders_not_ready(self):
        block_client()
        try:
            result = orders.sync_orders('acc1')
        finally:
            clear_client()
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'xianyu_not_ready')

        logs = db.query("SELECT * FROM operation_logs WHERE action = 'sync_orders'")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['success'], 0)

    def test_client_construction_failure_is_not_ready(self):
        """cookie 无效/缺 unb -> 仍然是 xianyu_not_ready，message 保留真实原因。"""
        install_boom_client('Cookie 里缺少 unb，无法确定账号 ID')
        try:
            result = orders.sync_orders('acc1')
        finally:
            clear_client()
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'xianyu_not_ready')
        self.assertIn('Cookie 里缺少 unb', result['message'])

    # ------------------------------------------------------------------ 发货

    def test_update_shipment_local_only(self):
        self.seed('O1')
        result = orders.update_shipment('O1', '顺丰', 'SF001')
        self.assertEqual(result['tracking_no'], 'SF001')
        self.assertIn('warning', result)          # 协议层未就绪
        self.assertTrue(result['reminder'])       # 发货提醒文案（来自 cs 模板）
        self.assertIn('SF001', result['reminder'])
        self.assertEqual(orders.get_order('O1')['status'], 'shipped')

        logs = db.query("SELECT * FROM operation_logs WHERE action = 'update_shipment'")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['success'], 1)

    def test_update_shipment_pushes_to_protocol(self):
        install_client(self.client)
        self.seed('O1')
        result = orders.update_shipment('O1', '顺丰', 'SF001')
        self.assertNotIn('warning', result)
        self.assertEqual(self.client.calls[0], ('ship_order', 'acc1', 'O1', '顺丰', 'SF001'))

    def test_update_shipment_validation_and_missing(self):
        self.seed('O1')
        for company, tracking in (('', 'SF1'), ('顺丰', '')):
            result = orders.update_shipment('O1', company, tracking)
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'invalid_fields')

        result = orders.update_shipment('NOPE', '顺丰', 'SF1')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_update_shipment_is_idempotent(self):
        self.seed('O1')
        orders.update_shipment('O1', '顺丰', 'SF001')
        orders.update_shipment('O1', '顺丰', 'SF001')
        # 同一单号不重复插 shipment
        self.assertEqual(len(db.query('SELECT * FROM shipments')), 1)

    def test_deliver(self):
        self.seed('O1', status='shipped')
        result = orders.deliver('O1')
        self.assertEqual(result['status'], 'delivered')
        self.assertEqual(orders.get_order('O1')['status'], 'delivered')

        missing = orders.deliver('NOPE')
        self.assertFalse(missing['success'])
        self.assertEqual(missing['code'], 'not_found')

    # ------------------------------------------------------------------ 物流

    def test_get_logistics_prefers_protocol(self):
        self.seed('O1')
        orders.update_shipment('O1', '顺丰', 'SF001')
        install_client(self.client)
        result = orders.get_logistics('O1')
        self.assertEqual(result['source'], 'xianyu')
        self.assertEqual(self.client.logistics_calls, 1)

    def test_get_logistics_falls_back_to_local(self):
        self.seed('O1')
        result = orders.get_logistics('O1')
        self.assertEqual(result['source'], 'local')
        self.assertIn('warning', result)

        missing = orders.get_logistics('NOPE')
        self.assertFalse(missing['success'])
        self.assertEqual(missing['code'], 'not_found')

    # ------------------------------------------------------------------ 回调落单 / 事件

    def test_upsert_order(self):
        result = orders.upsert_order({'order_id': 'X1', 'item_id': 'I9', 'amount': '12.5', 'status': 'paid'}, 'acc1')
        self.assertEqual(result['order_id'], 'X1')
        self.assertEqual(result['amount'], 12.5)

        orders.upsert_order({'order_id': 'X1', 'amount': 20, 'status': 'shipped'}, 'acc1')
        self.assertEqual(orders.list_orders()['total'], 1)
        self.assertEqual(orders.get_order('X1')['status'], 'shipped')

        self.assertFalse(orders.upsert_order({'amount': 1}, 'acc1')['success'])
        self.assertEqual(orders.upsert_order({'amount': 1}, 'acc1')['code'], 'invalid_fields')
        self.assertEqual(orders.upsert_order('nope', 'acc1')['code'], 'bad_request')

    def test_events_published(self):
        self.seed('O1')
        install_client(self.client)
        before = len(events.recent(limit=500))
        orders.sync_orders('acc1')
        orders.update_shipment('O1', '顺丰', 'SF1')
        orders.deliver('O1')
        topics = [e['topic'] for e in events.recent(limit=500)[before:]]
        self.assertIn('orders.synced', topics)
        self.assertIn('orders.shipment_updated', topics)
        self.assertIn('orders.delivered', topics)


if __name__ == '__main__':
    unittest.main(verbosity=2)
