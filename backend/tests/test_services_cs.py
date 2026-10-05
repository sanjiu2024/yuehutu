"""客服服务测试：意图分类、快捷回复、模板库（settings + ai_prompts）、发货提醒。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_services_cs.py"

**不打真实闲鱼平台**：纯本地模板库，不碰协议层。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import config, db, events  # noqa: E402
from backend.app.services import common, cs  # noqa: E402


class CsServiceTest(unittest.TestCase):
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
        db.execute('DELETE FROM ai_prompts')
        db.execute('DELETE FROM settings')
        db.execute('DELETE FROM operation_logs')

    # ------------------------------------------------------------------ 意图

    def test_classify_intent(self):
        cases = {
            '能便宜点吗': 'price',
            '这个包邮不': 'price',
            '什么时候发货': 'logistics',
            '快递单号给我一下': 'logistics',
            '我拍下了，帮我改价': 'order',
            '我要退货': 'aftersale',
            '几成新？有划痕吗': 'quality',
            '尺寸多大': 'spec',
            '还有货吗': 'stock',
        }
        for text, expected in cases.items():
            self.assertEqual(cs.classify_intent(text), expected, f'{text} -> {expected}')

    def test_classify_intent_unknown_and_empty(self):
        self.assertEqual(cs.classify_intent(''), 'unknown')
        self.assertEqual(cs.classify_intent('   '), 'unknown')
        self.assertEqual(cs.classify_intent('今天天气不错'), 'unknown')

    def test_classify_intent_prefers_longer_keyword(self):
        """长关键词权重更高：'什么时候发货' 里 '什么时候发' 属于订单，但物流词更长。"""
        self.assertEqual(cs.classify_intent('请问什么时候发货呀'), 'logistics')

    # ------------------------------------------------------------------ 快捷回复

    def test_quick_replies_always_non_empty(self):
        for intent in list(cs.INTENT_KEYWORDS) + ['unknown']:
            replies = cs.quick_replies(intent)
            self.assertTrue(replies, f'{intent} 应该有候选回复')
            self.assertTrue(all(isinstance(r, str) and r for r in replies))

        # 未知意图也有兜底
        self.assertEqual(cs.quick_replies('nonsense'), cs.DEFAULT_QUICK_REPLIES['unknown'])

    def test_suggest(self):
        result = cs.suggest('能便宜点吗')
        self.assertEqual(result['intent'], 'price')
        self.assertTrue(result['replies'])

    def test_quick_replies_include_custom_template(self):
        cs.save_template('custom_price', {'name': '自定义议价', 'intent': 'price', 'content': '这个真不刀了'})
        replies = cs.quick_replies('price')
        self.assertIn('这个真不刀了', replies)

    # ------------------------------------------------------------------ 模板库

    def test_default_templates_seeded(self):
        templates = cs.list_templates()
        ids = [t['id'] for t in templates]
        for expected in ('greeting', 'ship_reminder', 'ship_delay'):
            self.assertIn(expected, ids)
        self.assertTrue(all(t['enabled'] for t in templates))

    def test_save_template_creates_and_updates(self):
        created = cs.save_template('my_tpl', {'name': '我的模板', 'intent': 'price', 'content': '第一版'})
        self.assertEqual(created['content'], '第一版')
        self.assertEqual(created['source'], 'ai_prompts')   # 正文同步写进了 ai_prompts

        updated = cs.save_template('my_tpl', {'content': '第二版'})
        self.assertEqual(updated['content'], '第二版')
        self.assertEqual(updated['name'], '我的模板')        # 未传的字段保留

        # 幂等：不产生重复模板
        ids = [t['id'] for t in cs.list_templates()]
        self.assertEqual(ids.count('my_tpl'), 1)

        prompts = db.query("SELECT * FROM ai_prompts WHERE role = 'cs' AND name = '我的模板'")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]['content'], '第二版')

    def test_save_template_persists_into_ai_prompts(self):
        cs.save_template('greeting', {'content': '新问候语'})
        row = db.query_one("SELECT content FROM ai_prompts WHERE role = 'cs' AND name = '自动问候'")
        self.assertEqual(row['content'], '新问候语')
        # 再读模板时正文以 ai_prompts 为准
        self.assertEqual(cs.get_template('greeting')['content'], '新问候语')

    def test_save_template_enabled_flag(self):
        cs.save_template('t1', {'name': 'T1', 'intent': 'price', 'content': 'x', 'enabled': False})
        self.assertFalse(cs.get_template('t1')['enabled'])
        # 停用的模板不进快捷回复候选
        self.assertNotIn('x', cs.quick_replies('price'))

    def test_save_template_requires_id(self):
        result = cs.save_template('', {'content': 'x'})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_request')

    def test_get_template_missing(self):
        result = cs.get_template('nope')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_delete_template(self):
        cs.save_template('gone', {'name': '待删', 'intent': 'price', 'content': 'x'})
        result = cs.delete_template('gone')
        self.assertEqual(result['deleted'], 'gone')
        self.assertFalse(cs.get_template('gone')['success'])

        missing = cs.delete_template('gone')
        self.assertFalse(missing['success'])
        self.assertEqual(missing['code'], 'not_found')

    # ------------------------------------------------------------------ 渲染与业务文案

    def test_render_fills_placeholders(self):
        result = cs.render('ship_reminder', tracking_no='SF123', company='顺丰')
        self.assertIn('SF123', result['content'])
        self.assertNotIn('{tracking_no}', result['content'])

    def test_render_keeps_missing_placeholders(self):
        """缺参数不炸，占位符原样保留（人工再补）。"""
        result = cs.render('ship_reminder')
        self.assertIn('{tracking_no}', result['content'])

    def test_render_missing_template(self):
        result = cs.render('nope')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_greeting(self):
        result = cs.greeting(item_title='iPhone 13')
        self.assertTrue(result['content'])

    def test_shipping_reminder_picks_template_by_tracking(self):
        with_tracking = cs.shipping_reminder(tracking_no='SF001', company='顺丰')
        self.assertIn('SF001', with_tracking['content'])

        without = cs.shipping_reminder()
        self.assertTrue(without['content'])
        self.assertNotEqual(without['content'], with_tracking['content'])

    def test_faq(self):
        result = cs.faq('包邮吗')
        self.assertEqual(result['intent'], 'price')
        self.assertTrue(result['content'])
        self.assertTrue(result['replies'])

    # ------------------------------------------------------------------ 事件

    def test_events_published(self):
        before = len(events.recent(limit=500))
        cs.save_template('e1', {'name': 'E1', 'intent': 'price', 'content': 'x'})
        cs.delete_template('e1')
        topics = [e['topic'] for e in events.recent(limit=500)[before:]]
        self.assertEqual(topics.count('cs.templates_changed'), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
