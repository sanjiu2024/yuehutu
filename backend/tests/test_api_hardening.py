"""API 层两处加固的回归锁（独立复核提出的 P2-3 / P2-4）。

**为什么需要**：
1. `POST /api/risk/{account_id}/clear` 原先认请求体里的 `{verified: true}` —— 前端
   `Risk.tsx` 就是无条件这么发的，于是「只有人工验证成功才能解除冷却」这道门槛变成
   **调用方自证**，等于没有门槛：任何人（或任何页面脚本）都能把冷却清掉。真正的凭证
   只能由服务端写：真实滑块过了之后 `POST /api/accounts/{id}/captcha` 的后台任务会
   调 `risk_service.mark_verified()`。
2. 未知路由 404 走的是 Starlette 路由层抛的 `HTTPException`，而统一信封的 handler
   之前只注册在 FastAPI 的子类上 → `GET /api/definitely_not_here` 返回
   `{"detail":"Not Found"}`，与后端其它所有错误的 `{success,message,code}` 信封不一致。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_api_hardening.py"

不打真实闲鱼平台、不调真实 LLM。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core import config, db, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import risk_service  # noqa: E402

ACCOUNT_ID = 'hard_acc'


class ApiHardeningTest(unittest.TestCase):
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
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('hard','x',1)"))
        self.token = security.issue_token(self.user_id)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        db.execute('DELETE FROM risk_state')

    def tearDown(self):
        db.execute('DELETE FROM users')
        db.execute('DELETE FROM risk_state')
        db.execute('DELETE FROM risk_events')
        self.app_ctx.__exit__(None, None, None)

    # ---------------------------------------------------------------- 冷却闸门

    def test_clear_rejects_client_supplied_verified(self):
        """请求体里的 `verified: true` 不再算凭证 —— 这正是加固前被绕过的那条缝。"""
        resp = self.client.post(f'/api/risk/{ACCOUNT_ID}/clear', json={'verified': True},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'verification_required')

    def test_clear_accepts_server_side_credential(self):
        """服务端写下凭证后（真实验证通过的路径）必须能清 —— 别把闸门做成一律拒绝。"""
        risk_service.mark_verified(ACCOUNT_ID, 'slider')
        resp = self.client.post(f'/api/risk/{ACCOUNT_ID}/clear', json={}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], body)
        self.assertTrue(body['data']['cleared'])

    def test_clear_requires_auth(self):
        resp = self.client.post(f'/api/risk/{ACCOUNT_ID}/clear', json={})
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertEqual(resp.json()['code'], 'unauthorized')

    # ---------------------------------------------------------------- 统一信封

    def test_unknown_route_uses_envelope(self):
        resp = self.client.get('/api/definitely_not_here', headers=self.headers)
        self.assertEqual(resp.status_code, 404, resp.text)
        body = resp.json()
        self.assertIn('success', body, f'404 没走统一信封：{body}')
        self.assertFalse(body['success'])
        self.assertEqual(body['code'], 'not_found')

    def test_wrong_method_uses_envelope(self):
        resp = self.client.delete('/api/risk/status', headers=self.headers)
        self.assertEqual(resp.status_code, 405, resp.text)
        self.assertIn('success', resp.json(), f'405 没走统一信封：{resp.text}')

    def test_existing_route_still_works(self):
        """反向对照：404/405 的 handler 不能把正常响应的信封打乱。"""
        resp = self.client.get('/api/health')
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['success'])

    # ---------------------------------------------------------------- 端口文件

    def test_port_file_removed_on_shutdown(self):
        """退出时删掉端口文件：留着死端口比没有文件更难排查。"""
        import os

        config.PORT_PATH.write_text('19107', encoding='utf-8')
        os.environ['XYWB_PORT'] = '19107'
        try:
            with TestClient(create_app()):
                self.assertTrue(config.PORT_PATH.exists(), '启动时应写入端口文件')
            self.assertFalse(config.PORT_PATH.exists(), '退出时没删掉端口文件')
        finally:
            os.environ.pop('XYWB_PORT', None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
