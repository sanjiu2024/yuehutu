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
from backend.app.xianyu import compensate as cp_proto  # noqa: E402
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

#: 假响应：退款凭证（只读，bundle 读 `data.module.{verifyUrl,token}`）
VERIFY_OK = {'success': True, 'data': {'module': {
    'verifyUrl': 'https://verify.alipay.example/refund?x=1', 'token': 'VTOKEN-ABC'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：平台把凭证字段回了空串（非管理员态官方就是这样，不能拿 `''` 当可用凭证）
VERIFY_EMPTY = {'success': True, 'data': {'module': {'verifyUrl': '', 'token': ''}},
                'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：无权限（真机 `complain.list` / `address.list.query` 就是这个形状）
NO_PERMISSION = {'success': False, 'message': 'NO_PERMISSION::用户没有权限',
                 'ret': ['NO_PERMISSION::用户没有权限']}

#: 假响应：卖家工作台会话无权限（真机原文）
UNAUTHORIZED = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

# --- 第四十五批：赔付 / 维权（形状按 bundle 手写，不是真机原文） -----------------

#: `…compensate.service.detail.query`：官方带 `valueType:'string'`，`data.module` 才是业务体
COMP_DETAIL = {'api': cp_proto.DETAIL_API, 'ret': ['SUCCESS::调用成功'],
               'data': {'module': {
                   'orderId': 'O1', 'serviceCode': 'S1', 'amount': 1250,
                   'elements': [{'title': {'text': '平台判定需赔付 12.50 元'},
                                 'desc': '买家举证成立'}],
                   'serviceInfo': {'serviceIntroduce': '这是一笔赔付'},
               }}}
#: 同一条，但整个 `data` 是 JSON **字符串**（真机 `valueType:'string'` 就是这个形状）
COMP_DETAIL_STRING = {'api': cp_proto.DETAIL_API, 'ret': ['SUCCESS::调用成功'],
                      'data': json.dumps(COMP_DETAIL['data'], ensure_ascii=False)}
#: 平台没给 module（`data` 是空字典）
COMP_DETAIL_EMPTY = {'api': cp_proto.DETAIL_API, 'ret': ['SUCCESS::调用成功'], 'data': {}}
COMP_REASONS = {'api': cp_proto.REFUSE_FORM_API, 'ret': ['SUCCESS::调用成功'],
                'data': {'module': {'reasonChoices': ['原因A', '原因B']}}}
COMP_REASONS_MISSING = {'api': cp_proto.REFUSE_FORM_API, 'ret': ['SUCCESS::调用成功'],
                        'data': {'module': {'other': 1}}}
COMP_REFUSE_OK = {'api': cp_proto.REFUSE_API, 'ret': ['SUCCESS::调用成功'],
                  'data': {'module': {'success': True, 'msgId': 'm-1'}}}
COMP_REFUSE_NOFLAG = {'api': cp_proto.REFUSE_API, 'ret': ['SUCCESS::调用成功'],
                      'data': {'module': {}}}
COMP_REFUSE_FAIL = {'api': cp_proto.REFUSE_API, 'ret': ['SUCCESS::调用成功'],
                    'data': {'module': {'success': False, 'failReason': '凭证看不清'}}}
COMP_REFUSE_UNAUTHORIZED = {'api': cp_proto.REFUSE_API, 'ret': ['SUCCESS::调用成功'],
                            'data': {'module': {'success': False,
                                                'failReason': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'}}}
#: 第四十九批：订单备注（`mtop.taobao.idle.merchant.add.memo`）—— 官方这条**没有回执字段**，
#: 成功只有信封；`MEMO_NO_ECHO` 就是「平台收下了但什么都没回」的真实形状。
MEMO_OK = {'success': True, 'data': {'memo': '已电话确认', 'memoGroup': 2},
           'message': 'SUCCESS::调用成功'}
MEMO_NO_ECHO = {'success': True, 'message': 'SUCCESS::调用成功'}

ADJUST_RENDER_OK = {'success': True, 'message': 'SUCCESS::调用成功',
                    'data': {'module': {
                        'orderId': '42',
                        'address': '浙江省杭州市',
                        'modifyPriceTips': '改价后买家需重新确认',
                        'merchantItemVO': {'itemId': 7, 'itemPicUrl': 'u', 'title': 'T'},
                        'merchantPriceVO': {'auctionPrice': '199.00', 'buyNum': 1,
                                            'discountFee': '10.00',
                                            'totalPrice': '199.00', 'postFee': '10.00'}}}}
ADJUST_RENDER_DERIVED = {'success': True, 'message': 'SUCCESS::调用成功',
                         'data': {'module': {
                             'orderId': '42',
                             'merchantItemVO': {'itemId': 7},
                             'merchantPriceVO': {'auctionPrice': '199.00', 'buyNum': 1,
                                                 'totalPrice': '199.00', 'postFee': '10.00'}}}}
ADJUST_RENDER_MISSING = {'success': True, 'message': 'SUCCESS::调用成功', 'data': {}}
ADJUST_RENDER_NO_PRICES = {'success': True, 'message': 'SUCCESS::调用成功',
                           'data': {'module': {'orderId': '42',
                                               'merchantItemVO': {'itemId': 7},
                                               'merchantPriceVO': {'buyNum': 1}}}}

#: 确认闸门的反例（`True`/`'true'`/`1` 之外的都不算点头）
FALSE_LIKE = (False, None, 0, 'false', 'FALSE', 'no', 'yes', '', [], {})


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""

class FakeTradeClient:
    """假协议层：只实现 `mtop`，记录每次调用的 (api, data, v)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0', **kwargs: Any) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v, 'kw': kwargs})
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
        proto.VERIFY_URL_API: VERIFY_OK,
        cp_proto.DETAIL_API: COMP_DETAIL,
        cp_proto.REFUSE_FORM_API: COMP_REASONS,
        cp_proto.REFUSE_API: COMP_REFUSE_OK,
        proto.ADD_MEMO_API: MEMO_OK,
        proto.ADJUST_PRICE_RENDER_API: ADJUST_RENDER_OK,
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

    def get(self, path: str, headers: Optional[Dict[str, str]] = None):
        return self.client.get(path, headers=self.headers if headers is None else headers)

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


class RefundVerifyTokenTest(TradeWritePageTest):
    """退款凭证（只读）：`GET …/refunds/{refund_id}/verify-token`。

    它解决的问题：同意退款的 `auth_token` **不在退款列表帧里**（真机 `btnList[].clickEvent.data.url`
    是空串），用户在我们界面上无处可抄。官方前端的做法是先查 `mtop.idle.alipay.verify.url.query`
    拿 `data.module.token`。这条**只查不改**，所以**故意没有 confirm 闸门**。
    """

    def path(self, refund_id: str = 'R1') -> str:
        return f'{BASE}/{ACC}/refunds/{refund_id}/verify-token'

    def test_shape_matches_the_bundle(self):
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        data = payload['data']
        self.assertEqual(data.get('verify_url'), 'https://verify.alipay.example/refund?x=1')
        self.assertEqual(data.get('token'), 'VTOKEN-ABC')
        self.assertEqual(data.get('scene'), 'REFUND_PC')
        self.assertEqual(data.get('refund_id'), 'R1')
        self.assertEqual(data.get('api'), proto.VERIFY_URL_API)
        call = self.fake.call_of(proto.VERIFY_URL_API)
        self.assertEqual(call['v'], proto.VERIFY_URL_VERSION)
        self.assertEqual(call['data'], {
            'bizId': 'R1',
            'scene': proto.VERIFY_SCENE_REFUND,
            'callBackUrl': proto.VERIFY_CALLBACK_URL,
        })

    def test_has_no_confirm_gate(self):
        """只读接口不该有确认闸门：不带任何 body 也能拿到结果，且**确实打了平台**。"""
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.fake.apis(), [proto.VERIFY_URL_API])

    def test_empty_strings_are_none_not_an_usable_token(self):
        self.fake.payloads[proto.VERIFY_URL_API] = VERIFY_EMPTY
        payload = self.get(self.path()).json()
        self.assertTrue(payload.get('success'), payload)
        self.assertIsNone(payload['data'].get('token'), '空串不许当成可用凭证')
        self.assertIsNone(payload['data'].get('verify_url'))

    def test_platform_failure_is_a_protocol_error_and_not_a_logout(self):
        self.fake.payloads[proto.VERIFY_URL_API] = NO_PERMISSION
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'))
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('NO_PERMISSION', payload.get('message', ''))
        self.assertEqual(payload.get('platform_errors', {}).get('api'), proto.VERIFY_URL_API)
        self.assertEqual(self.login_state(), 'active', '无权限 ≠ 掉线')

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json().get('code'), 'verification_required')
        self.assertEqual(self.fake.apis(), [])

    def test_unknown_account_is_404(self):
        resp = self.get(f'{BASE}/nope/refunds/R1/verify-token')
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json().get('code'), 'not_found')

    def test_requires_login(self):
        resp = self.get(self.path(), headers={})
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_route_is_registered_as_get(self):
        methods = {route.path: sorted(route.methods) for route in trade_write_api.router.routes}
        self.assertEqual(methods.get('/api/trade/{account_id}/refunds/{refund_id}/verify-token'),
                         ['GET'])


class VerifyTokenProtocolTest(unittest.TestCase):
    """协议层：只读接口的签名与登记位置。"""

    def test_request_builder_has_no_confirm_parameter(self):
        import inspect
        self.assertNotIn('confirm', inspect.signature(proto.verify_url_request).parameters)

    def test_api_is_registered_as_read_not_write(self):
        self.assertIn(proto.VERIFY_URL_API, proto.READ_APIS)
        self.assertNotIn(proto.VERIFY_URL_API, proto.WRITE_APIS)
        self.assertNotIn(proto.VERIFY_URL_API, trade_proto.WRITE_APIS,
                         '只读接口不该进只读层的写黑名单')
        trade_proto.assert_read_only(proto.VERIFY_URL_API)  # 不抛异常即为通过

    def test_parser_never_invents_a_token(self):
        for raw in ({'success': True, 'data': {}},
                    {'success': True, 'data': {'module': {}}},
                    {'success': True, 'data': {'module': {'verifyUrl': '', 'token': ''}}}):
            with self.subTest(raw=raw):
                parsed = proto.parse_verify_result(raw)
                self.assertIsNone(parsed['token'])
                self.assertIsNone(parsed['verify_url'])
                self.assertEqual(parsed['scene'], proto.VERIFY_SCENE_REFUND)


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


# --------------------------------------------------------------------------- 关单 / 延时 / 提醒（第三十八批）

#: 假响应：关单原因候选（bundle @2598721 读 `data.closeReasons`，**字符串数组**）
CLOSE_REASONS_OK = {'success': True, 'data': {'closeReasons': ['买家不想要了', '其他原因']},
                    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 假响应：平台换了字段名（既不是 closeReasons 也不是 reasonList/reasons/list）
CLOSE_REASONS_MISSING = {'success': True, 'data': {'somethingElse': 1},
                         'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

CLOSE_ORDER_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
                  'ret': ['SUCCESS::调用成功']}

#: 假响应：批量延时（`data.module.{totalNum,successNum,failOrderInfos}`）
DELAY_OK = {'success': True, 'data': {'module': {
    'totalNum': 2, 'successNum': 1,
    'failOrderInfos': [{'orderId': '2', 'errorMsg': '订单状态不允许'}]}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

REMIND_OK = {'success': True,
             'data': {'module': {'totalNum': 1, 'successNum': 1, 'failOrderInfos': []}},
             'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

POSTAGE_PAY_OK = {'success': True, 'data': {'success': True}, 'message': 'SUCCESS::调用成功',
                  'ret': ['SUCCESS::调用成功']}

COMPLAIN_REVOKE_OK = {'success': True, 'data': {'success': True}, 'message': 'SUCCESS::调用成功',
                      'ret': ['SUCCESS::调用成功']}

#: 假响应：撤销投诉的**业务级**失败（顶层 success 仍是 true，失败在 `data.success`）
COMPLAIN_REVOKE_BIZ_FAIL = {'success': True,
                            'data': {'success': False, 'failReason': '投诉已处理，无法撤销'},
                            'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 第三十八批新增：五个写端点 + 一个只读端点
NEW_WRITE_CALLS = (
    ('close', BASE + '/%s/orders/1/close', {'close_reason': '买家不想要了'}),
    ('delay', BASE + '/%s/orders/1/delay', {'days': 3}),
    ('remind', BASE + '/%s/orders/1/remind', {}),
    ('postage-pay', BASE + '/%s/refunds/1/postage-pay', {'order_id': '1'}),
    ('complain-revoke', BASE + '/%s/complaints/1/revoke', {}),
)

#: 第三十八批之前就有的写端点（闸门同样只在服务层判）
LEGACY_WRITE_CALLS = (
    ('ship-dummy', BASE + '/%s/orders/1/ship-dummy', {}),
    ('adjust-price', BASE + '/%s/orders/1/adjust-price', {'price': '9.9'}),
)

#: 路径参数 → 测试用的假值（用来遍历**全部** POST 路由）
PATH_FILL = {'account_id': ACC, 'order_id': '1', 'refund_id': '1', 'complain_id': '1',
             'contact_id': '1'}


def fill_path(path: str) -> str:
    out = path
    for key, value in PATH_FILL.items():
        out = out.replace('{%s}' % key, value)
    return out


def post_routes() -> List[str]:
    return [route.path for route in trade_write_api.router.routes
            if 'POST' in getattr(route, 'methods', set())]


class ConfirmGateBypassTest(TradeWritePageTest):
    """★ 第三十八批真 bug 的回归：字符串 `"false"` 曾经能把写闸门强制成「已确认」。

    `bool('false')` 是 `True`（路由层曾用它改写 `confirm`），而 `not 'false'` 是 `False`
    （服务层曾有 10 处用裸的 `if not confirm:`）。两条路都会让 `{"confirm": "false"}`
    直接落到建客户端那一步 —— 打包版探针实测 `trade.ship_dummy` 回 **404 `not_found`**
    而不是 400 `confirmation_required`。现在：路由**原样透传**、服务层一律 `_truthy()`。

    这里遍历**本模块全部 POST 路由**（不手写清单），避免以后新增写接口时漏掉这条。
    """

    #: 一律不算点头（`_truthy` 只认 True / 'true' / 1）
    FALSE_LIKE = ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2)

    def setUp(self):
        super().setUp()
        # 「真点头能过闸门」那一条要真发出去，所以备好关单响应
        self.fake.payloads[proto.CLOSE_BY_SELLER_API] = CLOSE_ORDER_OK

    def test_every_write_route_refuses_false_like_confirm(self):
        routes = post_routes()
        # 19 条 POST（22 条路由里有 3 条只读 GET）—— 以后新增写接口时这个数只会涨
        self.assertGreaterEqual(len(routes), 19, routes)
        for path in routes:
            for value in self.FALSE_LIKE:
                resp = self.post(fill_path(path), {'confirm': value})
                self.assertEqual(resp.status_code, 400,
                                 f'{path} confirm={value!r} -> {resp.status_code} {resp.text}')
                self.assertEqual(resp.json().get('code'), 'confirmation_required',
                                 f'{path} confirm={value!r}')
            self.assertEqual(self.fake.calls, [],
                             f'{path}: 没确认却发了平台请求 {self.fake.apis()}')

    def test_missing_confirm_is_also_refused(self):
        for path in post_routes():
            resp = self.post(fill_path(path), {})
            self.assertEqual(resp.status_code, 400, f'{path} -> {resp.status_code}')
            self.assertEqual(resp.json().get('code'), 'confirmation_required', path)
        self.assertEqual(self.fake.calls, [])

    def test_true_like_confirm_does_open_the_gate(self):
        for value in (True, 'true', 'TRUE', ' true '):
            self.fake.calls.clear()
            resp = self.post(BASE + '/%s/orders/1/close' % ACC,
                             {'close_reason': '买家不想要了', 'confirm': value})
            self.assertEqual(resp.status_code, 200, f'confirm={value!r} -> {resp.text}')
            self.assertEqual(self.fake.apis(), [proto.CLOSE_BY_SELLER_API])


class CloseAndDelayTest(TradeWritePageTest):
    """关单 / 延时 / 提醒 / 运费退款 / 撤销投诉（第三十八批）。"""

    def setUp(self):
        super().setUp()
        self.fake.payloads.update({
            proto.CLOSE_REASON_API: CLOSE_REASONS_OK,
            proto.CLOSE_BY_SELLER_API: CLOSE_ORDER_OK,
            proto.BATCH_DELAY_API: DELAY_OK,
            proto.BATCH_REMIND_API: REMIND_OK,
            proto.POSTAGE_PAY_API: POSTAGE_PAY_OK,
            proto.COMPLAIN_REVOKE_API: COMPLAIN_REVOKE_OK,
        })

    # -- 协议层 ----------------------------------------------------------

    def test_close_order_puts_the_same_id_in_tid_and_biz_order_id(self):
        api, data = proto.close_order_request(123, '买家不想要了', confirm=True)
        self.assertEqual(api, proto.CLOSE_BY_SELLER_API)
        self.assertEqual(data, {'tid': '123', 'bizOrderId': '123',
                                'closeReason': '买家不想要了'})
        self.assertEqual(proto.CLOSE_BY_SELLER_VERSION, '2.0')

    def test_close_reason_request_is_just_the_order_id(self):
        api, data = proto.close_reason_request(7)
        self.assertEqual(api, proto.CLOSE_REASON_API)
        self.assertEqual(data, {'bizOrderId': '7'})

    def test_delay_time_is_days_in_seconds(self):
        for days, seconds in ((3, 259200), (5, 432000), (7, 604800), (10, 864000)):
            _, data = proto.delay_confirm_request(['1'], days, confirm=True)
            self.assertEqual(data['delayTime'], seconds, days)
            self.assertEqual(data['orderIdList'], ['1'])

    def test_delay_days_are_the_four_official_ones(self):
        for bad in (0, 1, 4, 30, 'x', None):
            with self.assertRaises(ValueError):
                proto.delay_confirm_request(['1'], bad, confirm=True)

    def test_order_id_list_takes_arrays_and_comma_strings(self):
        self.assertEqual(proto.order_id_list(['1', '2', '1']), ['1', '2'])
        self.assertEqual(proto.order_id_list('1,2, 3'), ['1', '2', '3'])
        self.assertEqual(proto.order_id_list('1，2'), ['1', '2'])

    def test_order_id_list_rejects_empty_and_too_many(self):
        for bad in ('', '  ', [], None, ',,'):
            with self.assertRaises(ValueError):
                proto.order_id_list(bad)
        with self.assertRaises(ValueError):
            proto.order_id_list([str(i) for i in range(proto.MAX_ORDER_IDS + 1)])

    def test_postage_pay_type_is_alipay_or_deposit(self):
        for pay_type in ('alipay', 'deposit'):
            _, data = proto.postage_refund_pay_request(1, 2, pay_type, confirm=True)
            self.assertEqual(data['payType'], pay_type)
        with self.assertRaises(ValueError):
            proto.postage_refund_pay_request(1, 2, 'wechat', confirm=True)

    def test_parse_close_reasons_never_invents_a_list(self):
        parsed = proto.parse_close_reasons(CLOSE_REASONS_OK)
        self.assertEqual(parsed['close_reasons'], ['买家不想要了', '其他原因'])
        missing = proto.parse_close_reasons(CLOSE_REASONS_MISSING)
        self.assertIsNone(missing['close_reasons'])
        self.assertIn('somethingElse', missing['raw_keys'])

    def test_parse_batch_result_keeps_none_when_module_is_missing(self):
        parsed = proto.parse_batch_result({'success': True, 'data': {}})
        self.assertIsNone(parsed['total_num'])
        self.assertIsNone(parsed['success_num'])
        self.assertIsNone(parsed['fail_order_infos'])

    def test_the_five_new_apis_are_in_the_write_whitelist(self):
        for api in (proto.CLOSE_BY_SELLER_API, proto.BATCH_DELAY_API, proto.BATCH_REMIND_API,
                    proto.POSTAGE_PAY_API, proto.COMPLAIN_REVOKE_API):
            self.assertIn(api, proto.WRITE_APIS)
        self.assertIn(proto.CLOSE_REASON_API, proto.READ_APIS)
        self.assertNotIn(proto.CLOSE_REASON_API, proto.WRITE_APIS)

    def test_the_read_only_layer_blacklists_the_new_write_apis(self):
        for api in (proto.POSTAGE_PAY_API, proto.COMPLAIN_REVOKE_API,
                    proto.CLOSE_BY_SELLER_API):
            self.assertIn(api, trade_proto.WRITE_APIS)

    # -- 只读：关单原因 --------------------------------------------------

    def test_close_reasons_is_read_only_and_has_no_confirm_gate(self):
        resp = self.get(BASE + '/%s/orders/1/close-reasons' % ACC)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['close_reasons'], ['买家不想要了', '其他原因'])
        self.assertEqual(data['warnings'], [])
        self.assertEqual(self.fake.apis(), [proto.CLOSE_REASON_API])
        self.assertEqual(self.fake.call_of(proto.CLOSE_REASON_API)['data'], {'bizOrderId': '1'})

    def test_close_reasons_missing_key_gives_a_warning_and_null(self):
        self.fake.payloads[proto.CLOSE_REASON_API] = CLOSE_REASONS_MISSING
        resp = self.get(BASE + '/%s/orders/1/close-reasons' % ACC)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertIsNone(data['close_reasons'])
        self.assertEqual([w['code'] for w in data['warnings']], ['close_reasons_missing'])

    def test_close_reasons_requires_the_order_id(self):
        payload = trade_write_svc.close_reasons(ACC, '   ')
        self.assertFalse(payload.get('success'), payload)
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    # -- 写：五个动作 ----------------------------------------------------

    def test_close_order_happy_path(self):
        data = self.ok_data(BASE + '/%s/orders/9/close' % ACC,
                            {'close_reason': '买家不想要了', 'confirm': True})
        self.assertEqual(data['action'], 'trade_close_order')
        self.assertEqual(self.fake.call_of(proto.CLOSE_BY_SELLER_API)['data'],
                         {'tid': '9', 'bizOrderId': '9', 'closeReason': '买家不想要了'})
        self.assertEqual(self.fake.call_of(proto.CLOSE_BY_SELLER_API)['v'], '2.0')
        rows = self.logs()
        self.assertTrue(any(row['action'] == 'trade_close_order' and row['success']
                            for row in rows), [dict(r) for r in rows])

    def test_close_order_needs_a_reason(self):
        payload = self.fail(BASE + '/%s/orders/9/close' % ACC, {'confirm': True})
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_delay_confirm_reports_counts(self):
        data = self.ok_data(BASE + '/%s/orders/1/delay' % ACC,
                            {'order_ids': ['1', '2'], 'days': 3, 'confirm': True})
        self.assertEqual(data['action'], 'trade_delay_confirm')
        self.assertEqual(data['total_num'], 2)
        self.assertEqual(data['success_num'], 1)
        self.assertEqual(data['fail_order_infos'],
                         [{'orderId': '2', 'errorMsg': '订单状态不允许'}])
        self.assertEqual(self.fake.call_of(proto.BATCH_DELAY_API)['data'],
                         {'orderIdList': ['1', '2'], 'delayTime': 259200})
        rows = self.logs()
        self.assertTrue(any(row['action'] == 'trade_delay_confirm' and row['success']
                            for row in rows), [dict(r) for r in rows])

    def test_delay_without_order_ids_uses_the_path_order(self):
        self.ok_data(BASE + '/%s/orders/7/delay' % ACC, {'days': 5, 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.BATCH_DELAY_API)['data'],
                         {'orderIdList': ['7'], 'delayTime': 432000})

    def test_remind_confirm_passes_remind_all(self):
        data = self.ok_data(BASE + '/%s/orders/1/remind' % ACC,
                            {'order_ids': '1,2', 'remind_all': True, 'confirm': True})
        self.assertEqual(data['action'], 'trade_remind_confirm')
        self.assertEqual(self.fake.call_of(proto.BATCH_REMIND_API)['data'],
                         {'orderIdList': ['1', '2'], 'remindAllOrder': True})

    def test_postage_pay_keeps_amount_out_of_the_request(self):
        data = self.ok_data(BASE + '/%s/refunds/5/postage-pay' % ACC,
                            {'order_id': '1', 'amount': '12.30', 'confirm': True})
        self.assertEqual(data['action'], 'trade_postage_refund_pay')
        self.assertEqual(data['amount'], '12.30')
        self.assertEqual(data['pay_type'], 'alipay')
        sent = self.fake.call_of(proto.POSTAGE_PAY_API)['data']
        self.assertNotIn('amount', sent)
        self.assertEqual(sent, {'orderId': '1', 'refundId': '5', 'payType': 'alipay'})

    def test_complain_revoke_happy_path(self):
        data = self.ok_data(BASE + '/%s/complaints/3/revoke' % ACC, {'confirm': True})
        self.assertEqual(data['action'], 'trade_complain_revoke')
        self.assertEqual(self.fake.call_of(proto.COMPLAIN_REVOKE_API)['data'],
                         {'complainId': '3'})

    def test_complain_revoke_business_failure_is_502(self):
        self.fake.payloads[proto.COMPLAIN_REVOKE_API] = COMPLAIN_REVOKE_BIZ_FAIL
        payload = self.fail(BASE + '/%s/complaints/3/revoke' % ACC, {'confirm': True},
                            status=502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertIn('无法撤销', json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.login_state(), 'active')

    # -- 平台拒绝 / 凭据 ------------------------------------------------

    def test_platform_refusal_is_502_and_not_a_logout(self):
        self.fake.payloads[proto.BATCH_DELAY_API] = UNAUTHORIZED
        payload = self.fail(BASE + '/%s/orders/1/delay' % ACC,
                            {'days': 3, 'confirm': True}, status=502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(self.login_state(), 'active')

    def test_missing_cookie_is_409_and_sends_nothing(self):
        db.execute('DELETE FROM account_credentials WHERE account_id=?', (ACC,))
        payload = self.fail(BASE + '/%s/orders/1/close' % ACC,
                            {'close_reason': 'x', 'confirm': True}, status=409)
        self.assertEqual(payload['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_and_sends_nothing(self):
        payload = self.fail(BASE + '/no-such-acc/orders/1/close',
                            {'close_reason': 'x', 'confirm': True}, status=404)
        self.assertEqual(payload['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.CLOSE_BY_SELLER_API] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        self.fail(BASE + '/%s/orders/1/close' % ACC,
                  {'close_reason': 'x', 'confirm': True}, status=409)
        self.assertEqual(self.login_state(), 'need_relogin')

    # -- 路由形状 --------------------------------------------------------

    def test_new_routes_are_registered_with_the_right_methods(self):
        paths = {(route.path, tuple(sorted(getattr(route, 'methods', ()))))
                 for route in trade_write_api.router.routes}
        self.assertIn((BASE + '/{account_id}/orders/{order_id}/close-reasons', ('GET',)), paths)
        for suffix in ('/close', '/delay', '/remind'):
            self.assertIn((BASE + '/{account_id}/orders/{order_id}' + suffix, ('POST',)), paths)
        self.assertIn((BASE + '/{account_id}/refunds/{refund_id}/postage-pay', ('POST',)), paths)
        self.assertIn((BASE + '/{account_id}/complaints/{complain_id}/revoke', ('POST',)), paths)


# --------------------------------------------------------------------------- 第四十五批：赔付 / 维权


class CompensateProtocolTest(unittest.TestCase):
    """协议层：接口名（逐字）、版本、读写切分、逐字载荷、解析四态。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(cp_proto.DETAIL_API,
                         'mtop.taobao.idle.merchant.compensate.service.detail.query')
        self.assertEqual(cp_proto.REFUSE_FORM_API,
                         'mtop.taobao.idle.merchant.compensate.service.refuse.render')
        self.assertEqual(cp_proto.REFUSE_API,
                         'mtop.taobao.idle.merchant.compensate.service.refuse')
        self.assertEqual(cp_proto.DEFAULT_VERSION, '1.0')
        self.assertEqual(cp_proto.DETAIL_VALUE_TYPE, 'string')
        self.assertEqual(cp_proto.REASON_DESC_MAX, 200)

    def test_read_and_write_sets_do_not_overlap(self):
        self.assertEqual(set(cp_proto.READ_APIS),
                         {cp_proto.DETAIL_API, cp_proto.REFUSE_FORM_API})
        self.assertEqual(cp_proto.WRITE_API_SET, {cp_proto.REFUSE_API})
        self.assertFalse(cp_proto.WRITE_API_SET & set(cp_proto.READ_APIS))

    def test_assert_write_only_accepts_the_write_api(self):
        self.assertEqual(cp_proto.assert_write(cp_proto.REFUSE_API), cp_proto.REFUSE_API)
        for api in cp_proto.READ_APIS:
            with self.assertRaises(cp_proto.UnknownApi):
                cp_proto.assert_write(api)

    def test_assert_read_rejects_the_write_api(self):
        self.assertEqual(cp_proto.assert_read(cp_proto.DETAIL_API), cp_proto.DETAIL_API)
        with self.assertRaises(cp_proto.UnknownApi):
            cp_proto.assert_read(cp_proto.REFUSE_API)

    def test_detail_request_shape(self):
        self.assertEqual(cp_proto.detail_request('O1', 'S1'), (cp_proto.DETAIL_API,
                                                               {'orderId': 'O1', 'serviceCode': 'S1'}))

    def test_refuse_form_request_shape(self):
        self.assertEqual(cp_proto.refuse_form_request('S1'), (cp_proto.REFUSE_FORM_API,
                                                              {'serviceCode': 'S1'}))

    def test_refuse_request_shape_is_verbatim(self):
        api, data = cp_proto.refuse_request('O1', 'S1', ['原因A', '原因B'], '不认可',
                                            ['u1', 'u2'], False, confirm=True)
        self.assertEqual(api, cp_proto.REFUSE_API)
        self.assertEqual(data, {
            'orderId': 'O1', 'serviceCode': 'S1',
            'sellerRefuseReason': '{"reasonChoices":["原因A","原因B"],"reasonDesc":"不认可"}',
            'refuseProofs': '["u1","u2"]', 'isManual': False,
        })

    def test_refuse_request_rejects_a_false_like_confirm(self):
        for value in FALSE_LIKE:
            with self.assertRaises(cp_proto.WriteNotConfirmed):
                cp_proto.refuse_request('O1', 'S1', ['原因A'], '', ['u1'], False, confirm=value)

    def test_refuse_validation_runs_in_the_protocol_layer(self):
        with self.assertRaises(ValueError):
            cp_proto.refuse_request('O1', 'S1', [], '', ['u1'], False, confirm=True)
        with self.assertRaises(ValueError):
            cp_proto.refuse_request('O1', 'S1', ['原因A'], '', [], False, confirm=True)
        with self.assertRaises(ValueError):
            cp_proto.refuse_request('O1', 'S1', ['原因A'], '字' * 201, ['u1'], False,
                                    confirm=True)
        with self.assertRaises(ValueError):
            cp_proto.refuse_request('O1', '', ['原因A'], '', ['u1'], False, confirm=True)

    def test_reason_choices_accept_a_comma_string_without_duplicates(self):
        api, data = cp_proto.refuse_request('O1', 'S1', '原因A, 原因B,原因A', '', ['u1'],
                                            False, confirm=True)
        self.assertEqual(json.loads(data['sellerRefuseReason'])['reasonChoices'],
                         ['原因A', '原因B'])

    def test_proof_urls_are_deduplicated(self):
        _, data = cp_proto.refuse_request('O1', 'S1', ['原因A'], '', ['u1', 'u1', 'u2'],
                                          False, confirm=True)
        self.assertEqual(json.loads(data['refuseProofs']), ['u1', 'u2'])

    def test_request_of_dispatches_and_rejects_unknown_actions(self):
        self.assertEqual(cp_proto.request_of('compensate_refuse', order_id='O1',
                                             service_code='S1', reason_choices=['原因A'],
                                             proof_urls=['u1'], confirm=True)[0],
                         cp_proto.REFUSE_API)
        with self.assertRaises(cp_proto.UnknownApi):
            cp_proto.request_of('nope', confirm=True)

    def test_module_of_accepts_both_shapes(self):
        self.assertEqual(cp_proto._module_of(COMP_DETAIL)['orderId'], 'O1')
        self.assertEqual(cp_proto._module_of(COMP_DETAIL_STRING)['orderId'], 'O1')

    def test_module_of_returns_none_when_the_platform_gives_nothing(self):
        self.assertIsNone(cp_proto._module_of(COMP_DETAIL_EMPTY))
        self.assertIsNone(cp_proto._module_of({}))

    def test_parse_detail_converts_cents_to_yuan(self):
        parsed = cp_proto.parse_detail(COMP_DETAIL)
        self.assertTrue(parsed['found'])
        self.assertEqual(parsed['amount'], 1250)
        self.assertEqual(parsed['amount_yuan'], '12.50')
        self.assertEqual(parsed['title'], '平台判定需赔付 12.50 元')
        self.assertEqual(parsed['desc'], '买家举证成立')

    def test_parse_detail_never_invents_numbers(self):
        parsed = cp_proto.parse_detail(COMP_DETAIL_EMPTY)
        self.assertFalse(parsed['found'])
        for key in ('amount', 'amount_yuan', 'title', 'desc', 'order_id', 'service_code'):
            self.assertIsNone(parsed[key], key)

    def test_parse_refuse_form_keeps_none_and_empty_apart(self):
        self.assertEqual(cp_proto.parse_refuse_form(COMP_REASONS)['reason_choices'],
                         ['原因A', '原因B'])
        self.assertEqual(cp_proto.parse_refuse_form({'data': {'module': {'reasonChoices': []}}})
                         ['reason_choices'], [])
        missing = cp_proto.parse_refuse_form(COMP_REASONS_MISSING)
        self.assertIsNone(missing['reason_choices'])
        self.assertTrue(missing['found'])

    def test_parse_refuse_reads_the_business_flag(self):
        self.assertTrue(cp_proto.parse_refuse(COMP_REFUSE_OK)['saved'])
        self.assertIs(cp_proto.parse_refuse(COMP_REFUSE_OK)['platform_success'], True)
        failed = cp_proto.parse_refuse(COMP_REFUSE_FAIL)
        self.assertFalse(failed['saved'])
        self.assertEqual(failed['platform_message'], '凭证看不清')
        self.assertIsNone(cp_proto.parse_refuse(COMP_REFUSE_NOFLAG)['platform_success'])

    def test_refuse_api_is_registered_as_a_write(self):
        self.assertIn('compensate_refuse', cp_proto.ACTIONS)
        self.assertEqual(cp_proto.ACTIONS['compensate_refuse'], cp_proto.REFUSE_API)
        self.assertIn(cp_proto.REFUSE_API, cp_proto.ALL_APIS)


class CompensatePageTest(TradeWritePageTest):
    """赔付三条路由的 HTTP 面。"""

    def detail_path(self, order_id: str = 'O1', service_code: str = 'S1') -> str:
        return f'{BASE}/{ACC}/orders/{order_id}/compensations/{service_code}'

    def reasons_path(self, service_code: str = 'S1') -> str:
        return f'{BASE}/{ACC}/compensations/{service_code}/reasons'

    def refuse_path(self, order_id: str = 'O1', service_code: str = 'S1') -> str:
        return f'{BASE}/{ACC}/orders/{order_id}/compensations/{service_code}/refuse'

    def refuse_body(self, **overrides: Any) -> Dict[str, Any]:
        body = {'reason_choices': ['原因A'], 'proof_urls': ['u1'], 'confirm': True}
        body.update(overrides)
        return body


class CompensateReadTest(CompensatePageTest):
    """两条只读：详情 + 拒绝原因候选（都**没有** confirm 闸门）。"""

    def test_detail_shape_matches_the_bundle(self):
        data = self.get(self.detail_path()).json()['data']
        self.assertEqual(data['order_id'], 'O1')
        self.assertEqual(data['service_code'], 'S1')
        self.assertEqual(data['amount_yuan'], '12.50')
        self.assertEqual(data['api'], cp_proto.DETAIL_API)
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['warnings'], [])
        call = self.fake.call_of(cp_proto.DETAIL_API)
        self.assertEqual(call['data'], {'orderId': 'O1', 'serviceCode': 'S1'})
        self.assertEqual(call['v'], '1.0')

    def test_detail_passes_value_type_string(self):
        """官方这一条带 `valueType:'string'`，不发就可能是另一种响应形状。"""
        self.get(self.detail_path())
        self.assertEqual(self.fake.call_of(cp_proto.DETAIL_API)['kw'],
                         {'value_type': 'string'})

    def test_detail_accepts_a_json_string_body(self):
        self.fake.payloads[cp_proto.DETAIL_API] = COMP_DETAIL_STRING
        data = self.get(self.detail_path()).json()['data']
        self.assertEqual(data['amount_yuan'], '12.50')

    def test_detail_has_no_confirm_gate(self):
        resp = self.get(self.detail_path())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.fake.apis(), [cp_proto.DETAIL_API])

    def test_detail_missing_module_is_a_warning_not_invented_data(self):
        self.fake.payloads[cp_proto.DETAIL_API] = COMP_DETAIL_EMPTY
        data = self.get(self.detail_path()).json()['data']
        self.assertFalse(data['found'])
        self.assertIsNone(data['amount'])
        self.assertIsNone(data['amount_yuan'])
        self.assertEqual([w['code'] for w in data['warnings']], ['compensate_detail_missing'])

    def test_reasons_shape(self):
        data = self.get(self.reasons_path()).json()['data']
        self.assertEqual(data['reason_choices'], ['原因A', '原因B'])
        self.assertEqual(data['api'], cp_proto.REFUSE_FORM_API)
        self.assertEqual(data['warnings'], [])

    def test_reasons_missing_key_is_none_plus_a_warning(self):
        self.fake.payloads[cp_proto.REFUSE_FORM_API] = COMP_REASONS_MISSING
        data = self.get(self.reasons_path()).json()['data']
        self.assertIsNone(data['reason_choices'], '「平台没给」不许写成空数组')
        self.assertEqual([w['code'] for w in data['warnings']], ['compensate_reasons_missing'])

    def test_platform_refusal_is_502_and_not_a_logout(self):
        self.fake.payloads[cp_proto.DETAIL_API] = UNAUTHORIZED
        resp = self.get(self.detail_path())
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.json()['code'], 'protocol_error')
        self.assertEqual(self.login_state(), 'active')

    def test_missing_cookie_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        resp = self.get(self.detail_path())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404(self):
        resp = self.get(f'{BASE}/nope/orders/O1/compensations/S1')
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')

    def test_requires_login(self):
        self.assertEqual(self.get(self.detail_path(), headers={}).status_code, 401)


class CompensateRefuseTest(CompensatePageTest):
    """写：确认闸门 → 字段校验 → 建客户端，三步顺序一条都不许换。"""

    def test_confirm_gate_rejects_every_false_like_value_without_calling_platform(self):
        for value in FALSE_LIKE:
            payload = self.fail(self.refuse_path(), self.refuse_body(confirm=value))
            self.assertEqual(payload['code'], 'confirmation_required', repr(value))
            self.assertEqual(self.fake.calls, [], repr(value))

    def test_confirm_accepts_true_true_string_and_one(self):
        for value in (True, 'true', ' TRUE ', 1):
            self.fake.calls.clear()
            resp = self.post(self.refuse_path(), self.refuse_body(confirm=value))
            self.assertEqual(resp.status_code, 200, (value, resp.text))
            self.assertEqual(len(self.fake.calls), 1, repr(value))

    def test_field_validation_runs_before_the_credential_check(self):
        """凭证/账号检查在建客户端那一步；字段错的时候一个平台请求都不该发。"""
        db.execute('DELETE FROM account_credentials WHERE account_id=?', (ACC,))
        payload = self.fail(self.refuse_path(), self.refuse_body(proof_urls=[]))
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertIn('凭证', payload['message'])
        self.assertEqual(self.fake.calls, [], '字段校验必须排在凭据检查之前')

    def test_empty_reason_choices_is_rejected(self):
        payload = self.fail(self.refuse_path(), self.refuse_body(reason_choices=[]))
        self.assertEqual(payload['code'], 'invalid_fields')

    def test_overlong_reason_desc_is_rejected(self):
        payload = self.fail(self.refuse_path(), self.refuse_body(reason_desc='字' * 201))
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertIn('200', payload['message'])

    def test_happy_path_sends_the_verbatim_payload_and_audits_it(self):
        data = self.ok_data(self.refuse_path(), self.refuse_body(
            reason_choices=['原因A', '原因B'], reason_desc='不认可',
            proof_urls=['u1', 'u2'], is_manual=True))
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'trade_compensate_refuse')
        self.assertEqual(data['submitted']['proof_count'], 2)
        self.assertEqual(data['submitted']['is_manual'], True)
        call = self.fake.call_of(cp_proto.REFUSE_API)
        self.assertEqual(call['data'], {
            'orderId': 'O1', 'serviceCode': 'S1',
            'sellerRefuseReason': '{"reasonChoices":["原因A","原因B"],"reasonDesc":"不认可"}',
            'refuseProofs': '["u1","u2"]', 'isManual': True,
        })
        self.assertEqual([(row['action'], row['success']) for row in self.logs()],
                         [('trade_compensate_refuse', 1)])

    def test_business_rejection_is_502_with_the_platform_wording(self):
        self.fake.payloads[cp_proto.REFUSE_API] = COMP_REFUSE_FAIL
        payload = self.fail(self.refuse_path(), self.refuse_body(), status=502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertIn('凭证看不清', payload['message'])
        self.assertEqual(self.login_state(), 'active', '平台业务拒绝 ≠ 掉线')

    def test_platform_refusal_never_touches_the_login_state(self):
        self.fake.payloads[cp_proto.REFUSE_API] = COMP_REFUSE_UNAUTHORIZED
        payload = self.fail(self.refuse_path(), self.refuse_body(), status=502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(self.login_state(), 'active')

    def test_envelope_failure_is_502(self):
        self.fake.payloads[cp_proto.REFUSE_API] = UNAUTHORIZED
        self.fail(self.refuse_path(), self.refuse_body(), status=502)

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[cp_proto.REFUSE_API] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        self.fail(self.refuse_path(), self.refuse_body(), status=409)
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_unknown_account_is_404_and_sends_nothing(self):
        payload = self.fail(f'{BASE}/nope/orders/O1/compensations/S1/refuse',
                            self.refuse_body(), status=404)
        self.assertEqual(payload['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_requires_login(self):
        resp = self.post(self.refuse_path(), self.refuse_body(), headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


class CompensateRoutesTest(CompensatePageTest):
    """路由形状：两条只读 + 一条写；写路径不许被 GET 命中。"""

    def test_routes_are_registered_with_the_right_methods(self):
        paths = {(route.path, tuple(sorted(getattr(route, 'methods', ()))))
                 for route in trade_write_api.router.routes}
        self.assertIn((BASE + '/{account_id}/orders/{order_id}/compensations/{service_code}',
                       ('GET',)), paths)
        self.assertIn((BASE + '/{account_id}/compensations/{service_code}/reasons', ('GET',)),
                      paths)
        self.assertIn((BASE + '/{account_id}/orders/{order_id}/compensations/{service_code}/refuse',
                       ('POST',)), paths)

    def test_get_on_the_write_path_is_not_allowed(self):
        resp = self.get(self.refuse_path())
        self.assertIn(resp.status_code, (404, 405), resp.text)


class MemoWriteTest(TradeWritePageTest):
    """第四十九批：订单备注（`mtop.taobao.idle.merchant.add.memo`）。

    这条接口最容易犯的三个错：把「备注不对买家展示」写成对买家可见、把官方四条校验
    提示换成人话（用户核对时对不上）、平台没回执时谎报「已生效」。
    """

    PATH = BASE + '/%s/orders/1/memo' % ACC

    def setUp(self):
        super().setUp()
        self.fake.payloads[proto.ADD_MEMO_API] = MEMO_OK

    # -- 协议层 ----------------------------------------------------------

    def test_api_name_version_and_registration(self):
        self.assertEqual(proto.ADD_MEMO_API, 'mtop.taobao.idle.merchant.add.memo')
        self.assertEqual(proto.ADD_MEMO_VERSION, '1.0')
        self.assertIn(proto.ADD_MEMO_API, proto.WRITE_APIS)
        self.assertNotIn(proto.ADD_MEMO_API, proto.READ_APIS)
        # 只读层的写黑名单必须是写协议层的**超集**
        self.assertIn(proto.ADD_MEMO_API, trade_proto.WRITE_APIS)

    def test_request_is_the_official_shape(self):
        api, data = proto.add_memo_request('123', '已电话确认', 2, confirm=True)
        self.assertEqual(api, proto.ADD_MEMO_API)
        self.assertEqual(data, {'orderId': '123', 'memo': '已电话确认', 'memoGroup': 2})
        self.assertEqual(proto.add_memo_request('1', 'a', confirm=True)[1]['memoGroup'], 1)

    def test_group_must_be_one_to_five(self):
        for value in (0, 6, 'x', None, ''):
            with self.assertRaises(ValueError, msg=repr(value)):
                proto.add_memo_request('1', 'a', value, confirm=True)

    def test_protocol_gate_only_accepts_literal_true(self):
        for value in (False, None, 'false', 0, 'no', 1, 'true', ' TRUE '):
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(value)):
                proto.add_memo_request('1', 'a', 1, confirm=value)

    def test_official_validation_messages_are_reused_verbatim(self):
        cases = (('', '输入内容不能为空~'),
                 ('第一行\n第二行', '输入内容不支持换行~'),
                 ('带表情😀', '输入内容不支持emoji表情~'),
                 ('特殊￥符号', '输入内容包含不支持的特殊符号~'),
                 ('x' * 51, '备注最多 50 个字，现在有 51 个'))
        for memo, expected in cases:
            self.assertEqual(proto.memo_problem(memo), expected, memo)
        for good in ('普通备注', 'A1 空格 与标点，。！？', 'a-b/c(1)[2]<3>'):
            self.assertEqual(proto.memo_problem(good), '', good)

    def test_parse_memo_reports_missing_echo(self):
        parsed = proto.parse_memo(MEMO_OK)
        self.assertTrue(parsed['echoed'])
        self.assertEqual(parsed['memo'], '已电话确认')
        self.assertEqual(parsed['memo_group'], 2)
        parsed = proto.parse_memo(MEMO_NO_ECHO)
        self.assertFalse(parsed['echoed'])
        self.assertIsNone(parsed['memo'])
        self.assertIsNone(parsed['memo_group'])

    # -- 服务层 / 路由 ---------------------------------------------------

    def test_confirm_gate_blocks_every_false_like_value(self):
        for value in ('false', 'no', '0', 0, '', None, 2):
            payload = self.fail(self.PATH, {'memo': 'a', 'confirm': value})
            self.assertEqual(payload.get('code'), 'confirmation_required', repr(value))
        self.assertEqual(self.fake.calls, [], '没确认却发了平台请求')

    def test_bad_memo_is_rejected_before_the_client(self):
        payload = self.fail(self.PATH, {'memo': '带😀', 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('emoji', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [], '字段校验必须早于建客户端')

    def test_happy_path_sends_the_official_payload_and_audits(self):
        data = self.ok_data(self.PATH, {'memo': '已电话确认', 'memo_group': 2, 'confirm': True})
        self.assertEqual(self.fake.apis(), [proto.ADD_MEMO_API])
        self.assertEqual(self.fake.call_of(proto.ADD_MEMO_API)['data'],
                         {'orderId': '1', 'memo': '已电话确认', 'memoGroup': 2})
        self.assertEqual(data.get('action'), 'trade_add_memo')
        self.assertEqual(data.get('submitted'), {'memo_length': 5, 'memo_group': '2'})
        self.assertFalse(data.get('warnings'), data.get('warnings'))
        self.assertIn('trade_add_memo', [row['action'] for row in self.logs()])

    def test_missing_echo_becomes_a_warning_not_a_lie(self):
        self.fake.payloads[proto.ADD_MEMO_API] = MEMO_NO_ECHO
        data = self.ok_data(self.PATH, {'memo': 'a', 'confirm': True})
        codes = [item.get('code') for item in (data.get('warnings') or [])]
        self.assertEqual(codes, ['trade_memo_no_echo'])
        self.assertIn('核对', json.dumps(data, ensure_ascii=False))

    def test_platform_failure_is_passed_through_and_not_a_logout(self):
        self.fake.payloads[proto.ADD_MEMO_API] = NO_PERMISSION
        payload = self.fail(self.PATH, {'memo': 'a', 'confirm': True}, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('NO_PERMISSION', payload.get('message', ''))
        self.assertEqual(self.login_state(), 'active', '无权限 ≠ 掉线')
        rows = [dict(row) for row in self.logs()]
        self.assertTrue(any(row['action'] == 'trade_add_memo' and not row['success'] for row in rows),
                        rows)

    def test_route_shape_and_method(self):
        paths = {(route.path, tuple(sorted(getattr(route, 'methods', ()))))
                 for route in trade_write_api.router.routes}
        self.assertIn((BASE + '/{account_id}/orders/{order_id}/memo', ('POST',)), paths)

    def test_requires_login(self):
        resp = self.post(self.PATH, {'memo': 'a', 'confirm': True}, headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


class AdjustPriceRenderTest(TradeWritePageTest):
    """只读：改价渲染（GET …/orders/{order_id}/adjust-price/render）。"""

    def path(self, order_id: str = '42') -> str:
        return BASE + '/%s/orders/%s/adjust-price/render' % (ACC, order_id)

    # -- 协议层 ----------------------------------------------------------

    def test_api_name_version_and_registration(self):
        self.assertEqual(proto.ADJUST_PRICE_RENDER_API,
                         'mtop.taobao.idle.trade.merchant.adjust.price.render')
        self.assertEqual(proto.ADJUST_PRICE_RENDER_VERSION, '1.0')
        self.assertIn(proto.ADJUST_PRICE_RENDER_API, proto.READ_APIS)
        self.assertNotIn(proto.ADJUST_PRICE_RENDER_API, proto.WRITE_APIS)

    def test_request_is_the_official_shape(self):
        api, data = proto.adjust_price_render_request(42)
        self.assertEqual(api, proto.ADJUST_PRICE_RENDER_API)
        self.assertEqual(data, {'orderId': '42'})
        self.assertEqual(proto.ADJUST_PRICE_RENDER_VERSION, '1.0')
        with self.assertRaises(ValueError):
            proto.adjust_price_render_request('')

    def test_parse_keeps_every_money_field_as_string(self):
        parsed = proto.parse_adjust_price_render(ADJUST_RENDER_OK)
        self.assertTrue(parsed['found'])
        self.assertEqual(parsed['order_id'], '42')
        self.assertEqual(parsed['item']['item_id'], '7')
        self.assertEqual(parsed['price_and_num']['price'], '199.00')
        self.assertEqual(parsed['price_and_num']['num'], 1)
        self.assertEqual(parsed['total_discount'], '10.00')
        self.assertEqual(parsed['origin_total_price'], '199.00')
        self.assertEqual(parsed['origin_item_price'], '189.00')
        self.assertTrue(parsed['item_price_derived'], '官方口径是算出来的，必须如实标注')
        self.assertEqual(parsed['ship_fee_info']['ship_fee'], '10.00')

    def test_derived_item_price_is_a_fact_not_a_warning(self):
        # 「按官方口径算的」是常态：正常响应**不该**带任何 warning。
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['item_price_derived'])
        self.assertEqual(data['origin_item_price'], '189.00')
        self.assertEqual(data['warnings'], [])

    def test_missing_total_or_post_fee_gives_null_and_a_warning(self):
        self.fake.payloads[proto.ADJUST_PRICE_RENDER_API] = ADJUST_RENDER_NO_PRICES
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['found'])
        self.assertIsNone(data['origin_item_price'])
        self.assertFalse(data['item_price_derived'])
        self.assertEqual([w['code'] for w in data['warnings']],
                         ['adjust_price_render_item_price_unknown'])

    def test_parse_never_invents_numbers_when_module_is_missing(self):
        parsed = proto.parse_adjust_price_render(ADJUST_RENDER_MISSING)
        self.assertFalse(parsed['found'])
        self.assertIsNone(parsed['origin_item_price'])
        self.assertIsNone(parsed['ship_fee_info']['ship_fee'])
        self.assertIsNone(parsed['price_and_num']['price'])
        self.assertEqual(parsed['raw_keys'], [])

    # -- 服务 + 路由 ------------------------------------------------------

    def test_route_is_read_only_and_calls_the_platform_once(self):
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['found'])
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(self.fake.apis(), [proto.ADJUST_PRICE_RENDER_API])
        self.assertEqual(self.fake.call_of(proto.ADJUST_PRICE_RENDER_API)['data'],
                         {'orderId': '42'})
        self.assertEqual(self.fake.call_of(proto.ADJUST_PRICE_RENDER_API)['v'], '1.0')

    def test_read_path_writes_no_operation_log(self):
        self.get(self.path())
        self.assertEqual([row['action'] for row in self.logs()], [])

    def test_missing_module_is_a_warning_and_nulls_not_zeros(self):
        self.fake.payloads[proto.ADJUST_PRICE_RENDER_API] = ADJUST_RENDER_MISSING
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertFalse(data['found'])
        self.assertIsNone(data['origin_total_price'])
        self.assertEqual([w['code'] for w in data['warnings']],
                         ['adjust_price_render_missing'])

    def test_missing_prices_never_become_zero(self):
        self.fake.payloads[proto.ADJUST_PRICE_RENDER_API] = ADJUST_RENDER_NO_PRICES
        data = self.get(self.path()).json()['data']
        self.assertIsNone(data['origin_total_price'])
        self.assertIsNone(data['price_and_num']['price'])
        self.assertIsNone(data['ship_fee_info']['ship_fee'])

    def test_empty_order_id_is_rejected_before_the_client(self):
        from backend.app.services import trade_write as svc

        original = svc._client

        def boom(account_id: str):
            raise AssertionError('字段校验必须在建客户端之前')

        svc._client = boom  # type: ignore[assignment]
        try:
            result = svc.adjust_price_render(ACC, '')
        finally:
            svc._client = original  # type: ignore[assignment]
        self.assertEqual(result.get('code'), 'invalid_fields')
        self.assertEqual(result.get('message'), '订单号不能为空')

    def test_post_on_the_read_path_is_not_allowed(self):
        resp = self.post(self.path())
        self.assertEqual(resp.status_code, 405, resp.text)

    def test_platform_failure_is_a_protocol_error_and_not_a_logout(self):
        self.fake.payloads[proto.ADJUST_PRICE_RENDER_API] = UNAUTHORIZED
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED',
                      json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.login_state(), 'active', '无权限 ≠ 掉线，不许写成 need_relogin')

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='', login_state='active')
        resp = self.get(self.path())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.apis(), [])

    def test_unknown_account_is_404(self):
        resp = self.get('/api/trade/nope/orders/42/adjust-price/render')
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_requires_login(self):
        resp = self.get(self.path(), headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
