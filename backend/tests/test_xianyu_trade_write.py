"""协议层交易写接口契约测试（第 60 批：IM 侧订单处理 + SKU 选择器 / 验货报告 / 智能表单）。

**不打真实闲鱼平台**：`httpx.MockTransport` 造 mtop 响应，逐条锁死「用哪个接口名、哪个版本、
payload 顶层带什么、解析器怎么读平台业务体」。接口名与形状来自官方 bundle 静态反解
（字符偏移见 `backend/app/xianyu/trade_write.py` 每个常量上方的注释），**全部未经真机验证** ——
写接口禁止对真实平台试打。

跑法（仓库根目录）：
    XIANYU_APP_DIR=$TEMP/xywb_trade_write PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_trade_write.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from backend.app.xianyu import trade  # noqa: E402
from backend.app.xianyu import trade_write as proto  # noqa: E402
from backend.app.xianyu.client import XianyuClient, XianyuError  # noqa: E402

#: 带 unb 与 _m_h5_tk 的假 Cookie（缺 unb 时 XianyuClient 直接 ValueError）
COOKIE = 'unb=2200123456; _m_h5_tk=abcdef123456_1712345678901; cookie2=xyz'


class MtopStub:
    """按 api 名回话的假 mtop 服务端，记录每次请求的 (api, version, payload)。"""

    def __init__(self, responses: Dict[str, Any],
                 fail_ret: Optional[Dict[str, str]] = None):
        self.responses = responses
        self.fail_ret = fail_ret or {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = parse_qs(request.content.decode('utf-8'))
        payload = json.loads(query['data'][0]) if query.get('data') else {}
        api = params.get('api', '')
        self.calls.append({'api': api, 'version': params.get('v', ''), 'payload': payload})
        if api in self.fail_ret:
            return httpx.Response(200, json={'ret': [self.fail_ret[api]], 'data': None})
        responder = self.responses.get(api)
        if responder is None:
            return httpx.Response(200, json={'ret': ['FAIL_SYS_API_NOT_FOUND::接口不存在'],
                                             'data': {}})
        data = responder(payload) if callable(responder) else responder
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': data})


def make_client(responses: Dict[str, Any], fail_ret: Optional[Dict[str, str]] = None):
    stub = MtopStub(responses, fail_ret)
    return XianyuClient(COOKIE, transport=httpx.MockTransport(stub)), stub


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


#: 假业务体：SKU 选择器（官方读 data.{components,skuInfo}）
SKU_BODY = {'skuInfo': [{'skuId': '1'}],
            'components': [{'render': 'skuItemInfo', 'data': {'itemId': 7}}]}
#: 假业务体：验货报告（官方只在 data.code == 'success' 时取 data.data）
BACKFILL_BODY = {'code': 'success', 'data': {'reportId': 'R1'}}
#: 假业务体：智能表单（schemaData 是 JSON 字符串）
SMART_FORM_BODY = {'result': {'formSchema': {'schemaData': '{"type": "object"}'},
                              'formData': {'field1': 'v1'}}}


class WriteRoundTripTest(unittest.TestCase):
    """四条写接口：真协议层把请求原样发出去（版本 + 载荷）。"""

    def call(self, api: str, data: Dict[str, Any], version: str):
        client, stub = make_client({api: {}})
        raw = run(client.mtop(api, data, v=version))
        self.assertEqual(len(stub.calls), 1, stub.calls)
        return raw, stub.calls[0]

    def test_order_cancel_goes_out_with_the_official_payload(self):
        api, data = proto.order_cancel_request(9, '买家不想要了', confirm=True)
        raw, call = self.call(api, data, proto.ORDER_CANCEL_VERSION)
        self.assertEqual(call['api'], 'mtop.taobao.idle.trade.order.cancel')
        self.assertEqual(call['version'], '2.0')
        self.assertEqual(call['payload'],
                         {'tid': '9', 'bizOrderId': '9', 'closeReason': '买家不想要了'})
        self.assertTrue(raw.get('success'), raw)

    def test_close_by_seller_im_is_a_different_api_name_same_payload(self):
        api, data = proto.close_by_seller_im_request(9, '买家不想要了', confirm=True)
        _, call = self.call(api, data, proto.CLOSE_BY_SELLER_IM_VERSION)
        self.assertEqual(call['api'], 'mtop.taobao.idle.trade.close.by.seller')
        self.assertNotEqual(call['api'], proto.CLOSE_BY_SELLER_API)
        self.assertEqual(call['payload']['bizOrderId'], '9')

    def test_seller_delay_confirm_sends_seconds_and_one_order(self):
        api, data = proto.seller_delay_confirm_request('9', 7, confirm=True)
        _, call = self.call(api, data, proto.SELLER_DELAY_CONFIRM_VERSION)
        self.assertEqual(call['api'], 'mtop.taobao.idle.trade.seller.delay.confirm')
        self.assertEqual(call['version'], '1.0')
        self.assertEqual(call['payload'], {'orderId': '9', 'delayTime': '604800'})

    def test_user_adjust_price_sends_one_key_per_sku(self):
        api, data = proto.user_adjust_price_request('9', {'sku-1': '12.50'}, confirm=True)
        _, call = self.call(api, data, proto.USER_ADJUST_PRICE_VERSION)
        self.assertEqual(call['api'], 'mtop.taobao.idle.trade.user.adjust.price')
        self.assertEqual(call['payload'], {'orderId': '9', 'sku-1': '1250'})

    def test_smart_form_callback_sends_a_double_stringified_ext(self):
        ext = {'event': 'submit', 'formData': '{"a": 1}'}
        api, data = proto.smart_form_callback_request('CB1', ext, confirm=True)
        _, call = self.call(api, data, proto.SMART_FORM_CALLBACK_VERSION)
        self.assertEqual(call['api'],
                         'mtop.taobao.merchant.heracles.inno.smart.form.callback')
        self.assertEqual(call['payload']['callbackCode'], 'CB1')
        inner = json.loads(call['payload']['extJson'])
        self.assertEqual(inner, ext)
        self.assertIsInstance(inner['formData'], str, 'formData 是字符串 ⇒ 双层字符串')

    def test_write_constructors_never_reach_the_client_without_confirm(self):
        for builder in (lambda: proto.order_cancel_request(9, '原因'),
                        lambda: proto.close_by_seller_im_request(9, '原因'),
                        lambda: proto.seller_delay_confirm_request('9', 3),
                        lambda: proto.user_adjust_price_request('9', {'k': '1'}),
                        lambda: proto.smart_form_callback_request('CB1', {'event': 'x'})):
            with self.assertRaises(proto.WriteNotConfirmed):
                builder()


class ReadRoundTripTest(unittest.TestCase):
    """三条只读接口：请求形状 + 用真实信封喂解析器。"""

    def call(self, api: str, data: Dict[str, Any], version: str, body: Any):
        client, stub = make_client({api: body})
        raw = run(client.mtop(api, data, v=version))
        self.assertEqual([c['api'] for c in stub.calls], [api])
        return raw, stub.calls[0]

    def test_sku_selector_request_and_parse(self):
        api, data = proto.sku_selector_request(7)
        raw, call = self.call(api, data, proto.SKU_SELECTOR_VERSION, SKU_BODY)
        self.assertEqual(call['payload'], {'itemId': '7'})
        parsed = proto.parse_sku_selector(raw)
        self.assertEqual(parsed['component_data']['skuItemInfo'], {'itemId': 7})
        self.assertEqual(parsed['sku_info'], [{'skuId': '1'}])
        self.assertTrue(parsed['found'])

    def test_backfill_report_request_and_parse(self):
        api, data = proto.backfill_report_request('9')
        raw, call = self.call(api, data, proto.BACKFILL_REPORT_VERSION, BACKFILL_BODY)
        self.assertEqual(call['api'],
                         'mtop.alibaba.idle.seller.platform.order.backfill.report')
        self.assertEqual(call['payload'], {'orderId': '9'})
        parsed = proto.parse_backfill_report(raw)
        self.assertTrue(parsed['report_ok'], parsed)
        self.assertEqual(parsed['code'], 'success')
        self.assertEqual(parsed['report'], {'reportId': 'R1'})

    def test_smart_form_query_request_and_parse(self):
        api, data = proto.smart_form_query_request('formId=9&bizType=order')
        raw, call = self.call(api, data, proto.SMART_FORM_QUERY_VERSION, SMART_FORM_BODY)
        self.assertEqual(call['payload'], {'formId': '9', 'bizType': 'order'})
        # 官方形状：平铺、值字符串（URLSearchParams），没有信封键
        _, from_url = proto.smart_form_query_request(
            'https://h5.m.goofish.com/form?instanceId=I1&sceneCode=S1')
        self.assertEqual(from_url, {'instanceId': 'I1', 'sceneCode': 'S1'})
        with self.assertRaises(ValueError):
            proto.smart_form_query_request({'params': {'instanceId': 'I1'}})
        parsed = proto.parse_smart_form_query(raw)
        self.assertEqual(parsed['schema'], {'type': 'object'})
        self.assertEqual(parsed['schema_raw'], '{"type": "object"}')
        self.assertEqual(parsed['form_data'], {'field1': 'v1'})


class PlatformFailureTest(unittest.TestCase):
    """平台拒绝要能被公共判定认出来（无权限 ≠ 掉线）。"""

    FAIL = 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'

    def test_no_permission_raises_out_of_mtop_with_the_ret_原文(self):
        client, stub = make_client(
            {}, fail_ret={proto.ORDER_CANCEL_API: self.FAIL})
        with self.assertRaises(XianyuError) as ctx:
            run(client.mtop(proto.ORDER_CANCEL_API,
                            {'tid': '9', 'bizOrderId': '9', 'closeReason': '原因'}, v='2.0'))
        self.assertEqual(ctx.exception.ret, [self.FAIL])
        self.assertEqual([c['api'] for c in stub.calls], [proto.ORDER_CANCEL_API])

    def test_the_same_ret_is_a_platform_failure_and_not_a_logout(self):
        # 失败信封形状（client.mtop 软失败时给的就是这个；硬失败则抛 XianyuError，
        # 由 common.call_method 收成 CALL_ERROR 后走 _call_api 的平台失败分支）
        raw = {'success': False, 'message': self.FAIL, 'ret': [self.FAIL], 'data': None}
        self.assertEqual(trade.envelope_error(raw), self.FAIL)
        self.assertFalse(proto.parse_write_result(raw)['platform_success'])
        self.assertIn('UNAUTHORIZED', trade.envelope_message(raw))


if __name__ == '__main__':
    unittest.main(verbosity=2)
