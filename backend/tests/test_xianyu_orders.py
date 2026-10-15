"""协议层订单接口契约测试（`backend/app/xianyu/client.py::XianyuClient.list_orders`）。

**不打真实闲鱼平台**：`httpx.MockTransport` 按真机 `sold.get` 的响应结构回话
（业务体在 `data.module.items`，字段分在 `commonData` / `buyerInfoVO` / `priceVO`），
逐条锁死「用哪个接口、哪个版本、payload 顶层带什么、怎么翻页、怎么归一化」——
`list_orders` 不存在正是 `POST /api/orders/sync` 一直 503 的根因。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\test_xianyu_orders.py"
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from backend.app.xianyu import trade  # noqa: E402
from backend.app.xianyu.client import (  # noqa: E402
    ORDER_LIST_PAGE_SIZE,
    XianyuClient,
    XianyuError,
)

#: 带 unb 的假 Cookie（没有 unb 时 XianyuClient 直接 ValueError）
COOKIE = 'unb=2200123456; _m_h5_tk=abcdef123456_1712345678901; cookie2=xyz'


def order_item(order_id: str, status: str = '交易成功', price: str = '39.90',
               buy_num: int = 1, total_price: Optional[str] = None) -> Dict[str, Any]:
    """真机订单项形状：`commonData` / `buyerInfoVO` / `itemVO` / `priceVO` 四层。

    `status` 默认「交易成功」—— 真机 `orderStatus` 就是**中文原文**。
    """
    return {
        'commonData': {
            'orderId': order_id,
            'itemId': 'I' + order_id,
            'orderStatus': status,
            'createTime': '2026-10-01 10:00:00',
            'paySuccessTime': '2026-10-01 10:01:00',
        },
        'buyerInfoVO': {'buyerId': 'b' + order_id, 'userNick': '买家' + order_id},
        'itemVO': {'title': '测试商品' + order_id,
                   'itemPicUrl': 'https://img.alicdn.com/a.jpg'},
        'priceVO': {'auctionPrice': price, 'buyNum': buy_num,
                    'totalPrice': price if total_price is None else total_price},
    }


def page_payload(items: List[Dict[str, Any]], total: int = 0) -> Dict[str, Any]:
    """真机容器：`data.module.items`（`trade.body_of` 认的就是这一层）。"""
    return {'module': {'items': items, 'totalCount': total}}


class MtopStub:
    """按 api 名回话的假 mtop 服务端。记录每一次请求的 (api, version, payload)。"""

    def __init__(self, responses: Dict[str, Any],
                 fail_ret: Optional[Dict[str, str]] = None):
        self.responses = responses
        self.fail_ret = fail_ret or {}
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        query = parse_qs(request.content.decode('utf-8'))
        payload = json.loads(query['data'][0]) if query.get('data') else {}
        api = params.get('api', '')
        self.calls.append({
            'api': api,
            'version': params.get('v', ''),
            'payload': payload,
        })
        if api in self.fail_ret:
            return httpx.Response(200, json={'ret': [self.fail_ret[api]], 'data': None})
        responder = self.responses.get(api)
        if responder is None:
            return httpx.Response(200, json={'ret': ['FAIL_SYS_API_NOT_FOUND::接口不存在'],
                                             'data': {}})
        data = responder(payload) if callable(responder) else responder
        return httpx.Response(200, json={'ret': ['SUCCESS::调用成功'], 'data': data})

    def apis(self) -> List[str]:
        return [call['api'] for call in self.calls]


def make_client(responses: Dict[str, Any], fail_ret: Optional[Dict[str, str]] = None):
    stub = MtopStub(responses, fail_ret)
    client = XianyuClient(COOKIE, transport=httpx.MockTransport(stub))
    return client, stub


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class ListOrdersTest(unittest.TestCase):
    ACCOUNT = '2200123456'

    def test_single_short_page_is_normalized(self):
        """一页不满 `size` ⇒ 只打一次接口，返回的是归一化订单。"""
        items = [order_item(f'O{i}') for i in range(3)]
        client, stub = make_client({trade.ORDER_LIST_API: page_payload(items)})
        result = run(client.list_orders(self.ACCOUNT))

        self.assertEqual(len(result['orders']), 3)
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['pages'], 1)
        self.assertEqual(stub.apis(), [trade.ORDER_LIST_API])
        call = stub.calls[0]
        self.assertEqual(call['version'], trade.ORDER_LIST_VERSION)
        self.assertEqual(call['payload']['pageNumber'], 1)
        self.assertEqual(call['payload']['rowsPerPage'], ORDER_LIST_PAGE_SIZE)

        first = result['orders'][0]
        # 归一化：真机字段藏在 commonData / buyerInfoVO / priceVO 里
        self.assertEqual(first['order_id'], 'O0')
        self.assertEqual(first['item_id'], 'IO0')
        self.assertEqual(first['order_status'], '交易成功')
        self.assertEqual(first['buyer']['user_id'], 'bO0')
        self.assertEqual(first['price']['total_price'], '39.90')
        self.assertEqual(first['times']['pay_time'], '2026-10-01 10:01:00')

    def test_multi_page_until_short_page(self):
        """平台没给 `totalCount` 时退回「满没满」判据：满页继续翻，短页停。"""
        full = [order_item(f'A{i}') for i in range(ORDER_LIST_PAGE_SIZE)]
        tail = [order_item('B0'), order_item('B1')]

        def responder(payload: Dict[str, Any]) -> Dict[str, Any]:
            if int(payload.get('pageNumber') or 1) == 1:
                return page_payload(full)
            return page_payload(tail)

        client, stub = make_client({trade.ORDER_LIST_API: responder})
        result = run(client.list_orders(self.ACCOUNT))

        self.assertEqual(result['pages'], 2)
        self.assertEqual(len(result['orders']), ORDER_LIST_PAGE_SIZE + 2)
        self.assertEqual([c['payload']['pageNumber'] for c in stub.calls], [1, 2])

    def test_total_count_stops_paging(self):
        """`totalCount` 报的总数拿够了就停 —— 不再多打一次接口。

        真机 `sold.get` 的这一层在 `data.module` 里，实测 `total_count=15`
        （`logs/order_sync57/platform_orders.json`）；`paging_of` 会合并 `body_of`。
        """
        items = [order_item(f'A{i}') for i in range(ORDER_LIST_PAGE_SIZE)]
        client, stub = make_client({trade.ORDER_LIST_API:
                                   page_payload(items, total=ORDER_LIST_PAGE_SIZE)})
        result = run(client.list_orders(self.ACCOUNT))

        self.assertEqual(result['pages'], 1)
        self.assertEqual(result['total'], ORDER_LIST_PAGE_SIZE)
        self.assertEqual(len(stub.calls), 1)

    def test_dropped_rows_are_reported(self):
        """空/重复 `order_id` 行会被丢弃，但**必须报出来**（不能静默）。

        第 57 批留下的待办（`docs/交接文档.md` §10 第 10 条）：返回的 `total` 是平台
        报的总数，可能大于实际拿到的行数。
        """
        items = [order_item('D1'), order_item('D1'), order_item('')]
        client, stub = make_client({trade.ORDER_LIST_API: page_payload(items, total=1)})
        result = run(client.list_orders(self.ACCOUNT))

        self.assertEqual([o['order_id'] for o in result['orders']], ['D1'])
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['dropped'], 2)
        self.assertEqual(len(stub.calls), 1)

    def test_truncated_page_keeps_paging(self):
        """平台把 `rowsPerPage` 截短（回的行数 < 请求的 `size`）时**不能**当最后一页停。

        没有 `totalCount` 兜底就会静默少同步：`services/orders.py` 只 upsert 拿到的行。
        """
        def responder(payload: Dict[str, Any]) -> Dict[str, Any]:
            start = (int(payload.get('pageNumber') or 1) - 1) * 5
            return page_payload([order_item(f'C{start + i}') for i in range(5)], total=15)

        client, stub = make_client({trade.ORDER_LIST_API: responder})
        result = run(client.list_orders(self.ACCOUNT))

        self.assertEqual(result['pages'], 3)
        self.assertEqual(result['total'], 15)
        self.assertEqual(len(result['orders']), 15)
        self.assertEqual([c['payload']['pageNumber'] for c in stub.calls], [1, 2, 3])

    def test_repeated_page_stops(self):
        """平台重复回同一页（真机上商品列表出现过）⇒ 不能再翻下去。"""
        items = [order_item(f'A{i}') for i in range(ORDER_LIST_PAGE_SIZE)]
        client, stub = make_client({trade.ORDER_LIST_API: page_payload(items)})
        result = run(client.list_orders(self.ACCOUNT))

        self.assertEqual(result['pages'], 2)
        self.assertEqual(len(result['orders']), ORDER_LIST_PAGE_SIZE)
        self.assertEqual(len(stub.calls), 2)

    def test_account_mismatch_is_rejected(self):
        """`account_id` 只是位置传参：串号必须当场报错，不能拿别人的订单落库。"""
        client, stub = make_client({trade.ORDER_LIST_API: page_payload([])})
        with self.assertRaises(ValueError):
            run(client.list_orders('9999999999999'))
        self.assertEqual(stub.calls, [])
        # 数字形态的账号 ID 与 Cookie 里的 unb 是同一个账号
        result = run(client.list_orders(int(self.ACCOUNT)))
        self.assertEqual(result['orders'], [])

    def test_platform_rejection_raises(self):
        """平台明确拒绝时 `mtop()` 抛异常 ⇒ 服务层落 502，而不是「同步了 0 条」的成功。"""
        client, _ = make_client({}, fail_ret={trade.ORDER_LIST_API:
                                             'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        with self.assertRaises(XianyuError):
            run(client.list_orders(self.ACCOUNT))


if __name__ == '__main__':
    unittest.main()
