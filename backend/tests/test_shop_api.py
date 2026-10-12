"""小铺组（子账号管理 / 客服分流）接口的服务层 + HTTP 契约测试。

**为什么单开一组**：这两个页面最容易犯三个错 ——

1. 把平台「没权限 / 没这个键」画成 0 或 `[]`：真机 `sub.account.quota.query`
   回的是 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`（账号对这个业务没权限），
   `customers.info.get` 回的是 `data: {}`（**连 `customers` 键都没有**）——
   都不等于「有 0 个」；
2. **只读路径**悄悄调写接口（注册/删除子账号、改分流规则、改客服分组）——
   客服分流的三个写能力已单独实现，但只读路径一条都不许碰（`WRITE_APIS` 黑名单仍在
   `backend/app/xianyu/shop.py`，写侧见 `test_shop_write_api.py`）；
3. 拆错平台的**双层信封**：外层 `success` 为真、内层 `data.code` 却是失败；
   也有像 `customer.group.get` 那样**根本没有**业务信封、整段 `data` 就是载荷。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_shop_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
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

from backend.app import main as app_main  # noqa: E402
from backend.app.api import shop as shop_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import shop as shop_proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'shop_acc'
GHOST = 'shop_ghost'
BASE = '/api/shop'

SUB_LIST_API = shop_proto.SUB_ACCOUNT_LIST_API
SUB_QUOTA_API = shop_proto.SUB_ACCOUNT_QUOTA_API
ROLE_LIST_API = shop_proto.ROLE_LIST_API
CS_RULE_API = shop_proto.CS_ROUTING_RULE_API
CS_GROUP_API = shop_proto.CS_GROUP_API
CS_AGENTS_API = shop_proto.CS_AGENT_LIST_API

#: 真机 `sub.account.info.list`（2026-10-10 账号 2217970177348）：这个账号确实没有子账号，
#: `total: 0` 是**平台明说的 0**，可以照用。
SUB_LIST_EMPTY = {
    'code': 'success', 'msg': '成功',
    'data': {'hasNextPage': False, 'list': [], 'pageNo': 1, 'pageSize': 20,
             'rowLimit': 20, 'rowOffset': 0, 'total': 0, 'totalPage': 0},
}
#: 带一行子账号（字段名**没有真机样例**，这里故意用几个「不认识」的键，
#: 断言服务层原样透传、不重命名不补字段）。
SUB_LIST_ONE = {
    'code': 'success', 'msg': '成功',
    'data': {'hasNextPage': True, 'list': [{'subId': 'sub-1', 'nick': '小助手',
                                            'roleId': 9, 'mobilePhone': '13800000000',
                                            'unknownField': {'a': 1}}],
             'pageNo': 1, 'pageSize': 20, 'total': 1, 'totalPage': 1},
}
#: 真机 `customer.routing.rule.info.get`：`routingRules` 是**对象**，只有 serviceTime，
#: 没有 offlineMessage / serviceRange，也**没有业务信封**。
CS_RULE = {'routingRules': {'serviceTime': {'startTime': '08:00', 'endTime': '23:00'}}}
#: 真机 `customer.group.get`：同样没有业务信封，`csGroupInfos: []` 是平台真值。
CS_GROUP = {'csGroupInfos': [], 'hasMore': False, 'page': 1, 'pageSize': 20, 'total': 0}
#: 真机 `customers.info.get`：`data: {}` —— 连 `customers` 键都没有。
CS_AGENTS_MISSING: Dict[str, Any] = {}
CS_AGENTS = {'customers': [{'customerUid': '111', 'nick': '客服甲',
                            'customName': '售后', 'roleNames': '客服'}]}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeShopClient:
    """假协议层：按 **mtop 接口名**回话，记录每次 (api, data)。

    服务层调的是 `backend/app/xianyu/shop.py` 里的协议函数，协议函数再调
    `client.mtop(...)` —— 所以假客户端只要实现 `mtop` 就够了。
    """

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = dict(payloads or {})
        self.errors = dict(errors or {})
        self.calls: List[tuple] = []
        self.account_id = ''

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   **kwargs: Any) -> Dict[str, Any]:
        self.calls.append((api, data))
        error = self.errors.get(api)
        if error is not None:
            raise error
        if api not in self.payloads:
            raise AssertionError(f'假协议层没有为 {api} 准备响应')
        return {'success': True, 'data': self.payloads[api], 'message': '',
                'ret': ['SUCCESS::调用成功'], 'cookies_str': ''}

    def apis(self) -> List[str]:
        return [call[0] for call in self.calls]

    def args_of(self, api: str) -> Any:
        for call in self.calls:
            if call[0] == api:
                return call[1]
        raise AssertionError(f'没有调用 {api}，实际调用: {self.apis()}')


def install_fake_protocol(client: FakeShopClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeShopClient:
        # `common._note_session_expired` 靠 `client.account_id` 找到要标记的账号
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


def full_client(**overrides: Any) -> FakeShopClient:
    payloads = {
        SUB_LIST_API: SUB_LIST_EMPTY,
        SUB_QUOTA_API: {'code': 'success', 'data': {'total': 5, 'used': 0}},
        ROLE_LIST_API: {'code': 'success', 'data': [{'roleId': 9, 'roleName': '客服'}]},
        CS_RULE_API: CS_RULE,
        CS_GROUP_API: CS_GROUP,
        CS_AGENTS_API: CS_AGENTS,
    }
    payloads.update(overrides)
    return FakeShopClient(payloads)


class ShopApiTest(unittest.TestCase):
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
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上
        # （真实挂载由 test_real_app_mounts_shop_router 单独锁）。
        app = FastAPI()
        app.include_router(shop_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('shopapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = '', headers: Optional[Dict[str, str]] = None):
        return self.client.get(f'{path}{query}',
                               headers=self.headers if headers is None else headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def expect_fail(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        """断言失败信封。

        **名字不能叫 `fail`**：`unittest.TestCase.fail(msg)` 是 `assertIsNone` /
        `assertTrue` 等断言的底层出口。一旦被同名覆盖，断言失败时会拿「失败文案」
        当 URL 去发请求，实测报出 `AssertionError: 404 != 400 : {"detail":"Not Found"}`，
        把真正的失败原因整个盖掉（踩过一次，记在这里）。
        """
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 子账号列表 ------------------------------------------------------

    def test_sub_accounts_shape_and_platform_source(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertEqual(data['account_id'], ACC)
        self.assertEqual(data['items'], [])
        self.assertEqual(data['total'], 0)  # 平台明说的 0，照用
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['page_size'], 20)
        self.assertEqual(data['has_next_page'], False)
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        self.assertEqual(data['quota'], {'total': 5, 'used': 0})
        self.assertEqual(data['roles'], [{'roleId': 9, 'roleName': '客服'}])

    def test_sub_accounts_passes_rows_through_untouched(self):
        seed_account()
        self.fake.payloads[SUB_LIST_API] = SUB_LIST_ONE
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertEqual(data['items'], SUB_LIST_ONE['data']['list'])
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['has_next_page'], True)
        # 不认识的字段也原样留着（服务层不重命名、不补字段）
        self.assertEqual(data['items'][0]['unknownField'], {'a': 1})

    def test_sub_accounts_sends_pageNo_and_pageSize(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertEqual(self.fake.args_of(SUB_LIST_API), {'pageNo': 1, 'pageSize': 20})
        self.fake.calls.clear()
        self.ok_data(f'{BASE}/{ACC}/sub-accounts', '?page=2&size=5')
        self.assertEqual(self.fake.args_of(SUB_LIST_API), {'pageNo': 2, 'pageSize': 5})

    def test_sub_accounts_quota_unauthorized_is_null_not_zero(self):
        """真机 quota 回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` → null + 中文 warning。"""
        seed_account()
        self.fake.errors[SUB_QUOTA_API] = RuntimeError(
            'MTOP 调用失败 [sub.account.quota.query]: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertIsNone(data['quota'])
        self.assertTrue(any('不是 0' in w for w in data['warnings']), data['warnings'])
        # 主结果（列表）不受可选接口失败影响
        self.assertEqual(data['items'], [])

    def test_sub_accounts_roles_failure_is_null_with_warning(self):
        seed_account()
        self.fake.errors[ROLE_LIST_API] = RuntimeError('boom')
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertIsNone(data['roles'])
        self.assertTrue(any('角色' in w for w in data['warnings']), data['warnings'])

    def test_sub_accounts_missing_list_key_is_null_not_empty(self):
        """缺 `list` 键 ≠ 「没有子账号」→ null + warning，不能补空数组。"""
        seed_account()
        self.fake.payloads[SUB_LIST_API] = {'code': 'success', 'data': {'total': 3}}
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertIsNone(data['items'])
        self.assertEqual(data['total'], 3)
        self.assertTrue(any('list' in w for w in data['warnings']), data['warnings'])

    def test_sub_accounts_missing_page_fields_are_null(self):
        """平台没回 totalPage / hasNextPage 就是 null，不补 0 / False。"""
        seed_account()
        self.fake.payloads[SUB_LIST_API] = {'code': 'success', 'data': {'list': [], 'total': 1}}
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertIsNone(data['total_page'])
        self.assertIsNone(data['has_next_page'])

    def test_roles_payload_that_is_a_bare_array_is_kept(self):
        """回归：bundle 记录 `role.list` 的 data 是**裸数组**，协议层不许把它吞掉。"""
        seed_account()
        self.fake.payloads[ROLE_LIST_API] = [{'roleId': 9, 'roleName': '客服', 'selected': True}]
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertEqual(data['roles'], [{'roleId': 9, 'roleName': '客服', 'selected': True}])
        # 拿到了就不该有「未返回角色列表」的 warning
        self.assertFalse(any('角色' in w for w in data['warnings']), data['warnings'])

    def test_roles_envelope_payload_is_unwrapped(self):
        """也允许 `{code:'success', data:[...]}` 这种带信封的数组。"""
        seed_account()
        self.fake.payloads[ROLE_LIST_API] = {'code': 'success',
                                             'data': [{'roleId': 1, 'roleName': '店长'}]}
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertEqual(data['roles'], [{'roleId': 1, 'roleName': '店长'}])

    def test_roles_non_list_payload_is_null_with_warning(self):
        """平台给了个非数组（这里是空对象）→ null + warning，不许静默变 null。"""
        seed_account()
        self.fake.payloads[ROLE_LIST_API] = {'code': 'success', 'data': {}}
        data = self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertIsNone(data['roles'])
        self.assertTrue(any('角色' in w for w in data['warnings']), data['warnings'])

    def test_sub_accounts_business_error_inside_envelope_is_502(self):
        """外层 success 为真、内层 `data.code` 是失败 —— 必须当失败。"""
        seed_account()
        self.fake.payloads[SUB_LIST_API] = {'code': 'FAIL_BIZ_NO_PERMISSION',
                                            'msg': '没有权限', 'data': None}
        body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('没有权限', body['message'])

    def test_sub_accounts_unknown_account_is_404(self):
        body = self.expect_fail(f'{BASE}/{GHOST}/sub-accounts', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])  # 一次平台都不打

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_409_and_writes_need_relogin(self):
        seed_account()
        self.fake.errors[SUB_LIST_API] = SessionExpiredError('登录态已过期')
        body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_bad_paging_is_400(self):
        seed_account()
        # page/size 给了但不合法一律 400 —— 包括 page=0（早先被 max(1,...) 吞成 1）
        for query in ('?size=0', '?size=101', '?size=abc', '?page=x',
                      '?page=0', '?page=-1'):
            body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', query, status=400)
            self.assertEqual(body['code'], 'invalid_fields', body)
        self.assertEqual(self.fake.calls, [])  # 参数都没过校验，一次平台都不打

    def test_unauthenticated_is_401(self):
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/sub-accounts', headers={})
        self.assertEqual(resp.status_code, 401)

    # -- 客服分流 --------------------------------------------------------

    def test_cs_routing_shape_from_real_payloads(self):
        seed_account()
        # 真机 `customers.info.get` 回 `data: {}`（连 customers 键都没有）——
        # 与「返回空数组」是两回事，所以这里用真机形状，而不是夹具默认的空列表。
        self.fake.payloads[CS_AGENTS_API] = CS_AGENTS_MISSING
        data = self.ok_data(f'{BASE}/{ACC}/cs-routing')
        self.assertEqual(data['routing']['service_time'],
                         {'start': '08:00', 'end': '23:00'})
        self.assertIsNone(data['routing']['offline_message'])
        self.assertIsNone(data['routing']['service_range'])
        self.assertEqual(data['routing']['raw'], CS_RULE['routingRules'])
        self.assertEqual(data['groups'], [])
        self.assertEqual(data['groups_total'], 0)  # 平台真值 0
        self.assertIsNone(data['agents'])  # 缺 customers 键 → null，不是 []
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(any('customers' in w for w in data['warnings']), data['warnings'])
        self.assertTrue(any('部分分流配置' in w for w in data['warnings']), data['warnings'])

    def test_cs_routing_agents_present_when_platform_returns_them(self):
        seed_account()
        self.fake.payloads[CS_AGENTS_API] = CS_AGENTS
        data = self.ok_data(f'{BASE}/{ACC}/cs-routing')
        self.assertEqual(data['agents'], CS_AGENTS['customers'])
        self.assertFalse(any('customers' in w for w in data['warnings']), data['warnings'])

    def test_cs_routing_group_paging_params(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/cs-routing')
        self.assertEqual(self.fake.args_of(CS_GROUP_API), {'page': 1, 'pageSize': 20})
        self.fake.calls.clear()
        self.ok_data(f'{BASE}/{ACC}/cs-routing', '?page=3&size=7')
        self.assertEqual(self.fake.args_of(CS_GROUP_API), {'page': 3, 'pageSize': 7})

    def test_cs_routing_groups_passed_through(self):
        seed_account()
        self.fake.payloads[CS_GROUP_API] = {
            'csGroupInfos': [{'groupId': 1, 'groupName': '售后组'}],
            'hasMore': True, 'page': 1, 'pageSize': 20, 'total': 1}
        data = self.ok_data(f'{BASE}/{ACC}/cs-routing')
        self.assertEqual(data['groups'], [{'groupId': 1, 'groupName': '售后组'}])
        self.assertEqual(data['groups_total'], 1)

    def test_cs_routing_unknown_account_is_404(self):
        body = self.expect_fail(f'{BASE}/{GHOST}/cs-routing', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_cs_routing_session_expired_is_409(self):
        seed_account()
        self.fake.errors[CS_RULE_API] = SessionExpiredError('登录态已过期')
        body = self.expect_fail(f'{BASE}/{ACC}/cs-routing', status=409)
        self.assertEqual(body['code'], 'verification_required')

    def test_cs_routing_bad_paging_is_400(self):
        seed_account()
        self.expect_fail(f'{BASE}/{ACC}/cs-routing', '?size=0', status=400)
        self.expect_fail(f'{BASE}/{ACC}/cs-routing', '?size=101', status=400)
        self.expect_fail(f'{BASE}/{ACC}/cs-routing', '?page=x', status=400)
        # 分页校验在打平台之前就返回了
        self.assertEqual(self.fake.calls, [])

    def test_cs_routing_empty_customers_array_is_empty_not_null(self):
        """平台**明确给了空数组** → `[]`；缺键才是 null（与上一档区分）。"""
        seed_account()
        self.fake.payloads[CS_AGENTS_API] = {'customers': []}
        data = self.ok_data(f'{BASE}/{ACC}/cs-routing')
        self.assertEqual(data['agents'], [])
        self.assertFalse(any('customers' in w for w in data['warnings']), data['warnings'])

    def test_cs_routing_missing_groups_key_is_null(self):
        seed_account()
        self.fake.payloads[CS_GROUP_API] = {'page': 1, 'pageSize': 20, 'total': 0}
        data = self.ok_data(f'{BASE}/{ACC}/cs-routing')
        self.assertIsNone(data['groups'])
        self.assertTrue(any('csGroupInfos' in w for w in data['warnings']), data['warnings'])

    def test_quota_request_carries_biz_type(self):
        """真机 quota 带 `{"bizType":"COMMONPRO"}`，缺业务参数平台会报错。"""
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.assertEqual(self.fake.args_of(SUB_QUOTA_API), {'bizType': 'COMMONPRO'})

    def test_protocol_unavailable_is_503_not_ready(self):
        """协议层缺失（ImportError）→ 503 `xianyu_not_ready`，不是 500。"""
        seed_account()
        sys.modules['backend.app.xianyu.client'] = None  # 强制 ImportError
        try:
            body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', status=503)
            self.assertEqual(body['code'], 'xianyu_not_ready')
            self.assertEqual(self.fake.calls, [])
        finally:
            install_fake_protocol(self.fake)

    def test_protocol_function_missing_is_503_not_500(self):
        """协议模块在、函数不在（半升级）→ 503 `xianyu_not_ready`，不是 AttributeError→500。"""
        seed_account()
        original = shop_proto.sub_accounts
        shop_proto.sub_accounts = None  # 模拟旧版本协议层没有这个函数
        try:
            body = self.expect_fail(f'{BASE}/{ACC}/sub-accounts', status=503)
            self.assertEqual(body['code'], 'xianyu_not_ready')
            self.assertEqual(self.fake.calls, [])
        finally:
            shop_proto.sub_accounts = original

    # -- 只读 ------------------------------------------------------------

    def test_shop_read_routes_are_get_and_write_routes_are_post(self):
        """只读两条是 GET、写三条是 POST，且方法集合里没有别的动词。

        2026-10-13 之前这里是「本模块不提供任何写路由」—— 客服分流的三个写能力落地后
        这句话不再成立，改成把两侧的路径与方法**逐条钉住**：既挡「悄悄加了个写路由」，
        也挡「把只读路由改成 POST」。
        """
        by_method: Dict[str, set] = {}
        for route in shop_api.router.routes:
            path = getattr(route, 'path', '')
            if not path:
                continue
            for method in (getattr(route, 'methods', set()) or set()):
                by_method.setdefault(method, set()).add(path)
        self.assertEqual(set(by_method), {'GET', 'POST'}, f'出现了预期外的方法: {set(by_method)}')
        self.assertEqual(by_method['GET'], {f'{BASE}/{{account_id}}/sub-accounts',
                                            f'{BASE}/{{account_id}}/cs-routing'})
        self.assertEqual(by_method['POST'], {f'{BASE}/{{account_id}}/cs-routing/rule',
                                             f'{BASE}/{{account_id}}/cs-groups',
                                             f'{BASE}/{{account_id}}/cs-groups/delete'})

    def test_no_write_api_is_ever_called(self):
        """两个**只读**页面跑一遍，所有 mtop 调用都必须落在只读清单里。

        （写接口的调用在 `test_shop_write_api.py` 里单独验，那边断言的是
        「未确认时零调用」。）
        """
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/sub-accounts')
        self.ok_data(f'{BASE}/{ACC}/cs-routing')
        # 先确认真的调过东西，否则「没有写接口」是句空话
        self.assertTrue(self.fake.calls, '一次 mtop 都没调，本用例什么都没验证到')
        for api in self.fake.apis():
            self.assertIn(api, shop_proto.READ_APIS, f'调了非只读接口: {api}')
            self.assertNotIn(api, shop_proto.WRITE_APIS, f'调了写接口: {api}')

    def test_real_app_mounts_shop_router(self):
        def collect(routes: Any, out: List[str]) -> None:
            for route in routes:
                path = getattr(route, 'path', '')
                if path:
                    out.append(path)
                nested = getattr(route, 'routes', None) or getattr(
                    getattr(route, 'original_router', None), 'routes', None)
                if nested:
                    collect(nested, out)

        paths: List[str] = []
        collect(app_main.create_app().routes, paths)
        # 挂上了就必须真在：谁把 'shop' 从 ROUTE_MODULES 里删掉，这里要变红。
        self.assertIn(f'{BASE}/{{account_id}}/sub-accounts', paths,
                      "create_app() 没挂上小铺路由（main.py 的 ROUTE_MODULES 需要 'shop'）")
        self.assertIn(f'{BASE}/{{account_id}}/cs-routing', paths)


if __name__ == '__main__':
    unittest.main(verbosity=2)
