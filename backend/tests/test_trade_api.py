# -*- coding: utf-8 -*-
"""交易组只读路由的契约测试（订单 / 退款 / 评价 / 投诉 / 退货地址）。

**为什么单开一组**：这一组接口最容易犯的四个错 ——

1. 把平台**没给**的字段画成 0（订单金额、退款金额、评价条数）；
2. 自己给金额换算单位（真机给的是字符串 `"39.90"`，不是分也不是浮点）；
3. 把 `NO_PERMISSION` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（卖家工作台会话才有的
   权限）当成账号掉线，让用户白扫一次码；
4. 悄悄**写**平台（发货、同意退款、评价、改价），本项目绝对禁止。

**真机样本**：下面 `ORDER_COUNT` / `SOLD_LIST` / `REFUND_LIST` / `RATE_LIST` 的字段
与取值来自 2026-10-10 账号 2217970177348 的只读探针
（`logs/seller_discover/trade_probe.json`），不是编的；两个失败信封同样是真机原文。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_trade_api.py"

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

from backend.app.api import trade as trade_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import trade  # noqa: E402
from backend.app.xianyu import trade as trade_proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'trade_acc'
BASE = '/api/trade'
PATHS = ('orders', 'refunds', 'evaluations', 'complaints', 'addresses')

#: 真机 `mtop.taobao.idle.merchant.order.count`（queryType=ORDER）——字段是
#: `count` + `queryCode`，**不是** bundle 里前端自己的 title/number/key。
ORDER_COUNT = {
    'success': True,
    'data': {'module': {'countInfoList': [
        {'count': 2, 'queryCode': 'TRADE_CLOSED'},
        {'count': 0, 'queryCode': 'SHIPPED'},
        {'count': 0, 'queryCode': 'NOT_PAY'},
        {'count': 15, 'queryCode': 'ALL'},
        {'count': 0, 'queryCode': 'REFUND'},
        {'count': 9, 'queryCode': 'TRADE_SUCCESS'},
        {'count': 1, 'queryCode': 'NOT_SHIP'},
        {'count': 3, 'queryCode': 'SOMETHING_NEW'},
    ]}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 真机 `mtop.taobao.idle.trade.merchant.sold.get`（第一条，交易关闭）
SOLD_LIST = {
    'success': True,
    'data': {'module': {'ext': {}, 'items': [{
        'buyerInfoVO': {
            'address': '山西省大同市平城区新旺街道新旺乡 正德嘉苑2号楼',
            'buyerId': 1817788280, 'isPrivacy': False, 'name': '王圆圆',
            'phone': '13353520828',
            'userIcon': 'http://img.alicdn.com/bao/uploaded/i4/TB2233uw5QnBKNjSZFmXXcApVXa_!!0-mytaobao.jpg',
            'userNick': '水星有趣的珍珠贝',
        },
        'columnVOList': [{
            'contentVOList': [{'key': {'text': '交易关闭'}}, {}],
            'name': '发货/退款状态', 'num': 2,
        }],
        'commonData': {
            'buyerApplyModifyAddress': False, 'consignTipsText': [{'text': '未发货'}],
            'createTime': '2026-10-09 08:29:55', 'finishTime': '2026-10-10 08:30:00',
            'inRefund': False, 'itemId': 1086671365960, 'orderId': 3317095524016013282,
            'orderStatus': '交易关闭', 'sellerRateStatus': 5, 'showDetail': True,
        },
        'itemVO': {
            'itemInfoLines': [],
            'itemPicUrl': 'http://img.alicdn.com/bao/uploaded/i1/O1CN01GTNueXM96pJ2XrKe_!!2217970177348-2-fleamarket.png',
            'title': '指定商品补货提醒｜库存监控｜尺码颜色关注｜到货通知',
        },
        'priceVO': {'auctionPrice': '39.90', 'buyNum': 1, 'confirmFee': '0.00',
                    'discountFee': '0.00', 'postFee': '0.00', 'refundFee': '0.00',
                    'totalPrice': '39.90'},
        'rightVO': {'btnList': [{
            'buttonDisable': False,
            'clickEvent': {'data': {'code': 'DELETE_ORDER'}, 'type': 'actionCode',
                           'utParam': {'arg1': 'DeleteOrder'}},
            'expanded': True, 'ext': {}, 'name': '删除订单', 'style': 'grey',
            'tradeAction': 'DELETE_ORDER',
        }]},
    }]}},
    'lastEndRow': 20, 'nextPage': 2, 'totalCount': 15, 'needDecryptKeys': [],
    'serverTime': 1791605956329,
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 真机 `mtop.taobao.idle.merchant.refund.list` —— 容器是**双层** `data.data`，
#: 分页键在信封顶层。
REFUND_LIST = {
    'success': True,
    'data': {'data': {
        'ext': {'totalRefundFee': '468.90'},
        'items': [{
            'buyerInfoVO': {
                'buyerId': 1817788280,
                'userIcon': 'https://api.goofish.com/m/userAvatar.action?id=1817788280&suffix=_60x60.jpg&needHttps=1',
                'userNick': '水星有趣的珍珠贝',
            },
            'commonData': {
                'createTime': '2026-09-29 22:23:30', 'itemId': '1086671365960',
                'orderId': '3317077308271013282',
                'orderSimpleRemark': '已退款买家¥10.00',
                'orderSimpleRemarkRichText': {'data': [{
                    'content': '已退款买家¥10.00',
                    'style': {'color': '#999999', 'fontSize': 14}, 'type': 'TEXT'}]},
                'orderStatus': '未发货退款', 'refundStatus': 5, 'showDetail': True,
            },
            'itemVO': {
                'itemInfoLines': [{'key': '规格', 'value': '其他服务/1-5天/元/次'}],
                'itemPicUrl': 'http://img.alicdn.com/bao/uploaded/i1/O1CN01GTNueXM96pJ2XrKe_!!2217970177348-2-fleamarket.png',
                'title': '指定商品补货提醒｜库存监控｜尺码颜色关注｜到货通知',
            },
            'priceVO': {'auctionPrice': '39.90', 'buyNum': 1, 'refundFee': '10.00',
                        'refundFeeView': 'alipays://platformapi/startapp?appId=20000003'},
            'refundInfoVO': {
                'csStatus': '不需要客服介入', 'gmtCreate': '2026-10-08 03:41:22',
                'goodsNeeds': 0, 'inSale': True, 'reason': '其它问题/协商一致退款',
                'refundAccept': False, 'refundId': '287567835887788280',
                'refundStatus': '已退款买家¥10.00（待发货）', 'refundType': 1,
            },
            'rightVO': {'btnList': [{
                'clickEvent': {'data': {'url': ''}, 'type': 'url', 'utParam': {}},
                'code': 'contact', 'name': '联系买家', 'style': {'bgColor': 'yellow'},
            }]},
        }],
    }},
    'lastEndRow': 20, 'nextPage': 2, 'totalCount': 4, 'needDecryptKeys': [],
    'serverTime': 1791605956329,
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 真机 `mtop.taobao.idle.merchant.rate.list`：items 是**订单**，评价在
#: `rateItemVOList`；本项目账号这一项目前都是空数组。
RATE_LIST = {
    'success': True,
    'data': {'module': {'ext': {}, 'items': [{
        'merchantBuyerVO': {
            'address': '辽宁省沈阳市沈北新区虎石台街道辽宁经济职业技术学院',
            'buyerId': 2222886507942, 'isPrivacy': False, 'name': '1',
            'phone': '18041364380',
            'userIcon': 'http://gtms03.alicdn.com/tps/i3/TB1LFGeKVXXXXbCaXXX07tlTXXX-200-200.png',
            'userNick': 'x***2',
        },
        'merchantCommonData': {
            'buyerApplyModifyAddress': False, 'consignTime': '2026-10-09 03:19:03',
            'createTime': '2026-10-09 03:01:52', 'finishTime': '2026-10-09 03:22:04',
            'inRefund': False, 'itemId': 1087247101297, 'orderId': 3316918549013103465,
            'orderStatus': '交易成功', 'paySuccessTime': '2026-10-09 03:02:57',
            'sellerRateStatus': 5, 'showDetail': True,
        },
        'merchantItemVO': {
            'itemInfoLines': [],
            'itemPicUrl': 'http://img.alicdn.com/bao/uploaded/i1/O1CN01Sn32Klmnu6C2XrKe_!!2217970177348-2-fleamarket.png',
            'title': '我的世界MC开服一条龙｜服务器部署｜整合包定制｜插件模组配置',
        },
        'rateItemVOList': [],
    }]}},
    'lastEndRow': 20, 'nextPage': 2, 'totalCount': 12,
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 平台字段名取自 bundle 里的读法（`rateItemVOList[]`），**本项目账号还没有评价**，
#: 所以这条不是真机样本，只用来锁归一化器的字段映射。
RATE_WITH_RECORD = {
    'success': True,
    'data': {'module': {'items': [{
        'merchantCommonData': {'orderId': 3316918549013103465, 'itemId': 1087247101297,
                               'orderStatus': '交易成功', 'sellerRateStatus': 6},
        'merchantItemVO': {'title': '专拍', 'itemPicUrl': 'https://img.alicdn.com/x.jpg',
                           'itemInfoLines': []},
        'merchantBuyerVO': {'buyerId': 2222886507942, 'userNick': 'x***2'},
        'rateItemVOList': [{
            'rateId': 123456789, 'rate': '很好', 'main': '卖家很耐心',
            'feedBack': '谢谢支持', 'buyerMainRateLevel': '好评', 'gmtCreate': 1791605956329,
            'illegal': False, 'pictCdnUrlList': ['https://img.alicdn.com/a.jpg'],
            'raterHeadImg': 'https://img.alicdn.com/h.jpg',
            'serviceTags': ['态度好'], 'seller': {'reply': '欢迎再来'},
        }],
    }]}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: `refund.refuse.render`：官方「拒绝原因」弹窗的数据源。
#: **本组四条夹具来自 bundle 静态取证（`idle-seller-trade/0.0.26/js/main.js`），
#: 没有真机样本** —— 字段名逐字来自官方代码，取值是示意值。
REFUSE_RENDER = {
    'success': True,
    'data': {'data': {
        'refuseReasonList': [
            {'refuseReasonId': '20653006', 'reasonId': '20653006', 'reasonName': '买家已使用'},
            {'refuseReasonId': '20653007', 'reasonId': '20653007', 'reasonName': '其他原因'},
            {'refuseReasonId': '20653008', 'reasonId': '20653008', 'reasonName': '与买家协商一致'},
        ],
        'refuseNegotiation': False,
        'refuseProof': True,
        'refundFee': '0.00',
    }},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}
#: 平台回了信封、但里面没有原因列表键（**必须给 null + warning，不能给 []**）
REFUSE_RENDER_NO_LIST = {
    'success': True,
    'data': {'data': {'refuseNegotiation': False, 'refundFee': '0.00'}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: `refund.detail`：官方按 `components[].render` 挑组件渲染 ⇒ 我们原样透传
REFUND_DETAIL = {
    'success': True,
    'data': {'data': {
        'components': [
            {'render': 'nodeStatusInfo', 'nodeStatus': 'REFUND_WAIT_SELLER_AGREE'},
            {'render': 'basicRefundInfo', 'refundId': 'r-1', 'refundFee': '12.80'},
            {'render': 'progressDetail', 'steps': []},
        ],
    }},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}
REFUND_DETAIL_NO_COMPONENTS = {
    'success': True,
    'data': {'data': {'refundId': 'r-1'}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: `postage.refund.detail.query`：响应在 `data.module`，官方读 `existProcessingCompensate`
POSTAGE_DETAIL = {
    'success': True,
    'data': {'module': {'existProcessingCompensate': False, 'refundFee': '8.00',
                        'status': 'WAIT_SELLER_AGREE'}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}
POSTAGE_DETAIL_EMPTY = {
    'success': True,
    'data': {'module': {}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: `postage.refund.refuse.reason.query`：`data.module.reasons` 是**字符串数组**
POSTAGE_REASONS = {
    'success': True,
    'data': {'module': {'reasons': ['邮费金额不对', '买家未协商', '其他原因']}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}
POSTAGE_REASONS_NO_KEY = {
    'success': True,
    'data': {'module': {'defaultReason': '邮费金额不对'}},
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}

#: 四条新路由（都是 GET，且都是**四段以上**路径 ⇒ 单独列出，供只读边界用例遍历）
REFUND_PATHS = (
    'refunds/r-1/refuse-reasons',
    'refunds/r-1/detail',
    'orders/o-1/postage-refund/detail',
    'orders/o-1/postage-refund/refuse-reasons',
)

#: 真机原文：投诉列表没有权限
COMPLAIN_DENIED = {'success': False, 'message': 'NO_PERMISSION::用户没有权限',
                   'ret': ['NO_PERMISSION::用户没有权限']}
#: 真机原文：退货地址没有权限（卖家工作台会话才有的能力）
ADDRESS_DENIED = {'success': False,
                  'message': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问',
                  'ret': ['FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问']}


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期。"""


class FakeTradeClient:
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


def install_fake_protocol(client: FakeTradeClient) -> None:
    module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeTradeClient:
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


def full_client(**overrides: Any) -> FakeTradeClient:
    payloads = {
        trade_proto.ORDER_COUNT_API: ORDER_COUNT,
        trade_proto.ORDER_LIST_API: SOLD_LIST,
        trade_proto.REFUND_LIST_API: REFUND_LIST,
        trade_proto.RATE_LIST_API: RATE_LIST,
        trade_proto.COMPLAIN_LIST_API: COMPLAIN_DENIED,
        trade_proto.ADDRESS_LIST_API: ADDRESS_DENIED,
    }
    payloads.update(overrides)
    return FakeTradeClient(payloads)


class TradePageTest(unittest.TestCase):
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
        app.include_router(trade_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('tradeapi','x',1)"))
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

    def fail(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body


class TradeReadOnlyTest(TradePageTest):
    """只读边界：五个路由都只有 GET。"""

    def test_all_trade_routes_reject_writes(self):
        for name in PATHS:
            path = f'{BASE}/{ACC}/{name}'
            for method in ('post', 'put', 'patch', 'delete'):
                resp = getattr(self.client, method)(path, headers=self.headers)
                self.assertEqual(resp.status_code, 405, f'{method} {path} → {resp.status_code}')

    def test_protocol_layer_refuses_write_apis(self):
        for api in trade_proto.WRITE_APIS:
            with self.assertRaises(trade_proto.ReadOnlyViolation):
                trade_proto.assert_read_only(api)

    def test_requests_require_login(self):
        resp = self.get(f'{BASE}/{ACC}/orders', headers={})
        self.assertIn(resp.status_code, (401, 403), resp.text)


class TradeOrdersTest(TradePageTest):
    """订单：真机形状透传 + 请求参数形状 + 页签计数。"""

    def setUp(self):
        super().setUp()
        seed_account()

    def test_orders_pass_the_real_frame_through(self):
        data = self.ok_data(f'{BASE}/{ACC}/orders')
        self.assertEqual(data['api'], trade_proto.ORDER_LIST_API)
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['page_size'], 10)
        self.assertTrue(data['fetched_at'])
        order = data['orders'][0]
        self.assertEqual(order['order_id'], '3317095524016013282')
        self.assertEqual(order['item_id'], '1086671365960')
        self.assertEqual(order['order_status'], '交易关闭')
        self.assertEqual(order['buyer']['nick'], '水星有趣的珍珠贝')
        self.assertEqual(order['buyer']['name'], '王圆圆')
        self.assertEqual(order['buyer']['user_id'], '1817788280')
        self.assertEqual(order['item']['title'], '指定商品补货提醒｜库存监控｜尺码颜色关注｜到货通知')
        self.assertEqual(order['times']['create_time'], '2026-10-09 08:29:55')
        self.assertEqual(order['times']['finish_time'], '2026-10-10 08:30:00')
        self.assertEqual(order['consign_tips'], ['未发货'])
        self.assertEqual(order['columns'][0]['name'], '发货/退款状态')
        self.assertEqual(order['columns'][0]['values'][0]['text'], '交易关闭')
        self.assertEqual(order['buttons'][0]['name'], '删除订单')
        self.assertEqual(order['buttons'][0]['code'], 'DELETE_ORDER')
        self.assertFalse(order['in_refund'])
        self.assertEqual(order['seller_rate_status'], 5)
        self.assertEqual(data['paging']['total_count'], 15)
        self.assertEqual(data['paging']['next_page'], 2)

    def test_money_stays_the_string_the_platform_sent(self):
        price = self.ok_data(f'{BASE}/{ACC}/orders')['orders'][0]['price']
        self.assertEqual(price['auction_price'], '39.90')
        self.assertEqual(price['total_price'], '39.90')
        self.assertEqual(price['post_fee'], '0.00')
        self.assertNotIsInstance(price['auction_price'], float)
        self.assertEqual(price['buy_num'], 1)

    def test_order_request_shape_matches_the_bundle(self):
        self.ok_data(f'{BASE}/{ACC}/orders?page=2&size=20')
        call = self.fake.call_of(trade_proto.ORDER_LIST_API)
        self.assertEqual(call['data'], {'pageNumber': 2, 'rowsPerPage': 20})
        self.assertEqual(call['v'], '1.0')

    def test_include_counts_costs_one_more_call(self):
        data = self.ok_data(f'{BASE}/{ACC}/orders?include_counts=1')
        self.assertEqual(self.fake.apis(),
                         [trade_proto.ORDER_LIST_API, trade_proto.ORDER_COUNT_API])
        self.assertEqual(self.fake.call_of(trade_proto.ORDER_COUNT_API)['data'],
                         {'queryType': 'ORDER'})
        self.assertEqual(data['counts']['ALL'], 15)
        self.assertEqual(data['counts']['TRADE_SUCCESS'], 9)
        self.assertEqual(data['tabs'][0]['label'], '交易关闭')
        self.assertIsNone(data['tabs'][-1]['label'])

    def test_counts_are_not_requested_by_default(self):
        self.ok_data(f'{BASE}/{ACC}/orders')
        self.assertEqual(self.fake.apis(), [trade_proto.ORDER_LIST_API])

    def test_missing_fields_are_null_and_never_zero(self):
        frame = {'success': True, 'data': {'module': {'items': [{'commonData': {
            'orderId': 1, 'orderStatus': '待发货'}}]}}}
        self.fake.payloads[trade_proto.ORDER_LIST_API] = frame
        order = self.ok_data(f'{BASE}/{ACC}/orders')['orders'][0]
        self.assertEqual(order['order_id'], '1')
        self.assertIsNone(order['price']['auction_price'])
        self.assertIsNone(order['price']['buy_num'])
        self.assertIsNone(order['columns'])
        self.assertIsNone(order['buttons'])
        self.assertIsNone(order['buyer'])
        self.assertIsNone(order['in_refund'])
        self.assertNotEqual(order['price']['auction_price'], 0)


class TradeRefundsTest(TradePageTest):
    """退款：双层容器 `data.data` + 顶层分页 + `ext.totalRefundFee`。"""

    def setUp(self):
        super().setUp()
        seed_account()

    def test_refunds_read_the_double_nested_container(self):
        data = self.ok_data(f'{BASE}/{ACC}/refunds')
        self.assertEqual(data['api'], trade_proto.REFUND_LIST_API)
        refund = data['refunds'][0]
        self.assertEqual(refund['refund_id'], '287567835887788280')
        self.assertEqual(refund['order_id'], '3317077308271013282')
        self.assertEqual(refund['order_status'], '未发货退款')
        self.assertEqual(refund['refund_status'], 5)
        self.assertEqual(refund['refund_status_text'], '已退款买家¥10.00（待发货）')
        self.assertEqual(refund['reason'], '其它问题/协商一致退款')
        self.assertEqual(refund['cs_status'], '不需要客服介入')
        self.assertEqual(refund['remark'], '已退款买家¥10.00')
        self.assertEqual(refund['refund_time'], '2026-10-08 03:41:22')
        self.assertEqual(refund['price']['refund_fee'], '10.00')
        self.assertEqual(refund['item']['info_lines'][0]['key'], '规格')
        self.assertEqual(data['total_refund_fee'], '468.90')
        self.assertEqual(data['paging']['total_count'], 4)
        self.assertFalse(refund['refund_accept'])
        self.assertTrue(refund['in_sale'])

    def test_refund_request_shape_matches_the_bundle(self):
        self.ok_data(f'{BASE}/{ACC}/refunds')
        call = self.fake.call_of(trade_proto.REFUND_LIST_API)
        self.assertEqual(call['data'], {'pageNumber': 1, 'rowsPerPage': 10,
                                        'queryType': 'refund'})
        self.assertEqual(call['v'], '1.0')


class TradeEvaluationsTest(TradePageTest):
    """评价：`rateItemVOList` 为空就是没有评价，不许画 0 条。"""

    def setUp(self):
        super().setUp()
        seed_account()

    def test_empty_rate_list_is_reported_as_no_rate(self):
        evaluation = self.ok_data(f'{BASE}/{ACC}/evaluations')['evaluations'][0]
        self.assertFalse(evaluation['has_rate'])
        self.assertIsNone(evaluation['rates'])
        self.assertEqual(evaluation['order_id'], '3316918549013103465')
        self.assertEqual(evaluation['order_status'], '交易成功')
        self.assertEqual(evaluation['buyer']['nick'], 'x***2')
        self.assertEqual(evaluation['item']['title'],
                         '我的世界MC开服一条龙｜服务器部署｜整合包定制｜插件模组配置')

    def test_rate_records_are_mapped_field_by_field(self):
        self.fake.payloads[trade_proto.RATE_LIST_API] = RATE_WITH_RECORD
        evaluation = self.ok_data(f'{BASE}/{ACC}/evaluations')['evaluations'][0]
        self.assertTrue(evaluation['has_rate'])
        record = evaluation['rates'][0]
        self.assertEqual(record['rate_id'], '123456789')
        self.assertEqual(record['rate'], '很好')
        self.assertEqual(record['main'], '卖家很耐心')
        self.assertEqual(record['feedback'], '谢谢支持')
        self.assertEqual(record['level'], '好评')
        self.assertEqual(record['pictures'], ['https://img.alicdn.com/a.jpg'])
        self.assertEqual(record['service_tags'], ['态度好'])
        self.assertFalse(record['illegal'])

    def test_evaluation_request_shape_matches_the_bundle(self):
        self.ok_data(f'{BASE}/{ACC}/evaluations')
        call = self.fake.call_of(trade_proto.RATE_LIST_API)
        self.assertEqual(call['data'], {'pageNumber': 1, 'rowsPerPage': 10,
                                        'queryType': 'RATE'})
        self.assertEqual(call['v'], '1.0')


class TradePlatformFailureTest(TradePageTest):
    """平台级失败：如实报错，绝不返回空列表假装成功。"""

    def setUp(self):
        super().setUp()
        seed_account()

    def test_complaints_permission_error_is_a_platform_error(self):
        body = self.fail(f'{BASE}/{ACC}/complaints', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('NO_PERMISSION', body['platform_errors']['explain'])
        self.assertEqual(body['platform_errors']['api'], trade_proto.COMPLAIN_LIST_API)
        self.assertIn('NO_PERMISSION', body['message'])

    def test_addresses_unauthorized_is_a_platform_error(self):
        body = self.fail(f'{BASE}/{ACC}/addresses', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('FAIL_BIZ_IDLE_USER_UNAUTHORIZED',
                      body['platform_errors']['explain'])

    def test_address_request_sends_no_data(self):
        self.fail(f'{BASE}/{ACC}/addresses', status=502)
        call = self.fake.call_of(trade_proto.ADDRESS_LIST_API)
        self.assertIsNone(call['data'])
        self.assertEqual(call['v'], '1.0')

    def test_failure_without_message_still_explains_itself(self):
        self.fake.payloads[trade_proto.ORDER_LIST_API] = {'success': False}
        body = self.fail(f'{BASE}/{ACC}/orders', status=502)
        self.assertTrue(body['platform_errors']['explain'])

    def test_empty_platform_body_is_not_treated_as_data(self):
        self.fake.payloads[trade_proto.ORDER_LIST_API] = {'success': True, 'data': {}}
        data = self.ok_data(f'{BASE}/{ACC}/orders')
        self.assertEqual(data['orders'], [])
        self.assertIsNone(data['paging']['total_count'])


#: 真机原文（2026-10-16，账号 2217970177348，logs/write64/b1e_address_list_raw.json）。
REAL_DOUBLE_ENVELOPE = {
    'success': True,
    'data': {
        'code': 'success',
        'data': [
            {'areaId': 330106, 'cityName': '杭州市', 'contactId': 3941114028,
             'contactName': '第64批探针', 'defaultAddr': False,
             'detailAddress': '文一西路 969 号 1 幢 101 室', 'districtName': '西湖区',
             'mobilePhone': '13800000000', 'provinceName': '浙江省', 'zipCode': '000000'},
            {'areaId': 330106, 'cityName': '杭州市', 'contactId': 3943370028,
             'contactName': '第64批探针B', 'defaultAddr': False,
             'detailAddress': '文一西路 969 号 3 幢 303 室', 'districtName': '西湖区',
             'mobilePhone': '13800000002', 'provinceName': '浙江省', 'zipCode': '000000'},
        ],
        'msg': '成功',
    },
    'message': 'SUCCESS::调用成功',
    'ret': ['SUCCESS::调用成功'],
}


class TradeAddressesPagingTest(TradePageTest):
    """退货地址：平台一次给全量 ⇒ 服务端内存切页；非法分页一次平台都不打（第 59 批）。"""

    #: 旧 bundle 读法：`data` 直接是数组。三个地址。真机 2026-10-16 是双层 `data.data`
    #: （`REAL_DOUBLE_ENVELOPE`），两条都测。
    ADDRESSES = {'success': True, 'data': [
        {'addressId': 1, 'name': '张三', 'phoneNum': '13800000001',
         'address': '浙江省杭州市西湖区某路 1 号', 'defaultAddr': True},
        {'addressId': 2, 'name': '李四', 'phoneNum': '13800000002',
         'address': '江苏省南京市玄武区某路 2 号'},
        {'addressId': 3, 'name': '王五', 'phoneNum': '13800000003',
         'address': '广东省深圳市南山区某路 3 号'},
    ], 'message': 'SUCCESS::调用成功', 'ret': ['SUCCESS::调用成功']}

    def setUp(self):
        super().setUp()
        seed_account()
        self.fake.payloads[trade_proto.ADDRESS_LIST_API] = self.ADDRESSES

    def test_without_paging_params_returns_the_whole_list(self):
        data = self.ok_data(f'{BASE}/{ACC}/addresses')
        self.assertEqual(len(data['addresses']), 3)
        self.assertEqual(data['items'], data['addresses'])
        self.assertEqual((data['page'], data['size'], data['pages']), (1, 3, 1))
        self.assertEqual(data['total'], 3)
        self.assertEqual(len(self.fake.calls), 1)

    def test_paging_slices_in_memory_without_a_second_platform_call(self):
        data = self.ok_data(f'{BASE}/{ACC}/addresses?page=2&size=2')
        self.assertEqual([a['address_id'] for a in data['items']], ['3'])
        self.assertEqual(data['addresses'], data['items'])
        self.assertEqual((data['page'], data['size'], data['pages'], data['total']),
                         (2, 2, 2, 3))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsNone(self.fake.call_of(trade_proto.ADDRESS_LIST_API)['data'])

    def test_only_one_param_uses_the_default_for_the_other(self):
        data = self.ok_data(f'{BASE}/{ACC}/addresses?size=2')
        self.assertEqual([a['address_id'] for a in data['items']], ['1', '2'])
        self.assertEqual((data['page'], data['size'], data['pages']), (1, 2, 2))

    def test_real_double_envelope_is_parsed_too(self):
        """真机 2026-10-16 原文（data.data 数组）—— 旧 items_of 在这里解析出 0 条。"""
        self.fake.payloads[trade_proto.ADDRESS_LIST_API] = REAL_DOUBLE_ENVELOPE
        data = self.ok_data(f'{BASE}/{ACC}/addresses')
        self.assertEqual([a['address_id'] for a in data['items']], ['3941114028', '3943370028'])
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['items'][0]['detail_address'], '文一西路 969 号 1 幢 101 室')

    def test_null_inner_data_is_an_empty_list(self):
        self.fake.payloads[trade_proto.ADDRESS_LIST_API] = {
            'success': True, 'data': {'code': 'success', 'data': None, 'msg': '成功'}}
        data = self.ok_data(f'{BASE}/{ACC}/addresses')
        self.assertEqual(data['addresses'], [])
        self.assertEqual((data['total'], data['size'], data['pages']), (0, 0, 0))

    def test_empty_list_is_zero_size_and_zero_pages(self):
        self.fake.payloads[trade_proto.ADDRESS_LIST_API] = {'success': True, 'data': []}
        data = self.ok_data(f'{BASE}/{ACC}/addresses')
        self.assertEqual(data['addresses'], [])
        self.assertEqual((data['total'], data['size'], data['pages']), (0, 0, 0))

    def test_illegal_paging_is_400_with_zero_platform_calls(self):
        for query in ('?page=0', '?size=0', '?size=51', '?page=abc', '?size=x'):
            with self.subTest(query=query):
                body = self.fail(f'{BASE}/{ACC}/addresses{query}')
                self.assertEqual(body['code'], 'invalid_fields')
                self.assertEqual(self.fake.calls, [])


class TradeCredentialTest(TradePageTest):
    """登录态：缺 Cookie / need_relogin 一律 409，且一次平台都不打。"""

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        seed_account(cookie='')
        body = self.fail(f'{BASE}/{ACC}/orders', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(self.fake.calls, [])

    def test_need_relogin_is_409(self):
        seed_account(login_state='need_relogin')
        body = self.fail(f'{BASE}/{ACC}/refunds', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail(f'{BASE}/nobody/orders', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_session_expired_is_reported_as_need_relogin(self):
        seed_account()
        self.fake.errors[trade_proto.ORDER_LIST_API] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail(f'{BASE}/{ACC}/orders', status=409)
        self.assertEqual(body['code'], 'verification_required')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?',
                           (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_risk_control_is_a_protocol_error_with_explain(self):
        seed_account()
        self.fake.errors[trade_proto.REFUND_LIST_API] = RuntimeError('FAIL_SYS_USER_VALIDATE')
        body = self.fail(f'{BASE}/{ACC}/refunds', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('FAIL_SYS_USER_VALIDATE', body['platform_errors']['explain'])


class TradePagingTest(TradePageTest):
    """分页参数校验：一律 invalid_fields 信封，不落 422 裸 body。"""

    def setUp(self):
        super().setUp()
        seed_account()

    def test_bad_paging_is_rejected_before_calling_platform(self):
        for query in ('?page=0', '?size=0', '?size=51', '?page=abc', '?size=x'):
            body = self.fail(f'{BASE}/{ACC}/orders{query}', status=400)
            self.assertEqual(body['code'], 'invalid_fields', query)
        self.assertEqual(self.fake.calls, [])

    def test_page_size_upper_bound_is_50(self):
        self.ok_data(f'{BASE}/{ACC}/orders?size=50')
        self.assertEqual(self.fake.call_of(trade_proto.ORDER_LIST_API)['data'],
                         {'pageNumber': 1, 'rowsPerPage': 50})


class OrderMemoNormalizeTest(unittest.TestCase):
    """`normalize_order` 要透出订单备注（第四十九批补的字段）。

    **为什么单独钉**：备注写接口（`mtop.taobao.idle.merchant.add.memo`）写完之后，
    界面得能把**当前**备注显示出来，否则用户只能靠「保存成功」猜。官方读回有两处：
    订单列表行的 `commonData.memo/memoGroup` 与详情里的 `merchantCommonData.*`
    （交易 bundle 偏移 2069748）⇒ 两处都认；**平台没给就是 None**，不许造空串。
    """

    def test_memo_comes_from_common_data(self):
        out = trade_proto.normalize_order({'commonData': {'orderId': '1', 'memo': '已电话确认',
                                                          'memoGroup': 3}})
        self.assertEqual(out['memo'], '已电话确认')
        self.assertEqual(out['memo_group'], 3)

    def test_memo_falls_back_to_merchant_common_data(self):
        out = trade_proto.normalize_order({'merchantCommonData': {'orderId': '1', 'memo': '老客户',
                                                                  'memoGroup': '2'}})
        self.assertEqual(out['memo'], '老客户')
        self.assertEqual(out['memo_group'], 2)

    def test_missing_memo_is_none_not_empty_string(self):
        out = trade_proto.normalize_order({'commonData': {'orderId': '1'}})
        self.assertIsNone(out['memo'])
        self.assertIsNone(out['memo_group'])

    def test_empty_memo_is_none(self):
        out = trade_proto.normalize_order({'commonData': {'orderId': '1', 'memo': '',
                                                          'memoGroup': 1}})
        self.assertIsNone(out['memo'])
        self.assertEqual(out['memo_group'], 1)


class AddressNormalizeTest(unittest.TestCase):
    """`normalize_address` 的字段口径（bundle 两处读法都要认，但绝不造字段）。

    **为什么单独钉**：官方 bundle 里退货地址有**两套键名** —— 只读页的 `formatter` 用
    `{addressId, name, phoneNum, address(拼好的整串), defaultAddr}`，写页的编辑弹窗按
    `{contactId, contactName, mobilePhone, provinceName, cityName, districtName,
    detailAddress, defaultAddr}` 逐字段读。真机 `address.list.query` 对我们无权限、
    拿不到样本，所以**不能判定哪一套是真的** ⇒ 两套都认，但平台没给的字段一个都不许出现。
    """

    MERGED = {
        'addressId': '123456789',
        'name': '张三',
        'phoneNum': '13800138000',
        'address': '浙江省 杭州市 西湖区 文一西路 1 号',
        'defaultAddr': True,
    }
    PER_FIELD = {
        'contactId': 987654321,
        'contactName': '李四',
        'mobilePhone': '13900139000',
        'provinceName': '浙江省',
        'cityName': '杭州市',
        'districtName': '西湖区',
        'detailAddress': '文一西路 2 号',
        'defaultAddr': False,
    }

    def test_read_page_shape_keeps_the_five_legacy_keys(self):
        out = trade_proto.normalize_address(self.MERGED)
        self.assertEqual(out['address_id'], '123456789')
        self.assertEqual(out['name'], '张三')
        self.assertEqual(out['phone'], '13800138000')
        self.assertEqual(out['address'], '浙江省 杭州市 西湖区 文一西路 1 号')
        self.assertIs(out['is_default'], True)

    def test_read_page_shape_does_not_invent_per_field_keys(self):
        out = trade_proto.normalize_address(self.MERGED)
        for key in ('contact_id', 'contact_name', 'mobile_phone', 'province_name',
                    'city_name', 'district_name', 'detail_address'):
            self.assertNotIn(key, out, f'{key} 平台没给，不许造')

    def test_write_page_shape_passes_every_field_through(self):
        out = trade_proto.normalize_address(self.PER_FIELD)
        self.assertEqual(out['address_id'], '987654321')  # contactId 兜底，且整数保精度
        self.assertEqual(out['name'], '李四')
        self.assertEqual(out['phone'], '13900139000')
        self.assertEqual(out['contact_id'], '987654321')
        self.assertEqual(out['contact_name'], '李四')
        self.assertEqual(out['mobile_phone'], '13900139000')
        self.assertEqual(out['province_name'], '浙江省')
        self.assertEqual(out['city_name'], '杭州市')
        self.assertEqual(out['district_name'], '西湖区')
        self.assertEqual(out['detail_address'], '文一西路 2 号')
        self.assertIs(out['is_default'], False)

    def test_write_page_shape_without_merged_address_is_not_guessed(self):
        # 逐字段那套没有 `address` 整串 —— 不许自己拼一个出来冒充平台数据。
        out = trade_proto.normalize_address(self.PER_FIELD)
        self.assertIsNone(out['address'])

    def test_empty_payload_gives_five_none_keys_and_nothing_else(self):
        out = trade_proto.normalize_address({})
        self.assertEqual(set(out), {'address_id', 'name', 'phone', 'address', 'is_default'})
        for key in ('address_id', 'name', 'phone', 'address'):
            self.assertIsNone(out[key], key)
        self.assertIsNone(out['is_default'])

    def test_blank_strings_do_not_become_fields(self):
        out = trade_proto.normalize_address({
            'contactId': '',
            'contactName': '   ',
            'mobilePhone': '',
            'provinceName': '   ',
            'detailAddress': None,
        })
        self.assertIsNone(out['address_id'])
        self.assertEqual(set(out), {'address_id', 'name', 'phone', 'address', 'is_default'})

    def test_non_dict_is_tolerated(self):
        for bad in (None, 'x', [], 3):
            out = trade_proto.normalize_address(bad)
            self.assertEqual(set(out), {'address_id', 'name', 'phone', 'address', 'is_default'}, bad)

    def test_default_flag_must_be_a_real_bool(self):
        # 真机可能给字符串 'true'；不是 bool 就给 None，不猜。
        self.assertIsNone(trade_proto.normalize_address({'defaultAddr': 'true'})['is_default'])
        self.assertIsNone(trade_proto.normalize_address({'defaultAddr': 1})['is_default'])




class RefundHandlingReadTest(TradePageTest):
    """退款处理四条只读（拒绝原因 / 退款详情 / 邮费退款详情与原因）。

    这一组最容易犯的四个错：

    1. 平台**没给**原因列表时给 `[]`（用户会以为「平台说没有原因」，其实是键没认出来）；
    2. 把 `refuseReasonId`（要提交给平台的 id）与 `reasonId`（官方用来判断置灰）混为一谈；
    3. 把官方置灰的那条（只能在手机 App 操作）也当成可选；
    4. 把退款详情/邮费详情**猜**成我们的字段名（官方是 `components[].render` 与 `data.module` 原样透传）。
    """

    def setUp(self):
        super().setUp()
        seed_account()

    # -- 拒绝原因（refuse.render） ----------------------------------------

    def test_refuse_reasons_map_both_ids_and_flag_the_app_only_one(self):
        self.fake.payloads[trade_proto.REFUND_REFUSE_RENDER_API] = REFUSE_RENDER
        data = self.ok_data(f'{BASE}/{ACC}/refunds/r-1/refuse-reasons')
        reasons = data['refuse_reasons']
        self.assertEqual(len(reasons), 3, data)
        self.assertEqual(reasons[0]['refuse_reason_id'], '20653006')
        self.assertEqual(reasons[0]['reason_id'], '20653006')
        self.assertEqual(reasons[0]['reason_name'], '买家已使用')
        self.assertFalse(reasons[0]['app_only'])
        self.assertTrue(reasons[1]['app_only'], '官方把 20653007 置灰（只能在 App 操作）')
        self.assertIn('手机闲鱼 App', data['app_only_hint'])
        self.assertEqual(list(data['app_only_reason_ids']), ['20653007'])
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['source'], 'platform')
        self.assertTrue(data['fetched_at'])
        self.assertEqual(data['refund_id'], 'r-1')

    def test_refuse_reason_request_shape_matches_the_bundle(self):
        self.fake.payloads[trade_proto.REFUND_REFUSE_RENDER_API] = REFUSE_RENDER
        self.ok_data(f'{BASE}/{ACC}/refunds/r-1/refuse-reasons',
                     '?order_id=o-1&refuse_reason_id=20653006')
        call = self.fake.call_of(trade_proto.REFUND_REFUSE_RENDER_API)
        self.assertEqual(call['data'], {'refundId': 'r-1', 'refuseReasonId': '20653006'})
        self.assertEqual(call['v'], '1.0')

    def test_refuse_reason_request_omits_the_optional_id(self):
        self.fake.payloads[trade_proto.REFUND_REFUSE_RENDER_API] = REFUSE_RENDER
        self.ok_data(f'{BASE}/{ACC}/refunds/r-1/refuse-reasons')
        call = self.fake.call_of(trade_proto.REFUND_REFUSE_RENDER_API)
        self.assertEqual(call['data'], {'refundId': 'r-1'})

    def test_missing_reason_list_is_null_with_a_warning_not_an_empty_list(self):
        self.fake.payloads[trade_proto.REFUND_REFUSE_RENDER_API] = REFUSE_RENDER_NO_LIST
        data = self.ok_data(f'{BASE}/{ACC}/refunds/r-1/refuse-reasons')
        self.assertIsNone(data['refuse_reasons'], '键没认出来 ≠ 平台说没有原因')
        self.assertFalse(data['refuse_reasons_found'])
        self.assertEqual([w['code'] for w in data['warnings']], ['refund_refuse_reasons_missing'])

    def test_blank_refund_id_is_rejected_before_calling_platform(self):
        result = trade.refund_refuse_reasons(ACC, '')
        self.assertIs(result['success'], False, result)
        self.assertEqual(result['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [], '字段校验必须早于建客户端')

    # -- 退款详情（refund.detail） ----------------------------------------

    def test_refund_detail_passes_components_through(self):
        self.fake.payloads[trade_proto.REFUND_DETAIL_API] = REFUND_DETAIL
        data = self.ok_data(f'{BASE}/{ACC}/refunds/r-1/detail', '?order_id=o-1')
        self.assertEqual(data['component_renders'],
                         ['nodeStatusInfo', 'basicRefundInfo', 'progressDetail'])
        self.assertEqual(data['components'][1]['raw']['refundFee'], '12.80')
        self.assertIn('nodeStatusInfo', data['known_renders'])
        self.assertEqual(data['warnings'], [])
        call = self.fake.call_of(trade_proto.REFUND_DETAIL_API)
        self.assertEqual(call['data'], {'orderId': 'o-1', 'refundId': 'r-1'})

    def test_refund_detail_without_components_warns(self):
        self.fake.payloads[trade_proto.REFUND_DETAIL_API] = REFUND_DETAIL_NO_COMPONENTS
        data = self.ok_data(f'{BASE}/{ACC}/refunds/r-1/detail')
        self.assertIsNone(data['components'])
        self.assertEqual([w['code'] for w in data['warnings']], ['refund_detail_components_missing'])

    # -- 邮费退款详情（postage.refund.detail.query） -----------------------

    def test_postage_detail_is_passed_through_with_one_normalized_flag(self):
        self.fake.payloads[trade_proto.POSTAGE_REFUND_DETAIL_API] = POSTAGE_DETAIL
        data = self.ok_data(f'{BASE}/{ACC}/orders/o-1/postage-refund/detail',
                            '?refund_id=r-1&refund_postage_type=SELLER')
        self.assertEqual(data['detail']['refundFee'], '8.00')
        self.assertIs(data['exist_processing_compensate'], False)
        self.assertEqual(data['warnings'], [])
        call = self.fake.call_of(trade_proto.POSTAGE_REFUND_DETAIL_API)
        self.assertEqual(call['data'], {'orderId': 'o-1', 'refundId': 'r-1',
                                        'refundPostageType': 'SELLER'})

    def test_postage_detail_empty_module_is_null_with_a_warning(self):
        self.fake.payloads[trade_proto.POSTAGE_REFUND_DETAIL_API] = POSTAGE_DETAIL_EMPTY
        data = self.ok_data(f'{BASE}/{ACC}/orders/o-1/postage-refund/detail')
        self.assertIsNone(data['detail'])
        self.assertIsNone(data['exist_processing_compensate'])
        self.assertEqual([w['code'] for w in data['warnings']], ['postage_refund_detail_missing'])

    def test_postage_detail_requires_an_order_id(self):
        result = trade.postage_refund_detail(ACC, '')
        self.assertIs(result['success'], False, result)
        self.assertEqual(result['code'], 'invalid_fields')
        self.assertEqual(self.fake.calls, [])

    # -- 邮费退款拒绝原因（postage.refund.refuse.reason.query） -------------

    def test_postage_reasons_are_strings(self):
        self.fake.payloads[trade_proto.POSTAGE_REFUND_REFUSE_REASONS_API] = POSTAGE_REASONS
        data = self.ok_data(f'{BASE}/{ACC}/orders/o-1/postage-refund/refuse-reasons',
                            '?default_reason=%E9%82%AE%E8%B4%B9%E9%87%91%E9%A2%9D%E4%B8%8D%E5%AF%B9')
        self.assertEqual(data['reasons'], ['邮费金额不对', '买家未协商', '其他原因'])
        self.assertEqual(data['warnings'], [])
        call = self.fake.call_of(trade_proto.POSTAGE_REFUND_REFUSE_REASONS_API)
        self.assertEqual(call['data'], {'orderId': 'o-1', 'defaultReason': '邮费金额不对'})

    def test_postage_reasons_without_the_key_is_null_with_a_warning(self):
        self.fake.payloads[trade_proto.POSTAGE_REFUND_REFUSE_REASONS_API] = POSTAGE_REASONS_NO_KEY
        data = self.ok_data(f'{BASE}/{ACC}/orders/o-1/postage-refund/refuse-reasons')
        self.assertIsNone(data['reasons'])
        self.assertEqual([w['code'] for w in data['warnings']], ['postage_refund_reasons_missing'])

    # -- 边界 ------------------------------------------------------------

    def test_the_four_new_routes_are_read_only(self):
        for name in REFUND_PATHS:
            path = f'{BASE}/{ACC}/{name}'
            for method in ('post', 'put', 'patch', 'delete'):
                resp = getattr(self.client, method)(path, headers=self.headers)
                self.assertEqual(resp.status_code, 405, f'{method} {path} → {resp.status_code}')

    def test_platform_refusal_is_a_platform_error_not_a_logout(self):
        self.fake.payloads[trade_proto.REFUND_REFUSE_RENDER_API] = COMPLAIN_DENIED
        body = self.fail(f'{BASE}/{ACC}/refunds/r-1/refuse-reasons', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('NO_PERMISSION', body['message'])
        self.assertEqual(self.login_state(), 'active', '无权限 ≠ 掉线')

    def test_unknown_account_is_404_and_never_calls_platform(self):
        body = self.fail(f'{BASE}/trade_nobody/refunds/r-1/detail', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_missing_cookie_is_409(self):
        db.execute('UPDATE account_credentials SET cookie=? WHERE account_id=?', ('', ACC))
        body = self.fail(f'{BASE}/{ACC}/orders/o-1/postage-refund/refuse-reasons', status=409)
        self.assertEqual(body['code'], 'verification_required')

    def test_requests_require_login(self):
        resp = self.get(f'{BASE}/{ACC}/refunds/r-1/refuse-reasons', headers={})
        self.assertIn(resp.status_code, (401, 403), resp.text)

    def login_state(self) -> str:
        row = db.query('SELECT login_state FROM account_credentials WHERE account_id=?', (ACC,))
        return (row[0]['login_state'] if row else '') or ''


if __name__ == '__main__':
    unittest.main(verbosity=2)
