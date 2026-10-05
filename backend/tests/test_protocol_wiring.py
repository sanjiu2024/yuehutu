"""协议层接线的回归锁。

**为什么需要这个文件**：`backend/tests/test_services_items.py:56` 给测试**注入**了一个
`module.get_client = lambda account_id: client` 工厂，而生产代码 `backend/app/xianyu/client.py`
里**根本没有这个工厂**，真实类签名是 `XianyuClient(cookie_str, client=None, transport=None)`。
于是 `common.get_client(account_id)` 过去走的是 `cls(account_id)` —— 把账号 ID 当 Cookie
串传进去，必然抛 `Cookie 里缺少 unb`。

后果不是报错而是**静默退化**：同步商品/发布/改价/上下架/发消息全部只写本地 SQLite，
用户以为在操作闲鱼。297 个用例全绿却抓不到，因为每一处都被自注入的假工厂绕过了。

这个文件只做一件事：**不注入任何工厂**，走真实路径建客户端。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_protocol_wiring.py"

不打真实闲鱼平台：只构造客户端并检查 cookie 解析，不发任何请求。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import db  # noqa: E402
from backend.app.services import common  # noqa: E402
from backend.app.xianyu import store  # noqa: E402

#: 一个形状正确的最小 Cookie：必须有 `unb`，否则 XianyuClient 会（正确地）拒绝。
GOOD_COOKIE = '_m_h5_tk=abc_123; unb=2200000001; cookie2=deadbeef; _tb_token_=xyz'
NO_UNB_COOKIE = '_m_h5_tk=abc_123; cookie2=deadbeef'


class ProtocolWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from backend.tests.testenv import use_temp_app_dir

        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        db.execute('DELETE FROM account_credentials')
        db.execute('DELETE FROM accounts')
        db.execute(
            "INSERT INTO accounts(id, nickname, enabled, auto_reply, created_at, updated_at) "
            "VALUES('wired','接线测试号',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        db.execute(
            'INSERT INTO account_credentials(account_id, cookie, xianyu_user_id, login_state,'
            ' relogin_reason, updated_at)'
            " VALUES('wired', ?, '2200000001', 'active', '', CURRENT_TIMESTAMP)",
            (GOOD_COOKIE,),
        )

    def test_real_client_is_constructed_from_stored_cookie(self):
        """核心断言：不注入工厂也能拿到真客户端 —— 断链修复前这里必失败。"""
        client, err = common.get_client('wired')
        self.assertIsNone(err, f'get_client 失败：{err}')
        self.assertIsNotNone(client, 'get_client 没返回客户端')
        self.assertEqual(type(client).__name__, 'XianyuClient')
        # 真客户端必须已经把 unb 解析成 cookie_id（账号 ID ≠ cookie 串的证据）
        self.assertEqual(client.cookie_id, '2200000001')
        self.assertEqual(client.cookie_str, GOOD_COOKIE)

    def test_missing_cookie_is_not_ready_not_a_crash(self):
        """账号没登录过时给结构化 not_ready，而不是把 ValueError 抛到路由层。"""
        db.execute("DELETE FROM account_credentials WHERE account_id = 'wired'")
        client, err = common.get_client('wired')
        self.assertIsNone(client)
        self.assertIsNotNone(err)
        self.assertEqual(err['code'], 'xianyu_not_ready')

    def test_cookie_without_unb_is_a_structured_failure(self):
        """Cookie 里没有 unb 时同样是信封（协议层的 ValueError 要被兜住）。"""
        db.execute("UPDATE account_credentials SET cookie = ? WHERE account_id = 'wired'",
                   (NO_UNB_COOKIE,))
        client, err = common.get_client('wired')
        self.assertIsNone(client)
        self.assertIsNotNone(err)
        self.assertEqual(err['code'], 'xianyu_not_ready')
        self.assertIn('unb', err['message'])

    def test_store_roundtrip_is_what_get_client_reads(self):
        """锁住耦合点：get_client 读的就是 store.get_cookie，不是别的地方。"""
        self.assertEqual(store.get_cookie('wired'), GOOD_COOKIE)
        store.set_cookie('wired', GOOD_COOKIE + '; extra=1', xianyu_user_id='2200000001')
        client, err = common.get_client('wired')
        self.assertIsNone(err)
        self.assertIn('extra=1', client.cookie_str)


class CallMethodTest(unittest.TestCase):
    """`call_method` 必须把协程跑完再算 ok（否则 async 协议方法= 假成功 + 协程泄漏）。"""

    def test_async_method_is_awaited(self):
        async def fetch():
            return {'items': [1, 2]}

        status, result = common.call_method(type('C', (), {'fetch': staticmethod(fetch)})(), ['fetch'])
        self.assertEqual(status, common.CALL_OK)
        self.assertEqual(result, {'items': [1, 2]}, '协程没被 await，拿到的是 coroutine 对象')

    def test_sync_method_still_works(self):
        status, result = common.call_method(
            type('C', (), {'fetch': staticmethod(lambda: 7)})(), ['fetch'])
        self.assertEqual(status, common.CALL_OK)
        self.assertEqual(result, 7)

    def test_exception_becomes_error_status(self):
        def boom():
            raise RuntimeError('炸了')

        status, err = common.call_method(type('C', (), {'fetch': staticmethod(boom)})(), ['fetch'])
        self.assertEqual(status, common.CALL_ERROR)
        self.assertIsInstance(err, RuntimeError)

    def test_missing_method(self):
        status, result = common.call_method(object(), ['nope'])
        self.assertEqual(status, common.CALL_MISSING)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
