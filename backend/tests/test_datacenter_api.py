"""数据组（数据总览 / 粉丝数据 / 客服数据）只读协议 + 服务层 + HTTP 契约测试。

**为什么单开一组**：这一族接口最容易犯三个错 ——

1. 把平台**拿不到**的指标画成 0（用户会以为「昨天真的没人看」）。真机实测
   （2026-10-10，账号 2217970177348）这个账号对 `datacompass` 的 `singleuser` /
   `fans` / `cs` 族一律 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`，
   所以「一屏 -」才是如实回答；只有平台**真的**返回 0 时才允许是 0。
2. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（无权限）当成账号掉线，让用户白扫一次码。
3. 顺手写平台（这族接口全是只读，一个写方法都不该存在）。

这组用例把口径钉住：

- `test_routes_are_read_only`：三个路径的 POST/PUT/PATCH/DELETE 一律 405；
- `test_missing_metrics_stay_null_never_zero`：平台没给的键是 `None`，**不是 0**；
- `test_platform_zero_is_kept`：平台真返回 0 时必须原样保留（0 与 None 是两件事）；
- `test_begin_time_real_shape`：真机 `get.begin.time` 的原始响应形状能被认出来；
- `test_all_calls_failed_is_502` / `test_partial_failure_keeps_other_metrics`：
  全坏给 502，坏一个只影响它自己贡献的指标；
- `test_missing_cookie_is_409_and_never_calls_platform`：没 Cookie 时**一次平台都不打**。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_datacenter_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import datacenter as datacenter_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import datacenter as datacenter_service  # noqa: E402
from backend.app.xianyu import datacenter as datacenter_proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'dc_acc'
BASE = '/api/datacenter'

#: 真机 `mtop.alibaba.idle.seller.datacompass.get.begin.time` 的**原始响应**
#: （2026-10-10 12:00，账号 2217970177348）：数据起始日 20260201。
BEGIN_TIME_REAL = {'code': 'success', 'data': '20260201', 'msg': '成功'}

#: 数据总览：平台给的形状（字段名按官方口径假设，认得出才填）
OVERVIEW_PAYLOAD = {
    'extendInfo': {'realDateRange': ['2026-10-04', '2026-10-10']},
    'data': {
        'exposureNum': 1971, 'exposureUv': 1298,
        'visitCount': 124, 'visitorCount': 47,
        'browseCount': 97, 'browseUv': 45,
        'onlineItemCount': 45, 'inquiryUserCount': 8,
        'threeMinReplyRate': 33.33,
    },
}
#: 只给了两个指标：其余键必须是 None（不是 0）
OVERVIEW_PARTIAL = {'data': {'exposureNum': 1971, 'visitorCount': 47}}
#: 平台**真的**返回 0：必须原样保留
OVERVIEW_ZERO = {'data': {'exposureNum': 0, 'visitCount': 0}}
FANS_PAYLOAD = {
    'data': {'fansTotal': 128, 'newFans': 6, 'lostFans': 1, 'activeFans': 33,
             'fansOrderCount': 4, 'fansBuyerCount': 3},
    'insights': [{'label': '18-24 岁', 'ratio': 0.31}],
}
CS_PAYLOAD = {
    'data': {'inquiryUserCount': 8, 'inquiryCount': 11, 'replyRate3min': 33.33,
             'avgReplyTime': 42, 'unrepliedCount': 2, 'satisfactionRate': 98.5},
}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeXianyuClient:
    """假协议层客户端：数据组只用到 `mtop`，这里只记调用、不联网。"""

    def __init__(self) -> None:
        self.account_id = ''
        # 生产里 `cookie_id` 与 `account_id` 同值（`client.py:239/248`），而
        # `common._note_session_expired` **优先**用 `cookie_id` 定位账号，所以要留着。
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        raise AssertionError('假客户端的 mtop 不该被走到（协议层被替换了）')


class FakeMtopClient:
    """真协议层用的假 client：`mtop` 按 api 回话或抛错（形状 = 真机信封，不联网）。"""

    def __init__(self, answers: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        self.calls.append((api, data))
        error = self.errors.get(api)
        if error is not None:
            raise error
        return self.answers.get(api, {})


class FakeDataCenterProtocol:
    """假协议层：按函数名回话，记录 `(client, start, end, seller_id)`。"""

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

    async def overview(self, client: Any, start: str = '', end: str = '',
                       seller_id: str = '') -> Any:
        return self._answer('overview', client, start, end, seller_id)

    async def fans(self, client: Any, start: str = '', end: str = '',
                   seller_id: str = '') -> Any:
        return self._answer('fans', client, start, end, seller_id)

    async def customer_service(self, client: Any, start: str = '', end: str = '',
                               seller_id: str = '') -> Any:
        return self._answer('customer_service', client, start, end, seller_id)

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')

    def names(self) -> List[str]:
        return [call[0] for call in self.calls]


def install_fake_protocol(protocol: FakeDataCenterProtocol) -> FakeXianyuClient:
    """注入两个假协议模块：`xianyu.client`（拿客户端）与 `xianyu.datacenter`（数据组）。"""
    client = FakeXianyuClient()
    client_module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeXianyuClient:
        # `common._note_session_expired` **优先** `client.cookie_id`、退回 `account_id`
        # （common.py:356）定位要标记的账号；生产里两者同值（client.py:239/248）。
        client.account_id = account_id
        client.cookie_id = account_id
        return client

    client_module.get_client = factory  # type: ignore[attr-defined]
    client_module.XianyuClient = FakeXianyuClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = client_module

    module = types.ModuleType('backend.app.xianyu.datacenter')
    module.overview = protocol.overview  # type: ignore[attr-defined]
    module.fans = protocol.fans  # type: ignore[attr-defined]
    module.customer_service = protocol.customer_service  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.datacenter'] = module
    return client


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)
    sys.modules.pop('backend.app.xianyu.datacenter', None)


def install_fake_client(account_id: str = ACC, answers: Optional[Dict[str, Any]] = None,
                        errors: Optional[Dict[str, BaseException]] = None) -> FakeMtopClient:
    """只注入假 `xianyu.client`，**真协议层 `xianyu.datacenter` 照常参与**。

    端到端口径用例必须走真协议层：`install_fake_protocol` 把协议层整个换掉，
    于是 `_collect` / `_begin_from` / 三个入口函数的真实行为在这组测试里一次都没被
    跑到过 ——「真机上 `begin_time` 永远是 None」就是这么漏掉的。
    """
    client = FakeMtopClient(answers=answers, errors=errors)
    client.account_id = account_id
    client.cookie_id = account_id
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = (lambda _account_id: client)  # type: ignore[attr-defined]
    module.XianyuClient = FakeMtopClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return client


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def full_protocol(**overrides: Any) -> FakeDataCenterProtocol:
    payloads = {
        'overview': {'metrics': datacenter_proto.extract_metrics(OVERVIEW_PAYLOAD, datacenter_proto.OVERVIEW_METRICS),
                     'begin_time': '20260201',
                     'real_date_range': ['2026-10-04', '2026-10-10'],
                     'suspect_keys': [], 'errors': {}},
        'fans': {'metrics': datacenter_proto.extract_metrics(FANS_PAYLOAD, datacenter_proto.FANS_METRICS),
                 'insights': FANS_PAYLOAD['insights'], 'real_date_range': None,
                 'suspect_keys': [], 'errors': {}},
        'customer_service': {'metrics': datacenter_proto.extract_metrics(CS_PAYLOAD, datacenter_proto.CS_METRICS),
                             'real_date_range': None, 'suspect_keys': [], 'errors': {}},
    }
    payloads.update(overrides)
    return FakeDataCenterProtocol(payloads)


class DataCenterApiTest(unittest.TestCase):
    """HTTP 契约 + 服务层口径。"""

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
        datacenter_service.clear_cache()
        self.proto = full_protocol()
        self.fake_client = install_fake_protocol(self.proto)
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上
        # （真实挂载由 Lead 在 main.py 里完成，见 docs/contracts/datacenter.md）。
        app = FastAPI()
        app.include_router(datacenter_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('dcapi','x',1)"))
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

    def fail_envelope(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 只读 ------------------------------------------------------------

    def test_routes_are_read_only(self):
        """对**真 app**（`main.create_app()`）验只读：自建 FastAPI 防不住挂载方式变化。

        先摘掉假协议层：`main` 在 import 时就会 `create_app()`，而 `services.finance`
        这类模块在**模块级**读 `xianyu.client.BILL_CODE_INCOME` —— 假客户端没这个常量。
        """
        seed_account()
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要
        with TestClient(app_main.create_app()) as real:
            for path in (f'{BASE}/{ACC}/overview', f'{BASE}/{ACC}/fans',
                         f'{BASE}/{ACC}/customer-service'):
                for method in ('post', 'put', 'patch', 'delete'):
                    resp = getattr(real, method)(path, headers=self.headers)
                    self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
                    self.assertFalse(resp.json().get('success'), f'{method} {path}: {resp.text}')
        self.assertEqual(self.proto.calls, [], '只读探测不该打平台')

    def test_requires_login(self):
        seed_account()
        resp = self.client.get(f'{BASE}/{ACC}/overview')
        self.assertIn(resp.status_code, (401, 403), resp.text)

    # -- 数据总览 --------------------------------------------------------

    def test_overview_metrics_pass_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(data['period'], {'from': '20261004', 'to': '20261010'})
        self.assertEqual(data['kind'], 'overview')
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['begin_time'], '20260201')
        self.assertEqual(data['metrics']['exposure'], 1971.0)
        self.assertEqual(data['metrics']['exposure_visitors'], 1298.0)
        self.assertEqual(data['metrics']['visits'], 124.0)
        self.assertEqual(data['metrics']['visit_visitors'], 47.0)
        self.assertEqual(data['metrics']['browse'], 97.0)
        self.assertEqual(data['metrics']['browse_visitors'], 45.0)
        self.assertEqual(data['metrics']['online_items'], 45.0)
        self.assertEqual(data['metrics']['inquiry_visitors'], 8.0)
        self.assertAlmostEqual(data['metrics']['inquiry_reply_rate_3min'], 33.33)
        self.assertEqual(data['real_date_range'], ['2026-10-04', '2026-10-10'])
        self.assertEqual(data['warnings'], [])
        # 契约键一个都不能少，哪怕平台没给
        self.assertEqual(set(data['metrics']), set(datacenter_proto.OVERVIEW_METRICS))

    def test_missing_metrics_stay_null_never_zero(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': datacenter_proto.extract_metrics(OVERVIEW_PARTIAL, datacenter_proto.OVERVIEW_METRICS),
            'begin_time': None, 'real_date_range': None, 'suspect_keys': [], 'errors': {},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['exposure'], 1971.0)
        self.assertEqual(data['metrics']['visit_visitors'], 47.0)
        for key in ('visits', 'browse', 'online_items', 'inquiry_visitors'):
            self.assertIsNone(data['metrics'][key], f'{key} 必须是 None，不能是 0')
        self.assertIsNone(data['begin_time'])

    def test_platform_zero_is_kept(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': datacenter_proto.extract_metrics(OVERVIEW_ZERO, datacenter_proto.OVERVIEW_METRICS),
            'begin_time': None, 'real_date_range': None, 'suspect_keys': [], 'errors': {},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['exposure'], 0.0, '平台真值 0 必须保留')
        self.assertEqual(data['metrics']['visits'], 0.0)
        self.assertIsNone(data['metrics']['browse'], '没给的仍然是 None')

    def test_partial_failure_keeps_other_metrics_and_warns(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': {'visits': 124.0, 'exposure': None, 'exposure_visitors': None,
                        'visit_visitors': None, 'browse': None, 'browse_visitors': None,
                        'online_items': None, 'inquiry_visitors': None,
                        'inquiry_reply_rate_3min': None},
            'begin_time': '20260201', 'real_date_range': None, 'suspect_keys': [],
            'errors': {'overview': 'XianyuError: MTOP 调用失败 '
                                   '[mtop...singleuser.browse.summary]: '
                                   'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['visits'], 124.0)
        self.assertIsNone(data['metrics']['exposure'])
        self.assertEqual(len(data['warnings']), 1)
        self.assertIn('没有「数据」权限', data['warnings'][0])
        # warnings 是给用户看的：只许出现中文业务名，不许漏 `overview` 这种内部调用名
        self.assertIn('数据总览', data['warnings'][0])
        self.assertNotIn('overview', data['warnings'][0])

    def test_all_calls_failed_is_502(self):
        seed_account()
        self.proto.errors['overview'] = RuntimeError('FAIL_SYS_BIZPARAM_MISSED::缺少业务参数dateType')
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('dateType', body['message'])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/nope/overview', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
                   'VALUES(?,?,?,?)', (ACC, '账号', 1, 1))
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.proto.calls, [], '没 Cookie 时一次平台都不许打')

    def test_session_expired_is_reported_as_need_relogin(self):
        seed_account()
        self.proto.errors['overview'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin', '登录态过期必须回写库')

    def test_session_expired_writes_back_via_cookie_id(self):
        """钉住 `_BoundProtocol` 的意义：回写走 client 的 **`cookie_id`**。

        `common._note_session_expired`（common.py:356）是 `cookie_id` 优先、`account_id`
        退回 —— 把 account_id 清空，只有 cookie_id 这条路能写回；而协议**模块**身上
        两个都没有，所以直接把模块传给 `call_method` 时回写必然失败（真机表现：协议
        报 Session过期、界面还显示已登录）。
        """
        seed_account()
        self.fake_client.cookie_id = ACC
        self.fake_client.account_id = ''
        self.proto.errors['overview'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_real_protocol_permission_denied_gives_null_not_502(self):
        """真机场景端到端（**真协议层** + 假 mtop）：除 `get.begin.time` 外全是「无权限」。

        三个页面都必须 200 + 一屏 `None` + 一句人话，**不能**是 502 —— 真机上 `fans`
        只有 2 个调用、`cs` 只有 1 个，按「全坏即抛」它们会**永远** 502，用户连
        「这个账号没有数据权限」都看不到。顺带端到端锁住 `begin_time`：
        `_mtop` 会剥掉外层信封，认不出裸日期的话这个字段真机上恒为 None。
        """
        seed_account()
        remove_fake_protocol()
        install_fake_client(
            answers={datacenter_proto.BEGIN_TIME_API: BEGIN_TIME_REAL},
            errors={api: RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
                    for api in (datacenter_proto.OVERVIEW_API, datacenter_proto.OVERVIEW_ITEM_API,
                                datacenter_proto.FANS_API, datacenter_proto.FANS_INSIGHTS_API,
                                datacenter_proto.CS_OVERVIEW_API)},
        )
        overview = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(overview['begin_time'], '20260201',
                         '_mtop 剥掉信封后拿到的是裸日期，也必须认得出')
        self.assertTrue(all(value is None for value in overview['metrics'].values()),
                        overview['metrics'])
        self.assertEqual(len(overview['warnings']), 2, overview['warnings'])
        self.assertIn('没有「数据」权限', overview['warnings'][0])
        for path in (f'{BASE}/{ACC}/fans', f'{BASE}/{ACC}/customer-service'):
            data = self.ok_data(path)
            self.assertTrue(all(value is None for value in data['metrics'].values()), path)
            self.assertTrue(data['warnings'], f'{path} 必须解释为什么是 -')
            self.assertIn('没有「数据」权限', data['warnings'][0], path)
        cs = self.ok_data(f'{BASE}/{ACC}/customer-service')
        self.assertIn('客服数据', cs['warnings'][0], 'warnings 不许漏内部调用名')

    def test_invalid_dates_are_400(self):
        seed_account()
        for query in ('?start=2026-10-04&end=20261010', '?start=20261010&end=20261004',
                      '?start=2026101&end=20261010', '?start=abcdefgh&end=20261010'):
            body = self.fail_envelope(f'{BASE}/{ACC}/overview', query, status=400)
            self.assertEqual(body['code'], 'invalid_fields', query)
        self.assertEqual(self.proto.calls, [], '日期不合法时不该打平台')

    def test_default_period_is_recent_seven_days(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        today = datetime.now().date()
        self.assertEqual(data['period']['to'], today.strftime('%Y%m%d'))
        self.assertEqual(data['period']['from'],
                         (today - timedelta(days=6)).strftime('%Y%m%d'))

    def test_dates_and_account_are_passed_to_protocol(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        client, start, end, seller_id = self.proto.args_of('overview')
        self.assertEqual((start, end, seller_id), ('20261004', '20261010', ACC))
        self.assertIsInstance(client, FakeXianyuClient)

    # -- 粉丝 / 客服 -----------------------------------------------------

    def test_fans_shape(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/fans', '?start=20261004&end=20261010')
        self.assertEqual(data['kind'], 'fans')
        self.assertEqual(data['metrics']['fans_total'], 128.0)
        self.assertEqual(data['metrics']['fans_new'], 6.0)
        self.assertEqual(data['metrics']['fans_lost'], 1.0)
        self.assertEqual(data['metrics']['fans_active'], 33.0)
        self.assertEqual(data['metrics']['fans_orders'], 4.0)
        self.assertEqual(data['metrics']['fans_buyers'], 3.0)
        self.assertEqual(data['insights'], FANS_PAYLOAD['insights'])
        self.assertEqual(set(data['metrics']), set(datacenter_proto.FANS_METRICS))

    def test_customer_service_shape(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/customer-service')
        self.assertEqual(data['kind'], 'customer_service')
        self.assertEqual(data['metrics']['inquiry_visitors'], 8.0)
        self.assertEqual(data['metrics']['inquiry_count'], 11.0)
        self.assertAlmostEqual(data['metrics']['reply_rate_3min'], 33.33)
        self.assertEqual(data['metrics']['avg_reply_seconds'], 42.0)
        self.assertEqual(data['metrics']['unreplied_count'], 2.0)
        self.assertAlmostEqual(data['metrics']['satisfaction'], 98.5)
        self.assertEqual(set(data['metrics']), set(datacenter_proto.CS_METRICS))

    def test_protocol_receives_three_positional_args(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/fans', '?start=20261004&end=20261010')
        self.ok_data(f'{BASE}/{ACC}/customer-service', '?start=20261004&end=20261010')
        self.assertEqual(self.proto.args_of('fans')[1:], ('20261004', '20261010', ACC))
        self.assertEqual(self.proto.args_of('customer_service')[1:], ('20261004', '20261010', ACC))

    # -- 缓存 ------------------------------------------------------------

    def test_cache_avoids_second_platform_call(self):
        seed_account()
        first = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        second = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(first, second)
        self.assertEqual(self.proto.names(), ['overview'], '5 分钟内不该重复打平台')
        datacenter_service.clear_cache()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(self.proto.names(), ['overview', 'overview'])

    def test_cache_key_includes_dates(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261005&end=20261010')
        self.assertEqual(self.proto.names(), ['overview', 'overview'])

    def test_failure_is_not_cached(self):
        seed_account()
        self.proto.errors['overview'] = RuntimeError('boom')
        self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.proto.errors.pop('overview')
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['exposure'], 1971.0, '失败结果绝不能进缓存')

    def test_metrics_missing_from_protocol_is_protocol_error(self):
        """假成功不许混过去：协议层不给 `metrics` 就是 502 `protocol_error`，
        不是 200 + 一屏 `-` 让用户以为今天真的没人来。"""
        seed_account()
        self.proto.payloads['overview']['metrics'] = None
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('指标字段', body['message'])

    def test_empty_metrics_is_protocol_error(self):
        """`metrics: {}` 也是协议异常 —— `{}` 是 dict，`isinstance(..., dict)` 挡不住它；
        放过去就是 200 + 0 个指标，会被读成「今天真的一个数据都没有」。"""
        seed_account()
        self.proto.payloads['overview']['metrics'] = {}
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('指标字段', body['message'])

    def test_real_protocol_cs_exposes_date_range_and_suspect_keys(self):
        """真协议层 + 假 mtop：客服页的 `real_date_range` / `unrecognized_keys` 必须透出。

        锁的是取值标签：`_collect` 只按 label 写 `raw`，CS 曾误读 `raw['overview']`
        （本页标签是 `'cs'`），于是这两个字段永远空转 —— 而 CS 页 6 个指标的别名全是
        假设，自证通道一断，别名认错了没人会发现。
        """
        seed_account()
        remove_fake_protocol()
        install_fake_client(answers={
            datacenter_proto.CS_OVERVIEW_API: {'data': {
                'csInquiryUserCount': 8, 'weirdUnknownCount': 3,
                'realDateRange': ['2026-10-04', '2026-10-10'],
            }},
        })
        data = self.ok_data(f'{BASE}/{ACC}/customer-service', '?start=20261004&end=20261010')
        self.assertEqual(data['real_date_range'], ['2026-10-04', '2026-10-10'],
                         '取值标签写错时这个字段永远是 None')
        self.assertIn('weirdUnknownCount', data.get('unrecognized_keys', []))
        self.assertIn('csInquiryUserCount', data.get('unrecognized_keys', []))

    def test_bound_protocol_account_id_falls_back_to_persist_account_id(self):
        """`_BoundProtocol.account_id` 不能恒为 ''：真 `XianyuClient` 没有 `account_id`
        （只有 `cookie_id` 与 `persist_account_id`，client.py），少一层退回，掉线回写
        就会被静默跳过。"""
        class Client:
            persist_account_id = 'acc-persist'
            cookie_id = ''

        bound = datacenter_service._BoundProtocol(object(), Client())
        self.assertEqual(bound.account_id, 'acc-persist')
        self.assertEqual(bound.cookie_id, '')
        with self.assertRaises(AttributeError):
            bound.cookie_str  # 协议函数才转发到模块，client 的其它属性不转发

    def test_cache_is_read_before_credentials(self):
        """缓存**先于**建客户端读：命中就直接返回 —— 既不付建客户端的代价，
        本地登录态变脏也不会把 5 分钟内的成功结果盖成 409。"""
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        db.execute("UPDATE account_credentials SET login_state='need_relogin' WHERE account_id=?",
                   (ACC,))
        data = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(data['metrics']['exposure'], 1971.0, '缓存结果不该被本地登录态盖掉')
        self.assertEqual(self.proto.names(), ['overview'], '命中缓存就不该再碰平台')

    def test_missing_protocol_layer_is_503(self):
        seed_account()
        with mock.patch.object(datacenter_service.common, 'protocol_module', return_value=None):
            value, failure = datacenter_service._call('overview', object())
        self.assertIsNone(value)
        self.assertEqual(failure['code'], 'xianyu_not_ready')
        self.assertIn('datacenter', failure['message'])

    def test_unrecognized_metric_keys_are_reported(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': datacenter_proto.extract_metrics(OVERVIEW_PAYLOAD, datacenter_proto.OVERVIEW_METRICS),
            'begin_time': None, 'real_date_range': None,
            'suspect_keys': ['newFansCount'], 'errors': {},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['unrecognized_keys'], ['newFansCount'])


class DataCenterProtocolTest(unittest.TestCase):
    """协议层纯函数：别名识别、日期入参、起始日解析。**不联网、不建库**。"""

    def test_date_data_without_dates_means_today(self):
        self.assertEqual(datacenter_proto.date_data(), {'dateType': 'recent1d'})
        self.assertEqual(datacenter_proto.date_data(None, None, 'acc1'),
                         {'dateType': 'recent1d', 'selectedSellerId': 'acc1'})

    def test_date_data_single_day_still_carries_the_date(self):
        """单日（start == end）也必须带日期：只发 `recent1d` 平台只能回**今天**，
        而 `period` 却标成用户要的那天 —— 对外撒谎。"""
        self.assertEqual(datacenter_proto.date_data('20261010', '20261010'),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010']})
        self.assertEqual(datacenter_proto.date_data('20261010', '20261010', 'acc1'),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010'], 'selectedSellerId': 'acc1'})

    def test_date_data_half_range_is_completed_from_the_other_end(self):
        self.assertEqual(datacenter_proto.date_data('20261010', ''),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010']})
        self.assertEqual(datacenter_proto.date_data(None, '20261010'),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010']})

    def test_date_data_custom_range_uses_month(self):
        payload = datacenter_proto.date_data('20261004', '20261010', 'acc1')
        self.assertEqual(payload['dateType'], 'month')
        self.assertEqual(payload['ms'], '20261004')
        self.assertEqual(payload['date'], ['20261004', '20261010'])
        self.assertEqual(payload['selectedSellerId'], 'acc1')

    def test_extract_metrics_walks_nested_payload(self):
        metrics = datacenter_proto.extract_metrics(OVERVIEW_PAYLOAD,
                                                   datacenter_proto.OVERVIEW_METRICS)
        self.assertEqual(metrics['exposure'], 1971.0)
        self.assertEqual(metrics['inquiry_reply_rate_3min'], 33.33)
        self.assertEqual(metrics['online_items'], 45.0)

    def test_extract_metrics_missing_is_none(self):
        metrics = datacenter_proto.extract_metrics({'data': {'foo': 1}},
                                                   datacenter_proto.OVERVIEW_METRICS)
        self.assertEqual(set(metrics), set(datacenter_proto.OVERVIEW_METRICS))
        for key, value in metrics.items():
            self.assertIsNone(value, f'{key} 认不出时必须是 None')

    def test_extract_metrics_keeps_platform_zero(self):
        metrics = datacenter_proto.extract_metrics({'data': {'exposureNum': 0, 'visitCount': 0}},
                                                   datacenter_proto.OVERVIEW_METRICS)
        self.assertEqual(metrics['exposure'], 0.0)
        self.assertEqual(metrics['visits'], 0.0)

    def test_find_number_ignores_booleans_and_strings(self):
        self.assertIsNone(datacenter_proto.find_number({'data': {'exposureNum': True}},
                                                       datacenter_proto.METRIC_ALIASES['exposure']))
        self.assertIsNone(datacenter_proto.find_number({'data': {'exposureNum': '1971'}},
                                                       datacenter_proto.METRIC_ALIASES['exposure']))
        self.assertEqual(datacenter_proto.find_number({'data': {'exposureNum': 1971}},
                                                      datacenter_proto.METRIC_ALIASES['exposure']), 1971.0)

    def test_begin_from_real_response(self):
        self.assertEqual(datacenter_proto._begin_from(BEGIN_TIME_REAL), '20260201')
        self.assertIsNone(datacenter_proto._begin_from({'code': 'success', 'data': 'x'}))
        self.assertIsNone(datacenter_proto._begin_from(None))

    def test_begin_from_accepts_stripped_payload(self):
        """`_mtop` 会剥掉外层信封，`begin_time` 回来的是**裸日期字符串**；
        只认 dict 的话真机上这个字段恒为 None。"""
        self.assertEqual(datacenter_proto._begin_from('20260201'), '20260201')
        self.assertIsNone(datacenter_proto._begin_from(''))

    def test_real_date_range(self):
        self.assertEqual(datacenter_proto._real_date_range(OVERVIEW_PAYLOAD),
                         ['2026-10-04', '2026-10-10'])
        self.assertIsNone(datacenter_proto._real_date_range({'data': {}}))

    def test_suspect_keys_flags_metric_like_names(self):
        found = datacenter_proto.suspect_keys({'data': {'newFansCount': 3, 'title': 'x'}},
                                              datacenter_proto.OVERVIEW_METRICS)
        self.assertIn('newFansCount', found)
        self.assertNotIn('title', found)

    def test_collect_partial_failure_keeps_others(self):
        class Client:
            async def mtop(self, api: str, data: Any = None) -> Any:
                if api == datacenter_proto.OVERVIEW_API:
                    raise RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
                return {'data': {'onlineItemCount': 45}}

        metrics, raw, errors = asyncio.run(datacenter_proto._collect(Client(), (
            ('overview', datacenter_proto.OVERVIEW_API, {}),
            ('items', datacenter_proto.OVERVIEW_ITEM_API, {}),
        ), datacenter_proto.OVERVIEW_METRICS))
        self.assertEqual(metrics['online_items'], 45.0)
        self.assertIsNone(metrics['exposure'])
        self.assertIn('overview', errors)
        self.assertIn('items', raw)

    def test_collect_all_failed_raises_most_severe_exception(self):
        """全坏才抛；混合失败时 **SessionExpired 优先** —— 它要触发 409 + 回写 need_relogin，
        被排在前面的业务错吞掉的话，掉线的账号界面还显示已登录。"""
        class Client:
            async def mtop(self, api: str, data: Any = None) -> Any:
                raise (SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
                       if api == datacenter_proto.OVERVIEW_ITEM_API
                       else RuntimeError(f'boom {api}'))

        # SessionExpired 排在**调用顺序的最后**，仍必须是它
        with self.assertRaises(SessionExpiredError) as ctx:
            asyncio.run(datacenter_proto._collect(Client(), (
                ('overview', datacenter_proto.OVERVIEW_API, {}),
                ('items', datacenter_proto.OVERVIEW_ITEM_API, {}),
            ), datacenter_proto.OVERVIEW_METRICS))
        # 类型已由 assertRaises 钉住；文案断言匹配**平台原文**，不写死我们的类名
        self.assertIn('FAIL_SYS_SESSION_EXPIRED', str(ctx.exception))

    def test_collect_all_permission_denied_is_not_fatal(self):
        """真机现状（无数据权限）：**全失败但都是「无权限」不算致命**。

        按「全坏即抛」，`fans`（2 个调用）和 `cs`（1 个调用）会**永远** 502，用户连
        「这个账号没有数据权限」都看不到；页面必须拿到 200 + 一屏 None + 一句人话。
        """
        class Client:
            async def mtop(self, api: str, data: Any = None) -> Any:
                raise RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')

        metrics, raw, errors = asyncio.run(datacenter_proto._collect(Client(), (
            ('overview', datacenter_proto.OVERVIEW_API, {}),
            ('items', datacenter_proto.OVERVIEW_ITEM_API, {}),
        ), datacenter_proto.OVERVIEW_METRICS))
        for key in datacenter_proto.OVERVIEW_METRICS:
            self.assertIsNone(metrics[key], f'{key} 拿不到必须是 None，不许是 0')
        self.assertEqual(set(errors), {'overview', 'items'})

    def test_service_warnings_translate_unauthorized(self):
        warnings = datacenter_service._warnings(
            {'overview': 'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        self.assertEqual(len(warnings), 1)
        self.assertIn('没有「数据」权限', warnings[0])
        # 给用户看的话里只许有中文业务名，不许漏 `overview` 这种内部调用名
        self.assertIn('数据总览', warnings[0])
        self.assertNotIn('overview', warnings[0])


if __name__ == '__main__':
    unittest.main()
