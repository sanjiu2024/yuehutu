"""第 61 批读接口（物流凭证 / 发货地 / 小铺服务状态 / 小铺统计 / 商品状态统计）协议层测试。

只打 httpx.MockTransport，**不访问真实平台**。盯三件事：

1. api 名与 data 形状照官方 bundle 调用点（偏移写在每条 docstring / 常量注释里）；
2. 平台**没给**的字段是 None，不是空串 / 空数组（空数组表示平台明确说没有）；
3. 只读路径不碰写接口：小铺 pc.service.open 在 WRITE_APIS 黑名单里。

跑法（仓库根目录）：
    XIANYU_APP_DIR=$TEMP/xywb_read61 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_read61.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.xianyu import datacenter as dc_proto  # noqa: E402
from backend.app.xianyu import logistics as lg_proto  # noqa: E402
from backend.app.xianyu import shop as shop_proto  # noqa: E402
from backend.app.xianyu.client import XianyuClient  # noqa: E402


class Stub:
    """假的 mtop 端点：把 form body 里的 data 解出来，回一份平台形状的信封。"""

    def __init__(self, data=None):
        self.calls = []
        self.data = {} if data is None else data

    def __call__(self, request):
        qs = parse_qs(request.content.decode())
        payload = json.loads(qs['data'][0]) if 'data' in qs else None
        self.calls.append((request.url.params.get('api'), payload))
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': self.data})


class Read61ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.client = XianyuClient('unb=1; _m_h5_tk=abc_123; cookie2=x')
        self.stub = Stub()
        self.client._http = lambda: None

        async def http():
            return httpx.AsyncClient(transport=httpx.MockTransport(self.stub))

        self.client._http = http

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    # -- 小铺：服务开通状态（只读） -------------------------------------

    def test_shop_pc_service_status_shape(self):
        self._run(shop_proto.pc_service_status(self.client, 'FAST_REFUND'))
        api, payload = self.stub.calls[-1]
        self.assertEqual(api, 'mtop.alibaba.idle.pc.service.status.query')
        self.assertEqual(payload, {'serviceCode': 'FAST_REFUND'})

    def test_shop_service_open_is_registered_as_write(self):
        self.assertIn('mtop.alibaba.idle.pc.service.open', shop_proto.WRITE_APIS)
        self.assertIn('mtop.alibaba.idle.pc.service.status.query', shop_proto.READ_APIS)
        self.assertNotIn('mtop.alibaba.idle.pc.service.open', shop_proto.READ_APIS)

    # -- 数据组：两个新读接口 -------------------------------------------

    def test_datacenter_shop_stats_shape(self):
        self._run(dc_proto.shop_stats(self.client, 'sess-9'))
        api, payload = self.stub.calls[-1]
        self.assertEqual(api, 'mtop.alibaba.idle.seller.pc.shop.stats.query')
        self.assertEqual(payload, {'sessionId': 'sess-9'})

    def test_datacenter_item_status_statistics_has_no_data(self):
        self._run(dc_proto.item_status_statistics(self.client))
        api, payload = self.stub.calls[-1]
        self.assertEqual(api, 'mtop.taobao.idle.seller.platform.pc.item.status.statistics')
        self.assertIn(payload, ({}, None))

    # -- 物流：凭证详情 --------------------------------------------------

    def test_unconsign_detail_request_and_parse(self):
        api, data = lg_proto.unconsign_detail_request(' 12345 ')
        self.assertEqual(api, 'mtop.taobao.idle.merchant.unconsign.detail')
        self.assertEqual(data, {'orderId': '12345'})

        # 解析口吃的是**平台信封**（与 parse_companies / parse_resend 同一口径）：
        # 业务体在 data 里，body_of 认得 data / data.data / data.module 三种容器。
        parsed = lg_proto.parse_unconsign_detail(
            {'ret': ['SUCCESS::调用成功'],
             'data': {'content': '已发货', 'imageList': ['a.jpg', 'b.jpg'], 'extra': 1}})
        self.assertEqual(parsed['content'], '已发货')
        self.assertEqual(parsed['image_list'], ['a.jpg', 'b.jpg'])
        self.assertEqual(parsed['raw'], {'content': '已发货', 'imageList': ['a.jpg', 'b.jpg'],
                                         'extra': 1})

    def test_unconsign_detail_empty_is_none_not_blank(self):
        """平台明确给空数组 -> 保留 []；平台没给键 -> None（两者不是一回事）。"""
        parsed = lg_proto.parse_unconsign_detail({'data': {'content': 'x', 'imageList': []}})
        self.assertEqual(parsed['image_list'], [])
        parsed = lg_proto.parse_unconsign_detail({})
        self.assertIsNone(parsed['content'])
        self.assertIsNone(parsed['image_list'])
        self.assertIsNone(parsed['raw'])

    def test_unconsign_detail_rejects_blank_order_id(self):
        for blank in ('', '   ', None):
            with self.assertRaises(ValueError):
                lg_proto.unconsign_detail_request(blank)

    def test_delivery_place_detail_request_and_parse(self):
        api, data = lg_proto.delivery_place_detail_request()
        self.assertEqual(
            api, 'mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query')
        self.assertIsNone(data)
        self.assertEqual(lg_proto.parse_delivery_place_detail({}), {'detail': None})
        self.assertEqual(lg_proto.parse_delivery_place_detail({'data': {'province': '浙江'}}),
                         {'detail': {'province': '浙江'}})


if __name__ == '__main__':
    unittest.main(verbosity=2)
