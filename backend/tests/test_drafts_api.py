"""草稿箱（官方 seller-item 的「草稿箱」tab：列表 / 详情 / 删除）的测试。

**为什么单开一组**：草稿箱里既有只读也有**平台写操作**（删草稿不可撤销）。最容易犯的错：

1. 没确认就发请求（红线：未确认时**一个平台请求都不许发**）；
2. 字段校验排在凭据检查**之后**（掉线时先报「去重新登录」，用户填半天才发现草稿 ID 是空的
   —— 三步顺序必须是 **确认 → 字段校验 → 建客户端**）；
3. 把「草稿列表为空」当成失败（一件草稿都没有是**正常结果**，不是错误）；
4. 平台没给 `sucCount`/`failCount` 就替它编成功条数（`ok` 只能是 `None`，不是 0 也不是全成功）；
5. 平台没逐条回报就编造 `failed[]` 明细；
6. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 之类的平台拒绝当成掉线，让用户白扫一次码；
7. `draftIds` 的入参形态：官方是**逗号串**，UI 给数组 —— 两种都要收。

**一次真机请求都没发过**（本机账号缺部分平台业务权限，红线不允许为了验证去动草稿）。
这里用假协议层钉住「字段形状与官方 bundle 逐字一致」，真机是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_drafts_api.py"
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

from backend.app.api import drafts as drafts_api  # noqa: E402
from backend.app.core import db, events, security  # noqa: E402
from backend.app.services import drafts as service  # noqa: E402
from backend.app.xianyu import draft as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'dr_acc'
BASE = '/api/drafts'

#: 列表成功（bundle @3072333 读 `e.data.data.itemSearchResponseList` + `e.data.data.total`）。
SEARCH_OK = {
    'code': 'success',
    'data': {
        'itemSearchResponseList': [
            {'draftId': 'd1', 'title': '草稿一', 'price': '39.90', 'maxPrice': '59.90',
             'quantity': 3, 'gmtCreateStr': '2026-10-01 10:00:00',
             'imageInfoDOList': [{'url': 'https://img.example/d1.jpg'}]},
            {'draftId': 'd2', 'title': '', 'quantity': None},
        ],
        'total': 2,
    },
}
#: **一件草稿都没有**：空数组 + total 0 —— 这是正常结果，不是失败。
SEARCH_EMPTY = {'code': 'success', 'data': {'itemSearchResponseList': [], 'total': 0}}
#: 平台回了成功但**没有**列表字段 —— 不许把它说成「0 条草稿」。
SEARCH_NO_KEY = {'code': 'success', 'data': {'somethingElse': 1}}
#: 详情：`fields` 原样透传（键名不翻译、不猜）。
DETAIL_OK = {'code': 'success', 'data': {'title': '草稿一', 'priceInfo': {'value': 39.9}}}
#: 详情拿不到内容 —— 契约里是 `null`，不是 `{}`。
DETAIL_NULL = {'code': 'success', 'data': None}
#: 单删成功（官方不读响应体，我们只要求平台 code 是 success）。
DELETE_OK = {'code': 'success', 'data': {}}
#: 批删成功（bundle @4098620 读 `sucCount` / `failCount`）。
BATCH_OK = {'code': 'success', 'data': {'sucCount': 2, 'failCount': 0}}
#: 批删一条失败：平台**不说是哪条** ⇒ `failed[]` 必须为空 + 一条 warning。
BATCH_PARTIAL = {'code': 'success', 'data': {'sucCount': 1, 'failCount': 1}}
#: 平台连计数都没给 ⇒ `ok` 是 `None`，不许编成 0 或「全部成功」。
BATCH_NO_COUNTS = {'code': 'success', 'data': {}}
#: 平台拒绝（无权限 ≠ 掉线）。
NO_PERMISSION = {'code': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED', 'msg': '无权限访问', 'data': None}


class XianyuError(RuntimeError):
    """与协议层同名（`common.call_failed` 只按异常本身分类，不看类名）。"""


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeDraftsClient:
    """假协议层：只实现 `mtop`，按 api 名回话，记录每次调用的参数。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[Dict[str, Any]] = []

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   spm_cnt: Optional[str] = None,
                   extra_headers: Optional[Dict[str, str]] = None) -> Any:
        self.calls.append({'api': api, 'data': data, 'v': v})
        error = self.errors.get(api)
        if error is not None:
            raise error
        return {'success': True, 'data': self.payloads.get(api), 'message': '',
                'ret': ['SUCCESS::调用成功']}

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


def install_fake_client(client: FakeDraftsClient, *, with_factory: bool = True) -> None:
    """把 `backend.app.xianyu.client` 换成假模块（`common.get_client` 懒导入它）。

    `with_factory=True`（默认）注入 `client.py::get_client` 工厂 ⇒ 绕过 Cookie 检查；
    测凭据检查本身时必须 `with_factory=False`。
    """
    module = types.ModuleType('backend.app.xianyu.client')
    if with_factory:
        def factory(account_id: str) -> FakeDraftsClient:
            client.account_id = account_id
            return client

        module.get_client = factory  # type: ignore[attr-defined]

    def ctor(cookie: str) -> FakeDraftsClient:
        client.cookie = cookie
        return client

    module.XianyuClient = ctor  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module


def remove_fake_client() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def login_state(account_id: str = ACC) -> str:
    row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                       (account_id,))
    return str(row['login_state']) if row else ''


def operation_actions() -> List[str]:
    return [str(r['action']) for r in
            db.query('SELECT action FROM operation_logs ORDER BY id')]


# =========================================================================== 协议层纯函数


class DraftProtocolTest(unittest.TestCase):
    """协议层纯函数：只钉字段形状与解析，不碰服务层。"""

    def test_five_apis_are_the_ones_from_the_bundle(self):
        self.assertEqual(proto.SEARCH_API, 'mtop.alibaba.idle.item.pc.draft.search')
        self.assertEqual(proto.GET_API, 'mtop.alibaba.idle.item.pc.draft.get')
        self.assertEqual(proto.UPDATE_API, 'mtop.alibaba.idle.item.pc.draft.update')
        self.assertEqual(proto.DELETE_API, 'mtop.alibaba.idle.item.pc.draft.delete')
        self.assertEqual(proto.BATCH_DELETE_API,
                         'mtop.alibaba.idle.idle.pc.draft.batch.delete')
        self.assertEqual(proto.DEFAULT_VERSION, '1.0')
        self.assertEqual(proto.PAGE_SIZES, (10, 20, 50))

    def test_assert_write_allows_the_two_and_rejects_reads(self):
        for action in ('delete', 'batch_delete'):
            api = proto.WRITE_APIS[action]['api']
            self.assertEqual(proto.assert_write(api), api)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.SEARCH_API)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_write(proto.UPDATE_API)

    def test_assert_read_allows_the_two_and_rejects_writes(self):
        for api in (proto.SEARCH_API, proto.GET_API):
            self.assertEqual(proto.assert_read(api), api)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_read(proto.DELETE_API)

    def test_update_is_registered_but_not_callable(self):
        """`draft.update` 故意不实现（payload 形状没证据），登记出来是为了说明「不是漏了」。"""
        self.assertIn(proto.UPDATE_API, proto.NOT_IMPLEMENTED)
        self.assertNotIn(proto.UPDATE_API, proto.ALL_APIS)
        self.assertNotIn(proto.UPDATE_API, proto.READ_APIS)
        self.assertNotIn(proto.UPDATE_API, proto.WRITE_API_SET)
        with self.assertRaises(proto.UnknownApi):
            proto.assert_read(proto.UPDATE_API)

    def test_normalize_draft_ids_accepts_both_shapes(self):
        """官方发逗号串，UI 给数组 —— 两种入参都要收。"""
        self.assertEqual(proto.normalize_draft_ids(['d1', 'd2']), 'd1,d2')
        self.assertEqual(proto.normalize_draft_ids('d1,d2'), 'd1,d2')
        self.assertEqual(proto.normalize_draft_ids(('d1',)), 'd1')
        self.assertEqual(proto.normalize_draft_ids(['d1', '', None, 'd2']), 'd1,d2')
        self.assertEqual(proto.normalize_draft_ids(None), '')
        self.assertEqual(proto.normalize_draft_ids(123), '')
        self.assertEqual(proto.draft_id_list('d1,d2'), ['d1', 'd2'])
        self.assertEqual(proto.draft_id_list(['d1', 'd1', 'd2']), ['d1', 'd2'])

    def test_list_request_is_pageno_pagesize(self):
        """bundle @4079050：`s = {pageNo: current||1, pageSize: size||20}`。"""
        self.assertEqual(proto.list_request(2, 20), (proto.SEARCH_API,
                                                     {'pageNo': 2, 'pageSize': 20}))
        self.assertEqual(proto.list_request('3', '50'),
                         (proto.SEARCH_API, {'pageNo': 3, 'pageSize': 50}))
        # pageNo 从 1 起：0 / 负数都纠正成 1
        self.assertEqual(proto.list_request(0)[1]['pageNo'], 1)
        self.assertEqual(proto.list_request(-5)[1]['pageNo'], 1)
        self.assertEqual(proto.list_request(None, None)[1], {'pageNo': 1, 'pageSize': 20})

    def test_list_request_rejects_an_unsupported_page_size(self):
        """官方草稿 tab 只有 10 / 20 / 50 三档，别的值直接拒绝（不替平台猜）。"""
        for bad in (30, 1, 100):
            with self.assertRaises(ValueError):
                proto.list_request(1, bad)

    def test_detail_and_delete_requests_are_single_ids(self):
        self.assertEqual(proto.detail_request('d1'), (proto.GET_API, {'draftId': 'd1'}))
        self.assertEqual(proto.delete_request('d1', confirm=True),
                         (proto.DELETE_API, {'draftId': 'd1'}))

    def test_batch_delete_request_is_a_comma_string(self):
        """bundle @4098620 原文 `{draftIds: T.join(",")}` —— 发数组平台不认。"""
        self.assertEqual(proto.batch_delete_request(['d1', 'd2'], confirm=True),
                         (proto.BATCH_DELETE_API, {'draftIds': 'd1,d2'}))
        self.assertEqual(proto.batch_delete_request('d1,d2', confirm=True),
                         (proto.BATCH_DELETE_API, {'draftIds': 'd1,d2'}))

    def test_write_builders_require_literal_true_confirm(self):
        for bad in (None, False, 'true', 1, 'yes'):
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.delete_request('d1', confirm=bad)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.batch_delete_request(['d1'], confirm=bad)

    def test_protocol_layer_blocks_empty_ids(self):
        with self.assertRaises(ValueError):
            proto.detail_request('')
        with self.assertRaises(ValueError):
            proto.delete_request('  ', confirm=True)
        with self.assertRaises(ValueError):
            proto.batch_delete_request([], confirm=True)

    def test_request_of_dispatch_and_unknown_action(self):
        self.assertEqual(proto.request_of('delete', draft_id='d1', confirm=True),
                         (proto.DELETE_API, {'draftId': 'd1'}))
        self.assertEqual(proto.request_of('batch_delete', draft_ids=['d1'], confirm=True),
                         (proto.BATCH_DELETE_API, {'draftIds': 'd1'}))
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('update', draft_id='d1')
        self.assertEqual(sorted(proto.ACTIONS), ['batch_delete', 'delete'])

    def test_is_success_treats_an_empty_draft_box_as_success(self):
        """空草稿箱是正常结果 —— 不能用「业务体为真」当判据。"""
        self.assertTrue(proto.is_success({'success': True, 'data': SEARCH_EMPTY}))
        self.assertTrue(proto.is_success({'success': True, 'data': SEARCH_OK}))
        self.assertTrue(proto.is_success({'success': True, 'data': DELETE_OK}))
        self.assertFalse(proto.is_success({'success': True, 'data': NO_PERMISSION}))
        self.assertFalse(proto.is_success({'success': False, 'data': None,
                                           'message': 'boom', 'ret': ['E::x']}))
        self.assertFalse(proto.is_success(None))
        self.assertFalse(proto.is_success({}))

    def test_parse_list_maps_pinned_keys_and_never_invents_values(self):
        parsed = proto.parse_list({'success': True, 'data': SEARCH_OK})
        self.assertTrue(parsed['found_key'])
        self.assertEqual(parsed['total'], 2)
        first, second = parsed['items']
        self.assertEqual(first, {
            'draft_id': 'd1', 'title': '草稿一', 'image_url': 'https://img.example/d1.jpg',
            'price': '39.90', 'max_price': '59.90', 'quantity': 3,
            'created_at': '2026-10-01 10:00:00',
        })
        # 空标题 / 空库存 / 没有图 ⇒ None（不造空串、不造 0）
        self.assertIsNone(second['title'])
        self.assertIsNone(second['quantity'])
        self.assertIsNone(second['image_url'])
        self.assertIsNone(second['price'])
        self.assertIsNone(second['created_at'])

    def test_parse_list_flags_a_missing_key(self):
        parsed = proto.parse_list({'success': True, 'data': SEARCH_NO_KEY})
        self.assertFalse(parsed['found_key'])
        self.assertEqual(parsed['items'], [])
        self.assertIsNone(parsed['total'])

    def test_parse_detail_passes_the_payload_through(self):
        parsed = proto.parse_detail({'success': True, 'data': DETAIL_OK})
        self.assertEqual(parsed['fields'], DETAIL_OK['data'])
        self.assertIsNone(proto.parse_detail({'success': True, 'data': DETAIL_NULL})['fields'])
        self.assertIsNone(proto.parse_detail({'success': True, 'data': {'code': 'success'}})
                          ['fields'])

    def test_parse_process_reads_the_two_counts(self):
        self.assertEqual(proto.parse_process({'success': True, 'data': BATCH_OK}),
                         {'suc_count': 2, 'fail_count': 0})
        # 0 与「平台没给」是两回事
        self.assertEqual(proto.parse_process({'success': True, 'data': BATCH_NO_COUNTS}),
                         {'suc_count': None, 'fail_count': None})
        self.assertEqual(proto.parse_process(None), {'suc_count': None, 'fail_count': None})

    def test_message_of_falls_back_to_the_client_layer(self):
        raw = {'success': False, 'data': None, 'message': 'boom', 'ret': ['E::x']}
        self.assertEqual(proto.message_of(raw), 'boom')
        self.assertEqual(proto.message_of({'success': False, 'data': None, 'message': '',
                                           'ret': ['E::x']}), 'E::x')
        self.assertEqual(proto.message_of({'success': True, 'data': NO_PERMISSION}),
                         '无权限访问')
        self.assertEqual(proto.failure_reason({'success': True, 'data': NO_PERMISSION},
                                              '兜底'), '无权限访问')
        self.assertEqual(proto.failure_reason(None, '兜底'), '兜底')


# =========================================================================== 服务层 + HTTP


class DraftApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        remove_fake_client()
        db.reset_connection()
        cls.tmp.cleanup()

    def setUp(self):
        db.reset_connection()
        remove_fake_client()
        for table in ('accounts', 'account_credentials', 'operation_logs'):
            db.execute(f'DELETE FROM {table}')
        self.fake = FakeDraftsClient(payloads={
            proto.SEARCH_API: SEARCH_OK,
            proto.GET_API: DETAIL_OK,
            proto.DELETE_API: DELETE_OK,
            proto.BATCH_DELETE_API: BATCH_OK,
        })
        install_fake_client(self.fake)
        app = FastAPI()
        app.include_router(drafts_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('drapi','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str):
        return self.client.get(path, headers=self.headers)

    def post(self, path: str, body: Any = None):
        return self.client.post(path, json=body if body is not None else {},
                                headers=self.headers)

    def ok_data(self, resp) -> Dict[str, Any]:
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = resp.json()
        self.assertTrue(payload.get('success'), payload)
        return payload['data']

    def fail_body(self, resp, status: int = 400) -> Dict[str, Any]:
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    # -- 只读：列表 ------------------------------------------------------

    def test_list_maps_rows_and_paging(self):
        seed_account()
        data = self.ok_data(self.get(f'{BASE}/{ACC}?page=1&size=20'))
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['size'], 20)
        self.assertEqual(data['pages'], 1)
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        self.assertEqual(data['items'][0]['draft_id'], 'd1')
        self.assertEqual(data['items'][0]['image_url'], 'https://img.example/d1.jpg')
        self.assertEqual(data['items'][1]['quantity'], None)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(self.fake.apis(), [proto.SEARCH_API])
        call = self.fake.calls[0]
        self.assertEqual(call['data'], {'pageNo': 1, 'pageSize': 20})
        self.assertEqual(call['v'], '1.0')

    def test_empty_draft_box_is_success_not_an_error(self):
        seed_account()
        self.fake.payloads[proto.SEARCH_API] = SEARCH_EMPTY
        data = self.ok_data(self.get(f'{BASE}/{ACC}'))
        self.assertEqual(data['items'], [])
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['warnings'], [])

    def test_missing_list_key_gets_a_warning_not_a_silent_zero(self):
        seed_account()
        self.fake.payloads[proto.SEARCH_API] = SEARCH_NO_KEY
        data = self.ok_data(self.get(f'{BASE}/{ACC}'))
        self.assertEqual(data['items'], [])
        self.assertIsNone(data['total'])
        self.assertEqual([w['code'] for w in data['warnings']], ['list_key_missing'])
        self.assertIn('不等于', data['warnings'][0]['message'])

    def test_unsupported_page_size_is_400_without_touching_the_platform(self):
        seed_account()
        body = self.fail_body(self.get(f'{BASE}/{ACC}?size=30'))
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('10', body['message'])
        self.assertEqual(self.fake.calls, [])

    def test_list_platform_refusal_is_502_and_does_not_touch_login_state(self):
        seed_account()
        self.fake.payloads[proto.SEARCH_API] = NO_PERMISSION
        body = self.fail_body(self.get(f'{BASE}/{ACC}'), status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertEqual(body['platform_errors']['api'], proto.SEARCH_API)
        self.assertEqual(body['platform_errors']['code'],
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(login_state(), 'active')

    # -- 只读：详情 ------------------------------------------------------

    def test_detail_passes_field_names_through_untouched(self):
        seed_account()
        data = self.ok_data(self.get(f'{BASE}/{ACC}/d1'))
        self.assertEqual(data['draft_id'], 'd1')
        self.assertEqual(data['fields'], DETAIL_OK['data'])
        self.assertEqual([w['code'] for w in data['warnings']],
                         ['field_names_not_pinned'])
        self.assertEqual(self.fake.calls[0]['data'], {'draftId': 'd1'})

    def test_detail_without_content_is_null_not_an_empty_object(self):
        seed_account()
        self.fake.payloads[proto.GET_API] = DETAIL_NULL
        data = self.ok_data(self.get(f'{BASE}/{ACC}/d1'))
        self.assertIsNone(data['fields'])
        self.assertEqual([w['code'] for w in data['warnings']],
                         ['platform_returned_nothing'])

    def test_read_path_never_calls_a_write_api(self):
        seed_account()
        self.get(f'{BASE}/{ACC}')
        self.get(f'{BASE}/{ACC}/d1')
        self.assertEqual(set(self.fake.apis()), {proto.SEARCH_API, proto.GET_API})

    def test_missing_cookie_is_409_and_sends_nothing(self):
        """没有 Cookie ⇒ 409 `verification_required`（与 items / item_templates 同口径），零平台调用。"""
        seed_account(cookie='')
        install_fake_client(self.fake, with_factory=False)
        body = self.fail_body(self.get(f'{BASE}/{ACC}'), status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404_and_sends_nothing(self):
        body = self.fail_body(self.get(f'{BASE}/nosuch'), status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    # -- 确认闸门（红线）------------------------------------------------

    def test_delete_without_confirm_sends_nothing(self):
        seed_account()
        body = self.fail_body(self.post(f'{BASE}/{ACC}/d1/delete'))
        self.assertEqual(body['code'], service.CONFIRM_CODE)
        self.assertIn('confirm=true', body['message'])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(operation_actions(), [])

    def test_batch_delete_without_confirm_sends_nothing(self):
        seed_account()
        body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                        {'draft_ids': ['d1', 'd2']}))
        self.assertEqual(body['code'], service.CONFIRM_CODE)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(operation_actions(), [])

    def test_false_like_confirm_values_are_not_consent(self):
        seed_account()
        for value in (False, 'false', 'FALSE', 0, '', None, 'yes', 2):
            body = self.fail_body(self.post(f'{BASE}/{ACC}/d1/delete',
                                            {'confirm': value}))
            self.assertEqual(body['code'], service.CONFIRM_CODE, value)
            body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                            {'draft_ids': ['d1'], 'confirm': value}))
            self.assertEqual(body['code'], service.CONFIRM_CODE, value)
        self.assertEqual(self.fake.calls, [])

    def test_truthy_accepts_true_string_and_one(self):
        self.assertTrue(service._truthy(True))
        self.assertTrue(service._truthy('true'))
        self.assertTrue(service._truthy(' TRUE '))
        self.assertTrue(service._truthy(1))
        self.assertFalse(service._truthy('1'))
        self.assertFalse(service._truthy(0))
        self.assertFalse(service._truthy('yes'))

    # -- 三步顺序：确认 → 字段校验 → 建客户端 ----------------------------

    def test_field_validation_runs_before_credential_check(self):
        """掉线账号 + 空 draft_ids → 先说「draft_ids 不能为空」，不是「去重新登录」。"""
        seed_account(cookie='', login_state='need_relogin')
        install_fake_client(self.fake, with_factory=False)
        body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                        {'draft_ids': [], 'confirm': True}))
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn('draft_ids', body['message'])
        self.assertEqual(self.fake.calls, [])
        # 字段齐了才轮到凭据检查
        body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                        {'draft_ids': ['d1'], 'confirm': True}), status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_batch_delete_over_the_limit_is_rejected_locally(self):
        seed_account()
        too_many = [f'd{i}' for i in range(service.BATCH_WRITE_LIMIT + 1)]
        body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                        {'draft_ids': too_many, 'confirm': True}))
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertIn(str(service.BATCH_WRITE_LIMIT), body['message'])
        self.assertEqual(self.fake.calls, [])

    # -- 写：happy path --------------------------------------------------

    def test_delete_happy_path(self):
        seed_account()
        data = self.ok_data(self.post(f'{BASE}/{ACC}/d1/delete', {'confirm': True}))
        self.assertEqual(data['draft_id'], 'd1')
        self.assertTrue(data['deleted'])
        self.assertEqual(data['source'], 'platform')
        call = self.fake.calls[0]
        self.assertEqual(call['api'], proto.DELETE_API)
        self.assertEqual(call['data'], {'draftId': 'd1'})
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(operation_actions(), ['draft_delete'])
        self.assertTrue(any(e.get('topic') == 'drafts.changed' for e in events.recent(20)))

    def test_batch_delete_sends_one_request_with_a_comma_string(self):
        """UI 给数组、AI 给逗号串 —— 到平台都是逗号串，而且**只发一次请求**。"""
        seed_account()
        data = self.ok_data(self.post(f'{BASE}/{ACC}/batch-delete',
                                      {'draft_ids': ['d1', 'd2'], 'confirm': True}))
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['ok'], 2)
        self.assertEqual(data['suc_count'], 2)
        self.assertEqual(data['fail_count'], 0)
        self.assertEqual(data['failed'], [])
        self.assertFalse(data['per_item_report'])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.calls[0]['data'], {'draftIds': 'd1,d2'})
        self.assertEqual(operation_actions(), ['draft_batch_delete'])

    def test_batch_delete_accepts_a_comma_string_from_the_ai_side(self):
        seed_account()
        data = self.ok_data(self.post(f'{BASE}/{ACC}/batch-delete',
                                      {'draft_ids': 'd1, d2', 'confirm': True}))
        self.assertEqual(data['total'], 2)
        self.assertEqual(self.fake.calls[0]['data'], {'draftIds': 'd1,d2'})

    def test_batch_delete_never_invents_per_item_failures(self):
        """平台只报 `failCount`、不说哪几条 ⇒ `failed[]` 必须是空的 + 一条 warning。"""
        seed_account()
        self.fake.payloads[proto.BATCH_DELETE_API] = BATCH_PARTIAL
        data = self.ok_data(self.post(f'{BASE}/{ACC}/batch-delete',
                                      {'draft_ids': ['d1', 'd2'], 'confirm': True}))
        self.assertEqual(data['ok'], 1)
        self.assertEqual(data['suc_count'], 1)
        self.assertEqual(data['fail_count'], 1)
        self.assertEqual(data['failed'], [])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_per_item_report'])
        self.assertIn('没有说明是哪几条', data['warnings'][0]['message'])

    def test_batch_delete_without_counts_reports_unknown_not_success(self):
        seed_account()
        self.fake.payloads[proto.BATCH_DELETE_API] = BATCH_NO_COUNTS
        data = self.ok_data(self.post(f'{BASE}/{ACC}/batch-delete',
                                      {'draft_ids': ['d1', 'd2'], 'confirm': True}))
        self.assertIsNone(data['ok'])
        self.assertIsNone(data['suc_count'])
        self.assertIsNone(data['fail_count'])
        self.assertEqual([w['code'] for w in data['warnings']], ['no_counts'])

    # -- 写：失败路径 ----------------------------------------------------

    def test_platform_refusal_is_502_and_keeps_the_login_state(self):
        """无权限 ≠ 掉线：不许写 `need_relogin`，否则用户白扫一次码。"""
        seed_account()
        for api in (proto.DELETE_API, proto.BATCH_DELETE_API):
            self.fake.payloads[api] = NO_PERMISSION
        body = self.fail_body(self.post(f'{BASE}/{ACC}/d1/delete', {'confirm': True}),
                              status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertEqual(body['platform_errors']['code'],
                         'FAIL_BIZ_IDLE_USER_UNAUTHORIZED')
        self.assertEqual(login_state(), 'active')
        body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                        {'draft_ids': ['d1'], 'confirm': True}), status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertEqual(login_state(), 'active')

    def test_session_expired_is_409_and_writes_back_need_relogin(self):
        seed_account()
        self.fake.errors[proto.BATCH_DELETE_API] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_body(self.post(f'{BASE}/{ACC}/batch-delete',
                                        {'draft_ids': ['d1'], 'confirm': True}), status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(login_state(), 'need_relogin')

    def test_missing_cookie_on_write_is_409_and_sends_nothing(self):
        seed_account(cookie='')
        install_fake_client(self.fake, with_factory=False)
        body = self.fail_body(self.post(f'{BASE}/{ACC}/d1/delete', {'confirm': True}),
                              status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_missing_draft_id_on_single_delete_is_400(self):
        seed_account()
        body = self.fail_body(self.post(f'{BASE}/{ACC}/%20/delete', {'confirm': True}))
        self.assertEqual(body['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_requires_login(self):
        resp = self.client.get(f'{BASE}/{ACC}')
        self.assertEqual(resp.status_code, 401, resp.text)


# =========================================================================== 路由


class DraftRouteTest(unittest.TestCase):
    def test_four_routes_are_registered_with_the_expected_methods(self):
        routes = {}
        for route in drafts_api.router.routes:
            path = getattr(route, 'path', '')
            methods = tuple(sorted(getattr(route, 'methods', ()) or ()))
            routes[path] = methods
        self.assertEqual(routes.get('/api/drafts/{account_id}'), ('GET',))
        self.assertEqual(routes.get('/api/drafts/{account_id}/{draft_id}'), ('GET',))
        self.assertEqual(routes.get('/api/drafts/{account_id}/batch-delete'), ('POST',))
        self.assertEqual(routes.get('/api/drafts/{account_id}/{draft_id}/delete'), ('POST',))

    def test_collection_action_is_declared_before_the_single_draft_route(self):
        paths = [getattr(r, 'path', '') for r in drafts_api.router.routes]
        self.assertLess(paths.index('/api/drafts/{account_id}/batch-delete'),
                        paths.index('/api/drafts/{account_id}/{draft_id}/delete'))

    def test_main_registers_the_drafts_module(self):
        from backend.app import main
        self.assertIn('drafts', main.ROUTE_MODULES)


if __name__ == '__main__':
    unittest.main(verbosity=2)
