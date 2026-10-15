"""confirm 闸门单点测试（第 58 批 task-10 收敛后的回归锁）。

为什么：`confirm` 判据曾在 13+ 个 services 模块里各复制一份，`bool('false') is True`
那类坑每份都可能重犯。2026-10-15 收敛到 `backend/app/services/common.py` 单点：
宽松档 `confirm_truthy`（`True` / `'true'` / `1`）、严格档 `confirm is True`
（广告写入 `services/ad_write.py` 与资质写平台 `services/qualification.py` 历史上就这口径）。

本文件锁三件事：

1. 单点判据的取值表 —— 含**故意收紧**：字符串 `'1'` 不再算确认。收敛前
   `services/shop_profile.py` 与 `services/sub_accounts.py` 接受它，收敛后被拒，
   与其它 14 个模块一致；旧行为没有任何测试覆盖，收紧是安全的（更少的误写）。
2. 两个业务码都落 HTTP 400：默认 `confirmation_required`、严格档 `confirm_required`
   （都不在 `common.FAIL_STATUS` 里 ⇒ `FAIL_STATUS.get(code, 400)`）。
3. AST 扫描：`backend/app/services/*.py` 里不许再出现自己的 `def _truthy`，
   只允许 `_truthy = common.confirm_truthy` 这种别名（防再复制一份判据）。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.app.services import common  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / 'backend' / 'app' / 'services'


class ConfirmTruthyTest(unittest.TestCase):
    """宽松档取值表：只有 True / 'true'（忽略大小写与空白）/ 1 算点头。"""

    def test_accepted_values(self):
        for value in (True, 'true', 'TRUE', ' true ', 1):
            self.assertTrue(common.confirm_truthy(value), repr(value))

    def test_rejected_values(self):
        for value in ('1', 'false', 'yes', 'on', 'no', 0, 2, -1, None, [], {}, ''):
            self.assertFalse(common.confirm_truthy(value), repr(value))

    def test_strict_only_accepts_literal_true(self):
        self.assertTrue(common.confirm_accepted(True, strict=True))
        for value in ('true', 1, '1', 'TRUE'):
            self.assertFalse(common.confirm_accepted(value, strict=True), repr(value))


class ConfirmEnvelopeTest(unittest.TestCase):
    """失败信封：码分两档，两个码都落 400，且已确认时返回 None。"""

    def test_loose_code_is_confirmation_required(self):
        problem = common.confirm_required('订单发货', False)
        self.assertEqual(problem['code'], 'confirmation_required')
        self.assertIn('订单发货', problem['message'])
        self.assertEqual(common.FAIL_STATUS.get(problem['code'], 400), 400)

    def test_strict_code_is_confirm_required(self):
        problem = common.confirm_required('广告出价', 'true', strict=True)
        self.assertEqual(problem['code'], 'confirm_required')
        self.assertEqual(common.FAIL_STATUS.get(problem['code'], 400), 400)

    def test_confirmed_returns_none(self):
        self.assertIsNone(common.confirm_required('订单发货', True))
        self.assertIsNone(common.confirm_required('广告出价', True, strict=True))

    def test_extra_fields_are_merged(self):
        problem = common.confirm_failure('子账号新建', extra={'action': '子账号新建'})
        self.assertEqual(problem['action'], '子账号新建')


class NoDuplicateGateTest(unittest.TestCase):
    """AST：services 里不许再出现自己的一份 `_truthy`。"""

    def test_no_local_truthy_definitions(self):
        offenders = []
        for path in sorted(SERVICE_DIR.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == '_truthy':
                    offenders.append(f'{path.name}:{node.lineno} 自己定义了 _truthy')
                if isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    if '_truthy' in names:
                        expr = ast.unparse(node.value)
                        if expr != 'common.confirm_truthy':
                            offenders.append(f'{path.name}:{node.lineno} _truthy = {expr}')
        self.assertEqual(offenders, [])

    def test_single_point_still_exists(self):
        for name in ('confirm_truthy', 'confirm_accepted', 'confirm_failure', 'confirm_required'):
            self.assertTrue(callable(getattr(common, name)), name)


if __name__ == '__main__':
    unittest.main()
