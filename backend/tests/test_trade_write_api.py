# -*- coding: utf-8 -*-
"""交易组**写**路由的契约测试（发货 / 改价 / 退款处理 / 评价 / 投诉处理）。

这一组最容易犯的五个错，逐条都有用例：

1. **没确认就写**：每个写接口都必须先拿到 `confirm=true`，否则一个平台请求都不许发；
2. **金额单位**：平台口径是**分**（`Math.round(100*价格).toString()`），服务层收元、协议层换算一次；
3. **平台没给的字段补 0**：发货计数平台没返回就必须是 `None`；
4. **把无权限当成掉线**：`NO_PERMISSION` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 是 `protocol_error`，
   **不许**把账号写成 `need_relogin`（用户会白扫一次码）；
5. **失败信封被吞**：平台失败要原样透出（带 `platform_errors.explain`）。

**接口名与参数形状的来源**：官方微应用 bundle 静态反解（`idle-seller-trade/0.0.26/js/main.js`，
偏移见 `backend/app/xianyu/trade_write.py` 每个常量上方注释），**全部未经真机验证** ——
写接口禁止对真实平台试打。下面所有样本都是**按 bundle 形状手写的假响应**，不是真机原文。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_trade_write_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import trade_write as trade_write_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import common as svc_common  # noqa: E402
from backend.app.services import trade_write as trade_write_svc  # noqa: E402
from backend.app.xianyu import trade as trade_proto  # noqa: E402
from backend.app.xianyu import trade_write as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'trade_write_acc'
BASE = '/api/trade'

#: 假响应：发货（`data.{totalNum,successNum}`，bundle 偏移 2624843 附近读的就是这两个键）
SHIP_OK = {'success': True, 'data': {'totalNum': 2, 'successNum': 2},
           'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：发货只回了一个计数（用来证明**没返回的字段是 None 而不是 0**）
SHIP_PARTIAL = {'success': True, 'data': {'successNum': 1},
                'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：无需物流发货
SHIP_DUMMY_OK = {'success': True, 'data': {},
                 'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：改价
ADJUST_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
             'ret': ['SUCCESS::调用成功']}

#: 假响应：同意退款
REFUND_AGREE_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
                   'ret': ['SUCCESS::调用成功']}

#: 假响应：拒绝退款
REFUND_REFUSE_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
                    'ret': ['SUCCESS::调用成功']}

#: 假响应：评价（bundle 读 `data.module.{success,successOrderIds,failOrderInfos}`）
RATE_OK = {'success': True, 'data': {'module': {
    'success': True, 'successOrderIds': ['3317095524016013282'], 'failOrderInfos': []}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：投诉拒绝成功
COMPLAIN_OK = {'success': True, 'data': {'success': True}, 'message': 'SUCCESS::调用成功',
               'ret': ['SUCCESS::调用成功']}

#: 假响应：投诉**业务级**失败 —— 顶层 success 仍是 true，失败在 `data.success/failReason`
COMPLAIN_BIZ_FAIL = {'success': True, 'data': {'success': False, 'failReason': '凭证不足，请补充'},
                     'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：无权限（真机 `complain.list` / `address.list.query` 就是这个形状）
NO_PERMISSION = {'success': False, 'message': 'NO_PERMISSION::用户没有权限',
                 'ret': ['NO_PERMISSION::用户没有权限']}

#: 假响应：卖家工作台会话无权限（真机原文）
UNAUTHORIZED = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeTradeClient:
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


def install_fake_protocol(client: FakeTradeClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeTradeClient:
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


def full_client(**overrides: Any) -> FakeTradeClient:
    payloads = {
        proto.SHIP_OFFLINE_API: SHIP_OK,
        proto.SHIP_DUMMY_API: SHIP_DUMMY_OK,
        proto.ADJUST_PRICE_API: ADJUST_OK,
        proto.REFUND_AGREE_API: REFUND_AGREE_OK,
        proto.REFUND_REFUSE_API: REFUND_REFUSE_OK,
        proto.RATE_CREATE_API: RATE_OK,
        proto.COMPLAIN_REFUSE_API: COMPLAIN_OK,
        proto.COMPLAIN_PROOF_APIS['sellerSubmitProof']: COMPLAIN_OK,
        proto.COMPLAIN_PROOF_APIS['submitActiveProof']: COMPLAIN_OK,
        proto.COMPLAIN_PROOF_APIS['submitPassiveProof']: COMPLAIN_OK,
    }
    payloads.update(overrides)
    return FakeTradeClient(payloads)


class TradeWritePageTest(unittest.TestCase):
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
            "INSERT INTO users(username, password_hash, is_admin) VALUES('tradewrite','x',1)"))
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


class WriteConfirmGateTest(TradeWritePageTest):
    """闸门：没有 `confirm` 就一个平台请求都不许发。"""

    def test_every_write_route_refuses_without_confirm(self):
        cases = [
            (f'{BASE}/{ACC}/orders/ship', {'trade_ids': ['T1'], 'mail_no': 'SF1',
                                           'cp_code': 'SF', 'address_id': 1}),
            (f'{BASE}/{ACC}/orders/O1/ship-dummy', {}),
            (f'{BASE}/{ACC}/orders/O1/adjust-price', {'modify_fee': '10.00',
                                                      'new_transport_fee': '0.00'}),
            (f'{BASE}/{ACC}/refunds/R1/agree', {'auth_token': 'tok'}),
            (f'{BASE}/{ACC}/refunds/R1/refuse', {'order_id': 'O1', 'refuse_reason_id': '9'}),
            (f'{BASE}/{ACC}/evaluations', {'trade_ids': ['T1'], 'feedback': '很好'}),
            (f'{BASE}/{ACC}/complaints/C1/refuse', {'proof_desc': '不是我的问题',
                                                    'proof_urls': ['https://img/a.jpg']}),
            (f'{BASE}/{ACC}/complaints/C1/proof', {'proof_desc': '凭证',
                                                   'proof_urls': ['https://img/a.jpg']}),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                payload = self.fail(path, body, status=400)
                self.assertEqual(payload.get('code'), 'confirmation_required', payload)
                self.assertIn('confirm', payload.get('message', ''))
        self.assertEqual(self.fake.apis(), [], '未确认时不许调用任何平台接口')

    def test_confirm_false_is_not_a_platform_call_either(self):
        payload = self.fail(f'{BASE}/{ACC}/orders/O1/ship-dummy', {'confirm': False})
        self.assertEqual(payload.get('code'), 'confirmation_required')
        self.assertEqual(self.fake.apis(), [])

    def test_protocol_layer_also_refuses_without_confirm(self):
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.ship_dummy_request('O1')
        with self.assertRaises(proto.WriteNotConfirmed):
            proto.adjust_price_request('O1', '1', '0')


class WriteShipTest(TradeWritePageTest):
    """发货：`orderInfos` 是 JSON 字符串、`addressId` 是数字、计数不许补 0。"""

    def test_ship_offline_shape_matches_the_bundle(self):
        data = self.ok_data(f'{BASE}/{ACC}/orders/ship', {
            'trade_ids': ['3317095524016013282', '3317095524016013283'],
            'mail_no': 'SF1234567890', 'cp_code': 'SF', 'address_id': 42, 'confirm': True})
        call = self.fake.call_of(proto.SHIP_OFFLINE_API)
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['data']['mailNo'], 'SF1234567890')
        self.assertEqual(call['data']['cpCode'], 'SF')
        self.assertEqual(call['data']['addressId'], 42)
        # orderInfos 必须是 **JSON 字符串**（bundle 里是 JSON.stringify）
        self.assertIsInstance(call['data']['orderInfos'], str)
        self.assertEqual(json.loads(call['data']['orderInfos']),
                         [{'tradeId': '3317095524016013282'}, {'tradeId': '3317095524016013283'}])
        self.assertEqual(data['total_num'], 2)
        self.assertEqual(data['success_num'], 2)
        self.assertEqual(data['action'], 'ship_offline')

    def test_ship_counts_are_none_when_platform_did_not_send_them(self):
        self.fake.payloads[proto.SHIP_OFFLINE_API] = SHIP_PARTIAL
        data = self.ok_data(f'{BASE}/{ACC}/orders/ship', {
            'trade_ids': ['T1'], 'mail_no': 'SF1', 'cp_code': 'SF', 'address_id': 1,
            'confirm': True})
        self.assertEqual(data['success_num'], 1)
        self.assertIsNone(data['total_num'], '平台没返回 totalNum 就是 None，不许补 0')
        self.assertIsNone(data['fail_num'])

    def test_ship_dummy_shape_matches_the_bundle(self):
        self.ok_data(f'{BASE}/{ACC}/orders/O1/ship-dummy', {
            'trade_text': '虚拟发货', 'pic_urls': ['https://img/a.jpg'], 'confirm': True})
        call = self.fake.call_of(proto.SHIP_DUMMY_API)
        self.assertEqual(call['data']['orderId'], 'O1')
        self.assertEqual(call['data']['tradeText'], '虚拟发货')
        self.assertEqual(call['data']['newUnconsign'], True)
        self.assertIsInstance(call['data']['picList'], str)
        self.assertEqual(json.loads(call['data']['picList']), ['https://img/a.jpg'])

    def test_ship_writes_an_operation_log(self):
        self.ok_data(f'{BASE}/{ACC}/orders/ship', {
            'trade_ids': ['T1'], 'mail_no': 'SF1', 'cp_code': 'SF', 'address_id': 1,
            'confirm': True})
        rows = self.logs()
        self.assertTrue(any(row['action'] == 'ship_offline' and row['success'] for row in rows),
                        [dict(r) for r in rows])


class WriteAdjustPriceTest(TradeWritePageTest):
    """改价：服务层收**元**，平台收**分**；官方前端拒绝 0 元商品价。"""

    def test_price_is_converted_to_fen(self):
        self.ok_data(f'{BASE}/{ACC}/orders/O1/adjust-price', {
            'modify_fee': '39.90', 'new_transport_fee': '0', 'confirm': True})
        call = self.fake.call_of(proto.ADJUST_PRICE_API)
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['data']['orderId'], 'O1')
        self.assertEqual(call['data']['modifyFee'], '3990')
        self.assertEqual(call['data']['newTransportFee'], '0')
        self.assertIsInstance(call['data']['modifyFee'], str)

    def test_zero_price_is_rejected_before_calling_platform(self):
        payload = self.fail(f'{BASE}/{ACC}/orders/O1/adjust-price',
                            {'modify_fee': '0', 'new_transport_fee': '0', 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_negative_and_non_numeric_amounts_are_rejected(self):
        for body in ({'modify_fee': '-1', 'new_transport_fee': '0'},
                     {'modify_fee': 'abc', 'new_transport_fee': '0'},
                     {'modify_fee': '10', 'new_transport_fee': '-0.5'}):
            with self.subTest(body=body):
                payload = self.fail(f'{BASE}/{ACC}/orders/O1/adjust-price',
                                    dict(body, confirm=True))
                self.assertEqual(payload.get('code'), 'invalid_fields', payload)
        self.assertEqual(self.fake.apis(), [])


class WriteRefundTest(TradeWritePageTest):
    """退款：同意要 `auth_token`；拒绝的 proof/negotiation 没内容就不传。"""

    def test_refund_agree_passes_the_token_through(self):
        self.ok_data(f'{BASE}/{ACC}/refunds/R1/agree',
                     {'auth_token': 'auth-token-from-platform', 'confirm': True})
        call = self.fake.call_of(proto.REFUND_AGREE_API)
        self.assertEqual(call['data'], {'refundId': 'R1', 'authToken': 'auth-token-from-platform'})

    def test_refund_agree_without_token_is_invalid_fields(self):
        payload = self.fail(f'{BASE}/{ACC}/refunds/R1/agree', {'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_refund_refuse_omits_empty_proof_and_negotiation(self):
        self.ok_data(f'{BASE}/{ACC}/refunds/R1/refuse',
                     {'order_id': 'O1', 'refuse_reason_id': '12', 'confirm': True})
        data = self.fake.call_of(proto.REFUND_REFUSE_API)['data']
        self.assertEqual(data['refundId'], 'R1')
        self.assertEqual(data['orderId'], 'O1')
        self.assertEqual(data['refuseReasonId'], '12')
        # 官方对空内容是 void 0（整个键不传），不是传空字符串
        self.assertNotIn('refuseProof', data)
        self.assertNotIn('negotiationApply', data)

    def test_refund_refuse_sends_proof_and_negotiation_when_given(self):
        self.ok_data(f'{BASE}/{ACC}/refunds/R1/refuse', {
            'order_id': 'O1', 'refuse_reason_id': '12',
            'proof_desc': '已按约定发货', 'proof_urls': ['https://img/a.jpg'],
            'negotiation': {'negotiationRefundFee': '0', 'idleNegotiationType': '1'},
            'confirm': True})
        data = self.fake.call_of(proto.REFUND_REFUSE_API)['data']
        proof = json.loads(data['refuseProof'])
        self.assertEqual(proof['desc'], '已按约定发货')
        self.assertEqual(proof['proofMultiMediaList'],
                         [{'mediaTypeEnum': 'IMAGE', 'url': 'https://img/a.jpg'}])
        self.assertEqual(json.loads(data['negotiationApply'])['idleNegotiationType'], '1')


class WriteRateTest(TradeWritePageTest):
    """评价：卖家侧只有 `rate.create`（回评也走它），`rate` 默认 -1。"""

    def test_rate_create_shape_matches_the_bundle(self):
        data = self.ok_data(f'{BASE}/{ACC}/evaluations', {
            'trade_ids': ['T1'], 'feedback': '发货很快', 'image_urls': ['https://img/a.jpg'],
            'anonymous': True, 'confirm': True})
        call = self.fake.call_of(proto.RATE_CREATE_API)
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['data']['tradeIdList'], ['T1'])
        self.assertEqual(call['data']['feedback'], '发货很快')
        self.assertEqual(call['data']['rate'], -1)
        self.assertEqual(call['data']['imageUrls'], ['https://img/a.jpg'])
        self.assertEqual(call['data']['anonymous'], True)
        self.assertEqual(data['rate_success'], True)
        self.assertEqual(data['success_order_ids'], ['3317095524016013282'])

    def test_feedback_is_required(self):
        payload = self.fail(f'{BASE}/{ACC}/evaluations', {'trade_ids': ['T1'], 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])


class WriteComplainTest(TradeWritePageTest):
    """投诉：三种举证走三个 mtop 名；业务级失败（顶层 success=true）也要翻成失败。"""

    def test_complain_refuse_shape_matches_the_bundle(self):
        self.ok_data(f'{BASE}/{ACC}/complaints/C1/refuse', {
            'proof_desc': '买家恶意投诉', 'proof_urls': ['https://img/a.jpg'], 'confirm': True})
        data = self.fake.call_of(proto.COMPLAIN_REFUSE_API)['data']
        self.assertEqual(data['complainId'], 'C1')
        self.assertEqual(data['proofDesc'], '买家恶意投诉')
        self.assertEqual(json.loads(data['proofMediaList']),
                         [{'type': 'image', 'url': 'https://img/a.jpg'}])

    def test_proof_kind_selects_the_right_api(self):
        kinds = {
            'sellerSubmitProof': proto.COMPLAIN_PROOF_APIS['sellerSubmitProof'],
            'submitActiveProof': proto.COMPLAIN_PROOF_APIS['submitActiveProof'],
            'submitPassiveProof': proto.COMPLAIN_PROOF_APIS['submitPassiveProof'],
        }
        for kind, api in kinds.items():
            with self.subTest(kind=kind):
                self.fake.calls.clear()
                self.ok_data(f'{BASE}/{ACC}/complaints/C1/proof', {
                    'proof_desc': '凭证', 'proof_urls': ['https://img/a.jpg'],
                    'kind': kind, 'confirm': True})
                self.assertEqual(self.fake.apis(), [api])

    def test_unknown_proof_kind_is_invalid_fields(self):
        payload = self.fail(f'{BASE}/{ACC}/complaints/C1/proof', {
            'proof_desc': '凭证', 'proof_urls': ['https://img/a.jpg'],
            'kind': 'notAKind', 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_proof_urls_are_required(self):
        payload = self.fail(f'{BASE}/{ACC}/complaints/C1/proof',
                            {'proof_desc': '凭证', 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.apis(), [])

    def test_business_level_failure_is_not_reported_as_success(self):
        self.fake.payloads[proto.COMPLAIN_REFUSE_API] = COMPLAIN_BIZ_FAIL
        resp = self.post(f'{BASE}/{ACC}/complaints/C1/refuse', {
            'proof_desc': '买家恶意投诉', 'proof_urls': ['https://img/a.jpg'], 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        self.assertIn('凭证不足', payload.get('message', ''))
        self.assertEqual(self.login_state(), 'active')


class WritePlatformFailureTest(TradeWritePageTest):
    """平台失败原样透出；**无权限不是掉线**。"""

    def test_no_permission_is_a_protocol_error_and_not_a_logout(self):
        self.fake.payloads[proto.COMPLAIN_REFUSE_API] = NO_PERMISSION
        payload = self.fail(f'{BASE}/{ACC}/complaints/C1/refuse',
                            {'proof_desc': 'x', 'proof_urls': ['https://img/a.jpg'],
                             'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('NO_PERMISSION', payload.get('message', ''))
        self.assertEqual(payload.get('platform_errors', {}).get('api'), proto.COMPLAIN_REFUSE_API)
        self.assertEqual(self.login_state(), 'active', '无权限 ≠ 掉线，不许写成 need_relogin')

    def test_unauthorized_is_a_protocol_error_and_not_a_logout(self):
        self.fake.payloads[proto.REFUND_AGREE_API] = UNAUTHORIZED
        payload = self.fail(f'{BASE}/{ACC}/refunds/R1/agree',
                            {'auth_token': 'tok', 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', payload.get('message', ''))
        self.assertEqual(self.login_state(), 'active')

    def test_platform_failure_is_logged_as_a_failure(self):
        self.fake.payloads[proto.REFUND_AGREE_API] = UNAUTHORIZED
        self.fail(f'{BASE}/{ACC}/refunds/R1/agree', {'auth_token': 'tok', 'confirm': True},
                  status=502)
        rows = self.logs()
        self.assertTrue(any(row['action'] == 'refund_agree' and not row['success'] for row in rows),
                        [dict(r) for r in rows])


class WriteCredentialTest(TradeWritePageTest):
    """凭据与登录态（与只读层同一套判定，复用 `services/trade._client`）。"""

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        payload = self.fail(f'{BASE}/{ACC}/orders/O1/ship-dummy', {'confirm': True}, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.fake.apis(), [])

    def test_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        payload = self.fail(f'{BASE}/{ACC}/orders/O1/ship-dummy', {'confirm': True}, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.fake.apis(), [])

    def test_unknown_account_is_404(self):
        payload = self.fail(f'{BASE}/nope/orders/O1/ship-dummy', {'confirm': True}, status=404)
        self.assertEqual(payload.get('code'), 'not_found')

    def test_session_expired_marks_the_account(self):
        self.fake.errors[proto.SHIP_DUMMY_API] = SessionExpiredError('登录态已过期，需要重新登录')
        payload = self.fail(f'{BASE}/{ACC}/orders/O1/ship-dummy', {'confirm': True}, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_requests_require_login(self):
        for path in (f'{BASE}/{ACC}/orders/O1/ship-dummy', f'{BASE}/{ACC}/evaluations'):
            with self.subTest(path=path):
                resp = self.post(path, {'confirm': True}, headers={})
                self.assertEqual(resp.status_code, 401, resp.text)


class WriteContractTest(TradeWritePageTest):
    """静态提取的契约本身也要锁住：名字进了只读黑名单、版本、必填闸门。"""

    def test_write_apis_are_blacklisted_in_the_read_only_layer(self):
        """只读层认识的写接口必须真的被 `assert_read_only` 拦住。

        只读层（`backend/app/xianyu/trade.py`）的 `WRITE_APIS` 必须是写协议层
        `trade_write.WRITE_APIS` 的**超集** —— 少一个，只读层就可能悄悄调用它。
        本任务交付时这里曾有 4 个缺口（发货两个 + 投诉举证 active/passive），
        Lead 已在 `trade.py` 补录，所以现在断言的是「无缺口 + 全被拦」。
        """
        missing = {api for api in proto.WRITE_APIS if api not in trade_proto.WRITE_APIS}
        self.assertEqual(missing, set(), f'只读层写黑名单漏了 {sorted(missing)}，去补 trade.WRITE_APIS')
        for api in proto.WRITE_APIS:
            with self.subTest(api=api):
                self.assertIn(api, trade_proto.WRITE_APIS)
                with self.assertRaises(trade_proto.ReadOnlyViolation):
                    trade_proto.assert_read_only(api)

    def test_versions_are_the_ones_in_the_bundle(self):
        self.assertEqual(proto.SHIP_OFFLINE_VERSION, '1.0')
        self.assertEqual(proto.SHIP_DUMMY_VERSION, '1.0')
        self.assertEqual(proto.ADJUST_PRICE_VERSION, '1.0')
        self.assertEqual(proto.REFUND_AGREE_VERSION, '1.0')
        self.assertEqual(proto.REFUND_REFUSE_VERSION, '1.0')
        self.assertEqual(proto.RATE_CREATE_VERSION, '1.0')
        self.assertEqual(proto.COMPLAIN_REFUSE_VERSION, '1.0')
        self.assertEqual(proto.COMPLAIN_PROOF_VERSION, '1.0')

    def test_yuan_to_fen_is_the_only_conversion(self):
        self.assertEqual(proto.yuan_to_fen('39.90'), '3990')
        self.assertEqual(proto.yuan_to_fen(0.01), '1')
        self.assertEqual(proto.yuan_to_fen('1.005'), '100')
        with self.assertRaises(ValueError):
            proto.yuan_to_fen('')

    def test_confirm_code_is_not_a_known_failure_code(self):
        # 未确认要落 400（未知 code 的默认档），不能误判成 502/409
        self.assertNotIn(trade_write_svc.CONFIRM_CODE, svc_common.FAIL_STATUS)
        status, _ = svc_common.respond(svc_common.fail('x', trade_write_svc.CONFIRM_CODE))
        self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
