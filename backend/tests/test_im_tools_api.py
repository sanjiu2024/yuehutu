# -*- coding: utf-8 -*-
"""IM 工具族（会话搜索 / 商品搜索 / 清未读）的测试。

**为什么单开一组**：这一族混了「两条只读 + 一条写」，最容易犯的错是：

1. 把平台没返回的列表补成 `[]` —— 「平台没说」和「真的没有」是两件事；
2. 把「总数」拿当前页条数冒充（`total` 缺了就得是 `None` + warning）；
3. 清未读没确认就发（红线：未确认时**一个平台请求都不许发**）；
4. 确认判断用 `bool(...)` —— `bool('false')` 是 `True`；
5. 平台明确回 `success: false` 还去清本地未读（谎报成功）；
6. 商品搜索只给本地会话行 id 时，拿 `peer_user_id` 冒充 cid；
7. 字段校验排在凭据检查**之后**（掉线时先喊「去重新登录」，用户填半天才发现参数错）。

**一次真机请求都没发过**：这里用假协议层钉住「接口名、版本号、入参形状与官方 bundle
逐字一致」（偏移见 `backend/app/xianyu/im_tools.py` 的常量注释与 `WRITE_APIS[...]['evidence']`），
平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_im_tools_api.py"

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

from backend.app.api import im_tools as im_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import im_tools as service  # noqa: E402
from backend.app.xianyu import im_tools as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'im47_acc'
BASE = '/api/im'
SESSION = 'cid-1'

#: 会话搜索：官方读 `resp.data` 里的 `sessionList` / `total` / `curPage`。
SESSION_SEARCH_OK = {
    'success': True,
    'data': {'sessionList': [{'cid': 'cid-1', 'title': '买家一', 'unread': 2,
                              'sessionType': '1', 'lastMessage': {'text': '在吗'}}],
             'total': 3, 'curPage': 1},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 别名键（平台换过键名）。
SESSION_SEARCH_ALIAS = {
    'success': True,
    'data': {'sessions': [{'sessionId': 'cid-2', 'nick': '买家二'}]},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台回了信封但没有列表键 —— 不是「没有会话」。
SESSION_SEARCH_MISSING = {
    'success': True, 'data': {'foo': 1},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 商品搜索：`items` + `hasMore`。
ITEM_SEARCH_OK = {
    'success': True,
    'data': {'items': [{'itemId': 'it-1', 'title': '手机壳', 'picUrl': 'http://x/1.jpg',
                        'price': '9.90'}], 'hasMore': True},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 官方另一处调用点用 `nextPage` 而不是 `hasMore`（两个都认）。
ITEM_SEARCH_NEXTPAGE = {
    'success': True, 'data': {'items': [], 'nextPage': 3},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
ITEM_SEARCH_MISSING = {
    'success': True, 'data': {'zzz': 1},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 清未读：官方发完不看响应 ⇒ 常见回执是空业务体。
CLEAN_NO_ECHO = {
    'success': True, 'data': {},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
CLEAN_OK = {
    'success': True, 'data': {'success': True},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台明确说没成功。
CLEAN_FALSE = {
    'success': True, 'data': {'success': False, 'message': '平台说不行'},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

#: 一条写路由 + 它的接口名（confirm 由用例单独塞）。
CLEAN_ROUTES: List[tuple] = [
    ('POST', f'{BASE}/{ACC}/unread/clean', {'kind': 'session'}, proto.UNREAD_CLEAN_API),
    ('POST', f'{BASE}/{ACC}/unread/clean', {'kind': 'system'}, proto.SYSTEMS_UNREAD_CLEAN_API),
]

FALSE_LIKE = ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2, [], {})


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeImToolsClient:
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


def install_fake_protocol(client: FakeImToolsClient, *, with_factory: bool = True) -> None:
    """注入假协议层（`with_factory=False` 用来测「没有 Cookie 一律 409 且零调用」）。"""
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeImToolsClient:
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


def seed_conversation(account_id: str = ACC, peer: str = 'buyer-1',
                      cid: str = 'cid-7') -> int:
    """插一行本地会话 + 一条带 `meta.cid` 的消息（cid 只存在消息里，会话表没有这列）。"""
    row_id = int(db.execute(
        'INSERT INTO conversations(account_id, peer_user_id, peer_nickname, unread, mode, '
        'agent_role) VALUES(?,?,?,?,?,?)', (account_id, peer, '买家一', 3, 'manual', 'item')))
    db.execute('INSERT INTO messages(conversation_id, direction, sender, content, msg_type, '
               'meta, created_at) VALUES(?,?,?,?,?,?,?)',
               (row_id, 'in', peer, 'hi', 'text',
                json.dumps({'cid': cid}, ensure_ascii=False), '2026-10-14 10:00:00'))
    return row_id


def full_client(**overrides: Any) -> FakeImToolsClient:
    payloads = {
        proto.SESSION_SEARCH_API: SESSION_SEARCH_OK,
        proto.ITEM_SEARCH_API: ITEM_SEARCH_OK,
        proto.UNREAD_CLEAN_API: CLEAN_NO_ECHO,
        proto.SYSTEMS_UNREAD_CLEAN_API: CLEAN_OK,
    }
    payloads.update(overrides)
    return FakeImToolsClient(payloads)


# --------------------------------------------------------------------------- 协议层


class ImToolsProtocolTest(unittest.TestCase):
    """纯函数：接口名、版本、入参形状、确认闸门、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.SESSION_SEARCH_API,
                         'mtop.taobao.idlemessage.pc.session.search')
        self.assertEqual(proto.ITEM_SEARCH_API,
                         'mtop.taobao.idlemessage.pc.tool.item.search')
        self.assertEqual(proto.UNREAD_CLEAN_API,
                         'mtop.taobao.idlemessage.pc.session.unread.clean')
        self.assertEqual(proto.SYSTEMS_UNREAD_CLEAN_API,
                         'mtop.taobao.idlemessage.pc.systems.unread.clean')

    def test_all_versions_are_1(self):
        for api in proto.READ_APIS:
            self.assertEqual(proto.READ_VERSIONS[api], '1.0', api)
        for api in proto.WRITE_API_SET:
            self.assertEqual(proto.WRITE_VERSIONS[api], '1.0', api)

    def test_read_and_write_sets_are_disjoint(self):
        self.assertEqual(proto.READ_APIS,
                         (proto.SESSION_SEARCH_API, proto.ITEM_SEARCH_API))
        self.assertEqual(proto.WRITE_API_SET,
                         frozenset({proto.UNREAD_CLEAN_API, proto.SYSTEMS_UNREAD_CLEAN_API}))
        self.assertEqual(set(proto.READ_APIS) & set(proto.WRITE_API_SET), set())
        for action, spec in proto.WRITE_APIS.items():
            self.assertTrue(spec['evidence'], action)
            self.assertIn(spec['version'], ('1.0',), action)

    def test_roles_and_labels(self):
        self.assertEqual(proto.SESSION_ROLES, ('own', 'other'))
        self.assertEqual(proto.SESSION_ROLE_LABELS['own'], '我的宝贝')
        self.assertEqual(proto.SESSION_ROLE_LABELS['other'], 'TA的宝贝')

    def test_session_search_request_shape(self):
        self.assertEqual(proto.session_search_request('  电池  ', 2, 30),
                         (proto.SESSION_SEARCH_API,
                          {'keyword': '电池', 'pageNumber': 2, 'pageSize': 30}, '1.0'))

    def test_session_search_validation(self):
        for bad, kwargs in (('关键词超长', {'keyword': 'x' * (proto.KEYWORD_MAX + 1)}),
                            ('页码 0', {'page': 0}),
                            ('页码非数字', {'page': 'a'}),
                            ('size 0', {'size': 0}),
                            ('size 超上限', {'size': proto.PAGE_SIZE_MAX + 1})):
            with self.assertRaises(ValueError, msg=bad):
                proto.session_search_request(**kwargs)

    def test_item_search_request_shape(self):
        """官方两处调用点一个发 `pageNumber`、一个发 `currentPage` ⇒ 两个都发。"""
        self.assertEqual(proto.item_search_request('cid-1', 'other', '壳', 3, 20),
                         (proto.ITEM_SEARCH_API,
                          {'sessionId': 'cid-1', 'pageSize': 20, 'pageNumber': 3,
                           'currentPage': 3, 'searchItemRole': 'other', 'queryWord': '壳'},
                          '1.0'))

    def test_item_search_validation(self):
        with self.assertRaises(ValueError):
            proto.item_search_request('', 'own')
        with self.assertRaises(ValueError):
            proto.item_search_request('cid-1', 'me')

    def test_clean_requests_carry_no_data(self):
        self.assertEqual(proto.clear_session_unread_request(confirm=True),
                         (proto.UNREAD_CLEAN_API, {}, '1.0'))
        self.assertEqual(proto.clear_systems_unread_request(confirm=True),
                         (proto.SYSTEMS_UNREAD_CLEAN_API, {}, '1.0'))

    def test_protocol_confirm_gate_is_literal_true(self):
        """协议层只认字面 `True`（归一在服务层，见 `_truthy`）。"""
        for bad in (None, False, 0, 'false', 'FALSE', 'no', 'true', 1):
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(bad)):
                proto.clear_session_unread_request(confirm=bad)

    def test_assert_write_and_read(self):
        for api in proto.WRITE_API_SET:
            self.assertEqual(proto.assert_write(api), api)
        for api in proto.READ_APIS:
            self.assertEqual(proto.assert_read(api), api)
            with self.assertRaises(proto.UnknownApi):
                proto.assert_write(api)
        for api in proto.WRITE_API_SET:
            with self.assertRaises(proto.UnknownApi):
                proto.assert_read(api)

    def test_request_of_dispatch(self):
        self.assertEqual(proto.request_of('clear_session_unread', confirm=True)[0],
                         proto.UNREAD_CLEAN_API)
        self.assertEqual(proto.request_of('clear_systems_unread', confirm=True)[0],
                         proto.SYSTEMS_UNREAD_CLEAN_API)
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('nope')

    def test_unread_kind_normalisation(self):
        self.assertEqual(proto._unread_kind(' session '), 'session')
        self.assertEqual(proto._unread_kind('SYSTEM'), 'system')
        with self.assertRaises(ValueError):
            proto._unread_kind('weird')

    def test_parse_session_search(self):
        parsed = proto.parse_session_search(SESSION_SEARCH_OK)
        self.assertEqual(parsed['sessions'][0]['session_id'], 'cid-1')
        self.assertEqual(parsed['sessions'][0]['title'], '买家一')
        self.assertEqual(parsed['sessions'][0]['unread'], 2)
        self.assertEqual(parsed['sessions'][0]['session_type'], 1)
        self.assertEqual(parsed['sessions'][0]['last_message'], '在吗')
        self.assertEqual(parsed['total'], 3)
        self.assertEqual(parsed['cur_page'], 1)
        self.assertEqual(parsed['list_key'], 'sessionList')
        self.assertTrue(parsed['found'])

    def test_parse_session_search_aliases(self):
        parsed = proto.parse_session_search(SESSION_SEARCH_ALIAS)
        self.assertEqual(parsed['sessions'][0]['session_id'], 'cid-2')
        self.assertEqual(parsed['sessions'][0]['title'], '买家二')
        self.assertIsNone(parsed['cur_page'])

    def test_parse_session_search_missing_is_none(self):
        parsed = proto.parse_session_search(SESSION_SEARCH_MISSING)
        self.assertIsNone(parsed['sessions'])
        self.assertIsNone(parsed['total'])
        self.assertTrue(parsed['found'])  # 信封有业务体，只是键不认识

    def test_parse_session_search_non_dict_entries_survive(self):
        parsed = proto.parse_session_search({'success': True, 'data': {'sessionList': ['x', 1]},
                                             'ret': ['SUCCESS::调用成功']})
        self.assertIsNone(parsed['sessions'][0]['session_id'])
        self.assertEqual(parsed['sessions'][0]['raw'], 'x')

    def test_parse_item_search(self):
        parsed = proto.parse_item_search(ITEM_SEARCH_OK)
        self.assertEqual(parsed['items'][0]['item_id'], 'it-1')
        self.assertEqual(parsed['items'][0]['title'], '手机壳')
        self.assertEqual(parsed['items'][0]['pic_url'], 'http://x/1.jpg')
        self.assertEqual(parsed['items'][0]['price'], '9.90')
        self.assertIs(parsed['has_more'], True)
        self.assertIsNone(parsed['next_page'])

    def test_parse_item_search_accepts_next_page(self):
        parsed = proto.parse_item_search(ITEM_SEARCH_NEXTPAGE)
        self.assertEqual(parsed['next_page'], 3)
        self.assertIsNone(parsed['has_more'])
        self.assertEqual(parsed['items'], [])

    def test_parse_item_search_missing_is_none(self):
        self.assertIsNone(proto.parse_item_search(ITEM_SEARCH_MISSING)['items'])

    def test_parse_clean_is_three_state(self):
        self.assertIsNone(proto.parse_clean(CLEAN_NO_ECHO)['platform_success'])
        self.assertIs(proto.parse_clean(CLEAN_OK)['platform_success'], True)
        parsed = proto.parse_clean(CLEAN_FALSE)
        self.assertIs(parsed['platform_success'], False)
        self.assertEqual(parsed['platform_message'], '平台说不行')


# --------------------------------------------------------------------------- 接口层


class ImToolsPageTest(unittest.TestCase):
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
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state',
                      'conversations', 'messages'):
            db.execute(f'DELETE FROM {table}')
        self.fake = full_client()
        install_fake_protocol(self.fake)
        app = FastAPI()
        app.include_router(im_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('im47','x',1)"))
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

    def data_of(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.get(path, params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()['data']

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


class ImSessionSearchTest(ImToolsPageTest):
    """会话搜索（只读）。"""

    def test_search_returns_sessions(self):
        data = self.data_of(f'{BASE}/{ACC}/sessions/search', params={'keyword': '买家'})
        self.assertEqual(data['keyword'], '买家')
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['size'], proto.PAGE_SIZE_DEFAULT)
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['cur_page'], 1)
        self.assertEqual(data['sessions'][0]['session_id'], 'cid-1')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        call = self.fake.call_of(proto.SESSION_SEARCH_API)
        self.assertEqual(call['data'], {'keyword': '买家', 'pageNumber': 1,
                                        'pageSize': proto.PAGE_SIZE_DEFAULT})
        self.assertEqual(call['v'], '1.0')

    def test_blank_keyword_is_allowed(self):
        data = self.data_of(f'{BASE}/{ACC}/sessions/search')
        self.assertEqual(data['keyword'], '')
        self.assertEqual(self.fake.call_of(proto.SESSION_SEARCH_API)['data']['keyword'], '')

    def test_missing_list_key_is_null_with_warning(self):
        self.fake.payloads[proto.SESSION_SEARCH_API] = SESSION_SEARCH_MISSING
        data = self.data_of(f'{BASE}/{ACC}/sessions/search')
        self.assertIsNone(data['sessions'])
        self.assertIsNone(data['total'])
        self.assertEqual([w['code'] for w in data['warnings']],
                         ['im_sessions_missing', 'im_session_total_missing'])

    def test_total_missing_is_null_not_page_count(self):
        self.fake.payloads[proto.SESSION_SEARCH_API] = SESSION_SEARCH_ALIAS
        data = self.data_of(f'{BASE}/{ACC}/sessions/search')
        self.assertIsNone(data['total'])
        self.assertEqual([w['code'] for w in data['warnings']], ['im_session_total_missing'])

    def test_keyword_too_long_is_400_without_calling_platform(self):
        resp = self.get(f'{BASE}/{ACC}/sessions/search',
                        params={'keyword': 'x' * (proto.KEYWORD_MAX + 1)})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_page_zero_is_422_from_fastapi(self):
        """`Query(ge=1)` 在路由层就拦住了（不是缺陷，是框架行为）。"""
        resp = self.get(f'{BASE}/{ACC}/sessions/search', params={'page': 0})
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_platform_refusal_is_502_and_keeps_login_state(self):
        self.fake.payloads[proto.SESSION_SEARCH_API] = NO_PERMISSION
        resp = self.get(f'{BASE}/{ACC}/sessions/search')
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.text)
        self.assertEqual(self.login_state(), 'active')  # 无权限 ≠ 掉线

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.SESSION_SEARCH_API] = SessionExpiredError('登录态过期')
        resp = self.get(f'{BASE}/{ACC}/sessions/search')
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_unknown_account_is_404_without_calling_platform(self):
        resp = self.get(f'{BASE}/no-such-acc/sessions/search')
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_without_calling_platform(self):
        seed_account(cookie='', login_state='need_relogin')
        resp = self.get(f'{BASE}/{ACC}/sessions/search')
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_unauthenticated_is_401(self):
        resp = self.get(f'{BASE}/{ACC}/sessions/search', headers={})
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(self.fake.calls, [])


class ImItemSearchTest(ImToolsPageTest):
    """商品搜索（只读）：我的宝贝 / TA的宝贝。"""

    def test_search_with_platform_cid(self):
        data = self.data_of(f'{BASE}/{ACC}/item-search',
                            params={'session_id': SESSION, 'role': 'other', 'keyword': '壳'})
        self.assertEqual(data['session_id'], SESSION)
        self.assertEqual(data['role'], 'other')
        self.assertEqual(data['items'][0]['item_id'], 'it-1')
        self.assertIs(data['has_more'], True)
        call = self.fake.call_of(proto.ITEM_SEARCH_API)
        self.assertEqual(call['data'], {'sessionId': SESSION, 'pageSize': proto.PAGE_SIZE_DEFAULT,
                                        'pageNumber': 1, 'currentPage': 1,
                                        'searchItemRole': 'other', 'queryWord': '壳'})

    def test_local_conversation_id_is_resolved_to_cid(self):
        row_id = seed_conversation(cid='cid-7')
        data = self.data_of(f'{BASE}/{ACC}/item-search',
                            params={'conversation_id': row_id, 'role': 'own'})
        self.assertEqual(data['session_id'], 'cid-7')
        self.assertEqual(self.fake.call_of(proto.ITEM_SEARCH_API)['data']['sessionId'], 'cid-7')

    def test_session_id_wins_over_conversation_id(self):
        row_id = seed_conversation(cid='cid-7')
        data = self.data_of(f'{BASE}/{ACC}/item-search',
                            params={'session_id': 'cid-explicit', 'conversation_id': row_id})
        self.assertEqual(data['session_id'], 'cid-explicit')

    def test_unknown_conversation_is_404(self):
        resp = self.get(f'{BASE}/{ACC}/item-search', params={'conversation_id': 999})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_conversation_without_cid_is_400_missing_cid(self):
        row_id = int(db.execute(
            'INSERT INTO conversations(account_id, peer_user_id, peer_nickname, unread, mode, '
            'agent_role) VALUES(?,?,?,?,?,?)', (ACC, 'buyer-9', '买家九', 0, 'manual', 'item')))
        resp = self.get(f'{BASE}/{ACC}/item-search', params={'conversation_id': row_id})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'missing_cid')
        self.assertEqual(self.fake.calls, [])

    def test_neither_id_is_400(self):
        resp = self.get(f'{BASE}/{ACC}/item-search')
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_bad_role_is_400_without_calling_platform(self):
        resp = self.get(f'{BASE}/{ACC}/item-search',
                        params={'session_id': SESSION, 'role': 'me'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_missing_list_key_is_null_with_warning(self):
        self.fake.payloads[proto.ITEM_SEARCH_API] = ITEM_SEARCH_MISSING
        data = self.data_of(f'{BASE}/{ACC}/item-search', params={'session_id': SESSION})
        self.assertIsNone(data['items'])
        self.assertEqual([w['code'] for w in data['warnings']], ['im_items_missing'])

    def test_next_page_is_passed_through(self):
        self.fake.payloads[proto.ITEM_SEARCH_API] = ITEM_SEARCH_NEXTPAGE
        data = self.data_of(f'{BASE}/{ACC}/item-search', params={'session_id': SESSION})
        self.assertEqual(data['items'], [])
        self.assertEqual(data['next_page'], 3)
        self.assertIsNone(data['has_more'])


class ImClearUnreadTest(ImToolsPageTest):
    """清未读（写）：确认闸门、本地清零、回执三态、审计与事件。"""

    def test_confirm_is_required_and_sends_nothing(self):
        for bad in FALSE_LIKE:
            resp = self.post(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': bad})
            self.assertEqual(resp.status_code, 400, f'{bad!r} {resp.text}')
            self.assertEqual(resp.json()['code'], 'confirmation_required', repr(bad))
        self.assertEqual(self.fake.calls, [])

    def test_true_like_values_pass(self):
        for index, good in enumerate((True, 'true', ' TRUE ', 1)):
            self.fake.calls.clear()
            row_id = seed_conversation(peer=f'buyer-{index}', cid=f'cid-{index}')
            data = self.ok_data(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': good})
            self.assertIs(data['saved'], True, repr(good))
            self.assertEqual(self.fake.apis(), [proto.UNREAD_CLEAN_API], repr(good))
            self.assertEqual(db.query_one('SELECT unread FROM conversations WHERE id = ?',
                                          (row_id,))['unread'], 0)

    def test_confirm_is_judged_before_field_validation(self):
        resp = self.post(f'{BASE}/{ACC}/unread/clean', {'kind': 'weird', 'confirm': None})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_clears_local_unread_and_audits(self):
        row_id = seed_conversation()
        other = seed_conversation(peer='buyer-2', cid='cid-8')
        before = len(self.topics(0))
        data = self.ok_data(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': True})
        self.assertIs(data['saved'], True)
        self.assertIsNone(data['platform_success'])
        self.assertEqual(data['cleared'], 2)
        self.assertEqual([w['code'] for w in data['warnings']], ['im_unread_unconfirmed'])
        for rid in (row_id, other):
            self.assertEqual(db.query_one('SELECT unread FROM conversations WHERE id = ?',
                                          (rid,))['unread'], 0)
        rows = self.operations()
        self.assertEqual([row['action'] for row in rows], ['im_unread_clean'])
        self.assertEqual(rows[0]['success'], 1)
        self.assertEqual(self.topics(before), ['chats.changed'])

    def test_platform_false_is_502_and_local_unread_stays(self):
        row_id = seed_conversation()
        self.fake.payloads[proto.UNREAD_CLEAN_API] = CLEAN_FALSE
        resp = self.post(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('平台说不行', resp.text)
        self.assertEqual(db.query_one('SELECT unread FROM conversations WHERE id = ?',
                                      (row_id,))['unread'], 3)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.login_state(), 'active')

    def test_platform_true_has_no_warning(self):
        self.fake.payloads[proto.UNREAD_CLEAN_API] = CLEAN_OK
        data = self.ok_data(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': True})
        self.assertIs(data['platform_success'], True)
        self.assertEqual(data['warnings'], [])

    def test_system_kind_uses_the_other_api_and_touches_nothing_local(self):
        row_id = seed_conversation()
        data = self.ok_data(f'{BASE}/{ACC}/unread/clean', {'kind': 'system', 'confirm': True})
        self.assertEqual(data['kind'], 'system')
        self.assertEqual(self.fake.apis(), [proto.SYSTEMS_UNREAD_CLEAN_API])
        self.assertIsNone(data['cleared'])
        self.assertEqual(db.query_one('SELECT unread FROM conversations WHERE id = ?',
                                      (row_id,))['unread'], 3)

    def test_bad_kind_is_400_without_calling_platform(self):
        resp = self.post(f'{BASE}/{ACC}/unread/clean', {'kind': 'weird', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_without_calling_platform(self):
        resp = self.post(f'{BASE}/no-such-acc/unread/clean',
                         {'kind': 'session', 'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.UNREAD_CLEAN_API] = SessionExpiredError('登录态过期')
        resp = self.post(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.login_state(), 'need_relogin')
        self.assertEqual(self.operations(), [])

    def test_unauthenticated_is_401(self):
        resp = self.post(f'{BASE}/{ACC}/unread/clean', {'kind': 'session', 'confirm': True},
                         headers={})
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(self.fake.calls, [])


class ImToolsRoutesTest(ImToolsPageTest):
    """路由面：三条路径、方法不串。"""

    def test_route_set(self):
        routes = {(route.path, method) for route in im_api.router.routes
                  for method in route.methods}
        self.assertEqual(routes, {
            ('/api/im/{account_id}/sessions/search', 'GET'),
            ('/api/im/{account_id}/item-search', 'GET'),
            ('/api/im/{account_id}/unread/clean', 'POST'),
        })

    def test_get_on_write_path_is_405(self):
        resp = self.get(f'{BASE}/{ACC}/unread/clean')
        self.assertEqual(resp.status_code, 405, resp.text)

    def test_module_is_registered_in_main(self):
        # 假协议层是空壳模块，而 `services/finance.py` 在导入时就读客户端常量 ⇒
        # 必须先摘掉假模块再 import main（与 test_fans_price_api.py 的 openapi_paths 同法）。
        remove_fake_protocol()
        try:
            from backend.app import main
            self.assertIn('im_tools', main.ROUTE_MODULES)
        finally:
            install_fake_protocol(self.fake)


if __name__ == '__main__':
    unittest.main(verbosity=2)
