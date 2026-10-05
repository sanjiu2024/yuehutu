"""多 AI 编排 —— 角色与提示词三级覆盖测试（全部离线，不打 LLM）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_agents_prompts.py"
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

    # ---------------- AI-5：去重键丢掉了 name ----------------

    def test_same_scope_different_name_coexist(self):
        """**AI-5**：去重键原先不含 `name`，第二条同 scope 的提示词会静默吃掉第一条
        （实测两次 POST 都返回同一个 id，话术A 无声消失）。"""
        a = prompts.upsert_prompt('global', content='话术A', role='item', name='话术A')
        b = prompts.upsert_prompt('global', content='话术B', role='item', name='话术B')
        self.assertNotEqual(a['id'], b['id'])
        rows = prompts.list_prompts(scope='global', role='item')
        self.assertEqual({r['name'] for r in rows}, {'话术A', '话术B'})
        # 第一条真的还在，没被第二条覆盖
        self.assertEqual(prompts.get_prompt(a['id'])['content'], '话术A')
        self.assertEqual(prompts.get_prompt(b['id'])['content'], '话术B')

    def test_same_name_still_upserts(self):
        """同名同 scope 仍然是覆盖（幂等），不是无限插行。"""
        a = prompts.upsert_prompt('global', content='v1', role='item', name='同一个')
        b = prompts.upsert_prompt('global', content='v2', role='item', name='同一个')
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(prompts.get_prompt(a['id'])['content'], 'v2')
        self.assertEqual(len(prompts.list_prompts(scope='global', role='item')), 1)

    def test_different_role_same_name_coexist(self):
        a = prompts.upsert_prompt('global', content='item 版', role='item', name='问候')
        b = prompts.upsert_prompt('global', content='cs 版', role='cs', name='问候')
        self.assertNotEqual(a['id'], b['id'])

    # ---------------- AI-6：scope=item 但没有 owner 的孤儿行 ----------------

    def test_item_scope_without_owner_is_rejected(self):
        """**AI-6**：原先 `POST /api/agents/prompts {"scope":"item"}` 能建出
        `item_id=''` 的孤儿行 —— 永远不生效，却在列表里骗人。"""
        with self.assertRaises(prompts.PromptValidationError) as ctx:
            prompts.upsert_prompt('item', content='孤儿', role='item')
        err = ctx.exception
        self.assertEqual(err.code, 'invalid_fields')
        self.assertIn('item_id', err.invalid_fields)

        # 结构化信封：前端可以直接把字段标红，而不是只看到一句 message
        env = err.as_envelope()
        self.assertFalse(env['success'])
        self.assertEqual(env['code'], 'invalid_fields')
        self.assertEqual(env['message'], err.message)
        self.assertEqual(env['invalid_fields'], err.invalid_fields)
        self.assertIn('item_id', env['invalid_fields'])
        self.assertEqual(json.loads(err.as_json()), env)

        # 一行都没落库
        self.assertEqual(prompts.list_prompts(scope='item'), [])

    def test_account_scope_without_owner_is_rejected(self):
        with self.assertRaises(prompts.PromptValidationError) as ctx:
            prompts.upsert_prompt('account', content='孤儿', role='item')
        self.assertIn('account_id', ctx.exception.invalid_fields)
        self.assertEqual(prompts.list_prompts(scope='account'), [])

    def test_blank_owner_counts_as_missing(self):
        for blank in ('', '   '):
            with self.subTest(blank=repr(blank)):
                with self.assertRaises(prompts.PromptValidationError):
                    prompts.upsert_prompt('item', content='x', role='item', item_id=blank)
                with self.assertRaises(prompts.PromptValidationError):
                    prompts.upsert_prompt('account', content='x', role='item', account_id=blank)
        self.assertEqual(prompts.list_prompts(scope='item'), [])

    def test_global_scope_needs_no_owner(self):
        row = prompts.upsert_prompt('global', content='全局', role='item')
        self.assertEqual(row['account_id'], '')
        self.assertEqual(row['item_id'], '')

    def test_owner_can_be_passed_explicitly(self):
        """除了位置参数 `owner_id`，也要能用具名 `account_id` / `item_id`。"""
        item_row = prompts.upsert_prompt('item', content='X', role='item', item_id='itemA')
        self.assertEqual(item_row['item_id'], 'itemA')
        self.assertEqual(item_row['account_id'], '')
        account_row = prompts.upsert_prompt('account', content='Y', role='item', account_id='acc1')
        self.assertEqual(account_row['account_id'], 'acc1')
        self.assertEqual(account_row['item_id'], '')
        # 具名参数与位置参数给的是同一个结果（幂等）
        again = prompts.upsert_prompt('item', 'itemA', content='X2', role='item')
        self.assertEqual(again['id'], item_row['id'])

    def test_validation_error_is_still_a_value_error(self):
        """向后兼容：`api/agents.py` 现在 `except ValueError`，不能漏接。"""
        self.assertTrue(issubclass(prompts.PromptValidationError, ValueError))
        with self.assertRaises(ValueError):
            prompts.upsert_prompt('galaxy', content='x')
        # `validate_prompt_fields` 本身是**查错**函数（返回 {字段: 原因}，空 dict 表示没问题），
        # 抛异常的是 `upsert_prompt`。
        errors = prompts.validate_prompt_fields('item', item_id='')
        self.assertIn('item_id', errors)
        self.assertEqual(prompts.validate_prompt_fields('item', item_id='itemA'), {})


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

    def test_orphan_item_prompt_is_rejected_over_http(self):
        """**AI-6 端到端**：HTTP 出口也要挡住孤儿行，并且库里真的没有脏数据。

        注：`api/agents.py` 目前把 `ValueError` 转成 `HTTPException(400, detail=str(exc))`，
        所以 400 的 body 是 `{"detail": "..."}` 而不是结构化信封（该文件不在 task-18 写范围）。
        """
        before = len(db.query("SELECT id FROM ai_prompts WHERE scope = 'item'"))
        resp = self.client.post('/api/agents/prompts', headers=self.headers,
                                json={'scope': 'item', 'content': '孤儿', 'role': 'item'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('item_id', resp.text)
        after = len(db.query("SELECT id FROM ai_prompts WHERE scope = 'item'"))
        self.assertEqual(after, before, '被拒绝的请求不能留下孤儿行')
        self.assertEqual(
            len(db.query("SELECT id FROM ai_prompts WHERE scope = 'item' AND item_id = ''")), 0)

    def test_two_prompts_with_different_names_both_visible_over_http(self):
        """**AI-5 端到端**：两条话术都要能在列表里看到。"""
        for name in ('话术A', '话术B'):
            resp = self.client.post('/api/agents/prompts', headers=self.headers,
                                    json={'scope': 'global', 'role': 'item', 'content': name,
                                          'name': name})
            self.assertEqual(resp.status_code, 200, resp.text)
        resp = self.client.get('/api/agents/prompts', headers=self.headers,
                               params={'scope': 'global', 'role': 'item'})
        self.assertEqual(resp.status_code, 200, resp.text)
        names = {r['name'] for r in resp.json()['data']['items']}
        # 这个 TestCase 的库在 setUpClass 建一次、方法之间不重建，所以只断言"两条都在"。
        # 旧实现（去重键不含 name）下第二条会把第一条顶掉，只剩余 '话术B' → 这里仍会挂。
        self.assertIn('话术A', names)
        self.assertIn('话术B', names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
