"""超级擦亮/安全中心只读协议请求形状测试，不访问真实平台。"""
from __future__ import annotations
import asyncio
import json
import unittest
from urllib.parse import parse_qs
import httpx
from backend.app.xianyu.client import (
    XianyuClient, AD_HOME_API, AD_ACCOUNT_CARD_API, AD_MEMBER_DATA_API,
    AD_CAMPAIGN_SEARCH_API, AD_CAMPAIGN_DATA_API, AD_ITEM_DATA_API, AD_SETTLE_API,
    SECURITY_API, SECURITY_VIOLATION_API, SECURITY_SITE_BIZ_CODE,
)

class Stub:
    def __init__(self): self.calls=[]; self.headers=[]
    def __call__(self, request):
        qs=parse_qs(request.content.decode())
        payload=json.loads(qs['data'][0])
        api=request.url.params.get('api')
        self.calls.append((api,payload))
        self.headers.append((api,dict(request.headers)))
        return httpx.Response(200,json={'ret':['SUCCESS::调用成功'],'data':{}})

class PromotionProtocolTest(unittest.TestCase):
    def setUp(self):
        self.client=XianyuClient('unb=1; _m_h5_tk=abc_123; cookie2=x')
        self.stub=Stub()
        self.client._http=lambda: None
        async def http(): return httpx.AsyncClient(transport=httpx.MockTransport(self.stub))
        self.client._http=http
    def test_read_only_request_shapes(self):
        async def run():
            await self.client.ad_home()
            await self.client.ad_account_card()
            await self.client.ad_member_data('20261009','20261009')
        asyncio.run(run())
        self.assertEqual(self.stub.calls[0][0], AD_HOME_API)
        self.assertEqual(self.stub.calls[0][1], {})
        self.assertEqual(json.loads(self.stub.calls[1][1]['requestJson']), {'scene':'PC_HOME'})
        self.assertEqual(json.loads(self.stub.calls[2][1]['requestJson']), {'startDate':'20261009','endDate':'20261009','includeEndDate':True})
    def test_no_write_api_is_declared(self):
        names={name for name,_ in self.stub.calls}
        self.assertFalse(any('operate' in name or 'submit' in name or 'withdraw' in name for name in names))

    def test_security_requests_carry_site_biz_code_header(self):
        """安全中心必须带站点 CODE —— 不带时平台回「站点CODE不能为空」。"""
        async def run():
            await self.client.security_score()
            await self.client.security_violations(page=2, size=5)
        asyncio.run(run())
        self.assertEqual([api for api,_ in self.stub.calls], [SECURITY_API, SECURITY_VIOLATION_API])
        self.assertEqual(self.stub.calls[0][1]['appName'], 'FM')
        self.assertEqual(self.stub.calls[1][1], {'biz':'FM','pageNum':2,'pageSize':5})
        for api, headers in self.stub.headers:
            self.assertEqual(headers.get('idle_site_biz_code'), SECURITY_SITE_BIZ_CODE, api)
            self.assertEqual(headers.get('idle_user_group_member_id'), '', api)

    def test_campaign_and_report_request_shapes(self):
        """计划 / 报表 / 结算：参数照官方 bundle 调用点，金额单位是分。"""
        async def run():
            await self.client.ad_campaigns('20261001','20261010',page=2,size=20,title_like=' 耳机 ')
            await self.client.ad_campaign_metrics('20261001','20261010',page=1,size=10)
            await self.client.ad_item_metrics('20261001','20261010',page=1,size=10)
            await self.client.ad_settle('20261001','20261010')
        asyncio.run(run())
        search=json.loads(self.stub.calls[0][1]['requestJson'])
        self.assertEqual(search, {'pageNum':2,'pageSize':20,'startDate':'20261001','endDate':'20261010','titleLike':'耳机'})
        for index in (1,2):
            body=json.loads(self.stub.calls[index][1]['requestJson'])
            self.assertEqual(body, {'startDate':'20261001','endDate':'20261010','pageNum':1,'pageSize':10,'includeEndDate':True,'allPeriod':False})
        self.assertEqual(json.loads(self.stub.calls[3][1]['requestJson']), {'startDate':'20261001','endDate':'20261010'})
        self.assertEqual([api for api,_ in self.stub.calls], [AD_CAMPAIGN_SEARCH_API, AD_CAMPAIGN_DATA_API, AD_ITEM_DATA_API, AD_SETTLE_API])

if __name__ == '__main__': unittest.main()
