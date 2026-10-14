"""写路由闸门覆盖面测试：每条**非 GET 的 HTTP 路由**背后那个服务层函数必须有确认闸门，
或者被显式列进下面的「合法无闸门」白名单并写明理由。

为什么要这个：第 52 / 53 批连续抓到两类**反向盲区** ——

* 第 52 批：`items.update_item` / `batch_update` / `downshelf` / `reshelf` / `publish_item`
  完全没有闸门（AI 侧连 `confirm` 属性都没声明）；
* 第 53 批：`orders.update_shipment`（真发货）与 `chats.send_manual`（给真买家发消息）
  同样没有闸门，而且 `mcp_server` 的 `orders_deliver` 还指向一条**纯本地**端点。

这些都不是"某个模块漏了"，而是"这条路由背后那个函数没加闸门"，而且**没有任何自动检查**
会报警（`scripts/reconcile_tools.py` 只查"声明了 confirm 的是不是写工具"，
打包版写闸门探针只查"清单里的端点该不该被拒"）。

这个测试补上正面清单：新增一条非 GET 路由时，要么它的服务函数带闸门，
要么必须把它加进 `GATE_FREE_ALLOWLIST` 并写清理由（纯本地 / 语义只读 / 登录恢复）。

判定方式（AST，不执行任何业务代码、零平台请求）：
处理函数体里 `<service_module>.<func>(...)` → 到对应服务模块里递归查
「签名有 confirm 参数」或「出现 `_confirm_gate` / `confirmation_required` /
`confirm_required` / `_truthy`」。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

ROOT = pathlib.Path(__file__).resolve().parents[2]
API_DIR = ROOT / 'backend' / 'app' / 'api'
SERVICE_DIR = ROOT / 'backend' / 'app' / 'services'

GATE_MARKERS = ('_confirm_gate', 'confirmation_required', 'confirm_required', '_truthy')
WRITE_METHODS = ('post', 'put', 'patch', 'delete')

# 合法无闸门的非 GET 路由：值 = 理由（必须写清，方便以后复查）
GATE_FREE_ALLOWLIST: dict[str, str] = {
    'DELETE /api/accounts/{account_id}':
        '纯本地：删本地账号记录 + 清缓存，不碰平台',
    'POST /api/accounts/{account_id}/captcha':
        '纯本地：清本机风控冷却/标记已验证',
    'POST /api/accounts/{account_id}/refresh-profile':
        '只读：拉平台用户资料回写本地',
    'POST /api/accounts/{account_id}/relogin':
        '登录恢复：只启动扫码流程，用户扫码本身才是确认',
    'POST /api/chats/classify':
        '纯本地：让 LLM 建议回复，不发给平台',
    'POST /api/chats/sync':
        '只读同步：拉平台历史会话/消息回写本地',
    'PUT /api/chats/{conversation_id}/ai':
        '纯本地：开关这个会话的 AI 自动回复',
    'POST /api/chats/{conversation_id}/ai-draft':
        '纯本地：让 LLM 拟一条草稿，不发送',
    'POST /api/chats/{conversation_id}/mode':
        '纯本地：切这个会话的接待模式',
    'POST /api/chats/{conversation_id}/read':
        '纯本地：清本地未读计数',
    'POST /api/items/sync':
        '只读同步：拉平台商品列表回写本地',
    'POST /api/items/{account_id}/sync':
        '只读同步：拉平台商品列表回写本地',
    'POST /api/items/{account_id}/publish/precheck':
        '语义只读（第五十批/第四十四批口径）：只跑图片/违禁词/分类推荐，绝不发布',
    'DELETE /api/items/{account_id}/{item_id}':
        '纯本地：只从本地列表移除；平台删除走 batch-delete（那条有闸门）',
    'POST /api/orders/sync':
        '只读同步：拉平台订单回写本地',
    'POST /api/orders/{order_id}/deliver':
        '纯本地：只改本地订单状态；真发货走 PUT /{order_id}/shipment（那条有闸门）',
    'POST /api/risk/clear':
        '纯本地：清本机风控冷却',
    'POST /api/risk/{account_id}/clear':
        '纯本地：清本机风控冷却',
    'PUT /api/risk/settings':
        '纯本地：改本机风控设置',
    'POST /api/security-center/{account_id}/appeal/form':
        '语义只读（契约写明）：取申诉表单，提交申诉那条有闸门',
    'POST /api/uploads/images':
        '上传自己的图片换 URL，不改平台业务数据；真正的副作用在发布那条（有闸门）',
}


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding='utf-8'))


def _router_prefix(tree: ast.Module) -> str:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'router':
                    if isinstance(node.value, ast.Call):
                        for kw in node.value.keywords:
                            if kw.arg == 'prefix' and isinstance(kw.value, ast.Constant):
                                return str(kw.value.value)
    return ''


def _service_aliases(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith('services'):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out


def _service_functions() -> dict[str, dict[str, ast.AST]]:
    table: dict[str, dict[str, ast.AST]] = {}
    for path in sorted(SERVICE_DIR.glob('*.py')):
        tree = _parse(path)
        table[path.stem] = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    return table


def _has_gate(funcs: dict[str, ast.AST], name: str, seen: set[str] | None = None) -> bool:
    seen = seen if seen is not None else set()
    if name in seen or name not in funcs:
        return False
    seen.add(name)
    node = funcs[name]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        names = [a.arg for a in list(args.args) + list(args.kwonlyargs)]
        if 'confirm' in names:
            return True
    dumped = ast.dump(node)
    if any(marker in dumped for marker in GATE_MARKERS):
        return True
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id.startswith('_') and func.id in funcs:
                if _has_gate(funcs, func.id, seen):
                    return True
    return False


def _write_routes_without_gate() -> list[str]:
    """返回形如 `POST /api/xxx` 的列表：非 GET 路由，且其服务函数查不到闸门。"""
    services = _service_functions()
    out: list[str] = []
    for path in sorted(API_DIR.glob('*.py')):
        tree = _parse(path)
        prefix = _router_prefix(tree)
        aliases = _service_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'router'
                ):
                    continue
                method = func.attr.lower()
                if method not in WRITE_METHODS:
                    continue
                raw = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else ''
                full = prefix + str(raw)
                gated: bool | None = None
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.Call):
                        continue
                    target = sub.func
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in aliases
                    ):
                        continue
                    module = aliases[target.value.id]
                    if module not in services or target.attr not in services[module]:
                        continue
                    if _has_gate(services[module], target.attr):
                        gated = True
                    elif gated is None:
                        gated = False
                if gated is False:
                    out.append('%s %s' % (method.upper(), full))
    return out


class WriteRoutesHaveGatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.without_gate = _write_routes_without_gate()

    def test_scan_found_routes(self) -> None:
        """扫描本身要有产出，否则说明 AST 逻辑坏了（静默通过最危险）。"""
        self.assertGreater(
            len(_write_routes_without_gate()) + 80, 80,
            '扫描结果异常，检查 _write_routes_without_gate()',
        )

    def test_每一条无闸门的写路由都在白名单里(self) -> None:
        new = [r for r in self.without_gate if r not in GATE_FREE_ALLOWLIST]
        self.assertEqual(
            new, [],
            '这些非 GET 路由背后的服务函数查不到确认闸门，也没有登记为「合法无闸门」：\n  '
            + '\n  '.join(new)
            + '\n\n要么给服务函数加 `_confirm_gate`，要么在 '
              'backend/tests/test_write_routes_have_gates.py 的 GATE_FREE_ALLOWLIST 里'
              '登记并写清理由。',
        )

    def test_白名单没有过期条目(self) -> None:
        stale = [r for r in GATE_FREE_ALLOWLIST if r not in self.without_gate]
        self.assertEqual(
            stale, [],
            '白名单里这些条目现在已经有闸门了（或路由没了），删掉它们免得白名单腐烂：\n  '
            + '\n  '.join(stale),
        )

    def test_白名单每条都写了理由(self) -> None:
        blank = [r for r, reason in GATE_FREE_ALLOWLIST.items() if not reason.strip()]
        self.assertEqual(blank, [], '白名单条目必须写理由：\n  ' + '\n  '.join(blank))


if __name__ == '__main__':
    unittest.main(verbosity=2)
