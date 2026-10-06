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
         status: str = 'on_sale') -> None:
    db.execute(
        'INSERT INTO items(account_id, item_id, title, price, stock, status) VALUES(?,?,?,?,?,?)',
        (account, item_id, f'商品 {item_id}', price, 1, status),
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

    # -- I5：单条编辑的响应形状 ------------------------------------------

    def test_single_edit_not_pushed_reports_warning(self):
        seed('A1', account=ACC, price=100.0)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'price': 9.9}, headers=self.headers)
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
                                json={'price': 9.9}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['synced'], resp.text)
        self.assertNotIn('warning', data)
        self.assertEqual(fake.calls, [('edit_item', 'A1', {'price': 9.9})])

    # -- task-26：编辑改图 / 假绿（REST 层口径） ---------------------------

    def test_single_edit_images_reaches_protocol_as_list(self):
        """改图走 REST：服务层交出去的 `images` 必须是列表，库里是 URL 字符串数组。"""
        seed('A1', account=ACC)
        fake = FakeClient(ACC)
        install_fake_protocol(fake)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'images': ['https://img.alicdn.com/a.png']},
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
                                json={'title': '新标题', 'ai_enabled': False},
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
                                json={'images': []}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['noop'], resp.text)
        self.assertEqual(data['updated_fields'], [])
        self.assertEqual(fake.calls, [])
        self.assertEqual(data['item']['images'], [])

    def test_single_edit_unknown_field_is_400(self):
        seed('A1', account=ACC)
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'不存在的字段': 1}, headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'invalid_fields')

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

    def test_publish_route_emits_items_updated(self):
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/publish',
                                json={'title': '新商品', 'price': 10}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        payloads = updates()
        self.assertEqual(payloads[-1]['action'], 'publish')
        self.assertEqual(payloads[-1]['item_ids'], ['NEW1'])

    def test_edit_route_emits_items_updated(self):
        seed('A1', account=ACC)
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/A1/edit',
                                json={'price': 1.0}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        payloads = updates()
        self.assertEqual(payloads[-1]['action'], 'edit')
        self.assertEqual(payloads[-1]['item_ids'], ['A1'])

    def test_delist_route_emits_items_updated(self):
        seed('A1', account=ACC, status='on_sale')
        install_fake_protocol(FakeClient(ACC))
        resp = self.client.post(f'/api/items/{ACC}/A1/delist', headers=self.headers)
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
                                json={'item_ids': ['A1', 'A2'], 'patch': {'price_set': 8.8}},
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
                                    json={'price': 5.0}, headers=self.headers)
        finally:
            events.publish = original  # type: ignore[assignment]
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['synced'], resp.text)
        row = db.query_one('SELECT price FROM items WHERE account_id = ? AND item_id = ?',
                           (ACC, 'A1'))
        self.assertEqual(float(row['price']), 5.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
