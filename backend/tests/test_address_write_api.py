"""退货地址写接口（官方 `…merchant.delivery.address.{add,update,delete,set.default}`）的测试。

**为什么单开一组**：这是「改用户真实收货地址」的写操作，翻车代价高。最容易犯的错：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 账号掉线时先报「去重新登录」，用户填半天才发现名字是空的
   （三步顺序必须是 **确认 → 字段校验 → 建客户端**）；
3. 编造平台没回显的 `contactId`（官方新增后是重拉列表找新条目，从不读响应体）；
4. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 之类的平台拒绝当成掉线，让用户白扫一次码；
5. 偷偷补上官方有、我们**不伪造**的 `division` / `areaId`（码源是
   `GET /api/logistics/{account_id}/divisions`，但调用方没给就一个字节都不发）。

**第 61 批已打过真机 add**（证据 `logs/write61/05_address_add.json`、`05b_address_add_with_area.json`、
`05d_address_add_full_raw.json`）：平台原话是不带 `areaId` 回
`FAIL_SYS_BIZPARAM_MISSED::缺少业务参数areaId`，只补 `areaId` 再回
`FAIL_SYS_BIZPARAM_MISSED::缺少业务参数defaultAddr` —— 两者都是平台硬必填。
这里用假协议层钉住「字段形状与官方 bundle 逐字一致」。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_address_write_api.py"

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

from backend.app.api import trade_write as trade_write_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import address_write as proto  # noqa: E402
from backend.app.xianyu import trade as trade_read  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'aw_acc'
BASE = '/api/trade'

#: 官方 `address.add` 成功（`data.contactId` 是官方**没有**读取的字段，我们读它是为了给用户回显）
ADD_OK = {'success': True, 'data': {'contactId': 'C10086'}, 'message': 'SUCCESS::调用成功',
          'ret': ['SUCCESS::调用成功']}
#: 平台接受了但没回显任何 id（不能编造）
ADD_NO_ECHO = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
               'ret': ['SUCCESS::调用成功']}
#: 官方 `address.update` / `delete` / `set.default` 的成功体官方都没读（可能是空对象）
PLAIN_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
            'ret': ['SUCCESS::调用成功']}
#: 真机原文：卖家工作台无权限（`address.list.query` 就是这个形状）
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}
#: 2026-10-16 真机原文（`logs/write64/b1h_delete_probe.json`）：**客户端层 success=true**，
#: 业务码藏在 `data.code` —— 删默认退货地址被平台拒绝，但信封看着像成功（第 64 批真 bug）。
REAL_DELETE_DEFAULT_REFUSED = {
    'success': True,
    'data': {'code': 'CAN_NOT_DELETE_DEFAULT_ADDRESS',
             'msg': '请不要删除默认的退货地址,请先设置其他的地址为默认退货地址后再删除本地址'},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 一份合法的表单（四个接口共用的字段；`area_id` / `default_addr` 是平台硬必填，见文件头）
FORM = {
    'contact_name': '张三',
    'mobile_phone': '13800138000',
    'province_name': '浙江省',
    'city_name': '杭州市',
    'district_name': '西湖区',
    'detail_address': '文一西路 969 号 1 幢 101 室',
    'area_id': '330106',
    'default_addr': True,
}


from backend.app.main import IdempotencyKeyMiddleware
class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeAddressClient:
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


def install_fake_protocol(client: FakeAddressClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeAddressClient:
        # `common._note_session_expired` 靠 `client.account_id` 找要标记的账号
        client.account_id = account_id
        return client

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


def full_client(**overrides: Any) -> FakeAddressClient:
    payloads = {
        proto.ADD_API: ADD_OK,
        proto.UPDATE_API: PLAIN_OK,
        proto.DELETE_API: PLAIN_OK,
        proto.SET_DEFAULT_API: PLAIN_OK,
    }
    payloads.update(overrides)
    return FakeAddressClient(payloads)


class AddressWritePageTest(unittest.TestCase):
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
        app.add_middleware(IdempotencyKeyMiddleware)
        app.include_router(trade_write_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('addrwrite','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body or {},
                                headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def fail(self, path: str, body: Optional[Dict[str, Any]] = None,
             status: int = 400) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    def login_state(self) -> str:
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id=?', (ACC,))
        return str(row['login_state']) if row else ''

    def logs(self) -> List[Any]:
        return db.query('SELECT action, success FROM operation_logs WHERE account_id=?', (ACC,))

    # -- 路径 ------------------------------------------------------------

    def add_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/addresses'

    def update_path(self, contact_id: str = 'C1', account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/addresses/{contact_id}/update'

    def delete_path(self, contact_id: str = 'C1', account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/addresses/{contact_id}/delete'

    def default_path(self, contact_id: str = 'C1', account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/addresses/{contact_id}/default'


# --------------------------------------------------------------------------- 协议层


class AddressWriteProtocolTest(unittest.TestCase):
    """纯函数：接口名、入参形状、确认闸门、解析口径 —— 全部离线。"""

    def test_api_names_match_the_bundle(self):
        self.assertEqual(proto.ADD_API,
                         'mtop.alibaba.idle.seller.platform.merchant.delivery.address.add')
        self.assertEqual(proto.UPDATE_API,
                         'mtop.alibaba.idle.seller.platform.merchant.delivery.address.update')
        self.assertEqual(proto.DELETE_API,
                         'mtop.alibaba.idle.seller.platform.merchant.delivery.address.delete')
        self.assertEqual(
            proto.SET_DEFAULT_API,
            'mtop.alibaba.idle.seller.platform.merchant.delivery.address.set.default')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_whitelist_rejects_anything_else(self):
        for action in proto.ACTIONS:
            self.assertEqual(proto.assert_write(proto.WRITE_APIS[action]['api']),
                             proto.WRITE_APIS[action]['api'])
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write('mtop.alibaba.idle.seller.platform.merchant.delivery.address.list.query')
        self.assertEqual(len(proto.ALL_APIS), 4)
        self.assertEqual(len(proto.ACTIONS), 4)

    def test_read_layer_refuses_these_apis(self):
        """只读层（`xianyu/trade.py`）必须把它们当写接口拦住，不能被当只读调出去。"""
        for api in proto.ALL_APIS:
            with self.assertRaises(Exception):
                trade_read.assert_read_only(api)

    def test_requests_need_explicit_confirm(self):
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.add_request(**FORM)
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.update_request('C1', **FORM)
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.delete_request('C1')
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.set_default_request('C1')
        for bad in (False, 'true', 1, 'yes'):
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.delete_request('C1', confirm=bad)

    def test_add_request_uses_official_field_names(self):
        api, data = proto.add_request(**FORM, confirm=True)
        self.assertEqual(api, proto.ADD_API)
        self.assertEqual(data, {
            'contactName': '张三',
            'mobilePhone': '13800138000',
            'provinceName': '浙江省',
            'cityName': '杭州市',
            'districtName': '西湖区',
            'detailAddress': '文一西路 969 号 1 幢 101 室',
            'areaId': '330106',
            'defaultAddr': True,
        })
        # 官方有 division（级联数组）—— 我们不伪造、不许出现
        self.assertNotIn('division', data)
        # 协议层口径仍是「调用方给了才发」：显式给空值就不发这两个键（不伪造）
        _api, bare = proto.add_request(**{**FORM, 'area_id': '', 'default_addr': None},
                                       confirm=True)
        self.assertNotIn('areaId', bare)
        self.assertNotIn('defaultAddr', bare)

    def test_optional_fields_only_when_given(self):
        """协议层口径：`areaId` / `defaultAddr` 只在调用方**显式**给出时才发（不伪造）。"""
        bare = {k: v for k, v in FORM.items() if k not in ('area_id', 'default_addr')}
        _api, data = proto.add_request(**bare, area_id='330106', default_addr=True, confirm=True)
        self.assertEqual(data['areaId'], '330106')
        self.assertIs(data['defaultAddr'], True)
        _api, data = proto.add_request(**bare, area_id='', default_addr=None, confirm=True)
        self.assertNotIn('areaId', data)
        self.assertNotIn('defaultAddr', data)

    def test_update_request_carries_contact_id(self):
        api, data = proto.update_request('C9', **FORM, confirm=True)
        self.assertEqual(api, proto.UPDATE_API)
        self.assertEqual(data['contactId'], 'C9')
        self.assertEqual(data['contactName'], '张三')
        with self.assertRaises(ValueError):
            proto.update_request('', **FORM, confirm=True)

    def test_delete_and_set_default_are_just_contact_id(self):
        api, data = proto.delete_request(123456789, confirm=True)
        self.assertEqual(api, proto.DELETE_API)
        self.assertEqual(data, {'contactId': '123456789'})
        api, data = proto.set_default_request('C1', confirm=True)
        self.assertEqual(api, proto.SET_DEFAULT_API)
        self.assertEqual(data, {'contactId': 'C1'})
        with self.assertRaises(ValueError):
            proto.delete_request('', confirm=True)
        with self.assertRaises(ValueError):
            proto.set_default_request(None, confirm=True)

    def test_request_of_dispatches_the_four_actions(self):
        api, _data = proto.request_of('add', confirm=True, **FORM)
        self.assertEqual(api, proto.ADD_API)
        api, _data = proto.request_of('update', contact_id='C1', confirm=True, **FORM)
        self.assertEqual(api, proto.UPDATE_API)
        api, _data = proto.request_of('delete', contact_id='C1', confirm=True)
        self.assertEqual(api, proto.DELETE_API)
        api, _data = proto.request_of('set_default', contact_id='C1', confirm=True)
        self.assertEqual(api, proto.SET_DEFAULT_API)
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('archive', contact_id='C1', confirm=True)

    def test_limits_match_the_official_schema(self):
        # bundle 内嵌 schema：contactName max 15 / mobilePhone max 11 + phone / detailAddress max 100
        self.assertEqual(proto.CONTACT_NAME_MAX, 15)
        self.assertEqual(proto.DETAIL_ADDRESS_MAX, 100)
        self.assertEqual(proto.MOBILE_DIGITS, 11)

    def test_is_success_follows_client_then_business_code(self):
        self.assertTrue(proto.is_success(PLAIN_OK))
        self.assertTrue(proto.is_success(ADD_OK))
        self.assertFalse(proto.is_success(NO_PERMISSION))
        # 客户端说成功、但业务体里带着失败码 —— 不能算成功
        self.assertFalse(proto.is_success(
            {'success': True, 'data': {'code': 'FAIL_BIZ_X', 'msg': '拒绝'}, 'message': '',
             'ret': ['SUCCESS::调用成功']}))
        self.assertTrue(proto.is_success(
            {'success': True, 'data': {'code': 'success'}, 'message': '',
             'ret': ['SUCCESS::调用成功']}))
        self.assertFalse(proto.is_success(None))

    def test_contact_id_is_none_when_platform_did_not_echo_it(self):
        self.assertEqual(proto.contact_id_of(ADD_OK), 'C10086')
        # 摊平后还嵌一层 data 的容器也要认
        self.assertEqual(proto.contact_id_of(
            {'success': True, 'data': {'data': {'contactId': 'C7'}}, 'message': '', 'ret': []}), 'C7')
        self.assertIsNone(proto.contact_id_of(ADD_NO_ECHO))
        self.assertIsNone(proto.contact_id_of({'success': True, 'data': {'id': ''}, 'message': '',
                                               'ret': []}))
        self.assertIsNone(proto.contact_id_of(None))

    def test_failure_reason_passes_platform_text_through(self):
        self.assertEqual(proto.failure_reason(NO_PERMISSION),
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        self.assertEqual(proto.failure_reason(ADD_OK, '兜底'), 'SUCCESS::调用成功')
        self.assertEqual(proto.failure_reason({'success': False, 'message': '', 'ret': []}, '兜底'), '兜底')
    def test_real_delete_default_refusal_is_not_success(self):
        # 真机原文：客户端说成功，业务码说不能删默认地址
        self.assertFalse(proto.is_success(REAL_DELETE_DEFAULT_REFUSED))
        self.assertEqual(proto.code_of(REAL_DELETE_DEFAULT_REFUSED),
                         'CAN_NOT_DELETE_DEFAULT_ADDRESS')
        self.assertEqual(proto.failure_reason(REAL_DELETE_DEFAULT_REFUSED, '兜底'),
                         '请不要删除默认的退货地址,请先设置其他的地址为默认退货地址后再删除本地址')


# --------------------------------------------------------------------------- 确认闸门


class AddressWriteConfirmGateTest(AddressWritePageTest):
    """红线：没有 `confirm` 就一个平台请求都不许发。"""

    def test_every_route_refuses_without_confirm(self):
        cases = [
            (self.add_path(), dict(FORM)),
            (self.update_path(), dict(FORM)),
            (self.delete_path(), {}),
            (self.default_path(), {}),
        ]
        for path, body in cases:
            payload = self.fail(path, body, status=400)
            self.assertEqual(payload.get('code'), 'confirmation_required', path)
            self.assertIn('确认', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_confirm_false_is_not_a_platform_call_either(self):
        for bad in (False, 'false', 0, '', None):
            payload = self.fail(self.add_path(), {**FORM, 'confirm': bad}, status=400)
            self.assertEqual(payload.get('code'), 'confirmation_required')
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.logs(), [])

    def test_protocol_layer_also_refuses_without_confirm(self):
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.add_request(**FORM)


# --------------------------------------------------------------------------- 字段校验


class AddressFieldValidationTest(AddressWritePageTest):
    """校验必须**先于**建客户端（掉线时也该先报「名字没填」）。"""

    def test_field_validation_runs_before_credential_check(self):
        seed_account(cookie='')  # 没有 Cookie：建客户端一定失败
        # area_id / default_addr 先给全，才能走到「联系人姓名」这一条
        payload = self.fail(self.add_path(), {**FORM, 'contact_name': '', 'confirm': True},
                            status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('联系人姓名', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_credential_check_still_wins_when_fields_are_complete(self):
        seed_account(cookie='')
        payload = self.fail(self.add_path(), {**FORM, 'confirm': True}, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_missing_province_is_invalid_fields(self):
        body = {**FORM, 'province_name': '', 'confirm': True}
        payload = self.fail(self.add_path(), body, status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('所在省', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_mobile_must_be_eleven_digits(self):
        for bad in ('138', '138001380001', 'abcdefghijk', ''):
            payload = self.fail(self.add_path(), {**FORM, 'mobile_phone': bad, 'confirm': True},
                                status=400)
            self.assertEqual(payload.get('code'), 'invalid_fields', bad)
        self.assertEqual(self.fake.calls, [])

    def test_name_and_detail_length_limits(self):
        payload = self.fail(self.add_path(), {**FORM, 'contact_name': '张' * 16, 'confirm': True},
                            status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('15', payload.get('message', ''))
        payload = self.fail(self.add_path(), {**FORM, 'detail_address': '路' * 101, 'confirm': True},
                            status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('100', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_update_without_contact_id_is_invalid_fields(self):
        payload = self.fail(self.update_path('  '), {**FORM, 'confirm': True}, status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_missing_area_id_is_invalid_fields_and_never_calls_platform(self):
        """平台硬必填：第 61 批真机不带 areaId 时回 FAIL_SYS_BIZPARAM_MISSED::缺少业务参数areaId。"""
        body = {k: v for k, v in FORM.items() if k != 'area_id'}
        payload = self.fail(self.add_path(), {**body, 'confirm': True}, status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('area_id', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_area_id_without_default_addr_is_invalid_fields_and_never_calls_platform(self):
        """只补 areaId 也不行：平台第二个缺的就是 defaultAddr（真机原话）。"""
        body = {k: v for k, v in FORM.items() if k != 'default_addr'}
        payload = self.fail(self.add_path(), {**body, 'confirm': True}, status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('default_addr', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_blank_area_id_is_invalid_fields_and_never_calls_platform(self):
        """空白串不算「给了区域码」：真机拿 '   ' 打过去平台回
        `FAIL_SYS_BIZPARAM_TYPE_ERROR::业务参数类型错误areaId`，所以本地就拦下。"""
        payload = self.fail(self.add_path(),
                            {**FORM, 'area_id': '   ', 'confirm': True}, status=400)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('area_id', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_default_addr_false_is_accepted_and_sent(self):
        """`default_addr` 是布尔：False 表示「不设为默认」，是合法值（只有 None 才算缺）。"""
        self.ok_data(self.add_path(), {**FORM, 'default_addr': False, 'confirm': True})
        self.assertIs(self.fake.call_of(proto.ADD_API)['data']['defaultAddr'], False)


# --------------------------------------------------------------------------- 四个接口


class AddressWriteHappyPathTest(AddressWritePageTest):

    def test_add_shape_matches_the_bundle(self):
        data = self.ok_data(self.add_path(), {**FORM, 'confirm': True})
        call = self.fake.call_of(proto.ADD_API)
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['data'], {
            'contactName': '张三', 'mobilePhone': '13800138000', 'provinceName': '浙江省',
            'cityName': '杭州市', 'districtName': '西湖区',
            'detailAddress': '文一西路 969 号 1 幢 101 室',
            'areaId': '330106', 'defaultAddr': True,
        })
        self.assertEqual(data['action'], 'add')
        self.assertEqual(data['contact_id'], 'C10086')
        self.assertEqual(data['api'], proto.ADD_API)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data.get('fetched_at'))
        self.assertEqual(len(self.fake.calls), 1)

    def test_mobile_is_normalized_to_digits(self):
        self.ok_data(self.add_path(), {**FORM, 'mobile_phone': '138 0013-8000', 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.ADD_API)['data']['mobilePhone'], '13800138000')

    def test_area_id_and_default_addr_reach_the_platform(self):
        """两者都是平台硬必填（服务层预检），所以 HTTP 这条路一定会带上。

        协议层「调用方给了才发」的缺省形状由 test_optional_fields_only_when_given 钉住。
        """
        self.ok_data(self.add_path(), {**FORM, 'confirm': True})
        data = self.fake.call_of(proto.ADD_API)['data']
        self.assertEqual(data['areaId'], '330106')
        self.assertIs(data['defaultAddr'], True)
        self.assertEqual(len(self.fake.calls), 1)

    def test_add_without_echo_is_a_warning_not_a_fake_id(self):
        self.fake = full_client(**{proto.ADD_API: ADD_NO_ECHO})
        install_fake_protocol(self.fake)
        data = self.ok_data(self.add_path(), {**FORM, 'confirm': True})
        self.assertIsNone(data['contact_id'])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_contact_id'])

    def test_update_shape_matches_the_bundle(self):
        data = self.ok_data(self.update_path('C9'), {**FORM, 'confirm': True})
        call = self.fake.call_of(proto.UPDATE_API)
        self.assertEqual(call['data']['contactId'], 'C9')
        self.assertEqual(call['data']['detailAddress'], FORM['detail_address'])
        self.assertEqual(data['action'], 'update')
        self.assertEqual(data['api'], proto.UPDATE_API)

    def test_delete_shape_matches_the_bundle(self):
        data = self.ok_data(self.delete_path('C9'), {'confirm': True})
        self.assertEqual(self.fake.call_of(proto.DELETE_API)['data'], {'contactId': 'C9'})
        self.assertEqual(data['action'], 'delete')

    def test_set_default_shape_matches_the_bundle(self):
        data = self.ok_data(self.default_path('C9'), {'confirm': True})
        self.assertEqual(self.fake.call_of(proto.SET_DEFAULT_API)['data'], {'contactId': 'C9'})
        self.assertEqual(data['action'], 'set_default')

    def test_every_call_is_one_of_the_four_write_apis(self):
        self.ok_data(self.add_path(), {**FORM, 'confirm': True})
        self.ok_data(self.update_path('C9'), {**FORM, 'confirm': True})
        self.ok_data(self.delete_path('C9'), {'confirm': True})
        self.ok_data(self.default_path('C9'), {'confirm': True})
        for api in self.fake.apis():
            self.assertIn(api, proto.ALL_APIS)

    def test_operation_log_written_per_write(self):
        self.ok_data(self.add_path(), {**FORM, 'confirm': True})
        self.ok_data(self.delete_path('C9'), {'confirm': True})
        actions = [row['action'] for row in self.logs()]
        self.assertEqual(actions, ['add', 'delete'])
        self.assertTrue(all(row['success'] for row in self.logs()))


# --------------------------------------------------------------------------- 平台与凭据失败


class AddressPlatformFailureTest(AddressWritePageTest):

    def test_no_permission_is_a_protocol_error_and_not_a_logout(self):
        self.fake = full_client(**{proto.ADD_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        payload = self.fail(self.add_path(), {**FORM, 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', payload.get('message', ''))
        self.assertEqual(payload.get('platform_errors', {}).get('api'), proto.ADD_API)
        # 无权限 ≠ 掉线
        self.assertEqual(self.login_state(), 'active')
        self.assertFalse(self.logs()[0]['success'])

    def test_platform_failure_is_not_reported_as_success(self):
        self.fake = full_client(**{proto.DELETE_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        payload = self.fail(self.delete_path('C9'), {'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
    def test_business_error_inside_a_success_envelope_is_a_failure(self):
        """客户端层 success=true、业务码在 `data.code` ⇒ 必须当失败（第 64 批真 bug）。"""
        self.fake = full_client(**{proto.DELETE_API: REAL_DELETE_DEFAULT_REFUSED})
        install_fake_protocol(self.fake)
        payload = self.fail(self.delete_path('C9'), {'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('请不要删除默认的退货地址', payload.get('message', ''))
        self.assertEqual(payload.get('platform_errors', {}).get('code'),
                         'CAN_NOT_DELETE_DEFAULT_ADDRESS')
        # 平台拒绝过就绝不能写成成功审计（旧代码正是这么写的）
        self.assertEqual(len(self.logs()), 1)
        self.assertFalse(self.logs()[0]['success'])

    def test_business_error_on_add_is_a_failure_too(self):
        refused = {'success': True, 'data': {'code': 'FAIL_BIZ_ADDRESS_ILLEGAL',
                                             'msg': '地址不合法'},
                   'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}
        self.fake = full_client(**{proto.ADD_API: refused})
        install_fake_protocol(self.fake)
        payload = self.fail(self.add_path(), {**FORM, 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('地址不合法', payload.get('message', ''))
        self.assertFalse(self.logs()[0]['success'])

    def test_session_expired_marks_the_account(self):
        self.fake.errors[proto.ADD_API] = SessionExpiredError('Session过期')
        payload = self.fail(self.add_path(), {**FORM, 'confirm': True}, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        self.fail(self.add_path(), {**FORM, 'confirm': True}, status=409)
        self.assertEqual(self.fake.calls, [])

    def test_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        self.fail(self.delete_path('C9'), {'confirm': True}, status=409)
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404(self):
        self.fail(self.add_path('nobody'), {**FORM, 'confirm': True}, status=404)
        self.assertEqual(self.fake.calls, [])

    def test_requests_require_login(self):
        resp = self.post(self.add_path(), {**FORM, 'confirm': True}, headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


# --------------------------------------------------------------------------- 路由


class AddressRouteTest(unittest.TestCase):

    def test_four_routes_are_registered_as_post(self):
        routes = {}
        for route in trade_write_api.router.routes:
            path = getattr(route, 'path', '')
            if '/addresses' in path:
                routes[path] = sorted(getattr(route, 'methods', []) or [])
        self.assertEqual(routes, {
            '/api/trade/{account_id}/addresses': ['POST'],
            '/api/trade/{account_id}/addresses/{contact_id}/update': ['POST'],
            '/api/trade/{account_id}/addresses/{contact_id}/delete': ['POST'],
            '/api/trade/{account_id}/addresses/{contact_id}/default': ['POST'],
        })




class AddressWriteIdempotencyTest(AddressWritePageTest):
    """第 63 批：退货地址写族接上幂等键（Idempotency-Key，CONTRACTS 9）。"""

    def test_same_key_and_payload_hits_the_platform_once(self):
        path = self.add_path()
        body = {**FORM, "confirm": True}
        headers = dict(self.headers, **{"Idempotency-Key": "addr-idem-1"})

        first = self.post(path, body, headers=headers)
        second = self.post(path, body, headers=headers)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.fake.apis(), [proto.ADD_API], "同键同载荷只许打平台一次")
        self.assertNotIn("idempotent_replay", first.json()["data"], first.text)
        self.assertTrue(second.json()["data"]["idempotent_replay"], second.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
