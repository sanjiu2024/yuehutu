"""骨架层冒烟测试：应用能起、建表齐、认证链路通、设置读写通。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_skeleton.py"
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core import config, db, security  # noqa: E402


class SkeletonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 整个数据目录挪到临时位置：只改 DB_PATH 会让临时库的随机密码覆盖真实
        # desktop_credentials.json（docs/PACKAGING.md 6.5）。
        from backend.tests.testenv import use_temp_app_dir

        cls.orig_db_path = config.DB_PATH
        use_temp_app_dir(cls.tmp.name)
        cls.orig_mcp_token = config.MCP_TOKEN_PATH

        from backend.app.main import create_app

        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        config.MCP_TOKEN_PATH = cls.orig_mcp_token
        cls.tmp.cleanup()

    def login(self) -> str:
        credentials = json.loads(config.CRED_PATH.read_text(encoding='utf-8'))
        resp = self.client.post(
            '/api/auth/login',
            json={'username': credentials['username'], 'password': credentials['password']},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'])
        return body['data']['token']

    def test_health(self):
        resp = self.client.get('/api/health')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['success'])

    def test_tables_created(self):
        rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r['name'] for r in rows}
        expected = {
            'users', 'sessions', 'settings', 'accounts', 'account_credentials', 'items',
            'providers', 'ai_roles', 'ai_prompts', 'conversations', 'messages',
            'agent_messages', 'agent_runs', 'orders', 'shipments', 'risk_state',
            'risk_events', 'operation_logs',
        }
        missing = expected - names
        self.assertFalse(missing, f'缺表: {sorted(missing)}')

    def test_requires_token(self):
        resp = self.client.get('/api/settings')
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertFalse(resp.json()['success'])

    def test_login_and_settings_roundtrip(self):
        token = self.login()
        headers = {'Authorization': f'Bearer {token}'}

        resp = self.client.get('/api/settings', headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('ai.enabled', resp.json()['data'])

        resp = self.client.put('/api/settings', headers=headers, json={'ui.theme': 'dark'})
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.get('/api/settings', headers=headers)
        self.assertEqual(resp.json()['data']['ui.theme'], 'dark')

        resp = self.client.get('/api/auth/me', headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['username'])

    def test_mcp_token_written_and_usable(self):
        """后端启动时会给 DSH 插件留一个长期 token，插件不用走登录流程。"""
        self.assertTrue(config.MCP_TOKEN_PATH.exists(), '启动时应写入 mcp_token.txt')
        token = config.MCP_TOKEN_PATH.read_text(encoding='utf-8').strip()
        self.assertTrue(token)
        resp = self.client.get('/api/settings', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_desktop_secret_login(self):
        resp = self.client.post('/api/auth/desktop', headers={'X-Desktop-Secret': security.local_secret()})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['token'])

        resp = self.client.post('/api/auth/desktop', headers={'X-Desktop-Secret': 'wrong'})
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_desktop_login_survives_clobbered_credentials(self):
        """回归（docs/PACKAGING.md 6.5）：凭据文件被别的库的密码覆盖后必须自愈。

        历史现象：跑过一次测试后，桌面外壳启动 → `管理员账号异常`（500），
        用户看到「窗口开了但没登录」。现在 `ensure_admin()` 会重生成一对一致的。
        """
        config.CRED_PATH.write_text(
            json.dumps({'username': 'admin', 'password': 'stale-password-from-another-db'}),
            encoding='utf-8',
        )
        resp = self.client.post('/api/auth/desktop', headers={'X-Desktop-Secret': security.local_secret()})
        self.assertEqual(resp.status_code, 200, resp.text)

        # 自愈后：凭据文件里的密码必须能真正登录（两边一致）
        cred = json.loads(config.CRED_PATH.read_text(encoding='utf-8'))
        self.assertIsNotNone(security.authenticate(cred['username'], cred['password']))
        # 且坏密码不再可用
        self.assertIsNone(security.authenticate(cred['username'], 'stale-password-from-another-db'))

    def test_desktop_login_survives_missing_credentials(self):
        config.CRED_PATH.unlink(missing_ok=True)
        resp = self.client.post('/api/auth/desktop', headers={'X-Desktop-Secret': security.local_secret()})
        self.assertEqual(resp.status_code, 200, resp.text)
        cred = json.loads(config.CRED_PATH.read_text(encoding='utf-8'))
        self.assertIsNotNone(security.authenticate(cred['username'], cred['password']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
