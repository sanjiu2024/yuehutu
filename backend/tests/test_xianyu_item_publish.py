"""商品发布数据源（类目 / 品牌 / 型号 / 属性 / 类目搜索）协议层请求形状 + 解析测试。

**为什么需要**：这 5 个接口的**接口名与入参形状是从官方压缩 JS 的字符偏移里读出来的**
（见 `backend/app/xianyu/item_publish.py` 文件头），没有任何真机证据。压缩 JS 里读错一个
键名，用户看到的就是「选了类目却没有品牌可选」，而且**不会报错** —— 静默空下拉框。
所以这里把「请求 data 形状」「响应取值路径」「认不出字段时不造假候选值」三条都钉死。

**不打真实闲鱼平台**：`httpx.MockTransport` 造响应（与 `test_xianyu_promotion.py` 同形态）。

跑法（仓库根目录）：
    XIANYU_APP_DIR=$TEMP/xywb_item_pub_cov PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_item_publish.py
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]

from backend.app.xianyu import item_publish as proto  # noqa: E402
from backend.app.xianyu.client import XianyuClient  # noqa: E402

COOKIE = 'unb=1; _m_h5_tk=abc_123; cookie2=x'


class Stub:
    """记录每次调用的 (api, data, version, valueType, headers)。"""

    def __init__(self, responses: Dict[str, Any] = None):
        self.responses: Dict[str, Any] = dict(responses or {})
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        body = parse_qs(request.content.decode())
        raw = body.get('data', ['{}'])[0]
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        api = params.get('api') or ''
        self.calls.append({
            'api': api,
            'data': payload,
            'v': params.get('v'),
            'value_type': params.get('valueType'),
            'headers': dict(request.headers),
        })
        body_out = self.responses.get(api, {})
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': body_out})

    def last(self) -> Dict[str, Any]:
        return self.calls[-1]


def make_client(responses: Dict[str, Any] = None):
    client = XianyuClient(COOKIE)
    stub = Stub(responses)
    client._http = lambda: None  # type: ignore[assignment]

    async def http():
        return httpx.AsyncClient(transport=httpx.MockTransport(stub))

    client._http = http  # type: ignore[assignment]
    return client, stub


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- 入参构造（纯函数）


class RequestShapeTest(unittest.TestCase):
    """入参构造必须逐字照官方 bundle（@偏移见协议层文件头）。"""

    def test_categories_has_no_data(self):
        """bundle @3832720：只 `{api, v:'1.0', valueType:'string'}`，**没有 data 字段**。"""
        self.assertEqual(proto.categories_data(), {})
        self.assertEqual(proto.search_categories_data(), {})

    def test_brands_uses_channel_category_id(self):
        self.assertEqual(proto.brands_data('12345'), {'channelCategoryId': 12345})

    def test_models_needs_both_ids(self):
        self.assertEqual(proto.models_data('123', '9'),
                         {'brandValueId': 9, 'channelCategoryId': 123})

    def test_property_uses_category_id(self):
        self.assertEqual(proto.property_data('777'), {'categoryId': 777})

    def test_non_numeric_ids_become_none_not_zero(self):
        """官方是 `Number(...)`：转不动就是 NaN，**绝不能变成 0**（0 是合法的类目 id 语义）。"""
        self.assertIsNone(proto.brands_data('abc')['channelCategoryId'])
        self.assertIsNone(proto.models_data('', 'x')['channelCategoryId'])
        self.assertIsNone(proto.models_data('1', None)['brandValueId'])
        # 数字字符串与带小数的字符串都能认（bundle 里 id 来自 URL/state，可能是字符串）
        self.assertEqual(proto.brands_data('12.0')['channelCategoryId'], 12)


# --------------------------------------------------------------------------- 真实请求形状


class ProtocolRequestTest(unittest.TestCase):
    """协议层真的发出去了什么（api / data / v / valueType）。"""

    def test_categories_request_shape(self):
        client, stub = make_client({proto.CATEGORIES_API: {
            'code': 'success',
            'data': {'selectIdleCategories': [
                {'categoryId': 1, 'categoryName': '手机', 'categoryLevel': 1, 'leaf': False,
                 'childrenCategoryDOs': [
                     {'categoryId': 2, 'categoryName': '智能手机', 'categoryLevel': 2, 'leaf': True},
                 ]},
            ]},
        }})
        out = run(proto.fetch_categories(client))
        self.assertEqual(len(stub.calls), 1)
        call = stub.last()
        self.assertEqual(call['api'], proto.CATEGORIES_API)
        self.assertEqual(call['data'], {})
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['value_type'], 'string')
        self.assertEqual(out['code'], 'success')
        self.assertEqual(out['options'][0]['category_id'], '1')
        self.assertEqual(out['options'][0]['category_name'], '手机')
        self.assertEqual(out['options'][0]['children'][0]['category_name'], '智能手机')
        self.assertTrue(out['options'][0]['children'][0]['leaf'])

    def test_brands_request_shape(self):
        client, stub = make_client({proto.BRANDS_API: {
            'code': 'success',
            'data': {'selectIdleBrandValues': [
                {'valueId': 11, 'valueName': 'Apple'},
                {'valueId': 12, 'valueName': '小米'},
            ]},
        }})
        out = run(proto.fetch_brands(client, '12345'))
        self.assertEqual(stub.last()['data'], {'channelCategoryId': 12345})
        self.assertEqual(stub.last()['api'], proto.BRANDS_API)
        self.assertEqual(out['options'], [{'value': '11', 'label': 'Apple'},
                                          {'value': '12', 'label': '小米'}])

    def test_models_request_shape(self):
        client, stub = make_client({proto.MODELS_API: {
            'code': 'success',
            'data': {'selectIdleModelValues': [{'valueId': 7, 'valueName': 'iPhone 15'}]},
        }})
        out = run(proto.fetch_models(client, '12345', '11'))
        self.assertEqual(stub.last()['data'],
                         {'brandValueId': 11, 'channelCategoryId': 12345})
        self.assertEqual(out['options'], [{'value': '7', 'label': 'iPhone 15'}])

    def test_property_request_shape(self):
        client, stub = make_client({proto.CATEGORY_PROPERTY_API: {
            'code': 'success',
            'data': {'fineness': [
                {'propertyId': 20000, 'propertyName': '成色',
                 'valueList': [{'valueId': '95', 'valueName': '95新'}]},
            ]},
        }})
        out = run(proto.fetch_category_properties(client, '999'))
        self.assertEqual(stub.last()['data'], {'categoryId': 999})
        self.assertEqual(out['options'], [{
            'property_id': '20000', 'property_name': '成色',
            'values': [{'value': '95', 'label': '95新'}],
        }])

    def test_search_categories_request_shape(self):
        client, stub = make_client({proto.SEARCH_CATEGORIES_API: {
            'code': 'success',
            'data': {'selectIdleCategories': [{'categoryId': 5, 'categoryName': '耳机'}]},
        }})
        out = run(proto.fetch_search_categories(client))
        self.assertEqual(stub.last()['api'], proto.SEARCH_CATEGORIES_API)
        self.assertEqual(stub.last()['data'], {})
        self.assertEqual(out['options'], [{'category_id': '5', 'category_name': '耳机',
                                           'level': None, 'leaf': None, 'children': []}])


# --------------------------------------------------------------------------- 认不出字段时不许造假


class UnknownShapeTest(unittest.TestCase):
    """平台换了字段名 / 没给数据时，**宁可空列表也不编候选值**（task-15 硬约束 6）。"""

    def test_unknown_keys_yield_empty_options_not_fake_ones(self):
        client, _ = make_client({proto.CATEGORIES_API: {'code': 'success', 'data': {'whatever': [1, 2]}}})
        out = run(proto.fetch_categories(client))
        self.assertEqual(out['options'], [])
        self.assertEqual(out['code'], 'success')

    def test_platform_failure_body_keeps_code_and_message(self):
        """平台回 `code != success` 时协议层照样把 code/msg 带出来（服务层据此判失败）。"""
        client, _ = make_client({proto.BRANDS_API: {
            'code': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED', 'msg': '无权限访问',
            'data': None,
        }})
        out = run(proto.fetch_brands(client, '1'))
        self.assertEqual(out['options'], [])
        self.assertEqual(out['code'], 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(out['message'], '无权限访问')

    def test_brand_rows_without_value_or_label_are_dropped(self):
        """只有半个字段的行丢掉：一个点不动的空选项比少一行更坏。"""
        client, _ = make_client({proto.BRANDS_API: {
            'code': 'success',
            'data': {'selectIdleBrandValues': [
                {'valueId': 1},                      # 缺 label
                {'valueName': '只有名字'},            # 缺 value
                {'valueId': 2, 'valueName': '正常'},
            ]},
        }})
        out = run(proto.fetch_brands(client, '1'))
        self.assertEqual(out['options'], [{'value': '2', 'label': '正常'}])

    def test_flat_list_business_body_is_tolerated(self):
        """真机若把业务体直接给成数组（`res.data.data` 是 list），也要能认。"""
        client, _ = make_client({proto.CATEGORIES_API: {
            'code': 'success', 'data': [{'categoryId': 3, 'categoryName': '球鞋'}],
        }})
        out = run(proto.fetch_categories(client))
        self.assertEqual([o['category_name'] for o in out['options']], ['球鞋'])

    def test_children_alias_is_tolerated(self):
        client, _ = make_client({proto.CATEGORIES_API: {
            'code': 'success',
            'data': {'selectIdleCategories': [
                {'categoryId': 1, 'categoryName': '父', 'children': [
                    {'categoryId': 2, 'categoryName': '子'}]},
            ]},
        }})
        out = run(proto.fetch_categories(client))
        self.assertEqual(out['options'][0]['children'][0]['category_id'], '2')


if __name__ == '__main__':
    unittest.main()
