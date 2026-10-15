"""商品读 5 条接口的**服务层 + HTTP 契约**测试（第 63 批 task-23）。

**为什么单开一组**（与协议层那组分工：那组管「发了什么、解析出了什么」，这组管
「路由/鉴权/信封/状态码」）：

1. 5 条 GET 必须在 `create_app().openapi()['paths']` 里查得到（本批的验收判据）；
2. 只读：POST/PUT/PATCH/DELETE 一律 405；
3. 没 Cookie / 登录态过期 → 409 `verification_required`，**一次平台都不打**；
4. 协议层缺失 → 503 `xianyu_not_ready`（不是 200 + 空列表，也不是 500）；
5. 平台业务码（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）是**平台的有效答复** → 仍是 200，
   带 `platform_code` / `platform_message` 原样透出；链路坏了才是 502 `protocol_error`。

跑法（仓库根目录）：
    XIANYU_APP_DIR=$TEMP/xywb_kurumi63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_items_read_api.py

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import items_read as api_module  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import items_read as service  # noqa: E402
from backend.app.xianyu import items_read as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'items_read_acc'
BASE = '/api/items-read'

#: 真机 / bundle 形状（bundle @3071624：`data.data.itemSearchResponseList` + `total`）
SEARCH_OK = {
    'code': 'success',
    'data': {'itemSearchResponseList': [
        {'itemId': '777888999', 'title': 'iPhone 15 保护壳', 'price': '19.90'}],
        'total': 37},
}
TERMS_OK = {'code': 'success', 'data': {
    'returnService': [{'key': '7', 'value': '7天无理由退货'}],
    'sendGoodsTime': [{'key': '24', 'value': '24小时内发货'}],
    'serviceGuarantee': []}}
PREGET_OK = {'code': 'success', 'data': {'needUpFirstHandItem': True,
                                         'industryId': '50023858'}}
CATEGORY_OK = {'code': 'success', 'data': {'selectIdleCategories': [
    {'categoryId': '1', 'categoryName': '手机'}]}}
REPORT_OK = {'code': 'success', 'data': {'reportId': 'r1'}}
#: 真机直调（缺站点 CODE 头）时平台回的业务码
UNAUTHORIZED = {'code': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED', 'msg': '无权限访问', 'data': None}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeItemsClient:
    """假协议层：只实现 `mtop`，按 api 名回话，记录每次调用。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
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


def install_fake_protocol(client: FakeItemsClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeItemsClient:
        client.account_id = account_id
        return client

    module.get_client = factory  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)
    # 只 pop `sys.modules` 不够：`from ..xianyu import client` 会把子模块**挂到父包的
    # 属性上**，那个属性还指着假模块 —— `main` 里 finance 在 import 期就要 client 的
    # 常量，拿到假模块直接 AttributeError（本轮真踩过）。
    package = sys.modules.get('backend.app.xianyu')
    if package is not None and 'client' in vars(package):
        delattr(package, 'client')


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


class ItemsReadApiTest(unittest.TestCase):
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
        for table in ('accounts', 'account_credentials', 'operation_logs'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeItemsClient(payloads={
            proto.SEARCH_API: SEARCH_OK,
            proto.SEARCH_TERM_API: TERMS_OK,
            proto.PREGET_API: PREGET_OK,
            proto.CATEGORY_API: CATEGORY_OK,
            proto.OPERATE_REPORT_API: REPORT_OK,
        })
        install_fake_protocol(self.fake)
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已挂上
        app = FastAPI()
        app.include_router(api_module.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('ir63','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = '', headers: Optional[Dict[str, str]] = None):
        # 注意别写 `headers or self.headers`：`{}` 是 falsy，会把「不带令牌」的用例
        # 悄悄换成带令牌，401 用例永远绿（本轮真踩过）。
        return self.client.get(f'{path}{query}',
                               headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def fail_body(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 路由登记（本批验收判据） ----------------------------------------

    def test_five_routes_are_in_the_real_openapi(self):
        """5 条 GET 在 `create_app().openapi()['paths']` 里查得到。"""
        # main 会把**所有**路由模块 import 一遍（而且 `import main` 那一刻就已经
        # `app = create_app()` 了），finance 等模块在 import 期就要真 client 的常量
        # —— 必须**先**摘掉假模块，再 import main。
        remove_fake_protocol()
        try:
            from backend.app.main import create_app
            paths = create_app().openapi()['paths']
        finally:
            install_fake_protocol(self.fake)
        expected = {
            f'{BASE}/{{account_id}}/search',
            f'{BASE}/{{account_id}}/categories',
            f'{BASE}/{{account_id}}/operate-report',
            f'{BASE}/{{account_id}}/search-terms',
            f'{BASE}/{{account_id}}/preget',
        }
        missing = sorted(path for path in expected if path not in paths)
        self.assertEqual(missing, [], f'缺路由: {missing}')
        for path in sorted(expected):
            self.assertIn('get', paths[path], path)

    def test_routes_are_read_only(self):
        """只读批次：写方法一律 405。"""
        for suffix in ('search', 'categories', 'operate-report', 'search-terms', 'preget'):
            path = f'{BASE}/{ACC}/{suffix}'
            for method in ('post', 'put', 'patch', 'delete'):
                resp = getattr(self.client, method)(path, headers=self.headers)
                self.assertEqual(resp.status_code, 405, f'{method} {path} → {resp.status_code}')

    def test_routes_require_authentication(self):
        seed_account()
        for suffix in ('search', 'categories', 'operate-report', 'search-terms', 'preget'):
            resp = self.get(f'{BASE}/{ACC}/{suffix}', headers={})
            self.assertEqual(resp.status_code, 401, suffix)

    # -- 商品列表 search -------------------------------------------------

    def test_search_returns_rows_and_the_exact_request(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/search', '?keyword=壳&page=2&size=10')
        self.assertEqual(data['account_id'], ACC)
        self.assertEqual(data['kind'], 'search_items')
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['api'], proto.SEARCH_API)
        self.assertEqual(data['platform_code'], 'success')
        self.assertEqual(data['found_key'], 'itemSearchResponseList')
        self.assertEqual(data['total'], 37)
        self.assertEqual(data['rows'][0]['itemId'], '777888999')
        self.assertIn('fetched_at', data)
        sent = self.fake.calls[0]['data']
        self.assertEqual(sent['pageNo'], 2)
        self.assertEqual(sent['pageSize'], 10)
        self.assertEqual(sent['bizType'], 'commonPro')
        self.assertEqual(sent['itemStatus'], '0,-9')
        self.assertEqual(sent['searchRequest'], '{"title": "壳"}')

    def test_search_defaults_match_the_official_page(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/search')
        self.assertEqual(self.fake.calls[0]['data']['pageNo'], 1)
        self.assertEqual(self.fake.calls[0]['data']['pageSize'], 20)
        self.assertEqual(data['sent']['searchRequest'], '{}')

    def test_search_missing_total_is_none_not_zero(self):
        seed_account()
        self.fake.payloads[proto.SEARCH_API] = {'code': 'success', 'data': {'total': None}}
        data = self.ok_data(f'{BASE}/{ACC}/search')
        self.assertIsNone(data['total'])
        self.assertEqual(data['rows'], [])

    def test_search_rejects_out_of_range_paging(self):
        """越界在建客户端之前拦下：422（Query 边界）——**一次平台都不打**。"""
        seed_account()
        for query in ('?page=0', '?size=0', '?size=99'):
            resp = self.get(f'{BASE}/{ACC}/search', query)
            self.assertEqual(resp.status_code, 422, query + resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_service_validates_paging_before_building_a_client(self):
        """服务层自己也守一份边界（MCP 之外还有别的调用方）→ 400 `invalid_fields`。"""
        result = service.search_items(ACC, page=0)
        self.assertEqual(result['code'], 'invalid_fields')
        result = service.search_items(ACC, size=99)
        self.assertEqual(result['code'], 'invalid_fields')
        result = service.search_items(ACC, page='x')
        self.assertEqual(result['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_search_rejects_a_lonely_order_direction(self):
        seed_account()
        body = self.fail_body(f'{BASE}/{ACC}/search', '?order_type=desc')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_search_rejects_unknown_order_direction(self):
        seed_account()
        body = self.fail_body(f'{BASE}/{ACC}/search', '?order_field=price&order_type=sideways')
        self.assertEqual(body['code'], 'invalid_fields')

    def test_search_forwards_order_and_raw_search_request(self):
        seed_account()
        raw = '{"keyword":"壳"}'
        self.ok_data(f'{BASE}/{ACC}/search',
                     '?order_field=price&order_type=DESC&search_request=' + quote(raw))
        sent = self.fake.calls[0]['data']
        self.assertEqual(sent['orderField'], 'price')
        self.assertEqual(sent['orderType'], 'desc')
        self.assertEqual(sent['searchRequest'], raw)

    def test_platform_business_code_is_200_not_502(self):
        """平台说「无权限」= 链路通、平台有效答复 → 200 + platform_code 原样透出。"""
        seed_account()
        self.fake.payloads[proto.SEARCH_API] = UNAUTHORIZED
        data = self.ok_data(f'{BASE}/{ACC}/search')
        self.assertEqual(data['platform_code'], 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(data['platform_message'], '无权限访问')
        self.assertEqual(data['rows'], [])

    # -- 筛选维度 search-terms -------------------------------------------

    def test_search_terms_returns_the_three_groups(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/search-terms')
        self.assertEqual(data['kind'], 'search_terms')
        self.assertEqual(data['api'], proto.SEARCH_TERM_API)
        self.assertEqual(data['terms']['returnService'],
                         [{'key': '7', 'value': '7天无理由退货'}])
        self.assertEqual(data['terms']['serviceGuarantee'], [])
        self.assertEqual(data['found_groups'], ['returnService', 'sendGoodsTime'])
        self.assertIsNone(self.fake.calls[0]['data'])

    # -- 预请求 preget ---------------------------------------------------

    def test_preget_defaults_to_first_hand_publish(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/preget', '?item_id=777&source_id=PC')
        self.assertEqual(data['kind'], 'preget')
        self.assertIs(data['need_up_first_hand_item'], True)
        self.assertEqual(data['industry_id'], '50023858')
        self.assertEqual(self.fake.calls[0]['data'], {
            'itemId': '777', 'sourceId': 'PC', 'publishScene': 'firstHandPublish'})

    # -- 透传的两条 ------------------------------------------------------

    def test_categories_and_report_pass_params_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/categories', '?params=%7B%22categoryId%22%3A%221%22%7D')
        self.assertEqual(data['kind'], 'category_query')
        self.assertEqual(data['sent'], {'categoryId': '1'})
        self.assertEqual(data['data'], CATEGORY_OK['data'])
        data = self.ok_data(f'{BASE}/{ACC}/operate-report', '?params=%7B%22itemId%22%3A%227%22%7D')
        self.assertEqual(data['kind'], 'operate_report')
        self.assertEqual(self.fake.calls[-1]['data'], {'itemId': '7'})
        self.assertEqual(data['data'], REPORT_OK['data'])

    def test_bad_params_json_is_400(self):
        seed_account()
        for params in ('%7Bnot-json', '%5B1%2C2%5D'):
            body = self.fail_body(f'{BASE}/{ACC}/categories', f'?params={params}')
            self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    # -- 鉴权 / 前置检查 -------------------------------------------------

    def test_missing_cookie_is_409_and_never_touches_the_platform(self):
        seed_account(cookie='')
        body = self.fail_body(f'{BASE}/{ACC}/search', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_need_relogin_state_is_409(self):
        seed_account(login_state='need_relogin')
        body = self.fail_body(f'{BASE}/{ACC}/search-terms', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail_body(f'{BASE}/{ACC}/preget', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_blank_account_id_is_400(self):
        seed_account()
        result = service.search_items('')
        self.assertEqual(result['code'], 'bad_request')

    # -- 协议层故障 ------------------------------------------------------

    def test_protocol_unavailable_is_503_not_ready(self):
        """协议模块缺失 → 503 `xianyu_not_ready`，**绝不能**当成「一件商品都没有」。"""
        seed_account()
        sys.modules['backend.app.xianyu.items_read'] = None
        try:
            body = self.fail_body(f'{BASE}/{ACC}/search', status=503)
            self.assertEqual(body['code'], 'xianyu_not_ready')
            self.assertEqual(self.fake.calls, [])
        finally:
            sys.modules.pop('backend.app.xianyu.items_read', None)

    def test_protocol_exception_is_502(self):
        seed_account()
        self.fake.errors[proto.SEARCH_API] = RuntimeError('连接被重置')
        body = self.fail_body(f'{BASE}/{ACC}/search', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('连接被重置', body['message'])

    def test_session_expired_is_409_and_writes_need_relogin(self):
        """协议层抛登录态过期 → 409 + `need_relogin`，并且**写回库**（靠 _BoundProtocol）。"""
        seed_account()
        self.fake.errors[proto.SEARCH_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED')
        body = self.fail_body(f'{BASE}/{ACC}/search', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_failure_is_logged(self):
        seed_account()
        self.fake.errors[proto.SEARCH_TERM_API] = RuntimeError('boom')
        self.fail_body(f'{BASE}/{ACC}/search-terms', status=502)
        row = db.query_one('SELECT action, success FROM operation_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(row)
        self.assertEqual(row['action'], 'search_terms')
        self.assertEqual(int(row['success']), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
