"""资质管理（超级擦亮）协议层 + 服务层 + HTTP 契约测试。

**为什么单开一组**：资质管理是本项目**唯一允许写平台**的推广页 —— 上传资质材料会真实
改变账号的资质状态、不可逆。最容易犯的三个错：

1. **没确认就把资质传上去**：`confirm` 闸门必须挡在「碰平台」**之前**（本项目里连
   `mtop` 都不该被调到）；字符串 `'true'` 不算确认；
2. **平台没返回的字段画成 0 / 空列表**：该账号真机资质就是「暂无数据」，而 0 与 `[]`
   都是**真实值**，两者必须分得清（没这个键 → `None`；平台给了空列表 → `[]`）；
3. **把「无权限」误判成掉线**：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 不是掉线，不许写
   `need_relogin`，否则用户白扫一次码。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_qualification_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fakes`），HTTP 层用
`app.dependency_overrides` 绕开登录。

注意响应形状：成功一律是 `common.respond` 套的 `{"success": true, "data": {...}}`
（`common.py:82`），失败信封才是**平铺**的 `{"success": false, "code": ...}`（:81）。
本文件用 `_data()` 取成功体，断言里顺带钉住这层包装。
"""
from __future__ import annotations

import asyncio
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

from backend.app.api import qualification as qualification_api  # noqa: E402
from backend.app.api.deps import current_user  # noqa: E402
from backend.app.core import db  # noqa: E402
from backend.app.xianyu import qualification as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'qual_acc'
BASE = '/api/qualification'

PROTO_MODULE = 'backend.app.xianyu.qualification'
CLIENT_MODULE = 'backend.app.xianyu.client'

#: 协议层函数名（假协议层要逐个摆出来，缺一个服务层就会 503）。
PROTO_NAMES = (
    'detail', 'submitted_records', 'allow_audit_categories', 'templates',
    'reusable_qualification', 'submit_packages', 'oss_credential',
)


class FakeMtopClient:
    """**真**协议层的 client 替身：记录 `mtop` 调用并回固定信封。"""

    def __init__(self, envelope: Optional[Dict[str, Any]] = None):
        self.account_id = ACC
        self.cookie_id = ACC
        self.envelope = {'data': {'ok': 1}} if envelope is None else envelope
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, **kwargs: Any) -> Any:
        self.calls.append({'api': api, 'data': data, 'kwargs': kwargs})
        return self.envelope


class FakeProtocol:
    """假协议层：模块级 async 函数，签名与 `xianyu/qualification.py` 一致。"""

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

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')

    def called(self, name: str) -> bool:
        return any(call[0] == name for call in self.calls)

    async def detail(self, client: Any, body: Any = None) -> Any:
        return self._answer('detail', body)

    async def submitted_records(self, client: Any, body: Any = None) -> Any:
        return self._answer('submitted_records', body)

    async def allow_audit_categories(self, client: Any, body: Any = None) -> Any:
        return self._answer('allow_audit_categories', body)

    async def templates(self, client: Any, body: Any = None) -> Any:
        return self._answer('templates', body)

    async def reusable_qualification(self, client: Any, body: Any = None) -> Any:
        return self._answer('reusable_qualification', body)

    async def submit_packages(self, client: Any, body: Any = None, *,
                              confirm: bool = False) -> Any:
        if confirm is not True:  # 假层也钉住闸门：未确认的调用不许被记录下来
            raise ValueError('未确认')
        return self._answer('submit_packages', body, confirm)

    async def oss_credential(self, client: Any, body: Any = None, *,
                             confirm: bool = False) -> Any:
        if confirm is not True:
            raise ValueError('未确认')
        return self._answer('oss_credential', body, confirm)


class FakeClient:
    """`common.get_client` 返回的客户端替身（假协议层不用它的 mtop）。"""

    def __init__(self) -> None:
        self.account_id = ''
        self.cookie_id = ACC


def install_fakes(fake: FakeProtocol, client: Optional[FakeClient] = None) -> None:
    """把假协议层 + 假客户端工厂塞进 sys.modules（真平台一次都不打）。"""
    module = types.ModuleType(PROTO_MODULE)
    for name in PROTO_NAMES:
        setattr(module, name, getattr(fake, name))
    module.WRITE_APIS = proto.WRITE_APIS  # type: ignore[attr-defined]
    sys.modules[PROTO_MODULE] = module

    holder = client or FakeClient()
    client_module = types.ModuleType(CLIENT_MODULE)

    def factory(account_id: str) -> FakeClient:
        # `common._note_session_expired` 靠 `client.account_id` 找到要标记的账号
        holder.account_id = account_id
        return holder

    client_module.get_client = factory  # type: ignore[attr-defined]
    client_module.XianyuClient = FakeClient  # type: ignore[attr-defined]
    sys.modules[CLIENT_MODULE] = client_module


def remove_fakes() -> None:
    sys.modules.pop(PROTO_MODULE, None)
    sys.modules.pop(CLIENT_MODULE, None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    if cookie is None:
        db.execute('DELETE FROM account_credentials WHERE account_id = ?', (account_id,))
        return
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def full_protocol(**overrides: Any) -> FakeProtocol:
    payloads: Dict[str, Any] = {
        'detail': {'qualification': {'status': 'NONE'}},
        'submitted_records': {'list': [], 'total': 0},
        'allow_audit_categories': {'data': [{'categoryId': 9}]},
        'templates': {'templates': [{'templateId': 1, 'name': '品牌授权'}]},
        'reusable_qualification': {'qualificationList': [{'id': 2}]},
        'submit_packages': {'packageId': 7},
        'oss_credential': {'accessId': 'ak', 'host': 'https://oss.example'},
    }
    payloads.update(overrides)
    return FakeProtocol(payloads)


class QualificationProtocolTest(unittest.TestCase):
    """真协议层（`xianyu/qualification.py`）：接口名冻结 + requestJson 形状 + 写闸门。"""

    def test_mtop_names_are_frozen(self):
        self.assertEqual(proto.QUALIFICATION_DETAIL_API,
                         'mtop.taobao.idle.ad.qualification.detail.query')
        self.assertEqual(proto.SUBMITTED_RECORDS_API,
                         'mtop.taobao.idle.ad.qualification.submittedRecords.batchQuery')
        self.assertEqual(proto.ALLOW_AUDIT_CATEGORIES_API,
                         'mtop.taobao.idle.ad.qualification.allowAuditCategories.query')
        self.assertEqual(proto.TEMPLATES_API,
                         'mtop.taobao.idle.ad.qualification.templates.queryv2')
        self.assertEqual(proto.REUSABLE_QUALIFICATION_API,
                         'mtop.taobao.idle.ad.qualification.reusableQualification.query')
        self.assertEqual(proto.PACKAGES_SUBMIT_API,
                         'mtop.taobao.idle.ad.qualification.packages.submit')
        self.assertEqual(proto.OSS_CREDENTIAL_API,
                         'mtop.taobao.idle.ad.qualification.get.oss.credential')
        self.assertEqual(proto.WRITE_APIS,
                         (proto.PACKAGES_SUBMIT_API, proto.OSS_CREDENTIAL_API))

    def test_request_json_shape_matches_ad_convention(self):
        client = FakeMtopClient()
        payload = asyncio.run(proto.detail(client, {'pageNum': 1}))
        self.assertEqual(payload, {'ok': 1})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]['api'], proto.QUALIFICATION_DETAIL_API)
        self.assertEqual(client.calls[0]['data'], {'requestJson': '{"pageNum":1}'})
        self.assertEqual(client.calls[0]['kwargs'], {'v': '1.0', 'spm_cnt': 'a21ybx.im.0.0'})

    def test_empty_body_is_still_request_json_object(self):
        client = FakeMtopClient()
        asyncio.run(proto.templates(client))
        self.assertEqual(client.calls[0]['data'], {'requestJson': '{}'})

    def test_non_dict_data_returns_none(self):
        client = FakeMtopClient({'data': []})
        self.assertIsNone(asyncio.run(proto.detail(client)))

    def test_write_gate_rejects_anything_but_true(self):
        client = FakeMtopClient()
        with self.assertRaises(ValueError):
            asyncio.run(proto.submit_packages(client, {'packages': [{'id': 1}]}))
        with self.assertRaises(ValueError):
            asyncio.run(proto.submit_packages(client, {'packages': [{'id': 1}]},
                                              confirm='true'))
        with self.assertRaises(ValueError):
            asyncio.run(proto.oss_credential(client, {'fileName': 'a.jpg'}, confirm=False))
        self.assertEqual(client.calls, [], '未确认时一次平台请求都不许发')

    def test_write_gate_lets_confirmed_call_through(self):
        client = FakeMtopClient()
        payload = asyncio.run(proto.submit_packages(client, {'packages': [{'id': 1}]},
                                                    confirm=True))
        self.assertEqual(payload, {'ok': 1})
        self.assertEqual(client.calls[0]['api'], proto.PACKAGES_SUBMIT_API)
        self.assertEqual(client.calls[0]['data'],
                         {'requestJson': '{"packages":[{"id":1}]}'})


class QualificationApiTest(unittest.TestCase):
    """服务层 + HTTP 契约。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fakes()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fakes()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        self.fake = full_protocol()
        install_fakes(self.fake)
        seed_account()
        app = FastAPI()
        app.include_router(qualification_api.router)
        app.dependency_overrides[current_user] = lambda: {'id': 1, 'username': 'tester'}
        self.http = TestClient(app)

    def _data(self, resp):
        """成功响应一律是 `{"success": true, "data": {...}}`（common.py:82）。"""
        body = resp.json()
        self.assertTrue(body.get('success'), f'期望成功信封，实际: {body}')
        return body['data']

    # -- 只读：平台给什么就是什么 ------------------------------------------

    def test_templates_pass_platform_payload_through(self):
        resp = self.http.get(f'{BASE}/{ACC}/templates')
        self.assertEqual(resp.status_code, 200)
        body = self._data(resp)
        self.assertEqual(body['templates'], [{'templateId': 1, 'name': '品牌授权'}])
        self.assertEqual(body['account_id'], ACC)
        self.assertEqual(body['source'], 'platform')
        self.assertTrue(body['fetched_at'])

    def test_missing_platform_fields_stay_none_never_zero(self):
        install_fakes(full_protocol(templates={}, submitted_records={}, detail=None))
        body = self._data(self.http.get(f'{BASE}/{ACC}/templates'))
        self.assertIsNone(body['templates'])
        body = self._data(self.http.get(f'{BASE}/{ACC}/submitted-records'))
        self.assertIsNone(body['records'])
        self.assertIsNone(body['total'], '平台没给总数 → None，绝不兜 0')
        body = self._data(self.http.get(f'{BASE}/{ACC}/detail'))
        self.assertIsNone(body['detail'])

    def test_empty_list_and_real_zero_are_kept(self):
        body = self._data(self.http.get(f'{BASE}/{ACC}/submitted-records'))
        self.assertEqual(body['records'], [], '平台给了空列表就是「确实一条都没有」')
        self.assertEqual(body['total'], 0, '真实的 0 必须原样保留')

    def test_detail_is_raw_passthrough(self):
        body = self._data(self.http.get(f'{BASE}/{ACC}/detail'))
        self.assertEqual(body['detail'], {'qualification': {'status': 'NONE'}})

    def test_categories_and_reusable_use_alias_keys(self):
        body = self._data(self.http.get(f'{BASE}/{ACC}/categories'))
        self.assertEqual(body['categories'], [{'categoryId': 9}])
        body = self._data(self.http.get(f'{BASE}/{ACC}/reusable'))
        self.assertEqual(body['qualifications'], [{'id': 2}])

    def test_submitted_records_sends_page_numbers(self):
        resp = self.http.get(f'{BASE}/{ACC}/submitted-records?page=2&size=5')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.fake.args_of('submitted_records')[0],
                         {'pageNum': 2, 'pageSize': 5})

    def test_bad_page_size_is_rejected_before_platform(self):
        resp = self.http.get(f'{BASE}/{ACC}/submitted-records?size=999')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    # -- 前置检查 ----------------------------------------------------------

    def test_unknown_account_is_404(self):
        resp = self.http.get(f'{BASE}/nobody/templates')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie=None)
        resp = self.http.get(f'{BASE}/{ACC}/templates')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [], '没 Cookie 时一次平台都不打')

    def test_unauthorized_is_protocol_error_not_need_relogin(self):
        """无权限 ≠ 掉线：不许写 need_relogin（否则用户白扫一次码）。"""
        install_fakes(FakeProtocol(
            errors={'templates': RuntimeError(
                'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::SM::无权限')}))
        resp = self.http.get(f'{BASE}/{ACC}/templates')
        self.assertEqual(resp.status_code, 502)
        body = resp.json()
        self.assertEqual(body['code'], 'protocol_error')
        self.assertNotIn('login_state', body)
        self.assertNotIn('need_relogin', str(body))

    # -- 写平台：闸门 ------------------------------------------------------

    def test_submit_without_confirm_is_rejected_and_never_calls_platform(self):
        resp = self.http.post(f'{BASE}/{ACC}/packages/submit',
                              json={'request': {'packages': [{'packageId': 7}]}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['code'], 'confirm_required')
        self.assertEqual(self.fake.calls, [], '未确认时连库都不查、一次平台请求都不发')

    def test_submit_with_confirm_passes_business_object_verbatim(self):
        request = {'packages': [{'packageId': 7, 'categoryId': 9}]}
        resp = self.http.post(f'{BASE}/{ACC}/packages/submit',
                              json={'confirm': True, 'request': request})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._data(resp)['submitted'], {'packageId': 7})
        self.assertEqual(self.fake.args_of('submit_packages')[0], request,
                         '业务对象原样进 requestJson，服务层不许改名/补键')
        self.assertIs(self.fake.args_of('submit_packages')[1], True)

    def test_submit_with_empty_business_object_is_rejected(self):
        resp = self.http.post(f'{BASE}/{ACC}/packages/submit',
                              json={'confirm': True, 'request': {}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_oss_credential_requires_confirm_too(self):
        resp = self.http.post(f'{BASE}/{ACC}/oss-credential',
                              json={'request': {'fileName': 'a.jpg'}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['code'], 'confirm_required')
        self.assertEqual(self.fake.calls, [])
        resp = self.http.post(f'{BASE}/{ACC}/oss-credential',
                              json={'confirm': True, 'request': {'fileName': 'a.jpg'}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._data(resp)['credential'],
                         {'accessId': 'ak', 'host': 'https://oss.example'})


if __name__ == '__main__':
    unittest.main()
