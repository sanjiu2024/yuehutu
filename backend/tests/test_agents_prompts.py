"""多 AI 编排 —— 角色与提示词三级覆盖测试（全部离线，不打 LLM）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_agents_prompts.py"
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agents import prompts, roles  # noqa: E402
from backend.app.core import config, db, security  # noqa: E402


class AgentsPromptsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()
        roles.ensure_roles()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.execute('DELETE FROM ai_prompts')
        db.execute("UPDATE ai_roles SET system_prompt = '', provider_id = NULL, model = ''")
        db.execute('DELETE FROM items')

    # ---------------- 角色 ----------------

    def test_roles_are_the_six_contract_roles(self):
        self.assertEqual(roles.ROLES, ('master', 'item', 'cs', 'bargain', 'cross', 'vision'))
        for role in roles.ROLES:
            self.assertTrue(roles.default_prompt(role).strip(), f'{role} 缺默认提示词')

    def test_role_for_conversation(self):
        self.assertEqual(roles.role_for_conversation({'agent_role': 'bargain', 'item_id': 'A'}), 'bargain')
        # agent_role 不合法时按有没有绑定商品回退
        self.assertEqual(roles.role_for_conversation({'agent_role': 'nonsense', 'item_id': 'A'}), 'item')
        self.assertEqual(roles.role_for_conversation({'agent_role': '', 'item_id': ''}), 'cs')
        self.assertEqual(roles.role_for_conversation(None), 'cs')

    def test_bargain_and_vision_are_reachable(self):
        """`bargain`/`vision` 此前在角色表里但没有任何路径会选中它们 —— 死角色。"""
        conv = {'agent_role': '', 'item_id': 'A'}
        # 讲价 → 议价 AI（不是商品客服）
        self.assertEqual(roles.role_for_conversation(conv, '能便宜点吗'), 'bargain')
        self.assertEqual(roles.role_for_conversation(conv, '这个最低多少'), 'bargain')
        # 买家发图 → 看图 AI
        self.assertEqual(
            roles.role_for_conversation(conv, {'content': '看看这个', 'meta': '{"image_url": "http://x/a.jpg"}'}),
            'vision',
        )
        # 手工会话的 agent_role 仍然最高优先，不被消息内容顶掉
        self.assertEqual(
            roles.role_for_conversation({'agent_role': 'bargain', 'item_id': 'A'}, '有货吗'),
            'bargain',
        )
        # 图 + 讲价同时出现时看图优先
        self.assertEqual(
            roles.role_for_conversation(conv, {'content': '能便宜点吗', 'meta': {'image': 'http://x/b.jpg'}}),
            'vision',
        )
        # 普通问句不许被误判成讲价
        self.assertEqual(roles.role_for_conversation(conv, '什么时候发货'), 'item')
        self.assertEqual(roles.role_for_conversation(conv, '有货吗'), 'item')
        self.assertEqual(roles.role_for_conversation(conv, {'content': '有货吗', 'meta': ''}), 'item')

    def test_get_and_update_role(self):
        before = roles.get_role('item')
        self.assertEqual(before['provider_id'], None)
        self.assertTrue(before['default_prompt'])

        after = roles.update_role('item', temperature=0.2, system_prompt='你是小助手')
        self.assertAlmostEqual(after['temperature'], 0.2)
        self.assertEqual(after['system_prompt'], '你是小助手')
        # 未传的字段保持原值
        self.assertEqual(after['enabled'], True)

        with self.assertRaises(ValueError):
            roles.get_role('not_a_role')

    # ---------------- 三级覆盖 ----------------

    def test_default_when_nothing_configured(self):
        text = prompts.system_prompt('item')
        self.assertEqual(text, roles.default_prompt('item'))

    def test_role_base_prompt_is_kept_and_appended_to(self):
        """三级覆盖的结果是**拼接**在角色基础提示词后面，不是替换。"""
        roles.update_role('item', system_prompt='【角色基础】我是这个商品的客服')
        prompts.upsert_prompt('global', content='【全局】不许骂人', role='item')

        text = prompts.system_prompt('item')
        self.assertIn('【角色基础】我是这个商品的客服', text)
        self.assertIn('【全局】不许骂人', text)
        self.assertLess(text.index('【角色基础】'), text.index('【全局】'))

    def test_scope_order_global_account_item(self):
        """越具体越靠后：global → account → item。"""
        db.execute("INSERT INTO items(account_id, item_id, title, price) VALUES('acc1','itemA','测试商品',10)")
        prompts.upsert_prompt('global', content='GLOBAL', role='item')
        prompts.upsert_prompt('account', 'acc1', content='ACCOUNT', role='item')
        prompts.upsert_prompt('item', 'itemA', content='ITEM', role='item')

        text = prompts.system_prompt('item', account_id='acc1', item_id='itemA')
        self.assertIn('GLOBAL', text)
        self.assertIn('ACCOUNT', text)
        self.assertIn('ITEM', text)
        self.assertLess(text.index('GLOBAL'), text.index('ACCOUNT'))
        self.assertLess(text.index('ACCOUNT'), text.index('ITEM'))

    def test_account_inferred_from_item(self):
        """只给 item_id 时，account 层要能自动从 items 表推出来。"""
        db.execute("INSERT INTO items(account_id, item_id, title, price) VALUES('acc9','itemZ','商品Z',1)")
        prompts.upsert_prompt('account', 'acc9', content='ACCOUNT_FROM_ITEM', role='cs')

        text = prompts.system_prompt('cs', item_id='itemZ')
        self.assertIn('ACCOUNT_FROM_ITEM', text)

    def test_priority_wins_within_same_scope(self):
        db.execute(
            "INSERT INTO ai_prompts(role, scope, content, priority) VALUES('item','global','LOW',1)")
        db.execute(
            "INSERT INTO ai_prompts(role, scope, content, priority) VALUES('item','global','HIGH',9)")
        text = prompts.system_prompt('item')
        self.assertIn('HIGH', text)
        self.assertNotIn('LOW', text)

    def test_role_specific_beats_generic_at_same_priority(self):
        db.execute("INSERT INTO ai_prompts(role, scope, content, priority) VALUES('','global','GENERIC',5)")
        db.execute("INSERT INTO ai_prompts(role, scope, content, priority) VALUES('item','global','SPECIFIC',5)")
        self.assertIn('SPECIFIC', prompts.system_prompt('item'))
        self.assertNotIn('GENERIC', prompts.system_prompt('item'))
        # 其它角色拿到的是通用那条
        self.assertIn('GENERIC', prompts.system_prompt('bargain'))

    def test_disabled_prompt_is_ignored(self):
        db.execute(
            "INSERT INTO ai_prompts(role, scope, content, priority, enabled) VALUES('item','global','OFF',99,0)")
        self.assertNotIn('OFF', prompts.system_prompt('item'))

    def test_other_role_prompt_not_leaked(self):
        prompts.upsert_prompt('global', content='MASTER_ONLY', role='master')
        self.assertNotIn('MASTER_ONLY', prompts.system_prompt('item'))

    def test_item_scope_needs_owner(self):
        prompts.upsert_prompt('item', 'itemA', content='ITEM_OWNER', role='item')
        self.assertNotIn('ITEM_OWNER', prompts.system_prompt('item', account_id='acc1'))
        self.assertIn('ITEM_OWNER', prompts.system_prompt('item', account_id='acc1', item_id='itemA'))

    # ---------------- upsert / list / delete ----------------

    def test_upsert_is_idempotent_on_same_key(self):
        first = prompts.upsert_prompt('global', content='v1', role='item')
        second = prompts.upsert_prompt('global', content='v2', role='item')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['content'], 'v2')
        self.assertEqual(len(prompts.list_prompts(scope='global')), 1)

    def test_upsert_rejects_bad_scope_and_role(self):
        with self.assertRaises(ValueError):
            prompts.upsert_prompt('galaxy', content='x')
        with self.assertRaises(ValueError):
            prompts.upsert_prompt('global', content='x', role='nobody')

    def test_list_prompts_filters(self):
        prompts.upsert_prompt('global', content='g', role='item')
        prompts.upsert_prompt('item', 'itemA', content='i', role='item')
        prompts.upsert_prompt('item', 'itemB', content='i2', role='cs')
        self.assertEqual(len(prompts.list_prompts(scope='global')), 1)
        self.assertEqual(len(prompts.list_prompts(scope='item', owner_id='itemA')), 1)
        self.assertEqual(len(prompts.list_prompts(role='cs')), 1)

    def test_delete_prompt(self):
        row = prompts.upsert_prompt('global', content='bye', role='item')
        self.assertTrue(prompts.delete_prompt(row['id']))
        self.assertIsNone(prompts.get_prompt(row['id']))

    def test_seed_defaults_fills_empty_only(self):
        roles.update_role('cs', system_prompt='我自己写的')
        written = prompts.seed_defaults()
        self.assertGreaterEqual(written, 1)
        self.assertEqual(roles.get_role('cs')['system_prompt'], '我自己写的')


class AgentsPromptsApiTest(unittest.TestCase):
    """路由层冒烟：只挂自己的 router，不依赖别的并行工作流。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.app.api import agents as agents_api

        app = FastAPI()
        app.include_router(agents_api.router)
        cls.client = TestClient(app)

        user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('tester','x',1)"))
        cls.headers = {'Authorization': f'Bearer {security.issue_token(user_id)}'}

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def test_requires_token(self):
        resp = self.client.get('/api/agents/roles')
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_list_roles(self):
        resp = self.client.get('/api/agents/roles', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual([r['role'] for r in data],
                         ['master', 'item', 'cs', 'bargain', 'cross', 'vision'])

    def test_put_role_and_preview_prompt(self):
        resp = self.client.put('/api/agents/roles/item', headers=self.headers,
                               json={'system_prompt': 'API_BASE', 'temperature': 0.4})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['system_prompt'], 'API_BASE')

        resp = self.client.post('/api/agents/prompts', headers=self.headers,
                                json={'scope': 'global', 'role': 'item', 'content': 'API_GLOBAL', 'priority': 3})
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.get('/api/agents/roles/item/prompt-preview', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        text = resp.json()['data']['prompt']
        self.assertIn('API_BASE', text)
        self.assertIn('API_GLOBAL', text)

    def test_prompt_crud_and_bad_scope(self):
        resp = self.client.post('/api/agents/prompts', headers=self.headers,
                                json={'scope': 'item', 'item_id': 'X1', 'content': 'ITEM_X1', 'role': 'item'})
        self.assertEqual(resp.status_code, 200, resp.text)
        prompt_id = resp.json()['data']['id']

        resp = self.client.get('/api/agents/prompts', headers=self.headers,
                               params={'scope': 'item', 'owner_id': 'X1'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()['data']['items']), 1)

        resp = self.client.put(f'/api/agents/prompts/{prompt_id}', headers=self.headers,
                               json={'content': 'ITEM_X1_V2'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['content'], 'ITEM_X1_V2')

        resp = self.client.delete(f'/api/agents/prompts/{prompt_id}', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.get('/api/agents/prompts', headers=self.headers, params={'scope': 'galaxy'})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_messages_endpoint(self):
        db.execute(
            "INSERT INTO agent_messages(from_agent, to_agent, kind, topic, payload) "
            "VALUES('item:A','cross','ask','other_item_info','{}')")
        resp = self.client.get('/api/agents/messages', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = resp.json()['data']
        self.assertTrue(any(r['kind'] == 'ask' for r in rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
