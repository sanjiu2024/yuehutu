# -*- coding: utf-8 -*-
"""客服分流写接口（保存分流规则 + 客服分组增删改）的测试。

**为什么单开一组**：这是「会真的改动卖家客服分流配置与分组」的写操作。最容易犯的错：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 账号掉线时先报「去重新登录」，用户填半天才发现分组名是空的
   （三步顺序必须是 **确认 → 字段校验 → 建客户端**）；
3. 把复合结构当普通字段发出去 —— 官方对这两个接口是
   `data:{routingRules: JSON.stringify(rule)}` / `{serviceRange: JSON.stringify(...),
   customers: JSON.stringify(...)}`，**字符串里再套 JSON**，发成对象平台会拒；
4. 编造平台没回显的 `groupId`，或拿我们发出去的内容冒充平台回执；
5. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 之类的平台拒绝当成掉线，让用户白扫一次码。

**一次真机写请求都没发过**（这会真的改卖家的分流配置，不允许为了验证去动）。
这里用假协议层钉住「字段形状与官方 bundle 逐字一致」，平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_shop_write_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
"""
from __future__ import annotations

import json
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
from backend.app.xianyu import shop_write as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'sw_acc'
BASE = '/api/shop'

#: 官方前端**从不读**这两个接口的响应体（保存后靠 toast + 重拉列表），所以成功体可能是空对象。
PLAIN_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
            'ret': ['SUCCESS::调用成功']}
#: 平台真的回显了规则（我们才回显；拿不到就是 None）
RULE_ECHOED = {'success': True,
               'data': {'routingRules': {'distributionMethod': 'COMBINED_LOW_LOAD',
                                         'repeatCustomer': 'LAST_SERVED_AGENT',
                                         'offlineMessage': 'MAIN_ACCOUNT'}},
               'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}
#: 平台回显了新建分组的 id
GROUP_ECHOED = {'success': True, 'data': {'groupId': 'G9001'},
                'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}
#: 真机原文：卖家工作台无权限
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

RULE_BODY: Dict[str, Any] = {
    'distribution_method': 'LOW_SATURATION_FIRST',
    'repeat_customer': 'LAST_SERVED_AGENT',
    'offline_message': 'MAIN_ACCOUNT',
    'service_time': {'start_time': '08:00', 'end_time': '23:00'},
    'confirm': True,
}
SPECIFIED_RULE_BODY: Dict[str, Any] = {
    'distribution_method': 'COMBINED_LOW_LOAD',
    'repeat_customer': 'LOW_DAILY_COUNT_FIRST',
    'offline_message': 'SPECIFIED_ACCOUNT',
    'specified_account_id': '999',
    'specified_account_nick': '小助手',
    'confirm': True,
}
GROUP_BODY: Dict[str, Any] = {
    'group_name': '售前组',
    'mode': 'unspecified',
    'customer_uids': ['111', 222],
    'confirm': True,
}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeShopWriteClient:
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


def install_fake_protocol(client: FakeShopWriteClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeShopWriteClient:
        # `common._note_session_expired` 靠 `client.account_id` 找要标记的账号
        client.account_id = account_id
        return client

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


def full_client(**overrides: Any) -> FakeShopWriteClient:
    payloads = {proto.ROUTING_RULE_API: PLAIN_OK, proto.GROUP_API: PLAIN_OK}
    payloads.update(overrides)
    return FakeShopWriteClient(payloads)


# --------------------------------------------------------------------------- 协议层


class ShopWriteProtocolTest(unittest.TestCase):
    """纯函数：接口名、入参形状、确认闸门、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.ROUTING_RULE_API,
                         'mtop.taobao.idlemessage.customer.routing.rule.operate')
        self.assertEqual(proto.GROUP_API,
                         'mtop.taobao.idlemessage.customer.group.operate')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_whitelist_contains_exactly_the_two_apis(self):
        self.assertEqual(proto.ALL_APIS, frozenset({proto.ROUTING_RULE_API, proto.GROUP_API}))
        self.assertEqual(proto.ACTIONS, ('routing_rule', 'group_save', 'group_delete'))
        # 新增/修改/删除共用同一个接口名
        self.assertEqual(proto.WRITE_APIS['group_save']['api'], proto.GROUP_API)
        self.assertEqual(proto.WRITE_APIS['group_delete']['api'], proto.GROUP_API)

    def test_assert_write_rejects_read_apis(self):
        self.assertEqual(proto.assert_write(proto.ROUTING_RULE_API), proto.ROUTING_RULE_API)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write('mtop.taobao.idlemessage.customer.routing.rule.info.get')
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write('mtop.taobao.idlemessage.customer.group.get')

    def test_confirm_gate_is_identity_not_truthiness(self):
        for bad in (None, False, 0, '', 'false', 'yes', 2):
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.routing_rule_request('LOW_SATURATION_FIRST', 'LAST_SERVED_AGENT',
                                           'MAIN_ACCOUNT', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.group_save_request('组', 'unspecified', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.group_delete_request('G1', confirm=bad)

    def test_routing_rule_is_a_json_string(self):
        api, data = proto.routing_rule_request(
            'LOW_SATURATION_FIRST', 'LAST_SERVED_AGENT', 'MAIN_ACCOUNT',
            service_time={'start_time': '08:00', 'end_time': '23:00'}, confirm=True)
        self.assertEqual(api, proto.ROUTING_RULE_API)
        self.assertEqual(list(data), ['routingRules'])
        self.assertIsInstance(data['routingRules'], str)
        self.assertEqual(json.loads(data['routingRules']), {
            'distributionMethod': 'LOW_SATURATION_FIRST',
            'repeatCustomer': 'LAST_SERVED_AGENT',
            'offlineMessage': 'MAIN_ACCOUNT',
            'serviceTime': {'startTime': '08:00', 'endTime': '23:00'},
        })

    def test_routing_rule_omits_service_time_when_not_given(self):
        _, data = proto.routing_rule_request('COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT',
                                             'MAIN_ACCOUNT', confirm=True)
        self.assertNotIn('serviceTime', json.loads(data['routingRules']))

    def test_specified_account_only_when_offline_message_says_so(self):
        _, data = proto.routing_rule_request(
            'COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT', 'SPECIFIED_ACCOUNT',
            specified_account_id='999', specified_account_nick='小助手', confirm=True)
        rule = json.loads(data['routingRules'])
        self.assertEqual(rule['specifiedAccountId'], 999)  # 官方是 Number(_)
        self.assertEqual(rule['specifiedAccountNick'], '小助手')

        # MAIN_ACCOUNT 时即使传了也不发（官方原文就是条件展开）
        _, plain = proto.routing_rule_request(
            'COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT', 'MAIN_ACCOUNT',
            specified_account_id='999', specified_account_nick='小助手', confirm=True)
        rule = json.loads(plain['routingRules'])
        self.assertNotIn('specifiedAccountId', rule)
        self.assertNotIn('specifiedAccountNick', rule)

    def test_specified_account_requires_both_id_and_nick(self):
        with self.assertRaises(ValueError):
            proto.routing_rule_request('COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT',
                                       'SPECIFIED_ACCOUNT', specified_account_nick='小助手',
                                       confirm=True)
        with self.assertRaises(ValueError):
            proto.routing_rule_request('COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT',
                                       'SPECIFIED_ACCOUNT', specified_account_id='999',
                                       confirm=True)

    def test_routing_enums_are_validated(self):
        for kwargs in (
            {'distribution_method': 'WHATEVER'},
            {'repeat_customer': 'WHATEVER'},
            {'offline_message': 'WHATEVER'},
        ):
            payload = {'distribution_method': 'COMBINED_LOW_LOAD',
                       'repeat_customer': 'LAST_SERVED_AGENT',
                       'offline_message': 'MAIN_ACCOUNT'}
            payload.update(kwargs)
            with self.assertRaises(ValueError):
                proto.routing_rule_request(confirm=True, **payload)

    def test_service_time_must_be_a_pair(self):
        with self.assertRaises(ValueError):
            proto.routing_rule_request('COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT', 'MAIN_ACCOUNT',
                                       service_time='08:00-23:00', confirm=True)
        with self.assertRaises(ValueError):
            proto.routing_rule_request('COMBINED_LOW_LOAD', 'LAST_SERVED_AGENT', 'MAIN_ACCOUNT',
                                       service_time={'start_time': '08:00'}, confirm=True)

    def test_group_create_shape(self):
        api, data = proto.group_save_request('售前组', 'unspecified', customer_uids=['111', 222],
                                             confirm=True)
        self.assertEqual(api, proto.GROUP_API)
        self.assertEqual(data['operateType'], 'create')
        self.assertEqual(data['groupName'], '售前组')
        self.assertNotIn('groupId', data)  # 新增不带 id（官方只在编辑态带）
        self.assertEqual(json.loads(data['serviceRange']), {'mode': 'unspecified'})
        self.assertEqual(json.loads(data['customers']),
                         [{'customerUid': 111}, {'customerUid': 222}])

    def test_group_update_shape_carries_group_id(self):
        _, data = proto.group_save_request('售前组', 'unspecified', operate_type='update',
                                           group_id='G1', confirm=True)
        self.assertEqual(data['operateType'], 'update')
        self.assertEqual(data['groupId'], 'G1')

    def test_group_update_requires_group_id(self):
        with self.assertRaises(ValueError):
            proto.group_save_request('售前组', 'unspecified', operate_type='update', confirm=True)

    def test_group_specified_scope_builds_specified_config(self):
        _, data = proto.group_save_request(
            '售前组', 'specified', specified_type_name='订单类型', type_data=['A', 'B'],
            confirm=True)
        self.assertEqual(json.loads(data['serviceRange']), {
            'mode': 'specified',
            'specifiedConfig': {'type': 'ORDER', 'typeName': '订单类型', 'typeData': ['A', 'B']},
        })

    def test_group_specified_type_defaults_to_order_and_skips_empty_type_data(self):
        _, data = proto.group_save_request('售前组', 'specified', type_data=[], confirm=True)
        self.assertEqual(json.loads(data['serviceRange']),
                         {'mode': 'specified', 'specifiedConfig': {'type': 'ORDER'}})

    def test_group_unspecified_scope_has_no_specified_config(self):
        _, data = proto.group_save_request('售前组', 'unspecified', specified_type='ORDER',
                                           specified_type_name='订单类型', confirm=True)
        self.assertEqual(json.loads(data['serviceRange']), {'mode': 'unspecified'})

    def test_group_mode_case_is_passed_through_untouched(self):
        """写侧枚举是小写、读侧渲染判大写 —— 我们**不转换**，原样透传并接受已知写法。"""
        for mode in ('unspecified', 'specified', 'UNSPECIFIED', 'SPECIFIED', 'DEFAULT'):
            _, data = proto.group_save_request('售前组', mode, confirm=True)
            self.assertEqual(json.loads(data['serviceRange'])['mode'], mode)
        with self.assertRaises(ValueError):
            proto.group_save_request('售前组', 'nope', confirm=True)

    def test_group_name_is_required_and_bounded(self):
        with self.assertRaises(ValueError):
            proto.group_save_request('', 'unspecified', confirm=True)
        with self.assertRaises(ValueError):
            proto.group_save_request('名' * (proto.GROUP_NAME_MAX + 1), 'unspecified',
                                     confirm=True)

    def test_group_customers_skip_blank_uids(self):
        _, data = proto.group_save_request('售前组', 'unspecified',
                                           customer_uids=['111', '', None, '  '], confirm=True)
        self.assertEqual(json.loads(data['customers']), [{'customerUid': 111}])

    def test_group_delete_sends_only_two_keys(self):
        api, data = proto.group_delete_request('G1', confirm=True)
        self.assertEqual(api, proto.GROUP_API)
        self.assertEqual(data, {'operateType': 'delete', 'groupId': 'G1'})
        with self.assertRaises(ValueError):
            proto.group_delete_request('', confirm=True)

    def test_request_of_dispatches_and_rejects_unknown_actions(self):
        api, data = proto.request_of('routing_rule', distribution_method='COMBINED_LOW_LOAD',
                                     repeat_customer='LAST_SERVED_AGENT',
                                     offline_message='MAIN_ACCOUNT', confirm=True)
        self.assertEqual(api, proto.ROUTING_RULE_API)
        self.assertIn('routingRules', data)
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('nope', confirm=True)

    def test_json_helper_matches_stringify_shape(self):
        """中文不转义、无多余空格 —— 贴近官方 `JSON.stringify` 的字节。"""
        self.assertEqual(proto._json({'groupName': '售前组'}), '{"groupName":"售前组"}')

    def test_success_needs_client_ok_and_no_platform_error_code(self):
        self.assertTrue(proto.is_success(PLAIN_OK))
        self.assertTrue(proto.is_success({'success': True, 'data': {'code': 'success'}}))
        self.assertFalse(proto.is_success({'success': True, 'data': {'code': 'FAIL_XX'}}))
        self.assertFalse(proto.is_success({'success': False, 'message': 'boom'}))
        self.assertFalse(proto.is_success(None))
        self.assertFalse(proto.is_success('nope'))

    def test_failure_reason_prefers_platform_message_then_client_ret(self):
        self.assertEqual(proto.failure_reason(NO_PERMISSION),
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        self.assertEqual(proto.failure_reason({'success': False, 'data': {'msg': '平台说的'},
                                               'message': '客户端说的'}), '平台说的')
        self.assertEqual(proto.failure_reason({'success': False, 'ret': ['R1::原因']}), 'R1::原因')
        self.assertEqual(proto.failure_reason({'success': False}, fallback='兜底'), '兜底')

    def test_envelope_error_sees_the_inner_business_code(self):
        """双层信封：外层 success 为真、内层 data.code 失败 —— 必须判失败。

        这是写接口最危险的一类假成功（只看外层会把平台拒绝当「保存成功」）。
        """
        self.assertEqual(proto.envelope_error(
            {'success': True, 'data': {'code': 'FAIL_XX', 'msg': '平台说不行'}}), '平台说不行')
        # 再嵌一层（`data.data`）也认
        self.assertEqual(proto.envelope_error(
            {'success': True, 'data': {'data': {'code': 'FAIL_YY', 'msg': '更深一层'}}}),
            '更深一层')
        # 没有 code 键 → 以客户端结论为准（成功体可能是空对象）
        self.assertEqual(proto.envelope_error(PLAIN_OK), '')
        self.assertEqual(proto.envelope_error(
            {'success': True, 'data': {'code': 'success'}}), '')
        # 客户端层失败照样认
        self.assertEqual(proto.envelope_error(NO_PERMISSION),
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')

    def test_saved_rule_is_none_when_platform_does_not_echo(self):
        self.assertIsNone(proto.saved_rule_of(PLAIN_OK))
        self.assertEqual(proto.saved_rule_of(RULE_ECHOED),
                         RULE_ECHOED['data']['routingRules'])

    def test_saved_rule_accepts_a_json_string_body(self):
        raw = {'success': True, 'data': {'routingRules': '{"offlineMessage":"MAIN_ACCOUNT"}'}}
        self.assertEqual(proto.saved_rule_of(raw), {'offlineMessage': 'MAIN_ACCOUNT'})
        broken = {'success': True, 'data': {'routingRules': '{not json'}}
        self.assertIsNone(proto.saved_rule_of(broken))

    def test_group_id_is_none_when_platform_does_not_echo(self):
        self.assertIsNone(proto.group_id_of(PLAIN_OK))
        self.assertEqual(proto.group_id_of(GROUP_ECHOED), 'G9001')
        self.assertEqual(proto.group_id_of({'success': True, 'data': {'groupId': 12345}}), '12345')


# --------------------------------------------------------------------------- HTTP


class ShopWritePageTest(unittest.TestCase):
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
            "INSERT INTO users(username, password_hash, is_admin) VALUES('shopwrite','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

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

    def rule_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/cs-routing/rule'

    def groups_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/cs-groups'

    def group_delete_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/cs-groups/delete'


class ShopWriteConfirmGateTest(ShopWritePageTest):
    """红线：未确认时**一个平台请求都不许发**。"""

    def test_rule_without_confirm_is_400_and_sends_nothing(self):
        payload = self.fail(self.rule_path(), {k: v for k, v in RULE_BODY.items()
                                               if k != 'confirm'})
        self.assertEqual(payload.get('code'), 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_group_save_without_confirm_is_400_and_sends_nothing(self):
        payload = self.fail(self.groups_path(), {k: v for k, v in GROUP_BODY.items()
                                                 if k != 'confirm'})
        self.assertEqual(payload.get('code'), 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_group_delete_without_confirm_is_400_and_sends_nothing(self):
        payload = self.fail(self.group_delete_path(), {'group_id': 'G1'})
        self.assertEqual(payload.get('code'), 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_confirm_false_like_values_are_not_consent(self):
        """`'false'` / `0` / `''` / `None` 都不算点头（`bool('false')` 是 True，很危险）。"""
        for value in (False, 'false', 0, '', None, 'no'):
            body = dict(RULE_BODY, confirm=value)
            payload = self.fail(self.rule_path(), body)
            self.assertEqual(payload.get('code'), 'confirmation_required', payload)
        self.assertEqual(self.fake.calls, [])

    def test_confirm_true_is_accepted(self):
        self.ok_data(self.rule_path(), RULE_BODY)
        self.assertEqual(self.fake.apis(), [proto.ROUTING_RULE_API])


class ShopWriteValidationTest(ShopWritePageTest):
    """字段校验必须在建客户端之前（纯本地、免费）。"""

    def test_field_validation_runs_before_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        payload = self.fail(self.groups_path(), {'group_name': '', 'mode': 'unspecified',
                                                 'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('不能为空', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [], '字段没填就不该去建客户端')

        # 字段齐了才轮到凭据检查（409 而不是 400）
        payload = self.fail(self.groups_path(), GROUP_BODY, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_bad_enum_is_400(self):
        payload = self.fail(self.rule_path(), dict(RULE_BODY, distribution_method='NOPE'))
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_specified_offline_message_requires_account(self):
        body = dict(RULE_BODY, offline_message='SPECIFIED_ACCOUNT')
        payload = self.fail(self.rule_path(), body)
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertIn('specified_account_id', payload.get('message', ''))
        self.assertEqual(self.fake.calls, [])

    def test_update_without_group_id_is_400(self):
        payload = self.fail(self.groups_path(), dict(GROUP_BODY, operate_type='update'))
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_delete_without_group_id_is_400(self):
        payload = self.fail(self.group_delete_path(), {'confirm': True})
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_group_name_too_long_is_400(self):
        payload = self.fail(self.groups_path(),
                            dict(GROUP_BODY, group_name='名' * (proto.GROUP_NAME_MAX + 1)))
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_bad_service_time_is_400(self):
        payload = self.fail(self.rule_path(), dict(RULE_BODY, service_time={'start_time': '08:00'}))
        self.assertEqual(payload.get('code'), 'invalid_fields')
        self.assertEqual(self.fake.calls, [])


class ShopWriteHappyPathTest(ShopWritePageTest):
    """真正发出去的数据形状必须与官方 bundle 逐字一致。"""

    def test_routing_rule_sends_a_json_string_with_official_field_names(self):
        data = self.ok_data(self.rule_path(), RULE_BODY)
        call = self.fake.call_of(proto.ROUTING_RULE_API)
        self.assertEqual(call['v'], proto.DEFAULT_VERSION)
        self.assertEqual(list(call['data']), ['routingRules'])
        self.assertIsInstance(call['data']['routingRules'], str)
        self.assertEqual(json.loads(call['data']['routingRules']), {
            'distributionMethod': 'LOW_SATURATION_FIRST',
            'repeatCustomer': 'LAST_SERVED_AGENT',
            'offlineMessage': 'MAIN_ACCOUNT',
            'serviceTime': {'startTime': '08:00', 'endTime': '23:00'},
        })
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'routing_rule')
        self.assertEqual(data['api'], proto.ROUTING_RULE_API)

    def test_routing_rule_echoes_platform_rule_when_present(self):
        self.fake.payloads[proto.ROUTING_RULE_API] = RULE_ECHOED
        data = self.ok_data(self.rule_path(), RULE_BODY)
        self.assertEqual(data['rule'], RULE_ECHOED['data']['routingRules'])
        self.assertEqual(data['warnings'], [])

    def test_routing_rule_warns_when_platform_does_not_echo(self):
        data = self.ok_data(self.rule_path(), RULE_BODY)
        self.assertIsNone(data['rule'])
        self.assertIn('平台没有回显保存后的分流规则', ' '.join(data['warnings']))
        # 我们**实际发出去**的规则仍然给用户看（本地回执，不是平台回执）
        self.assertEqual(data['submitted']['distributionMethod'], 'LOW_SATURATION_FIRST')

    def test_specified_rule_sends_account_as_number(self):
        self.ok_data(self.rule_path(), SPECIFIED_RULE_BODY)
        rule = json.loads(self.fake.call_of(proto.ROUTING_RULE_API)['data']['routingRules'])
        self.assertEqual(rule['specifiedAccountId'], 999)
        self.assertEqual(rule['specifiedAccountNick'], '小助手')

    def test_group_create_sends_json_strings_for_range_and_customers(self):
        data = self.ok_data(self.groups_path(), GROUP_BODY)
        call = self.fake.call_of(proto.GROUP_API)
        self.assertEqual(call['v'], proto.DEFAULT_VERSION)
        self.assertEqual(call['data']['operateType'], 'create')
        self.assertEqual(call['data']['groupName'], '售前组')
        self.assertNotIn('groupId', call['data'])
        self.assertEqual(json.loads(call['data']['serviceRange']), {'mode': 'unspecified'})
        self.assertEqual(json.loads(call['data']['customers']),
                         [{'customerUid': 111}, {'customerUid': 222}])
        self.assertTrue(data['saved'])
        self.assertEqual(data['operate_type'], 'create')

    def test_group_create_warns_when_platform_does_not_echo_the_id(self):
        data = self.ok_data(self.groups_path(), GROUP_BODY)
        self.assertIsNone(data['group_id'])
        self.assertIn('没有回显新分组的 id', ' '.join(data['warnings']))

    def test_group_create_keeps_echoed_id(self):
        self.fake.payloads[proto.GROUP_API] = GROUP_ECHOED
        data = self.ok_data(self.groups_path(), GROUP_BODY)
        self.assertEqual(data['group_id'], 'G9001')
        self.assertEqual(data['warnings'], [])

    def test_group_update_keeps_the_given_id(self):
        body = dict(GROUP_BODY, operate_type='update', group_id='G7')
        data = self.ok_data(self.groups_path(), body)
        self.assertEqual(data['group_id'], 'G7')
        self.assertEqual(data['warnings'], [], '编辑态不需要「没回显 id」这条 warning')
        self.assertEqual(self.fake.call_of(proto.GROUP_API)['data']['groupId'], 'G7')

    def test_group_specified_scope_sends_specified_config(self):
        body = dict(GROUP_BODY, mode='specified', specified_type_name='订单类型',
                    type_data=['A'])
        self.ok_data(self.groups_path(), body)
        scope = json.loads(self.fake.call_of(proto.GROUP_API)['data']['serviceRange'])
        self.assertEqual(scope, {'mode': 'specified',
                                 'specifiedConfig': {'type': 'ORDER', 'typeName': '订单类型',
                                                     'typeData': ['A']}})

    def test_group_delete_sends_exactly_two_keys(self):
        data = self.ok_data(self.group_delete_path(), {'group_id': 'G1', 'confirm': True})
        call = self.fake.call_of(proto.GROUP_API)
        self.assertEqual(call['data'], {'operateType': 'delete', 'groupId': 'G1'})
        self.assertTrue(data['deleted'])
        self.assertEqual(data['operate_type'], 'delete')

    def test_writes_are_logged_and_published(self):
        from backend.app.core import events
        before = len(events.recent(limit=500))
        self.ok_data(self.rule_path(), RULE_BODY)
        self.ok_data(self.groups_path(), GROUP_BODY)
        self.ok_data(self.group_delete_path(), {'group_id': 'G1', 'confirm': True})
        actions = [row['action'] for row in self.logs()]
        self.assertIn('cs_routing_rule_save', actions)
        self.assertIn('cs_group_save', actions)
        self.assertIn('cs_group_delete', actions)
        self.assertTrue(all(int(row['success']) == 1 for row in self.logs()))
        topics = [event['topic'] for event in events.recent(limit=500)[before:]]
        self.assertIn('shop.changed', topics)


class ShopWriteFailureTest(ShopWritePageTest):
    """失败路径：平台拒绝 ≠ 掉线；掉线必须回写登录态。"""

    def test_platform_refusal_is_502_and_does_not_touch_login_state(self):
        self.fake.payloads[proto.ROUTING_RULE_API] = NO_PERMISSION
        payload = self.fail(self.rule_path(), RULE_BODY, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', payload.get('message', ''))
        self.assertEqual(payload.get('platform_errors', {}).get('api'), proto.ROUTING_RULE_API)
        self.assertEqual(self.login_state(), 'active', '无权限不是掉线，不许改登录态')

    def test_platform_business_error_inside_envelope_is_502(self):
        self.fake.payloads[proto.GROUP_API] = {
            'success': True, 'data': {'code': 'FAIL_XX', 'msg': '平台说不行'}}
        payload = self.fail(self.groups_path(), GROUP_BODY, status=502)
        self.assertEqual(payload.get('code'), 'protocol_error')
        self.assertIn('平台说不行', payload.get('message', ''))

    def test_session_expired_is_409_and_writes_need_relogin(self):
        self.fake.errors[proto.GROUP_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::过期')
        payload = self.fail(self.groups_path(), GROUP_BODY, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_missing_cookie_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        payload = self.fail(self.rule_path(), RULE_BODY, status=409)
        self.assertEqual(payload.get('code'), 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_and_sends_nothing(self):
        payload = self.fail(f'{BASE}/nope/cs-routing/rule', RULE_BODY, status=404)
        self.assertEqual(payload.get('code'), 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_unauthenticated_is_401(self):
        resp = self.post(self.rule_path(), RULE_BODY, headers={})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.fake.calls, [])

    def test_protocol_layer_without_mtop_is_503(self):
        class NoMtop:
            pass

        remove_fake_protocol()
        module = types.ModuleType('backend.app.xianyu.client')
        module.get_client = lambda account_id: NoMtop()  # type: ignore[attr-defined]
        module.XianyuClient = NoMtop  # type: ignore[attr-defined]
        sys.modules['backend.app.xianyu.client'] = module
        payload = self.fail(self.rule_path(), RULE_BODY, status=503)
        self.assertEqual(payload.get('code'), 'xianyu_not_ready')


class ShopWriteRouteTest(unittest.TestCase):
    """路由登记：三条 POST 必须在，且路径与前端/契约一致。"""

    def test_three_post_routes_are_registered(self):
        found = set()
        for route in shop_api.router.routes:
            methods = set(getattr(route, 'methods', set()) or set())
            if 'POST' in methods:
                found.add(getattr(route, 'path', ''))
        self.assertEqual(found, {f'{BASE}/{{account_id}}/cs-routing/rule',
                                 f'{BASE}/{{account_id}}/cs-groups',
                                 f'{BASE}/{{account_id}}/cs-groups/delete',
                                 # 第三十二批：店铺资料三个写端点共用同一个 router
                                 f'{BASE}/{{account_id}}/profile/nick',
                                 f'{BASE}/{{account_id}}/profile/desc',
                                 f'{BASE}/{{account_id}}/profile/avatar'})

    def test_read_routes_are_untouched(self):
        gets = {getattr(route, 'path', '') for route in shop_api.router.routes
                if 'GET' in (getattr(route, 'methods', set()) or set())}
        self.assertEqual(gets, {f'{BASE}/{{account_id}}/sub-accounts',
                                f'{BASE}/{{account_id}}/cs-routing',
                                f'{BASE}/{{account_id}}/profile'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
