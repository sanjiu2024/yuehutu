"""Provider（API Key 统一管理）测试 —— **全部 mock，绝不打真实 LLM API**。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_agents_providers.py"

覆盖：
- `llm.resolve_provider` 的解析顺序与默认回退（显式 id → 角色 → settings → is_default → 第一条 enabled）
- 无可用提供者 / 已停用 / 没模型 的报错
- `/api/providers` CRUD、is_default 切换（同时写 settings）、api_key 不回显明文
- `POST /api/providers/{id}/test` 成功与失败两条路径（mock `llm.chat`）
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agents import llm, roles  # noqa: E402
from backend.app.core import config, db, security  # noqa: E402


def _fake_chat(text='你好，我是 mock 模型'):
    async def _chat(messages, **kwargs):
        return {
            'text': text,
            'usage': {'prompt_tokens': 3, 'completion_tokens': 5, 'total_tokens': 8},
            'model': kwargs.get('model') or 'mock-model',
            'provider_id': kwargs.get('provider_id') or 1,
            'tool_calls': [],
            'raw': {},
        }

    return _chat


class ProvidersTestBase(unittest.TestCase):
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
        db.reset_connection()  # 连接是线程本地缓存的，每个测试方法都要重新绑定临时库
        db.execute('DELETE FROM providers')
        db.execute('DELETE FROM agent_runs')
        db.execute("UPDATE ai_roles SET provider_id = NULL, model = ''")
        db.execute("DELETE FROM settings WHERE key = 'ai.default_provider_id'")

    def add_provider(self, name, *, base_url='http://127.0.0.1:9/v1', api_key='sk-x',
                     model='mock-model', enabled=1, is_default=0):
        return int(db.execute(
            'INSERT INTO providers(name, base_url, api_key, default_model, enabled, is_default) '
            'VALUES(?,?,?,?,?,?)',
            (name, base_url, api_key, model, enabled, is_default),
        ))


class ResolveProviderTest(ProvidersTestBase):
    def test_explicit_id_wins(self):
        first = self.add_provider('a', is_default=1)
        second = self.add_provider('b')
        got = llm.resolve_provider(provider_id=second)
        self.assertEqual(got['id'], second)
        self.assertEqual(got['name'], 'b')
        self.assertEqual(got['api_key'], 'sk-x')
        self.assertNotEqual(got['id'], first)

    def test_role_provider_beats_default(self):
        default_id = self.add_provider('default', is_default=1)
        role_id = self.add_provider('role-owned')
        db.execute("UPDATE ai_roles SET provider_id = ? WHERE role = 'bargain'", (role_id,))
        self.assertEqual(llm.resolve_provider('bargain')['id'], role_id)
        # 没配角色的角色仍然走默认
        self.assertEqual(llm.resolve_provider('item')['id'], default_id)

    def test_settings_default_beats_is_default_flag(self):
        self.add_provider('flagged', is_default=1)
        configured = self.add_provider('configured')
        db.execute("INSERT INTO settings(key, value) VALUES('ai.default_provider_id', ?)",
                   (str(configured),))
        self.assertEqual(llm.resolve_provider('item')['id'], configured)

    def test_is_default_flag_used_when_no_setting(self):
        self.add_provider('first-enabled')
        flagged = self.add_provider('flagged', is_default=1)
        self.assertEqual(llm.resolve_provider('item')['id'], flagged)

    def test_falls_back_to_first_enabled(self):
        disabled = self.add_provider('disabled', enabled=0)
        alive = self.add_provider('alive')
        got = llm.resolve_provider('item')
        self.assertEqual(got['id'], alive)
        self.assertNotEqual(got['id'], disabled)

    def test_no_provider_raises(self):
        with self.assertRaises(llm.LLMError) as ctx:
            llm.resolve_provider('item')
        self.assertIn('提供者', str(ctx.exception))

    def test_disabled_explicit_provider_raises(self):
        pid = self.add_provider('off', enabled=0)
        with self.assertRaises(llm.LLMError) as ctx:
            llm.resolve_provider(provider_id=pid)
        self.assertIn('停用', str(ctx.exception))
        self.assertEqual(ctx.exception.provider_id, pid)

    def test_missing_provider_raises(self):
        with self.assertRaises(llm.LLMError) as ctx:
            llm.resolve_provider(provider_id=4242)
        self.assertIn('不存在', str(ctx.exception))

    def test_role_model_overrides_default_model(self):
        pid = self.add_provider('p', model='base-model')
        db.execute("UPDATE ai_roles SET provider_id = ?, model = 'role-model' WHERE role = 'vision'", (pid,))
        self.assertEqual(llm.resolve_provider('vision')['model'], 'role-model')
        self.assertEqual(llm.resolve_provider('cs')['model'], 'base-model')

    def test_provider_without_model_raises(self):
        pid = self.add_provider('nomodel', model='')
        with self.assertRaises(llm.LLMError) as ctx:
            llm.resolve_provider(provider_id=pid)
        self.assertIn('模型', str(ctx.exception))


class ProviderApiTest(ProvidersTestBase):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.app.api import providers as providers_api

        app = FastAPI()
        app.include_router(providers_api.router)
        self.client = TestClient(app)
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('p','x',1)"))
        self.headers = {'Authorization': f'Bearer {security.issue_token(self.user_id)}'}

    def tearDown(self):
        db.execute('DELETE FROM users')

    def test_requires_token(self):
        self.assertEqual(self.client.get('/api/providers').status_code, 401)

    def test_crud_and_key_masking(self):
        resp = self.client.post('/api/providers', headers=self.headers, json={
            'name': 'deepseek', 'base_url': 'https://api.deepseek.com/v1/',
            'api_key': 'sk-super-secret-key-123456', 'default_model': 'deepseek-chat',
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        pid = data['id']
        self.assertEqual(data['base_url'], 'https://api.deepseek.com/v1')  # 去掉尾斜杠
        self.assertTrue(data['has_key'])
        self.assertNotIn('api_key', data)
        self.assertNotIn('sk-super-secret-key-123456', resp.text)
        self.assertTrue(data['api_key_masked'].startswith('sk-s'))
        self.assertTrue(data['api_key_masked'].endswith('3456'))
        self.assertNotIn('secret', data['api_key_masked'])

        # 列表也不回明文
        listing = self.client.get('/api/providers', headers=self.headers)
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn('sk-super-secret-key-123456', listing.text)
        self.assertEqual(len(listing.json()['data']), 1)

        # 更新：不传 api_key 时不能把真 key 冲掉
        resp = self.client.put(f'/api/providers/{pid}', headers=self.headers,
                               json={'name': 'deepseek-2'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['name'], 'deepseek-2')
        row = db.query_one('SELECT api_key FROM providers WHERE id = ?', (pid,))
        self.assertEqual(row['api_key'], 'sk-super-secret-key-123456')

        # 显式传新 key 才覆盖
        self.client.put(f'/api/providers/{pid}', headers=self.headers, json={'api_key': 'sk-new'})
        self.assertEqual(db.query_one('SELECT api_key FROM providers WHERE id = ?', (pid,))['api_key'], 'sk-new')

        resp = self.client.delete(f'/api/providers/{pid}', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(self.client.get('/api/providers', headers=self.headers).json()['data']), 0)

    def test_create_requires_name_and_base_url(self):
        resp = self.client.post('/api/providers', headers=self.headers, json={'name': 'x'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn('base_url', resp.text)

    def test_missing_provider_is_404(self):
        self.assertEqual(self.client.put('/api/providers/999', headers=self.headers,
                                         json={'name': 'x'}).status_code, 404)
        self.assertEqual(self.client.delete('/api/providers/999', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/api/providers/999/test', headers=self.headers).status_code, 404)

    def test_set_default_writes_settings_and_clears_others(self):
        first = self.add_provider('a', is_default=1)
        second = self.add_provider('b')
        resp = self.client.post(f'/api/providers/{second}/default', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['is_default'])

        rows = {int(r['id']): int(r['is_default']) for r in db.query('SELECT id, is_default FROM providers')}
        self.assertEqual(rows[second], 1)
        self.assertEqual(rows[first], 0)

        setting = db.query_one("SELECT value FROM settings WHERE key = 'ai.default_provider_id'")
        self.assertEqual(setting['value'], str(second))
        # resolve_provider 真的会跟着走
        self.assertEqual(llm.resolve_provider('item')['id'], second)

    def test_set_default_via_put_too(self):
        pid = self.add_provider('only')
        resp = self.client.put(f'/api/providers/{pid}/default', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()['data']['is_default'])

    def test_delete_detaches_roles(self):
        pid = self.add_provider('gone')
        db.execute("UPDATE ai_roles SET provider_id = ? WHERE role = 'item'", (pid,))
        self.client.delete(f'/api/providers/{pid}', headers=self.headers)
        row = db.query_one("SELECT provider_id FROM ai_roles WHERE role = 'item'")
        self.assertIsNone(row['provider_id'])

    def test_test_endpoint_success_is_mocked(self):
        pid = self.add_provider('ok', api_key='sk-ok')
        seen = {}

        async def fake(messages, **kwargs):
            seen.update(kwargs)
            seen['messages'] = messages
            return {'text': '你好呀', 'usage': {'total_tokens': 8}, 'model': 'mock-model',
                    'provider_id': pid, 'tool_calls': [], 'raw': {}}

        with mock.patch.object(llm, 'chat', fake):
            resp = self.client.post(f'/api/providers/{pid}/test', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'])
        self.assertTrue(body['data']['ok'])
        self.assertEqual(body['data']['sample'], '你好呀')
        self.assertIn('latency_ms', body['data'])
        self.assertEqual(seen['provider_id'], pid)

    def test_test_endpoint_reports_failure_without_500(self):
        pid = self.add_provider('bad')

        async def boom(messages, **kwargs):
            raise llm.LLMError('HTTP 401: invalid api key', status=401, provider_id=pid)

        with mock.patch.object(llm, 'chat', boom):
            resp = self.client.post(f'/api/providers/{pid}/test', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertFalse(data['ok'])
        self.assertIn('401', data['error'])


class ProviderToolTest(ProvidersTestBase):
    def test_providers_upsert_tool_writes_row(self):
        from backend.app.agents import tools

        result = asyncio.run(tools.call_tool('providers_upsert', {
            'name': 'tool-provider', 'base_url': 'http://127.0.0.1:9/v1',
            'api_key': 'sk-tool', 'default_model': 'mock-model',
        }))
        self.assertTrue(result['ok'], result)
        row = db.query_one("SELECT * FROM providers WHERE name = 'tool-provider'")
        self.assertIsNotNone(row)
        self.assertEqual(row['api_key'], 'sk-tool')

    def test_providers_list_tool_hides_key(self):
        from backend.app.agents import tools

        self.add_provider('listed', api_key='sk-hidden')
        result = asyncio.run(tools.call_tool('providers_list', {}))
        self.assertTrue(result['ok'], result)
        self.assertNotIn('api_key', result['providers'][0])
        self.assertNotIn('sk-hidden', str(result))

    def test_llm_chat_uses_resolved_provider_and_records_run(self):
        """`llm.chat` 的 HTTP 层被 mock，但 provider 解析 + agent_runs 落库是真跑。"""
        pid = self.add_provider('recorder', model='rec-model')

        class _Resp:
            status_code = 200
            text = '{}'

            def json(self):
                return {'choices': [{'message': {'content': 'hi'}}],
                        'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9}}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None, headers=None):
                self.url = url
                self.body = json
                self.headers = headers
                return _Resp()

        client = _Client()
        with mock.patch('backend.app.agents.llm.httpx.AsyncClient', return_value=client):
            result = asyncio.run(llm.chat([{'role': 'user', 'content': '在吗'}], role='cs'))

        self.assertEqual(result['text'], 'hi')
        self.assertEqual(result['model'], 'rec-model')
        self.assertEqual(result['provider_id'], pid)
        self.assertEqual(client.url, 'http://127.0.0.1:9/v1/chat/completions')
        self.assertEqual(client.headers['Authorization'], 'Bearer sk-x')
        self.assertEqual(client.body['model'], 'rec-model')

        run = db.query_one('SELECT * FROM agent_runs ORDER BY id DESC LIMIT 1')
        self.assertEqual(run['status'], 'ok')
        self.assertEqual(run['role'], 'cs')
        self.assertEqual(run['prompt_tokens'], 7)
        self.assertEqual(run['completion_tokens'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
