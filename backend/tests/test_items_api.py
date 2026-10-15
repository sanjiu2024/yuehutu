"""商品 HTTP 路由契约测试（`backend/app/api/items.py`）。

**为什么需要**：
1. `GET /api/items/{account_id}`（按账号拉商品列表）原先**不存在**，请求会被
   `GET /api/items/{item_id}` 吞掉 —— 前端按账号拉列表拿到的是 404
   「商品不存在」，看起来像「商品拉不过来」，其实是路由缺失。这里锁死它与
   `GET /api/items/{account_id}/{item_id}` 能各走各的。
2. 单条编辑 `POST /api/items/{account_id}/{item_id}/edit` 返回的是
   `{item, synced, updated_fields, warning?}`，**不是**批量形状
   （`{total, ok, warnings, failed}`）。前端曾经把它当批量结果读，于是 warning
   被吞、弹「没有可处理的商品（共 0 个）」。这里锁住单条形状。
3. 写操作成功后必须发 `items.updated` 事件（前端商品页靠它静默重拉）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_items_api.py"

**不打真实闲鱼平台、不调真实 LLM**：协议层用假模块注入。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core import db, events, security  # noqa: E402
from backend.app.services import common as common_module  # noqa: E402
from backend.app.xianyu import item_publish as real_publish_proto  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'api_acc'
OTHER = 'api_other'


class FakeClient:
    """假协议层：只记调用、不联网。签名与真 `XianyuClient` 逐字一致。"""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.calls: List[tuple] = []
        self.items: List[Dict[str, Any]] = []
        #: item_id -> 详情响应（`get_item` 用，补真库存走这条路）
        self.details: Dict[str, Dict[str, Any]] = {}

    async def get_item(self, item_id: str) -> Dict[str, Any]:
        self.calls.append(('get_item', item_id))
        return self.details.get(item_id) or {'id': item_id, 'stock': None}

    async def list_items(self, page_number: int = 1, page_size: int = 20,
                         max_pages: int = 5) -> Dict[str, Any]:
        self.calls.append(('list_items', page_number, page_size, max_pages))
        return {'items': list(self.items), 'total': len(self.items), 'next_page': 0}

    async def publish_item(self, payload: dict) -> Dict[str, Any]:
        self.calls.append(('publish_item', payload))
        return {'item_id': 'NEW1', 'itemId': 'NEW1', 'raw': {'itemId': 'NEW1'}}

    async def publish_precheck(self, payload: dict) -> Dict[str, Any]:
        self.calls.append(('publish_precheck', payload))
        return {
            'ready': True,
            'steps': [
                {'step': 'images', 'ok': True, 'detail': '1 张图片已准备好'},
                {'step': 'bad_words', 'ok': True, 'detail': None},
                {'step': 'category', 'ok': True, 'detail': '推荐分类：其他闲置，分类路径 1 条'},
            ],
            'warnings': [],
            'payload': {'publishScene': 'mainPublish'},
            'category': {'ok': True, 'cards': 1},
        }

    async def edit_item(self, item_id: str, payload: dict) -> Dict[str, Any]:
        self.calls.append(('edit_item', item_id, payload))
        return {'itemId': item_id, 'success': True}

    async def downshelf(self, item_id: str) -> Dict[str, Any]:
        self.calls.append(('downshelf', item_id))
        return {'success': True}

    async def reshelf(self, item_id: str) -> Dict[str, Any]:
        self.calls.append(('reshelf', item_id))
        return {'success': True}

    async def set_status(self, item_id: str, status: str) -> Dict[str, Any]:
        self.calls.append(('set_status', item_id, status))
        return {'success': True}


def install_fake_protocol(client: FakeClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    module.XianyuClient = FakeClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def fake_item(item_id: str, title: str = '测试商品', price: float = 100.0,
              status: str = 'on_sale', stock: Any = 1) -> Dict[str, Any]:
    return {'item_id': item_id, 'title': title, 'price': price, 'stock': stock,
            'status': status}


def seed(item_id: str, account: str = ACC, price: float = 100.0,
         status: str = 'on_sale', stock: int = 1) -> None:
    db.execute(
        'INSERT INTO items(account_id, item_id, title, price, stock, status) VALUES(?,?,?,?,?,?)',
        (account, item_id, f'商品 {item_id}', price, stock, status),
    )


def updates(limit: int = 50) -> List[Dict[str, Any]]:
    """最近发出的 `items.updated` 事件 payload。"""
    return [e['payload'] for e in events.recent(limit) if e['topic'] == 'items.updated']


class ItemsApiTest(unittest.TestCase):
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
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM operation_logs')
        # 事件总线是进程级 deque，用例之间会互相看见对方发过的事件
        events._recent.clear()
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('itemsapi','x',1)"))
        self.token = security.issue_token(self.user_id)
        self.headers = {'Authorization': 'Bearer ' + self.token}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- I7：按账号拉列表的路由 ------------------------------------------

    def test_get_items_by_account_route_exists(self):
        """缺这条路由时，请求会被 `GET /{item_id}` 吞成 404「商品不存在」。"""
        seed('A1', account=ACC, price=11.0)
        seed('A2', account=ACC, price=22.0)
        seed('B1', account=OTHER, price=33.0)

        resp = self.client.get(f'/api/items/{ACC}', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], resp.text)
        data = body['data']
        self.assertIn('items', data)
        self.assertTrue(data.get('items'), resp.text)
        self.assertEqual(sorted(i['item_id'] for i in data['items']), ['A1', 'A2'])
        self.assertEqual(data['total'], 2)

    def test_get_items_unknown_account_is_empty_not_404(self):
        """账号没有商品（哪怕账号不存在）也该是空列表，不该报「商品不存在」。"""
        resp = self.client.get('/api/items/no_such_account', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], resp.text)
        self.assertEqual(body['data']['items'], [])

    def test_get_items_by_account_filters_status(self):
        seed('A1', account=ACC, status='on_sale')
        seed('A2', account=ACC, status='off_shelf')
        resp = self.client.get(f'/api/items/{ACC}?status=off_shelf', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([i['item_id'] for i in resp.json()['data']['items']], ['A2'])

    def test_status_buckets_counts_and_sort_over_http(self):
        """前端下拉框发的 `onsale` / `offline` + 表头点击发的 `sort` / `order`，真跑一遍。

        **真机背景（2026-10-08）**：账号 `2217970177348` 有 45 件 `on_sale` 商品，
        `?status=onsale` 回的是 `total=0`（原样透传成 `WHERE status = 'onsale'`）；
        账号 `3998190171` 的 39 件商品全是 `sold`，其中 34 件库存未知、2 件库存 0。
        用户要的是「售罄的商品应该在下架分区里面」。
        """
        seed('ON', account=ACC, price=30.0, status='on_sale', stock=5)
        seed('UNKNOWN', account=ACC, price=20.0, status='on_sale', stock=-1)
        seed('ZERO', account=ACC, price=10.0, status='on_sale', stock=0)
        seed('SOLD', account=ACC, price=40.0, status='sold', stock=1)

        def payload(query: str) -> Dict[str, Any]:
            resp = self.client.get('/api/items?' + query, headers=self.headers)
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertTrue(body['success'], resp.text)
            return body['data']

        onsale = payload('status=onsale')
        # 库存未知算在售，售罄（库存 0）不算
        self.assertEqual(sorted(i['item_id'] for i in onsale['items']), ['ON', 'UNKNOWN'])
        self.assertEqual({i['item_id'] for i in payload('status=offline')['items']}, {'ZERO', 'SOLD'})
        self.assertEqual([i['item_id'] for i in payload('status=sold')['items']], ['SOLD'])
        # counts 不跟着当前分区走：选着「在售」也要能看见另外两个分区有多少件
        self.assertEqual(onsale['counts'], {'all': 4, 'onsale': 2, 'offline': 2, 'sold': 1})

        asc = payload(f'account_id={ACC}&sort=price&order=asc')
        self.assertEqual([i['item_id'] for i in asc['items']], ['ZERO', 'UNKNOWN', 'ON', 'SOLD'])
        # 库存未知（-1 哨兵）不管升序降序都沉底
        self.assertEqual([i['item_id'] for i in payload(f'account_id={ACC}&sort=stock&order=asc')['items']],
                         ['ZERO', 'SOLD', 'ON', 'UNKNOWN'])
        self.assertEqual([i['item_id'] for i in payload(f'account_id={ACC}&sort=stock&order=desc')['items']],
                         ['ON', 'SOLD', 'ZERO', 'UNKNOWN'])
        # 认不出的列名落回默认排序：不报错、也不把值拼进 SQL
        bogus = payload('sort=price%3B%20DROP%20TABLE%20items&order=x')
        self.assertEqual(bogus['total'], 4)
        self.assertEqual(len(bogus['items']), 4)
        self.assertTrue(db.query_one('SELECT COUNT(*) AS n FROM items')['n'] == 4)

    def test_get_single_item_still_works(self):
        """补了新路由不能把 `/{account_id}/{item_id}` 撞坏。"""
        seed('A1', account=ACC)
        resp = self.client.get(f'/api/items/{ACC}/A1', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['item_id'], 'A1')

    def test_get_single_item_cross_account_is_404(self):
        seed('A1', account=ACC)
        resp = self.client.get(f'/api/items/{OTHER}/A1', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertFalse(resp.json()['success'])

    # -- 删除本地记录（商品页「删除」按钮的真实后端） ----------------------

    def test_delete_item_removes_local_record_and_publishes(self):
        seed('A1', account=ACC)
        resp = self.client.delete(f'/api/items/{ACC}/A1', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['deleted'])
        self.assertIsNone(db.query_one('SELECT id FROM items WHERE item_id = ?', ('A1',)))
        # 列表接口里也不该再出现
        listed = self.client.get(f'/api/items/{ACC}', headers=self.headers).json()['data']['items']
        self.assertEqual([i['item_id'] for i in listed], [])
        self.assertTrue(any(u.get('action') == 'delete' for u in updates()), updates())

    def test_delete_item_cross_account_is_404_and_keeps_row(self):
        """路径里的 account_id 必须真的起作用：拿别的账号删不到，也不许删掉。"""
        seed('A1', account=ACC)
        resp = self.client.delete(f'/api/items/{OTHER}/A1', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertIsNotNone(db.query_one('SELECT id FROM items WHERE item_id = ?', ('A1',)))

    def test_delete_item_unknown_is_404(self):
        resp = self.client.delete(f'/api/items/{ACC}/nope', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertFalse(resp.json()['success'])

    # -- I5：单条编辑的响应形状 ------------------------------------------

    def test_single_edit_not_pushed_reports_warning(self):
        seed('A1', account=ACC, price=100.0)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'price': 9.9}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        # 单条形状：不是批量的 total/ok/warnings/failed
        self.assertIn('item', data)
        self.assertIn('synced', data)
        self.assertFalse(data['synced'])
        self.assertTrue(data.get('warning'), resp.text)
        self.assertEqual(data['item']['price'], 9.9)

    def test_single_edit_with_protocol_is_synced(self):
        seed('A1', account=ACC, price=100.0)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'price': 9.9}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['synced'], resp.text)
        self.assertNotIn('warning', data)
        self.assertEqual(fake.calls, [('edit_item', 'A1', {'price': 9.9})])

    # -- task-26：编辑改图 / 假绿（REST 层口径） ---------------------------

    # -- task-30：已卖出商品的编辑闸门（REST 层） -------------------------

    def test_single_edit_sold_item_is_not_pushed(self):
        """已卖出商品走 REST 编辑：不推平台、synced=false、warning 说明原因。"""
        seed('A1', account=ACC, status='sold')
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'price': 9.9}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertFalse(data['synced'], resp.text)
        self.assertIn('已卖出', data['warning'])
        self.assertEqual(data['local_only_fields'], ['price'])
        self.assertEqual(fake.calls, [])
        self.assertEqual(data['item']['price'], 9.9)
        self.assertEqual(data['item']['status'], 'sold')

    def test_single_edit_images_reaches_protocol_as_list(self):
        """改图走 REST：服务层交出去的 `images` 必须是列表，库里是 URL 字符串数组。"""
        seed('A1', account=ACC)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'images': ['https://img.alicdn.com/a.png']},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['synced'], resp.text)
        self.assertNotIn('warning', data)
        self.assertEqual(fake.calls,
                         [('edit_item', 'A1', {'images': ['https://img.alicdn.com/a.png']})])
        self.assertEqual(data['item']['images'], ['https://img.alicdn.com/a.png'])

    def test_single_edit_mixed_local_only_reports_not_synced(self):
        """改标题 + ai_enabled：只有标题推得出去 -> synced 必须 False（修假绿）。"""
        seed('A1', account=ACC)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'title': '新标题', 'ai_enabled': False},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertFalse(data['synced'], resp.text)
        self.assertIn('仅本地生效', data['warning'])
        self.assertIn('ai_enabled', data['warning'])
        self.assertEqual(data['local_only_fields'], ['ai_enabled'])
        self.assertEqual(fake.calls, [('edit_item', 'A1', {'title': '新标题'})])
        self.assertEqual(data['item']['title'], '新标题')

    def test_single_edit_empty_images_is_noop(self):
        """`images: []` = 不改图：200、noop、不推协议层、不报错。"""
        seed('A1', account=ACC)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'images': []}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['noop'], resp.text)
        self.assertEqual(data['updated_fields'], [])
        self.assertEqual(fake.calls, [])
        self.assertEqual(data['item']['images'], [])

    def test_single_edit_unknown_field_is_400(self):
        seed('A1', account=ACC)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, '不存在的字段': 1}, headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'invalid_fields')

    def test_single_edit_stock_delta_is_pushed(self):
        """库存增减：7 + 3 = 10，并且真的推给了协议层（用户第 5 轮：「怎么没有修改数量」）。"""
        seed('A1', account=ACC, stock=7)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'stock_delta': 3}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['synced'], resp.text)
        self.assertEqual(data['item']['stock'], 10)
        self.assertEqual([c for c in fake.calls if c[0] == 'edit_item'][0][2]['stock'], 10)

    def test_single_edit_unknown_stock_delta_is_400(self):
        """未知库存（-1）不许加减：会算出编造的数字。"""
        seed('A1', account=ACC, stock=-1)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'stock_delta': 3}, headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'unknown_stock')
        self.assertIn('库存未知', body['message'])
        self.assertEqual(db.query_one("SELECT stock FROM items WHERE item_id = 'A1'")['stock'], -1)

    def test_single_edit_stock_set_on_unknown_is_allowed(self):
        seed('A1', account=ACC, stock=-1)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'stock_set': 66}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['item']['stock'], 66)

    def test_batch_edit_stock_delta_skips_unknown(self):
        """批量：已知库存的被改，未知库存那条进 failed 且明细带原因，恒等式仍成立。"""
        seed('A1', account=ACC, stock=5)
        seed('A2', account=ACC, stock=-1)
        resp = self.client.post(f'/api/items/{ACC}/batch-edit',
                                json={'confirm': True, 'item_ids': ['A1', 'A2'], 'patch': {'stock_delta': 10}},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['total'], 2)
        self.assertEqual(len(data['failed']), 1)
        self.assertEqual(data['failed'][0]['id'], 'A2')
        self.assertEqual(data['failed'][0]['code'], 'unknown_stock')
        self.assertEqual(data['total'], data['ok'] + data['local_only'] + data['noop'] + len(data['failed']))
        rows = {r['item_id']: r['stock'] for r in db.query('SELECT item_id, stock FROM items')}
        self.assertEqual(rows['A1'], 15)
        self.assertEqual(rows['A2'], -1)

    # -- 补丁字段非法时必须是 400/逐条失败，不是 500（独立审查 D4） ---------

    def test_single_edit_stock_delta_null_is_400(self):
        """`stock_delta: null` 是**前端真会发出来的**：输入框为空时
        `Math.max(0, NaN)` 得 NaN，`JSON.stringify(NaN)` 就是 `null`。

        旧行为：`int(float(None))` → TypeError → 500（用户看到「服务器错误」，
        完全不知道是哪个字段的问题）。
        """
        seed('A1', account=ACC, stock=5)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'stock_delta': None}, headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('stock_delta', body['message'])
        self.assertEqual(db.query_one("SELECT stock FROM items WHERE item_id = 'A1'")['stock'], 5,
                         '非法输入不许落库')

    def test_single_edit_stock_set_non_numeric_is_400(self):
        seed('A1', account=ACC, stock=5)
        for bad in ('abc', [], {}, True, 'nan', 'inf'):
            with self.subTest(bad=bad):
                resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                        json={'confirm': True, 'stock_set': bad}, headers=self.headers)
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()['code'], 'invalid_fields')

    def test_single_edit_stock_set_fraction_is_400(self):
        """半件库存没有意义：`2.5` 静默截成 `2` 也是编数字。"""
        seed('A1', account=ACC, stock=5)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'stock_set': 2.5}, headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')

    def test_batch_edit_stock_delta_null_fails_every_item_not_500(self):
        """批量里同样的 `null`：整批不能 500，必须逐条进 `failed` 且恒等式成立。"""
        seed('A1', account=ACC, stock=5)
        seed('A2', account=ACC, stock=6)
        resp = self.client.post(f'/api/items/{ACC}/batch-edit',
                                json={'confirm': True, 'item_ids': ['A1', 'A2'], 'patch': {'stock_delta': None}},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['ok'], 0)
        self.assertEqual(len(data['failed']), 2)
        self.assertEqual({f['id'] for f in data['failed']}, {'A1', 'A2'})
        self.assertTrue(all(f['code'] == 'invalid_fields' for f in data['failed']), data['failed'])
        self.assertEqual(data['total'],
                         data['ok'] + data['local_only'] + data['noop'] + len(data['failed']))
        rows = {r['item_id']: r['stock'] for r in db.query('SELECT item_id, stock FROM items')}
        self.assertEqual(rows, {'A1': 5, 'A2': 6})

    # -- 写操作后的 items.updated 事件 ------------------------------------

    def test_sync_route_emits_items_updated(self):
        fake = FakeClient(ACC)
        fake.items = [fake_item('S1', price=1.0), fake_item('S2', price=2.0)]
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/sync', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['count'], 2)
        # 真的落库了
        rows = db.query('SELECT item_id FROM items WHERE account_id = ? ORDER BY item_id', (ACC,))
        self.assertEqual([r['item_id'] for r in rows], ['S1', 'S2'])

        payloads = updates()
        self.assertTrue(payloads, '同步成功后必须发 items.updated')
        self.assertEqual(payloads[-1]['account_id'], ACC)
        self.assertEqual(sorted(payloads[-1]['item_ids']), ['S1', 'S2'])
        self.assertEqual(payloads[-1]['action'], 'sync')

    def test_sync_route_heals_stale_fake_stock(self):
        """REST 层自愈：库里旧代码写死的假 1 被同步清掉，并当次补成平台真值。

        用户的抱怨正是「库存显示不对」：那 45 件假 1 只靠重新同步是永远好不了的。
        """
        seed('H1', account=ACC)  # seed 写的就是旧代码那种假 1
        seed('H2', account=ACC)
        fake = FakeClient(ACC)
        fake.items = [fake_item('H1', stock=None), fake_item('H2', stock=None)]
        fake.details['H1'] = {'id': 'H1', 'stock': 66}  # H2 没有详情 -> 补不到
        install_fake_protocol(fake)

        resp = self.client.post(f'/api/items/{ACC}/sync', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['stock_refreshed'], 1)

        rows = {r['item_id']: dict(r) for r in
                db.query('SELECT * FROM items WHERE account_id = ?', (ACC,))}
        self.assertEqual(rows['H1']['stock'], 66)  # 假 1 -> -1 -> 真值 66（一次同步内）
        self.assertEqual(rows['H2']['stock'], -1)  # 补不到就是 -1，不编数
        # 前端拉列表时拿到的也是 -1 / 66，不再是「全是 1」
        listed = self.client.get(f'/api/items/{ACC}', headers=self.headers).json()['data']['items']
        listed_stock = {i['item_id']: i['stock'] for i in listed}
        self.assertEqual(listed_stock, {'H1': 66, 'H2': -1})

    def test_sync_route_not_ready_emits_nothing(self):
        """协议层没就绪：返回结构化 code，且**不许**发「已同步」事件。"""
        resp = self.client.post(f'/api/items/{ACC}/sync', headers=self.headers)
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()['code'], 'xianyu_not_ready')
        self.assertEqual(updates(), [])

    def test_publish_precheck_route_is_read_only(self):
        """预检路由：200 + 分步结果，且**不创建商品、不发 items.updated**。"""
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        # 预检先查账号是否存在（`services.trade._client`：不存在 → 404）⇒ 用例里先把账号放进库；
        # 用完立刻删掉，免得后面 `test_sync_route_not_ready_emits_nothing` 因为「库里有账号 +
        # Cookie」而走到真协议层去构造真客户端（那会打真平台）。
        db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
                   'VALUES(?,?,?,?)', (ACC, '预检账号', 1, 1))
        db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
                   'VALUES(?,?,?)', (ACC, 'cookie-ok', 'active'))
        try:
            resp = self.client.post(f'/api/items/{ACC}/publish/precheck',
                                    json={'title': '新商品', 'price': 10,
                                          'images': ['https://img/a.jpg']},
                                    headers=self.headers)
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()['data']
            self.assertTrue(data['ready'])
            self.assertEqual([s['step'] for s in data['steps']],
                             ['images', 'bad_words', 'category'])
            self.assertIn('不会注册分类路径', data['note'])
            self.assertEqual([c[0] for c in fake.calls], ['publish_precheck'])
            self.assertEqual(updates(), [])
            self.assertEqual(db.query('SELECT * FROM items'), [])
        finally:
            db.execute('DELETE FROM account_credentials WHERE account_id = ?', (ACC,))
            db.execute('DELETE FROM accounts WHERE id = ?', (ACC,))

    def test_publish_precheck_route_unknown_account_is_404(self):
        """账号 id 打错 → 404 `not_found`（不是 503「后端未就绪」）。"""
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post('/api/items/nobody-here/publish/precheck',
                                json={'title': '新商品', 'price': 10,
                                      'images': ['https://img/a.jpg']},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')

    def test_publish_precheck_route_validates_fields(self):
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/publish/precheck',
                                json={'price': 10}, headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')

    def test_publish_precheck_route_needs_auth(self):
        resp = self.client.post(f'/api/items/{ACC}/publish/precheck',
                                json={'title': 'x', 'images': ['https://img/a.jpg']})
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_publish_route_emits_items_updated(self):
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/publish',
                                json={'confirm': True, 'title': '新商品', 'price': 10}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        payloads = updates()
        self.assertEqual(payloads[-1]['action'], 'publish')
        self.assertEqual(payloads[-1]['item_ids'], ['NEW1'])

    def test_edit_route_emits_items_updated(self):
        seed('A1', account=ACC)
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'confirm': True, 'price': 1.0}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        payloads = updates()
        self.assertEqual(payloads[-1]['action'], 'edit')
        self.assertEqual(payloads[-1]['item_ids'], ['A1'])

    def test_delist_route_emits_items_updated(self):
        seed('A1', account=ACC, status='on_sale')
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/A1/delist',
                                json={'confirm': True}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['synced'], resp.text)
        payloads = updates()
        self.assertEqual(payloads[-1]['action'], 'status')
        self.assertEqual(payloads[-1]['item_ids'], ['A1'])

    def test_batch_edit_route_emits_items_updated(self):
        seed('A1', account=ACC, price=100.0)
        seed('A2', account=ACC, price=100.0)
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/batch-edit',
                                json={'confirm': True, 'item_ids': ['A1', 'A2'], 'patch': {'price_set': 8.8}},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        payloads = updates()
        self.assertEqual(payloads[-1]['action'], 'batch_edit')
        self.assertEqual(sorted(payloads[-1]['item_ids']), ['A1', 'A2'])

    def test_event_publish_failure_does_not_break_request(self):
        """发事件失败不能把写操作一起弄挂（事件是旁路，主流程必须成功）。"""
        seed('A1', account=ACC, price=100.0)
        install_fake_protocol(FakeClient(ACC))
        original = events.publish

        def boom(topic, payload=None):
            raise RuntimeError('事件总线炸了')

        events.publish = boom  # type: ignore[assignment]
        try:
            resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                    json={'confirm': True, 'price': 5.0}, headers=self.headers)
        finally:
            events.publish = original  # type: ignore[assignment]
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['synced'], resp.text)
        row = db.query_one('SELECT price FROM items WHERE account_id = ? AND item_id = ?',
                           (ACC, 'A1'))
        self.assertEqual(float(row['price']), 5.0)


class ItemWriteConfirmGateApiTest(ItemsApiTest):
    """第 52 批：老一代商品写路由的确认闸门（HTTP 面）。

    路由层**不做**判断（`confirm=body.get('confirm')` 原样下传），判断权只在服务层 ——
    所以这里要断言的是「HTTP 400 + `confirmation_required` + 平台零调用 + 本地没改」。
    """

    #: 五个写路由（方法、路径、请求体）
    WRITE_ROUTES = (
        ('POST', '/api/items/{acc}/A1/edit', {'price': 1.0}),
        ('POST', '/api/items/{acc}/batch-edit', {'item_ids': ['A1'], 'patch': {'price_set': 1.0}}),
        ('POST', '/api/items/{acc}/publish', {'title': 'x', 'price': 1.0}),
        ('POST', '/api/items/{acc}/A1/delist', {}),
        ('POST', '/api/items/{acc}/A1/relist', {}),
    )

    FALSE_LIKE = ('false', 'no', 0, '', None)

    def test_write_routes_require_confirm(self):
        seed('A1', account=ACC, price=100.0)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        for method, path, body in self.WRITE_ROUTES:
            with self.subTest(path=path):
                resp = self.client.request(method, path.format(acc=ACC), json=body,
                                           headers=self.headers)
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()['code'], 'confirmation_required')
        # 闸门在最前：一条平台调用都没发出去，价格也没动
        self.assertEqual(fake.calls, [])
        self.assertEqual(float(db.query_one(
            'SELECT price FROM items WHERE account_id = ? AND item_id = ?',
            (ACC, 'A1'))['price']), 100.0)

    def test_false_like_confirm_is_rejected_over_http(self):
        seed('A1', account=ACC, price=100.0)
        for value in self.FALSE_LIKE:
            with self.subTest(confirm=value):
                resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                        json={'confirm': value, 'price': 1.0},
                                        headers=self.headers)
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()['code'], 'confirmation_required')


# --------------------------------------------------------------------------- 发布数据源（第 60 批）

#: 发布数据源路由用到的 5 个协议函数名（`services/items.py` 里的 `*_CALL` 常量）
PUBLISH_DATA_CALLS = (
    'fetch_categories', 'fetch_brands', 'fetch_models',
    'fetch_category_properties', 'fetch_search_categories',
)


def seed_account(account_id: str = ACC, cookie: str = 'unb=1; _m_h5_tk=abc_123',
                 login_state: str = 'ok') -> None:
    """建账号 + 凭据（发布数据源路由的前置检查要看这两张表）。"""
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled) VALUES(?,?,1)',
               (account_id, account_id))
    db.execute(
        'INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
        'VALUES(?,?,?)', (account_id, cookie, login_state))


class PublishDataFakeClient:
    """假客户端：只记 mtop 调用，按 api 名回**平台形状**的响应。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, Dict[str, Any]] = {
            'mtop.alibaba.idle.seller.platform.item.publish.categories': {
                'code': 'success',
                'data': {'selectIdleCategories': [
                    {'categoryId': 100, 'categoryName': '手机', 'categoryLevel': 1,
                     'leaf': False,
                     'childrenCategoryDOs': [
                         {'categoryId': 101, 'categoryName': '智能手机', 'categoryLevel': 2,
                          'leaf': True}]},
                ]},
            },
            'mtop.alibaba.idle.seller.platform.item.publish.brands': {
                'code': 'success',
                'data': {'selectIdleBrandValues': [{'valueId': 9, 'valueName': 'Apple'}]},
            },
            'mtop.alibaba.idle.seller.platform.item.publish.models': {
                'code': 'success',
                'data': {'selectIdleModelValues': [{'valueId': 7, 'valueName': 'iPhone 15'}]},
            },
            'mtop.taobao.idle.seller.platform.item.pc.category.property.query': {
                'code': 'success',
                'data': {'fineness': [
                    {'propertyId': 20000, 'propertyName': '成色',
                     'valueList': [{'valueId': '95', 'valueName': '95新'}]}]},
            },
            'mtop.taobao.idle.seller.platform.item.pc.search.categories': {
                'code': 'success',
                'data': {'selectIdleCategories': [{'categoryId': 5, 'categoryName': '耳机'}]},
            },
        }

    async def mtop(self, api: str, data: Any = None, v: str = '1.0', **kwargs: Any):
        self.calls.append({'api': api, 'data': data, 'v': v, 'kwargs': kwargs})
        return {'success': True, 'data': self.responses.get(api, {})}


def install_fake_publish_client(client: PublishDataFakeClient) -> types.ModuleType:
    """把假 \`client\` 协议模块塞进 sys.modules：\`common.get_client\` 优先用它的 \`get_client\` 工厂。"""
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    module.XianyuClient = PublishDataFakeClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return module


def install_fake_publish_protocol(client: PublishDataFakeClient) -> types.ModuleType:
    """把假 `item_publish` 协议模块塞进 sys.modules（`common.protocol_module` 懒导入它）。

    **真协议层的 5 个 fetch_* 函数不在这里重写**：服务层只通过
    `common.call_method(_BoundProtocol(module, client), (name,), client, ...)` 找函数名，
    所以假模块只要同名可调用即可 —— 请求形状由
    `backend/tests/test_xianyu_item_publish.py` 用 MockTransport 单独钉死。
    """
    module = types.ModuleType('backend.app.xianyu.item_publish')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    for name in PUBLISH_DATA_CALLS:
        setattr(module, name, getattr(real_publish_proto, name))
    sys.modules['backend.app.xianyu.item_publish'] = module
    return module


def remove_fake_publish_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.item_publish', None)


class ItemPublishDataApiTest(ItemsApiTest):
    """发布数据源 5 条只读 GET 路由（类目 / 品牌 / 型号 / 属性 / 类目搜索）。"""

    def setUp(self):
        super().setUp()
        self.fake = PublishDataFakeClient()
        # 两件都要装：假 client 模块让 \`common.get_client\` 不去建真客户端（否则测试会真联网），
        # 假 item_publish 模块让 \`common.protocol_module('item_publish')\` 拿到它。
        install_fake_publish_client(self.fake)
        install_fake_publish_protocol(self.fake)

    def tearDown(self):
        remove_fake_publish_protocol()
        remove_fake_protocol()
        # **必须清干净**：accounts / account_credentials 是跨用例共享的表，
        # 留下一个带 Cookie 的账号会让同文件里「协议层没就绪 → 503」这类用例
        # 改成真建客户端、真联网（实测就是这么坏的）。
        db.execute('DELETE FROM account_credentials WHERE account_id = ?', (ACC,))
        db.execute('DELETE FROM accounts WHERE id = ?', (ACC,))
        super().tearDown()

    def test_routes_are_registered_before_item_id(self):
        """5 条路径必须真注册（缺了会被 `GET /{account_id}/{item_id}` 吞成 404「商品不存在」）。"""
        # 走 openapi：`app.routes` 里是 `_IncludedRouter`（子路由被包了一层，没有 `.path`）
        paths = set(self.client.app.openapi()['paths'])
        for path in ('/api/items/{account_id}/publish/categories',
                     '/api/items/{account_id}/publish/brands',
                     '/api/items/{account_id}/publish/models',
                     '/api/items/{account_id}/publish/category-properties',
                     '/api/items/{account_id}/publish/search-categories'):
            self.assertIn(path, paths, path)

    def test_categories_route(self):
        seed_account()
        resp = self.client.get(f'/api/items/{ACC}/publish/categories', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], resp.text)
        data = body['data']
        self.assertEqual(data['kind'], 'categories')
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['platform_code'], 'success')
        self.assertEqual(data['source'], 'platform')
        option = data['options'][0]
        self.assertEqual(option['category_id'], '100')
        self.assertEqual(option['category_name'], '手机')
        self.assertEqual(option['children'][0]['category_id'], '101')
        self.assertTrue(option['children'][0]['leaf'])

    def test_brands_route_passes_category_id(self):
        seed_account()
        resp = self.client.get(f'/api/items/{ACC}/publish/brands?category_id=100',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['kind'], 'brands')
        self.assertEqual(data['options'], [{'value': '9', 'label': 'Apple'}])

    def test_models_route_needs_both_ids(self):
        seed_account()
        # 缺 brand_id → 400，且**一次平台请求都不发**
        resp = self.client.get(f'/api/items/{ACC}/publish/models?category_id=100',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])
        resp = self.client.get(f'/api/items/{ACC}/publish/models?category_id=100&brand_id=9',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['options'], [{'value': '7', 'label': 'iPhone 15'}])

    def test_category_properties_route(self):
        seed_account()
        resp = self.client.get(f'/api/items/{ACC}/publish/category-properties?category_id=100',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['kind'], 'category_properties')
        self.assertEqual(data['options'][0]['property_name'], '成色')
        self.assertEqual(data['options'][0]['values'], [{'value': '95', 'label': '95新'}])

    def test_search_categories_route(self):
        seed_account()
        resp = self.client.get(f'/api/items/{ACC}/publish/search-categories',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['kind'], 'search_categories')
        self.assertEqual(data['options'][0]['category_name'], '耳机')

    def test_empty_options_are_200_not_failure(self):
        """平台回了空候选值 → 200 + 空列表（如实说「平台没给」），**不是**失败、更不是编数据。"""
        seed_account()
        self.fake.responses[
            'mtop.alibaba.idle.seller.platform.item.publish.categories'] = {
                'code': 'success', 'data': {'selectIdleCategories': []}}
        resp = self.client.get(f'/api/items/{ACC}/publish/categories', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['options'], [])
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['platform_code'], 'success')

    def test_platform_permission_code_is_surfaced(self):
        """平台回无权限 → 仍 200（链路是通的），但 `platform_code` 必须原样带出来。"""
        seed_account()
        self.fake.responses[
            'mtop.alibaba.idle.seller.platform.item.publish.categories'] = {
                'code': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED', 'msg': '无权限访问', 'data': None}
        resp = self.client.get(f'/api/items/{ACC}/publish/categories', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['platform_code'], 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(data['platform_message'], '无权限访问')
        self.assertEqual(data['options'], [])

    def test_unknown_account_is_404_and_no_platform_call(self):
        resp = self.client.get('/api/items/no_such_acc/publish/categories',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_and_no_platform_call(self):
        """账号在、但没有 Cookie → 409 `verification_required`，且一次平台都不打。"""
        seed_account(cookie='')
        resp = self.client.get(f'/api/items/{ACC}/publish/categories', headers=self.headers)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_protocol_module_missing_is_503_not_empty_options(self):
        """协议层没装 → 503 `xianyu_not_ready`，**绝不能**当成「这个类目下没有候选值」。"""
        seed_account()
        # 真模块在仓库里是存在的，所以「没装」只能用 patch 模拟：让
        # common.protocol_module 对 'item_publish' 返回 None（其余名字照旧，
        # 否则 common.get_client 也拿不到 client）。
        real = common_module.protocol_module

        def _missing(name: str):
            return None if name == 'item_publish' else real(name)

        common_module.protocol_module = _missing
        try:
            resp = self.client.get(f'/api/items/{ACC}/publish/categories',
                                   headers=self.headers)
        finally:
            common_module.protocol_module = real
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertFalse(resp.json()['success'], resp.text)
        self.assertEqual(resp.json()['code'], 'xianyu_not_ready')

    def test_route_is_get_only(self):
        """只读数据源不许有写方法：POST 一律 405。"""
        seed_account()
        for suffix in ('categories', 'brands', 'models', 'category-properties',
                       'search-categories'):
            with self.subTest(suffix=suffix):
                resp = self.client.post(f'/api/items/{ACC}/publish/{suffix}',
                                        json={}, headers=self.headers)
                self.assertEqual(resp.status_code, 405, resp.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
