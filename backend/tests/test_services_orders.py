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
        #: 签名必须与真实 `backend/app/xianyu/client.py::XianyuClient(cookie_str, ...)` 一致。
        #: 老测试写的是 `__init__(self, account_id)` —— 照抄了 common.get_client 当年
        #: 那条**把账号 ID 当 Cookie 传**的错误假设，所以哪怕生产断链也一路绿。
        def __init__(self, cookie_str, client=None, transport=None):
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

        orders.update_shipment('O1', '顺丰', 'SF123', confirm=True)
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

        orders.update_shipment('O1', '中通', 'ZT1', confirm=True)
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

    def test_sync_orders_accepts_normalized_protocol_rows(self):
        """协议层 `list_orders` 回的是 `trade.normalize_order` 的**嵌套结构**。

        老映射只认扁平字段：没有 `buyer_id`/`amount` 的行会写成空串与 0，
        这条把真机形状锁住。
        """

        class NestedClient:
            def list_orders(self, account_id):
                return {'orders': [{
                    'order_id': 'O9',
                    'item_id': 'I9',
                    'order_status': '交易成功',
                    'buyer': {'user_id': 'b9'},
                    'price': {'auction_price': '39.90', 'buy_num': 2, 'total_price': '79.80'},
                }], 'total': 1, 'page': 1, 'pages': 1, 'size': 20}

        install_client(NestedClient())
        self.assertEqual(orders.sync_orders('acc1')['count'], 1)

        order = orders.get_order('O9')
        self.assertEqual(order['item_id'], 'I9')
        self.assertEqual(order['buyer_id'], 'b9')
        # 金额取 price.total_price（实付）；平台原文是字符串
        self.assertEqual(order['amount'], 79.8)
        self.assertEqual(order['status'], '交易成功')
        self.assertIsInstance(order['raw'], dict)
        # 「交易成功」是真机终态（trade_probe.json），要算进已发货那一档
        self.assertEqual(orders.order_stats()['shipped'], 1)

    def test_missing_total_price_is_not_estimated(self):
        """`total_price` 缺就落 NULL：**不能**拿「单价 × 件数」顶替。

        真机反例（`logs/seller_discover/trade_probe.json`，orderId 3316918549013103465）：
        `auctionPrice=199.00` / `buyNum=1` 而 `totalPrice=8.00`（优惠后实付）——
        推算会把实付 8.00 记成 199.00，还进 `order_stats` 的 `SUM(amount)`。
        """

        class NoTotalPriceClient:
            def list_orders(self, account_id):
                return {'orders': [{
                    'order_id': 'O7',
                    'item_id': 'I7',
                    'order_status': '交易成功',
                    'buyer': {'user_id': 'b7'},
                    'price': {'auction_price': '199.00', 'buy_num': 1, 'total_price': None},
                }]}

        install_client(NoTotalPriceClient())
        self.assertEqual(orders.sync_orders('acc1')['count'], 1)
        self.assertIsNone(orders.get_order('O7')['amount'])
        self.assertEqual(orders.order_stats()['amount'], 0.0)

    def test_sync_orders_leaves_missing_amount_null(self):
        """平台没给金额就落 NULL，不补 0（§5.4）—— 补 0 会把它算进本地成交额。"""

        class NoAmountClient:
            def list_orders(self, account_id):
                return {'orders': [{'order_id': 'O8', 'item_id': 'I8', 'status': 'paid'}]}

        install_client(NoAmountClient())
        self.assertEqual(orders.sync_orders('acc1')['count'], 1)
        self.assertIsNone(orders.get_order('O8')['amount'])
        # SUM 出 NULL 不能让统计接口整条报错
        self.assertEqual(orders.order_stats()['amount'], 0.0)

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
        """cookie 无效/缺 unb -> 仍然是 xianyu_not_ready，message 保留真实原因。

        必须先有一条凭证：`common.get_client` 现在会先 `store.get_cookie(account_id)`
        再构造客户端，没有 Cookie 时它连构造都不做（那是另一条路径，见
        `test_protocol_wiring.py`）。这里要测的正是「构造本身炸了」。
        """
        db.execute("INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) "
                   "VALUES('acc1','测试号',1,1)")
        db.execute("INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) "
                   "VALUES('acc1','unb=2200000001; cookie2=x','active')")
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
        result = orders.update_shipment('O1', '顺丰', 'SF001', confirm=True)
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
        result = orders.update_shipment('O1', '顺丰', 'SF001', confirm=True)
        self.assertNotIn('warning', result)
        self.assertEqual(self.client.calls[0], ('ship_order', 'acc1', 'O1', '顺丰', 'SF001'))

    def test_update_shipment_validation_and_missing(self):
        self.seed('O1')
        for company, tracking in (('', 'SF1'), ('顺丰', '')):
            result = orders.update_shipment('O1', company, tracking, confirm=True)
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'invalid_fields')

        result = orders.update_shipment('NOPE', '顺丰', 'SF1', confirm=True)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_update_shipment_is_idempotent(self):
        self.seed('O1')
        orders.update_shipment('O1', '顺丰', 'SF001', confirm=True)
        orders.update_shipment('O1', '顺丰', 'SF001', confirm=True)
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
        orders.update_shipment('O1', '顺丰', 'SF001', confirm=True)
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
        orders.update_shipment('O1', '顺丰', 'SF1', confirm=True)
        orders.deliver('O1')
        topics = [e['topic'] for e in events.recent(limit=500)[before:]]
        self.assertIn('orders.synced', topics)
        self.assertIn('orders.shipment_updated', topics)
        self.assertIn('orders.delivered', topics)


class OrderShipConfirmGateTest(OrdersServiceTest):
    """发货的确认闸门：**这是真发货**（会往平台写物流信息、买家能看到）。

    第 53 批补的闸门 —— 以前 `PUT /api/orders/{id}/shipment` 一路到平台都没有「你确定吗」。
    """

    #: 一律不算点头（`bool('false')` 是 `True`，所以只认字面量口径）。
    FALSE_LIKE = ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2, [], {})

    def test_without_confirm_is_400_and_touches_nothing(self):
        self.seed('O1')
        install_client(self.client)

        result = orders.update_shipment('O1', '顺丰', 'SF1')

        self.assertFalse(result['success'], result)
        self.assertEqual(result['code'], 'confirmation_required')
        # 一条货都没发出去、一条日志都没写、平台一次都没碰
        self.assertEqual(db.query('SELECT * FROM shipments'), [])
        self.assertEqual(orders.get_order('O1')['status'], 'paid')
        self.assertEqual(db.query('SELECT * FROM operation_logs'), [])
        self.assertEqual(self.client.calls, [])

    def test_false_like_values_are_all_rejected(self):
        self.seed('O1')
        for value in self.FALSE_LIKE:
            with self.subTest(value=value):
                self.assertEqual(
                    orders.update_shipment('O1', '顺丰', 'SF1', confirm=value)['code'],
                    'confirmation_required',
                )
        self.assertEqual(db.query('SELECT * FROM shipments'), [])

    def test_true_like_values_pass(self):
        for value in (True, 'true', ' TRUE ', 1):
            with self.subTest(value=value):
                self.setUp()
                self.seed('O1')
                result = orders.update_shipment('O1', '顺丰', 'SF1', confirm=value)
                self.assertNotIn('code', result, result)
                self.assertEqual(result['tracking_no'], 'SF1')

    def test_gate_runs_before_field_validation(self):
        """三步顺序：**确认 → 字段校验 → 建客户端**（没点头就别提「单号不能为空」）。"""
        self.seed('O1')
        result = orders.update_shipment('O1', '', '', confirm=False)
        self.assertEqual(result['code'], 'confirmation_required')

        checked = orders.update_shipment('O1', '', '', confirm=True)
        self.assertEqual(checked['code'], 'invalid_fields')


if __name__ == '__main__':
    unittest.main(verbosity=2)
