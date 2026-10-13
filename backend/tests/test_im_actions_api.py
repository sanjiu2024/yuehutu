# -*- coding: utf-8 -*-
"""IM 会话操作（拉黑状态查询 / 拉黑买家 / 解除拉黑 / 发商品卡片）的测试。

**为什么单开一组**：这一族是**会话级写操作**，最容易犯的错是：

1. 没确认就发写请求（红线：未确认时**一个平台请求都不许发**）；
2. 确认判断用 `bool(...)` —— `bool('false')` 是 `True`，把用户明确的拒绝当成同意；
3. **版本号抄错**：这一族里 `blacklist.add` 是 **2.0**，其余三条都是 1.0（bundle 逐字）；
4. 把平台没返回的 `isInBlack` 补成 `False` —— 「平台没说」和「没拉黑」是两件事；
5. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现会话 id 是空的；
6. 把登录态判错：平台「无权限」不等于掉线，不该让用户白扫一次码；
7. 替平台给 `sessionId` 加/去 `@goofish` 后缀（我们库里存的是去掉后缀的那个，原样透传）。

**一次真机请求都没发过**（第 40 批红线是零平台写请求）。这里用假协议层钉住「接口名、版本号、
入参形状与官方 bundle 逐字一致」（偏移见 `backend/app/xianyu/im_actions.py` 的常量注释与
`WRITE_APIS[...]['evidence']`），平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_im_actions_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
"""
from __future__ import annotations

import json
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

from backend.app.api import im_actions as im_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import im_actions  # noqa: E402
from backend.app.xianyu import im_actions as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'im_acc'
BASE = '/api/im'
SESSION = 'cid-1'

#: 拉黑状态：已拉黑（官方读 `data.isInBlack` / `data.showBlackList`）。
STATUS_IN = {
    'success': True,
    'data': {'isInBlack': True, 'showBlackList': True},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 拉黑状态：未拉黑。
STATUS_OUT = {
    'success': True,
    'data': {'isInBlack': False, 'showBlackList': True},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台回了信封但业务体里**没有** `isInBlack` —— 不等于「没拉黑」。
STATUS_MISSING_KEY = {
    'success': True, 'data': {'showBlackList': True},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台业务体是空的 —— 更不等于「没拉黑」。
STATUS_EMPTY = {
    'success': True, 'data': {},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 写成功（官方只看信封就 toast）。
ACTION_OK = {
    'success': True, 'data': {'code': 'success'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 业务层失败（信封 success=True 但 code 不是 success）。
ACTION_BIZ_FAIL = {
    'success': True, 'data': {'code': 'FAIL_BIZ_BLACKLIST', 'message': '不能拉黑自己'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}
#: 转交 / 结束会话：官方只看信封就提示成功（业务体里没有 `success` 布尔）。
SESSION_OK = {
    'success': True, 'data': {'code': 'success'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 重新发起聊天：官方带真回执 `data.success` 与平台回显的 sessionId。
REJOIN_OK = {
    'success': True, 'data': {'success': True, 'sessionId': 'cid-9'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 重新发起但平台自己说没成功（信封 success=True 也不行）。
REJOIN_SAYS_FALSE = {
    'success': True, 'data': {'success': False, 'message': '会话已结束'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}

#: 三条写路由 + 各自的接口名（confirm 由用例单独塞）。
WRITE_ROUTES: List[tuple] = [
    ('POST', f'{BASE}/{ACC}/blacklist', {'session_id': SESSION}, proto.BLACKLIST_ADD_API),
    ('POST', f'{BASE}/{ACC}/blacklist/remove', {'session_id': SESSION},
     proto.BLACKLIST_REMOVE_API),
    ('POST', f'{BASE}/{ACC}/cards', {'session_id': SESSION, 'item_id': '9001'},
     proto.CARD_SEND_API),
]

#: 会话接管的三条写路由（第 43 批）：转交 / 结束 / 重新发起。
SESSION_WRITE_ROUTES: List[tuple] = [
    ('POST', f'{BASE}/{ACC}/sessions/transfer',
     {'session_id': SESSION, 'target_uid': '8888'}, proto.SESSION_DELIVER_API),
    ('POST', f'{BASE}/{ACC}/sessions/leave', {'session_id': SESSION},
     proto.SESSION_LEAVE_API),
    ('POST', f'{BASE}/{ACC}/sessions/rejoin', {'session_id': SESSION},
     proto.SESSION_REJOIN_API),
]

FALSE_LIKE = ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2, [], {})


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeImClient:
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


def install_fake_protocol(client: FakeImClient, *, with_factory: bool = True) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeImClient:
        client.account_id = account_id
        return client

    if with_factory:
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


def full_client(**overrides: Any) -> FakeImClient:
    payloads = {
        proto.BLACKLIST_QUERY_API: STATUS_OUT,
        proto.BLACKLIST_ADD_API: ACTION_OK,
        proto.BLACKLIST_REMOVE_API: ACTION_OK,
        proto.CARD_SEND_API: ACTION_OK,
        proto.SESSION_DELIVER_API: SESSION_OK,
        proto.SESSION_LEAVE_API: SESSION_OK,
        proto.SESSION_REJOIN_API: REJOIN_OK,
    }
    payloads.update(overrides)
    return FakeImClient(payloads)


# --------------------------------------------------------------------------- 协议层


class ImActionsProtocolTest(unittest.TestCase):
    """纯函数：接口名、**版本号**、入参形状、确认闸门、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.BLACKLIST_QUERY_API,
                         'mtop.taobao.idlemessage.pc.blacklist.query')
        self.assertEqual(proto.BLACKLIST_ADD_API,
                         'mtop.taobao.idlemessage.pc.blacklist.add')
        self.assertEqual(proto.BLACKLIST_REMOVE_API,
                         'mtop.taobao.idlemessage.pc.blacklist.remove')
        self.assertEqual(proto.CARD_SEND_API,
                         'mtop.taobao.idlemessage.message.card.send')

    def test_add_is_version_2_and_the_rest_are_1(self):
        """这一族最容易被抄错的地方：只有 add 是 2.0。"""
        self.assertEqual(proto.WRITE_VERSIONS[proto.BLACKLIST_ADD_API], '2.0')
        self.assertEqual(proto.WRITE_VERSIONS[proto.BLACKLIST_REMOVE_API], '1.0')
        self.assertEqual(proto.WRITE_VERSIONS[proto.CARD_SEND_API], '1.0')
        self.assertEqual(proto.READ_VERSIONS[proto.BLACKLIST_QUERY_API], '1.0')

    def test_whitelist_has_three_writes_and_one_read(self):
        self.assertEqual(proto.READ_APIS, (proto.BLACKLIST_QUERY_API,))
        self.assertEqual(proto.ACTIONS, ('blacklist_add', 'blacklist_remove', 'card_send',
                                         'session_deliver', 'session_leave',
                                         'session_rejoin'))
        for action, spec in proto.WRITE_APIS.items():
            self.assertIn(spec['api'], proto.ALL_APIS, action)
            self.assertTrue(spec['evidence'], action)
            self.assertTrue(spec['data'], action)
            self.assertIn(spec['version'], ('1.0', '2.0'), action)

    def test_blacklist_family_stays_three_plus_one(self):
        """第 43 批加的会话接管族不许混进拉黑族的口径。"""
        self.assertEqual(proto.BLACKLIST_ADD_API, 'mtop.taobao.idlemessage.pc.blacklist.add')
        self.assertEqual(proto.WRITE_VERSIONS[proto.BLACKLIST_ADD_API], '2.0')
        self.assertNotIn(proto.SESSION_DELIVER_API, proto.READ_APIS)

    def test_assert_write_rejects_read_api(self):
        for api in proto.WRITE_API_SET:
            self.assertEqual(proto.assert_write(api), api)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.BLACKLIST_QUERY_API)

    def test_assert_read_rejects_write_apis(self):
        self.assertEqual(proto.assert_read(proto.BLACKLIST_QUERY_API),
                         proto.BLACKLIST_QUERY_API)
        for api in proto.WRITE_API_SET:
            with self.assertRaises(proto.UnknownApi):
                proto.assert_read(api)

    def test_query_request_payload_and_version(self):
        self.assertEqual(proto.blacklist_query_request(' cid-1 '),
                         (proto.BLACKLIST_QUERY_API, {'sessionId': 'cid-1'}, '1.0'))

    def test_add_request_payload_and_version(self):
        self.assertEqual(proto.blacklist_add_request(SESSION, confirm=True),
                         (proto.BLACKLIST_ADD_API, {'sessionId': SESSION}, '2.0'))

    def test_remove_request_payload_and_version(self):
        self.assertEqual(proto.blacklist_remove_request(SESSION, confirm=True),
                         (proto.BLACKLIST_REMOVE_API, {'sessionId': SESSION}, '1.0'))

    def test_card_request_payload_and_version(self):
        self.assertEqual(proto.card_send_request(SESSION, ' 9001 ', confirm=True),
                         (proto.CARD_SEND_API, {'sessionId': SESSION, 'itemId': '9001'}, '1.0'))

    def test_session_id_is_passed_verbatim(self):
        """**不替平台加/去 `@goofish` 后缀**（库里存的是去掉后缀的那个）。"""
        _, data, _ = proto.blacklist_query_request('cid-1@goofish')
        self.assertEqual(data['sessionId'], 'cid-1@goofish')
        _, data2, _ = proto.blacklist_add_request('cid-1', confirm=True)
        self.assertEqual(data2['sessionId'], 'cid-1')

    def test_blank_session_id_is_rejected(self):
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                proto.blacklist_query_request(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                proto.blacklist_add_request(bad, confirm=True)

    def test_session_id_length_guard(self):
        with self.assertRaises(ValueError):
            proto.blacklist_query_request('x' * (proto.SESSION_ID_MAX + 1))

    def test_blank_item_id_is_rejected(self):
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                proto.card_send_request(SESSION, bad, confirm=True)

    def test_confirm_gate_is_identity_not_truthiness(self):
        """`bool('false')` 是 True ⇒ 只认字面量 True。"""
        for bad in (None, False, 0, '', 'false', 'no', 'yes', 2, 'True', 'true', 1):
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(bad)):
                proto.blacklist_add_request(SESSION, confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(bad)):
                proto.blacklist_remove_request(SESSION, confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(bad)):
                proto.card_send_request(SESSION, '9001', confirm=bad)
        self.assertIsNotNone(proto.blacklist_add_request(SESSION, confirm=True))

    def test_request_of_dispatches_by_action(self):
        self.assertEqual(proto.request_of('blacklist_add', session_id=SESSION, confirm=True)[0],
                         proto.BLACKLIST_ADD_API)
        self.assertEqual(proto.request_of('blacklist_remove', session_id=SESSION,
                                        confirm=True)[0], proto.BLACKLIST_REMOVE_API)
        self.assertEqual(proto.request_of('card_send', session_id=SESSION, item_id='9',
                                         confirm=True)[0], proto.CARD_SEND_API)
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('nope', session_id=SESSION, confirm=True)

    def test_parse_blacklist_reads_official_keys(self):
        parsed = proto.parse_blacklist(STATUS_IN)
        self.assertIs(parsed['is_in_black'], True)
        self.assertIs(parsed['show_black_list'], True)
        self.assertEqual(parsed['is_in_black_key'], 'isInBlack')
        self.assertEqual(proto.parse_blacklist(STATUS_OUT)['is_in_black'], False)

    def test_parse_blacklist_missing_key_is_none_not_false(self):
        """平台没说 ≠ 没拉黑。"""
        parsed = proto.parse_blacklist(STATUS_MISSING_KEY)
        self.assertIsNone(parsed['is_in_black'])
        self.assertIs(parsed['show_black_list'], True)
        empty = proto.parse_blacklist(STATUS_EMPTY)
        self.assertIsNone(empty['is_in_black'])
        self.assertIsNone(empty['show_black_list'])
        self.assertFalse(empty['found'])

    def test_parse_blacklist_accepts_snake_case_aliases(self):
        parsed = proto.parse_blacklist({'data': {'is_in_black': 'true', 'show_black_list': '0'}})
        self.assertIs(parsed['is_in_black'], True)
        self.assertIs(parsed['show_black_list'], False)

    def test_parse_blacklist_unrecognized_value_is_none(self):
        parsed = proto.parse_blacklist({'data': {'isInBlack': 'maybe'}})
        self.assertIsNone(parsed['is_in_black'])

    def test_parse_action_success_and_failures(self):
        self.assertTrue(proto.parse_action(ACTION_OK)['saved'])
        self.assertEqual(proto.parse_action(ACTION_OK)['platform_message'], '')
        failed = proto.parse_action(NO_PERMISSION)
        self.assertFalse(failed['saved'])
        self.assertIn('无权限访问', failed['platform_message'])
        biz = proto.parse_action(ACTION_BIZ_FAIL)
        self.assertFalse(biz['saved'])
        self.assertEqual(biz['platform_message'], '不能拉黑自己')

    def test_parse_action_on_garbage_is_not_a_crash(self):
        self.assertTrue(proto.parse_action(None)['saved'])
        self.assertTrue(proto.parse_action('not-a-dict')['saved'])


# --------------------------------------------------------------------------- 页面/接口


class ImActionsPageTest(unittest.TestCase):
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
        app.include_router(im_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('im','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, headers: Optional[Dict[str, str]] = None,
            params: Optional[Dict[str, Any]] = None):
        return self.client.get(path, params=params,
                               headers=self.headers if headers is None else headers)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body or {},
                                headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()['data']

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


class ImBlacklistReadTest(ImActionsPageTest):
    """只读：拉黑状态、缺键、平台拒绝、凭据与鉴权。"""

    def test_status_returns_the_flags(self):
        data = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION}).json()['data']
        self.assertIs(data['is_in_black'], False)
        self.assertIs(data['show_black_list'], True)
        self.assertEqual(data['session_id'], SESSION)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        call = self.fake.call_of(proto.BLACKLIST_QUERY_API)
        self.assertEqual(call['data'], {'sessionId': SESSION})
        self.assertEqual(call['v'], '1.0')

    def test_status_in_black_is_true(self):
        self.fake.payloads[proto.BLACKLIST_QUERY_API] = STATUS_IN
        data = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION}).json()['data']
        self.assertIs(data['is_in_black'], True)

    def test_missing_key_is_null_with_warning(self):
        self.fake.payloads[proto.BLACKLIST_QUERY_API] = STATUS_MISSING_KEY
        data = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION}).json()['data']
        self.assertIsNone(data['is_in_black'])
        self.assertEqual([w['code'] for w in data['warnings']], ['is_in_black_missing'])

    def test_empty_body_is_null_with_a_stronger_warning(self):
        self.fake.payloads[proto.BLACKLIST_QUERY_API] = STATUS_EMPTY
        data = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION}).json()['data']
        self.assertIsNone(data['is_in_black'])
        self.assertIsNone(data['show_black_list'])
        self.assertEqual([w['code'] for w in data['warnings']], ['blacklist_status_missing'])

    def test_blank_session_id_is_400_without_calling_platform(self):
        for params in ({}, {'session_id': '   '}):
            resp = self.get(f'{BASE}/{ACC}/blacklist', params=params)
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_platform_refusal_is_502_and_keeps_login_state(self):
        self.fake.payloads[proto.BLACKLIST_QUERY_API] = NO_PERMISSION
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.text)
        self.assertEqual(self.login_state(), 'active')  # 无权限 ≠ 掉线

    def test_missing_cookie_is_409_and_sends_nothing(self):
        db.execute('DELETE FROM account_credentials')
        remove_fake_protocol()
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_and_sends_nothing(self):
        resp = self.get(f'{BASE}/nope_acc/blacklist', params={'session_id': SESSION})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_without_token_is_401(self):
        self.assertEqual(self.get(f'{BASE}/{ACC}/blacklist',
                                  headers={}, params={'session_id': SESSION}).status_code, 401)

    def test_protocol_without_mtop_is_503(self):
        module = types.ModuleType('backend.app.xianyu.client')
        module.XianyuClient = type('Bare', (), {})
        sys.modules['backend.app.xianyu.client'] = module
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'session_id': SESSION})
        self.assertEqual(resp.status_code, 503, resp.text)

    def test_read_path_accepts_both_get_and_post(self):
        """`/blacklist` 上 GET 是读、POST 是写 —— 方法不同、路径相同（见 api/im_actions.py）。"""
        resp = self.post(f'{BASE}/{ACC}/blacklist', {'session_id': SESSION})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')

    def test_write_only_routes_reject_get(self):
        self.assertEqual(self.get(f'{BASE}/{ACC}/blacklist/remove').status_code, 405)
        self.assertEqual(self.get(f'{BASE}/{ACC}/cards').status_code, 405)


class ImConfirmGateTest(ImActionsPageTest):
    """确认闸门：未确认时一个平台请求都不许发（三条写路由逐条钉）。"""

    def test_without_confirm_is_400_and_sends_nothing(self):
        for _method, route, body, _api in WRITE_ROUTES:
            resp = self.post(route, body)
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()['code'], 'confirmation_required')
            self.assertEqual(self.fake.calls, [], route)

    def test_false_like_values_are_never_a_nod(self):
        for _method, route, body, _api in WRITE_ROUTES:
            for bad in FALSE_LIKE:
                resp = self.post(route, dict(body, confirm=bad))
                self.assertEqual(resp.status_code, 400, f'{route} {bad!r} -> {resp.text}')
                self.assertEqual(resp.json()['code'], 'confirmation_required', repr(bad))
                self.assertEqual(self.fake.calls, [], f'{route} {bad!r}')

    def test_string_true_is_normalized_and_sends(self):
        """服务层认 `'true'`；传给协议层前归一成 True（否则会出现自相矛盾的 400）。"""
        data = self.ok_data(f'{BASE}/{ACC}/blacklist', {'session_id': SESSION, 'confirm': 'true'})
        self.assertTrue(data['saved'])
        self.assertEqual(self.fake.apis(), [proto.BLACKLIST_ADD_API])

    def test_numeric_one_is_a_nod(self):
        data = self.ok_data(f'{BASE}/{ACC}/blacklist/remove',
                            {'session_id': SESSION, 'confirm': 1})
        self.assertTrue(data['deleted'])
        self.assertEqual(self.fake.apis(), [proto.BLACKLIST_REMOVE_API])

    def test_confirm_is_judged_before_field_validation(self):
        """确认在最前：连字段错都不该报，否则误导「填对了就能发」。"""
        resp = self.post(f'{BASE}/{ACC}/cards', {'session_id': '', 'item_id': '', 'confirm': None})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])


class ImValidationTest(ImActionsPageTest):
    """字段校验：排在凭据检查之前（本地、免费）。"""

    def test_blank_session_id_is_400_without_calling_platform(self):
        for _method, route, body, _api in WRITE_ROUTES:
            resp = self.post(route, dict(body, session_id='   ', confirm=True))
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()['code'], 'invalid_fields')
            self.assertEqual(self.fake.calls, [], route)

    def test_blank_item_id_is_400_without_calling_platform(self):
        resp = self.post(f'{BASE}/{ACC}/cards',
                         {'session_id': SESSION, 'item_id': '', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_field_validation_runs_before_the_credential_check(self):
        """掉线 + 字段空 ⇒ 说字段，不说「去重新登录」。"""
        db.execute('DELETE FROM account_credentials')
        remove_fake_protocol()
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(f'{BASE}/{ACC}/blacklist', {'session_id': '', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_after_confirm(self):
        resp = self.post(f'{BASE}/nope_acc/blacklist', {'session_id': SESSION, 'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])


class ImWriteHappyPathTest(ImActionsPageTest):
    """写成功：请求形状、版本号、审计日志、实时刷新事件。"""

    def test_blacklist_add(self):
        before = len(self.topics(0))
        data = self.ok_data(f'{BASE}/{ACC}/blacklist', {'session_id': SESSION, 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'blacklist_add')
        self.assertEqual(data['session_id'], SESSION)
        self.assertEqual(data['submitted'], {'sessionId': SESSION})
        call = self.fake.call_of(proto.BLACKLIST_ADD_API)
        self.assertEqual(call['data'], {'sessionId': SESSION})
        self.assertEqual(call['v'], '2.0')
        self.assertEqual([row['action'] for row in self.operations()], ['im_blacklist_add'])
        self.assertIn('chats.changed', self.topics(before))

    def test_blacklist_remove(self):
        before = len(self.topics(0))
        data = self.ok_data(f'{BASE}/{ACC}/blacklist/remove',
                            {'session_id': SESSION, 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertTrue(data['deleted'])
        self.assertEqual(data['action'], 'blacklist_remove')
        self.assertEqual(self.fake.call_of(proto.BLACKLIST_REMOVE_API)['v'], '1.0')
        self.assertEqual([row['action'] for row in self.operations()], ['im_blacklist_remove'])
        self.assertIn('chats.changed', self.topics(before))

    def test_card_send(self):
        data = self.ok_data(f'{BASE}/{ACC}/cards',
                            {'session_id': SESSION, 'item_id': '9001', 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'card_send')
        self.assertEqual(data['item_id'], '9001')
        self.assertEqual(data['submitted'], {'sessionId': SESSION, 'itemId': '9001'})
        self.assertEqual(self.fake.call_of(proto.CARD_SEND_API)['v'], '1.0')
        self.assertEqual([row['action'] for row in self.operations()], ['im_card_send'])

    def test_only_one_platform_call_per_action(self):
        for _method, route, body, api in WRITE_ROUTES:
            self.fake.calls.clear()
            self.ok_data(route, dict(body, confirm=True))
            self.assertEqual(self.fake.apis(), [api], route)

    def test_log_detail_carries_the_session_id(self):
        self.ok_data(f'{BASE}/{ACC}/blacklist', {'session_id': SESSION, 'confirm': True})
        row = self.operations()[0]
        self.assertIn(SESSION, str(row['detail']))


class ImWriteFailureTest(ImActionsPageTest):
    """写失败：平台拒绝、业务层拒绝、登录态过期 —— 都不许写审计日志/发事件。"""

    def test_platform_refusal_is_502_without_log_or_event(self):
        self.fake.payloads[proto.BLACKLIST_ADD_API] = NO_PERMISSION
        before = len(self.topics(0))
        resp = self.post(f'{BASE}/{ACC}/blacklist', {'session_id': SESSION, 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.text)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.topics(before), [])
        self.assertEqual(self.login_state(), 'active')

    def test_business_level_refusal_is_502(self):
        self.fake.payloads[proto.CARD_SEND_API] = ACTION_BIZ_FAIL
        resp = self.post(f'{BASE}/{ACC}/cards',
                         {'session_id': SESSION, 'item_id': '9001', 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('不能拉黑自己', resp.text)
        self.assertEqual(self.operations(), [])

    def test_session_expiry_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.BLACKLIST_REMOVE_API] = SessionExpiredError('过期了')
        resp = self.post(f'{BASE}/{ACC}/blacklist/remove',
                         {'session_id': SESSION, 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')
        self.assertEqual(self.operations(), [])

    def test_unexpected_exception_is_not_a_crash(self):
        self.fake.errors[proto.CARD_SEND_API] = RuntimeError('boom')
        resp = self.post(f'{BASE}/{ACC}/cards',
                         {'session_id': SESSION, 'item_id': '9001', 'confirm': True})
        self.assertIn(resp.status_code, (500, 502), resp.text)
        self.assertEqual(self.operations(), [])


class ImRoutesTest(unittest.TestCase):
    """路由登记：前缀、方法、以及 `main.py` 里挂上了这个模块。"""

    def test_router_prefix_and_paths(self):
        self.assertEqual(im_api.router.prefix, '/api/im')
        pairs = {(route.path, method) for route in im_api.router.routes
                 for method in route.methods}
        self.assertIn(('/api/im/{account_id}/blacklist', 'GET'), pairs)
        self.assertIn(('/api/im/{account_id}/blacklist', 'POST'), pairs)
        self.assertIn(('/api/im/{account_id}/blacklist/remove', 'POST'), pairs)
        self.assertIn(('/api/im/{account_id}/cards', 'POST'), pairs)
        self.assertIn(('/api/im/{account_id}/sessions/transfer', 'POST'), pairs)
        self.assertIn(('/api/im/{account_id}/sessions/leave', 'POST'), pairs)
        self.assertIn(('/api/im/{account_id}/sessions/rejoin', 'POST'), pairs)
        self.assertEqual(len(im_api.router.routes), 7)

    def test_main_registers_the_module(self):
        import backend.app.main as app_main
        self.assertIn('im_actions', app_main.ROUTE_MODULES)

    def test_only_one_read_route(self):
        methods = [route.methods for route in im_api.router.routes]
        get_routes = [m for m in methods if 'GET' in m]
        self.assertEqual(len(get_routes), 1)


class ImConversationIdTest(ImActionsPageTest):
    """会话页手里只有本地会话行 id：允许只传 `conversation_id`，由后端解析出平台 cid。

    **为什么必须有这条路径**：`conversations` 表里**没有 cid 列**（`backend/app/core/db.py:119-131`），
    cid 只落在 `messages.meta` 里（`backend/app/services/chats.py::_platform_cid`）。
    前端拿不到 cid，只能把本地行 id 传上来。
    """

    def setUp(self):
        super().setUp()
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')

    def seed_conversation(self, account_id: str = ACC, peer: str = 'buyer-1',
                          cid: str = '') -> int:
        """插一条会话；`cid` 给了就补一条带 `meta.cid` 的消息。"""
        row_id = int(db.execute(
            'INSERT INTO conversations(account_id, peer_user_id, peer_nickname) '
            'VALUES(?,?,?)', (account_id, peer, '买家')))
        if cid:
            db.execute(
                'INSERT INTO messages(conversation_id, direction, sender, content, meta) '
                'VALUES(?,?,?,?,?)',
                (row_id, 'in', 'buyer', '你好',
                 json.dumps({'cid': cid}, ensure_ascii=False)))
        return row_id

    def test_conversation_id_resolves_the_platform_cid(self):
        cid = 'cid-from-db'
        row_id = self.seed_conversation(cid=cid)
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'conversation_id': str(row_id)})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['session_id'], cid)
        self.assertEqual(self.fake.call_of(proto.BLACKLIST_QUERY_API)['data']['sessionId'], cid)

    def test_session_id_wins_when_both_are_given(self):
        row_id = self.seed_conversation(cid='cid-from-db')
        resp = self.get(f'{BASE}/{ACC}/blacklist',
                        params={'session_id': 'cid-direct', 'conversation_id': str(row_id)})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['session_id'], 'cid-direct')

    def test_write_with_conversation_id_uses_the_cid(self):
        row_id = self.seed_conversation(cid='cid-write')
        data = self.ok_data(f'{BASE}/{ACC}/blacklist',
                            {'conversation_id': row_id, 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertEqual(self.fake.call_of(proto.BLACKLIST_ADD_API)['data']['sessionId'],
                         'cid-write')

    def test_card_send_with_conversation_id(self):
        row_id = self.seed_conversation(cid='cid-card')
        data = self.ok_data(f'{BASE}/{ACC}/cards',
                            {'conversation_id': row_id, 'item_id': '9001', 'confirm': True})
        self.assertTrue(data['saved'])
        call = self.fake.call_of(proto.CARD_SEND_API)
        self.assertEqual(call['data']['sessionId'], 'cid-card')
        self.assertEqual(call['data']['itemId'], '9001')

    def test_conversation_without_cid_is_400_and_calls_nothing(self):
        row_id = self.seed_conversation()
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'conversation_id': str(row_id)})
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], im_actions.MISSING_CID_CODE)
        self.assertIn('同步历史会话', body['message'])
        self.assertEqual(self.fake.calls, [])

    def test_write_without_cid_is_400_and_calls_nothing(self):
        row_id = self.seed_conversation()
        resp = self.post(f'{BASE}/{ACC}/blacklist',
                         {'conversation_id': row_id, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], im_actions.MISSING_CID_CODE)
        self.assertEqual(self.fake.calls, [])

    def test_unknown_conversation_is_404_and_calls_nothing(self):
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'conversation_id': '999999'})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_cross_account_conversation_is_404(self):
        seed_account('other_acc')
        row_id = self.seed_conversation(account_id='other_acc', peer='buyer-2', cid='cid-x')
        resp = self.get(f'{BASE}/{ACC}/blacklist', params={'conversation_id': str(row_id)})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_neither_id_is_400(self):
        resp = self.get(f'{BASE}/{ACC}/blacklist')
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('会话 id', body['message'])
        self.assertEqual(self.fake.calls, [])

    def test_write_neither_id_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/blacklist', {'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

class ImSessionProtocolTest(unittest.TestCase):
    """会话接管族协议层：接口名、版本、载荷、严格确认、回执三态。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.SESSION_DELIVER_API,
                         'mtop.taobao.idlemessage.customer.deliver.session')
        self.assertEqual(proto.SESSION_LEAVE_API,
                         'mtop.taobao.idlemessage.customer.leave.session')
        self.assertEqual(proto.SESSION_REJOIN_API,
                         'mtop.taobao.idlemessage.customer.rejoin.session')

    def test_three_actions_are_white_listed_writes(self):
        for action in ('session_deliver', 'session_leave', 'session_rejoin'):
            self.assertIn(action, proto.WRITE_APIS)
            self.assertIn(action, proto.ACTIONS)
        # 只读集合里不许混进这三条
        for api in (proto.SESSION_DELIVER_API, proto.SESSION_LEAVE_API,
                    proto.SESSION_REJOIN_API):
            self.assertNotIn(api, proto.READ_APIS)

    def test_deliver_payload_is_verbatim(self):
        api, data, version = proto.session_deliver_request(SESSION, '8888', confirm=True)
        self.assertEqual(api, proto.SESSION_DELIVER_API)
        self.assertEqual(data, {'sessionId': SESSION, 'targetUid': '8888'})
        self.assertEqual(version, '1.0')

    def test_leave_and_rejoin_carry_only_the_session_id(self):
        for builder in (proto.session_leave_request, proto.session_rejoin_request):
            _api, data, version = builder(SESSION, confirm=True)
            self.assertEqual(data, {'sessionId': SESSION})
            self.assertEqual(version, '1.0')

    def test_confirm_is_identity_not_truthiness(self):
        for bad in ('true', '1', 1, 'yes', False, None):
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.session_leave_request(SESSION, confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.session_deliver_request(SESSION, '8888', confirm=bad)

    def test_blank_target_uid_is_rejected(self):
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError):
                proto.session_deliver_request(SESSION, bad, confirm=True)

    def test_too_long_target_uid_is_rejected(self):
        with self.assertRaises(ValueError):
            proto.session_deliver_request(
                SESSION, 'u' * (proto.TARGET_UID_MAX + 1), confirm=True)

    def test_deliver_without_a_target_is_rejected(self):
        # `target_uid` 是位置参数：漏传就是编程错误（TypeError），不是 400。
        with self.assertRaises(TypeError):
            proto.request_of('session_deliver', confirm=True, session_id=SESSION)

    def test_parse_session_action_reads_the_receipt(self):
        parsed = proto.parse_session_action(REJOIN_OK)
        self.assertTrue(parsed['saved'])
        self.assertIs(parsed['platform_success'], True)
        self.assertEqual(parsed['returned_session_id'], 'cid-9')
        self.assertEqual(parsed['platform_message'], '')

    def test_platform_says_false_wins_over_the_envelope(self):
        parsed = proto.parse_session_action(REJOIN_SAYS_FALSE)
        self.assertFalse(parsed['saved'])
        self.assertIs(parsed['platform_success'], False)
        self.assertIn('会话已结束', parsed['platform_message'])

    def test_missing_receipt_key_is_none_not_false(self):
        parsed = proto.parse_session_action(SESSION_OK)
        self.assertTrue(parsed['saved'])
        self.assertIsNone(parsed['platform_success'])
        self.assertIsNone(parsed['returned_session_id'])

    def test_envelope_failure_is_not_saved(self):
        self.assertFalse(proto.parse_session_action(NO_PERMISSION)['saved'])


class ImSessionWriteTest(ImActionsPageTest):
    """会话接管三条写：载荷、审计、事件、闸门、失败都不留痕。"""

    def setUp(self):
        super().setUp()
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')

    def seed_conversation(self, account_id: str = ACC, peer: str = 'buyer-1',
                          cid: str = '') -> int:
        row_id = int(db.execute(
            'INSERT INTO conversations(account_id, peer_user_id, peer_nickname) '
            'VALUES(?,?,?)', (account_id, peer, '买家')))
        if cid:
            db.execute(
                'INSERT INTO messages(conversation_id, direction, sender, content, meta) '
                'VALUES(?,?,?,?,?)',
                (row_id, 'in', 'buyer', '你好',
                 json.dumps({'cid': cid}, ensure_ascii=False)))
        return row_id

    def test_deliver_happy_path(self):
        before = len(self.topics(0))
        data = self.ok_data(f'{BASE}/{ACC}/sessions/transfer',
                            {'session_id': SESSION, 'target_uid': '8888', 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'session_deliver')
        self.assertEqual(data['session_id'], SESSION)
        self.assertEqual(data['target_uid'], '8888')
        self.assertEqual(data['submitted'], {'sessionId': SESSION, 'targetUid': '8888'})
        self.assertIsNone(data['platform_success'])
        call = self.fake.call_of(proto.SESSION_DELIVER_API)
        self.assertEqual(call['data'], {'sessionId': SESSION, 'targetUid': '8888'})
        self.assertEqual(call['v'], '1.0')
        self.assertEqual([row['action'] for row in self.operations()], ['im_session_deliver'])
        self.assertIn('chats.changed', self.topics(before))

    def test_leave_happy_path(self):
        data = self.ok_data(f'{BASE}/{ACC}/sessions/leave',
                            {'session_id': SESSION, 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'session_leave')
        self.assertEqual(self.fake.call_of(proto.SESSION_LEAVE_API)['v'], '1.0')
        self.assertEqual([row['action'] for row in self.operations()], ['im_session_leave'])

    def test_rejoin_reports_the_platform_receipt(self):
        data = self.ok_data(f'{BASE}/{ACC}/sessions/rejoin',
                            {'session_id': SESSION, 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertTrue(data['rejoined'])
        self.assertIs(data['platform_success'], True)
        self.assertEqual(data['returned_session_id'], 'cid-9')
        self.assertEqual([row['action'] for row in self.operations()], ['im_session_rejoin'])

    def test_false_like_confirms_never_send(self):
        for _method, route, body, _api in SESSION_WRITE_ROUTES:
            for bad in FALSE_LIKE:
                self.fake.calls.clear()
                resp = self.post(route, dict(body, confirm=bad))
                self.assertEqual(resp.status_code, 400, '%s confirm=%r' % (route, bad))
                self.assertEqual(resp.json()['code'], 'confirmation_required')
                self.assertEqual(self.fake.calls, [], route)

    def test_string_true_confirm_sends(self):
        data = self.ok_data(f'{BASE}/{ACC}/sessions/leave',
                            {'session_id': SESSION, 'confirm': 'true'})
        self.assertTrue(data['saved'])

    def test_blank_target_uid_is_400_and_sends_nothing(self):
        resp = self.post(f'{BASE}/{ACC}/sessions/transfer',
                         {'session_id': SESSION, 'target_uid': '  ', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertIn('转交目标客服', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_neither_id_is_400(self):
        for _method, route, body, _api in SESSION_WRITE_ROUTES:
            resp = self.post(route, {'confirm': True})
            self.assertEqual(resp.status_code, 400, route)
            self.assertEqual(resp.json()['code'], 'invalid_fields')
            self.assertEqual(self.fake.calls, [], route)

    def test_platform_says_false_is_502_without_log_or_event(self):
        self.fake.payloads[proto.SESSION_REJOIN_API] = REJOIN_SAYS_FALSE
        before = len(self.topics(0))
        resp = self.post(f'{BASE}/{ACC}/sessions/rejoin',
                         {'session_id': SESSION, 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('会话已结束', resp.text)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.topics(before), [])

    def test_platform_refusal_is_502_and_keeps_login_state(self):
        self.fake.payloads[proto.SESSION_LEAVE_API] = NO_PERMISSION
        resp = self.post(f'{BASE}/{ACC}/sessions/leave',
                         {'session_id': SESSION, 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.text)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.login_state(), 'active')

    def test_session_expiry_is_409(self):
        self.fake.errors[proto.SESSION_REJOIN_API] = SessionExpiredError('过期了')
        resp = self.post(f'{BASE}/{ACC}/sessions/rejoin',
                         {'session_id': SESSION, 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_only_one_platform_call_per_action(self):
        for _method, route, body, api in SESSION_WRITE_ROUTES:
            self.fake.calls.clear()
            self.ok_data(route, dict(body, confirm=True))
            self.assertEqual(self.fake.apis(), [api], route)

    def test_conversation_id_also_works_for_sessions(self):
        row_id = self.seed_conversation(cid='cid-from-db')
        data = self.ok_data(f'{BASE}/{ACC}/sessions/leave',
                            {'conversation_id': row_id, 'confirm': True})
        self.assertTrue(data['saved'])
        self.assertEqual(self.fake.call_of(proto.SESSION_LEAVE_API)['data'],
                         {'sessionId': 'cid-from-db'})

    def test_log_detail_carries_the_session_id(self):
        self.ok_data(f'{BASE}/{ACC}/sessions/leave',
                     {'session_id': SESSION, 'confirm': True})
        self.assertIn(SESSION, str(self.operations()[0]['detail']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
