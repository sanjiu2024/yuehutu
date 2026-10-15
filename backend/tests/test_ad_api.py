"""推广（投放）补齐接口的 HTTP 契约 + 服务层口径测试（不访问真实平台）。

三组：

* \`AdApiTest\`：自建 FastAPI 挂 \`api/ad.py\` 的路由 + **假协议层**（\`xianyu.ad\`），
  验信封 / 错误语义 / 参数形状 / 写闸门；另用真 app（\`main.create_app()\`）验只读。
* \`AdEndToEndTest\`：只换 \`xianyu.client\`（假 mtop），**真协议层参与** —— 这样
  \`requestJson\` 的实际拼装与 \`v/spm_cnt\` 参数才真的被跑到过。
* \`AdIdempotencyTest\`：走真 app，验 \`Idempotency-Key\`（中间件挂在 main 上，自建 app 验不到）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import ad as ad_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import ad as ad_service  # noqa: E402
from backend.app.services import common  # noqa: E402
from backend.app.xianyu import ad as ad_proto  # noqa: E402
from backend.app.xianyu.client import AD_SPM, XianyuError as RealXianyuError  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'ad_acc'
BASE = '/api/promotion/ad'
KEY_HEADER = 'Idempotency-Key'


class SessionExpiredError(RuntimeError):
    """与协议层同名：\`common._is_session_expired\` 按**类名**认登录态过期，不看文案。"""


class FakeXianyuClient:
    """假协议层客户端：假协议层被整体替换时，它只负责被 \`get_client\` 返回。"""

    def __init__(self) -> None:
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, **kwargs: Any) -> Any:
        raise AssertionError('假客户端的 mtop 不该被走到（协议层被替换了）')


class FakeMtopClient:
    """真协议层用的假 client：按 api 回话或抛错（形状 = 真机信封，不联网）。"""

    def __init__(self, answers: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0', **kwargs: Any) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v, 'kw': kwargs})
        error = self.errors.get(api)
        if error is not None:
            raise error
        return {'success': True, 'data': self.answers.get(api, {}),
                'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'], 'cookies_str': ''}

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


class FakeAdProtocol:
    """假协议层：按函数名回话，记录 \`(name, client, *args)\`。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[tuple] = []
        # 目录与常量照真协议层给：服务层的 kind 白名单就来自 READ_APIS。
        self.READ_APIS = ad_proto.READ_APIS
        self.MESSAGE_READ_API = ad_proto.MESSAGE_READ_API
        self.WriteNotConfirmed = ad_proto.WriteNotConfirmed

    def _answer(self, name: str, *args: Any) -> Any:
        self.calls.append((name,) + args)
        error = self.errors.get(name)
        if error is not None:
            raise error
        return self.payloads.get(name)

    async def account_info(self, client: Any, body: Any = None) -> Any:
        return self._answer('account_info', client, body)

    async def package_info(self, client: Any, body: Any = None) -> Any:
        return self._answer('package_info', client, body)

    async def item_search(self, client: Any, body: Any = None) -> Any:
        return self._answer('item_search', client, body)

    async def customize_bid_estimates(self, client: Any, body: Any = None) -> Any:
        return self._answer('customize_bid_estimates', client, body)

    async def group_list(self, client: Any, body: Any = None) -> Any:
        return self._answer('group_list', client, body)

    async def group_data(self, client: Any, body: Any = None) -> Any:
        return self._answer('group_data', client, body)

    async def campaign_log(self, client: Any, body: Any = None) -> Any:
        return self._answer('campaign_log', client, body)

    async def message_list(self, client: Any, body: Any = None) -> Any:
        return self._answer('message_list', client, body)

    async def message_detail(self, client: Any, body: Any = None) -> Any:
        return self._answer('message_detail', client, body)

    async def message_unread(self, client: Any) -> Any:
        return self._answer('message_unread', client)

    async def message_read(self, client: Any, body: Any = None, confirm: Any = False) -> Any:
        return self._answer('message_read', client, body, confirm)

    # -- 取用记录 --------------------------------------------------------

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')

    def names(self) -> List[str]:
        return [call[0] for call in self.calls]


def full_protocol(**overrides: Any) -> FakeAdProtocol:
    payloads = {kind: {'echo': kind} for kind in ad_service.READ_KINDS}
    payloads['message_read'] = {'updated': 2}
    payloads.update(overrides)
    return FakeAdProtocol(payloads)


def install_fake_protocol(protocol: FakeAdProtocol) -> FakeXianyuClient:
    """注入两个假协议模块：\`xianyu.client\`（拿客户端）与 \`xianyu.ad\`（推广接口）。"""
    client = FakeXianyuClient()
    client_module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeXianyuClient:
        # \`common._note_session_expired\` 优先用 cookie_id 定位账号（common.py:356）。
        client.account_id = account_id
        client.cookie_id = account_id
        return client

    client_module.get_client = factory  # type: ignore[attr-defined]
    client_module.XianyuClient = FakeXianyuClient  # type: ignore[attr-defined]
    client_module.AD_SPM = AD_SPM  # type: ignore[attr-defined]  # 真协议层 _mtop 会从 client 模块取
    sys.modules['backend.app.xianyu.client'] = client_module

    module = types.ModuleType('backend.app.xianyu.ad')
    for name in ('account_info', 'package_info', 'item_search', 'customize_bid_estimates',
                 'group_list', 'group_data', 'campaign_log', 'message_list', 'message_detail',
                 'message_unread', 'message_read'):
        setattr(module, name, getattr(protocol, name))
    module.READ_APIS = protocol.READ_APIS  # type: ignore[attr-defined]
    module.MESSAGE_READ_API = protocol.MESSAGE_READ_API  # type: ignore[attr-defined]
    module.WriteNotConfirmed = protocol.WriteNotConfirmed  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.ad'] = module
    return client


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)
    sys.modules.pop('backend.app.xianyu.ad', None)


def install_fake_client(account_id: str = ACC, answers: Optional[Dict[str, Any]] = None,
                        errors: Optional[Dict[str, BaseException]] = None) -> FakeMtopClient:
    """只注入假 \`xianyu.client\`，**真协议层 \`xianyu.ad\` 照常参与**。"""
    client = FakeMtopClient(answers=answers, errors=errors)
    client.account_id = account_id
    client.cookie_id = account_id
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = (lambda _account_id: client)  # type: ignore[attr-defined]
    module.XianyuClient = FakeMtopClient  # type: ignore[attr-defined]
    module.AD_SPM = AD_SPM  # type: ignore[attr-defined]  # 真协议层 _mtop 会从 client 模块取
    sys.modules['backend.app.xianyu.client'] = module
    return client


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


class AdApiTest(unittest.TestCase):
    """HTTP 契约 + 服务层口径。"""

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
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state',
                      'idempotency_keys'):
            db.execute(f'DELETE FROM {table}')
        seed_account()
        self.proto = full_protocol()
        self.fake_client = install_fake_protocol(self.proto)
        app = FastAPI()
        app.include_router(ad_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('adapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = '', headers: Optional[Dict[str, str]] = None):
        return self.client.get(f'{path}{query}',
                               headers=self.headers if headers is None else headers)

    def post(self, path: str, body: Any, headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body,
                                headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def fail_envelope(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 只读 + 登录 -----------------------------------------------------

    def test_routes_are_read_only(self):
        """对**真 app** 验只读：自建 FastAPI 防不住挂载方式变化。"""
        seed_account()
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要
        with TestClient(app_main.create_app()) as real:
            for path in (f'{BASE}/{ACC}/account-info', f'{BASE}/{ACC}/package-info',
                         f'{BASE}/{ACC}/item-search', f'{BASE}/{ACC}/bid-estimates',
                         f'{BASE}/{ACC}/groups', f'{BASE}/{ACC}/groups/data',
                         f'{BASE}/{ACC}/campaign-log', f'{BASE}/{ACC}/messages',
                         f'{BASE}/{ACC}/messages/unread', f'{BASE}/{ACC}/messages/12',
                         f'{BASE}/apis'):
                for method in ('post', 'put', 'patch', 'delete'):
                    resp = getattr(real, method)(path, headers=self.headers)
                    self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
                    self.assertFalse(resp.json().get('success'), f'{method} {path}: {resp.text}')
        self.assertEqual(self.proto.calls, [], '只读探测不该打平台')

    def test_requires_login(self):
        seed_account()
        resp = self.client.get(f'{BASE}/{ACC}/account-info')
        self.assertIn(resp.status_code, (401, 403), resp.text)

    # -- 目录 ------------------------------------------------------------

    def test_apis_catalog(self):
        data = self.ok_data(f'{BASE}/apis')
        self.assertTrue(data['protocol_ready'])
        self.assertEqual([row['kind'] for row in data['reads']], list(ad_service.READ_KINDS))
        for row in data['reads']:
            self.assertEqual(row['method'], 'GET')
            self.assertEqual(row['api'], ad_proto.READ_APIS[row['kind']]['api'])
        self.assertEqual(data['writes'], [{'action': 'message_read',
                                           'api': ad_proto.MESSAGE_READ_API,
                                           'method': 'POST', 'confirm_required': True}])

    def test_read_kinds_match_protocol(self):
        """服务层抄了一份 kind 白名单（协议层可能整个导入不进来），两份不许漂移。"""
        self.assertEqual(tuple(ad_service.READ_KINDS), tuple(ad_proto.READ_APIS))
        self.assertEqual(set(ad_service.WRITE_ACTIONS), {'message_read'})

    # -- 只读：参数形状 --------------------------------------------------

    def test_account_info_passes_body(self):
        data = self.ok_data(f'{BASE}/{ACC}/account-info',
                            '?order_price=100&promotion_mode=CUSTOMIZE&bid_type=CPC&scene=CREATION_REAL_TIME')
        self.assertEqual(data['kind'], 'account_info')
        self.assertEqual(data['api'], ad_proto.ACCOUNT_INFO_API)
        self.assertEqual(data['account_id'], ACC)
        self.assertEqual(data['source'], 'platform')
        self.assertIn('fetched_at', data)
        self.assertEqual(data['data'], {'echo': 'account_info'})
        self.assertEqual(self.proto.args_of('account_info')[1], {
            'orderPrice': '100', 'promotionMode': 'CUSTOMIZE', 'bidType': 'CPC',
            'scene': 'CREATION_REAL_TIME'})

    def test_account_info_omits_empty_fields(self):
        self.ok_data(f'{BASE}/{ACC}/account-info')
        self.assertEqual(self.proto.args_of('account_info')[1], {})

    def test_package_info_passes_body(self):
        resp = self.client.request('GET', f'{BASE}/{ACC}/package-info',
                                   json={'promotionMode': 'CUSTOMIZE'}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.proto.args_of('package_info')[1], {'promotionMode': 'CUSTOMIZE'})

    def test_package_info_empty_body_is_allowed(self):
        """没有查询条件时按空对象查（官方 initPlanInfo 也是这么发的）。"""
        self.ok_data(f'{BASE}/{ACC}/package-info')
        self.assertEqual(self.proto.args_of('package_info')[1], {})

    def test_item_search_needs_item_or_key(self):
        body = self.fail_envelope(f'{BASE}/{ACC}/item-search')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [], '参数不齐不该打平台')

    def test_item_search_body(self):
        self.ok_data(f'{BASE}/{ACC}/item-search',
                     '?item_id=123&need_tabs=true&campaign_id=9&page_num=2&page_size=20&sort_field=exposure&sort_type=desc')
        self.assertEqual(self.proto.args_of('item_search')[1], {
            'needTabs': True, 'itemId': '123', 'campaignId': 9, 'sortField': 'exposure',
            'sortType': 'desc', 'pageNum': 2, 'pageSize': 20})

    def test_bid_estimates_needs_body(self):
        body = self.fail_envelope(f'{BASE}/{ACC}/bid-estimates')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_bid_estimates_passes_body(self):
        """GET + JSON body（官方出价页传的就是整包对象）。"""
        resp = self.client.request('GET', f'{BASE}/{ACC}/bid-estimates',
                                   json={'entityType': 'ITEM', 'entityList': ['1']},
                                   headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.proto.args_of('customize_bid_estimates')[1],
                         {'entityType': 'ITEM', 'entityList': ['1']})

    def test_bid_estimates_payload_query_fallback(self):
        """发不出 GET body 的客户端走 `?payload=<json>`；坏 JSON 是 400，不静默当空对象。"""
        data = self.ok_data(f'{BASE}/{ACC}/bid-estimates',
                            '?payload=%7B%22entityType%22%3A%22ITEM%22%7D')
        self.assertEqual(data['kind'], 'customize_bid_estimates')
        self.assertEqual(self.proto.args_of('customize_bid_estimates')[1], {'entityType': 'ITEM'})
        body = self.fail_envelope(f'{BASE}/{ACC}/bid-estimates', '?payload=not-json')
        self.assertEqual(body['code'], 'invalid_fields')

    def test_group_list_body(self):
        self.ok_data(f'{BASE}/{ACC}/groups',
                     '?start_date=20261001&end_date=20261010&page_num=1&page_size=20&campaign_id=7&group_status=1&promotion_mode=CUSTOMIZE&title_like=耳机')
        self.assertEqual(self.proto.args_of('group_list')[1], {
            'startDate': '20261001', 'endDate': '20261010', 'includeEndDate': True,
            'pageNum': 1, 'pageSize': 20, 'campaignId': 7, 'groupStatus': 1,
            'promotionMode': 'CUSTOMIZE', 'campaignTitleLike': '耳机'})

    def test_group_list_keeps_include_end_date(self):
        """官方恒传 \`includeEndDate: true\`；少了它平台按「不含结束日」算，数据对不上。"""
        self.ok_data(f'{BASE}/{ACC}/groups')
        self.assertIs(self.proto.args_of('group_list')[1]['includeEndDate'], True)

    def test_group_data_needs_ad_group_id(self):
        body = self.fail_envelope(f'{BASE}/{ACC}/groups/data')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_group_data_body(self):
        self.ok_data(f'{BASE}/{ACC}/groups/data',
                     '?ad_group_id=42&start_date=20261001&end_date=20261010&all_period=false')
        self.assertEqual(self.proto.args_of('group_data')[1], {
            'startDate': '20261001', 'endDate': '20261010', 'includeEndDate': True,
            'allPeriod': False, 'adGroupId': 42})

    def test_campaign_log_defaults_and_enums(self):
        self.ok_data(f'{BASE}/{ACC}/campaign-log',
                     '?start_date=20261001&end_date=20261010&page_num=1&page_size=20&operator_type=1&operation_type=2')
        self.assertEqual(self.proto.args_of('campaign_log')[1], {
            'startDate': '20261001', 'endDate': '20261010', 'campaignId': 0,
            'pageNum': 1, 'pageSize': 20, 'operatorType': 1, 'operationType': 2})

    def test_campaign_log_omits_unset_enums(self):
        """官方没选筛选时**不带**这两个字段（带 0 与不带不是一回事）。"""
        self.ok_data(f'{BASE}/{ACC}/campaign-log')
        self.assertEqual(self.proto.args_of('campaign_log')[1],
                         {'startDate': '', 'endDate': '', 'campaignId': 0})

    def test_message_list_body(self):
        self.ok_data(f'{BASE}/{ACC}/messages', '?page_num=2&page_size=10')
        self.assertEqual(self.proto.args_of('message_list')[1], {'pageNum': 2, 'pageSize': 10})

    def test_message_detail_needs_id(self):
        seed_account()
        result = ad_service.message_detail(ACC, '')
        self.assertEqual(result['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_message_detail_body(self):
        data = self.ok_data(f'{BASE}/{ACC}/messages/12')
        self.assertEqual(data['kind'], 'message_detail')
        self.assertEqual(self.proto.args_of('message_detail')[1], {'messageId': '12'})

    def test_message_unread_takes_no_args(self):
        """官方这条 data 为空、不套 requestJson：服务层一个参数都不许给协议层。"""
        data = self.ok_data(f'{BASE}/{ACC}/messages/unread')
        self.assertEqual(data['kind'], 'message_unread')
        self.assertEqual(data['api'], ad_proto.MESSAGE_UNREAD_API)
        self.assertEqual(self.proto.args_of('message_unread'), (self.fake_client,))

    # -- 只读：错误语义 --------------------------------------------------

    def test_bad_date_is_400(self):
        body = self.fail_envelope(f'{BASE}/{ACC}/groups', '?start_date=2026-10-01')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_bad_page_is_400(self):
        body = self.fail_envelope(f'{BASE}/{ACC}/messages', '?page_num=0')
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_unknown_account_404(self):
        db.execute('DELETE FROM accounts WHERE id = ?', (ACC,))
        body = self.fail_envelope(f'{BASE}/{ACC}/account-info', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_missing_cookie_409(self):
        seed_account(cookie='')
        body = self.fail_envelope(f'{BASE}/{ACC}/account-info', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.proto.calls, [])

    def test_need_relogin_409(self):
        seed_account(login_state='need_relogin')
        body = self.fail_envelope(f'{BASE}/{ACC}/account-info', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.proto.calls, [])

    def test_protocol_missing_503(self):
        """协议层没装是 503，**不是**「平台没数据」—— 后者会让页面显示一片空。"""
        seed_account()
        real = common.protocol_module
        with mock.patch.object(common, 'protocol_module',
                                        lambda name: None if name == 'ad' else real(name)):
            body = self.fail_envelope(f'{BASE}/{ACC}/account-info', status=503)
        self.assertEqual(body['code'], common.NOT_READY_CODE)
        self.assertEqual(self.proto.calls, [])

    def test_protocol_error_502(self):
        seed_account()
        self.proto.errors['account_info'] = RuntimeError('boom')
        body = self.fail_envelope(f'{BASE}/{ACC}/account-info', status=502)
        self.assertEqual(body['code'], 'protocol_error')

    def test_session_expired_marks_need_relogin(self):
        seed_account()
        self.proto.errors['group_list'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/groups', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_non_dict_response_is_protocol_error(self):
        """协议层返回非 dict（信封没剥干净 / 平台给的是列表）算协议异常，不能当空数据。"""
        seed_account()
        self.proto.payloads['account_info'] = ['not', 'a', 'dict']
        body = self.fail_envelope(f'{BASE}/{ACC}/account-info', status=502)
        self.assertEqual(body['code'], 'protocol_error')

    # -- 服务层通用入口 --------------------------------------------------

    def test_read_generic_dispatch(self):
        seed_account()
        result = ad_service.read(ACC, 'group_data', {'ad_group_id': '42'})
        self.assertTrue(result.get('kind') == 'group_data', result)
        self.assertEqual(self.proto.args_of('group_data')[1]['adGroupId'], 42)

    def test_read_generic_unknown_kind(self):
        seed_account()
        result = ad_service.read(ACC, 'nope', {})
        self.assertEqual(result['code'], 'not_found')
        self.assertEqual(self.proto.calls, [])

    # -- 写：标记已读 ----------------------------------------------------

    def test_write_requires_confirm(self):
        seed_account()
        resp = self.post(f'{BASE}/{ACC}/messages/read', {'message_ids': [1, 2]})
        self.assertEqual(resp.status_code, 400, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'confirm_required')
        self.assertEqual(self.proto.calls, [], '没确认就不许打平台')

    def test_write_confirm_must_be_literal_true(self):
        """strict 档：字符串 'true' / 1 都不算确认（与 services/ad_write.py 同口径）。"""
        seed_account()
        for confirm in ('true', 1, 'yes'):
            resp = self.post(f'{BASE}/{ACC}/messages/read',
                             {'confirm': confirm, 'message_ids': [1]})
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()['code'], 'confirm_required')
        self.assertEqual(self.proto.calls, [])

    def test_write_success_envelope(self):
        seed_account()
        resp = self.post(f'{BASE}/{ACC}/messages/read',
                         {'confirm': True, 'message_ids': [1, 2]})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['kind'], 'message_read')
        self.assertEqual(data['api'], ad_proto.MESSAGE_READ_API)
        self.assertIs(data['confirmed'], True)
        self.assertIn('written_at', data)
        self.assertEqual(data['data'], {'updated': 2})
        args = self.proto.args_of('message_read')
        self.assertEqual(args[1], {'messageIds': [1, 2]})
        self.assertIs(args[2], True)

    def test_write_normalizes_ids(self):
        seed_account()
        for raw in ('1,2,3', '[4,5]', [6, '7']):
            self.post(f'{BASE}/{ACC}/messages/read', {'confirm': True, 'message_ids': raw})
        self.assertEqual([call[2]['messageIds'] for call in self.proto.calls],
                         [[1, 2, 3], [4, 5], [6, 7]])

    def test_write_requires_ids(self):
        seed_account()
        for raw in (None, [], '', '  '):
            resp = self.post(f'{BASE}/{ACC}/messages/read',
                             {'confirm': True, 'message_ids': raw})
            self.assertEqual(resp.status_code, 400, resp.text)
            self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_write_protocol_rejects_without_confirm(self):
        """协议层自己抛 WriteNotConfirmed 时翻成 400 confirm_required，而不是 502。"""
        seed_account()
        self.proto.errors['message_read'] = ad_proto.WriteNotConfirmed('写接口必须显式 confirm=True')
        resp = self.post(f'{BASE}/{ACC}/messages/read',
                         {'confirm': True, 'message_ids': [1]})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirm_required')

    def test_write_platform_failure_is_protocol_error_and_logged(self):
        seed_account()
        self.proto.errors['message_read'] = RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        resp = self.post(f'{BASE}/{ACC}/messages/read',
                         {'confirm': True, 'message_ids': [1]})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.json()['code'], 'protocol_error')
        row = db.query_one('SELECT action, success FROM operation_logs WHERE account_id = ?', (ACC,))
        self.assertIsNotNone(row, '写失败也要留操作记录')
        self.assertEqual(row['action'], 'message_read')
        self.assertEqual(row['success'], 0)

    def test_write_logs_operation(self):
        seed_account()
        self.post(f'{BASE}/{ACC}/messages/read', {'confirm': True, 'message_ids': [3]})
        row = db.query_one('SELECT action, item_id, success FROM operation_logs WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['action'], 'message_read')
        self.assertEqual(row['item_id'], '3')
        self.assertEqual(row['success'], 1)

    def test_execute_unknown_action_404(self):
        seed_account()
        result = ad_service.execute(ACC, 'campaign_delete', {}, True)
        self.assertEqual(result['code'], 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_execute_message_read(self):
        seed_account()
        result = ad_service.execute(ACC, 'message_read', {'message_ids': [8]}, True)
        self.assertEqual(result['kind'], 'message_read')
        self.assertIs(result['confirmed'], True)


class AdEndToEndTest(unittest.TestCase):
    """只换 client、真协议层参与：\`requestJson\` 的拼装与 \`v/spm_cnt\` 参数真被跑到过。"""

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
        seed_account()
        self.fake_client = install_fake_client(answers={
            ad_proto.GROUP_LIST_API: {'result': {'groupList': []}},
            ad_proto.MESSAGE_UNREAD_API: {'unreadCount': 5},
            ad_proto.MESSAGE_READ_API: {'updated': 1},
        })
        app = FastAPI()
        app.include_router(ad_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('ade2e','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    def test_group_list_end_to_end(self):
        resp = self.client.get(f'{BASE}/{ACC}/groups?start_date=20261001&end_date=20261010&page_num=1',
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['data'], {'result': {'groupList': []}})
        self.assertEqual(len(self.fake_client.calls), 1)
        call = self.fake_client.calls[0]
        self.assertEqual(call['api'], ad_proto.GROUP_LIST_API)
        self.assertEqual(json.loads(call['data']['requestJson']), {
            'startDate': '20261001', 'endDate': '20261010', 'includeEndDate': True, 'pageNum': 1})
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['kw'].get('spm_cnt'), AD_SPM)

    def test_unread_end_to_end_sends_empty_data(self):
        resp = self.client.get(f'{BASE}/{ACC}/messages/unread', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()['data']['data'], {'unreadCount': 5})
        call = self.fake_client.calls[0]
        self.assertEqual(call['api'], ad_proto.MESSAGE_UNREAD_API)
        self.assertEqual(call['data'], {})

    def test_message_read_end_to_end(self):
        resp = self.client.post(f'{BASE}/{ACC}/messages/read',
                                json={'confirm': True, 'message_ids': [3, 4]},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        call = self.fake_client.calls[0]
        self.assertEqual(call['api'], ad_proto.MESSAGE_READ_API)
        self.assertEqual(json.loads(call['data']['requestJson']), {'messageIds': [3, 4]})

    def test_platform_failure_is_502(self):
        self.fake_client.errors[ad_proto.GROUP_LIST_API] = RealXianyuError(
            'MTOP 调用失败: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        resp = self.client.get(f'{BASE}/{ACC}/groups', headers=self.headers)
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.json()['code'], 'protocol_error')


class AdIdempotencyTest(unittest.TestCase):
    """走真 app：\`Idempotency-Key\` 由 main 的中间件塞进 contextvar，自建 app 验不到。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415
        cls.app_main = app_main

    @classmethod
    def tearDownClass(cls):
        remove_fake_protocol()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fake_protocol()
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state',
                      'idempotency_keys'):
            db.execute(f'DELETE FROM {table}')
        seed_account()
        self.proto = full_protocol()
        install_fake_protocol(self.proto)
        self.app_ctx = TestClient(self.app_main.create_app())
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('adidem','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id),
                        KEY_HEADER: 'ad-key-1'}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    def test_same_key_replays_instead_of_double_write(self):
        body = {'confirm': True, 'message_ids': [1, 2]}
        first = self.client.post(f'{BASE}/{ACC}/messages/read', json=body, headers=self.headers)
        second = self.client.post(f'{BASE}/{ACC}/messages/read', json=body, headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.proto.names(), ['message_read'], '同键同指纹只许打平台一次')
        self.assertTrue(second.json()['data']['idempotent_replay'])
        self.assertNotIn('idempotent_replay', first.json()['data'])

    def test_different_key_writes_again(self):
        body = {'confirm': True, 'message_ids': [1]}
        self.client.post(f'{BASE}/{ACC}/messages/read', json=body, headers=self.headers)
        headers = dict(self.headers, **{KEY_HEADER: 'ad-key-2'})
        self.client.post(f'{BASE}/{ACC}/messages/read', json=body, headers=headers)
        self.assertEqual(self.proto.names(), ['message_read', 'message_read'])


if __name__ == '__main__':
    unittest.main()
