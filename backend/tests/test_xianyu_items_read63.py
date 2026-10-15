"""商品读 5 条接口的**协议层**形状测试（第 63 批 task-23），不访问真实平台。

**为什么单开一组**：这一批最容易犯的四个错 ——

1. 把平台没返回的 `total` 画成 0（「一件商品都没有」和「平台没告诉我」是两件事）；
2. 把 `searchRequest` 当对象发（它是 **JSON 字符串**，bundle 原文 `searchRequest:'{}'`）；
3. 把 `itemSearchResponseList` 之外**没证实**的键猜出来（本机没有真机样本，字段表没证据）；
4. 顺手写平台 —— 本模块只读，一条 `.set` / `.update` / `.delete` 都不许有。

跑法（仓库根目录）：
    XIANYU_APP_DIR=$TEMP/xywb_kurumi63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_items_read63.py

协议函数都是「模块级 async 函数 + client 作入参」，所以这里**不需要**打桩 HTTP：
直接传一个假 `mtop` 客户端，断言「发了什么」与「解析出了什么」。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.xianyu import items_read as proto  # noqa: E402

#: 真机 / bundle 形状：商品列表（bundle @3071624 从 `data.data.itemSearchResponseList` 取）
SEARCH_BODY = {
    'code': 'success',
    'data': {
        'itemSearchResponseList': [
            {'itemId': '777888999', 'title': 'iPhone 15 保护壳', 'price': '19.90',
             'itemStatus': '0', 'picUrl': '//img.example/1.jpg'},
        ],
        'total': 37,
    },
}
#: bundle @3075400 的筛选维度字典形状
TERMS_BODY = {
    'code': 'success',
    'data': {
        'returnService': [{'key': '7', 'value': '7天无理由退货'},
                          {'key': '', 'value': '脏行：key 空'}],
        'sendGoodsTime': [{'key': '24', 'value': '24小时内发货'}, '脏行：不是对象'],
        'serviceGuarantee': [],
    },
}
#: bundle @3062151 的预请求响应形状（只证实了这两个键）
PREGET_BODY = {'code': 'success', 'data': {'needUpFirstHandItem': True,
                                           'industryId': '50023858'}}


class FakeMtopClient:
    """假协议层：只实现 `mtop`，按 api 名回话，记录每次调用的 `data`。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None) -> None:
        self.payloads = payloads or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   spm_cnt: Optional[str] = None,
                   extra_headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v})
        return {'success': True, 'data': self.payloads.get(api), 'message': '',
                'ret': ['SUCCESS::调用成功']}

    def run(self, coro):
        return asyncio.run(coro)


class ItemsReadProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeMtopClient({
            proto.SEARCH_API: SEARCH_BODY,
            proto.SEARCH_TERM_API: TERMS_BODY,
            proto.PREGET_API: PREGET_BODY,
            proto.CATEGORY_API: {'code': 'success', 'data': {'selectIdleCategories': []}},
            proto.OPERATE_REPORT_API: {'code': 'success', 'data': {'reportId': 'r1'}},
        })

    # -- 接口名与固定入参 -------------------------------------------------

    def test_api_names_match_the_bundle_evidence(self):
        """5 个 api 全名逐字来自 bundle（改名 = 静默 404/无权限）。"""
        self.assertEqual(proto.SEARCH_API,
                         'mtop.alibaba.idle.seller.pc.common.item.search')
        self.assertEqual(proto.CATEGORY_API,
                         'mtop.alibaba.idle.seller.platform.idle.category.query')
        self.assertEqual(proto.OPERATE_REPORT_API,
                         'mtop.alibaba.idle.seller.platform.item.operate.report.get')
        self.assertEqual(proto.SEARCH_TERM_API,
                         'mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query')
        self.assertEqual(proto.PREGET_API, 'mtop.idle.pc.idleitem.preget')

    def test_fixed_values_match_the_official_page(self):
        self.assertEqual(proto.SEARCH_BIZ_TYPE, 'commonPro')
        self.assertEqual(proto.SEARCH_DEFAULT_STATUS, '0,-9')
        self.assertEqual(proto.SEARCH_PAGE_SIZES, (10, 20, 50))
        self.assertEqual(proto.SEARCH_ORDER_TYPES, ('asc', 'desc'))
        self.assertEqual(proto.SEARCH_PREGET_SCENE, 'firstHandPublish')
        self.assertEqual(proto.TERM_GROUPS,
                         ('returnService', 'sendGoodsTime', 'serviceGuarantee'))

    # -- 请求形状 ---------------------------------------------------------

    def test_search_body_matches_the_official_defaults(self):
        """默认请求体逐字：pageNo/pageSize/bizType/searchRequest/itemStatus。"""
        self.assertEqual(proto.search_data(), {
            'pageNo': 1, 'pageSize': 20, 'bizType': 'commonPro',
            'searchRequest': '{}', 'itemStatus': '0,-9',
        })

    def test_search_request_is_a_json_string(self):
        """`searchRequest` 必须是字符串（发对象平台会报参数错）。"""
        data = proto.search_data(keyword='保护壳', page=2, size=50)
        self.assertIsInstance(data['searchRequest'], str)
        self.assertEqual(json.loads(data['searchRequest']), {'title': '保护壳'})
        self.assertEqual((data['pageNo'], data['pageSize']), (2, 50))

    def test_raw_search_request_wins_over_keyword(self):
        """探针给了原始串就原样发（官方搜索表单的键名在 bundle 里看不到）。"""
        raw = '{"keyword":"壳","sortField":"price"}'
        data = proto.search_data(keyword='被忽略', search_request=raw)
        self.assertEqual(data['searchRequest'], raw)

    def test_order_keys_are_only_sent_together(self):
        """排序字段与方向必须成对：只发方向平台会报参数错，默认排序交给平台。"""
        self.assertNotIn('orderField', proto.search_data(order_type='desc'))
        self.assertNotIn('orderType', proto.search_data(order_field='price'))
        data = proto.search_data(order_field='price', order_type='DESC')
        self.assertEqual(data['orderField'], 'price')
        self.assertEqual(data['orderType'], 'desc')

    def test_preget_body_is_verbatim_from_the_bundle(self):
        self.assertEqual(proto.preget_data('777', 'PC'), {
            'itemId': '777', 'sourceId': 'PC', 'publishScene': 'firstHandPublish',
        })
        self.assertEqual(proto.preget_data()['publishScene'], 'firstHandPublish')

    # -- 响应解析 ---------------------------------------------------------

    def test_search_parses_the_official_envelope(self):
        result = self.fake.run(proto.search_items(self.fake, keyword='壳', page=2, size=10))
        self.assertEqual(self.fake.calls[0]['api'], proto.SEARCH_API)
        self.assertEqual(self.fake.calls[0]['data']['pageNo'], 2)
        self.assertEqual(result['code'], 'success')
        self.assertEqual(result['found_key'], 'itemSearchResponseList')
        self.assertEqual(result['total'], 37)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['itemId'], '777888999')
        # 行内字段**原样透传**，不提升、不猜
        self.assertEqual(result['rows'][0]['picUrl'], '//img.example/1.jpg')

    def test_unknown_list_key_is_empty_not_invented(self):
        """平台改了字段名 → 空列表 + `found_key=''`，绝不编行。"""
        self.fake.payloads[proto.SEARCH_API] = {'code': 'success', 'data': {'foo': []}}
        result = self.fake.run(proto.search_items(self.fake))
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['found_key'], '')
        self.assertIsNone(result['total'])

    def test_missing_total_is_none_not_zero(self):
        """`total` 认不出 → None（0 会让界面显示「一共 0 件商品」）。"""
        self.fake.payloads[proto.SEARCH_API] = {'code': 'success', 'data': {}}
        self.assertIsNone(self.fake.run(proto.search_items(self.fake))['total'])
        for body in ({'total': True}, {'total': ''}, {'total': None}, {'total': 'abc'}):
            self.fake.payloads[proto.SEARCH_API] = {'code': 'success', 'data': body}
            self.assertIsNone(self.fake.run(proto.search_items(self.fake))['total'], body)
        self.fake.payloads[proto.SEARCH_API] = {
            'code': 'success', 'data': {'totalCount': '12'}}
        self.assertEqual(self.fake.run(proto.search_items(self.fake))['total'], 12)

    def test_search_terms_keeps_only_clean_rows(self):
        """筛选维度：只留 `{key,value}` 都非空的行，缺失分组是空列表（不是 None）。"""
        result = self.fake.run(proto.search_terms(self.fake))
        self.assertEqual(self.fake.calls[0]['api'], proto.SEARCH_TERM_API)
        self.assertEqual(result['terms']['returnService'],
                         [{'key': '7', 'value': '7天无理由退货'}])
        self.assertEqual(result['terms']['sendGoodsTime'],
                         [{'key': '24', 'value': '24小时内发货'}])
        self.assertEqual(result['terms']['serviceGuarantee'], [])
        self.assertEqual(result['found_groups'], ['returnService', 'sendGoodsTime'])

    def test_search_terms_sends_no_data(self):
        """无参接口：`data` 必须是 None（bundle 的官方调用点 `O0()` 就没带参）。"""
        self.fake.run(proto.search_terms(self.fake))
        self.assertIsNone(self.fake.calls[0]['data'])

    def test_preget_promotes_only_the_two_proven_fields(self):
        result = self.fake.run(proto.preget(self.fake, item_id='777'))
        self.assertEqual(self.fake.calls[0]['api'], proto.PREGET_API)
        self.assertIs(result['need_up_first_hand_item'], True)
        self.assertEqual(result['industry_id'], '50023858')
        self.assertEqual(result['data'], PREGET_BODY['data'])
        self.assertNotIn('need_up_first_hand_item', result['data'])

    def test_preget_missing_fields_are_none(self):
        self.fake.payloads[proto.PREGET_API] = {'code': 'success', 'data': {}}
        result = self.fake.run(proto.preget(self.fake))
        self.assertIsNone(result['need_up_first_hand_item'])
        self.assertIsNone(result['industry_id'])
        self.assertIs(result['found'], True)

    def test_passthrough_apis_send_and_report_what_they_got(self):
        """类目 / 操作报告：入参透传，`sent` 里能一眼看出我们发了什么。"""
        category = self.fake.run(proto.category_query(self.fake, {'categoryId': '1'}))
        self.assertEqual(self.fake.calls[-1]['api'], proto.CATEGORY_API)
        self.assertEqual(category['sent'], {'categoryId': '1'})
        self.assertIs(category['found'], True)
        # 没给 params → 发空对象（不是 None：平台按 POST data 解析）
        self.fake.run(proto.category_query(self.fake))
        self.assertEqual(self.fake.calls[-1]['data'], {})
        report = self.fake.run(proto.operate_report(self.fake, {'itemId': '1'}))
        self.assertEqual(self.fake.calls[-1]['api'], proto.OPERATE_REPORT_API)
        self.assertEqual(report['sent'], {'itemId': '1'})
        self.assertEqual(report['data'], {'reportId': 'r1'})

    def test_platform_business_code_is_passed_through(self):
        """平台业务码/原文原样带出（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 是平台的有效答复）。"""
        self.fake.payloads[proto.SEARCH_API] = {
            'code': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED', 'msg': '无权限访问', 'data': None}
        result = self.fake.run(proto.search_items(self.fake))
        self.assertEqual(result['code'], 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(result['message'], '无权限访问')
        self.assertEqual(result['rows'], [])

    def test_client_level_failure_message_falls_back_to_ret(self):
        """客户端层失败（没有平台信封）时退回 `message` / `ret[0]`。"""
        self.assertEqual(proto.code_of({'success': False, 'data': None,
                                        'message': '', 'ret': ['FAIL_SYS_X::出错了']}), '')
        self.assertEqual(proto.message_of({'success': False, 'data': None,
                                           'message': '', 'ret': ['FAIL_SYS_X::出错了']}),
                         'FAIL_SYS_X::出错了')
        self.assertEqual(proto.message_of({'success': True, 'data': {'msg': '平台原文'}}),
                         '平台原文')

    # -- 只读纪律 ---------------------------------------------------------

    def test_module_exposes_exactly_the_five_read_functions(self):
        """模块只有这 5 个 async 接口（多一个就是有人在往里加写方法）。"""
        coroutines = sorted(
            name for name, value in vars(proto).items()
            if inspect.iscoroutinefunction(value))
        self.assertEqual(coroutines, ['category_query', 'operate_report', 'preget',
                                      'search_items', 'search_terms'])

    def test_no_write_api_is_declared(self):
        """5 个 api 里不许出现 set/update/delete/remove（只读批次）。"""
        apis = [value for name, value in vars(proto).items()
                if name.endswith('_API') and isinstance(value, str)]
        self.assertEqual(len(apis), 5)
        for api in apis:
            self.assertTrue(api.startswith('mtop.'), api)
            for verb in ('set', 'update', 'delete', 'remove', 'publish', 'create'):
                self.assertNotIn(verb, api, api)


if __name__ == '__main__':
    unittest.main(verbosity=2)
