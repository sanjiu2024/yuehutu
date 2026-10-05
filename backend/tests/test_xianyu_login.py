"""扫码登录与账号路由测试。

**全部离线**：用 ``httpx.MockTransport`` 顶掉真实网络。这里的每一轮状态
推进都来自脚本化的假响应，绝不碰 passport.goofish.com。

跑法（仓库根目录）::

    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_xianyu_login.py"

本文件守的是三条最容易写错、写错就要用户重扫的语义：

1. **CONFIRMED 但没有 unb 不算成功** —— 旧项目在这里报成功，于是前端拿着
   一张没有账号 ID 的「登录成功」去建账号，建出一堆空壳。
2. **iframeRedirect（人脸/短信验证）不能结束会话** —— 判死或 break，用户
   刷完脸永远等不到确认。
3. **NO_PROXY 含 ``[::1]`` 时 httpx 在构造阶段就抛异常** —— 旧项目扫码
   整体瘫痪的真因，这里用真实环境变量复现并验证消毒后的构造。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import config, db  # noqa: E402
from backend.app.xianyu import cookies as ck  # noqa: E402
from backend.app.xianyu import login as qr_login  # noqa: E402
from backend.app.xianyu import net, store  # noqa: E402

# 一段最小的 viewData，形状与真实页面一致（正则要求以 `};` 收尾）。
VIEW_DATA_HTML = (
    '<html><script>window.viewData = '
    '{"loginFormData": {"t": "T-PARAM", "ck": "CK-PARAM"}};'
    '</script></html>'
)


class _FakePlatform:
    """脚本化的假闲鱼：按 URL 分发响应，二维码状态可逐轮变化。"""

    def __init__(self, statuses=None, confirmed_cookies=None):
        self.statuses = list(statuses or ['NEW'])
        self.confirmed_cookies = confirmed_cookies or {
            'unb': '99887766', 'cookie2': 'c2', 'x5sec': 'PASS-TOKEN',
        }
        self.calls = []
        # path -> 该次请求带回的 query 参数（回归用：mini_login.htm 必须带 appName 等）
        self.queries = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # 用 path 而不是整串 URL 匹配：generate.do / query.do 都带一堆 query 参数。
        path = request.url.path
        self.calls.append((request.method, path))
        self.queries.setdefault(path, dict(request.url.params))

        if 'h5api.m.goofish.com' in url and 'mtop.gaia' in url:
            # MTOP 握手：靠 Set-Cookie 种下 _m_h5_tk。
            return httpx.Response(
                200, headers={'set-cookie': '_m_h5_tk=token123_1700000000000; Path=/'},
                json={'ret': ['FAIL_SYS_TOKEN_EMPTY::令牌为空']},
            )

        if path == qr_login.API_MINI_LOGIN:
            # 复刻真实 passport 的行为：不带 appName 时返回 200 + 「非法请求
            # [appNameError]」页面，正文里没有 window.viewData。少了这段，测试
            # 对「漏传查询参数」完全无感（用户实测踩过一次）。
            if request.url.params.get('appName') != 'xianyu':
                return httpx.Response(
                    200, text='<html><h3>非法请求 [appNameError]</h3></html>')
            return httpx.Response(200, text=VIEW_DATA_HTML)

        if path == qr_login.API_GENERATE_QR:
            return httpx.Response(200, json={'content': {
                'success': True,
                'data': {'codeContent': 'https://passport.goofish.com/qr/abc', 't': 'T-PARAM', 'ck': 'CK-PARAM'},
            }})

        if path == qr_login.API_SCAN_STATUS:
            status = self.statuses.pop(0) if self.statuses else 'NEW'
            data = {'qrCodeStatus': status}
            headers = []
            if status == 'CONFIRMED':
                # 确认那一轮响应里就带着完整登录态。
                # **每个 Cookie 一条 Set-Cookie 头**：合成一条 `a=1; b=2` 会被
                # httpx 按属性解析，只有第一个能成活，x5sec 会丢。
                headers = [(b'set-cookie', f'{k}={v}; Path=/'.encode())
                           for k, v in self.confirmed_cookies.items()]
            return httpx.Response(200, headers=headers, json={'content': {'data': data}})

        if 'mtop.idle.web.user.page.head' in url:
            return httpx.Response(200, json={
                'ret': ['SUCCESS::调用成功'],
                'data': {'module': {'base': {'displayName': '闲鱼测试号'}}},
            })

        raise AssertionError(f'测试没有为这个请求准备响应: {request.method} {url}')

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class _TempDB:
    """把数据库指向临时目录。绝不动用户真实数据。"""

    def __enter__(self):
        # ignore_cleanup_errors：Windows 上 loguru 的 sink 与 sqlite WAL 可能仍
        # 短暂持有文件句柄，删除临时目录会抛 WinError 32/267。清理失败不该让
        # 业务断言变红 —— 目录在系统临时区，OS 迟早回收。
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._saved = {
            'DB_PATH': config.DB_PATH,
            'DATA_DIR': config.DATA_DIR,
            'LOGS_DIR': config.LOGS_DIR,
            'LOG_PATH': config.LOG_PATH,
            'SECRET_PATH': config.SECRET_PATH,
            'CRED_PATH': config.CRED_PATH,
            'MCP_TOKEN_PATH': config.MCP_TOKEN_PATH,
        }
        config.DATA_DIR = root
        config.LOGS_DIR = root / 'logs'
        config.LOG_PATH = root / 'logs' / 'app.log'
        config.DB_PATH = root / 'app.db'
        config.SECRET_PATH = root / 'local_secret.txt'
        config.CRED_PATH = root / 'desktop_credentials.json'
        config.MCP_TOKEN_PATH = root / 'mcp_token.txt'
        db.reset_connection()
        db.init_db()
        return self

    def __exit__(self, *exc):
        db.reset_connection()
        # loguru 的文件 handler 会一直握着 app.log，Windows 上文件被占用时
        # TemporaryDirectory 清理会抛 WinError 32。先摘掉 handler 再删目录。
        from loguru import logger

        logger.remove()
        for key, value in self._saved.items():
            setattr(config, key, value)
        db.reset_connection()
        self._tmp.cleanup()
        return False


class QRLoginFlowTest(unittest.TestCase):
    def setUp(self):
        self.manager = qr_login.QRLoginManager()

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_start_returns_ticket_and_qr_image(self):
        platform = _FakePlatform()
        self.manager.transport = platform.transport()

        data = self._run(self.manager.start())

        self.assertTrue(data['session_id'])
        self.assertTrue(data['qr_url'].startswith('data:image/png;base64,'))
        # 前端只认 qr_image_b64（裸 base64，不带 data URL 前缀）。
        self.assertNotIn('data:image', data['qr_image_base64'])
        import base64

        raw = base64.b64decode(data['qr_image_base64'])
        self.assertTrue(raw.startswith(b'\x89PNG'), '二维码必须是合法 PNG')
        self.assertGreater(data['expire_in'], 0)

    def test_start_requires_login_form_data(self):
        """拿不到 loginFormData 必须明确报错，不能带着空参数继续。"""
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if 'mtop.gaia' in url:
                return httpx.Response(200, json={})
            return httpx.Response(200, text='<html>没有 viewData</html>')

        self.manager.transport = httpx.MockTransport(handler)
        with self.assertRaises(qr_login.QRLoginError) as ctx:
            self._run(self.manager.start())
        self.assertIn('viewData', str(ctx.exception))

    def test_poll_waiting_then_scanned(self):
        platform = _FakePlatform(statuses=['NEW', 'SCANED'])
        self.manager.transport = platform.transport()
        ticket = self._run(self.manager.start())['session_id']

        first = self._run(self.manager.poll(ticket))
        self.assertEqual(first['status'], qr_login.STATUS_WAITING)

        second = self._run(self.manager.poll(ticket))
        self.assertEqual(second['status'], qr_login.STATUS_SCANNED)

    def test_confirmed_yields_cookie_with_x5sec(self):
        platform = _FakePlatform(statuses=['NEW', 'SCANED', 'CONFIRMED'])
        self.manager.transport = platform.transport()
        ticket = self._run(self.manager.start())['session_id']

        self._run(self.manager.poll(ticket))
        self._run(self.manager.poll(ticket))
        final = self._run(self.manager.poll(ticket))

        self.assertEqual(final['status'], qr_login.STATUS_SUCCESS)
        self.assertEqual(final['user_id'], '99887766')
        self.assertEqual(final['nickname'], '闲鱼测试号')
        # 通过凭证必须在，且挑战标记被清掉（否则下次请求会带着标记撞风控）。
        self.assertTrue(ck.has_x5sec(final['cookie']))
        self.assertFalse(ck.is_blocked_cookie(final['cookie']))

    def test_confirmed_without_unb_is_not_success(self):
        """**最关键的一条**：没有 unb 就不能算登录成功。

        旧实现只看 ``qrCodeStatus == CONFIRMED`` 就报成功，前端于是拿一个空
        账号 ID 去建账号；用户看到「登录成功」但列表里没有账号，也不知道该
        重扫。
        """
        platform = _FakePlatform(
            statuses=['CONFIRMED'],
            confirmed_cookies={'cookie2': 'only-visitor-cookie'},
        )
        self.manager.transport = platform.transport()
        ticket = self._run(self.manager.start())['session_id']

        result = self._run(self.manager.poll(ticket))

        self.assertNotEqual(result['status'], qr_login.STATUS_SUCCESS)
        self.assertEqual(result['user_id'], '')
        self.assertIn('unb', result.get('message', ''))

    def test_iframe_redirect_keeps_session_alive(self):
        """人脸/短信验证态必须保持会话，且延长有效期。"""
        platform = _FakePlatform(statuses=['CONFIRMED'])
        platform.transport()
        original_handler = platform.__call__

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == qr_login.API_SCAN_STATUS:
                return httpx.Response(200, json={'content': {'data': {
                    'qrCodeStatus': 'CONFIRMED',
                    'iframeRedirect': True,
                    'iframeRedirectUrl': 'https://passport.goofish.com/verify',
                }}})
            return original_handler(request)

        self.manager.transport = httpx.MockTransport(handler)
        ticket = self._run(self.manager.start())['session_id']
        session = self.manager.get_session(ticket)

        result = self._run(self.manager.poll(ticket))

        self.assertEqual(result['status'], qr_login.STATUS_VERIFICATION)
        self.assertEqual(session.status, qr_login.STATUS_VERIFICATION)
        # 延长一次（300 -> 600），且只延一次。
        self.assertEqual(session.expire_time,
                         qr_login.QR_EXPIRE_SECONDS + qr_login.VERIFICATION_EXTRA_SECONDS)
        session.extend_for_verification()
        self.assertEqual(session.expire_time,
                         qr_login.QR_EXPIRE_SECONDS + qr_login.VERIFICATION_EXTRA_SECONDS)
        # 会话还必须活着，能被继续轮询。
        self.assertIsNotNone(self.manager.get_session(ticket))

    def test_expired_status(self):
        platform = _FakePlatform(statuses=['EXPIRED'])
        self.manager.transport = platform.transport()
        ticket = self._run(self.manager.start())['session_id']

        result = self._run(self.manager.poll(ticket))
        self.assertEqual(result['status'], qr_login.STATUS_EXPIRED)

    def test_unknown_ticket_is_expired_not_crash(self):
        result = self._run(self.manager.poll('does-not-exist'))
        self.assertEqual(result['status'], qr_login.STATUS_EXPIRED)

    def test_cleanup_expired_removes_dead_sessions(self):
        platform = _FakePlatform()
        self.manager.transport = platform.transport()
        ticket = self._run(self.manager.start())['session_id']
        self.manager.sessions[ticket].created_time -= (
            qr_login.QR_EXPIRE_SECONDS + qr_login.VERIFICATION_EXTRA_SECONDS + 10)

        self.assertEqual(self.manager.cleanup_expired(), 1)
        self.assertNotIn(ticket, self.manager.sessions)

    def test_success_is_cached_without_extra_request(self):
        """已成功的会话再次轮询直接返回，不再打接口。

        重复确认会再发一次请求，白白多一次风控暴露面。
        """
        platform = _FakePlatform(statuses=['CONFIRMED'])
        self.manager.transport = platform.transport()
        ticket = self._run(self.manager.start())['session_id']
        self._run(self.manager.poll(ticket))
        before = len(platform.calls)

        again = self._run(self.manager.poll(ticket))

        self.assertEqual(again['status'], qr_login.STATUS_SUCCESS)
        self.assertEqual(len(platform.calls), before)


class ViewDataParsingTest(unittest.TestCase):
    def test_non_greedy_stops_at_first_brace(self):
        """贪婪正则会吞掉后面所有脚本，导致 JSON 解析失败。"""
        html = ('<script>window.viewData = {"loginFormData": {"t": "1"}};</script>'
                '<script>window.viewData = {"other": 1};</script>')
        match = qr_login.VIEW_DATA_RE.search(html)
        self.assertIsNotNone(match)
        parsed = json.loads(match.group(1))
        self.assertEqual(parsed, {'loginFormData': {'t': '1'}})


class LoginQueryParamsTest(unittest.TestCase):
    """mini_login.htm 与 generate.do 都必须带上 appName 等参数。

    真实 passport 少了 ``appName`` 会返回 200 + 「非法请求 [appNameError]」页面，
    正文里没有 ``window.viewData``，上层只会报「未能解析出 viewData」，真因被吃掉。
    """

    def test_login_query_carries_required_params(self):
        query = qr_login.login_query()
        self.assertEqual(query['appName'], 'xianyu')
        self.assertEqual(query['appEntrance'], 'web')
        self.assertEqual(query['stie'], '77')
        self.assertTrue(query['rnd'])

    def test_rnd_changes_every_call(self):
        self.assertNotEqual(qr_login.login_query()['rnd'], qr_login.login_query()['rnd'])

    def test_start_sends_params_to_mini_login(self):
        import asyncio

        platform = _FakePlatform()
        manager = qr_login.QRLoginManager(
            transport=httpx.MockTransport(platform))
        with _TempDB():
            asyncio.run(manager.start())

        sent = platform.queries.get(qr_login.API_MINI_LOGIN) or {}
        self.assertEqual(sent.get('appName'), 'xianyu', f'mini_login 实收参数: {sent}')
        self.assertEqual(sent.get('appEntrance'), 'web')

        generated = platform.queries.get(qr_login.API_GENERATE_QR) or {}
        self.assertEqual(generated.get('appName'), 'xianyu')


class StoreTest(unittest.TestCase):
    def test_upsert_and_list_hides_cookie(self):
        with _TempDB():
            store.upsert_account(account_id='u1', nickname='小明',
                                 xianyu_user_id='u1', cookie='unb=u1; x5sec=ok',
                                 login_state=store.STATE_ACTIVE)
            accounts = store.list_accounts()

            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]['nickname'], '小明')
            # 列表接口绝不能带出 Cookie 原文。
            self.assertNotIn('cookie', accounts[0])
            self.assertTrue(accounts[0]['has_cookie'])
            self.assertTrue(accounts[0]['has_x5sec'])
            self.assertEqual(store.get_cookie('u1'), 'unb=u1; x5sec=ok')

    def test_upsert_does_not_reset_switches(self):
        """重新扫码不能把用户关掉的开关悄悄打开。"""
        with _TempDB():
            store.upsert_account(account_id='u2', xianyu_user_id='u2', cookie='unb=u2')
            store.update_account('u2', enabled=False, auto_reply=False)
            store.upsert_account(account_id='u2', nickname='新昵称',
                                 xianyu_user_id='u2', cookie='unb=u2; x5sec=ok')

            account = store.get_account('u2')
            self.assertEqual(account['nickname'], '新昵称')
            self.assertFalse(account['enabled'])
            self.assertFalse(account['auto_reply'])

    def test_update_account_partial(self):
        """只改一个字段时，其它字段必须原样保留。"""
        with _TempDB():
            store.upsert_account(account_id='u3', xianyu_user_id='u3', cookie='unb=u3')
            store.update_account('u3', auto_reply=False)
            store.update_account('u3', remark='备注')

            account = store.get_account('u3')
            self.assertFalse(account['auto_reply'])
            self.assertEqual(account['remark'], '备注')

    def test_delete_removes_credentials(self):
        with _TempDB():
            store.upsert_account(account_id='u4', xianyu_user_id='u4', cookie='unb=u4; x5sec=ok')
            self.assertTrue(store.delete_account('u4'))
            self.assertIsNone(store.get_account('u4'))
            self.assertEqual(store.get_cookie('u4'), '')
            # 幂等：第二次删要返回 False 而不是抛异常。
            self.assertFalse(store.delete_account('u4'))

    def test_mark_need_relogin_is_not_risk(self):
        with _TempDB():
            store.upsert_account(account_id='u5', xianyu_user_id='u5', cookie='unb=u5')
            store.mark_need_relogin('u5', 'Session过期')
            account = store.get_account('u5')
            self.assertEqual(account['login_state'], store.STATE_NEED_RELOGIN)
            self.assertEqual(account['relogin_reason'], 'Session过期')


class NoProxyConstructionTest(unittest.TestCase):
    """NO_PROXY 消毒的真实回归测试（旧项目就是死在这里）。"""

    def test_async_client_constructs_with_bracket_no_proxy(self):
        import os

        original = os.environ.get('NO_PROXY')
        try:
            os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1,[::1]'
            net.sanitize_no_proxy()

            async def build():
                client = net.async_client()
                await client.aclose()
                return True

            import asyncio

            self.assertTrue(asyncio.run(build()))
        finally:
            if original is None:
                os.environ.pop('NO_PROXY', None)
            else:
                os.environ['NO_PROXY'] = original
            net.sanitize_no_proxy()


class AccountsApiTest(unittest.TestCase):
    """账号路由：鉴权信封 + 扫码登录全流程（mock 掉真实平台）。"""

    def _client(self):
        from fastapi.testclient import TestClient

        from backend.app import main

        return TestClient(main.app, raise_server_exceptions=False)

    def test_requires_token(self):
        with _TempDB():
            with self._client() as client:
                response = client.get('/api/accounts')

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertIn('message', body)
        self.assertEqual(body['code'], 'unauthorized')

    def _login_token(self):
        """建一个本地管理员并换一个可用 token。

        直接走 ``security.authenticate`` + ``issue_token``（不经过 ``/api/auth``
        路由），这样本文件只依赖 core 的稳定接口，别人改 auth 路由不会牵连这里。
        """
        from backend.app.core import security

        username, password = security.ensure_admin()
        user = security.authenticate(username, password)
        if user is None:
            return None
        return security.issue_token(user['id'])

    def test_full_qr_flow_creates_account(self):
        with _TempDB():
            platform = _FakePlatform(statuses=['SCANED', 'CONFIRMED'])
            qr_login.manager.transport = platform.transport()
            try:
                with self._client() as client:
                    token = self._login_token()
                    if token is None:
                        self.skipTest('未生成随机密码，跳过需要登录的用例')
                    headers = {'Authorization': f'Bearer {token}'}

                    started = client.post('/api/accounts/qr/start', headers=headers).json()
                    self.assertTrue(started['success'])
                    ticket = started['data']['ticket']
                    self.assertTrue(started['data']['qr_image_b64'])

                    scanned = client.get(f'/api/accounts/qr/{ticket}', headers=headers).json()
                    self.assertEqual(scanned['data']['state'], 'scanned')

                    done = client.get(f'/api/accounts/qr/{ticket}', headers=headers).json()
                    self.assertEqual(done['data']['state'], 'confirmed')
                    self.assertEqual(done['data']['account_id'], '99887766')

                    accounts = client.get('/api/accounts', headers=headers).json()['data']
                    self.assertEqual([a['id'] for a in accounts], ['99887766'])
                    self.assertNotIn('cookie', accounts[0])
                    self.assertEqual(accounts[0]['runtime_state'], 'stopped')
            finally:
                qr_login.manager.transport = None
                qr_login.manager.sessions.clear()


if __name__ == '__main__':
    unittest.main(verbosity=2)
