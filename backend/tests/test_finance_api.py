"""财务四页（只读）的服务层 + HTTP 契约测试。

**为什么单开一组**：财务页最容易犯的错是「把拿不到画成 0.00」—— 用户会以为账对不上。
这组用例把四条硬口径钉住：

1. 认不出的金额/件数一律 `null`，**不补 0**（`test_unknown_numbers_stay_null`）；
2. 账号没 Cookie / 掉线 → **409 + `login_state=need_relogin`**，而且**一次平台都不打**
   （`test_*_never_calls_platform`）—— 真机实测这族接口用网页版 Cookie 就能调通，
   所以回 `FAIL_SYS_SESSION_EXPIRED` 就是真掉线，不能像商品数据那样降级；
3. 平台**不接受空的业务大类**（真机 `INVALID_PARAM::业务大类不能为空`），
   默认值必须从平台 metadata 的第一级大类取，不能自己写死
   （`test_biz_type_comes_from_metadata_not_hardcoded`）；
4. 四个页面**全是只读**（`test_finance_routes_are_read_only`）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_finance_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import main as app_main  # noqa: E402
from backend.app.api import finance as finance_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import finance as finance_service  # noqa: E402
from backend.app.xianyu.client import EXPENSE_BIZ_DEFAULT  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'fin_acc'
OTHER = 'fin_other'
BASE = '/api/finance'

#: 真机 income summary 的归一化结果（账号 2217970177348 / 202609 / 交易货款）
INCOME_SUMMARY = {
    'currency': 'CNY', 'income': 40.11, 'income_refund': 0.0,
    'expense': 0.0, 'expense_refund': 0.0,
    'net_income': 40.11, 'net_expense': 0.0, 'agg_counts': 4,
}
INCOME_BILLS = {
    'bills': [{
        'bill_cycle': '202609', 'bill_no': '2026090000664051081999891099',
        'biz_name': '交易货款', 'biz_type': 'goods_income',
        'amount': 40.11, 'income': 40.11, 'income_refund': 0.0,
        'expense': 0.0, 'expense_refund': 0.0, 'currency': 'CNY', 'agg_counts': 4,
    }],
    'total': 1, 'has_next': True,
}
INCOME_META = {
    'bill_code': 'goods_income', 'bill_name': '订单收入',
    'categories': [{'code': 'goods_income', 'name': '交易货款', 'children': []}],
}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeFinanceClient:
    """假协议层：按方法名回话，记录调用参数。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[tuple] = []

    def _answer(self, name: str, *args: Any) -> Any:
        self.calls.append((name,) + args)
        error = self.errors.get(name)
        if error is not None:
            raise error
        return self.payloads.get(name)

    async def finance_metadata(self, bill_code: str = '') -> Any:
        return self._answer('finance_metadata', bill_code)

    async def finance_month_summary(self, kind: str, start: str, end: str,
                                    bill_code: str, biz_type: str = '') -> Any:
        return self._answer('finance_month_summary', kind, start, end, bill_code, biz_type)

    async def finance_month_detail(self, kind: str, start: str, end: str, bill_code: str,
                                   biz_type: str = '', page_no: int = 1,
                                   page_size: int = 20) -> Any:
        return self._answer('finance_month_detail', kind, start, end, bill_code, biz_type,
                            page_no, page_size)

    async def finance_invoice_bills(self, page_no: int = 1, page_size: int = 20,
                                    biz_types: Any = None) -> Any:
        return self._answer('finance_invoice_bills', page_no, page_size, biz_types)

    async def finance_invoice_requests(self, page_no: int = 1, page_size: int = 20) -> Any:
        return self._answer('finance_invoice_requests', page_no, page_size)

    async def finance_invoice_meta(self) -> Any:
        return self._answer('finance_invoice_meta')

    async def finance_purchaser(self) -> Any:
        return self._answer('finance_purchaser')

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')


def install_fake_protocol(client: FakeFinanceClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeFinanceClient:
        # `common._note_session_expired` 靠 `client.account_id` 找到要标记的账号
        # （真实客户端有 `cookie_id`），少了它「登录态过期」就不会回写库。
        client.account_id = account_id
        return client

    module.get_client = factory  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) VALUES(?,?,?,?)',
               (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def full_client(**overrides: Any) -> FakeFinanceClient:
    payloads = {
        'finance_metadata': INCOME_META,
        'finance_month_summary': INCOME_SUMMARY,
        'finance_month_detail': INCOME_BILLS,
        'finance_invoice_bills': {
            'groups': [{
                'biz_type': 'IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS',
                'biz_type_name': '软件服务费', 'total_amount': 0.4,
                'bills': [{'bill_cycle': '202609', 'can_invoice_amount': 0.4,
                           'choice_type': 'CAN_CHOICE'}],
            }],
            'total_amount': 0.64, 'total_count': 2,
        },
        'finance_invoice_requests': {'requests': [], 'total': 0, 'has_next': False},
        'finance_invoice_meta': [{'biz_type': 'IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS',
                                  'biz_type_name': '软件服务费', 'tax_rate': 0.06}],
        'finance_purchaser': {'purchaser_name': '万春华', 'purchaser_type': '个人',
                              'support_invoice_types': ['数字化电子普票']},
    }
    payloads.update(overrides)
    return FakeFinanceClient(payloads)


class FinanceApiTest(unittest.TestCase):
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
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上 'finance'
        # （真实挂载由 test_real_app_mounts_finance_routes 单独锁）。
        app = FastAPI()
        app.include_router(finance_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('finapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = '', headers: Optional[Dict[str, str]] = None):
        return self.client.get(f'{BASE}{path}{query}',
                               headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def assert_error(self, resp, status: int, code: str) -> Dict[str, Any]:
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertEqual(body.get('code'), code, body)
        self.assertTrue(body.get('message'), body)
        return body

    # -- 收入 / 支出账单 --------------------------------------------------

    def test_income_returns_summary_bills_and_meta(self):
        seed_account()
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income', '?from=202609&to=202609')
        self.assertEqual(data['kind'], 'income')
        self.assertEqual(data['bill_code'], 'goods_income')
        self.assertEqual(data['period'], {'from': '202609', 'to': '202609'})
        self.assertEqual(data['summary']['income'], 40.11)
        self.assertEqual(data['summary']['agg_counts'], 4)
        self.assertEqual(len(data['bills']), 1)
        self.assertEqual(data['bills'][0]['bill_no'], '2026090000664051081999891099')
        self.assertEqual(data['total'], 1)
        self.assertTrue(data['has_next'])
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['biz_categories'], INCOME_META['categories'])

    def test_fetched_at_is_utc_second_precision(self):
        # `frontend/src/format.ts::formatTime` 对不带时区的这种串补 Z 再本地化 ——
        # 这里要是发本地时间，页面上就白多 8 小时。
        seed_account()
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income')
        stamp = data['fetched_at']
        parsed = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')
        delta = abs((datetime.utcnow() - parsed).total_seconds())
        self.assertLess(delta, 120, f'{stamp} 看起来不是 UTC')

    def test_biz_type_comes_from_metadata_not_hardcoded(self):
        # 平台不接受空 bizType（真机 INVALID_PARAM::业务大类不能为空），
        # 默认值必须来自平台自己的分类树。
        seed_account()
        self.fake.payloads['finance_metadata'] = {
            'bill_code': 'goods_income', 'bill_name': '订单收入',
            'categories': [{'code': 'platform_code_1', 'name': '交易货款', 'children': []}],
        }
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income')
        self.assertEqual(data['biz_type'], 'platform_code_1')
        self.assertEqual(
            self.fake.args_of('finance_month_summary'),
            ('income', data['period']['from'], data['period']['to'], 'goods_income',
             'platform_code_1'))
        self.assertEqual(self.fake.args_of('finance_month_detail')[4], 'platform_code_1')

    def test_explicit_biz_type_wins_over_metadata(self):
        seed_account()
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income', '?biz_type=IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS')
        self.assertEqual(data['biz_type'], 'IDLE_TECHNICAL_SERVICE_FEE_STD_PROCESS')

    def test_expense_falls_back_to_default_biz_type(self):
        # 支出账单真机的第一级大类就是「同城服务软件服务费」；metadata 拿不到时兜底到它。
        seed_account()
        self.fake.payloads['finance_metadata'] = None
        self.fake.payloads['finance_month_summary'] = {
            'currency': 'CNY', 'expense': None, 'net_expense': None, 'agg_counts': 0}
        self.fake.payloads['finance_month_detail'] = {'bills': [], 'total': 0, 'has_next': False}
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/expense')
        self.assertEqual(data['bill_code'], 'common_settle')
        self.assertEqual(data['biz_type'], EXPENSE_BIZ_DEFAULT)
        self.assertEqual(data['bill_name'], '支出账单')
        self.assertEqual(len(data['warnings']), 1)
        self.assertIn('metadata', data['warnings'][0])

    def test_income_without_metadata_fails_with_a_clear_reason(self):
        # 收入账单没有兜底大类 → 必须明确说「取不到分类」，而不是拿空 bizType 去打平台。
        seed_account()
        self.fake.payloads['finance_metadata'] = None
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/income'), 502, 'protocol_error')
        self.assertIn('业务大类', body['message'])
        self.assertEqual([c[0] for c in self.fake.calls], ['finance_metadata'])

    def test_default_cycle_is_current_month(self):
        seed_account()
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income')
        current = datetime.now().strftime('%Y%m')
        self.assertEqual(data['period'], {'from': current, 'to': current})

    def test_unknown_numbers_stay_null(self):
        seed_account()
        self.fake.payloads['finance_month_summary'] = {
            'currency': 'CNY', 'income': None, 'net_income': None, 'agg_counts': None}
        self.fake.payloads['finance_month_detail'] = {
            'bills': [{'bill_cycle': '202609', 'amount': None, 'agg_counts': None}],
            'total': None, 'has_next': None,
        }
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income')
        self.assertIsNone(data['summary']['income'])
        self.assertIsNone(data['summary']['net_income'])
        self.assertIsNone(data['bills'][0]['amount'])
        self.assertIsNone(data['total'])
        self.assertIsNone(data['has_next'])
        self.assertNotIn(0.0, [data['summary']['income'], data['bills'][0]['amount']])

    def test_detail_failure_keeps_summary_with_a_warning(self):
        seed_account()
        self.fake.errors['finance_month_detail'] = RuntimeError('接口 500')
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/income')
        self.assertEqual(data['summary']['income'], 40.11)
        self.assertEqual(data['bills'], [])
        self.assertEqual(len(data['warnings']), 1)
        self.assertIn('流水', data['warnings'][0])
        self.assertIn('接口 500', data['warnings'][0])

    def test_both_summary_and_detail_failing_is_502(self):
        seed_account()
        self.fake.errors['finance_month_summary'] = RuntimeError('接口 500')
        self.fake.errors['finance_month_detail'] = RuntimeError('接口 500')
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/income'), 502, 'protocol_error')
        self.assertIn('平台财务接口调用失败', body['message'])
        self.assertIn('接口 500', body['message'])

    # -- 登录态 / 参数 ----------------------------------------------------

    def test_no_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/income'), 409, 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.fake.calls, [], '没有 Cookie 时不该打平台')

    def test_need_relogin_is_409_and_never_calls_platform(self):
        seed_account(login_state='need_relogin')
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/purchaser'), 409, 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_from_platform_is_409_not_502(self):
        seed_account()
        self.fake.errors['finance_purchaser'] = SessionExpiredError('Session过期')
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/purchaser'), 409, 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_unknown_account_is_404(self):
        install_fake_protocol(self.fake)
        self.assert_error(self.get(f'/{OTHER}/income'), 404, 'not_found')

    def test_bad_cycle_format_is_400(self):
        seed_account()
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/income', '?from=2026-09'), 400,
                                 'invalid_fields')
        self.assertIn('YYYYMM', body['message'])
        self.assertEqual(self.fake.calls, [])

    def test_cycle_from_after_to_is_400(self):
        seed_account()
        install_fake_protocol(self.fake)
        self.assert_error(self.get(f'/{ACC}/income', '?from=202610&to=202609'), 400,
                          'invalid_fields')

    def test_paging_limits_are_400(self):
        seed_account()
        install_fake_protocol(self.fake)
        self.assert_error(self.get(f'/{ACC}/income', '?page=0'), 400, 'invalid_fields')
        self.assert_error(self.get(f'/{ACC}/income', '?size=999'), 400, 'invalid_fields')

    # -- 发票 ------------------------------------------------------------

    def test_invoices_returns_groups_and_computed_has_next(self):
        seed_account()
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/invoices')
        self.assertEqual(data['total_amount'], 0.64)
        self.assertEqual(data['total_count'], 2)
        self.assertFalse(data['has_next'], '20/页 装得下 2 条，不该有下一页')
        self.assertEqual(data['groups'][0]['total_amount'], 0.4)
        self.assertEqual(data['groups'][0]['bills'][0]['choice_type'], 'CAN_CHOICE')
        self.assertEqual(data['meta'][0]['biz_type_name'], '软件服务费')
        self.assertEqual(data['requests'], [])
        self.assertEqual(data['request_total'], 0)

    def test_invoices_has_next_is_none_when_total_is_unknown(self):
        seed_account()
        self.fake.payloads['finance_invoice_bills'] = {'groups': [], 'total_amount': None,
                                                       'total_count': None}
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/invoices')
        self.assertIsNone(data['has_next'])
        self.assertIsNone(data['total_amount'])

    def test_invoices_meta_failure_is_only_a_warning(self):
        seed_account()
        self.fake.errors['finance_invoice_meta'] = RuntimeError('接口下线')
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/invoices')
        self.assertEqual(data['meta'], [])
        self.assertEqual(len(data['warnings']), 1)
        self.assertIn('业务类型', data['warnings'][0])

    def test_invoices_both_bill_and_request_failing_is_502(self):
        seed_account()
        self.fake.errors['finance_invoice_bills'] = RuntimeError('接口 500')
        self.fake.errors['finance_invoice_requests'] = RuntimeError('接口 500')
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/invoices'), 502, 'protocol_error')
        self.assertIn('平台发票接口调用失败', body['message'])

    def test_invoice_requests_are_passed_through_untouched(self):
        # 真机这个列表是空的 —— 字段名一个都没见过，所以不许猜映射。
        seed_account()
        self.fake.payloads['finance_invoice_requests'] = {
            'requests': [{'unknownField': 'x', 'another': 1}], 'total': 1, 'has_next': False}
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/invoices')
        self.assertEqual(data['requests'], [{'unknownField': 'x', 'another': 1}])
        self.assertEqual(data['request_total'], 1)

    def test_invoice_biz_types_filter_is_split(self):
        seed_account()
        install_fake_protocol(self.fake)
        self.ok_data(f'/{ACC}/invoices', '?biz_types=A,%20B')
        self.assertEqual(self.fake.args_of('finance_invoice_bills')[2], ['A', 'B'])

    # -- 开票信息 ---------------------------------------------------------

    def test_purchaser_returns_info(self):
        seed_account()
        install_fake_protocol(self.fake)
        data = self.ok_data(f'/{ACC}/purchaser')
        self.assertEqual(data['purchaser_name'], '万春华')
        self.assertEqual(data['purchaser_type'], '个人')
        self.assertEqual(data['support_invoice_types'], ['数字化电子普票'])
        self.assertEqual(data['source'], 'platform')

    def test_purchaser_failure_is_502(self):
        seed_account()
        self.fake.errors['finance_purchaser'] = RuntimeError('接口 500')
        install_fake_protocol(self.fake)
        body = self.assert_error(self.get(f'/{ACC}/purchaser'), 502, 'protocol_error')
        self.assertIn('开票信息接口调用失败', body['message'])

    # -- 只读 / 鉴权 / 挂载 -----------------------------------------------

    def test_finance_routes_are_read_only(self):
        seed_account()
        install_fake_protocol(self.fake)
        for method in ('post', 'put', 'patch', 'delete'):
            resp = getattr(self.client, method)(
                f'{BASE}/{ACC}/income', headers=self.headers)
            self.assertEqual(resp.status_code, 405, f'{method} 不该存在: {resp.text}')
        self.assertEqual(self.fake.calls, [])

    def test_routes_require_auth(self):
        seed_account()
        install_fake_protocol(self.fake)
        resp = self.client.get(f'{BASE}/{ACC}/income')
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_real_app_mounts_finance_routes(self):
        # `create_app()` 里 `include_router` 会嵌一层，所以要递归收集；
        # 顺带用真 app 打一次，确认没有被商品详情的 `/{account_id}/{item_id}` 抢走。
        def collect(routes: Any, out: List[str]) -> None:
            for route in routes:
                path = getattr(route, 'path', '')
                if path:
                    out.append(path)
                nested = getattr(route, 'routes', None) or getattr(
                    getattr(route, 'original_router', None), 'routes', None)
                if nested:
                    collect(nested, out)

        paths: List[str] = []
        collect(app_main.create_app().routes, paths)
        for suffix in ('income', 'expense', 'invoices', 'purchaser'):
            self.assertIn(f'{BASE}/{{account_id}}/{suffix}', paths,
                          f'create_app() 没挂上财务路由 /{suffix}')

        seed_account()
        install_fake_protocol(self.fake)
        with TestClient(app_main.create_app()) as client:
            resp = client.get(f'{BASE}/{ACC}/income', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['source'], 'platform')


if __name__ == '__main__':
    unittest.main()
