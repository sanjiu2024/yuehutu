"""买家来信入站链路的回归锁：**买家说话 -> 落库 -> AI 真的被触发**。

**为什么需要这个文件**：`services.chats.handle_incoming()` 是全仓唯一能让 AI「因为
买家说话而开口」的入口，但在这次改动之前，全仓只有 `test_services_chats.py` 在直接调它
—— 真实链路（IM 推送 / HTTP 请求）根本没有接到它上面。结果是：会话流水线、AI 编排层、
多角色都在，但**没有任何真实事件会走到那里**，AI 永远不会回买家一句话。

本文件锁三件事：
1. **协议层分发点**：`xianyu.im.ImSession` 收到「不配对任何 pending 请求」的帧时，
   会交给服务层注册的 handler；`parse_incoming_message` 能从推送里抠出买家消息，
   且**认不出就返回 None**（不许凭空造会话）。
2. **幂等**：同一平台 `msg_id` 重复送达只落一条消息、**只触发一次 AI**。
3. **不炸**：编排层未就绪 / LLM 报错 / 字段缺失，一律 200 或规整的失败信封，绝不 500。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_chats_inbound.py"

**不打真实闲鱼平台**（协议层只验纯函数与回调分发，不开 WebSocket）、
**不调真实 LLM**（`llm.chat` 全程 mock）。
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.agents import llm, roles  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import chats  # noqa: E402
from backend.app.xianyu import im  # noqa: E402

ACCOUNT_ID = 'inbound_acc'
BUYER_ID = 'buyer_u1'
ITEM_ID = 'item_A'


def _fake_chat(text_by_role=None, default='（mock 回复）', calls=None):
    """假的 `llm.chat`：按 role 返回文本并记录调用（照抄 test_agents_cross 的用法）。"""
    text_by_role = text_by_role or {}

    async def _chat(messages, *, role=None, **kwargs):
        if calls is not None:
            calls.append({'role': role, 'messages': messages, 'kwargs': kwargs})
        text = text_by_role.get(role, f'{default}[{role}]')
        return {'text': text, 'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                                       'total_tokens': 2},
                'model': 'mock-model', 'provider_id': 1, 'tool_calls': [], 'raw': {}}

    return _chat


def _push_frame(msg_id='m-1', content='这个还在吗？', cid='conv-1', buyer_id=BUYER_ID,
                item_id=ITEM_ID):
    """造一帧像线上那样嵌套的 IM 买家消息推送。"""
    return {
        'headers': {'mid': ''},  # 平台主动推送没有 mid —— 正是要分发的那一类
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


class FakeRuntime:
    """假编排层：真实签名（async，只生成不发送），验证服务层对接没有歧义。"""

    def __init__(self):
        self.calls = []

    async def reply_for_message(self, conversation_id, message_id):
        self.calls.append((conversation_id, message_id))
        return {'ok': True, 'reply': '在的，可以拍下【假编排层】', 'role': 'item'}


class _TempAppMixin:
    """临时数据目录 + 干净的 conversations/messages（测试规则要求用 testenv）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        from backend.tests.testenv import use_temp_app_dir

        use_temp_app_dir(cls.tmp.name)
        roles.ensure_roles()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')
        db.execute('DELETE FROM agent_messages')

    def _conversation(self, mode='auto', agent_role='item'):
        return chats.ensure_conversation(
            ACCOUNT_ID, BUYER_ID, '买家小王', ITEM_ID, mode=mode, agent_role=agent_role,
        )


# --------------------------------------------------------------------------- 1. 帧解析


class ImFrameParseTest(_TempAppMixin, unittest.TestCase):
    """协议层纯函数：认得出买家消息，也认得出「这不是消息」。"""

    def test_parses_nested_push_frame(self):
        parsed = im.parse_incoming_message(_push_frame())
        self.assertEqual(parsed['msg_id'], 'm-1')
        self.assertEqual(parsed['buyer_id'], BUYER_ID)      # @goofish 已剥掉
        self.assertEqual(parsed['cid'], 'conv-1')
        self.assertEqual(parsed['content'], '这个还在吗？')
        self.assertEqual(parsed['item_id'], ITEM_ID)

    def test_parses_base64_custom_content(self):
        """自造帧（build_send_frame 用的就是 custom+base64）也要能解回来。"""
        frame = _push_frame()
        raw = json.dumps({'text': {'text': '包邮吗'}}, ensure_ascii=False)
        frame['body']['message']['content'] = {
            'custom': {'data': base64.b64encode(raw.encode('utf-8')).decode('ascii')}
        }
        self.assertEqual(im.parse_incoming_message(frame)['content'], '包邮吗')

    def test_non_message_frames_return_none(self):
        """心跳回帧 / 空 body / 非 dict：返回 None，不许造出 buyer_id 为空的会话。"""
        for payload in (
            None, '', 'pong', {}, {'headers': {}, 'body': []},
            {'body': {'message': {'cid': 'c', 'senderId': BUYER_ID, 'content': {}}}},
            {'body': {'message': {'cid': 'c', 'senderId': BUYER_ID,
                                 'content': {'text': {'text': '   '}}}}},
        ):
            self.assertIsNone(im.parse_incoming_message(payload), f'不该解析: {payload!r}')


class ImDispatchTest(_TempAppMixin, unittest.TestCase):
    """分发点：非配对帧 -> 注册的 handler；协议层不认识 services。"""

    def test_session_dispatch_calls_registered_handler(self):
        session = im.ImSession('cookie', 'token', 'seller_1')
        seen = []
        session.on_message(lambda payload: seen.append(payload))
        frame = _push_frame()
        self.assertEqual(session.dispatch(frame), 1)
        self.assertEqual(seen, [frame])

    def test_handler_exception_does_not_break_dispatch(self):
        """一个 handler 挂掉，其它 handler 和读循环都不能受影响。"""
        session = im.ImSession('cookie', 'token', 'seller_1')
        seen = []

        def boom(payload):
            raise RuntimeError('handler 挂了')

        session.on_message(boom)
        session.on_message(lambda payload: seen.append(payload))
        self.assertEqual(session.dispatch(_push_frame()), 1)  # boom 被跳过
        self.assertEqual(len(seen), 1)

    def test_read_loop_routes_unmatched_frame_to_dispatch(self):
        """读循环的真实分支：mid 配不上 pending -> 走 dispatch（不是丢掉）。"""
        import asyncio

        session = im.ImSession('cookie', 'token', 'seller_1')
        seen = []
        session.on_message(seen.append)

        class _FakeWs:
            def __init__(self):
                self._frames = iter([json.dumps(_push_frame(), ensure_ascii=False)])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._frames)
                except StopIteration:
                    raise StopAsyncIteration

        async def _run():
            session._ws = _FakeWs()
            task = asyncio.ensure_future(session._read_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['body']['message']['msgId'], 'm-1')

    def test_protocol_layer_does_not_import_services(self):
        """协议层不许 import services —— 分层靠服务层反向注入（`attach_im_session`）。"""
        import ast

        tree = ast.parse((REPO_ROOT / 'backend' / 'app' / 'xianyu' / 'im.py').read_text('utf-8'))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or '')
        self.assertFalse([name for name in imported if 'services' in name], imported)
        # 反向注入点确实在服务层，且只依赖协议层的公开回调 API
        self.assertTrue(callable(chats.attach_im_session))

    def test_service_handler_is_wired_to_parse_and_handle(self):
        """`im_handler_for` 造出的回调真的把帧喂进了 `handle_incoming`。"""
        runtime = FakeRuntime()
        with mock.patch.object(chats, '_load_runtime', return_value=runtime):
            result = chats.im_handler_for(ACCOUNT_ID)(_push_frame())

        self.assertEqual(result['duplicate'], False)
        self.assertEqual(result['message']['content'], '这个还在吗？')
        self.assertEqual(result['ai_reply']['content'], '在的，可以拍下【假编排层】')
        self.assertEqual(len(runtime.calls), 1)
        # 会话是按 (account_id, buyer_id) 现建的，买家 id 已剥掉 @goofish
        self.assertEqual(result['message']['conversation_id'],
                         chats.get_conversation(result['message']['conversation_id'])['id'])


# --------------------------------------------------------------------------- 2. 服务层幂等


class HandleIncomingTest(_TempAppMixin, unittest.TestCase):
    """服务层：幂等 / 模式 / 未就绪 / 报错。"""

    def test_duplicate_msg_id_lands_once_and_triggers_ai_once(self):
        conversation = self._conversation()
        runtime = FakeRuntime()
        with mock.patch.object(chats, '_load_runtime', return_value=runtime):
            first = chats.handle_incoming(conversation['id'], '在吗', msg_id='m-100')
            second = chats.handle_incoming(conversation['id'], '在吗', msg_id='m-100')

        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['message']['id'], first['message']['id'])  # 同一条
        self.assertIsNone(second['ai_reply'])
        self.assertEqual(len(runtime.calls), 1)  # AI 只被触发一次

        page = chats.get_messages(conversation['id'])
        roles_in_db = [m['sender'] for m in page['messages']]
        self.assertEqual(roles_in_db, ['buyer', 'ai:item'])  # 不是 4 条

    def test_different_msg_ids_are_not_confused(self):
        """`abc` 不能误命中 `xabc`（meta 粗筛之后还做了精确比对）。"""
        conversation = self._conversation(mode='manual')
        chats.handle_incoming(conversation['id'], '第一条', msg_id='xabc')
        result = chats.handle_incoming(conversation['id'], '第二条', msg_id='abc')
        self.assertFalse(result['duplicate'])
        self.assertEqual(len(chats.get_messages(conversation['id'])['messages']), 2)

    def test_no_msg_id_still_works_and_does_not_dedupe(self):
        """平台没给 msg_id 时退化成「每次都落」—— 不假装幂等。"""
        conversation = self._conversation(mode='manual')
        chats.handle_incoming(conversation['id'], '在吗')
        chats.handle_incoming(conversation['id'], '在吗')
        self.assertEqual(len(chats.get_messages(conversation['id'])['messages']), 2)

    def test_manual_mode_stores_message_without_ai(self):
        conversation = self._conversation(mode='manual')
        runtime = FakeRuntime()
        with mock.patch.object(chats, '_load_runtime', return_value=runtime):
            result = chats.handle_incoming(conversation['id'], '你好')
        self.assertIsNone(result['ai_reply'])
        self.assertEqual(runtime.calls, [])
        self.assertEqual(result['mode'], 'manual')

    def test_runtime_not_ready_still_stores_incoming(self):
        """编排层未就绪：买家消息照样落库，`ai_result` 为 None，不报错。"""
        conversation = self._conversation()
        with mock.patch.object(chats, '_load_runtime', return_value=None):
            result = chats.handle_incoming(conversation['id'], '还在吗')
        self.assertIsNone(result['ai_result'])
        self.assertIsNone(result['ai_reply'])
        self.assertEqual(result['message']['content'], '还在吗')

    def test_llm_error_is_soft(self):
        """LLM 抛 LLMError：信封里带 ai_result 失败信息，但买家消息不丢、函数不抛。"""
        conversation = self._conversation()

        async def _boom(*args, **kwargs):
            raise llm.LLMError('提供者未配置')

        with mock.patch.object(llm, 'chat', _boom):
            result = chats.handle_incoming(conversation['id'], '多少钱')
        self.assertIsNone(result['ai_reply'])
        self.assertFalse(result['ai_result']['success'])
        self.assertEqual(len(chats.get_messages(conversation['id'])['messages']), 1)

    def test_creates_conversation_from_account_and_buyer(self):
        """第一次来信：按 (account_id, buyer_id) 现建会话（IM 冷启动路径）。"""
        result = chats.handle_incoming(
            0, '你好', account_id=ACCOUNT_ID, peer_user_id=BUYER_ID,
            item_id=ITEM_ID, peer_nickname='小王',
        )
        conversation = chats.get_conversation(result['message']['conversation_id'])
        self.assertEqual(conversation['peer_user_id'], BUYER_ID)
        self.assertEqual(conversation['peer_nickname'], '小王')
        self.assertEqual(conversation['item_id'], ITEM_ID)
        self.assertEqual(conversation['mode'], 'auto')

    def test_missing_conversation_without_account_is_failure_envelope(self):
        result = chats.handle_incoming(9999, '你好')
        self.assertTrue(result['success'] is False)
        self.assertEqual(result['code'], 'not_found')


# --------------------------------------------------------------------------- 3. HTTP 端到端


class IncomingApiTest(_TempAppMixin, unittest.TestCase):
    """`POST /api/chats/incoming` 全程真实：路由 -> services -> runtime -> llm(mock)。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        db.execute('DELETE FROM providers')
        db.execute("INSERT INTO providers(id, name, base_url, api_key, default_model) "
                   "VALUES(1, 'mock', 'http://127.0.0.1:9/v1', 'sk-mock', 'mock-model')")

    def setUp(self):
        super().setUp()
        db.execute('DELETE FROM users')
        self.app_ctx = TestClient(create_app())
        self.client = self.app_ctx.__enter__()
        user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('inbound','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(user_id)}

    def tearDown(self):
        self.app_ctx.__exit__(None, None, None)
        db.execute('DELETE FROM users')

    def _post(self, payload):
        return self.client.post('/api/chats/incoming', json=payload, headers=self.headers)

    def test_buyer_message_triggers_ai_reply_end_to_end(self):
        """核心断言：一个 HTTP 请求 -> 买家消息落库 -> **AI 回复也被落库**。"""
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat({'item': '在的，现货【AI】'}, calls=calls)):
            resp = self._post({
                'account_id': ACCOUNT_ID, 'buyer_id': BUYER_ID, 'item_id': ITEM_ID,
                'content': '这个还在吗？', 'msg_id': 'api-m-1',
            })

        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'], body)
        data = body['data']
        self.assertEqual(data['message']['direction'], 'in')
        self.assertEqual(data['message']['sender'], 'buyer')
        self.assertEqual(data['ai_reply']['sender'], 'ai:item')     # item_id 给了 -> item 角色
        self.assertIn('【AI】', data['ai_reply']['content'])
        self.assertEqual([c['role'] for c in calls], ['item'])
        self.assertEqual(data['duplicate'], False)

        # 落库可查：GET /messages 里能看到一来一往
        page = self.client.get(
            f"/api/chats/{data['message']['conversation_id']}/messages", headers=self.headers,
        ).json()
        self.assertEqual([m['direction'] for m in page['data']['messages']], ['in', 'out'])

    def test_role_falls_back_to_cs_without_item_id(self):
        """没给 item_id -> 会话建出来是 cs 角色，AI 也用 cs 人设（§4 角色路由）。"""
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat({'cs': '您好【客服】'}, calls=calls)):
            resp = self._post({'account_id': ACCOUNT_ID, 'buyer_id': 'buyer_cs',
                               'content': '发票能开吗'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['ai_reply']['sender'], 'ai:cs')
        self.assertEqual([c['role'] for c in calls], ['cs'])

    def test_repeat_same_msg_id_is_idempotent_over_http(self):
        """同一条平台消息重投两次：第二次 `duplicate: true`，消息数不变、AI 不再触发。"""
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls=calls)):
            first = self._post({'account_id': ACCOUNT_ID, 'buyer_id': BUYER_ID,
                                'item_id': ITEM_ID, 'content': '在吗', 'msg_id': 'api-m-2'})
            second = self._post({'account_id': ACCOUNT_ID, 'buyer_id': BUYER_ID,
                                 'item_id': ITEM_ID, 'content': '在吗', 'msg_id': 'api-m-2'})

        self.assertFalse(first.json()['data']['duplicate'])
        self.assertTrue(second.json()['data']['duplicate'])
        self.assertEqual(first.json()['data']['message']['id'],
                         second.json()['data']['message']['id'])
        self.assertEqual(len(calls), 1)

        cid = first.json()['data']['message']['conversation_id']
        page = self.client.get(f'/api/chats/{cid}/messages', headers=self.headers).json()
        self.assertEqual(len(page['data']['messages']), 2)  # 一条买家 + 一条 AI

    def test_manual_takeover_stops_ai(self):
        """人工接管（mode=manual）后，买家再说话 AI 不插嘴。"""
        conv = self._conversation(mode='manual')
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls=calls)):
            resp = self._post({'account_id': ACCOUNT_ID, 'conversation_id': conv['id'],
                               'buyer_id': BUYER_ID, 'content': '在吗'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()['data']['ai_reply'])
        self.assertEqual(calls, [])

    def test_direction_out_is_manual_reply(self):
        """`direction=out` 表示卖家自己发的消息 -> 走 send_manual。"""
        conv = self._conversation()
        resp = self._post({'account_id': ACCOUNT_ID, 'conversation_id': conv['id'],
                           'content': '我看看', 'direction': 'out'})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['message']['direction'], 'out')
        self.assertEqual(data['message']['sender'], 'human')
        self.assertEqual(data['mode'], 'manual')

    def test_validation_returns_envelope_not_500(self):
        """缺字段 / 乱给 direction：规整 400 信封，不是 500、也不是裸 detail。"""
        for payload, code in (
            ({'account_id': ACCOUNT_ID, 'buyer_id': BUYER_ID}, 'invalid_fields'),      # 缺 content
            ({'account_id': ACCOUNT_ID, 'content': 'x'}, 'invalid_fields'),            # 缺 buyer_id
            ({'buyer_id': BUYER_ID, 'content': 'x'}, 'invalid_fields'),                # 缺 account_id
            ({'account_id': ACCOUNT_ID, 'conversation_id': 1, 'content': 'x',
              'direction': 'sideways'}, 'invalid_fields'),                            # direction 非法
        ):
            resp = self._post(payload)
            self.assertEqual(resp.status_code, 400, resp.text)
            body = resp.json()
            self.assertEqual(body['success'], False)
            self.assertEqual(body['code'], code)

    def test_unknown_conversation_returns_not_found_envelope(self):
        """给了不存在的 conversation_id 就 404，**不会**顺手新建一个（typo 不该静默造会话）。"""
        resp = self._post({'account_id': ACCOUNT_ID, 'conversation_id': 424242,
                           'buyer_id': BUYER_ID, 'content': 'x'})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(chats.list_conversations()['total'], 0)

    def test_llm_failure_does_not_500(self):
        """LLM 报错：HTTP 200 + 买家消息仍在（AI 只是没接上话）。"""
        async def _boom(*args, **kwargs):
            raise llm.LLMError('提供者未配置')

        with mock.patch.object(llm, 'chat', _boom):
            resp = self._post({'account_id': ACCOUNT_ID, 'buyer_id': 'buyer_llm', 'content': '在吗'})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertIsNone(data['ai_reply'])
        self.assertFalse(data['ai_result']['success'])
        self.assertEqual(data['message']['content'], '在吗')

    def test_runtime_not_ready_does_not_500(self):
        """编排层整个不可用：接口仍然 200，只落买家消息。"""
        with mock.patch.object(chats, '_load_runtime', return_value=None):
            resp = self._post({'account_id': ACCOUNT_ID, 'buyer_id': 'buyer_nr', 'content': '在吗'})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()['data']['ai_reply'])

    def test_requires_auth(self):
        resp = self.client.post('/api/chats/incoming', json={'account_id': ACCOUNT_ID,
                                                            'buyer_id': BUYER_ID, 'content': 'x'})
        self.assertIn(resp.status_code, (401, 403), resp.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
