"""商品数据接口契约测试（`backend/app/api/item_stats.py` + `services/item_stats.py`）。

**为什么要这一组**：这个接口的第一原则是「**本地真实能算的算准，平台拿不到的一律
`null`，禁止补 0 或假数据**」。所有能悄悄违反它的写法（顺手 `or 0`、缺平台数据时填
默认值、把「本地没有记录」当成「发生过 0 次」）都在这组用例里被钉住。另外两条：

1. `GET /api/items/{account_id}/stats` 与 `GET /api/items/{account_id}/{item_id}`
   路径形状一样，**谁先声明谁赢** —— 顺序错了就返回 404「商品不存在」，
   看起来像「这个账号没商品」，所以这里锁死顺序（lead 负责挂载）。
2. 平台接口失败**默认降级**（本地统计照常返回 + `warnings` 说清原因），
   只有显式 `?source=platform` 才升级成 502。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_item_stats_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import main as app_main  # noqa: E402
from backend.app.api import item_stats as item_stats_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import item_stats as item_stats_service  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'stats_acc'
OTHER = 'stats_other'
ITEM_A = 'itemA'
ITEM_B = 'itemB'
STATS_PATH = '/api/items/{account_id}/stats'


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeClient:
    """假协议层：**没有** `item_stats` —— 模拟「平台没提供商品数据接口」。"""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.calls: List[tuple] = []


class StatsClient(FakeClient):
    """假协议层：有 `item_stats`。`error` 非空时抛出（模拟平台调用失败）。"""

    def __init__(self, account_id: str, payload: Any = None,
                 error: Optional[BaseException] = None):
        super().__init__(account_id)
        self.payload = {'exposure': 1234, 'visits': 56, 'visitors': 12} if payload is None else payload
        self.error = error

    async def item_stats(self, item_id: Optional[str] = None) -> Any:
        self.calls.append(('item_stats', item_id))
        if self.error is not None:
            raise self.error
        return self.payload


class AccountLevelClient(FakeClient):
    """假协议层：**只有账号级**接口 —— 签名里没有 item_id，只能不带参数调。

    响应可能是账号级汇总（认不出归属 → 必须降级），也可能是按 item_id 分桶的整本账
    （能认出各商品自己那份 → 可用）。
    """

    def __init__(self, account_id: str, payload: Any = None):
        super().__init__(account_id)
        self.payload = {} if payload is None else payload

    async def item_stats(self) -> Any:
        self.calls.append(('item_stats',))
        return self.payload


class LabelledClient(FakeClient):
    """假协议层：按 item 查，但响应**自带属于别人**的 item_id（认领不一致）。"""

    def __init__(self, account_id: str, owner: str):
        super().__init__(account_id)
        self.owner = owner

    async def item_stats(self, item_id: Optional[str] = None) -> Any:
        self.calls.append(('item_stats', item_id))
        return {'item_id': self.owner, 'exposure': 999, 'visits': 888}


class MapByItemClient(FakeClient):
    """假协议层：按 item 查，但每次都回**整本账**（按 item_id 分桶）。"""

    async def item_stats(self, item_id: Optional[str] = None) -> Any:
        self.calls.append(('item_stats', item_id))
        return {'items': [
            {'item_id': ITEM_A, 'exposure': 11},
            {'item_id': ITEM_B, 'exposure': 22, 'visits': 7},
        ]}


def install_fake_protocol(client: FakeClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'ok') -> None:
    """账号 + 凭证。`cookie=''` 表示没登录过；`login_state='need_relogin'` 表示掉线。"""
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) VALUES(?,?,?,?)',
               (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def seed_item(item_id: str, account: str = ACC, title: str = '', price: Any = 100.0,
              status: str = 'on_sale', images: str = '[]') -> None:
    db.execute(
        'INSERT INTO items(account_id, item_id, title, price, stock, status, images) '
        'VALUES(?,?,?,?,?,?,?)',
        (account, item_id, title or f'商品 {item_id}', price, 1, status, images),
    )


def seed_conversation(conversation_id: int, item_id: str = ITEM_A, account: str = ACC,
                      peer: str = '') -> int:
    db.execute(
        'INSERT INTO conversations(id, account_id, peer_user_id, peer_nickname, item_id, mode) '
        'VALUES(?,?,?,?,?,?)',
        (conversation_id, account, peer or f'buyer_{conversation_id}', '买家', item_id, 'auto'),
    )
    return conversation_id


def seed_message(conversation_id: int, content: str = '在吗') -> None:
    """`messages.direction` 是 NOT NULL，手写 INSERT 必须带上。"""
    db.execute(
        'INSERT INTO messages(conversation_id, direction, sender, content) VALUES(?,?,?,?)',
        (conversation_id, 'in', 'buyer', content),
    )


def seed_order(order_id: str, item_id: str = ITEM_A, buyer_id: str = 'buyer_1',
               status: str = 'paid', account: str = ACC) -> None:
    db.execute(
        'INSERT INTO orders(account_id, order_id, item_id, buyer_id, amount, status) '
        'VALUES(?,?,?,?,?,?)',
        (account, order_id, item_id, buyer_id, 10.0, status),
    )


class ItemStatsApiTest(unittest.TestCase):
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
        for table in ('items', 'conversations', 'messages', 'orders',
                      'account_credentials', 'accounts', 'operation_logs'):
            db.execute(f'DELETE FROM {table}')
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上 'item_stats'。
        # 真实挂载顺序由 test_real_app_mounts_stats_route_before_item_detail 单独锁。
        app = FastAPI()
        app.include_router(item_stats_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('statsapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def stats(self, account: str = ACC, query: str = '', headers: Optional[Dict[str, str]] = None):
        return self.client.get(f'/api/items/{account}/stats{query}',
                               headers=self.headers if headers is None else headers)

    def data_of(self, account: str = ACC, query: str = '') -> Dict[str, Any]:
        resp = self.stats(account, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), resp.text)
        return body['data']

    def item_of(self, data: Dict[str, Any], item_id: str) -> Dict[str, Any]:
        found = [i for i in data['items'] if i['item_id'] == item_id]
        self.assertEqual(len(found), 1, f'{item_id} 不在 {data["items"]}')
        return found[0]

    # -- 本地口径 + null 规则 --------------------------------------------

    def test_local_stats_use_real_local_counts(self):
        """本地真能算的：会话数 / 消息数 / 最近消息时间 / 订单 / 买家 / 支付。"""
        seed_account()
        seed_item(ITEM_A)
        conv = seed_conversation(1, item_id=ITEM_A)
        seed_message(conv, '在吗')
        seed_message(conv, '发货了吗')
        seed_order('o1', item_id=ITEM_A, buyer_id='b1', status='paid')
        seed_order('o2', item_id=ITEM_A, buyer_id='b2', status='shipped')
        seed_order('o3', item_id=ITEM_A, buyer_id='b1', status='pending')
        install_fake_protocol(FakeClient(ACC))

        item = self.item_of(self.data_of(), ITEM_A)
        self.assertEqual(item['conversations'], 1)
        self.assertEqual(item['messages'], 2)
        self.assertTrue(item['last_message_at'], item)
        self.assertEqual(item['orders'], 3)
        self.assertEqual(item['buyers'], 2, '买家数按 buyer_id 去重')
        self.assertEqual(item['paid_orders'], 2, 'pending 不算已支付')

    def test_missing_platform_data_is_null_not_zero(self):
        """没有平台接口时，平台指标必须是 `null` —— 绝不补 0。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(FakeClient(ACC))

        data = self.data_of()
        self.assertEqual(data['source'], 'local')
        item = self.item_of(data, ITEM_A)
        for key in ('exposure', 'visits', 'visitors'):
            self.assertIsNone(item[key], f'{key} 本地算不出来，必须是 null')
        self.assertTrue(any('平台' in w for w in data['warnings']), data['warnings'])

    def test_local_zero_counts_are_null_not_zero(self):
        """本地没有记录 ≠ 发生过 0 次：一条数据都没有的商品，各计数是 null。"""
        seed_account()
        seed_item(ITEM_A)
        seed_item(ITEM_B)  # 没会话、没订单
        seed_order('o1', item_id=ITEM_A, buyer_id='b1', status='paid')
        install_fake_protocol(FakeClient(ACC))

        item = self.item_of(self.data_of(), ITEM_B)
        for key in ('orders', 'buyers', 'paid_orders', 'conversations', 'messages'):
            self.assertIsNone(item[key], f'{key} 应该是 null 而不是 0')
        self.assertIsNone(item['last_message_at'])

    def test_no_orders_at_all_warns_and_returns_null(self):
        """整张 orders 表都没有该账号的记录时，是「没同步」而不是「0 单」。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(FakeClient(ACC))

        data = self.data_of()
        item = self.item_of(data, ITEM_A)
        self.assertIsNone(item['orders'])
        self.assertTrue(any('订单' in w for w in data['warnings']), data['warnings'])

    def test_orders_without_item_id_are_excluded_and_warned(self):
        seed_account()
        seed_item(ITEM_A)
        seed_order('o1', item_id=ITEM_A, buyer_id='b1', status='paid')
        seed_order('o2', item_id='', buyer_id='b2', status='paid')  # 没关联商品
        install_fake_protocol(FakeClient(ACC))

        data = self.data_of()
        self.assertEqual(self.item_of(data, ITEM_A)['orders'], 1)
        self.assertTrue(any('未关联商品' in w or '没有关联商品' in w for w in data['warnings']),
                        data['warnings'])

    def test_images_and_price_are_parsed_from_local_row(self):
        """images 是 JSON 字符串；坏 JSON 给空数组，不能把接口打挂。"""
        seed_account()
        seed_item(ITEM_A, price=88.5, images='["https://img/a.jpg","https://img/b.jpg"]')
        seed_item(ITEM_B, price=0, images='not-json')
        install_fake_protocol(FakeClient(ACC))

        data = self.data_of()
        item_a = self.item_of(data, ITEM_A)
        self.assertEqual(item_a['images'], ['https://img/a.jpg', 'https://img/b.jpg'])
        self.assertEqual(item_a['price'], 88.5)
        self.assertEqual(item_a['status'], 'on_sale')
        self.assertEqual(self.item_of(data, ITEM_B)['images'], [])

    def test_updated_at_is_utc_milliseconds(self):
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(FakeClient(ACC))

        stamp = self.data_of()['updated_at']
        self.assertIsInstance(stamp, int)
        self.assertGreater(stamp, 1_600_000_000_000, '毫秒时间戳（UTC），不是秒')
        self.assertLess(abs(stamp - int(time.time() * 1000)), 60_000)

    # -- 平台口径 --------------------------------------------------------

    def test_platform_metrics_are_used_when_available(self):
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={
            'exposure': 1234, 'visits': 56, 'visitors': 12, 'orders': 7, 'paidOrders': 5}))

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        item = self.item_of(data, ITEM_A)
        self.assertEqual(item['exposure'], 1234)
        self.assertEqual(item['visits'], 56)
        self.assertEqual(item['visitors'], 12)
        self.assertEqual(item['orders'], 7, '平台给了订单数就优先用平台的')
        self.assertEqual(item['paid_orders'], 5, '认 camelCase 别名')
        self.assertFalse(any('平台' in w and '降级' in w for w in data['warnings']), data['warnings'])

    def test_platform_metrics_missing_keys_stay_null(self):
        """平台只给了一部分：没给的仍然 null，不许拿本地值冒充平台值。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={'visits': 5}))

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        item = self.item_of(data, ITEM_A)
        self.assertEqual(item['visits'], 5)
        self.assertIsNone(item['exposure'])
        self.assertIsNone(item['visitors'])

    def test_platform_failure_degrades_to_local_with_warning(self):
        """平台挂了不该让整个接口 500：降级 + 说清原因。"""
        seed_account()
        seed_item(ITEM_A)
        seed_conversation(1, item_id=ITEM_A)
        seed_message(1)
        install_fake_protocol(StatsClient(ACC, error=RuntimeError('接口 500')))

        data = self.data_of()
        self.assertEqual(data['source'], 'local')
        item = self.item_of(data, ITEM_A)
        self.assertIsNone(item['exposure'])
        self.assertEqual(item['conversations'], 1, '降级后本地统计照常返回')
        self.assertTrue(any('降级' in w for w in data['warnings']), data['warnings'])

    def test_platform_unparsable_response_degrades_with_warning(self):
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={'unexpected': 'shape'}))

        data = self.data_of()
        self.assertEqual(data['source'], 'local')
        self.assertIsNone(self.item_of(data, ITEM_A)['exposure'])
        self.assertTrue(any('可识别' in w or '无法解析' in w for w in data['warnings']),
                        data['warnings'])

    def test_account_level_response_is_not_attributed_to_every_item(self):
        """账号级汇总（「本店总曝光 20481」）**不许**按到每条商品头上 —— 那是假数据。

        平台只提供不带参数的接口时，响应里认不出「这条商品那一份」就必须降级。
        """
        seed_account()
        seed_item(ITEM_A)
        seed_item(ITEM_B)
        install_fake_protocol(AccountLevelClient(
            ACC, payload={'exposure': 20481, 'visits': 306, 'visitors': 91}))

        data = self.data_of()
        self.assertEqual(data['source'], 'local', '认不出归属就不能声称是平台数据')
        for item_id in (ITEM_A, ITEM_B):
            self.assertIsNone(self.item_of(data, item_id)['exposure'],
                              f'{item_id} 不该继承账号级汇总值')
        self.assertTrue(any('可识别' in w or '无法解析' in w for w in data['warnings']),
                        data['warnings'])

    def test_account_level_response_with_per_item_map_is_used(self):
        """账号级接口但响应按 item_id 分桶时，能认出各商品的自己那份。"""
        seed_account()
        seed_item(ITEM_A)
        seed_item(ITEM_B)
        install_fake_protocol(AccountLevelClient(ACC, payload={
            ITEM_A: {'exposure': 111},
            ITEM_B: {'exposure': 222, 'visits': 9},
        }))

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        item_a = self.item_of(data, ITEM_A)
        self.assertEqual(item_a['exposure'], 111)
        self.assertIsNone(item_a['visits'], 'A 那份里没有 visits')
        item_b = self.item_of(data, ITEM_B)
        self.assertEqual((item_b['exposure'], item_b['visits']), (222, 9))

    def test_platform_call_passes_item_id_once_without_probing(self):
        """按商品查只调一次：**不许**靠「先带 item_id 再不带」的抛异常探测签名。

        旧写法对每条商品调两次（第二次不带参数），既白费平台配额，又会把「不带参数
        拿到的账号级汇总」当成这条商品的数据。
        """
        seed_account()
        seed_item(ITEM_A)
        seed_item(ITEM_B)
        fake = StatsClient(ACC)
        install_fake_protocol(fake)

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        calls = [c for c in fake.calls if c[0] == 'item_stats']
        self.assertEqual(len(calls), 2, f'每条商品只该调一次，实际 {calls}')
        self.assertEqual(sorted(c[1] for c in calls), sorted([ITEM_A, ITEM_B]),
                         f'必须带上 item_id，不能退化成不带参数调用: {calls}')

    def test_response_labelled_with_another_item_is_not_attributed(self):
        """平台明说这份数据属于**别的**商品时，不能张冠李戴到当前商品头上。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(LabelledClient(ACC, owner='item_OTHER'))

        data = self.data_of()
        self.assertEqual(data['source'], 'local')
        item_a = self.item_of(data, ITEM_A)
        self.assertIsNone(item_a['exposure'], '别人的曝光数不许当成 A 的')
        self.assertIsNone(item_a['visits'])
        self.assertTrue(any('不属于' in w for w in data['warnings']), data['warnings'])

    def test_per_item_call_with_whole_map_picks_own_bucket(self):
        """按商品查但每次都回整本账：只认自己那一桶，别拿别人的数。"""
        seed_account()
        seed_item(ITEM_A)
        seed_item(ITEM_B)
        install_fake_protocol(MapByItemClient(ACC))

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(self.item_of(data, ITEM_A)['exposure'], 11)
        self.assertIsNone(self.item_of(data, ITEM_A)['visits'], 'A 那桶里没有 visits')
        item_b = self.item_of(data, ITEM_B)
        self.assertEqual((item_b['exposure'], item_b['visits']), (22, 7))

    def test_signature_probe_of_uninspectable_callable_is_unknown(self):
        """`inspect.signature` 认不出来时返回 `None`（退回两次尝试），不抛异常。"""
        self.assertIsNone(item_stats_service._accepts_item_id(max))

    def test_strict_platform_source_failure_is_502(self):
        """显式 `?source=platform`：平台拿不到就是 502，不许悄悄降级。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, error=RuntimeError('接口 500')))

        resp = self.stats(query='?source=platform')
        self.assertEqual(resp.status_code, 502, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'protocol_error')

    def test_strict_platform_without_platform_method_is_503(self):
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(FakeClient(ACC))

        resp = self.stats(query='?source=platform')
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()['code'], 'xianyu_not_ready')

    def test_platform_session_expired_is_409(self):
        """平台调用里发现登录态过期 → 409（前端据此弹重新扫码），不是 502。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, error=SessionExpiredError('session expired')))

        resp = self.stats()
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')

    def test_local_source_never_touches_platform(self):
        """`?source=local` 是纯本地：不调协议层，也不要求 Cookie。"""
        seed_account(cookie='', login_state='unknown')  # 没登录过
        seed_item(ITEM_A)
        fake = StatsClient(ACC, error=RuntimeError('不该被调用'))
        install_fake_protocol(fake)

        data = self.data_of(query='?source=local')
        self.assertEqual(data['source'], 'local')
        self.assertEqual(fake.calls, [], 'source=local 时不该调协议层')
        self.assertFalse(any('平台' in w for w in data['warnings']), data['warnings'])

    # -- 账号 / 参数 / 鉴权 ----------------------------------------------

    def test_unknown_account_is_404(self):
        seed_item(ITEM_A)  # 有商品，但账号行不存在
        resp = self.stats(account='ghost_acc')
        self.assertEqual(resp.status_code, 404, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'not_found')
        self.assertIn('ghost_acc', body['message'])

    def test_account_without_cookie_is_409(self):
        seed_account(cookie='')  # 账号在，但没 Cookie
        seed_item(ITEM_A)
        resp = self.stats()
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')

    def test_need_relogin_state_is_409(self):
        seed_account(login_state='need_relogin')
        seed_item(ITEM_A)
        resp = self.stats()
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')

    def test_item_filter_returns_single_item(self):
        seed_account()
        seed_item(ITEM_A)
        seed_item(ITEM_B)
        install_fake_protocol(FakeClient(ACC))

        data = self.data_of(query=f'?item_id={ITEM_A}')
        self.assertEqual([i['item_id'] for i in data['items']], [ITEM_A])
        self.assertEqual(data['total'], 1)

    def test_unknown_item_filter_is_404(self):
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(FakeClient(ACC))

        resp = self.stats(query='?item_id=nope')
        self.assertEqual(resp.status_code, 404, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'not_found')
        self.assertIn('nope', body['message'])

    def test_unsupported_source_is_400(self):
        seed_account()
        seed_item(ITEM_A)
        resp = self.stats(query='?source=guess')
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')

    def test_requires_login(self):
        seed_account()
        seed_item(ITEM_A)
        resp = self.client.get(f'/api/items/{ACC}/stats')
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_empty_account_list_is_not_an_error(self):
        """账号存在但一条商品都没有 → 200 + 空列表（不是 404）。"""
        seed_account()
        install_fake_protocol(FakeClient(ACC))
        data = self.data_of()
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)

    # -- 独立审查 D1/D2/D3 的回归锁 ---------------------------------------

    def test_wants_reaches_the_response(self):
        """D2：`wants` 在别名表里、协议层也解析，就必须真的出现在响应里。

        旧实现 `_merge` 的返回字典漏了这个键 → CONTRACTS 写了、HANDOFF 说修了、
        端到端永远拿不到（审查实测 `{'exposureNum':41,'showNum':5,'wantNum':2}`
        只出 exposure/visits）。
        """
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={'exposure': 41, 'visits': 5, 'wants': 2}))

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(self.item_of(data, ITEM_A)['wants'], 2)
        self.assertIn('wants', data['metrics']['platform'])

    def test_wants_missing_is_none_not_zero(self):
        """平台没给「想要人数」时是 `None`（前端显示「-」），不许补 0。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={'exposure': 1}))

        self.assertIsNone(self.item_of(self.data_of(), ITEM_A)['wants'])

    def test_alias_table_recognizes_protocol_field_names(self):
        """D3：协议层的真字段名（`exposureNum`/`showNum`/`wantNum`）别名表要认。

        这些名字是 `xianyu/client.py` 改名前的平台原名；别名表认不出时，
        任何「没经过协议层改名」的响应（账号级接口、别的客户端实现）都会
        整条丢掉。
        """
        self.assertEqual(
            item_stats_service._normalize_platform(
                {'exposureNum': 100, 'showNum': 20, 'wantNum': 7}),
            {'exposure': 100.0, 'visits': 20.0, 'wants': 7.0},
        )

    def test_unrecognized_metric_like_key_warns_instead_of_silence(self):
        """D3：响应里有「像指标但没认出」的字段时要留一句 warning，不能完全静默。

        旧行为：200 + `source:platform` + 指标全 null + 零 warning，
        用户只看到「这功能坏了」。
        """
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={'wantNum': 3, 'exposureCnt': 88}))

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(self.item_of(data, ITEM_A)['wants'], 3)
        self.assertTrue(any('exposureCnt' in w for w in data['warnings']), data['warnings'])

    def test_structural_keys_do_not_trigger_the_warning(self):
        """结构性字段（item_id/title/code）不该被报成「没认出的指标」——那种 warning
        每次请求都会响，用户会学会无视它。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, payload={
            'item_id': ITEM_A, 'title': '耳机', 'code': 'SUCCESS', 'exposure': 9,
        }))

        data = self.data_of()
        self.assertEqual(self.item_of(data, ITEM_A)['exposure'], 9)
        self.assertFalse(any('没认出' in w for w in data['warnings']), data['warnings'])

    def test_no_second_probe_call_when_signature_accepts_item_id(self):
        """D1：签名收得下 `item_id` 时**不许**再补一次「不带参数」的调用。

        真协议层对空 `item_id` 直接失败（`xianyu/client.py::item_stats` 早先
        `return None`），第二次调用会把「第一次为什么失败」盖成
        「成功但没有内容」，端到端只剩一句「认不出」。
        """
        seed_account()
        seed_item(ITEM_A)
        fake = StatsClient(ACC, payload={'exposure': 5})
        install_fake_protocol(fake)

        data = self.data_of()
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(self.item_of(data, ITEM_A)['exposure'], 5)
        self.assertEqual([c[1] for c in fake.calls], [ITEM_A],
                         f'只该按商品调一次，实际 {fake.calls}')

    def test_ok_but_none_is_a_failure_with_reason(self):
        """D1：`CALL_OK` + `None`（协议层认不出内容）也是失败，且要留下原因。

        旧写法把它当成成功的空响应 → 降级文案是「返回的内容里没有可识别的指标」，
        和「会话拿不到」「接口下线」长得一样。
        """
        seed_account()
        seed_item(ITEM_A)
        # 假客户端真的返回 None（`StatsClient(payload=None)` 会走默认值，所以显式覆盖）
        fake = StatsClient(ACC, payload={'exposure': 1})
        fake.payload = None
        install_fake_protocol(fake)

        data = self.data_of()
        self.assertEqual(data['source'], 'local')
        self.assertIsNone(self.item_of(data, ITEM_A)['exposure'])
        self.assertTrue(any('没有返回可用内容' in w for w in data['warnings']), data['warnings'])

    def test_strict_platform_failure_message_carries_the_real_reason(self):
        """D1：`?source=platform` 失败时 message 必须有真实原因，不能是空串。"""
        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, error=RuntimeError('接口 500')))

        resp = self.stats(query='?source=platform')
        self.assertEqual(resp.status_code, 502, resp.text)
        message = resp.json()['message']
        self.assertIn('接口 500', message, message)
        # 真断言「冒号后面不是空的」：比旧文案（`平台返回的商品数据无法识别`）
        # 是恒真的 —— 那个串现实现永不产出，message 退化成
        # `平台商品数据接口调用失败:` 也能过。取第一个冒号之后的内容来判。
        self.assertNotEqual(message.split(':', 1)[-1].strip(), '', message)

    def test_failure_reasons_are_distinguishable(self):
        """D1 的核心：三种不同的平台失败**不能**给出同一句话。

        旧实现（无参回退 + `CALL_OK`+None 当成功）把「抛错」「返回 None」
        「响应认不出」全压成同一句「无法识别」。
        """
        seen: List[str] = []

        def platform_warning() -> str:
            """平台那条 warning（列表里还有「本地还没有订单数据」等无关项）。"""
            return next(w for w in self.data_of()['warnings'] if '平台' in w)

        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(StatsClient(ACC, error=RuntimeError('风控/网络错')))
        seen.append(platform_warning())

        none_client = StatsClient(ACC, payload={'exposure': 1})
        none_client.payload = None
        install_fake_protocol(none_client)
        seen.append(platform_warning())

        install_fake_protocol(StatsClient(ACC, payload={'somethingElse': 1}))
        seen.append(platform_warning())

        self.assertEqual(len(set(seen)), 3, f'三种失败必须能区分开: {seen}')
        self.assertTrue(any('风控/网络错' in w for w in seen), seen)

    # -- 路由挂载顺序（lead 的写范围） -----------------------------------

    def test_real_app_mounts_stats_route_before_item_detail(self):
        """`/api/items/{account_id}/stats` 必须声明在 `/{account_id}/{item_id}` 之前。

        还没挂进 `ROUTE_MODULES` 时**跳过**（lead 的写范围），不伪造通过；
        挂错顺序（在 'items' 之后）时这里会红 —— 请求会被商品详情吞成 404。
        """
        if 'item_stats' not in app_main.ROUTE_MODULES:
            self.skipTest("backend/app/main.py 的 ROUTE_MODULES 里还没有 'item_stats'")

        seed_account()
        seed_item(ITEM_A)
        install_fake_protocol(FakeClient(ACC))

        # 新版 FastAPI 把 `include_router` 的结果包成 `_IncludedRouter`（`.path is None`），
        # 真正带 path 的 APIRoute 在它嵌套的 `.routes` 里 —— 只看顶层会得到「一条 API 路由
        # 都没有」的假象（`test_contract_routes.py` 的 `walk()` 就是为此写的）。
        def collect(routes, out: List[str]) -> None:
            for route in routes:
                path = getattr(route, 'path', None)
                if path:
                    out.append(path)
                nested = getattr(route, 'routes', None) or getattr(
                    getattr(route, 'original_router', None), 'routes', None)
                if nested:
                    collect(nested, out)

        paths: List[str] = []
        collect(app_main.create_app().routes, paths)
        self.assertIn(STATS_PATH, paths, f'create_app() 没挂上 {STATS_PATH}')
        detail = '/api/items/{account_id}/{item_id}'
        if detail in paths:
            self.assertLess(paths.index(STATS_PATH), paths.index(detail),
                            'stats 路由必须排在 {account_id}/{item_id} 之前')

        with TestClient(app_main.create_app()) as client:
            resp = client.get(f'/api/items/{ACC}/stats', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('source', resp.json()['data'], '被商品详情路由抢先匹配了')


if __name__ == '__main__':
    unittest.main(verbosity=2)
