# -*- coding: utf-8 -*-
"""纠纷协商（联系纠纷小二）的测试：只读 3 条 + 写 4 条。

**为什么单开一组**：这一族最容易犯的错是：

1. 没确认就发写请求（红线：未确认时**一个平台请求都不许发**）；
2. 确认判断用 `bool(...)` —— `bool('false')` 是 `True`，把用户明确的拒绝当成同意；
3. **`content` 抄错形态**：我们发出去的是 **JSON 字符串**（`{"text":"…"}`），而平台回来的
   可能是**已经解析好的对象**（官方渲染直接读 `content.text`）—— 发错平台不认，收错界面空白；
4. 把平台没返回的留言列表补成 `[]` —— 「平台没说」和「真的一条都没有」是两件事；
5. 字段校验排在凭据检查**之后** —— 掉线时先喊「去重新登录」，用户填半天才发现内容为空；
6. 把登录态判错：平台「无权限」不等于掉线，不该让用户白扫一次码；
7. 图片漏掉 `sendKey`（官方**总是**带，空串也带）或漏掉 `sceneType:'userActive'`。

**一次真机请求都没发过**（第 41 批红线是零平台写请求）。这里用假协议层钉住「接口名、版本号、
入参形状与官方 bundle 逐字一致」（偏移见 `backend/app/xianyu/dispute.py` 的常量注释与
`WRITE_APIS[...]['evidence']`），平台是否接受仍属未验证。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_disputes_api.py"

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

from backend.app.api import disputes as disputes_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import disputes  # noqa: E402
from backend.app.xianyu import dispute as proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'dispute_acc'
BASE = '/api/disputes'
CID = 'cid-dispute-1'

SUCCESS = {'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

#: 取会话成功（官方读 `data.conversationId`）。
CONV_OK = {'success': True, 'data': {'conversationId': CID}, **SUCCESS}
#: 平台回了信封但没给会话 id —— 拿不到 id 后面每一步都做不了，必须 502 而不是假装成功。
CONV_NO_ID = {'success': True, 'data': {}, **SUCCESS}

#: 一条文字留言（`content` 是**字符串**，平台就是这样回的）。
MSG_TEXT = {
    'msgId': 'm-1',
    'header': {'sequence': 3, 'senderRole': 'USER', 'senderName': '我',
               'senderAvatar': 'https://a/x.png', 'gmtCreate': 1700000000000},
    'content': '{"text":"你好，麻烦看下"}',
    'contentType': 'PLAIN_TEXT',
}
#: 一条图片留言（`content` 是**对象**）。
MSG_IMAGE = {
    'msgId': 'm-2',
    'header': {'sequence': 4, 'senderRole': 'XIAOER', 'senderName': '小二'},
    'content': {'url': 'https://a/y.png', 'ext': 'PNG', 'altText': '凭证',
                'width': 800, 'height': 600, 'thumbURL': 'https://a/y-thumb.png'},
    'contentType': 'IMAGE',
}
HIST_OK = {'success': True, 'data': {'messageList': [MSG_TEXT, MSG_IMAGE],
                                     'currentTime': '1700000000123'}, **SUCCESS}
#: 平台明确回了**空列表** —— 这是「真的一条留言都没有」，与「没给字段」不同。
HIST_EMPTY = {'success': True, 'data': {'messageList': [], 'currentTime': '1'}, **SUCCESS}
#: 平台没给列表键（可能改了字段名）—— 必须 `null` + warning，不能补成 `[]`。
HIST_NO_KEY = {'success': True, 'data': {'foo': 1}, **SUCCESS}

UNREAD_OK = {'success': True, 'data': {'result': [MSG_TEXT]}, **SUCCESS}
UNREAD_EMPTY = {'success': True, 'data': {'result': []}, **SUCCESS}
UNREAD_NO_KEY = {'success': True, 'data': {}, **SUCCESS}

#: 发留言成功（官方读 `data.success` / `msgId` / `sequence` / `tips`）。
SEND_OK = {'success': True, 'data': {'success': True, 'msgId': 'm-9', 'sequence': 9,
                                     'tips': '小二已收到'}, **SUCCESS}
#: 平台接受了但没回 msgId / sequence —— 不是失败，但要给 informational warning。
SEND_NO_ECHO = {'success': True, 'data': {'success': True}, **SUCCESS}
#: 业务层拒绝（信封 success=True，业务体 success=False）。
SEND_BIZ_FAIL = {'success': True, 'data': {'success': False, 'failReason': '会话已关闭'}, **SUCCESS}
#: 真机原文：卖家工作台无权限。
NO_PERMISSION = {'success': False, 'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                 'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}

#: 四条写路由 + 各自的接口名（confirm 由用例单独塞）。
WRITE_ROUTES: List[tuple] = [
    ('POST', f'{BASE}/{ACC}/{CID}/messages', {'text': '你好'}, proto.SEND_API),
    ('POST', f'{BASE}/{ACC}/{CID}/messages/image', {'url': 'https://a/y.png'}, proto.SEND_API),
    ('POST', f'{BASE}/{ACC}/{CID}/urge', {}, proto.SEND_API),
    ('POST', f'{BASE}/{ACC}/{CID}/mark-read', {'msg_id': 'm-1', 'sequence': 3},
     proto.MARK_READ_API),
]

#: 七条路由（path, method）—— 冻结契约里的全部。
EXPECTED_ROUTES = {
    ('GET', f'{BASE}/{{account_id}}/conversation'),
    ('GET', f'{BASE}/{{account_id}}/{{conversation_id}}/messages'),
    ('GET', f'{BASE}/{{account_id}}/{{conversation_id}}/unread'),
    ('POST', f'{BASE}/{{account_id}}/{{conversation_id}}/messages'),
    ('POST', f'{BASE}/{{account_id}}/{{conversation_id}}/messages/image'),
    ('POST', f'{BASE}/{{account_id}}/{{conversation_id}}/urge'),
    ('POST', f'{BASE}/{{account_id}}/{{conversation_id}}/mark-read'),
}

FALSE_LIKE = ('false', 'FALSE', ' false ', 'no', 'yes', '0', 0, '', None, 2, [], {})


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeDisputeClient:
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


def install_fake_protocol(client: FakeDisputeClient, *, with_factory: bool = True,
                          factory: Any = None) -> None:
    """注入假协议层。

    `with_factory=False` 时**不**注入 `get_client` 工厂，于是 `common.get_client` 会走
    「读库里的 cookie → XianyuClient(cookie)」那条真实路径 —— 只有这样才能测
    「没有 Cookie 一律 409 且零平台调用」。
    """
    module = types.ModuleType('backend.app.xianyu.client')

    def default_factory(account_id: str) -> FakeDisputeClient:
        client.account_id = account_id
        return client

    if with_factory:
        module.get_client = factory or default_factory  # type: ignore[attr-defined]
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


def full_client(**overrides: Any) -> FakeDisputeClient:
    payloads = {
        proto.CONVERSATION_API: CONV_OK,
        proto.HISTORY_API: HIST_OK,
        proto.UNREAD_API: UNREAD_OK,
        proto.SEND_API: SEND_OK,
        proto.MARK_READ_API: SEND_OK,
    }
    payloads.update(overrides)
    return FakeDisputeClient(payloads)


# --------------------------------------------------------------------------- 协议层


class DisputesProtocolTest(unittest.TestCase):
    """接口名、版本、白名单、请求形状、解析（全部来自 bundle 静态证据）。"""

    def test_api_names_are_the_official_ones(self):
        self.assertEqual(proto.CONVERSATION_API,
                         'mtop.taobao.idle.shop.dispute.message.get.conversation')
        self.assertEqual(proto.HISTORY_API,
                         'mtop.taobao.idle.shop.dispute.message.query.history')
        self.assertEqual(proto.UNREAD_API,
                         'mtop.taobao.idle.shop.dispute.message.query.unread.list')
        self.assertEqual(proto.MARK_READ_API, 'mtop.taobao.idle.shop.dispute.message.mark.read')
        self.assertEqual(proto.SEND_API, 'mtop.taobao.idle.shop.dispute.message.send')

    def test_every_api_is_v1_0(self):
        """这一族**全是** v1.0（与 IM 那族「版本逐条不同」不一样，别抄混）。"""
        self.assertEqual(set(proto.READ_VERSIONS.values()), {'1.0'})
        self.assertEqual(set(proto.WRITE_VERSIONS.values()), {'1.0'})

    def test_read_and_write_sets_are_disjoint_and_complete(self):
        self.assertEqual(set(proto.READ_APIS), {proto.CONVERSATION_API, proto.HISTORY_API,
                                                proto.UNREAD_API})
        self.assertEqual(proto.WRITE_API_SET, {proto.SEND_API, proto.MARK_READ_API})
        self.assertFalse(set(proto.READ_APIS) & set(proto.WRITE_API_SET))
        self.assertEqual(proto.ALL_APIS,
                         set(proto.READ_APIS) | set(proto.WRITE_API_SET))

    def test_assert_write_only_accepts_registered_write_apis(self):
        """只读接口不能被当写接口放行（`ALL_APIS - READ_APIS` 那种写法会漏）。"""
        self.assertEqual(proto.assert_write(proto.SEND_API), proto.SEND_API)
        for api in proto.READ_APIS:
            with self.assertRaises(proto.UnknownApi):
                proto.assert_write(api)

    def test_assert_read_rejects_write_apis(self):
        for api in proto.WRITE_API_SET:
            with self.assertRaises(proto.UnknownApi):
                proto.assert_read(api)

    def test_actions_are_the_four_writes(self):
        self.assertEqual(set(proto.ACTIONS), {'send_text', 'send_image', 'urge', 'mark_read'})

    def test_conversation_request_sends_all_three_ids(self):
        api, data, version = proto.conversation_request('o-1', '', 'd-1')
        self.assertEqual(api, proto.CONVERSATION_API)
        self.assertEqual(data, {'orderId': 'o-1', 'refundId': '', 'disputeId': 'd-1'})
        self.assertEqual(version, '1.0')

    def test_conversation_request_needs_at_least_one_id(self):
        with self.assertRaises(ValueError):
            proto.conversation_request('', '', '')

    def test_history_request_carries_the_cursor(self):
        api, data, version = proto.history_request(CID, '1700000000123')
        self.assertEqual(api, proto.HISTORY_API)
        self.assertEqual(data, {'conversationId': CID, 'beginTime': '1700000000123'})
        self.assertEqual(version, '1.0')

    def test_history_request_first_page_has_empty_cursor(self):
        _, data, _ = proto.history_request(CID)
        self.assertEqual(data['beginTime'], '')

    def test_unread_request_shape(self):
        api, data, version = proto.unread_request(CID)
        self.assertEqual(api, proto.UNREAD_API)
        self.assertEqual(data, {'conversationId': CID})
        self.assertEqual(version, '1.0')

    def test_conversation_id_is_required_and_trimmed(self):
        _, data, _ = proto.history_request('  cid-x  ')
        self.assertEqual(data['conversationId'], 'cid-x')
        with self.assertRaises(ValueError):
            proto.history_request('   ')

    def test_conversation_id_length_is_capped(self):
        with self.assertRaises(ValueError):
            proto.history_request('x' * (proto.CONVERSATION_ID_MAX + 1))

    def test_send_text_content_is_a_compact_json_string(self):
        """`content` 必须是 JSON **字符串**（官方 `JSON.stringify`，无空格）。"""
        api, data, version = proto.send_text_request(CID, '  你好  ', confirm=True)
        self.assertEqual(api, proto.SEND_API)
        self.assertEqual(data['sceneType'], proto.SCENE_TYPE)
        self.assertEqual(data['contentType'], proto.CONTENT_TEXT)
        self.assertIsInstance(data['content'], str)
        self.assertEqual(data['content'], '{"text":"你好"}')
        self.assertEqual(json.loads(data['content']), {'text': '你好'})
        self.assertEqual(version, '1.0')

    def test_send_text_rejects_empty_and_overlong_text(self):
        with self.assertRaises(ValueError):
            proto.send_text_request(CID, '   ', confirm=True)
        with self.assertRaises(ValueError):
            proto.send_text_request(CID, 'x' * (proto.TEXT_MAX + 1), confirm=True)

    def test_send_image_content_has_the_six_official_keys_and_send_key(self):
        api, data, version = proto.send_image_request(
            CID, 'https://a/y.png', ext='png', width=800, height=600, alt_text='凭证',
            thumb_url='https://a/y-t.png', send_key='sk-1', confirm=True)
        self.assertEqual(api, proto.SEND_API)
        self.assertEqual(data['contentType'], proto.CONTENT_IMAGE)
        self.assertEqual(data['sceneType'], proto.SCENE_TYPE)
        self.assertEqual(json.loads(data['content']), {
            'url': 'https://a/y.png', 'ext': 'PNG', 'altText': '凭证',
            'width': 800, 'height': 600, 'thumbURL': 'https://a/y-t.png'})
        self.assertEqual(data['sendKey'], 'sk-1')
        self.assertEqual(version, '1.0')

    def test_send_image_defaults_follow_the_official_ones(self):
        """官方 `ef.ext || 'JPG'`、尺寸缺省 0、`sendKey` 空串也带。"""
        _, data, _ = proto.send_image_request(CID, 'https://a/y.png', confirm=True)
        body = json.loads(data['content'])
        self.assertEqual(body['ext'], 'JPG')
        self.assertEqual(body['width'], 0)
        self.assertEqual(body['height'], 0)
        self.assertEqual(body['thumbURL'], 'https://a/y.png')
        self.assertIn('sendKey', data)
        self.assertEqual(data['sendKey'], '')

    def test_send_image_needs_a_url(self):
        with self.assertRaises(ValueError):
            proto.send_image_request(CID, '  ', confirm=True)

    def test_urge_uses_the_official_fixed_text(self):
        api, data, _ = proto.urge_request(CID, confirm=True)
        self.assertEqual(api, proto.SEND_API)
        self.assertEqual(json.loads(data['content']), {'text': '催促处理维权'})
        self.assertEqual(data['contentType'], proto.CONTENT_TEXT)

    def test_mark_read_shape(self):
        api, data, version = proto.mark_read_request(CID, 'm-1', 3, confirm=True)
        self.assertEqual(api, proto.MARK_READ_API)
        self.assertEqual(data, {'conversationId': CID, 'msgId': 'm-1', 'sequence': 3})
        self.assertEqual(version, '1.0')

    def test_mark_read_sequence_must_be_an_integer(self):
        with self.assertRaises(ValueError):
            proto.mark_read_request(CID, 'm-1', 'abc', confirm=True)
        with self.assertRaises(ValueError):
            proto.mark_read_request(CID, 'm-1', None, confirm=True)

    def test_mark_read_msg_id_is_required(self):
        with self.assertRaises(ValueError):
            proto.mark_read_request(CID, '   ', 3, confirm=True)

    def test_confirm_must_be_literally_true(self):
        """`'false'` / `'yes'` / `2` 都不算确认（第 38 批那条真缺陷的口径）。"""
        for value in FALSE_LIKE:
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.send_text_request(CID, '你好', confirm=value)
            with self.assertRaises(proto.WriteNotConfirmed):
                proto.mark_read_request(CID, 'm-1', 3, confirm=value)

    def test_request_of_dispatches_by_action(self):
        api, data, _ = proto.request_of('send_text', conversation_id=CID, text='你好',
                                        confirm=True)
        self.assertEqual(api, proto.SEND_API)
        self.assertEqual(data['contentType'], proto.CONTENT_TEXT)
        api, data, _ = proto.request_of('mark_read', conversation_id=CID, msg_id='m-1',
                                        sequence=3, confirm=True)
        self.assertEqual(api, proto.MARK_READ_API)
        self.assertEqual(data['sequence'], 3)

    def test_request_of_rejects_unknown_action(self):
        with self.assertRaises(proto.UnknownApi):
            proto.request_of('nope', confirm=True)

    # -- 解析 -------------------------------------------------------------

    def test_parse_conversation_reads_the_official_key(self):
        parsed = proto.parse_conversation(CONV_OK)
        self.assertEqual(parsed['conversation_id'], CID)
        self.assertEqual(parsed['conversation_id_key'], 'conversationId')
        self.assertTrue(parsed['found'])

    def test_parse_conversation_reports_not_found(self):
        parsed = proto.parse_conversation(CONV_NO_ID)
        self.assertIsNone(parsed['conversation_id'])
        self.assertFalse(parsed['found'])

    def test_parse_history_handles_string_content(self):
        parsed = proto.parse_history(HIST_OK)
        first = parsed['messages'][0]
        self.assertEqual(first['msg_id'], 'm-1')
        self.assertEqual(first['sequence'], 3)
        self.assertEqual(first['sender_role'], 'USER')
        self.assertEqual(first['text'], '你好，麻烦看下')
        self.assertTrue(first['content_parsed'])
        self.assertIsNone(first['image'])
        self.assertEqual(parsed['current_time'], '1700000000123')
        self.assertEqual(parsed['count'], 2)

    def test_parse_history_handles_dict_content(self):
        second = proto.parse_history(HIST_OK)['messages'][1]
        self.assertIsNone(second['text'])
        self.assertFalse(second['content_parsed'])
        self.assertEqual(second['image']['url'], 'https://a/y.png')
        self.assertEqual(second['image']['ext'], 'PNG')
        self.assertEqual(second['image']['width'], 800)
        self.assertEqual(second['image']['thumb_url'], 'https://a/y-thumb.png')

    def test_parse_history_never_invents_missing_fields(self):
        """平台没给的字段一律 `None`，绝不补 0 / 空串。"""
        parsed = proto.parse_history({'success': True, 'data': {'messageList': [
            {'msgId': 'm-x', 'content': '{"text":"hi"}'}]}})
        message = parsed['messages'][0]
        self.assertIsNone(message['sequence'])
        self.assertIsNone(message['sender_role'])
        self.assertIsNone(message['sender_name'])
        self.assertIsNone(message['gmt_create'])
        self.assertIsNone(message['tips'])
        self.assertIsNone(message['content_type'])
        self.assertIsNone(parsed['current_time'])

    def test_parse_history_has_more_rules(self):
        """官方：`length < 20 || messageList[0].header.sequence === 1` 就是到底了。"""
        short = proto.parse_history(HIST_OK)
        self.assertFalse(short['has_more'])
        full = proto.parse_history({'success': True, 'data': {'messageList': [
            {'msgId': f'm-{i}', 'header': {'sequence': i + 5}, 'content': '{"text":"x"}'}
            for i in range(proto.PAGE_SIZE)]}})
        self.assertTrue(full['has_more'])
        first_page = proto.parse_history({'success': True, 'data': {'messageList': [
            {'msgId': f'm-{i}', 'header': {'sequence': i + 1}, 'content': '{"text":"x"}'}
            for i in range(proto.PAGE_SIZE)]}})
        self.assertFalse(first_page['has_more'])

    def test_parse_history_reports_the_missing_list_key(self):
        parsed = proto.parse_history(HIST_NO_KEY)
        self.assertEqual(parsed['messages'], [])
        self.assertIsNone(parsed['list_key'])

    def test_parse_unread_reads_result_list(self):
        parsed = proto.parse_unread(UNREAD_OK)
        self.assertEqual(parsed['count'], 1)
        self.assertEqual(parsed['list_key'], 'result')
        self.assertEqual(parsed['messages'][0]['msg_id'], 'm-1')

    def test_parse_unread_reports_the_missing_list_key(self):
        self.assertIsNone(proto.parse_unread(UNREAD_NO_KEY)['list_key'])

    def test_parse_send_reads_the_receipt(self):
        parsed = proto.parse_send(SEND_OK)
        self.assertTrue(parsed['saved'])
        self.assertEqual(parsed['msg_id'], 'm-9')
        self.assertEqual(parsed['sequence'], 9)
        self.assertEqual(parsed['tips'], '小二已收到')
        self.assertIsNone(parsed['platform_message'])

    def test_parse_send_keeps_the_platform_words_on_business_failure(self):
        parsed = proto.parse_send(SEND_BIZ_FAIL)
        self.assertFalse(parsed['saved'])
        self.assertEqual(parsed['platform_message'], '会话已关闭')

    def test_parse_send_detects_the_client_envelope_failure(self):
        parsed = proto.parse_send(NO_PERMISSION)
        self.assertFalse(parsed['saved'])
        self.assertIn('无权限访问', parsed['platform_message'])


# --------------------------------------------------------------------------- 接口层


class DisputesPageTest(unittest.TestCase):
    """接口层基类：临时数据目录 + 自挂路由 + 假协议层。"""

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
        app.include_router(disputes_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('dp','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}
        seed_account()

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def get(self, path: str, headers: Optional[Dict[str, str]] = None,
            params: Optional[Dict[str, Any]] = None):
        return self.client.get(path, params=params,
                               headers=self.headers if headers is None else headers)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None):
        return self.client.post(path, json=body or {},
                                headers=self.headers if headers is None else headers)

    def login_state(self, account_id: str = ACC) -> str:
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (account_id,))
        return row['login_state'] if row else ''

    def operations(self) -> List[Dict[str, Any]]:
        return db.query('SELECT action, success, detail FROM operation_logs '
                        'WHERE account_id = ? ORDER BY id', (ACC,))

    def topics(self, before: int) -> List[str]:
        from backend.app.core import events
        return [event['topic'] for event in events.recent(limit=500)[before:]]

    def event_count(self) -> int:
        from backend.app.core import events
        return len(events.recent(limit=500))


class DisputesReadTest(DisputesPageTest):
    """只读三条：会话、留言、未读；三态（null / [] / 有数据）必须分开。"""

    def test_conversation_returns_the_id(self):
        resp = self.get(f'{BASE}/{ACC}/conversation',
                        params={'order_id': 'o-1', 'dispute_id': 'd-1'})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['conversation_id'], CID)
        self.assertTrue(data['found'])
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        self.assertEqual(self.fake.call_of(proto.CONVERSATION_API)['data'],
                         {'orderId': 'o-1', 'refundId': '', 'disputeId': 'd-1'})

    def test_conversation_without_any_id_is_400(self):
        resp = self.get(f'{BASE}/{ACC}/conversation')
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_conversation_without_platform_id_is_502(self):
        """平台没给 conversationId ⇒ 502（拿不到 id 后面每一步都做不了，不能假装成功）。"""
        self.fake.payloads[proto.CONVERSATION_API] = CONV_NO_ID
        resp = self.get(f'{BASE}/{ACC}/conversation', params={'order_id': 'o-1'})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertEqual(resp.json()['code'], 'protocol_error')

    def test_messages_return_both_content_shapes(self):
        resp = self.get(f'{BASE}/{ACC}/{CID}/messages')
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['conversation_id'], CID)
        self.assertEqual(data['current_time'], '1700000000123')
        self.assertIs(data['has_more'], False)
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['messages'][0]['text'], '你好，麻烦看下')
        self.assertEqual(data['messages'][1]['image']['url'], 'https://a/y.png')
        self.assertEqual(self.fake.call_of(proto.HISTORY_API)['data'],
                         {'conversationId': CID, 'beginTime': ''})

    def test_messages_passes_the_cursor_through(self):
        self.get(f'{BASE}/{ACC}/{CID}/messages', params={'begin_time': '1700000000123'})
        self.assertEqual(self.fake.call_of(proto.HISTORY_API)['data']['beginTime'],
                         '1700000000123')

    def test_messages_empty_list_is_not_the_same_as_missing(self):
        """`[]` 是「真的一条都没有」——不该带 warning。"""
        self.fake.payloads[proto.HISTORY_API] = HIST_EMPTY
        data = self.get(f'{BASE}/{ACC}/{CID}/messages').json()['data']
        self.assertEqual(data['messages'], [])
        self.assertEqual(data['warnings'], [])

    def test_messages_missing_key_is_null_with_warning(self):
        """平台没给列表键 ⇒ `null` + warning（补成 `[]` 就是假数据）。"""
        self.fake.payloads[proto.HISTORY_API] = HIST_NO_KEY
        data = self.get(f'{BASE}/{ACC}/{CID}/messages').json()['data']
        self.assertIsNone(data['messages'])
        self.assertIsNone(data['has_more'])
        codes = [w['code'] for w in data['warnings']]
        self.assertIn('dispute_messages_missing', codes)
        self.assertIn('dispute_current_time_missing', codes)

    def test_unread_returns_the_list(self):
        data = self.get(f'{BASE}/{ACC}/{CID}/unread').json()['data']
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['messages'][0]['msg_id'], 'm-1')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(self.fake.call_of(proto.UNREAD_API)['data'], {'conversationId': CID})

    def test_unread_empty_list_has_no_warning(self):
        self.fake.payloads[proto.UNREAD_API] = UNREAD_EMPTY
        data = self.get(f'{BASE}/{ACC}/{CID}/unread').json()['data']
        self.assertEqual(data['messages'], [])
        self.assertEqual(data['warnings'], [])

    def test_unread_missing_key_is_null_with_warning(self):
        self.fake.payloads[proto.UNREAD_API] = UNREAD_NO_KEY
        data = self.get(f'{BASE}/{ACC}/{CID}/unread').json()['data']
        self.assertIsNone(data['messages'])
        self.assertEqual([w['code'] for w in data['warnings']], ['dispute_unread_missing'])

    def test_read_platform_refusal_is_502_and_does_not_touch_login_state(self):
        self.fake.payloads[proto.HISTORY_API] = NO_PERMISSION
        resp = self.get(f'{BASE}/{ACC}/{CID}/messages')
        self.assertEqual(resp.status_code, 502, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('无权限访问', body['message'])
        self.assertEqual(body['platform_errors']['api'], proto.HISTORY_API)
        self.assertEqual(self.login_state(), 'active')

    def test_session_expiry_is_409_and_marks_need_relogin(self):
        self.fake.errors[proto.HISTORY_API] = SessionExpiredError('登录态已过期')
        resp = self.get(f'{BASE}/{ACC}/{CID}/messages')
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')

    def test_unknown_account_is_404_with_zero_platform_calls(self):
        resp = self.get(f'{BASE}/nope/{CID}/messages')
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_no_cookie_is_409_with_zero_platform_calls(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.get(f'{BASE}/{ACC}/{CID}/messages')
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_missing_client_is_503(self):
        install_fake_protocol(self.fake, factory=lambda account_id: (None, None))
        resp = self.get(f'{BASE}/{ACC}/{CID}/messages')
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_requires_authentication(self):
        resp = self.get(f'{BASE}/{ACC}/{CID}/messages', headers={})
        self.assertEqual(resp.status_code, 401, resp.text)


class DisputesConfirmGateTest(DisputesPageTest):
    """写闸门：未确认时**一个平台请求都不许发**，且 `'false'` 不算确认。"""

    def test_writes_need_confirmation(self):
        for method, path, body, _api in WRITE_ROUTES:
            resp = self.post(path, body)
            self.assertEqual(resp.status_code, 400, f'{path}: {resp.text}')
            self.assertEqual(resp.json()['code'], 'confirmation_required', path)
        self.assertEqual(self.fake.calls, [])

    def test_false_like_values_are_not_confirmation(self):
        for value in FALSE_LIKE:
            for _method, path, body, _api in WRITE_ROUTES:
                payload = dict(body, confirm=value)
                resp = self.post(path, payload)
                self.assertEqual(resp.status_code, 400, f'{path} confirm={value!r}: {resp.text}')
                self.assertEqual(resp.json()['code'], 'confirmation_required',
                                 f'{path} confirm={value!r}')
        self.assertEqual(self.fake.calls, [])

    def test_confirmation_happens_before_field_validation(self):
        """未确认时连「内容为空」都不该报 —— 否则会误导用户「填对了就能发」。"""
        resp = self.post(f'{BASE}/{ACC}/{CID}/messages', {'text': '', 'confirm': 'false'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')
        self.assertEqual(self.fake.calls, [])

    def test_confirmation_happens_before_the_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(f'{BASE}/{ACC}/{CID}/urge', {'confirm': 'false'})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'confirmation_required')


class DisputesValidationTest(DisputesPageTest):
    """字段校验在建客户端**之前**：掉线时也该先说「内容不能为空」。"""

    def test_field_validation_runs_before_the_credential_check(self):
        seed_account(cookie='', login_state='need_relogin')
        install_fake_protocol(self.fake, with_factory=False)
        resp = self.post(f'{BASE}/{ACC}/{CID}/messages', {'text': '', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_empty_text_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/{CID}/messages', {'text': '   ', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_overlong_text_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/{CID}/messages',
                         {'text': 'x' * (proto.TEXT_MAX + 1), 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_missing_image_url_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/{CID}/messages/image', {'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_bad_sequence_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/{CID}/mark-read',
                         {'msg_id': 'm-1', 'sequence': 'abc', 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    def test_missing_msg_id_is_400(self):
        resp = self.post(f'{BASE}/{ACC}/{CID}/mark-read',
                         {'sequence': 3, 'confirm': True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_empty_conversation_id_in_path_is_404_not_a_platform_call(self):
        resp = self.post(f'{BASE}/{ACC}/%20/messages', {'text': '你好', 'confirm': True})
        self.assertIn(resp.status_code, (400, 404, 405), resp.text)
        self.assertEqual(self.fake.calls, [])


class DisputesWriteHappyPathTest(DisputesPageTest):
    """写成功：请求形状、回执、审计、事件。"""

    def test_send_text_posts_the_official_shape(self):
        before = self.event_count()
        data = self.post(f'{BASE}/{ACC}/{CID}/messages',
                         {'text': '  你好  ', 'confirm': True}).json()['data']
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'send_text')
        self.assertEqual(data['msg_id'], 'm-9')
        self.assertEqual(data['sequence'], 9)
        self.assertEqual(data['tips'], '小二已收到')
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        call = self.fake.call_of(proto.SEND_API)
        self.assertEqual(call['v'], '1.0')
        self.assertEqual(call['data']['conversationId'], CID)
        self.assertEqual(call['data']['sceneType'], 'userActive')
        self.assertEqual(call['data']['contentType'], 'PLAIN_TEXT')
        self.assertEqual(call['data']['content'], '{"text":"你好"}')
        self.assertIn('disputes.changed', self.topics(before))

    def test_send_text_is_logged(self):
        self.post(f'{BASE}/{ACC}/{CID}/messages', {'text': '你好', 'confirm': True})
        rows = self.operations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], 'dispute_send_text')
        self.assertEqual(rows[0]['success'], 1)

    def test_send_image_posts_the_official_shape(self):
        data = self.post(f'{BASE}/{ACC}/{CID}/messages/image',
                         {'url': 'https://a/y.png', 'ext': 'png', 'width': 800,
                          'height': 600, 'alt_text': '凭证', 'confirm': True}).json()['data']
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'send_image')
        call = self.fake.call_of(proto.SEND_API)
        self.assertEqual(call['data']['contentType'], 'IMAGE')
        self.assertEqual(json.loads(call['data']['content']),
                         {'url': 'https://a/y.png', 'ext': 'PNG', 'altText': '凭证',
                          'width': 800, 'height': 600, 'thumbURL': 'https://a/y.png'})
        self.assertIn('sendKey', call['data'])

    def test_urge_sends_the_fixed_text(self):
        data = self.post(f'{BASE}/{ACC}/{CID}/urge', {'confirm': True}).json()['data']
        self.assertEqual(data['action'], 'urge')
        call = self.fake.call_of(proto.SEND_API)
        self.assertEqual(json.loads(call['data']['content']), {'text': '催促处理维权'})

    def test_mark_read_posts_the_official_shape(self):
        data = self.post(f'{BASE}/{ACC}/{CID}/mark-read',
                         {'msg_id': 'm-1', 'sequence': 3, 'confirm': True}).json()['data']
        self.assertTrue(data['saved'])
        self.assertEqual(data['action'], 'mark_read')
        call = self.fake.call_of(proto.MARK_READ_API)
        self.assertEqual(call['data'], {'conversationId': CID, 'msgId': 'm-1', 'sequence': 3})

    def test_no_receipt_gives_an_informational_warning(self):
        """平台接受了但没回 msgId/sequence ⇒ 不是失败，但要说清楚。"""
        self.fake.payloads[proto.SEND_API] = SEND_NO_ECHO
        data = self.post(f'{BASE}/{ACC}/{CID}/messages',
                         {'text': '你好', 'confirm': True}).json()['data']
        self.assertTrue(data['saved'])
        self.assertIsNone(data['msg_id'])
        self.assertIsNone(data['sequence'])
        self.assertEqual([w['code'] for w in data['warnings']], ['dispute_no_echo'])


class DisputesWriteFailureTest(DisputesPageTest):
    """写失败：业务拒绝、平台拒绝、掉线、账号不存在。"""

    def test_business_refusal_is_502_with_the_platform_words(self):
        self.fake.payloads[proto.SEND_API] = SEND_BIZ_FAIL
        resp = self.post(f'{BASE}/{ACC}/{CID}/messages', {'text': '你好', 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        body = resp.json()
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('会话已关闭', body['message'])
        self.assertEqual(self.operations(), [])

    def test_client_envelope_refusal_is_502_and_keeps_login_state(self):
        self.fake.payloads[proto.MARK_READ_API] = NO_PERMISSION
        resp = self.post(f'{BASE}/{ACC}/{CID}/mark-read',
                         {'msg_id': 'm-1', 'sequence': 3, 'confirm': True})
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn('无权限访问', resp.json()['message'])
        self.assertEqual(self.login_state(), 'active')

    def test_session_expiry_is_409_on_writes_too(self):
        self.fake.errors[proto.SEND_API] = SessionExpiredError('登录态已过期')
        resp = self.post(f'{BASE}/{ACC}/{CID}/urge', {'confirm': True})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()['code'], 'verification_required')
        self.assertEqual(self.login_state(), 'need_relogin')
        self.assertEqual(self.operations(), [])

    def test_unknown_account_is_404_with_zero_platform_calls(self):
        resp = self.post(f'{BASE}/nope/{CID}/urge', {'confirm': True})
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(self.fake.calls, [])


class DisputesRoutesTest(DisputesPageTest):
    """路由集合：七条（path, method）一个不多一个不少，且没有写接口挂在 GET 上。"""

    def test_route_set_matches_the_frozen_contract(self):
        from backend.app.api.disputes import router
        found = {(method, route.path) for route in router.routes
                 for method in getattr(route, 'methods', set())}
        self.assertEqual(found, EXPECTED_ROUTES)

    def test_get_on_a_write_path_is_405(self):
        resp = self.get(f'{BASE}/{ACC}/{CID}/urge')
        self.assertEqual(resp.status_code, 405, resp.text)
        self.assertEqual(self.fake.calls, [])

    def test_openapi_has_seven_operations(self):
        from fastapi import FastAPI as _FastAPI
        app = _FastAPI()
        app.include_router(disputes_api.router)
        paths = app.openapi()['paths']
        operations = sum(len(item) for item in paths.values())
        self.assertEqual(operations, 7)
        self.assertEqual(len(paths), 6)  # `messages` 的 GET 与 POST 共用一条 path


if __name__ == '__main__':
    unittest.main(verbosity=2)
