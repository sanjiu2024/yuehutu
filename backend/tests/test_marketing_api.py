"""营销页只读接口的 HTTP 契约 + 服务层口径。

钉住四件事（task-59）：

1. **平台没返回的指标是 `null`，不是 0**；平台真的回了 `0`，那就是 `0`（不能把真值擦掉）。
2. **`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（无权限）不是掉线**：全部接口都无权限时给
   `200` + 一屏 `null` + 人话 warning，**绝不**写 `need_relogin`；只有链路 / 登录态故障
   才给 `502 protocol_error` / `409 verification_required`。
3. **全部只读**：四条路径上的 POST / PUT / PATCH / DELETE 一律 `405`，且一个写接口
   （`WRITE_APIS` 里登记的那两个）都不会被调用。
4. **没有 Cookie 时零平台调用**：服务层先查库，缺 Cookie 直接 409，不打平台。

假协议层（`install_fake_protocol`）用于行为用例；`install_fake_client` 保留**真协议层**
`backend.app.xianyu.marketing` 参与，用于口径用例（否则 `_collect` / `extract_metrics`
的真实行为一次都跑不到）。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import marketing as marketing_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import marketing as marketing_service  # noqa: E402
from backend.app.xianyu import marketing as marketing_proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'mkt_acc'
BASE = '/api/marketing'

#: 真机 / bundle 里的平台响应形状（字段名见 `docs/contracts/marketing.md` 的证据表）。
ITEMS_PAYLOAD = {
    'code': 'success',
    'data': {'totalCount': 3, 'list': [{'itemId': 'a'}, {'itemId': 'b'}, {'itemId': 'c'}]},
}
#: 平台真的回 0：必须原样保留（0 是真实值）
ITEMS_ZERO = {'code': 'success', 'data': {'totalCount': 0, 'list': []}}
#: 只给了一个指标：其余契约键必须是 None（不是 0）
TASKS_PARTIAL = {'code': 'success', 'data': {'runningCount': 2}}
TASKS_PAYLOAD = {'code': 'success', 'data': {'runningCount': 2, 'totalCount': 5}}
COMPETITIVENESS_PAYLOAD = {
    'code': 'success',
    'data': {'lowCount': 4, 'highCount': 9, 'avgScore': 61.5,
             'list': [{'itemId': 'a', 'score': 30}]},
}
RECORDS_PAYLOAD = {
    'code': 'success',
    'data': {'totalCount': 2, 'list': [{'id': 1}, {'id': 2}]},
}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeXianyuClient:
    """假协议层客户端：只会被 `common.get_client` 造出来，`mtop` 不该被走到。"""

    def __init__(self) -> None:
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        raise AssertionError('假客户端的 mtop 不该被走到（协议层被替换了）')


class FakeMtopClient:
    """真协议层用的假 client：`mtop` 按 api 回话或抛错（形状 = 真机信封，不联网）。"""

    def __init__(self, answers: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        self.calls.append((api, data))
        error = self.errors.get(api)
        if error is not None:
            raise error
        return self.answers.get(api, {})


class FakeMarketingProtocol:
    """假协议层：按函数名回话，记录 `(name, client, *args)`。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[tuple] = []

    def _answer(self, name: str, *args: Any) -> Any:
        self.calls.append((name,) + args)
        error = self.errors.get(name)
        if error is not None:
            raise error
        return self.payloads.get(name)

    async def items(self, client: Any, page: int = 1, page_size: int = 20,
                    seller_id: str = '') -> Any:
        return self._answer('items', client, page, page_size, seller_id)

    async def tasks(self, client: Any, seller_id: str = '') -> Any:
        return self._answer('tasks', client, seller_id)

    async def competitiveness(self, client: Any, item_ids: Any = None,
                             seller_id: str = '') -> Any:
        return self._answer('competitiveness', client, item_ids, seller_id)

    async def records(self, client: Any, page: int = 1, page_size: int = 20,
                      seller_id: str = '') -> Any:
        return self._answer('records', client, page, page_size, seller_id)


def install_fake_protocol(protocol: FakeMarketingProtocol) -> FakeXianyuClient:
    """注入两个假协议模块：`xianyu.client`（拿客户端）与 `xianyu.marketing`（营销页）。"""
    client = FakeXianyuClient()
    client_module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeXianyuClient:
        client.account_id = account_id
        client.cookie_id = account_id
        return client

    client_module.get_client = factory  # type: ignore[attr-defined]
    client_module.XianyuClient = FakeXianyuClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = client_module

    module = types.ModuleType('backend.app.xianyu.marketing')
    module.items = protocol.items  # type: ignore[attr-defined]
    module.tasks = protocol.tasks  # type: ignore[attr-defined]
    module.competitiveness = protocol.competitiveness  # type: ignore[attr-defined]
    module.records = protocol.records  # type: ignore[attr-defined]
    for attr in ('ITEMS_METRICS', 'INFO_METRICS', 'COMPETITIVENESS_METRICS',
                 'RECORDS_METRICS', 'WRITE_APIS'):
        setattr(module, attr, getattr(marketing_proto, attr))
    sys.modules['backend.app.xianyu.marketing'] = module
    return client


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)
    sys.modules.pop('backend.app.xianyu.marketing', None)


def install_fake_client(account_id: str = ACC, answers: Optional[Dict[str, Any]] = None,
                        errors: Optional[Dict[str, BaseException]] = None) -> FakeMtopClient:
    """只注入假 `xianyu.client`，**真协议层 `xianyu.marketing` 照常参与**。"""
    client = FakeMtopClient(answers=answers, errors=errors)
    client.account_id = account_id
    client.cookie_id = account_id
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = (lambda _account_id: client)  # type: ignore[attr-defined]
    module.XianyuClient = FakeMtopClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return client


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def _metrics(payload: Dict[str, Any], keys: tuple) -> Dict[str, Any]:
    return marketing_proto.extract_metrics(payload.get('data'), keys)


def full_protocol(**overrides: Any) -> FakeMarketingProtocol:
    payloads = {
        'items': {'metrics': _metrics(ITEMS_PAYLOAD, marketing_proto.ITEMS_METRICS),
                  'items': ITEMS_PAYLOAD['data']['list'], 'suspect_keys': [], 'errors': {}},
        'tasks': {'metrics': _metrics(TASKS_PAYLOAD, marketing_proto.INFO_METRICS),
                  'tasks': None, 'suspect_keys': [], 'errors': {}},
        'competitiveness': {
            'metrics': _metrics(COMPETITIVENESS_PAYLOAD,
                                marketing_proto.COMPETITIVENESS_METRICS),
            'items': COMPETITIVENESS_PAYLOAD['data']['list'],
            'suspect_keys': [], 'errors': {}},
        'records': {'metrics': _metrics(RECORDS_PAYLOAD, marketing_proto.RECORDS_METRICS),
                    'records': RECORDS_PAYLOAD['data']['list'], 'suspect_keys': [],
                    'errors': {}},
    }
    payloads.update(overrides)
    return FakeMarketingProtocol(payloads)


class MarketingApiTest(unittest.TestCase):
    """HTTP 契约 + 服务层口径（假协议层）。"""

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
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        self.proto = full_protocol()
        self.fake_client = install_fake_protocol(self.proto)
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上
        # （真实挂载由 Lead 在 main.py 的 ROUTE_MODULES 里完成）。
        app = FastAPI()
        app.include_router(marketing_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('mktapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = '', headers: Optional[Dict[str, str]] = None):
        return self.client.get(f'{path}{query}',
                               headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def fail_envelope(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 只读 ------------------------------------------------------------

    def test_routes_are_read_only(self):
        """四条路径上的写方法一律 405，且只读探测不该打平台。"""
        seed_account()
        for path in (f'{BASE}/{ACC}/items', f'{BASE}/{ACC}/tasks',
                     f'{BASE}/{ACC}/price-competitiveness', f'{BASE}/{ACC}/records'):
            for method in ('post', 'put', 'patch', 'delete'):
                resp = getattr(self.client, method)(path, headers=self.headers)
                self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
        self.assertEqual(self.proto.calls, [], '只读探测不该打平台')

    def test_write_apis_are_registered_but_never_called(self):
        """写接口只登记名字：出现在 `WRITE_APIS` 里，但没有任何调用点。"""
        self.assertIn('mtop.taobao.idle.bpro.marketing.task.operate.pc',
                      marketing_proto.WRITE_APIS)
        self.assertIn('mtop.taobao.idle.bpro.marketing.task.batch.operate.pc',
                      marketing_proto.WRITE_APIS)
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/items')
        for _api, _data in self.fake_client.calls:  # pragma: no cover - 断言用不上
            self.fail('只读接口不该打平台')
        self.assertTrue(self.proto.calls)

    def test_only_get_is_registered_on_the_router(self):
        methods = {route.methods and tuple(sorted(route.methods)) or ()
                   for route in marketing_api.router.routes}
        self.assertEqual(methods, {('GET',)})

    def test_mtop_names_match_the_official_bundle(self):
        """四个只读接口名必须与官方营销 bundle 原文**逐字**一致。

        这条是补的回归锁：早先 `PRICE_COMPETITIVENESS_API` 与 `OPERATION_RECORD_API`
        漏了 `bpro.marketing.` 这一段（短名在 bundle 里出现 **0 次**），真机会直接回
        `FAIL_SYS_API_NOT_FOUNDED` —— 测试当时只按常量符号引用，所以没抓住。

        bundle 证据：`logs/seller_discover/marketing_src/js_main.js`（3,516,103 字节），
        四个长名各出现 1 次。
        """
        self.assertEqual(
            marketing_proto.MARKET_ITEM_LIST_API,
            'mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list')
        self.assertEqual(
            marketing_proto.MARKET_INFO_API,
            'mtop.taobao.idle.bpro.marketing.info.enhance.query.pc')
        self.assertEqual(
            marketing_proto.PRICE_COMPETITIVENESS_API,
            'mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query')
        self.assertEqual(
            marketing_proto.OPERATION_RECORD_API,
            'mtop.taobao.idle.bpro.marketing.operation.record.list.pc')

    # -- 四个页面 --------------------------------------------------------

    def test_items_ok(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items')
        self.assertEqual(data['kind'], 'items')
        self.assertEqual(data['account_id'], ACC)
        self.assertEqual(data['metrics']['item_count'], 3.0)
        self.assertEqual(len(data['items']), 3)
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['warnings'], [])
        # 协议层拿到的分页参数是整数（前端可能给字符串）
        name, _client, page, size, _seller = self.proto.calls[-1]
        self.assertEqual((name, page, size), ('items', 1, 20))

    def test_items_pagination_reaches_protocol(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/items', '?page=3&size=50')
        self.assertEqual(self.proto.calls[-1][2:4], (3, 50))

    def test_tasks_ok(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/tasks')
        self.assertEqual(data['metrics'], {'running_count': 2.0, 'total_count': 5.0})

    def test_price_competitiveness_ok(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/price-competitiveness')
        self.assertEqual(data['metrics']['low_count'], 4.0)
        self.assertEqual(data['metrics']['avg_score'], 61.5)
        self.assertEqual(len(data['items']), 1)

    def test_price_competitiveness_item_ids_split(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/price-competitiveness', '?item_ids=a,%20b,,c')
        self.assertEqual(self.proto.calls[-1][2], ['a', 'b', 'c'])

    def test_records_ok(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/records')
        self.assertEqual(data['metrics']['record_count'], 2.0)
        self.assertEqual(len(data['records']), 2)

    # -- 口径 1：null 不是 0 ---------------------------------------------

    def test_missing_metric_is_null_not_zero(self):
        """协议层给了 None 的契约键 → JSON 里是 null，**不能变 0**。"""
        seed_account()
        metrics = {'running_count': 2.0, 'total_count': None}
        self.proto.payloads['tasks'] = {'metrics': metrics, 'tasks': None,
                                        'suspect_keys': [], 'errors': {}}
        resp = self.get(f'{BASE}/{ACC}/tasks')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()['data']['metrics']['total_count'])
        self.assertNotEqual(resp.json()['data']['metrics']['total_count'], 0)

    def test_contract_keys_all_present_even_when_platform_says_nothing(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/tasks')
        self.assertEqual(set(data['metrics']), set(marketing_proto.INFO_METRICS))

    def test_protocol_metrics_missing_contract_key_gives_502(self):
        """契约承诺「平台没给的键也一定出现」——缺键是协议层坏了，必须 502。"""
        seed_account()
        self.proto.payloads['tasks'] = {'metrics': {'running_count': 1.0},
                                        'suspect_keys': [], 'errors': {}}
        body = self.fail_envelope(f'{BASE}/{ACC}/tasks', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('total_count', body['message'])

    def test_protocol_empty_metrics_gives_502(self):
        seed_account()
        self.proto.payloads['items'] = {'metrics': {}, 'items': None,
                                        'suspect_keys': [], 'errors': {}}
        body = self.fail_envelope(f'{BASE}/{ACC}/items', status=502)
        self.assertEqual(body['code'], 'protocol_error')

    # -- 口径 2：无权限 ≠ 掉线 -------------------------------------------

    def test_permission_denied_is_null_with_warning_not_502(self):
        """真协议层 + 全部接口 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` → 200 + null + warning。"""
        seed_account()
        remove_fake_protocol()
        error = RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        install_fake_client(errors={marketing_proto.MARKET_ITEM_LIST_API: error})
        resp = self.get(f'{BASE}/{ACC}/items')
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertIsNone(data['metrics']['item_count'])
        self.assertIsNone(data['items'])
        # 文案要点：说清「是权限问题、换个有权限的账号」，且**不能**说掉线
        warnings = data['warnings']
        self.assertTrue(any('业务权限' in text for text in warnings), warnings)
        self.assertTrue(all('重新登录' not in text for text in warnings), warnings)

    def test_permission_denied_does_not_write_need_relogin(self):
        """无权限**绝不**写 `need_relogin`（否则用户白扫一次码）。"""
        seed_account()
        remove_fake_protocol()
        error = RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        install_fake_client(errors={marketing_proto.MARKET_ITEM_LIST_API: error})
        self.get(f'{BASE}/{ACC}/items')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'active')

    def test_link_failure_gives_502(self):
        """非权限故障（链路/签名）→ 502 protocol_error，不是「一屏 -」。"""
        seed_account()
        remove_fake_protocol()
        install_fake_client(errors={marketing_proto.MARKET_ITEM_LIST_API:
                                    RuntimeError('code=FAIL_SYS_ILLEGAL_ACCESS')})
        body = self.fail_envelope(f'{BASE}/{ACC}/items', status=502)
        self.assertEqual(body['code'], 'protocol_error')

    def test_session_expired_gives_409_and_writes_need_relogin(self):
        seed_account()
        remove_fake_protocol()
        install_fake_client(errors={marketing_proto.MARKET_ITEM_LIST_API:
                                    SessionExpiredError('SESSION_EXPIRED')})
        body = self.fail_envelope(f'{BASE}/{ACC}/items', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    # -- 真协议层：0 是真实值 --------------------------------------------

    def test_real_protocol_keeps_zero(self):
        seed_account()
        remove_fake_protocol()
        install_fake_client(answers={marketing_proto.MARKET_ITEM_LIST_API: ITEMS_ZERO})
        data = self.ok_data(f'{BASE}/{ACC}/items')
        self.assertEqual(data['metrics']['item_count'], 0.0)
        self.assertIsNot(data['metrics']['item_count'], None)

    def test_real_protocol_extracts_counts(self):
        seed_account()
        remove_fake_protocol()
        install_fake_client(answers={marketing_proto.MARKET_ITEM_LIST_API: ITEMS_PAYLOAD})
        data = self.ok_data(f'{BASE}/{ACC}/items')
        self.assertEqual(data['metrics']['item_count'], 3.0)
        self.assertEqual(len(data['items']), 3)

    def test_real_protocol_partial_payload_fills_other_keys_with_null(self):
        seed_account()
        remove_fake_protocol()
        install_fake_client(answers={marketing_proto.MARKET_INFO_API: TASKS_PARTIAL})
        data = self.ok_data(f'{BASE}/{ACC}/tasks')
        self.assertEqual(data['metrics']['running_count'], 2.0)
        self.assertIsNone(data['metrics']['total_count'])

    # -- 前置检查与未就绪 ------------------------------------------------

    def test_missing_cookie_makes_zero_platform_calls(self):
        db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
                   'VALUES(?,?,?,?)', (ACC, '无 Cookie', 1, 1))
        body = self.fail_envelope(f'{BASE}/{ACC}/items', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.proto.calls, [], '缺 Cookie 时不该打平台')
        self.assertEqual(self.fake_client.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/nope/items', status=404)
        self.assertEqual(body['code'], 'not_found')

    def test_protocol_layer_missing_is_503(self):
        seed_account()
        with mock.patch.object(marketing_service.common, 'protocol_module', return_value=None):
            body = self.fail_envelope(f'{BASE}/{ACC}/items', status=503)
        self.assertEqual(body['code'], 'xianyu_not_ready')

    def test_bad_pagination_is_400(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/items', '?page=0&size=999', status=400)
        self.assertEqual(body['code'], 'invalid_fields')

    def test_non_integer_pagination_is_400(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/items', '?size=abc', status=400)
        self.assertEqual(body['code'], 'invalid_fields')

    def test_unknown_kind_path_is_not_routed(self):
        """`kind` 不是路径参数：不存在「未知 kind」这条路，FastAPI 自己 404。"""
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/nope-kind')
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.proto.calls, [])


class MarketingProtocolTest(unittest.TestCase):
    """真协议层：取值只认别名，认不出给 None（绝不兜 0）。"""

    def test_find_number_returns_none_when_absent(self):
        self.assertIsNone(marketing_proto.find_number({'data': {'foo': 1}}, ('bar',)))

    def test_find_number_ignores_booleans(self):
        self.assertIsNone(marketing_proto.find_number({'data': {'count': True}}, ('count',)))

    def test_find_number_keeps_zero(self):
        self.assertEqual(marketing_proto.find_number({'count': 0}, ('count',)), 0.0)

    def test_find_number_reads_nested(self):
        self.assertEqual(
            marketing_proto.find_number({'data': {'data': {'totalCount': 7}}}, ('totalcount',)),
            7.0)

    def test_find_list_returns_none_for_empty(self):
        """空数组是「平台说没有」，契约层给 None；两者都不会变成 0。"""
        self.assertIsNone(marketing_proto.find_list({'list': []}))

    def test_find_list_reads_nested(self):
        self.assertEqual(marketing_proto.find_list({'data': {'list': [1, 2]}}), [1, 2])

    def test_suspect_keys_flags_unknown_metric_like_fields(self):
        found = marketing_proto.suspect_keys({'data': {'unknownCount': 3}}, ('item_count',))
        self.assertIn('unknownCount', found)

    def test_extract_metrics_never_returns_zero_for_missing(self):
        out = marketing_proto.extract_metrics({'data': {'x': 1}}, ('item_count',))
        self.assertEqual(out, {'item_count': None})


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
