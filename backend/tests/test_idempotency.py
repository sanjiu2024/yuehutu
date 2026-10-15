# -*- coding: utf-8 -*-
"""写操作幂等键（`Idempotency-Key`，§10-11）的契约测试。

这一批加的机制只有一条主线：**同一个键 + 同一个请求只打平台一次**。用例逐条钉住它：

1. 同键同载荷 ⇒ 第二次**回放**上次响应（带 `idempotent_replay`），平台调用数仍是 1、
   `operation_logs` 也只有 1 行；
2. 同键不同载荷 ⇒ `conflict`（409），**不打平台、不覆盖原记录**；
3. 不同键 ⇒ 各打一次（幂等键不是限流）；
4. 不带键 ⇒ 行为与加这个机制之前完全一致；
5. **失败的响应也记**：平台已生效但响应丢了时，同键重发不许再打一次；
6. 闸门拒绝（没 `confirm`）**不占用键**：补上确认后同一个键还能正常提交；
7. 中间件真的挂在 `main.create_app()` 上（不是只在服务层躺着）；
8. 指纹对载荷敏感、对字典键序不敏感；过期键会被顺手清掉。

跑法（仓库根目录）：
    PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_idempotency.py

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core import db, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import common as svc_common  # noqa: E402
from backend.app.xianyu import trade_write as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'idem_acc'
PATH = f'/api/trade/{ACC}/orders/O1/ship-dummy'
HEADER = 'Idempotency-Key'

#: 假响应：无需物流发货（形状同 `test_trade_write_api.py`，不是真机原文）
SHIP_DUMMY_OK: Dict[str, Any] = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
                                 'ret': ['SUCCESS::调用成功']}

#: 假响应：平台级失败（顶层 success=false ⇒ 服务层翻成 protocol_error / 502）
PLATFORM_FAIL: Dict[str, Any] = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}


class FakeTradeClient:
    """假协议层：只实现 `mtop`，记录每次调用的 (api, data, v)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None) -> None:
        self.payloads = payloads or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0', **kwargs: Any) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v, 'kw': kwargs})
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有准备 {api} 的响应')
        return self.payloads[api]

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


def install_fake_protocol(client: FakeTradeClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeTradeClient:
        client.account_id = account_id
        return client

    module.get_client = factory  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC) -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, 'cookie-ok', 'active'))


class IdempotencyTest(unittest.TestCase):
    """走**真 app**（`main.create_app()`）：中间件挂在 main 上，自建 FastAPI 验不到。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fake_protocol()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'idempotency_keys'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeTradeClient({proto.SHIP_DUMMY_API: SHIP_DUMMY_OK})
        install_fake_protocol(self.fake)
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('idem','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def post(self, key: Optional[str] = None, body: Optional[Dict[str, Any]] = None,
             path: str = PATH):
        headers = dict(self.headers)
        if key is not None:
            headers[HEADER] = key
        return self.client.post(path, json=body if body is not None else {'confirm': True},
                                headers=headers)

    def logs(self) -> List[Any]:
        return db.query('SELECT action, success FROM operation_logs WHERE account_id=?', (ACC,))

    # -- 用例 ------------------------------------------------------------

    def test_same_key_same_body_calls_platform_once(self):
        first = self.post('k-1', {'trade_text': '已发货', 'confirm': True})
        second = self.post('k-1', {'trade_text': '已发货', 'confirm': True})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.fake.calls), 1, self.fake.apis())
        replay = second.json()['data']
        self.assertTrue(replay.get('idempotent_replay'), replay)
        self.assertEqual(replay.get('api'), proto.SHIP_DUMMY_API)
        self.assertEqual(replay.get('account_id'), ACC)
        # 回放不写第二条审计：审计行是「我们打过一次平台」的记录
        self.assertEqual(len(self.logs()), 1)

    def test_same_key_different_body_is_conflict(self):
        self.post('k-2', {'trade_text': 'A', 'confirm': True})
        resp = self.post('k-2', {'trade_text': 'B', 'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        self.assertEqual(payload.get('code'), 'conflict', payload)
        self.assertEqual(len(self.fake.calls), 1, self.fake.apis())
        row = db.query_one('SELECT response FROM idempotency_keys WHERE key=?', ('k-2',))
        self.assertIn(proto.SHIP_DUMMY_API, row['response'])

    def test_same_key_other_account_is_conflict(self):
        other = 'idem_acc_2'
        seed_account(other)
        self.post('k-3', {'trade_text': 'A', 'confirm': True})
        resp = self.post('k-3', {'trade_text': 'A', 'confirm': True},
                         path=f'/api/trade/{other}/orders/O1/ship-dummy')
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(len(self.fake.calls), 1, self.fake.apis())

    def test_different_keys_call_platform_twice(self):
        self.post('k-4a', {'trade_text': 'A', 'confirm': True})
        second = self.post('k-4b', {'trade_text': 'A', 'confirm': True})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.fake.calls), 2, self.fake.apis())
        self.assertFalse(second.json()['data'].get('idempotent_replay'))
        self.assertEqual(len(self.logs()), 2)

    def test_without_key_behaviour_is_unchanged(self):
        self.post(None, {'trade_text': 'A', 'confirm': True})
        second = self.post(None, {'trade_text': 'A', 'confirm': True})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.fake.calls), 2, self.fake.apis())
        self.assertNotIn('idempotent_replay', second.json()['data'])
        self.assertEqual(db.query('SELECT key FROM idempotency_keys'), [])

    def test_failed_response_is_remembered_and_not_retried(self):
        self.fake.payloads[proto.SHIP_DUMMY_API] = PLATFORM_FAIL
        first = self.post('k-5', {'trade_text': 'A', 'confirm': True})
        second = self.post('k-5', {'trade_text': 'A', 'confirm': True})
        self.assertEqual(first.status_code, 502, first.text)
        self.assertEqual(second.status_code, 502, second.text)
        self.assertTrue(second.json().get('idempotent_replay'), second.json())
        # 平台可能已经生效、只是响应丢了：同键重发**不许**再打一次
        self.assertEqual(len(self.fake.calls), 1, self.fake.apis())
        self.assertEqual(len(self.logs()), 1)

    def test_gate_failure_does_not_consume_the_key(self):
        blocked = self.post('k-6', {'trade_text': 'A'})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(db.query('SELECT key FROM idempotency_keys'), [])
        retry = self.post('k-6', {'trade_text': 'A', 'confirm': True})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(len(self.fake.calls), 1, self.fake.apis())

    def test_middleware_is_registered_on_the_real_app(self):
        names = [m.cls.__name__ for m in self.app_ctx.app.user_middleware]
        self.assertIn('IdempotencyKeyMiddleware', names)

    def test_header_is_case_insensitive(self):
        headers = dict(self.headers)
        headers['idempotency-key'] = 'k-7'
        body = {'trade_text': 'A', 'confirm': True}
        self.client.post(PATH, json=body, headers=headers)
        self.client.post(PATH, json=body, headers={**self.headers, 'IDEMPOTENCY-KEY': 'k-7'})
        self.assertEqual(len(self.fake.calls), 1, self.fake.apis())

    def test_shared_helper_replays_without_calling_again(self):
        """各写族接闸门的公共入口 `common.idempotent_call` 自己的语义。"""
        calls: List[int] = []

        def call() -> Dict[str, Any]:
            calls.append(1)
            return {'success': True, 'n': len(calls)}

        svc_common.set_idempotency_key('helper-1')
        try:
            first = svc_common.idempotent_call(ACC, 'close_order', 'O1', {'a': 1}, call)
            second = svc_common.idempotent_call(ACC, 'close_order', 'O1', {'a': 1}, call)
        finally:
            svc_common.set_idempotency_key('')
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, {'success': True, 'n': 1})
        self.assertTrue(second.get('idempotent_replay'), second)
        self.assertEqual(second.get('n'), 1)

    def test_expired_keys_are_purged(self):
        db.execute(
            "INSERT INTO idempotency_keys(key, account_id, action, fingerprint, response, created_at) "
            "VALUES('old-key', ?, 'ship_dummy', 'x', '{}', datetime('now', '-30 days'))",
            (ACC,))
        self.post('k-8', {'trade_text': 'A', 'confirm': True})
        keys = [row['key'] for row in db.query('SELECT key FROM idempotency_keys')]
        self.assertNotIn('old-key', keys)
        self.assertIn('k-8', keys)


class FingerprintTest(unittest.TestCase):
    """指纹是纯函数：键序无关、载荷敏感（不用起 app）。"""

    def test_key_order_does_not_matter(self):
        left = svc_common.idempotency_fingerprint('a', 'ship_dummy', 'O1', {'x': 1, 'y': 2})
        right = svc_common.idempotency_fingerprint('a', 'ship_dummy', 'O1', {'y': 2, 'x': 1})
        self.assertEqual(left, right)

    def test_payload_account_action_item_all_matter(self):
        base = svc_common.idempotency_fingerprint('a', 'ship_dummy', 'O1', {'x': 1})
        self.assertNotEqual(base, svc_common.idempotency_fingerprint('a', 'ship_dummy', 'O1', {'x': 2}))
        self.assertNotEqual(base, svc_common.idempotency_fingerprint('b', 'ship_dummy', 'O1', {'x': 1}))
        self.assertNotEqual(base, svc_common.idempotency_fingerprint('a', 'close_order', 'O1', {'x': 1}))
        self.assertNotEqual(base, svc_common.idempotency_fingerprint('a', 'ship_dummy', 'O2', {'x': 1}))

    def test_contextvar_defaults_to_empty(self):
        self.assertEqual(svc_common.current_idempotency_key(), '')
        svc_common.set_idempotency_key('k')
        self.assertEqual(svc_common.current_idempotency_key(), 'k')
        svc_common.set_idempotency_key(None)
        self.assertEqual(svc_common.current_idempotency_key(), '')


if __name__ == '__main__':
    unittest.main()
