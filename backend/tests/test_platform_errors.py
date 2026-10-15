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

from backend.app.services import common  # noqa: E402
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

    def test_bill_query_delay_says_when_the_bill_will_exist(self):
        """真机：查当月账单回 `BILL_QUERY_DELAY_ERROR::…请在 2026-11-05 后查看`。

        人话必须点出「次月 5 号」并给出出路（改成上个月），否则用户只会反复点刷新。
        """
        hint = pe.explain(
            '协议层调用失败: MTOP 调用失败 [mtop.idle.finance.monthbill.expense.summary.query]: '
            'BILL_QUERY_DELAY_ERROR::暂无法查看2026年10月的账单，请在2026-11-05后查看')
        self.assertIn('次月 5 号', hint)
        self.assertIn('上一个月', hint)
        self.assertEqual(pe.find_code(
            'BILL_QUERY_DELAY_ERROR::暂无法查看2026年10月的账单'), 'BILL_QUERY_DELAY_ERROR')


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

    def test_bare_permission_wording_gets_a_hint_without_a_code(self):
        """平台只回人话「无权限访问」、没带码的行也要说人话（真机 logs 里有）。

        但**不能**顺手编一个 `platform_code`：原文里没有码，编出来会让按码分支的
        前端跟着说错话。
        """
        out = pe.decorate(self._failure(
            '协议层调用失败: MTOP 调用失败 '
            '[mtop.taobao.idlemessage.customer.leave.session]: 无权限访问'))
        self.assertIn('没有开通', out['message'])
        self.assertEqual(out['platform_hint'],
                         pe.hint_for('FAIL_BIZ_IDLE_USER_UNAUTHORIZED'))
        self.assertNotIn('platform_code', out)


class DescribeTest(unittest.TestCase):
    """`describe()`：通用错误路径的一句人话（比 `explain()` 多退一步到「没权限」）。"""

    def test_known_code_wins(self):
        self.assertEqual(pe.describe('FAIL_BIZ_IDLE_UNKNOWN_THROWABLE::发布失败，请稍后重试吧~'),
                         pe.hint_for('FAIL_BIZ_IDLE_UNKNOWN_THROWABLE'))

    def test_bare_permission_wording_falls_back_to_the_shared_sentence(self):
        self.assertEqual(pe.describe('XianyuError: 无权限访问'),
                         pe.hint_for('FAIL_BIZ_IDLE_USER_UNAUTHORIZED'))

    def test_unknown_and_other_codes_stay_silent(self):
        # 没见过的码不许编解释。
        self.assertEqual(pe.describe('XianyuError: boom'), '')
        # `FAIL_SYS_NO_PERMISSION` 是另一个码，别套「没开通功能」的解释。
        self.assertEqual(pe.describe('FAIL_SYS_NO_PERMISSION::无权限操作'), '')

    def test_accepts_several_texts_like_explain(self):
        self.assertEqual(pe.describe('协议层调用失败', 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'),
                         pe.hint_for('FAIL_BIZ_IDLE_USER_UNAUTHORIZED'))


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


class ReadPathRefusalTest(unittest.TestCase):
    """只读接口的平台拒绝也要说人话（2026-10-12 真机 35 条 GET 巡检见过的码）。

    用户看到「用户没有权限」的第一反应是「我是不是掉线了」→ 去重新扫码 → 白扫一次，
    问题还在。所以「这个账号没开这个功能」必须与「登录态过期」在文案上分开。
    """

    def test_no_permission_says_it_is_not_a_logout(self):
        hint = pe.explain(
            '协议层调用失败: MTOP 调用失败 [mtop.taobao.idle.cco.shop.complain.list]: '
            'NO_PERMISSION::用户没有权限')
        self.assertIn('权限', hint)
        self.assertIn('不是登录掉线', hint)
        self.assertNotIn('重新扫码登录', hint)

    def test_bare_no_permission_does_not_match_the_prefixed_code(self):
        """`FAIL_SYS_NO_PERMISSION` 是另一个码，不该套裸码 `NO_PERMISSION` 的解释。"""
        self.assertEqual(pe.find_code('FAIL_SYS_NO_PERMISSION::无权限操作'), '')
        self.assertEqual(pe.explain('FAIL_SYS_NO_PERMISSION::无权限操作'), '')

    def test_user_unauthorized_matches(self):
        hint = pe.explain(
            '协议层调用失败: MTOP 调用失败 '
            '[mtop.alibaba.idle.seller.common.sub.account.info.list]: '
            'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        self.assertIn('没有开通', hint)
        self.assertIn('不是登录掉线', hint)

    def test_permission_judgement_is_one_place(self):
        """「没权限」的判据只有 `pe.is_permission` 一处（审计问题 13）。

        `services/datacenter.py` / `services/marketing.py` 原来各自写
        `'UNAUTHORIZED' in text or '无权限' in text`，于是同一个平台码在
        「算不算没权限」这件事上有两份判据。
        """
        self.assertTrue(pe.is_permission(
            'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED'))
        self.assertTrue(pe.is_permission(
            '协议层调用失败: MTOP 调用失败 [x]: NO_PERMISSION::用户没有权限'))
        # 真机 logs 里有只透传人话、没带码的行。
        self.assertTrue(pe.is_permission('XianyuError: 无权限访问'))
        # `FAIL_SYS_NO_PERMISSION` 是**另一个码**，别套这两个码的解释（与 explain 同口径）。
        self.assertFalse(pe.is_permission('FAIL_SYS_NO_PERMISSION::无权限操作'))
        self.assertFalse(pe.is_permission('XianyuError: boom'))

    def test_permission_hint_reuses_the_single_wording(self):
        """「没权限」的人话全站一份：认不出具体码时复用已收录的那句，不编新解释。"""
        expected = pe.hint_for('FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(pe.permission_hint('无权限访问'), expected)
        self.assertEqual(
            pe.permission_hint('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'), expected)
        self.assertIn('不是登录掉线', pe.permission_hint('NO_PERMISSION::用户没有权限'))

    def test_qualification_system_errors_are_not_blamed_on_the_user(self):
        busy = pe.explain(
            '协议层调用失败: MTOP 调用失败 '
            '[mtop.taobao.idle.ad.qualification.templates.queryv2]: '
            'E_AD_BP_COMMON_SYSTEM_EXCEPTION::闲鱼太累了，请稍后再试～')
        self.assertIn('平台', busy)
        self.assertIn('重试', busy)
        other = pe.explain(
            '协议层调用失败: MTOP 调用失败 '
            '[mtop.taobao.idle.ad.qualification.detail.query]: '
            'COMMON_SERVER_ERROR::服务繁忙,请稍后再试~')
        self.assertIn('服务繁忙', other)
        # 两个码各归各的，不互相串味
        self.assertNotEqual(busy, other)

    def test_respond_decorates_failures_without_touching_the_status_code(self):
        """唯一的出口 `common.respond` 会补人话，但**不改** `code`/状态码。"""
        status, body = common.respond({
            'success': False,
            'message': '协议层调用失败: MTOP 调用失败 [x]: NO_PERMISSION::用户没有权限',
            'code': 'protocol_error',
        })
        self.assertEqual(status, 502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertEqual(body['platform_code'], 'NO_PERMISSION')
        self.assertIn('不是登录掉线', body['message'])

    def test_respond_leaves_unknown_codes_and_success_alone(self):
        _, body = common.respond({
            'success': False, 'message': 'FAIL_SYS_SOMETHING_NEW::没见过的错误',
            'code': 'protocol_error',
        })
        self.assertEqual(body['message'], 'FAIL_SYS_SOMETHING_NEW::没见过的错误')
        self.assertNotIn('platform_code', body)
        _, ok = common.respond({'items': []})
        self.assertEqual(ok, {'success': True, 'data': {'items': []}})


    def test_order_status_error_stays_generic(self):
        """这个码不只在发货出现，措辞不许绑死某个动作。"""
        hint = pe.explain('协议层调用失败: ORDER_STATUS_ERROR::订单状态不正确')
        self.assertIn('状态', hint)
        self.assertIn('刷新', hint)
        self.assertNotIn('不允许发货', hint)


if __name__ == '__main__':
    unittest.main(verbosity=2)
