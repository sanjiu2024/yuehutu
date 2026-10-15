"""协议层纯函数测试：Cookie 语义、风控阶梯、指纹头、mtop 签名与解析。

全部离线，不碰任何真实平台。跑法（仓库根目录）::

    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_xianyu_cookies.py"

**为什么这些断言值得单独一个文件**：这里测的都是「写错就白做」的判定逻辑。
其中最贵的一条是「没有 x5sec 就不算成功」——旧项目在这里判错过一次，
报成功后上层拿着一张没通过风控的 Cookie 继续跑，每个请求都撞风控。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.xianyu import cookies as ck  # noqa: E402
from backend.app.xianyu import fingerprint, mtop, risk  # noqa: E402


class CookieParseTest(unittest.TestCase):
    def test_parse_basic(self):
        parsed = ck.parse_cookie_string('unb=123; _m_h5_tk=abc_1699999999999; cna=xyz')
        self.assertEqual(parsed['unb'], '123')
        self.assertEqual(parsed['_m_h5_tk'], 'abc_1699999999999')
        self.assertEqual(parsed['cna'], 'xyz')

    def test_parse_empty_returns_dict_not_raise(self):
        # 旧实现空串抛 ValueError，逼得每个调用方都写 try；这里必须是空 dict。
        self.assertEqual(ck.parse_cookie_string(''), {})
        self.assertEqual(ck.parse_cookie_string(None), {})

    def test_parse_skips_garbage_segments(self):
        parsed = ck.parse_cookie_string('a=1; ; broken; b=2')
        self.assertEqual(parsed, {'a': '1', 'b': '2'})

    def test_parse_splits_on_first_equals_only(self):
        parsed = ck.parse_cookie_string('token=YWJj==; x=1')
        self.assertEqual(parsed['token'], 'YWJj==')

    def test_cookie_string_roundtrip(self):
        original = 'a=1; b=2'
        self.assertEqual(ck.cookie_string(ck.parse_cookie_string(original)), original)
        self.assertEqual(ck.cookie_string({}), '')


class X5SecSemanticsTest(unittest.TestCase):
    """x5sec = 通过凭证；x5secdata/x5sectag/x5step = 挑战未完成标记。"""

    def test_has_x5sec_true(self):
        self.assertTrue(ck.has_x5sec('unb=1; x5sec=abc'))
        self.assertTrue(ck.has_x5sec('X5Sec=abc'))  # 平台偶尔回大小写变体

    def test_challenge_markers_alone_are_not_pass(self):
        # 这是最关键的一条：只有挑战标记时**绝不能**算通过。
        blocked = 'unb=1; x5secdata=abc; x5sectag=def; x5step=1'
        self.assertFalse(ck.has_x5sec(blocked))
        self.assertTrue(ck.is_blocked_cookie(blocked))

    def test_is_blocked_cookie_false_without_markers(self):
        self.assertFalse(ck.is_blocked_cookie('unb=1; cna=2'))
        self.assertFalse(ck.is_blocked_cookie(''))

    def test_is_blocked_cookie_false_when_pass_present(self):
        # 已有凭证时，残留标记不算被拦。
        self.assertFalse(ck.is_blocked_cookie('x5sec=ok; x5secdata=old'))

    def test_drop_challenge_markers_keeps_them_without_x5sec(self):
        """没有 x5sec 时**必须原样保留**标记。

        x5secdata 里带着惩罚页地址，人工验证要靠它定位滑块页；提前清掉会让
        用户点「去验证」落到首页，永远过不去。
        """
        blocked = 'unb=1; x5secdata=penalty-url; x5step=1'
        self.assertEqual(ck.drop_challenge_markers(blocked), blocked)

    def test_drop_challenge_markers_clears_only_after_pass(self):
        cleaned = ck.drop_challenge_markers('x5sec=ok; x5secdata=old; x5sectag=t; x5step=1; cna=c')
        self.assertNotIn('x5secdata', cleaned)
        self.assertNotIn('x5sectag', cleaned)
        self.assertNotIn('x5step', cleaned)
        self.assertIn('x5sec=ok', cleaned)
        self.assertIn('cna=c', cleaned)  # 不相关的 Cookie 不能被误删

    def test_merge_cookies_response_overrides(self):
        merged = ck.merge_cookies('_m_h5_tk=old_1; unb=9', {'_m_h5_tk': 'new_2'})
        parsed = ck.parse_cookie_string(merged)
        self.assertEqual(parsed['_m_h5_tk'], 'new_2')
        self.assertEqual(parsed['unb'], '9')


class RiskControlTest(unittest.TestCase):
    def test_markers(self):
        self.assertIn('FAIL_SYS_USER_VALIDATE', risk.RISK_CONTROL_MARKERS)
        self.assertIn('RGV587_ERROR', risk.RISK_CONTROL_MARKERS)
        self.assertTrue(risk.is_risk_control_error('FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦'))
        self.assertFalse(risk.is_risk_control_error('SUCCESS'))

    def test_session_expired_is_not_risk(self):
        """登录态过期**不是风控**，不许熔断。

        混为一谈的后果：用户只是 Cookie 过期，却被程序按风控冷却 20 分钟，
        而且 consecutive_hits 一路累加，下次真撞风控时直接跳到最长冷却。
        """
        for message in ('FAIL_SYS_SESSION_EXPIRED::Session过期', 'Session过期'):
            self.assertTrue(risk.is_session_expired(message))
            self.assertFalse(risk.is_risk_control_error(message))

    def test_business_failure_is_not_risk(self):
        """真机反例（2026-10-15）：平台拒掉我们载荷的业务失败文案里带「请稍后重试」，
        不许当成风控。误判的代价：账号被本地熔断 60s（COOLDOWN_STEPS 首档）、
        `risk_state` 多一条假命中（风控页显示一次没发生的风控）、同进程后续调用全被
        `RiskControlError 账号处于风控冷却中` 拦下。
        """
        for message in ('FAIL_BIZ_IDLEITEM_UNKNOWN_THROWABLE::发布失败，请稍后重试吧!',
                        '协议层调用失败，仅本地生效: FAIL_BIZ_JSON_ERROR::发布失败，请稍后重试吧!'):
            self.assertFalse(risk.is_risk_control_error(message))
        # 真见过的限流文案（logs/firsthand_probe.json）仍然要认，不能把标记删干净
        self.assertTrue(risk.is_risk_control_error(
            'FAIL_BIZ_USER_SHOW_SYSTEM_ERROR::亲，小闲鱼有点忙，请稍后重试哦'))

    def test_cooldown_ladder(self):
        self.assertEqual(risk.COOLDOWN_STEPS, (60, 180, 300, 600, 1200))

    def test_guard_climbs_ladder(self):
        guard = risk.AccountGuard('unb-1')
        expected = [60, 180, 300, 600, 1200]
        for step, seconds in enumerate(expected):
            retry_after = guard.trip('FAIL_SYS_USER_VALIDATE')
            self.assertGreaterEqual(retry_after, 0)
            self.assertTrue(guard.is_blocked)
            remaining = guard.remaining_seconds
            # 允许 1s 的执行误差
            self.assertLessEqual(abs(remaining - seconds), 1,
                                 f'第 {step + 1} 次熔断应为 {seconds}s，实际 {remaining}s')

    def test_guard_caps_at_last_step(self):
        guard = risk.AccountGuard('unb-2')
        for _ in range(10):
            guard.trip('FAIL_SYS_USER_VALIDATE')
        self.assertEqual(guard.consecutive_hits, 10)
        self.assertLessEqual(guard.remaining_seconds, 1200)

    def test_guard_reset_clears(self):
        guard = risk.AccountGuard('unb-3')
        guard.trip('FAIL_SYS_USER_VALIDATE')
        self.assertTrue(guard.is_blocked)
        guard.reset()
        self.assertFalse(guard.is_blocked)
        self.assertEqual(guard.consecutive_hits, 0)
        self.assertEqual(guard.remaining_seconds, 0)

    def test_registry_is_per_cookie_id(self):
        """熔断按 cookie_id **独立**计数。

        共用一个计数器时，A 账号撞风控会把 B 账号也锁死 —— 多账号运营直接废掉。
        """
        registry = risk.RiskRegistry()
        registry.trip('unb-A', 'FAIL_SYS_USER_VALIDATE')
        registry.trip('unb-A', 'FAIL_SYS_USER_VALIDATE')
        registry.trip('unb-B', 'FAIL_SYS_USER_VALIDATE')

        self.assertEqual(registry.get('unb-A').consecutive_hits, 2)
        self.assertEqual(registry.get('unb-B').consecutive_hits, 1)
        self.assertTrue(registry.get('unb-A').is_blocked)
        self.assertFalse(registry.get('unb-C').is_blocked)

    def test_registry_session_expired_does_not_count(self):
        registry = risk.RiskRegistry()
        self.assertEqual(registry.trip('unb-D', 'FAIL_SYS_SESSION_EXPIRED::Session过期'), 0)
        self.assertEqual(registry.get('unb-D').consecutive_hits, 0)
        self.assertFalse(registry.get('unb-D').is_blocked)

    def test_registry_reset_one_or_all(self):
        registry = risk.RiskRegistry()
        registry.trip('unb-E', 'FAIL_SYS_USER_VALIDATE')
        registry.trip('unb-F', 'FAIL_SYS_USER_VALIDATE')
        registry.reset('unb-E')
        self.assertFalse(registry.get('unb-E').is_blocked)
        self.assertTrue(registry.get('unb-F').is_blocked)
        registry.reset()
        self.assertFalse(registry.get('unb-F').is_blocked)

    def test_registry_status_snapshot(self):
        registry = risk.RiskRegistry()
        registry.trip('unb-G', 'FAIL_SYS_USER_VALIDATE')
        status = registry.status()
        self.assertIn('unb-G', status)
        self.assertTrue(status['unb-G']['blocked'])
        self.assertGreater(status['unb-G']['remaining_seconds'], 0)


class FingerprintTest(unittest.TestCase):
    def test_ua_is_measured_not_invented(self):
        """UA 必须是本机正式版 Chrome 的实测值，不许自造。"""
        self.assertIn('Windows NT 10.0; Win64; x64', fingerprint.CHROME_UA)
        self.assertIn(f'Chrome/{fingerprint.CHROME_MAJOR}.0.0.0', fingerprint.CHROME_UA)
        self.assertTrue(fingerprint.CHROME_UA.startswith('Mozilla/5.0 '))

    def test_grease_brand_exact(self):
        # 实测是 `Not/A)Brand`，不是网上常见的猜测值 `Not=A?Brand`。
        self.assertEqual(fingerprint.GREASE_BRAND, 'Not/A)Brand')
        self.assertIn('Not/A)Brand";v="99"', fingerprint.SEC_CH_UA)

    def test_sec_ch_ua_versions_match_major(self):
        self.assertIn(f'"{fingerprint.CHROME_MAJOR}"', fingerprint.SEC_CH_UA)
        self.assertEqual(fingerprint.SEC_CH_UA_MOBILE, '?0')
        self.assertEqual(fingerprint.SEC_CH_UA_PLATFORM, '"Windows"')

    def test_headers_lowercase_and_overridable(self):
        headers = fingerprint.headers()
        for key in headers:
            self.assertEqual(key, key.lower(), 'httpx header 键必须小写')
        self.assertEqual(headers['user-agent'], fingerprint.CHROME_UA)
        self.assertEqual(headers['sec-ch-ua'], fingerprint.SEC_CH_UA)

        merged = fingerprint.headers({'cookie': 'unb=1'})
        self.assertEqual(merged['cookie'], 'unb=1')
        # extra 不得污染模块级常量
        self.assertNotIn('cookie', fingerprint.FINGERPRINT_HEADERS)


class MtopTest(unittest.TestCase):
    def test_sign_is_md5_of_token_t_appkey_data(self):
        import hashlib

        expected = hashlib.md5(b'tok&1700000000000&34839810&{"a":1}').hexdigest()
        self.assertEqual(mtop.generate_sign('1700000000000', 'tok', '{"a":1}'), expected)

    def test_token_from_cookies(self):
        # _m_h5_tk 的形状是 `<token>_<expire_ms>`，签名只用前半段。
        cookies = '_m_h5_tk=abc123_1699999999999; _m_h5_tk_enc=xyz'
        self.assertEqual(mtop.token_from_cookies(cookies), 'abc123')
        self.assertEqual(mtop.token_from_cookies('unb=1'), '')

    def test_parse_ret_requires_success(self):
        """success 判定必须是「ret 里含 SUCCESS」。

        若判成「ret 里不含 FAIL」，那么「接口不存在」这类返回值会被当成功，
        上层拿着空 data 一路走下去，报错位置离真因十万八千里。
        """
        ok, message, ret = mtop.parse_ret({'ret': ['SUCCESS::调用成功'], 'data': {'x': 1}})
        self.assertTrue(ok)
        self.assertEqual(ret, ['SUCCESS::调用成功'])

        ok, message, _ = mtop.parse_ret({'ret': ['FAIL_SYS_ILLEGAL_ACCESS::非法请求']})
        self.assertFalse(ok)
        self.assertIn('FAIL_SYS_ILLEGAL_ACCESS', message)

        ok, _, _ = mtop.parse_ret({'ret': ['FAIL_SYS_API_NOT_FOUND::接口不存在']})
        self.assertFalse(ok)

        ok, _, _ = mtop.parse_ret({})
        self.assertFalse(ok)

    def test_encode_data_compact_and_utf8(self):
        # 签名对 data 字符串敏感：必须是紧凑分隔符 + 不转义中文，否则签名对不上。
        encoded = mtop.encode_data({'title': '中文标题', 'price': 1})
        self.assertNotIn(' ', encoded)
        self.assertIn('中文标题', encoded)

    def test_call_params_shape(self):
        params = mtop.call_params('mtop.taobao.idle.pc.detail', '1.0', {'itemId': '1'},
                                  token='tok', t='1700000000000')
        for key in ('jsv', 'appKey', 't', 'sign', 'v', 'type', 'dataType', 'api',
                    'sessionOption', 'spm_cnt'):
            self.assertIn(key, params)
        self.assertEqual(params['appKey'], '34839810')
        self.assertEqual(params['v'], '1.0')
        self.assertEqual(params['type'], 'originaljson')
        self.assertEqual(params['api'], 'mtop.taobao.idle.pc.detail')
        self.assertEqual(params['sessionOption'], 'AutoLoginOnly')

    def test_api_url(self):
        self.assertEqual(
            mtop.api_url('mtop.taobao.idle.pc.detail', '1.0'),
            'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/',
        )

    def test_merge_token_cookies_only_touches_h5_tk(self):
        """只合并 _m_h5_tk / _m_h5_tk_enc，不碰其它 Cookie。

        响应里可能带一堆访客 Cookie，全量合并会把登录态冲掉。
        """
        class FakeResponse:
            cookies = {'_m_h5_tk': 'new_1', 'cna': 'visitor'}

        merged = mtop.merge_token_cookies('unb=1; _m_h5_tk=old_1', FakeResponse())
        parsed = ck.parse_cookie_string(merged)
        self.assertEqual(parsed['_m_h5_tk'], 'new_1')
        self.assertNotIn('cna', parsed)
        self.assertEqual(parsed['unb'], '1')

    def test_device_id_shape(self):
        device_id = mtop.generate_device_id('12345')
        self.assertTrue(device_id.endswith('-12345'))
        # UUID 本体必须是标准 36 位（自己拼十六进制会拼出位数不对的假 UUID）。
        uuid_part = device_id[: -len('-12345')]
        self.assertEqual(len(uuid_part), 36)
        self.assertEqual(uuid_part.count('-'), 4)


class NoProxySanitizeTest(unittest.TestCase):
    """NO_PROXY 消毒。旧项目扫码登录整体瘫痪的真因就在这里。"""

    def _restore(self, key, original):
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    def test_bracket_entry_is_unbracketed(self):
        from backend.app.xianyu import net

        original = os.environ.get('NO_PROXY')
        try:
            # 复现本机真实值。
            os.environ['NO_PROXY'] = 'localhost,127.0.0.1,[::1]'
            net.sanitize_no_proxy()
            self.assertNotIn('[', os.environ['NO_PROXY'])
            self.assertIn('::1', os.environ['NO_PROXY'])
            # 去掉方括号后 httpx 必须能构造出来（构造阶段就抛才是要修的 bug）。
            import httpx

            client = httpx.Client()
            client.close()
        finally:
            self._restore('NO_PROXY', original)
            net.sanitize_no_proxy()

    def test_bracket_with_port(self):
        from backend.app.xianyu import net

        original = os.environ.get('NO_PROXY')
        try:
            os.environ['NO_PROXY'] = '[::1]:8080,localhost'
            net.sanitize_no_proxy()
            self.assertIn('::1:8080', os.environ['NO_PROXY'])
            self.assertNotIn('[', os.environ['NO_PROXY'])
        finally:
            self._restore('NO_PROXY', original)
            net.sanitize_no_proxy()

    def test_clean_value_untouched(self):
        from backend.app.xianyu import net

        original = os.environ.get('NO_PROXY')
        try:
            os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1'
            self.assertEqual(net.sanitize_no_proxy(), [])
            self.assertEqual(os.environ['NO_PROXY'], 'localhost,127.0.0.1,::1')
        finally:
            self._restore('NO_PROXY', original)
            net.sanitize_no_proxy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
