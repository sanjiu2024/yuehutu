"""风控服务测试：冷却阶梯、熔断状态、风控事件、自动滑块、清除冷却的硬规则。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_services_risk.py"

**不打真实闲鱼平台**：本服务不碰协议层，纯本地表 + settings。
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

from backend.app.core import config, db, events  # noqa: E402
from backend.app.services import common, risk_service  # noqa: E402


class RiskServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.execute('DELETE FROM risk_state')
        db.execute('DELETE FROM risk_events')
        db.execute('DELETE FROM operation_logs')
        db.execute("DELETE FROM settings WHERE key LIKE 'risk.%'")

    # ------------------------------------------------------------------ 冷却阶梯

    def test_cooldown_steps_default(self):
        self.assertEqual(risk_service.cooldown_steps(), [60, 180, 300, 600, 1200])
        self.assertEqual(risk_service.cooldown_for(1), 60)
        self.assertEqual(risk_service.cooldown_for(2), 180)
        # 超出阶梯取最后一档，不越界
        self.assertEqual(risk_service.cooldown_for(99), 1200)
        self.assertEqual(risk_service.cooldown_for(0), 60)

    def test_cooldown_steps_from_settings_and_bad_values(self):
        common.set_setting('risk.cooldown_steps', '5, 10 ,x,-3,')
        # 非法项跳过、非正数丢掉
        self.assertEqual(risk_service.cooldown_steps(), [5, 10])
        self.assertEqual(risk_service.cooldown_for(2), 10)
        self.assertEqual(risk_service.cooldown_for(5), 10)

        # 全是垃圾 -> 回落默认，绝不抛异常
        common.set_setting('risk.cooldown_steps', 'abc')
        self.assertEqual(risk_service.cooldown_steps(), risk_service.DEFAULT_STEPS)

    # ------------------------------------------------------------------ 状态

    def test_status_single_account_creates_state(self):
        result = risk_service.status('acc1')
        self.assertEqual(result['account_id'], 'acc1')
        self.assertFalse(result['blocked'])
        self.assertFalse(result['auto_slider'])
        self.assertEqual(result['state']['consecutive_hits'], 0)
        self.assertEqual(result['cooldown_steps'], risk_service.DEFAULT_STEPS)

    def test_status_all_accounts(self):
        risk_service.record_hit('acc1')
        risk_service.status('acc2')
        result = risk_service.status()
        self.assertEqual(len(result['states']), 2)
        self.assertIn('acc1', result['blocked'])
        self.assertNotIn('acc2', result['blocked'])
        self.assertIn('refresh_interval_seconds', result)

    def test_is_blocked(self):
        self.assertFalse(risk_service.is_blocked('acc1'))
        risk_service.record_hit('acc1')
        self.assertTrue(risk_service.is_blocked('acc1'))

        risk_service.clear_cooldown('acc1', verified=True)
        self.assertFalse(risk_service.is_blocked('acc1'))

    def test_record_hit_escalates(self):
        first = risk_service.record_hit('acc1')
        self.assertEqual(first['consecutive_hits'], 1)
        self.assertEqual(first['cooldown_seconds'], 60)

        second = risk_service.record_hit('acc1')
        self.assertEqual(second['consecutive_hits'], 2)
        self.assertEqual(second['cooldown_seconds'], 180)
        self.assertGreater(second['blocked_until'], time.time())

        # 命中记进 risk_events
        listed = risk_service.events()
        self.assertEqual(listed['total'], 2)
        self.assertEqual(listed['events'][0]['kind'], 'hit')

    def test_streak_decays_after_quiet_period(self):
        """连击要会断。否则 consecutive_hits 只增不减，跑几天后随便一次小命中都按
        最后一档封禁 —— 旧项目「闸门实际永久生效」就是这么来的。"""
        risk_service.record_hit('acc1')
        risk_service.record_hit('acc1')
        self.assertEqual(risk_service.status()['states'][0]['consecutive_hits'], 2)

        # 假装上次命中发生在很久以前
        stale = time.time() - risk_service.STREAK_RESET_SECONDS - 60
        db.execute('UPDATE risk_state SET last_hit_at = ? WHERE account_id = ?', (stale, 'acc1'))

        again = risk_service.record_hit('acc1')
        self.assertEqual(again['consecutive_hits'], 1)
        self.assertEqual(again['cooldown_seconds'], 60)

    def test_streak_does_not_decay_while_recent(self):
        """冷却期内再命中要升级，不能被衰减掉。"""
        risk_service.record_hit('acc1')
        row = db.query_one('SELECT last_hit_at FROM risk_state WHERE account_id = ?', ('acc1',))
        risk_service.record_hit('acc1')
        self.assertGreater(float(row['last_hit_at']), 0)
        self.assertEqual(risk_service.status()['states'][0]['consecutive_hits'], 2)

    def test_record_hit_requires_account(self):
        result = risk_service.record_hit('')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_request')

    def test_events_filter_and_limit(self):
        risk_service.record_hit('acc1')
        risk_service.record_hit('acc2')
        risk_service.record_hit('acc1')

        self.assertEqual(risk_service.events()['total'], 3)
        self.assertEqual(risk_service.events(account_id='acc1')['total'], 2)
        self.assertEqual(risk_service.events(limit=1)['total'], 1)

    # ------------------------------------------------------------------ 自动滑块

    def test_auto_slider_default_off(self):
        self.assertFalse(risk_service.auto_slider_enabled())
        self.assertFalse(risk_service.settings()['auto_slider'])

        risk_service.set_auto_slider(True)
        self.assertTrue(risk_service.auto_slider_enabled())
        self.assertTrue(risk_service.settings()['auto_slider'])

        risk_service.set_auto_slider(False)
        self.assertFalse(risk_service.auto_slider_enabled())

    def test_update_settings(self):
        result = risk_service.update_settings({
            'auto_slider': True, 'cooldown_steps': [10, 20], 'refresh_interval_seconds': 300,
        })
        self.assertTrue(result['auto_slider'])
        self.assertEqual(result['cooldown_steps'], [10, 20])
        self.assertEqual(result['refresh_interval_seconds'], 300)

        # 字符串形式也认
        result = risk_service.update_settings({'cooldown_steps': '7,8,9'})
        self.assertEqual(result['cooldown_steps'], [7, 8, 9])

        # 空值 = 「保持当前阶梯」，不是重置为默认
        self.assertEqual(risk_service.update_settings({'cooldown_steps': '  '})['cooldown_steps'], [7, 8, 9])
        # 全是垃圾项 -> 回落默认
        self.assertEqual(risk_service.update_settings({'cooldown_steps': 'x,-1'})['cooldown_steps'],
                         risk_service.DEFAULT_STEPS)

        bad = risk_service.update_settings('nope')
        self.assertFalse(bad['success'])
        self.assertEqual(bad['code'], 'bad_request')

        # 非法 interval 返回结构化失败，而不是抛 ValueError
        self.assertEqual(risk_service.update_settings({'refresh_interval_seconds': 'abc'})['code'], 'invalid_fields')
        self.assertEqual(risk_service.update_settings({'refresh_interval_seconds': 0})['code'], 'invalid_fields')

    # ------------------------------------------------------------------ 清除冷却（硬规则）

    def test_clear_cooldown_refused_without_verification(self):
        risk_service.record_hit('acc1')
        result = risk_service.clear_cooldown('acc1')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'verification_required')
        # 拒绝也必须留痕
        self.assertTrue(risk_service.is_blocked('acc1'))
        kinds = [e['kind'] for e in risk_service.events()['events']]
        self.assertIn('clear_refused', kinds)
        logs = db.query("SELECT * FROM operation_logs WHERE action = 'clear_cooldown'")
        self.assertEqual(logs[0]['success'], 0)

    def test_clear_cooldown_with_explicit_verified(self):
        risk_service.record_hit('acc1')
        result = risk_service.clear_cooldown('acc1', verified=True)
        self.assertTrue(result['cleared'])
        self.assertEqual(result['state']['consecutive_hits'], 0)
        self.assertFalse(risk_service.is_blocked('acc1'))

    def test_clear_cooldown_with_protocol_credential(self):
        """协议层 captcha 写下 verification_type 后，无需显式 verified 也能清。"""
        risk_service.record_hit('acc1')
        risk_service.mark_verified('acc1', 'slider')
        result = risk_service.clear_cooldown('acc1')
        self.assertTrue(result['cleared'])
        self.assertFalse(risk_service.is_blocked('acc1'))

    def test_mark_verified_requires_account(self):
        result = risk_service.mark_verified('')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_request')

    def test_clear_cooldown_requires_account(self):
        result = risk_service.clear_cooldown('')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_request')

    # ------------------------------------------------------------------ 事件

    def test_events_published(self):
        before = len(events.recent(limit=500))
        risk_service.record_hit('acc1')
        risk_service.set_auto_slider(True)
        risk_service.mark_verified('acc1')
        risk_service.clear_cooldown('acc1')
        topics = [e['topic'] for e in events.recent(limit=500)[before:]]
        self.assertEqual(topics.count('risk.changed'), 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
