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
import subprocess
import sys
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
        result = self.client.call_tool('chats_send', {'conversation_id': 88, 'content': '在的'})
        self.assertTrue(result['success'], result)
        call = self.recorder.calls[-1]
        self.assertEqual((call['method'], call['path']), ('POST', '/api/chats/88/send'))
        self.assertEqual(call['body'], {'content': '在的'})
        self.assertEqual(call['headers']['content-type'], 'application/json')

    def test_unknown_fields_are_not_forwarded(self):
        """模型多给的字段不能透传到后端（避免后端 422）。"""
        self.recorder.routes['/api/items/acc1/i1/edit'] = {'success': True, 'data': {}}
        self.client.call_tool('items_edit', {'account_id': 'acc1', 'item_id': 'i1',
                                             'price': 9.9, 'bogus': 'x'})
        self.assertEqual(self.recorder.calls[-1]['body'], {'price': 9.9})

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
        env = dict(os.environ)
        env['PYTHONPATH'] = str(REPO_ROOT) + os.pathsep + env.get('PYTHONPATH', '')
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        env['XYWB_BASE_URL'] = 'http://127.0.0.1:59999'   # 故意的死端口
        env['XYWB_TOKEN'] = 'SMOKE_TOKEN'
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
