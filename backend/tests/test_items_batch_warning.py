"""批量改价/上下架的「假成功」回归锁 + 跨账号批改必须失败（task-8）。

**为什么需要**：
1. `services/items.py` 的 `update_item` / `_set_status` 都会算出 `warning`（协议层没实现、
   没就绪、或调用失败 → 「本地改了但没推到闲鱼」），但旧的 `batch_update` 只
   `ok += 1`、summary 里根本没有 warning 这一项 → `POST /api/items/{account_id}/batch-edit`
   与上下架接口**恒报 ok=N**：前端弹「已批量修改 N 个商品」、MCP 的 `items_batch_edit`
   也按成功处理，而闲鱼上其实一个都没改。现在口径是
   `ok / synced` = 本地已改 **且** 已推到平台，`warnings / local_only` = 本地改了没推出去。
2. `POST /api/items/{account_id}/batch-edit` 原先**完全忽略路径里的 account_id**，
   拿 A 账号的路径能改 B 账号的商品（顺带绕过了账号维度的日志/事件归属）。
   现在按 `(account_id, item_id)` 配对校验，属于别人的逐条进 `failed`
   （`code=account_mismatch`），**不整批 400** —— MCP 与前端都靠逐条结果。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_items_batch_warning.py"

**不打真实闲鱼平台、不调真实 LLM**：协议层一律用假模块注入。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core import db, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import items  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'batch_acc'
OTHER = 'other_acc'


class FakeClient:
    """假协议层客户端：只记调用，不联网。

    **签名与 `backend/app/xianyu/client.py::XianyuClient` 逐字一致**：真类是
    `edit_item(item_id, payload)` / `downshelf(item_id)` / `reshelf(item_id)`，
    都没有 `account_id` 参数（账号身份由 client 自己的 cookie 决定）。
    顺着服务层的错误调用签成 `(account_id, item_id, ...)` 会让假对象比真对象宽松，
    测试挡住「多传一个参数」这类真 bug（真机上是 502 TypeError）。
    签名对拍在 `backend/tests/test_services_items.py::FakeClientSignatureTest`。
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.calls = []

    def edit_item(self, item_id: str, payload: dict):
        self.calls.append(('edit_item', item_id, payload))
        return {'ok': True}

    def downshelf(self, item_id: str):
        self.calls.append(('downshelf', item_id))
        return {'ok': True}

    def reshelf(self, item_id: str):
        self.calls.append(('reshelf', item_id))
        return {'ok': True}


def install_fake_protocol(client: FakeClient) -> types.ModuleType:
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    module.XianyuClient = FakeClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed(item_id: str, account: str = ACC, price: float = 100.0, status: str = 'on_sale') -> None:
    db.execute(
        'INSERT INTO items(account_id, item_id, title, price, stock, status) VALUES(?,?,?,?,?,?)',
        (account, item_id, f'商品 {item_id}', price, 1, status),
    )


def price_of(account: str, item_id: str) -> float:
    row = db.query_one(
        'SELECT price FROM items WHERE account_id = ? AND item_id = ?', (account, item_id)
    )
    return float(row['price'])


def status_of(account: str, item_id: str) -> str:
    row = db.query_one(
        'SELECT status FROM items WHERE account_id = ? AND item_id = ?', (account, item_id)
    )
    return str(row['status'])


class BatchWarningServiceTest(unittest.TestCase):
    """服务层口径：warning 不等于 ok。"""

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
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM operation_logs')
        self.client = FakeClient(ACC)

    def test_batch_update_not_pushed_is_not_ok(self):
        """协议层没就绪 → ok 必须是 0，改动记在 warnings / local_only 里。"""
        seed('A1')
        seed('A2')
        result = items.batch_update(['A1', 'A2'], {'price_set': 9.9})

        self.assertEqual(result['total'], 2)
        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['local_only'], 2)
        self.assertEqual([w['id'] for w in result['warnings']], ['A1', 'A2'])
        self.assertEqual(result['warnings'][0]['code'], 'not_pushed')
        self.assertEqual(result['failed'], [])
        # 本地确实改了 —— warning 是「没推到闲鱼」，不是「没改」
        self.assertEqual(price_of(ACC, 'A1'), 9.9)
        self.assertEqual(price_of(ACC, 'A2'), 9.9)

    def test_batch_update_push_success_is_ok(self):
        """反向对照：协议层真的推成功了，ok 才等于条数。"""
        seed('A1')
        seed('A2')
        install_fake_protocol(self.client)
        result = items.batch_update(['A1', 'A2'], {'price_set': 9.9})

        self.assertEqual(result['ok'], 2)
        self.assertEqual(result['local_only'], 0)
        self.assertEqual(result['warnings'], [])
        self.assertEqual([c[0] for c in self.client.calls], ['edit_item', 'edit_item'])

    def test_downshelf_not_pushed_is_not_ok(self):
        seed('A1')
        seed('A2')
        result = items.downshelf(['A1', 'A2'])

        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['local_only'], 2)
        self.assertEqual(result['status'], 'off_shelf')
        self.assertEqual(status_of(ACC, 'A1'), 'off_shelf')

        install_fake_protocol(self.client)
        result = items.reshelf(['A1'])
        self.assertEqual(result['ok'], 1)
        self.assertEqual(result['warnings'], [])

    def test_batch_update_cross_account_must_fail(self):
        """拿 ACC 的 account_id 改 OTHER 账号的商品 → 必须失败，且不能改到别人的数据。"""
        seed('A1', account=ACC, price=100.0)
        seed('B1', account=OTHER, price=555.0)
        result = items.batch_update(['A1', 'B1'], {'price_set': 9.9}, account_id=ACC)

        self.assertEqual(result['total'], 2)
        self.assertEqual(len(result['failed']), 1)
        self.assertEqual(result['failed'][0]['id'], 'B1')
        self.assertEqual(result['failed'][0]['code'], 'account_mismatch')
        self.assertEqual(result['local_only'], 1)
        self.assertEqual([w['id'] for w in result['warnings']], ['A1'])
        # 别人的商品一个字节都没动
        self.assertEqual(price_of(OTHER, 'B1'), 555.0)
        self.assertEqual(price_of(ACC, 'A1'), 9.9)

    def test_downshelf_cross_account_must_fail(self):
        seed('B1', account=OTHER, status='on_sale')
        result = items.downshelf(['B1'], account_id=ACC)

        self.assertEqual(result['ok'], 0)
        self.assertEqual(len(result['failed']), 1)
        self.assertEqual(result['failed'][0]['code'], 'account_mismatch')
        self.assertEqual(status_of(OTHER, 'B1'), 'on_sale')


class BatchEditApiTest(unittest.TestCase):
    """HTTP 层：批量接口返回 200 + 逐条结果，但 body 里必须能看出「一条都没推出去」。"""

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
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM operation_logs')
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('batch','x',1)"))
        self.token = security.issue_token(self.user_id)
        self.headers = {'Authorization': 'Bearer ' + self.token}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    def test_batch_edit_not_ready_reports_local_only(self):
        """协议层没就绪：HTTP 仍是 200（逐条语义），但 ok 必须为 0、local_only 为 2。"""
        seed('A1')
        seed('A2')
        resp = self.client.post(
            f'/api/items/{ACC}/batch-edit',
            json={'item_ids': ['A1', 'A2'], 'patch': {'price_set': 9.9}},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], resp.text)
        data = body['data']
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['ok'], 0)          # 旧实现这里是 2 —— 正是本次修的假成功
        self.assertEqual(data['local_only'], 2)
        self.assertEqual(len(data['warnings']), 2)
        self.assertEqual(data['failed'], [])
        self.assertEqual(price_of(ACC, 'A1'), 9.9)

    def test_batch_edit_cross_account_item_goes_to_failed(self):
        seed('A1', account=ACC, price=100.0)
        seed('B1', account=OTHER, price=555.0)
        resp = self.client.post(
            f'/api/items/{ACC}/batch-edit',
            json={'item_ids': ['A1', 'B1'], 'patch': {'price_set': 9.9}},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['total'], 2)
        self.assertEqual([f['id'] for f in data['failed']], ['B1'])
        self.assertEqual(data['failed'][0]['code'], 'account_mismatch')
        # 跨账号那条完全没改：别人的价格原封不动
        self.assertEqual(price_of(OTHER, 'B1'), 555.0)

    def test_delist_cross_account_item_goes_to_failed(self):
        seed('B1', account=OTHER, status='on_sale')
        resp = self.client.post(f'/api/items/{ACC}/B1/delist', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['failed'][0]['code'], 'account_mismatch')
        self.assertEqual(status_of(OTHER, 'B1'), 'on_sale')

    def test_batch_edit_unknown_item_is_not_found(self):
        seed('A1')
        resp = self.client.post(
            f'/api/items/{ACC}/batch-edit',
            json={'item_ids': ['A1', 'NOPE'], 'patch': {'price_set': 9.9}},
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual([f['code'] for f in data['failed']], ['not_found'])
        self.assertEqual(data['local_only'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
