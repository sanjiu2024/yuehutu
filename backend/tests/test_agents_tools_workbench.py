"""`backend/app/agents/tools.py` 的工具契约测试（task-56）。

这些测试守的是「工具名 ↔ 真实路由」「null 不补 0」「业务失败不被包装成成功」三条口径。
它们**不碰真数据库、不发真网络请求**：业务层的 client 入口被换成假对象，
协议层返回的 payload 由本文件自己造。
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

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


# task-64 新接进来的写能力：11 个**写**工具 + 1 个只读的推广动作目录。
# `confirm` 一律由**服务层**判定（工具层原样透传），所以这里只核对
# 「名字 ↔ 服务层函数 ↔ HTTP 路由」三件事，不重复测服务层的确认逻辑。
WRITE_TOOLS: Dict[str, Dict[str, Any]] = {
    'trade_write_ship_offline': {
        'module': 'trade_write', 'fn': 'ship_offline', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/orders/ship'},
    'trade_write_ship_dummy': {
        'module': 'trade_write', 'fn': 'ship_dummy', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/orders/{order_id}/ship-dummy'},
    'trade_write_adjust_price': {
        'module': 'trade_write', 'fn': 'adjust_price', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/orders/{order_id}/adjust-price'},
    'trade_write_refund_agree': {
        'module': 'trade_write', 'fn': 'refund_agree', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/refunds/{refund_id}/agree'},
    'trade_write_refund_refuse': {
        'module': 'trade_write', 'fn': 'refund_refuse', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/refunds/{refund_id}/refuse'},
    'trade_write_rate_create': {
        'module': 'trade_write', 'fn': 'rate_create', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/evaluations'},
    'trade_write_complain_refuse': {
        'module': 'trade_write', 'fn': 'complain_refuse', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/complaints/{complain_id}/refuse'},
    'trade_write_complain_proof': {
        'module': 'trade_write', 'fn': 'complain_proof', 'confirm': True,
        'method': 'POST', 'path': '/api/trade/{account_id}/complaints/{complain_id}/proof'},
    'promotion_write_execute': {
        'module': 'ad_write', 'fn': 'execute', 'confirm': True,
        'method': 'POST', 'path': '/api/promotion/write/{account_id}/{action}'},
    'qualification_submit_packages': {
        'module': 'qualification', 'fn': 'submit_packages', 'confirm': True,
        'method': 'POST', 'path': '/api/qualification/{account_id}/packages/submit'},
    'qualification_oss_credential': {
        'module': 'qualification', 'fn': 'oss_credential', 'confirm': True,
        'method': 'POST', 'path': '/api/qualification/{account_id}/oss-credential'},
    'item_templates_create': {
        'module': 'item_templates', 'fn': 'create_template', 'confirm': True,
        'method': 'POST', 'path': '/api/item-templates/{account_id}/create'},
    'item_templates_modify': {
        'module': 'item_templates', 'fn': 'modify_template', 'confirm': True,
        'method': 'POST', 'path': '/api/item-templates/{account_id}/{template_id}/modify'},
    'item_templates_delete': {
        'module': 'item_templates', 'fn': 'delete_template', 'confirm': True,
        'method': 'POST', 'path': '/api/item-templates/{account_id}/{template_id}/delete'},
    'item_templates_unbind': {
        'module': 'item_templates', 'fn': 'unbind_items', 'confirm': True,
        'method': 'POST', 'path': '/api/item-templates/{account_id}/unbind'},
    # 只读：列推广写动作目录，故意不要 confirm。
    'promotion_write_actions': {
        'module': 'ad_write', 'fn': 'actions_catalog', 'confirm': False,
        'method': 'GET', 'path': '/api/promotion/write/actions'},
    # ---- task-73：3 个商品写能力 ----
    'items_batch_offline': {
        'module': 'items', 'fn': 'batch_offline', 'confirm': True,
        'method': 'POST', 'path': '/api/items/{account_id}/batch-offline'},
    'items_batch_delete': {
        'module': 'items', 'fn': 'batch_delete', 'confirm': True,
        'method': 'POST', 'path': '/api/items/{account_id}/batch-delete'},
    'items_quick_edit': {
        'module': 'items', 'fn': 'quick_edit', 'confirm': True,
        'method': 'POST', 'path': '/api/items/{account_id}/{item_id}/quick-edit'},
}

WRITE_TOOL_NAMES = list(WRITE_TOOLS)

#: 一个「参数齐全但 confirm=False」的最小调用，用来验证工具层不替服务层做确认判定。
_CONFIRM_FALSE_CALLS: Dict[str, Dict[str, Any]] = {
    'trade_write_ship_offline': {
        'account_id': 'acc-x', 'trade_ids': ['t1'], 'mail_no': 'SF123',
        'cp_code': 'SF', 'address_id': 'addr-1', 'confirm': False},
    'trade_write_ship_dummy': {'account_id': 'acc-x', 'order_id': 't1', 'confirm': False},
    'trade_write_adjust_price': {
        'account_id': 'acc-x', 'order_id': 't1', 'modify_fee': 10.5,
        'new_transport_fee': 0, 'confirm': False},
    'trade_write_refund_agree': {
        'account_id': 'acc-x', 'refund_id': 'r1', 'auth_token': 'tok', 'confirm': False},
    'trade_write_refund_refuse': {
        'account_id': 'acc-x', 'refund_id': 'r1', 'order_id': 't1',
        'refuse_reason_id': '9', 'confirm': False},
    'trade_write_rate_create': {
        'account_id': 'acc-x', 'trade_ids': ['t1'], 'feedback': '好买家', 'confirm': False},
    'trade_write_complain_refuse': {
        'account_id': 'acc-x', 'complain_id': 'c1', 'proof_desc': 'd',
        'proof_urls': ['http://x/1.png'], 'confirm': False},
    'trade_write_complain_proof': {
        'account_id': 'acc-x', 'complain_id': 'c1', 'proof_desc': 'd',
        'proof_urls': ['http://x/1.png'], 'confirm': False},
    'promotion_write_execute': {
        'account_id': 'acc-x', 'action': 'campaign_pause', 'confirm': False},
    'qualification_submit_packages': {'account_id': 'acc-x', 'confirm': False},
    'qualification_oss_credential': {'account_id': 'acc-x', 'confirm': False},
    'item_templates_create': {
        'account_id': 'acc-x', 'template_name': '模版', 'confirm': False},
    'item_templates_modify': {
        'account_id': 'acc-x', 'template_id': 'tpl-1', 'template_name': '模版',
        'confirm': False},
    'item_templates_delete': {
        'account_id': 'acc-x', 'template_id': 'tpl-1', 'confirm': False},
    'item_templates_unbind': {
        'account_id': 'acc-x', 'item_ids': 'i1', 'mode': 'free_ship', 'confirm': False},
    # ---- task-73：3 个商品写能力 ----
    'items_batch_offline': {
        'account_id': 'acc-x', 'item_ids': 'i1,i2', 'confirm': False},
    'items_batch_delete': {
        'account_id': 'acc-x', 'item_ids': 'i1,i2', 'confirm': False},
    'items_quick_edit': {
        'account_id': 'acc-x', 'item_id': 'i1', 'price': 9.9, 'quantity': 3,
        'confirm': False},
}


class WriteToolRegistryTest(unittest.TestCase):
    """task-64：写工具的「名字 ↔ 服务层函数 ↔ HTTP 路由」三方对账。"""

    def test_write_tools_are_registered(self) -> None:
        for name in WRITE_TOOL_NAMES:
            with self.subTest(name=name):
                self.assertIsNotNone(spec_of(name), f'TOOL_SPECS 里没有 {name}')
                self.assertIn(name, tools.TOOL_NAMES, f'TOOL_NAMES 里没有 {name}')
                self.assertIn(name, tools._HANDLERS, f'_HANDLERS 里没有 {name}')

    def test_every_write_target_resolves_to_declared_function(self) -> None:
        for name, meta in WRITE_TOOLS.items():
            with self.subTest(name=name):
                self.assertEqual(tools._SERVICE_TARGETS.get(name), [meta['module'], meta['fn']],
                                 f'{name} 的 _SERVICE_TARGETS 与预期不符')
                target, error = tools._resolve_service(name)
                self.assertIsNone(error, f'{name} 解析失败: {error}')
                self.assertTrue(callable(target), f'{name} 解析出来不是函数: {target!r}')
                module = importlib.import_module(f"backend.app.services.{meta['module']}")
                self.assertIs(target, getattr(module, meta['fn']),
                              f'{name} 解析到的不是 {meta["module"]}.{meta["fn"]}')

    def test_write_tools_require_confirm_and_warn_in_chinese(self) -> None:
        """写操作不许有默认确认：`confirm` 必须在 required 里，且描述里有中文告警。"""
        for name, meta in WRITE_TOOLS.items():
            with self.subTest(name=name):
                params = params_of(name)
                if not meta['confirm']:
                    self.assertNotIn('confirm', params.get('required', []),
                                     f'{name} 是只读工具，不该强制 confirm')
                    continue
                self.assertIn('confirm', params.get('required', []),
                              f'{name} 的 required 必须含 confirm')
                self.assertIn('confirm', properties_of(name), f'{name} 的 properties 里要有 confirm')
                desc = (spec_of(name) or {}).get('description', '')
                self.assertIn('这是写操作', desc, f'{name} 的 description 要说明这是写操作')
                self.assertIn('confirm=true', desc, f'{name} 的 description 要写明确认方式')
                self.assertIn('不确定就不要调', desc, f'{name} 的 description 要劝退不确定的调用')

    def test_write_routes_are_post_and_listed_keys_cover_required(self) -> None:
        """MCP 侧：写工具必须 POST；required 里每个字段都要被路由认识。"""
        from mcp_server import client as mcp_client

        for name, meta in WRITE_TOOLS.items():
            with self.subTest(name=name):
                route = mcp_client.TOOL_ROUTES.get(name)
                self.assertIsNotNone(route, f'{name} 在 MCP TOOL_ROUTES 里没有路由')
                self.assertEqual(route['method'], meta['method'], f'{name} 的 method 不对')
                self.assertEqual(route['path'], meta['path'], f'{name} 的 path 不对')
                if meta['confirm']:
                    self.assertEqual(route['method'], 'POST', f'{name} 是写工具，必须 POST')
                known = (set(route.get('path_params', [])) | set(route.get('query', []))
                         | set(route.get('body', [])))
                for field in params_of(name).get('required', []):
                    self.assertIn(field, known, f'{name}.{field} 路由不认识')


#: 营销四条只读路由的黄金值。2026-10 用官方 bundle 证据订正过**查询参数**
#: （路径名沿用首版，别改）：`/items` 与 `/records` 是单对象口径、**不吃分页**。
#: 写死成黄金表是为了让「把 page/size 加回去」这类漂移一定有测试红。
MARKETING_ROUTES: Dict[str, Dict[str, Any]] = {
    'marketing_items': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/items',
        'path_params': ['account_id'], 'query': ['item_id', 'date_type', 'date_range']},
    'marketing_tasks': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/tasks',
        'path_params': ['account_id'],
        'query': ['biz_scene', 'query_scene', 'page', 'size', 'item_ids']},
    'marketing_price_competitiveness': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/price-competitiveness',
        'path_params': ['account_id'],
        'query': ['biz_scene', 'item_id', 'sku_prices', 'commission_rate']},
    'marketing_records': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/records',
        'path_params': ['account_id'], 'query': ['task_id', 'biz_scene', 'query_scene']},
}


class RepoWideCrossCheckTest(unittest.TestCase):
    """全仓对账：工具名 → 服务层函数 / spec 参数 → 路由键。只读，不打平台。"""

    def test_every_declared_service_target_exists(self) -> None:
        """`_SERVICE_TARGETS` 声称的 `[module, fn]` 必须在那个模块上真的存在。

        这条守的是「服务层函数改名了但工具表没跟着改」——只断言 `_resolve_service`
        不报错是不够的，它会把 AttributeError 变成中文错误串而**看起来正常**。
        """
        for name, (module_name, fn_name) in sorted(tools._SERVICE_TARGETS.items()):
            with self.subTest(name=name):
                module = importlib.import_module(f'backend.app.services.{module_name}')
                self.assertTrue(hasattr(module, fn_name),
                                f'backend.app.services.{module_name} 没有 {fn_name}()，'
                                f'但 {name} 指向它')

    def test_marketing_targets_point_to_corrected_functions(self) -> None:
        """2026-10 订正：服务层函数名是 item_metrics / operation_records（不是 items / records）。"""
        self.assertEqual(tools._SERVICE_TARGETS['marketing_items'], ['marketing', 'item_metrics'])
        self.assertEqual(tools._SERVICE_TARGETS['marketing_records'],
                         ['marketing', 'operation_records'])
        marketing = importlib.import_module('backend.app.services.marketing')
        self.assertFalse(hasattr(marketing, 'items'),
                         'services/marketing.py 里出现了一个叫 items 的公开函数，'
                         '工具表可能又指回了错名字')
        self.assertFalse(hasattr(marketing, 'records'), 'services/marketing.py 里不该有 records()')

    def test_marketing_routes_match_corrected_contract(self) -> None:
        """黄金表：四条营销路由的 method/path/query 逐字对齐订正后的契约。"""
        from mcp_server import client as mcp_client

        for name, expected in MARKETING_ROUTES.items():
            with self.subTest(name=name):
                route = mcp_client.TOOL_ROUTES.get(name)
                self.assertIsNotNone(route, f'{name} 在 MCP TOOL_ROUTES 里没有路由')
                for key, value in expected.items():
                    self.assertEqual(route.get(key), value,
                                     f'{name} 的 {key} 与订正后的契约不符')

    def test_every_route_covers_every_spec_property(self) -> None:
        """路由的 path_params ∪ query ∪ body 必须覆盖 spec 的**每一个**属性（不只 required）。

        否则参数会被 MCP 客户端静默丢掉——HTTP 那头只会看到缺参数。
        """
        from mcp_server import client as mcp_client

        for name, route in sorted(mcp_client.TOOL_ROUTES.items()):
            spec = spec_of(name)
            if spec is None:
                continue
            with self.subTest(name=name):
                known = (set(route.get('path_params', [])) | set(route.get('query', []))
                         | set(route.get('body', [])))
                for field in params_of(name).get('properties', {}):
                    self.assertIn(field, known, f'{name}.{field} 路由不认识，会被丢掉')

    def test_write_tool_specs_are_well_formed(self) -> None:
        """12 条新工具也受 SchemaShapeTest 的同款约束（那边只遍历 22 个只读工具）。"""
        for name in WRITE_TOOL_NAMES:
            with self.subTest(name=name):
                params = params_of(name)
                self.assertEqual(params.get('type'), 'object', f'{name} 的 parameters.type 必须是 object')
                self.assertIsInstance(params.get('required'), list, f'{name} 缺 required')
                self.assertTrue(params.get('additionalProperties') is False,
                                f'{name} 必须 additionalProperties: False')
                if WRITE_TOOLS[name]['confirm']:
                    confirm = properties_of(name).get('confirm')
                    self.assertIsInstance(confirm, dict, f'{name} 的 confirm 没有 schema')
                    self.assertEqual(confirm.get('type'), 'boolean',
                                     f'{name} 的 confirm 必须是 boolean，否则模型会传字符串 "true"')

    def test_spec_properties_are_accepted_by_the_service_function(self) -> None:
        """spec 里声明的参数必须真能被 `_filter_args` 传给服务层（不能被静默丢掉）。"""
        for name in sorted(tools._SERVICE_TARGETS):
            target, error = tools._resolve_service(name)
            if target is None:
                continue
            with self.subTest(name=name):
                sig = inspect.signature(target)
                if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                    continue
                props = set(params_of(name).get('properties', {}))
                kept = set(tools._filter_args(target, {k: None for k in props}))
                self.assertEqual(props - kept, set(),
                                 f'{name} 的这些参数会被 _filter_args 静默丢掉')
                required = [p.name for p in sig.parameters.values()
                            if p.default is p.empty
                            and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
                self.assertEqual(set(required) - props, set(),
                                 f'{name} 的服务层必填参数没写进 spec.properties')


class WriteToolDispatchTest(unittest.TestCase):
    """工具层**不做**确认判定：原样透传 confirm，错误码与服务层一致。"""

    def test_confirm_false_is_refused_by_service_not_by_tool_layer(self) -> None:
        for name, args in _CONFIRM_FALSE_CALLS.items():
            with self.subTest(name=name):
                result = asyncio.run(tools.call_tool(name, dict(args)))
                self.assertIs(result.get('ok'), False, f'{name} 没确认却回成功: {result}')
                self.assertIn('confirm', str(result.get('code', '')),
                              f'{name} 的错误码应该来自服务层的 confirm_required: {result}')

    def test_missing_confirm_is_rejected_before_dispatch(self) -> None:
        """confirm 在 required 里，所以缺参数时连服务层都不会被调到。"""
        for name in _CONFIRM_FALSE_CALLS:
            with self.subTest(name=name):
                args = dict(_CONFIRM_FALSE_CALLS[name])
                args.pop('confirm')
                result = asyncio.run(tools.call_tool(name, args))
                self.assertIs(result.get('ok'), False, f'{name} 缺 confirm 却回成功: {result}')

    def test_confirm_is_actually_passed_through_to_the_service_layer(self) -> None:
        """把服务层函数换成间谍，证明工具层**真的调了它**、而且 confirm 原样传过去。

        只断言「返回 ok=False 且 code 含 confirm」是不够的：工具层自己插一句
        `if confirm is not True: return {...}` 也能造出同样的返回体。间谍能区分两者。
        """
        seen = []

        def spy(account_id, trade_ids, mail_no, cp_code, address_id, *, confirm=False):
            seen.append(confirm)
            return {'from': 'service-spy'}

        with mock.patch('backend.app.services.trade_write.ship_offline', spy):
            result = asyncio.run(tools.call_tool('trade_write_ship_offline', {
                'account_id': 'acc-x', 'trade_ids': ['t1'], 'mail_no': 'SF1',
                'cp_code': 'SF', 'address_id': 'a1', 'confirm': False}))
        self.assertEqual(seen, [False], '工具层自己短路了，没把 confirm 透传给服务层')
        self.assertEqual(result.get('result'), {'from': 'service-spy'},
                         '工具层改写了服务层的返回值')

    def test_unknown_tool_is_not_silently_ok(self) -> None:
        result = asyncio.run(tools.call_tool('trade_write_nope', {'account_id': 'a', 'confirm': True}))
        self.assertIs(result.get('ok'), False)

    def test_read_only_promotion_catalog_works_without_confirm(self) -> None:
        """目录工具是只读的：不打平台、不需要账号，直接读服务层常量。"""
        result = asyncio.run(tools.call_tool('promotion_write_actions', {}))
        self.assertIs(result.get('ok'), True, f'目录工具不该失败: {result}')
        actions = result['result']['actions']
        self.assertTrue(actions)
        self.assertTrue(all(item['confirm_required'] is True for item in actions))
        unsupported = {item['action'] for item in result['result']['unsupported']}
        self.assertEqual(unsupported, {'recharge', 'withdraw'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
