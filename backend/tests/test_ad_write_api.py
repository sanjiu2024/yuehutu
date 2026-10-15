"""超级擦亮写接口：假协议层单测。

**一个真实写请求都不发** —— 协议层被整个换成 ``FakeWriteClient``（记录 api / data / kwargs，
按 api 回话或抛异常）。验收点是「接口名对不对、参数形状对不对、没确认会不会真的不发」，
不是「平台答不答应」。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import ad_write as ad_write_api
from backend.app.core import db, security
from backend.app.services import ad_write as ad_write_service
from backend.tests.testenv import use_temp_app_dir
from backend.app.xianyu import ad_write as protocol

ACC = '2217970177348'
BASE = '/api/promotion/write'


from backend.app.main import IdempotencyKeyMiddleware
class XianyuError(Exception):
    """与协议层同名：``common._is_session_expired`` 按**类名**认登录态过期，不看文案。"""


class FakeWriteClient:
    """假协议层：只实现 ``mtop``，把每次调用的 api / data / kwargs 记下来。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, **kwargs: Any) -> Any:
        self.calls.append((api, data, kwargs))
        error = self.errors.get(api)
        if error is not None:
            raise error
        if api in self.payloads:
            return {'data': self.payloads[api]}
        return {'data': None}  # 平台没返回 -> None，服务层不许补 0

    def api_names(self) -> List[str]:
        return [call[0] for call in self.calls]


def install_fake_protocol(client: FakeWriteClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')
    module.AD_SPM = 'a21e6z'  # type: ignore[attr-defined]

    def factory(account_id: str) -> FakeWriteClient:
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


class AdWriteTestBase(unittest.TestCase):
    """假协议层 + 临时数据目录 + 自挂 router 的脚手架。"""

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
        self.fake = FakeWriteClient()
        install_fake_protocol(self.fake)
        app = FastAPI()
        app.add_middleware(IdempotencyKeyMiddleware)
        app.include_router(ad_write_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('adwrite','x',1)"))
        self.headers = {'Authorization': 'Bearer ' + security.issue_token(self.user_id)}

    def tearDown(self):
        db.execute('DELETE FROM users')
        self.app_ctx.__exit__(None, None, None)

    # -- 小工具 ----------------------------------------------------------

    def post(self, action: str, body: Any, account_id: str = ACC):
        return self.client.post(f'{BASE}/{account_id}/{action}', json=body, headers=self.headers)

    def ok(self, action: str, payload: Any = None, account_id: str = ACC) -> Dict[str, Any]:
        resp = self.post(action, {'confirm': True, 'payload': payload}, account_id)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get('success'), body)
        return body['data']

    def fail(self, action: str, body: Any, account_id: str = ACC, status: int = 400) -> Dict[str, Any]:
        resp = self.post(action, body, account_id)
        self.assertEqual(resp.status_code, status, resp.text)
        payload = resp.json()
        self.assertFalse(payload.get('success'), payload)
        return payload

    def sent(self) -> tuple:
        self.assertEqual(len(self.fake.calls), 1,
                         f'期望恰好一次协议调用，实际 {self.fake.api_names()}')
        return self.fake.calls[0]

    def request_json(self, call: tuple) -> Any:
        self.assertIn('requestJson', call[1], f'data 里没有 requestJson: {call[1]}')
        return json.loads(call[1]['requestJson'])


class AdWriteCatalogTest(AdWriteTestBase):
    def test_catalog_lists_supported_actions(self):
        resp = self.client.get(f'{BASE}/actions', headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()['data']
        names = [item['action'] for item in data['actions']]
        self.assertEqual(sorted(names), sorted(protocol.WRITE_APIS))
        self.assertIn('campaign_edit', names)
        self.assertIn('account_log_operates', names)
        for item in data['actions']:
            self.assertEqual(item['api'], protocol.WRITE_APIS[item['action']]['api'])
            self.assertTrue(item['confirm_required'])

    def test_catalog_marks_recharge_and_withdraw_unsupported(self):
        data = self.client.get(f'{BASE}/actions', headers=self.headers).json()['data']
        unsupported = {item['action']: item['reason'] for item in data['unsupported']}
        self.assertEqual(set(unsupported), {'recharge', 'withdraw'})
        # 说清「为什么不做」，而不是含糊的 TODO
        self.assertIn('recharge.query', unsupported['recharge'])
        self.assertIn('withdraw.query', unsupported['withdraw'])
        self.assertIn('闲鱼 App', unsupported['withdraw'])


class AdWriteConfirmTest(AdWriteTestBase):
    def test_missing_confirm_is_refused_without_any_protocol_call(self):
        seed_account()
        for body in ({}, {'payload': {'campaignId': 1}}, {'confirm': False},
                     {'confirm': 1}, {'confirm': 'true'}):
            payload = self.fail('campaign_edit', body)
            self.assertEqual(payload['code'], 'confirm_required', payload)
        self.assertEqual(self.fake.calls, [], '未确认的写操作不许发出任何协议调用')

    def test_confirm_is_checked_before_account_lookup(self):
        # 账号根本不存在，但没确认时先报 confirm_required（不泄漏账号是否存在）
        payload = self.fail('campaign_edit', {'confirm': False}, account_id='999')
        self.assertEqual(payload['code'], 'confirm_required', payload)
        self.assertEqual(self.fake.calls, [])

    def test_protocol_layer_refuses_unconfirmed_directly(self):
        import asyncio
        with self.assertRaises(protocol.WriteNotConfirmed):
            asyncio.run(protocol.campaign_edit(self.fake, {'campaignId': 1}, confirm=False))
        with self.assertRaises(protocol.WriteNotConfirmed):
            asyncio.run(protocol.campaign_start_before(self.fake, confirm=False))
        self.assertEqual(self.fake.calls, [])


class AdWriteProtocolShapeTest(AdWriteTestBase):
    """接口名与参数形状：对着 bundle 逐个核对。"""

    def test_campaign_edit_sends_request_json(self):
        seed_account()
        data = self.ok('campaign_edit', {'campaignId': 12345, 'title': '新标题'})
        api, body, kwargs = self.sent()
        self.assertEqual(api, 'mtop.taobao.idle.ad.campaign.edit')
        self.assertEqual(self.request_json((api, body, kwargs)),
                         {'campaignId': 12345, 'title': '新标题'})
        self.assertEqual(kwargs.get('v'), '1.0')
        self.assertEqual(data['api'], 'mtop.taobao.idle.ad.campaign.edit')
        self.assertTrue(data['confirmed'])
        self.assertEqual(data['source'], 'platform')

    def test_every_campaign_action_hits_its_own_api(self):
        seed_account()
        for action, api in (('campaign_delete', 'mtop.taobao.idle.ad.campaign.delete'),
                            ('campaign_batch_delete', 'mtop.taobao.idle.ad.campaign.batch.delete'),
                            ('campaign_pause', 'mtop.taobao.idle.ad.campaign.pause'),
                            ('campaign_start', 'mtop.taobao.idle.ad.campaign.startV2'),
                            ('item_campaign_renew', 'mtop.taobao.idle.ad.item.campaign.renew')):
            self.fake.calls.clear()
            self.ok(action, {'campaignId': 7})
            self.assertEqual(self.fake.api_names(), [api], action)
            self.assertEqual(self.request_json(self.fake.calls[0]), {'campaignId': 7}, action)

    def test_start_before_sends_empty_data_without_request_json(self):
        seed_account()
        self.ok('campaign_start_before')
        api, body, kwargs = self.sent()
        self.assertEqual(api, 'mtop.taobao.idle.ad.campaign.start.before')
        self.assertEqual(body, {})
        self.assertNotIn('requestJson', body)
        self.assertEqual(kwargs.get('v'), '1.0')

    def test_activity_and_account_actions(self):
        seed_account()
        for action, api in (('activity_apply', 'mtop.taobao.idle.ad.activity.apply'),
                            ('activity_task_execute', 'mtop.taobao.idle.ad.activity.task.execute'),
                            ('account_log_operates', 'mtop.taobao.idle.ad.bp.account.log.operates')):
            self.fake.calls.clear()
            self.ok(action, {'taskId': 3})
            self.assertEqual(self.fake.api_names(), [api], action)
            self.assertEqual(self.request_json(self.fake.calls[0]), {'taskId': 3}, action)

    def test_request_json_is_compact_unicode_not_escaped(self):
        seed_account()
        self.ok('campaign_edit', {'title': '擦亮'})
        _, body, _ = self.sent()
        self.assertEqual(body['requestJson'], '{"title":"擦亮"}')


class AdWriteResultTest(AdWriteTestBase):
    def test_platform_null_stays_null_and_is_not_filled_with_zero(self):
        seed_account()
        data = self.ok('campaign_edit', {'campaignId': 1})
        self.assertIsNone(data['result'], '平台没返回就必须是 null，不许补 0')

    def test_platform_payload_is_passed_through_untouched(self):
        seed_account()
        self.fake.payloads['mtop.taobao.idle.ad.campaign.edit'] = {
            'campaignId': 1, 'budget': None, 'balanceOfYuan': '33.58'}
        data = self.ok('campaign_edit', {'campaignId': 1})
        self.assertEqual(data['result'], {'campaignId': 1, 'budget': None, 'balanceOfYuan': '33.58'})

    def test_unsupported_funds_actions_refuse_without_any_call(self):
        seed_account()
        for action in ('recharge', 'withdraw'):
            payload = self.fail(action, {'confirm': True, 'payload': {'amount': 100}})
            self.assertEqual(payload['code'], 'unsupported', payload)
            self.assertIn('闲鱼 App', payload['message'])
        self.assertEqual(self.fake.calls, [], '充值/提现绝不许发出任何协议调用')

    def test_unknown_action_is_not_found(self):
        seed_account()
        payload = self.fail('campaign_explode', {'confirm': True}, status=404)
        self.assertEqual(payload['code'], 'not_found', payload)
        self.assertEqual(self.fake.calls, [])

    def test_missing_account_is_not_found(self):
        payload = self.fail('campaign_edit', {'confirm': True}, account_id='404', status=404)
        self.assertEqual(payload['code'], 'not_found', payload)
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_needs_verification(self):
        db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
                   'VALUES(?,?,?,?)', (ACC, 'x', 1, 1))
        payload = self.fail('campaign_edit', {'confirm': True}, status=409)
        self.assertEqual(payload['code'], 'verification_required', payload)
        self.assertEqual(payload['login_state'], 'need_relogin')
        self.assertEqual(self.fake.calls, [])


class AdWriteFailureTest(AdWriteTestBase):
    def test_platform_refusal_is_protocol_error_and_not_logout(self):
        seed_account()
        self.fake.errors['mtop.taobao.idle.ad.campaign.edit'] = XianyuError(
            'FAIL_SYS_NO_PERMISSION::无权限操作')
        payload = self.fail('campaign_edit', {'confirm': True}, status=502)
        self.assertEqual(payload['code'], 'protocol_error', payload)
        self.assertIn('FAIL_SYS_NO_PERMISSION', payload['message'])
        self.assertNotIn('login_state', payload, '无权限 ≠ 掉线，不许写 need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'active')

    def test_session_expired_marks_account_need_relogin(self):
        seed_account()
        self.fake.errors['mtop.taobao.idle.ad.campaign.pause'] = XianyuError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        payload = self.fail('campaign_pause', {'confirm': True}, status=409)
        self.assertEqual(payload['code'], 'verification_required', payload)
        self.assertEqual(payload['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')


class AdWriteServiceTest(AdWriteTestBase):
    """服务层显式函数：不走 HTTP 也要有 confirm。"""

    def test_service_functions_require_confirm(self):
        seed_account()
        payload = ad_write_service.campaign_pause(ACC, {'campaignId': 1})
        self.assertEqual(payload['code'], 'confirm_required', payload)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(ad_write_service.campaign_start_before(ACC)['code'], 'confirm_required')

    def test_service_function_confirmed_returns_api_name(self):
        seed_account()
        data = ad_write_service.activity_apply(ACC, {'activityId': 9}, confirm=True)
        self.assertEqual(data['api'], 'mtop.taobao.idle.ad.activity.apply')
        self.assertEqual(self.fake.api_names(), ['mtop.taobao.idle.ad.activity.apply'])

    def test_service_refuses_funds_actions(self):
        seed_account()
        self.assertEqual(ad_write_service.execute(ACC, 'recharge', {}, True)['code'], 'unsupported')
        self.assertEqual(ad_write_service.execute(ACC, 'withdraw', {}, True)['code'], 'unsupported')
        self.assertEqual(self.fake.calls, [])




class AdWriteIdempotencyTest(AdWriteTestBase):
    """第 63 批：推广写族接上幂等键（Idempotency-Key，CONTRACTS 9）。"""

    def test_same_key_and_payload_hits_the_platform_once(self):
        seed_account()
        path = f"{BASE}/{ACC}/campaign_edit"
        body = {"confirm": True, "payload": {"campaignId": 12345, "title": "新标题"}}
        headers = dict(self.headers, **{"Idempotency-Key": "adw-idem-1"})

        first = self.client.post(path, json=body, headers=headers)
        second = self.client.post(path, json=body, headers=headers)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.fake.calls), 1, "同键同载荷只许打平台一次")
        self.assertNotIn("idempotent_replay", first.json()["data"], first.text)
        self.assertTrue(second.json()["data"]["idempotent_replay"], second.text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
