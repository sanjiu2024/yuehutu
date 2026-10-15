"""AI/MCP 可调用面收口（task-8 / 第 58 批）：单条上下架写工具 items_delist / items_relist。

背景：`docs/audit/write_consistency.md` §0 问题 9 指出 123 条写路由里有 42 条没有 AI/MCP 面，
本批把其中**真缺口**的两条接上 —— `POST /api/items/{account_id}/{item_id}/delist` 与
`.../relist`（前端 Items.tsx 一直在用，AI/MCP 面却调不到）。

这条测试守的是「三方接线 + 闸门」四件事，缺一条都会让模型以为能改平台而其实没接上：
1. `backend/app/agents/tools.py` TOOL_SPECS：required 含 confirm、description 有中文警示；
2. `mcp_server/client.py` TOOL_ROUTES：POST 到真实注册的路径，body 带 confirm；
3. 服务层落点真实存在（`backend/app/services/items.py` downshelf / reshelf），
   且工具参数 `item_id` 经 `_SERVICE_ARG_MAPS` 映射成服务形参 `item_ids`；
4. `scripts/reconcile_tools.py` 的 WRITE_PREFIX 登记（否则五方对账第 5 步会把它们
   误判成「只读工具却带 confirm 参数」）。

跑法（仓库根目录）：
    PYTHONPATH=. C:\\Python310\\python.exe backend\\tests\\test_ai_surface58.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
import unittest
from urllib import request as urllib_request
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agents import tools as agent_tools  # noqa: E402
from backend.app.services import items as items_service  # noqa: E402
from mcp_server import client as mcp_client  # noqa: E402

#: 本批接的两条写工具 → (MCP 路径, 服务函数名)
NEW_WRITE_TOOLS = {
    'items_delist': ('/api/items/{account_id}/{item_id}/delist', 'downshelf'),
    'items_relist': ('/api/items/{account_id}/{item_id}/relist', 'reshelf'),
}

WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')


def _spec(name: str) -> dict:
    for entry in agent_tools.TOOL_SPECS:
        if entry['function']['name'] == name:
            return entry['function']
    raise AssertionError(f'TOOL_SPECS 里没有 {name}')


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
    """拦住 urllib.request.urlopen，记录真正发出去的请求。"""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        split = urlsplit(req.full_url)
        raw = req.data
        self.calls.append({
            'method': req.get_method(),
            'path': split.path,
            'query': dict(parse_qsl(split.query)),
            'body': json.loads(raw.decode('utf-8')) if raw else None,
        })
        return FakeResponse({'success': True, 'data': {}})


class ToolSpecTest(unittest.TestCase):
    """1) agents 面：写工具必须声明 confirm 闸门。"""

    def test_specs_require_confirm(self):
        for name in NEW_WRITE_TOOLS:
            fn = _spec(name)
            required = fn['parameters'].get('required', [])
            self.assertIn('confirm', required, f'{name} 的 required 缺 confirm')
            self.assertIn('confirm', fn['parameters']['properties'], f'{name} 的 properties 缺 confirm')
            self.assertIn('confirm=true', fn.get('description', ''), f'{name} 描述没写 confirm=true')
            self.assertFalse(fn['parameters'].get('additionalProperties', False) is not False
                             and fn['parameters']['additionalProperties'],
                             f'{name} 应禁止额外字段')

    def test_required_fields_are_declared(self):
        for name in NEW_WRITE_TOOLS:
            fn = _spec(name)
            props = fn['parameters']['properties']
            for key in fn['parameters']['required']:
                self.assertIn(key, props, f'{name} required 里的 {key} 没在 properties 声明')


class RouteTest(unittest.TestCase):
    """2) MCP 面：路由落到真实注册的写路径，body 带 confirm。"""

    def test_routes_are_platform_writes(self):
        for name, (path, _service) in NEW_WRITE_TOOLS.items():
            route = mcp_client.TOOL_ROUTES.get(name)
            self.assertIsNotNone(route, f'TOOL_ROUTES 里没有 {name}')
            self.assertEqual(route['method'], 'POST', f'{name} 应该是 POST')
            self.assertEqual(route['path'], path, f'{name} 路径不对')
            self.assertEqual(route.get('path_params'), ['account_id', 'item_id'])
            self.assertIn('confirm', route.get('body') or [], f'{name} 的 body 没带 confirm')

    def test_canonical_specs_match_route(self):
        for name in NEW_WRITE_TOOLS:
            spec = mcp_client.CANONICAL_SPECS.get(name)
            self.assertIsNotNone(spec, f'CANONICAL_SPECS 里没有 {name}')
            self.assertEqual(spec['input_schema']['required'], ['account_id', 'item_id', 'confirm'])

    def test_paths_are_registered_in_app(self):
        """TOOL_ROUTES 的路径必须真的注册在 FastAPI 上（否则 call_tool 只会 404）。"""
        from backend.app.main import create_app
        app = create_app()
        # 用 openapi() 而不是 app.routes：include_router 之后 app.routes 里是
        # _IncludedRouter（没有 .path），openapi 的 paths 才是展开后的真实路径。
        registered = set(app.openapi().get('paths') or {})
        for name, (path, _service) in NEW_WRITE_TOOLS.items():
            self.assertIn(path, registered, f'{name} 的路径 {path} 没注册')


class ConfirmGateTest(unittest.TestCase):
    """3) 客户端闸门：没点头根本发不出请求。"""

    def setUp(self):
        self.recorder = UrlRecorder()
        self._orig = urllib_request.urlopen
        urllib_request.urlopen = self.recorder
        self.addCleanup(setattr, urllib_request, 'urlopen', self._orig)
        self.client = mcp_client.BackendClient(base_url='http://127.0.0.1:9999', token='T')

    def test_without_confirm_no_request(self):
        for name, (path, _service) in NEW_WRITE_TOOLS.items():
            result = self.client.call_tool(name, {'account_id': 'acc1', 'item_id': 'i1'})
            self.assertFalse(result.get('success'), f'{name} 没 confirm 却成功了：{result}')
            self.assertEqual(result.get('code'), 'confirm_required', f'{name} 的错误码不对：{result}')
        self.assertEqual(self.recorder.calls, [], '没确认却发出了请求')

    def test_confirm_true_posts_to_route(self):
        for name, (path, _service) in NEW_WRITE_TOOLS.items():
            result = self.client.call_tool(name, {'account_id': 'acc1', 'item_id': 'i1',
                                                  'confirm': True})
            self.assertTrue(result.get('success'), result)
            call = self.recorder.calls[-1]
            self.assertEqual((call['method'], call['path']), ('POST', path.replace('{account_id}', 'acc1')
                                                              .replace('{item_id}', 'i1')))
            self.assertEqual(call['body'], {'confirm': True})


class ServiceWiringTest(unittest.TestCase):
    """4) agents → services 落点：item_id 必须映射成服务形参 item_ids。"""

    def test_targets_and_handlers(self):
        for name, (_path, service) in NEW_WRITE_TOOLS.items():
            self.assertEqual(agent_tools._SERVICE_TARGETS.get(name), ['items', service],
                             f'{name} 的 _SERVICE_TARGETS 不对')
            self.assertIn(name, agent_tools._HANDLERS, f'{name} 没有 handler')

    def test_arg_map_declares_item_ids(self):
        for name in NEW_WRITE_TOOLS:
            self.assertEqual(agent_tools._SERVICE_ARG_MAPS.get(name), {'item_id': 'item_ids'},
                             f'{name} 的 _SERVICE_ARG_MAPS 不对')

    def test_service_signatures_accept_item_ids_and_confirm(self):
        for name, (_path, service) in NEW_WRITE_TOOLS.items():
            fn = getattr(items_service, service, None)
            self.assertIsNotNone(fn, f'services/items.py 里没有 {service}')
            params = list(__import__('inspect').signature(fn).parameters)
            self.assertEqual(params[0], 'item_ids', f'{service} 第一个形参应该是 item_ids')
            self.assertIn('confirm', params, f'{service} 收不到 confirm')

    def test_dispatch_maps_item_id_to_item_ids(self):
        captured = {}

        def fake(item_ids=None, account_id=None, confirm=None):
            captured.update(item_ids=item_ids, account_id=account_id, confirm=confirm)
            return {'success': True, 'data': {}}

        for name, (_path, service) in NEW_WRITE_TOOLS.items():
            original = getattr(items_service, service)
            setattr(items_service, service, fake)
            try:
                result = asyncio.run(agent_tools._dispatch_service(
                    name, {'account_id': 'acc1', 'item_id': 'i1', 'confirm': True}))
            finally:
                setattr(items_service, service, original)
            self.assertTrue(result.get('ok'), f'{name} 派发失败：{result}')
            self.assertEqual(captured, {'item_ids': 'i1', 'account_id': 'acc1', 'confirm': True},
                             f'{name} 的实参不对：{captured}')

    def test_service_gate_rejects_without_confirm(self):
        """服务层是第一道闸门：不给 confirm 必须返回业务失败信封，不碰平台。"""
        for name, (_path, service) in NEW_WRITE_TOOLS.items():
            result = asyncio.run(agent_tools._dispatch_service(
                name, {'account_id': 'acc1', 'item_id': 'i1'}))
            self.assertFalse(result.get('ok'), f'{name} 没确认却 ok=True：{result}')
            self.assertEqual(result.get('code'), items_service.CONFIRM_CODE, result)


class ApiPassthroughTest(unittest.TestCase):
    """5) 路由层只透传：confirm 原样交给服务层（闸门不在路由层，别两边各判一次）。"""

    def test_api_handlers_forward_confirm(self):
        source = (REPO_ROOT / 'backend' / 'app' / 'api' / 'items.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ('delist_item', 'relist_item'):
                found[node.name] = ast.unparse(node)
        self.assertEqual(set(found), {'delist_item', 'relist_item'})
        self.assertIn('confirm=', found['delist_item'])
        self.assertIn('downshelf', found['delist_item'])
        self.assertIn('confirm=', found['relist_item'])
        self.assertIn('reshelf', found['relist_item'])


class ReconcileRegistrationTest(unittest.TestCase):
    """6) 五方对账：带 confirm 的写工具必须登记进 WRITE_PREFIX。

    `scripts/reconcile_tools.py` 的 WRITE_PREFIX 是硬编码元组（脚本 docstring 明说新增写工具
    要同步登记），漏登记会让第 5 步把它误判成「只读工具却带 confirm 参数」⇒ RECON_FAIL。
    这里按「有 confirm + 走写方法」**反推**应登记集合，避免以后又漏。
    """

    def _write_prefix(self) -> tuple:
        source = (REPO_ROOT / 'scripts' / 'reconcile_tools.py').read_text(encoding='utf-8')
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'WRITE_PREFIX' for t in node.targets):
                return ast.literal_eval(node.value)
        raise AssertionError('reconcile_tools.py 里找不到 WRITE_PREFIX')

    def test_every_confirm_tool_is_registered(self):
        prefix = self._write_prefix()
        missing = []
        for entry in agent_tools.TOOL_SPECS:
            fn = entry['function']
            name = fn['name']
            if name == 'promotion_write_actions':  # 只读目录，脚本显式排除
                continue
            if 'confirm' not in fn['parameters'].get('required', []):
                continue
            route = mcp_client.TOOL_ROUTES.get(name) or {}
            if route.get('method') not in WRITE_METHODS:
                continue
            if not name.startswith(prefix):
                missing.append(name)
        self.assertEqual(missing, [], f'这些写工具没登记进 WRITE_PREFIX：{missing}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
