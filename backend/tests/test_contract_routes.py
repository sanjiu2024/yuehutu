"""契约一致性测试：`docs/CONTRACTS.md` §3 冻结的 REST 路径必须真的注册出来。

为什么要这个：并行开发时前端按契约写调用、后端各自实现，两边都"能跑"但一接就 404。
这个测试是唯一的客观裁判 —— 契约里写了就必须存在（多出来的额外路由不算错）。

只查"路径 + 方法"是否注册，不校验业务行为（行为由各模块自己的测试负责）。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.app.main import create_app  # noqa: E402

# docs/CONTRACTS.md §3 的冻结清单（路径 -> 允许的方法集合）
CONTRACT_ROUTES: dict[str, set[str]] = {
    '/api/auth/login': {'POST'},
    '/api/auth/logout': {'POST'},
    '/api/auth/me': {'GET'},
    '/api/health': {'GET'},
    '/api/accounts': {'GET'},
    '/api/accounts/qr/start': {'POST'},
    '/api/accounts/qr/{ticket}': {'GET'},
    '/api/accounts/{account_id}': {'PATCH', 'DELETE'},
    '/api/accounts/{account_id}/relogin': {'POST'},
    '/api/accounts/{account_id}/captcha': {'POST'},
    '/api/items': {'GET'},
    '/api/items/{account_id}/{item_id}': {'GET'},
    '/api/items/{account_id}/sync': {'POST'},
    '/api/items/{account_id}/publish': {'POST'},
    '/api/items/{account_id}/{item_id}/edit': {'POST'},
    '/api/items/{account_id}/{item_id}/delist': {'POST'},
    '/api/items/{account_id}/{item_id}/relist': {'POST'},
    '/api/items/{account_id}/batch-edit': {'POST'},
    '/api/items/{account_id}/batch-publish': {'POST'},
    '/api/items/{account_id}/operation-logs': {'GET'},
    '/api/chats/conversations': {'GET'},
    '/api/chats/incoming': {'POST'},
    '/api/chats/sync': {'POST'},
    '/api/chats/{conversation_id}/messages': {'GET'},
    '/api/chats/{conversation_id}/send': {'POST'},
    '/api/chats/{conversation_id}/mode': {'POST'},
    '/api/chats/{conversation_id}/ai-draft': {'POST'},
    '/api/agents/roles': {'GET'},
    '/api/agents/roles/{role}': {'PUT'},
    '/api/agents/prompts': {'GET', 'POST'},
    '/api/agents/prompts/{prompt_id}': {'PUT', 'DELETE'},
    '/api/agents/master/chat': {'POST'},
    '/api/agents/runs': {'GET'},
    '/api/agents/messages': {'GET'},
    '/api/providers': {'GET', 'POST'},
    '/api/providers/{provider_id}': {'PUT', 'DELETE'},
    '/api/providers/{provider_id}/test': {'POST'},
    '/api/providers/{provider_id}/default': {'POST'},
    '/api/orders': {'GET'},
    '/api/orders/sync': {'POST'},
    '/api/orders/{order_id}/deliver': {'POST'},
    '/api/orders/{order_id}/logistics': {'GET'},
    '/api/risk/status': {'GET'},
    '/api/risk/{account_id}/clear': {'POST'},
    '/api/settings': {'GET', 'PUT'},
    '/api/ws': {'WS'},
}


def _registered() -> dict[str, set[str]]:
    """真实注册的路由表。

    不能只看 `app.openapi()`：WebSocket 路由（`/api/ws`）不进 OpenAPI，
    但它确实是契约的一部分，所以要顺带遍历 `app.routes`。
    """
    app = create_app()
    out: dict[str, set[str]] = {}
    for path, spec in app.openapi().get('paths', {}).items():
        out.setdefault(path, set()).update(m.upper() for m in spec if m in {'get', 'post', 'put', 'delete', 'patch'})

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, 'path', None)
            if path:
                methods = {m.upper() for m in (getattr(route, 'methods', None) or [])}
                if not methods and 'WebSocket' in type(route).__name__:
                    methods = {'WS'}
                out.setdefault(path, set()).update(methods)
            nested = getattr(route, 'routes', None)
            if nested is None:
                original = getattr(route, 'original_router', None)
                nested = getattr(original, 'routes', None)
            if nested:
                walk(nested)

    walk(app.routes)
    return out


class ContractRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registered = _registered()

    def test_all_contract_paths_registered(self):
        missing = sorted(set(CONTRACT_ROUTES) - set(self.registered))
        self.assertEqual(missing, [], f'契约里有但没实现的路由: {missing}')

    def test_contract_methods_present(self):
        problems = []
        for path, methods in CONTRACT_ROUTES.items():
            have = self.registered.get(path, set())
            lack = sorted(methods - have)
            if lack:
                problems.append(f'{path} 缺 {lack}（现有 {sorted(have)}）')
        self.assertEqual(problems, [], '方法不齐: ' + '; '.join(problems))

    def test_frontend_calls_match_backend(self):
        """前端 src 里出现的 `/api/...` 字面量（去掉模板变量后）必须在后端存在。

        这条是为了抓住"前端调 /relogin、后端只实现 /toggle"这类漂移。
        """
        import re

        src = pathlib.Path(__file__).resolve().parents[2] / 'frontend' / 'src'
        if not src.exists():
            self.skipTest('前端源码不存在')
        pattern = re.compile(r'`?/api/[A-Za-z0-9_\-/]*')
        unknown: set[str] = set()
        for tsx in src.rglob('*.ts*'):
            text = tsx.read_text(encoding='utf-8', errors='replace')
            for raw in pattern.findall(text):
                # 把 ${...} 模板变量归一成契约里的占位名不好做，直接看"前缀是否被任何已注册路径覆盖"
                cleaned = re.sub(r'\$\{[^}]*\}', 'X', raw.rstrip('`')).rstrip('/')
                if not cleaned.startswith('/api/'):
                    continue
                if self._matches_any(cleaned):
                    continue
                unknown.add(cleaned)
        self.assertEqual(sorted(unknown), [], f'前端调用了后端不存在的接口: {sorted(unknown)}')

    def _matches_any(self, called: str) -> bool:
        # 只是某个已注册路径的前缀（例如 `/api/auth`、`/api/ws` 这种拼接前缀）不算缺失
        if any(path.startswith(called + '/') for path in self.registered):
            return True
        called_parts = [p for p in called.split('/') if p]
        for path in self.registered:
            path_parts = [p for p in path.split('/') if p]
            if len(path_parts) != len(called_parts):
                continue
            ok = True
            for a, b in zip(called_parts, path_parts):
                if b.startswith('{') and b.endswith('}'):
                    continue
                if a == 'X' or a == b:
                    continue
                ok = False
                break
            if ok:
                return True
        return False


if __name__ == '__main__':
    unittest.main(verbosity=2)
