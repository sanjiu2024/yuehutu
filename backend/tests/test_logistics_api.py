# -*- coding: utf-8 -*-
"""物流族（快递公司列表 / 运单号认公司 / 修改运单号）的测试。

**为什么单开一组**：这一族是「发货之后」的物流信息维护，最容易犯的错是：

1. 没确认就发写请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现单号是空的；
3. 把平台没返回的列表键补成 `[]` —— 平台没给 ≠ 一家快递公司都没有；
4. 认不出公司时**猜**一个快递公司（必须给 `null` + warning）；
5. 把登录态判错：平台「无权限」不等于掉线，不该让用户白扫一次码。

**一次真机写请求都没发过**（这会在卖家账号下真的改物流单号）。这里用假协议层钉住
「字段形状与官方 bundle 逐字一致」（偏移见 `backend/app/xianyu/logistics.py` 的常量注释与
`WRITE_APIS['resend']['evidence']`），平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_logistics_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
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

from backend.app.api import logistics as lg_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import logistics as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'lg_acc'
BASE = '/api/logistics'

#: 快递公司列表成功（官方读 `resp.data.items[].{code,name}`）。
COMPANIES_OK = {
    'success': True,
    'data': {'items': [{'code': 'SF', 'name': '顺丰速运'},
                       {'code': 'YTO', 'name': '圆通速递'},
                       {'code': 'OTHER', 'name': '其他'}]},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台明确回了空数组 —— 就是「一家都没有」。
COMPANIES_EMPTY = {
    'success': True, 'data': {'items': []},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台连 `items` 键都没给 —— 不等于「一家都没有」。
COMPANIES_MISSING = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 运单号认公司成功（`unionCode` + `cpName`）。
GUESS_OK = {
    'success': True,
    'data': {'unionCode': 'SF', 'cpName': '顺丰速运'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 认不出公司（平台没回 unionCode）。
GUESS_NO_MATCH = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 修改运单号成功（官方 toast「修改成功」）。
RESEND_OK = {
    'success': True,
    'data': {'code': 'success', 'data': {'cpCode': 'SF'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台收了请求但没回显结果。
RESEND_NO_ECHO = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}
#: 客户端层失败（平台信封为空时只剩它）。
CLIENT_ERROR = {'success': False, 'message': 'FAIL_SYS_ILLEGAL_ACCESS::非法请求',
                'ret': ['FAIL_SYS_ILLEGAL_ACCESS::非法请求']}

WRITE_ROUTES: List[tuple] = [
    ('POST', f'{BASE}/{ACC}/orders/3317095524016013282/resend',
     {'mail_no': 'SF1234567890', 'cp_code': 'SF', 'confirm': True}),
]


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeLogisticsClient:
    """假协议层：只实现 `mtop`，记录每次调用的 (api, data, v)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0') -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v})
        error = self.errors.get(api)
        if error is not None:
            raise error
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有准备 {api} 的响应')
        return self.payloads[api]

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]

    def call_of(self, api: str) -> Dict[str, Any]:
        for call in self.calls:
            if call['api'] == api:
                return call
        raise AssertionError(f'没有调用 {api}，实际调用: {self.apis()}')


def install_fake_protocol(client: FakeLogisticsClient, *, with_factory: bool = True) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeLogisticsClient:
        client.account_id = account_id
        return client

    if with_factory:
        module.get_client = factory  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def full_client(**overrides: Any) -> FakeLogisticsClient:
    payloads = {
        proto.COMPANIES_API: COMPANIES_OK,
        proto.GUESS_API: GUESS_OK,
        proto.RESEND_API: RESEND_OK,
    }
    payloads.update(overrides)
    return FakeLogisticsClient(payloads)


# --------------------------------------------------------------------------- 协议层


class LogisticsProtocolTest(unittest.TestCase):
    """纯函数：接口名、入参形状、确认闸门、单号校验、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.COMPANIES_API, 'com.taobao.idle.logistics.new.get.companies')
        self.assertEqual(proto.GUESS_API, 'mtop.taobao.idle.logistics.guess.mailno')
        self.assertEqual(proto.RESEND_API,
                         'mtop.taobao.idle.logistics.merchant.consign.resend')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_whitelist_has_one_write_and_two_reads(self):
        self.assertEqual(proto.READ_APIS, (proto.COMPANIES_API, proto.GUESS_API))
        self.assertEqual(proto.ACTIONS, ('resend',))
        for action, spec in proto.WRITE_APIS.items():
            self.assertIn(spec['api'], proto.ALL_APIS, action)
            self.assertTrue(spec['evidence'], action)
            self.assertTrue(spec['data'], action)

    def test_assert_write_rejects_read_apis(self):
        self.assertEqual(proto.assert_write(proto.RESEND_API), proto.RESEND_API)
        for api in proto.READ_APIS:
            with self.assertRaises(proto.UnknownApi):
                proto.assert_write(api)

    def test_assert_read_rejects_the_write_api(self):
        for api in proto.READ_APIS:
            self.assertEqual(proto.assert_read(api), api)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_read(proto.RESEND_API)

    def test_companies_request_sends_no_data(self):
        """官方原文 `hO({api, v:"1.0"})` —— 连 data 都没有。"""
        self.assertEqual(proto.companies_request(), (proto.COMPANIES_API, None))

    def test_guess_request_payload_is_verbatim(self):
        self.assertEqual(proto.guess_request(' SF123 '),
                         (proto.GUESS_API, {'mailNo': 'SF123'}))

    def test_guess_request_rejects_blank_mail_no(self):
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                proto.guess_request(bad)

    def test_resend_payload_is_verbatim(self):
        api, data = proto.resend_request('3317', 'SF1234567890', 'SF', confirm=True)
        self.assertEqual(api, proto.RESEND_API)
        self.assertEqual(data, {'tradeId': '3317', 'mailNo': 'SF1234567890', 'cpCode': 'SF'})

    def test_resend_defaults_to_other_company_code(self):
        """官方下拉初值就是 "OTHER"，空串要走默认值。"""
        _, data = proto.resend_request('3317', 'SF1', '', confirm=True)
        self.assertEqual(data['cpCode'], proto.DEFAULT_COMPANY_CODE)
        _, data2 = proto.resend_request('3317', 'SF1', '   ', confirm=True)
        self.assertEqual(data2['cpCode'], 'OTHER')

    def test_resend_requires_order_id_and_mail_no(self):
        with self.assertRaises(ValueError):
            proto.resend_request('', 'SF1', 'SF', confirm=True)
        with self.assertRaises(ValueError):
            proto.resend_request('3317', '  ', 'SF', confirm=True)

    def test_confirm_gate_is_identity_not_truthiness(self):
        """`bool('false')` 是 True ⇒ 只认字面量 True。"""
        for bad in (None, False, 0, '', 'false', 'no', 'yes', 2, 'True'):
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(bad)):
                proto.resend_request('3317', 'SF1', 'SF', confirm=bad)
        self.assertIsNotNone(proto.resend_request('3317', 'SF1', 'SF', confirm=True))

    def test_mail_no_length_guard(self):
        with self.assertRaises(ValueError):
            proto.resend_request('3317', 'X' * (proto.MAIL_NO_MAX + 1), 'SF', confirm=True)

    def test_parse_companies_reads_items(self):
        parsed = proto.parse_companies(COMPANIES_OK)
        self.assertEqual(parsed['found_key'], 'items')
        self.assertEqual(parsed['companies'][0], {'code': 'SF', 'name': '顺丰速运'})

    def test_parse_companies_missing_key_is_none_not_empty(self):
        parsed = proto.parse_companies(COMPANIES_MISSING)
        self.assertIsNone(parsed['companies'])
        self.assertIsNone(parsed['found_key'])
        self.assertEqual(proto.parse_companies(COMPANIES_EMPTY)['companies'], [])

    def test_parse_companies_accepts_fallback_keys(self):
        self.assertEqual(proto.parse_companies({'data': {'list': [{'code': 'SF',
                                                                   'name': '顺丰'}]}})['found_key'],
                         'list')

    def test_parse_companies_keeps_missing_fields_as_none(self):
        parsed = proto.parse_companies({'data': {'items': [{'code': 'SF'}, 'junk']}})
        self.assertEqual(parsed['companies'], [{'code': 'SF', 'name': None}])

    def test_parse_guess_reads_union_code(self):
        self.assertEqual(proto.parse_guess(GUESS_OK), {'cp_code': 'SF', 'cp_name': '顺丰速运'})

    def test_parse_guess_never_invents_a_company(self):
        self.assertEqual(proto.parse_guess(GUESS_NO_MATCH), {'cp_code': None, 'cp_name': None})
        self.assertEqual(proto.parse_guess({}), {'cp_code': None, 'cp_name': None})

    def test_parse_resend_reports_success_and_failure(self):
        self.assertTrue(proto.parse_resend(RESEND_OK)['saved'])
        self.assertEqual(proto.parse_resend(RESEND_OK)['cp_code'], 'SF')
        refused = proto.parse_resend(NO_PERMISSION)
        self.assertFalse(refused['saved'])
        self.assertIn('无权限访问', refused['platform_message'])


# --------------------------------------------------------------------------- 服务 / HTTP


class LogisticsPageTest(unittest.TestCase):
    """共用的脚手架（自挂本模块路由，不依赖 main.py 是否已挂）。"""

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
        self.fake = full_client()
        install_fake_protocol(self.fake)
        app = FastAPI()
        app.include_router(lg_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('logistics','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, headers: Optional[Dict[str, str]] = None):
        return self.client.get(path, headers=self.headers if headers is None else headers)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body or {},
                                headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()['data']

    def login_state(self, account_id: str = ACC) -> str:
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (account_id,))
        return row['login_state'] if row else ''

    def operations(self) -> List[Dict[str, Any]]:
        return db.query('SELECT action, success, detail FROM operation_logs '
                        'WHERE account_id = ? ORDER BY id', (ACC,))

    def topics(self, before: int) -> List[str]:
        from backend.app.core import events
        return [event['topic'] for event in events.recent(limit=500)[before:]]


class LogisticsReadTest(LogisticsPageTest):
    """只读：快递公司列表、运单号认公司、缺键、平台拒绝、凭据与鉴权。"""

    def test_companies_returns_rows_and_calls_without_data(self):
        data = self.client.get(f'{BASE}/{ACC}/companies', headers=self.headers).json()['data']
        self.assertEqual(data['count'], 3)
        self.assertEqual(data['companies'][0], {'code': 'SF', 'name': '顺丰速运'})
        self.assertEqual(data['default_code'], 'OTHER')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(self.fake.calls[0]['data'], None)
        self.assertEqual(self.fake.calls[0]['v'], '1.0')
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])

    def test_companies_missing_key_is_empty_with_warning(self):
        self.fake.payloads[proto.COMPANIES_API] = COMPANIES_MISSING
        data = self.client.get(f'{BASE}/{ACC}/companies', headers=self.headers).json()['data']
        self.assertEqual(data['companies'], [])
        self.assertIsNone(data['found_key'])
        self.assertEqual([w['code'] for w in data['warnings']], ['list_key_missing'])

    def test_companies_empty_list_has_no_warning(self):
        self.fake.payloads[proto.COMPANIES_API] = COMPANIES_EMPTY
        data = self.client.get(f'{BASE}/{ACC}/companies', headers=self.headers).json()['data']
        self.assertEqual(data['companies'], [])
        self.assertEqual(data['warnings'], [])

    def test_guess_returns_the_company(self):
        data = self.client.get(f'{BASE}/{ACC}/guess', params={'mail_no': ' SF123 '},
                               headers=self.headers).json()['data']
        self.assertEqual((data['cp_code'], data['cp_name'], data['matched']),
                         ('SF', '顺丰速运', True))
        self.assertEqual(self.fake.call_of(proto.GUESS_API)['data'], {'mailNo': 'SF123'})
        self.assertEqual(data['warnings'], [])

    def test_guess_without_match_is_null_with_warning(self):
        self.fake.payloads[proto.GUESS_API] = GUESS_NO_MATCH
        data = self.client.get(f'{BASE}/{ACC}/guess', params={'mail_no': 'ZZ1'},
                               headers=self.headers).json()['data']
        self.assertIsNone(data['cp_code'])
        self.assertIsNone(data['cp_name'])
        self.assertFalse(data['matched'])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_company_matched'])

    def test_guess_rejects_blank_mail_no_without_calling_platform(self):
        resp = self.client.get(f'{BASE}/{ACC}/guess', params={'mail_no': '   '},
                               headers=self.headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_platform_refusal_is_502_with_the_platform_text(self):
        self.fake.payloads[proto.COMPANIES_API] = NO_PERMISSION
        resp = self.client.get(f'{BASE}/{ACC}/companies', headers=self.headers)
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.text)
        self.assertEqual(self.login_state(), 'active')  # 无权限 ≠ 掉线

    def test_missing_cookie_is_409_and_sends_nothing(self):
        db.execute('DELETE FROM account_credentials')
        remove_fake_protocol()
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.client.get(f'{BASE}/{ACC}/companies', headers=self.headers)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_and_sends_nothing(self):
        resp = self.client.get(f'{BASE}/nope_acc/companies', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_without_token_is_401(self):
        self.assertEqual(self.client.get(f'{BASE}/{ACC}/companies').status_code, 401)

    def test_protocol_without_mtop_is_503(self):
        module = types.ModuleType('backend.app.xianyu.client')
        module.XianyuClient = type('Bare', (), {})
        sys.modules['backend.app.xianyu.client'] = module
        resp = self.client.get(f'{BASE}/{ACC}/companies', headers=self.headers)
        self.assertEqual(resp.status_code, 503, resp.text)

    def test_routes_are_read_only_for_read_paths(self):
        self.assertEqual(self.post(f'{BASE}/{ACC}/companies').status_code, 405)
        self.assertEqual(self.post(f'{BASE}/{ACC}/guess').status_code, 405)


class LogisticsConfirmGateTest(LogisticsPageTest):
    """确认闸门：未确认时一个平台请求都不许发。"""

    def test_resend_without_confirm_is_400_and_sends_nothing(self):
        for _method, route, body in WRITE_ROUTES:
            resp = self.post(route, {k: v for k, v in body.items() if k != 'confirm'})
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()['code'], 'confirmation_required')
            self.assertEqual(self.fake.calls, [], route)

    def test_false_like_values_are_never_a_nod(self):
        route = WRITE_ROUTES[0][1]
        for bad in ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2):
            resp = self.post(route, {'mail_no': 'SF1', 'cp_code': 'SF', 'confirm': bad})
            self.assertEqual(resp.status_code, 400, f'{bad!r} -> {resp.text}')
            self.assertEqual(resp.json()['code'], 'confirmation_required', repr(bad))
            self.assertEqual(self.fake.calls, [], repr(bad))

    def test_true_confirm_actually_sends(self):
        data = self.ok_data(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertTrue(data['saved'])
        self.assertEqual(self.fake.apis(), [proto.RESEND_API])


class LogisticsValidationTest(LogisticsPageTest):
    """字段校验：纯本地、免费，排在凭据检查**之前**。"""

    def test_blank_mail_no_is_400_without_calling_platform(self):
        resp = self.post(WRITE_ROUTES[0][1],
                         {'mail_no': '  ', 'cp_code': 'SF', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_field_validation_runs_before_credential_check(self):
        """空 Cookie + 空单号 ⇒ 先报字段错（400），而不是先喊「去重新登录」（409）。"""
        db.execute('DELETE FROM account_credentials')
        remove_fake_protocol()
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(WRITE_ROUTES[0][1], {'mail_no': '', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_blank_mail_no_with_credentials_is_409(self):
        db.execute('DELETE FROM account_credentials')
        remove_fake_protocol()
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(WRITE_ROUTES[0][1],
                         {'mail_no': 'SF1', 'cp_code': 'SF', 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)

    def test_unknown_account_is_404(self):
        resp = self.post(f'{BASE}/nope_acc/orders/1/resend',
                         {'mail_no': 'SF1', 'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])


class LogisticsHappyPathTest(LogisticsPageTest):
    """写成功：payload 逐字、日志、事件、默认公司编码。"""

    def test_payload_is_verbatim(self):
        self.ok_data(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        call = self.fake.call_of(proto.RESEND_API)
        self.assertEqual(call['data'], {'tradeId': '3317095524016013282',
                                        'mailNo': 'SF1234567890', 'cpCode': 'SF'})
        self.assertEqual(call['v'], '1.0')

    def test_blank_cp_code_falls_back_to_other(self):
        data = self.ok_data(WRITE_ROUTES[0][1],
                            {'mail_no': 'SF123', 'cp_code': '  ', 'confirm': True})
        self.assertEqual(data['cp_code'], 'OTHER')
        self.assertEqual(self.fake.call_of(proto.RESEND_API)['data']['cpCode'], 'OTHER')

    def test_result_shape_exposes_what_was_submitted(self):
        data = self.ok_data(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertEqual(data['action'], 'resend')
        self.assertEqual(data['order_id'], '3317095524016013282')
        self.assertEqual(data['mail_no'], 'SF1234567890')
        self.assertTrue(data['saved'])
        self.assertEqual(data['submitted']['cpCode'], 'SF')
        self.assertEqual(data['source'], 'platform')

    def test_write_is_logged_and_announced(self):
        from backend.app.core import events
        before = len(events.recent(limit=500))
        self.ok_data(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        rows = self.operations()
        self.assertTrue(any(row['action'] == 'logistics_resend' and row['success']
                            for row in rows), rows)
        self.assertIn('orders.shipment_updated', self.topics(before))

    def test_no_echo_still_counts_as_saved_with_a_note(self):
        """平台信封说成功就是成功（官方也只 toast「修改成功」）；只是没法本地核对待编码。"""
        self.fake.payloads[proto.RESEND_API] = RESEND_NO_ECHO
        data = self.ok_data(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertTrue(data['saved'])
        self.assertFalse(data['echoed'])
        self.assertEqual(data['mail_no'], 'SF1234567890')  # 以提交值为准，不编造回显
        self.assertEqual([w['code'] for w in data['warnings']], ['not_echoed'])
        self.assertTrue(any(row['action'] == 'logistics_resend' for row in self.operations()))


class LogisticsFailureTest(LogisticsPageTest):
    """失败路径：平台拒绝、登录态过期、协议层缺失。"""

    def test_platform_refusal_is_502_and_keeps_login_state(self):
        self.fake.payloads[proto.RESEND_API] = NO_PERMISSION
        resp = self.post(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.text)
        self.assertEqual(self.login_state(), 'active')

    def test_client_level_failure_is_502(self):
        self.fake.payloads[proto.RESEND_API] = CLIENT_ERROR
        resp = self.post(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertEqual(resp.status_code, 502, resp.text)

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.RESEND_API] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        resp = self.post(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_protocol_without_mtop_is_503_on_write(self):
        module = types.ModuleType('backend.app.xianyu.client')
        module.XianyuClient = type('Bare', (), {})
        sys.modules['backend.app.xianyu.client'] = module
        resp = self.post(WRITE_ROUTES[0][1], WRITE_ROUTES[0][2])
        self.assertEqual(resp.status_code, 503, resp.text)


class LogisticsRouteTest(LogisticsPageTest):
    """路由挂载与形状。"""

    def test_three_routes_are_registered(self):
        paths = {}
        for route in lg_api.router.routes:
            paths.setdefault(getattr(route, 'path', ''), set()).update(route.methods)
        self.assertEqual(paths['/api/logistics/{account_id}/companies'], {'GET'})
        self.assertEqual(paths['/api/logistics/{account_id}/guess'], {'GET'})
        self.assertEqual(paths['/api/logistics/{account_id}/orders/{order_id}/resend'],
                         {'POST'})

    def test_module_is_mounted_on_the_real_app(self):
        remove_fake_protocol()
        from backend.app.main import ROUTE_MODULES, create_app
        self.assertIn('logistics', ROUTE_MODULES)
        paths = create_app().openapi()['paths']
        self.assertIn('/api/logistics/{account_id}/companies', paths)
        self.assertIn('/api/logistics/{account_id}/guess', paths)
        self.assertIn('/api/logistics/{account_id}/orders/{order_id}/resend', paths)


if __name__ == '__main__':
    unittest.main(verbosity=2)
