"""运费模版（官方 `post-temple` 页）只读接口的服务层 + HTTP 契约测试。

**为什么单开一组**：这一页最容易犯的错有四个 ——

1. 把 `totalCount` 缺失画成 0（「一个模版都没有」和「平台没告诉我」是两件事）；
2. 把模版元素里**没证实**的字段猜出来（本机真机账号一个模版都没有，行字段表没证据）；
3. 手滑**写平台**（create/modify/del/unbind 在 `WRITE_APIS` 里，只记录不实现）；
4. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（缺站点 CODE 头）当成账号掉线，让用户白扫一次码。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_item_templates_api.py"

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

from backend.app.api import item_templates as item_templates_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import item_templates as service  # noqa: E402
from backend.app.xianyu import item_templates as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'tpl_acc'
BASE = '/api/item-templates'

#: 真机 `mtop.alibaba.idle.seller.platform.pro.freight.template.query` 的响应体
#: （2026-10-10，账号 2217970177348：这个账号一个运费模版都没有，data 空、totalCount 0）
TEMPLE_LIST_EMPTY = {
    'code': 'success', 'currentPage': 1, 'data': [], 'hasNextPage': False,
    'pageSize': 10, 'totalCount': 0,
}
#: 行字段里**只有** `templateId` / `templateName` 有证据（bundle @4110816 把
#: `pc.all` 的元素映射成 `{label: templateName, value: templateId}`，`del`/`detail` 收 `templateId`）。
#: 其余键是占位，用来验证「原样透传到 raw、不猜字段」。
TEMPLE_LIST_ROWS = {
    'code': 'success', 'currentPage': 2, 'hasNextPage': True, 'pageSize': 10, 'totalCount': 3,
    'data': [{'templateId': 987654321, 'templateName': '默认运费模版',
              'excessPrice': '3', 'isDefault': True}],
}
TEMPLE_DETAIL = {'code': 'success', 'templateId': 987654321, 'templateName': '默认运费模版',
                 'isDefault': True, 'excessPrice': '3'}
#: 真机直调（缺站点 CODE 头）的原样报错
UNAUTHORIZED_MESSAGE = ('MTOP 调用失败 [mtop.alibaba.idle.seller.platform.pro.freight.template.query]: '
                        'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')


class XianyuError(RuntimeError):
    """与协议层同名（`common.call_failed` 只按异常本身分类，不看类名）。"""


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeTemplatesClient:
    """假协议层：只实现 `mtop`，按 api 名回话，记录每次调用的参数与头。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None,
                 headers: Optional[Dict[str, str]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []
        self._headers = headers

    def _security_headers(self) -> Dict[str, str]:
        """真实客户端有这个方法（站点 CODE 头）；假客户端也提供，好断言头被透传。"""
        return self._headers or {}

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   spm_cnt: Optional[str] = None,
                   extra_headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v, 'extra_headers': extra_headers})
        error = self.errors.get(api)
        if error is not None:
            raise error
        return {'success': True, 'data': self.payloads.get(api), 'message': '',
                'ret': ['SUCCESS::调用成功']}

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


def install_fake_protocol(client: FakeTemplatesClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeTemplatesClient:
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


class ItemTemplatesApiTest(unittest.TestCase):
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
        for table in ('accounts', 'account_credentials', 'operation_logs'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeTemplatesClient(
            payloads={proto.LIST_API: TEMPLE_LIST_EMPTY, proto.DETAIL_API: TEMPLE_DETAIL},
            headers={'idle_site_biz_code': 'COMMONPRO'},
        )
        install_fake_protocol(self.fake)
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已挂上（该文件不在本次写范围）。
        app = FastAPI()
        app.include_router(item_templates_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('tplapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, query: str = ''):
        return self.client.get(f'{path}{query}', headers=self.headers)

    def ok_data(self, path: str, query: str = '') -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def fail_body(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 列表 ------------------------------------------------------------

    def test_list_passes_platform_shapes_through(self):
        """真机形状（data 空、totalCount 0）原样透传：0 是平台给的真值。"""
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertEqual(data['templates'], [])
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['total'], 0)
        self.assertIs(data['has_next'], False)
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['source'], 'platform')
        # 默认分页参数与真机一致
        self.assertEqual(self.fake.calls[0]['api'], proto.LIST_API)
        self.assertEqual(self.fake.calls[0]['data'], {'pageNo': 1, 'pageSize': 20})

    def test_missing_total_is_none_not_zero(self):
        """平台没返回 totalCount → None（不拿 0 冒充「一个都没有」）。"""
        seed_account()
        self.fake.payloads[proto.LIST_API] = {'code': 'success', 'data': []}
        data = self.ok_data(f'{BASE}/{ACC}')
        self.assertIsNone(data['total'])
        self.assertIsNone(data['has_next'])
        self.assertEqual(data['templates'], [])

    def test_rows_are_promoted_without_inventing_fields(self):
        """已证实字段提升为 template_id/template_name，其余原样进 raw。"""
        seed_account()
        self.fake.payloads[proto.LIST_API] = TEMPLE_LIST_ROWS
        data = self.ok_data(f'{BASE}/{ACC}', '?page=2&size=10')
        self.assertEqual(data['total'], 3)
        self.assertIs(data['has_next'], True)
        self.assertEqual(data['page'], 2)
        entry = data['templates'][0]
        self.assertEqual(entry['template_id'], '987654321')
        self.assertEqual(entry['template_name'], '默认运费模版')
        self.assertEqual(entry['raw'], TEMPLE_LIST_ROWS['data'][0])
        self.assertNotIn('excess_price', entry)  # 没证实的字段不猜
        self.assertEqual(self.fake.calls[0]['data'],
                         {'pageNo': 2, 'pageSize': 10})

    def test_template_name_search_is_forwarded(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}', '?template_name=默认')
        self.assertEqual(self.fake.calls[0]['data'],
                         {'pageNo': 1, 'pageSize': 20, 'templateName': '默认'})

    # -- 详情 ------------------------------------------------------------

    def test_detail_returns_raw_and_id(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/987654321')
        self.assertEqual(data['template_id'] if 'template_id' in data else
                         data['template']['template_id'], '987654321')
        self.assertEqual(data['template']['template_name'], '默认运费模版')
        self.assertEqual(data['template']['raw'], TEMPLE_DETAIL)
        self.assertEqual(self.fake.calls[0]['api'], proto.DETAIL_API)
        self.assertEqual(self.fake.calls[0]['data'], {'templateId': '987654321'})

    def test_missing_template_is_404_not_empty_200(self):
        """平台说没有这个模版 → 如实 404，不返回空壳 200。"""
        seed_account()
        self.fake.payloads[proto.DETAIL_API] = {}
        body = self.fail_body(f'{BASE}/{ACC}/404404', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertIn('404404', body['message'])

    def test_service_rejects_empty_template_id(self):
        seed_account()
        failure = service.template_detail(ACC, '  ')
        self.assertFalse(failure.get('success'))
        self.assertEqual(failure['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    # -- 只读 / 登录态 / 参数 --------------------------------------------

    def test_routes_are_read_only(self):
        """写方法一律 405：本模块没有创建/编辑/删除/解绑路由。"""
        seed_account()
        for method, path in (('post', f'{BASE}/{ACC}'),
                             ('put', f'{BASE}/{ACC}/1'),
                             ('delete', f'{BASE}/{ACC}/1'),
                             ('patch', f'{BASE}/{ACC}/1')):
            resp = getattr(self.client, method)(path, headers=self.headers)
            self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        """没 Cookie：如实 409 verification_required，一次平台都不打。"""
        seed_account(cookie='')
        body = self.fail_body(f'{BASE}/{ACC}', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_reported_as_need_relogin(self):
        seed_account()
        self.fake.errors[proto.LIST_API] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::会话过期')
        body = self.fail_body(f'{BASE}/{ACC}', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')

    def test_unauthorized_is_not_treated_as_logout(self):
        """缺站点 CODE 头（FAIL_BIZ_IDLE_USER_UNAUTHORIZED）→ 502 protocol_error，
        不能被当成账号掉线（那会让用户白扫一次码）。"""
        seed_account()
        self.fake.errors[proto.LIST_API] = XianyuError(UNAUTHORIZED_MESSAGE)
        body = self.fail_body(f'{BASE}/{ACC}', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertNotEqual(body.get('login_state'), 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'active')

    def test_site_code_headers_are_forwarded(self):
        """站点 CODE 头必须透传给 mtop（否则平台回 UNAUTHORIZED）。"""
        seed_account()
        self.ok_data(f'{BASE}/{ACC}')
        self.assertEqual(self.fake.calls[0]['extra_headers'],
                         {'idle_site_biz_code': 'COMMONPRO'})

    def test_bad_pagination_is_400_and_never_calls_platform(self):
        seed_account()
        for query, status in (('?page=abc', 400), ('?size=999', 400), ('?size=0', 400)):
            body = self.fail_body(f'{BASE}/{ACC}', query, status=status)
            self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])


class ItemTemplatesWriteTest(unittest.TestCase):
    """写接口（新建 / 修改 / 删除 / 设置运费）。

    红线只有一条：**未确认时一个平台请求都不发**。所以几乎每个用例都断言 `self.fake.calls`。
    """

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
        for table in ('accounts', 'account_credentials', 'operation_logs'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeTemplatesClient(
            payloads={
                proto.LIST_API: TEMPLE_LIST_EMPTY,
                proto.CREATE_API: {'code': 'success', 'msg': '', 'data': {'templateId': 111222}},
                proto.MODIFY_API: {'code': 'success', 'msg': ''},
                proto.DELETE_API: {'code': 'success', 'msg': ''},
                proto.UNBIND_API: {'code': 'success', 'msg': ''},
            },
            headers={'idle_site_biz_code': 'COMMONPRO'},
        )
        install_fake_protocol(self.fake)
        app = FastAPI()
        app.include_router(item_templates_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('tplwrite','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def post(self, path: str, body: Any):
        return self.client.post(path, json=body, headers=self.headers)

    def ok(self, path: str, body: Any) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def failed(self, path: str, body: Any, status: int, code: str) -> Dict[str, Any]:
        resp = self.post(path, body)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        self.assertEqual(payload.get('code'), code, payload)
        return payload

    def last_payload(self) -> Any:
        self.assertTrue(self.fake.calls, '一个平台请求都没发')
        return self.fake.calls[-1]['data']

    def write_paths(self) -> List[str]:
        return [
            f'{BASE}/{ACC}/create',
            f'{BASE}/{ACC}/unbind',
            f'{BASE}/{ACC}/tpl-1/modify',
            f'{BASE}/{ACC}/tpl-1/delete',
        ]

    # -- 闸门（最重要） --------------------------------------------------

    def test_every_write_without_confirm_is_400_and_never_calls_platform(self):
        """四个写接口：没带 confirm 一律 400，且**零平台请求**。"""
        seed_account()
        for path in self.write_paths():
            self.failed(path, {}, 400, 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_confirm_false_is_not_confirmation(self):
        """`confirm: false` / `'false'` / `'yes'` 都不是确认。"""
        seed_account()
        for value in (False, 'false', 'yes', 0, None):
            self.failed(f'{BASE}/{ACC}/create', {'confirm': value, 'template_name': '模版'},
                        400, 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_gate_runs_before_field_validation(self):
        """先看确认、再看字段：没确认时连「名称不能为空」都不该先报。"""
        seed_account()
        body = self.failed(f'{BASE}/{ACC}/create', {}, 400, 'confirmation_required')
        self.assertNotIn('不能为空', body['message'])
        self.assertEqual(self.fake.calls, [])

    def test_field_validation_runs_before_credential_check(self):
        """字段校验在建客户端之前：账号掉线时也该先说「名称没填」，而不是「去重新登录」。

        纯本地校验免费，先做；否则用户填错一个字要先去扫码，扫完再被告知字段错。
        """
        seed_account(login_state='need_relogin')
        body = self.failed(f'{BASE}/{ACC}/create', {'confirm': True}, 400, 'invalid_fields')
        self.assertIn('不能为空', body['message'])
        self.assertEqual(self.fake.calls, [])
        # 字段齐了才轮到凭据检查。
        self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '模版'},
                    409, 'verification_required')
        self.assertEqual(self.fake.calls, [])

    # -- 新建 ------------------------------------------------------------

    def test_create_requires_name_and_bounds_it(self):
        seed_account()
        self.failed(f'{BASE}/{ACC}/create', {'confirm': True}, 400, 'invalid_fields')
        self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '   '},
                    400, 'invalid_fields')
        self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': 'x' * 31},
                    400, 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_create_builds_the_official_payload(self):
        """字段名逐字照官方 bundle @2211045：camelCase + templateDetails 是 JSON 字符串。"""
        seed_account()
        data = self.ok(f'{BASE}/{ACC}/create', {
            'confirm': True,
            'template_name': '默认运费模版',
            'item_prov': '浙江省',
            'item_city': '杭州市',
            'is_default': True,
            'details': [
                {'receive_division_name': '全国', 'first_price': '8', 'excess_price': '3',
                 'first': '1', 'excess': '1'},
            ],
        })
        call = self.fake.calls[-1]
        self.assertEqual(call['api'], proto.CREATE_API)
        payload = call['data']
        self.assertEqual(payload['templateName'], '默认运费模版')
        self.assertIs(payload['isDefault'], True)
        self.assertEqual(payload['itemProv'], '浙江省')
        self.assertEqual(payload['itemCity'], '杭州市')
        # 明细：snake_case → 官方字段名，operate 默认 add
        import json as _json
        details = _json.loads(payload['templateDetails'])
        self.assertEqual(details, [{'receiveDivisionName': '全国', 'firstPrice': '8',
                                    'excessPrice': '3', 'first': '1', 'excess': '1',
                                    'operate': 'add'}])
        # 新建**不该**带 templateId / version
        self.assertNotIn('templateId', payload)
        self.assertNotIn('version', payload)
        # 站点 CODE 头照旧透传
        self.assertEqual(call['extra_headers'], {'idle_site_biz_code': 'COMMONPRO'})
        # 回包里带平台给的 templateId
        self.assertEqual(data['template_id'], 111222)
        self.assertEqual(data['action'], '新建')
        self.assertEqual(data['source'], 'platform')

    def test_create_omits_blank_optional_fields(self):
        """空的选填字段**不发**（官方是 undefined，不是空串）。"""
        seed_account()
        self.ok(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '只填名字'})
        payload = self.last_payload()
        self.assertEqual(payload['templateName'], '只填名字')
        self.assertEqual(payload['templateDetails'], '[]')
        for key in ('itemProv', 'itemCity', 'valuation', 'templateId', 'version'):
            self.assertNotIn(key, payload)

    def test_platform_business_code_decides_success(self):
        """传输层 success:true 不算成功：业务码不是 success 就是 502，并把平台原文交出去。"""
        seed_account()
        self.fake.payloads[proto.CREATE_API] = {'code': 'fail', 'msg': '模版名称已存在'}
        body = self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '重名'},
                           502, 'protocol_error')
        self.assertIn('模版名称已存在', body['message'])
        self.assertEqual(body['platform_errors']['code'], 'fail')

    def test_missing_business_code_is_502_not_fake_success(self):
        """平台没回业务码 → 502（不假装成功）。"""
        seed_account()
        self.fake.payloads[proto.CREATE_API] = None
        body = self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '模版'},
                           502, 'protocol_error')
        self.assertIn('业务码', body['message'])

    # -- 修改 / 删除 -----------------------------------------------------

    def test_modify_sends_template_id_and_version(self):
        """修改要比新建多 templateId 与 version（官方 bundle @2244766）。"""
        seed_account()
        self.ok(f'{BASE}/{ACC}/tpl-9/modify', {
            'confirm': True, 'template_name': '改名了', 'version': 7,
            'details': [{'receive_division_name': '全国', 'first_price': 8,
                         'excess_price': 3, 'first': 1, 'excess': 1}],
        })
        call = self.fake.calls[-1]
        self.assertEqual(call['api'], proto.MODIFY_API)
        self.assertEqual(call['data']['templateId'], 'tpl-9')
        self.assertEqual(call['data']['version'], 7)
        self.assertEqual(call['data']['templateName'], '改名了')

    def test_modify_still_requires_a_name(self):
        seed_account()
        self.failed(f'{BASE}/{ACC}/tpl-9/modify', {'confirm': True}, 400, 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_delete_sends_only_template_id(self):
        seed_account()
        data = self.ok(f'{BASE}/{ACC}/tpl-9/delete', {'confirm': True})
        call = self.fake.calls[-1]
        self.assertEqual(call['api'], proto.DELETE_API)
        self.assertEqual(call['data'], {'templateId': 'tpl-9'})
        self.assertEqual(data['template_id'], 'tpl-9')
        self.assertEqual(data['action'], '删除')

    # -- 设置运费（三种模式） --------------------------------------------

    def test_unbind_free_ship(self):
        seed_account()
        self.ok(f'{BASE}/{ACC}/unbind', {'confirm': True, 'item_ids': '1,2', 'mode': 'free_ship'})
        self.assertEqual(self.last_payload(), {
            'itemIds': '1,2', 'supportFreight': True, 'onlyTakeSelf': False,
            'needAutoFreight': False, 'isFreeShip': True,
        })

    def test_unbind_template_turns_support_freight_off(self):
        seed_account()
        self.ok(f'{BASE}/{ACC}/unbind', {'confirm': True, 'item_ids': ['1', '2'],
                                         'mode': 'template', 'template_id': 'tpl-9'})
        self.assertEqual(self.last_payload(), {
            'itemIds': '1,2', 'supportFreight': False, 'onlyTakeSelf': False,
            'needAutoFreight': False, 'isFreeShip': False, 'idleTemplateId': 'tpl-9',
        })

    def test_unbind_fixed_price_converts_yuan_to_cent(self):
        seed_account()
        self.ok(f'{BASE}/{ACC}/unbind', {'confirm': True, 'item_ids': '1',
                                         'mode': 'fixed', 'post_price': 8.8})
        payload = self.last_payload()
        self.assertEqual(payload['postPriceInCent'], 880)
        self.assertIs(payload['isFreeShip'], False)
        self.assertIs(payload['supportFreight'], True)

    def test_unbind_validation(self):
        seed_account()
        bad = [
            {'confirm': True, 'item_ids': '1', 'mode': 'nope'},
            {'confirm': True, 'item_ids': '', 'mode': 'free_ship'},
            {'confirm': True, 'item_ids': '1', 'mode': 'template'},
            {'confirm': True, 'item_ids': '1', 'mode': 'fixed'},
            {'confirm': True, 'item_ids': '1', 'mode': 'fixed', 'post_price': 'abc'},
            {'confirm': True, 'item_ids': '1', 'mode': 'fixed', 'post_price': 0},
            {'confirm': True, 'item_ids': '1', 'mode': 'fixed', 'post_price': -1},
        ]
        for body in bad:
            self.failed(f'{BASE}/{ACC}/unbind', body, 400, 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    # -- 账号与协议层异常 ------------------------------------------------

    def test_expired_account_is_409_and_never_calls_platform(self):
        seed_account(login_state='need_relogin')
        body = self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '模版'},
                           409, 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_no_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        self.failed(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '模版'},
                    409, 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_platform_exception_is_502_with_platform_text(self):
        seed_account()
        self.fake.errors[proto.DELETE_API] = XianyuError(UNAUTHORIZED_MESSAGE)
        body = self.failed(f'{BASE}/{ACC}/tpl-9/delete', {'confirm': True}, 502, 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED', body['message'])

    def test_write_never_calls_a_read_api(self):
        """写路径不许手滑打到只读接口上。"""
        seed_account()
        self.ok(f'{BASE}/{ACC}/create', {'confirm': True, 'template_name': '模版'})
        self.ok(f'{BASE}/{ACC}/tpl-1/modify', {'confirm': True, 'template_name': '模版'})
        self.ok(f'{BASE}/{ACC}/tpl-1/delete', {'confirm': True})
        self.ok(f'{BASE}/{ACC}/unbind', {'confirm': True, 'item_ids': '1', 'mode': 'free_ship'})
        self.assertEqual(self.fake.apis(), [proto.CREATE_API, proto.MODIFY_API,
                                            proto.DELETE_API, proto.UNBIND_API])
        for api in self.fake.apis():
            self.assertNotIn(api, proto.READ_METHODS)

    def test_write_registry_matches_the_four_actions(self):
        """`WRITE_APIS` 登记的名字必须和协议层常量一致（防止改名后测试还在看旧表）。"""
        self.assertEqual(sorted(proto.WRITE_APIS), ['create', 'delete', 'modify', 'unbind'])
        self.assertEqual(proto.WRITE_APIS['create']['api'], proto.CREATE_API)
        self.assertEqual(proto.WRITE_APIS['modify']['api'], proto.MODIFY_API)
        self.assertEqual(proto.WRITE_APIS['delete']['api'], proto.DELETE_API)
        self.assertEqual(proto.WRITE_APIS['unbind']['api'], proto.UNBIND_API)
        for action, spec in proto.WRITE_APIS.items():
            self.assertIn('bundle @', spec['evidence'], action)
        self.assertEqual(proto.WRITE_METHODS, {
            'create': 'create_temple', 'modify': 'modify_temple',
            'delete': 'del_temple', 'unbind': 'unbind_item',
        })


if __name__ == '__main__':
    unittest.main(verbosity=2)
