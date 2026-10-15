"""推广（投放）补齐接口的协议层请求形状测试：只打 httpx.MockTransport，不访问真实平台。

盯着四件事：

1. **api 名与 data 形状**照官方 bundle 的调用点（偏移写在 `backend/app/xianyu/ad.py` 每条 docstring 里）；
2. 官方调用点**恒定带**的键由协议层补默认值 / 拦住残包 —— 真机 2026-10-16 实测：
   少 `tab` 或 `entityType`/`entityList` 时平台回 `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`（502）；
3. `message.unread.info` 是**唯一**不套 `requestJson` 的一条（官方 data 为空）；
4. 写接口 `message.read` 没有 `confirm=True` 时**一个字节都不发**。
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
            await ad_proto.package_info(self.client, {
                'entityType': 1,
                'entityList': [{'entityId': '1', 'catId': 2, 'price': 300,
                                'areaTarget': False, 'startTime': '2026-01-01'}],
            })
            await ad_proto.item_search(self.client, {'itemId': '123'})
            await ad_proto.customize_bid_estimates(self.client, {'entityType': 1,
                                                                 'entityList': [{'entityId': '1'}]})
            await ad_proto.group_list(self.client, {'pageNum': 1, 'pageSize': 20, 'includeEndDate': True})
            await ad_proto.group_data(self.client, {'adGroupId': 9, 'allPeriod': False})
            await ad_proto.campaign_log(self.client, {'startDate': '20261009', 'endDate': '20261016',
                                                      'campaignId': 0, 'pageNum': 1})
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
        # 官方恒定带的默认值（bundle @3406850 KYe / @3396844）由协议层补齐：
        # 真机不发 tab 时平台回 E_AD_BP_COMMON_PARAM_NULL（2026-10-16）。
        self.assertEqual(json.loads(self.stub.calls[2][1]['requestJson']),
                         {'tab': 'platReco', 'needTabs': True, 'itemId': '123'})
        self.assertEqual(json.loads(self.stub.calls[1][1]['requestJson']),
                         {'entityType': 1,
                          'entityList': [{'entityId': '1', 'catId': 2, 'price': 300,
                                          'areaTarget': False, 'startTime': '2026-01-01'}],
                          'isGoodsSelectedTabChange': True})
        self.assertEqual(json.loads(self.stub.calls[3][1]['requestJson']),
                         {'entityType': 1, 'entityList': [{'entityId': '1'}],
                          'pcFirstRequest': True})

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
            await ad_proto.group_list(self.client, ['not', 'a', 'dict'])

        asyncio.run(run())
        self.assertEqual([json.loads(p['requestJson']) for _, p in self.stub.calls], [{}, {}])

    # -- 官方恒定带的键：默认值 + 残包拦截 ---------------------------------

    def test_official_defaults_are_filled(self):
        """官方调用点恒带的键缺了就补官方首次加载的默认值（不是编业务参数）。"""
        async def run():
            await ad_proto.item_search(self.client, {'searchKey': '猫粮'})
            await ad_proto.customize_bid_estimates(self.client, {'entityType': 2,
                                                                   'entityList': [{'entityId': '9'}]})
            await ad_proto.package_info(self.client, {'entityType': 1, 'entityList': [{'entityId': '1'}]})

        asyncio.run(run())
        self.assertEqual(json.loads(self.stub.calls[0][1]['requestJson']),
                         {'tab': 'platReco', 'needTabs': True, 'searchKey': '猫粮'})
        self.assertEqual(json.loads(self.stub.calls[1][1]['requestJson']),
                         {'entityType': 2, 'entityList': [{'entityId': '9'}], 'pcFirstRequest': True})
        self.assertEqual(json.loads(self.stub.calls[2][1]['requestJson']),
                         {'entityType': 1, 'entityList': [{'entityId': '1'}],
                          'isGoodsSelectedTabChange': True})

    def test_caller_values_win_over_defaults(self):
        async def run():
            await ad_proto.item_search(self.client, {'itemId': '1', 'tab': 'all', 'needTabs': False})

        asyncio.run(run())
        self.assertEqual(json.loads(self.stub.calls[0][1]['requestJson']),
                         {'tab': 'all', 'needTabs': False, 'itemId': '1'})

    def test_missing_required_params_never_hit_platform(self):
        """真机上残包只会换来 502 + E_AD_BP_COMMON_PARAM_NULL，本地先拦住。"""
        cases = [
            ('package_info', {}),
            ('package_info', {'entityType': 1}),          # 缺 entityList
            ('item_search', {'pageNum': 1}),               # 缺 itemId/searchKey
            ('customize_bid_estimates', {'entityList': []}),
            ('campaign_log', {'startDate': '', 'endDate': ''}),
        ]
        for name, body in cases:
            with self.subTest(name=name, body=body):
                with self.assertRaises(ad_proto.MissingRequiredParams):
                    asyncio.run(getattr(ad_proto, name)(self.client, body))
        self.assertEqual(self.stub.calls, [], '残包一个字节都不该发出去')

    def test_service_read_strips_convenience_keys(self):
        """`read()` 的便利键不许透到平台：真机复打时曾把 entity_type/entity_list 原样发出去。"""
        from backend.app.services.ad import _payload_only

        self.assertEqual(_payload_only({'entity_type': '1', 'entity_list': '[{}]',
                                        'is_goods_selected_tab_change': 'true',
                                        'entityList': [{'entityId': '1'}], 'tab': 'all'}),
                         {'entityList': [{'entityId': '1'}], 'tab': 'all'})

    def test_missing_required_is_not_raised_when_caller_supplies_it(self):
        async def run():
            await ad_proto.campaign_log(self.client, {'startDate': '20261009', 'endDate': '20261016'})

        asyncio.run(run())
        # 枚举由协议层补 0（官方 @1794033 初值；真机 2026-10-16 A/B：不带回
        # E_AD_BP_COMMON_SYSTEM_EXCEPTION，带 0 正常返回 data.result）。
        self.assertEqual(json.loads(self.stub.calls[0][1]['requestJson']),
                         {'startDate': '20261009', 'endDate': '20261016',
                          'operatorType': 0, 'operationType': 0})

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
        for kind in list(ad_proto.BODY_DEFAULTS) + list(ad_proto.BODY_REQUIRED):
            self.assertIn(kind, ad_proto.READ_APIS, kind)


if __name__ == '__main__':
    unittest.main()
