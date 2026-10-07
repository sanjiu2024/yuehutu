"""`agent_chat_messages`（主管 AI 对话落库）的单测。

覆盖：append 返回值与默认值、分页（limit/before_id/total/has_more）、thread 隔离、
clear、失败行也落库、tool_calls JSON 往返、可选字段落成 NULL/0、参数化 SQL；
以及路由层：`POST /api/agents/master/chat` 两个回合都落库（含失败）、
`GET/DELETE /api/agents/chat/messages`、`/messages` 与 `/runs` 新增的 total/offset。

跑法（仓库根）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_agent_history.py"
不打真实 LLM：`llm.chat` 全部打桩。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.agents import history, llm, roles, runtime  # noqa: E402
from backend.app.core import config, db, security  # noqa: E402


def _stub_chat(payload):
    """把 `llm.chat` 换成直接返回 payload 的协程，不联网。"""
    async def fake(*args, **kwargs):
        return payload
    return fake


class AgentHistoryTest(unittest.TestCase):
    """纯数据层。"""

    def setUp(self):
        # 每个用例都换一次连接：db 的连接是线程本地缓存的，不重置会读到上一个库。
        # 表在类内跨用例复用，先清空再种一行「基准」。
        db.reset_connection()
        db.execute('DELETE FROM agent_chat_messages')
        self.row = history.append('master', 'user', '你好')
        self.assertGreater(self.row, 0)

    def tearDown(self):
        db.reset_connection()

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

    def test_append_returns_id_and_defaults(self):
        page = history.list_messages('master', limit=10)
        item = page['items'][0]
        self.assertIsInstance(item['id'], int)
        self.assertEqual(item['thread'], 'master')
        self.assertEqual(item['role'], 'user')
        self.assertEqual(item['content'], '你好')
        self.assertEqual(item['tool_calls'], [])
        self.assertIsNone(item['provider_id'])       # 拿不到就是 NULL，不编 0
        self.assertEqual(item['model'], '')
        self.assertEqual(item['latency_ms'], 0)
        self.assertEqual(item['tokens'], 0)
        self.assertEqual(item['status'], 'ok')
        self.assertEqual(item['error'], '')
        self.assertTrue(item['created_at'])

    def test_append_returns_increasing_ids(self):
        second = history.append('master', 'assistant', '在的')
        self.assertGreater(second, self.row)

    def test_tool_calls_json_round_trip(self):
        calls = [{'name': 'items_list', 'args': {'account_id': 'acc1'}, 'result': {'ok': True}}]
        row_id = history.append('master', 'assistant', '查好了', tool_calls=calls)
        raw = db.query_one('SELECT tool_calls FROM agent_chat_messages WHERE id = ?', (row_id,))
        self.assertIsInstance(raw['tool_calls'], str)
        page = history.list_messages('master', limit=10)
        stored = [i for i in page['items'] if i['id'] == row_id][0]
        self.assertEqual(stored['tool_calls'], calls)

    def test_tool_calls_accepts_json_string(self):
        row_id = history.append('master', 'assistant', 'x', tool_calls='[{"name":"risk_status"}]')
        stored = [i for i in history.list_messages('master')['items'] if i['id'] == row_id][0]
        self.assertEqual(stored['tool_calls'], [{'name': 'risk_status'}])

    def test_bad_tool_calls_json_becomes_empty_list(self):
        row_id = history.append('master', 'assistant', 'x', tool_calls='不是 JSON')
        stored = [i for i in history.list_messages('master')['items'] if i['id'] == row_id][0]
        self.assertEqual(stored['tool_calls'], [])

    def test_error_row_is_persisted(self):
        row_id = history.append('master', 'assistant', '', status='error', error='连接超时',
                                latency_ms=1200, tool_calls=[{'name': 'items_list', 'args': {}, 'result': {}}])
        stored = [i for i in history.list_messages('master')['items'] if i['id'] == row_id][0]
        self.assertEqual(stored['status'], 'error')
        self.assertEqual(stored['error'], '连接超时')
        self.assertEqual(stored['latency_ms'], 1200)
        self.assertEqual(len(stored['tool_calls']), 1)

    def test_optional_fields_null_and_zero(self):
        row_id = history.append('master', 'assistant', 'x', provider_id='', latency_ms=None, tokens=None)
        raw = db.query_one('SELECT provider_id, latency_ms, tokens FROM agent_chat_messages WHERE id = ?',
                           (row_id,))
        self.assertIsNone(raw['provider_id'])
        self.assertEqual(raw['latency_ms'], 0)
        self.assertEqual(raw['tokens'], 0)

    def test_provider_id_coerced_to_int(self):
        row_id = history.append('master', 'assistant', 'x', provider_id='7')
        raw = db.query_one('SELECT provider_id FROM agent_chat_messages WHERE id = ?', (row_id,))
        self.assertEqual(raw['provider_id'], 7)

    def test_list_messages_ascending(self):
        history.append('master', 'user', 'a')
        history.append('master', 'assistant', 'b')
        items = history.list_messages('master', limit=50)['items']
        self.assertEqual([i['content'] for i in items], ['你好', 'a', 'b'])
        self.assertEqual([i['id'] for i in items], sorted(i['id'] for i in items))

    def test_list_messages_limit_returns_latest_page(self):
        for n in range(6):
            history.append('master', 'user', f'm{n}')
        page = history.list_messages('master', limit=3)
        self.assertEqual([i['content'] for i in page['items']], ['m3', 'm4', 'm5'])
        self.assertEqual(page['total'], 7)
        self.assertTrue(page['has_more'])

    def test_list_messages_before_id_pages_backwards(self):
        for n in range(6):
            history.append('master', 'user', f'm{n}')
        first = history.list_messages('master', limit=3)
        second = history.list_messages('master', limit=3, before_id=first['items'][0]['id'])
        self.assertEqual([i['content'] for i in second['items']], ['m0', 'm1', 'm2'])
        self.assertEqual(second['total'], 7)          # total 不随 before_id 变
        self.assertTrue(second['has_more'])
        third = history.list_messages('master', limit=3, before_id=second['items'][0]['id'])
        self.assertEqual([i['content'] for i in third['items']], ['你好'])
        self.assertFalse(third['has_more'])

    def test_list_messages_limit_clamped(self):
        for n in range(3):
            history.append('master', 'user', f'c{n}')
        # 下限是 **1 条**：`limit=0` 过去被 `0 or 50` 吃成默认 50（复审 M4）。
        # 这条断言在只有 1 行的库里恒真，所以先补足 4 行。
        self.assertEqual(len(history.list_messages('master', limit=0)['items']), 1)
        self.assertEqual(len(history.list_messages('master', limit=-5)['items']), 1)
        # 上限是 MAX_LIMIT（把它压小来验上界，不必为了这条插 500 行）。
        with mock.patch.object(history, 'MAX_LIMIT', 2):
            self.assertEqual(len(history.list_messages('master', limit=99999)['items']), 2)
        # 没传 / 传了非数字 → 默认 50
        self.assertEqual(len(history.list_messages('master', limit=None)['items']), 4)
        self.assertEqual(len(history.list_messages('master', limit='abc')['items']), 4)

    def test_list_messages_ignores_an_invalid_before_id(self):
        """`before_id='abc'` 不许静默变成「没有更早的消息了」（复审 L2）。"""
        for n in range(2):
            history.append('master', 'user', f'c{n}')
        page = history.list_messages('master', limit=10, before_id='abc')
        self.assertEqual(len(page['items']), 3)          # 游标被忽略，照常返回最新一页
        self.assertEqual(page['total'], 3)

    def test_non_array_tool_calls_are_stored_as_empty(self):
        """合法 JSON 但不是数组 → 存 '[]'（写进去有、读出来没有 = 写读不对称，复审 L1）。"""
        for bad in ('{}', 'null', '123', '"text"', 123):
            row_id = history.append('master', 'assistant', 'x', tool_calls=bad)
            raw = db.query_one('SELECT tool_calls FROM agent_chat_messages WHERE id = ?', (row_id,))
            self.assertEqual(raw['tool_calls'], '[]', f'tool_calls={bad!r}')
            stored = [i for i in history.list_messages('master')['items'] if i['id'] == row_id][0]
            self.assertEqual(stored['tool_calls'], [])

    def test_json_array_string_round_trips(self):
        row_id = history.append('master', 'assistant', 'x', tool_calls='[{"name": "items_list"}]')
        stored = [i for i in history.list_messages('master')['items'] if i['id'] == row_id][0]
        self.assertEqual(stored['tool_calls'], [{'name': 'items_list'}])

    def test_list_messages_empty_thread(self):
        page = history.list_messages('nobody')
        self.assertEqual(page, {'items': [], 'total': 0, 'has_more': False})

    def test_threads_are_isolated(self):
        history.append('other', 'user', '别的线程')
        page = history.list_messages('master')
        self.assertEqual(page['total'], 1)
        self.assertEqual([i['content'] for i in page['items']], ['你好'])
        self.assertEqual(history.count('other'), 1)

    def test_clear_only_removes_one_thread(self):
        history.append('master', 'assistant', '在的')
        history.append('other', 'user', '留着')
        self.assertEqual(history.clear('master'), 2)
        self.assertEqual(history.count('master'), 0)
        self.assertEqual(history.count('other'), 1)
        self.assertEqual(history.clear('master'), 0)

    def test_sql_is_parameterized(self):
        payload = "'; DROP TABLE agent_chat_messages; --"
        row_id = history.append('master', 'user', payload)
        self.assertEqual(history.list_messages('master')['items'][-1]['content'], payload)
        self.assertIsNotNone(db.query_one('SELECT id FROM agent_chat_messages WHERE id = ?', (row_id,)))

    def test_unknown_thread_clears_nothing(self):
        self.assertEqual(history.clear('nope'), 0)


class AgentHistoryApiTest(unittest.TestCase):
    """路由层冒烟：只挂自己的 router。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()
        roles.ensure_roles()

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

    def setUp(self):
        db.reset_connection()
        history.clear('master')
        history.clear('other')

    def _chat(self, text='看看有几个账号'):
        return self.client.post('/api/agents/master/chat', headers=self.headers, json={'message': text})

    def _stored(self):
        resp = self.client.get('/api/agents/chat/messages', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()['data']

    def test_requires_token(self):
        self.assertEqual(self.client.get('/api/agents/chat/messages').status_code, 401)
        self.assertEqual(self.client.delete('/api/agents/chat/messages').status_code, 401)

    def test_master_chat_persists_both_turns(self):
        payload = {'text': '你有 2 个账号', 'usage': {'prompt_tokens': 30, 'completion_tokens': 12},
                   'model': 'stub-model', 'provider_id': 7, 'tool_calls': []}
        with mock.patch.object(llm, 'chat', _stub_chat(payload)):
            resp = self._chat()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['reply'], '你有 2 个账号')

        data = self._stored()
        self.assertEqual(data['total'], 2)
        self.assertFalse(data['has_more'])
        user_msg, ai_msg = data['items']
        self.assertEqual((user_msg['role'], user_msg['content']), ('user', '看看有几个账号'))
        self.assertEqual(ai_msg['role'], 'assistant')
        self.assertEqual(ai_msg['content'], '你有 2 个账号')
        self.assertEqual(ai_msg['status'], 'ok')
        self.assertEqual(ai_msg['model'], 'stub-model')
        self.assertEqual(ai_msg['provider_id'], 7)
        self.assertEqual(ai_msg['tokens'], 42)
        self.assertGreaterEqual(ai_msg['latency_ms'], 0)

    def test_exception_in_run_master_still_records_error_row(self):
        """`run_master` **抛异常**时也要留痕（复审 M3）。

        以前这里是裸 `await`：读提示词/查温度/读历史都在 `run_master` 自己的 try 之外，
        真出异常时用户只看到 500，AI 会话记录里连「他问过什么、为什么没回」都没有。
        """
        async def boom(*args, **kwargs):
            raise RuntimeError('db is gone')

        with mock.patch.object(runtime, 'run_master', boom):
            resp = self._chat('在吗')
        self.assertEqual(resp.status_code, 502, resp.text)

        data = self._stored()
        self.assertEqual(data['total'], 2)              # 用户那句 + 一行 error
        ai_msg = data['items'][1]
        self.assertEqual(ai_msg['role'], 'assistant')
        self.assertEqual(ai_msg['status'], 'error')
        self.assertIn('db is gone', ai_msg['error'])
        self.assertIn('RuntimeError', ai_msg['error'])

    def test_master_chat_failure_persists_error_row_with_tool_calls(self):
        executed = [{'name': 'items_list', 'args': {}, 'result': {'ok': True}}]
        failed = {'ok': False, 'error': '提供者 #1 的 API Key 无法解密', 'role': 'master', 'tool_calls': executed}
        with mock.patch.object(runtime, 'run_master', _stub_chat(failed)):
            resp = self._chat('把所有商品下架')
        self.assertEqual(resp.status_code, 502, resp.text)

        data = self._stored()
        self.assertEqual(data['total'], 2)
        ai_msg = data['items'][1]
        self.assertEqual(ai_msg['status'], 'error')
        self.assertIn('API Key 无法解密', ai_msg['error'])
        self.assertEqual(ai_msg['tool_calls'], executed)   # 失败也要能看到它其实改了什么

    def test_history_survives_a_new_request(self):
        with mock.patch.object(llm, 'chat', _stub_chat({'text': '好的', 'usage': {}, 'model': 'm',
                                                        'provider_id': None, 'tool_calls': []})):
            self._chat('第一句')
            self._chat('第二句')
        self.assertEqual(self._stored()['total'], 4)

    def test_history_pagination_over_http(self):
        for n in range(5):
            history.append('master', 'user', f'm{n}')
        first = self.client.get('/api/agents/chat/messages', headers=self.headers,
                                params={'limit': 2}).json()['data']
        self.assertEqual([i['content'] for i in first['items']], ['m3', 'm4'])
        self.assertEqual(first['total'], 5)
        self.assertTrue(first['has_more'])
        second = self.client.get('/api/agents/chat/messages', headers=self.headers,
                                 params={'limit': 2, 'before_id': first['items'][0]['id']}).json()['data']
        self.assertEqual([i['content'] for i in second['items']], ['m1', 'm2'])

    def test_delete_clears_history(self):
        history.append('master', 'user', 'a')
        history.append('master', 'user', 'b')
        history.append('other', 'user', 'c')
        resp = self.client.delete('/api/agents/chat/messages', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['deleted'], 2)
        self.assertEqual(history.count('master'), 0)
        self.assertEqual(history.count('other'), 1)

    def test_recording_failure_does_not_break_the_chat(self):
        payload = {'text': '照常回答', 'usage': {}, 'model': 'm', 'provider_id': None, 'tool_calls': []}
        with mock.patch.object(llm, 'chat', _stub_chat(payload)), \
                mock.patch.object(history, 'append', side_effect=RuntimeError('db is gone')):
            resp = self._chat()
        self.assertEqual(resp.status_code, 200, resp.text)      # 落库失败不许 500
        self.assertEqual(resp.json()['data']['reply'], '照常回答')

    def test_messages_keeps_array_and_adds_total(self):
        runtime.record_agent_message('item:A', 'item:B', 'ask', topic='t', payload={'q': 1})
        resp = self.client.get('/api/agents/messages', headers=self.headers,
                               params={'limit': 1, 'offset': 0})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsInstance(body['data'], list)               # 前端还在按数组用
        self.assertEqual(len(body['data']), 1)
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['limit'], 1)
        self.assertEqual(body['offset'], 0)
        self.assertEqual(resp.json()['data'][0]['payload'], {'q': 1})

    def test_runs_keeps_array_and_adds_total(self):
        db.execute('INSERT INTO agent_runs(role, provider_id, model, prompt_tokens, completion_tokens, '
                   'latency_ms, status) VALUES(?,?,?,?,?,?,?)', ('master', 1, 'm', 10, 5, 100, 'ok'))
        resp = self.client.get('/api/agents/runs', headers=self.headers, params={'limit': 5, 'offset': 0})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsInstance(body['data'], list)
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['limit'], 5)

    def test_empty_message_rejected(self):
        self.assertEqual(self.client.post('/api/agents/master/chat', headers=self.headers,
                                          json={'message': '   '}).status_code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
