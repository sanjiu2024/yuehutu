"""数据组（数据总览 / 粉丝数据 / 客服数据）只读协议 + 服务层 + HTTP 契约测试。

**为什么单开一组**：这一族接口最容易犯三个错 ——

1. 把平台**拿不到**的指标画成 0（用户会以为「昨天真的没人看」）。真机实测
   （2026-10-10，账号 2217970177348）这个账号对 `datacompass` 的 `singleuser` /
   `fans` / `cs` 族一律 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`，
   所以「一屏 -」才是如实回答；只有平台**真的**返回 0 时才允许是 0。
2. 把 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（无权限）当成账号掉线，让用户白扫一次码。
3. 顺手写平台（这族接口全是只读，一个写方法都不该存在）。

这组用例把口径钉住：

- `test_routes_are_read_only`：三个路径的 POST/PUT/PATCH/DELETE 一律 405；
- `test_missing_metrics_stay_null_never_zero`：平台没给的键是 `None`，**不是 0**；
- `test_platform_zero_is_kept`：平台真返回 0 时必须原样保留（0 与 None 是两件事）；
- `test_begin_time_real_shape`：真机 `get.begin.time` 的原始响应形状能被认出来；
- `test_all_calls_failed_is_502` / `test_partial_failure_keeps_other_metrics`：
  全坏给 502，坏一个只影响它自己贡献的指标；
- `test_missing_cookie_is_409_and_never_calls_platform`：没 Cookie 时**一次平台都不打**。

跑法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe backend\\tests\\test_datacenter_api.py"

**不打真实闲鱼平台**：协议层用假模块注入（`install_fake_protocol`）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.api import datacenter as datacenter_api  # noqa: E402
from backend.app.core import db, security  # noqa: E402
from backend.app.services import datacenter as datacenter_service  # noqa: E402
from backend.app.xianyu import datacenter as datacenter_proto  # noqa: E402
from backend.tests.testenv import use_temp_app_dir  # noqa: E402

ACC = 'dc_acc'
BASE = '/api/datacenter'

#: 真机 `mtop.alibaba.idle.seller.datacompass.get.begin.time` 的**原始响应**
#: （2026-10-10 12:00，账号 2217970177348）：数据起始日 20260201。
BEGIN_TIME_REAL = {'code': 'success', 'data': '20260201', 'msg': '成功'}

#: 数据总览：平台给的形状（字段名按官方口径假设，认得出才填）
OVERVIEW_PAYLOAD = {
    'extendInfo': {'realDateRange': ['2026-10-04', '2026-10-10']},
    'data': {
        'exposureNum': 1971, 'exposureUv': 1298,
        'visitCount': 124, 'visitorCount': 47,
        'browseCount': 97, 'browseUv': 45,
        'onlineItemCount': 45, 'inquiryUserCount': 8,
        'threeMinReplyRate': 33.33,
    },
}
#: 只给了两个指标：其余键必须是 None（不是 0）
OVERVIEW_PARTIAL = {'data': {'exposureNum': 1971, 'visitorCount': 47}}
#: 平台**真的**返回 0：必须原样保留
OVERVIEW_ZERO = {'data': {'exposureNum': 0, 'visitCount': 0}}
FANS_PAYLOAD = {
    'data': {'fansTotal': 128, 'newFans': 6, 'lostFans': 1, 'activeFans': 33,
             'fansOrderCount': 4, 'fansBuyerCount': 3},
    'insights': [{'label': '18-24 岁', 'ratio': 0.31}],
}
#: 粉丝画像：官方页面渲染的是 `fans.insights.query` 响应里的**具名列表**
#: （bundle `datacenter_main.js` 偏移 2452720 的 `rwe()`：性别 / 粉丝类型 / 省份 / 八大人群）。
#: 注意 `profileVal` 是**标签**不是数字，比例有 `usrRatio`（0~1）与 `usrRatioFormat`（`'12.00%'`）两种。
FANS_INSIGHTS_PAYLOAD = {
    'code': 'success',
    'data': {
        'buyerGenderList': [
            {'profileVal': 'F', 'usrRatio': 0.62},
            {'profileVal': 'M', 'usrRatio': 0.38},
        ],
        'fansTypeList': [
            {'profileVal': 'D_活跃粉', 'usrRatioFormat': '25.00%'},
            {'profileVal': 'A_流失粉', 'usrRatioFormat': '10.00%'},
        ],
        'buyerProvinceList': [
            {'profileVal': '广东', 'usrRatioFormat': '12.00%'},
            {'profileVal': '浙江', 'usrRatioFormat': '20.00%'},
        ],
        'buyerEightTpList': [
            {'profileVal': '学生群体', 'usrRatioFormat': '30.00%'},
            {'profileVal': '未来之星', 'usrRatioFormat': '5.00%'},
        ],
    },
}
#: 平台把接口都答了、却一组画像分组都没给（真机上是「改了字段名」）
FANS_NO_BREAKDOWN = {'code': 'success', 'data': {'fansTotal': 128}}
CS_PAYLOAD = {
    'data': {'inquiryUserCount': 8, 'inquiryCount': 11, 'replyRate3min': 33.33,
             'avgReplyTime': 42, 'unrepliedCount': 2, 'satisfactionRate': 98.5},
}

#: 商品数据（官方 `/seller-data/commodity`）：列表响应。指标列名由平台决定，
#: 我们**原样透传**（`ipvUv` / `payAmt` 这类是平台字段，不翻译成我们的键）。
COMMODITY_PAYLOAD = {
    'data': {
        'list': [
            {'itmId': '812345678901', 'itmName': '测试商品 A',
             'itmPicUrl': 'https://img.alicdn.com/a.jpg', 'accountName': '月狐兔_',
             'ipvUv': 12, 'ipvPv': 30, 'payCnt': 1, 'payAmt': 39.9},
            {'itmId': '812345678902', 'itmName': '测试商品 B', 'itmPicUrl': '',
             'accountName': '月狐兔_', 'ipvUv': 3, 'ipvPv': 4, 'payCnt': 0, 'payAmt': 0},
        ],
        'total': 45,
    },
    'extendInfo': [{'realDateRange': ['2026-10-13', '2026-10-13']}],
}
#: 指标列定义：官方形状是「分组数组 + subFields」，页面摊平后每列带组名
COMMODITY_INDICATORS = {
    'data': [
        {'name': '流量', 'subFields': [
            {'name': 'ipvUv', 'title': '商品访客数', 'key': 'ipvUv'},
            {'name': 'ipvPv', 'title': '商品浏览量', 'key': 'ipvPv'},
        ]},
        {'name': '成交', 'subFields': [
            {'name': 'payCnt', 'title': '支付笔数', 'key': 'payCnt'},
            {'name': 'payAmt', 'title': '支付金额', 'key': 'payAmt'},
        ]},
    ],
}
#: 平台没回列表键（改了字段名）：必须是 None，**不是空列表**
COMMODITY_NO_LIST = {'data': {'total': 0}}
#: 平台没回列定义：必须是 None
COMMODITY_NO_COLUMNS = {'code': 'success', 'data': None}


def commodity_payload(rows: Any = COMMODITY_PAYLOAD, columns: Any = COMMODITY_INDICATORS,
                      total: Any = 45, date_type: str = datacenter_proto.DATE_TYPE_DAY,
                      dates: Optional[List[str]] = None,
                      errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """把「平台原始响应」拼成协议层 `commodity()` 的返回形状（真解析函数参与）。"""
    parsed = datacenter_proto.parse_commodity_list(rows)
    indicators = datacenter_proto.parse_commodity_indicators(columns)
    return {
        'rows': parsed['rows'],
        'total': parsed['total'] if total is None else total,
        'extend_info': parsed['extend_info'],
        'found_key': parsed['found_key'],
        'columns': indicators['columns'],
        'groups': indicators['groups'],
        'real_date_range': None,
        'date_type': date_type,
        'dates': list(dates or []),
        'errors': errors or {},
        'raw': {},
    }


#: 经营概况卡的真实响应形状（`singleuser.seller.summary`）：顶部指标在
#: `graphBannerBenchData.bannerDataList`、折线在 `graphDataList`，指标名是平台字段名。
CARD_SELLER_PAYLOAD = {
    'extendInfo': {'realDateRange': ['2026-10-13', '2026-10-13']},
    'data': {
        'graphBannerBenchData': {
            'bannerDataList': [
                {'name': 'vstPv', 'data': 124, 'ratioFormat': '+3.2%', 'dataFormat': '12.4%'},
                {'name': 'payAmt', 'data': 40.11, 'cycle': '日环比'},
                # 平台新增了一个我们不认识的指标名：**不猜中文**，label 给 None
                {'name': 'brandNewMetric', 'data': 7},
            ],
            'graphDataList': [
                {'ds': '20261012', 'vstPv': 100},
                {'ds': '20261013', 'vstPv': 124},
            ],
        },
        'promotionInfoList': [{'bizId': 1, 'amountOfYuan': '33.58'}],
    },
}
#: 退款概览卡：`data` 里每个键都是一个指标对象（不是 graphBanner 结构）
CARD_REFUND_PAYLOAD = {
    'data': {
        'crtRfdCnt': {'name': 'crtRfdCnt', 'data': 2, 'ratioFormat': '-1'},
        'succRfdRate': {'name': 'succRfdRate', 'data': '50%'},
    },
}


def card_payload(kind: str = 'seller', payload: Any = None, found: bool = True,
                 date_type: str = datacenter_proto.DATE_TYPE_DAY,
                 dates: Optional[List[str]] = None,
                 errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """把「平台原始响应」拼成协议层 `card()` 的返回形状（**真解析函数参与**）。

    `found=False` 用来模拟「平台改了字段名，body 是空的」：此时指标/折线全空，服务层
    要额外给一条 warning —— 空列表与「结构都没有」是两件事。
    """
    parsed = datacenter_proto.parse_card(
        CARD_SELLER_PAYLOAD if payload is None else payload, kind,
        datacenter_proto.card_labels(kind, 'seller-id'),
    )
    parsed.update({
        'date_type': date_type,
        'dates': list(dates or []),
        'errors': errors or {},
        'raw': {},
    })
    if not found:
        parsed.update({'found': False, 'metrics': [], 'banner': [], 'series': []})
    return parsed


#: 三张明细表的行（字段名逐字取自官方表头的 `dataIndex`）
CS_DETAIL_ROWS = [
    {'acctName': '月狐兔_', 'consultUv': 12, 'visitorCnt': 10, 'consultItmCnt': 5,
     'avgRespTm': '36秒', 'custSatCnt': 4.8, 'custSatRate': '96%', 'validReviewCnt': 20,
     'validPosReviewCnt': 19, 'validNegReviewCnt': 1, 'respRate3min': '88.5%',
     'respRate1min': '50%'},
]
CS_DIVERSION_ROWS = [{'divTp': '客服分组', 'dtlTp': '默认分组', 'sessCnt': 37}]
CS_EVALUATION_ROWS = [{'buyerUserName': '张三', 'buyerNick': 'zhangsan',
                       'subUserName': '月狐兔_', 'reviewTime': '2026-10-13 10:00:00',
                       'reviewScore': '很满意', 'dissatReason': ''}]


def cs_list_payload(kind: str = 'detail', rows: Any = None, total: Any = 3,
                    errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """协议层 `cs_list` 的返回值形状（列是 `(平台键, 官方中文标题)` 元组）。"""
    if rows is None:
        rows = {'detail': CS_DETAIL_ROWS, 'diversion': CS_DIVERSION_ROWS,
                'evaluation': CS_EVALUATION_ROWS}[kind]
    return {
        'kind': kind,
        'rows': rows,
        'columns': datacenter_proto.CS_LIST_COLUMNS[kind],
        'total': total,
        'found_key': 'list',
        'real_date_range': ['2026-10-13', '2026-10-13'],
        'date_type': datacenter_proto.DATE_TYPE_DAY,
        'dates': [],
        'raw': {},
        'errors': errors or {},
    }


class SessionExpiredError(RuntimeError):
    """与协议层同名：`common._is_session_expired` 按**类名**认登录态过期，不看文案。"""


class FakeXianyuClient:
    """假协议层客户端：数据组只用到 `mtop`，这里只记调用、不联网。"""

    def __init__(self) -> None:
        self.account_id = ''
        # 生产里 `cookie_id` 与 `account_id` 同值（`client.py:239/248`），而
        # `common._note_session_expired` **优先**用 `cookie_id` 定位账号，所以要留着。
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        raise AssertionError('假客户端的 mtop 不该被走到（协议层被替换了）')


class FakeMtopClient:
    """真协议层用的假 client：`mtop` 按 api 回话或抛错（形状 = 真机信封，不联网）。"""

    def __init__(self, answers: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.account_id = ''
        self.cookie_id = ''
        self.calls: List[tuple] = []

    async def mtop(self, api: str, data: Any = None, extra_headers: Any = None) -> Any:
        self.calls.append((api, data))
        error = self.errors.get(api)
        if error is not None:
            raise error
        return self.answers.get(api, {})


class FakeDataCenterProtocol:
    """假协议层：按函数名回话，记录 `(client, start, end, seller_id)`。"""

    def __init__(self, payloads: Optional[Dict[str, Any]] = None,
                 errors: Optional[Dict[str, BaseException]] = None):
        self.payloads = payloads or {}
        self.errors = errors or {}
        self.calls: List[tuple] = []

    def _answer(self, name: str, *args: Any) -> Any:
        self.calls.append((name,) + args)
        error = self.errors.get(name)
        if error is not None:
            raise error
        return self.payloads.get(name)

    async def overview(self, client: Any, start: str = '', end: str = '',
                       seller_id: str = '') -> Any:
        return self._answer('overview', client, start, end, seller_id)

    async def fans(self, client: Any, start: str = '', end: str = '',
                   seller_id: str = '') -> Any:
        return self._answer('fans', client, start, end, seller_id)

    async def customer_service(self, client: Any, start: str = '', end: str = '',
                               seller_id: str = '') -> Any:
        return self._answer('customer_service', client, start, end, seller_id)

    async def commodity(self, client: Any, start: str = '', end: str = '', seller_id: str = '',
                        category_info: Any = None, item_id: str = '', item_name: str = '',
                        is_dp_stk: str = '', mkt_act_id: str = '', page: int = 1,
                        page_size: int = 20, order_by: str = '', order: str = '') -> Any:
        return self._answer('commodity', client, start, end, seller_id, category_info,
                            item_id, item_name, is_dp_stk, mkt_act_id, page, page_size,
                            order_by, order)

    async def card(self, client: Any, kind: str = 'trade', start: str = '', end: str = '',
                   seller_id: str = '') -> Any:
        """与真协议层同样**从 start/end 推导** `date_type` / `dates`。

        假协议层若回一份写死的 `dates`，服务层就会以为「调用方没给日期」，
        于是区间显示成今天 —— 那种假绿会把真 bug 藏起来。
        """
        answer = self._answer('card', client, kind, start, end, seller_id)
        if isinstance(answer, dict):
            date_type, dates = datacenter_proto.card_dates(start, end)
            answer = dict(answer)
            answer['date_type'] = date_type
            answer['dates'] = list(dates)
        return answer

    async def cs_list(self, client: Any, kind: str = 'detail', start: str = '',
                      end: str = '', seller_id: str = '', order_by: str = '',
                      order: str = '', page: int = 1, page_size: int = 10) -> Any:
        """与真协议层同样**从 start/end 推导** `date_type` / `dates`（理由同 `card`）。"""
        answer = self._answer('cs_list', client, kind, start, end, seller_id, order_by,
                              order, page, page_size)
        if isinstance(answer, dict):
            date_type, dates = datacenter_proto.commodity_dates(start, end)
            answer = dict(answer)
            answer['date_type'] = date_type
            answer['dates'] = list(dates)
            # 与真协议层一致：**按请求的 kind 给标题与列**（照抄一份死 payload 会让
            # 「三张表各自的列」永远相等 —— 那种假绿会把真 bug 藏起来）
            answer['kind'] = kind
            answer['columns'] = datacenter_proto.CS_LIST_COLUMNS[kind]
        return answer

    def args_of(self, name: str) -> tuple:
        for call in self.calls:
            if call[0] == name:
                return call[1:]
        raise AssertionError(f'没有调用 {name}，实际调用: {[c[0] for c in self.calls]}')

    def names(self) -> List[str]:
        return [call[0] for call in self.calls]


def install_fake_protocol(protocol: FakeDataCenterProtocol) -> FakeXianyuClient:
    """注入两个假协议模块：`xianyu.client`（拿客户端）与 `xianyu.datacenter`（数据组）。"""
    client = FakeXianyuClient()
    client_module = types.ModuleType('backend.app.xianyu.client')

    def factory(account_id: str) -> FakeXianyuClient:
        # `common._note_session_expired` **优先** `client.cookie_id`、退回 `account_id`
        # （common.py:356）定位要标记的账号；生产里两者同值（client.py:239/248）。
        client.account_id = account_id
        client.cookie_id = account_id
        return client

    client_module.get_client = factory  # type: ignore[attr-defined]
    client_module.XianyuClient = FakeXianyuClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = client_module

    module = types.ModuleType('backend.app.xianyu.datacenter')
    module.overview = protocol.overview  # type: ignore[attr-defined]
    module.fans = protocol.fans  # type: ignore[attr-defined]
    module.customer_service = protocol.customer_service  # type: ignore[attr-defined]
    module.commodity = protocol.commodity  # type: ignore[attr-defined]
    module.card = protocol.card  # type: ignore[attr-defined]
    module.cs_list = protocol.cs_list  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.datacenter'] = module
    return client


def remove_fake_protocol() -> None:
    sys.modules.pop('backend.app.xianyu.client', None)
    sys.modules.pop('backend.app.xianyu.datacenter', None)


def install_fake_client(account_id: str = ACC, answers: Optional[Dict[str, Any]] = None,
                        errors: Optional[Dict[str, BaseException]] = None) -> FakeMtopClient:
    """只注入假 `xianyu.client`，**真协议层 `xianyu.datacenter` 照常参与**。

    端到端口径用例必须走真协议层：`install_fake_protocol` 把协议层整个换掉，
    于是 `_collect` / `_begin_from` / 三个入口函数的真实行为在这组测试里一次都没被
    跑到过 ——「真机上 `begin_time` 永远是 None」就是这么漏掉的。
    """
    client = FakeMtopClient(answers=answers, errors=errors)
    client.account_id = account_id
    client.cookie_id = account_id
    module = types.ModuleType('backend.app.xianyu.client')
    module.get_client = (lambda _account_id: client)  # type: ignore[attr-defined]
    module.XianyuClient = FakeMtopClient  # type: ignore[attr-defined]
    sys.modules['backend.app.xianyu.client'] = module
    return client


def seed_account(account_id: str = ACC, cookie: str = 'cookie-ok',
                 login_state: str = 'active') -> None:
    db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
               'VALUES(?,?,?,?)', (account_id, f'账号 {account_id}', 1, 1))
    db.execute('INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) '
               'VALUES(?,?,?)', (account_id, cookie, login_state))


def fans_payload(insights: Any = FANS_INSIGHTS_PAYLOAD,
                 errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """把「平台原始响应」拼成协议层 `fans()` 的返回形状（**真解析函数参与**）。

    与 `commodity_payload()` 同一个理由：假协议层若自己编一份 `insights_breakdown`，
    解析口径就没人测了。
    """
    return {
        'metrics': datacenter_proto.extract_metrics(FANS_PAYLOAD, datacenter_proto.FANS_METRICS),
        'insights': FANS_PAYLOAD['insights'],
        'insights_breakdown': datacenter_proto.parse_fans_insights(insights),
        'real_date_range': None,
        'suspect_keys': [],
        'errors': errors or {},
    }


def full_protocol(**overrides: Any) -> FakeDataCenterProtocol:
    payloads = {
        'overview': {'metrics': datacenter_proto.extract_metrics(OVERVIEW_PAYLOAD, datacenter_proto.OVERVIEW_METRICS),
                     'begin_time': '20260201',
                     'real_date_range': ['2026-10-04', '2026-10-10'],
                     'suspect_keys': [], 'errors': {}},
        'fans': fans_payload(),
        'customer_service': {'metrics': datacenter_proto.extract_metrics(CS_PAYLOAD, datacenter_proto.CS_METRICS),
                             'real_date_range': None, 'suspect_keys': [], 'errors': {}},
        'commodity': commodity_payload(),
        'card': card_payload(),
        'cs_list': cs_list_payload(),
    }
    payloads.update(overrides)
    return FakeDataCenterProtocol(payloads)


class DataCenterApiTest(unittest.TestCase):
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
        for table in ('accounts', 'account_credentials', 'operation_logs', 'risk_state'):
            db.execute(f'DELETE FROM {table}')
        datacenter_service.clear_cache()
        self.proto = full_protocol()
        self.fake_client = install_fake_protocol(self.proto)
        # 自挂本模块的路由：行为用例不依赖 main.py 是否已经挂上
        # （真实挂载由 Lead 在 main.py 里完成，见 docs/contracts/datacenter.md）。
        app = FastAPI()
        app.include_router(datacenter_api.router)
        self.app_ctx = TestClient(app)
        self.client = self.app_ctx.__enter__()
        self.user_id = int(db.execute(
            "INSERT INTO users(username, password_hash, is_admin) VALUES('dcapi','x',1)"))
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

    def fail_envelope(self, path: str, query: str = '', status: int = 400) -> Dict[str, Any]:
        resp = self.get(path, query)
        self.assertEqual(resp.status_code, status, resp.text)
        body = resp.json()
        self.assertFalse(body.get('success'), body)
        return body

    # -- 只读 ------------------------------------------------------------

    def test_routes_are_read_only(self):
        """对**真 app**（`main.create_app()`）验只读：自建 FastAPI 防不住挂载方式变化。

        先摘掉假协议层：`main` 在 import 时就会 `create_app()`，而 `services.finance`
        这类模块在**模块级**读 `xianyu.client.BILL_CODE_INCOME` —— 假客户端没这个常量。
        """
        seed_account()
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要
        with TestClient(app_main.create_app()) as real:
            for path in (f'{BASE}/{ACC}/overview', f'{BASE}/{ACC}/fans',
                         f'{BASE}/{ACC}/customer-service', f'{BASE}/{ACC}/commodity',
                         f'{BASE}/{ACC}/card/seller', f'{BASE}/{ACC}/cs/detail'):
                for method in ('post', 'put', 'patch', 'delete'):
                    resp = getattr(real, method)(path, headers=self.headers)
                    self.assertEqual(resp.status_code, 405, f'{method} {path}: {resp.text}')
                    self.assertFalse(resp.json().get('success'), f'{method} {path}: {resp.text}')
        self.assertEqual(self.proto.calls, [], '只读探测不该打平台')

    def test_requires_login(self):
        seed_account()
        resp = self.client.get(f'{BASE}/{ACC}/overview')
        self.assertIn(resp.status_code, (401, 403), resp.text)

    # -- 数据总览 --------------------------------------------------------

    def test_overview_metrics_pass_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(data['period'], {'from': '20261004', 'to': '20261010'})
        self.assertEqual(data['kind'], 'overview')
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['begin_time'], '20260201')
        self.assertEqual(data['metrics']['exposure'], 1971.0)
        self.assertEqual(data['metrics']['exposure_visitors'], 1298.0)
        self.assertEqual(data['metrics']['visits'], 124.0)
        self.assertEqual(data['metrics']['visit_visitors'], 47.0)
        self.assertEqual(data['metrics']['browse'], 97.0)
        self.assertEqual(data['metrics']['browse_visitors'], 45.0)
        self.assertEqual(data['metrics']['online_items'], 45.0)
        self.assertEqual(data['metrics']['inquiry_visitors'], 8.0)
        self.assertAlmostEqual(data['metrics']['inquiry_reply_rate_3min'], 33.33)
        self.assertEqual(data['real_date_range'], ['2026-10-04', '2026-10-10'])
        self.assertEqual(data['warnings'], [])
        # 契约键一个都不能少，哪怕平台没给
        self.assertEqual(set(data['metrics']), set(datacenter_proto.OVERVIEW_METRICS))

    def test_missing_metrics_stay_null_never_zero(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': datacenter_proto.extract_metrics(OVERVIEW_PARTIAL, datacenter_proto.OVERVIEW_METRICS),
            'begin_time': None, 'real_date_range': None, 'suspect_keys': [], 'errors': {},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['exposure'], 1971.0)
        self.assertEqual(data['metrics']['visit_visitors'], 47.0)
        for key in ('visits', 'browse', 'online_items', 'inquiry_visitors'):
            self.assertIsNone(data['metrics'][key], f'{key} 必须是 None，不能是 0')
        self.assertIsNone(data['begin_time'])

    def test_platform_zero_is_kept(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': datacenter_proto.extract_metrics(OVERVIEW_ZERO, datacenter_proto.OVERVIEW_METRICS),
            'begin_time': None, 'real_date_range': None, 'suspect_keys': [], 'errors': {},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['exposure'], 0.0, '平台真值 0 必须保留')
        self.assertEqual(data['metrics']['visits'], 0.0)
        self.assertIsNone(data['metrics']['browse'], '没给的仍然是 None')

    def test_partial_failure_keeps_other_metrics_and_warns(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': {'visits': 124.0, 'exposure': None, 'exposure_visitors': None,
                        'visit_visitors': None, 'browse': None, 'browse_visitors': None,
                        'online_items': None, 'inquiry_visitors': None,
                        'inquiry_reply_rate_3min': None},
            'begin_time': '20260201', 'real_date_range': None, 'suspect_keys': [],
            'errors': {'overview': 'XianyuError: MTOP 调用失败 '
                                   '[mtop...singleuser.browse.summary]: '
                                   'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['visits'], 124.0)
        self.assertIsNone(data['metrics']['exposure'])
        self.assertEqual(len(data['warnings']), 1)
        self.assertIn('没有「数据」权限', data['warnings'][0])
        # warnings 是给用户看的：只许出现中文业务名，不许漏 `overview` 这种内部调用名
        self.assertIn('数据总览', data['warnings'][0])
        self.assertNotIn('overview', data['warnings'][0])

    def test_all_calls_failed_is_502(self):
        seed_account()
        self.proto.errors['overview'] = RuntimeError('FAIL_SYS_BIZPARAM_MISSED::缺少业务参数dateType')
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('dateType', body['message'])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/nope/overview', status=404)
        self.assertEqual(body['code'], 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_missing_cookie_is_409_and_never_calls_platform(self):
        db.execute('INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) '
                   'VALUES(?,?,?,?)', (ACC, '账号', 1, 1))
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        self.assertEqual(self.proto.calls, [], '没 Cookie 时一次平台都不许打')

    def test_session_expired_is_reported_as_need_relogin(self):
        seed_account()
        self.proto.errors['overview'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['code'], 'verification_required')
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin', '登录态过期必须回写库')

    def test_session_expired_writes_back_via_cookie_id(self):
        """钉住 `_BoundProtocol` 的意义：回写走 client 的 **`cookie_id`**。

        `common._note_session_expired`（common.py:356）是 `cookie_id` 优先、`account_id`
        退回 —— 把 account_id 清空，只有 cookie_id 这条路能写回；而协议**模块**身上
        两个都没有，所以直接把模块传给 `call_method` 时回写必然失败（真机表现：协议
        报 Session过期、界面还显示已登录）。
        """
        seed_account()
        self.fake_client.cookie_id = ACC
        self.fake_client.account_id = ''
        self.proto.errors['overview'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=409)
        self.assertEqual(body['login_state'], 'need_relogin')
        row = db.query_one('SELECT login_state FROM account_credentials WHERE account_id = ?', (ACC,))
        self.assertEqual(row['login_state'], 'need_relogin')

    def test_real_protocol_permission_denied_gives_null_not_502(self):
        """真机场景端到端（**真协议层** + 假 mtop）：除 `get.begin.time` 外全是「无权限」。

        三个页面都必须 200 + 一屏 `None` + 一句人话，**不能**是 502 —— 真机上 `fans`
        只有 2 个调用、`cs` 只有 1 个，按「全坏即抛」它们会**永远** 502，用户连
        「这个账号没有数据权限」都看不到。顺带端到端锁住 `begin_time`：
        `_mtop` 会剥掉外层信封，认不出裸日期的话这个字段真机上恒为 None。
        """
        seed_account()
        remove_fake_protocol()
        install_fake_client(
            answers={datacenter_proto.BEGIN_TIME_API: BEGIN_TIME_REAL},
            errors={api: RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
                    for api in (datacenter_proto.OVERVIEW_API, datacenter_proto.OVERVIEW_ITEM_API,
                                datacenter_proto.FANS_API, datacenter_proto.FANS_INSIGHTS_API,
                                datacenter_proto.CS_OVERVIEW_API)},
        )
        overview = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(overview['begin_time'], '20260201',
                         '_mtop 剥掉信封后拿到的是裸日期，也必须认得出')
        self.assertTrue(all(value is None for value in overview['metrics'].values()),
                        overview['metrics'])
        self.assertEqual(len(overview['warnings']), 2, overview['warnings'])
        self.assertIn('没有「数据」权限', overview['warnings'][0])
        for path in (f'{BASE}/{ACC}/fans', f'{BASE}/{ACC}/customer-service'):
            data = self.ok_data(path)
            self.assertTrue(all(value is None for value in data['metrics'].values()), path)
            self.assertTrue(data['warnings'], f'{path} 必须解释为什么是 -')
            self.assertIn('没有「数据」权限', data['warnings'][0], path)
        cs = self.ok_data(f'{BASE}/{ACC}/customer-service')
        self.assertIn('客服数据', cs['warnings'][0], 'warnings 不许漏内部调用名')

    def test_invalid_dates_are_400(self):
        seed_account()
        for query in ('?start=2026-10-04&end=20261010', '?start=20261010&end=20261004',
                      '?start=2026101&end=20261010', '?start=abcdefgh&end=20261010'):
            body = self.fail_envelope(f'{BASE}/{ACC}/overview', query, status=400)
            self.assertEqual(body['code'], 'invalid_fields', query)
        self.assertEqual(self.proto.calls, [], '日期不合法时不该打平台')

    def test_default_period_is_recent_seven_days(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        today = datetime.now().date()
        self.assertEqual(data['period']['to'], today.strftime('%Y%m%d'))
        self.assertEqual(data['period']['from'],
                         (today - timedelta(days=6)).strftime('%Y%m%d'))

    def test_dates_and_account_are_passed_to_protocol(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        client, start, end, seller_id = self.proto.args_of('overview')
        self.assertEqual((start, end, seller_id), ('20261004', '20261010', ACC))
        self.assertIsInstance(client, FakeXianyuClient)

    # -- 粉丝 / 客服 -----------------------------------------------------

    def test_fans_shape(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/fans', '?start=20261004&end=20261010')
        self.assertEqual(data['kind'], 'fans')
        self.assertEqual(data['metrics']['fans_total'], 128.0)
        self.assertEqual(data['metrics']['fans_new'], 6.0)
        self.assertEqual(data['metrics']['fans_lost'], 1.0)
        self.assertEqual(data['metrics']['fans_active'], 33.0)
        self.assertEqual(data['metrics']['fans_orders'], 4.0)
        self.assertEqual(data['metrics']['fans_buyers'], 3.0)
        self.assertEqual(data['insights'], FANS_PAYLOAD['insights'])
        self.assertEqual(set(data['metrics']), set(datacenter_proto.FANS_METRICS))

    def test_customer_service_shape(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/customer-service')
        self.assertEqual(data['kind'], 'customer_service')
        self.assertEqual(data['metrics']['inquiry_visitors'], 8.0)
        self.assertEqual(data['metrics']['inquiry_count'], 11.0)
        self.assertAlmostEqual(data['metrics']['reply_rate_3min'], 33.33)
        self.assertEqual(data['metrics']['avg_reply_seconds'], 42.0)
        self.assertEqual(data['metrics']['unreplied_count'], 2.0)
        self.assertAlmostEqual(data['metrics']['satisfaction'], 98.5)
        self.assertEqual(set(data['metrics']), set(datacenter_proto.CS_METRICS))

    def test_protocol_receives_three_positional_args(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/fans', '?start=20261004&end=20261010')
        self.ok_data(f'{BASE}/{ACC}/customer-service', '?start=20261004&end=20261010')
        self.assertEqual(self.proto.args_of('fans')[1:], ('20261004', '20261010', ACC))
        self.assertEqual(self.proto.args_of('customer_service')[1:], ('20261004', '20261010', ACC))

    # -- 缓存 ------------------------------------------------------------

    def test_cache_avoids_second_platform_call(self):
        seed_account()
        first = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        second = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(first, second)
        self.assertEqual(self.proto.names(), ['overview'], '5 分钟内不该重复打平台')
        datacenter_service.clear_cache()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(self.proto.names(), ['overview', 'overview'])

    def test_cache_key_includes_dates(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261005&end=20261010')
        self.assertEqual(self.proto.names(), ['overview', 'overview'])

    def test_failure_is_not_cached(self):
        seed_account()
        self.proto.errors['overview'] = RuntimeError('boom')
        self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.proto.errors.pop('overview')
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['metrics']['exposure'], 1971.0, '失败结果绝不能进缓存')

    def test_metrics_missing_from_protocol_is_protocol_error(self):
        """假成功不许混过去：协议层不给 `metrics` 就是 502 `protocol_error`，
        不是 200 + 一屏 `-` 让用户以为今天真的没人来。"""
        seed_account()
        self.proto.payloads['overview']['metrics'] = None
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('指标字段', body['message'])

    def test_empty_metrics_is_protocol_error(self):
        """`metrics: {}` 也是协议异常 —— `{}` 是 dict，`isinstance(..., dict)` 挡不住它；
        放过去就是 200 + 0 个指标，会被读成「今天真的一个数据都没有」。"""
        seed_account()
        self.proto.payloads['overview']['metrics'] = {}
        body = self.fail_envelope(f'{BASE}/{ACC}/overview', status=502)
        self.assertEqual(body['code'], 'protocol_error')
        self.assertIn('指标字段', body['message'])

    def test_real_protocol_cs_exposes_date_range_and_suspect_keys(self):
        """真协议层 + 假 mtop：客服页的 `real_date_range` / `unrecognized_keys` 必须透出。

        锁的是取值标签：`_collect` 只按 label 写 `raw`，CS 曾误读 `raw['overview']`
        （本页标签是 `'cs'`），于是这两个字段永远空转 —— 而 CS 页 6 个指标的别名全是
        假设，自证通道一断，别名认错了没人会发现。
        """
        seed_account()
        remove_fake_protocol()
        install_fake_client(answers={
            datacenter_proto.CS_OVERVIEW_API: {'data': {
                'csInquiryUserCount': 8, 'weirdUnknownCount': 3,
                'realDateRange': ['2026-10-04', '2026-10-10'],
            }},
        })
        data = self.ok_data(f'{BASE}/{ACC}/customer-service', '?start=20261004&end=20261010')
        self.assertEqual(data['real_date_range'], ['2026-10-04', '2026-10-10'],
                         '取值标签写错时这个字段永远是 None')
        self.assertIn('weirdUnknownCount', data.get('unrecognized_keys', []))
        self.assertIn('csInquiryUserCount', data.get('unrecognized_keys', []))

    def test_bound_protocol_account_id_falls_back_to_persist_account_id(self):
        """`_BoundProtocol.account_id` 不能恒为 ''：真 `XianyuClient` 没有 `account_id`
        （只有 `cookie_id` 与 `persist_account_id`，client.py），少一层退回，掉线回写
        就会被静默跳过。"""
        class Client:
            persist_account_id = 'acc-persist'
            cookie_id = ''

        bound = datacenter_service._BoundProtocol(object(), Client())
        self.assertEqual(bound.account_id, 'acc-persist')
        self.assertEqual(bound.cookie_id, '')
        with self.assertRaises(AttributeError):
            bound.cookie_str  # 协议函数才转发到模块，client 的其它属性不转发

    def test_cache_is_read_before_credentials(self):
        """缓存**先于**建客户端读：命中就直接返回 —— 既不付建客户端的代价，
        本地登录态变脏也不会把 5 分钟内的成功结果盖成 409。"""
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        db.execute("UPDATE account_credentials SET login_state='need_relogin' WHERE account_id=?",
                   (ACC,))
        data = self.ok_data(f'{BASE}/{ACC}/overview', '?start=20261004&end=20261010')
        self.assertEqual(data['metrics']['exposure'], 1971.0, '缓存结果不该被本地登录态盖掉')
        self.assertEqual(self.proto.names(), ['overview'], '命中缓存就不该再碰平台')

    def test_missing_protocol_layer_is_503(self):
        seed_account()
        with mock.patch.object(datacenter_service.common, 'protocol_module', return_value=None):
            value, failure = datacenter_service._call('overview', object())
        self.assertIsNone(value)
        self.assertEqual(failure['code'], 'xianyu_not_ready')
        self.assertIn('datacenter', failure['message'])

    def test_unrecognized_metric_keys_are_reported(self):
        seed_account()
        self.proto.payloads['overview'] = {
            'metrics': datacenter_proto.extract_metrics(OVERVIEW_PAYLOAD, datacenter_proto.OVERVIEW_METRICS),
            'begin_time': None, 'real_date_range': None,
            'suspect_keys': ['newFansCount'], 'errors': {},
        }
        data = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertEqual(data['unrecognized_keys'], ['newFansCount'])


class DataCenterProtocolTest(unittest.TestCase):
    """协议层纯函数：别名识别、日期入参、起始日解析。**不联网、不建库**。"""

    def test_date_data_without_dates_means_today(self):
        self.assertEqual(datacenter_proto.date_data(), {'dateType': 'recent1d'})
        self.assertEqual(datacenter_proto.date_data(None, None, 'acc1'),
                         {'dateType': 'recent1d', 'selectedSellerId': 'acc1'})

    def test_date_data_single_day_still_carries_the_date(self):
        """单日（start == end）也必须带日期：只发 `recent1d` 平台只能回**今天**，
        而 `period` 却标成用户要的那天 —— 对外撒谎。"""
        self.assertEqual(datacenter_proto.date_data('20261010', '20261010'),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010']})
        self.assertEqual(datacenter_proto.date_data('20261010', '20261010', 'acc1'),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010'], 'selectedSellerId': 'acc1'})

    def test_date_data_half_range_is_completed_from_the_other_end(self):
        self.assertEqual(datacenter_proto.date_data('20261010', ''),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010']})
        self.assertEqual(datacenter_proto.date_data(None, '20261010'),
                         {'dateType': 'month', 'ms': '20261010',
                          'date': ['20261010', '20261010']})

    def test_date_data_custom_range_uses_month(self):
        payload = datacenter_proto.date_data('20261004', '20261010', 'acc1')
        self.assertEqual(payload['dateType'], 'month')
        self.assertEqual(payload['ms'], '20261004')
        self.assertEqual(payload['date'], ['20261004', '20261010'])
        self.assertEqual(payload['selectedSellerId'], 'acc1')

    def test_extract_metrics_walks_nested_payload(self):
        metrics = datacenter_proto.extract_metrics(OVERVIEW_PAYLOAD,
                                                   datacenter_proto.OVERVIEW_METRICS)
        self.assertEqual(metrics['exposure'], 1971.0)
        self.assertEqual(metrics['inquiry_reply_rate_3min'], 33.33)
        self.assertEqual(metrics['online_items'], 45.0)

    def test_extract_metrics_missing_is_none(self):
        metrics = datacenter_proto.extract_metrics({'data': {'foo': 1}},
                                                   datacenter_proto.OVERVIEW_METRICS)
        self.assertEqual(set(metrics), set(datacenter_proto.OVERVIEW_METRICS))
        for key, value in metrics.items():
            self.assertIsNone(value, f'{key} 认不出时必须是 None')

    def test_extract_metrics_keeps_platform_zero(self):
        metrics = datacenter_proto.extract_metrics({'data': {'exposureNum': 0, 'visitCount': 0}},
                                                   datacenter_proto.OVERVIEW_METRICS)
        self.assertEqual(metrics['exposure'], 0.0)
        self.assertEqual(metrics['visits'], 0.0)

    def test_find_number_ignores_booleans_and_strings(self):
        self.assertIsNone(datacenter_proto.find_number({'data': {'exposureNum': True}},
                                                       datacenter_proto.METRIC_ALIASES['exposure']))
        self.assertIsNone(datacenter_proto.find_number({'data': {'exposureNum': '1971'}},
                                                       datacenter_proto.METRIC_ALIASES['exposure']))
        self.assertEqual(datacenter_proto.find_number({'data': {'exposureNum': 1971}},
                                                      datacenter_proto.METRIC_ALIASES['exposure']), 1971.0)

    def test_begin_from_real_response(self):
        self.assertEqual(datacenter_proto._begin_from(BEGIN_TIME_REAL), '20260201')
        self.assertIsNone(datacenter_proto._begin_from({'code': 'success', 'data': 'x'}))
        self.assertIsNone(datacenter_proto._begin_from(None))

    def test_begin_from_accepts_stripped_payload(self):
        """`_mtop` 会剥掉外层信封，`begin_time` 回来的是**裸日期字符串**；
        只认 dict 的话真机上这个字段恒为 None。"""
        self.assertEqual(datacenter_proto._begin_from('20260201'), '20260201')
        self.assertIsNone(datacenter_proto._begin_from(''))

    def test_real_date_range(self):
        self.assertEqual(datacenter_proto._real_date_range(OVERVIEW_PAYLOAD),
                         ['2026-10-04', '2026-10-10'])
        self.assertIsNone(datacenter_proto._real_date_range({'data': {}}))

    def test_suspect_keys_flags_metric_like_names(self):
        found = datacenter_proto.suspect_keys({'data': {'newFansCount': 3, 'title': 'x'}},
                                              datacenter_proto.OVERVIEW_METRICS)
        self.assertIn('newFansCount', found)
        self.assertNotIn('title', found)

    def test_collect_partial_failure_keeps_others(self):
        class Client:
            async def mtop(self, api: str, data: Any = None) -> Any:
                if api == datacenter_proto.OVERVIEW_API:
                    raise RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')
                return {'data': {'onlineItemCount': 45}}

        metrics, raw, errors = asyncio.run(datacenter_proto._collect(Client(), (
            ('overview', datacenter_proto.OVERVIEW_API, {}),
            ('items', datacenter_proto.OVERVIEW_ITEM_API, {}),
        ), datacenter_proto.OVERVIEW_METRICS))
        self.assertEqual(metrics['online_items'], 45.0)
        self.assertIsNone(metrics['exposure'])
        self.assertIn('overview', errors)
        self.assertIn('items', raw)

    def test_collect_all_failed_raises_most_severe_exception(self):
        """全坏才抛；混合失败时 **SessionExpired 优先** —— 它要触发 409 + 回写 need_relogin，
        被排在前面的业务错吞掉的话，掉线的账号界面还显示已登录。"""
        class Client:
            async def mtop(self, api: str, data: Any = None) -> Any:
                raise (SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
                       if api == datacenter_proto.OVERVIEW_ITEM_API
                       else RuntimeError(f'boom {api}'))

        # SessionExpired 排在**调用顺序的最后**，仍必须是它
        with self.assertRaises(SessionExpiredError) as ctx:
            asyncio.run(datacenter_proto._collect(Client(), (
                ('overview', datacenter_proto.OVERVIEW_API, {}),
                ('items', datacenter_proto.OVERVIEW_ITEM_API, {}),
            ), datacenter_proto.OVERVIEW_METRICS))
        # 类型已由 assertRaises 钉住；文案断言匹配**平台原文**，不写死我们的类名
        self.assertIn('FAIL_SYS_SESSION_EXPIRED', str(ctx.exception))

    def test_collect_all_permission_denied_is_not_fatal(self):
        """真机现状（无数据权限）：**全失败但都是「无权限」不算致命**。

        按「全坏即抛」，`fans`（2 个调用）和 `cs`（1 个调用）会**永远** 502，用户连
        「这个账号没有数据权限」都看不到；页面必须拿到 200 + 一屏 None + 一句人话。
        """
        class Client:
            async def mtop(self, api: str, data: Any = None) -> Any:
                raise RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')

        metrics, raw, errors = asyncio.run(datacenter_proto._collect(Client(), (
            ('overview', datacenter_proto.OVERVIEW_API, {}),
            ('items', datacenter_proto.OVERVIEW_ITEM_API, {}),
        ), datacenter_proto.OVERVIEW_METRICS))
        for key in datacenter_proto.OVERVIEW_METRICS:
            self.assertIsNone(metrics[key], f'{key} 拿不到必须是 None，不许是 0')
        self.assertEqual(set(errors), {'overview', 'items'})

    def test_service_warnings_translate_unauthorized(self):
        warnings = datacenter_service._warnings(
            {'overview': 'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        self.assertEqual(len(warnings), 1)
        self.assertIn('没有「数据」权限', warnings[0])

    def test_card_permission_denied_returns_errors_instead_of_raising(self):
        """真协议层：无权限**不许抛**（抛了界面上就是四个红框 + 一屏 mtop 原文）。

        这条是**真机抓出来的**：假协议层早就按「回 `errors`」写，服务层测试因此一直绿，
        而真协议层当时是直接抛 ⇒ 打包版上四张卡全 502、原文糊在卡片里，
        同一页的核心指标却是「人话 + 一屏 -」。所以这条必须在**协议层**用真函数钉住。
        """
        client = FakeMtopClient(errors={
            datacenter_proto.CARD_APIS['trade']:
                RuntimeError('FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')})
        parsed = asyncio.run(datacenter_proto.card(client, 'trade', '', '', ''))
        self.assertFalse(parsed['found'])
        self.assertEqual(parsed['metrics'], [])
        self.assertIn('trade', parsed['errors'])
        self.assertIn('UNAUTHORIZED', parsed['errors']['trade'])

    def test_card_other_failures_still_raise(self):
        """链路/风控/登录态这类故障**照抛** —— 它们必须变成 502/409 说实话。"""
        client = FakeMtopClient(errors={
            datacenter_proto.CARD_APIS['refund']: RuntimeError('FAIL_SYS_TRAFFIC_LIMIT::挤爆了')})
        with self.assertRaises(RuntimeError):
            asyncio.run(datacenter_proto.card(client, 'refund', '', '', ''))

    def test_card_session_expired_still_raises(self):
        """登录态过期必须照抛：`common.call_failed` 按类名认它 → 409 + 回写 need_relogin。"""
        client = FakeMtopClient(errors={
            datacenter_proto.CARD_APIS['seller']:
                SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')})
        with self.assertRaises(SessionExpiredError):
            asyncio.run(datacenter_proto.card(client, 'seller', '', '', ''))

    def test_service_warnings_use_chinese_labels_not_internal_names(self):
        """给用户看的话里只许有中文业务名，不许漏 `overview` 这种内部调用名。"""
        warnings = datacenter_service._warnings(
            {'overview': 'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        self.assertIn('数据总览', warnings[0])
        self.assertNotIn('overview', warnings[0])

    def test_service_warnings_have_chinese_labels_for_cards(self):
        """四张卡片的错误标签就是 `kind`，也必须翻成中文（否则卡片上写 `trade`）。"""
        for kind, label in (('trade', '交易数据'), ('seller', '经营概况'),
                            ('repurchase', '复购'), ('refund', '退款概览')):
            warnings = datacenter_service._warnings(
                {kind: 'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
            self.assertIn(label, warnings[0], kind)
            self.assertNotIn(kind, warnings[0], kind)


class CommodityTest(DataCenterApiTest):
    """商品数据页（官方 `/seller-data/commodity`）的协议 + 服务 + HTTP 口径。

    这一页与另外三页不同：**列由平台定义**（`item.indicators`）、**分页排序由平台做**、
    默认日期是**今天**而不是最近 7 天。三条都容易被「想当然」写错，所以逐条钉住。
    """

    # -- 协议层：入参形状（官方 bundle 逐字） ----------------------------

    def test_request_matches_the_official_payload(self):
        payload = datacenter_proto.commodity_list_request(page=2, page_size=50)
        self.assertEqual(set(payload), {
            'selectedSellerId', 'tabType', 'dateRange', 'ms', 'dateType',
            'categoryInfoStr', 'itmName', 'itmId', 'isDpStk', 'mktActId',
            'page', 'pageSize', 'orderBy', 'order',
        })
        self.assertEqual(payload['tabType'], 'all')
        self.assertEqual(payload['dateType'], datacenter_proto.DATE_TYPE_DAY)
        self.assertEqual(payload['dateRange'], '')
        self.assertEqual(payload['ms'], '')
        self.assertEqual(payload['categoryInfoStr'], '[]')
        self.assertEqual((payload['page'], payload['pageSize']), (2, 50))

    def test_custom_range_uses_pipe_and_custom_date_type(self):
        date_type, dates = datacenter_proto.commodity_dates('20261001', '20261007')
        self.assertEqual(date_type, datacenter_proto.COMMODITY_DATE_CUSTOM)
        self.assertEqual(dates, ['20261001', '20261007'])
        payload = datacenter_proto.commodity_list_request(date_type=date_type, dates=dates)
        self.assertEqual(payload['dateRange'], '20261001|20261007')
        self.assertEqual(payload['ms'], '')

    def test_single_sided_range_fills_the_other_side(self):
        self.assertEqual(datacenter_proto.commodity_dates('20261001', ''),
                         (datacenter_proto.COMMODITY_DATE_CUSTOM, ['20261001', '20261001']))

    def test_category_info_is_a_json_string(self):
        self.assertEqual(datacenter_proto.category_info_str(None), '[]')
        self.assertEqual(datacenter_proto.category_info_str([]), '[]')
        self.assertEqual(datacenter_proto.category_info_str([{'id': 1}]), '[{"id":1}]')
        # 元素本身是 JSON 字符串（官方 `list.map(JSON.parse)` 的输入形态）
        self.assertEqual(datacenter_proto.category_info_str(['{"id":2}']), '[{"id":2}]')
        # 直接给 JSON 字符串也认
        self.assertEqual(datacenter_proto.category_info_str('[{"id":3}]'), '[{"id":3}]')
        # 给了个对象（不是数组）→ 明确报错，不猜
        with self.assertRaises(ValueError):
            datacenter_proto.category_info_str('{"id":1}')

    def test_parse_list_reads_the_real_shape(self):
        parsed = datacenter_proto.parse_commodity_list(COMMODITY_PAYLOAD)
        self.assertEqual(parsed['total'], 45.0)
        self.assertEqual(parsed['found_key'], 'list')
        self.assertEqual(len(parsed['rows']), 2)
        self.assertEqual(parsed['extend_info'], COMMODITY_PAYLOAD['extendInfo'])

    def test_missing_list_key_is_none_not_empty(self):
        parsed = datacenter_proto.parse_commodity_list(COMMODITY_NO_LIST)
        self.assertIsNone(parsed['rows'],
                          '认不出列表键必须是 None：空列表会被读成「这个卖家一件商品都没有」')
        self.assertEqual(parsed['found_key'], '')

    def test_parse_indicators_flattens_groups(self):
        parsed = datacenter_proto.parse_commodity_indicators(COMMODITY_INDICATORS)
        self.assertEqual([c['name'] for c in parsed['columns']],
                         ['ipvUv', 'ipvPv', 'payCnt', 'payAmt'])
        self.assertEqual(parsed['columns'][0]['title'], '商品访客数')
        self.assertEqual(parsed['columns'][0]['group'], '流量')
        self.assertEqual([g['name'] for g in parsed['groups']], ['流量', '成交'])
        self.assertEqual(len(parsed['groups'][1]['fields']), 2)

    def test_indicators_not_a_list_is_none(self):
        parsed = datacenter_proto.parse_commodity_indicators(COMMODITY_NO_COLUMNS)
        self.assertIsNone(parsed['columns'])
        self.assertIsNone(parsed['groups'])

    # -- HTTP + 服务层 ---------------------------------------------------

    def test_default_is_today_and_rows_pass_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/commodity')
        self.assertEqual(data['kind'], 'commodity')
        self.assertEqual(data['source'], 'platform')
        self.assertEqual(data['date_type'], datacenter_proto.DATE_TYPE_DAY)
        self.assertEqual(data['period'], {'from': '', 'to': ''})
        self.assertEqual(data['total'], 45.0)
        self.assertEqual((data['page'], data['page_size'], data['pages']), (1, 20, 3))
        row = data['rows'][0]
        self.assertEqual(row['itmName'], '测试商品 A')
        self.assertEqual(row['itmId'], '812345678901')
        # 平台列名原样透传：改名的话前端拿列定义（`name`）就取不到值
        self.assertEqual(row['ipvUv'], 12)
        self.assertIn('payAmt', row)
        self.assertEqual([c['name'] for c in data['columns']][:2], ['ipvUv', 'ipvPv'])
        self.assertEqual(data['warnings'], [])

    def test_platform_calls_use_the_page_args(self):
        seed_account()
        self.ok_data(f'{BASE}/{ACC}/commodity?start=20261001&end=20261007&page=2&size=10'
                     '&order_by=ipvUv&order=desc&item_name=%E6%B5%8B%E8%AF%95')
        args = self.proto.args_of('commodity')
        # (client, start, end, seller_id, category_info, item_id, item_name, is_dp_stk,
        #  mkt_act_id, page, page_size, order_by, order)
        self.assertEqual(args[1:4], ('20261001', '20261007', ACC))
        self.assertEqual(args[6], '测试')
        self.assertEqual(args[9:], (2, 10, 'ipvUv', 'desc'))

    def test_missing_list_gives_null_rows_and_a_warning(self):
        seed_account()
        self.proto = full_protocol(commodity=commodity_payload(rows=COMMODITY_NO_LIST))
        install_fake_protocol(self.proto)
        data = self.ok_data(f'{BASE}/{ACC}/commodity')
        self.assertIsNone(data['rows'])
        self.assertTrue(any('列表' in w for w in data['warnings']), data['warnings'])

    def test_missing_columns_gives_null_columns_and_a_warning(self):
        seed_account()
        self.proto = full_protocol(commodity=commodity_payload(columns=COMMODITY_NO_COLUMNS))
        install_fake_protocol(self.proto)
        data = self.ok_data(f'{BASE}/{ACC}/commodity')
        self.assertIsNone(data['columns'])
        self.assertTrue(any('指标列' in w for w in data['warnings']), data['warnings'])

    def test_permission_denied_is_200_with_a_human_warning(self):
        """真机现状：这个账号对 `datacompass` 一族一律「无权限」。

        「无权限」是平台给的有效答复，页面必须拿到 200 + 人话，而不是 502；
        而且**不能说成掉线**（用户会白扫一次码）。
        """
        seed_account()
        denied = 'XianyuError: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'
        self.proto = full_protocol(commodity=commodity_payload(
            rows=COMMODITY_NO_LIST, columns=COMMODITY_NO_COLUMNS,
            errors={'commodity': denied, 'columns': denied}))
        install_fake_protocol(self.proto)
        data = self.ok_data(f'{BASE}/{ACC}/commodity')
        self.assertIsNone(data['rows'])
        self.assertTrue(any('没有「数据」权限' in w for w in data['warnings']), data['warnings'])
        self.assertTrue(any('商品数据' in w for w in data['warnings']), data['warnings'])
        for warning in data['warnings']:
            self.assertNotIn('commodity', warning, 'warnings 里不许漏内部调用名')

    def test_size_over_the_limit_is_422_and_never_calls_the_platform(self):
        seed_account()
        resp = self.get(f'{BASE}/{ACC}/commodity', '?size=99')
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(self.proto.calls, [])

    def test_service_rejects_bad_args_with_400(self):
        """服务层自己也要拦（MCP 与其它调用方不走 FastAPI 的 `Query` 约束）。"""
        seed_account()
        for kwargs in ({'page': 0}, {'size': 99}, {'size': 'x'},
                       {'order': 'sideways'}, {'order': 'asc'}):
            result = datacenter_service.commodity(ACC, **kwargs)
            self.assertFalse(result.get('success'), kwargs)
            self.assertEqual(result.get('code'), 'invalid_fields', kwargs)
        self.assertEqual(self.proto.calls, [], '参数不合法时一次平台都不该打')

    def test_missing_cookie_is_409_and_never_calls_the_platform(self):
        seed_account(cookie='')
        body = self.fail_envelope(f'{BASE}/{ACC}/commodity', status=409)
        self.assertEqual(body.get('login_state'), 'need_relogin')
        self.assertEqual(self.proto.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/{ACC}/commodity', status=404)
        self.assertEqual(body.get('code'), 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_route_is_mounted_on_the_real_app(self):
        """挂载形状必须与测试里自挂的一致（自建 FastAPI 防不住挂载方式变化）。

        用 `openapi()` 而不是自己遍历 `app.routes`：子路由被 `include_router` 包在
        `_IncludedRouter` 里（`path` 是 None），手写遍历会**一条都走不到**、
        于是这条用例变成永远通过的空断言。
        """
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要

        paths = app_main.create_app().openapi().get('paths', {})
        self.assertIn(f'{BASE}/{{account_id}}/commodity', paths)
        self.assertIn('get', paths[f'{BASE}/{{account_id}}/commodity'])


class CardTest(DataCenterApiTest):
    """数据总览页四张「经营卡片」的协议 + 服务 + HTTP 口径。

    与前面几页的两点不同（都是官方页面行为）：默认区间是**今天**（`recent1d`，且**不带**
    `dateRange`）、入参用 `dateRange`（`|` 连接）而不是 `date`。指标名与中文标题都以平台
    为准 —— 平台新增一个我们不认识的指标名时，`label` 必须是 `None`（界面显示平台字段名），
    绝不能猜一个中文出来。
    """

    # -- 协议层：入参形状（官方 bundle 逐字） ----------------------------

    def test_request_matches_the_official_payload(self):
        payload = datacenter_proto.card_request(seller_id='221')
        self.assertEqual(payload, {'dateType': datacenter_proto.DATE_TYPE_DAY,
                                   'selectedSellerId': '221'})
        # 官方的 dateRange / ms 是 `undefined`：`JSON.stringify` 会让整键消失 ⇒ 不放键
        self.assertNotIn('dateRange', payload)
        self.assertNotIn('ms', payload)

    def test_custom_range_uses_pipe_and_month_uses_ms(self):
        payload = datacenter_proto.card_request(
            date_type=datacenter_proto.COMMODITY_DATE_CUSTOM,
            dates=['20261001', '20261013'], seller_id='221')
        self.assertEqual(payload['dateRange'], '20261001|20261013')
        self.assertNotIn('ms', payload)
        monthly = datacenter_proto.card_request(date_type=datacenter_proto.DATE_TYPE_MONTH,
                                                dates=['202610'], seller_id='221')
        self.assertEqual(monthly['ms'], '202610')
        self.assertNotIn('dateRange', monthly)

    def test_dates_default_to_today_without_a_range(self):
        self.assertEqual(datacenter_proto.card_dates('', ''),
                         (datacenter_proto.DATE_TYPE_DAY, []))
        self.assertEqual(datacenter_proto.card_dates('20261001', ''),
                         (datacenter_proto.COMMODITY_DATE_CUSTOM, ['20261001', '20261001']))

    def test_labels_come_from_the_official_tables(self):
        self.assertEqual(datacenter_proto.card_labels('refund')['crtRfdRate'], '退款发起率')
        self.assertEqual(datacenter_proto.card_labels('repurchase')['rptOrdCnt'], '复购订单数')
        # 单个账号口径用的是 v_t 那张表（支付笔数），不是全部账号的 h_t（支付订单数）
        self.assertEqual(datacenter_proto.card_labels('seller', '221')['payOrdCnt'], '支付笔数')
        self.assertEqual(datacenter_proto.card_labels('seller', '')['payOrdCnt'], '支付订单数')

    def test_parse_card_reads_banner_and_series(self):
        parsed = datacenter_proto.parse_card(
            CARD_SELLER_PAYLOAD, 'seller', datacenter_proto.card_labels('seller', '221'))
        self.assertEqual([m['key'] for m in parsed['banner']], ['vstPv', 'payAmt', 'brandNewMetric'])
        self.assertEqual(parsed['banner'][0]['value'], 124)
        self.assertEqual(parsed['banner'][0]['ratio'], '+3.2%')
        self.assertEqual(parsed['banner'][0]['ratio_format'], '12.4%')
        self.assertEqual(parsed['banner'][1]['cycle'], '日环比')
        self.assertEqual([s['key'] for s in parsed['series']], ['vstPv'])
        self.assertEqual(parsed['series'][0]['points'],
                         [{'date': '20261012', 'value': 100}, {'date': '20261013', 'value': 124}])
        self.assertEqual(parsed['promotion'], [{'bizId': 1, 'amountOfYuan': '33.58'}])
        self.assertEqual(parsed['real_date_range'], ['2026-10-13', '2026-10-13'])

    def test_unknown_metric_keeps_the_platform_name_without_a_label(self):
        parsed = datacenter_proto.parse_card(
            CARD_SELLER_PAYLOAD, 'seller', datacenter_proto.card_labels('seller', '221'))
        unknown = parsed['banner'][2]
        self.assertIsNone(unknown['label'], '不认识的指标名绝不能猜中文')
        self.assertEqual(unknown['platform_name'], 'brandNewMetric')
        self.assertEqual(unknown['value'], 7)

    def test_parse_card_refund_reads_flat_metric_objects(self):
        parsed = datacenter_proto.parse_card(
            CARD_REFUND_PAYLOAD, 'refund', datacenter_proto.card_labels('refund'))
        self.assertEqual([m['key'] for m in parsed['metrics']], ['crtRfdCnt', 'succRfdRate'])
        self.assertEqual(parsed['metrics'][0]['label'], '发起退款笔数')
        self.assertEqual(parsed['metrics'][0]['ratio'], '-1')
        self.assertEqual(parsed['metrics'][1]['value'], '50%')
        self.assertEqual(parsed['banner'], [])

    def test_parse_card_missing_body_is_not_found(self):
        parsed = datacenter_proto.parse_card({}, 'trade', datacenter_proto.card_labels('trade'))
        self.assertFalse(parsed['found'])
        self.assertEqual((parsed['metrics'], parsed['banner'], parsed['series']), ([], [], []))
        self.assertIsNone(parsed['real_date_range'])

    def test_trade_card_keeps_item_flow_transfer_data_apart(self):
        payload = {'data': {'payAmt': 12, 'itemFlowTransferData': {'a': 1}}}
        parsed = datacenter_proto.parse_card(payload, 'trade',
                                             datacenter_proto.card_labels('trade'))
        self.assertEqual([m['key'] for m in parsed['metrics']], ['payAmt'])
        self.assertEqual(parsed['extra'], {'itemFlowTransferData': {'a': 1}})

    def test_metric_entry_never_invents_a_value(self):
        entry = datacenter_proto._metric_entry('k', {'name': 'k'}, {})
        self.assertIsNone(entry['value'])
        self.assertIsNone(entry['ratio'])
        self.assertIsNone(entry['label'])

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            asyncio.get_event_loop().run_until_complete(
                datacenter_proto.card(FakeMtopClient(), 'nope'))

    # -- 服务层 + HTTP ---------------------------------------------------

    def test_default_period_is_today_and_date_type_is_recent1d(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/card/seller')
        self.assertEqual(data['kind'], 'seller')
        self.assertEqual(data['date_type'], datacenter_proto.DATE_TYPE_DAY)
        self.assertEqual(data['period']['from'], datetime.now().strftime('%Y%m%d'))
        self.assertEqual(data['banner'][0]['label'], '商品访问次数')
        self.assertEqual(data['banner'][0]['value'], 124)
        # 协议层收到的仍是**空** start/end（线上参数是 recent1d，不带 dateRange）
        # `args_of` 记的是 `(client, kind, start, end, seller_id)`
        self.assertEqual(self.proto.args_of('card')[2:4], ('', ''))
        self.assertEqual(self.proto.args_of('card')[1], 'seller')

    def test_custom_range_is_passed_through_and_shown(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/card/trade', '?start=20261001&end=20261007')
        self.assertEqual(data['period'], {'from': '20261001', 'to': '20261007'})
        self.assertEqual(self.proto.args_of('card')[2:4], ('20261001', '20261007'))
        self.assertEqual(self.proto.args_of('card')[1], 'trade')

    def test_single_sided_range_is_completed(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/card/refund', '?start=20261001')
        self.assertEqual(data['period'], {'from': '20261001', 'to': '20261001'})

    def test_invalid_date_is_400_and_never_calls_the_platform(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/card/seller', '?start=2026')
        self.assertEqual(body.get('code'), 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_unknown_kind_is_400_and_never_calls_the_platform(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/card/nope')
        self.assertEqual(body.get('code'), 'invalid_fields')
        self.assertIn('trade', body.get('message', ''))
        self.assertEqual(self.proto.calls, [])

    def test_missing_body_gives_empty_lists_and_a_warning(self):
        seed_account()
        self.proto.payloads['card'] = card_payload(found=False)
        data = self.ok_data(f'{BASE}/{ACC}/card/seller')
        self.assertEqual((data['metrics'], data['banner'], data['series']), ([], [], []))
        self.assertTrue(any('没有返回这张卡片' in w for w in data['warnings']), data['warnings'])

    def test_permission_denied_is_a_human_warning_not_502(self):
        seed_account()
        self.proto.payloads['card'] = card_payload(errors={
            'card': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        data = self.ok_data(f'{BASE}/{ACC}/card/seller')
        self.assertTrue(any('没有「数据」权限' in w for w in data['warnings']), data['warnings'])

    def test_permission_denied_does_not_also_blame_the_field_names(self):
        """「这个账号没权限」与「平台可能改了字段名」不能同时说 —— 后者会把人往错方向带。"""
        seed_account()
        self.proto.payloads['card'] = card_payload(found=False, errors={
            'seller': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        data = self.ok_data(f'{BASE}/{ACC}/card/seller')
        self.assertEqual(len(data['warnings']), 1, data['warnings'])
        self.assertIn('经营概况', data['warnings'][0])
        self.assertIn('没有「数据」权限', data['warnings'][0])

    def test_platform_error_is_502_with_protocol_error(self):
        seed_account()
        self.proto.errors['card'] = RuntimeError('mtop 调用失败')
        body = self.fail_envelope(f'{BASE}/{ACC}/card/seller', status=502)
        self.assertEqual(body.get('code'), 'protocol_error')

    def test_session_expired_is_409_and_marks_need_relogin(self):
        seed_account()
        self.proto.errors['card'] = SessionExpiredError('FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/card/seller', status=409)
        self.assertEqual(body.get('code'), 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')

    def test_missing_cookie_is_409_and_never_calls_the_platform(self):
        seed_account(cookie='')
        body = self.fail_envelope(f'{BASE}/{ACC}/card/seller', status=409)
        self.assertEqual(body.get('code'), 'verification_required')
        self.assertEqual(self.proto.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/nope/card/seller', status=404)
        self.assertEqual(body.get('code'), 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_route_is_mounted_on_the_real_app(self):
        """挂载形状必须与测试里自挂的一致（自建 FastAPI 防不住挂载方式变化）。"""
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要

        paths = app_main.create_app().openapi().get('paths', {})
        self.assertIn(f'{BASE}/{{account_id}}/card/{{kind}}', paths)
        self.assertIn('get', paths[f'{BASE}/{{account_id}}/card/{{kind}}'])


class CsListTest(DataCenterApiTest):
    """客服数据页三张明细表（客服明细 / 订单分流明细 / 客户评价明细）的协议 + 服务 + HTTP。

    三张表共用一套入参与响应形状（官方就是三个接口），但**列名与中文标题全取官方表头**：
    我们不翻译、不增删 —— 平台加列就多显示一列，平台改列名就跟着改。响应是**双层 `data`**
    （官方前端写的是 `v.data.data.data.list`），`rows=None` 与 `rows=[]` 是两件事。
    """

    # -- 协议层：入参形状（官方 bundle 逐字） ----------------------------

    def test_request_matches_the_official_payload(self):
        payload = datacenter_proto.cs_list_request('detail')
        self.assertEqual(payload, {
            'selectedSellerId': '', 'dateRange': '', 'ms': '',
            'dateType': datacenter_proto.DATE_TYPE_DAY,
            'orderBy': '', 'order': '', 'page': 1, 'pageSize': 10,
        })

    def test_custom_range_uses_pipe_and_month_uses_ms(self):
        payload = datacenter_proto.cs_list_request(
            'evaluation', date_type=datacenter_proto.COMMODITY_DATE_CUSTOM,
            dates=['20261001', '20261013'], seller_id='221')
        self.assertEqual(payload['dateRange'], '20261001|20261013')
        self.assertEqual(payload['ms'], '')
        monthly = datacenter_proto.cs_list_request(
            'diversion', date_type=datacenter_proto.DATE_TYPE_MONTH, dates=['202610'])
        self.assertEqual(monthly['ms'], '202610')
        self.assertEqual(monthly['dateRange'], '')

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            datacenter_proto.cs_list_request('nope')
        with self.assertRaises(ValueError):
            asyncio.get_event_loop().run_until_complete(
                datacenter_proto.cs_list(FakeMtopClient(), 'nope'))

    def test_columns_come_from_the_official_table(self):
        detail = datacenter_proto.CS_LIST_COLUMNS['detail']
        self.assertEqual(len(detail), 12)
        self.assertEqual(detail[0], ('acctName', '账号名称'))
        self.assertEqual(detail[-1], ('respRate1min', '1分钟响应率'))
        self.assertEqual(datacenter_proto.CS_LIST_COLUMNS['diversion'],
                         (('divTp', '分流类型'), ('dtlTp', '明细分类'), ('sessCnt', '会话数')))
        evaluation = datacenter_proto.CS_LIST_COLUMNS['evaluation']
        self.assertEqual(evaluation[0], ('buyerUserName', '客户名称'))
        self.assertEqual(evaluation[-1], ('dissatReason', '不满意原因'))

    def test_parse_reads_the_double_nested_data(self):
        payload = {'data': {'code': 'success', 'data': {'list': [{'acctName': 'a'}],
                                                        'total': 3}},
                   'extendInfo': {'realDateRange': ['2026-10-13', '2026-10-13']}}
        parsed = datacenter_proto.parse_cs_list(payload, 'detail')
        self.assertEqual(parsed['rows'], [{'acctName': 'a'}])
        self.assertEqual(parsed['total'], 3)
        self.assertEqual(parsed['found_key'], 'list')
        self.assertEqual(parsed['real_date_range'], ['2026-10-13', '2026-10-13'])

    def test_parse_without_the_list_key_is_none_not_empty(self):
        missing = datacenter_proto.parse_cs_list(
            {'data': {'code': 'success', 'data': {'total': 0}}}, 'detail')
        self.assertIsNone(missing['rows'], '「平台没给列表」不能当成「一条都没有」')
        self.assertEqual(missing['total'], 0, 'total=0 是有效值，要保留')
        empty = datacenter_proto.parse_cs_list(
            {'data': {'code': 'success', 'data': {'list': [], 'total': 0}}}, 'detail')
        self.assertEqual(empty['rows'], [])

    def test_parse_ignores_a_failed_business_code(self):
        parsed = datacenter_proto.parse_cs_list(
            {'data': {'code': 'FAIL_BIZ_X', 'data': {'list': [{'a': 1}], 'total': 1}}},
            'diversion')
        self.assertIsNone(parsed['rows'])

    def test_permission_denied_returns_errors_instead_of_raising(self):
        """平台「拒绝访问」是有效答复（与同页其它块同一口径），不能变成 502 红框。"""
        client = FakeMtopClient(errors={datacenter_proto.CS_LIST_APIS['detail']: RuntimeError(
            'MTOP 调用失败 [mtop.alibaba.idle.seller.pc.datacompass.cs.detail.list]: '
            'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问')})
        result = asyncio.get_event_loop().run_until_complete(
            datacenter_proto.cs_list(client, 'detail'))
        self.assertIsNone(result['rows'])
        self.assertIn('detail', result['errors'])
        self.assertIn('UNAUTHORIZED', result['errors']['detail'])

    def test_other_failures_still_raise(self):
        client = FakeMtopClient(
            errors={datacenter_proto.CS_LIST_APIS['detail']: RuntimeError('boom')})
        with self.assertRaises(RuntimeError):
            asyncio.get_event_loop().run_until_complete(
                datacenter_proto.cs_list(client, 'detail'))

    def test_session_expired_still_raises(self):
        client = FakeMtopClient(
            errors={datacenter_proto.CS_LIST_APIS['detail']: SessionExpiredError(
                'FAIL_SYS_SESSION_EXPIRED::Session过期')})
        with self.assertRaises(SessionExpiredError):
            asyncio.get_event_loop().run_until_complete(
                datacenter_proto.cs_list(client, 'detail'))

    # -- 服务层 + HTTP ---------------------------------------------------

    def test_every_kind_is_served_with_its_own_title_and_columns(self):
        seed_account()
        for kind, title, first_key in (('detail', '客服明细', 'acctName'),
                                       ('diversion', '订单分流明细', 'divTp'),
                                       ('evaluation', '客户评价明细', 'buyerUserName')):
            with self.subTest(kind=kind):
                data = self.ok_data(f'{BASE}/{ACC}/cs/{kind}')
                self.assertEqual(data['kind'], kind)
                self.assertEqual(data['title'], title)
                self.assertEqual(data['columns'][0], {'key': first_key,
                                                      'title': data['columns'][0]['title']})
                self.assertEqual(data['source'], 'platform')

    def test_default_period_is_today_and_the_platform_gets_no_dates(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/cs/detail')
        self.assertEqual(data['date_type'], datacenter_proto.DATE_TYPE_DAY)
        self.assertEqual(data['period']['from'], datetime.now().strftime('%Y%m%d'))
        # 协议层收到的仍是**空** start/end（线上参数是 recent1d，不带 dateRange）
        # `args_of` 记的是 `(client, kind, start, end, seller_id, order_by, order, page, page_size)`
        self.assertEqual(self.proto.args_of('cs_list')[1:4], ('detail', '', ''))
        self.assertEqual(self.proto.args_of('cs_list')[8], 10)

    def test_custom_range_and_paging_are_passed_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/cs/evaluation',
                            '?start=20261001&end=20261007&page=2&size=20')
        self.assertEqual(data['period'], {'from': '20261001', 'to': '20261007'})
        args = self.proto.args_of('cs_list')
        self.assertEqual(args[1:4], ('evaluation', '20261001', '20261007'))
        self.assertEqual(args[7:9], (2, 20))
        self.assertEqual(data['page'], 2)
        self.assertEqual(data['page_size'], 20)

    def test_total_zero_is_kept_and_pages_are_computed(self):
        seed_account()
        self.proto.payloads['cs_list'] = cs_list_payload(rows=[], total=0)
        data = self.ok_data(f'{BASE}/{ACC}/cs/detail')
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['pages'], 0)
        self.proto.payloads['cs_list'] = cs_list_payload(total=21)
        data = self.ok_data(f'{BASE}/{ACC}/cs/detail', '?size=10')
        self.assertEqual(data['pages'], 3)

    def test_rows_null_gives_a_warning_not_an_empty_table(self):
        seed_account()
        self.proto.payloads['cs_list'] = cs_list_payload(rows=None, total=None)
        self.proto.payloads['cs_list']['rows'] = None
        data = self.ok_data(f'{BASE}/{ACC}/cs/diversion')
        self.assertIsNone(data['rows'])
        self.assertTrue(any('没有返回这张明细表' in w for w in data['warnings']),
                        data['warnings'])

    def test_permission_denied_is_one_human_warning_not_502(self):
        seed_account()
        self.proto.payloads['cs_list'] = cs_list_payload(
            rows=None, total=None,
            errors={'detail': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        self.proto.payloads['cs_list']['rows'] = None
        data = self.ok_data(f'{BASE}/{ACC}/cs/detail')
        self.assertEqual(len(data['warnings']), 1, data['warnings'])
        self.assertIn('客服明细', data['warnings'][0])
        self.assertIn('没有「数据」权限', data['warnings'][0])

    def test_unknown_kind_is_400_and_never_calls_the_platform(self):
        seed_account()
        body = self.fail_envelope(f'{BASE}/{ACC}/cs/nope')
        self.assertEqual(body.get('code'), 'invalid_fields')
        self.assertIn('detail', body.get('message', ''))
        self.assertEqual(self.proto.calls, [])

    def test_bad_paging_is_rejected_and_never_calls_the_platform(self):
        seed_account()
        # 上下界由 `Query(ge/le)` 先拦（422），与服务层 400 的分工与商品数据页一致
        for query in ('?page=0', '?size=999'):
            with self.subTest(query=query):
                resp = self.get(f'{BASE}/{ACC}/cs/detail{query}')
                self.assertEqual(resp.status_code, 422, resp.text)
        # `order` 的取值是业务约束 → 服务层 400 `invalid_fields`
        self.assertEqual(self.fail_envelope(f'{BASE}/{ACC}/cs/detail',
                                            '?order=asc').get('code'), 'invalid_fields')
        self.assertEqual(self.fail_envelope(f'{BASE}/{ACC}/cs/detail',
                                            '?order=sideways').get('code'), 'invalid_fields')
        self.assertEqual(self.proto.calls, [])

    def test_platform_error_is_502_with_protocol_error(self):
        seed_account()
        self.proto.errors['cs_list'] = RuntimeError('mtop 调用失败')
        body = self.fail_envelope(f'{BASE}/{ACC}/cs/detail', status=502)
        self.assertEqual(body.get('code'), 'protocol_error')

    def test_session_expired_is_409_and_marks_need_relogin(self):
        seed_account()
        self.proto.errors['cs_list'] = SessionExpiredError(
            'FAIL_SYS_SESSION_EXPIRED::Session过期')
        body = self.fail_envelope(f'{BASE}/{ACC}/cs/detail', status=409)
        self.assertEqual(body.get('code'), 'verification_required')
        self.assertEqual(body.get('login_state'), 'need_relogin')

    def test_missing_cookie_is_409_and_never_calls_the_platform(self):
        seed_account(cookie='')
        body = self.fail_envelope(f'{BASE}/{ACC}/cs/detail', status=409)
        self.assertEqual(body.get('code'), 'verification_required')
        self.assertEqual(self.proto.calls, [])

    def test_unknown_account_is_404(self):
        body = self.fail_envelope(f'{BASE}/nope/cs/detail', status=404)
        self.assertEqual(body.get('code'), 'not_found')
        self.assertEqual(self.proto.calls, [])

    def test_route_is_mounted_on_the_real_app(self):
        """挂载形状必须与测试里自挂的一致（自建 FastAPI 防不住挂载方式变化）。"""
        remove_fake_protocol()
        from backend.app import main as app_main  # noqa: PLC0415 - 真 app 只在这里需要

        paths = app_main.create_app().openapi().get('paths', {})
        self.assertIn(f'{BASE}/{{account_id}}/cs/{{kind}}', paths)
        self.assertIn('get', paths[f'{BASE}/{{account_id}}/cs/{{kind}}'])


class FansBreakdownTest(DataCenterApiTest):
    """第 51 批：粉丝画像分组（只读）。

    官方「粉丝数据」页渲染的是 `fans.insights.query` 响应里的具名列表
    （`buyerGenderList` / `fansTypeList` / `buyerProvinceList` / `buyerEightTpList`），
    不是我们原先找的那个 `insights` 列表 —— 原先真机上这一栏永远是空的。
    """

    def breakdown(self, insights: Any = FANS_INSIGHTS_PAYLOAD) -> Dict[str, Any]:
        seed_account()
        self.proto.payloads['fans'] = fans_payload(insights)
        data = self.ok_data(f'{BASE}/{ACC}/fans')
        return data['insights_breakdown']

    def test_breakdown_has_the_four_groups_from_the_real_shape(self):
        breakdown = self.breakdown()
        self.assertIsNotNone(breakdown)
        self.assertEqual(sorted(breakdown['groups']),
                         ['eight_type', 'fans_type', 'gender', 'province'])
        self.assertEqual(breakdown['raw_keys'],
                         ['buyerGenderList', 'fansTypeList', 'buyerProvinceList', 'buyerEightTpList'])

    def test_group_carries_its_title_and_platform_key(self):
        group = self.breakdown()['groups']['gender']
        self.assertEqual(group['title'], '性别分布')
        self.assertEqual(group['platform_key'], 'buyerGenderList')

    def test_gender_is_translated_and_male_comes_first(self):
        items = self.breakdown()['groups']['gender']['items']
        self.assertEqual([item['name'] for item in items], ['男', '女'])
        self.assertEqual([item['ratio'] for item in items], [0.38, 0.62])
        self.assertEqual([item['raw_label'] for item in items], ['M', 'F'])

    def test_fans_type_drops_the_letter_prefix_and_sorts_a_to_g(self):
        items = self.breakdown()['groups']['fans_type']['items']
        self.assertEqual([item['name'] for item in items], ['流失粉', '活跃粉'])
        self.assertEqual([item['raw_label'] for item in items], ['A_流失粉', 'D_活跃粉'])

    def test_eight_types_follow_the_official_order_table(self):
        """官方顺序表里 `未来之星` 是 0 ⇒ 排最前（bundle 偏移 2491628）。"""
        items = self.breakdown()['groups']['eight_type']['items']
        self.assertEqual([item['name'] for item in items], ['未来之星', '学生群体'])

    def test_provinces_sort_by_ratio_desc(self):
        items = self.breakdown()['groups']['province']['items']
        self.assertEqual([item['name'] for item in items], ['浙江', '广东'])
        self.assertEqual([item['ratio'] for item in items], [0.2, 0.12])

    def test_ratio_text_is_kept_verbatim_and_ratio_is_derived_from_it(self):
        items = self.breakdown()['groups']['province']['items']
        self.assertEqual(items[0]['ratio_text'], '20.00%')
        self.assertAlmostEqual(items[0]['ratio'], 0.2)

    def test_unknown_label_sorts_last_and_is_not_dropped(self):
        payload = {'data': {'fansTypeList': [
            {'profileVal': 'Z_没见过的档位', 'usrRatioFormat': '1.00%'},
            {'profileVal': 'A_流失粉', 'usrRatioFormat': '10.00%'},
        ]}}
        items = self.breakdown(payload)['groups']['fans_type']['items']
        self.assertEqual([item['name'] for item in items], ['流失粉', '没见过的档位'])

    def test_entry_without_ratio_is_null_never_zero(self):
        payload = {'data': {'buyerProvinceList': [{'profileVal': '没有比例的省'}]}}
        items = self.breakdown(payload)['groups']['province']['items']
        self.assertIsNone(items[0]['ratio'])
        self.assertIsNone(items[0]['ratio_text'])

    def test_entries_without_ratio_sort_last(self):
        payload = {'data': {'buyerProvinceList': [
            {'profileVal': '没比例'}, {'profileVal': '广东', 'usrRatioFormat': '12.00%'},
        ]}}
        items = self.breakdown(payload)['groups']['province']['items']
        self.assertEqual([item['name'] for item in items], ['广东', '没比例'])

    def test_platform_without_groups_gives_null_and_a_warning(self):
        seed_account()
        self.proto.payloads['fans'] = fans_payload(FANS_NO_BREAKDOWN)
        data = self.ok_data(f'{BASE}/{ACC}/fans')
        self.assertIsNone(data['insights_breakdown'])
        self.assertIn(datacenter_service.WARN_FANS_INSIGHTS_MISSING, data['warnings'])

    def test_permission_denied_keeps_only_the_permission_warning(self):
        """账号没有「数据」权限时已经有权限 warning，不再叠一句「画像没解析出来」。"""
        seed_account()
        self.proto.payloads['fans'] = fans_payload(FANS_NO_BREAKDOWN, errors={
            'insights': 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问'})
        data = self.ok_data(f'{BASE}/{ACC}/fans')
        self.assertIsNone(data['insights_breakdown'])
        self.assertNotIn(datacenter_service.WARN_FANS_INSIGHTS_MISSING, data['warnings'])
        self.assertTrue(any('没有「数据」权限' in text for text in data['warnings']), data['warnings'])

    def test_other_kinds_do_not_carry_the_breakdown(self):
        seed_account()
        overview = self.ok_data(f'{BASE}/{ACC}/overview')
        self.assertNotIn('insights_breakdown', overview)

    def test_legacy_insights_list_is_still_passed_through(self):
        seed_account()
        data = self.ok_data(f'{BASE}/{ACC}/fans')
        self.assertEqual(data['insights'], FANS_PAYLOAD['insights'])


if __name__ == '__main__':
    unittest.main()
