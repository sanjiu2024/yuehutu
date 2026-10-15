# -*- coding: utf-8 -*-
"""话术（官方 IM 工作台的「快捷回复」：分组 + 短语）的测试。

**为什么单开一组**：这一族是「平台侧配置」的读 + 写，最容易犯的错是：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现分组名超了 10 个字；
3. 把平台**没回显 id** 当成「保存成功」或者「保存失败」—— 两种都是谎报；
4. 把平台没返回的列表键补成 `[]` —— 平台没给 ≠ 一条话术都没有；
5. 把登录态判错：平台「无权限」不等于掉线，不该让用户白扫一次码。

**一次真机写请求都没发过**（这会在卖家账号下真的建/删分组与短语）。这里用假协议层钉住
「字段形状与官方 bundle 逐字一致」（偏移见 `backend/app/xianyu/quick_reply.py` 的
`WRITE_APIS[...]['evidence']`），平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_quick_reply_api.py"

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

from backend.app.api import quick_reply as qr_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import quick_reply as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'qr_acc'
BASE = '/api/quick-reply'

#: 列表成功：两个分组，第一个带两条短语（官方调用点读 `resp.data.groups`）。
LIST_OK = {
    'success': True,
    'data': {
        'code': 'success',
        'groups': [
            {'groupId': 9001, 'groupName': '常用', 'contents': [
                {'contentId': 11, 'content': '在的，稍等我看一下～'},
                {'contentId': 12, 'content': '已经发货了，麻烦注意查收'},
            ]},
            {'groupId': 9002, 'groupName': '售后', 'contents': []},
        ],
        'total': 2,
    },
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台没给 `groups` 键 —— 不等于「一条话术都没有」。
LIST_MISSING = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台给了列表但没给 total ⇒ 用数出来的条数。
LIST_NO_TOTAL = {
    'success': True,
    'data': {'code': 'success', 'groups': [{'groupId': 9001, 'groupName': '常用', 'contents': []}]},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 分组写回执（官方成功判据 = `groupInfo.groupId` 存在）。
GROUP_CREATE_OK = {
    'success': True,
    'data': {'code': 'success', 'groupInfo': {'groupId': 9001, 'groupName': '常用'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
GROUP_UPDATE_OK = {
    'success': True,
    'data': {'code': 'success', 'groupInfo': {'groupId': 9001, 'groupName': '常用话术'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
GROUP_DELETE_OK = {
    'success': True,
    'data': {'code': 'success', 'groupInfo': {'groupId': 9001}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台接受了请求但**没回显 groupInfo** —— 既不是成功也不是失败。
GROUP_NO_ECHO = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 短语写回执（官方成功判据 = `contentInfo.contentId` 存在）。
CONTENT_CREATE_OK = {
    'success': True,
    'data': {'code': 'success', 'contentInfo': {'contentId': 11, 'groupId': 9001,
                                                'content': '在的，稍等我看一下～'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
CONTENT_UPDATE_OK = {
    'success': True,
    'data': {'code': 'success', 'contentInfo': {'contentId': 11, 'groupId': 9001,
                                                'content': '在的，马上帮您看'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
CONTENT_DELETE_OK = {
    'success': True,
    'data': {'code': 'success', 'contentInfo': {'contentId': 11, 'groupId': 9001}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
CONTENT_NO_ECHO = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}
#: 客户端层失败（平台信封为空时只剩它）。
CLIENT_ERROR = {'success': False, 'message': 'FAIL_SYS_ILLEGAL_ACCESS::非法请求',
                'ret': ['FAIL_SYS_ILLEGAL_ACCESS::非法请求']}

WRITE_ROUTES: List[tuple] = [
    ('POST', f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True}),
    ('POST', f'{BASE}/{ACC}/groups/9001', {'group_name': '常用话术', 'confirm': True}),
    ('POST', f'{BASE}/{ACC}/groups/9001/delete', {'confirm': True}),
    ('POST', f'{BASE}/{ACC}/phrases', {'group_id': 9001, 'content': '在的', 'confirm': True}),
    ('POST', f'{BASE}/{ACC}/phrases/11', {'group_id': 9001, 'content': '在的',
                                          'confirm': True}),
    ('POST', f'{BASE}/{ACC}/phrases/11/delete', {'group_id': 9001, 'confirm': True}),
]


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeQuickReplyClient:
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


def install_fake_protocol(client: FakeQuickReplyClient, *, with_factory: bool = True) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeQuickReplyClient:
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


def full_client(**overrides: Any) -> FakeQuickReplyClient:
    payloads = {
        proto.LIST_API: LIST_OK,
        proto.GROUP_API: GROUP_CREATE_OK,
        proto.CONTENT_API: CONTENT_CREATE_OK,
    }
    payloads.update(overrides)
    return FakeQuickReplyClient(payloads)


# --------------------------------------------------------------------------- 协议层


class QuickReplyProtocolTest(unittest.TestCase):
    """纯函数：接口名、入参形状、确认闸门、长度上限、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.LIST_API, 'mtop.taobao.idlemessage.quickreply.list.get.v1')
        self.assertEqual(proto.GROUP_API, 'mtop.taobao.idlemessage.quickreply.group.operate')
        self.assertEqual(proto.CONTENT_API,
                         'mtop.taobao.idlemessage.quickreply.content.operate')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_whitelist_has_six_writes_and_one_read(self):
        self.assertEqual(proto.READ_APIS, (proto.LIST_API,))
        self.assertEqual(proto.ACTIONS, ('group_create', 'group_update', 'group_delete',
                                         'phrase_create', 'phrase_update', 'phrase_delete'))
        for action, spec in proto.WRITE_APIS.items():
            self.assertIn(spec['api'], proto.ALL_APIS, action)
            self.assertTrue(spec['evidence'], action)
            self.assertTrue(spec['data'], action)

    def test_assert_write_rejects_read_api(self):
        for action, spec in proto.WRITE_APIS.items():
            self.assertEqual(proto.assert_write(spec['api']), spec['api'], action)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.LIST_API)

    def test_assert_read_rejects_write_apis(self):
        self.assertEqual(proto.assert_read(proto.LIST_API), proto.LIST_API)
        for action, spec in proto.WRITE_APIS.items():
            with self.assertRaises(proto.UnknownApi):
                proto.assert_read(spec['api'])

    def test_confirm_gate_is_identity_not_truthiness(self):
        """`bool('false')` 是 True ⇒ 只认字面量 True。"""
        for bad in (None, False, 0, '', 'false', 'no', 'yes', 2, 'True'):
            with self.assertRaises(proto.WriteNotConfirmed, msg=repr(bad)):
                proto.group_request('create', group_name='常用', confirm=bad)
        self.assertIsNotNone(proto.group_request('create', group_name='常用', confirm=True))

    def test_list_request_sends_no_params(self):
        self.assertEqual(proto.list_request(), (proto.LIST_API, {}))

    def test_group_create_payload_is_verbatim(self):
        api, data = proto.group_request('create', group_name=' 常用 ', confirm=True)
        self.assertEqual(api, proto.GROUP_API)
        self.assertEqual(data, {'groupName': '常用', 'operateType': 'create'})

    def test_group_update_payload_is_verbatim(self):
        api, data = proto.group_request('update', group_id='9001', group_name='常用话术',
                                        confirm=True)
        self.assertEqual(data, {'groupId': 9001, 'groupName': '常用话术',
                                'operateType': 'update'})

    def test_group_delete_payload_is_verbatim(self):
        api, data = proto.group_request('delete', group_id=9001, confirm=True)
        self.assertEqual(data, {'groupId': 9001, 'operateType': 'delete'})

    def test_phrase_create_payload_is_verbatim(self):
        api, data = proto.content_request('create', group_id=9001, content='在的', confirm=True)
        self.assertEqual(api, proto.CONTENT_API)
        self.assertEqual(data, {'groupId': 9001, 'content': '在的', 'operateType': 'create'})

    def test_phrase_update_payload_is_verbatim(self):
        api, data = proto.content_request('update', group_id=9001, content_id=11,
                                          content='在的，马上帮您看', confirm=True)
        self.assertEqual(data, {'groupId': 9001, 'contentId': 11,
                                'content': '在的，马上帮您看', 'operateType': 'update'})

    def test_phrase_delete_payload_is_verbatim(self):
        api, data = proto.content_request('delete', group_id=9001, content_id=11, confirm=True)
        self.assertEqual(data, {'groupId': 9001, 'contentId': 11, 'operateType': 'delete'})

    def test_request_of_dispatches_by_action(self):
        api, data = proto.request_of('group_create', group_name='常用', confirm=True)
        self.assertEqual((api, data['operateType']), (proto.GROUP_API, 'create'))
        api, data = proto.request_of('phrase_delete', group_id=9001, content_id=11, confirm=True)
        self.assertEqual((api, data['operateType']), (proto.CONTENT_API, 'delete'))
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('group_rename', group_name='x', confirm=True)

    def test_unknown_operate_type_is_rejected(self):
        with self.assertRaises(ValueError):
            proto.group_request('rename', group_name='常用', confirm=True)
        with self.assertRaises(ValueError):
            proto.content_request('remove', group_id=9001, content_id=11, confirm=True)

    def test_group_name_limit_is_ten(self):
        """官方输入框 `maxLength:10`。"""
        self.assertEqual(proto.normalize_group_name('字' * 10), '字' * 10)
        with self.assertRaises(ValueError):
            proto.normalize_group_name('字' * 11)
        with self.assertRaises(ValueError):
            proto.normalize_group_name('   ')

    def test_content_limit_is_two_hundred(self):
        """官方 `maxLength:200` 且 onChange 里再 `slice(0,200)`。"""
        self.assertEqual(len(proto.normalize_content('字' * 200)), 200)
        with self.assertRaises(ValueError):
            proto.normalize_content('字' * 201)
        with self.assertRaises(ValueError):
            proto.normalize_content('')

    def test_ids_reject_blank_and_keep_numbers(self):
        self.assertEqual(proto.normalize_group_id(9001), 9001)
        self.assertEqual(proto.normalize_group_id('9001'), 9001)
        self.assertEqual(proto.normalize_content_id('c-1'), 'c-1')
        for bad in (None, '', '   ', True, False):
            with self.assertRaises(ValueError, msg=repr(bad)):
                proto.normalize_group_id(bad)

    def test_parse_groups_reads_rows(self):
        parsed = proto.parse_groups(LIST_OK)
        self.assertTrue(parsed['found_key'])
        self.assertEqual(parsed['total'], 2)
        self.assertTrue(parsed['total_from_platform'])
        first = parsed['groups'][0]
        self.assertEqual(first['group_id'], '9001')
        self.assertEqual(first['group_name'], '常用')
        self.assertEqual([c['content_id'] for c in first['contents']], ['11', '12'])
        self.assertEqual(parsed['groups'][1]['contents'], [])

    def test_parse_groups_missing_key_is_none_not_empty(self):
        parsed = proto.parse_groups(LIST_MISSING)
        self.assertFalse(parsed['found_key'])
        self.assertIsNone(parsed['groups'])
        self.assertIsNone(parsed['total'])

    def test_parse_groups_total_falls_back_to_counted(self):
        parsed = proto.parse_groups(LIST_NO_TOTAL)
        self.assertEqual(parsed['total'], 1)
        self.assertFalse(parsed['total_from_platform'])

    def test_parse_group_write_needs_group_id(self):
        self.assertTrue(proto.parse_group_write(GROUP_CREATE_OK)['saved'])
        self.assertEqual(proto.parse_group_write(GROUP_CREATE_OK)['group_id'], '9001')
        self.assertFalse(proto.parse_group_write(GROUP_NO_ECHO)['saved'])
        self.assertIsNone(proto.parse_group_write(GROUP_NO_ECHO)['group_id'])

    def test_parse_content_write_needs_content_id(self):
        parsed = proto.parse_content_write(CONTENT_CREATE_OK)
        self.assertTrue(parsed['saved'])
        self.assertEqual((parsed['content_id'], parsed['group_id']), ('11', '9001'))
        self.assertFalse(proto.parse_content_write(CONTENT_NO_ECHO)['saved'])

    def test_is_success_variants(self):
        self.assertTrue(proto.is_success(LIST_OK))
        self.assertFalse(proto.is_success(CLIENT_ERROR))
        self.assertFalse(proto.is_success({'success': True, 'data': {'error': '分组名重复'}}))
        self.assertFalse(proto.is_success({'success': True, 'data': {'code': 'FAIL_XX'}}))
        self.assertFalse(proto.is_success(None))
        self.assertTrue(proto.is_success({'success': True, 'data': {}}))

    def test_failure_reason_priority(self):
        """客户端层失败文案优先，其次业务体 msg/message/error。"""
        self.assertIn('非法请求', proto.failure_reason(CLIENT_ERROR))
        self.assertEqual(proto.failure_reason({'success': True, 'data': {'error': '分组名重复'}}),
                         '分组名重复')
        self.assertEqual(proto.failure_reason({'success': True, 'data': {}}),
                         '平台接口返回失败')

    def test_json_body_is_compact_and_utf8(self):
        self.assertEqual(proto.json_body({'a': 1, 'b': '中文'}), '{"a":1,"b":"中文"}')


# --------------------------------------------------------------------------- 服务 / HTTP


class QuickReplyPageTest(unittest.TestCase):
    """共用的脚手架（自挂本模块路由，不依赖 main.py 是否已挂）。"""

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
        app.include_router(qr_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('quickreply','x',1)"))
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


class QuickReplyReadTest(QuickReplyPageTest):
    """只读：列表、缺键、平台拒绝、凭据与鉴权。"""

    def test_list_returns_groups(self):
        data = self.client.get(f'{BASE}/{ACC}/groups', headers=self.headers).json()['data']
        self.assertEqual(data['api'], proto.LIST_API)
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['total'], 2)
        self.assertTrue(data['total_from_platform'])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['groups'][0]['group_name'], '常用')

    def test_list_missing_key_is_null_with_warning(self):
        self.fake.payloads[proto.LIST_API] = LIST_MISSING
        data = self.client.get(f'{BASE}/{ACC}/groups', headers=self.headers).json()['data']
        self.assertIsNone(data['groups'])
        self.assertIsNone(data['count'])
        self.assertIsNone(data['total'])
        self.assertEqual([w['code'] for w in data['warnings']], ['list_key_missing'])

    def test_list_platform_refusal_is_502_with_human_hint(self):
        self.fake.payloads[proto.LIST_API] = NO_PERMISSION
        resp = self.client.get(f'{BASE}/{ACC}/groups', headers=self.headers)
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.LIST_API)
        self.assertIn('不是登录掉线', payload['message'])
        self.assertEqual(self.login_state(), 'active')

    def test_list_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.LIST_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        resp = self.client.get(f'{BASE}/{ACC}/groups', headers=self.headers)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_list_without_cookie_is_409_and_sends_nothing(self):
        db.execute('UPDATE account_credentials SET cookie = ? WHERE account_id = ?', ('', ACC))
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.client.get(f'{BASE}/{ACC}/groups', headers=self.headers)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_list_unknown_account_is_404(self):
        resp = self.client.get(f'{BASE}/nobody/groups', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_list_requires_token(self):
        self.assertEqual(self.client.get(f'{BASE}/{ACC}/groups').status_code, 401)


class QuickReplyGroupsPagingTest(QuickReplyPageTest):
    """话术列表：平台一次给全部 ⇒ 服务端内存切页；非法分页零平台调用（第 59 批）。"""

    def groups(self, query: str = '') -> Dict[str, Any]:
        resp = self.client.get(f'{BASE}/{ACC}/groups{query}', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()['data']

    def test_without_paging_params_returns_the_whole_list(self):
        data = self.groups()
        self.assertEqual(len(data['groups']), 2)
        self.assertEqual(data['items'], data['groups'])
        self.assertEqual((data['page'], data['size'], data['pages']), (1, 2, 1))
        self.assertEqual((data['count'], data['total']), (2, 2))

    def test_paging_slices_in_memory_without_a_second_platform_call(self):
        data = self.groups('?page=2&size=1')
        self.assertEqual([g['group_id'] for g in data['groups']], ['9002'])
        self.assertEqual(data['groups'], data['items'])
        self.assertEqual((data['page'], data['size'], data['pages']), (2, 1, 2))
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['total'], 2)
        self.assertTrue(data['total_from_platform'])
        self.assertEqual(len(self.fake.calls), 1)

    def test_illegal_paging_is_400_with_zero_platform_calls(self):
        for query in ('?page=0', '?size=51', '?page=abc'):
            with self.subTest(query=query):
                resp = self.client.get(f'{BASE}/{ACC}/groups{query}', headers=self.headers)
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()['code'], 'invalid_fields')
                self.assertEqual(self.fake.calls, [])

    def test_missing_list_key_stays_null_even_with_paging(self):
        self.fake.payloads[proto.LIST_API] = LIST_MISSING
        data = self.groups('?page=1&size=10')
        self.assertIsNone(data['groups'])
        self.assertIsNone(data['items'])
        self.assertIsNone(data['count'])
        self.assertIsNone(data['total'])
        self.assertIsNone(data['pages'])


class QuickReplyConfirmGateTest(QuickReplyPageTest):
    """写路径第一道闸门：没点头就一个平台请求都不许发。"""

    def test_every_write_route_refuses_without_confirm(self):
        for method, path, body in WRITE_ROUTES:
            payload = {k: v for k, v in body.items() if k != 'confirm'}
            resp = self.post(path, payload)
            self.assertEqual(resp.status_code, 400, f'{path} {resp.text}')
            self.assertEqual(resp.json()['code'], 'confirmation_required', path)
        self.assertEqual(self.fake.calls, [])

    def test_false_like_confirm_is_not_consent(self):
        for bad in (False, 0, '', 'false', 'no', None):
            resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': bad})
            self.assertEqual(resp.status_code, 400, repr(bad))
            self.assertEqual(resp.json()['code'], 'confirmation_required', repr(bad))
        self.assertEqual(self.fake.calls, [])

    def test_confirm_true_actually_sends(self):
        self.ok_data(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True})
        self.assertEqual(self.fake.apis(), [proto.GROUP_API])


class QuickReplyValidationTest(QuickReplyPageTest):
    """字段校验在建客户端之前：纯本地、免费，掉线时也该先说字段哪里不对。"""

    def test_blank_group_name_is_400_and_sends_nothing(self):
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '   ', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertIn('分组名称不能为空', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_too_long_group_name_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '字' * 11, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('不能超过 10 个字', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_too_long_content_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/phrases',
                         {'group_id': 9001, 'content': '字' * 201, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('不能超过 200 个字', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_missing_group_id_on_phrase_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/phrases', {'content': '在的', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('分组 id不能为空', resp.json()['message'])
        self.assertEqual(self.fake.calls, [])

    def test_field_validation_runs_before_credential_check(self):
        """空 Cookie + 空分组名 ⇒ 400 invalid_fields（不是 409），且零平台调用。"""
        db.execute('UPDATE account_credentials SET cookie = ? WHERE account_id = ?', ('', ACC))
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_write_action_is_not_reachable_over_http(self):
        """HTTP 层只暴露 6 个动作；乱传路径段不该变成写请求。"""
        resp = self.post(f'{BASE}/{ACC}/groups/9001/rename', {'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])


class QuickReplyHappyPathTest(QuickReplyPageTest):
    """写成功路径：payload 逐字、审计与事件、以及「没回显 id」的诚实处置。"""

    def test_create_group(self):
        from backend.app.core import events
        before = len(events.recent(limit=500))
        data = self.ok_data(f'{BASE}/{ACC}/groups', {'group_name': ' 常用 ', 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.GROUP_API)['data'],
                         {'groupName': '常用', 'operateType': 'create'})
        self.assertEqual(data['action'], 'create')
        self.assertTrue(data['saved'])
        self.assertEqual(data['group_id'], '9001')
        self.assertEqual(data['submitted'], {'groupName': '常用', 'operateType': 'create'})
        self.assertEqual(data['warnings'], [])
        self.assertEqual([row['action'] for row in self.operations()],
                         ['quick_reply_group_create'])
        self.assertEqual(self.topics(before), ['quick_reply.changed'])

    def test_update_group_keeps_given_id_in_path(self):
        data = self.ok_data(f'{BASE}/{ACC}/groups/9001',
                            {'group_name': '常用话术', 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.GROUP_API)['data'],
                         {'groupId': 9001, 'groupName': '常用话术', 'operateType': 'update'})
        self.assertEqual(data['action'], 'update')
        self.assertTrue(data['saved'])

    def test_delete_group_reports_deleted(self):
        self.fake.payloads[proto.GROUP_API] = GROUP_DELETE_OK
        data = self.ok_data(f'{BASE}/{ACC}/groups/9001/delete', {'confirm': True})
        self.assertEqual(self.fake.call_of(proto.GROUP_API)['data'],
                         {'groupId': 9001, 'operateType': 'delete'})
        self.assertEqual(data['action'], 'delete')
        self.assertTrue(data['deleted'])
        self.assertEqual([row['action'] for row in self.operations()],
                         ['quick_reply_group_delete'])

    def test_create_phrase(self):
        data = self.ok_data(f'{BASE}/{ACC}/phrases',
                            {'group_id': 9001, 'content': '在的，稍等我看一下～',
                             'confirm': True})
        self.assertEqual(self.fake.call_of(proto.CONTENT_API)['data'],
                         {'groupId': 9001, 'content': '在的，稍等我看一下～',
                          'operateType': 'create'})
        self.assertTrue(data['saved'])
        self.assertEqual(data['content_id'], '11')

    def test_update_phrase(self):
        self.fake.payloads[proto.CONTENT_API] = CONTENT_UPDATE_OK
        data = self.ok_data(f'{BASE}/{ACC}/phrases/11',
                            {'group_id': 9001, 'content': '在的，马上帮您看', 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.CONTENT_API)['data'],
                         {'groupId': 9001, 'contentId': 11, 'content': '在的，马上帮您看',
                          'operateType': 'update'})
        self.assertEqual(data['action'], 'update')
        self.assertTrue(data['saved'])

    def test_delete_phrase_reports_deleted(self):
        self.fake.payloads[proto.CONTENT_API] = CONTENT_DELETE_OK
        data = self.ok_data(f'{BASE}/{ACC}/phrases/11/delete',
                            {'group_id': 9001, 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.CONTENT_API)['data'],
                         {'groupId': 9001, 'contentId': 11, 'operateType': 'delete'})
        self.assertTrue(data['deleted'])
        self.assertEqual([row['action'] for row in self.operations()],
                         ['quick_reply_phrase_delete'])

    def test_missing_group_id_echo_is_saved_false_with_warning(self):
        """平台没回显 id ⇒ 不谎报成功也不谎报失败：200 + saved=False + 中文 warning。"""
        self.fake.payloads[proto.GROUP_API] = GROUP_NO_ECHO
        data = self.ok_data(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True})
        self.assertFalse(data['saved'])
        self.assertIsNone(data['group_id'])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_group_id'])
        self.assertIn('已经发出', data['warnings'][0]['message'])
        self.assertEqual([row['success'] for row in self.operations()], [0])

    def test_missing_content_id_echo_is_saved_false_with_warning(self):
        self.fake.payloads[proto.CONTENT_API] = CONTENT_NO_ECHO
        data = self.ok_data(f'{BASE}/{ACC}/phrases',
                            {'group_id': 9001, 'content': '在的', 'confirm': True})
        self.assertFalse(data['saved'])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_content_id'])


class QuickReplyFailureTest(QuickReplyPageTest):
    """失败路径：平台拒绝 / 登录态过期 / 账号不存在 / 协议层缺失。"""

    def test_platform_refusal_is_502_and_does_not_touch_login_state(self):
        self.fake.payloads[proto.GROUP_API] = NO_PERMISSION
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.GROUP_API)
        self.assertIn('不是登录掉线', payload['message'])
        self.assertEqual(self.login_state(), 'active')
        self.assertEqual([row['success'] for row in self.operations()], [0])

    def test_client_layer_failure_is_502(self):
        self.fake.payloads[proto.CONTENT_API] = CLIENT_ERROR
        resp = self.post(f'{BASE}/{ACC}/phrases',
                         {'group_id': 9001, 'content': '在的', 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('非法请求', resp.json()['message'])

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.GROUP_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_missing_cookie_on_write_is_409_and_sends_nothing(self):
        db.execute('UPDATE account_credentials SET cookie = ? WHERE account_id = ?', ('', ACC))
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_on_write_is_404(self):
        resp = self.post(f'{BASE}/nobody/groups', {'group_name': '常用', 'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_protocol_module_without_mtop_is_503(self):
        module = types.ModuleType('backend.app.xianyu.client')

        def factory(account_id: str) -> Any:
            return object()  # 没有 mtop

        module.get_client = factory  # type: ignore[attr-defined]
        module.XianyuClient = object  # type: ignore[attr-defined]
        sys.modules['backend.app.xianyu.client'] = module
        resp = self.post(f'{BASE}/{ACC}/groups', {'group_name': '常用', 'confirm': True})
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()['code'], 'xianyu_not_ready')


class QuickReplyRouteTest(QuickReplyPageTest):
    """路由形状：1 条 GET + 6 条 POST，方法不对就是 405。"""

    def test_methods(self):
        spec = {}
        # 先把假协议层摘掉：`create_app()` 会 import 全部路由模块，其中财务服务在
        # import 期就读 `xianyu.client` 的常量，假模块没有那些常量。
        remove_fake_protocol()
        from backend.app.main import create_app
        for route in create_app().openapi()['paths'].items():
            path, ops = route
            if path.startswith(BASE):
                spec[path] = set(ops)
        self.assertEqual(spec.get(f'{BASE}/{{account_id}}/groups'), {'get', 'post'})
        self.assertEqual(spec.get(f'{BASE}/{{account_id}}/groups/{{group_id}}'), {'post'})
        self.assertEqual(spec.get(f'{BASE}/{{account_id}}/groups/{{group_id}}/delete'), {'post'})
        self.assertEqual(spec.get(f'{BASE}/{{account_id}}/phrases'), {'post'})
        self.assertEqual(spec.get(f'{BASE}/{{account_id}}/phrases/{{content_id}}'), {'post'})
        self.assertEqual(spec.get(f'{BASE}/{{account_id}}/phrases/{{content_id}}/delete'), {'post'})

    def test_get_on_write_route_is_405(self):
        resp = self.client.get(f'{BASE}/{ACC}/groups/9001/delete', headers=self.headers)
        self.assertEqual(resp.status_code, 405, resp.text)

    def test_router_is_registered_in_route_modules(self):
        remove_fake_protocol()
        from backend.app import main
        self.assertIn('quick_reply', main.ROUTE_MODULES)


if __name__ == '__main__':
    unittest.main(verbosity=2)
