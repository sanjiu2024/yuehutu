# -*- coding: utf-8 -*-
"""店铺资料（昵称 / 简介 / 头像）读 + 写的测试。

**为什么单开一组**：这三个写接口会**真的改卖家的店铺对外形象**（昵称、简介、头像），
而且官方用的是与客服分流不同的另一套包装（`Qg`，判 `result.success`、错误在
`result.data.message`），所以最容易犯的错是：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现昵称只有 2 个字；
3. 把官方没给的上限当没有（昵称官方是 **4~12** 个字符，简介没有给上限 ⇒ 我们只要求非空）；
4. 头像传二进制 —— 官方这条收的是**图片 URL**（`data:{userAvatar:'https://…'}`）；
5. 把平台没返回的字段补成 `0` / `''` —— 平台没给就是 `null`（前端显示「-」）。

**一次真机写请求都没发过**（这会真的改卖家的店铺形象，不允许为了验证去动）。
这里用假协议层钉住「字段形状与官方 bundle 逐字一致」，平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_shop_profile_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import shop as shop_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import shop_profile as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'sp_acc'
BASE = '/api/shop'

#: 官方从不读这三个写接口的响应体（成功靠 toast + 重拉详情）⇒ 成功体可能是空对象。
PLAIN_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
            'ret': ['SUCCESS::调用成功']}

#: 平台真的返回了资料（扁平在 `data` 里，与官方消费方 `data.userNick` 一致）。
DETAIL_OK = {
    'success': True,
    'data': {
        'userNick': '月狐兔_',
        'shopDesc': '只卖真货，支持验货',
        'userAvatar': 'https://img.alicdn.com/a.png',
        'industry': '数码',
        'alipayAccount': 'a***@example.com',
        'businessType': '个人',
        'proShopValidTime': '2027-01-01',
    },
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}

#: 平台什么都没给（账号没权限 / 接口没数据）——**不能补 0 或空串**。
DETAIL_EMPTY = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
                'ret': ['SUCCESS::调用成功']}

#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

NICK_BODY: Dict[str, Any] = {'nick': '月狐兔_小铺', 'confirm': True}
DESC_BODY: Dict[str, Any] = {'desc': '只卖真货，支持验货', 'confirm': True}
AVATAR_BODY: Dict[str, Any] = {'avatar_url': 'https://img.alicdn.com/a.png', 'confirm': True}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeShopProfileClient:
    """假协议层：只实现 `mtop`，记录每次调用的 (api, data, v)。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0') -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v})
        error = self.errors.get(api)
        if error is not None:
            raise error
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有准备 {api} 的响应')
        return self.payloads[api]

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]

    def call_of(self, api: str) -> Dict[str, Any]:
        for call in self.calls:
            if call['api'] == api:
                return call
        raise AssertionError(f'没有调用 {api}，实际调用: {self.apis()}')


def install_fake_protocol(client: FakeShopProfileClient, *, with_factory: bool = True) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeShopProfileClient:
        client.account_id = account_id
        return client

    if with_factory:
        module.get_client = factory  # type: ignore[attr-defined]
    module.XianyuClient = type(client)  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def full_client(**overrides: Any) -> FakeShopProfileClient:
    payloads = {
        proto.DETAIL_API: DETAIL_OK,
        proto.NICK_API: PLAIN_OK,
        proto.DESC_API: PLAIN_OK,
        proto.AVATAR_API: PLAIN_OK,
    }
    payloads.update(overrides)
    return FakeShopProfileClient(payloads)


# --------------------------------------------------------------------------- 协议层


class ShopProfileProtocolTest(unittest.TestCase):
    """纯函数：接口名、入参形状、确认闸门、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.DETAIL_API, 'mtop.alibaba.idle.shop.manage.detail')
        self.assertEqual(proto.NICK_API, 'mtop.alibaba.idle.shop.manage.nick.update')
        self.assertEqual(proto.DESC_API, 'mtop.alibaba.idle.shop.manage.desc.update')
        self.assertEqual(proto.AVATAR_API, 'mtop.alibaba.idle.shop.manage.avatar.update')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_whitelist_is_exactly_three_writes_and_one_read(self):
        self.assertEqual(proto.ALL_APIS, frozenset({
            proto.DETAIL_API, proto.NICK_API, proto.DESC_API, proto.AVATAR_API}))
        self.assertEqual(proto.READ_APIS, (proto.DETAIL_API,))
        self.assertEqual(proto.ACTIONS, ('nick', 'desc', 'avatar'))

    def test_assert_write_rejects_the_read_api(self):
        self.assertEqual(proto.assert_write(proto.NICK_API), None)
        self.assertEqual(proto.assert_write(proto.AVATAR_API), None)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.DETAIL_API)

    def test_assert_read_rejects_write_apis(self):
        self.assertEqual(proto.assert_read(proto.DETAIL_API), None)
        for api in (proto.NICK_API, proto.DESC_API, proto.AVATAR_API):
            with self.assertRaises(proto.UnknownApi):
                proto.assert_read(api)

    def test_confirm_gate_is_identity_not_truthiness(self):
        for bad in (None, False, 0, '', 'false', 'yes', 2):
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.nick_request('月狐兔_小铺', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.desc_request('简介', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.avatar_request('https://x/a.png', confirm=bad)

    def test_nick_request_shape_and_trim(self):
        api, data = proto.nick_request('  月狐兔_小铺  ', confirm=True)
        self.assertEqual(api, proto.NICK_API)
        self.assertEqual(data, {'userNick': '月狐兔_小铺'})  # 官方调用点先 trim

    def test_nick_length_matches_the_official_limits(self):
        # 官方原文：<4 → 「昵称不能少于4个字符」；>12 → 「昵称不能超过12个字符」
        # 用乘法构造，避免手数字数（上一版把 13 个字符写成 9 个，测试假绿）
        self.assertEqual(proto.nick_request('字' * 4, confirm=True)[1], {'userNick': '字' * 4})
        self.assertEqual(proto.nick_request('字' * 12, confirm=True)[1], {'userNick': '字' * 12})
        with self.assertRaises(ValueError) as ctx:
            proto.nick_request('字' * 3, confirm=True)
        self.assertIn('不能少于4个字符', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            proto.nick_request('字' * 13, confirm=True)
        self.assertIn('不能超过12个字符', str(ctx.exception))

    def test_nick_rejects_blank(self):
        for blank in ('', '   ', None):
            with self.assertRaises(ValueError):
                proto.nick_request(blank, confirm=True)

    def test_desc_request_shape(self):
        api, data = proto.desc_request('  只卖真货  ', confirm=True)
        self.assertEqual(api, proto.DESC_API)
        self.assertEqual(data, {'shopDesc': '只卖真货'})
        with self.assertRaises(ValueError):
            proto.desc_request('   ', confirm=True)

    def test_avatar_request_takes_a_url_not_binary(self):
        api, data = proto.avatar_request('https://img.alicdn.com/a.png', confirm=True)
        self.assertEqual(api, proto.AVATAR_API)
        self.assertEqual(data, {'userAvatar': 'https://img.alicdn.com/a.png'})
        with self.assertRaises(ValueError) as ctx:
            proto.avatar_request('/local/a.png', confirm=True)
        self.assertIn('http', str(ctx.exception))
        with self.assertRaises(ValueError):
            proto.avatar_request('', confirm=True)

    def test_request_of_dispatches_and_rejects_unknown(self):
        self.assertEqual(proto.request_of('nick', nick='月狐兔_小铺', confirm=True)[0],
                         proto.NICK_API)
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('unknown', confirm=True)

    def test_payload_of_accepts_flat_and_nested(self):
        nested = {'success': True, 'data': {'code': 'success', 'data': {'userNick': 'x'}}}
        self.assertEqual(proto.payload_of(nested), {'userNick': 'x'})
        self.assertEqual(proto.payload_of(DETAIL_OK)['userNick'], '月狐兔_')
        self.assertEqual(proto.payload_of('not-a-dict'), {})

    def test_parse_profile_never_invents_values(self):
        # 协议层只回「平台给了的键」+ raw；补 None 是服务层 get_profile 的事。
        parsed = proto.parse_profile(DETAIL_EMPTY)
        for _, ours in proto.PROFILE_KEYS:
            self.assertNotIn(ours, parsed, ours)
        self.assertIsNone(parsed['raw'])  # 平台什么都没给 ⇒ raw 是 None（不是空对象）

    def test_parse_profile_maps_every_known_key(self):
        parsed = proto.parse_profile(DETAIL_OK)
        self.assertEqual(parsed['user_nick'], '月狐兔_')
        self.assertEqual(parsed['shop_desc'], '只卖真货，支持验货')
        self.assertEqual(parsed['user_avatar'], 'https://img.alicdn.com/a.png')
        self.assertEqual(parsed['industry'], '数码')
        self.assertEqual(parsed['alipay_account'], 'a***@example.com')
        self.assertEqual(parsed['business_type'], '个人')
        self.assertEqual(parsed['pro_shop_valid_time'], '2027-01-01')

    def test_parse_profile_treats_blank_string_as_missing(self):
        parsed = proto.parse_profile({'success': True, 'data': {'userNick': '   ',
                                                                'shopDesc': ''}})
        self.assertNotIn('user_nick', parsed)
        self.assertNotIn('shop_desc', parsed)

    def test_is_success_handles_both_wrappers(self):
        self.assertTrue(proto.is_success(PLAIN_OK))
        self.assertTrue(proto.is_success({'success': True, 'data': {'code': 'success'}}))
        self.assertFalse(proto.is_success(NO_PERMISSION))
        self.assertFalse(proto.is_success({'success': True, 'data': {'code': 'FAIL_X'}}))
        self.assertFalse(proto.is_success(None))

    def test_failure_reason_prefers_the_platform_message(self):
        self.assertIn('无权限访问', proto.failure_reason(NO_PERMISSION))
        inner = {'success': True, 'data': {'code': 'FAIL_X', 'data': {'message': '平台说不行'}}}
        self.assertEqual(proto.failure_reason(inner), '平台说不行')

    def test_parse_write_reports_the_action_and_api(self):
        parsed = proto.parse_write(PLAIN_OK, 'nick')
        self.assertEqual(parsed['action'], 'nick')
        self.assertEqual(parsed['api'], proto.NICK_API)
        self.assertTrue(parsed['updated'])


# --------------------------------------------------------------------------- 服务 / HTTP


class ShopProfilePageTest(unittest.TestCase):
    """共用的脚手架（自挂本模块路由，不依赖 main.py 是否已挂）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fake_protocol()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        self.fake = full_client()
        install_fake_protocol(self.fake)
        app = FastAPI()
        app.include_router(shop_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('shopprofile','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, headers: Optional[Dict[str, str]] = None):
        return self.client.get(path, headers=self.headers if headers is None else headers)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body or {},
                                headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def fail(self, path: str, body: Optional[Dict[str, Any]] = None,
             status: int = 400) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    def login_state(self) -> str:
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id=?', (ACC,))
        return str(row['login_state']) if row else ''

    def logs(self) -> List[Any]:
        return db.query('SELECT action, success FROM operation_logs WHERE account_id=?', (ACC,))

    def profile_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/profile'

    def nick_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/profile/nick'

    def desc_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/profile/desc'

    def avatar_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/profile/avatar'


class ShopProfileReadTest(ShopProfilePageTest):
    """只读：平台给什么就是什么，没给的一律 None。"""

    def test_profile_returns_the_platform_values(self):
        resp = self.get(self.profile_path())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['user_nick'], '月狐兔_')
        self.assertEqual(data['shop_desc'], '只卖真货，支持验货')
        self.assertEqual(data['api'], proto.DETAIL_API)
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        self.assertEqual(self.fake.apis(), [proto.DETAIL_API])

    def test_missing_fields_are_none_not_zero(self):
        self.fake = full_client(**{proto.DETAIL_API: DETAIL_EMPTY})
        install_fake_protocol(self.fake)
        data = self.client.get(self.profile_path(), headers=self.headers).json()['data']
        for _, ours in proto.PROFILE_KEYS:
            self.assertIsNone(data[ours], ours)

    def test_empty_platform_payload_comes_with_a_warning(self):
        self.fake = full_client(**{proto.DETAIL_API: DETAIL_EMPTY})
        install_fake_protocol(self.fake)
        data = self.client.get(self.profile_path(), headers=self.headers).json()['data']
        self.assertTrue(data['warnings'])
        self.assertTrue(any('没有返回' in w['message'] for w in data['warnings']))

    def test_unknown_account_is_404(self):
        resp = self.get(self.profile_path('nope'))
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.get(self.profile_path())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_requires_login(self):
        resp = self.get(self.profile_path(), headers={})
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_platform_refusal_is_502_and_not_a_logout(self):
        self.fake = full_client(**{proto.DETAIL_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        resp = self.get(self.profile_path())
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.DETAIL_API)
        self.assertEqual(self.login_state(), 'active')

    def test_session_expiry_is_409_and_marks_need_relogin(self):
        self.fake = full_client()
        self.fake.errors[proto.DETAIL_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        install_fake_protocol(self.fake)
        resp = self.get(self.profile_path())
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.login_state(), 'need_relogin')


class ShopProfileConfirmGateTest(ShopProfilePageTest):
    """红线：未确认时**一个平台请求都不许发**。"""

    def test_nick_without_confirm_is_400_and_sends_nothing(self):
        payload = self.fail(self.nick_path(), {k: v for k, v in NICK_BODY.items()
                                               if k != 'confirm'})
        self.assertEqual(payload['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_desc_without_confirm_is_400_and_sends_nothing(self):
        payload = self.fail(self.desc_path(), {'desc': DESC_BODY['desc']})
        self.assertEqual(payload['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_avatar_without_confirm_is_400_and_sends_nothing(self):
        payload = self.fail(self.avatar_path(), {'avatar_url': AVATAR_BODY['avatar_url']})
        self.assertEqual(payload['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_false_like_confirm_values_do_not_count(self):
        for bad in (False, 0, '', 'false', 'no', None):
            body = dict(NICK_BODY, confirm=bad)
            payload = self.fail(self.nick_path(), body)
            self.assertEqual(payload['code'], 'confirmation_required', body)
        self.assertEqual(self.fake.calls, [])


class ShopProfileValidationTest(ShopProfilePageTest):
    """字段校验必须排在凭据检查**之前**（纯本地、免费，掉线时也该先报字段问题）。"""

    def test_field_validation_runs_before_credential_check(self):
        seed_account(cookie='')
        install_fake_protocol(self.fake, with_factory=False)
        payload = self.fail(self.nick_path(), dict(NICK_BODY, nick='ab'))
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertIn('4个字符', payload['message'])
        self.assertEqual(self.fake.calls, [])

        # 字段齐了才轮到凭据检查
        resp = self.post(self.nick_path(), NICK_BODY)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_blank_nick_is_rejected_locally(self):
        payload = self.fail(self.nick_path(), dict(NICK_BODY, nick='   '))
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_bad_avatar_url_is_rejected_locally(self):
        payload = self.fail(self.avatar_path(), dict(AVATAR_BODY, avatar_url='/tmp/a.png'))
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_action_is_rejected_by_the_protocol_layer(self):
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('whatever', confirm=True)
        self.assertEqual(self.fake.calls, [])


class ShopProfileHappyPathTest(ShopProfilePageTest):
    """写成功：字段形状与官方 bundle 逐字一致，且落日志、发事件。"""

    def test_nick_payload_is_verbatim(self):
        data = self.ok_data(self.nick_path(), NICK_BODY)
        call = self.fake.call_of(proto.NICK_API)
        self.assertEqual(call['data'], {'userNick': '月狐兔_小铺'})
        self.assertEqual(call['v'], '1.0')
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'nick')

    def test_desc_payload_is_verbatim(self):
        self.ok_data(self.desc_path(), DESC_BODY)
        self.assertEqual(self.fake.call_of(proto.DESC_API)['data'],
                         {'shopDesc': '只卖真货，支持验货'})

    def test_avatar_payload_is_verbatim(self):
        self.ok_data(self.avatar_path(), AVATAR_BODY)
        self.assertEqual(self.fake.call_of(proto.AVATAR_API)['data'],
                         {'userAvatar': 'https://img.alicdn.com/a.png'})

    def test_writes_are_logged_and_announced(self):
        self.ok_data(self.nick_path(), NICK_BODY)
        actions = [row['action'] for row in self.logs()]
        self.assertIn('shop_profile_nick', actions)
        topics = [event['topic'] for event in _recent_events()]
        self.assertIn('shop.changed', topics)

    def test_exactly_one_platform_call_per_write(self):
        self.ok_data(self.nick_path(), NICK_BODY)
        self.assertEqual(self.fake.apis(), [proto.NICK_API])


class ShopProfileFailureTest(ShopProfilePageTest):
    """失败路径：平台拒绝 ≠ 掉线，掉线要回写。"""

    def test_platform_refusal_is_502_and_not_a_logout(self):
        self.fake = full_client(**{proto.NICK_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        resp = self.post(self.nick_path(), NICK_BODY)
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.NICK_API)
        self.assertEqual(self.login_state(), 'active')

    def test_failed_write_is_logged_as_failure(self):
        self.fake = full_client(**{proto.NICK_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        self.post(self.nick_path(), NICK_BODY)
        rows = [row for row in self.logs() if row['action'] == 'shop_profile_nick']
        self.assertTrue(rows)
        self.assertEqual(int(rows[0]['success']), 0)

    def test_session_expiry_is_409_and_marks_need_relogin(self):
        self.fake = full_client()
        self.fake.errors[proto.NICK_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        install_fake_protocol(self.fake)
        resp = self.post(self.nick_path(), NICK_BODY)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_unknown_account_is_404_and_sends_nothing(self):
        resp = self.post(self.nick_path('nope'), NICK_BODY)
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_requires_login(self):
        resp = self.post(self.nick_path(), NICK_BODY, headers={})
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_protocol_layer_without_any_client_is_503(self):
        # 半升级场景：协议层模块在，但既没有 get_client 工厂也没有 XianyuClient。
        module = types.ModuleType('backend.app.xianyu.client')
        sys.modules['backend.app.xianyu.client'] = module
        resp = self.post(self.nick_path(), NICK_BODY)
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(self.fake.calls, [])


def _recent_events() -> List[Dict[str, Any]]:
    from backend.app.core import events
    return events.recent(limit=50)


class ShopProfileRouteTest(unittest.TestCase):
    """路由形状：只读是 GET、写全是 POST，且挂在 `/api/shop` 下。"""

    def test_profile_routes_are_registered(self):
        paths: Dict[str, set] = {}
        for route in shop_api.router.routes:
            paths.setdefault(route.path, set()).update(route.methods or set())
        self.assertIn('/api/shop/{account_id}/profile', paths)
        self.assertEqual(paths['/api/shop/{account_id}/profile'], {'GET'})
        for suffix in ('/nick', '/desc', '/avatar'):
            path = f'/api/shop/{{account_id}}/profile{suffix}'
            self.assertIn(path, paths, path)
            self.assertEqual(paths[path], {'POST'}, path)

    def test_write_routes_are_not_reachable_by_get(self):
        app = FastAPI()
        app.include_router(shop_api.router)
        with TestClient(app) as client:
            resp = client.get(f'{BASE}/{ACC}/profile/nick')
            self.assertEqual(resp.status_code, 405, resp.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
