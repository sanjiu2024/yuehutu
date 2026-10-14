"""MCP 工具路由 ↔ 真实 OpenAPI 的对账测试。

为什么要这个：第 53 批抓到 `orders_deliver` 指向 `POST /api/orders/{order_id}/deliver`
—— 那条端点是**纯本地**的（只改本地订单状态，零平台请求），于是外部 MCP 客户端喊「发货」
永远发不出去，而且**没有任何自动检查会报警**（路径存在、方法也对，只是语义完全错位）。

这个测试是那类「静默漂移」的回归防线：`mcp_server/client.py::TOOL_ROUTES` 的每一条都必须
和 `create_app().openapi()` 里真实注册的端点对得上 ——

1. 路径（`{param}` 归一成 `{}`）+ 方法存在；
2. `path_params` 正好等于端点路径里的占位符（多一个会 422、少一个会拼出空段）；
3. `query` 里的键必须是端点真正声明的 query 参数（**否则 FastAPI 静默忽略**，
   例如第 20 批的 `finance_income` 曾把 `from/to` 写成 `cycle_from/cycle_to`）；
   `query_rename` 改名后的名字才算数；
4. 端点的 requestBody 若是**带 properties 的模型**，工具声明的 body 键必须是它的子集；
5. 工具名必须已经能被 `canonical_name()` 认出来（别名表不留半接线）。

只读、零平台请求、不建客户端。
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.app.main import create_app  # noqa: E402
from mcp_server import client as mc  # noqa: E402

PLACEHOLDER = re.compile(r'\{([^}]+)\}')
METHODS = ('get', 'post', 'put', 'patch', 'delete')


def _normalize(path: str) -> str:
    return PLACEHOLDER.sub('{}', path)


def _endpoint_index() -> dict[tuple[str, str], tuple[str, dict]]:
    """{(归一化路径, 方法): (原始路径, operation)}"""
    paths = create_app().openapi().get('paths', {})
    index: dict[tuple[str, str], tuple[str, dict]] = {}
    for raw_path, ops in paths.items():
        for method, op in ops.items():
            if method.lower() in METHODS:
                index.setdefault((_normalize(raw_path), method.upper()), (raw_path, op))
    return index


class ToolRoutesMatchOpenApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = _endpoint_index()
        cls.routes = dict(mc.TOOL_ROUTES)

    # -- 基本前提 ---------------------------------------------------------
    def test_tool_routes_are_not_empty(self) -> None:
        self.assertGreater(len(self.routes), 100, '工具路由表太小，像是没加载到')

    def test_every_tool_name_is_canonical(self) -> None:
        bad = {
            name: mc.canonical_name(name)
            for name in self.routes
            if mc.canonical_name(name) != name
        }
        self.assertEqual(bad, {}, '别名表把工具名解析成了别的名字')

    # -- 路径 / 方法 ------------------------------------------------------
    def test_every_route_exists_in_openapi(self) -> None:
        missing = []
        for name, route in sorted(self.routes.items()):
            path = str(route.get('path') or '')
            method = str(route.get('method') or 'GET').upper()
            if (_normalize(path), method) not in self.index:
                others = sorted({
                    m for (np, m) in self.index if np == _normalize(path)
                })
                missing.append(
                    '%s → %s %s（同路径上的方法：%s）'
                    % (name, method, path, others or '路径不存在')
                )
        self.assertEqual(missing, [], '工具路由指向了不存在的端点：\n' + '\n'.join(missing))

    def test_path_params_match_the_endpoint(self) -> None:
        bad = []
        for name, route in sorted(self.routes.items()):
            path = str(route.get('path') or '')
            method = str(route.get('method') or 'GET').upper()
            hit = self.index.get((_normalize(path), method))
            if hit is None:
                continue  # 由 test_every_route_exists_in_openapi 负责报
            raw_path, _op = hit
            declared = set(PLACEHOLDER.findall(raw_path))
            tool_params = set(route.get('path_params') or [])
            if tool_params != declared:
                bad.append(
                    '%s：工具=%s 端点=%s'
                    % (name, sorted(tool_params), sorted(declared))
                )
        self.assertEqual(bad, [], 'path_params 与端点不一致：\n' + '\n'.join(bad))

    # -- 参数是否真被端点接受 --------------------------------------------
    def test_query_params_are_accepted_by_the_endpoint(self) -> None:
        bad = []
        for name, route in sorted(self.routes.items()):
            tool_query = list(route.get('query') or [])
            if not tool_query:
                continue
            path = str(route.get('path') or '')
            method = str(route.get('method') or 'GET').upper()
            hit = self.index.get((_normalize(path), method))
            if hit is None:
                continue
            _raw_path, op = hit
            accepted = {
                p.get('name') for p in (op.get('parameters') or [])
                if p.get('in') == 'query'
            }
            rename = route.get('query_rename') or {}
            unknown = [q for q in tool_query if rename.get(q, q) not in accepted]
            if unknown:
                bad.append(
                    '%s：多声明了 %s（端点只认 %s）'
                    % (name, unknown, sorted(accepted))
                )
        self.assertEqual(
            bad, [],
            '工具声明了端点不接受的 query（会被 FastAPI 静默忽略）：\n' + '\n'.join(bad),
        )

    def test_body_keys_are_accepted_by_the_endpoint(self) -> None:
        bad = []
        for name, route in sorted(self.routes.items()):
            tool_body = list(route.get('body') or [])
            if not tool_body:
                continue
            path = str(route.get('path') or '')
            method = str(route.get('method') or 'GET').upper()
            hit = self.index.get((_normalize(path), method))
            if hit is None:
                continue
            _raw_path, op = hit
            schema = (
                ((op.get('requestBody') or {}).get('content') or {})
                .get('application/json', {})
                .get('schema', {})
            )
            props = schema.get('properties')
            if not props:
                # 自由 dict（Body(...)）没有 properties，无法对账
                continue
            unknown = [b for b in tool_body if b not in props]
            if unknown:
                bad.append(
                    '%s：多声明了 %s（端点只认 %s）'
                    % (name, unknown, sorted(props))
                )
        self.assertEqual(
            bad, [], '工具声明了端点不接受的 body 字段：\n' + '\n'.join(bad)
        )

    def test_write_tools_point_at_a_gated_endpoint(self) -> None:
        """声明了 `confirm` 的写工具，必须指向**背后有确认闸门**的那条路由。

        这条专门防第 53 批那类**语义漂移**：`orders_deliver` 结构上完全合法
        （路径存在、方法对、path_params 对），但它指向的是**纯本地**的
        `POST /api/orders/{order_id}/deliver` —— 外部 MCP 客户端喊「发货」
        永远发不出去，而上面那些结构检查一条都不会报警。
        """
        from backend.app.agents import tools as agent_tools
        from backend.tests.test_write_routes_have_gates import _write_routes_without_gate

        ungated = {_normalize(item.split(' ', 1)[1]) + '|' + item.split(' ', 1)[0]
                   for item in _write_routes_without_gate()}

        bad = []
        checked = 0
        for name, route in sorted(self.routes.items()):
            spec = None
            try:
                spec = agent_tools.get_tool(name)
            except Exception:  # noqa: BLE001 - 没有规格的工具（如 master_chat）跳过
                spec = None
            if not spec:
                continue
            required = (
                ((spec.get('function') or {}).get('parameters') or {}).get('required')
            ) or []
            if 'confirm' not in required:
                continue
            checked += 1
            path = str(route.get('path') or '')
            method = str(route.get('method') or 'GET').upper()
            if _normalize(path) + '|' + method in ungated:
                bad.append('%s → %s %s（这条路由背后没有闸门）' % (name, method, path))
        self.assertGreater(checked, 20, '声明 confirm 的写工具太少，检查口径可能坏了')
        self.assertEqual(
            bad, [],
            '写工具的 MCP 路由指向了没有确认闸门的端点：\n' + '\n'.join(bad),
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
