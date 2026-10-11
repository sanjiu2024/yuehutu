"""商品写接口（官方 seller-item 族：批量下架 / 批量删除 / 快捷改价改库存）的测试。

**为什么单开一组**：这一批是**平台写操作**，翻车代价比只读页高得多。最容易犯的错：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 账号掉线时先报「去重新登录」，用户填半天才发现
   连 `item_ids` 都是空的（三步顺序必须是 **确认 → 字段校验 → 建客户端**）；
3. 平台没逐条回报就默认成功（`ok` 造假）；
4. 平台回了信封但业务体是空的，还当成功；
5. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 之类的平台拒绝当成掉线，让用户白扫一次码；
6. `itemIds` 的入参形态：官方是**逗号串**，UI 给数组 —— 两种都要收。

**一次真机写请求都没发过**（本机两个账号登录态都过期，不允许为了验证去改平台数据）。
所以这里用假协议层钉住「字段形状与官方 bundle 逐字一致」，真机是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_item_write_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_client`），假客户端只实现 `mtop`。
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import items as items_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import items as service  # noqa: E402
from backend.app.xianyu import item_write as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'iw_acc'
BASE = '/api/items'

#: 官方 `batch.offline` / `batch.delete` 的响应形状（bundle @4097558 读
#: `res.data.data.itemProcessResultList`，元素带 `success`）。
BATCH_OK = {
    'code': 'success',
    'data': {'itemProcessResultList': [
        {'itemId': 'i1', 'success': True},
        {'itemId': 'i2', 'success': True},
    ]},
}
#: 一条成功一条失败（逐条明细必须原样透出，不能合并成「2 个里有 1 个失败」）。
BATCH_PARTIAL = {
    'code': 'success',
    'data': {
        'itemProcessResultList': [
            {'itemId': 'i1', 'success': True},
            {'itemId': 'i2', 'success': False, 'errorCode': 'ITEM_NOT_FOUND',
             'errorMsg': '商品不存在或已删除'},
        ],
        'failCount': 1,
    },
}
#: 平台接受了整批但**没有**逐条清单（不假装逐条都确认过）。
BATCH_NO_REPORT = {'code': 'success', 'data': {'accepted': True}}
#: `info.update` 成功（bundle @4100255 判 `res.data.data` 真）。
QUICK_OK = {'code': 'success', 'data': {'itemId': 'i1', 'price': '39.9', 'quantity': 7}}
#: 平台回了信封但业务体是空的 —— 官方 bundle 判**失败**，我们也要判失败。
ENVELOPE_EMPTY = {'code': 'FAIL_BIZ_SOMETHING', 'msg': '商品状态不允许该操作', 'data': None}


class XianyuError(RuntimeError):
    """与协议层同名（`common.call_failed` 只按异常本身分类，不看类名）。"""


def wrap(payload: Any, ret: Optional[List[str]] = None) -> Dict[str, Any]:
    """把平台业务信封包成 `client.mtop` 的返回值。

    协议层解析函数收的是**客户端返回值**（`{'success','data','message','ret'}`，
    与 `xianyu/trade.py` 的 `envelope_error` / `body_of` 同一约定），业务信封在
    `raw['data']`。测试里把这一步显式写出来，免得两边对「raw 到底指哪一层」理解错。
    """
    return {'success': True, 'data': payload, 'message': '',
            'ret': ret or ['SUCCESS::调用成功']}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeItemsClient:
    """假协议层：只实现 `mtop`，按 api 名回话，记录每次调用的参数。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   spm_cnt: Optional[str] = None,
                   extra_headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v})
        error = self.errors.get(api)
        if error is not None:
            raise error
        return {'success': True, 'data': self.payloads.get(api), 'message': '',
                'ret': ['SUCCESS::调用成功']}

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


def install_fake_client(client: FakeItemsClient, *, with_factory: bool = True) -> None:
    """把 `backend.app.xianyu.client` 换成假模块（`common.get_client` 懒导入它）。

    `with_factory=True`（默认）时注入 `client.py::get_client` 工厂 —— `common.get_client`
    会**优先**用它，于是绕过 Cookie 检查（真机客户端就是这么接的）。
    要测「凭据检查」本身时必须 `with_factory=False`：这时 `common.get_client` 走
    `store.get_cookie(account_id)` → 空 Cookie 就 `not_ready`。
    """
    module = types.ModuleType('backend.app.xianyu.client')
    if with_factory:
        def factory(account_id: str) -> FakeItemsClient:
            client.account_id = account_id
            return client

        module.get_client = factory  # type: ignore[attr-defined]

    def ctor(cookie: str) -> FakeItemsClient:
        client.cookie = cookie
        return client

    module.XianyuClient = ctor  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_client() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def seed_item(item_id: str, account_id: str = ACC, *, price: float = 10.0,
              stock: int = 1, status: str = 'on_sale') -> None:
    db.execute(
        'INSERT INTO items(account_id, item_id, title, price, stock, status) VALUES(?,?,?,?,?,?)',
        (account_id, item_id, f'商品 {item_id}', price, stock, status),
    )


def item_row(item_id: str, account_id: str = ACC) -> Optional[Any]:
    return db.query_one('SELECT * FROM items WHERE account_id = ? AND item_id = ?',
                        (account_id, item_id))


# =========================================================================== 协议层纯函数


class ItemWriteProtocolTest(unittest.TestCase):
    """协议层纯函数：只钉字段形状与解析，不碰服务层。"""

    def test_four_apis_are_the_ones_from_the_bundle(self):
        self.assertEqual(proto.OFFLINE_API, 'mtop.alibaba.idle.seller.pc.item.offline')
        self.assertEqual(proto.BATCH_OFFLINE_API,
                         'mtop.alibaba.idle.seller.pc.item.batch.offline')
        self.assertEqual(proto.BATCH_DELETE_API,
                         'mtop.alibaba.idle.seller.pc.item.batch.delete')
        self.assertEqual(proto.INFO_UPDATE_API,
                         'mtop.alibaba.idle.seller.pc.item.info.update')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_assert_write_allows_the_four_and_rejects_others(self):
        for action in ('offline', 'batch_offline', 'batch_delete', 'quick_update'):
            self.assertEqual(proto.assert_write(proto.WRITE_APIS[action]['api']),
                             proto.WRITE_APIS[action]['api'])
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write('mtop.alibaba.idle.seller.pc.item.delete')

    def test_dead_apis_are_registered_but_not_callable(self):
        """4 条「声明了但调用点 0」的接口：登记出来，但不进 `ALL_APIS`。"""
        self.assertEqual(len(proto.NOT_IMPLEMENTED), 4)
        for api in proto.NOT_IMPLEMENTED:
            self.assertNotIn(api, proto.ALL_APIS)
            with self.assertRaises(proto.UnknownApi):
                proto.assert_write(api)

    def test_normalize_item_ids_accepts_both_shapes(self):
        """官方发逗号串，UI 给数组 —— 两种入参都要收。"""
        self.assertEqual(proto.normalize_item_ids(['i1', 'i2']), 'i1,i2')
        self.assertEqual(proto.normalize_item_ids('i1,i2'), 'i1,i2')
        self.assertEqual(proto.normalize_item_ids(('i1',)), 'i1')
        self.assertEqual(proto.normalize_item_ids(' i1 , i2 '), 'i1 , i2')
        self.assertEqual(proto.normalize_item_ids(['i1', '', None, 'i2']), 'i1,i2')
        self.assertEqual(proto.normalize_item_ids(None), '')
        self.assertEqual(proto.item_id_list('i1,i2'), ['i1', 'i2'])
        self.assertEqual(proto.item_id_list([]), [])

    def test_batch_request_is_a_comma_string_not_a_list(self):
        """bundle @4097558 原文 `{itemIds: T.join(",")}` —— 发数组平台不认。"""
        api, data = proto.batch_offline_request(['i1', 'i2'])
        self.assertEqual(api, proto.BATCH_OFFLINE_API)
        self.assertEqual(data, {'itemIds': 'i1,i2'})
        api, data = proto.batch_delete_request('i1,i2')
        self.assertEqual(api, proto.BATCH_DELETE_API)
        self.assertEqual(data, {'itemIds': 'i1,i2'})

    def test_offline_request_is_a_single_item_id(self):
        self.assertEqual(proto.offline_request('i1'), (proto.OFFLINE_API, {'itemId': 'i1'}))

    def test_quick_update_sends_only_the_fields_given(self):
        """官方是 `undefined` 不是空串 —— 没给的字段**不放键**。"""
        self.assertEqual(proto.quick_update_request('i1', price=39.9),
                         (proto.INFO_UPDATE_API, {'itemId': 'i1', 'price': 39.9}))
        self.assertEqual(proto.quick_update_request('i1', quantity=7),
                         (proto.INFO_UPDATE_API, {'itemId': 'i1', 'quantity': 7}))
        self.assertEqual(proto.quick_update_request('i1', price=39.9, quantity=7),
                         (proto.INFO_UPDATE_API, {'itemId': 'i1', 'price': 39.9, 'quantity': 7}))
        # 有 SKU 时走另一条路（`itemSkuListStr`），与 price/quantity 互斥
        self.assertEqual(proto.quick_update_request('i1', sku_list_str='[{"skuId":1}]'),
                         (proto.INFO_UPDATE_API,
                          {'itemId': 'i1', 'itemSkuListStr': '[{"skuId":1}]'}))

    def test_request_of_dispatches_by_action(self):
        self.assertEqual(proto.request_of('batch_offline', item_ids=['i1'])[0],
                         proto.BATCH_OFFLINE_API)
        self.assertEqual(proto.request_of('batch_delete', item_ids=['i1'])[0],
                         proto.BATCH_DELETE_API)
        self.assertEqual(proto.request_of('offline', item_id='i1')[0], proto.OFFLINE_API)
        self.assertEqual(proto.request_of('quick_update', item_id='i1', quantity=2)[0],
                         proto.INFO_UPDATE_API)
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('nope')

    def test_success_judgement_follows_the_bundle(self):
        """官方判据是 `res.data.data` 真 —— 空 dict / 空 list / None 全算失败。"""
        self.assertTrue(proto.is_success(wrap(BATCH_OK)))
        self.assertTrue(proto.is_success(wrap(QUICK_OK)))
        self.assertFalse(proto.is_success(wrap(ENVELOPE_EMPTY)))
        self.assertFalse(proto.is_success(wrap({'code': 'success', 'data': {}})))
        self.assertFalse(proto.is_success(wrap({'code': 'success', 'data': []})))
        self.assertFalse(proto.is_success(wrap({'code': 'success'})))
        self.assertFalse(proto.is_success(None))
        self.assertFalse(proto.is_success('nope'))
        # 客户端层失败（mtop 自己报错）也必须是失败
        self.assertFalse(proto.is_success({'success': False, 'data': BATCH_OK,
                                           'message': 'MTOP 调用失败'}))

    def test_envelope_helpers_read_the_platform_fields(self):
        raw = wrap(ENVELOPE_EMPTY)
        self.assertEqual(proto.code_of(raw), 'FAIL_BIZ_SOMETHING')
        self.assertEqual(proto.message_of(raw), '商品状态不允许该操作')
        self.assertEqual(proto.failure_reason(raw), '商品状态不允许该操作')
        self.assertEqual(proto.failure_reason({'data': {}}, '兜底'), '兜底')
        self.assertEqual(proto.business(wrap(BATCH_OK))['itemProcessResultList'][0]['itemId'],
                         'i1')

    def test_process_results_and_counts(self):
        self.assertEqual(len(proto.process_results(wrap(BATCH_OK))), 2)
        self.assertEqual(proto.count_process_results(wrap(BATCH_OK)), (2, 0))
        self.assertEqual(proto.count_process_results(wrap(BATCH_PARTIAL)), (1, 1))
        # 拿不到清单回空列表（不造假的成功条目）
        self.assertEqual(proto.process_results(wrap(BATCH_NO_REPORT)), [])
        self.assertEqual(proto.process_results(wrap(ENVELOPE_EMPTY)), [])

    def test_process_failures_are_per_item(self):
        failures = proto.process_failures(wrap(BATCH_PARTIAL))
        self.assertEqual(failures, [{'id': 'i2', 'code': 'ITEM_NOT_FOUND',
                                     'message': '商品不存在或已删除'}])
        # 平台没给原因时给一句人话，不是空串
        bare = wrap({'code': 'success', 'data': {'itemProcessResultList': [
            {'itemId': 'i9', 'success': False}]}})
        self.assertEqual(proto.process_failures(bare)[0]['message'], '平台没有给出原因')
        self.assertEqual(proto.process_failures(bare)[0]['code'], 'platform_rejected')

    def test_failure_count_and_updated_values_do_not_fake_zero(self):
        self.assertEqual(proto.failure_count(wrap(BATCH_PARTIAL)), 1)
        # 平台没给 failCount → None（0 与「平台没给」是两回事）
        self.assertIsNone(proto.failure_count(wrap(BATCH_OK)))
        self.assertEqual(proto.updated_values(wrap(QUICK_OK)),
                         {'price': '39.9', 'quantity': 7})
        self.assertEqual(proto.updated_values(wrap({'code': 'success',
                                                    'data': {'itemId': 'i1'}})), {})

    def test_actions_are_the_write_apis(self):
        self.assertEqual(sorted(proto.ACTIONS), ['batch_delete', 'batch_offline',
                                                 'offline', 'quick_update'])
        for action in proto.ACTIONS:
            self.assertIn(proto.WRITE_APIS[action]['api'], proto.ALL_APIS)
            self.assertTrue(proto.WRITE_APIS[action]['evidence'])


# =========================================================================== 服务层 + HTTP


class ItemWriteApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_client()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fake_client()
        for table in ('accounts', 'account_credentials', 'items', 'operation_logs'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeItemsClient(payloads={
            proto.BATCH_OFFLINE_API: BATCH_OK,
            proto.BATCH_DELETE_API: BATCH_OK,
            proto.INFO_UPDATE_API: QUICK_OK,
        })
        install_fake_client(self.fake)
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已挂上。
        app = FastAPI()
        app.include_router(items_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('iwapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def post(self, path: str, body: Any = None):
        return self.client.post(path, json=body if body is not None else {},
                                headers=self.headers)

    def ok_data(self, path: str, body: Any = None) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def fail_body(self, path: str, body: Any = None, status: int = 400) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    # -- 确认闸门（红线：未确认时一个平台请求都不发）------------------------

    def test_batch_offline_without_confirm_sends_nothing(self):
        seed_account()
        seed_item('i1')
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline', {'item_ids': ['i1']})
        self.assertEqual(body['code'], service.CONFIRM_CODE)
        self.assertIn('confirm=true', body['message'])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(item_row('i1')['status'], 'on_sale')

    def test_batch_delete_without_confirm_sends_nothing(self):
        seed_account()
        seed_item('i1')
        body = self.fail_body(f'{BASE}/{ACC}/batch-delete', {'item_ids': ['i1']})
        self.assertEqual(body['code'], service.CONFIRM_CODE)
        self.assertEqual(self.fake.calls, [])
        self.assertIsNotNone(item_row('i1'))

    def test_quick_edit_without_confirm_sends_nothing(self):
        seed_account()
        seed_item('i1', price=10.0)
        body = self.fail_body(f'{BASE}/{ACC}/i1/quick-edit', {'price': 39.9})
        self.assertEqual(body['code'], service.CONFIRM_CODE)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(item_row('i1')['price'], 10.0)

    def test_false_like_confirm_values_are_not_consent(self):
        """`False` / `'false'` / `0` / `''` / `None` 都不算点头。"""
        seed_account()
        seed_item('i1')
        for value in (False, 'false', 'FALSE', 0, '', None, 'yes', 2):
            body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                                  {'item_ids': ['i1'], 'confirm': value})
            self.assertEqual(body['code'], service.CONFIRM_CODE, value)
        self.assertEqual(self.fake.calls, [])

    def test_truthy_accepts_true_string_and_one(self):
        self.assertTrue(service._truthy(True))
        self.assertTrue(service._truthy('true'))
        self.assertTrue(service._truthy(' TRUE '))
        self.assertTrue(service._truthy(1))
        self.assertFalse(service._truthy('1'))
        self.assertFalse(service._truthy(0))

    # -- 三步顺序：确认 → 字段校验 → 建客户端 ------------------------------

    def test_field_validation_runs_before_credential_check(self):
        """掉线账号 + 空 item_ids → 先说「item_ids 不能为空」，不是「去重新登录」。"""
        seed_account(cookie='', login_state='need_relogin')
        # 不注入工厂：这样 `common.get_client` 真的会去查 Cookie（注入工厂会绕过它）
        install_fake_client(self.fake, with_factory=False)
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': [], 'confirm': True})
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('item_ids', body['message'])
        self.assertEqual(self.fake.calls, [])
        # 字段齐了才轮到凭据检查
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': ['i1'], 'confirm': True}, status=503)
        self.assertEqual(body['code'], 'xianyu_not_ready')
        self.assertIn('Cookie', body['message'])
        self.assertEqual(self.fake.calls, [])

    def test_quick_edit_validates_before_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_client(self.fake, with_factory=False)
        body = self.fail_body(f'{BASE}/{ACC}/i1/quick-edit',
                              {'confirm': True})
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('至少要填一个', body['message'])
        body = self.fail_body(f'{BASE}/{ACC}/i1/quick-edit',
                              {'price': 'abc', 'confirm': True})
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_empty_item_ids_is_rejected(self):
        seed_account()
        for value in (None, '', [], '  ', [''], [None]):
            body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                                  {'item_ids': value, 'confirm': True})
            self.assertEqual(body['code'], 'invalid_fields', value)
        self.assertEqual(self.fake.calls, [])

    def test_too_many_ids_is_rejected(self):
        seed_account()
        ids = [f'i{n}' for n in range(service.BATCH_WRITE_LIMIT + 1)]
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': ids, 'confirm': True})
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn(str(service.BATCH_WRITE_LIMIT), body['message'])
        self.assertEqual(self.fake.calls, [])

    # -- 批量下架 ---------------------------------------------------------

    def test_batch_offline_happy_path(self):
        seed_account()
        seed_item('i1')
        seed_item('i2')
        data = self.ok_data(f'{BASE}/{ACC}/batch-offline',
                            {'item_ids': ['i1', 'i2'], 'confirm': True})
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['ok'], 2)
        self.assertEqual(data['failed'], [])
        self.assertEqual(data['status'], 'off_shelf')
        self.assertIs(data['per_item_report'], True)
        self.assertEqual(item_row('i1')['status'], 'off_shelf')
        self.assertEqual(item_row('i2')['status'], 'off_shelf')
        # 一次请求搞定（不是逐条 N 次）
        self.assertEqual(self.fake.apis(), [proto.BATCH_OFFLINE_API])
        self.assertEqual(self.fake.calls[0]['data'], {'itemIds': 'i1,i2'})
        self.assertEqual(self.fake.calls[0]['v'], '1.0')

    def test_batch_offline_accepts_comma_string(self):
        """MCP 侧给逗号串，UI 侧给数组 —— 两种都要收。"""
        seed_account()
        seed_item('i1')
        seed_item('i2')
        data = self.ok_data(f'{BASE}/{ACC}/batch-offline',
                            {'item_ids': 'i1,i2', 'confirm': True})
        self.assertEqual(data['ok'], 2)
        self.assertEqual(self.fake.calls[0]['data'], {'itemIds': 'i1,i2'})

    def test_batch_offline_reports_per_item_failures(self):
        """逐条失败明细必须原样透出，`ok` 不能把失败的那条算进去。"""
        seed_account()
        seed_item('i1')
        seed_item('i2')
        self.fake.payloads[proto.BATCH_OFFLINE_API] = BATCH_PARTIAL
        data = self.ok_data(f'{BASE}/{ACC}/batch-offline',
                            {'item_ids': ['i1', 'i2'], 'confirm': True})
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['ok'], 1)
        self.assertEqual(data['failed'], [{'id': 'i2', 'code': 'ITEM_NOT_FOUND',
                                           'message': '商品不存在或已删除'}])
        self.assertEqual(data['platform_fail_count'], 1)
        self.assertEqual(item_row('i1')['status'], 'off_shelf')
        # 失败的那条本地**不动**（不假装它下架了）
        self.assertEqual(item_row('i2')['status'], 'on_sale')

    def test_ids_the_platform_never_reported_are_failures(self):
        """平台只回报了一部分：没回报的一律算失败，绝不默认成功。"""
        seed_account()
        for item_id in ('i1', 'i2', 'i3'):
            seed_item(item_id)
        self.fake.payloads[proto.BATCH_OFFLINE_API] = {
            'code': 'success',
            'data': {'itemProcessResultList': [{'itemId': 'i1', 'success': True}]},
        }
        data = self.ok_data(f'{BASE}/{ACC}/batch-offline',
                            {'item_ids': ['i1', 'i2', 'i3'], 'confirm': True})
        self.assertEqual(data['ok'], 1)
        missing = [f for f in data['failed'] if f['code'] == 'no_per_item_report']
        self.assertEqual(sorted(f['id'] for f in missing), ['i2', 'i3'])
        self.assertEqual(item_row('i2')['status'], 'on_sale')

    def test_no_per_item_report_is_flagged_not_faked(self):
        """平台接受整批但没给清单：按成功处理，但如实标 `per_item_report: False`。"""
        seed_account()
        seed_item('i1')
        self.fake.payloads[proto.BATCH_OFFLINE_API] = BATCH_NO_REPORT
        data = self.ok_data(f'{BASE}/{ACC}/batch-offline',
                            {'item_ids': ['i1'], 'confirm': True})
        self.assertEqual(data['ok'], 1)
        self.assertIs(data['per_item_report'], False)
        self.assertEqual(data['failed'], [])

    def test_local_row_missing_is_a_warning_not_silence(self):
        seed_account()
        self.fake.payloads[proto.BATCH_OFFLINE_API] = {
            'code': 'success',
            'data': {'itemProcessResultList': [{'itemId': 'ghost', 'success': True}]},
        }
        data = self.ok_data(f'{BASE}/{ACC}/batch-offline',
                            {'item_ids': ['ghost'], 'confirm': True})
        self.assertEqual(data['ok'], 1)
        self.assertEqual(len(data['warnings']), 1)
        self.assertEqual(data['warnings'][0]['code'], 'local_missing')
        self.assertEqual(data['warnings'][0]['id'], 'ghost')

    def test_empty_envelope_is_a_502_not_a_success(self):
        """平台回了信封但业务体是空的 —— 官方 bundle 判失败，我们也要判失败。"""
        seed_account()
        seed_item('i1')
        self.fake.payloads[proto.BATCH_OFFLINE_API] = ENVELOPE_EMPTY
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': ['i1'], 'confirm': True}, status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('商品状态不允许该操作', body['message'])
        self.assertEqual(item_row('i1')['status'], 'on_sale')

    def test_session_expired_is_409_and_writes_back_need_relogin(self):
        seed_account()
        seed_item('i1')
        self.fake.errors[proto.BATCH_OFFLINE_API] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': ['i1'], 'confirm': True}, status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(item_row('i1')['status'], 'on_sale')
        state = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                             (ACC,))
        self.assertEqual(state['login_state'], 'need_relogin')

    def test_platform_rejection_is_not_treated_as_logout(self):
        """无权限 / 参数不对 ≠ 掉线：不许把账号标成 need_relogin 让用户白扫一次码。"""
        seed_account()
        seed_item('i1')
        self.fake.payloads[proto.BATCH_OFFLINE_API] = {
            'code': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED', 'msg': '无权限访问', 'data': None}
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': ['i1'], 'confirm': True}, status=502)
        self.assertEqual(body['code'], 'protocol_error')
        state = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                             (ACC,))
        self.assertEqual(state['login_state'], 'active')

    def test_missing_protocol_method_is_503(self):
        """协议层半升级（没有 mtop）→ 503，不是 500。"""
        seed_account()
        seed_item('i1')

        class NoMtop:
            pass

        install_fake_client(NoMtop())  # type: ignore[arg-type]
        body = self.fail_body(f'{BASE}/{ACC}/batch-offline',
                              {'item_ids': ['i1'], 'confirm': True}, status=503)
        self.assertEqual(body['code'], 'xianyu_not_ready')

    # -- 批量删除 ---------------------------------------------------------

    def test_batch_delete_removes_local_rows_after_platform_ok(self):
        seed_account()
        seed_item('i1')
        seed_item('i2')
        data = self.ok_data(f'{BASE}/{ACC}/batch-delete',
                            {'item_ids': ['i1', 'i2'], 'confirm': True})
        self.assertEqual(data['ok'], 2)
        self.assertEqual(data['status'], 'deleted')
        self.assertIsNone(item_row('i1'))
        self.assertIsNone(item_row('i2'))
        self.assertEqual(self.fake.apis(), [proto.BATCH_DELETE_API])

    def test_batch_delete_keeps_local_rows_when_platform_fails(self):
        seed_account()
        seed_item('i1')
        self.fake.payloads[proto.BATCH_DELETE_API] = ENVELOPE_EMPTY
        self.fail_body(f'{BASE}/{ACC}/batch-delete',
                       {'item_ids': ['i1'], 'confirm': True}, status=502)
        self.assertIsNotNone(item_row('i1'))

    # -- 快捷改价 / 改库存 -------------------------------------------------

    def test_quick_edit_price_only(self):
        seed_account()
        seed_item('i1', price=10.0, stock=3)
        data = self.ok_data(f'{BASE}/{ACC}/i1/quick-edit',
                            {'price': 39.9, 'confirm': True})
        self.assertEqual(data['item_id'], 'i1')
        self.assertIs(data['synced'], True)
        self.assertIsNone(data['warning'])
        self.assertEqual(item_row('i1')['price'], 39.9)
        # 没给的字段不碰本地
        self.assertEqual(item_row('i1')['stock'], 3)
        self.assertEqual(self.fake.calls[0]['data'], {'itemId': 'i1', 'price': 39.9})

    def test_quick_edit_quantity_only(self):
        seed_account()
        seed_item('i1', price=10.0, stock=3)
        data = self.ok_data(f'{BASE}/{ACC}/i1/quick-edit',
                            {'quantity': 7, 'confirm': True})
        self.assertEqual(data['quantity'], 7)
        self.assertEqual(item_row('i1')['stock'], 7)
        self.assertEqual(item_row('i1')['price'], 10.0)

    def test_quick_edit_uses_platform_echo_when_present(self):
        seed_account()
        seed_item('i1', price=10.0)
        data = self.ok_data(f'{BASE}/{ACC}/i1/quick-edit',
                            {'price': 39.9, 'quantity': 7, 'confirm': True})
        self.assertEqual(data['price'], '39.9')
        self.assertEqual(data['quantity'], 7)

    def test_quick_edit_validation(self):
        seed_account()
        seed_item('i1')
        cases = [
            ({'confirm': True}, '至少要填一个'),
            ({'price': 0, 'confirm': True}, '必须大于 0'),
            ({'price': -1, 'confirm': True}, '必须大于 0'),
            ({'price': 'abc', 'confirm': True}, '必须是数字'),
            ({'quantity': -1, 'confirm': True}, '不能为负'),
            ({'quantity': 'x', 'confirm': True}, '必须是整数'),
            ({'quantity': 1.5, 'confirm': True}, '必须是整数'),
        ]
        for body_in, fragment in cases:
            body = self.fail_body(f'{BASE}/{ACC}/i1/quick-edit', body_in)
            self.assertEqual(body['code'], 'invalid_fields', body_in)
            self.assertIn(fragment, body['message'], body_in)
        self.assertEqual(self.fake.calls, [])

    def test_quick_edit_empty_item_id_is_rejected(self):
        seed_account()
        # 路径里的空 id 匹配不上路由（404），所以直接打服务层
        result = service.quick_edit(ACC, '  ', price=1, confirm=True)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_fields')

    def test_quick_edit_local_missing_is_a_warning(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/ghost/quick-edit',
                            {'price': 5, 'confirm': True})
        self.assertIs(data['synced'], True)
        self.assertIn('本地没有这条商品记录', data['warning'])

    def test_quick_edit_does_not_touch_item_status(self):
        """整份回提会把已卖出商品重新上架（真机缺陷）；这条路径**不碰** status。"""
        seed_account()
        seed_item('i1', status='sold')
        self.ok_data(f'{BASE}/{ACC}/i1/quick-edit', {'price': 5, 'confirm': True})
        self.assertEqual(item_row('i1')['status'], 'sold')
        self.assertEqual(self.fake.calls[0]['data'], {'itemId': 'i1', 'price': 5.0})

    # -- 副作用（日志 / 事件）---------------------------------------------

    def test_every_ok_item_is_logged(self):
        seed_account()
        seed_item('i1')
        seed_item('i2')
        self.ok_data(f'{BASE}/{ACC}/batch-offline',
                     {'item_ids': ['i1', 'i2'], 'confirm': True})
        logs = db.query("SELECT action, item_id, success FROM operation_logs "
                        "WHERE account_id = ? ORDER BY item_id", (ACC,))
        self.assertEqual([(row['action'], row['item_id'], row['success']) for row in logs],
                         [('batch_offline', 'i1', 1), ('batch_offline', 'i2', 1)])

    def test_quick_edit_is_logged(self):
        seed_account()
        seed_item('i1')
        self.ok_data(f'{BASE}/{ACC}/i1/quick-edit', {'quantity': 2, 'confirm': True})
        logs = db.query("SELECT action, item_id FROM operation_logs WHERE account_id = ?", (ACC,))
        self.assertEqual([(row['action'], row['item_id']) for row in logs], [('quick_edit', 'i1')])

    def test_events_are_published(self):
        """`publish()` 一定留历史（没有订阅者时也留），所以直接查 `recent()`。"""
        from backend.app.core import events

        seed_account()
        seed_item('i1')
        before = len(events.recent(limit=500))
        self.ok_data(f'{BASE}/{ACC}/batch-offline',
                     {'item_ids': ['i1'], 'confirm': True})
        topics = [event['topic'] for event in events.recent(limit=500)[before:]]
        self.assertIn('items.status_changed', topics)

    # -- 路由存在性（防止以后被 `{item_id}` 吞掉）--------------------------

    def test_three_write_routes_are_registered(self):
        paths = {route.path for route in items_api.router.routes}
        self.assertIn('/api/items/{account_id}/batch-offline', paths)
        self.assertIn('/api/items/{account_id}/batch-delete', paths)
        self.assertIn('/api/items/{account_id}/{item_id}/quick-edit', paths)

    def test_no_write_api_is_ever_called_by_read_paths(self):
        """读路径（列表 / 单条）永远不该碰这四个写接口。"""
        seed_account()
        seed_item('i1')
        self.client.get(f'{BASE}/{ACC}', headers=self.headers)
        self.client.get(f'{BASE}/{ACC}/i1', headers=self.headers)
        self.assertEqual(self.fake.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
