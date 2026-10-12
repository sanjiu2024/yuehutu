"""买家改地址审批（官方 `…merchant.order.{get.modify.address.info,address.modify.agree,address.modify.refuse}`）的测试。

**为什么单开一组**：同意改地址会**真的改掉订单的收货地址**（货可能寄到别人那儿），翻车代价极高。
最容易犯的错：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后**（三步顺序必须是 **确认 → 字段校验 → 建客户端**）；
3. 把平台拒绝（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）当成掉线，让用户白扫一次码；
4. 在 `modifyStatus != 'WAITING'` 时把 `modifyDeliverId` 当有效值用（平台只在 WAITING 才给）；
5. 隐私号订单：编一份明文，或把平台给的打码字段当明文展示；
6. 只读查询被加上 `confirm` 闸门（它没有副作用，加闸门只会稀释真危险操作的信号）。

**一次真机写请求都没发过**（账号 `2217970177348` 已登录，但改地址审批要买家先发起申请，
不能为了验证去改用户的真实订单）。这里用假协议层钉住「字段形状与官方 bundle 逐字符一致」，
平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_order_address_api.py"

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
from backend.app.xianyu import order_address as proto  # noqa: E402
from backend.app.xianyu import trade as trade_read  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'oa_acc'
BASE = '/api/trade'
ORDER = 'O20261012001'

#: 官方 `get.modify.address.info` 成功且**有申请待处理**（偏移 3101551）
INFO_WAITING = {
    'success': True,
    'data': {
        'module': {
            'modifyStatus': 'WAITING',
            'modifyDeliverId': '9001',
            'nowAddressInfoVO': {
                'name': '张*', 'phoneNumber': '138****0000',
                'address': '浙江省杭州市西湖区**路 1 号', 'privacyFlag': False,
            },
            'modifyAddressInfoVO': {
                'name': '张三', 'phoneNumber': '13800138000',
                'address': '浙江省杭州市余杭区文一西路 969 号',
            },
        },
    },
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 没有申请（官方组件初值就是 `'NONE'`），平台不给 deliverId、也不给两个 VO
INFO_NONE = {
    'success': True,
    'data': {'module': {'modifyStatus': 'NONE'}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 隐私号订单：`nowAddressInfoVO.privacyFlag` 为真，平台给的是打码字段
INFO_PRIVACY = {
    'success': True,
    'data': {
        'module': {
            'modifyStatus': 'WAITING',
            'modifyDeliverId': '9002',
            'nowAddressInfoVO': {
                'name': '李*', 'phoneNumber': '139****1111',
                'address': '北京市朝阳区**路 2 号', 'privacyFlag': True,
            },
            'modifyAddressInfoVO': {
                'name': '李四', 'phoneNumber': '13900139000', 'address': '北京市海淀区某路 3 号',
            },
        },
    },
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 隐私解密成功：接口带 `valueType:'string'`，业务体是 **JSON 字符串**
PRIVACY_OK = {
    'success': True,
    'data': ('{"name":"李四","suffix":"先生","phoneNumber":"13900139000",'
             '"address":"北京市海淀区某路 3 号"}'),
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 同意 / 拒绝成功（官方只判成功、不读响应体）
PLAIN_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
            'ret': ['SUCCESS::调用成功']}

#: 真机原文形状：卖家工作台无权限
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeOrderAddressClient:
    """假协议层：只实现 `mtop`，记录每次调用的 (api, data, v, value_type)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []
        self.account_id = ''

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   value_type: Optional[str] = None) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v, 'value_type': value_type})
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


def install_fake_protocol(client: FakeOrderAddressClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeOrderAddressClient:
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


def full_client(**overrides: Any) -> FakeOrderAddressClient:
    payloads = {
        proto.INFO_API: INFO_WAITING,
        proto.AGREE_API: PLAIN_OK,
        proto.REFUSE_API: PLAIN_OK,
        proto.PRIVACY_API: PRIVACY_OK,
    }
    payloads.update(overrides)
    return FakeOrderAddressClient(payloads)


class OrderAddressPageTest(unittest.TestCase):
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
        app.include_router(trade_write_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('oa','x',1)"))
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

    def ok_data(self, resp) -> Dict[str, Any]:
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def fail(self, resp, status: int = 400) -> Dict[str, Any]:
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

    def info_path(self, order_id: str = ORDER, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/orders/{order_id}/modify-address'

    def agree_path(self, order_id: str = ORDER, account_id: str = ACC) -> str:
        return f'{self.info_path(order_id, account_id)}/agree'

    def refuse_path(self, order_id: str = ORDER, account_id: str = ACC) -> str:
        return f'{self.info_path(order_id, account_id)}/refuse'


class OrderAddressProtocolTest(unittest.TestCase):
    """纯函数层：请求形状与官方 bundle 逐字符一致、响应解析从不瞎编。"""

    def test_apis_are_the_official_names(self):
        self.assertEqual(proto.INFO_API, 'mtop.idle.merchant.order.get.modify.address.info')
        self.assertEqual(proto.AGREE_API, 'mtop.idle.merchant.order.address.modify.agree')
        self.assertEqual(proto.REFUSE_API, 'mtop.idle.merchant.order.address.modify.refuse')
        self.assertEqual(proto.PRIVACY_API, 'mtop.idle.trade.order.detail.privacy.address')
        self.assertEqual(proto.PRIVACY_VALUE_TYPE, 'string')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')
        self.assertEqual(proto.STATUS_WAITING, 'WAITING')

    def test_read_api_is_not_declared_as_a_write(self):
        """只读查询**不能**进 `WRITE_APIS`，否则会被加确认闸门、也会进写黑名单。"""
        self.assertEqual(set(proto.WRITE_APIS), {'agree', 'refuse'})
        self.assertEqual({entry['api'] for entry in proto.WRITE_APIS.values()},
                         {proto.AGREE_API, proto.REFUSE_API})
        self.assertNotIn(proto.INFO_API, proto.ALL_APIS)
        self.assertNotIn(proto.PRIVACY_API, proto.ALL_APIS)
        self.assertEqual(proto.READ_APIS, (proto.INFO_API, proto.PRIVACY_API))
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.INFO_API)
        self.assertEqual(proto.assert_write(proto.AGREE_API), proto.AGREE_API)

    def test_agree_request_matches_the_bundle(self):
        api, data = proto.agree_request(ORDER, '9001', confirm=True)
        self.assertEqual(api, proto.AGREE_API)
        self.assertEqual(data, {'bizOrderId': ORDER, 'deliverId': '9001'})

    def test_refuse_request_matches_the_bundle(self):
        api, data = proto.refuse_request(ORDER, '9001', confirm=True)
        self.assertEqual(api, proto.REFUSE_API)
        self.assertEqual(data, {'bizOrderId': ORDER, 'deliverId': '9001'})

    def test_writes_require_explicit_confirmation(self):
        for builder in (proto.agree_request, proto.refuse_request):
            for bad in (None, False, 'false', 0, ''):
                with self.assertRaises(proto.WriteNotConfirmed):
                    builder(ORDER, '9001', confirm=bad)

    def test_empty_ids_are_refused(self):
        for builder in (proto.agree_request, proto.refuse_request):
            with self.assertRaises(ValueError):
                builder('', '9001', confirm=True)
            with self.assertRaises(ValueError):
                builder(ORDER, '', confirm=True)

    def test_request_of_dispatches_and_rejects_unknown(self):
        api, data = proto.request_of('agree', order_id=ORDER, deliver_id='9', confirm=True)
        self.assertEqual((api, data), (proto.AGREE_API, {'bizOrderId': ORDER, 'deliverId': '9'}))
        api, data = proto.request_of('refuse', order_id=ORDER, deliver_id='9', confirm=True)
        self.assertEqual((api, data), (proto.REFUSE_API, {'bizOrderId': ORDER, 'deliverId': '9'}))
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('cancel', order_id=ORDER, deliver_id='9', confirm=True)

    def test_info_request_matches_the_bundle(self):
        api, data = proto.info_request(ORDER)
        self.assertEqual(api, proto.INFO_API)
        self.assertEqual(data, {'bizOrderId': ORDER})
        api, data = proto.info_request('')
        self.assertEqual(data, {'bizOrderId': ''})

    def test_privacy_request_uses_modify_address_scene(self):
        api, data = proto.privacy_request(ORDER)
        self.assertEqual(api, proto.PRIVACY_API)
        self.assertEqual(data, {'bizOrderId': ORDER, 'pageSource': 'modify_address'})

    def test_read_builders_have_no_confirm_parameter(self):
        import inspect
        for fn in (proto.info_request, proto.privacy_request):
            self.assertNotIn('confirm', inspect.signature(fn).parameters, fn.__name__)

    def test_parse_info_waiting(self):
        parsed = proto.parse_info(INFO_WAITING)
        self.assertEqual(parsed['status'], 'WAITING')
        self.assertEqual(parsed['deliver_id'], '9001')
        self.assertTrue(parsed['can_operate'])
        self.assertEqual(parsed['now_address']['name'], '张*')
        self.assertEqual(parsed['modify_address']['name'], '张三')
        self.assertFalse(parsed['privacy_flagged'])

    def test_parse_info_without_request_gives_null_not_empty_dict(self):
        """没有申请时：`status` 用平台原文、`deliver_id`/两个 VO 给 `None`，**不编** `'NONE'` 或 `{}`。"""
        parsed = proto.parse_info(INFO_NONE)
        self.assertEqual(parsed['status'], 'NONE')
        self.assertIsNone(parsed['deliver_id'])
        self.assertIsNone(parsed['now_address'])
        self.assertIsNone(parsed['modify_address'])
        self.assertFalse(parsed['can_operate'])

    def test_parse_info_ignores_deliver_id_outside_waiting(self):
        """平台只在 WAITING 才给 `modifyDeliverId`；别的状态即使字段在也不能认。"""
        payload = {'success': True, 'data': {'module': {
            'modifyStatus': 'DONE', 'modifyDeliverId': '9001'}}}
        parsed = proto.parse_info(payload)
        self.assertEqual(parsed['status'], 'DONE')
        self.assertIsNone(parsed['deliver_id'])
        self.assertFalse(parsed['can_operate'])

    def test_parse_info_tolerates_garbage(self):
        for bad in (None, 'x', [], {'success': True}, {'success': True, 'data': {'module': 'x'}}):
            parsed = proto.parse_info(bad)
            self.assertIsNone(parsed['status'])
            self.assertIsNone(parsed['deliver_id'])
            self.assertFalse(parsed['can_operate'])

    def test_parse_privacy_json_string(self):
        parsed = proto.parse_privacy(PRIVACY_OK)
        self.assertEqual(parsed, {'name': '李四', 'suffix': '先生',
                                  'phone_number': '13900139000',
                                  'address': '北京市海淀区某路 3 号'})

    def test_parse_privacy_never_invents_plaintext(self):
        for bad in (None, {}, {'success': True, 'data': 'not json'},
                    {'success': True, 'data': '{}'}, {'success': False, 'data': {}}):
            self.assertIsNone(proto.parse_privacy(bad))

    def test_parse_write_shape(self):
        ok = proto.parse_write(PLAIN_OK)
        self.assertTrue(ok['platform_success'])
        # 成功时平台的 `message` 就是 `SUCCESS::调用成功`（这里只要求「不编」：原文透传）
        self.assertEqual(ok['platform_message'], 'SUCCESS::调用成功')
        bad = proto.parse_write(NO_PERMISSION)
        self.assertFalse(bad['platform_success'])
        self.assertEqual(bad['platform_message'], 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')

    def test_fixtures_still_match_the_parsers(self):
        """钉住测试数据本身：上面那几份响应体必须能被解析器认出（防测试漂移）。"""
        self.assertEqual(proto.parse_info(INFO_WAITING)['deliver_id'], '9001')
        self.assertEqual(proto.parse_info(INFO_PRIVACY)['deliver_id'], '9002')
        self.assertTrue(proto.parse_info(INFO_PRIVACY)['privacy_flagged'])
        self.assertEqual(trade_read.envelope_error(INFO_WAITING), '')
        self.assertEqual(trade_read.envelope_error(NO_PERMISSION),
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')


class OrderAddressReadTest(OrderAddressPageTest):
    """只读查询：没有确认闸门、不碰登录态、隐私号解不开就如实说。"""

    def test_read_returns_the_contract_shape(self):
        data = self.ok_data(self.get(self.info_path()))
        self.assertEqual(data['status'], 'WAITING')
        self.assertEqual(data['deliver_id'], '9001')
        self.assertTrue(data['can_operate'])
        self.assertEqual(data['order_id'], ORDER)
        self.assertEqual(data['account_id'], ACC)
        self.assertEqual(data['api'], proto.INFO_API)
        self.assertIn('fetched_at', data)
        # 无隐私标志时 `decrypted` 仍要显式出现（是 null，不是缺键）
        self.assertIn('decrypted', data)
        self.assertIsNone(data['decrypted'])
        self.assertEqual(data['warnings'], [])
        call = self.fake.call_of(proto.INFO_API)
        self.assertEqual(call['data'], {'bizOrderId': ORDER})
        self.assertEqual(call['v'], '1.0')

    def test_read_has_no_confirm_gate_and_never_touches_write_apis(self):
        self.ok_data(self.get(self.info_path()))
        self.assertEqual(self.fake.apis(), [proto.INFO_API])
        self.assertNotIn(proto.AGREE_API, self.fake.apis())
        self.assertNotIn(proto.REFUSE_API, self.fake.apis())

    def test_read_privacy_order_decrypts_with_value_type(self):
        self.fake = full_client(**{proto.INFO_API: INFO_PRIVACY})
        install_fake_protocol(self.fake)
        data = self.ok_data(self.get(self.info_path()))
        self.assertTrue(data['privacy_flagged'])
        self.assertEqual(data['decrypted'], {'name': '李四', 'suffix': '先生',
                                             'phone_number': '13900139000',
                                             'address': '北京市海淀区某路 3 号'})
        self.assertEqual([w['code'] for w in data['warnings']], ['privacy_flagged'])
        call = self.fake.call_of(proto.PRIVACY_API)
        self.assertEqual(call['value_type'], 'string')
        self.assertEqual(call['data'], {'bizOrderId': ORDER, 'pageSource': 'modify_address'})

    def test_read_privacy_failure_is_a_warning_not_an_error(self):
        """解密失败**不能**把整页判成失败：用户至少还能看到平台原样给的打码字段。"""
        self.fake = full_client(**{proto.INFO_API: INFO_PRIVACY, proto.PRIVACY_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        data = self.ok_data(self.get(self.info_path()))
        self.assertIsNone(data['decrypted'])
        self.assertEqual([w['code'] for w in data['warnings']],
                         ['privacy_flagged', 'privacy_decrypt_failed'])
        self.assertEqual(self.login_state(), 'active')

    def test_read_without_privacy_flag_never_calls_the_decrypt_api(self):
        self.ok_data(self.get(self.info_path()))
        self.assertNotIn(proto.PRIVACY_API, self.fake.apis())

    def test_read_platform_refusal_is_502_and_does_not_mark_relogin(self):
        self.fake = full_client(**{proto.INFO_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        payload = self.fail(self.get(self.info_path()), 502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.INFO_API)
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', payload['message'])
        self.assertEqual(self.login_state(), 'active')

    def test_read_session_expired_is_409_and_marks_relogin(self):
        self.fake = full_client()
        self.fake.errors[proto.INFO_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        install_fake_protocol(self.fake)
        self.fail(self.get(self.info_path()), 409)
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_read_without_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        install_fake_protocol(self.fake)
        self.fail(self.get(self.info_path()), 409)
        self.assertEqual(self.fake.apis(), [])

    def test_read_unknown_account_is_404(self):
        self.fail(self.get(self.info_path(account_id='nobody')), 404)

    def test_read_requires_login(self):
        self.fail(self.get(self.info_path(), headers={}), 401)


class OrderAddressWriteGateTest(OrderAddressPageTest):
    """同意 / 拒绝：红线是「未确认时一个平台请求都不发」。"""

    def test_write_without_confirm_is_400_and_sends_nothing(self):
        for path in (self.agree_path(), self.refuse_path()):
            resp = self.post(path, {'deliver_id': '9001'})
            payload = self.fail(resp, 400)
            self.assertEqual(payload['code'], 'confirmation_required')
        self.assertEqual(self.fake.apis(), [])

    def test_confirm_must_be_a_real_yes(self):
        """`'false'` **不是**同意 —— 对「改真实收货地址」这种操作，宽松判真会出人命。"""
        for bad in (None, False, 'false', 0, '', 'no', 2):
            for path in (self.agree_path(), self.refuse_path()):
                payload = self.fail(self.post(path, {'deliver_id': '9001', 'confirm': bad}), 400)
                self.assertEqual(payload['code'], 'confirmation_required')
        self.assertEqual(self.fake.apis(), [])

    def test_agree_happy_path(self):
        data = self.ok_data(self.post(self.agree_path(),
                                      {'deliver_id': '9001', 'confirm': True}))
        self.assertEqual(data['action'], 'agree')
        self.assertEqual(data['order_id'], ORDER)
        self.assertEqual(data['deliver_id'], '9001')
        self.assertEqual(data['api'], proto.AGREE_API)
        call = self.fake.call_of(proto.AGREE_API)
        self.assertEqual(call['data'], {'bizOrderId': ORDER, 'deliverId': '9001'})
        self.assertEqual(call['v'], '1.0')
        logged = [row['action'] for row in self.logs()]
        self.assertIn('order_address_agree', logged)

    def test_refuse_happy_path(self):
        data = self.ok_data(self.post(self.refuse_path(),
                                      {'deliver_id': '9002', 'confirm': True}))
        self.assertEqual(data['action'], 'refuse')
        self.assertEqual(data['api'], proto.REFUSE_API)
        self.assertEqual(self.fake.call_of(proto.REFUSE_API)['data'],
                         {'bizOrderId': ORDER, 'deliverId': '9002'})
        self.assertIn('order_address_refuse', [row['action'] for row in self.logs()])

    def test_missing_deliver_id_is_400_before_any_platform_call(self):
        for path in (self.agree_path(), self.refuse_path()):
            payload = self.fail(self.post(path, {'confirm': True}), 400)
            self.assertEqual(payload['code'], 'invalid_fields')
            self.assertIn('deliver_id', payload['message'])
        self.assertEqual(self.fake.apis(), [])

    def test_field_validation_runs_before_the_credential_check(self):
        """掉线 + 缺字段时，先说「缺 deliver_id」，而不是让用户去重新登录。"""
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake)
        payload = self.fail(self.post(self.agree_path(), {'confirm': True}), 400)
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])
        # 字段补齐后才走到凭据检查（409）
        self.fail(self.post(self.agree_path(), {'deliver_id': '9001', 'confirm': True}), 409)
        self.assertEqual(self.fake.apis(), [])

    def test_write_without_cookie_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        install_fake_protocol(self.fake)
        self.fail(self.post(self.agree_path(), {'deliver_id': '9001', 'confirm': True}), 409)
        self.assertEqual(self.fake.apis(), [])

    def test_platform_refusal_is_502_and_keeps_login_state(self):
        self.fake = full_client(**{proto.AGREE_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        payload = self.fail(self.post(self.agree_path(),
                                      {'deliver_id': '9001', 'confirm': True}), 502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', payload['message'])
        self.assertEqual(self.login_state(), 'active')

    def test_session_expired_is_409_and_marks_relogin(self):
        self.fake = full_client()
        self.fake.errors[proto.AGREE_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        install_fake_protocol(self.fake)
        self.fail(self.post(self.agree_path(), {'deliver_id': '9001', 'confirm': True}), 409)
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_write_unknown_account_is_404(self):
        self.fail(self.post(self.agree_path(account_id='nobody'),
                            {'deliver_id': '9001', 'confirm': True}), 404)

    def test_write_requires_login(self):
        self.fail(self.post(self.agree_path(), {'deliver_id': '9001', 'confirm': True},
                            headers={}), 401)


class OrderAddressRouteTest(unittest.TestCase):
    """路由形状：一条 GET + 两条 POST（读的那条**不能**是 POST，否则会被当成写操作）。"""

    def routes(self):
        return {route.path: list(route.methods) for route in trade_write_api.router.routes}

    def test_read_route_is_a_get(self):
        methods = self.routes().get(f'{BASE}/{{account_id}}/orders/{{order_id}}/modify-address')
        self.assertIsNotNone(methods)
        self.assertEqual(methods, ['GET'])

    def test_write_routes_are_posts(self):
        routes = self.routes()
        self.assertEqual(
            routes.get(f'{BASE}/{{account_id}}/orders/{{order_id}}/modify-address/agree'),
            ['POST'])
        self.assertEqual(
            routes.get(f'{BASE}/{{account_id}}/orders/{{order_id}}/modify-address/refuse'),
            ['POST'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
