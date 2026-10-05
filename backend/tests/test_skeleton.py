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
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

        from backend.app.main import create_app

        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
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

    def test_desktop_secret_login(self):
        resp = self.client.post('/api/auth/desktop', headers={'X-Desktop-Secret': security.local_secret()})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['token'])

        resp = self.client.post('/api/auth/desktop', headers={'X-Desktop-Secret': 'wrong'})
        self.assertEqual(resp.status_code, 401, resp.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
