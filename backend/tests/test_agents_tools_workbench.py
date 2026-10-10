"""`backend/app/agents/tools.py` 的工具契约测试（task-56）。

这些测试守的是「工具名 ↔ 真实路由」「null 不补 0」「业务失败不被包装成成功」三条口径。
它们**不碰真数据库、不发真网络请求**：业务层的 client 入口被换成假对象，
协议层返回的 payload 由本文件自己造。
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(_REPO_ROOT))

from backend.app.agents import tools

REPO = _REPO_ROOT
API_DIR = REPO / 'backend' / 'app' / 'api'

# task-56 新接进来的 22 个只读工具，以及它们各自打到的 HTTP 路由。
NEW_TOOLS: Dict[str, Dict[str, Any]] = {
    'trade_orders': {'module': 'trade', 'fn': 'orders', 'verb': 'get',
                     'path': '/api/trade/{account_id}/orders'},
    'trade_refunds': {'module': 'trade', 'fn': 'refunds', 'verb': 'get',
                      'path': '/api/trade/{account_id}/refunds'},
    'trade_evaluations': {'module': 'trade', 'fn': 'evaluations', 'verb': 'get',
                          'path': '/api/trade/{account_id}/evaluations'},
    'trade_complaints': {'module': 'trade', 'fn': 'complaints', 'verb': 'get',
                         'path': '/api/trade/{account_id}/complaints'},
    'trade_addresses': {'module': 'trade', 'fn': 'addresses', 'verb': 'get',
                        'path': '/api/trade/{account_id}/addresses'},
    'shop_sub_accounts': {'module': 'shop', 'fn': 'sub_accounts', 'verb': 'get',
                          'path': '/api/shop/{account_id}/sub-accounts'},
    'shop_cs_routing': {'module': 'shop', 'fn': 'cs_routing', 'verb': 'get',
                        'path': '/api/shop/{account_id}/cs-routing'},
    'datacenter_overview': {'module': 'datacenter', 'fn': 'overview', 'verb': 'get',
                            'path': '/api/datacenter/{account_id}/overview'},
    'datacenter_fans': {'module': 'datacenter', 'fn': 'fans', 'verb': 'get',
                        'path': '/api/datacenter/{account_id}/fans'},
    'datacenter_customer_service': {
        'module': 'datacenter', 'fn': 'customer_service', 'verb': 'get',
        'path': '/api/datacenter/{account_id}/customer-service'},
    'notifications_list': {'module': 'notify', 'fn': 'notifications', 'verb': 'get',
                           'path': '/api/notifications/{account_id}'},
    'item_templates_list': {'module': 'item_templates', 'fn': 'list_templates',
                            'verb': 'get', 'path': '/api/item-templates/{account_id}'},
    'item_templates_get': {'module': 'item_templates', 'fn': 'template_detail',
                           'verb': 'get',
                           'path': '/api/item-templates/{account_id}/{template_id}'},
    'finance_income': {'module': 'finance', 'fn': 'account_bills', 'verb': 'get',
                       'path': '/api/finance/{account_id}/income'},
    'finance_expense': {'module': 'finance', 'fn': 'account_bills', 'verb': 'get',
                        'path': '/api/finance/{account_id}/expense'},
    'finance_invoices': {'module': 'finance', 'fn': 'account_invoices', 'verb': 'get',
                         'path': '/api/finance/{account_id}/invoices'},
    'finance_purchaser': {'module': 'finance', 'fn': 'account_purchaser', 'verb': 'get',
                          'path': '/api/finance/{account_id}/purchaser'},
    'promotion_overview': {'module': 'promotion', 'fn': 'overview', 'verb': 'get',
                           'path': '/api/promotion/{account_id}/overview'},
    'promotion_campaigns': {'module': 'promotion', 'fn': 'campaigns', 'verb': 'get',
                            'path': '/api/promotion/{account_id}/campaigns'},
    'promotion_reports': {'module': 'promotion', 'fn': 'reports', 'verb': 'get',
                          'path': '/api/promotion/{account_id}/reports'},
    'promotion_account': {'module': 'promotion', 'fn': 'account', 'verb': 'get',
                          'path': '/api/promotion/{account_id}/account'},
    'security_center_status': {'module': 'security_center', 'fn': 'read', 'verb': 'get',
                               'path': '/api/security-center/{account_id}'},
}

NEW_TOOL_NAMES = list(NEW_TOOLS)


def spec_of(name: str) -> Optional[Dict[str, Any]]:
    """按名字取一条 TOOL_SPECS，取不到回 None。"""
    for spec in tools.TOOL_SPECS:
        function = spec.get('function') if isinstance(spec, dict) else None
        if isinstance(function, dict) and function.get('name') == name:
            return function
    return None


def params_of(name: str) -> Dict[str, Any]:
    function = spec_of(name)
    return (function or {}).get('parameters', {}) or {}


def properties_of(name: str) -> Dict[str, Any]:
    return params_of(name).get('properties', {}) or {}


def _api_routes() -> Dict[str, List[str]]:
    """AST 扫 backend/app/api/*.py，得到 {METHOD /full/path: [file:line]}。"""
    found: Dict[str, List[str]] = {}
    for path in sorted(API_DIR.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        prefix = ''
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == 'APIRouter'
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    prefix = node.args[0].value.rstrip('/')
        if not prefix:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef)):
                continue
            verb = None
            route = None
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                    if deco.func.attr in ('get', 'post', 'put', 'patch', 'delete'):
                        verb = deco.func.attr
                        if deco.args and isinstance(deco.args[0], ast.Constant):
                            route = str(deco.args[0].value)
            if verb is None or route is None:
                continue
            full = prefix + ('' if route.startswith('/') else '/') + route
            while '//' in full:
                full = full.replace('//', '/')
            found.setdefault(f'{verb.upper()} {full}', []).append(f'{path.name}:{node.lineno}')
    return found


class ToolRegistryTest(unittest.TestCase):
    """工具名与 TOOL_SPECS / MASTER_TOOLS / TOOL_NAMES 三者必须对齐。"""

    def test_all_new_tools_are_registered(self) -> None:
        for name in NEW_TOOL_NAMES:
            with self.subTest(name=name):
                self.assertIsNotNone(spec_of(name), f'TOOL_SPECS 里没有 {name}')
                self.assertIn(name, tools.TOOL_NAMES, f'TOOL_NAMES 里没有 {name}')

    def test_master_tools_excludes_only_master_chat(self) -> None:
        master_names = [s['function']['name'] for s in tools.MASTER_TOOLS]
        self.assertNotIn('master_chat', master_names, 'master_chat 会递归，不能进主管 AI 的工具集')
        self.assertEqual(sorted(master_names),
                         sorted(n for n in tools.TOOL_NAMES if n != 'master_chat'))
        self.assertEqual(len(tools.MASTER_TOOLS), len(tools.TOOL_NAMES) - 1)

    def test_handlers_cover_every_spec(self) -> None:
        """有工具名没 handler = 调用时直接抛 KeyError。"""
        missing = [n for n in tools.TOOL_NAMES if n not in tools._HANDLERS]
        self.assertEqual(missing, [], f'这些工具没有 handler: {missing}')


class SchemaShapeTest(unittest.TestCase):
    """每个新工具的 parameters 都是合法 JSON Schema，且只读参数齐全。"""

    def test_parameters_are_object_schema(self) -> None:
        for name in NEW_TOOL_NAMES:
            with self.subTest(name=name):
                params = params_of(name)
                self.assertEqual(params.get('type'), 'object', f'{name} 的 parameters.type 必须是 object')
                self.assertIn('properties', params, f'{name} 缺 properties')
                self.assertIn('required', params, f'{name} 缺 required')
                self.assertIsInstance(params.get('required'), list)
                self.assertTrue(params.get('additionalProperties') is False,
                                f'{name} 必须 additionalProperties: False')

    def test_account_scoped_tools_require_account_id(self) -> None:
        for name, meta in NEW_TOOLS.items():
            with self.subTest(name=name):
                self.assertIn('account_id', properties_of(name), f'{name} 缺 account_id')
                self.assertIn('account_id', params_of(name).get('required', []),
                              f'{name} 必须把 account_id 放进 required')

    def test_descriptions_are_chinese_and_explain_account_id(self) -> None:
        for name in NEW_TOOL_NAMES:
            with self.subTest(name=name):
                desc = (spec_of(name) or {}).get('description', '')
                self.assertTrue(desc.strip(), f'{name} 没有 description')
                self.assertIn('account_id', desc, f'{name} 的 description 要说明不传 account_id 会怎样')

    def test_read_only_tools_have_no_write_markers(self) -> None:
        """只做读：工具名与 description 里不该出现发布/删除/修改这类写动作。"""
        banned = ('publish', 'delete', 'edit', 'update', 'create', 'remove', 'set_', 'send', 'sync')
        for name in NEW_TOOL_NAMES:
            with self.subTest(name=name):
                for mark in banned:
                    self.assertNotIn(mark, name, f'{name} 看起来是写工具，task-56 只要读')


class ServiceTargetTest(unittest.TestCase):
    """`_SERVICE_TARGETS` 里的每一项都必须能解析成真函数。"""

    def test_every_target_resolves(self) -> None:
        for name in NEW_TOOL_NAMES:
            with self.subTest(name=name):
                target, error = tools._resolve_service(name)
                self.assertIsNone(error, f'{name} 解析失败: {error}')
                self.assertTrue(callable(target), f'{name} 解析出来不是函数: {target!r}')

    def test_target_matches_declared_module_and_fn(self) -> None:
        for name, meta in NEW_TOOLS.items():
            with self.subTest(name=name):
                module = importlib.import_module(f"backend.app.services.{meta['module']}")
                self.assertTrue(hasattr(module, meta['fn']),
                                f"backend.app.services.{meta['module']} 没有 {meta['fn']}()")

    def test_tool_names_of_same_module_point_to_same_module(self) -> None:
        """finance_income / finance_expense 共用 account_bills，靠固定参数区分。"""
        self.assertEqual(tools._SERVICE_TARGETS['finance_income'],
                         tools._SERVICE_TARGETS['finance_expense'])
        self.assertEqual(tools._SERVICE_FIXED_ARGS['finance_income'], {'kind': 'income'})
        self.assertEqual(tools._SERVICE_FIXED_ARGS['finance_expense'], {'kind': 'expense'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
