"""`services/common.py::log_operation` 的回归锁（这个收口点原先没有测试）。

背景（第三十二批自查）：闸门探针故意用不存在的账号 `9999999999999` 走确认腿，
`operation_logs` 里因此出现 `account_id='9999999999999'` 的行。**这是探针留痕、不是缺陷**：
审计行记录的是「我们尝试做过什么」，不该反过来依赖「账号行存在」。

> 曾经在这里加过「账号不存在就不写」的挡板，结果 `run_all` 挂了 21 条 ——
> 「商品存在但账号行缺失」这类正常调用也被一起挡了，所以回退。
> 这个文件把**现有口径**钉住，免得下次又有人顺手加挡板。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_services_common.py"

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
from backend.app.services import common  # noqa: E402


class LogOperationTest(unittest.TestCase):
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
        db.execute('DELETE FROM operation_logs')
        db.execute('DELETE FROM accounts')

    def rows(self):
        return db.query('SELECT account_id, action, success, detail FROM operation_logs')

    def test_known_account_writes_a_row(self):
        db.execute('INSERT INTO accounts(id, nickname) VALUES(?,?)', ('acc-1', '账号一'))
        # 返回值是 db.execute 的 lastrowid，不是行数：只断言「写了」
        self.assertGreater(common.log_operation('acc-1', 'ship', 'item-1', True, '已发货'), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['account_id'], 'acc-1')
        self.assertEqual(rows[0]['action'], 'ship')
        self.assertEqual(rows[0]['success'], 1)
        self.assertEqual(rows[0]['detail'], '已发货')

    def test_no_account_row_still_writes(self):
        """**故意不校验账号存在**：商品在、账号行缺失时也必须留审计行。"""
        self.assertGreater(common.log_operation('no-such-account', 'ship', 'item-1', False, '失败'), 0)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['success'], 0)

    def test_kind_still_overrides_action(self):
        """既有口径不变：`kind` 传了就顶掉 `action`（表里只有一个 action 列）。"""
        common.log_operation('acc-1', 'nick', None, True, 'ok', kind='shop_profile_nick')
        self.assertEqual(self.rows()[0]['action'], 'shop_profile_nick')

    def test_dict_detail_is_serialised(self):
        common.log_operation('acc-1', 'rule', None, True, {'a': 1})
        self.assertEqual(self.rows()[0]['detail'], '{"a": 1}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
