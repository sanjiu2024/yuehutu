"""会话/消息服务测试：会话与消息、人工接管、AI 开关语义、编排层懒导入。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_services_chats.py"

**不打真实闲鱼平台**：协议层与编排层都用 sys.modules 注入假模块。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


class _ChatsTestBase(unittest.TestCase):
    """公共夹具。分出来是为了让回归锁类能复用，而不继承父类的全部用例（避免重复跑）。"""

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
        db.execute('DELETE FROM accounts')
        clear_module(CLIENT_MODULE)
        clear_module(RUNTIME_MODULE)
        self.client = FakeClient()

    def make_account(self, account_id='acc1', enabled=1, auto_reply=1):
        """建账号行（任务 C3 之后，入站新建会话要求账号存在；B3 读 enabled/auto_reply）。"""
        db.execute(
            'INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) VALUES(?,?,?,?)',
            (account_id, f'账号{account_id}', enabled, auto_reply),
        )
        return account_id

    def make_conversation(self, peer='buyer1', account='acc1', mode='auto'):
        return chats.ensure_conversation(account, peer, peer_nickname=f'买家{peer}', mode=mode)


class ChatsServiceTest(_ChatsTestBase):
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
        # 有真 cid 才会走到「协议层」这一步；没有 cid 的话问题是 `missing_cid`（另一条用例）
        chats.append_message(conv['id'], 'buyer', '在吗', meta={'source': 'im', 'cid': 'cid-x'})
        block_module(CLIENT_MODULE)
        try:
            result = chats.send_manual(conv['id'], '我自己回他')
        finally:
            clear_module(CLIENT_MODULE)

        self.assertFalse(result['sent'])
        self.assertEqual(result['mode'], 'manual')
        self.assertEqual(result['code'], 'xianyu_not_ready')
        self.assertIn('warning', result)
        self.assertEqual(result['message']['sender'], 'human')
        # 未送达必须如实标注：sent_ok=0 + meta.send_error
        self.assertFalse(result['message']['sent_ok'])
        self.assertIn('send_error', json.loads(result['message']['meta'] or '{}'))

        row = db.query_one('SELECT mode FROM conversations WHERE id = ?', (conv['id'],))
        self.assertEqual(row['mode'], 'manual')

        logs = db.query("SELECT * FROM operation_logs WHERE action = 'send_manual'")
        self.assertEqual(len(logs), 1)
        # 没发出去就不许记成功（以前恒记 success=1，「人工回复半残」查不出来）
        self.assertEqual(logs[0]['success'], 0)
        self.assertIn('xianyu_not_ready', logs[0]['detail'])

    def test_send_manual_without_cid_does_not_send_to_buyer_id(self):
        """回归锁（缺陷 C2）：取不到 cid 时**不能**拿 peer_user_id 冒充平台会话 id。

        老代码 `_platform_cid()` 取不到就 `return peer_user_id`，于是 `send_message`
        的第一个位置参数变成了买家 user id —— 消息发到一个平台不认识的会话，
        本地还记成 `sent: true`。审计实测参数为 `('audit_peer', 'manual-reply-content', None)`。
        """
        install_client(self.client)
        conv = self.make_conversation(peer='audit_peer')  # 会话里没有任何带 cid 的消息

        result = chats.send_manual(conv['id'], '人工回复')

        self.assertEqual(self.client.sent, [], '取不到 cid 时一个字节都不该发出去')
        self.assertFalse(result['sent'])
        self.assertEqual(result['code'], 'missing_cid')
        self.assertIn('cid', result['warning'])
        # 本地留存但标注未送达
        self.assertFalse(result['message']['sent_ok'])
        self.assertIn('cid', json.loads(result['message']['meta'] or '{}')['send_error'])
        log = db.query_one("SELECT * FROM operation_logs WHERE action = 'send_manual'")
        self.assertEqual(int(log['success']), 0)

    def test_send_manual_pushes_to_protocol(self):
        """有真 cid 时按真签名 `send_message(cid, text, toid=None)` 发出去。"""
        install_client(self.client)
        conv = self.make_conversation()
        chats.append_message(conv['id'], 'buyer', '在吗', meta={'source': 'im', 'cid': 'cid-real'})

        result = chats.send_manual(conv['id'], '人工回复')

        self.assertTrue(result['sent'])
        self.assertEqual(result['code'], 'ok')
        self.assertNotIn('warning', result)
        # 回归：老代码传的是 (account_id, peer_user_id, text)，按真签名
        # send_message(cid, text, toid=None) 解出来就是「cid=账号 id、正文=买家 id」——
        # 所以这里必须同时钉住「第一个位置是会话 id、第二个位置才是正文」。
        cid, text, _toid = self.client.sent[0]
        self.assertNotEqual(cid, 'acc1', '第一个位置不能是账号 id')
        self.assertEqual(cid, 'cid-real', '必须用 IM 帧带来的 cid')
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
        self.make_account('acc1')  # C3 之后：新建会话要求账号真实存在
        result = chats.handle_incoming(
            0, '新买家来信', account_id='acc1', peer_user_id='newbie', peer_nickname='新买家',
        )
        self.assertEqual(result['message']['content'], '新买家来信')
        self.assertEqual(chats.list_conversations()['total'], 1)

    def test_handle_incoming_rejects_unknown_account(self):
        """回归锁（缺陷 C3）：幽灵账号不许凭空造会话。

        审计实测：`POST /api/chats/incoming {"account_id": "nope_acc", ...}` 会 200 建出
        一个 `account_id='nope_acc'` 的会话，会话列表随后出现真实账号的「幽灵会话」。
        """
        result = chats.handle_incoming(
            0, '我是幽灵', account_id='nope_acc', peer_user_id='b1',
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'not_found')
        self.assertIn('nope_acc', result['message'])
        self.assertEqual(db.query('SELECT * FROM conversations'), [])
        self.assertEqual(db.query('SELECT * FROM messages'), [])

    # ------------------------------------------------------------------ 事件

    def test_events_published(self):
        conv = self.make_conversation()
        before = len(events.recent(limit=500))
        chats.append_message(conv['id'], 'buyer', 'hi')
        chats.set_ai_enabled(conv['id'], False)
        topics = [e['topic'] for e in events.recent(limit=500)[before:]]
        self.assertIn('chats.message', topics)
        self.assertIn('chats.mode_changed', topics)


class InboundGateTest(_ChatsTestBase):
    """task-19 修复的回归锁：账号闸门、cid 诚实性、图片透传、删号清理。

    审计报告 `docs/audit/chats.md` 的缺陷编号（C2/C3/B3/A2）与 §10 残留清理。
    """

    # ------------------------------------------------------------------ C3 幽灵会话

    def test_ghost_account_does_not_create_conversation_but_existing_still_works(self):
        """只在「新建会话」这步拦账号；已有会话的老路径不许被拦（IM 循环靠它）。"""
        self.make_account('acc1')
        conv = self.make_conversation(peer='buyer1')

        # 已有会话：账号行删掉也照常收信（回归锁：别把校验塞进 ensure_conversation/get_conversation）
        db.execute('DELETE FROM accounts WHERE id = ?', ('acc1',))
        result = chats.handle_incoming(conv['id'], '老会话还能收信')
        self.assertEqual(result['message']['content'], '老会话还能收信')

        # 新建会话：账号不存在 -> 拒绝，一条库都不落
        before = len(db.query('SELECT * FROM conversations'))
        ghost = chats.handle_incoming(0, '幽灵', account_id='ghost_acc', peer_user_id='g1')
        self.assertFalse(ghost['success'])
        self.assertEqual(ghost['code'], 'not_found')
        self.assertEqual(len(db.query('SELECT * FROM conversations')), before)

    # ------------------------------------------------------------------ B3 账号级闸门

    def test_disabled_account_does_not_trigger_ai(self):
        """B3：账号 enabled=0（未启用）时不许自动回复。全仓 `enabled` 原来只有 main.py 读过。"""
        self.make_account('acc1', enabled=0, auto_reply=1)
        conv = self.make_conversation()
        module = types.ModuleType(RUNTIME_MODULE)

        async def reply_for_message(conversation_id, message_id):
            raise AssertionError('账号没启用时不该调 AI')

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertIsNone(result['ai_reply'])
        self.assertEqual(result['ai_result']['code'], 'account_auto_reply_off')
        self.assertEqual(len(db.query('SELECT * FROM messages')), 1)

    def test_auto_reply_off_account_does_not_trigger_ai(self):
        """B3：账号级 auto_reply=0（该账号关自动回复）时不许自动回复，但**不影响别的账号**。"""
        self.make_account('acc1', enabled=1, auto_reply=0)
        self.make_account('acc2', enabled=1, auto_reply=1)
        conv = self.make_conversation(account='acc1')
        other = chats.ensure_conversation('acc2', 'buyer2', mode='auto')
        module = types.ModuleType(RUNTIME_MODULE)
        calls = []

        async def reply_for_message(conversation_id, message_id):
            calls.append(conversation_id)
            return {'ok': True, 'reply': 'acc2 的回复', 'role': 'item'}

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            off = chats.handle_incoming(conv['id'], '在吗')
            on = chats.handle_incoming(other['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertIsNone(off['ai_reply'])
        self.assertEqual(off['ai_result']['code'], 'account_auto_reply_off')
        self.assertEqual(calls, [other['id']])
        self.assertEqual(on['ai_reply']['content'], 'acc2 的回复')

    def test_account_gate_missing_row_does_not_block(self):
        """账号行不存在时放行（回归锁）：test_im_loop.py 全程用不存在的账号派发 IM 帧。

        那条测试不在我的写范围且不可改，所以「表里没这行」必须是**放行**而不是拒绝。
        """
        conv = self.make_conversation()
        module = types.ModuleType(RUNTIME_MODULE)

        async def reply_for_message(conversation_id, message_id):
            return {'ok': True, 'reply': '放行', 'role': 'item'}

        module.reply_for_message = reply_for_message  # type: ignore[attr-defined]
        sys.modules[RUNTIME_MODULE] = module
        try:
            result = chats.handle_incoming(conv['id'], '在吗')
        finally:
            clear_module(RUNTIME_MODULE)

        self.assertEqual(result['ai_reply']['content'], '放行')

    def test_deliver_ai_reply_respects_account_gate(self):
        """直投路径同样过闸门（B3）：发送函数自己会查，不只靠 handle_incoming。"""
        install_client(self.client)
        conv = self.make_conversation()
        self.make_account('acc1', enabled=0)
        msg = chats.append_message(conv['id'], 'ai:item', '草稿')

        result = chats.deliver_ai_reply(conv['id'], msg['id'], '草稿', cid='cid-1')

        self.assertFalse(result['sent'])
        self.assertEqual(result['code'], 'account_auto_reply_off')
        self.assertEqual(self.client.sent, [])
        row = db.query_one('SELECT sent_ok FROM messages WHERE id = ?', (msg['id'],))
        self.assertEqual(int(row['sent_ok']), 0)

    def test_incoming_meta_only_whitelists_known_keys(self):
        """meta 白名单：任意 body 键不许灌进 messages.meta。"""
        from backend.app.api import chats as api_chats

        meta = api_chats._incoming_meta(
            {'source': 'im', 'image_url': 'https://x.test/a.png', 'evil': 'boom', 'cid': 'c1'},
        )
        self.assertEqual(meta['image_url'], 'https://x.test/a.png')
        self.assertEqual(meta['cid'], 'c1')
        self.assertNotIn('evil', meta)

    # ------------------------------------------------------------------ A2 角色与图片

    def test_ensure_conversation_does_not_prewrite_agent_role(self):
        """A2：建会话时**不预写死合法角色**。

        `roles.role_for_conversation()` 的第一条规则是「conv.agent_role 是合法角色就用它」，
        以前 services 建会话时写死 `agent_role='item'`，于是 bargain/vision/cs 规则永远不可达。
        """
        conv = chats.ensure_conversation('acc1', 'buyer1', item_id='item-9')
        row = db.query_one('SELECT agent_role FROM conversations WHERE id = ?', (conv['id'],))
        self.assertEqual(str(row['agent_role'] or ''), '')
        self.assertIsNone(conv['agent_role'] or None)

    def test_incoming_image_url_makes_vision_reachable(self):
        """A2 后半：图片要能走到编排层，`vision` 角色才可达。

        图片进 `messages.meta.image_url` -> `agents.roles` 选 vision -> `agents.runtime`
        把图塞进模型消息。以前 `meta` 只有 `{'source': 'api'}`，整条链路没有生产者。
        """
        from backend.app.agents import roles as roles_mod

        self.make_account('acc1')
        install_client(self.client)
        result = chats.handle_incoming(
            0, '这个款式还有别的颜色吗',
            account_id='acc1', peer_user_id='buyer_img',
            meta={'source': 'api', 'image_url': 'https://img.test/real.jpg'},
        )
        msg = result['message']
        meta = json.loads(msg['meta'] or '{}')
        self.assertEqual(meta['image_url'], 'https://img.test/real.jpg')

        row = db.query_one('SELECT * FROM conversations WHERE id = ?', (msg['conversation_id'],))
        self.assertEqual(roles_mod.role_for_conversation(row, msg), 'vision')

    def test_first_image_url_scrapes_raw_frame(self):
        """IM 帧的真实形状没有文档（`parse_incoming_message` 不回图片字段），所以要能抠原始帧。"""
        frame = {
            'body': {'message': {
                'content': {'text': {'text': '看看这个'}},
                'extension': {'picUrl': 'https://img.test/from-frame.jpg', 'itemId': 'i1'},
            }},
        }
        self.assertEqual(chats._first_image_url(frame), 'https://img.test/from-frame.jpg')
        # 不是图片地址的一律不要（宁可 vision 不触发，也别把无关 url 送进模型）
        self.assertEqual(chats._first_image_url({'body': {'url': 'https://x.test/page.html'}}), '')
        self.assertEqual(chats._first_image_url({}), '')
        self.assertEqual(chats._first_image_url({'image_url': 'not-a-url'}), '')

    # ------------------------------------------------------------------ §10 删号清理

    def test_purge_account_data_is_idempotent_and_scoped(self):
        """§10：删号要清会话与消息，且只清这个账号的；重复调用安全。"""
        self.make_conversation(peer='b1', account='acc_del')
        self.make_conversation(peer='b2', account='acc_del')
        self.make_conversation(peer='b3', account='acc_keep')
        conv_del = chats.list_conversations(account_id='acc_del')['conversations'][0]
        conv_keep = chats.list_conversations(account_id='acc_keep')['conversations'][0]
        for conv_id in (conv_del['id'], conv_keep['id']):
            chats.append_message(conv_id, 'buyer', 'hi')

        first = chats.purge_account_data('acc_del')
        self.assertEqual(first, {'conversations': 2, 'messages': 1})
        second = chats.purge_account_data('acc_del')
        self.assertEqual(second, {'conversations': 0, 'messages': 0})

        self.assertEqual(chats.list_conversations(account_id='acc_del')['total'], 0)
        self.assertEqual(chats.list_conversations(account_id='acc_keep')['total'], 1)
        self.assertEqual(len(db.query('SELECT * FROM messages')), 1)

    def test_purge_account_data_missing_account(self):
        self.assertEqual(chats.purge_account_data('nobody'), {'conversations': 0, 'messages': 0})
        self.assertEqual(chats.purge_account_data(None), {'conversations': 0, 'messages': 0})

    # ------------------------------------------------------------------ C5 闸门可达性

    def test_mode_not_auto_decision_is_reported_on_incoming(self):
        """C5：`mode_not_auto` 决策必须从入站路径就能看见（老代码只在投递函数里，生产不可达）。"""
        conv = self.make_conversation(mode='manual')
        result = chats.handle_incoming(conv['id'], '在吗')
        self.assertIsNone(result['ai_reply'])
        # ai_send 保持 None：编排层测试断言 manual 模式下 ai_send is None
        self.assertIsNone(result['ai_send'])
        self.assertEqual(result['ai_result']['code'], 'mode_not_auto')
        self.assertIn('manual', result['ai_result']['message'])


class ImBackfillTest(_ChatsTestBase):
    """把平台上的**历史**会话拉回本地（`chats.sync_im_conversations`）。

    为什么要有这条路径：IM 入站 handler 只处理「正在发生」的推送帧，进程启动之前就
    存在于平台上的会话永远不会自己进库。真机表现是用户那句话 ——
    「我账号上面的消息一个没读取过来。」
    """

    def setUp(self):
        super().setUp()
        self.make_account('acc1')
        self.calls = {'conversations': [], 'messages': []}

    def _install(self, conversations=None, messages=None):
        """装一个只实现这两个口子的假客户端（同步函数，`call_method` 两种都吃）。"""

        class FakeImClient:
            def __init__(inner):
                inner.conversations = conversations if conversations is not None else [
                    {
                        'cid': 'cid-1',
                        'buyer_id': 'buyer-1',
                        'buyer_name': '小明',
                        'buyer_avatar': 'https://img/a.jpg',
                        'item_id': 'item-9',
                        'item_title': '测试商品',
                        'last_message': '在吗？',
                        'last_time': 1700000002000,
                        'unread': 2,
                    }
                ]
                inner.messages = messages if messages is not None else [
                    {
                        'msg_id': 'm-2', 'cid': 'cid-1', 'buyer_id': 'buyer-1',
                        'is_self': False, 'content': '在吗？', 'images': [],
                        'msg_type': 'text', 'time': 1700000002000,
                    },
                    {
                        'msg_id': 'm-1', 'cid': 'cid-1', 'buyer_id': 'buyer-1',
                        'is_self': True, 'content': '在的', 'images': [],
                        'msg_type': 'text', 'time': 1700000001000,
                    },
                ]

            def fetch_conversations(inner, start_timestamp=None, limit=50):
                self.calls['conversations'].append((start_timestamp, limit))
                return inner.conversations

            def fetch_messages(inner, cid, start_timestamp=None, limit=20):
                self.calls['messages'].append((cid, limit))
                return inner.messages

        install_client(FakeImClient())
        return FakeImClient

    # ------------------------------------------------------------------ 正常回填

    def test_backfill_creates_conversation_and_messages(self):
        self._install()
        result = chats.sync_im_conversations('acc1')
        self.assertEqual(result['scanned'], 1)
        self.assertEqual(result['conversations_new'], 1)
        self.assertEqual(result['messages'], 2)

        listed = chats.list_conversations(account_id='acc1')
        self.assertEqual(listed['total'], 1)
        conv = listed['conversations'][0]
        self.assertEqual(conv['peer_user_id'], 'buyer-1')
        self.assertEqual(conv['peer_nickname'], '小明')
        self.assertEqual(conv['item_id'], 'item-9')
        self.assertEqual(conv['unread'], 2, '未读数要对齐平台的红点')

        messages = chats.get_messages(conv['id'])['messages']
        self.assertEqual([one['content'] for one in messages], ['在的', '在吗？'],
                         '消息要按平台时间正序，别倒着灌')
        buyer = messages[-1]
        self.assertEqual(buyer['sender'], 'buyer')
        self.assertEqual(buyer['direction'], 'in')
        meta = json.loads(buyer['meta']) if isinstance(buyer['meta'], str) else buyer['meta']
        self.assertIn('cid', meta, 'cid 要留在 meta 里，AI 回复才发得出去')
        self.assertEqual(meta.get('external_id'), 'm-2')

    def test_backfill_is_idempotent(self):
        self._install()
        first = chats.sync_im_conversations('acc1')
        conv_id = chats.list_conversations(account_id='acc1')['conversations'][0]['id']
        second = chats.sync_im_conversations('acc1')
        self.assertEqual(first['messages'], 2)
        self.assertEqual(second['conversations_new'], 0)
        self.assertEqual(second['messages'], 0)
        self.assertEqual(second['messages_skipped'], 2)
        self.assertEqual(len(chats.get_messages(conv_id)['messages']), 2,
                         '重复回填不许灌重复消息')

    def test_backfill_skips_conversation_without_buyer(self):
        self._install(conversations=[{'cid': 'cid-x'}])
        result = chats.sync_im_conversations('acc1')
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['conversations_new'], 0)
        self.assertEqual(chats.list_conversations()['total'], 0)

    def test_single_conversation_failure_does_not_fail_the_whole_sync(self):
        self._install()
        original = chats.common.call_method

        def flaky(client, names, *args, **kwargs):
            if names and names[0] == 'fetch_messages':
                return chats.common.CALL_ERROR, 'boom'
            return original(client, names, *args, **kwargs)

        with mock.patch.object(chats.common, 'call_method', side_effect=flaky):
            result = chats.sync_im_conversations('acc1')
        self.assertEqual(result['conversations_new'], 1)
        self.assertEqual(result['messages'], 0)
        self.assertTrue(result.get('warnings'), '单会话拉消息失败要留 warning，不整体失败')

    # ------------------------------------------------------------------ 失败路径

    def test_no_client_returns_structured_envelope(self):
        clear_module(CLIENT_MODULE)

        def no_client(account_id):
            return None, {'success': False, 'message': '账号还没 Cookie', 'code': 'xianyu_not_ready'}

        with mock.patch.object(chats.common, 'get_client', side_effect=no_client):
            result = chats.sync_im_conversations('acc1')
        self.assertEqual(result['code'], 'xianyu_not_ready')

    def test_protocol_failure_becomes_envelope(self):
        class Broken:
            def fetch_conversations(self, start_timestamp=None, limit=50):
                raise RuntimeError('网络断了')

        install_client(Broken())
        result = chats.sync_im_conversations('acc1')
        self.assertFalse(result.get('success', True))
        self.assertTrue(result.get('code'))


class PeerNicknameOwnershipTest(_ChatsTestBase):
    """task-21：`conversations.peer_nickname` 只允许是**买家**的昵称。

    真机上报：用户会话列表整列显示成他自己的昵称。会话条目里根本没有买家昵称字段，
    而 `lastMessage.message.extension.reminderTitle` 是最后一条消息**发送者**的昵称 ——
    自己发最后一条时那就是账号自己的名字（老代码无条件拿它当买家名）。
    """

    MY_NICK = '月狐兔_'
    BUYER_NICK = '一蓑烟雨任平生'

    def setUp(self):
        super().setUp()
        self.make_account('acc1')
        db.execute('UPDATE accounts SET nickname = ? WHERE id = ?', (self.MY_NICK, 'acc1'))

    # ------------------------------------------------------------------ 夹具

    def _install(self, buyer_name='', messages=None):
        """装一个只实现同步两个口子的假客户端（`buyer_name` = 会话条目里解析出的昵称）。"""
        conversations = [{
            'cid': 'cid-1', 'buyer_id': 'buyer-1', 'buyer_name': buyer_name,
            'item_id': 'item-9', 'last_message': '', 'last_time': 1, 'unread': 0,
        }]
        rows = list(messages or [])

        class FakeImClient:
            def fetch_conversations(inner, start_timestamp=None, limit=50):
                return conversations

            def fetch_messages(inner, cid, start_timestamp=None, limit=20):
                return rows

        install_client(FakeImClient())

    def _message(self, nick, *, mine, msg_id, time):
        return {
            'msg_id': msg_id, 'cid': 'cid-1', 'buyer_id': 'buyer-1', 'is_self': mine,
            'content': '在吗', 'images': [], 'msg_type': 'text', 'time': time,
            'sender_nick': '' if mine else nick, 'sender_nick_raw': nick,
        }

    def _nick(self, peer='buyer-1'):
        row = db.query_one(
            'SELECT peer_nickname FROM conversations WHERE peer_user_id = ?', (peer,),
        )
        return '' if row is None else str(row['peer_nickname'] or '')

    def _frame(self, nick, buyer_id='buyer-1'):
        return {
            'lwp': '/r/MessageManager/listUserMessages',
            'body': [{
                'msgId': 'im-1', 'cid': 'conv-1@goofish',
                'senderId': f'{buyer_id}@goofish',
                'content': {'text': {'text': '这个还在吗'}},
                'extension': {'reminderTitle': nick},
            }],
        }

    # ------------------------------------------------------------------ 同步：修脏数据

    def test_sync_repairs_my_own_nickname_already_stored(self):
        """用户库里现在就是脏的：整列都是他自己的昵称，同步要把它改成买家名。"""
        chats.ensure_conversation('acc1', 'buyer-1', peer_nickname=self.MY_NICK)
        self._install(messages=[self._message(self.BUYER_NICK, mine=False, msg_id='m-b', time=2)])

        result = chats.sync_im_conversations('acc1')

        self.assertEqual(self._nick(), self.BUYER_NICK, '买家发来的消息里的昵称才是客户名')
        self.assertEqual(result['nicknames_fixed'], 1)

    def test_sync_never_writes_my_own_nickname(self):
        """会话条目和自己消息里都只有我自己的昵称 -> 宁可空着，也不能当客户名。"""
        self._install(
            buyer_name=self.MY_NICK,
            messages=[self._message(self.MY_NICK, mine=True, msg_id='m-s', time=1)],
        )

        result = chats.sync_im_conversations('acc1')

        self.assertEqual(self._nick(), '', '拿不到买家昵称就留空（前端兜底「买家 <id>」）')
        self.assertEqual(result['nicknames_fixed'], 0)

    def test_sync_keeps_a_good_nickname_instead_of_overwriting_it(self):
        """库里已经是别的买家名时不覆盖：别用一个来路不明的串把对的名字冲掉。"""
        chats.ensure_conversation('acc1', 'buyer-1', peer_nickname='小明')
        self._install(
            buyer_name=self.MY_NICK,
            messages=[self._message('新名字', mine=False, msg_id='m-b', time=2)],
        )

        result = chats.sync_im_conversations('acc1')

        self.assertEqual(self._nick(), '小明')
        self.assertEqual(result['nicknames_fixed'], 0)

    def test_sync_learns_my_nickname_from_my_own_message(self):
        """账号表里没有昵称时，靠自己发的消息学会「我是谁」，照样能认出脏昵称。"""
        db.execute('UPDATE accounts SET nickname = ? WHERE id = ?', ('', 'acc1'))
        chats.ensure_conversation('acc1', 'buyer-1', peer_nickname=self.MY_NICK)
        self._install(messages=[
            self._message(self.MY_NICK, mine=True, msg_id='m-s', time=1),
            self._message(self.BUYER_NICK, mine=False, msg_id='m-b', time=2),
        ])

        result = chats.sync_im_conversations('acc1')

        self.assertEqual(self._nick(), self.BUYER_NICK)
        self.assertEqual(result['nicknames_fixed'], 1)

    # ------------------------------------------------------------------ 入站帧

    def test_inbound_frame_fills_the_nickname_of_a_new_conversation(self):
        result = chats.dispatch_im_message('acc1', self._frame(self.BUYER_NICK))

        self.assertIsNotNone(result, '买家来信要认得出来')
        self.assertEqual(self._nick(), self.BUYER_NICK)

    def test_inbound_frame_never_writes_my_own_nickname(self):
        chats.dispatch_im_message('acc1', self._frame(self.MY_NICK))
        self.assertEqual(self._nick(), '')

    def test_inbound_frame_does_not_overwrite_a_good_nickname(self):
        chats.ensure_conversation('acc1', 'buyer-1', peer_nickname='小明')
        chats.dispatch_im_message('acc1', self._frame(self.BUYER_NICK))
        self.assertEqual(self._nick(), '小明')


class UnparsedFrameLogTest(unittest.TestCase):
    """真机日志噪声：平台例行帧不许按 WARNING 记。

    上线后每 30 秒一次心跳回帧、每分钟一次同步包，全打成 WARNING 会把真正的
    「认不出的买家消息」淹掉 —— 而那条日志是首上线唯一的线索（`/s/vulcan` 里
    带的 bizType 370 是商品草稿推送，跟私信无关）。
    """

    def _summary(self, frame_type):
        fake = SimpleNamespace(frame_summary=lambda payload: {'type': frame_type, 'body': '{}'})
        return fake

    def test_routine_frames_are_quiet(self):
        for frame_type in ('/s/sync', '/s/vulcan', ''):
            with mock.patch.object(chats.logger, 'warning') as warn:
                chats._warn_unparsed_frame(self._summary(frame_type), {'a': 1}, '不是可处理的买家文本消息')
            warn.assert_not_called()

    def test_unknown_frame_still_warns(self):
        with mock.patch.object(chats.logger, 'warning') as warn:
            chats._warn_unparsed_frame(
                self._summary('/r/MessageManager/listUserMessages'), {'a': 1}, '不是可处理的买家文本消息',
            )
        warn.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
