# -*- coding: utf-8 -*-
"""子账号管理（岗位只读 + 新建 / 删除 / 改手机号 / 改岗位 / 改密码）的测试。

**为什么单开一组**：这是全项目**唯一一处把用户密码送到平台**的功能，最容易犯的错是：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现密码不合规；
3. **把密码明文写进日志 / 返回值 / 事件**（红线：明文只允许在本机进程内出现）；
4. 公钥拿不到还硬发注册请求（平台必然拒绝，而且等于白提交一次）；
5. 把平台没返回的字段补成 `0` / `''` —— 平台没给就是 `null`。

密码加密：官方前端用 `JSEncrypt`（`sub.account.sub.pk` 取 `{publicKey, cryptoKey}`，
RSAES-PKCS1-v1_5）。我们**在本机服务端用纯 Python 实现同一算法**（`pow()` 就是模幂，
不引第三方依赖），前端只把明文发给本机后端。本文件用一把**固定的 1024 位测试密钥**
把它解密回来验证填充与算法正确 —— 那把私钥只存在于测试里。

**一次真机写请求都没发过**（这会在卖家账号下真的建/删子账号、改密码）。
这里用假协议层钉住「字段形状与官方 bundle 逐字一致」，平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_sub_accounts_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`），假客户端只实现 `mtop`。
"""
from __future__ import annotations

import base64
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

from backend.app.api import sub_accounts as sub_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.xianyu import sub_account as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'sa_acc'
BASE = '/api/sub-accounts'

#: 固定测试密钥（1024 位，只为单元测试；私钥只在本文件里用来解回来校验）。
PEM_PUBLIC = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDD73hMKQXaOPiOsoKKWHmVbvrQqMwoQxxMNbsd
GCni2zrdpPadnMcFCK/dA3uy5aas9ceh++cq0/x07m2rlB5gYijsf1bo5C9DoZ5qy9mvDSDZL6Lq
H9UJub5pPgS0czpYTGc3Z152tci68Gyn1OaoiF13Ihy3J0rw09z8vnrvVQIDAQAB
-----END PUBLIC KEY-----"""
MODULUS = int(
    'c3ef784c2905da38f88eb2828a5879956efad0a8cc28431c4c35bb1d1829e2db3adda4f69d9cc'
    '70508afdd037bb2e5a6acf5c7a1fbe72ad3fc74ee6dab941e606228ec7f56e8e42f43a19e6ac'
    'bd9af0d20d92fa2ea1fd509b9be693e04b4733a584c6737675e76b5c8baf06ca7d4e6a8885d7'
    '7221cb7274af0d3dcfcbe7aef55', 16)
PRIVATE = int(
    '83a5251d7ebe98e58853502b174ae4bdfb67f7c4420f972faf20b6cc908cac08825fa353b3883'
    'fcbe90f481eec00c1fe1fddaf38eda85f05396d53394a7be57fcc28c0a6022d9f19d964e6731'
    'e9c863ee4007555430cf0216bb732cd21c1851a7ba107a455f17eb5c2346bc5e58ad18d463899'
    '54c5cda9da516b03850ef4a20d', 16)

#: 官方对 `sub.pk` 的响应：标准 `{code,msg,data}` 信封，公钥在 `data.data` 里。
PUBKEY_OK = {
    'success': True,
    'data': {'code': 'success', 'data': {'publicKey': PEM_PUBLIC, 'cryptoKey': 'CK-1'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台没给公钥（拿不到就**不许**提交任何东西）。
PUBKEY_MISSING = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 注册成功：回显 subId。
REGISTER_OK = {
    'success': True,
    'data': {'code': 'success', 'data': {'subId': '9001'}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 官方从不读这几个写接口的响应体 ⇒ 成功体可能是空对象。
PLAIN_OK = {'success': True, 'data': {}, 'message': 'SUCCESS::调用成功',
            'ret': ['SUCCESS::调用成功']}
#: 岗位列表（官方调用点读 `[{roleId, roleName, selected}]`）。
ROLES_OK = {
    'success': True,
    'data': {'code': 'success', 'data': {'roleList': [
        {'roleId': 11, 'roleName': '客服', 'selected': True},
        {'roleId': 22, 'roleName': '发货', 'selected': False},
    ]}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 平台没返回岗位数组 —— 不等于「没有岗位」。
ROLES_MISSING = {
    'success': True, 'data': {'code': 'success', 'data': {}},
    'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功'],
}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

PASSWORD = 'abc12345'
CREATE_BODY: Dict[str, Any] = {
    'nick': '小客服', 'mobile': '138 0013-8000', 'role_ids': [11, 22],
    'password': PASSWORD, 'confirm': True,
}


def decrypt_password(blob: str) -> str:
    """用测试私钥把 `encrypt_password` 的结果解回来（校验算法与填充）。"""
    raw = base64.b64decode(blob)
    plain = pow(int.from_bytes(raw, 'big'), PRIVATE, MODULUS).to_bytes(len(raw), 'big')
    assert plain[0] == 0 and plain[1] == 2, plain[:4]
    sep = plain.index(b'\x00', 2)
    return plain[sep + 1:].decode('utf-8')


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeSubAccountClient:
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


def install_fake_protocol(client: FakeSubAccountClient, *, with_factory: bool = True) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeSubAccountClient:
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


def full_client(**overrides: Any) -> FakeSubAccountClient:
    payloads = {
        proto.ROLE_LIST_API: ROLES_OK,
        proto.SUB_PK_API: PUBKEY_OK,
        proto.REGISTER_API: REGISTER_OK,
        proto.DELETE_API: PLAIN_OK,
        proto.MOBILE_API: PLAIN_OK,
        proto.ROLE_API: PLAIN_OK,
        proto.PASSWORD_API: PLAIN_OK,
    }
    payloads.update(overrides)
    return FakeSubAccountClient(payloads)


# --------------------------------------------------------------------------- 协议层


class SubAccountProtocolTest(unittest.TestCase):
    """纯函数：接口名、入参形状、确认闸门、加密、解析口径 —— 全部离线。"""

    def test_api_names_are_verbatim(self):
        self.assertEqual(proto.INFO_LIST_API,
                         'mtop.alibaba.idle.seller.common.sub.account.info.list')
        self.assertEqual(proto.REGISTER_API,
                         'mtop.alibaba.idle.seller.platform.common.sub.account.register')
        self.assertEqual(proto.DELETE_API,
                         'mtop.alibaba.idle.seller.platform.sub.account.delete')
        self.assertEqual(proto.MOBILE_API,
                         'mtop.alibaba.idle.seller.platform.sub.account.mobilephone.update')
        self.assertEqual(proto.ROLE_API,
                         'mtop.alibaba.idle.seller.platform.sub.account.role.update')
        self.assertEqual(proto.PASSWORD_API,
                         'mtop.alibaba.idle.seller.platform.sub.account.password.update')
        self.assertEqual(proto.SUB_PK_API,
                         'mtop.alibaba.idle.seller.platform.sub.account.sub.pk')
        self.assertEqual(proto.ROLE_LIST_API,
                         'mtop.alibaba.idle.seller.platform.user.role.list')
        self.assertEqual(proto.PERMISSION_TREE_API,
                         'mtop.alibaba.idle.seller.platform.account.role.permission.tree')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')

    def test_whitelist_covers_five_writes_and_the_read_api(self):
        self.assertEqual(proto.READ_APIS, (proto.ROLE_LIST_API,))
        self.assertEqual(proto.ACTIONS,
                         ('create', 'delete', 'update_mobile', 'update_roles', 'update_password'))
        for action, spec in proto.WRITE_APIS.items():
            self.assertIn(spec['api'], proto.ALL_APIS, action)
            self.assertTrue(spec['evidence'], action)
        for api in (proto.INFO_LIST_API, proto.SUB_PK_API, proto.PERMISSION_TREE_API):
            self.assertIn(api, proto.ALL_APIS)

    def test_assert_write_rejects_read_apis(self):
        for action, spec in proto.WRITE_APIS.items():
            self.assertIsNone(proto.assert_write(spec['api']), action)
        for api in (proto.ROLE_LIST_API, proto.INFO_LIST_API, proto.SUB_PK_API,
                    proto.PERMISSION_TREE_API):
            with self.assertRaises(proto.UnknownApi):
                proto.assert_write(api)

    def test_assert_read_rejects_write_apis(self):
        self.assertIsNone(proto.assert_read(proto.ROLE_LIST_API))
        for action, spec in proto.WRITE_APIS.items():
            with self.assertRaises(proto.UnknownApi):
                proto.assert_read(spec['api'])

    def test_confirm_gate_is_identity_not_truthiness(self):
        for bad in (None, False, 0, '', 'false', 'yes', 2):
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.create_request('小客服', '13800138000', [11], 'enc', 'ck', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.delete_request('1', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.mobile_request('1', '13800138000', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.roles_request('1', [11], confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.password_request('1', 'enc', 'ck', confirm=bad)

    def test_normalize_mobile_strips_spaces_and_dashes(self):
        # 官方校验 /^1[3-9]\d{9}$/，而官方输入框允许带空格/横线 ⇒ 先洗再判
        self.assertEqual(proto.normalize_mobile('138 0013-8000'), '13800138000')
        self.assertEqual(proto.normalize_mobile(' 13800138000 '), '13800138000')

    def test_normalize_mobile_rejects_bad_numbers(self):
        for bad in ('', '   ', '1380013800', '138001380000', '23800138000', '1380013800a',
                    None):
            with self.assertRaises(ValueError) as ctx:
                proto.normalize_mobile(bad)
            self.assertIn('11位手机号码', str(ctx.exception))

    def test_normalize_mobile_accepts_a_bare_integer(self):
        # 宽松一点：AI 工具/表单把手机号当数字传也能用（11 位不会丢精度）
        self.assertEqual(proto.normalize_mobile(13800138000), '13800138000')

    def test_check_password_policy_matches_the_official_regex(self):
        # 官方 /^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,15}$/
        for good in ('abc12345', 'A1b2C3d4E5f6G7h', '12345678a'):
            self.assertEqual(proto.check_password(good), good)
        for bad in ('abc1234', 'a' * 8, '1' * 8, 'abc1234567890123', 'abc 12345', 'abc-12345',
                    '', None):
            with self.assertRaises(ValueError) as ctx:
                proto.check_password(bad)
            self.assertIn('8到15位', str(ctx.exception))

    def test_normalize_role_ids_accepts_comma_string_and_list(self):
        self.assertEqual(proto.normalize_role_ids('11,22'), [11, 22])
        self.assertEqual(proto.normalize_role_ids('11，22'), [11, 22])  # 中文逗号
        self.assertEqual(proto.normalize_role_ids([11, 22]), [11, 22])
        self.assertEqual(proto.normalize_role_ids('11'), [11])
        self.assertEqual(proto.normalize_role_ids(11), [11])
        self.assertEqual(proto.normalize_role_ids(' 11 , 22 '), [11, 22])

    def test_normalize_role_ids_dedupes_and_keeps_order(self):
        self.assertEqual(proto.normalize_role_ids([22, 11, 22]), [22, 11])

    def test_normalize_role_ids_rejects_empty_bad_and_too_many(self):
        for bad in ('', '   ', [], None, [0], [-1], ['x'], ['11', 'x']):
            with self.assertRaises(ValueError):
                proto.normalize_role_ids(bad)
        with self.assertRaises(ValueError) as ctx:
            proto.normalize_role_ids(list(range(1, proto.MAX_ROLE_IDS + 2)))
        self.assertIn(str(proto.MAX_ROLE_IDS), str(ctx.exception))

    def test_create_request_shape_is_verbatim(self):
        api, data = proto.create_request('小客服', '13800138000', [11, 22], 'ENC', 'CK-1',
                                         confirm=True)
        self.assertEqual(api, proto.REGISTER_API)
        self.assertEqual(data, {'nick': '小客服', 'mobile': '13800138000', 'roleIds': [11, 22],
                                'encryptedPassword': 'ENC', 'cryptoKey': 'CK-1'})
        # 可选字段没给就**不放键**（官方只在扩展参数非空时才带 ext）
        self.assertNotIn('customName', data)
        self.assertNotIn('ext', data)

    def test_create_request_carries_custom_name_and_ext(self):
        _, data = proto.create_request('小客服', '13800138000', [11], 'ENC', 'CK-1',
                                       custom_name='客服小号', ext='{"a":1}', confirm=True)
        self.assertEqual(data['customName'], '客服小号')
        self.assertEqual(data['ext'], '{"a":1}')

    def test_create_request_rejects_blank_nick(self):
        for blank in ('', '   ', None):
            with self.assertRaises(ValueError) as ctx:
                proto.create_request(blank, '13800138000', [11], 'ENC', 'CK-1', confirm=True)
            self.assertIn('不能为空', str(ctx.exception))

    def test_other_write_request_shapes(self):
        self.assertEqual(proto.delete_request('9001', confirm=True),
                         (proto.DELETE_API, {'subId': '9001'}))
        self.assertEqual(proto.mobile_request('9001', '138 0013-8000', confirm=True),
                         (proto.MOBILE_API, {'subId': '9001', 'mobilePhone': '13800138000'}))
        self.assertEqual(proto.roles_request('9001', '11,22', confirm=True),
                         (proto.ROLE_API, {'subId': '9001', 'roleIds': [11, 22]}))
        self.assertEqual(proto.password_request('9001', 'ENC', 'CK-1', confirm=True),
                         (proto.PASSWORD_API, {'subId': '9001', 'encryptedPassword': 'ENC',
                                               'cryptoKey': 'CK-1'}))

    def test_request_of_dispatch_and_unknown_action(self):
        api, data = proto.request_of('delete', sub_id='9001', confirm=True)
        self.assertEqual((api, data), (proto.DELETE_API, {'subId': '9001'}))
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('rename', sub_id='1', confirm=True)

    def test_read_requests_carry_no_parameters(self):
        self.assertEqual(proto.roles_request_of(), (proto.ROLE_LIST_API, {}))
        self.assertEqual(proto.public_key_request(), (proto.SUB_PK_API, {}))

    def test_parse_public_key_reads_spki_pem(self):
        modulus, exponent = proto.parse_public_key(PEM_PUBLIC)
        self.assertEqual(modulus, MODULUS)
        self.assertEqual(exponent, 65537)

    def test_parse_public_key_rejects_garbage(self):
        for bad in ('', '   ', None, 'not a key'):
            with self.assertRaises(proto.EncryptionError):
                proto.parse_public_key(bad)

    def test_encrypt_password_is_pkcs1_v1_5_and_decrypts_back(self):
        blob = proto.encrypt_password(PASSWORD, PEM_PUBLIC)
        raw = base64.b64decode(blob)
        self.assertEqual(len(raw), 128)  # 1024 位模长
        # 密文前两字节是任意的（这里不是明文块，填充只能在解密后校验）
        self.assertEqual(decrypt_password(blob), PASSWORD)

    def test_encrypt_password_is_randomised_and_leaks_no_plaintext(self):
        first = proto.encrypt_password(PASSWORD, PEM_PUBLIC)
        second = proto.encrypt_password(PASSWORD, PEM_PUBLIC)
        self.assertNotEqual(first, second)  # 每次新随机填充
        self.assertNotIn(PASSWORD, first)

    def test_is_success_variants(self):
        self.assertTrue(proto.is_success(REGISTER_OK))
        self.assertTrue(proto.is_success(PLAIN_OK))
        self.assertFalse(proto.is_success(NO_PERMISSION))
        self.assertFalse(proto.is_success(None))
        self.assertFalse(proto.is_success('x'))
        # 外层成功、内层业务码失败 ⇒ 必须算失败
        inner_fail = {'success': True, 'data': {'code': 'FAIL_BIZ', 'msg': '业务失败'}}
        self.assertFalse(proto.is_success(inner_fail))

    def test_failure_reason_priority(self):
        self.assertEqual(proto.failure_reason(NO_PERMISSION),
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
        inner_fail = {'success': True, 'data': {'code': 'FAIL_BIZ', 'msg': '业务失败'}}
        self.assertEqual(proto.failure_reason(inner_fail), '业务失败')
        self.assertEqual(proto.failure_reason({'success': True, 'data': {},
                                               'message': '客户端失败'}), '客户端失败')
        self.assertEqual(proto.failure_reason({'success': True, 'data': {},
                                               'ret': ['ret 里的原因']}), 'ret 里的原因')
        self.assertEqual(proto.failure_reason({'success': True, 'data': {}}), '')

    def test_parse_roles_reads_the_official_shape(self):
        parsed = proto.parse_roles(ROLES_OK)
        self.assertEqual(parsed['total'], 2)
        self.assertEqual(parsed['roles'], [
            {'role_id': 11, 'role_name': '客服', 'selected': True},
            {'role_id': 22, 'role_name': '发货', 'selected': False},
        ])

    def test_parse_roles_returns_none_instead_of_an_empty_list(self):
        # 平台没给数组 ≠ 没有岗位 ⇒ roles=None（调用方加 warning），不造 []
        parsed = proto.parse_roles(ROLES_MISSING)
        self.assertIsNone(parsed['roles'])
        self.assertIsNone(parsed['total'])

    def test_parse_public_key_response_never_invents_a_key(self):
        parsed = proto.parse_public_key_response(PUBKEY_OK)
        self.assertEqual(parsed['public_key'], PEM_PUBLIC)
        self.assertEqual(parsed['crypto_key'], 'CK-1')
        empty = proto.parse_public_key_response(PUBKEY_MISSING)
        self.assertIsNone(empty['public_key'])
        self.assertIsNone(empty['crypto_key'])

    def test_parse_write_reports_action_and_saved(self):
        parsed = proto.parse_write(REGISTER_OK, 'create')
        self.assertEqual(parsed['action'], 'create')
        self.assertEqual(parsed['api'], proto.REGISTER_API)
        self.assertTrue(parsed['saved'])
        failed = proto.parse_write(NO_PERMISSION, 'delete')
        self.assertFalse(failed['saved'])
        self.assertIn('无权限访问', failed['platform_message'])


# --------------------------------------------------------------------------- HTTP 层


class SubAccountsPageTest(unittest.TestCase):
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
        app.include_router(sub_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('subacc','x',1)"))
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

    def roles_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}/roles'

    def create_path(self, account_id: str = ACC) -> str:
        return f'{BASE}/{account_id}'

    def sub_path(self, suffix: str, account_id: str = ACC, sub_id: str = '9001') -> str:
        return f'{BASE}/{account_id}/{sub_id}/{suffix}'


class SubAccountsReadTest(SubAccountsPageTest):
    def test_roles_are_returned_with_the_platform_shape(self):
        resp = self.get(self.roles_path())
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['roles'], [
            {'role_id': 11, 'role_name': '客服', 'selected': True},
            {'role_id': 22, 'role_name': '发货', 'selected': False},
        ])
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['api'], proto.ROLE_LIST_API)

    def test_missing_role_array_is_a_warning_not_an_empty_list(self):
        self.fake = full_client(**{proto.ROLE_LIST_API: ROLES_MISSING})
        install_fake_protocol(self.fake)
        data = self.get(self.roles_path()).json()['data']
        self.assertIsNone(data['roles'])
        self.assertEqual(len(data['warnings']), 1)
        self.assertEqual(data['warnings'][0]['code'], 'roles_missing')
        self.assertIn('不等于', data['warnings'][0]['message'])

    def test_platform_refusal_is_502_and_is_not_a_logout(self):
        self.fake = full_client(**{proto.ROLE_LIST_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        resp = self.get(self.roles_path())
        self.assertEqual(resp.status_code, 502, resp.text)
        payload = resp.json()
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.ROLE_LIST_API)
        self.assertEqual(self.login_state(), 'active')  # 无权限 ≠ 掉线

    def test_unknown_account_is_404(self):
        self.fake = full_client()
        install_fake_protocol(self.fake)
        resp = self.get(self.roles_path('nope'))
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.apis(), [])

    def test_requires_login(self):
        resp = self.get(self.roles_path(), headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


class SubAccountsConfirmGateTest(SubAccountsPageTest):
    """红线：未确认时**一个平台请求都不许发**。"""

    def test_every_write_route_refuses_without_confirm(self):
        cases = [
            (self.create_path(), {'nick': '小客服', 'mobile': '13800138000',
                                  'role_ids': [11], 'password': PASSWORD}),
            (self.sub_path('delete'), {}),
            (self.sub_path('mobile'), {'mobile': '13800138000'}),
            (self.sub_path('roles'), {'role_ids': [11]}),
            (self.sub_path('password'), {'password': PASSWORD}),
        ]
        for path, body in cases:
            payload = self.fail(path, body, 400)
            self.assertEqual(payload['code'], 'confirmation_required', path)
            self.assertEqual(self.fake.apis(), [], path)  # 零平台调用

    def test_false_like_confirm_values_are_not_consent(self):
        for bad in (False, 'false', 0, '', None, 'no'):
            body = dict(CREATE_BODY)
            body['confirm'] = bad
            payload = self.fail(self.create_path(), body, 400)
            self.assertEqual(payload['code'], 'confirmation_required', repr(bad))
            self.assertEqual(self.fake.apis(), [], repr(bad))

    def test_confirm_true_is_the_only_thing_that_sends(self):
        self.ok_data(self.create_path(), CREATE_BODY)
        self.assertEqual(self.fake.apis(), [proto.SUB_PK_API, proto.REGISTER_API])


class SubAccountsValidationTest(SubAccountsPageTest):
    """字段校验必须排在凭据检查**之前**（掉线时也该先说「密码不合规」）。"""

    def test_field_validation_runs_before_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        body = dict(CREATE_BODY, password='short')
        payload = self.fail(self.create_path(), body, 400)
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertIn('8到15位', payload['message'])
        self.assertEqual(self.fake.apis(), [])
        # 字段齐了才轮到凭据检查
        payload = self.fail(self.create_path(), CREATE_BODY, 409)
        self.assertEqual(payload['code'], 'verification_required')
        self.assertEqual(self.fake.apis(), [])

    def test_blank_nick_is_rejected_locally(self):
        payload = self.fail(self.create_path(), dict(CREATE_BODY, nick='   '), 400)
        self.assertEqual(payload['code'], 'invalid_fields')
        self.assertIn('昵称', payload['message'])
        self.assertEqual(self.fake.apis(), [])

    def test_bad_mobile_is_rejected_locally(self):
        payload = self.fail(self.create_path(), dict(CREATE_BODY, mobile='1380013800'), 400)
        self.assertIn('11位手机号码', payload['message'])
        self.assertEqual(self.fake.apis(), [])

    def test_empty_role_ids_are_rejected_locally(self):
        payload = self.fail(self.create_path(), dict(CREATE_BODY, role_ids=[]), 400)
        self.assertIn('岗位', payload['message'])
        self.assertEqual(self.fake.apis(), [])

    def test_too_many_role_ids_are_rejected_locally(self):
        body = dict(CREATE_BODY, role_ids=list(range(1, proto.MAX_ROLE_IDS + 2)))
        payload = self.fail(self.create_path(), body, 400)
        self.assertIn(str(proto.MAX_ROLE_IDS), payload['message'])
        self.assertEqual(self.fake.apis(), [])

    def test_weak_password_is_rejected_before_the_public_key_call(self):
        payload = self.fail(self.sub_path('password'), {'password': 'abcdefgh', 'confirm': True},
                            400)
        self.assertIn('8到15位', payload['message'])
        self.assertEqual(self.fake.apis(), [])

    def test_bad_mobile_on_the_update_route_is_rejected_locally(self):
        payload = self.fail(self.sub_path('mobile'),
                            {'mobile': '23800138000', 'confirm': True}, 400)
        self.assertIn('11位手机号码', payload['message'])
        self.assertEqual(self.fake.apis(), [])


class SubAccountsHappyPathTest(SubAccountsPageTest):
    def test_create_asks_for_the_public_key_then_registers(self):
        data = self.ok_data(self.create_path(), CREATE_BODY)
        self.assertEqual(self.fake.apis(), [proto.SUB_PK_API, proto.REGISTER_API])
        self.assertEqual(self.fake.call_of(proto.SUB_PK_API)['data'], {})
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'create')
        self.assertEqual(data['sub_id'], '9001')
        self.assertEqual(data['mobile'], '13800138000')  # 空格/横线已洗掉
        self.assertEqual(data['role_ids'], [11, 22])
        self.assertEqual(data['warnings'], [])

    def test_register_payload_is_encrypted_not_plaintext(self):
        self.ok_data(self.create_path(), CREATE_BODY)
        sent = self.fake.call_of(proto.REGISTER_API)['data']
        self.assertEqual(sent['nick'], '小客服')
        self.assertEqual(sent['mobile'], '13800138000')
        self.assertEqual(sent['roleIds'], [11, 22])
        self.assertEqual(sent['cryptoKey'], 'CK-1')
        self.assertNotEqual(sent['encryptedPassword'], PASSWORD)
        self.assertNotIn(PASSWORD, sent['encryptedPassword'])
        self.assertEqual(decrypt_password(sent['encryptedPassword']), PASSWORD)

    def test_password_never_appears_in_the_response_or_the_logs(self):
        resp = self.post(self.create_path(), CREATE_BODY)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn(PASSWORD, resp.text)
        for row in self.logs():
            self.assertNotIn(PASSWORD, str(dict(row)))

    def test_create_warns_when_the_platform_does_not_echo_a_sub_id(self):
        self.fake = full_client(**{proto.REGISTER_API: PLAIN_OK})
        install_fake_protocol(self.fake)
        data = self.ok_data(self.create_path(), CREATE_BODY)
        self.assertIsNone(data['sub_id'])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_sub_id'])

    def test_delete_shape(self):
        data = self.ok_data(self.sub_path('delete'), {'confirm': True})
        self.assertEqual(self.fake.call_of(proto.DELETE_API)['data'], {'subId': '9001'})
        self.assertTrue(data['saved'])
        self.assertEqual(data['sub_id'], '9001')

    def test_update_mobile_shape(self):
        data = self.ok_data(self.sub_path('mobile'),
                            {'mobile': '138 0013-8000', 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.MOBILE_API)['data'],
                         {'subId': '9001', 'mobilePhone': '13800138000'})
        self.assertEqual(data['mobile'], '13800138000')

    def test_update_roles_shape(self):
        data = self.ok_data(self.sub_path('roles'), {'role_ids': '11,22', 'confirm': True})
        self.assertEqual(self.fake.call_of(proto.ROLE_API)['data'],
                         {'subId': '9001', 'roleIds': [11, 22]})
        self.assertEqual(data['role_ids'], [11, 22])

    def test_update_password_encrypts_and_never_echoes_the_password(self):
        resp = self.post(self.sub_path('password'), {'password': PASSWORD, 'confirm': True})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.fake.apis(), [proto.SUB_PK_API, proto.PASSWORD_API])
        sent = self.fake.call_of(proto.PASSWORD_API)['data']
        self.assertEqual(sent['subId'], '9001')
        self.assertEqual(decrypt_password(sent['encryptedPassword']), PASSWORD)
        self.assertNotIn(PASSWORD, resp.text)

    def test_missing_public_key_means_nothing_is_submitted(self):
        self.fake = full_client(**{proto.SUB_PK_API: PUBKEY_MISSING})
        install_fake_protocol(self.fake)
        payload = self.fail(self.create_path(), CREATE_BODY, 502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(self.fake.apis(), [proto.SUB_PK_API])  # 没有发注册请求

    def test_missing_public_key_on_password_update_also_submits_nothing(self):
        self.fake = full_client(**{proto.SUB_PK_API: PUBKEY_MISSING})
        install_fake_protocol(self.fake)
        self.fail(self.sub_path('password'), {'password': PASSWORD, 'confirm': True}, 502)
        self.assertEqual(self.fake.apis(), [proto.SUB_PK_API])

    def test_writes_are_logged_and_announced(self):
        self.ok_data(self.sub_path('delete'), {'confirm': True})
        self.ok_data(self.sub_path('mobile'), {'mobile': '13800138000', 'confirm': True})
        self.ok_data(self.sub_path('roles'), {'role_ids': [11], 'confirm': True})
        actions = sorted(str(row['action']) for row in self.logs())
        self.assertEqual(actions, ['sub_account_delete', 'sub_account_mobile', 'sub_account_roles'])
        for row in self.logs():
            self.assertTrue(row['success'])

    def test_failed_write_is_logged_as_failure(self):
        self.fake = full_client(**{proto.DELETE_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        self.fail(self.sub_path('delete'), {'confirm': True}, 502)
        rows = [dict(row) for row in self.logs()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], 'sub_account_delete')
        self.assertFalse(rows[0]['success'])


class SubAccountsFailureTest(SubAccountsPageTest):
    def test_platform_refusal_is_502_and_keeps_the_login_state(self):
        self.fake = full_client(**{proto.REGISTER_API: NO_PERMISSION})
        install_fake_protocol(self.fake)
        payload = self.fail(self.create_path(), CREATE_BODY, 502)
        self.assertEqual(payload['code'], 'protocol_error')
        self.assertEqual(payload['platform_errors']['api'], proto.REGISTER_API)
        self.assertEqual(self.login_state(), 'active')

    def test_session_expired_is_409_and_marks_need_relogin(self):
        self.fake = full_client()
        # 异常必须放进 errors（塞进 payloads 会被当成响应体，于是变成 502）
        self.fake.errors[proto.DELETE_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        install_fake_protocol(self.fake)
        payload = self.fail(self.sub_path('delete'), {'confirm': True}, 409)
        self.assertEqual(payload['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_missing_cookie_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        payload = self.fail(self.sub_path('delete'), {'confirm': True}, 409)
        self.assertEqual(payload['code'], 'verification_required')
        self.assertEqual(self.fake.apis(), [])

    def test_need_relogin_account_is_409_and_sends_nothing(self):
        seed_account(login_state='need_relogin')
        self.fail(self.sub_path('roles'), {'role_ids': [11], 'confirm': True}, 409)
        self.assertEqual(self.fake.apis(), [])

    def test_unknown_account_is_404_and_sends_nothing(self):
        payload = self.fail(self.sub_path('delete', account_id='nope'), {'confirm': True}, 404)
        self.assertEqual(payload['code'], 'not_found')
        self.assertEqual(self.fake.apis(), [])

    def test_requires_login_on_writes(self):
        resp = self.post(self.sub_path('delete'), {'confirm': True}, headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


class SubAccountsRouteTest(unittest.TestCase):
    def test_routes_are_registered_with_the_expected_methods(self):
        found = {}
        for route in sub_api.router.routes:
            found.setdefault(route.path, set()).update(route.methods or set())
        self.assertEqual(found.get(f'{BASE}/{{account_id}}/roles'), {'GET'})
        self.assertEqual(found.get(f'{BASE}/{{account_id}}'), {'POST'})
        for suffix in ('delete', 'mobile', 'roles', 'password'):
            self.assertEqual(found.get(f'{BASE}/{{account_id}}/{{sub_id}}/{suffix}'), {'POST'},
                             suffix)

    def test_write_routes_are_not_reachable_by_get(self):
        app = FastAPI()
        app.include_router(sub_api.router)
        with TestClient(app) as client:
            for path in (f'{BASE}/x', f'{BASE}/x/1/delete', f'{BASE}/x/1/mobile',
                         f'{BASE}/x/1/roles', f'{BASE}/x/1/password'):
                self.assertEqual(client.get(path).status_code, 405, path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
