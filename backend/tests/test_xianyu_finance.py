"""财务协议层用例：解析器（金额/计数/信封）与七个 mtop 方法的**请求形状**。

请求形状是这一批最值钱的部分：账单族参数包在 `{'request': {...}}`，发票族包在
`{'mtopRequest': ...}`，而且 `invoice.bill.query` 要**字符串化 JSON**、
`invoice.request.list.query` 要**对象** —— 三种写法都是真机抓包得来的，
写错任何一种平台都只回一个含糊的错误码。
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

from backend.app.xianyu import risk  # noqa: E402
from backend.app.xianyu import client as client_mod  # noqa: E402
from backend.app.xianyu.client import (  # noqa: E402
    BILL_AGGREGATE_BY_BIZ,
    EXPENSE_BIZ_DEFAULT,
    FINANCE_EXPENSE_DETAIL_API,
    FINANCE_EXPENSE_SUMMARY_API,
    FINANCE_INCOME_DETAIL_API,
    FINANCE_INCOME_SUMMARY_API,
    FINANCE_INVOICE_BILL_API,
    FINANCE_INVOICE_META_API,
    FINANCE_INVOICE_REQUEST_API,
    FINANCE_METADATA_API,
    FINANCE_PURCHASER_API,
    FINANCE_SPM,
    SessionExpiredError,
    XianyuClient,
    _count_of,
    _finance_model,
    _money,
    parse_finance_bills,
    parse_finance_invoice_bills,
    parse_finance_invoice_meta,
    parse_finance_invoice_requests,
    parse_finance_metadata,
    parse_finance_purchaser,
    parse_finance_summary,
)

COOKIE = 'unb=2200123456; _m_h5_tk=abcdef123456_1712345678901; cookie2=xyz'

#: 真机 income summary 的 `model.content`（账号 2217970177348 / 202609）
INCOME_CONTENT = {
    'currency': 'CNY',
    'totalIncomeAmount': 40.11,
    'totalIncomeRefundAmount': 0.0,
    'totalExpendAmount': 0.0,
    'totalExpendRefundAmount': 0.0,
    'netIncomeAmount': 40.11,
    'netExpendAmount': 0.0,
    'totalAggCounts': 4,
}


class MtopStub:
    """按 api 名回话的假 mtop 服务端（记录 (api, version, spm, payload)）。"""

    def __init__(self, responses: Dict[str, Any], fail_ret: Optional[Dict[str, str]] = None):
        self.responses = responses
        self.fail_ret = fail_ret or {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = parse_qs(request.content.decode('utf-8'))
        payload = json.loads(query['data'][0]) if query.get('data') else {}
        api = params.get('api', '')
        self.calls.append({
            'api': api,
            'version': params.get('v', ''),
            'spm_cnt': params.get('spm_cnt', ''),
            'payload': payload,
        })
        if api in self.fail_ret:
            return httpx.Response(200, json={'ret': [self.fail_ret[api]], 'data': None})
        if api not in self.responses:
            return httpx.Response(
                200, json={'ret': ['FAIL_SYS_API_NOT_FOUND::接口不存在'], 'data': {}})
        responder = self.responses[api]
        # 显式登记成 None = 平台成功但没给内容（`data: null`）
        data = responder(payload) if callable(responder) else responder
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': data})

    def payload_of(self, api: str) -> Dict[str, Any]:
        for call in self.calls:
            if call['api'] == api:
                return call['payload']
        raise AssertionError(f'没有调用 {api}，实际调用: {self.apis()}')

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


def make_client(responses: Dict[str, Any], fail_ret: Optional[Dict[str, str]] = None):
    stub = MtopStub(responses, fail_ret=fail_ret)
    return XianyuClient(COOKIE, transport=httpx.MockTransport(stub)), stub


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FinanceProtocolCase(unittest.TestCase):
    def setUp(self):
        risk.registry.reset()


# --------------------------------------------------------------------------- 纯函数


class MoneyTest(FinanceProtocolCase):
    def test_accepts_both_float_and_string(self):
        # 真机同一个接口族里 incomeAmount 是 float、canInvoiceAmount 是字符串
        self.assertEqual(_money(40.11), 40.11)
        self.assertEqual(_money('0.40'), 0.4)
        self.assertEqual(_money('1,234.5'), 1234.5)

    def test_unknown_is_none_not_zero(self):
        for value in (None, '', '   ', 'abc', True, False):
            self.assertIsNone(_money(value), f'{value!r} 应该认不出')

    def test_rounds_to_cent(self):
        self.assertEqual(_money(0.404999), 0.4)
        self.assertEqual(_money('0.005'), 0.01)

    def test_count_never_guesses_from_bool(self):
        self.assertEqual(_count_of(4), 4)
        self.assertEqual(_count_of('4'), 4)
        self.assertEqual(_count_of('4.9'), 4)
        self.assertIsNone(_count_of(None))
        self.assertIsNone(_count_of(True))
        self.assertIsNone(_count_of('abc'))


class FinanceModelTest(FinanceProtocolCase):
    def test_unwraps_envelope_and_accepts_bare_model(self):
        self.assertEqual(_finance_model({'httpStatusCode': 0, 'model': {'a': 1}}), {'a': 1})
        self.assertEqual(_finance_model({'a': 1}), {'a': 1})
        self.assertEqual(_finance_model({'data': {'model': {'a': 2}}}), {'a': 2})
        self.assertEqual(_finance_model({'data': {'a': 3}}), {'a': 3})
        self.assertEqual(_finance_model(None), {})


class ParseMetadataTest(FinanceProtocolCase):
    def test_reads_biz_categories_tree(self):
        data = {'httpStatusCode': 0, 'model': {'metadata': {
            'billCode': 'goods_income',
            'billName': '订单收入',
            'bizCategories': [{
                'bizCode': 'goods_income',
                'bizName': '交易货款',
                'subBizCategories': [{'subBizCode': '126866937', 'subBizName': '3C数码配件'}],
            }],
        }}}
        got = parse_finance_metadata(data)
        self.assertEqual(got['bill_code'], 'goods_income')
        self.assertEqual(got['bill_name'], '订单收入')
        self.assertEqual(got['categories'], [{
            'code': 'goods_income',
            'name': '交易货款',
            'children': [{'code': '126866937', 'name': '3C数码配件'}],
        }])

    def test_missing_metadata_is_empty_not_error(self):
        got = parse_finance_metadata({'model': {}})
        self.assertEqual(got['categories'], [])
        self.assertEqual(got['bill_code'], '')


class ParseSummaryTest(FinanceProtocolCase):
    def test_reads_content(self):
        got = parse_finance_summary({'model': {'content': INCOME_CONTENT}})
        self.assertEqual(got['income'], 40.11)
        self.assertEqual(got['net_income'], 40.11)
        self.assertEqual(got['agg_counts'], 4)
        self.assertEqual(got['currency'], 'CNY')

    def test_missing_amount_is_none(self):
        got = parse_finance_summary({'model': {'content': {'totalAggCounts': 0}}})
        self.assertIsNone(got['income'])
        self.assertEqual(got['agg_counts'], 0)


class ParseBillsTest(FinanceProtocolCase):
    def test_reads_top_level_paging(self):
        data = {
            'model': {'data': [{
                'billCycle': '202609',
                'billNo': '2026090000664051081999891099',
                'bizName': '交易货款',
                'bizType': 'goods_income',
                'curAmount': 40.11,
                'incomeAmount': 40.11,
                'incomeRefundAmount': 0.0,
                'expendAmount': 0.0,
                'expendRefundAmount': 0.0,
                'currency': 'CNY',
                'aggCounts': 4,
            }]},
            'hasNext': True,
            'totalCount': 1,
        }
        got = parse_finance_bills(data)
        self.assertEqual(got['total'], 1)
        self.assertTrue(got['has_next'])
        self.assertEqual(len(got['bills']), 1)
        self.assertEqual(got['bills'][0]['amount'], 40.11)
        self.assertEqual(got['bills'][0]['agg_counts'], 4)

    def test_empty_page(self):
        got = parse_finance_bills({'model': {'data': []}, 'hasNext': False, 'totalCount': 0})
        self.assertEqual(got['bills'], [])
        self.assertEqual(got['total'], 0)
        self.assertFalse(got['has_next'])


class ParseInvoiceTest(FinanceProtocolCase):
    def test_bills_keep_string_amounts_as_numbers(self):
        data = {'model': {
            'billGroups': [{
                'bizType': 'IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS',
                'bizTypeName': '软件服务费',
                'totalAmount': '0.40',
                'bills': [{
                    'id': '60808233',
                    'billCycle': '202609',
                    'bizType': 'IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS',
                    'itemName': '软件服务费',
                    'canInvoiceAmount': '0.40',
                    'invoiceAmountWithTax': '0.40',
                    'unableAmount': '0.00',
                    'taxRate': '0.06',
                    'companyName': '杭州闲鱼信息技术有限公司',
                    'companyTaxNo': '91330110352442187W',
                    'companyCode': 'T86',
                    'status': 'VALID',
                    'dealStatus': 'UNLOCKED_AVAILABLE',
                    'choiceType': {'choiceType': 'CAN_CHOICE'},
                    'gmtCreate': '2026-09-30 00:00:00',
                }],
            }],
            'totalAmount': '0.64',
            'totalCount': 2,
        }}
        got = parse_finance_invoice_bills(data)
        self.assertEqual(got['total_amount'], 0.64)
        self.assertEqual(got['total_count'], 2)
        group = got['groups'][0]
        self.assertEqual(group['total_amount'], 0.4)
        bill = group['bills'][0]
        self.assertEqual(bill['can_invoice_amount'], 0.4)
        self.assertEqual(bill['tax_rate'], 0.06)
        # choiceType 是**嵌套 dict**，不是字符串
        self.assertEqual(bill['choice_type'], 'CAN_CHOICE')
        self.assertEqual(bill['company_tax_no'], '91330110352442187W')

    def test_meta_list(self):
        data = {'model': {'bizTypeConfigs': [{
            'bizType': 'IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS',
            'bizTypeName': '软件服务费',
            'cargoName': '软件服务费',
            'companyName': '杭州闲鱼信息技术有限公司',
            'companyCode': 'T86',
            'companyTaxNo': '91330110352442187W',
            'companyAddress': '浙江省杭州市',
            'companyPhone': '0571-00000000',
            'taxRate': '0.06',
        }]}}
        got = parse_finance_invoice_meta(data)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['biz_type_name'], '软件服务费')
        self.assertEqual(got[0]['tax_rate'], 0.06)

    def test_requests_are_passed_through_untouched(self):
        # 真机这个列表是空的 —— 字段名一个都没见过，所以**不许**猜映射
        data = {'model': {'hasNext': False, 'list': [{'whateverField': 'x'}], 'totalCount': 1}}
        got = parse_finance_invoice_requests(data)
        self.assertEqual(got['requests'], [{'whateverField': 'x'}])
        self.assertEqual(got['total'], 1)
        self.assertFalse(got['has_next'])

    def test_purchaser(self):
        got = parse_finance_purchaser({'model': {
            'purchaserName': '万春华',
            'purchaserType': '个人',
            'supportInvoiceTypes': ['数字化电子普票'],
        }})
        self.assertEqual(got['purchaser_name'], '万春华')
        self.assertEqual(got['purchaser_type'], '个人')
        self.assertEqual(got['support_invoice_types'], ['数字化电子普票'])


# --------------------------------------------------------------------------- 请求形状


class RequestShapeTest(FinanceProtocolCase):
    def test_metadata_wraps_request(self):
        client, stub = make_client({FINANCE_METADATA_API: {'model': {}}})
        run(client.finance_metadata('goods_income'))
        self.assertEqual(stub.payload_of(FINANCE_METADATA_API),
                         {'request': {'billCode': 'goods_income'}})

    def test_income_summary_uses_request_envelope_and_aggregate(self):
        client, stub = make_client({FINANCE_INCOME_SUMMARY_API: {'model': {'content': {}}}})
        run(client.finance_month_summary('income', '202609', '202609', 'goods_income',
                                        'goods_income'))
        payload = stub.payload_of(FINANCE_INCOME_SUMMARY_API)
        self.assertEqual(payload['request']['billCycleFrom'], '202609')
        self.assertEqual(payload['request']['billCycleTo'], '202609')
        self.assertEqual(payload['request']['billCode'], 'goods_income')
        self.assertEqual(payload['request']['bizType'], 'goods_income')
        self.assertEqual(payload['request']['aggregateType'], BILL_AGGREGATE_BY_BIZ)

    def test_empty_biz_type_is_not_sent(self):
        client, stub = make_client({FINANCE_INCOME_SUMMARY_API: {'model': {'content': {}}}})
        run(client.finance_month_summary('income', '202609', '202609', 'goods_income'))
        self.assertNotIn('bizType', stub.payload_of(FINANCE_INCOME_SUMMARY_API)['request'])

    def test_expense_uses_expense_api(self):
        client, stub = make_client({FINANCE_EXPENSE_SUMMARY_API: {'model': {'content': {}}}})
        run(client.finance_month_summary('expense', '202609', '202609', 'common_settle',
                                        EXPENSE_BIZ_DEFAULT))
        self.assertEqual(stub.apis(), [FINANCE_EXPENSE_SUMMARY_API])
        self.assertEqual(stub.payload_of(FINANCE_EXPENSE_SUMMARY_API)['request']['bizType'],
                         EXPENSE_BIZ_DEFAULT)

    def test_detail_carries_paging(self):
        client, stub = make_client({FINANCE_INCOME_DETAIL_API: {'model': {'data': []}}})
        run(client.finance_month_detail('income', '202609', '202609', 'goods_income',
                                       'goods_income', 3, 15))
        payload = stub.payload_of(FINANCE_INCOME_DETAIL_API)['request']
        self.assertEqual((payload['pageNo'], payload['pageSize']), (3, 15))

    def test_expense_detail_uses_expense_api(self):
        client, stub = make_client({FINANCE_EXPENSE_DETAIL_API: {'model': {'data': []}}})
        run(client.finance_month_detail('expense', '202609', '202609', 'common_settle'))
        self.assertEqual(stub.apis(), [FINANCE_EXPENSE_DETAIL_API])

    def test_invoice_bills_sends_stringified_json(self):
        client, stub = make_client({FINANCE_INVOICE_BILL_API: {'model': {'billGroups': []}}})
        run(client.finance_invoice_bills(2, 10, ['A', 'B']))
        raw = stub.payload_of(FINANCE_INVOICE_BILL_API)['mtopRequest']
        self.assertIsInstance(raw, str, 'invoice.bill.query 的 mtopRequest 必须是字符串')
        self.assertEqual(json.loads(raw), {'pageNo': 2, 'pageSize': 10, 'bizTypes': ['A', 'B']})

    def test_invoice_requests_sends_an_object(self):
        client, stub = make_client(
            {FINANCE_INVOICE_REQUEST_API: {'model': {'list': [], 'totalCount': 0}}})
        run(client.finance_invoice_requests(2, 10))
        raw = stub.payload_of(FINANCE_INVOICE_REQUEST_API)['mtopRequest']
        self.assertIsInstance(raw, dict, 'invoice.request.list.query 的 mtopRequest 是对象')
        self.assertEqual(raw, {'pageNo': 2, 'pageSize': 10})

    def test_meta_and_purchaser_send_empty_payload(self):
        client, stub = make_client({
            FINANCE_INVOICE_META_API: {'model': {'bizTypeConfigs': []}},
            FINANCE_PURCHASER_API: {'model': {'purchaserName': '万春华'}},
        })
        run(client.finance_invoice_meta())
        run(client.finance_purchaser())
        self.assertEqual(stub.payload_of(FINANCE_INVOICE_META_API), {})
        self.assertEqual(stub.payload_of(FINANCE_PURCHASER_API), {})

    def test_version_and_spm(self):
        client, stub = make_client({FINANCE_PURCHASER_API: {'model': {}}})
        run(client.finance_purchaser())
        call = stub.calls[0]
        self.assertEqual(call['version'], '1.0')
        self.assertEqual(call['spm_cnt'], FINANCE_SPM)


class FailureModeTest(FinanceProtocolCase):
    def test_unparsable_payload_returns_none(self):
        client, _stub = make_client({FINANCE_PURCHASER_API: None})
        self.assertIsNone(run(client.finance_purchaser()))

    def test_session_expired_is_not_swallowed(self):
        # 财务族用网页版 Cookie 就能调（真机 10/10 成功），所以登录态过期必须原样上抛 ——
        # 服务层据此回 409 让用户重新扫码，不能降级成「没数据」。
        client, _stub = make_client(
            {}, fail_ret={FINANCE_PURCHASER_API: 'FAIL_SYS_SESSION_EXPIRED::Session过期'})
        with self.assertRaises(SessionExpiredError):
            run(client.finance_purchaser())

    def test_unknown_api_failure_raises(self):
        client, _stub = make_client(
            {}, fail_ret={FINANCE_PURCHASER_API: 'FAIL_SYS_API_NOT_FOUND::接口不存在'})
        with self.assertRaises(client_mod.XianyuError):
            run(client.finance_purchaser())


if __name__ == '__main__':
    unittest.main()
