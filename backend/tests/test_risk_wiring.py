"""协议层熔断 ↔ `risk_state` 双向接线的回归锁。

**为什么需要这个文件**：协议层 `backend/app/xianyu/risk.py` 的 `AccountGuard` 会
真的拦请求，但状态只在内存里；UI 读的是 `backend/app/services/risk_service.py` 的
`risk_state` 表。两边不通时，`GET /api/risk/status` 永远显示「无命中」，而协议层
已经在默默熔断 —— 用户在风控页看到的是假的平静；反过来，用户划完滑块验证通过，
UI 说没事了，协议层却照样拿 `is_blocked` 把请求拦下来。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_risk_wiring.py"

**不打真实闲鱼平台**：只调 `_check_response`（纯函数式分流）与「已被拦下」的
`mtop`（在发请求之前就抛 `RiskControlError`），不产生任何网络调用。
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

from backend.app.core import db, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import risk_service  # noqa: E402
from backend.app.xianyu import risk as protocol_risk  # noqa: E402
from backend.app.xianyu.client import RiskControlError, XianyuClient  # noqa: E402

#: 形态正确的最小 Cookie：`unb` 就是账号 ID，也是协议层熔断的分桶键。
COOKIE = '_m_h5_tk=abc_123; unb=2200000001; cookie2=deadbeef; _tb_token_=xyz'
ACCOUNT_ID = '2200000001'


class RiskWiringTest(unittest.TestCase):
    """协议层命中 -> 落库；验证通过 -> 两端复位。"""

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
        db.execute('DELETE FROM risk_state')
        db.execute('DELETE FROM risk_events')
        db.execute('DELETE FROM operation_logs')
        db.execute("DELETE FROM settings WHERE key LIKE 'risk.%'")
        # 内存里的熔断不跨用例：不清会把上一个用例的冷却带进来（假阳性）。
        protocol_risk.registry.reset()

    def _state(self):
        return risk_service.status(ACCOUNT_ID)['state']

    # ---------------------------------------------------------------- 正向落库

    def test_protocol_hit_lands_in_risk_state(self):
        """`XianyuClient._check_response` 判定风控 -> 库里 consecutive_hits 真的 +1。"""
        client = XianyuClient(COOKIE)
        with self.assertRaises(RiskControlError) as ctx:
            client._check_response({'data': {'retryAfter': 0}}, 'FAIL_SYS_USER_VALIDATE')

        self.assertEqual(ctx.exception.retry_after, 60, '内存冷却应为阶梯第 1 档')
        state = self._state()
        self.assertEqual(state['consecutive_hits'], 1)
        self.assertTrue(state['blocked'], f'UI 应该看到命中：{state}')

        events = risk_service.events()['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'hit')
        self.assertEqual(events[0]['account_id'], ACCOUNT_ID)

    def test_cooldown_ladder_follows_cooldown_steps(self):
        """内存阶梯与库里的 blocked_until 同步升级：60 / 180 / 300。"""
        self.assertEqual(protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR'), 60)
        self.assertEqual(protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR'), 180)
        self.assertEqual(protocol_risk.registry.trip(ACCOUNT_ID, '哎哟喂'), 300)

        state = self._state()
        self.assertEqual(state['consecutive_hits'], 3)
        self.assertTrue(state['blocked'])
        self.assertGreaterEqual(state['remaining_seconds'], 295)
        # 同一档阶梯：内存剩余不应该比库里短（短了就是 UI 先「解封」）
        self.assertGreaterEqual(
            protocol_risk.registry.get(ACCOUNT_ID).remaining_seconds,
            state['remaining_seconds'] - 1,
        )

    def test_server_retry_after_is_not_downgraded(self):
        """响应里写 Retry-After=900 时，库里不能只写阶梯的 60。"""
        cooldown = protocol_risk.registry.trip(ACCOUNT_ID, '请稍后重试', min_cooldown=900)
        self.assertEqual(cooldown, 900)
        self.assertGreaterEqual(self._state()['remaining_seconds'], 895)
        self.assertGreaterEqual(protocol_risk.registry.get(ACCOUNT_ID).remaining_seconds, 895)

    def test_session_expired_is_not_a_hit(self):
        """登录态过期不是风控：不许熔断，也不许在库里留下命中。"""
        self.assertEqual(protocol_risk.registry.trip(ACCOUNT_ID, 'FAIL_SYS_SESSION_EXPIRED'), 0)
        state = self._state()
        self.assertEqual(state['consecutive_hits'], 0)
        self.assertFalse(state['blocked'])
        self.assertEqual(risk_service.events()['total'], 0)

    def test_blocked_request_refused_without_double_count(self):
        """被拦下的请求**不再**记一次命中：拦请求是熔断的后果，不是新的命中。"""
        protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR')
        client = XianyuClient(COOKIE)
        # 已在冷却中 -> 在发任何 HTTP 之前就抛，测试因此不需要网络
        with self.assertRaises(RiskControlError):
            asyncio.run(client.mtop('mtop.taobao.idle.pc.detail', {'itemId': '1'}))
        self.assertEqual(self._state()['consecutive_hits'], 1)

    # ---------------------------------------------------------------- 反向复位

    def test_refused_clear_does_not_release_protocol_guard(self):
        """未验证就清冷却：拒绝，且**不许**放掉内存里那把锁（那就是开了后门）。"""
        protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR')
        refused = risk_service.clear_cooldown(ACCOUNT_ID)
        self.assertEqual(refused['code'], 'verification_required')
        self.assertTrue(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)
        self.assertTrue(risk_service.status(ACCOUNT_ID)['blocked'])
        self.assertEqual(self._state()['consecutive_hits'], 1)

    def test_resync_restores_in_memory_lock_from_db(self):
        """重启后内存锁没了，但库里还在冷却 —— 启动时必须回灌，否则 UI 说
        「冷却中」而协议层立刻放行，两边又各说各话。"""
        risk_service.record_hit(ACCOUNT_ID, 'hit', '服务端要求稍后再试')
        self.assertTrue(risk_service.status(ACCOUNT_ID)['blocked'])
        # 模拟进程重启：内存注册表全清，库不动
        protocol_risk.registry.reset()
        self.assertFalse(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)

        restored = risk_service.resync_from_db()

        guard = protocol_risk.registry.get(ACCOUNT_ID)
        self.assertEqual(restored, 1)
        self.assertTrue(guard.is_blocked, '重启后没恢复冷却，会立刻继续打闲鱼')
        self.assertEqual(guard.consecutive_hits, 1)

    def test_resync_ignores_expired_cooldown(self):
        """冷却早就过期的账号不该被回灌成封锁。"""
        risk_service.record_hit(ACCOUNT_ID, 'hit', '服务端要求稍后再试')
        db.execute('UPDATE risk_state SET blocked_until = ? WHERE account_id = ?',
                   (time.time() - 10, ACCOUNT_ID))
        protocol_risk.registry.reset()

        self.assertEqual(risk_service.resync_from_db(), 0)
        self.assertFalse(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)

    def test_mark_verified_resets_both_ends(self):
        """人工验证成功 -> 内存冷却与库里状态同时复位。"""
        protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR')
        self.assertTrue(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)

        risk_service.mark_verified(ACCOUNT_ID, 'slider')

        guard = protocol_risk.registry.get(ACCOUNT_ID)
        self.assertFalse(guard.is_blocked, '内存熔断没被复位，请求还会被拦')
        self.assertEqual(guard.consecutive_hits, 0)

        cleared = risk_service.clear_cooldown(ACCOUNT_ID)
        self.assertTrue(cleared['cleared'])
        self.assertFalse(risk_service.status(ACCOUNT_ID)['blocked'])
        self.assertEqual(self._state()['consecutive_hits'], 0)

    def test_clear_cooldown_resets_protocol_guard(self):
        """`clear_cooldown(verified=True)`（服务端确认验证通过）也要放掉内存锁。"""
        protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR')
        self.assertTrue(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)

        cleared = risk_service.clear_cooldown(ACCOUNT_ID, verified=True)
        self.assertTrue(cleared['cleared'])
        self.assertFalse(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)


class RiskStatusApiTest(unittest.TestCase):
    """`GET /api/risk/status` / `/api/risk/events` 必须反映真实命中（不是那张空表）。"""

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
        from fastapi.testclient import TestClient

        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('wire','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        db.execute('DELETE FROM risk_state')
        db.execute('DELETE FROM risk_events')
        protocol_risk.registry.reset()

    def tearDown(self):
        db.execute('DELETE FROM users')
        db.execute('DELETE FROM risk_state')
        db.execute('DELETE FROM risk_events')
        protocol_risk.registry.reset()
        self.app_ctx.__exit__(None, None, None)

    def test_status_and_events_show_protocol_hit(self):
        protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR')

        resp = self.client.get('/api/risk/status', params={'account_id': ACCOUNT_ID},
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertTrue(data['blocked'], f'协议层已熔断，UI 却说没命中：{data}')
        self.assertEqual(data['state']['consecutive_hits'], 1)

        events = self.client.get('/api/risk/events', params={'account_id': ACCOUNT_ID},
                                 headers=self.headers)
        self.assertEqual(events.status_code, 200, events.text)
        self.assertEqual(events.json()['data']['total'], 1)

    def test_clear_endpoint_after_captcha_releases_protocol_guard(self):
        """真实验证通过（服务端写凭证）-> 页面上清冷却后，协议层也不再拦请求。"""
        protocol_risk.registry.trip(ACCOUNT_ID, 'RGV587_ERROR')
        risk_service.mark_verified(ACCOUNT_ID, 'slider')

        resp = self.client.post(f'/api/risk/{ACCOUNT_ID}/clear', json={}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['cleared'])
        self.assertFalse(protocol_risk.registry.get(ACCOUNT_ID).is_blocked)
        self.assertFalse(risk_service.status(ACCOUNT_ID)['blocked'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
