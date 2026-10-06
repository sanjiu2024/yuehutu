"""商品服务测试：增删改查、批量汇总、operation_logs 落库、协议层懒导入。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_services_items.py"

**不打真实闲鱼平台**：协议层用 sys.modules 注入假模块。
"""

from __future__ import annotations

import inspect
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import config, db, events  # noqa: E402
from backend.app.services import common, items  # noqa: E402
# 真协议层类的引用在**假模块注入之前**绑定，之后 FakeClient 随便替换 sys.modules
# 都不影响这里 —— 签名对拍必须以真类为基准。
from backend.app.xianyu.client import XianyuClient as RealXianyuClient  # noqa: E402


class FakeClient:
    """假协议层客户端：只记调用，不联网。

    **签名必须与 `backend/app/xianyu/client.py::XianyuClient` 逐字一致**。
    这个类曾经写成 `list_items(self, account_id)` / `publish_item(self, account_id, payload)`
    —— 顺着**服务层的错误调用**签名，于是 21 个用例全绿，真机上却
    `publish_item() takes 2 positional arguments but 3 were given`（502）。
    假对象比真对象宽松一分，测试就少挡一类真 bug，所以这里连默认值都照抄，
    并在文件末尾用 `inspect.signature` 逐方法对拍。
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.calls = []

    def list_items(self, page_number: int = 1, page_size: int = 20, max_pages: int = 25):
        self.calls.append(('list_items', page_number, page_size, max_pages))
        return {
            'items': [
                {'item_id': 'A1', 'title': 'iPhone 13', 'price': 2500, 'stock': 1, 'status': 'on_sale'},
                {'item_id': 'A2', 'title': 'AirPods', 'price': 400, 'stock': 2, 'status': 'on_sale'},
            ],
            'total': 2,
        }

    def edit_item(self, item_id: str, payload: dict):
        self.calls.append(('edit_item', item_id, payload))
        return {'ok': True}

    def publish_item(self, payload: dict):
        self.calls.append(('publish_item', payload))
        return {'item_id': 'NEW1'}

    def downshelf(self, item_id: str):
        self.calls.append(('downshelf', item_id))
        return {'ok': True}

    def reshelf(self, item_id: str):
        self.calls.append(('reshelf', item_id))
        return {'ok': True}

    def set_status(self, item_id: str, status: str):
        self.calls.append(('set_status', item_id, status))
        return {'ok': True}


def install_fake_protocol(client: FakeClient) -> types.ModuleType:
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    module.XianyuClient = FakeClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def block_protocol() -> None:
    """模拟「协议层还没写完」：sys.modules 里放 None，import 会抛 ImportError。"""
    sys.modules['backend.app.xianyu.client'] = None  # type: ignore[assignment]


def unblock_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def install_boom_protocol(message: str) -> types.ModuleType:
    """模块存在、但客户端构造就抛异常（cookie 无效/缺 unb 的真实形态）。

    `common.get_client` 现在是「先取 cookie、再构造」，所以只给 `XianyuClient`
    会先撞上「账号没有 Cookie」，永远走不到构造失败那条分支；这里同时给一个
    会抛异常的 `get_client` 工厂（协议层真实存在的那个入口），才是被测的分支。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    class BoomClient:
        def __init__(self, account_id):
            raise RuntimeError(message)

    def boom_factory(account_id):
        raise RuntimeError(message)

    module.XianyuClient = BoomClient  # type: ignore[attr-defined]
    module.get_client = boom_factory  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return module


class ItemsServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM operation_logs')
        remove_fake_protocol()
        unblock_protocol()
        self.client = FakeClient('acc1')

    def seed(self, item_id='I1', title='测试商品', price=100.0, stock=1, status='on_sale', account='acc1'):
        db.execute(
            'INSERT INTO items(account_id, item_id, title, price, stock, status) VALUES(?,?,?,?,?,?)',
            (account, item_id, title, price, stock, status),
        )
        return item_id

    # ------------------------------------------------------------------ 读

    def test_list_items_filter_and_keyword(self):
        self.seed('I1', '红色杯子')
        self.seed('I2', '蓝色杯子')
        self.seed('I3', '绿色盘子', account='acc2')

        result = items.list_items()
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['items']), 3)

        result = items.list_items(keyword='杯子')
        self.assertEqual(result['total'], 2)

        result = items.list_items(account_id='acc2')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['item_id'], 'I3')

        result = items.list_items(status='on_sale', page=1, size=2)
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['pages'], 2)

    def test_get_item_and_missing(self):
        self.seed('I1')
        item = items.get_item('I1')
        self.assertEqual(item['item_id'], 'I1')
        self.assertIsInstance(item['images'], list)
        self.assertTrue(item['ai_enabled'])

        missing = items.get_item('NOPE')
        self.assertFalse(missing['success'])
        self.assertEqual(missing['code'], 'not_found')

    def test_get_item_by_account(self):
        self.seed('I1', account='acc1')
        found = items.get_item_by_account('acc1', 'I1')
        self.assertEqual(found['item_id'], 'I1')
        # 账号不匹配 -> 拒绝（不许串号回落到别的账号的商品）
        missing = items.get_item_by_account('accX', 'I1')
        self.assertFalse(missing['success'])
        self.assertEqual(missing['code'], 'not_found')

    # ------------------------------------------------------------------ 改

    def test_update_item_fields_and_logs(self):
        self.seed('I1', price=100.0, title='旧标题')
        result = items.update_item('I1', {'价格': 88.5, '标题': '新标题', '库存': 5})
        self.assertNotIn('success', result)
        self.assertEqual(result['item']['price'], 88.5)
        self.assertEqual(result['item']['title'], '新标题')
        self.assertEqual(result['item']['stock'], 5)
        self.assertIn('warning', result)  # 协议层没就绪 -> 本地生效
        self.assertFalse(result['synced'])

        rows = db.query("SELECT * FROM operation_logs WHERE action = 'update_item'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['item_id'], 'I1')
        self.assertEqual(rows[0]['success'], 1)

    def test_update_item_rejects_bad_fields(self):
        self.seed('I1')
        result = items.update_item('I1', {'不存在的字段': 1})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_fields')

        result = items.update_item('I1', {'price': 'abc'})
        self.assertFalse(result['success'])

        result = items.update_item('NOPE', {'price': 1})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

        result = items.update_item('I1', {})
        self.assertFalse(result['success'])

    def test_update_item_pushes_to_protocol(self):
        install_fake_protocol(self.client)
        self.seed('I1')
        result = items.update_item('I1', {'price': 66})
        self.assertTrue(result['synced'])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][0], 'edit_item')

    # ------------------------------------------------------------------ 批量

    def test_batch_update_summary(self):
        """协议层**没**就绪：本地改了，但一条都没推出去 -> ok 必须是 0。

        这是本次修的缺陷：旧实现只 `ok += 1`，把 `update_item` 的 warning 吞掉，
        于是 HTTP 200 + ok=2，前端/MCP 都以为已经改到闲鱼上了。
        """
        self.seed('I1')
        self.seed('I2')
        result = items.batch_update(['I1', 'I2', 'MISSING'], {'价格': 50})
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['ok'], 0)          # 没推到平台 -> 不算成功
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['local_only'], 2)  # 本地改了但没推
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual(result['warnings'][0]['code'], 'not_pushed')
        self.assertEqual(len(result['failed']), 1)
        self.assertEqual(result['failed'][0]['id'], 'MISSING')
        self.assertEqual(result['skipped'], ['MISSING'])
        self.assertIn('不存在', result['failed'][0]['message'])

        # 逐条记录：2 条成功 + 1 条失败
        logs = db.query('SELECT * FROM operation_logs')
        self.assertEqual(len(logs), 3)
        self.assertEqual(sum(1 for r in logs if r['success'] == 1), 2)

        # 本地确实改了（warning 只是「没推到闲鱼」，不是「没改」）
        self.assertEqual(items.get_item('I1')['price'], 50.0)
        self.assertEqual(items.get_item('I2')['price'], 50.0)

    def test_batch_update_counts_ok_only_after_push(self):
        """反向对照：协议层真的推成功了，ok 才算 2。"""
        self.seed('I1')
        self.seed('I2')
        install_fake_protocol(self.client)
        try:
            result = items.batch_update(['I1', 'I2'], {'价格': 50})
            self.assertEqual(result['ok'], 2)
            self.assertEqual(result['local_only'], 0)
            self.assertEqual(result['warnings'], [])
            self.assertEqual(result['failed'], [])
            self.assertEqual([c[0] for c in self.client.calls], ['edit_item', 'edit_item'])
        finally:
            remove_fake_protocol()

    def test_batch_update_calc_patch(self):
        install_fake_protocol(self.client)
        try:
            self.seed('I1', price=100.0)
            self.seed('I2', price=200.0)
            result = items.batch_update(['I1', 'I2'], {'price_delta': -10})
            self.assertEqual(result['ok'], 2)
            self.assertEqual(items.get_item('I1')['price'], 90.0)
            self.assertEqual(items.get_item('I2')['price'], 190.0)

            result = items.batch_update(['I1'], {'title_prefix': '【特价】'})
            self.assertEqual(result['ok'], 1)
            self.assertTrue(items.get_item('I1')['title'].startswith('【特价】'))
        finally:
            remove_fake_protocol()

    def test_batch_update_empty_and_bad_fields(self):
        result = items.batch_update([], {'price': 1})
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['ok'], 0)
        self.assertTrue(result['failed'])

        self.seed('I1')
        result = items.batch_update(['I1'], {'price': 'x'})
        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['failed'][0]['id'], 'I1')

    # ------------------------------------------------------------------ 上下架

    def test_downshelf_and_reshelf(self):
        """协议层没就绪时 ok=0（本地状态改了、但没推到闲鱼）；推成功才算 ok。"""
        self.seed('I1')
        self.seed('I2')
        result = items.downshelf(['I1', 'I2', 'NOPE'])
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['local_only'], 2)
        self.assertEqual(len(result['warnings']), 2)
        self.assertEqual(len(result['failed']), 1)
        self.assertEqual(items.get_item('I1')['status'], 'off_shelf')

        result = items.reshelf(['I1'])
        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['local_only'], 1)
        self.assertEqual(items.get_item('I1')['status'], 'on_sale')

        # 同上，装上假协议层 -> 真的推成功了，ok 才等于条数
        install_fake_protocol(self.client)
        try:
            result = items.downshelf(['I1', 'I2', 'NOPE'])
            self.assertEqual(result['ok'], 2)
            self.assertEqual(result['local_only'], 0)
            self.assertEqual(result['warnings'], [])
            result = items.reshelf(['I1'])
            self.assertEqual(result['ok'], 1)
            self.assertEqual(result['warnings'], [])
        finally:
            remove_fake_protocol()

    # ------------------------------------------------------------------ 协议层

    def test_sync_items_with_fake_protocol(self):
        install_fake_protocol(self.client)
        result = items.sync_items('acc1')
        self.assertNotIn('success', result)
        self.assertEqual(result['count'], 2)
        self.assertEqual(len(result['items']), 2)

        # 再同步一次是 upsert，不重复
        result = items.sync_items('acc1')
        self.assertEqual(result['count'], 2)
        self.assertEqual(items.list_items(account_id='acc1')['total'], 2)

        logs = db.query("SELECT * FROM operation_logs WHERE action = 'sync_items'")
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]['success'], 1)

    def test_protocol_not_ready(self):
        """协议层模块缺失（ImportError）-> code 统一 xianyu_not_ready，绝不抛异常。"""
        block_protocol()
        try:
            result = items.sync_items('acc1')
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'xianyu_not_ready')
            self.assertTrue(result['message'])
            self.assertEqual(result['message'], '协议层尚未就绪')

            result = items.publish_item('acc1', {'title': 'x', 'price': 1})
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'xianyu_not_ready')
        finally:
            unblock_protocol()

        # 失败也要留痕
        logs = db.query("SELECT * FROM operation_logs WHERE success = 0")
        self.assertEqual(len(logs), 2)

    def test_client_construction_failure_is_not_ready(self):
        """模块在、但客户端构造失败（cookie 无效/缺 unb）-> 同样是 xianyu_not_ready。

        Lead 裁决：协议层不可用的**所有**形态都收敛成同一个 code，
        message 保留真实原因便于排查，且不抛异常到路由层。
        """
        install_boom_protocol('Cookie 里缺少 unb，无法确定账号 ID')
        try:
            result = items.sync_items('acc1')
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'xianyu_not_ready')
            self.assertIn('Cookie 里缺少 unb', result['message'])
            self.assertIn('协议层客户端创建失败', result['message'])

            result = items.publish_item('acc1', {'title': 'x', 'price': 1})
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'xianyu_not_ready')
            self.assertIn('Cookie 里缺少 unb', result['message'])
        finally:
            unblock_protocol()

    def test_get_item_by_account_pair(self):
        """CONTRACTS §3 的 GET /api/items/{account_id}/{item_id} 对应的服务层查询。"""
        self.seed('I1', account='acc1', title='甲')
        self.seed('I1', account='acc2', title='乙')
        self.seed('I2', account='acc1', title='丙')

        found = items.get_item_by_account('acc1', 'I1')
        self.assertTrue(found)
        self.assertEqual(found['title'], '甲')

        # 账号与商品必须配对：acc2 的 I1 不能顶替 acc1 的 I1
        self.assertEqual(items.get_item_by_account('acc2', 'I1')['title'], '乙')
        self.assertEqual(items.get_item_by_account('acc1', 'I2')['title'], '丙')
        for bad in (items.get_item_by_account('acc9', 'I1'), items.get_item_by_account('acc1', 'nope')):
            self.assertFalse(bad['success'])
            self.assertEqual(bad['code'], 'not_found')

    def test_sync_items_requires_account(self):
        result = items.sync_items('')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_request')

    def test_publish_item_with_fake_protocol(self):
        install_fake_protocol(self.client)
        result = items.publish_item('acc1', {'title': '新商品', 'price': 12.5, 'description': '描述'})
        self.assertNotIn('success', result)
        self.assertEqual(result['item_id'], 'NEW1')
        self.assertEqual(items.get_item('NEW1')['title'], '新商品')

    def test_publish_item_needs_title(self):
        result = items.publish_item('acc1', {'price': 1})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_fields')

    # ------------------------------------------------------------------ 统计与日志

    def test_item_stats(self):
        self.seed('I1', price=100.0, stock=2, status='on_sale')
        self.seed('I2', price=50.0, stock=1, status='off_shelf')
        stats = items.item_stats()
        self.assertEqual(stats['total'], 2)
        self.assertEqual(stats['on_sale'], 1)
        self.assertEqual(stats['by_status']['off_shelf'], 1)
        self.assertEqual(stats['inventory_value'], 250.0)

    def test_operation_logs_listing(self):
        self.seed('I1')
        items.update_item('I1', {'price': 1})
        items.update_item('I1', {'price': 2})
        logs = items.list_operation_logs(account_id='acc1', limit=10)
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(isinstance(log['success'], bool) for log in logs))
        self.assertEqual(items.list_operation_logs(account_id='nobody'), [])

    # ------------------------------------------------------------------ 事件

    def test_events_published(self):
        self.seed('I1')
        before = len(events.recent(limit=500))
        items.update_item('I1', {'price': 9})
        items.downshelf(['I1'])
        after = events.recent(limit=500)
        topics = [e['topic'] for e in after[before:]]
        self.assertIn('items.updated', topics)
        self.assertIn('items.status_changed', topics)


class FakeClientSignatureTest(unittest.TestCase):
    """**假协议层必须和真协议层同签名**（I6 的回归锁）。

    2026-08 的真事故：`FakeClient.publish_item(self, account_id, payload)` 顺着
    服务层的错误调用写，21 个用例全绿，真机上第一次发布就
    `publish_item() takes 2 positional arguments but 3 were given`（502）。
    假对象比真的宽松 → 测试挡不住真 bug。这里逐方法对拍参数名与默认值。
    """

    #: 服务层会真的去调的协议层方法（不是全量 API，只锁会被误写的这些）
    CHECKED = ('list_items', 'publish_item', 'edit_item', 'downshelf', 'reshelf', 'set_status')

    def test_fake_client_signatures_match_real(self):
        for name in self.CHECKED:
            with self.subTest(method=name):
                real = inspect.signature(getattr(RealXianyuClient, name))
                fake = inspect.signature(getattr(FakeClient, name))
                self.assertEqual(
                    [(p.name, p.default, p.kind) for p in fake.parameters.values()],
                    [(p.name, p.default, p.kind) for p in real.parameters.values()],
                    f'FakeClient.{name} 的签名和真类不一致：{fake} != {real}',
                )

    def test_real_protocol_layer_exposes_every_method_service_calls(self):
        """服务层调的每个方法，真类都得有 —— 否则就是 503 xianyu_not_ready 假通。"""
        for name in self.CHECKED:
            with self.subTest(method=name):
                self.assertTrue(hasattr(RealXianyuClient, name),
                                f'XianyuClient 缺少服务层要调的方法 {name}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
