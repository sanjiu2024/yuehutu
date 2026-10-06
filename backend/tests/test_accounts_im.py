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

再一条（task-22）：cookie 导入/续期恢复的账号从来没有昵称/头像 ——
`client.user_profile()` 全仓只有 `login.py:389` 一个调用点，而那个载荷连
`avatar_url` 都不带（`login.py:397-403`）。本文件同时锁住资料回填：拿到就落库、
空值不覆盖旧值、30 分钟节流、平台报错不影响列表/刷新接口、回填不许碰 cookie。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_accounts_im.py"

不打真实闲鱼平台、不调真实 LLM（协议客户端全部打桩）。
"""
from __future__ import annotations

import asyncio
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
from backend.app.services import accounts as accounts_service  # noqa: E402
from backend.app.services import common, im_service  # noqa: E402
from backend.app.xianyu import store  # noqa: E402
from backend.app.xianyu.client import RiskControlError, SessionExpiredError  # noqa: E402

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
        #: 账号资料（task-22）：`profile_calls` 用来验节流，`profile_error` 用来验「平台报错不影响接口」
        self.profile_calls = 0
        self.profile_loop = None
        self.profile = {'nickname': f'资料-{account_id}',
                        'avatar_url': f'https://img.example/{account_id}.jpg'}
        self.profile_error = None

    def on_im_session(self, hook):
        if hook not in self.hooks:
            self.hooks.append(hook)
        return hook

    async def user_profile(self, user_id=None):
        """真协议层是 `async def`（`backend/app/xianyu/client.py:598`）。

        顺手记下**实际运行所在的 event loop**：`common.call_method` 必须把它丢进
        项目共享循环（`common.shared_loop()`），而不是 `asyncio.run` 一个新 loop ——
        临时 loop 一关，挂在它上面的长连接任务就被销毁（真机表现是「会话建好即死」）。
        """
        self.profile_calls += 1
        self.profile_loop = asyncio.get_running_loop()
        if self.profile_error is not None:
            raise self.profile_error
        return dict(self.profile)

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
        # task-22 的资料节流是模块级缓存：不清掉会让「第二次必须跳过」的用例
        # 被上一个用例的窗口带偏。空 id = 清空全部。
        accounts_service.forget_profile_cache('')

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

    def _account(self, account_id: str, *, enabled: bool = False, cookie: str = '',
                 nickname: str = '测试号') -> None:
        store.upsert_account(
            account_id=account_id, nickname=nickname, xianyu_user_id=account_id,
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

    # ------------------------------------------------- 5. 账号资料回填（task-22）

    def test_refresh_profile_persists_nickname_and_avatar(self):
        """Cookie 导入的账号也必须能拿到昵称/头像（过去只有扫码登录那一瞬会拉）。

        `user_profile` 必须是 `async def` 且跑在**共享事件循环**上：这是项目硬规则
        （`common.run_awaitable` / `common.shared_loop()`），不许 `asyncio.run` 新 loop。
        """
        self._account('prof_acc', nickname='')
        fake = FakeClient('prof_acc')
        self.clients['prof_acc'] = fake

        resp = self.client.post('/api/accounts/prof_acc/refresh-profile', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['account_id'], 'prof_acc')
        self.assertEqual(data['nickname'], '资料-prof_acc')
        self.assertEqual(data['avatar_url'], 'https://img.example/prof_acc.jpg')
        self.assertTrue(data['refreshed'], data)

        account = store.get_account('prof_acc')
        self.assertEqual(account['nickname'], '资料-prof_acc', account)
        self.assertEqual(account['avatar_url'], 'https://img.example/prof_acc.jpg', account)
        self.assertEqual(fake.profile_calls, 1)
        self.assertIs(fake.profile_loop, common.shared_loop(),
                      '账号资料没有跑在共享事件循环上（用 asyncio.run 建了新 loop）')

    def test_refresh_empty_nickname_keeps_old_and_never_touches_cookie(self):
        """空昵称不许清掉旧昵称；回填必须走窄写，`upsert_account` 会抹掉 cookie/enabled。"""
        self._account('prof_keep', nickname='月狐兔_', enabled=True)
        before = store.get_account('prof_keep')
        self.assertTrue(before['enabled'])
        fake = FakeClient('prof_keep')
        fake.profile = {'nickname': '', 'avatar_url': 'https://img.example/keep.jpg'}
        self.clients['prof_keep'] = fake

        result = accounts_service.refresh_profile('prof_keep', force=True)

        self.assertTrue(result['refreshed'], result)
        account = store.get_account('prof_keep')
        self.assertEqual(account['nickname'], '月狐兔_', '空昵称把旧昵称清掉了')
        self.assertEqual(account['avatar_url'], 'https://img.example/keep.jpg', account)
        self.assertEqual(store.get_cookie('prof_keep'), 'unb=prof_keep; a=1',
                         'cookie 被回填动作写丢了（八成用了 upsert_account）')
        self.assertTrue(account['enabled'], 'enabled 被回填动作写回默认值了')
        self.assertEqual(account['login_state'], store.STATE_ACTIVE, account)

    def test_refresh_is_throttled_but_force_bypasses_it(self):
        """节流：同一次进程内不许每次列表刷新都去撞平台接口。"""
        self._account('prof_throttle', nickname='')
        fake = FakeClient('prof_throttle')
        self.clients['prof_throttle'] = fake

        first = accounts_service.refresh_profile('prof_throttle')
        second = accounts_service.refresh_profile('prof_throttle')
        self.assertTrue(first['refreshed'], first)
        self.assertFalse(second['refreshed'], second)
        self.assertIn('跳过', second.get('message', ''))
        self.assertEqual(fake.profile_calls, 1, '节流没生效，第二次又打了平台')

        forced = accounts_service.refresh_profile('prof_throttle', force=True)
        self.assertTrue(forced['refreshed'], forced)
        self.assertEqual(fake.profile_calls, 2)

    def test_platform_error_never_breaks_list_or_refresh_endpoint(self):
        """平台抛错（风控/网络/改版）：列表与刷新接口都必须 200，账号照常可用。"""
        self._account('prof_err', nickname='', enabled=True)
        fake = FakeClient('prof_err')
        fake.profile_error = RiskControlError('FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦')
        self.clients['prof_err'] = fake

        resp = self.client.get('/api/accounts', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = {row['id']: row for row in resp.json()['data']}
        self.assertIn('prof_err', rows)

        resp = self.client.post('/api/accounts/prof_err/refresh-profile', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertFalse(data['refreshed'], data)
        self.assertIn('FAIL_SYS_USER_VALIDATE', data['message'])
        self.assertEqual(data['nickname'], '')
        self.assertEqual(store.get_account('prof_err')['login_state'], store.STATE_ACTIVE,
                         '拉资料撞风控不该把账号标成需要重登')

    def test_list_fills_missing_nickname_in_background_then_throttles(self):
        """列表接口对缺昵称账号做后台补位，且**不阻塞响应**、不重复打平台。"""
        self._account('prof_list', nickname='', enabled=True, cookie='unb=prof_list; a=1')
        fake = FakeClient('prof_list')
        self.clients['prof_list'] = fake

        started = time.monotonic()
        resp = self.client.get('/api/accounts', headers=self.headers)
        elapsed = time.monotonic() - started
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertLess(elapsed, 1.0, f'列表被资料拉取阻塞了 {elapsed:.2f}s')

        self.assertTrue(
            self._wait(lambda: store.get_account('prof_list')['nickname'] == '资料-prof_list'),
            '后台补位没把昵称落库')
        calls_after_first = fake.profile_calls
        self.assertGreaterEqual(calls_after_first, 1)

        # 再查一次列表：节流窗口内不许再打一次平台
        resp = self.client.get('/api/accounts', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = {row['id']: row for row in resp.json()['data']}
        self.assertEqual(rows['prof_list']['nickname'], '资料-prof_list')
        time.sleep(0.3)  # 给可能被错误起出来的线程一点时间
        self.assertEqual(fake.profile_calls, calls_after_first,
                         '列表每次刷新都重复拉平台资料（节流没接上）')


if __name__ == '__main__':
    unittest.main(verbosity=2)
