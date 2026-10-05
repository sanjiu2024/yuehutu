"""IM 入站闭环回归：**买家帧 -> 落库 -> AI 回复 -> 真的发回闲鱼**。

**为什么需要这个文件**：`services.chats.attach_im_session()` 在本次改动之前**没有任何
生产调用方**。协议层 `xianyu.client._ensure_im()` 建好 `ImSession` 后直接把会话返回，
没有注册入站 handler —— 买家真的说话时，帧被投递到一个空 handler 列表上，AI 永远不
开口；就算开口了，回复也只停在库里（`messages.sent_ok=0`），没有代码经协议层
`send_message` 发回去。本文件锁这条链路的四件事：

1. **接线**：`common.get_client()` -> `im_service.bind()` -> 客户端建会话时回调钩子 ->
   `chats.attach_im_session()`，handler 真的挂上去了（不经 HTTP 也能收信）。
2. **投递**：AI 回复经协议层 `send_message` 发出，`messages.sent_ok` 如实回写为 1。
3. **不该发的不发，且必须留痕**：`mode != 'auto'`（人工接管）/ 缺 cid / 风控冷却 /
   协议层报错，一律写 `operation_logs`（必要时写 `risk_events` 与
   `messages.meta.send_error`），不许静默。
4. **认不出的入站帧**：留一条 WARNING（原始 type + 截断 body），不抛异常、不造会话。

**不打真实闲鱼平台**（假协议客户端，只在服务层调用的两个口子 `ensure_im` /
`send_message` 上打桩）、**不调真实 LLM**（`llm.chat` 全程 mock）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_im_loop.py"
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from loguru import logger  # noqa: E402

from backend.app.agents import llm, roles  # noqa: E402
from backend.app.core import db  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import chats, common, im_service  # noqa: E402
from backend.app.xianyu import im, risk  # noqa: E402

ACCOUNT_ID = 'im_loop_acc'
BUYER_ID = 'buyer_loop'
ITEM_ID = 'item_loop'
CID = 'conv-loop-1'
CLIENT_MODULE = 'backend.app.xianyu.client'


# --------------------------------------------------------------------------- 测试替身


def _fake_chat(text_by_role=None, default='（mock 回复）', calls=None):
    """假的 `llm.chat`：按 role 返回文本并记录调用。"""
    text_by_role = text_by_role or {}

    async def _chat(messages, *, role=None, **kwargs):
        if calls is not None:
            calls.append({'role': role, 'messages': messages, 'kwargs': kwargs})
        text = text_by_role.get(role, f'{default}[{role}]')
        return {'text': text, 'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                                       'total_tokens': 2},
                'model': 'mock-model', 'provider_id': 1, 'tool_calls': [], 'raw': {}}

    return _chat


def _push_frame(msg_id='m-1', content='这个还在吗？', cid=CID, buyer_id=BUYER_ID,
                item_id=ITEM_ID):
    """造一帧像线上那样嵌套的 IM 买家消息推送（无 mid = 平台主动推送）。"""
    return {
        'headers': {'mid': ''},
        'body': {
            'message': {
                'msgId': msg_id,
                'cid': f'{cid}@goofish',
                'senderId': f'{buyer_id}@goofish',
                'content': {'text': {'text': content}},
                'extension': {'itemId': item_id},
            }
        },
    }


class FakeProtocolClient:
    """假协议客户端：只实现服务层用到的两个口子。

    `ensure_im()` 会先逐个触发 `on_im_session` 钩子再返回会话 —— 与真
    `XianyuClient._ensure_im()` 的行为一致（建好会话就通知钩子）。
    """

    def __init__(self, session=None, fail=None):
        self.session = session or im.ImSession('cookie', 'token', 'seller_1')
        self.fail = fail          # 设了就抛这个异常，模拟协议层发送失败
        self.sent = []            # [(cid, text)]
        self.hooks = []

    def on_im_session(self, hook):
        if hook not in self.hooks:
            self.hooks.append(hook)
        return hook

    def ensure_im(self):
        for hook in list(self.hooks):
            hook(self.session)
        return self.session

    def send_message(self, cid, text):
        if self.fail is not None:
            raise self.fail
        self.sent.append((cid, text))
        return {'ok': True}


class FakeRuntime:
    """假编排层：真实签名（async，只生成不发送）。"""

    def __init__(self):
        self.calls = []

    async def reply_for_message(self, conversation_id, message_id):
        self.calls.append((conversation_id, message_id))
        return {'ok': True, 'reply': '在的，可以拍下【假编排层】', 'role': 'item'}


def install_client(client):
    """把假协议客户端模块塞进 sys.modules —— `common.get_client` 会优先用它的工厂。"""
    module = types.ModuleType(CLIENT_MODULE)
    module.get_client = lambda account_id: client
    sys.modules[CLIENT_MODULE] = module
    return module


def clear_client_module():
    sys.modules.pop(CLIENT_MODULE, None)


class _TempAppMixin:
    """临时数据目录 + 干净的 conversations/messages/日志表（测试规则要求用 testenv）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from backend.tests.testenv import use_temp_app_dir

        use_temp_app_dir(cls.tmp.name)
        roles.ensure_roles()
        db.execute('DELETE FROM providers')
        db.execute("INSERT INTO providers(id, name, base_url, api_key, default_model) "
                   "VALUES(1, 'mock', 'http://127.0.0.1:9/v1', 'sk-mock', 'mock-model')")

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')
        db.execute('DELETE FROM agent_messages')
        db.execute('DELETE FROM operation_logs')
        db.execute('DELETE FROM risk_events')
        clear_client_module()
        im_service.forget(ACCOUNT_ID)
        risk.registry.reset(ACCOUNT_ID)

    def tearDown(self):
        clear_client_module()
        im_service.forget(ACCOUNT_ID)
        risk.registry.reset(ACCOUNT_ID)

    def _conversation(self, mode='auto'):
        return chats.ensure_conversation(
            ACCOUNT_ID, BUYER_ID, '买家小王', ITEM_ID, mode=mode, agent_role='item',
        )

    def _last_out_message(self):
        return db.query_one("SELECT * FROM messages WHERE direction = 'out' ORDER BY id DESC")


# --------------------------------------------------------------------------- 1. 接线


class ImWiringTest(_TempAppMixin, unittest.TestCase):
    """缺陷 A 的回归锁：不经 HTTP，IM 帧自己走到 AI，并且回复真的发出去。"""

    def test_client_hook_wires_handler_and_reply_is_sent(self):
        """`get_client` 挂钩子 -> 建会话时注入 handler -> 帧 -> AI -> 真的发回。"""
        client = FakeProtocolClient()
        install_client(client)

        got, err = common.get_client(ACCOUNT_ID)
        self.assertIs(got, client)
        self.assertIsNone(err)

        session = client.ensure_im()      # 协议层建会话 -> 钩子 -> attach_im_session
        self.assertEqual(len(session._handlers), 1)

        with mock.patch.object(llm, 'chat', _fake_chat({'item': '在的，现货【IM】'})):
            delivered = session.dispatch(_push_frame(msg_id='im-m-1'))

        self.assertEqual(delivered, 1)
        self.assertEqual(client.sent, [(CID, '在的，现货【IM】')])

        row = self._last_out_message()
        self.assertIsNotNone(row)
        self.assertEqual(int(row['sent_ok']), 1)
        self.assertNotIn('send_error', json.loads(row['meta'] or '{}'))
        self.assertEqual(row['direction'], 'out')

        log = db.query_one(
            "SELECT * FROM operation_logs WHERE action = 'ai_reply_send' ORDER BY id DESC")
        self.assertIsNotNone(log)
        self.assertEqual(int(log['success']), 1)

    def test_start_is_idempotent_and_one_frame_triggers_one_ai(self):
        """`im_service.start()` 重复调用不会重复挂 handler；一封来信只触发一次 AI。"""
        client = FakeProtocolClient()
        install_client(client)

        first = im_service.start(ACCOUNT_ID)
        second = im_service.start(ACCOUNT_ID)
        self.assertTrue(first['attached'], first)
        self.assertTrue(second['attached'], second)

        session = im_service.current_session(ACCOUNT_ID)
        self.assertIs(session, client.session)
        self.assertEqual(len(session._handlers), 1)

        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat({'item': '只回一次'}, calls=calls)):
            session.dispatch(_push_frame(msg_id='im-m-2'))

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(client.sent), 1)

    def test_attach_im_session_twice_keeps_single_handler(self):
        session = im.ImSession('cookie', 'token', 'seller_1')
        chats.attach_im_session(session, ACCOUNT_ID)
        chats.attach_im_session(session, ACCOUNT_ID)
        self.assertEqual(len(session._handlers), 1)

    def test_same_platform_msg_id_dispatched_twice_sends_once(self):
        """同一 msg_id 重复送达：只回一次、只发一次（幂等贯穿到投递）。"""
        client = FakeProtocolClient()
        install_client(client)
        im_service.start(ACCOUNT_ID)
        session = im_service.current_session(ACCOUNT_ID)

        frame = _push_frame(msg_id='im-m-dup')
        with mock.patch.object(llm, 'chat', _fake_chat({'item': '只回一次'})):
            session.dispatch(frame)
            session.dispatch(frame)

        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(db.query("SELECT * FROM messages WHERE direction = 'out'")), 1)

    def test_start_without_cookie_returns_not_ready_envelope(self):
        """没有 Cookie 的账号：返回 not_ready 信封，不是抛异常、也不是假成功。"""
        result = im_service.start('acc_without_cookie')
        self.assertTrue(common.is_failure(result), result)
        self.assertEqual(result['code'], 'xianyu_not_ready')
        self.assertIsNone(im_service.current_session('acc_without_cookie'))


# --------------------------------------------------------------------------- 2. 投递闸门


class AiReplyDeliveryTest(_TempAppMixin, unittest.TestCase):
    """不该发的三种情况都不许静默：模式 / 缺 cid / 风控冷却；报错也不许往上抛。"""

    def test_manual_mode_never_sends_and_records_reason(self):
        client = FakeProtocolClient()
        install_client(client)
        conversation = self._conversation(mode='manual')

        incoming = chats.handle_incoming(
            conversation['id'], '在吗', msg_id='m-manual',
            meta={'source': 'im', 'cid': CID},
        )
        self.assertIsNone(incoming['ai_reply'])
        self.assertIsNone(incoming['ai_send'])
        self.assertEqual(client.sent, [])

        draft = chats.append_message(
            conversation['id'], 'ai:item', '草稿', ai_generated=True, sent_ok=False)
        result = chats.deliver_ai_reply(conversation['id'], draft['id'], '草稿', cid=CID)
        self.assertFalse(result['sent'])
        self.assertEqual(result['code'], 'mode_not_auto')
        self.assertEqual(client.sent, [])

        row = db.query_one('SELECT * FROM messages WHERE id = ?', (draft['id'],))
        self.assertEqual(int(row['sent_ok']), 0)
        self.assertIn('manual', json.loads(row['meta'])['send_error'])

    def test_missing_cid_records_failure_instead_of_silently_dropping(self):
        """HTTP 手动灌进来的来信没有 cid：AI 照样生成，但发送失败要留痕。"""
        client = FakeProtocolClient()
        install_client(client)
        conversation = self._conversation()

        with mock.patch.object(chats, '_load_runtime', return_value=FakeRuntime()):
            result = chats.handle_incoming(conversation['id'], '在吗', msg_id='m-nocid')

        self.assertIsNotNone(result['ai_reply'])
        self.assertFalse(result['ai_send']['sent'])
        self.assertEqual(result['ai_send']['code'], 'missing_cid')
        self.assertEqual(client.sent, [])

        row = db.query_one('SELECT * FROM messages WHERE id = ?', (result['ai_reply']['id'],))
        self.assertEqual(int(row['sent_ok']), 0)
        self.assertIn('cid', json.loads(row['meta'])['send_error'])

        log = db.query_one("SELECT * FROM operation_logs WHERE action = 'ai_reply_send'")
        self.assertIsNotNone(log)
        self.assertEqual(int(log['success']), 0)

    def test_risk_cooldown_blocks_send_without_retry(self):
        """账号在风控冷却里：不发、不重试，并记一条 `ai_reply_blocked`。"""
        client = FakeProtocolClient()
        install_client(client)
        risk.registry.get(ACCOUNT_ID).trip('RGV587_ERROR')

        conversation = self._conversation()
        draft = chats.append_message(
            conversation['id'], 'ai:item', '在的', ai_generated=True, sent_ok=False)
        result = chats.deliver_ai_reply(conversation['id'], draft['id'], '在的', cid=CID)

        self.assertFalse(result['sent'])
        self.assertEqual(result['code'], 'risk_cooldown')
        self.assertEqual(client.sent, [])
        events = db.query('SELECT * FROM risk_events WHERE account_id = ?', (ACCOUNT_ID,))
        self.assertTrue(any(event['kind'] == 'ai_reply_blocked' for event in events), events)

    def test_protocol_send_error_is_recorded_not_raised(self):
        """协议层发送抛异常（风控报错文案）：投递口吞掉并把原因写清楚。"""
        client = FakeProtocolClient(fail=RuntimeError('RGV587_ERROR 哎哟喂，被挤爆了'))
        install_client(client)

        conversation = self._conversation()
        draft = chats.append_message(
            conversation['id'], 'ai:item', '在的', ai_generated=True, sent_ok=False)
        result = chats.deliver_ai_reply(conversation['id'], draft['id'], '在的', cid=CID)

        self.assertFalse(result['sent'])
        self.assertEqual(result['code'], 'risk_blocked')
        self.assertEqual(client.sent, [])

        row = db.query_one('SELECT * FROM messages WHERE id = ?', (draft['id'],))
        self.assertEqual(int(row['sent_ok']), 0)
        self.assertIn('失败', json.loads(row['meta'])['send_error'])

        events = db.query('SELECT * FROM risk_events WHERE account_id = ?', (ACCOUNT_ID,))
        self.assertTrue(any(event['kind'] == 'ai_reply_blocked' for event in events), events)
        log = db.query_one("SELECT * FROM operation_logs WHERE action = 'ai_reply_send'")
        self.assertEqual(int(log['success']), 0)


# --------------------------------------------------------------------------- 3. 认不出的帧


class UnparsedFrameWarningTest(_TempAppMixin, unittest.TestCase):
    """认不出的入站帧：WARNING 留痕（type + 截断 body），不抛、不造会话。"""

    def test_unparsed_frame_logs_warning_without_raising(self):
        client = FakeProtocolClient()
        install_client(client)
        im_service.start(ACCOUNT_ID)
        session = im_service.current_session(ACCOUNT_ID)

        captured = []
        sink = logger.add(captured.append, level='WARNING', format='{message}')
        try:
            delivered = session.dispatch({'headers': {'lwp': '/pong'}, 'body': [1, 2, 3]})
        finally:
            logger.remove(sink)

        self.assertEqual(delivered, 1)                 # handler 跑完了，没被吞成异常
        self.assertTrue(any('type=/pong' in text for text in captured), captured)
        self.assertTrue(any('body=[1, 2, 3]' in text for text in captured), captured)
        self.assertEqual(db.query('SELECT * FROM conversations'), [])

    def test_dispatch_im_message_returns_none_for_non_message_frames(self):
        self.assertIsNone(chats.dispatch_im_message(ACCOUNT_ID, None))
        self.assertIsNone(chats.dispatch_im_message(ACCOUNT_ID, {}))


# --------------------------------------------------------------------------- 5. 启动接线


class ImBootstrapTest(_TempAppMixin, unittest.TestCase):
    """后端启动时必须**自己**给账号挂 IM 入站。

    缺陷：`im_service.start()` 是收私信的唯一入口，但此前没有任何生产调用方 ——
    后端跑起来能推 WS 事件、能查数据，就是收不到一句真实买家消息。UI 上也没有
    「开始收信」按钮，所以没人调用等于整条链路空转。本文件锁「启动即挂载」。
    """

    def setUp(self):
        super().setUp()
        db.execute('DELETE FROM accounts')
        db.execute('DELETE FROM account_credentials')

    def _seed_account(self, account_id, enabled=1, cookie='unb=2200000001; _m_h5_tk=x'):
        db.execute(
            'INSERT INTO accounts(id, nickname, enabled) VALUES(?, ?, ?)',
            (account_id, '启动测试小铺', enabled),
        )
        db.execute(
            'INSERT INTO account_credentials(account_id, cookie, login_state) '
            'VALUES(?, ?, ?)',
            (account_id, cookie, 'ok'),
        )

    def test_lifespan_attaches_im_for_enabled_accounts_with_cookie(self):
        self._seed_account('boot_acc')
        self._seed_account('boot_disabled', enabled=0)
        self._seed_account('boot_no_cookie', cookie='')

        calls = []
        started = threading.Event()

        def fake_start(account_id):
            calls.append(account_id)
            started.set()
            return {'account_id': account_id, 'attached': True, 'connected': True}

        with mock.patch.object(im_service, 'start', side_effect=fake_start):
            with TestClient(create_app()):
                self.assertTrue(
                    started.wait(timeout=10),
                    '后端启动后没有给任何账号挂 IM 入站',
                )

        # 只有「已启用 + 库里有 Cookie」的账号该被拉起来；禁用账号和没 Cookie 的
        # 一律不该出现在这里（没 Cookie 时 start() 会回 not_ready，属纯浪费）。
        self.assertEqual(calls, ['boot_acc'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
