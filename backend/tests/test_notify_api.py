"""通知中心（`/api/notifications`）只读接口的服务层 + HTTP 契约测试。

**为什么单开一组**：通知中心是「看着像有列表、其实没有」的典型 ——

1. 真机页面上的通知条目（「超时未付款，系统关闭了订单」…）**不是 REST 拉来的**：
   18 秒抓包期间没有任何列表接口被调用，条目由 IM/ACCS 长连接推送。
   REST 侧只有频道树 + 未读总数，所以 `items` 只能是 `[]` + 中文 warning，
   **不许造条目**；
2. 标记已读（`mtop.taobao.idlemessage.pc.notifys.read`）现在是**真写接口**：
   `POST /api/notifications/{account_id}/read`，body `{message_id, confirm}`；
   未确认 ⇒ 400 `confirmation_required` 且**零平台请求**，平台没明确回执 ⇒
   `saved=false` + warning（**不谎报已读**）；只读路径（GET）仍然一个写接口都不许调；
3. 真机未读 `total: 0` 是**平台明说的 0**（可以照用），
   而响应里没有 `total` 键时必须给 `null`，不能顺手写成 0。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_notify_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
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

from backend.app import main as app_main  # noqa: E402
from backend.app.api import notify as notify_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import notify as notify_proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'notify_acc'
GHOST = 'notify_ghost'
BASE = '/api/notifications'

SESSIONS_API = notify_proto.NOTIFY_SESSIONS_API
REDPOINT_API = notify_proto.REDPOINT_QUERY_API

#: 真机 `notify.sessions.list`（2026-10-10 账号 2217970177348）的频道树（截取结构）。
#: 注意它**没有业务信封**：外层 `data` 直接就是 `{notifyInfoList, success}`。
NOTIFY_SESSIONS = {
    'notifyInfoList': [
        {'icon': 'https://img.alicdn.com/a.png', 'key': 'all',
         'sessionId': '66443628497', 'title': '全部'},
        {'key': 'importance', 'sessionId': '67101460137', 'title': '重要'},
        {'key': 'trade', 'title': '交易履约', 'children': [
            {'key': 'order', 'sessionId': '66442852712', 'title': '订单'},
            {'key': 'after_sales', 'sessionId': '67714393045', 'title': '售后'},
        ]},
    ],
    'success': True,
}
#: 真机 `redpoint.query`：`total: 0` 是平台真值。
REDPOINT = {'needDecryptKeys': [], 'needDecryptKeysV2': [], 'total': 0}

WRITE_API = notify_proto.NOTIFYS_READ_API
#: 真机请求原文（`logs/seller_discover/notify_capture2.json:93-95`）里的 `messageId`。
MSG_ID = '4338118620042.PNM'
#: 平台**明确**回执 `success: true`（mtop 的 `data` 段）——
#: 真机原文见 `logs/notify_read58/07b_raw_mtop_response.json`。
MARK_READ_ECHO = {'success': True}
#: 平台沉默：业务体里没有 `success` 字段 —— **不等于成功**。
MARK_READ_SILENT = {}
#: 平台自己说没成功（业务体 `success: false`）。
MARK_READ_SAYS_FALSE = {'success': False, 'message': '标记已读失败'}
#: 业务 `code` 不是 success（平台原话在 `message`）。
MARK_READ_BIZ_FAIL = {'code': 'FAIL_BIZ_NOTIFY_READ', 'message': '通知不存在'}
#: 真机原文：无权限（信封层失败）。
MARK_READ_NO_PERMISSION = {
    'success': False,
    'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
    'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'],
}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeNotifyClient:
    """假协议层：按 **mtop 接口名**回话，记录每次 (api, data)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None,
                 envelopes: Optional[Dict[str, Any]] = None):
        self.payloads = dict(payloads or {})
        self.errors = dict(errors or {})
        #: 整个客户端信封原样返回（模拟 `success: false` 的信封层失败）。
        self.envelopes = dict(envelopes or {})
        self.calls: List[tuple] = []
        self.account_id = ''

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   **kwargs: Any) -> Dict[str, Any]:
        self.calls.append((api, data))
        error = self.errors.get(api)
        if error is not None:
            raise error
        if api in self.envelopes:
            return dict(self.envelopes[api])
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有为 {api} 准备响应')
        return {'success': True, 'data': self.payloads[api], 'message': '',
                'ret': ['SUCCESS::调用成功'], 'cookies_str': ''}

    def apis(self) -> List[str]:
        return [call[0] for call in self.calls]


def install_fake_protocol(client: FakeNotifyClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeNotifyClient:
        # `common._note_session_expired` 靠 `client.account_id` 找到要标记的账号
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


def full_client(**overrides: Any) -> FakeNotifyClient:
    payloads = {SESSIONS_API: NOTIFY_SESSIONS, REDPOINT_API: REDPOINT,
                WRITE_API: MARK_READ_ECHO}
    payloads.update(overrides)
    return FakeNotifyClient(payloads)


class NotifyApiTest(unittest.TestCase):
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
        # （真实挂载由 test_real_app_mounts_notify_router 单独锁）。
        app = FastAPI()
        app.include_router(notify_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('notifyapi','x',1)"))
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

    def expect_fail(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        """断言失败信封。

        **名字不能叫 `fail`**：`unittest.TestCase.fail(msg)` 是 `assertIsNone` /
        `assertTrue` 等断言的底层出口，同名覆盖会让断言失败时拿文案当 URL 发请求
        （踩过一次，报出 `AssertionError: 404 != 400`，真原因被盖掉）。
        """
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    def post(self, path: str, body: Dict[str, Any],
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body,
                                headers=self.headers if headers is None else headers)

    def post_ok(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def post_fail(self, path: str, body: Dict[str, Any], status: int = 400) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    def audit_rows(self) -> List[Any]:
        return db.query("SELECT action, item_id, success FROM operation_logs ORDER BY id") \
            if hasattr(db, 'query') else []

    # -- 形状 ------------------------------------------------------------

    def test_notifications_channels_and_unread(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertEqual(data['account_id'], ACC)
        self.assertEqual(data['channels'], NOTIFY_SESSIONS['notifyInfoList'])
        self.assertEqual(data['unread'], {'total': 0})  # 平台明说的 0，照用
        self.assertEqual(data['items'], [])  # REST 没有列表接口 → 空，不造条目
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['page_size'], 20)
        self.assertTrue(data['fetched_at'])

    def test_notifications_always_warns_that_entries_come_from_im(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertTrue(any('长连接' in w for w in data['warnings']), data['warnings'])

    def test_notifications_page_and_size_are_validated_and_echoed(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}', '?page=3&size=50')
        self.assertEqual((data['page'], data['page_size']), (3, 50))
        # page/size 给了但不合法一律 400 —— page=0 早先被 max(1,...) 吞成 1，是错的
        for query in ('?size=0', '?size=101', '?size=x', '?page=y',
                      '?page=0', '?page=-1'):
            body = self.expect_fail(f'{BASE}/{ACC}', query, status=400)
            self.assertEqual(body['code'], 'invalid_fields', body)

    def test_notifications_missing_channel_key_is_null_with_warning(self):
        """缺 `notifyInfoList` 键 ≠ 「没有频道」→ null + warning，不补空数组。"""
        seed_account()
        self.fake.payloads[SESSIONS_API] = {'success': True}
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertIsNone(data['channels'])
        self.assertTrue(any('notifyInfoList' in w for w in data['warnings']), data['warnings'])

    def test_notifications_empty_channels_array_is_empty_not_null(self):
        """平台**明确给了空数组** → `[]`；缺键才是 null（与上一档区分）。"""
        seed_account()
        self.fake.payloads[SESSIONS_API] = {'notifyInfoList': [], 'success': True}
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertEqual(data['channels'], [])
        self.assertFalse(any('notifyInfoList' in w for w in data['warnings']), data['warnings'])

    def test_notifications_unread_missing_is_null_not_zero(self):
        seed_account()
        self.fake.payloads[REDPOINT_API] = {'needDecryptKeys': []}
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertIsNone(data['unread']['total'])
        self.assertTrue(any('不是 0' in w for w in data['warnings']), data['warnings'])

    def test_notifications_unread_zero_is_kept_as_zero(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertEqual(data['unread']['total'], 0)
        self.assertFalse(any('不是 0' in w for w in data['warnings']), data['warnings'])

    def test_unread_failure_does_not_kill_the_whole_page(self):
        """未读数接口挂了 → 频道照给，unread.total 是 null + warning，不是整页 502。"""
        seed_account()
        self.fake.errors[REDPOINT_API] = RuntimeError('rgv587')
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertEqual(data['channels'], NOTIFY_SESSIONS['notifyInfoList'])
        self.assertIsNone(data['unread']['total'])
        self.assertTrue(any('未读数' in w for w in data['warnings']), data['warnings'])

    # -- 失败口径 --------------------------------------------------------

    def test_notifications_unknown_account_is_404(self):
        body = self.expect_fail(f'{BASE}/{GHOST}', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])  # 一次平台都不打

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        body = self.expect_fail(f'{BASE}/{ACC}', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        body = self.expect_fail(f'{BASE}/{ACC}', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_409_and_writes_need_relogin(self):
        seed_account()
        self.fake.errors[SESSIONS_API] = SessionExpiredError('登录态已过期')
        body = self.expect_fail(f'{BASE}/{ACC}', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_business_error_inside_envelope_is_502(self):
        seed_account()
        self.fake.payloads[SESSIONS_API] = {'code': 'FAIL_BIZ_X', 'msg': '没有权限',
                                            'data': None}
        body = self.expect_fail(f'{BASE}/{ACC}', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('没有权限', body['message'])

    def test_unauthenticated_is_401(self):
        seed_account()
        resp = self.get(f'{BASE}/{ACC}', headers={})
        self.assertEqual(resp.status_code, 401)

    def test_protocol_unavailable_is_503_not_ready(self):
        """协议层缺失（ImportError）→ 503 `xianyu_not_ready`，不是 500。"""
        seed_account()
        sys.modules['backend.app.xianyu.client'] = None  # 强制 ImportError
        try:
            body = self.expect_fail(f'{BASE}/{ACC}', status=503)
            self.assertEqual(body['code'], 'xianyu_not_ready')
            self.assertEqual(self.fake.calls, [])
        finally:
            install_fake_protocol(self.fake)

    def test_protocol_function_missing_is_503_not_500(self):
        """协议模块在、函数不在（半升级）→ 503 `xianyu_not_ready`，不是 AttributeError→500。"""
        seed_account()
        original = notify_proto.notify_channels
        notify_proto.notify_channels = None
        try:
            body = self.expect_fail(f'{BASE}/{ACC}', status=503)
            self.assertEqual(body['code'], 'xianyu_not_ready')
            self.assertEqual(self.fake.calls, [])
        finally:
            notify_proto.notify_channels = original

    # -- 写：标记已读（POST /{account_id}/read） -------------------------

    def test_mark_read_protocol_request_matches_the_real_capture(self):
        """请求形状照真机原文：`{"messageId": "..."}`，版本 1.0，且**没 confirm 就构造不出来**。"""
        with self.assertRaises(notify_proto.WriteNotConfirmed):
            notify_proto.mark_read_request(MSG_ID)
        api, data, version = notify_proto.mark_read_request(MSG_ID, confirm=True)
        self.assertEqual(api, 'mtop.taobao.idlemessage.pc.notifys.read')
        self.assertEqual(data, {'messageId': MSG_ID})
        self.assertEqual(version, '1.0')
        for bad in ('', '   ', None):
            with self.assertRaises(ValueError):
                notify_proto.mark_read_request(bad, confirm=True)
        with self.assertRaises(ValueError):
            notify_proto.mark_read_request('x' * 129, confirm=True)

    def test_mark_read_needs_confirm_and_sends_nothing(self):
        """红线：未确认时**一个平台请求都不发**（`1`/`'true'` 之外的都算没确认）。"""
        seed_account()
        for bad in (None, False, 'false', 'no', '0', 0, ''):
            body = self.post_fail(f'{BASE}/{ACC}/read',
                                  {'message_id': MSG_ID, 'confirm': bad})
            self.assertEqual(body['code'], 'confirmation_required', body)
        self.assertEqual(self.fake.calls, [])

    def test_mark_read_gate_runs_before_the_account_lookup(self):
        """闸门在第一行：账号根本不存在 + 没确认 ⇒ 400 `confirmation_required`（不是 404）。"""
        body = self.post_fail(f'{BASE}/{GHOST}/read', {'message_id': MSG_ID})
        self.assertEqual(body['code'], 'confirmation_required', body)
        self.assertEqual(self.fake.calls, [])

    def test_mark_read_platform_echo_true_is_saved_and_audited(self):
        seed_account()
        self.fake.payloads[WRITE_API] = MARK_READ_ECHO
        data = self.post_ok(f'{BASE}/{ACC}/read', {'message_id': MSG_ID, 'confirm': True})
        self.assertEqual(self.fake.apis(), [WRITE_API])  # 只打这一条，且是登记过的写接口
        self.assertEqual(self.fake.calls[0][1], {'messageId': MSG_ID})
        self.assertEqual(data['action'], 'notify_mark_read')
        self.assertTrue(data['saved'])
        self.assertIs(data['platform_success'], True)
        self.assertEqual(data['warnings'], [])
        row = db.query_one('SELECT action, item_id, success FROM operation_logs '
                           'ORDER BY id DESC LIMIT 1')
        self.assertEqual((row['action'], row['item_id'], row['success']),
                         ('notify_mark_read', MSG_ID, 1))

    def test_mark_read_platform_silence_is_not_reported_as_saved(self):
        """平台没给 `success` 字段 ⇒ `saved=false` + warning，**且不记审计**（不谎报已读）。"""
        seed_account()
        self.fake.payloads[WRITE_API] = MARK_READ_SILENT
        data = self.post_ok(f'{BASE}/{ACC}/read', {'message_id': MSG_ID, 'confirm': True})
        self.assertFalse(data['saved'])
        self.assertIsNone(data['platform_success'])
        self.assertTrue(any('可能没有被标记已读' in w['message'] for w in data['warnings']),
                        data['warnings'])
        self.assertIsNone(db.query_one('SELECT id FROM operation_logs LIMIT 1'))

    def test_mark_read_platform_says_false_is_502_with_platform_words(self):
        seed_account()
        self.fake.payloads[WRITE_API] = MARK_READ_SAYS_FALSE
        body = self.post_fail(f'{BASE}/{ACC}/read',
                              {'message_id': MSG_ID, 'confirm': True}, status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('标记已读失败', body['message'])
        self.assertIsNone(db.query_one('SELECT id FROM operation_logs LIMIT 1'))

    def test_mark_read_business_error_code_is_502(self):
        seed_account()
        self.fake.payloads[WRITE_API] = MARK_READ_BIZ_FAIL
        body = self.post_fail(f'{BASE}/{ACC}/read',
                              {'message_id': MSG_ID, 'confirm': True}, status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('通知不存在', body['message'])

    def test_mark_read_envelope_failure_is_502_and_keeps_login_state(self):
        """平台信封失败（无权限）⇒ 502 + 平台原话，**不改 `login_state`**（无权限 ≠ 掉线）。"""
        seed_account()
        self.fake.envelopes[WRITE_API] = MARK_READ_NO_PERMISSION
        body = self.post_fail(f'{BASE}/{ACC}/read',
                              {'message_id': MSG_ID, 'confirm': True}, status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('无权限访问', body['message'])
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'active')

    def test_mark_read_empty_message_id_is_400_and_sends_nothing(self):
        seed_account()
        for bad in ('', '   ', None):
            body = self.post_fail(f'{BASE}/{ACC}/read',
                                  {'message_id': bad, 'confirm': True})
            self.assertEqual(body['code'], 'invalid_fields', body)
        self.assertEqual(self.fake.calls, [])

    def test_mark_read_missing_cookie_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        body = self.post_fail(f'{BASE}/{ACC}/read',
                              {'message_id': MSG_ID, 'confirm': True}, status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_mark_read_session_expired_is_409_and_writes_need_relogin(self):
        seed_account()
        self.fake.errors[WRITE_API] = SessionExpiredError('登录态已过期')
        body = self.post_fail(f'{BASE}/{ACC}/read',
                              {'message_id': MSG_ID, 'confirm': True}, status=409)
        self.assertEqual(body['code'], 'verification_required')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_mark_read_unauthenticated_is_401(self):
        seed_account()
        resp = self.post(f'{BASE}/{ACC}/read', {'message_id': MSG_ID, 'confirm': True},
                         headers={})
        self.assertEqual(resp.status_code, 401)

    # -- 路由面与只读面 --------------------------------------------------

    def test_notify_routes_are_one_read_plus_one_confirmed_write(self):
        """路由面：一个 GET（只读）+ 一个 POST（写，服务层第一行就是 confirm 闸门）。"""
        paths: Dict[str, set] = {}
        for route in notify_api.router.routes:
            path = getattr(route, 'path', '')
            if path:
                paths[path] = set(getattr(route, 'methods', set()) or set())
        self.assertEqual(paths, {
            f'{BASE}/{{account_id}}': {'GET'},
            f'{BASE}/{{account_id}}/read': {'POST'},
        })

    def test_read_path_never_calls_a_write_api(self):
        """只读路径：所有 mtop 调用都必须落在只读清单里（尤其不能标记已读）。"""
        seed_account()
        self.ok_data(f'{BASE}/{ACC}')
        self.assertIn(SESSIONS_API, self.fake.apis())
        self.assertIn(REDPOINT_API, self.fake.apis())
        for api in self.fake.apis():
            self.assertIn(api, notify_proto.READ_APIS, f'调了非只读接口: {api}')
            self.assertNotIn(api, notify_proto.WRITE_APIS, f'调了写接口: {api}')

    def test_write_api_is_registered_as_a_write_api(self):
        seed_account()
        self.fake.payloads[WRITE_API] = MARK_READ_ECHO
        self.post_ok(f'{BASE}/{ACC}/read', {'message_id': MSG_ID, 'confirm': True})
        self.assertEqual(self.fake.apis(), [WRITE_API])
        self.assertIn(WRITE_API, notify_proto.WRITE_APIS)
        self.assertNotIn(WRITE_API, notify_proto.READ_APIS)
        self.assertEqual(notify_proto.WRITE_VERSIONS[WRITE_API], '1.0')

    def test_real_app_mounts_notify_router(self):
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
        self.assertIn(f'{BASE}/{{account_id}}', paths,
                      'create_app() 没挂上通知路由（main.py 的 ROUTE_MODULES 需要 notify）')
        self.assertIn(f'{BASE}/{{account_id}}/read', paths,
                      'create_app() 没挂上标记已读路由（POST .../read）')
        # 老路径不能再存在（前端按 /api/notifications 写页面）
        self.assertNotIn('/api/notify/{account_id}', paths)


if __name__ == '__main__':
    unittest.main(verbosity=2)
