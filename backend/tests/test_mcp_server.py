"""MCP stdio server 测试（DSH 插件，task-6）。

覆盖：
1. `tools/list` 暴露 CONTRACTS §5 全部规范名，且**不**暴露动词式别名；
2. `tools/call` 正确拼 URL / 带 Bearer token / 组 query 与 body；
3. 后端不可用 → 结构化错误（不抛栈）；
4. 真实子进程冒烟：`Popen([sys.executable, 'mcp_server/server.py'])` 喂
   `initialize` + `tools/list` + `tools/call` 的 JSON-RPC 行，断言有响应。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_mcp_server.py"
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
import io
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_server import client as mcp_client  # noqa: E402
from mcp_server import server as mcp_server  # noqa: E402


def _smoke_env(**overrides) -> dict:
    """子进程冒烟用的环境：故意指向死端口，避免测试依赖真后端。"""
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO_ROOT) + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    env['XYWB_BASE_URL'] = 'http://127.0.0.1:59999'
    env['XYWB_TOKEN'] = 'SMOKE_TOKEN'
    env.update(overrides)
    return env

# CONTRACTS.md §5 的规范工具名（2026-10-06 lead 裁决）
CANONICAL_TOOLS = {
    'accounts_list', 'accounts_set_enabled', 'accounts_relogin',
    'items_list', 'items_get', 'items_edit', 'items_publish', 'items_batch_edit', 'items_sync',
    'chats_list', 'chats_read', 'chats_send', 'chats_set_mode',
    'orders_list', 'orders_deliver',
    'risk_status', 'risk_clear',
    'providers_list', 'providers_upsert',
    'master_chat',
}


# --------------------------------------------------------------------------
# 假的 HTTP 层：拦住 urlopen，记录请求
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UrlRecorder:
    """替换 `urllib.request.urlopen`，按 (method, path) 返回预设响应。"""

    def __init__(self, routes=None, exc=None):
        self.routes = dict(routes or {})
        self.exc = exc
        self.calls = []

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        split = urlsplit(url)
        path = split.path
        query = dict(parse_qsl(split.query))
        body = None
        raw = req.data
        if raw:
            body = json.loads(raw.decode('utf-8'))
        self.calls.append({
            'method': method, 'url': url, 'path': path, 'query': query, 'body': body,
            'headers': {k.lower(): v for k, v in req.header_items()},
        })
        if self.exc is not None:
            raise self.exc
        for key in ((method, path), path):
            if key in self.routes:
                value = self.routes[key]
                if isinstance(value, Exception):
                    raise value
                return FakeResponse(value)
        return FakeResponse({'success': False, 'code': 'http_404', 'message': 'not found'}, status=404)


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.recorder = UrlRecorder()
        self._orig_urlopen = urllib_request.urlopen
        urllib_request.urlopen = self.recorder
        self.addCleanup(setattr, urllib_request, 'urlopen', self._orig_urlopen)
        self.client = mcp_client.BackendClient(base_url='http://127.0.0.1:9999', token='TESTTOKEN')


# --------------------------------------------------------------------------
# 1. tools/list
# --------------------------------------------------------------------------

class ToolsListTest(ClientTestCase):
    def test_contract_tools_all_present(self):
        names = {spec['name'] for spec in mcp_client.tool_specs()}
        missing = CANONICAL_TOOLS - names
        self.assertFalse(missing, f'tools/list 缺规范工具: {sorted(missing)}')

    def test_no_alias_leaked_into_tools_list(self):
        """别名只允许在 tools/call 里接受，不能出现在 tools/list（否则模型会选错）。"""
        names = {spec['name'] for spec in mcp_client.tool_specs()}
        leaked = set(mcp_client.ALIASES) & names
        self.assertFalse(leaked, f'tools/list 暴露了别名: {sorted(leaked)}')

    def test_every_listed_tool_has_route_and_schema(self):
        for spec in mcp_client.tool_specs():
            self.assertIn(spec['name'], mcp_client.TOOL_ROUTES, spec['name'])
            self.assertIsInstance(spec['description'], str)
            self.assertTrue(spec['description'])
            schema = spec['inputSchema']
            self.assertEqual(schema.get('type'), 'object', spec['name'])
            self.assertIn('properties', schema, spec['name'])

    def test_no_listed_tool_requires_missing_field(self):
        """必填字段必须真的在 properties 里，且是路由认识的参数。"""
        for spec in mcp_client.tool_specs():
            route = mcp_client.TOOL_ROUTES[spec['name']]
            known = set(route.get('path_params', [])) | set(route.get('query', [])) | set(route.get('body', []))
            schema = spec['inputSchema']
            for field in schema.get('required', []):
                self.assertIn(field, schema.get('properties', {}), f'{spec["name"]}.{field}')
                self.assertIn(field, known, f'{spec["name"]}.{field} 路由不认识')

    def test_tools_list_rpc_shape(self):
        resp = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/list'}, self.client)
        self.assertEqual(resp['id'], 7)
        self.assertIn('tools', resp['result'])
        self.assertTrue(all('inputSchema' in t for t in resp['result']['tools']))


# --------------------------------------------------------------------------
# 2. tools/call：URL / token / 参数
# --------------------------------------------------------------------------

class ToolCallTest(ClientTestCase):
    def test_get_with_query_and_token(self):
        self.recorder.routes['/api/items'] = {'success': True, 'data': []}
        result = self.client.call_tool('items_list', {'account_id': 'acc1', 'limit': 10, 'q': '鞋'})
        self.assertTrue(result['success'], result)
        call = self.recorder.calls[-1]
        self.assertEqual(call['method'], 'GET')
        self.assertEqual(call['path'], '/api/items')
        self.assertEqual(call['query'], {'account_id': 'acc1', 'limit': '10', 'q': '鞋'})
        self.assertEqual(call['headers']['authorization'], 'Bearer TESTTOKEN')

    def test_path_params_and_body(self):
        self.recorder.routes['/api/chats/88/send'] = {'success': True, 'data': {'ok': True}}
        # `confirm` 是第 53 批补进这条路由的写闸门参数（给买家发消息会真的发出去）。
        result = self.client.call_tool('chats_send', {'conversation_id': 88, 'content': '在的',
                                                      'confirm': True})
        self.assertTrue(result['success'], result)
        call = self.recorder.calls[-1]
        self.assertEqual((call['method'], call['path']), ('POST', '/api/chats/88/send'))
        self.assertEqual(call['body'], {'content': '在的', 'confirm': True})
        self.assertEqual(call['headers']['content-type'], 'application/json')

    def test_unknown_fields_are_not_forwarded(self):
        """模型多给的字段不能透传到后端（避免后端 422）。"""
        self.recorder.routes['/api/items/acc1/i1/edit'] = {'success': True, 'data': {}}
        # `confirm` 是第 52 批补进这条路由的写闸门参数：这里必须显式点头，
        # 否则会被客户端闸门挡下、根本发不出请求（被测的是「未知字段不透传」）。
        self.client.call_tool('items_edit', {'account_id': 'acc1', 'item_id': 'i1',
                                             'price': 9.9, 'bogus': 'x', 'confirm': True})
        self.assertEqual(self.recorder.calls[-1]['body'], {'price': 9.9, 'confirm': True})

    def test_finance_cycle_goes_out_as_from_to(self):
        """账期的线上名是 from/to：工具参数 cycle_from/cycle_to 必须被翻译。

        回归钉（2026-10-11）：曾经发 `?cycle_from=…`，FastAPI 静默忽略未知 query
        ⇒ 账期悄悄回落默认值，**结果错但不报错**。
        """
        self.recorder.routes['/api/finance/acc1/income'] = {'success': True, 'data': {}}
        result = self.client.call_tool('finance_income', {
            'account_id': 'acc1', 'cycle_from': '202609', 'cycle_to': '202609'})
        self.assertTrue(result['success'], result)
        call = self.recorder.calls[-1]
        self.assertEqual(call['path'], '/api/finance/acc1/income')
        self.assertEqual(call['query'], {'from': '202609', 'to': '202609'})
        self.assertNotIn('cycle_from', call['query'])

    def test_verb_alias_accepted(self):
        """任务卡里的动词式名字仍可调用（但不进 tools/list）。"""
        self.recorder.routes['/api/chats/conversations'] = {'success': True, 'data': []}
        result = self.client.call_tool('list_conversations', {'limit': 5})
        self.assertTrue(result['success'], result)
        self.assertEqual(self.recorder.calls[-1]['path'], '/api/chats/conversations')

    def test_master_chat(self):
        self.recorder.routes['/api/agents/master/chat'] = {'success': True, 'data': {'reply': '好'}}
        result = self.client.call_tool('master_chat', {'message': '把 A 商品降到 99'})
        self.assertTrue(result['success'], result)
        self.assertEqual(self.recorder.calls[-1]['body'], {'message': '把 A 商品降到 99'})

    def test_agents_messages(self):
        self.recorder.routes['/api/agents/messages'] = {'success': True, 'data': []}
        self.client.call_tool('list_agent_messages', {'conversation_id': 5})
        call = self.recorder.calls[-1]
        self.assertEqual(call['path'], '/api/agents/messages')
        self.assertEqual(call['query'], {'conversation_id': '5'})

    def test_providers_upsert_with_id_uses_put(self):
        self.recorder.routes[('PUT', '/api/providers/3')] = {'success': True, 'data': {}}
        self.client.call_tool('providers_upsert', {'id': 3, 'default_model': 'gpt-4o'})
        call = self.recorder.calls[-1]
        self.assertEqual((call['method'], call['path']), ('PUT', '/api/providers/3'))
        self.assertEqual(call['body'], {'default_model': 'gpt-4o'})

    def test_missing_required_path_param(self):
        result = self.client.call_tool('items_get', {'account_id': 'acc1'})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_args')
        self.assertEqual(self.recorder.calls, [])

    def test_unknown_tool(self):
        result = self.client.call_tool('nope_not_a_tool', {})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'unknown_tool')

    def test_items_get_direct_endpoint(self):
        self.recorder.routes['/api/items/acc1/i1'] = {'success': True, 'data': {'item_id': 'i1'}}
        result = self.client.call_tool('items_get', {'account_id': 'acc1', 'item_id': 'i1'})
        self.assertTrue(result['success'], result)
        self.assertEqual(self.recorder.calls[-1]['path'], '/api/items/acc1/i1')

    def test_items_get_falls_back_when_endpoint_missing(self):
        """services 的 GET /api/items/{acc}/{item} 还没落地时，退化为列表+本地精确过滤。"""
        def route(req, timeout=None):
            path = urlsplit(req.full_url).path
            self.recorder.calls.append({'method': req.get_method(), 'path': path})
            if path == '/api/items/acc1/i1':
                return FakeResponse({'success': False, 'code': 'http_404', 'message': 'no'}, status=404)
            return FakeResponse({'success': True, 'data': [
                {'item_id': 'other', 'title': 'x'}, {'item_id': 'i1', 'title': '找到了'}]})
        urllib_request.urlopen = route
        result = self.client.call_tool('items_get', {'account_id': 'acc1', 'item_id': 'i1'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['item']['title'], '找到了')
        self.assertEqual(result['data']['_fallback'], 'list_filter')


# --------------------------------------------------------------------------
# 3. 后端不可用 → 结构化错误
# --------------------------------------------------------------------------

class BackendDownTest(ClientTestCase):
    def test_connection_refused_is_structured(self):
        urllib_request.urlopen = UrlRecorder(exc=urllib_error.URLError(ConnectionRefusedError(10061, '拒绝')))
        result = self.client.call_tool('accounts_list', {})
        self.assertIsInstance(result, dict)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'backend_unreachable')
        self.assertIn('启动后端', result['message'])

    def test_timeout_is_structured(self):
        urllib_request.urlopen = UrlRecorder(
            exc=urllib_error.URLError(TimeoutError('timed out')))
        result = self.client.health()
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'backend_timeout')

    def test_tools_call_wraps_error_as_isError(self):
        urllib_request.urlopen = UrlRecorder(exc=urllib_error.URLError(ConnectionRefusedError(10061, '拒绝')))
        resp = mcp_server.dispatch(
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': 'accounts_list', 'arguments': {}}},
            self.client)
        self.assertNotIn('error', resp)
        self.assertTrue(resp['result']['isError'])
        payload = json.loads(resp['result']['content'][0]['text'])
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'backend_unreachable')

    def test_http_500_body_is_parsed(self):
        urllib_request.urlopen = UrlRecorder(exc=_http_error(
            'http://x/api/accounts', 500, 'boom', {'success': False, 'message': '内部错误'}))
        result = self.client.call_tool('accounts_list', {})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'http_500')
        self.assertEqual(result['message'], '内部错误')

    def test_401_hint(self):
        urllib_request.urlopen = UrlRecorder(exc=_http_error(
            'http://x/api/accounts', 401, 'unauthorized', {'success': False, 'message': 'unauthorized'}))
        result = self.client.call_tool('accounts_list', {})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'unauthorized')
        self.assertIn('mcp_token.txt', result['message'])

    def test_non_json_response(self):
        self.recorder.routes['/api/accounts'] = b'<html>502 Bad Gateway</html>'
        result = self.client.call_tool('accounts_list', {})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'bad_response')


class _BytesReader:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def close(self):
        pass


def _http_error(url: str, code: int, msg: str, body) -> urllib_error.HTTPError:
    """构造一个带响应体的 HTTPError（urllib 的 fp 参数要能 read/close）。"""
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
    return urllib_error.HTTPError(url, code, msg, {}, _BytesReader(raw))


# --------------------------------------------------------------------------
# token 来源
# --------------------------------------------------------------------------

class TokenTest(unittest.TestCase):
    def test_env_token_wins(self):
        os.environ['XYWB_TOKEN'] = 'FROM_ENV'
        try:
            client = mcp_client.BackendClient()
            self.assertEqual(client._token, 'FROM_ENV')
            self.assertEqual(client.token_source, 'env:XYWB_TOKEN')
        finally:
            os.environ.pop('XYWB_TOKEN', None)

    def test_token_file_fallback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mcp_token.txt'
            path.write_text('FROM_FILE\n', encoding='utf-8')
            orig = mcp_client.default_token_path
            mcp_client.default_token_path = lambda: path
            try:
                client = mcp_client.BackendClient()
                self.assertEqual(client._token, 'FROM_FILE')
                self.assertTrue(client.token_source.startswith('file:'))
            finally:
                mcp_client.default_token_path = orig

    def test_base_url_from_env(self):
        os.environ['XYWB_BASE_URL'] = 'http://127.0.0.1:12345/'
        try:
            self.assertEqual(mcp_client.BackendClient().base_url, 'http://127.0.0.1:12345')
        finally:
            os.environ.pop('XYWB_BASE_URL', None)


# --------------------------------------------------------------------------
# 4. 真实子进程冒烟
# --------------------------------------------------------------------------

class SubprocessSmokeTest(unittest.TestCase):
    """真的把 server.py 起成子进程，走 stdin/stdout 说 MCP 协议。"""

    @classmethod
    def setUpClass(cls):
        env = _smoke_env()
        cls.proc = subprocess.Popen(
            [sys.executable, '-u', str(REPO_ROOT / 'mcp_server' / 'server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT), env=env, text=True, encoding='utf-8', bufsize=1,
        )
        cls._stderr_lines = []
        cls._reader = threading.Thread(target=cls._drain_stderr, daemon=True)
        cls._reader.start()

    @classmethod
    def _drain_stderr(cls):
        for line in cls.proc.stderr:
            cls._stderr_lines.append(line.rstrip())

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.proc.stdin:
                cls.proc.stdin.close()
            cls.proc.wait(timeout=10)
        except Exception:
            cls.proc.kill()
            cls.proc.wait(timeout=10)

    def send(self, message):
        self.__class__.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.__class__.proc.stdin.flush()

    def read_response(self, timeout=30.0):
        result = {}

        def _read():
            line = self.proc.stdout.readline()
            if line:
                result['line'] = line
        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), f'等响应超时；stderr=\n' + '\n'.join(self._stderr_lines))
        self.assertTrue(result.get('line'), '子进程没回任何东西')
        return json.loads(result['line'])

    def test_full_handshake(self):
        self.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                   'params': {'protocolVersion': '2024-11-05',
                              'capabilities': {},
                              'clientInfo': {'name': 'unittest', 'version': '1'}}})
        init = self.read_response()
        print('\n[smoke] initialize 响应: ' + json.dumps(init, ensure_ascii=False))
        self.assertEqual(init['id'], 1)
        self.assertEqual(init['result']['serverInfo']['name'], 'xianyu-ai-workbench')
        self.assertEqual(init['result']['protocolVersion'], '2024-11-05')
        self.assertIn('tools', init['result']['capabilities'])

        # 通知不回包 —— 立刻发 tools/list，若 server 违规回包，读到的 id 就不是 2
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        listed = self.read_response()
        print('[smoke] tools/list 响应(截断): ' + json.dumps(listed, ensure_ascii=False)[:600])
        self.assertEqual(listed['id'], 2, 'notifications 不该回包')
        names = {t['name'] for t in listed['result']['tools']}
        missing = CANONICAL_TOOLS - names
        self.assertFalse(missing, f'子进程 tools/list 缺: {sorted(missing)}')
        self.assertNotIn('list_items', names, 'tools/list 不该出现别名')

        # 后端不可用 → 结构化错误，进程不崩
        self.send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                   'params': {'name': 'accounts_list', 'arguments': {}}})
        called = self.read_response()
        print('[smoke] tools/call(后端未起) 响应: ' + json.dumps(called, ensure_ascii=False)[:400])
        self.assertEqual(called['id'], 3)
        self.assertTrue(called['result']['isError'])
        payload = json.loads(called['result']['content'][0]['text'])
        self.assertFalse(payload['success'])
        self.assertEqual(payload['code'], 'backend_unreachable')

        # 未知方法 → JSON-RPC error，进程还活着
        self.send({'jsonrpc': '2.0', 'id': 4, 'method': 'no/such/method'})
        unknown = self.read_response()
        self.assertEqual(unknown['error']['code'], mcp_server.METHOD_NOT_FOUND)

        # 坏 JSON → parse error，进程还活着
        self.proc.stdin.write('{not json\n')
        self.proc.stdin.flush()
        parsed = self.read_response()
        self.assertEqual(parsed['error']['code'], mcp_server.PARSE_ERROR)

        # ping 之后仍然正常
        self.send({'jsonrpc': '2.0', 'id': 5, 'method': 'ping'})
        pong = self.read_response()
        self.assertEqual(pong['id'], 5)

        self.assertIsNone(self.proc.poll(), '子进程不该退出')
        print('[smoke] stderr: ' + ' | '.join(self._stderr_lines[:4]))


# --------------------------------------------------------------------------
# 协议层单测（不走网络）
# --------------------------------------------------------------------------

class ProtocolTest(ClientTestCase):
    def test_initialize_negotiates_protocol(self):
        resp = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                    'params': {'protocolVersion': '2025-06-18'}}, self.client)
        self.assertEqual(resp['result']['protocolVersion'], '2025-06-18')

        resp = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize',
                                    'params': {'protocolVersion': '1999-01-01'}}, self.client)
        self.assertEqual(resp['result']['protocolVersion'], mcp_server.PROTOCOL_VERSION)

    def test_notification_returns_none(self):
        self.assertIsNone(mcp_server.dispatch(
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, self.client))

    def test_empty_handlers(self):
        for method in ('resources/list', 'prompts/list'):
            resp = mcp_server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method}, self.client)
            self.assertIn('result', resp)

    def test_bad_json_line_does_not_kill_loop(self):
        import io
        stdin = io.StringIO('{bad json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
        stdout = io.StringIO()
        rc = mcp_server.serve(stdin=stdin, stdout=stdout, client=self.client)
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['error']['code'], mcp_server.PARSE_ERROR)
        self.assertEqual(lines[1]['result'], {})


class ToolSpecsSourceTest(unittest.TestCase):
    def test_source_reported(self):
        source = mcp_client.tool_specs_source()
        self.assertTrue(source)
        # agents 工作流落地后这里会变成 TOOL_SPECS；两种都合法
        self.assertTrue(source.startswith('builtin') or 'TOOL_SPECS' in source, source)

    def test_no_toolspec_silently_dropped(self):
        """TOOL_SPECS 里出现的工具必须都能在 tools/list 里被调用到。

        若 agents 加了一个我这边没有路由的工具，它会被静默丢掉 —— 这个断言就是
        那条对齐线的守卫：失败时去 mcp_server/client.py::TOOL_ROUTES 补路由。
        """
        external, source = mcp_client._load_external_specs()
        if not external:
            self.skipTest(f'TOOL_SPECS 不可用，跳过对齐检查：{source}')
        listed = {s['name'] for s in mcp_client.tool_specs()}
        dropped = set(external) - listed
        self.assertFalse(dropped, f'TOOL_SPECS 有工具没被 MCP 暴露: {sorted(dropped)}（去 TOOL_ROUTES 补路由）')

    def test_no_routeless_or_unknown_listed(self):
        external, source = mcp_client._load_external_specs()
        if not external:
            self.skipTest('TOOL_SPECS 不可用')
        unknown = {s['name'] for s in mcp_client.tool_specs()} - set(external) - set(mcp_client.MCP_ONLY_TOOLS)
        self.assertFalse(unknown, f'MCP 暴露了 TOOL_SPECS 里没有的工具: {sorted(unknown)}')

    def test_normalize_openai_function_shape(self):
        """agents 工作流确认的形态：list[{"type":"function","function":{name,description,parameters}}]"""
        raw = [{
            'type': 'function',
            'function': {
                'name': 'items_list',
                'description': '来自 TOOL_SPECS 的描述',
                'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}},
            },
        }]
        normalized = mcp_client._normalize_specs(raw)
        self.assertEqual(normalized['items_list']['description'], '来自 TOOL_SPECS 的描述')
        self.assertEqual(normalized['items_list']['inputSchema']['properties']['q']['type'], 'string')

    def test_normalize_mapping_shape(self):
        normalized = mcp_client._normalize_specs(
            {'items_list': {'description': 'd', 'input_schema': {'type': 'object', 'properties': {}}}})
        self.assertIn('items_list', normalized)

    def test_external_specs_override_description(self):
        orig = mcp_client._load_external_specs
        mcp_client._load_external_specs = lambda: (
            {'items_list': {'description': '来自 TOOL_SPECS', 'inputSchema': None}}, 'fake')
        try:
            specs = {s['name']: s for s in mcp_client.tool_specs()}
            self.assertEqual(specs['items_list']['description'], '来自 TOOL_SPECS')
            # 兜底 schema 仍在（external 的 inputSchema 是 None）
            self.assertIn('properties', specs['items_list']['inputSchema'])
        finally:
            mcp_client._load_external_specs = orig

    def test_toolspec_fallback_is_not_silent(self):
        """守卫不能因为「TOOL_SPECS 不可用」就整体跳过。

        独立复核发现的缝：新增工具时若同时把 `agents/tools.py` 写坏（语法错/缺依赖），
        对齐测试会双双 skipTest，套件仍然全绿，而 tools/list 已悄悄退回内置清单。
        这里把「文件在、却加载不出来」判为失败。
        """
        tools_py = pathlib.Path(mcp_client.__file__).resolve().parents[1] / 'backend' / 'app' / 'agents' / 'tools.py'
        if not tools_py.exists():
            self.skipTest(f'{tools_py} 尚未落地（agents 工作流还没写完）')
        external, source = mcp_client._load_external_specs()
        self.assertTrue(external, f'backend/app/agents/tools.py 存在，但 TOOL_SPECS 加载失败，'
                                  f'tools/list 已静默退回内置清单：{source}')


class RouteRegistrationTest(unittest.TestCase):
    """MCP 的每条路由都必须真的在后端注册过（否则模型看到的是必然报错的工具）。

    用 AST 读 `backend/app/api/*.py` 的 `@router.<verb>` 装饰器 + 文件里的
    `prefix=`，不启动后端、不碰数据库。
    """

    @staticmethod
    def _registered() -> set:
        api_dir = REPO_ROOT / 'backend' / 'app' / 'api'
        if not api_dir.is_dir():
            return set()
        out = set()
        for path in sorted(api_dir.glob('*.py')):
            src = path.read_text(encoding='utf-8')
            m = re.search(r"""prefix\s*=\s*['"]([^'"]*)""", src)
            prefix = m.group(1) if m else ''
            for verb, route in re.findall(
                    r"""@router\.(get|post|put|patch|delete)\(\s*['"]([^'"]*)""", src):
                full = (prefix + route).replace('//', '/').rstrip('/') or '/'
                out.add((verb.upper(), re.sub(r'\{[^}]+\}', '{}', full)))
        return out

    def test_every_mcp_route_is_registered(self):
        registered = self._registered()
        if not registered:
            self.skipTest('backend/app/api 尚未落地')
        missing = []
        for name, route in sorted(mcp_client.TOOL_ROUTES.items()):
            key = (route['method'].upper(), re.sub(r'\{[^}]+\}', '{}', route['path']))
            if key not in registered:
                missing.append(f'{name} -> {route["method"]} {route["path"]}')
        self.assertFalse(missing, '这些 MCP 路由后端没注册：\n  ' + '\n  '.join(missing))

    def test_lead_confirmed_routes_match_contract(self):
        """2026-10-06 lead 点名的 5 条，路径必须与 CONTRACTS §3 逐字一致。"""
        expected = {
            'items_batch_edit': ('POST', '/api/items/{account_id}/batch-edit'),
            'accounts_relogin': ('POST', '/api/accounts/{account_id}/relogin'),
            'accounts_set_enabled': ('PATCH', '/api/accounts/{account_id}'),
            'items_get': ('GET', '/api/items/{account_id}/{item_id}'),
            'chats_list': ('GET', '/api/chats/conversations'),
        }
        for name, (method, path) in expected.items():
            route = mcp_client.TOOL_ROUTES[name]
            self.assertEqual((route['method'], route['path']), (method, path), name)


class RouteParamAlignmentTest(unittest.TestCase):
    """每条 MCP 路由声明的 query / body 键，必须被**真实端点**接受。

    为什么需要这条：FastAPI 对未知 query 参数是**静默忽略**的。路由声明里写错一个
    键名（例如 finance 写 `cycle_from`，而端点的线上名是 `from`）既不会报错、也不会
    4xx，只会让筛选条件失效、悄悄返回默认范围的数据 —— 单看任何一侧都发现不了。

    2026-10-11 这条检查抓到三处真漂移：`finance_income` / `finance_expense` 的
    `cycle_from`/`cycle_to`（端点认 `from`/`to`，现由 `query_rename` 翻译）、
    `accounts_list` 声明了 `enabled/q/limit`（端点根本不收任何 query）。
    """

    @classmethod
    def setUpClass(cls):
        from backend.app.main import create_app
        cls.spec = create_app().openapi()

    def _op(self, path: str, method: str):
        return self.spec['paths'][path][method]

    def _known(self, path: str, method: str) -> bool:
        return path in self.spec['paths'] and method in self.spec['paths'][path]

    def _query_names(self, path: str, method: str) -> set:
        return {p['name'] for p in self._op(path, method).get('parameters', [])
                if p.get('in') == 'query'}

    def _body_names(self, path: str, method: str):
        """端点接受的 body 键；body 是自由 dict（`Dict[str, Any]`）时返回 None = 什么都收。"""
        schema = ((self._op(path, method).get('requestBody') or {})
                  .get('content', {}).get('application/json', {}) or {}).get('schema') or {}
        ref = schema.get('$ref')
        if ref:
            schema = self.spec['components']['schemas'][ref.split('/')[-1]]
        props = schema.get('properties')
        return set(props) if props else None

    def test_declared_query_keys_are_accepted(self):
        problems = []
        for name, route in sorted(mcp_client.TOOL_ROUTES.items()):
            declared = route.get('query') or []
            path, method = route['path'], route['method'].lower()
            if not declared or not self._known(path, method):
                continue  # 路径本身由 RouteRegistrationTest 管
            rename = route.get('query_rename') or {}
            wire = {rename.get(k, k) for k in declared}
            accepted = self._query_names(path, method)
            bad = sorted(wire - accepted)
            if bad:
                problems.append(f'{name}: {bad} 端点不接受（它接受 {sorted(accepted)}）')
        self.assertFalse(
            problems,
            '这些 MCP 路由的 query 键端点不接受，会被 FastAPI 静默忽略：\n  '
            + '\n  '.join(problems)
            + '\n（要么改键名，要么在路由里加 query_rename 做翻译）')

    def test_declared_body_keys_are_accepted(self):
        problems = []
        for name, route in sorted(mcp_client.TOOL_ROUTES.items()):
            declared = route.get('body') or []
            path, method = route['path'], route['method'].lower()
            if not declared or not self._known(path, method):
                continue
            accepted = self._body_names(path, method)
            if accepted is None:
                continue  # 自由 dict，端点什么都收
            bad = sorted(set(declared) - accepted)
            if bad:
                problems.append(f'{name}: {bad} 端点不接受（它接受 {sorted(accepted)}）')
        self.assertFalse(problems, '这些 MCP 路由的 body 键端点不接受：\n  ' + '\n  '.join(problems))

    def test_query_rename_maps_declared_keys_only(self):
        """`query_rename` 的键必须同时出现在 `query` 里，否则是写错了没人会用。"""
        problems = []
        for name, route in sorted(mcp_client.TOOL_ROUTES.items()):
            rename = route.get('query_rename') or {}
            if not rename:
                continue
            orphan = sorted(set(rename) - set(route.get('query') or []))
            if orphan:
                problems.append(f'{name}: query_rename 里的 {orphan} 不在 query 里')
        self.assertFalse(problems, 'query_rename 写错了：\n  ' + '\n  '.join(problems))

    def test_finance_cycle_is_renamed_on_the_wire(self):
        """财务账期的回归钉：工具参数叫 cycle_from/cycle_to，线上必须发 from/to。"""
        for name in ('finance_income', 'finance_expense'):
            route = mcp_client.TOOL_ROUTES[name]
            self.assertEqual(route.get('query_rename'),
                             {'cycle_from': 'from', 'cycle_to': 'to'}, name)


class RobustnessTest(unittest.TestCase):
    """独立复核报出的缺陷的回归测试。"""

    def test_corrupt_token_file_does_not_kill_client(self):
        """token 文件非 UTF-8 时必须降级为「无 token」，不能抛 UnicodeDecodeError。"""
        tmp = pathlib.Path(tempfile.mkdtemp()) / 'mcp_token.txt'
        tmp.write_bytes(b'\xff\xfe\x00bad-bytes')
        orig = mcp_client.default_token_path
        mcp_client.default_token_path = lambda: tmp
        os.environ.pop('XYWB_TOKEN', None)
        try:
            token, source = mcp_client._read_token()
            self.assertIsNone(token, f'坏文件不该产出 token，得到 {token!r}')
            self.assertIn('已忽略', source)
            client = mcp_client.BackendClient(base_url='http://127.0.0.1:9')
            self.assertIsNone(client._token)          # 构造不抛
            self.assertEqual(client.token_source, source)
        finally:
            mcp_client.default_token_path = orig
            shutil.rmtree(tmp.parent, ignore_errors=True)

    def test_invalid_utf8_on_stdin_does_not_kill_server(self):
        """stdin 上的坏字节只回一条 parse error，后续请求照常处理。"""
        proc = subprocess.Popen(
            [sys.executable, '-u', os.path.join(REPO_ROOT, 'mcp_server', 'server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_smoke_env(),
        )
        try:
            out, _ = proc.communicate(
                b'\xff\xfe bad bytes\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n', timeout=60)
        finally:
            if proc.poll() is None:      # pragma: no cover
                proc.kill()
        self.assertEqual(proc.returncode, 0, '坏字节不该让 server 打栈退出')
        lines = [json.loads(l) for l in out.decode('utf-8', 'replace').splitlines() if l.strip()]
        self.assertEqual(len(lines), 2, f'期望 parse error + ping 响应，实际 {lines}')
        self.assertEqual(lines[0]['error']['code'], mcp_server.PARSE_ERROR)
        self.assertEqual(lines[1]['id'], 7)
        self.assertEqual(lines[1]['result'], {})

    def test_providers_upsert_with_id_filters_unknown_fields(self):
        """带 id 的更新也必须过白名单，不能把模型多给的字段原样 PUT。"""
        recorded = {}

        class Recorder(mcp_client.BackendClient):
            def __init__(self):
                super().__init__(base_url='http://127.0.0.1:9', token='T')

            def request(self, method, path, query=None, body=None):
                recorded['call'] = (method, path, body)
                return mcp_client.ok({})

        Recorder().call_tool('providers_upsert', {
            'id': 3, 'default_model': 'm', 'BOGUS': 'x', 'secret_typo': 1,
        })
        self.assertEqual(recorded['call'], ('PUT', '/api/providers/3', {'default_model': 'm'}))

    def test_providers_upsert_id_is_escaped(self):
        """id 必须转义，否则 '1/../accounts' 能穿越到别的端点。"""
        recorded = {}

        class Recorder(mcp_client.BackendClient):
            def __init__(self):
                super().__init__(base_url='http://127.0.0.1:9', token='T')

            def request(self, method, path, query=None, body=None):
                recorded['path'] = path
                return mcp_client.ok({})

        Recorder().call_tool('providers_upsert', {'id': '1/../accounts', 'name': 'n'})
        self.assertEqual(recorded['path'], '/api/providers/1%2F..%2Faccounts')

    def test_providers_upsert_with_id_needs_a_field(self):
        class Recorder(mcp_client.BackendClient):
            def __init__(self):
                super().__init__(base_url='http://127.0.0.1:9', token='T')

            def request(self, method, path, query=None, body=None):  # pragma: no cover
                raise AssertionError('不该发请求')

        result = Recorder().call_tool('providers_upsert', {'id': 3})
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_args')

    def test_items_get_fallback_raises_page_size(self):
        """404 兜底要撑大页大小，否则目标商品排在第 50 条之后会误报 not_found。"""
        seen = []

        class Recorder(mcp_client.BackendClient):
            def __init__(self):
                super().__init__(base_url='http://127.0.0.1:9', token='T')

            def request(self, method, path, query=None, body=None):
                seen.append((path, query))
                if path.startswith('/api/items/') and path.count('/') > 3:
                    return mcp_client.error('http_404', 'HTTP 404')
                return mcp_client.ok({'items': [{'item_id': 'x1'}]})

        result = Recorder().call_tool('items_get', {'account_id': 'a1', 'item_id': 'x1'})
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['_fallback'], 'list_filter')
        self.assertEqual(seen[-1][1], {'account_id': 'a1', 'q': 'x1', 'limit': 500})

    def test_external_narrow_schema_does_not_hide_route_params(self):
        """external schema 比路由窄时，不能整块覆盖 —— 否则参数从契约里消失。"""
        orig = mcp_client._load_external_specs
        mcp_client._load_external_specs = lambda: (
            {'accounts_list': {'description': 'd',
                               'inputSchema': {'type': 'object', 'properties': {},
                                               'additionalProperties': False}}}, 'fake')
        try:
            specs = {s['name']: s for s in mcp_client.tool_specs()}
            props = specs['accounts_list']['inputSchema']['properties']
            for key in mcp_client.TOOL_ROUTES['accounts_list']['query']:
                self.assertIn(key, props, f'路由参数 {key} 被 external schema 抹掉了')
            self.assertEqual(specs['accounts_list']['description'], 'd')   # 描述仍以 external 为准
        finally:
            mcp_client._load_external_specs = orig


class BaseUrlResolutionTest(unittest.TestCase):
    """桌面形态端口是随机挑的，插件必须能从 `data/backend_port.txt` 发现后端。

    优先级：显式参数 > `XYWB_BASE_URL` > 端口文件 > 默认 8080。
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / 'data').mkdir(parents=True, exist_ok=True)
        self._env = {k: os.environ.get(k) for k in ('XIANYU_APP_DIR', 'XYWB_BASE_URL')}
        os.environ['XIANYU_APP_DIR'] = str(self.tmp)
        os.environ.pop('XYWB_BASE_URL', None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_port(self, content):
        (self.tmp / 'data' / 'backend_port.txt').write_text(content, encoding='utf-8')

    def test_reads_port_from_file(self):
        self._write_port('18090')
        url, source = mcp_client.resolve_base_url()
        self.assertEqual(url, 'http://127.0.0.1:18090')
        self.assertIn('backend_port.txt', source)

    def test_port_file_is_stripped(self):
        self._write_port('  18090 \n')
        self.assertEqual(mcp_client.resolve_base_url()[0], 'http://127.0.0.1:18090')

    def test_env_wins_over_port_file(self):
        self._write_port('18090')
        os.environ['XYWB_BASE_URL'] = 'http://127.0.0.1:7777/'
        url, source = mcp_client.resolve_base_url()
        self.assertEqual(url, 'http://127.0.0.1:7777')      # 去掉尾斜杠
        self.assertEqual(source, 'env:XYWB_BASE_URL')

    def test_explicit_arg_wins_over_everything(self):
        self._write_port('18090')
        os.environ['XYWB_BASE_URL'] = 'http://127.0.0.1:7777'
        self.assertEqual(mcp_client.resolve_base_url('http://127.0.0.1:1/')[0],
                         'http://127.0.0.1:1')

    def test_missing_file_falls_back_without_raising(self):
        url, source = mcp_client.resolve_base_url()
        self.assertEqual(url, mcp_client.DEFAULT_BASE_URL)
        self.assertIn('不存在', source)

    def test_invalid_contents_fall_back_without_raising(self):
        for bad in ('', '   ', 'not-a-port', '99999', '0', '-1'):
            with self.subTest(content=bad):
                self._write_port(bad)
                url, _ = mcp_client.resolve_base_url()
                self.assertEqual(url, mcp_client.DEFAULT_BASE_URL)

    def test_non_utf8_port_file_falls_back(self):
        (self.tmp / 'data' / 'backend_port.txt').write_bytes(b'\xff\xfe\x00bad')
        url, _ = mcp_client.resolve_base_url()
        self.assertEqual(url, mcp_client.DEFAULT_BASE_URL)

    def test_client_uses_port_file(self):
        self._write_port('18090')
        client = mcp_client.BackendClient()
        self.assertEqual(client.base_url, 'http://127.0.0.1:18090')
        self.assertIn('backend_port.txt', client.base_url_source)

    def test_app_dir_honours_xianyu_app_dir(self):
        """与 backend/app/core/config.py:19 同规则，测试才能把数据目录挪走。"""
        self.assertEqual(mcp_client.default_port_path(),
                         self.tmp / 'data' / 'backend_port.txt')


class WriteToolConfirmGateTest(unittest.TestCase):
    """AI 这条路上「没确认就一个请求都不发」必须成立，而且说法要统一。

    背景（2026-10-11 真机探针 `mcp_write_gate.py` 抓到的）：`mcp_server/client.py`
    原来只在「一个请求体字段都没给」时报 `invalid_args`「至少需要一个请求体字段：
    …」，于是模型调 `item_templates_delete` 会收到「至少需要一个请求体字段：confirm」
    —— 听起来像「随便补一个字段就能发」，而真正缺的是那一步确认；`promotion_write_execute`
    （自由 body）却报 `confirm_required`，同一个意思两种说法。
    """

    def _client(self):
        """任何 HTTP 都不该发生 —— 一旦真发请求，这个 request() 会直接失败。"""
        calls = []

        class Recorder(mcp_client.BackendClient):
            def __init__(self):
                super().__init__(base_url='http://127.0.0.1:9', token='T')

            def request(self, method, path, query=None, body=None):
                calls.append((method, path, body))
                raise AssertionError('未确认的写工具不该发请求：%s %s' % (method, path))

        return Recorder(), calls

    def test_every_write_route_refuses_without_confirm(self):
        """72 条写路由逐个试：都不发请求、都回 confirm_required。"""
        write_routes = sorted(k for k, v in mcp_client.TOOL_ROUTES.items()
                              if 'confirm' in (v.get('body') or []))
        self.assertEqual(len(write_routes), 72, '写工具数量变了就要同步复核这份清单')
        for name in write_routes:
            with self.subTest(tool=name):
                client, calls = self._client()
                result = client.call_tool(name, {})
                self.assertFalse(result['success'], name)
                self.assertEqual(result['code'], 'confirm_required', name)
                self.assertEqual(calls, [], name)

    def test_confirm_false_is_not_confirm(self):
        """`confirm: false` / `'false'` / `0` 都不算点头。"""
        for value in (False, 'false', 'FALSE', 0, '', None):
            with self.subTest(confirm=value):
                client, calls = self._client()
                result = client.call_tool('item_templates_delete',
                                          {'account_id': 'a', 'template_id': 't',
                                           'confirm': value})
                self.assertEqual(result['code'], 'confirm_required')
                self.assertEqual(calls, [])

    def test_read_tools_are_not_gated(self):
        """只读工具不能被这条闸门误伤：它该一路走到 dispatch，而不是回 confirm_required。"""
        seen = []

        class Recorder(mcp_client.BackendClient):
            def __init__(self):
                super().__init__(base_url='http://127.0.0.1:9', token='T')

            def request(self, method, path, query=None, body=None):
                seen.append((method, path))
                return mcp_client.ok({})

        result = Recorder().call_tool('accounts_list', {})
        self.assertTrue(result['success'], result)
        self.assertEqual(seen, [('GET', '/api/accounts')])

    def test_confirm_routes_are_exactly_the_write_tools(self):
        """反向断言：声明了 confirm 的 72 条，必须与 `agents/tools.py` 的写工具清单一致。"""
        from backend.app.agents import tools as agent_tools
        declared = {n for n, r in mcp_client.TOOL_ROUTES.items()
                    if 'confirm' in (r.get('body') or [])}
        write_specs = set()
        for spec in agent_tools.TOOL_SPECS:
            fn = spec.get('function') or {}
            props = (fn.get('parameters') or {}).get('properties') or {}
            if 'confirm' in props:
                write_specs.add(fn.get('name'))
        self.assertEqual(declared, write_specs)
        self.assertEqual(len(declared), 72)

    def test_truthy_helper_matches_service_layer(self):
        """与 `backend/app/services/item_templates.py::_truthy` 同一口径。"""
        self.assertTrue(mcp_client._truthy(True))
        self.assertTrue(mcp_client._truthy('true'))
        self.assertTrue(mcp_client._truthy(' 1 '))
        self.assertFalse(mcp_client._truthy('false'))
        self.assertFalse(mcp_client._truthy(0))
        self.assertFalse(mcp_client._truthy(None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
