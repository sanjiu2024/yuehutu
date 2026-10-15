"""推广（投放）补齐接口的协议层请求形状测试：只打 httpx.MockTransport，不访问真实平台。

盯着三件事：

1. **api 名与 data 形状**照官方 bundle 的调用点（偏移写在 `backend/app/xianyu/ad.py` 每条 docstring 里）；
2. `message.unread.info` 是**唯一**不套 `requestJson` 的一条（官方 data 为空）；
3. 写接口 `message.read` 没有 `confirm=True` 时**一个字节都不发**。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from urllib.parse import parse_qs

import httpx

from backend.app.xianyu import ad as ad_proto
from backend.app.xianyu.client import AD_SPM, XianyuClient


class Stub:
    """假的 mtop 端点：把 form body 里的 data 解出来，回一份真机形状的信封。"""

    def __init__(self, data=None):
        self.calls = []
        self.params = []
        self.data = data if data is not None else {}

    def __call__(self, request):
        qs = parse_qs(request.content.decode())
        payload = json.loads(qs['data'][0])
        api = request.url.params.get('api')
        self.calls.append((api, payload))
        self.params.append(dict(request.url.params))
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': self.data})


class AdProtocolTest(unittest.TestCase):
    def setUp(self):
        self.client = XianyuClient('unb=1; _m_h5_tk=abc_123; cookie2=x')
        self.stub = Stub()
        self.client._http = lambda: None

        async def http():
            return httpx.AsyncClient(transport=httpx.MockTransport(self.stub))

        self.client._http = http

    # -- 只读：形状 ------------------------------------------------------

    def test_every_read_api_sends_request_json(self):
        async def run():
            await ad_proto.account_info(self.client, {'orderPrice': '100', 'scene': 'CREATION_REAL_TIME'})
            await ad_proto.package_info(self.client, {'promotionMode': 'CUSTOMIZE'})
            await ad_proto.item_search(self.client, {'itemId': '123', 'needTabs': True})
            await ad_proto.customize_bid_estimates(self.client, {'entityType': 'ITEM', 'entityList': ['1']})
            await ad_proto.group_list(self.client, {'pageNum': 1, 'pageSize': 20, 'includeEndDate': True})
            await ad_proto.group_data(self.client, {'adGroupId': 9, 'allPeriod': False})
            await ad_proto.campaign_log(self.client, {'campaignId': 0, 'pageNum': 1})
            await ad_proto.message_list(self.client, {'pageNum': 1, 'pageSize': 10})
            await ad_proto.message_detail(self.client, {'messageId': 5})

        asyncio.run(run())
        self.assertEqual([api for api, _ in self.stub.calls], [
            ad_proto.ACCOUNT_INFO_API, ad_proto.PACKAGE_INFO_API, ad_proto.ITEM_SEARCH_API,
            ad_proto.CUSTOMIZE_BID_ESTIMATES_API, ad_proto.GROUP_LIST_API, ad_proto.GROUP_DATA_API,
            ad_proto.CAMPAIGN_LOG_API, ad_proto.MESSAGE_LIST_API, ad_proto.MESSAGE_DETAIL_API,
        ])
        for api, payload in self.stub.calls:
            self.assertEqual(set(payload), {'requestJson'}, api)
            json.loads(payload['requestJson'])  # 必须是合法 JSON（官方就是 JSON.stringify）
        self.assertEqual(json.loads(self.stub.calls[0][1]['requestJson']),
                         {'orderPrice': '100', 'scene': 'CREATION_REAL_TIME'})
        self.assertEqual(json.loads(self.stub.calls[4][1]['requestJson']),
                         {'pageNum': 1, 'pageSize': 20, 'includeEndDate': True})

    def test_unread_info_sends_empty_data_without_request_json(self):
        """官方 @703173 的 data 是空对象、不套 requestJson；套了平台会当成另一个请求。"""
        self.stub.data = {'unreadCount': 3}

        async def run():
            return await ad_proto.message_unread(self.client)

        payload = asyncio.run(run())
        self.assertEqual(payload, {'unreadCount': 3})
        self.assertEqual(self.stub.calls, [(ad_proto.MESSAGE_UNREAD_API, {})])

    def test_reads_return_platform_data_dict(self):
        self.stub.data = {'total': 7, 'result': {'itemList': []}}

        async def run():
            return await ad_proto.item_search(self.client, {'itemId': '1'})

        self.assertEqual(asyncio.run(run()), {'total': 7, 'result': {'itemList': []}})

    def test_non_dict_body_is_sent_as_empty_object(self):
        async def run():
            await ad_proto.account_info(self.client, None)
            await ad_proto.package_info(self.client, ['not', 'a', 'dict'])

        asyncio.run(run())
        self.assertEqual([json.loads(p['requestJson']) for _, p in self.stub.calls], [{}, {}])

    # -- mtop 参数口径 ---------------------------------------------------

    def test_requests_use_ad_spm_and_v1(self):
        """推广域统一带 `spm_cnt=a21ybx.im.0.0` 与 v=1.0（bundle 的 yp 包装同口径）。"""
        async def run():
            await ad_proto.group_list(self.client, {'pageNum': 1})
            await ad_proto.message_unread(self.client)

        asyncio.run(run())
        self.assertEqual(AD_SPM, 'a21ybx.im.0.0')
        for params in self.stub.params:
            self.assertEqual(params['v'], '1.0')
            self.assertEqual(params['spm_cnt'], AD_SPM)
            self.assertEqual(params['api'], self.stub.calls[self.stub.params.index(params)][0])

    # -- 写：闸门 --------------------------------------------------------

    def test_message_read_refuses_without_confirm(self):
        async def run(confirm):
            return await ad_proto.message_read(self.client, {'messageIds': [1, 2]}, confirm)

        for confirm in (False, None, 'true', 1, ''):
            with self.assertRaises(ad_proto.WriteNotConfirmed):
                asyncio.run(run(confirm))
        self.assertEqual(self.stub.calls, [], '没确认就不该发出任何请求')

    def test_message_read_sends_ids_after_confirm(self):
        async def run():
            return await ad_proto.message_read(self.client, {'messageIds': [1, 2]}, True)

        asyncio.run(run())
        self.assertEqual([api for api, _ in self.stub.calls], [ad_proto.MESSAGE_READ_API])
        self.assertEqual(json.loads(self.stub.calls[0][1]['requestJson']), {'messageIds': [1, 2]})

    # -- 目录自洽 --------------------------------------------------------

    def test_read_apis_table_covers_every_constant(self):
        """`READ_APIS` 是服务层白名单的来源，漏一条就等于该接口在服务层 404。"""
        self.assertEqual(sorted(ad_proto.READ_APIS), sorted([
            'account_info', 'package_info', 'item_search', 'customize_bid_estimates',
            'group_list', 'group_data', 'campaign_log',
            'message_list', 'message_detail', 'message_unread',
        ]))
        constants = {name: value for name, value in vars(ad_proto).items()
                     if name.endswith('_API') and isinstance(value, str)}
        for kind, spec in ad_proto.READ_APIS.items():
            self.assertIn(spec['api'], constants.values(), kind)
        self.assertNotIn(ad_proto.MESSAGE_READ_API,
                         [spec['api'] for spec in ad_proto.READ_APIS.values()],
                         '写接口不许出现在只读目录里')
        self.assertIs(ad_proto.READ_APIS['message_unread']['request_json'], False)
        for kind, spec in ad_proto.READ_APIS.items():
            if kind != 'message_unread':
                self.assertIs(spec['request_json'], True, kind)


if __name__ == '__main__':
    unittest.main()
