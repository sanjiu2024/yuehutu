"""账号 ↔ IM 入站生命周期的回归锁（task-17：C1/B1/B2/C4）。

**为什么需要**：用户抱怨「对话区同步不过来对话」。审计（`docs/audit/chats.md` §2、
`docs/audit/accounts.md` §5）查到的真因不是 WS 总线也不是前端订阅 —— 而是
`im_service.start` 的生产调用点**只有** `backend/app/main.py:117`（启动那一刻筛
`enabled=1` 一次性挂载）。审计打桩实测的三行原样输出：

    PATCH enabled=true 后  --- im_service.active_accounts (启用后): []
    POST relogin 后        --- im_service.active_accounts (relogin 后): []
    DELETE 账号后          --- im_service.active_accounts (删除后): []

再加一条：`xianyu.client.SessionExpiredError`（`backend/app/xianyu/client.py:52`）
全仓无生产捕获，被 `common.call_method` 统一降级成通用 `protocol_error`，前端永远
显示「已登录」。本文件把这几条锁死：**登录/启用/重登要真挂上、停用/删除要真摘掉、
Cookie 过期要回写 `need_relogin` 并把事件发出去**。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_accounts_im.py"

不打真实闲鱼平台、不调真实 LLM（协议客户端全部打桩）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import accounts as accounts_api  # noqa: E402
from backend.app.core import db, events, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import common, im_service  # noqa: E402
from backend.app.xianyu import store  # noqa: E402
from backend.app.xianyu.client import SessionExpiredError  # noqa: E402

EXPIRED_PLATFORM_TEXT = 'FAIL_SYS_SESSION_EXPIRED::Session过期'


class FakeSession:
    """假 IM 会话：够 `im_service` / `chats.attach_im_session` 用。"""

    def __init__(self) -> None:
        self.connected = True
        self.closed = False
        self.handlers = []

    def on_message(self, handler):
        if handler not in self.handlers:
            self.handlers.append(handler)
        return handler

    def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeClient:
    """假协议客户端。

    `ensure_im()` 每次调用都**真的新建**一条会话并计数 —— 只有这样才测得出
    `im_service.start` 的幂等短路（真机上重复建连 = 同一条买家消息收多份）。
    """

    def __init__(self, account_id: str) -> None:
        self.cookie_id = account_id
        self.account_id = account_id
        self.cookie_str = f'unb={account_id}; _m_h5_tk=stub'
        self.hooks = []
        self.sessions_created = 0
        #: 设成异常对象后，所有协议调用都抛它（模拟 Cookie 过期）
        self.fail_with = None
        #: 记下真正发出去的 `(cid, text)`，用来锁「发到平台的那个会话 id」
        self.sent = []

    def on_im_session(self, hook):
        if hook not in self.hooks:
            self.hooks.append(hook)
        return hook

    def ensure_im(self):
        if self.fail_with is not None:
            raise self.fail_with
        self.sessions_created += 1
        session = FakeSession()
        for hook in list(self.hooks):
            hook(session)
        return session

    def send_message(self, cid, text, toid=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((cid, text))
        return {'msg_id': 'stub_msg'}


class AccountsImTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from backend.tests.testenv import use_temp_app_dir

        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        cls.tmp.cleanup()

    # ------------------------------------------------------------------ 夹具

    def setUp(self):
        db.reset_connection()
        for account_id in im_service.active_accounts():
            im_service.forget(account_id)
        self._wipe()

        self.clients = {}
        self._real_get_client = common.get_client
        self._real_start = im_service.start
        self._real_stop = im_service.stop
        self._real_qr_login = accounts_api.qr_login
        common.get_client = self._fake_get_client
        im_service.start = self._real_start
        im_service.stop = self._real_stop

        # lifespan 里的 im-bootstrap 也是后台线程：先起 app（此刻库里没有账号），
        # 再插数据，避免它替我们把账号挂上、掩盖掉被测的调用点。
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('im_acc','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(user_id)}

    def tearDown(self):
        common.get_client = self._real_get_client
        im_service.start = self._real_start
        im_service.stop = self._real_stop
        accounts_api.qr_login = self._real_qr_login
        for account_id in im_service.active_accounts():
            im_service.forget(account_id)
        self.app_ctx.__exit__(None, None, None)
        self._wipe()

    def _wipe(self) -> None:
        for table in ('messages', 'conversations', 'account_credentials', 'accounts',
                      'risk_state', 'risk_events', 'operation_logs', 'users'):
            try:
                db.execute(f'DELETE FROM {table}')
            except Exception:  # noqa: BLE001 - 表可能还没建（首次跑）
                pass

    def _fake_get_client(self, account_id):
        client = self.clients.get(account_id)
        if client is None:
            client = self.clients[account_id] = FakeClient(account_id)
        return client, None

    def _account(self, account_id: str, *, enabled: bool = False, cookie: str = '') -> None:
        store.upsert_account(
            account_id=account_id, nickname='测试号', xianyu_user_id=account_id,
            cookie=cookie or f'unb={account_id}; a=1', login_state=store.STATE_ACTIVE,
        )
        store.update_account(account_id, enabled=1 if enabled else 0)

    def _wait(self, predicate, timeout: float = 3.0) -> bool:
        """等后台挂载线程跑完（挂载必须后台做，否则扫码登录的 HTTP 响应会被拖死）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    # ------------------------------------------------- 1. 登录成功必须当场挂载

    def test_qr_login_success_starts_im_without_restart(self):
        """审计原话 `im_service_start_calls_after_login 0` —— 现在必须 ≥ 1。"""
        calls = []

        def counting_start(account_id):
            calls.append(account_id)
            return {'account_id': account_id, 'attached': True, 'connected': True}

        im_service.start = counting_start

        class FakeQR:
            async def start_qr_login(self):
                return {'session_id': 'tk1', 'qr_image_base64': 'x', 'expire_in': 60}

            async def poll_qr_login(self, ticket):
                return {'status': 'success', 'user_id': 'runtime_acc',
                        'cookie': 'unb=runtime_acc; a=1', 'nickname': '运行期登录号'}

        accounts_api.qr_login = FakeQR()

        resp = self.client.get('/api/accounts/qr/tk1', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['state'], 'confirmed')
        self.assertTrue(self._wait(lambda: calls), f'登录成功后没挂 IM：calls={calls}')
        self.assertEqual(calls, ['runtime_acc'])
        self.assertEqual(
            store.get_account('runtime_acc')['login_state'], store.STATE_ACTIVE)

    # ------------------------------------------------- 2. 幂等 + 启用/停用/删除

    def test_start_is_idempotent_and_active_accounts_reflects_it(self):
        """重复 start 不能重复建连（真机上是多条 WebSocket 收同一批消息）。"""
        self._account('acc1')
        first = im_service.start('acc1')
        second = im_service.start('acc1')

        self.assertTrue(first['attached'], first)
        self.assertFalse(first.get('reused'), first)
        self.assertTrue(second['attached'], second)
        self.assertTrue(second.get('reused'), second)
        self.assertEqual(self.clients['acc1'].sessions_created, 1, '重复 start 重复建了连')
        self.assertEqual(im_service.active_accounts(), ['acc1'])

        stopped = im_service.stop('acc1')
        self.assertEqual(stopped['closed'], True, stopped)
        self.assertEqual(im_service.active_accounts(), [])

    def test_patch_enabled_toggles_im_lifecycle(self):
        """`enabled` 必须真的是 IM 入站开关，不是库里一个没人读的数字。"""
        self._account('acc2')
        calls = []
        im_service.start = lambda account_id: calls.append(('start', account_id)) or {
            'account_id': account_id, 'attached': True, 'connected': True}
        im_service.stop = lambda account_id: calls.append(('stop', account_id)) or {
            'account_id': account_id, 'closed': True}

        resp = self.client.patch('/api/accounts/acc2', json={'enabled': True},
                                 headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(self._wait(lambda: calls))
        self.assertEqual(calls, [('start', 'acc2')])

        resp = self.client.patch('/api/accounts/acc2', json={'enabled': False},
                                 headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, [('start', 'acc2'), ('stop', 'acc2')])

        # 只改备注不碰开关：不许顺手挂/摘会话
        resp = self.client.patch('/api/accounts/acc2', json={'remark': '只改备注'},
                                 headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(calls), 2, calls)

    def test_delete_stops_im_and_purges_chats(self):
        """删号：会话要摘掉（active_accounts 变空）、会话/消息要清掉。

        审计 §10 的原样证据是删完还剩 `AFTER_convs [{"id": 1, "account_id": "acc_del"}]`
        与 `AFTER_messages_count 1` —— 这里的 purge 走 task-19 的真实现（不再打桩）。
        """
        self._account('acc3')
        self._account('other_acc')
        self.assertTrue(im_service.start('acc3')['attached'])
        self.assertEqual(im_service.active_accounts(), ['acc3'])
        session = im_service.current_session('acc3')

        db.execute('INSERT INTO conversations(account_id, peer_user_id, item_id) VALUES(?,?,?)',
                   ('acc3', 'buyer9', 'item9'))
        conversation_id = int(db.query_one(
            'SELECT id FROM conversations WHERE account_id = ?', ('acc3',))['id'])
        db.execute('INSERT INTO messages(conversation_id, direction, sender, content) '
                   'VALUES(?,?,?,?)', (conversation_id, 'in', 'buyer', '在吗'))
        # 另一个账号的会话/消息必须完好（purge 不许误伤）
        db.execute('INSERT INTO conversations(account_id, peer_user_id, item_id) VALUES(?,?,?)',
                   ('other_acc', 'buyer7', 'item7'))
        other_id = int(db.query_one('SELECT id FROM conversations WHERE account_id = ?',
                                    ('other_acc',))['id'])
        db.execute('INSERT INTO messages(conversation_id, direction, sender, content) '
                   'VALUES(?,?,?,?)', (other_id, 'in', 'buyer', '别的账号的消息'))

        resp = self.client.delete('/api/accounts/acc3', headers=self.headers)

        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['deleted'], data)
        self.assertTrue(data['purged'], data)
        self.assertEqual(data['counts'], {'conversations': 1, 'messages': 1}, data)
        self.assertEqual(
            db.query_one('SELECT COUNT(*) AS n FROM conversations WHERE account_id = ?',
                         ('acc3',))['n'], 0)
        self.assertEqual(
            db.query_one('SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?',
                         (conversation_id,))['n'], 0)
        self.assertEqual(
            db.query_one('SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?',
                         (other_id,))['n'], 1, 'purge 误删了别的账号的消息')
        self.assertTrue(session.closed, '删号后旧 IM 会话还活着')
        self.assertEqual(im_service.active_accounts(), [])

    def test_purge_missing_is_reported_not_faked(self):
        """`purge_account_data` 还没就绪时必须如实报，不能假装清干净了。"""
        from backend.app.services import chats as chats_service

        original = getattr(chats_service, 'purge_account_data', None)
        if original is not None:
            delattr(chats_service, 'purge_account_data')
        try:
            info = accounts_api._purge_account_chats('acc_x')
        finally:
            if original is not None:
                chats_service.purge_account_data = original
        self.assertFalse(info['purged'], info)
        self.assertIn('error', info)

    # ------------------------------------------------- 3. Cookie 过期回写

    def test_session_expired_marks_need_relogin_and_publishes(self):
        """`SessionExpiredError` 必须留下：库里 need_relogin + `accounts.changed`。"""
        self._account('exp_acc')
        client = FakeClient('exp_acc')
        client.fail_with = SessionExpiredError(EXPIRED_PLATFORM_TEXT)

        seen = []
        real_publish = events.publish
        events.publish = lambda topic, payload=None: seen.append((topic, payload or {}))
        try:
            status, value = common.call_method(client, ('send_message',), 'cid', '你好')
        finally:
            events.publish = real_publish

        self.assertEqual(status, common.CALL_ERROR)
        # 原文不许丢：调用方（chats/deliver_ai_reply）就是靠 str(value) 拼 warning 的
        self.assertIn(EXPIRED_PLATFORM_TEXT, str(value))
        self.assertIn('需要重新登录', str(value))
        self.assertIsInstance(value, SessionExpiredError)
        self.assertIsInstance(value, RuntimeError)

        account = store.get_account('exp_acc')
        self.assertEqual(account['login_state'], store.STATE_NEED_RELOGIN, account)

        topics = [topic for topic, _ in seen]
        self.assertIn('accounts.changed', topics, seen)
        payload = dict(seen[topics.index('accounts.changed')][1])
        self.assertEqual(payload['account_id'], 'exp_acc')
        self.assertEqual(payload['reason'], 'session_expired')
        self.assertEqual(payload['login_state'], store.STATE_NEED_RELOGIN)

        envelope = common.call_failed(status, value)
        self.assertEqual(envelope['code'], 'verification_required', envelope)
        self.assertIn('需要重新登录', envelope['message'])
        self.assertIn(EXPIRED_PLATFORM_TEXT, envelope['message'])
        self.assertEqual(envelope['login_state'], 'need_relogin')

    def test_send_manual_session_expired_surfaces_in_http_response(self):
        """人工回复撞上过期：HTTP 响应里要带得出「需要重新登录」，且原文不丢。"""
        self._account('exp_send')
        db.execute('INSERT INTO conversations(account_id, peer_user_id, item_id) VALUES(?,?,?)',
                   ('exp_send', 'buyer1', 'item1'))
        conversation_id = int(db.query_one(
            'SELECT id FROM conversations WHERE account_id = ?', ('exp_send',))['id'])
        # 平台会话 id（cid）只出现在 IM 帧里、跟着消息落进 `messages.meta`；
        # `chats._platform_cid` 取不到就**不发**（不再拿 peer_user_id 冒充）。
        db.execute('INSERT INTO messages(conversation_id, direction, sender, content, meta) '
                   'VALUES(?,?,?,?,?)',
                   (conversation_id, 'in', 'buyer', '在吗', '{"cid": "cid_exp_send"}'))
        self.clients['exp_send'] = FakeClient('exp_send')
        self.clients['exp_send'].fail_with = SessionExpiredError(EXPIRED_PLATFORM_TEXT)

        resp = self.client.post(f'/api/chats/{conversation_id}/send',
                                json={'content': '在的'}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertFalse(data['sent'], data)
        self.assertIn('需要重新登录', data['warning'])
        self.assertIn(EXPIRED_PLATFORM_TEXT, data['warning'])
        self.assertEqual(data['code'], 'send_failed')
        self.assertEqual(store.get_account('exp_send')['login_state'], store.STATE_NEED_RELOGIN)

    # ------------------------------------------------- 4. relogin 重新挂载

    def test_relogin_restarts_im(self):
        """relogin 之后不许出现「进程里再也没挂过」的状态。"""
        self._account('acc4')
        calls = []
        im_service.start = lambda account_id: calls.append(account_id) or {
            'account_id': account_id, 'attached': True, 'connected': True}

        class FakeQR:
            async def start_qr_login(self):
                return {'session_id': 'tk4', 'qr_image_base64': 'x', 'expire_in': 60}

        accounts_api.qr_login = FakeQR()
        resp = self.client.post('/api/accounts/acc4/relogin', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(self._wait(lambda: calls), f'relogin 后没重新挂 IM：calls={calls}')
        self.assertEqual(calls, ['acc4'])
        self.assertEqual(resp.json()['data']['login_state'], store.STATE_NEED_RELOGIN)


if __name__ == '__main__':
    unittest.main(verbosity=2)
