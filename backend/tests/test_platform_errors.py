"""平台错误码翻译表测试（`backend/app/services/platform_errors.py`）。

**为什么要这一组**：这张表的价值全在「不许讲错」上 —— 用户是照着这句话决定下一步
操作的（改价格？重新扫码？等冷却？）。三类写法会悄悄讲错，都在这里钉住：

1. **子串包含**：`..._CAN_NOT_PUBLISH_V2` 命中 `..._CAN_NOT_PUBLISH` 的解释；
2. **编解释**：没见过的码也套一句「请检查价格」；
3. **改判定**：`decorate()` 顺手把 `code` 从 `protocol_error` 改成 `risk_blocked`，
   于是 HTTP 状态码跟着变（`code` 决定状态码，见 `services/common.py::FAIL_STATUS`）。

另外锁住真机原文能翻出来（第七轮截图里那句裸错误码）。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_platform_errors.py"
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.services import items as items_service  # noqa: E402
from backend.app.services import platform_errors as pe  # noqa: E402

#: 第七轮真机截图里发布失败时右下角 toast 的原文。
REAL_TOAST = (
    '协议层调用失败: FAIL_BIZ_IDLE_UNKNOWN_THROWABLE::发布失败，请稍后重试吧~'
)
#: 真机风控原文（会话页 toast）。
REAL_RISK = (
    '协议层调用失败: FAIL_SYS_USER_VALIDATE; '
    'RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试'
)


class MatchCodeTest(unittest.TestCase):
    """错误码按词边界匹配，不是子串包含。"""

    def test_exact_code_matches(self):
        self.assertTrue(pe.match_code(REAL_TOAST, 'FAIL_BIZ_IDLE_UNKNOWN_THROWABLE'))

    def test_same_prefix_longer_code_does_not_match(self):
        text = 'FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH_V2::亲，不能发布'
        self.assertFalse(pe.match_code(text, 'FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH'))

    def test_code_inside_a_longer_identifier_does_not_match(self):
        self.assertFalse(pe.match_code('MY_RGV587_ERROR_BACKUP', 'RGV587_ERROR'))

    def test_code_next_to_punctuation_matches(self):
        # 真机文案里码后面跟的是 `;`/`::`/空格，都属于词边界。
        self.assertTrue(pe.match_code(REAL_RISK, 'RGV587_ERROR'))
        self.assertTrue(pe.match_code('RGV587_ERROR', 'RGV587_ERROR'))

    def test_empty_inputs_are_false(self):
        self.assertFalse(pe.match_code('', 'RGV587_ERROR'))
        self.assertFalse(pe.match_code(REAL_RISK, ''))
        self.assertFalse(pe.match_code(None, 'RGV587_ERROR'))


class ExplainTest(unittest.TestCase):
    """真机见过的码要翻成人话；没见过的原样透传（返回空串）。"""

    def test_real_publish_failure_gets_actionable_hint(self):
        hint = pe.explain(REAL_TOAST)
        self.assertIn('售价', hint)
        self.assertIn('库存', hint)
        # 必须告诉用户「现在能做什么」，不能只说「失败了」。
        self.assertIn('改回正常范围', hint)

    def test_real_risk_failure_is_recognized(self):
        self.assertTrue(pe.is_risk(REAL_RISK))
        self.assertIn('风控', pe.explain(REAL_RISK))

    def test_unknown_code_gets_no_hint(self):
        self.assertEqual(pe.explain('协议层调用失败: FAIL_SYS_SOMETHING_NEW::没见过的错误'), '')
        self.assertEqual(pe.find_code('FAIL_SYS_SOMETHING_NEW::没见过的错误'), '')

    def test_empty_text_gets_no_hint(self):
        self.assertEqual(pe.explain(''), '')
        self.assertEqual(pe.explain(None), '')

    def test_platform_typo_token_exoired_is_covered(self):
        # 平台自己把 EXPIRED 拼成 EXOIRED，照抄才能匹配上。
        self.assertTrue(pe.is_session_expired('FAIL_SYS_TOKEN_EXOIRED::令牌过期'))
        self.assertFalse(pe.is_session_expired('FAIL_SYS_TOKEN_EXPIRED::令牌过期'))

    def test_find_code_accepts_a_list(self):
        # 异常上的 `ret` 就是列表（`XianyuError(message, ret=[...])`）。
        code = pe.find_code(['FAIL_SYS_SESSION_EXPIRED::Session过期', '其它'])
        self.assertEqual(code, 'FAIL_SYS_SESSION_EXPIRED')

    def test_hint_for_unknown_code_is_empty(self):
        self.assertEqual(pe.hint_for('FAIL_SYS_SOMETHING_NEW'), '')


class DecorateTest(unittest.TestCase):
    """`decorate()` 只给失败信封补人话，不动其它任何东西。"""

    def _failure(self, message: str) -> dict:
        return {
            'success': False,
            'message': message,
            'code': 'protocol_error',
            'status_code': 502,
        }

    def test_known_code_appends_hint_and_keeps_code(self):
        out = pe.decorate(self._failure(REAL_TOAST))
        self.assertIn('售价', out['message'])
        self.assertEqual(out['platform_code'], 'FAIL_BIZ_IDLE_UNKNOWN_THROWABLE')
        self.assertTrue(out['platform_hint'])
        # `code` 决定 HTTP 状态码，翻译表不许碰它。
        self.assertEqual(out['code'], 'protocol_error')
        self.assertEqual(out['status_code'], 502)

    def test_risk_code_gets_a_risk_flag(self):
        out = pe.decorate(self._failure(REAL_RISK))
        self.assertTrue(out['risk'])
        self.assertIn('风控', out['message'])

    def test_non_risk_code_has_no_risk_flag(self):
        out = pe.decorate(self._failure(REAL_TOAST))
        self.assertNotIn('risk', out)

    def test_unknown_code_returns_the_same_object(self):
        problem = self._failure('协议层调用失败: FAIL_SYS_SOMETHING_NEW::没见过的错误')
        self.assertIs(pe.decorate(problem), problem)

    def test_success_envelope_is_untouched(self):
        ok = {'success': True, 'data': {'item_id': 'x'}}
        self.assertIs(pe.decorate(ok), ok)

    def test_non_dict_is_untouched(self):
        self.assertEqual(pe.decorate('boom'), 'boom')
        self.assertIsNone(pe.decorate(None))

    def test_original_dict_is_not_mutated(self):
        problem = self._failure(REAL_TOAST)
        before = dict(problem)
        pe.decorate(problem)
        self.assertEqual(problem, before)

    def test_code_can_be_found_on_the_exception_ret_list(self):
        # 有些失败信封的 message 里只有人话，码在异常的 `ret` 上。
        class FakeError(RuntimeError):
            ret = ['FAIL_SYS_USER_VALIDATE::请进行滑块验证']

        problem = self._failure('协议层调用失败')
        out = pe.decorate(problem, FakeError('协议层调用失败'))
        self.assertEqual(out['platform_code'], 'FAIL_SYS_USER_VALIDATE')
        self.assertTrue(out['risk'])

    def test_hint_is_not_appended_twice(self):
        problem = self._failure(REAL_TOAST)
        once = pe.decorate(problem)
        twice = pe.decorate(once)
        self.assertEqual(once['message'], twice['message'])


class ItemsIntegrationTest(unittest.TestCase):
    """编辑路径的 `items._platform_error_hint` 用的是同一份判定。"""

    def test_real_publish_toast_also_explains_on_the_edit_path(self):
        hint = items_service._platform_error_hint(REAL_TOAST)
        self.assertIn('售价', hint)
        # 调用方直接把它拼在平台原文后面，所以必须自带括号，否则连成一串。
        self.assertTrue(hint.startswith('（') and hint.endswith('）'), hint)

    def test_item_specific_hint_still_wins(self):
        text = (
            '协议层修改失败: FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH'
            '::亲，您不能发布多库存宝贝哦'
        )
        self.assertIn('单件库存', items_service._platform_error_hint(text))

    def test_unknown_code_is_passed_through_unchanged(self):
        self.assertEqual(
            items_service._platform_error_hint('FAIL_SYS_SOMETHING_NEW::没见过的错误'), ''
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
