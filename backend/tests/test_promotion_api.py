"""超级擦亮（推广）与安全中心（只读）的服务层 + HTTP 契约测试。

**为什么单开一组**：这两页是「只读镜像」，最容易犯的错有三个 ——

1. 把平台拿不到的数字画成 0（余额、曝光、扣分都有人会当成真值）；
2. 悄悄**写平台**（充值/提现/申诉/暂停计划），这在本项目里是绝对禁止的；
3. 把安全中心的 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（缺站点 CODE 头）误判成账号掉线，
   让用户白扫一次码。

这组用例把口径钉住：

- `test_promotion_routes_are_read_only` / `test_security_center_routes_are_read_only`：
  四个写方法一律 405；
- `test_overview_passes_platform_shapes_through`：余额/首页/指标**原样透传**，
  分/元两种单位都由前端换算，服务层不许自己改数值；
- `test_campaign_amounts_keep_platform_units`：`cost: 5990` 保持 5990（分），不除 100；
- `test_missing_cookie_is_409_and_never_calls_platform`：没 Cookie 时**一次平台都不打**；
- `test_session_expired_is_reported_as_need_relogin`：真掉线如实 409 + 回写 `need_relogin`。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_promotion_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import main as app_main  # noqa: E402
from backend.app.api import promotion as promotion_api  # noqa: E402
from backend.app.api import security_center as security_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'promo_acc'
PROMO_BASE = '/api/promotion'
SEC_BASE = '/api/security-center'

#: 真机 `mtop.taobao.idle.ad.home`（2026-10-10，账号 2217970177348）的关键字段
AD_HOME = {
    'campaignInfo': {'allCampaignCount': 0, 'systemPauseCampaignCount': 6,
                     'userPauseCampaignCount': 3,
                     'nearCompletionConsumptionCampaignCount': 0},
    'allAccountBalanceDetails': [{'balance': 3358}],
}
#: 真机 `bp.account.card.query`：金额是**元字符串**
AD_CARD = {'totalUsableBalanceOfCent': 3358, 'totalUsableBalanceOfYuan': '33.58',
           'cashBalanceOfYuan': '33.58', 'bonusBalanceOfYuan': '0',
           'frozenBalanceOfYuan': '0', 'memberId': 2217970177348}
#: 真机 `member.data.query` 的今日指标（全 0 是真值，不是拿不到）
AD_MEMBER = {'data': {'cost': 0, 'showPv': 0, 'clickPv': 0, 'payAmount': 0},
             'dataDetailVOList': [{'time': '2026-10-10 00', 'cost': 0}]}
#: 真机 `campaign.search`：`cost` 单位是**分**（5990 = ¥59.90，与官方报表 59.9 一致）
AD_CAMPAIGNS = {
    'result': [{
        'campaignId': 98927523, 'title': '擦亮宝贝20261002035349',
        'campaignTypeName': '智能推荐套餐', 'statusDescCode': 'FINISHED',
        'startTime': '2026-10-02', 'endTime': '2026-10-09', 'adGroupCount': 1,
        'entityList': [{'entityId': '108577639387', 'title': '专拍',
                        'imgUrl': 'https://img.alicdn.com/x.jpg', 'price': '25.00'}],
        'adDataDetailVO': {'cost': 5990, 'showPv': 6690, 'clickPv': 193, 'ctr': 2.88},
    }],
    'total': 9, 'nextPage': 2,
}
AD_PLANS = {'result': [{'idleAdGroupDataVO': {'data': {'cost': 5990, 'showPv': 6690}}}],
           'total': 6}
AD_ITEMS = {'result': [{'itemId': '108577639387', 'title': '专拍',
                        'imgUrl': 'https://img.alicdn.com/x.jpg', 'price': '25.00',
                        'adData': {'cost': 2100, 'showPv': 1865},
                        'previousData': {'cost': 800}}],
            'total': 26}
AD_SETTLE = {'totalAmountOfYuan': '169.27', 'cashAmountOfYuan': '169.27',
             'giftAmountOfYuan': '0', 'redPacketAmountOfYuan': '0',
             'memberId': 2217970177348}
#: 真机 `idle.pc.security.score.get` / `violation.list`
SEC_SCORE = {'totalScore': 100,
             'lastChangedScoreRecord': {'changeTypeName': '截止当前的闲气值',
                                        'expectedChangeScore': 100}}
SEC_VIOLATIONS = {'data': [{'punishId': 187988498827, 'punishTitle': '发布违禁商品信息',
                            'punishReason': '发布的商品内容中含有违禁信息',
                            'punishResult': '删除商品,闲鱼违规通知', 'point': 0,
                            'punishTime': 1758988454000, 'appealTimeLimit': 0}],
                  'totalCount': 3, 'hasNext': False}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakePromotionClient:
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

    async def ad_home(self) -> Any:
        return self._answer('ad_home')

    async def ad_account_card(self) -> Any:
        return self._answer('ad_account_card')

    async def ad_member_data(self, start: str, end: str) -> Any:
        return self._answer('ad_member_data', start, end)

    async def ad_campaigns(self, start: str, end: str, page_no: int = 1,
                           page_size: int = 20, title_like: str = '') -> Any:
        return self._answer('ad_campaigns', start, end, page_no, page_size, title_like)

    async def ad_campaign_metrics(self, start: str, end: str, page_no: int = 1,
                                  page_size: int = 20) -> Any:
        return self._answer('ad_campaign_metrics', start, end, page_no, page_size)

    async def ad_item_metrics(self, start: str, end: str, page_no: int = 1,
                              page_size: int = 20) -> Any:
        return self._answer('ad_item_metrics', start, end, page_no, page_size)

    async def ad_settle(self, start: str, end: str) -> Any:
        return self._answer('ad_settle', start, end)

    async def security_score(self) -> Any:
        return self._answer('security_score')

    async def security_violations(self, page_no: int = 1, page_size: int = 20) -> Any:
        return self._answer('security_violations', page_no, page_size)

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')


def install_fake_protocol(client: FakePromotionClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakePromotionClient:
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
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def full_client(**overrides: Any) -> FakePromotionClient:
    payloads = {
        'ad_home': AD_HOME,
        'ad_account_card': AD_CARD,
        'ad_member_data': AD_MEMBER,
        'ad_campaigns': AD_CAMPAIGNS,
        'ad_campaign_metrics': AD_PLANS,
        'ad_item_metrics': AD_ITEMS,
        'ad_settle': AD_SETTLE,
        'security_score': SEC_SCORE,
        'security_violations': SEC_VIOLATIONS,
    }
    payloads.update(overrides)
    return FakePromotionClient(payloads)


class ReadOnlyPageTest(unittest.TestCase):
    """两个页面共用的脚手架。"""

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
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上
        # （真实挂载由 test_real_app_mounts_both_routers 单独锁）。
        app = FastAPI()
        app.include_router(promotion_api.router)
        app.include_router(security_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('promoapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = '', headers: Optional[Dict[str, str]] = None):
        return self.client.get(f'{path}{query}',
                               headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def fail(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body


class PromotionApiTest(ReadOnlyPageTest):
    def test_overview_passes_platform_shapes_through(self):
        seed_account()
        data = self.ok_data(f'{PROMO_BASE}/{ACC}/overview', '?start=20261001&end=20261007')
        self.assertEqual(data['period'], {'from': '20261001', 'to': '20261007'})
        self.assertEqual(data['home'], AD_HOME)
        self.assertEqual(data['account'], AD_CARD)
        self.assertEqual(data['metrics'], AD_MEMBER)
        self.assertEqual(data['source'], 'platform')
        # 余额两种单位都原样留着（分 3358 / 元 '33.58'），服务层不许自己换算
        self.assertEqual(data['account']['totalUsableBalanceOfCent'], 3358)
        self.assertEqual(data['account']['totalUsableBalanceOfYuan'], '33.58')

    def test_default_period_is_recent_seven_days(self):
        seed_account()
        data = self.ok_data(f'{PROMO_BASE}/{ACC}/overview')
        today = datetime.now().date()
        self.assertEqual(data['period']['to'], today.strftime('%Y%m%d'))
        self.assertEqual(data['period']['from'],
                         (today - timedelta(days=6)).strftime('%Y%m%d'))
        self.assertEqual(self.fake.args_of('ad_member_data'),
                         (data['period']['from'], data['period']['to']))

    def test_campaigns_pass_dates_and_paging_to_platform(self):
        seed_account()
        data = self.ok_data(f'{PROMO_BASE}/{ACC}/campaigns',
                            '?start=20261002&end=20261008&page=2&size=30')
        self.assertEqual(data['page'], 2)
        self.assertEqual(data['page_size'], 30)
        self.assertEqual(data['campaigns'], AD_CAMPAIGNS)
        self.assertEqual(self.fake.args_of('ad_campaigns'),
                         ('20261002', '20261008', 2, 30, ''))

    def test_campaign_amounts_keep_platform_units(self):
        """`cost: 5990` 是**分**：服务层保持原值，换算只在前端做（`cents()`）。"""
        seed_account()
        data = self.ok_data(f'{PROMO_BASE}/{ACC}/campaigns')
        row = data['campaigns']['result'][0]
        self.assertEqual(row['adDataDetailVO']['cost'], 5990)
        self.assertEqual(row['entityList'][0]['imgUrl'], 'https://img.alicdn.com/x.jpg')

    def test_reports_fetch_both_plan_and_item_tables(self):
        seed_account()
        data = self.ok_data(f'{PROMO_BASE}/{ACC}/reports', '?page=1&size=20')
        self.assertEqual(data['plans'], AD_PLANS)
        self.assertEqual(data['items'], AD_ITEMS)
        self.assertEqual(self.fake.args_of('ad_campaign_metrics')[2:], (1, 20))
        self.assertEqual(self.fake.args_of('ad_item_metrics')[2:], (1, 20))

    def test_account_returns_balance_and_settle(self):
        seed_account()
        data = self.ok_data(f'{PROMO_BASE}/{ACC}/account', '?start=20261001&end=20261009')
        self.assertEqual(data['account'], AD_CARD)
        self.assertEqual(data['settle']['totalAmountOfYuan'], '169.27')
        self.assertEqual(self.fake.args_of('ad_settle'), ('20261001', '20261009'))

    def test_invalid_date_is_rejected_without_calling_platform(self):
        seed_account()
        body = self.fail(f'{PROMO_BASE}/{ACC}/overview', '?start=2026-10-01&end=20261007')
        self.assertEqual(body['code'], 'invalid_fields')
        body = self.fail(f'{PROMO_BASE}/{ACC}/campaigns', '?start=20261009&end=20261001')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_page_size_bounds_are_enforced(self):
        seed_account()
        for size in ('0', '51', '-1'):
            body = self.fail(f'{PROMO_BASE}/{ACC}/campaigns', f'?size={size}')
            self.assertEqual(body['code'], 'invalid_fields', size)
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        for path in ('overview', 'campaigns', 'reports', 'account'):
            body = self.fail(f'{PROMO_BASE}/{ACC}/{path}', status=409)
            self.assertEqual(body['code'], 'verification_required', path)
            self.assertEqual(body.get('login_state'), 'need_relogin', path)
        self.assertEqual(self.fake.calls, [])

    def test_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        body = self.fail(f'{PROMO_BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail(f'{PROMO_BASE}/nope/overview', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_reported_as_need_relogin(self):
        seed_account()
        self.fake.errors['ad_home'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail(f'{PROMO_BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_platform_failure_is_502_not_a_fake_zero(self):
        seed_account()
        self.fake.errors['ad_account_card'] = RuntimeError('FAIL_BIZ_AD_SYSTEM_BUSY')
        body = self.fail(f'{PROMO_BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('FAIL_BIZ_AD_SYSTEM_BUSY', body['message'])

    def test_promotion_routes_are_read_only(self):
        seed_account()
        for method in ('post', 'put', 'patch', 'delete'):
            for path in ('overview', 'campaigns', 'reports', 'account'):
                resp = getattr(self.client, method)(
                    f'{PROMO_BASE}/{ACC}/{path}', headers=self.headers)
                self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
        self.assertEqual(self.fake.calls, [])

    def test_requires_authorization(self):
        seed_account()
        resp = self.get(f'{PROMO_BASE}/{ACC}/overview', headers={})
        self.assertIn(resp.status_code, (401, 403), resp.text)


class SecurityCenterApiTest(ReadOnlyPageTest):
    def test_reads_score_and_violations(self):
        seed_account()
        data = self.ok_data(f'{SEC_BASE}/{ACC}')
        self.assertEqual(data['score'], SEC_SCORE)
        self.assertEqual(data['violations'], SEC_VIOLATIONS)
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(self.fake.args_of('security_violations'), (1, 20))

    def test_page_is_passed_to_platform(self):
        seed_account()
        self.ok_data(f'{SEC_BASE}/{ACC}', '?page=2&size=50')
        self.assertEqual(self.fake.args_of('security_violations'), (2, 50))

    def test_payload_carries_fetched_at(self):
        """页脚「数据更新于」靠它；缺了就会显示成 `-`（真机 2026-10-10 踩过）。"""
        seed_account()
        data = self.ok_data(f'{SEC_BASE}/{ACC}')
        self.assertRegex(str(data.get('fetched_at')), r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_page_bounds_are_rejected(self):
        seed_account()
        for query in ('?page=0', '?size=0', '?size=51'):
            resp = self.get(f'{SEC_BASE}/{ACC}{query}')
            self.assertEqual(resp.status_code, 422, f'{query}: {resp.text}')
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        body = self.fail(f'{SEC_BASE}/{ACC}', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail(f'{SEC_BASE}/nope', status=404)
        self.assertEqual(body['code'], 'not_found')

    def test_site_code_permission_error_is_not_reported_as_logout(self):
        """缺站点 CODE 头时平台回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`。

        这不是掉线（真机 2026-10-10：同一个 Cookie 调 `ad.home`/`finance` 都是 SUCCESS），
        所以必须落成 `protocol_error`（502）**而不是** `verification_required` ——
        否则用户会被反复叫去重新扫码，问题却一直在客户端缺头。
        """
        seed_account()
        self.fake.errors['security_score'] = RuntimeError(
            'MTOP 调用失败 [mtop.idle.pc.security.score.get]: '
            'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        body = self.fail(f'{SEC_BASE}/{ACC}', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'active')

    def test_session_expired_is_reported_as_need_relogin(self):
        seed_account()
        self.fake.errors['security_score'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail(f'{SEC_BASE}/{ACC}', status=409)
        self.assertEqual(body.get('login_state'), 'need_relogin')

    def test_security_center_routes_are_read_only(self):
        seed_account()
        for method in ('post', 'put', 'patch', 'delete'):
            resp = getattr(self.client, method)(f'{SEC_BASE}/{ACC}', headers=self.headers)
            self.assertEqual(resp.status_code, 405, f'{method}: {resp.text}')
        self.assertEqual(self.fake.calls, [])


class RealMountTest(unittest.TestCase):
    """真实 app 必须挂上两组路由（页面靠它才能用）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def test_real_app_mounts_both_routers(self):
        # `create_app()` 里 `include_router` 会嵌一层，所以要递归收集。
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
        for suffix in ('overview', 'campaigns', 'reports', 'account'):
            self.assertIn(f'{PROMO_BASE}/{{account_id}}/{suffix}', paths,
                          f'create_app() 没挂上推广路由 /{suffix}')
        self.assertIn(f'{SEC_BASE}/{{account_id}}', paths, 'create_app() 没挂上安全中心路由')

        # 再用真 app 各打一次，确认没有被别的路由抢走、也不是空壳。
        db.reset_connection()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        seed_account()
        fake = full_client()
        install_fake_protocol(fake)
        with TestClient(app_main.create_app()) as client:
            user_id = int(db.execute(
                "INSERT INTO users(username, password_hash, is_admin) VALUES('promomount','x',1)"))
            headers = {'Authorization': 'Bearer ' + security.issue_token(user_id)}
            promo = client.get(f'{PROMO_BASE}/{ACC}/overview', headers=headers)
            sec = client.get(f'{SEC_BASE}/{ACC}', headers=headers)
            db.execute('DELETE FROM users')
        self.assertEqual(promo.status_code, 200, promo.text)
        self.assertEqual(promo.json()['data']['source'], 'platform')
        self.assertEqual(sec.status_code, 200, sec.text)
        self.assertEqual(sec.json()['data']['score']['totalScore'], 100)


if __name__ == '__main__':
    unittest.main(verbosity=2)
