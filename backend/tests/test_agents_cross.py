"""跨商品协查测试（**全部 mock LLM，绝不打真实 API**）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_agents_cross.py"

覆盖：
- ask/answer 两条都落 agent_messages，且 from/to 正确
- 深度超过 2 被拒
- 排除当前会话自己的商品
- 超时 / LLM 报错不炸链路
- 主管 AI 工具调用（run_master）与 master_chat 不在自己的工具集里
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.agents import llm, roles, runtime, tools  # noqa: E402
from backend.app.core import config, db  # noqa: E402


def _fake_chat(text_by_role=None, default='（mock 回复）', calls=None):
    """造一个假的 llm.chat：按 role 返回不同文本，并记录调用。"""
    text_by_role = text_by_role or {}

    async def _chat(messages, *, role=None, **kwargs):
        if calls is not None:
            calls.append({'role': role, 'messages': messages, 'kwargs': kwargs})
        text = text_by_role.get(role, f'{default}[{role}]')
        return {'text': text, 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
                'model': 'mock-model', 'provider_id': 1, 'tool_calls': [], 'raw': {}}

    return _chat


class CrossAskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()
        roles.ensure_roles()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()  # 连接是线程本地缓存的，每个测试方法都要重新绑定临时库
        db.execute('DELETE FROM agent_messages')
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')
        db.execute('DELETE FROM agent_runs')
        db.execute("UPDATE ai_roles SET system_prompt = '', provider_id = NULL, model = ''")
        db.execute('DELETE FROM ai_prompts')
        db.execute('DELETE FROM providers')
        db.execute(
            "INSERT INTO providers(id, name, base_url, api_key, default_model, enabled, is_default) "
            "VALUES(1,'mock','http://127.0.0.1:9/v1','sk-mock','mock-model',1,1)")
        db.execute(
            "INSERT INTO items(account_id, item_id, title, price, stock) "
            "VALUES('acc1','A','红色连衣裙',50,1)")
        db.execute(
            "INSERT INTO items(account_id, item_id, title, price, stock) "
            "VALUES('acc1','B','蓝色牛仔裤 全新',120,2)")

    def messages(self):
        rows = db.query('SELECT * FROM agent_messages ORDER BY id')
        return [dict(r) for r in rows]

    def test_ask_and_answer_land_in_agent_messages(self):
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat({'item': '牛仔裤 120 元，全新', 'cross': '蓝色牛仔裤 120 元，全新包邮'}, calls=calls)):
            text = asyncio.run(runtime.cross_ask(
                'item:A', '你们那条蓝色牛仔裤多少钱', exclude_item_id='A', conversation_id=7))

        self.assertIn('120', text)
        rows = self.messages()
        kinds = [r['kind'] for r in rows]
        self.assertIn('ask', kinds)
        self.assertIn('answer', kinds)

        # 第一条：item:A → cross（发起）
        first = rows[0]
        self.assertEqual((first['from_agent'], first['to_agent'], first['kind']),
                         ('item:A', 'cross', 'ask'))
        self.assertEqual(first['conversation_id'], 7)

        # 第二条：cross → item:B（转问目标商品）
        to_item = [r for r in rows if r['to_agent'] == 'item:B']
        self.assertTrue(to_item, f'没有向 item:B 发 ask: {rows}')
        self.assertEqual(to_item[0]['kind'], 'ask')
        self.assertEqual(json.loads(to_item[0]['payload'])['item_id'], 'B')

        # 第三条：item:B → cross（商品 AI 答复）
        from_item = [r for r in rows if r['from_agent'] == 'item:B' and r['kind'] == 'answer']
        self.assertTrue(from_item, f'item:B 没有回 answer: {rows}')

        # 最后：cross → item:A（汇总回原 AI）
        last = rows[-1]
        self.assertEqual((last['from_agent'], last['to_agent'], last['kind']),
                         ('cross', 'item:A', 'answer'))
        self.assertIn('120', json.loads(last['payload'])['answer'])

    def test_excludes_own_item(self):
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls=calls)):
            asyncio.run(runtime.cross_ask(
                'item:A', '红色连衣裙和蓝色牛仔裤哪个更划算', exclude_item_id='A', conversation_id=1))
        rows = self.messages()
        # 问题同时命中 A 和 B，但当前会话自己就是 A，绝不能**转问** item:A
        # （cross 最后把 answer 汇总回 item:A 是正常的）
        self.assertFalse([r for r in rows if r['to_agent'] == 'item:A' and r['kind'] == 'ask'], rows)
        self.assertTrue([r for r in rows if r['to_agent'] == 'item:B'], rows)

    def test_candidate_items_preferred(self):
        with mock.patch.object(llm, 'chat', _fake_chat()):
            asyncio.run(runtime.cross_ask(
                'item:A', '这个有货吗', exclude_item_id='A', conversation_id=1, candidate_items=['B']))
        rows = self.messages()
        self.assertTrue([r for r in rows if r['to_agent'] == 'item:B'], rows)

    def test_depth_limit_rejected(self):
        with mock.patch.object(llm, 'chat', _fake_chat()):
            with self.assertRaises(ValueError):
                asyncio.run(runtime.cross_ask('item:A', 'x', depth=3))
        self.assertFalse([r for r in self.messages() if r['kind'] == 'ask'])

    def test_depth_limit_boundary_ok(self):
        with mock.patch.object(llm, 'chat', _fake_chat()):
            text = asyncio.run(runtime.cross_ask('item:A', '蓝色牛仔裤多少钱', exclude_item_id='A', depth=2))
        self.assertTrue(text)
        # 深度已经 2，不能再往下递归，但 cross→item:A 的 answer 必须落库
        rows = self.messages()
        self.assertEqual(rows[-1]['kind'], 'answer')
        self.assertEqual(rows[-1]['to_agent'], 'item:A')

    def test_no_match_returns_notify(self):
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls=calls)):
            text = asyncio.run(runtime.cross_ask('item:A', '完全不存在的商品名', exclude_item_id='A'))
        self.assertTrue(text)
        self.assertEqual(calls, [])  # 没匹配到商品就不该调 LLM
        rows = self.messages()
        self.assertEqual([r['kind'] for r in rows], ['ask', 'notify'])

    def test_llm_failure_does_not_break_chain(self):
        async def boom(messages, **kwargs):
            raise llm.LLMError('HTTP 500: 模型炸了')

        with mock.patch.object(llm, 'chat', boom):
            text = asyncio.run(runtime.cross_ask(
                'item:A', '蓝色牛仔裤多少钱', exclude_item_id='A', conversation_id=3))
        self.assertTrue(text)  # 仍然给原 AI 一句能用的兜底话术
        rows = self.messages()
        self.assertTrue(any(r['kind'] == 'notify' for r in rows), rows)
        self.assertEqual(rows[-1]['kind'], 'answer')
        self.assertEqual(rows[-1]['to_agent'], 'item:A')

    def test_timeout_is_contained(self):
        async def slow(messages, **kwargs):
            await asyncio.sleep(5)
            return {'text': 'too late'}

        original = runtime.CROSS_TIMEOUT
        runtime.CROSS_TIMEOUT = 0.05
        try:
            with mock.patch.object(llm, 'chat', slow):
                text = asyncio.run(runtime.cross_ask(
                    'item:A', '蓝色牛仔裤多少钱', exclude_item_id='A'))
        finally:
            runtime.CROSS_TIMEOUT = original
        self.assertTrue(text)
        self.assertTrue(any(r['kind'] == 'notify' for r in self.messages()))

    def test_match_items_scores_by_title(self):
        matched = runtime.match_items('蓝色牛仔裤还有吗', exclude_item_id='A')
        self.assertEqual([m['item_id'] for m in matched], ['B'])
        self.assertEqual(runtime.match_items('蓝色牛仔裤还有吗', exclude_item_id='B'), [])

    def test_match_items_regression_chinese_substring(self):
        """回归锁：中文没有空格，买家问「这条牛仔裤多少钱」必须命中标题「蓝色牛仔裤 全新」。

        老实现按空格/标点切词，标题只切出 `['蓝色牛仔裤']` 一个 token，整串不在问句里
        → 命中 0 → 商品 A 的 AI 永远不会去问商品 B 的 AI（用户核心诉求）。
        """
        matched = runtime.match_items('这条牛仔裤多少钱', exclude_item_id='A')
        self.assertEqual([m['item_id'] for m in matched], ['B'],
                         f'中文子串匹配回归了：{matched}')

        # 同一个问题，从 B 的会话里问出去时，绝不能把 B 自己再匹配回来
        self.assertEqual(runtime.match_items('这条牛仔裤多少钱', exclude_item_id='B'), [])

    def test_match_items_regression_excludes_self_only(self):
        """排除自身商品后仍能命中对方 —— 这正是「A 的 AI 问 B 的 AI」的最小前提。"""
        self.assertEqual(runtime.match_items('牛仔裤', exclude_item_id='A')[0]['item_id'], 'B')
        self.assertEqual(runtime.match_items('连衣裙', exclude_item_id='B')[0]['item_id'], 'A')
        # 两个都不排除时两条都在
        both = runtime.match_items('红色连衣裙 蓝色牛仔裤', exclude_item_id=None)
        self.assertEqual(sorted(m['item_id'] for m in both), ['A', 'B'])

    def test_match_items_ignores_generic_words(self):
        """「全新/包邮」这类每个商品都有的词不能把无关商品算成命中。"""
        self.assertEqual(runtime.match_items('全新包邮吗', exclude_item_id='A'), [])

    def test_match_items_ignores_condition_words(self):
        """回归锁：纯问成色的消息不能匹到商品标题里的成色词。

        买家只问一句「是九成新吗」，标题「二手九成新运动鞋」里的 `九成/成新` 两个
        2-gram 会凑满阈值 → 为一句不含任何商品名的问句发起跨商品协查（同时误命中多个
        商品）。根因是停用词**只清洗标题、不清洗问题**，且成色词不在停用词表里。
        """
        db.execute(
            "INSERT INTO items(account_id, item_id, title, price, stock) "
            "VALUES('acc1','C','二手九成新运动鞋',80,1)")
        try:
            self.assertEqual(runtime.match_items('是九成新吗', exclude_item_id='A'), [],
                             '成色词又变成命中了：纯问成色不该触发跨商品协查')
            self.assertEqual(runtime.match_items('这个几成新', exclude_item_id='A'), [])
            # 反向对照：问商品名时仍然要命中（别把停用词清洗做成「一律不命中」）
            matched = runtime.match_items('这双运动鞋还有吗', exclude_item_id='A')
            self.assertEqual([m['item_id'] for m in matched], ['C'], f'正常匹配被误伤：{matched}')
        finally:
            db.execute("DELETE FROM items WHERE item_id = 'C'")

    def test_recent_agent_messages_limit_and_filter(self):
        db.execute("INSERT INTO agent_messages(conversation_id, from_agent, to_agent, kind) "
                   "VALUES(1,'a','b','ask')")
        db.execute("INSERT INTO agent_messages(conversation_id, from_agent, to_agent, kind) "
                   "VALUES(2,'a','b','ask')")
        self.assertEqual(len(runtime.recent_agent_messages(limit=10, conversation_id=1)), 1)

    # ---------------- AI-1：跨商品协查必须按账号隔离 ----------------

    def add_foreign_item(self, item_id='F', title='苹果 iPhone 13 256G 黑色 全新', account_id='acc2'):
        db.execute(
            'INSERT INTO items(account_id, item_id, title, price, stock) VALUES(?,?,?,?,1)',
            (account_id, item_id, title, 3999),
        )

    def test_match_items_filters_by_account(self):
        """**AI-1**：候选集原先 `SELECT ... FROM items ORDER BY id DESC LIMIT 500`
        完全没有 `WHERE account_id`，别的闲鱼号在卖什么会泄漏给当前账号的买家。"""
        self.add_foreign_item()
        # 不传 account_id：保持旧行为（现有调用方/测试依赖它，不能砸）
        loose = [m['item_id'] for m in runtime.match_items('苹果 iPhone 13 内存多大', exclude_item_id='A')]
        self.assertIn('F', loose)

        # 传了 account_id：跨账号商品必须消失
        scoped = runtime.match_items('苹果 iPhone 13 内存多大', exclude_item_id='A', account_id='acc1')
        self.assertEqual(scoped, [], f'跨账号命中了: {scoped}')

        # 本账号的商品照常命中，别修成「一律不匹配」
        same = runtime.match_items('蓝色牛仔裤还有吗', exclude_item_id='A', account_id='acc1')
        self.assertEqual([m['item_id'] for m in same], ['B'])

    def test_match_items_result_carries_account(self):
        matched = runtime.match_items('蓝色牛仔裤还有吗', exclude_item_id='A', account_id='acc1')
        self.assertEqual(matched[0]['account_id'], 'acc1')

    def test_cross_ask_filters_by_account(self):
        """`cross_ask` 也要透传 account_id，不能只在匹配函数里生效。"""
        self.add_foreign_item()
        with mock.patch.object(llm, 'chat', _fake_chat()):
            text = asyncio.run(runtime.cross_ask(
                'item:A', '苹果 iPhone 13 还有货吗', exclude_item_id='A',
                account_id='acc1', conversation_id=5))
        self.assertTrue(text)
        rows = self.messages()
        self.assertFalse([r for r in rows if r['to_agent'] == 'item:F'], f'问到别的账号去了: {rows}')
        self.assertEqual([r['kind'] for r in rows], ['ask', 'notify'], rows)

    # ---------------- 下架/已售商品不参与协查 ----------------

    def test_match_items_skips_unlisted_items(self):
        for status in ('sold', 'delisted', 'deleted', 'offline', '下架', '已售'):
            with self.subTest(status=status):
                db.execute("DELETE FROM items WHERE item_id = 'U'")
                db.execute(
                    "INSERT INTO items(account_id, item_id, title, price, stock, status) "
                    "VALUES('acc1','U','限量版帆布鞋 全新',99,0,?)", (status,))
                self.assertEqual(
                    runtime.match_items('限量版帆布鞋还有吗', exclude_item_id='A', account_id='acc1'), [],
                    f'status={status} 的下架商品不该参与协查')
        db.execute("DELETE FROM items WHERE item_id = 'U'")

    def test_match_items_keeps_unknown_and_on_sale(self):
        """`items.status` 默认值是 'unknown'，不能因为过滤 status 就把默认行全筛掉。"""
        for status in ('unknown', 'on_sale'):
            with self.subTest(status=status):
                db.execute("DELETE FROM items WHERE item_id = 'U'")
                db.execute(
                    "INSERT INTO items(account_id, item_id, title, price, stock, status) "
                    "VALUES('acc1','U','限量版帆布鞋 全新',99,1,?)", (status,))
                matched = runtime.match_items('限量版帆布鞋还有吗', exclude_item_id='A', account_id='acc1')
                self.assertEqual([m['item_id'] for m in matched], ['U'], f'status={status} 被误杀')
        db.execute("DELETE FROM items WHERE item_id = 'U'")

    def test_match_items_skips_unlisted_even_without_account(self):
        """不传 account_id 时也要过滤下架商品（那条 SQL 分支同样被修了）。"""
        db.execute(
            "INSERT INTO items(account_id, item_id, title, price, stock, status) "
            "VALUES('acc1','U','限量版帆布鞋 全新',99,0,'sold')")
        try:
            self.assertEqual(runtime.match_items('限量版帆布鞋还有吗', exclude_item_id='A'), [])
        finally:
            db.execute("DELETE FROM items WHERE item_id = 'U'")

    def test_match_items_candidate_items_still_filtered(self):
        """显式传 candidate_items 的分支也不能漏掉账号过滤。"""
        self.add_foreign_item()
        scoped = runtime.match_items('随便什么', candidate_items=['B', 'F'], account_id='acc1')
        self.assertEqual([m['item_id'] for m in scoped], ['B'], f'candidate 分支漏了账号过滤: {scoped}')
        # 切到 acc2 就应该只剩 F
        other = runtime.match_items('随便什么', candidate_items=['B', 'F'], account_id='acc2')
        self.assertEqual([m['item_id'] for m in other], ['F'], other)
        # 不传 account_id：旧行为，两个账号的都返回
        loose = runtime.match_items('随便什么', candidate_items=['B', 'F'])
        self.assertEqual(sorted(m['item_id'] for m in loose), ['B', 'F'])


class ReplyForMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()
        roles.ensure_roles()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        db.execute('DELETE FROM agent_messages')
        db.execute('DELETE FROM messages')
        db.execute('DELETE FROM conversations')
        db.execute('DELETE FROM items')
        db.execute('DELETE FROM agent_runs')
        db.execute("UPDATE ai_roles SET system_prompt = ''")
        db.execute('DELETE FROM ai_prompts')
        db.execute('DELETE FROM providers')
        db.execute("INSERT INTO providers(id, name, base_url, api_key, default_model) "
                   "VALUES(1,'mock','http://127.0.0.1:9/v1','sk-mock','mock-model')")
        db.execute("INSERT INTO items(account_id, item_id, title, price) VALUES('acc1','A','红色连衣裙',50)")
        db.execute("INSERT INTO items(account_id, item_id, title, price) VALUES('acc1','B','蓝色牛仔裤 全新',120)")
        self.conv_id = int(db.execute(
            "INSERT INTO conversations(account_id, peer_user_id, item_id, mode, agent_role) "
            "VALUES('acc1','buyer1','A','auto','item')"))
        self.msg_id = int(db.execute(
            "INSERT INTO messages(conversation_id, direction, sender, content) "
            "VALUES(?,'in','buyer','这条牛仔裤多少钱')", (self.conv_id,)))

    def test_item_reply_uses_item_role_and_cross_lookup(self):
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat({'item': '120 元，全新', 'cross': '蓝色牛仔裤 120 元'}, calls=calls)):
            result = asyncio.run(runtime.reply_for_message(self.conv_id, self.msg_id))

        self.assertTrue(result['ok'], result)
        self.assertEqual(result['role'], 'item')
        self.assertTrue(result['reply'])
        self.assertTrue(result['cross_used'])
        # 触发了跨商品协查：向 item:B 发过 ask
        rows = [dict(r) for r in db.query('SELECT * FROM agent_messages ORDER BY id')]
        self.assertTrue([r for r in rows if r['to_agent'] == 'item:B' and r['kind'] == 'ask'], rows)

    def test_reply_does_not_cross_accounts(self):
        """**AI-1 端到端**：acc1 的买家问一个只有 acc2 在卖的商品，绝不能问过去。

        修复前实测：往 acc2 插一个「苹果 iPhone 13 256G 黑色 全新」后，
        `POST /api/chats/incoming {"account_id":"acc1", ...}` 的 agent_messages 目标
        是 `[('cross',), ('item:itC',), ('item:itA',)]` —— itC 属于 acc2。
        """
        db.execute(
            "INSERT INTO items(account_id, item_id, title, price, stock, status) "
            "VALUES('acc2','F','苹果 iPhone 13 256G 黑色 全新',3999,1,'on_sale')")
        msg = int(db.execute(
            "INSERT INTO messages(conversation_id, direction, sender, content) "
            "VALUES(?,'in','buyer','苹果 iPhone 13 还有货吗')", (self.conv_id,)))

        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls=calls)):
            result = asyncio.run(runtime.reply_for_message(self.conv_id, msg))

        self.assertTrue(result['ok'], result)
        rows = [dict(r) for r in db.query('SELECT * FROM agent_messages ORDER BY id')]
        self.assertFalse([r for r in rows if r['to_agent'] == 'item:F'], f'跨账号协查: {rows}')
        self.assertFalse(result['cross_used'], f'不该触发协查: {result}')

        # 反过来：本账号内的商品仍然可以协查（别把功能修死）
        msg2 = int(db.execute(
            "INSERT INTO messages(conversation_id, direction, sender, content) "
            "VALUES(?,'in','buyer','这条牛仔裤多少钱')", (self.conv_id,)))
        with mock.patch.object(llm, 'chat', _fake_chat()):
            result2 = asyncio.run(runtime.reply_for_message(self.conv_id, msg2))
        self.assertTrue(result2['cross_used'], result2)
        rows2 = [dict(r) for r in db.query('SELECT * FROM agent_messages ORDER BY id')]
        self.assertTrue([r for r in rows2 if r['to_agent'] == 'item:B' and r['kind'] == 'ask'], rows2)

    def test_reply_forces_vision_for_image(self):
        conv = int(db.execute(
            "INSERT INTO conversations(account_id, peer_user_id, item_id, agent_role) "
            "VALUES('acc1','buyer2','A','vision')"))
        msg = int(db.execute(
            "INSERT INTO messages(conversation_id, direction, sender, content, meta) "
            "VALUES(?,'in','buyer','看看这个','{\"image_url\":\"http://x/1.jpg\"}')", (conv,)))
        calls = []
        with mock.patch.object(llm, 'chat', _fake_chat(calls=calls)):
            result = asyncio.run(runtime.reply_for_message(conv, msg))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['role'], 'vision')
        sent = calls[-1]['messages']
        # 图片按 OpenAI 多模态格式塞进 user content
        self.assertIsInstance(sent[-1]['content'], list)
        self.assertEqual(sent[-1]['content'][1]['type'], 'image_url')

    def test_missing_conversation_is_soft_error(self):
        result = asyncio.run(runtime.reply_for_message(99999, self.msg_id))
        self.assertFalse(result['ok'])
        self.assertIn('不存在', result['error'])

    def test_no_provider_is_soft_error(self):
        db.execute('DELETE FROM providers')
        result = asyncio.run(runtime.reply_for_message(self.conv_id, self.msg_id))
        self.assertFalse(result['ok'])
        self.assertIn('提供者', result['error'])


class MasterChatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.orig_db_path = config.DB_PATH
        config.DB_PATH = Path(cls.tmp.name) / 'app.db'
        db.reset_connection()
        db.init_db()
        roles.ensure_roles()

    @classmethod
    def tearDownClass(cls):
        db.reset_connection()
        config.DB_PATH = cls.orig_db_path
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        db.execute('DELETE FROM agent_runs')
        db.execute('DELETE FROM agent_messages')
        db.execute('DELETE FROM items')
        db.execute("UPDATE ai_roles SET system_prompt = ''")
        db.execute('DELETE FROM ai_prompts')
        db.execute('DELETE FROM providers')
        db.execute("INSERT INTO providers(id, name, base_url, api_key, default_model) "
                   "VALUES(1,'mock','http://127.0.0.1:9/v1','sk-mock','mock-model')")
        db.execute("INSERT INTO items(account_id, item_id, title, price, stock) "
                   "VALUES('acc1','A','红色连衣裙',50,3)")

    def test_master_tool_is_not_in_its_own_toolset(self):
        self.assertIn('master_chat', tools.TOOL_NAMES)
        self.assertNotIn('master_chat', [s['function']['name'] for s in tools.MASTER_TOOLS])
        self.assertEqual(len(tools.MASTER_TOOLS), len(tools.TOOL_SPECS) - 1)

    def test_tool_names_match_contract(self):
        expected = [
            'accounts_list', 'accounts_set_enabled', 'accounts_relogin',
            'items_list', 'items_get', 'items_edit', 'items_publish', 'items_batch_edit', 'items_sync',
            # ---- 草稿箱（前两条只读；后两条会真的删掉闲鱼上的草稿，required 必含 confirm）----
            'drafts_list', 'drafts_get', 'drafts_delete', 'drafts_batch_delete',
            'chats_list', 'chats_read', 'chats_send', 'chats_set_mode',
            # ---- 话术 / 快捷回复（前一条只读；后六条会真的改动平台上的快捷回复）----
            'quick_reply_list',
            'quick_reply_group_create', 'quick_reply_group_update', 'quick_reply_group_delete',
            'quick_reply_phrase_create', 'quick_reply_phrase_update', 'quick_reply_phrase_delete',
            'orders_list', 'orders_deliver',
            'risk_status', 'risk_clear',
            'providers_list', 'providers_upsert',
            # ---- 交易 ----
            'trade_orders', 'trade_refunds', 'trade_evaluations', 'trade_complaints', 'trade_addresses',
            # ---- 小铺 ----
            'shop_sub_accounts', 'shop_cs_routing',
            # ---- 客服分流写（会真的改平台数据，required 必含 confirm）----
            'shop_cs_routing_rule_save', 'shop_cs_group_save', 'shop_cs_group_delete',
    'shop_profile_get',
    'shop_profile_update_nick',
    'shop_profile_update_desc',
    'shop_profile_update_avatar',
    'sub_accounts_roles',
    'sub_account_create',
    'sub_account_delete',
    'sub_account_update_mobile',
    'sub_account_update_roles',
    'sub_account_update_password',
            # ---- 数据中心 ----
            'datacenter_overview', 'datacenter_fans', 'datacenter_customer_service',
            'datacenter_commodity',
            'datacenter_card',
            'datacenter_cs_list',
            # ---- 通知 ----
            'notifications_list',
            # ---- 运费模版 ----
            'item_templates_list', 'item_templates_get',
            # ---- 运费模版写（会真的改平台数据，required 必含 confirm）----
            'item_templates_create', 'item_templates_modify', 'item_templates_delete',
            'item_templates_unbind',
            # ---- 财务 ----
            'finance_income', 'finance_expense', 'finance_invoices', 'finance_purchaser',
            # ---- 推广 ----
            'promotion_overview', 'promotion_campaigns', 'promotion_reports', 'promotion_account',
            'promotion_funds', 'promotion_fund_operations',
            'promotion_recharge_detail', 'promotion_withdraw_detail',
            # ---- 安全中心 ----
            'security_center_status',
            # ---- 营销 ----
            'marketing_items', 'marketing_tasks', 'marketing_price_competitiveness',
            'marketing_records',
            # ---- 交易写（会真的改平台数据，required 必含 confirm）----
            'trade_write_ship_offline', 'trade_write_ship_dummy', 'trade_write_adjust_price',
            'trade_write_refund_agree',
            # ---- 交易只读（拿同意退款要用的 auth_token，故意没有 confirm）----
            'trade_refund_verify_token',
            'trade_write_refund_refuse', 'trade_write_rate_create',
            'trade_write_complain_refuse', 'trade_write_complain_proof',
            # ---- 退货地址写（会真的改平台上的退货地址，required 必含 confirm）----
            'trade_address_add', 'trade_address_update',
            'trade_address_delete', 'trade_address_set_default',
            # ---- 安全中心申诉（form 只读；其余三条会真的提交，required 必含 confirm）----
            'security_appeal_form',
            'security_appeal_submit', 'security_appeal_submit_cro', 'security_appeal_urge',
            # ---- 推广写 ----
            'promotion_write_actions', 'promotion_write_execute',
            # ---- 资质写 ----
            'qualification_submit_packages', 'qualification_oss_credential',
            # ---- 商品写（会真的改平台数据，required 必含 confirm）----
            'items_batch_offline', 'items_batch_delete', 'items_quick_edit',
            # ---- 改收货地址审批（1 读 + 2 写；读那条故意不是写名字的前缀）----
            'trade_order_address_request',
            'trade_order_address_agree', 'trade_order_address_refuse',
            # ---- 第三十八批：关单 / 延时 / 提醒 / 运费退款 / 撤销投诉（1 读 + 5 写）----
            'trade_close_reasons',
            'trade_close_order', 'trade_delay_confirm', 'trade_remind_confirm',
            'trade_postage_refund_pay', 'trade_complain_revoke',
            'logistics_companies', 'logistics_guess_mailno',
            'logistics_resend_consign',
            'im_blacklist_status', 'im_blacklist_add', 'im_blacklist_remove',
            'im_card_send',
            'master_chat',
        ]
        self.assertEqual(tools.TOOL_NAMES, expected)
        # 每个**写**工具的 required 必须含 confirm，且 description 里要有中文告警。
        # （promotion_write_actions 只是列目录、security_appeal_form 只是读表单，都是只读的，
        #   故意不在这个名单里。）
        write_tools = [
            'trade_write_ship_offline', 'trade_write_ship_dummy', 'trade_write_adjust_price',
            'trade_write_refund_agree', 'trade_write_refund_refuse', 'trade_write_rate_create',
            'trade_write_complain_refuse', 'trade_write_complain_proof',
            'trade_address_add', 'trade_address_update',
            'trade_address_delete', 'trade_address_set_default',
            'promotion_write_execute',
            'qualification_submit_packages', 'qualification_oss_credential',
            'item_templates_create', 'item_templates_modify', 'item_templates_delete',
            'item_templates_unbind',
            'items_batch_offline', 'items_batch_delete', 'items_quick_edit',
            'trade_order_address_agree', 'trade_order_address_refuse',
            'shop_cs_routing_rule_save', 'shop_cs_group_save', 'shop_cs_group_delete',
            'trade_order_address_agree', 'trade_order_address_refuse',
            'shop_cs_routing_rule_save', 'shop_cs_group_save', 'shop_cs_group_delete',
            'quick_reply_group_create', 'quick_reply_group_update', 'quick_reply_group_delete',
            'quick_reply_phrase_create', 'quick_reply_phrase_update', 'quick_reply_phrase_delete',
    'shop_profile_update_nick',
    'shop_profile_update_desc',
    'shop_profile_update_avatar',
    'sub_account_create',
    'sub_account_delete',
    'sub_account_update_mobile',
    'sub_account_update_roles',
    'sub_account_update_password',
            'drafts_delete', 'drafts_batch_delete',
            'security_appeal_submit', 'security_appeal_submit_cro', 'security_appeal_urge',
            'trade_close_order', 'trade_delay_confirm', 'trade_remind_confirm',
            'trade_postage_refund_pay', 'trade_complain_revoke',
            'logistics_resend_consign',
            'im_blacklist_add', 'im_blacklist_remove', 'im_card_send',
        ]
        for name in write_tools:
            spec = tools.get_tool(name)
            self.assertIsNotNone(spec, name)
            self.assertIn('confirm', spec['function']['parameters']['required'], name)
            self.assertIn('confirm=true', spec['function']['description'], name)
        for spec in tools.TOOL_SPECS:
            self.assertEqual(spec['type'], 'function')
            self.assertIn('name', spec['function'])
            self.assertIn('description', spec['function'])
            self.assertIn('parameters', spec['function'])
            self.assertEqual(spec['function']['parameters']['type'], 'object')

    def test_call_tool_reads_items_from_db(self):
        result = asyncio.run(tools.call_tool('items_list', {}))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['items'][0]['item_id'], 'A')

        result = asyncio.run(tools.call_tool('items_get', {'account_id': 'acc1', 'item_id': 'A'}))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['item']['title'], '红色连衣裙')

    def test_call_tool_validates_required_and_unknown(self):
        result = asyncio.run(tools.call_tool('items_get', {'account_id': 'acc1'}))
        self.assertFalse(result['ok'])
        self.assertIn('缺少参数', result['error'])

        result = asyncio.run(tools.call_tool('nope_nope', {}))
        self.assertFalse(result['ok'])
        self.assertIn('未知工具', result['error'])

    def test_call_tool_missing_item_is_soft_error(self):
        result = asyncio.run(tools.call_tool('items_get', {'account_id': 'acc1', 'item_id': 'ZZZ'}))
        self.assertFalse(result['ok'])
        self.assertIn('没找到', result['error'])

    def test_providers_list_hides_api_key(self):
        result = asyncio.run(tools.call_tool('providers_list', {}))
        self.assertTrue(result['ok'], result)
        self.assertNotIn('api_key', result['providers'][0])

    def test_run_master_executes_tool_then_answers(self):
        rounds = {'n': 0}

        async def fake(messages, *, role=None, tools=None, **kwargs):
            rounds['n'] += 1
            if rounds['n'] == 1:
                return {'text': '', 'tool_calls': [{'id': 'c1', 'function': {
                    'name': 'items_list', 'arguments': '{}'}}],
                    'usage': {}, 'model': 'mock-model', 'provider_id': 1, 'raw': {}}
            return {'text': '你有 1 个在售商品：红色连衣裙。', 'tool_calls': [], 'usage': {},
                    'model': 'mock-model', 'provider_id': 1, 'raw': {}}

        with mock.patch.object(llm, 'chat', fake):
            result = asyncio.run(runtime.run_master('我有哪些商品？'))

        self.assertTrue(result['ok'], result)
        self.assertIn('红色连衣裙', result['reply'])
        self.assertEqual(len(result['tool_calls']), 1)
        self.assertEqual(result['tool_calls'][0]['name'], 'items_list')
        self.assertTrue(result['tool_calls'][0]['result']['ok'])
        self.assertEqual(rounds['n'], 2)

    def test_run_master_simple_answer_without_tools(self):
        with mock.patch.object(llm, 'chat', _fake_chat({'master': '好的，我看看'})):
            result = asyncio.run(runtime.run_master('在吗'))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['reply'], '好的，我看看')
        self.assertEqual(result['tool_calls'], [])

    def test_run_master_llm_failure(self):
        async def boom(messages, **kwargs):
            raise llm.LLMError('HTTP 401: bad key')

        with mock.patch.object(llm, 'chat', boom):
            result = asyncio.run(runtime.run_master('在吗'))
        self.assertFalse(result['ok'])
        self.assertIn('401', result['error'])

    def test_run_master_llm_failure_keeps_provider_id(self):
        """失败行也要能说出「是哪个提供者挂的」（复审 M2）。"""
        async def boom(messages, **kwargs):
            raise llm.LLMError('HTTP 401: bad key', provider_id=7)

        with mock.patch.object(llm, 'chat', boom):
            result = asyncio.run(runtime.run_master('在吗'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['provider_id'], 7)

    def test_run_master_truncated_keeps_provider_and_model(self):
        """工具轮数用尽那条分支同样是**成功**，不许丢 provider_id/model/usage（复审 M1）。"""
        async def always_calls_tool(messages, *, role=None, tools=None, **kwargs):
            return {'text': '', 'tool_calls': [{'id': 'c1', 'function': {
                'name': 'items_list', 'arguments': '{}'}}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 1},
                'model': 'mock-model', 'provider_id': 3, 'raw': {}}

        with mock.patch.object(llm, 'chat', always_calls_tool):
            result = asyncio.run(runtime.run_master('一直调工具'))

        self.assertTrue(result['ok'], result)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['provider_id'], 3)
        self.assertEqual(result['model'], 'mock-model')
        self.assertEqual(result['usage']['prompt_tokens'], 5)
        self.assertEqual(len(result['tool_calls']), tools.MAX_TOOL_ROUNDS)

    def test_recent_runs_forwards_offset(self):
        """包装函数漏转发 `offset` 时，翻页永远翻不动（复审 L5）。"""
        seen = {}

        def fake_recent_runs(limit=100, role=None, offset=0):
            seen.update(limit=limit, role=role, offset=offset)
            return []

        with mock.patch.object(llm, 'recent_runs', fake_recent_runs):
            runtime.recent_runs(limit=20, role='master', offset=40)
        self.assertEqual(seen, {'limit': 20, 'role': 'master', 'offset': 40})

    def test_master_chat_tool_delegates_to_run_master(self):
        with mock.patch.object(llm, 'chat', _fake_chat({'master': '我在'})):
            result = asyncio.run(tools.call_tool('master_chat', {'message': '在吗'}))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['reply'], '我在')

    def test_master_chat_api(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.app.api import agents as agents_api
        from backend.app.core import security

        app = FastAPI()
        app.include_router(agents_api.router)
        client = TestClient(app)
        user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('m','x',1)"))
        headers = {'Authorization': f'Bearer {security.issue_token(user_id)}'}

        with mock.patch.object(llm, 'chat', _fake_chat({'master': '店里有一件红色连衣裙。'})):
            resp = client.post('/api/agents/master/chat', headers=headers, json={'message': '有什么商品'})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body['success'])
        self.assertIn('红色连衣裙', body['data']['reply'])

        resp = client.post('/api/agents/master/chat', headers=headers, json={'message': ''})
        self.assertEqual(resp.status_code, 400, resp.text)

        resp = client.post('/api/agents/master/chat', json={'message': 'hi'})
        self.assertEqual(resp.status_code, 401, resp.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
