"""营销页只读复刻的验收测试。

三层都钉：

* **协议层**（`backend/app/xianyu/marketing.py`）：取值口径（缺指标 = `None` 不是 0）、
  参数构造的**逐字键名**、`_collect` 的容错分层、枚举全集。
* **服务层**（`backend/app/services/marketing.py`）：参数校验（枚举 / 分页 / JSON 形状）、
  契约键校验、warning 文案、登录态回写。
* **HTTP**（`backend/app/api/marketing.py`）：四条 GET 的信封、**只读 405**、
  以及「无权限 ≠ 掉线」。

**为什么另有一组 `install_fake_client` 用例**：`install_fake_protocol` 会把协议层整个
换掉，于是协议层自己的 `_collect` / 取值逻辑在这组用例里一次都跑不到 ——「真机上指标
永远是 None」这种 bug 就是这么漏掉的（`test_datacenter_api.py` 踩过同一个坑）。

**为什么参数键名要逐字钉**：这些键来自官方 bundle 反推，置信度只有「中」。`itmId`
（官方**少一个 e**）、`itemIdListStr`（JSON 字符串不是数组）、`currentSkuPriceMapStr`
这类地方写错一个字母，真机不会报错，只会静默回空 —— 所以必须有测试挡住。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import marketing as marketing_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import marketing as marketing_service  # noqa: E402
from backend.app.xianyu import marketing as marketing_proto  # noqa: E402
from backend.app.xianyu.client import XianyuError  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'mkt_acc'
BASE = '/api/marketing'

#: 接口 1 的原始响应形状：`{"code":"success","data":{...}}`，数据取 `data.list[0]`。
ITEM_RAW = {
    'code': 'success',
    'data': {'list': [{
        'showPv': 120, 'ipv': 88, 'showUv': 95, 'ipvUv': 70,
        'payQty': 3, 'payAmt': 15000, 'netPayAmt': 14000, 'estPayFee': 300,
    }]},
}
#: 只给了一个指标：其余必须是 None（不是 0）。
ITEM_PARTIAL = {'code': 'success', 'data': {'list': [{'showPv': 120}]}}
#: 真实值就是 0 —— 0 必须原样保留，不能被当成「没拿到」。
ITEM_ZERO = {'code': 'success', 'data': {'list': [{'payQty': 0}]}}

#: 接口 2 的原始响应：顶层 `result`，二选一 `taskPageResult` / `itemPageResult`。
TASKS_RAW = {
    'result': {
        'taskPageResult': {'result': [{'taskId': 't1'}, {'taskId': 't2'}],
                           'total': 7, 'nextPage': True},
        'userTrafficTaskInfo': {'boostedCount': 2},
    },
}

#: 接口 3 的原始响应：`result.trafficBoostStr` 是**字符串**。
COMP_RAW = {'result': {'trafficBoostStr': '当前价格竞争力：中'}}

#: 接口 4 的原始响应：`result.result` 是数组，条目内部字段零证据（原样透传）。
REC_RAW = {'result': {'result': [{'operation': 'TOB_START_TASK'}]}}

#: 平台明确答复「你没权限」——这是有效回答，不是掉线。
PERMISSION_TEXT = 'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED'


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeXianyuClient:
    """假协议层客户端：这组用例只走 `mtop`，这里只记调用、不联网。"""

    def __init__(self) -> None:
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        raise AssertionError('假客户端的 mtop 不该被走到（协议层被替换了）')


class FakeMtopClient:
    """真协议层用的假 client：`mtop` 按 api 回话或抛错（不联网）。"""

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


class FakeMarketingProtocol:
    """假协议层：按函数名回话，记录 `(name, client, *args)`。"""

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

    async def item_metrics(self, client: Any, item_id: str = '', date_type: str = '',
                           date_range: str = '') -> Any:
        return self._answer('item_metrics', client, item_id, date_type, date_range)

    async def tasks(self, client: Any, biz_scene: str = '', query_scene: str = '',
                    page_num: int = 1, page_size: int = 10, item_ids: Any = None) -> Any:
        return self._answer('tasks', client, biz_scene, query_scene, page_num, page_size, item_ids)

    async def competitiveness(self, client: Any, biz_scene: str = '', item_id: str = '',
                              sku_prices: Any = None, commission_rate: Any = None) -> Any:
        return self._answer('competitiveness', client, biz_scene, item_id, sku_prices,
                            commission_rate)

    async def operation_records(self, client: Any, task_id: str = '', biz_scene: str = '',
                                query_scene: str = '') -> Any:
        return self._answer('operation_records', client, task_id, biz_scene, query_scene)

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')

    def names(self) -> List[str]:
        return [call[0] for call in self.calls]


#: 假协议模块要一并带上这些属性：服务层会在**运行时**从协议模块上读枚举全集
#: （`proto.DATE_TYPES` 等）和契约键元组（`_contract_problem` 按它校验）。
_FAKE_MODULE_ATTRS = (
    'DATE_TYPES', 'DEFAULT_DATE_TYPE', 'CUSTOM_DATE_RANGE_SEPARATOR',
    'BIZ_SCENES', 'DEFAULT_BIZ_SCENE', 'QUERY_SCENES', 'DEFAULT_QUERY_SCENE',
    'RECORD_QUERY_SCENE', 'WRITE_APIS',
    'ITEM_METRICS', 'TASK_METRICS', 'TASK_FLAGS', 'COMPETITIVENESS_TEXTS', 'RECORD_METRICS',
)


def install_fake_protocol(protocol: FakeMarketingProtocol) -> FakeXianyuClient:
    """注入假协议层：`xianyu.client`（拿客户端）与 `xianyu.marketing`（四个页面）。"""
    client = FakeXianyuClient()
    client_module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeXianyuClient:
        # `common._note_session_expired` 优先用 `client.cookie_id` 定位账号；生产里
        # `cookie_id` 与 `account_id` 同值（client.py:239/248）。
        client.account_id = account_id
        client.cookie_id = account_id
        return client

    client_module.get_client = factory  # type: ignore[attr-defined]
    client_module.XianyuClient = FakeXianyuClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = client_module

    module = types.ModuleType('backend.app.xianyu.marketing')
    for attr in _FAKE_MODULE_ATTRS:
        setattr(module, attr, getattr(marketing_proto, attr))
    module.item_metrics = protocol.item_metrics  # type: ignore[attr-defined]
    module.tasks = protocol.tasks  # type: ignore[attr-defined]
    module.competitiveness = protocol.competitiveness  # type: ignore[attr-defined]
    module.operation_records = protocol.operation_records  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.marketing'] = module
    return client


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)
    sys.modules.pop('backend.app.xianyu.marketing', None)


def install_fake_client(account_id: str = ACC, answers: Optional[Dict[str, Any]] = None,
                        errors: Optional[Dict[str, BaseException]] = None) -> FakeMtopClient:
    """只注入假 `xianyu.client`，**真协议层 `xianyu.marketing` 照常参与**。"""
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


def _metrics(raw: Dict[str, Any]) -> Dict[str, Any]:
    return marketing_proto.extract_metrics(raw['data']['list'][0],
                                           marketing_proto.ITEM_METRICS)


def full_protocol(**overrides: Any) -> FakeMarketingProtocol:
    page = TASKS_RAW['result']['taskPageResult']
    payloads = {
        'item_metrics': {
            'metrics': _metrics(ITEM_RAW), 'texts': {}, 'flags': {},
            'items': ITEM_RAW['data']['list'], 'suspect_keys': [], 'errors': {}, 'raw': {},
        },
        'tasks': {
            'metrics': marketing_proto.extract_metrics(page, marketing_proto.TASK_METRICS),
            'texts': {},
            'flags': marketing_proto.extract_flags(page, marketing_proto.TASK_FLAGS),
            'tasks': page['result'], 'page_kind': 'task',
            'user_task_info': TASKS_RAW['result']['userTrafficTaskInfo'],
            'suspect_keys': [], 'errors': {}, 'raw': {},
        },
        'competitiveness': {
            'metrics': {},
            'texts': marketing_proto.extract_texts(COMP_RAW['result'],
                                                   marketing_proto.COMPETITIVENESS_TEXTS),
            'flags': {}, 'items': None, 'suspect_keys': [], 'errors': {}, 'raw': {},
        },
        'operation_records': {
            'metrics': {}, 'texts': {}, 'flags': {},
            'records': REC_RAW['result']['result'], 'suspect_keys': [], 'errors': {}, 'raw': {},
        },
    }
    payloads.update(overrides)
    return FakeMarketingProtocol(payloads)


def _blank_payload(keys: tuple, *, texts: tuple = (), flags: tuple = (),
                   list_key: Optional[str] = None) -> Dict[str, Any]:
    """一屏 None 的协议层输出（「平台没给」而不是「平台说 0」）。"""
    payload: Dict[str, Any] = {
        'metrics': {key: None for key in keys},
        'texts': {key: None for key in texts},
        'flags': {key: None for key in flags},
        'suspect_keys': [], 'errors': {}, 'raw': {},
    }
    if list_key:
        payload[list_key] = None
    return payload


class _MarketingHttpBase(unittest.TestCase):
    """自建 app + 真鉴权（不依赖 `main.py` 是否已经挂上本路由）。"""

    def setUp(self):
        db.reset_connection()
        remove_fake_protocol()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        app = FastAPI()
        app.include_router(marketing_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('mktapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        remove_fake_protocol()
        self.app_ctx.__exit__(None, None, None)

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

    def login_state(self) -> str:
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        return '' if row is None else str(row['login_state'])


class MarketingApiTest(_MarketingHttpBase):
    """HTTP 契约 + 服务层口径（协议层被替换成假的）。"""

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
        super().setUp()
        self.proto = full_protocol()
        self.fake_client = install_fake_protocol(self.proto)

    # -- 只读 ------------------------------------------------------------

    def test_only_get_is_registered_on_the_router(self):
        methods = set()
        for route in marketing_api.router.routes:
            methods |= set(getattr(route, 'methods', set()) or set())
        self.assertEqual(methods, {'GET'}, f'营销页只读，实际注册了 {methods}')

    def test_routes_are_read_only(self):
        """四个路径的 POST/PUT/PATCH/DELETE 全部 405（FastAPI 对未注册方法自动回）。"""
        seed_account()
        paths = (f'{BASE}/{ACC}/items', f'{BASE}/{ACC}/tasks',
                 f'{BASE}/{ACC}/price-competitiveness', f'{BASE}/{ACC}/records')
        for path in paths:
            for method in ('post', 'put', 'patch', 'delete'):
                resp = getattr(self.client, method)(path, headers=self.headers)
                self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
                self.assertFalse(resp.json().get('success'), f'{method} {path}: {resp.text}')
        self.assertEqual(self.proto.calls, [], '只读探测不该打平台')

    def test_routes_are_read_only_on_real_app_when_mounted(self):
        """挂进 `main.py` 之后，真 app 上同样只读（未挂时跳过，见契约文档 §3.1）。"""
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要
        if 'marketing' not in app_main.ROUTE_MODULES:
            self.skipTest('marketing 尚未挂进 main.ROUTE_MODULES（需 Lead 追加）')
        seed_account()
        with TestClient(app_main.create_app()) as real:
            for path in (f'{BASE}/{ACC}/items', f'{BASE}/{ACC}/tasks',
                         f'{BASE}/{ACC}/price-competitiveness',
                         f'{BASE}/{ACC}/records'):
                for method in ('post', 'put', 'patch', 'delete'):
                    resp = getattr(real, method)(path, headers=self.headers)
                    self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')

    def test_write_apis_are_registered_but_never_called(self):
        """两个写接口只登记名字，本模块一次都不许发。"""
        self.assertEqual(len(marketing_proto.WRITE_APIS), 2)
        for api in marketing_proto.WRITE_APIS:
            self.assertIn('bpro.marketing.task', api)
        seed_account()
        for path, query in (
            (f'{BASE}/{ACC}/items', '?item_id=i1'),
            (f'{BASE}/{ACC}/tasks', ''),
            (f'{BASE}/{ACC}/price-competitiveness', '?item_id=i1'),
            (f'{BASE}/{ACC}/records', '?task_id=t1'),
        ):
            self.ok_data(path, query)
        self.assertEqual(self.fake_client.calls, [], '假客户端不该被走到')
        self.assertTrue(all(name not in marketing_proto.WRITE_APIS
                            for name in self.proto.names()))

    def test_requires_login(self):
        seed_account()
        resp = self.client.get(f'{BASE}/{ACC}/tasks')
        self.assertIn(resp.status_code, (401, 403), resp.text)

    # -- 四个页面的 happy path -------------------------------------------

    def test_item_metrics_pass_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items', '?item_id=i1&date_type=recent7d')
        self.assertEqual(data['kind'], 'items')
        self.assertEqual(data['metrics']['show_pv'], 120.0)
        self.assertEqual(data['metrics']['pay_amt'], 15000.0)
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['item_id'], 'i1')
        self.assertEqual(data['date_type'], 'recent7d')
        self.assertEqual(self.proto.args_of('item_metrics')[1:], ('i1', 'recent7d', ''))

    def test_item_metrics_default_date_type_is_recent1d(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(data['date_type'], 'recent1d')
        self.assertEqual(self.proto.args_of('item_metrics')[2], 'recent1d')

    def test_tasks_pass_through_and_echo_params(self):
        seed_account()
        data = self.ok_data(
            f'{BASE}/{ACC}/tasks',
            '?biz_scene=TRAFFIC_ORDER_BOOST&query_scene=ORDER_BOOST_SET&page=2&size=20')
        self.assertEqual(data['metrics']['total'], 7.0)
        self.assertEqual(data['tasks'], [{'taskId': 't1'}, {'taskId': 't2'}])
        self.assertEqual(data['page_kind'], 'task')
        self.assertEqual(data['user_task_info'], {'boostedCount': 2})
        self.assertTrue(data['flags']['next_page'])
        self.assertEqual(data['page'], 2)
        self.assertEqual(data['page_size'], 20)
        self.assertEqual(self.proto.args_of('tasks')[1:],
                         ('TRAFFIC_ORDER_BOOST', 'ORDER_BOOST_SET', 2, 20, []))

    def test_tasks_item_ids_are_split_into_a_list(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/tasks', '?item_ids=i1,%20i2%20,,i1')
        self.assertEqual(self.proto.args_of('tasks')[5], ['i1', 'i2'])

    def test_price_competitiveness_returns_text_not_number(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/price-competitiveness',
                            '?item_id=i1&biz_scene=TRAFFIC_ORDER_BOOST')
        self.assertEqual(data['texts']['traffic_boost_text'], '当前价格竞争力：中')
        self.assertEqual(data['metrics'], {})
        self.assertEqual(self.proto.args_of('competitiveness')[2], 'i1')

    def test_price_competitiveness_parses_sku_prices_json(self):
        seed_account()
        prices = {'123': 9.9}
        data = self.ok_data(f'{BASE}/{ACC}/price-competitiveness',
                            '?item_id=i1&sku_prices=' + quote(json.dumps(prices), safe=''))
        self.assertEqual(data['sku_prices'], prices)
        self.assertEqual(self.proto.args_of('competitiveness')[3], prices)

    def test_operation_records_pass_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/records', '?task_id=t1')
        self.assertEqual(data['records'], [{'operation': 'TOB_START_TASK'}])
        self.assertEqual(data['query_scene'], 'ORDER_BOOST_OPERATION_RECORD')
        self.assertEqual(self.proto.args_of('operation_records')[1:],
                         ('t1', 'TRAFFIC_ORDER_BOOST', 'ORDER_BOOST_OPERATION_RECORD'))

    # -- 三条硬口径 ------------------------------------------------------

    def test_missing_metric_is_null_not_zero(self):
        """平台没给的指标必须是 `null`，**不能**顶成 0。"""
        self.proto.payloads['item_metrics'] = {
            'metrics': _metrics(ITEM_PARTIAL), 'texts': {}, 'flags': {},
            'items': None, 'suspect_keys': [], 'errors': {}, 'raw': {},
        }
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(data['metrics']['show_pv'], 120.0)
        for key in ('ipv', 'ipv_uv', 'pay_qty', 'pay_amt', 'net_pay_amt', 'est_pay_fee'):
            self.assertIsNone(data['metrics'][key], f'{key} 必须是 None，不是 0')
        self.assertEqual(len(data['metrics']), len(marketing_proto.ITEM_METRICS))

    def test_zero_is_preserved(self):
        """真值是 0 时必须原样保留 —— 「0」和「没拿到」是两回事。"""
        self.proto.payloads['item_metrics'] = {
            'metrics': _metrics(ITEM_ZERO), 'texts': {}, 'flags': {},
            'items': None, 'suspect_keys': [], 'errors': {}, 'raw': {},
        }
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(data['metrics']['pay_qty'], 0.0)
        self.assertIsNotNone(data['metrics']['pay_qty'])

    def test_contract_keys_are_all_present(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(set(data['metrics']), set(marketing_proto.ITEM_METRICS))

    def test_missing_contract_key_is_502(self):
        """协议层少给一个契约键 = 协议异常（502），不是「成功但没数据」。"""
        self.proto.payloads['item_metrics'] = {
            'metrics': {'show_pv': 1.0}, 'texts': {}, 'flags': {},
            'items': None, 'suspect_keys': [], 'errors': {}, 'raw': {},
        }
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/items', '?item_id=i1', 502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('缺字段', body['message'])

    def test_empty_metrics_dict_is_502(self):
        self.proto.payloads['tasks'] = {'metrics': {}, 'texts': {}, 'flags': {},
                                        'tasks': None, 'suspect_keys': [], 'errors': {}, 'raw': {}}
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 502)

    def test_non_dict_payload_is_502(self):
        self.proto.payloads['tasks'] = 'not-a-dict'
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 502)

    def test_records_requires_the_list_key(self):
        self.proto.payloads['operation_records'] = {'metrics': {}, 'texts': {}, 'flags': {},
                                                    'suspect_keys': [], 'errors': {}, 'raw': {}}
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/records', '?task_id=t1', 502)

    def test_permission_denied_is_null_with_warning_not_502(self):
        """`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 是**有效回答**：200 + 一屏 None + 人话 warning。"""
        payload = _blank_payload(marketing_proto.ITEM_METRICS, list_key='items')
        payload['errors'] = {'item_metrics': PERMISSION_TEXT}
        self.proto.payloads['item_metrics'] = payload
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertTrue(all(value is None for value in data['metrics'].values()))
        self.assertEqual(len(data['warnings']), 1)
        text = data['warnings'][0]
        self.assertIn('没有开通这个功能的权限', text)
        self.assertIn('营销商品数据', text)
        self.assertNotIn('重新登录', text)
        self.assertEqual(self.login_state(), 'active', '无权限不该写 need_relogin')

    def test_link_failure_is_502(self):
        self.proto.errors['tasks'] = RuntimeError('boom')
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('boom', body['message'])
        self.assertEqual(self.login_state(), 'active')

    def test_session_expired_is_409_and_writes_need_relogin(self):
        self.proto.errors['tasks'] = SessionExpiredError('登录态已过期')
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        body = self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 409)
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.proto.calls, [], '缺 Cookie 必须零平台调用')
        # 前置检查只**回答**「要重新登录」，不替用户改库里的登录态（与 datacenter 同口径）。
        self.assertEqual(self.login_state(), 'active')

    def test_already_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 409)
        self.assertEqual(self.proto.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/nope/tasks', '', 404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_missing_protocol_layer_is_503(self):
        seed_account()
        with mock.patch.object(marketing_service.common, 'protocol_module', return_value=None):
            body = self.fail_envelope(f'{BASE}/{ACC}/tasks', '', 503)
        self.assertEqual(body['code'], 'xianyu_not_ready')

    # -- 参数校验 --------------------------------------------------------

    def test_item_id_is_required(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/items', '', 400)
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('item_id', body['message'])
        self.assertEqual(self.proto.calls, [])

    def test_unknown_date_type_is_400(self):
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/items', '?item_id=i1&date_type=nope', 400)
        self.assertEqual(self.proto.calls, [])

    def test_custom_date_requires_a_range(self):
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/items', '?item_id=i1&date_type=customDate', 400)
        self.fail_envelope(f'{BASE}/{ACC}/items',
                           '?item_id=i1&date_type=customDate&date_range=2026', 400)

    def test_custom_date_range_is_accepted(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/items',
                            '?item_id=i1&date_type=customDate&date_range=20261001%7C20261007')
        self.assertEqual(data['date_range'], '20261001|20261007')
        self.assertEqual(self.proto.args_of('item_metrics')[3], '20261001|20261007')

    def test_date_range_without_custom_date_is_400(self):
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/items',
                           '?item_id=i1&date_type=recent1d&date_range=20261001%7C20261007', 400)

    def test_unknown_biz_scene_is_400(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/tasks', '?biz_scene=NOPE', 400)
        self.assertIn('TRAFFIC_ORDER_BOOST', body['message'])
        self.assertEqual(self.proto.calls, [])

    def test_unknown_query_scene_is_400(self):
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '?query_scene=NOPE', 400)
        self.assertEqual(self.proto.calls, [])

    def test_page_size_out_of_range_is_400(self):
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '?size=51', 400)
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '?size=0', 400)
        self.fail_envelope(f'{BASE}/{ACC}/tasks', '?page=abc', 400)
        self.assertEqual(self.proto.calls, [])

    def test_task_id_is_required(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/records', '', 400)
        self.assertIn('task_id', body['message'])

    def test_sku_prices_must_be_a_json_object(self):
        seed_account()
        self.fail_envelope(f'{BASE}/{ACC}/price-competitiveness', '?item_id=i1&sku_prices=nope',
                           400)
        self.fail_envelope(f'{BASE}/{ACC}/price-competitiveness',
                           '?item_id=i1&sku_prices=%5B1%5D', 400)
        self.assertEqual(self.proto.calls, [])

    def test_commission_rate_only_for_order_boost(self):
        seed_account()
        self.fail_envelope(
            f'{BASE}/{ACC}/price-competitiveness',
            '?item_id=i1&biz_scene=TRAFFIC_DRIVEN_REPRICING&commission_rate=0.05', 400)

    def test_unknown_kind_path_is_not_routed(self):
        """不存在的 kind 不是 404 信封，而是 FastAPI 自己的 Not Found（没有这条路由）。"""
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/nope-kind')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.proto.calls, [])


class MarketingEndToEndTest(_MarketingHttpBase):
    """走**真协议层**（只换 client）的端到端用例。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def test_item_metrics_reads_the_real_envelope(self):
        """真协议层：剥 `data` 信封、取 `data.list[0]`、按别名填 8 个指标。"""
        fake = install_fake_client(answers={marketing_proto.MARKET_ITEM_LIST_API: ITEM_RAW})
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['metrics']['show_pv'], 120.0)
        self.assertEqual(data['metrics']['est_pay_fee'], 300.0)
        self.assertEqual(len(fake.calls), 1)
        api, sent = fake.calls[0]
        self.assertEqual(api, marketing_proto.MARKET_ITEM_LIST_API)
        self.assertEqual(sent, {'itmId': 'i1', 'dateType': 'recent1d', 'dateRange': ''})

    def test_item_metrics_keeps_a_real_zero(self):
        fake = install_fake_client(answers={marketing_proto.MARKET_ITEM_LIST_API: ITEM_ZERO})
        seed_account()
        data = self.get(f'{BASE}/{ACC}/items', '?item_id=i1').json()['data']
        self.assertEqual(data['metrics']['pay_qty'], 0.0)
        self.assertIsNone(data['metrics']['show_pv'])
        self.assertEqual(len(fake.calls), 1)

    def test_tasks_reads_task_page_result(self):
        fake = install_fake_client(answers={marketing_proto.MARKET_INFO_API: TASKS_RAW})
        seed_account()
        data = self.get(f'{BASE}/{ACC}/tasks').json()['data']
        self.assertEqual(data['metrics']['total'], 7.0)
        self.assertEqual(len(data['tasks']), 2)
        self.assertEqual(data['page_kind'], 'task')
        sent = fake.calls[0][1]
        self.assertEqual(sent['pageNum'], 1)
        self.assertEqual(sent['pageSize'], 10)
        self.assertNotIn('itemIdListStr', sent)

    def test_tasks_serialises_item_ids_as_a_json_string(self):
        fake = install_fake_client(answers={marketing_proto.MARKET_INFO_API: TASKS_RAW})
        seed_account()
        self.get(f'{BASE}/{ACC}/tasks', '?item_ids=i1,i2')
        sent = fake.calls[0][1]
        self.assertEqual(json.loads(sent['itemIdListStr']), ['i1', 'i2'])

    def test_competitiveness_reads_traffic_boost_str(self):
        fake = install_fake_client(answers={marketing_proto.PRICE_COMPETITIVENESS_API: COMP_RAW})
        seed_account()
        data = self.get(f'{BASE}/{ACC}/price-competitiveness', '?item_id=i1').json()['data']
        self.assertEqual(data['texts']['traffic_boost_text'], '当前价格竞争力：中')
        sent = fake.calls[0][1]
        self.assertEqual(sent['itemId'], 'i1')
        self.assertEqual(json.loads(sent['currentSkuPriceMapStr']), {})
        self.assertNotIn('currentCommissionRate', sent)

    def test_operation_records_reads_result_result(self):
        fake = install_fake_client(answers={marketing_proto.OPERATION_RECORD_API: REC_RAW})
        seed_account()
        data = self.get(f'{BASE}/{ACC}/records', '?task_id=t1').json()['data']
        self.assertEqual(data['records'], [{'operation': 'TOB_START_TASK'}])
        self.assertEqual(fake.calls[0][1]['taskId'], 't1')

    def test_permission_denied_on_the_real_protocol_layer(self):
        """真协议层：**所有**接口都因无权限失败 → 不抛，一屏 None + warning（200）。"""
        fake = install_fake_client(
            errors={marketing_proto.MARKET_ITEM_LIST_API: XianyuError(PERMISSION_TEXT)})
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(all(value is None for value in data['metrics'].values()))
        self.assertIn('没有开通这个功能的权限', data['warnings'][0])
        self.assertEqual(len(fake.calls), 1)

    def test_link_error_on_the_real_protocol_layer_is_502(self):
        fake = install_fake_client(
            errors={marketing_proto.MARKET_ITEM_LIST_API: RuntimeError('socket closed')})
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/items', '?item_id=i1')
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('socket closed', resp.json()['message'])
        self.assertEqual(len(fake.calls), 1)


class MarketingProtocolTest(unittest.TestCase):
    """协议层纯函数：取值口径 + 参数构造的逐字键名。"""

    # -- 取值口径 --------------------------------------------------------

    def test_find_number_returns_none_when_absent(self):
        self.assertIsNone(marketing_proto.find_number({}, ('showPv',)))
        self.assertIsNone(marketing_proto.find_number({'other': 1}, ('showPv',)))

    def test_find_number_ignores_bool(self):
        self.assertIsNone(marketing_proto.find_number({'showPv': True}, ('showPv',)))

    def test_find_number_preserves_zero(self):
        self.assertEqual(marketing_proto.find_number({'showPv': 0}, ('showPv',)), 0.0)

    def test_find_number_reads_nested(self):
        payload = {'data': {'list': [{'metrics': {'payQty': 5}}]}}
        self.assertEqual(marketing_proto.find_number(payload, ('payQty',)), 5.0)

    def test_find_number_is_case_insensitive(self):
        self.assertEqual(marketing_proto.find_number({'SHOWPV': 7}, ('showPv',)), 7.0)

    def test_find_text_rejects_blank(self):
        self.assertIsNone(marketing_proto.find_text({'trafficBoostStr': '   '},
                                                    ('trafficBoostStr',)))
        self.assertEqual(marketing_proto.find_text({'trafficBoostStr': ' 中 '},
                                                   ('trafficBoostStr',)), '中')

    def test_find_list_returns_none_for_empty_array(self):
        """空数组不是「没有数据」，是「没拿到」—— 必须 None。"""
        self.assertIsNone(marketing_proto.find_list({'list': []}))
        self.assertIsNone(marketing_proto.find_list({}))
        self.assertEqual(marketing_proto.find_list({'list': [1]}), [1])

    def test_find_value_passes_through_non_strings(self):
        self.assertIs(marketing_proto.find_value({'nextPage': True}, ('nextPage',)), True)
        self.assertEqual(marketing_proto.find_value({'nextPage': 3}, ('nextPage',)), 3)
        self.assertIsNone(marketing_proto.find_value({'nextPage': None}, ('nextPage',)))

    def test_extract_metrics_fills_every_contract_key(self):
        metrics = marketing_proto.extract_metrics(ITEM_PARTIAL['data']['list'][0],
                                                  marketing_proto.ITEM_METRICS)
        self.assertEqual(set(metrics), set(marketing_proto.ITEM_METRICS))
        self.assertEqual(metrics['show_pv'], 120.0)
        self.assertIsNone(metrics['pay_amt'])

    def test_suspect_keys_flags_unknown_metricish_names(self):
        found = marketing_proto.suspect_keys({'whateverCount': 1, 'name': 'x'},
                                             marketing_proto.ITEM_METRICS)
        self.assertIn('whatevercount', found)
        self.assertNotIn('name', found)

    # -- 参数构造（逐字键名） --------------------------------------------

    def test_item_list_data_uses_the_official_misspelling(self):
        """官方键名就是 `itmId`（少一个 e）—— 写成 `itemId` 平台会直接不认。"""
        sent = marketing_proto.item_list_data('i1')
        self.assertEqual(set(sent), {'itmId', 'dateType', 'dateRange'})
        self.assertEqual(sent['itmId'], 'i1')
        self.assertEqual(sent['dateType'], 'recent1d')
        self.assertEqual(sent['dateRange'], '')

    def test_info_data_omits_item_id_list_when_empty(self):
        sent = marketing_proto.info_data()
        self.assertEqual(set(sent), {'bizScene', 'queryScene', 'pageNum', 'pageSize'})
        self.assertEqual(sent['pageNum'], 1)
        self.assertEqual(sent['pageSize'], 10)

    def test_info_data_serialises_item_ids_as_json_string(self):
        sent = marketing_proto.info_data(item_ids=['i1', 'i2'])
        self.assertIsInstance(sent['itemIdListStr'], str)
        self.assertEqual(json.loads(sent['itemIdListStr']), ['i1', 'i2'])

    def test_competitiveness_data_keys(self):
        sent = marketing_proto.competitiveness_data('TRAFFIC_ORDER_BOOST', 'i1', {'1': 9.9})
        self.assertEqual(set(sent), {'bizScene', 'itemId', 'currentSkuPriceMapStr'})
        self.assertEqual(json.loads(sent['currentSkuPriceMapStr']), {'1': 9.9})
        with_rate = marketing_proto.competitiveness_data(
            'TRAFFIC_ORDER_BOOST', 'i1', {}, '0.05')
        self.assertEqual(with_rate['currentCommissionRate'], '0.05')

    def test_record_data_keys(self):
        sent = marketing_proto.record_data('t1')
        self.assertEqual(set(sent), {'taskId', 'bizScene', 'queryScene'})
        self.assertEqual(sent['queryScene'], 'ORDER_BOOST_OPERATION_RECORD')

    # -- 枚举与容错 ------------------------------------------------------

    def test_enums_match_the_bundle(self):
        self.assertEqual(len(marketing_proto.DATE_TYPES), 4)
        self.assertEqual(len(marketing_proto.BIZ_SCENES), 2)
        self.assertEqual(len(marketing_proto.QUERY_SCENES), 15)
        self.assertEqual(len(marketing_proto.OPERATIONS), 8)
        self.assertEqual(len(marketing_proto.OPERATION_CODES), 10)
        self.assertEqual(marketing_proto.RECORD_QUERY_SCENE, 'ORDER_BOOST_OPERATION_RECORD')

    def test_error_classifiers(self):
        self.assertTrue(marketing_proto._is_permission_error(RuntimeError(PERMISSION_TEXT)))
        self.assertFalse(marketing_proto._is_permission_error(RuntimeError('boom')))
        self.assertTrue(marketing_proto._is_session_error(SessionExpiredError('x')))
        self.assertFalse(marketing_proto._is_session_error(RuntimeError('boom')))

    def test_collect_swallows_all_permission_errors(self):
        client = FakeMtopClient(errors={
            marketing_proto.MARKET_ITEM_LIST_API: RuntimeError(PERMISSION_TEXT)})
        raw, errors = asyncio.run(marketing_proto._collect(client, (
            ('item_metrics', marketing_proto.MARKET_ITEM_LIST_API, {}, 'data'),
        )))
        self.assertEqual(raw, {})
        self.assertIn('item_metrics', errors)

    def test_collect_raises_when_a_link_error_is_mixed_in(self):
        client = FakeMtopClient(errors={
            marketing_proto.MARKET_ITEM_LIST_API: RuntimeError('boom'),
            marketing_proto.MARKET_INFO_API: RuntimeError(PERMISSION_TEXT)})
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(marketing_proto._collect(client, (
                ('a', marketing_proto.MARKET_ITEM_LIST_API, {}, 'data'),
                ('b', marketing_proto.MARKET_INFO_API, {}, 'result'),
            )))
        self.assertIn('boom', str(ctx.exception))

    def test_collect_prefers_the_session_error(self):
        client = FakeMtopClient(errors={
            marketing_proto.MARKET_ITEM_LIST_API: RuntimeError('boom'),
            marketing_proto.MARKET_INFO_API: SessionExpiredError('过期')})
        with self.assertRaises(SessionExpiredError):
            asyncio.run(marketing_proto._collect(client, (
                ('a', marketing_proto.MARKET_ITEM_LIST_API, {}, 'data'),
                ('b', marketing_proto.MARKET_INFO_API, {}, 'result'),
            )))

    def test_mtop_unwraps_both_envelope_keys(self):
        client = FakeMtopClient(answers={
            marketing_proto.MARKET_ITEM_LIST_API: ITEM_RAW,
            marketing_proto.MARKET_INFO_API: TASKS_RAW,
        })
        data = asyncio.run(marketing_proto._mtop(
            client, marketing_proto.MARKET_ITEM_LIST_API, {}, 'data'))
        self.assertIn('list', data)
        result = asyncio.run(marketing_proto._mtop(
            client, marketing_proto.MARKET_INFO_API, {}, 'result'))
        self.assertIn('taskPageResult', result)

    def test_item_metrics_keeps_an_empty_screen_when_platform_says_nothing(self):
        client = FakeMtopClient(answers={marketing_proto.MARKET_ITEM_LIST_API: {'code': 'success'}})
        payload = asyncio.run(marketing_proto.item_metrics(client, 'i1'))
        self.assertEqual(set(payload['metrics']), set(marketing_proto.ITEM_METRICS))
        self.assertTrue(all(value is None for value in payload['metrics'].values()))


if __name__ == '__main__':
    unittest.main()
