"""多 AI 编排 —— 角色判定（`bargain` / `vision` 死角色修复，审计 AI-2）测试。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_agents_roles.py"

**修复前**：`backend/app/agents/roles.py::role_for_conversation` 的规则 1 是
「`conversations.agent_role` 合法就用它」，而 `services/chats.py::default_agent_role()`
在**建会话时**就把每个会话写成了 `item`/`cs` —— 规则 1 恒命中，规则 2/3
（`vision`/`bargain`）永远执行不到。角色表里 `bargain`/`vision` 两行配置形同虚设。

**修复后**：把 `agent_role` 分成两类 ——
- 自动兜底 `item`/`cs`：允许被消息内容（图片 / 砍价话术）进一步特化；
- 显式钉住 `master`/`bargain`/`cross`/`vision`：消息内容无权推翻。

本文件除了纯函数矩阵，还**端到端**跑 `runtime.reply_for_message()`（打桩 LLM），
把被打桩 LLM 真实收到的 `role` 与 `system` 打印出来 —— 这是「真被选中」的证据，
纯函数测试只能证明判定函数，不能证明调用链用的是它。

全部离线，不打真实 LLM API。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agents import llm, roles, runtime  # noqa: E402
from backend.app.core import config, db, events  # noqa: E402

ACCOUNT_ID = 'acc1'
ITEM_ID = 'itA'
BUYER_ID = 'buyer1'


def _fake_chat(calls=None):
    """打桩 LLM：把收到的 role / system 存下来（**不发任何网络请求**）。"""
    async def _chat(messages, *, role=None, **kwargs):
        if calls is not None:
            calls.append({'role': role, 'messages': messages, 'kwargs': kwargs})
        return {
            'text': f'（mock 回复 by {role}）',
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            'model': 'mock-model',
            'provider_id': 1,
            'tool_calls': [],
            'raw': {},
        }

    return _chat


class RolesTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()  # 连接是线程本地缓存的，每个测试方法都要重新绑定临时库
        db.execute('DELETE FROM ai_roles')
        db.execute('DELETE FROM ai_prompts')
        db.execute('DELETE FROM providers')
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM conversations')
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM agent_messages')
        db.execute('DELETE FROM agent_runs')
        db.execute("DELETE FROM settings WHERE key = 'ai.default_provider_id'")
        roles.ensure_roles()
        self.provider_id = int(db.execute(
            "INSERT INTO providers(name, base_url, api_key, default_model, enabled, is_default) "
            "VALUES('mock','http://127.0.0.1:9/v1',?,'mock-model',1,1)",
            (__import__('backend.app.core.crypto', fromlist=['crypto']).encrypt('sk-mock'),),
        ))

    def add_item(self, item_id=ITEM_ID, title='红色连衣裙 全新', account_id=ACCOUNT_ID, status='on_sale'):
        db.execute(
            'INSERT INTO items(account_id, item_id, title, price, status) VALUES(?,?,?,?,?)',
            (account_id, item_id, title, 50, status),
        )

    def add_conversation(self, item_id=ITEM_ID, agent_role='item'):
        """模拟 `services/chats.py` 建会话：**它总是写死一个合法的 agent_role**。"""
        return int(db.execute(
            'INSERT INTO conversations(account_id, peer_user_id, peer_nickname, item_id, mode, agent_role) '
            "VALUES(?,?,?,?,'auto',?)",
            (ACCOUNT_ID, BUYER_ID, '买家小王', item_id, agent_role),
        ))

    def add_message(self, conversation_id, content, meta=None):
        return int(db.execute(
            'INSERT INTO messages(conversation_id, direction, sender, content, msg_type, meta) '
            "VALUES(?,'in','buyer',?,'text',?)",
            (conversation_id, content, meta or ''),
        ))


# --------------------------------------------------------------------------
# 一、纯函数矩阵：哪些 agent_role 会被消息内容特化
# --------------------------------------------------------------------------

class RoleMatrixTest(RolesTestBase):
    def test_contract_roles_and_split(self):
        self.assertEqual(roles.ROLES, ('master', 'item', 'cs', 'bargain', 'cross', 'vision'))
        # 自动兜底与显式钉住必须正好把 6 个角色分完
        self.assertEqual(roles.GENERIC_ROLES, ('item', 'cs'))
        self.assertEqual(roles.PINNED_ROLES, ('master', 'bargain', 'cross', 'vision'))
        self.assertEqual(sorted(set(roles.GENERIC_ROLES) | set(roles.PINNED_ROLES)), sorted(roles.ROLES))
        self.assertEqual(set(roles.GENERIC_ROLES) & set(roles.PINNED_ROLES), set())

    def test_auto_default_item_is_specialized_to_bargain(self):
        """**这就是死角色的修复点**：`services/chats.py` 写的 `agent_role='item'`
        不能挡住议价规则。"""
        conv = {'agent_role': 'item', 'item_id': ITEM_ID}
        for text in ('能便宜点吗', '这个最低多少', '包邮吗，能少点不'):
            with self.subTest(text=text):
                self.assertEqual(roles.role_for_conversation(conv, text), 'bargain')
        # 普通问句仍然是商品客服
        self.assertEqual(roles.role_for_conversation(conv, '什么时候发货'), 'item')
        self.assertEqual(roles.role_for_conversation(conv, '有货吗'), 'item')

    def test_auto_default_item_is_specialized_to_vision(self):
        conv = {'agent_role': 'item', 'item_id': ITEM_ID}
        image = {'content': '看看这个', 'meta': '{"image_url": "http://x/a.jpg"}'}
        self.assertEqual(roles.role_for_conversation(conv, image), 'vision')
        # 图片优先于砍价
        both = {'content': '能便宜点吗', 'meta': {'image': 'http://x/b.jpg'}}
        self.assertEqual(roles.role_for_conversation(conv, both), 'vision')

    def test_auto_default_cs_is_specialized_too(self):
        """没绑定商品的会话被写成 `cs`，同样不该挡住图片/议价规则。"""
        conv = {'agent_role': 'cs', 'item_id': ''}
        self.assertEqual(roles.role_for_conversation(conv, '能便宜点吗'), 'bargain')
        self.assertEqual(
            roles.role_for_conversation(conv, {'content': '看看', 'meta': {'image_url': 'http://x/c.png'}}),
            'vision',
        )
        # 既不是图片也不是砍价 → 保持 cs（而不是掉到 item）
        self.assertEqual(roles.role_for_conversation(conv, '什么时候发货'), 'cs')

    def test_pinned_roles_win_over_message(self):
        for role in ('master', 'bargain', 'cross', 'vision'):
            with self.subTest(role=role):
                conv = {'agent_role': role, 'item_id': ITEM_ID}
                self.assertEqual(roles.role_for_conversation(conv, '能便宜点吗'), role)
                self.assertEqual(
                    roles.role_for_conversation(conv, {'content': 'x', 'meta': {'image_url': 'http://x/1.jpg'}}),
                    role,
                )

    def test_fallbacks_without_agent_role(self):
        self.assertEqual(roles.role_for_conversation({'agent_role': '', 'item_id': ITEM_ID}), 'item')
        self.assertEqual(roles.role_for_conversation({'agent_role': '', 'item_id': ''}), 'cs')
        self.assertEqual(roles.role_for_conversation(None), 'cs')
        # 非法 agent_role 当作没写
        self.assertEqual(roles.role_for_conversation({'agent_role': 'nonsense', 'item_id': ITEM_ID}), 'item')
        self.assertEqual(roles.role_for_conversation({'agent_role': 'nonsense', 'item_id': ''}), 'cs')

    def test_no_message_keeps_old_behaviour(self):
        """不传 message 时行为与旧版完全一致（回归锁）。"""
        self.assertEqual(roles.role_for_conversation({'agent_role': 'item', 'item_id': ITEM_ID}), 'item')
        self.assertEqual(roles.role_for_conversation({'agent_role': '', 'item_id': ITEM_ID}), 'item')


# --------------------------------------------------------------------------
# 二、端到端：让被打桩 LLM 亲口说出它收到的 role / system
# --------------------------------------------------------------------------

class RoleEndToEndTest(RolesTestBase):
    def test_bargain_reply_uses_bargain_role_and_prompt(self):
        """会话 agent_role='item'（chats.py 的写法）+ 砍价话术 → 真选 `bargain`。"""
        self.add_item()
        conv_id = self.add_conversation(agent_role='item')
        msg_id = self.add_message(conv_id, '老板能便宜点吗，最低多少')

        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls)):
            result = asyncio.run(runtime.reply_for_message(conv_id, msg_id))

        print('\n[A2 证据 · bargain] reply_for_message -> role =', result.get('role'))
        print('[A2 证据 · bargain] 打桩 LLM 收到的 role =', calls[0]['role'])
        print('[A2 证据 · bargain] system 前 60 字 =', calls[0]['messages'][0]['content'][:60])

        self.assertTrue(result['ok'], result)
        self.assertEqual(result['role'], 'bargain')
        self.assertEqual(calls[0]['role'], 'bargain')
        # system 是 bargain 角色的提示词（不是 item 的）
        system = calls[0]['messages'][0]['content']
        self.assertEqual(system, roles.default_prompt('bargain'))
        self.assertNotEqual(system, roles.default_prompt('item'))
        self.assertEqual(calls[0]['kwargs'].get('conversation_id'), conv_id)

    def test_vision_reply_uses_vision_role_and_multimodal_payload(self):
        """会话 agent_role='item' + 消息带 image_url → 真选 `vision`。"""
        self.add_item()
        conv_id = self.add_conversation(agent_role='item')
        msg_id = self.add_message(conv_id, '这个和图片一样吗',
                                  meta='{"image_url": "http://127.0.0.1:9/a.jpg"}')

        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls)):
            result = asyncio.run(runtime.reply_for_message(conv_id, msg_id))

        print('\n[A2 证据 · vision] reply_for_message -> role =', result.get('role'))
        print('[A2 证据 · vision] 打桩 LLM 收到的 role =', calls[0]['role'])
        print('[A2 证据 · vision] system 前 60 字 =', calls[0]['messages'][0]['content'][:60])

        self.assertTrue(result['ok'], result)
        self.assertEqual(result['role'], 'vision')
        self.assertEqual(calls[0]['role'], 'vision')
        self.assertEqual(calls[0]['messages'][0]['content'], roles.default_prompt('vision'))
        # 图片走多模态 content list
        last = calls[0]['messages'][-1]
        self.assertIsInstance(last['content'], list)
        self.assertEqual(last['content'][1]['type'], 'image_url')
        self.assertEqual(last['content'][1]['image_url']['url'], 'http://127.0.0.1:9/a.jpg')

    def test_item_reply_still_item_for_plain_question(self):
        """回归锁：普通问句不许被 A2 的改动带跑。"""
        self.add_item()
        conv_id = self.add_conversation(agent_role='item')
        msg_id = self.add_message(conv_id, '什么时候发货')

        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls)):
            result = asyncio.run(runtime.reply_for_message(conv_id, msg_id))
        self.assertEqual(result['role'], 'item')
        self.assertEqual(calls[0]['role'], 'item')


# --------------------------------------------------------------------------
# 三、角色配置读写 + 前端刷新事件
# --------------------------------------------------------------------------

class RoleConfigTest(RolesTestBase):
    def test_ensure_roles_is_idempotent(self):
        roles.ensure_roles()
        roles.ensure_roles()
        rows = list(db.query('SELECT role FROM ai_roles'))
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['role'] for r in rows}, set(roles.ROLES))

    def test_get_and_update_role(self):
        before = roles.get_role('bargain')
        self.assertEqual(before['label'], '议价 AI')
        self.assertTrue(before['default_prompt'])
        self.assertAlmostEqual(before['temperature'], 0.8)

        after = roles.update_role('bargain', temperature=0.15, system_prompt='只让 5 块',
                                  model='bargain-model')
        self.assertAlmostEqual(after['temperature'], 0.15)
        self.assertEqual(after['system_prompt'], '只让 5 块')
        self.assertEqual(after['model'], 'bargain-model')
        self.assertTrue(after['enabled'])  # 没传的字段保持原值

        with self.assertRaises(ValueError):
            roles.get_role('not_a_role')
        with self.assertRaises(ValueError):
            roles.update_role('not_a_role', temperature=1)

    def test_role_temperature_falls_back_to_default(self):
        self.assertAlmostEqual(roles.role_temperature('master'), 0.3)
        roles.update_role('master', temperature=0.9)
        self.assertAlmostEqual(roles.role_temperature('master'), 0.9)
        # 表里没有的行（被清掉）也要给默认值
        db.execute("DELETE FROM ai_roles WHERE role = 'master'")
        self.assertAlmostEqual(roles.role_temperature('master'), 0.3)

    def test_role_provider_change_publishes_providers_changed(self):
        """角色↔提供者映射变了要发 WS 事件，前端「AI 设置」页靠它静默重拉。"""
        topics = []
        original = events.publish

        def spy(topic, payload=None):
            topics.append((topic, payload or {}))
            return original(topic, payload or {})

        other = int(db.execute(
            "INSERT INTO providers(name, base_url, api_key, default_model) "
            "VALUES('mock2','http://127.0.0.1:9/v1','','m2')"
        ))
        with mock.patch.object(events, 'publish', spy):
            roles.update_role('item', provider_id=self.provider_id)
            roles.update_role('item', system_prompt='只是改文案，不该发事件')
            roles.update_role('item', provider_id=other)

        changed = [p for t, p in topics if t == 'providers.changed']
        self.assertEqual(len(changed), 2, topics)
        self.assertEqual(changed[0]['action'], 'roles')
        self.assertEqual(changed[0]['role'], 'item')
        self.assertEqual(changed[0]['provider_id'], self.provider_id)
        self.assertEqual(changed[1]['provider_id'], other)
        # payload 里不许出现任何 Key
        for payload in changed:
            self.assertNotIn('api_key', payload)
            self.assertNotIn('sk-mock', str(payload))

    def test_publish_failure_does_not_break_update(self):
        with mock.patch.object(events, 'publish', side_effect=RuntimeError('bus down')):
            data = roles.update_role('cs', provider_id=self.provider_id)
        self.assertEqual(data['provider_id'], self.provider_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
