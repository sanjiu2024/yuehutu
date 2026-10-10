# -*- coding: utf-8 -*-
"""安全中心写接口测试：申诉表单获取 / 提交申诉 / 催办。

**不打真实平台**：用 `FakeSecurityClient` 顶掉 `backend.app.xianyu.client`
（`services.trade._client` 是惰性 import，替换 `sys.modules` 才生效）。

重点断言：
1. 三个写路由未确认 → `confirmation_required`，且**一个平台请求都没发**；
2. 站点 CODE 头 `idle_site_biz_code: COMMONPRO` 真的发出去了（安全中心不带会被判无权限）；
3. 平台没返回的字段是 `None`，不是 `0`/`{}`；
4. 无权限（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）不是掉线，登录态仍是 `active`。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import security_write as security_write_api
from backend.app.core import db, security
from backend.app.xianyu import security_write as proto
from backend.tests.testenv import use_temp_app_dir

ACC = 'acc-sw-1'

FORM_OK = {
    'success': True,
    'data': {'formData': {'formId': 'F-1', 'questions': []}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}
FORM_NO_DATA = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功'}
SUBMIT_OK = {'success': True, 'data': {'appealId': 'A-9527'}, 'message': 'SUCCESS::调用成功'}
SUBMIT_NO_APPEAL_ID = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功'}
URGE_OK = {'success': True, 'data': {'ok': True}, 'message': 'SUCCESS::调用成功'}
URGE_NO_DATA = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功'}
NO_PERMISSION = {
    'success': False,
    'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
    'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'],
}
SYSTEM_ERROR = {
    'success': False,
    'message': 'SYSTEM_ERROR::系统繁忙，请稍后重试',
    'ret': ['SYSTEM_ERROR::系统繁忙，请稍后重试'],
}


def _field(row: Any, name: str, index: int) -> Any:
    """sqlite 行可能是 `Row`（按名取）也可能是 tuple（按下标取）。"""
    try:
        return row[name]
    except (TypeError, IndexError, KeyError):
        return row[index]


class FakeSecurityClient:
    """假协议层：记录每次 mtop 调用，按需返回预置信封。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.payloads: Dict[str, Any] = dict(payloads or {})
        self.errors: Dict[str, BaseException] = dict(errors or {})
        self.calls: List[Dict[str, Any]] = []
        self.account_id = ''

    @staticmethod
    def _security_headers() -> Dict[str, str]:
        return {
            'idle_site_biz_code': 'COMMONPRO',
            'origin': 'https://seller.goofish.com',
            'referer': 'https://seller.goofish.com/',
        }

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   extra_headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v, 'extra_headers': extra_headers})
        if api in self.errors:
            raise self.errors[api]
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有准备 {api} 的响应')
        return self.payloads[api]

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]

    def call_of(self, api: str) -> Optional[Dict[str, Any]]:
        for call in self.calls:
            if call['api'] == api:
                return call
        return None


def install_fake_protocol(client: FakeSecurityClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeSecurityClient:
        client.account_id = account_id
        return client

    module.get_client = factory  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


class SecurityWriteApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        db.reset_connection()
        remove_fake_protocol()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeSecurityClient()
        install_fake_protocol(self.fake)
        app = FastAPI()
        app.include_router(security_write_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('secwrite', 'x', 1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        self.seed_account()

    def tearDown(self) -> None:
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # ------------------------------------------------------------------ 工具

    def seed_account(self, account_id: str = ACC, cookie: str = 'cookie-ok',
                     login_state: str = 'active') -> None:
        db.execute(
            'INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
            f"VALUES('{account_id}', '安全中心测试', 1, 0)")
        db.execute(
            'INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
            f"VALUES('{account_id}', '{cookie}', '{login_state}')")

    def stub(self, api: str, payload: Any) -> None:
        self.fake.payloads[api] = payload

    def post(self, path: str, body: Optional[Dict[str, Any]] = None):
        return self.client.post(path, json=body if body is not None else {}, headers=self.headers)

    def ok_data(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        data = payload.get('data')
        return data if isinstance(data, dict) else payload

    def fail(self, path: str, body: Optional[Dict[str, Any]] = None,
             status: int = 400) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    def login_state(self) -> Optional[str]:
        rows = db.query(
            f"SELECT login_state FROM account_credentials WHERE account_id='{ACC}'")
        return _field(rows[0], 'login_state', 0) if rows else None

    def logs(self) -> List[Any]:
        rows = db.query(f"SELECT action, success FROM operation_logs WHERE account_id='{ACC}'")
        return [(_field(row, 'action', 0), _field(row, 'success', 1)) for row in rows]

    # ------------------------------------------------------------------ 路由

    def test_routes_are_mounted(self) -> None:
        paths = {route.path for route in security_write_api.router.routes}
        for suffix in ('/appeal/form', '/appeal/submit', '/appeal/cro/submit', '/appeal/urge'):
            with self.subTest(suffix=suffix):
                self.assertIn(f'/api/security-center/{{account_id}}{suffix}', paths)

    # ------------------------------------------------------------------ 表单（只读）

    def test_form_is_read_only_and_needs_no_confirm(self) -> None:
        self.stub(proto.APPEAL_FORM_GET_API, FORM_OK)
        data = self.ok_data(f'/api/security-center/{ACC}/appeal/form', {'form_id': 'F-1'})
        self.assertEqual(data['form_data']['formId'], 'F-1')
        self.assertEqual(self.fake.apis(), [proto.APPEAL_FORM_GET_API])
        call = self.fake.call_of(proto.APPEAL_FORM_GET_API)
        self.assertEqual(call['data'], {'formId': 'F-1'})
        self.assertEqual(call['v'], '1.0')

    def test_site_code_header_is_sent(self) -> None:
        self.stub(proto.APPEAL_FORM_GET_API, FORM_OK)
        self.ok_data(f'/api/security-center/{ACC}/appeal/form', {'form_id': 'F-1'})
        headers = self.fake.call_of(proto.APPEAL_FORM_GET_API)['extra_headers'] or {}
        self.assertEqual(headers.get('idle_site_biz_code'), 'COMMONPRO')

    def test_header_fallback_when_client_has_no_security_headers(self) -> None:
        self.fake._security_headers = None  # type: ignore[assignment]
        self.stub(proto.APPEAL_FORM_GET_API, FORM_OK)
        self.ok_data(f'/api/security-center/{ACC}/appeal/form', {'form_id': 'F-1'})
        self.assertIsNone(self.fake.call_of(proto.APPEAL_FORM_GET_API)['extra_headers'])

    def test_form_without_form_id_is_invalid_fields(self) -> None:
        payload = self.fail(f'/api/security-center/{ACC}/appeal/form', {'form_id': '   '})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_form_missing_form_data_is_null_not_empty_dict(self) -> None:
        self.stub(proto.APPEAL_FORM_GET_API, FORM_NO_DATA)
        data = self.ok_data(f'/api/security-center/{ACC}/appeal/form', {'form_id': 'F-1'})
        self.assertIn('form_data', data)
        self.assertIsNone(data['form_data'])

    # ------------------------------------------------------------------ 确认闸门

    def test_write_routes_require_confirm_and_send_nothing(self) -> None:
        cases = [
            ('/appeal/submit', {'parameter': {'formId': 'F-1'}}),
            ('/appeal/cro/submit', {'punish_id': 987, 'proof_items': [{'code': 'appealDesc'}]}),
            ('/appeal/urge', {'punish_id': 987}),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                payload = self.fail(f'/api/security-center/{ACC}{path}', body)
                self.assertEqual(payload.get('code'), 'confirmation_required')
                self.assertIn('confirm', payload.get('message', ''))
        self.assertEqual(self.fake.apis(), [], '未确认时不许调用任何平台接口')

    def test_confirm_false_is_also_refused(self) -> None:
        payload = self.fail(f'/api/security-center/{ACC}/appeal/urge',
                            {'punish_id': 987, 'confirm': False})
        self.assertEqual(payload.get('code'), 'confirmation_required')
        self.assertEqual(self.fake.apis(), [])

    def test_protocol_layer_also_refuses_without_confirm(self) -> None:
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.appeal_submit_request({'formId': 'F-1'})
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.appeal_submit_cro_request(987, [])
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.appeal_urge_request(987)
        self.assertEqual(self.fake.apis(), [])

    # ------------------------------------------------------------------ 提交申诉

    def test_submit_success_sends_json_parameter(self) -> None:
        self.stub(proto.APPEAL_SUBMIT_API, SUBMIT_OK)
        data = self.ok_data(f'/api/security-center/{ACC}/appeal/submit',
                            {'parameter': {'formId': 'F-1', 'appealReason': '误判'}, 'confirm': True})
        self.assertEqual(data['appeal_id'], 'A-9527')
        self.assertEqual(data['api'], proto.APPEAL_SUBMIT_API)
        call = self.fake.call_of(proto.APPEAL_SUBMIT_API)
        self.assertEqual(json.loads(call['data']['parameter']),
                         {'formId': 'F-1', 'appealReason': '误判'})
        self.assertEqual((call['extra_headers'] or {}).get('idle_site_biz_code'), 'COMMONPRO')
        self.assertIn(('security_appeal_submit', 1), self.logs())

    def test_submit_string_parameter_passes_through(self) -> None:
        self.stub(proto.APPEAL_SUBMIT_API, SUBMIT_OK)
        self.ok_data(f'/api/security-center/{ACC}/appeal/submit',
                     {'parameter': '{"formId":"F-2"}', 'confirm': True})
        sent = self.fake.call_of(proto.APPEAL_SUBMIT_API)['data']['parameter']
        self.assertEqual(sent, '{"formId":"F-2"}')

    def test_submit_empty_parameter_is_invalid_fields(self) -> None:
        payload = self.fail(f'/api/security-center/{ACC}/appeal/submit',
                            {'parameter': {}, 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_submit_without_appeal_id_is_business_failure(self) -> None:
        self.stub(proto.APPEAL_SUBMIT_API, SUBMIT_NO_APPEAL_ID)
        payload = self.fail(f'/api/security-center/{ACC}/appeal/submit',
                            {'parameter': {'formId': 'F-1'}, 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('appealId', payload.get('message', ''))
        self.assertIsNone(payload.get('appeal_id'))

    def test_submit_failure_envelope_is_passed_through(self) -> None:
        self.stub(proto.APPEAL_SUBMIT_API, NO_PERMISSION)
        payload = self.fail(f'/api/security-center/{ACC}/appeal/submit',
                            {'parameter': {'formId': 'F-1'}, 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', payload.get('message', ''))
        self.assertEqual(payload['platform_errors']['api'], proto.APPEAL_SUBMIT_API)

    def test_no_permission_is_not_relogin(self) -> None:
        self.stub(proto.APPEAL_SUBMIT_API, NO_PERMISSION)
        payload = self.fail(f'/api/security-center/{ACC}/appeal/submit',
                            {'parameter': {'formId': 'F-1'}, 'confirm': True}, status=502)
        self.assertNotEqual(payload.get('code'), 'need_relogin')
        self.assertEqual(self.login_state(), 'active')

    # ------------------------------------------------------------------ 复核 cro

    def test_cro_submit_payload_shape(self) -> None:
        self.stub(proto.APPEAL_SUBMIT_CRO_API, SUBMIT_OK)
        items = [proto.proof_item('appealDesc', [{'value': '误判', 'desc': '误判'}],
                                  index=1, name='填写申诉描述')]
        data = self.ok_data(f'/api/security-center/{ACC}/appeal/cro/submit',
                            {'punish_id': 987, 'proof_items': items, 'confirm': True})
        self.assertEqual(data['appeal_id'], 'A-9527')
        sent = json.loads(self.fake.call_of(proto.APPEAL_SUBMIT_CRO_API)['data']['parameter'])
        self.assertEqual(sent['punishId'], 987)
        self.assertEqual(sent['proofItems'][0]['code'], 'appealDesc')
        self.assertEqual(sent['proofItems'][0]['index'], 1)
        self.assertEqual(sent['proofItems'][0]['details'][0]['value'], '误判')

    def test_proof_item_without_code_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            proto.proof_item('', [])
        with self.assertRaises(ValueError):
            proto.appeal_submit_cro_request('abc', [], confirm=True)

    def test_cro_read_apis_are_not_declared_as_write_or_read(self) -> None:
        self.assertEqual(len(proto.WRITE_APIS), 3)
        for name, api in proto.CRO_READ_APIS.items():
            with self.subTest(name=name):
                self.assertNotIn(api, proto.WRITE_APIS)
                self.assertNotIn(api, proto.READ_APIS)

    # ------------------------------------------------------------------ 催办

    def test_urge_success_and_missing_data(self) -> None:
        self.stub(proto.APPEAL_URGE_API, URGE_OK)
        data = self.ok_data(f'/api/security-center/{ACC}/appeal/urge',
                            {'punish_id': '987', 'confirm': True})
        self.assertTrue(data['urged'])
        self.assertEqual(self.fake.call_of(proto.APPEAL_URGE_API)['data'], {'punishId': 987})

        self.fake.payloads[proto.APPEAL_URGE_API] = URGE_NO_DATA
        self.fake.calls.clear()
        payload = self.fail(f'/api/security-center/{ACC}/appeal/urge',
                            {'punish_id': 987, 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIsNone(payload.get('urged'))

    def test_invalid_punish_id_is_invalid_fields(self) -> None:
        payload = self.fail(f'/api/security-center/{ACC}/appeal/urge',
                            {'punish_id': 'abc', 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_urge_failure_envelope_is_passed_through(self) -> None:
        self.stub(proto.APPEAL_URGE_API, SYSTEM_ERROR)
        payload = self.fail(f'/api/security-center/{ACC}/appeal/urge',
                            {'punish_id': 987, 'confirm': True}, status=502)
        self.assertIn('SYSTEM_ERROR', payload.get('message', ''))
        self.assertEqual(payload['platform_errors']['api'], proto.APPEAL_URGE_API)


if __name__ == '__main__':
    unittest.main(verbosity=2)
