"""WebSocket 事件投递测试。

**为什么需要这个文件**：`/api/ws` 是前端的实时刷新通道，之前只在
`scripts/smoke_write.py` 里被真实验证过一次，`backend/tests/` 里**一条 WS 用例都没有**
（契约闸门只验路径注册）。结果就是「账号 PATCH 不发事件」这种缺陷单测全绿、只有真实
WS 冒烟才发现 —— 前端点了开关却看不到刷新，且没有任何测试会转红。

覆盖三件事：
1. 无 token / 错 token 的连接必须被拒（4401），不接受匿名订阅。
2. `events.publish()` 的事件能真的推到已连上的 WS 客户端。
3. **回归**：`PATCH /api/accounts/{id}` 必须发 `accounts.changed`（真实走完整 app）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_ws_events.py"

不打真实闲鱼平台、不调真实 LLM。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from backend.app.core import db, events, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402


class _Publisher(threading.Thread):
    """后台反复发同一个 topic，直到主线程拿到事件。

    为什么要反复发：`websocket_connect` 返回时只保证握手完成，服务端的
    `events.subscribe()` 在 accept 之后才执行 —— 只发一次可能发在订阅之前，
    测试会偶发失败（flaky）。这里用「持续发 + 主线程阻塞收」把竞态消掉。
    """

    def __init__(self, topic: str, payload: dict):
        super().__init__(daemon=True)
        self.topic = topic
        self.payload = payload
        # 注意别叫 `_stop` —— 那是 `threading.Thread._stop` 的方法名，覆盖掉之后
        # `join()` 内部调 `self._stop()` 会抛 `TypeError: 'Event' object is not callable`。
        self._done = threading.Event()

    def run(self) -> None:
        while not self._done.is_set():
            events.publish(self.topic, self.payload)
            self._done.wait(0.02)

    def stop(self) -> None:
        self._done.set()
        self.join(timeout=2)


class WsEventTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from backend.tests.testenv import use_temp_app_dir

        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        # `with` 才会执行 lifespan —— `events.bind_loop()` 在那里调用；
        # 不 bind loop 时 publish() 会静默丢弃（`if not queues or _loop is None: return`）。
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('ws','x',1)"))
        self.token = security.issue_token(self.user_id)

    def tearDown(self):
        db.execute('DELETE FROM users')
        db.execute('DELETE FROM accounts')
        self.app_ctx.__exit__(None, None, None)

    # ------------------------------------------------------------ 鉴权

    def test_missing_token_is_rejected(self):
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect('/api/ws'):
                pass
        self.assertEqual(ctx.exception.code, 4401)

    def test_bogus_token_is_rejected(self):
        # 不能因为「token 非空」就放进去 —— verify_token 必须真校验。
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect('/api/ws?token=not-a-real-token'):
                pass
        self.assertEqual(ctx.exception.code, 4401)

    # ------------------------------------------------------------ 事件投递

    def test_published_event_reaches_client(self):
        publisher = _Publisher('unit.probe', {'n': 7})
        try:
            with self.client.websocket_connect(f'/api/ws?token={self.token}') as ws:
                publisher.start()
                event = ws.receive_json()
        finally:
            publisher.stop()

        self.assertEqual(event['topic'], 'unit.probe')
        self.assertEqual(event['payload'], {'n': 7})
        self.assertIn('ts', event)

    # ------------------------------------------------------------ 回归：账号变更必须发事件

    def test_patch_account_publishes_event(self):
        """回归 `fix: 账号 PATCH/toggle/删除不发事件`。

        这个用例在修复前会 `receive_json()` 一直阻塞到超时/断开 —— 真实冒烟的
        表现是「WS 连上 6 秒收不到事件」。
        """
        db.execute(
            "INSERT INTO accounts(id, nickname, enabled, auto_reply, created_at, updated_at) "
            "VALUES('ws_acc','测试号',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")

        got: list = []
        found = threading.Event()

        def read_until_found(ws) -> None:
            # 只认 `accounts.changed`，其余（探针残留事件）跳过 —— 否则 `got[0]`
            # 可能是探针的 `ws.ready`，用例会偶发失败。
            while not found.is_set():
                event = ws.receive_json()
                if event.get('topic') == 'accounts.changed':
                    got.append(event)
                    found.set()

        with self.client.websocket_connect(f'/api/ws?token={self.token}') as ws:
            # 先确认订阅已建立，再打 PATCH —— 否则事件可能发在订阅之前。
            probe = _Publisher('ws.ready', {})
            probe.start()
            try:
                while True:
                    if ws.receive_json()['topic'] == 'ws.ready':
                        break
            finally:
                probe.stop()

            resp = self.client.patch('/api/accounts/ws_acc', headers={
                'Authorization': f'Bearer {self.token}'}, json={'remark': '改了备注'})
            self.assertEqual(resp.status_code, 200, resp.text)

            reader = threading.Thread(target=read_until_found, args=(ws,), daemon=True)
            reader.start()
            found.wait(timeout=5)

        self.assertTrue(got, 'PATCH 账号后台 5 秒内没有收到 accounts.changed 事件')
        event = got[0]
        self.assertEqual(event['payload']['account_id'], 'ws_acc')
        self.assertEqual(event['payload']['patch'], {'remark': '改了备注'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
