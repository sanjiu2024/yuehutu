# -*- coding: utf-8 -*-
"""物流族（官方卖家工作台「订单管理 → 发货 / 修改物流」）：快递公司列表 + 运单号认公司 + 修改运单号（重发）。

**接口来源（静态 bundle 反解，零风控）**：官方交易微应用
`https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3,879,584 B，单行压缩 JS）。
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\trade_bundle\\main.js` 与
`C:\\Users\\Administrator\\.dsh-tmp\\seller_scan\\js_008_vendors.js`，**证据用字符偏移**。

**为什么单独开一个模块**：`backend/app/xianyu/trade_write.py` 已经有发货两条
（`consign.offline` / `consign.dummy`，登记在它的 `WRITE_APIS` 里），但那是「把这一单发出去」；
这一族是**发货之后**的物流信息维护（改运单号）+ 两个纯只读的辅助接口（快递公司表、运单号认公司），
官方页面上它们也确实是独立的三个调用点。独立成模块便于逐条标注证据。

**包装**：三个接口在 bundle 里分别走 `hO` / `eN.G` / `th.G`，业务字段**直接放 `data`**
（不是 `requestJson` 包装），`companies` 那条**连 `data` 都没有**（官方原文 `hO({api, v:"1.0"})`）。
所以这里全部返回 `(api, data)` 二元组，由服务层统一
`common.call_method(client, ('mtop',), api, data, v='1.0')` 发出去；`data` 为 `None` 时客户端不发 `data` 键。

**官方默认值（逐字照抄，不要自己发明）**：
* 快递公司下拉的默认值 `DEFAULT_COMPANY_CODE = 'OTHER'`（bundle 里 `useState("OTHER")` 两处）；
* 认不出公司时官方把 `unionCode` 追加进下拉（`{value: unionCode, label: cpName}`），并把值设成它；
* 运单号只校验**非空**（官方 `t && t.trim() ? resolve() : reject(new Error("请输入快递单号"))`），
  我们没有自己加长度/格式上限。

**未验证边界**：一次真机写请求都没发过（红线不允许为了验证去改你平台的物流单号）。
`companies` / `guess` 两条只读接口的形状也来自 bundle，尚未真机跑过。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from .trade import body_of, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 快递公司列表（只读）。bundle 偏移 2617308（交易微应用）：
#: `hO({api:"com.taobao.idle.logistics.new.get.companies", v:"1.0"})` —— **没有 `data`**；
#: 响应读 `resp.data.items.map(e => ({label: e.name, value: e.code}))`。
COMPANIES_API = 'com.taobao.idle.logistics.new.get.companies'

#: 运单号认快递公司（只读）。bundle 偏移 2617817（交易微应用，输入框 500ms 防抖）：
#: `hO({api:"mtop.taobao.idle.logistics.guess.mailno", v:"1.0", data:{mailNo:e}})`;
#: 响应读 `resp.data.unionCode`（公司编码）与 `resp.data.cpName`（公司名）。
GUESS_API = 'mtop.taobao.idle.logistics.guess.mailno'

#: 修改运单号 / 重发（写）。bundle 偏移 2766767（交易微应用「修改物流信息」弹窗）：
#: `hO({api:"mtop.taobao.idle.logistics.merchant.consign.resend", v:"1.0",
#:      data:{tradeId:c, mailNo:f, cpCode:S}})`，成功 toast「修改成功」。
#: 同一接口在 `seller_scan/js_008_vendors.js` 偏移 917558 也有定义（`eM = eu => eN.G({api, v:"1.0", data:eu})`）。
RESEND_API = 'mtop.taobao.idle.logistics.merchant.consign.resend'

#: 三个接口的版本号（bundle 原文全是 `v:"1.0"`）。
DEFAULT_VERSION = '1.0'

#: 官方快递公司下拉的默认值（bundle 两处 `useState("OTHER")`）。
DEFAULT_COMPANY_CODE = 'OTHER'

#: 快递单号的长度上限放得很宽（官方没校验长度，只校验非空）；这里只挡明显不是单号的输入。
MAIL_NO_MAX = 64

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 字符偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'resend': {
        'api': RESEND_API,
        'data': '{tradeId, mailNo, cpCode}',
        'evidence': 'bundle @2766767 声明 + 调用点 `({tradeId:c, mailNo:f, cpCode:S})`'
                    '（弹窗初值：`mailNo` 默认 ""、`companyName` 默认 "其他"）/'
                    ' js_008_vendors.js @917558 同族定义',
    },
}

#: 只读接口（`assert_read` 用它）。
READ_APIS: Tuple[str, ...] = (COMPANIES_API, GUESS_API)

#: 本模块登记的全部接口（写 + 读）。
ALL_APIS = frozenset([*READ_APIS, *(entry['api'] for entry in WRITE_APIS.values())])

#: 写动作名（服务层/工具层按这个枚举动作）。
ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)


class UnknownApi(ValueError):
    """调用了本模块不认识的接口。"""


class WriteNotConfirmed(ValueError):
    """写接口没有显式 `confirm=True`。"""


def assert_write(api: str) -> str:
    """确认 `api` 是本模块登记的**写**接口，返回它（方便串在构造器前面）。"""
    if api not in (entry['api'] for entry in WRITE_APIS.values()):
        raise UnknownApi(f'不是本模块的写接口：{api}')
    return api


def assert_read(api: str) -> str:
    """确认 `api` 是本模块登记的**只读**接口。"""
    if api not in READ_APIS:
        raise UnknownApi(f'不是本模块的只读接口：{api}')
    return api


def _need_confirm(confirm: Any) -> None:
    """写闸门：只有**真的是** `True` 才算点头。

    刻意不用 `bool(confirm)` —— `bool('false')` 是 `True`（第 222 条缺陷的教训）。
    """
    if confirm is not True:
        raise WriteNotConfirmed('这是写操作，会真的改动闲鱼上的物流单号，必须显式 confirm=true')


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return str(value)


def _mail_no(value: Any, label: str = '快递单号') -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError(f'请输入{label}')
    if len(text) > MAIL_NO_MAX:
        raise ValueError(f'{label}太长了（超过 {MAIL_NO_MAX} 个字符）')
    return text


# --------------------------------------------------------------------------- 请求构造


def companies_request() -> Tuple[str, None]:
    """快递公司列表：官方**不带 `data`**。"""
    return assert_read(COMPANIES_API), None


def guess_request(mail_no: Any) -> Tuple[str, Dict[str, Any]]:
    """运单号认公司：`{mailNo}`。"""
    return assert_read(GUESS_API), {'mailNo': _mail_no(mail_no)}


def resend_request(order_id: Any, mail_no: Any, cp_code: Any = '',
                   *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """修改运单号（重发）：`{tradeId, mailNo, cpCode}`。

    `cpCode` 留空时用官方默认值 `OTHER`（官方下拉初值就是它）。
    """
    _need_confirm(confirm)
    trade_id = _text(order_id).strip()
    if not trade_id:
        raise ValueError('订单号不能为空')
    code = _text(cp_code).strip() or DEFAULT_COMPANY_CODE
    return assert_write(RESEND_API), {
        'tradeId': trade_id,
        'mailNo': _mail_no(mail_no),
        'cpCode': code,
    }


# --------------------------------------------------------------------------- 解析


def _body(raw: Any) -> Dict[str, Any]:
    """取出业务体。

    `trade.body_of` 认得三种容器：`data.module`（部分接口）、`data.data`（双层）、`data` 本身。
    这里直接复用它 —— 三个接口的实际层级不一样（`companies` 是单层、`resend` 的回执是双层），
    手写一层 `raw['data']` 会在双层那一条上悄悄取不到值（第一次跑测试就是栽在这里）。
    """
    body = body_of(raw)
    return body if isinstance(body, dict) else {}


def parse_companies(raw: Any) -> Dict[str, Any]:
    """快递公司列表：认 `items`（官方读的就是这个键），其次 `list` / `companies`。

    认不出列表键 → `companies=None`（**不是 `[]`**，`[]` 表示平台明确回了空列表）。
    """
    data = _body(raw)
    for key in ('items', 'list', 'companies'):
        value = data.get(key)
        if isinstance(value, list):
            rows = []
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                code = entry.get('code')
                name = entry.get('name')
                rows.append({
                    'code': None if code is None else _text(code),
                    'name': None if name is None else _text(name),
                })
            return {'companies': rows, 'found_key': key}
    return {'companies': None, 'found_key': None}


def parse_guess(raw: Any) -> Dict[str, Any]:
    """运单号认公司：`unionCode` / `cpName`；平台没给就是 `None`（不猜）。"""
    data = _body(raw)
    code = data.get('unionCode')
    name = data.get('cpName')
    return {
        'cp_code': None if code is None else _text(code),
        'cp_name': None if name is None else _text(name),
    }


def parse_resend(raw: Any) -> Dict[str, Any]:
    """修改运单号的结果。

    成功判据就是**平台信封**（官方也是只看 envelope 就 toast「修改成功」，回执里没有业务 id）。
    `echoed` 表示平台有没有回显快递公司编码 —— 没回显也不等于失败，只是「没法本地核对」。
    """
    failure = envelope_error(raw)
    data = _body(raw)
    code = data.get('cpCode') or data.get('unionCode')
    return {
        'saved': not failure,
        'echoed': code is not None,
        'cp_code': None if code is None else _text(code),
        'platform_message': failure or envelope_message(raw),
    }


# --------------------------------------------------------------------------- READ-COVERAGE-61
#: 未发货凭证详情（只读）。bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3138431：
#:   hO({api:"mtop.taobao.idle.merchant.unconsign.detail", v:"1.0", data:{orderId:v}})
#: （v 是该行订单的 orderId），响应读 resp.data.content（凭证文本）与 resp.data.imageList
#: （凭证图片数组，官方 PreviewGroup 逐张预览）。
#: **不确定项**：官方只在这两处取值，content / imageList 之外还有没有字段没有取证 ——
#: 所以本层只认这两个键，其余原样放在 raw 里，不猜。
UNCONSIGN_DETAIL_API = 'mtop.taobao.idle.merchant.unconsign.detail'

#: 发货地详情（只读）。bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js：
#:   W6 = () => U6({api:"mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query"})
#: 官方**不带 data**，紧挨着写接口 merchant.delivery.place.info.set 与只读的
#: merchant.delivery.address.list.query。**不确定项**：响应形状未取证（官方调用点不解析
#: 返回值），本层原样返回。
DELIVERY_PLACE_DETAIL_API = 'mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query'

#: 两条并进只读登记表。
READ_APIS = READ_APIS + (UNCONSIGN_DETAIL_API, DELIVERY_PLACE_DETAIL_API)


def unconsign_detail_request(order_id: Any) -> Tuple[str, Dict[str, Any]]:
    """未发货凭证详情：{orderId}。订单号空则抛 ValueError（本地校验，不建客户端）。"""
    trade_id = _text(order_id).strip()
    if not trade_id:
        raise ValueError('订单号不能为空')
    return assert_read(UNCONSIGN_DETAIL_API), {'orderId': trade_id}


def delivery_place_detail_request() -> Tuple[str, None]:
    """发货地详情：官方**不带 data**。"""
    return assert_read(DELIVERY_PLACE_DETAIL_API), None


def parse_unconsign_detail(raw: Any) -> Dict[str, Any]:
    """未发货凭证：content（文本）/ imageList（图片数组）。

    平台没给某个键时是 None（**不是空串 / 空数组**）—— 空数组表示「平台明确说没有图」。
    响应里除了这两个键之外的字段原样放在 raw，不猜语义。
    """
    data = _body(raw)
    content = data.get('content')
    images = data.get('imageList')
    return {
        'content': None if content is None else _text(content),
        'image_list': images if isinstance(images, list) else None,
        'raw': data or None,
    }


def parse_delivery_place_detail(raw: Any) -> Dict[str, Any]:
    """发货地详情：形状未取证，**原样透出业务体**，一个字段都不猜。"""
    data = _body(raw)
    return {'detail': data or None}
