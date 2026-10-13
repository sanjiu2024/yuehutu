# -*- coding: utf-8 -*-
"""粉丝价（全部粉 / 老粉 / 已购粉）的测试：只读 1 条 + 写 1 条。

**为什么单开一组**：这一族最容易犯的错是：

1. **把「整份替换」当成「改一个组」** —— 官方弹窗预填三组值再一起发，`fansPriceList`
   里没有的组 = 没配置 ⇒ **留空就是清空**。请求形状抄错，用户会莫名其妙丢掉两档价；
2. 没确认就发写请求（红线：未确认时**一个平台请求都不许发**）；
3. 确认判断用 `bool(...)` —— `bool('false')` 是 `True`（第 38 批真缺陷的教训）；
4. 把平台没返回的配置补成 `[]` —— 「平台没说」和「三个组都没配」是两件事；
5. 拿不到的价格补 0 —— 0 元粉丝价是假数据，必须是 `null`（前端显示 `-`）；
6. 价格校验漏项：`0`、`-1`、3 位小数、`1e3` 都得拦（官方只挡了数字输入框，我们不留洞）；
7. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现价格非法。

**一次真机请求都没发过**（写验证的红线是零平台写请求）。这里用假协议层钉住
「接口名、版本号、入参形状与官方 bundle 逐字一致」（偏移见 `backend/app/xianyu/fans_price.py`
的常量注释与 `WRITE_APIS['fans_price_set']['evidence']`），平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_fans_price_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import fans_price as fans_price_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import fans_price as service  # noqa: E402
from backend.app.xianyu import fans_price as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'fans_acc'
BASE = '/api/items'
ITEM = '123456'

SUCCESS = {'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 读成功：官方 `data.data.fansPriceConfig.fansPriceList`（两层 data，与 `body_of` 认的容器一致）。
READ_OK = {
    'success': True,
    'data': {
        'code': 'success',
        'data': {
            'fansPriceConfig': {
                'itemId': ITEM,
                'fansPriceList': [
                    {'fansGroup': 'all', 'priceString': '9.9'},
                    {'fansGroup': 'old', 'priceString': '8.8'},
                ],
            },
        },
    },
    **SUCCESS,
}
#: 平台明确回了**空列表** —— 这是「三个组真的都没配」，与「没给字段」不同。
READ_EMPTY = {
    'success': True,
    'data': {'code': 'success', 'data': {'fansPriceConfig': {'fansPriceList': []}}},
    **SUCCESS,
}
#: 平台没给配置键（可能改了字段名）—— 必须 `null` + warning，不能补成 `[]`。
READ_NO_KEY = {'success': True, 'data': {'code': 'success', 'data': {'foo': 1}}, **SUCCESS}
#: 平台层直接给列表 + 别名键（`group` / `price`）—— 解析要认。
READ_ALIAS = {
    'success': True,
    'data': {'code': 'success', 'fansPriceList': [{'group': 'buy', 'price': '7.7'}]},
    **SUCCESS,
}
#: 业务层拒绝（信封 success=True，业务体 `success=False`）。
READ_BIZ_FAIL = {
    'success': True,
    'data': {'code': 'success', 'data': {'success': False, 'failReason': '商品不存在'}},
    **SUCCESS,
}

#: 写成功：官方判据是 `resp.data.data` 有东西（@4101069 真 ⇒ toast「编辑成功」）。
SET_OK = {'success': True, 'data': {'code': 'success', 'data': {'ok': 1}}, **SUCCESS}
#: 平台接受了但没回执（业务体只剩 `code`/`msg` 这类「回话」）⇒ 既不说成功也不说失败。
SET_NO_ECHO = {'success': True, 'data': {'code': 'success', 'msg': '调用成功'}, **SUCCESS}
#: 业务层拒绝 + 平台原话。
SET_BIZ_FAIL = {
    'success': True,
    'data': {'code': 'success', 'data': {'success': False, 'failReason': '粉丝价不能高于原价'}},
    **SUCCESS,
}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

#: 冻结契约里的两条路由（同一条 path，GET 读 + POST 写）。
PATH = f'{BASE}/{{account_id}}/{{item_id}}/fans-price'
EXPECTED_ROUTES = {('GET', PATH), ('POST', PATH)}

FALSE_LIKE = ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2, [], {})


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeFansPriceClient:
    """假协议层：只实现 `mtop`，记录每次调用的 (api, data, v)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0') -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v})
        error = self.errors.get(api)
        if error is not None:
            raise error
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有准备 {api} 的响应')
        return self.payloads[api]

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]

    def call_of(self, api: str) -> Dict[str, Any]:
        for call in self.calls:
            if call['api'] == api:
                return call
        raise AssertionError(f'没有调用 {api}，实际调用: {self.apis()}')


def install_fake_protocol(client: FakeFansPriceClient, *, with_factory: bool = True,
                          factory: Any = None) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def default_factory(account_id: str) -> FakeFansPriceClient:
        client.account_id = account_id
        return client

    if with_factory:
        module.get_client = factory or default_factory  # type: ignore[attr-defined]
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


def full_client(**overrides: Any) -> FakeFansPriceClient:
    payloads = {proto.GET_API: READ_OK, proto.SET_API: SET_OK}
    payloads.update(overrides)
    return FakeFansPriceClient(payloads)


# --------------------------------------------------------------------------- 协议层


class FansPriceProtocolTest(unittest.TestCase):
    """接口名、版本、白名单、价格校验、请求形状、解析（全部来自 bundle 静态证据）。"""

    def test_api_names_are_the_official_ones(self):
        self.assertEqual(proto.GET_API,
                         'mtop.alibaba.idle.seller.platform.item.operate.fansprice.get')
        self.assertEqual(proto.SET_API,
                         'mtop.alibaba.idle.seller.platform.item.operate.fansprice.set')

    def test_every_api_is_v1_0(self):
        self.assertEqual(set(proto.READ_VERSIONS.values()), {'1.0'})
        self.assertEqual(set(proto.WRITE_VERSIONS.values()), {'1.0'})

    def test_read_and_write_sets_are_disjoint_and_complete(self):
        self.assertEqual(set(proto.READ_APIS), {proto.GET_API})
        self.assertEqual(proto.WRITE_API_SET, {proto.SET_API})
        self.assertFalse(set(proto.READ_APIS) & set(proto.WRITE_API_SET))
        self.assertEqual(set(proto.ALL_APIS), set(proto.READ_APIS) | set(proto.WRITE_API_SET))

    def test_assert_write_only_accepts_registered_write_apis(self):
        """只读接口不能被当写接口放行（`ALL_APIS - READ_APIS` 那种写法会漏）。"""
        self.assertEqual(proto.assert_write(proto.SET_API), proto.SET_API)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.GET_API)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write('mtop.something.else')

    def test_assert_read_rejects_write_apis(self):
        self.assertEqual(proto.assert_read(proto.GET_API), proto.GET_API)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_read(proto.SET_API)

    # -- 价格校验 --------------------------------------------------------

    def test_good_prices_pass(self):
        for value in ('9', '9.9', '9.99', '.9', ' 9.9 ', 9.9, 10, '99999999.99'):
            self.assertIsNone(proto.price_problem(value), repr(value))

    def test_blank_price_is_not_a_problem(self):
        """空 = 这一组不提交（也就是清空该组），不是格式错。"""
        for value in ('', '   ', None):
            self.assertIsNone(proto.price_problem(value), repr(value))

    def test_bad_prices_are_rejected_with_chinese_words(self):
        self.assertEqual(proto.price_problem('abc'), '粉丝价必须是数字')
        self.assertEqual(proto.price_problem('1e3'), '粉丝价必须是数字')
        self.assertEqual(proto.price_problem('9.'), '粉丝价必须是数字')
        self.assertEqual(proto.price_problem('+9'), '粉丝价必须是数字')
        self.assertEqual(proto.price_problem('0'), '粉丝价必须大于0，请修改')
        self.assertEqual(proto.price_problem('-1'), '粉丝价必须是数字')
        self.assertEqual(proto.price_problem('9.999'), '粉丝价最多为2位小数，请修改')
        self.assertEqual(proto.price_problem('100000000'), '粉丝价不能超过 99999999.99')

    # -- 请求形状 --------------------------------------------------------

    def test_get_request_shape(self):
        api, data, version = proto.get_request(ITEM)
        self.assertEqual(api, proto.GET_API)
        self.assertEqual(data, {'itemId': ITEM})
        self.assertEqual(version, '1.0')

    def test_get_request_rejects_a_blank_item_id(self):
        with self.assertRaises(ValueError):
            proto.get_request('  ')

    def test_set_request_keeps_the_official_nesting(self):
        """官方：`o={itemId,fansPriceList}` → `i={itemId,fansPriceConfig:o}`。"""
        api, data, version = proto.set_request(ITEM, {'all': '9.9', 'old': '8.8'}, confirm=True)
        self.assertEqual(api, proto.SET_API)
        self.assertEqual(version, '1.0')
        self.assertEqual(data['itemId'], ITEM)
        config = data['fansPriceConfig']
        self.assertEqual(config['itemId'], ITEM)
        self.assertEqual(config['fansPriceList'],
                         [{'fansGroup': 'all', 'priceString': '9.9'},
                          {'fansGroup': 'old', 'priceString': '8.8'}])

    def test_set_request_order_follows_the_official_columns(self):
        """顺序固定「全部粉 → 老粉 → 已购粉」，与入参给的顺序无关。"""
        _api, data, _v = proto.set_request(ITEM, {'buy': '7.7', 'all': '9.9', 'old': '8.8'},
                                           confirm=True)
        self.assertEqual([entry['fansGroup'] for entry in data['fansPriceConfig']['fansPriceList']],
                         ['all', 'old', 'buy'])

    def test_set_request_accepts_the_snake_case_list(self):
        _api, data, _v = proto.set_request(
            ITEM, [{'fans_group': 'buy', 'price_string': '7.7'}], confirm=True)
        self.assertEqual(data['fansPriceConfig']['fansPriceList'],
                         [{'fansGroup': 'buy', 'priceString': '7.7'}])

    def test_set_request_needs_a_strict_confirm(self):
        """协议层那道硬闸只认 `confirm is True`（服务层会先归一）。"""
        for value in (None, 'true', 1, 'yes'):
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(value)):
                proto.set_request(ITEM, {'all': '9.9'}, confirm=value)

    def test_set_request_refuses_to_clear_everything(self):
        for groups in ({}, {'all': '', 'old': None, 'buy': '  '}, [], None):
            with self.assertRaises(ValueError, msg=repr(groups)):
                proto.set_request(ITEM, groups, confirm=True)

    def test_set_request_rejects_unknown_groups_and_bad_prices(self):
        with self.assertRaises(ValueError) as ctx:
            proto.set_request(ITEM, {'x': '9.9'}, confirm=True)
        self.assertIn('不认识的粉丝组', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            proto.set_request(ITEM, {'all': '0'}, confirm=True)
        self.assertIn('全部粉：粉丝价必须大于0', str(ctx.exception))

    def test_request_of_dispatches_and_rejects_unknown_actions(self):
        api, data, _v = proto.request_of('fans_price_set', item_id=ITEM, groups={'all': '9.9'},
                                         confirm=True)
        self.assertEqual(api, proto.SET_API)
        self.assertEqual(data['fansPriceConfig']['fansPriceList'],
                         [{'fansGroup': 'all', 'priceString': '9.9'}])
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('fans_price_get', item_id=ITEM)

    # -- 解析 ------------------------------------------------------------

    def test_parse_config_reads_the_official_shape(self):
        parsed = proto.parse_config(READ_OK)
        self.assertTrue(parsed['found'])
        self.assertEqual(parsed['problem'], '')
        self.assertEqual(parsed['groups'], {'all': '9.9', 'old': '8.8', 'buy': None})
        self.assertEqual([entry['fans_group'] for entry in parsed['prices']], ['all', 'old'])

    def test_parse_config_empty_list_is_found(self):
        """`[]` 是「真没配」，`found` 必须是 True（否则前端会说「平台没返回」）。"""
        parsed = proto.parse_config(READ_EMPTY)
        self.assertTrue(parsed['found'])
        self.assertEqual(parsed['prices'], [])
        self.assertEqual(parsed['groups'], {'all': None, 'old': None, 'buy': None})

    def test_parse_config_missing_key_is_not_found(self):
        parsed = proto.parse_config(READ_NO_KEY)
        self.assertFalse(parsed['found'])
        self.assertIsNone(parsed['prices'])

    def test_parse_config_accepts_alias_keys_at_the_platform_level(self):
        parsed = proto.parse_config(READ_ALIAS)
        self.assertTrue(parsed['found'])
        self.assertEqual(parsed['groups']['buy'], '7.7')

    def test_parse_config_never_invents_a_group(self):
        """组名认不出就丢掉这一条，**绝不猜组名**。"""
        parsed = proto.parse_config({'success': True, 'data': {'fansPriceList': [
            {'fansGroup': 'vip', 'priceString': '1.1'},
            {'fansGroup': 'all', 'priceString': ''},
            {'fansGroup': 'old', 'priceString': '8.8'},
        ]}})
        self.assertEqual([entry['fans_group'] for entry in parsed['prices']], ['old'])
        self.assertEqual(parsed['groups'], {'all': None, 'old': '8.8', 'buy': None})

    def test_parse_config_reports_the_business_failure(self):
        parsed = proto.parse_config(READ_BIZ_FAIL)
        self.assertEqual(parsed['problem'], '商品不存在')

    def test_parse_set_reads_the_receipt(self):
        parsed = proto.parse_set(SET_OK)
        self.assertTrue(parsed['saved'])
        self.assertEqual(parsed['platform_message'], '')

    def test_parse_set_no_echo_is_not_a_success(self):
        """信封成功、业务体只剩「回话」⇒ 既不说成功也不说失败。"""
        parsed = proto.parse_set(SET_NO_ECHO)
        self.assertFalse(parsed['saved'])
        self.assertEqual(parsed['platform_message'], '')

    def test_parse_set_keeps_the_platform_words_on_business_failure(self):
        parsed = proto.parse_set(SET_BIZ_FAIL)
        self.assertFalse(parsed['saved'])
        self.assertEqual(parsed['platform_message'], '粉丝价不能高于原价')

    def test_parse_set_detects_the_client_envelope_failure(self):
        parsed = proto.parse_set(NO_PERMISSION)
        self.assertFalse(parsed['saved'])
        self.assertIn('无权限访问', parsed['platform_message'])


# --------------------------------------------------------------------------- 接口层


class FansPricePageTest(unittest.TestCase):
    """接口层基类：临时数据目录 + 自挂路由 + 假协议层。"""

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
        app = FastAPI()
        app.include_router(fans_price_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('fp','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, headers: Optional[Dict[str, str]] = None):
        return self.client.get(path, headers=self.headers if headers is None else headers)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body or {},
                                headers=self.headers if headers is None else headers)

    def read_url(self, account_id: str = ACC, item_id: str = ITEM) -> str:
        return f'{BASE}/{account_id}/{item_id}/fans-price'

    def login_state(self, account_id: str = ACC) -> str:
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (account_id,))
        return row['login_state'] if row else ''

    def operations(self) -> List[Dict[str, Any]]:
        return db.query('SELECT action, success, detail FROM operation_logs '
                        'WHERE account_id = ? ORDER BY id', (ACC,))

    def topics(self, before: int) -> List[str]:
        from backend.app.core import events
        return [event['topic'] for event in events.recent(limit=500)[before:]]

    def event_count(self) -> int:
        from backend.app.core import events
        return len(events.recent(limit=500))


class FansPriceReadTest(FansPricePageTest):
    """只读一条：三态（null / [] / 有数据）必须分开。"""

    def test_read_returns_the_three_groups(self):
        resp = self.get(self.read_url())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['item_id'], ITEM)
        self.assertEqual(data['api'], proto.GET_API)
        self.assertEqual(data['groups'], {'all': '9.9', 'old': '8.8', 'buy': None})
        self.assertEqual(data['prices'], [{'fans_group': 'all', 'price_string': '9.9'},
                                          {'fans_group': 'old', 'price_string': '8.8'}])
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        call = self.fake.call_of(proto.GET_API)
        self.assertEqual(call['data'], {'itemId': ITEM})
        self.assertEqual(call['v'], '1.0')

    def test_read_does_not_leak_the_platform_key_names(self):
        """`price_key` 只留给排查，不进给前端的契约。"""
        for entry in self.get(self.read_url()).json()['data']['prices']:
            self.assertEqual(sorted(entry), ['fans_group', 'price_string'])

    def test_empty_list_is_not_the_same_as_missing(self):
        self.fake.payloads[proto.GET_API] = READ_EMPTY
        data = self.get(self.read_url()).json()['data']
        self.assertEqual(data['prices'], [])
        self.assertEqual(data['groups'], {'all': None, 'old': None, 'buy': None})
        self.assertEqual(data['warnings'], [])

    def test_missing_config_is_null_with_warning(self):
        """平台没给配置键 ⇒ `null` + warning（补成 `[]` 就是假数据）。"""
        self.fake.payloads[proto.GET_API] = READ_NO_KEY
        data = self.get(self.read_url()).json()['data']
        self.assertIsNone(data['prices'])
        self.assertEqual([w['code'] for w in data['warnings']], ['fans_price_config_missing'])
        self.assertEqual(data['groups'], {'all': None, 'old': None, 'buy': None})

    def test_blank_item_id_is_400_with_zero_platform_calls(self):
        resp = self.get(f'{BASE}/{ACC}/%20/fans-price')
        self.assertIn(resp.status_code, (400, 404, 405), resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_platform_refusal_is_502_and_does_not_touch_login_state(self):
        self.fake.payloads[proto.GET_API] = NO_PERMISSION
        resp = self.get(self.read_url())
        self.assertEqual(resp.status_code, 502, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('无权限访问', body['message'])
        self.assertEqual(body['platform_errors']['api'], proto.GET_API)
        self.assertEqual(self.login_state(), 'active')

    def test_business_failure_is_502_with_the_platform_words(self):
        self.fake.payloads[proto.GET_API] = READ_BIZ_FAIL
        resp = self.get(self.read_url())
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('商品不存在', resp.json()['message'])

    def test_session_expiry_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.GET_API] = SessionExpiredError('登录态已过期')
        resp = self.get(self.read_url())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_unknown_account_is_404_with_zero_platform_calls(self):
        resp = self.get(self.read_url(account_id='nope'))
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_no_cookie_is_409_with_zero_platform_calls(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.get(self.read_url())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_missing_client_is_503(self):
        install_fake_protocol(self.fake, factory=lambda account_id: (None, None))
        resp = self.get(self.read_url())
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_requires_authentication(self):
        resp = self.get(self.read_url(), headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


class FansPriceConfirmGateTest(FansPricePageTest):
    """写闸门：未确认时**一个平台请求都不许发**，且 `'false'` 不算确认。"""

    def test_write_needs_confirmation(self):
        resp = self.post(self.read_url(), {'groups': {'all': '9.9'}})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_false_like_values_are_not_confirmation(self):
        for value in FALSE_LIKE:
            resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': value})
            self.assertEqual(resp.status_code, 400, f'confirm={value!r}: {resp.text}')
            self.assertEqual(resp.json()['code'], 'confirmation_required', f'confirm={value!r}')
        self.assertEqual(self.fake.calls, [])

    def test_true_like_values_do_pass_the_gate(self):
        """服务层用 `_truthy` 认 `True` / `'true'` / `1`（AI 与 MCP 客户端可能传字符串）。"""
        for value in (True, 'true', ' TRUE ', 1):
            self.fake.calls.clear()
            resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': value})
            self.assertEqual(resp.status_code, 200, f'confirm={value!r}: {resp.text}')
            self.assertEqual(len(self.fake.calls), 1, f'confirm={value!r}')

    def test_confirmation_happens_before_field_validation(self):
        """未确认时连「价格非法」都不该报 —— 否则会误导用户「填对了就能发」。"""
        resp = self.post(self.read_url(), {'groups': {'all': '0'}, 'confirm': 'false'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_confirmation_happens_before_the_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': 'false'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')


class FansPriceValidationTest(FansPricePageTest):
    """字段校验在建客户端**之前**：掉线时也该先说「价格非法」。"""

    def test_field_validation_runs_before_the_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(self.read_url(), {'groups': {'all': '0'}, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_all_groups_blank_is_400(self):
        """三组全空 = 会把粉丝价全清掉，挡住。"""
        resp = self.post(self.read_url(),
                         {'groups': {'all': '', 'old': None, 'buy': '  '}, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertIn('至少要填一组', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_no_groups_at_all_is_400(self):
        resp = self.post(self.read_url(), {'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('至少要填一组', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_unknown_group_is_400(self):
        resp = self.post(self.read_url(), {'groups': {'vip': '9.9'}, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('不认识的粉丝组', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_zero_price_is_400_with_the_group_label(self):
        resp = self.post(self.read_url(), {'groups': {'old': '0'}, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['message'], '老粉：粉丝价必须大于0，请修改')
        self.assertEqual(self.fake.calls, [])

    def test_three_decimals_is_400(self):
        resp = self.post(self.read_url(), {'groups': {'buy': '7.777'}, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('最多为2位小数', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_scientific_notation_is_400(self):
        resp = self.post(self.read_url(), {'groups': {'all': '1e3'}, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('必须是数字', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_blank_item_id_is_400_with_zero_platform_calls(self):
        resp = self.post(f'{BASE}/{ACC}/%20/fans-price', {'groups': {'all': '9.9'},
                                                          'confirm': True})
        self.assertIn(resp.status_code, (400, 404, 405), resp.text)
        self.assertEqual(self.fake.calls, [])


class FansPriceWriteHappyPathTest(FansPricePageTest):
    """写成功：请求形状、整份替换语义、审计、事件。"""

    def test_write_posts_the_official_shape(self):
        before = self.event_count()
        data = self.post(self.read_url(),
                         {'groups': {'all': '9.9', 'old': ' 8.8 ', 'buy': ''},
                          'confirm': True}).json()['data']
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'fans_price_set')
        self.assertEqual(data['api'], proto.SET_API)
        self.assertEqual(data['item_id'], ITEM)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['submitted'], [{'fans_group': 'all', 'price_string': '9.9'},
                                             {'fans_group': 'old', 'price_string': '8.8'}])
        call = self.fake.call_of(proto.SET_API)
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['data'], {
            'itemId': ITEM,
            'fansPriceConfig': {
                'itemId': ITEM,
                'fansPriceList': [{'fansGroup': 'all', 'priceString': '9.9'},
                                  {'fansGroup': 'old', 'priceString': '8.8'}],
            },
        })
        self.assertIn('items.changed', self.topics(before))

    def test_write_is_logged(self):
        self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': True})
        rows = self.operations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], 'fans_price_set')
        self.assertEqual(rows[0]['success'], 1)
        self.assertIn('9.9', rows[0]['detail'])

    def test_write_accepts_the_snake_case_list(self):
        data = self.post(self.read_url(),
                         {'prices': [{'fans_group': 'buy', 'price_string': '7.7'}],
                          'confirm': True}).json()['data']
        self.assertTrue(data['saved'])
        self.assertEqual(data['submitted'], [{'fans_group': 'buy', 'price_string': '7.7'}])

    def test_write_accepts_the_official_camel_case_list(self):
        data = self.post(self.read_url(),
                         {'prices': [{'fansGroup': 'all', 'priceString': '9.9'}],
                          'confirm': True}).json()['data']
        self.assertEqual(data['submitted'], [{'fans_group': 'all', 'price_string': '9.9'}])

    def test_prices_win_over_groups(self):
        """两个都给时以 `prices` 为准（形状更贴近平台）。"""
        data = self.post(self.read_url(),
                         {'groups': {'all': '1.1'},
                          'prices': [{'fans_group': 'old', 'price_string': '8.8'}],
                          'confirm': True}).json()['data']
        self.assertEqual(data['submitted'], [{'fans_group': 'old', 'price_string': '8.8'}])

    def test_one_platform_call_per_write(self):
        self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': True})
        self.assertEqual(self.fake.apis(), [proto.SET_API])


class FansPriceWriteFailureTest(FansPricePageTest):
    """写失败/没回执：不谎报成功，也不谎报失败。"""

    def test_envelope_refusal_is_502_with_the_platform_words(self):
        self.fake.payloads[proto.SET_API] = NO_PERMISSION
        resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.json()['code'], 'protocol_error')
        self.assertIn('无权限访问', resp.json()['message'])
        self.assertEqual(self.login_state(), 'active')
        self.assertEqual(self.operations(), [])

    def test_business_refusal_is_502_with_the_platform_words(self):
        self.fake.payloads[proto.SET_API] = SET_BIZ_FAIL
        resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('粉丝价不能高于原价', resp.json()['message'])
        self.assertEqual(self.operations(), [])

    def test_no_echo_is_saved_false_with_a_warning(self):
        """平台既没报错也没回执：如实说「不知道」，且**不留审计、不发事件**。"""
        before = self.event_count()
        self.fake.payloads[proto.SET_API] = SET_NO_ECHO
        data = self.post(self.read_url(), {'groups': {'all': '9.9'},
                                           'confirm': True}).json()['data']
        self.assertFalse(data['saved'])
        self.assertEqual([w['code'] for w in data['warnings']], ['fans_price_no_echo'])
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.topics(before), [])

    def test_session_expiry_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.SET_API] = SessionExpiredError('登录态已过期')
        resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')
        self.assertEqual(self.operations(), [])

    def test_unknown_account_is_404_with_zero_platform_calls(self):
        resp = self.post(self.read_url(account_id='nope'), {'groups': {'all': '9.9'},
                                                            'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_no_cookie_is_409_with_zero_platform_calls(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(self.read_url(), {'groups': {'all': '9.9'}, 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])


class FansPriceRoutesTest(FansPricePageTest):
    """路由面：冻结契约里的两条（同一条 path，GET 读 + POST 写）。"""

    def openapi_paths(self) -> Dict[str, Any]:
        """取真实 app 的 openapi 路径表。

        **必须先摘掉假协议层**：`create_app()` 会 import 二十几个服务模块，其中
        `services/finance.py` 在**导入时**就读 `xianyu.client` 的常量；假模块是空壳，
        会炸成 `AttributeError: ... has no attribute 'BILL_CODE_INCOME'`。
        另外 `_IncludedRouter` 不展开进 `app.routes`，只有 `openapi()` 才看得全
        （用 `app.routes` 判断会得出「连 quick-edit 都不存在」的假结论）。
        """
        remove_fake_protocol()
        try:
            from backend.app.main import create_app
            return create_app().openapi()['paths']
        finally:
            install_fake_protocol(self.fake)

    def registered(self) -> set:
        found = set()
        for path, methods in self.openapi_paths().items():
            for method in methods:
                found.add((method.upper(), path))
        return found

    def test_the_two_routes_are_registered(self):
        registered = self.registered()
        for method, path in EXPECTED_ROUTES:
            self.assertIn((method, path), registered)

    def test_openapi_declares_exactly_these_two(self):
        methods = self.openapi_paths()[PATH]
        self.assertEqual(sorted(methods), ['get', 'post'])

    def test_other_methods_are_405(self):
        resp = self.client.put(self.read_url(), json={}, headers=self.headers)
        self.assertEqual(resp.status_code, 405, resp.text)
        self.assertEqual(self.fake.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
