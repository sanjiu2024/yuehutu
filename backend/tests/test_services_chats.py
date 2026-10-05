"""会话/消息服务测试：会话与消息、人工接管、AI 开关语义、编排层懒导入。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_services_chats.py"

**不打真实闲鱼平台**：协议层与编排层都用 sys.modules 注入假模块。
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

from backend.app.core import config, db, events  # noqa: E402
from backend.app.services import chats  # noqa: E402

CLIENT_MODULE = 'backend.app.xianyu.client'
RUNTIME_MODULE = 'backend.app.agents.runtime'


class FakeClient:
    def __init__(self, account_id='acc1'):
        self.account_id = account_id
        self.sent = []

    def send_message(self, cid, text, toid=None):
        # 真签名见 backend/app/xianyu/client.py:345 —— 老 FakeClient 照着错误调用点
        # 抄成了 (account_id, peer_user_id, text)，所以「人工接管发错对象和正文」这个
        # 缺陷在 297 条全绿里也抓不到。
        self.sent.append((cid, text, toid))
        return {'ok': True}


def install_client(client) -> None:
    module = types.ModuleType(CLIENT_MODULE)
    module.get_client = lambda account_id: client  # type: ignore[attr-defined]
    sys.modules[CLIENT_MODULE] = module


def block_module(name: str) -> None:
    """把模块塞成 None：import 时抛 ImportError（模拟「还没写完」）。"""
    sys.modules[name] = None  # type: ignore[assignment]


def clear_module(name: str) -> None:
    sys.modules.pop(name, None)


class ChatsServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        clear_module(CLIENT_MODULE)
        clear_module(RUNTIME_MODULE)
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')
        db.execute('DELETE FROM operation_logs')
        clear_module(CLIENT_MODULE)
        clear_module(RUNTIME_MODULE)
        self.client = FakeClient()

    def make_conversation(self, peer='buyer1', account='acc1', mode='auto'):
        return chats.ensure_conversation(account, peer, peer_nickname=f'买家{peer}', mode=mode)

    # ------------------------------------------------------------------ 会话

    def test_ensure_conversation_is_idempotent(self):
        first = self.make_conversation()
        second = chats.ensure_conversation('acc1', 'buyer1', peer_nickname='新昵称')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['peer_nickname'], '新昵称')
        self.assertEqual(db.query('SELECT * FROM conversations') and len(db.query('SELECT * FROM conversations')), 1)

    def test_list_conversations_filters(self):
        self.make_conversation('b1')
        self.make_conversation('b2', mode='manual')
        self.make_conversation('b3', account='acc2')

        self.assertEqual(chats.list_conversations()['total'], 3)
        self.assertEqual(chats.list_conversations(account_id='acc1')['total'], 2)
        self.assertEqual(chats.list_conversations(mode='manual')['total'], 1)
        self.assertEqual(chats.list_conversations(keyword='b2')['total'], 1)

    def test_list_conversations_has_last_message(self):
        conv = self.make_conversation()
        chats.append_message(conv['id'], 'buyer', '在吗')
        result = chats.list_conversations()
        self.assertEqual(result['conversations'][0]['last_message']['content'], '在吗')

    def test_get_conversation_missing(self):
        result = chats.get_conversation(999)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    # ------------------------------------------------------------------ 消息

    def test_append_message_directions(self):
        conv = self.make_conversation()

        incoming = chats.append_message(conv['id'], 'buyer', '多少钱')
        self.assertEqual(incoming['direction'], 'in')
        self.assertEqual(incoming['sender'], 'buyer')

        manual = chats.append_message(conv['id'], 'human', '100 包邮')
        self.assertEqual(manual['direction'], 'out')
        self.assertEqual(manual['sender'], 'human')

        ai = chats.append_message(conv['id'], 'ai:item', '您好', ai_generated=True, mark_unread=False)
        self.assertEqual(ai['direction'], 'out')
        self.assertEqual(ai['sender'], 'ai:item')

        # 买家来信未读数 +1，人工/AI 回复不加
        row = db.query_one('SELECT unread FROM conversations WHERE id = ?', (conv['id'],))
        self.assertEqual(row['unread'], 1)

    def test_get_messages_pagination(self):
        conv = self.make_conversation()
        for index in range(5):
            chats.append_message(conv['id'], 'buyer', f'消息{index}')

        result = chats.get_messages(conv['id'], page=1, size=2)
        self.assertEqual(result['total'], 5)
        self.assertEqual(result['pages'], 3)
        self.assertEqual(len(result['messages']), 2)
        # 返回按时间正序（最新的一页在最后）
        self.assertEqual(result['messages'][-1]['content'], '消息4')

        result = chats.get_messages(conv['id'], page=3, size=2)
        self.assertEqual(result['messages'][0]['content'], '消息0')

        missing = chats.get_messages(999)
        self.assertFalse(missing['success'])

    def test_append_message_missing_conversation(self):
        result = chats.append_message(999, 'buyer', 'hi')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    # ------------------------------------------------------------------ 人工接管

    def test_send_manual_local_only_when_protocol_missing(self):
        conv = self.make_conversation()
        block_module(CLIENT_MODULE)
        try:
            result = chats.send_manual(conv['id'], '我自己回他')
        finally:
            clear_module(CLIENT_MODULE)

        self.assertFalse(result['sent'])
        self.assertEqual(result['mode'], 'manual')
        self.assertIn('warning', result)
        self.assertEqual(result['message']['sender'], 'human')

        row = db.query_one('SELECT mode FROM conversations WHERE id = ?', (conv['id'],))
        self.assertEqual(row['mode'], 'manual')

        logs = db.query("SELECT * FROM operation_logs WHERE action = 'send_manual'")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['success'], 1)

    def test_send_manual_pushes_to_protocol(self):
        install_client(self.client)
        conv = self.make_conversation()
        result = chats.send_manual(conv['id'], '人工回复')
        self.assertTrue(result['sent'])
        self.assertNotIn('warning', result)
        # 回归：老代码传的是 (account_id, peer_user_id, text)，按真签名
        # send_message(cid, text, toid=None) 解出来就是「cid=账号 id、正文=买家 id」——
        # 所以这里必须同时钉住「第一个位置是会话 id、第二个位置才是正文」。
        cid, text, _toid = self.client.sent[0]
        self.assertNotEqual(cid, 'acc1', '第一个位置不能是账号 id')
        self.assertEqual(cid, 'buyer1', '单聊里 cid 与买家 id 相同')
        self.assertEqual(text, '人工回复')

    def test_send_manual_prefers_cid_from_im_meta(self):
        """IM 帧带来的 cid 优先于 peer_user_id（两者不等时不能发错会话）。"""
        install_client(self.client)
        conv = self.make_conversation()
        chats.append_message(conv['id'], 'buyer', '在吗', meta={'source': 'im', 'cid': 'cid-from-im'})
        result = chats.send_manual(conv['id'], '人工回复')
        self.assertTrue(result['sent'])
        self.assertEqual(self.client.sent[0][0], 'cid-from-im')
        self.assertEqual(self.client.sent[0][1], '人工回复')

    def test_send_manual_rejects_empty_and_missing(self):
        conv = self.make_conversation()
        result = chats.send_manual(conv['id'], '   ')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_fields')

        result = chats.send_manual(999, 'x')
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    # ------------------------------------------------------------------ AI 开关

    def test_set_ai_enabled_semantics(self):
        conv = self.make_conversation()
        self.assertEqual(conv['mode'], 'auto')
        self.assertTrue(conv['ai_enabled'])

        off = chats.set_ai_enabled(conv['id'], False)
        self.assertEqual(off['mode'], 'manual')
        self.assertFalse(off['ai_enabled'])

        on = chats.set_ai_enabled(conv['id'], True)
        self.assertEqual(on['mode'], 'auto')
        self.assertTrue(on['ai_enabled'])

        logs = db.query("SELECT * FROM operation_logs WHERE action = 'set_ai_enabled'")
        self.assertEqual(len(logs), 2)

    def test_set_ai_enabled_missing(self):
        result = chats.set_ai_enabled(999, True)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_set_mode_validation(self):
        conv = self.make_conversation()
        result = chats.set_mode(conv['id'], 'off')
        self.assertEqual(result['mode'], 'off')
        self.assertFalse(result['ai_enabled'])

        bad = chats.set_mode(conv['id'], 'weird')
        self.assertFalse(bad['success'])
        self.assertEqual(bad['code'], 'invalid_fields')

    def test_mark_read(self):
        conv = self.make_conversation()
        chats.append_message(conv['id'], 'buyer', 'hi')
        chats.mark_read(conv['id'])
        row = db.query_one('SELECT unread FROM conversations WHERE id = ?', (conv['id'],))
        self.assertEqual(row['unread'], 0)

    # ------------------------------------------------------------------ 买家来信 + AI

    def test_handle_incoming_without_runtime(self):
        """编排层没就绪：只落买家消息，不报错。"""
        conv = self.make_conversation()
        block_module(RUNTIME_MODULE)
        try:
            result = chats.handle_incoming(conv['id'], '这个还有吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertEqual(result['message']['content'], '这个还有吗')
        self.assertIsNone(result['ai_reply'])
        self.assertEqual(len(db.query('SELECT * FROM messages')), 1)

    def test_handle_incoming_with_fake_runtime(self):
        """编排层真实签名是 async reply_for_message(conversation_id, message_id)。"""
        conv = self.make_conversation()
        calls = []
        module = types.ModuleType(RUNTIME_MODULE)

        async def reply_for_message(conversation_id, message_id):
            calls.append((conversation_id, message_id))
            return {'ok': True, 'reply': '收到：在吗', 'role': 'item'}

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertEqual(calls, [(conv['id'], result['message']['id'])])
        self.assertIsNotNone(result['ai_reply'])
        self.assertEqual(result['ai_reply']['content'], '收到：在吗')
        self.assertEqual(result['ai_reply']['sender'], 'ai:item')
        self.assertTrue(result['ai_result']['success'])
        self.assertEqual(len(db.query('SELECT * FROM messages')), 2)

    def test_handle_incoming_manual_mode_skips_ai(self):
        conv = self.make_conversation(mode='manual')
        module = types.ModuleType(RUNTIME_MODULE)

        async def reply_for_message(conversation_id, message_id):
            raise AssertionError('manual 模式下不该调 AI')

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertIsNone(result['ai_reply'])
        self.assertEqual(len(db.query('SELECT * FROM messages')), 1)

    def test_handle_incoming_ai_error_does_not_break(self):
        conv = self.make_conversation()
        module = types.ModuleType(RUNTIME_MODULE)

        async def boom(conversation_id, message_id):
            raise RuntimeError('模型挂了')

        module.reply_for_message = boom  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        # AI 炸了也必须落买家消息、必须回结构化结果，不能抛异常
        self.assertEqual(result['message']['content'], '在吗')
        self.assertIsNone(result['ai_reply'])
        self.assertFalse(result['ai_result']['success'])
        self.assertIn('模型挂了', result['ai_result']['message'])
        self.assertEqual(len(db.query('SELECT * FROM messages')), 1)

    def test_handle_incoming_ai_returns_not_ok(self):
        """编排层返回 ok=False（没配 provider 等）-> 不落 AI 消息，但不影响买家消息。"""
        conv = self.make_conversation()
        module = types.ModuleType(RUNTIME_MODULE)

        async def reply_for_message(conversation_id, message_id):
            return {'ok': False, 'error': '没有可用的模型供应商'}

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertIsNone(result['ai_reply'])
        self.assertFalse(result['ai_result']['success'])
        self.assertIn('模型供应商', result['ai_result']['message'])
        self.assertEqual(len(db.query('SELECT * FROM messages')), 1)

    def test_handle_incoming_sync_fake_runtime_still_works(self):
        """兼容同步假实现（不是协程）：不能因为 await 逻辑把它搞坏。"""
        conv = self.make_conversation()
        module = types.ModuleType(RUNTIME_MODULE)
        module.reply_for_message = lambda conversation_id, message_id: {'reply': '同步也能用'}  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertEqual(result['ai_reply']['content'], '同步也能用')

    # ------------------------------------------------------------------ AI 草稿

    def test_ai_draft_only_generates(self):
        """ai-draft 只生成建议：不落库、不发送、不发消息事件。"""
        conv = self.make_conversation()
        chats.append_message(conv['id'], 'buyer', '能便宜点吗')
        module = types.ModuleType(RUNTIME_MODULE)

        async def reply_for_message(conversation_id, message_id):
            return {'ok': True, 'reply': '可以少 10 块', 'role': 'bargain'}

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            before = len(db.query('SELECT * FROM messages'))
            result = chats.ai_draft(conv['id'])
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertEqual(result['draft'], '可以少 10 块')
        self.assertEqual(result['role'], 'bargain')
        # 关键：没有新增消息
        self.assertEqual(len(db.query('SELECT * FROM messages')), before)

    def test_ai_draft_without_runtime(self):
        conv = self.make_conversation()
        chats.append_message(conv['id'], 'buyer', '在吗')
        block_module(RUNTIME_MODULE)
        try:
            result = chats.ai_draft(conv['id'])
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'xianyu_not_ready')

    def test_ai_draft_without_buyer_message(self):
        conv = self.make_conversation()
        module = types.ModuleType(RUNTIME_MODULE)
        module.reply_for_message = lambda *a: {'ok': True, 'reply': 'x'}  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.ai_draft(conv['id'])
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'invalid_fields')

    def test_ai_draft_missing_conversation(self):
        result = chats.ai_draft(999)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')

    def test_handle_incoming_creates_conversation(self):
        result = chats.handle_incoming(
            0, '新买家来信', account_id='acc1', peer_user_id='newbie', peer_nickname='新买家',
        )
        self.assertEqual(result['message']['content'], '新买家来信')
        self.assertEqual(chats.list_conversations()['total'], 1)

    # ------------------------------------------------------------------ 事件

    def test_events_published(self):
        conv = self.make_conversation()
        before = len(events.recent(limit=500))
        chats.append_message(conv['id'], 'buyer', 'hi')
        chats.set_ai_enabled(conv['id'], False)
        topics = [e['topic'] for e in events.recent(limit=500)[before:]]
        self.assertIn('chats.message', topics)
        self.assertIn('chats.mode_changed', topics)


if __name__ == '__main__':
    unittest.main(verbosity=2)
