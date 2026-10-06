"""商品服务测试：增删改查、批量汇总、operation_logs 落库、协议层懒导入。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_services_items.py"

**不打真实闲鱼平台**：协议层用 sys.modules 注入假模块。
"""

from __future__ import annotations

import inspect
import json
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
from backend.app.xianyu.client import RiskControlError as RealRiskControlError  # noqa: E402
from backend.app.xianyu.client import SessionExpiredError as RealSessionExpiredError  # noqa: E402
# 真协议层的图片入参转换（公开函数）：用来验证服务层交出去的形状**真能被它接受**，
# 而不是只对着假客户端自说自话。
from backend.app.xianyu.client import images_to_image_infos  # noqa: E402


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
        #: item_id -> 详情响应（`get_item` 用）；值为异常实例时当作调用抛错
        self.details = {}
        #: 用例可覆盖列表响应（例如模拟「列表不带库存」的真机情况）
        self.list_result = None

    def list_items(self, page_number: int = 1, page_size: int = 20, max_pages: int = 25):
        self.calls.append(('list_items', page_number, page_size, max_pages))
        if self.list_result is not None:
            return self.list_result
        return {
            'items': [
                {'item_id': 'A1', 'title': 'iPhone 13', 'price': 2500, 'stock': 1, 'status': 'on_sale'},
                {'item_id': 'A2', 'title': 'AirPods', 'price': 400, 'stock': 2, 'status': 'on_sale'},
            ],
            'total': 2,
        }

    def get_item(self, item_id: str):
        self.calls.append(('get_item', item_id))
        detail = self.details.get(item_id)
        if isinstance(detail, BaseException):
            raise detail
        if detail is None:
            return {'id': item_id, 'stock': None}
        return detail

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
    # 让 `items._is_risk_or_expired` 的 isinstance 分支走真类（假模块里没有这个异常时
    # 风控熔断那条路根本测不到）。
    module.RiskControlError = RealRiskControlError  # type: ignore[attr-defined]
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
        # 补库存的限速在用例里归零，否则每个用例白白等 0.6s×N
        self._orig_interval = items.STOCK_REFRESH_INTERVAL
        items.STOCK_REFRESH_INTERVAL = 0

    def tearDown(self):
        items.STOCK_REFRESH_INTERVAL = self._orig_interval

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

    # ------------------------------------------- 已卖出商品的编辑闸门（task-30）

    def test_update_sold_item_is_not_pushed(self):
        """已卖出的商品绝不能推编辑：平台会接受提交并把它**重新上架**。

        真机 1087247101297：详情 `itemStatusStr="卖掉了"`，一次 edit 提交后回帧
        `publishScene="pcMainPublish"`，随后同步 status 就从 sold 变回 on_sale。
        """
        install_fake_protocol(self.client)
        self.seed('I1', status='sold')
        result = items.update_item('I1', {'title': '改个标题'})
        self.assertEqual(self.client.calls, [])            # 一次协议调用都不能发
        self.assertFalse(result['synced'])
        self.assertIn('已卖出', result['warning'])
        self.assertEqual(result['local_only_fields'], ['title'])
        # 改动仍落本地，并且**状态不能被改成在售**
        self.assertEqual(items.get_item('I1')['title'], '改个标题')
        self.assertEqual(items.get_item('I1')['status'], 'sold')

    def test_update_on_sale_item_still_pushes(self):
        """对照：在售商品照旧推平台（闸门只针对 sold）。"""
        install_fake_protocol(self.client)
        self.seed('I1', status='on_sale')
        result = items.update_item('I1', {'title': '改个标题'})
        self.assertTrue(result['synced'])
        self.assertEqual(self.client.calls, [('edit_item', 'I1', {'title': '改个标题'})])

    def test_batch_update_sold_items_are_warnings_not_ok(self):
        """批量同一规则：sold 的商品进 warnings（不算 ok），也不发协议调用。"""
        install_fake_protocol(self.client)
        self.seed('I1', status='sold')
        self.seed('I2', status='on_sale')
        result = items.batch_update(['I1', 'I2'], {'price': 12}, account_id='acc1')
        self.assertEqual(result['ok'], 1)
        self.assertEqual(len(result['warnings']), 1)
        self.assertEqual(result['warnings'][0]['id'], 'I1')
        self.assertIn('已卖出', result['warnings'][0]['message'])
        self.assertEqual([c[1] for c in self.client.calls], ['I2'])

    # ------------------------------------------------- 改图：白名单 / 空值 / 假绿（task-26）

    def _db_images(self, item_id: str = 'I1'):
        row = db.query_one('SELECT images FROM items WHERE item_id = ?', (item_id,))
        return json.loads(row['images']) if row and row['images'] else []

    def test_update_images_strings_push_list_and_store_urls(self):
        """只传 images（URL 字符串）：推协议层的必须是**列表**，库里是 URL 字符串数组。

        服务层交 JSON 字符串给协议层的话，``images_to_image_infos`` 会把它当成
        「一张图」，报 `第 1 张图片不是 http/https URL`。
        """
        install_fake_protocol(self.client)
        self.seed('I1')
        result = items.update_item('I1', {'images': ['https://img.alicdn.com/a.png']})
        self.assertNotIn('success', result)
        self.assertTrue(result['synced'])
        self.assertNotIn('warning', result)
        self.assertEqual(self.client.calls,
                         [('edit_item', 'I1', {'images': ['https://img.alicdn.com/a.png']})])
        self.assertEqual(self._db_images(), ['https://img.alicdn.com/a.png'])
        self.assertEqual(result['item']['images'], ['https://img.alicdn.com/a.png'])

        # 真协议层能直接吃下服务层交出去的形状
        sent = self.client.calls[0][2]['images']
        self.assertIsInstance(sent, list)
        infos = images_to_image_infos(sent)
        self.assertEqual(infos[0]['url'], 'https://img.alicdn.com/a.png')
        self.assertEqual(infos[0]['major'], 'true')

    def test_update_images_objects_keep_size_for_protocol_but_store_urls(self):
        """新上传的图是 ``{url,width,height}``：尺寸要活着到协议层，库里只留 URL。"""
        install_fake_protocol(self.client)
        self.seed('I1')
        entry = {'url': 'https://img.alicdn.com/a.png', 'width': 1254, 'height': 1254}
        result = items.update_item('I1', {'images': [entry]})
        self.assertTrue(result['synced'])
        sent = self.client.calls[0][2]['images']
        self.assertEqual(sent, [entry])  # 推协议层：完整条目（含尺寸）
        infos = images_to_image_infos(sent)
        self.assertEqual((infos[0]['widthSize'], infos[0]['heightSize']), ('1254', '1254'))
        # 库里必须是字符串数组：对象进库前端 parseImages 渲染不出缩略图
        self.assertEqual(self._db_images(), ['https://img.alicdn.com/a.png'])
        self.assertEqual(result['item']['images'], ['https://img.alicdn.com/a.png'])

        # 混排（新对象 + 历史 URL）且 URL 重复 -> 顺序保留、去重
        result = items.update_item('I1', {'images': [
            entry, 'https://img.alicdn.com/b.png', 'https://img.alicdn.com/a.png',
        ]})
        self.assertEqual(self._db_images(),
                         ['https://img.alicdn.com/a.png', 'https://img.alicdn.com/b.png'])
        self.assertEqual(result['item']['images'],
                         ['https://img.alicdn.com/a.png', 'https://img.alicdn.com/b.png'])

    def test_update_empty_images_is_noop_not_error(self):
        """``images: []`` = 本次不改图：不报错、不推、不算 local_only、不写库。"""
        install_fake_protocol(self.client)
        self.seed('I1')
        db.execute("UPDATE items SET images = ? WHERE item_id = 'I1'",
                   (json.dumps(['https://img.alicdn.com/keep.png']),))

        result = items.update_item('I1', {'images': []})
        self.assertNotIn('success', result)          # 不是错误信封
        self.assertTrue(result['noop'])
        self.assertEqual(result['updated_fields'], [])
        self.assertNotIn('warning', result)
        self.assertFalse(result.get('local_only_fields'))
        self.assertEqual(self.client.calls, [])      # 协议层一次都没打
        self.assertEqual(self._db_images(), ['https://img.alicdn.com/keep.png'])
        logs = db.query("SELECT * FROM operation_logs WHERE action = 'update_item'")
        self.assertEqual(len(logs), 1)

    def test_update_images_without_protocol_is_local_only(self):
        """协议层没就绪：本地照存 URL（本地优先），但 synced 必须是 False。"""
        self.seed('I1')
        result = items.update_item('I1', {'images': ['https://img.alicdn.com/a.png']})
        self.assertFalse(result['synced'])
        self.assertIn('协议层', result['warning'])
        self.assertEqual(self._db_images(), ['https://img.alicdn.com/a.png'])

    def test_update_mixed_local_only_field_is_not_synced(self):
        """标题 + ai_enabled 混合：只推了标题，`synced` 必须是 False。

        旧实现 ``synced = warning is None`` -> True，用户看到「已修改并同步」，
        而 ``ai_enabled`` 只落在本地（假绿）。
        """
        install_fake_protocol(self.client)
        self.seed('I1', title='旧标题')
        result = items.update_item('I1', {'title': '新标题', 'ai_enabled': False})
        self.assertFalse(result['synced'])
        self.assertIn('ai_enabled', result['warning'])
        self.assertIn('仅本地生效', result['warning'])
        self.assertEqual(result['local_only_fields'], ['ai_enabled'])
        self.assertEqual(self.client.calls, [('edit_item', 'I1', {'title': '新标题'})])
        self.assertEqual(result['item']['title'], '新标题')
        self.assertFalse(result['item']['ai_enabled'])   # 本地确实改了

    def test_batch_update_images_pushes_every_item(self):
        install_fake_protocol(self.client)
        self.seed('I1')
        self.seed('I2')
        result = items.batch_update(['I1', 'I2'], {'images': ['https://img.alicdn.com/a.png']})
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['ok'], 2)
        self.assertEqual(result['local_only'], 0)
        self.assertEqual(result['noop'], 0)
        self.assertEqual(result['warnings'], [])
        self.assertEqual([c[0] for c in self.client.calls], ['edit_item', 'edit_item'])
        self.assertEqual(self.client.calls[0][2]['images'], ['https://img.alicdn.com/a.png'])
        self.assertEqual(self._db_images('I2'), ['https://img.alicdn.com/a.png'])

    def test_batch_update_mixed_local_only_is_not_ok(self):
        """批量路径同样不许假绿：标题能推、ai_enabled 不能 -> ok=0、local_only=2。"""
        install_fake_protocol(self.client)
        self.seed('I1')
        self.seed('I2')
        result = items.batch_update(['I1', 'I2'], {'title': '新标题', 'ai_enabled': False})
        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['local_only'], 2)
        self.assertEqual(result['noop'], 0)
        self.assertEqual(len(result['warnings']), 2)
        self.assertIn('仅本地生效', result['warnings'][0]['message'])
        self.assertEqual([c[0] for c in self.client.calls], ['edit_item', 'edit_item'])

    def test_batch_update_empty_images_is_noop_not_ok(self):
        """批量里 ``images: []`` 一行都没改 -> ok=0、noop=1（不能算成功）。"""
        install_fake_protocol(self.client)
        self.seed('I1')
        result = items.batch_update(['I1'], {'images': []})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['noop'], 1)
        self.assertEqual(result['local_only'], 0)
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['failed'], [])
        self.assertEqual(result['updated'], [])
        self.assertEqual(self.client.calls, [])

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

    # ------------------------------------------------- 库存：不拿 1 冒充（真机列表卡没库存字段）

    def _real_frame_list(self, *item_ids: str) -> dict:
        """真机列表卡的归一化结果：**没有库存字段**（stock=None），图片也在。"""
        return {'items': [
            {'item_id': item_id, 'title': f'商品{item_id}', 'price': 49.9,
             'stock': None, 'status': 'on_sale',
             'images': ['https://img.alicdn.com/a.png']}
            for item_id in item_ids
        ]}

    def test_sync_unknown_stock_overwrites_stale_fake_one(self):
        """**自愈**：旧代码硬编码写进库的假 1 必须被同步清成 -1，不能永久保留。

        否则用户库里那 45 件假 1 永远自愈不了：重新同步多少次 excluded 都是 -1 ->
        继续保留假 1；而 `_refresh_unknown_stock` 只挑 `stock < 0` 的行，假 1 连补详情
        队列都进不去。
        """
        self.seed('A1', stock=1)  # 旧代码留下的假库存
        db.execute("UPDATE items SET images = ?, description = ? WHERE item_id = 'A1'",
                   (json.dumps(['https://img.alicdn.com/kept.png']), '本地描述'))
        install_fake_protocol(self.client)
        # 真机列表：没有库存字段、没有图片、没有描述
        self.client.list_result = {'items': [
            {'item_id': 'A1', 'title': '商品A1', 'price': 49.9, 'stock': None, 'status': 'on_sale'},
            {'item_id': 'A2', 'title': '商品A2', 'price': 49.9, 'stock': None, 'status': 'on_sale'},
        ]}

        result = items.sync_items('acc1')
        self.assertEqual(result['count'], 2)
        rows = {r['item_id']: dict(r) for r in db.query('SELECT * FROM items')}
        self.assertEqual(rows['A1']['stock'], -1)  # 假 1 被清掉（不再是 1，也不是 7 那种旧值）
        self.assertEqual(rows['A2']['stock'], -1)  # 新入库：未知就是 -1，不编数
        # 「列表为空不覆盖本地」的规则不变：图片/描述仍是库里的
        self.assertEqual(json.loads(rows['A1']['images']), ['https://img.alicdn.com/kept.png'])
        self.assertEqual(rows['A1']['description'], '本地描述')

    def test_sync_self_heals_stale_one_to_real_stock_in_one_pass(self):
        """一次同步内完成自愈：假 1 -> -1 -> 补详情写成平台真值 66。"""
        self.seed('A1', stock=1)  # 假库存
        install_fake_protocol(self.client)
        self.client.list_result = {'items': [
            {'item_id': 'A1', 'title': '商品A1', 'price': 49.9, 'stock': None, 'status': 'on_sale'},
        ]}
        self.client.details['A1'] = {'id': 'A1', 'stock': 66}

        result = items.sync_items('acc1')
        self.assertEqual(result['stock_refreshed'], 1)
        row = dict(db.query_one("SELECT * FROM items WHERE item_id = 'A1'"))
        self.assertEqual(row['stock'], 66)
        self.assertIn(('get_item', 'A1'), self.client.calls)

    def test_sync_refreshes_a_45_item_account_in_one_pass(self):
        """上限必须覆盖真实账号规模：45 件全未知时一次同步全部补齐（limit=40 就不够）。"""
        self.assertEqual(items.STOCK_REFRESH_LIMIT, 60)
        item_ids = [f'S{i:02d}' for i in range(45)]
        for item_id in item_ids:
            self.seed(item_id, stock=-1)
        install_fake_protocol(self.client)
        self.client.list_result = self._real_frame_list(*item_ids)
        for item_id in item_ids:
            self.client.details[item_id] = {'id': item_id, 'stock': 66}

        result = items.sync_items('acc1')
        self.assertEqual(result['count'], 45)
        self.assertEqual(result['stock_refreshed'], 45)
        self.assertNotIn('stock_refresh_stopped', result)
        unknown = db.query('SELECT item_id FROM items WHERE stock < 0')
        self.assertEqual(unknown, [])
        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM items')['n'], 45)

    def test_sync_refreshes_real_stock_from_detail(self):
        """真库存在详情里（`data.itemDO.quantity`）-> 逐件补回来落库。"""
        install_fake_protocol(self.client)
        self.client.list_result = self._real_frame_list('A1')
        self.client.details['A1'] = {'id': 'A1', 'stock': 66,
                                     'images': ['https://img.alicdn.com/d.png'],
                                     'desc': '详情描述'}
        result = items.sync_items('acc1')
        self.assertEqual(result['stock_refreshed'], 1)
        self.assertNotIn('stock_refresh_stopped', result)
        row = dict(db.query_one("SELECT * FROM items WHERE item_id = 'A1'"))
        self.assertEqual(row['stock'], 66)
        self.assertEqual(json.loads(row['images']), ['https://img.alicdn.com/d.png'])
        self.assertEqual(row['description'], '详情描述')
        self.assertIn(('get_item', 'A1'), self.client.calls)

    def test_sync_stops_refresh_on_risk_control(self):
        """碰到风控**立刻停手**：后面的商品一件都不打，已有数据保持不动。"""
        install_fake_protocol(self.client)
        self.client.list_result = self._real_frame_list('A1', 'A2')
        self.client.details['A1'] = RealRiskControlError('FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦')

        result = items.sync_items('acc1')
        self.assertEqual(result['stock_refresh_stopped'], 'risk')
        self.assertEqual(result['stock_refreshed'], 0)
        self.assertEqual([c for c in self.client.calls if c[0] == 'get_item'],
                         [('get_item', 'A1')])  # A2 一次都没打
        rows = {r['item_id']: dict(r) for r in db.query('SELECT * FROM items')}
        self.assertEqual(rows['A1']['stock'], -1)
        self.assertEqual(rows['A2']['stock'], -1)

    def test_sync_stops_refresh_on_session_expired(self):
        """登录态过期同样立刻停手（不是风控，但继续打也没意义）。"""
        install_fake_protocol(self.client)
        self.client.list_result = self._real_frame_list('A1', 'A2')
        self.client.details['A1'] = RealSessionExpiredError('登录态已过期，需要重新登录')

        result = items.sync_items('acc1')
        self.assertEqual(result['stock_refresh_stopped'], 'session')
        self.assertEqual([c for c in self.client.calls if c[0] == 'get_item'],
                         [('get_item', 'A1')])

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
