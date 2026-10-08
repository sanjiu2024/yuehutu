"""`services/purge.py` 的回归锁：删号清理与孤儿自愈。

背景（用户真机提问）：「删除的账号数据怎么还在？」—— 老版本删号只清
`accounts`/`account_credentials`/`risk_*`/`conversations`/`messages`，于是
`items`（真机上留了 39 件）、`operation_logs`（15 行）、`item_stats_cache`、`orders`
全部留下，商品页照旧把已删账号的商品列出来。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_services_purge.py"

只用临时库；不打真实闲鱼平台。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.core import config, db  # noqa: E402
from backend.app.services import purge  # noqa: E402

ALL_TABLES = ('messages', 'conversations', 'items', 'item_stats_cache', 'operation_logs',
              'orders', 'shipments', 'ai_prompts', 'risk_state', 'risk_events',
              'account_credentials', 'accounts', 'agent_chat_messages')


class _PurgeBase(unittest.TestCase):
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
        for table in ALL_TABLES:
            db.execute('DELETE FROM %s' % table)

    # ------------------------------------------------------------- 夹具

    def account(self, account_id: str) -> None:
        db.execute('INSERT INTO accounts(id, nickname) VALUES(?,?)', (account_id, account_id))
        db.execute('INSERT INTO account_credentials(account_id) VALUES(?)', (account_id,))

    def seed(self, account_id: str, item_id: str) -> None:
        """给一个账号铺满所有「账号级」业务行（含会话与消息）。"""
        db.execute('INSERT INTO items(account_id, item_id, title) VALUES(?,?,?)',
                   (account_id, item_id, '标题'))
        db.execute('INSERT INTO item_stats_cache(account_id, item_id, exposure) VALUES(?,?,?)',
                   (account_id, item_id, 12))
        db.execute('INSERT INTO operation_logs(account_id, item_id, action) VALUES(?,?,?)',
                   (account_id, item_id, 'edit'))
        db.execute('INSERT INTO orders(account_id, order_id, item_id) VALUES(?,?,?)',
                   (account_id, 'O-' + item_id, item_id))
        db.execute('INSERT INTO shipments(order_id, company) VALUES(?,?)',
                   ('O-' + item_id, '顺丰'))
        db.execute("INSERT INTO ai_prompts(role, scope, account_id, content) "
                   "VALUES('item','account',?,?)", (account_id, '账号级提示词'))
        db.execute('INSERT INTO conversations(account_id, peer_user_id, item_id) VALUES(?,?,?)',
                   (account_id, 'buyer-' + item_id, item_id))
        conv = db.query_one('SELECT id FROM conversations WHERE account_id = ?', (account_id,))
        db.execute('INSERT INTO messages(conversation_id, direction, sender, content) '
                   "VALUES(?,?,?,?)", (int(conv['id']), 'in', 'buyer', '在吗'))

    def n(self, table: str, account_id: str) -> int:
        row = db.query_one('SELECT COUNT(*) AS c FROM %s WHERE account_id = ?' % table,
                           (account_id,))
        return int(row['c'])

    def shipments_of(self, order_id: str) -> int:
        row = db.query_one('SELECT COUNT(*) AS c FROM shipments WHERE order_id = ?', (order_id,))
        return int(row['c'])

    def messages_of(self, account_id: str) -> int:
        """`messages` 没有 account_id 列，只能经 conversations 关联（写错过一次）。"""
        row = db.query_one('SELECT COUNT(*) AS c FROM messages m '
                           'JOIN conversations c ON c.id = m.conversation_id '
                           'WHERE c.account_id = ?', (account_id,))
        return int(row['c'])


class PurgeAccountDataTest(_PurgeBase):
    def test_deletes_every_account_scoped_table_and_spares_others(self):
        self.account('a1')
        self.account('a2')
        self.seed('a1', 'i1')
        self.seed('a2', 'i2')

        counts = purge.purge_account_data('a1')

        self.assertEqual(counts, {'items': 1, 'item_stats_cache': 1, 'operation_logs': 1,
                                  'orders': 1, 'ai_prompts': 1, 'shipments': 1}, counts)
        for table in purge.ACCOUNT_TABLES:
            self.assertEqual(self.n(table, 'a1'), 0, table)
            self.assertEqual(self.n(table, 'a2'), 1, table)
        self.assertEqual(self.shipments_of('O-i1'), 0)
        self.assertEqual(self.shipments_of('O-i2'), 1, '误删了别的账号的发货单')

    def test_is_idempotent_and_safe_on_empty_input(self):
        self.account('a1')
        purge.purge_account_data('a1')
        self.assertEqual(sum(purge.purge_account_data('a1').values()), 0)
        self.assertEqual(sum(purge.purge_account_data('').values()), 0)

    def test_shipments_without_orders_are_left_alone(self):
        """发货单按订单号删：订单不属于这个账号时不许顺手删掉它。"""
        self.account('a1')
        db.execute('INSERT INTO orders(account_id, order_id, item_id) VALUES(?,?,?)',
                   ('other', 'O-other', 'i9'))
        db.execute('INSERT INTO shipments(order_id, company) VALUES(?,?)', ('O-other', '中通'))

        purge.purge_account_data('a1')

        self.assertEqual(self.shipments_of('O-other'), 1)


class OrphanTest(_PurgeBase):
    def test_orphan_ids_and_purge_only_touch_deleted_accounts(self):
        self.account('alive')
        self.seed('alive', 'i9')
        # 模拟老版本留下的残留：账号行已经删了，业务行还在。
        self.seed('ghost', 'g1')

        self.assertEqual(purge.orphan_account_ids(), ['ghost'])

        result = purge.purge_orphans()

        self.assertEqual(result['purged_accounts'], 1, result)
        self.assertGreater(result['purged_rows'], 0, result)
        self.assertEqual(result['accounts']['ghost']['items'], 1, result)
        self.assertEqual(result['accounts']['ghost']['conversations'], 1, result)
        self.assertEqual(result['accounts']['ghost']['messages'], 1, result)
        for table in purge.ACCOUNT_TABLES + ('conversations',):
            self.assertEqual(self.n(table, 'ghost'), 0, table)
            self.assertEqual(self.n(table, 'alive'), 1, table)
        self.assertEqual(self.messages_of('alive'), 1)

        self.assertEqual(purge.orphan_account_ids(), [])
        self.assertEqual(purge.purge_orphans()['purged_accounts'], 0)

    def test_empty_account_id_is_not_an_orphan(self):
        """`account_id=''` 是「全局/不属于任何账号」，不是孤儿（真删了会连累全站提示词）。"""
        db.execute("INSERT INTO ai_prompts(role, scope, account_id, content) "
                   "VALUES('item','global','','全局提示词')")
        self.assertEqual(purge.orphan_account_ids(), [])
        self.assertEqual(purge.purge_orphans()['purged_accounts'], 0)
        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM ai_prompts')['n'], 1)

    def test_master_chat_history_is_never_touched(self):
        """主管 AI 的对话不属于任何账号，删号清理不许碰它。"""
        db.execute("INSERT INTO agent_chat_messages(thread, account_id, role, content) "
                   "VALUES('master','','user','你好')")
        self.account('alive')
        self.seed('ghost', 'g1')

        purge.purge_orphans()

        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM agent_chat_messages')['n'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
