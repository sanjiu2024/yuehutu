# -*- coding: utf-8 -*-
"""退货地址写接口（官方卖家工作台「退货地址」页）：新增 / 修改 / 删除 / 设为默认 —— 协议层。

**接口来源（静态 bundle 反解，零风控）**：官方交易微应用
`https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3,879,584 B，单行压缩 JS）。
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\trade_bundle\\main.js`，**证据用字符偏移**
（单行文件行号无意义），偏移与调用点原文见每个常量的注释与 `WRITE_APIS[*]['evidence']`。

**为什么单独开一个模块**：`backend/app/xianyu/trade.py` 把这一族登记在它的 `WRITE_APIS`
黑名单里（只读路径必须拦住它们），但只读层不实现写；而 `trade_write.py` 是「交易」组
（订单/退款/评价/投诉）的写协议。退货地址属于「小铺 → 退货地址」页，独立成模块便于
逐条标注证据，也让 `assert_write()` 的白名单边界清晰。

**包装**：四个接口在 bundle 里都走同一个 `U6({api, v:'1.0', data, callbackOptions})`，
业务字段**直接放 `data`**（不是 `requestJson` 包装），所以这里全部返回 `(api, data)` 二元组，
由服务层统一 `common.call_method(client, ('mtop',), api, data, v='1.0')` 发出去。

**诚实偏离（重要，不要偷偷补）**：官方新增/编辑表单里除了下面这些字段，还有一个
`division`（三元素 `{label,value}` 级联数组，来自 `DivisionSelect`）与一个隐藏的 `areaId`
（**区级区域码**，官方 `areaId = dist.value || values.areaId`）。
`mtop.taobao.idle.division.all.get` 对本机账号是 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，但
**第 61 批找到了另一个可用码源** `mtop.cainiao.geography.frontend.divisionsinfo.get`
（服务层 `GET /api/logistics/{account_id}/divisions`，见 `xianyu/division.py`），所以：

- **不伪造** `areaId`：协议层仍是调用方给了才发这个键（服务层已把它当硬必填）；
- **不发形状不对的** `division`：只发 `provinceName` / `cityName` / `districtName` 三个名字。

平台若不接受，错误原文会原样透出（`envelope_error`），不会被说成成功。

**未验证边界**：一次真机写请求都没发过（本机账号登录态已过期，红线不允许为了验证去改
平台上的收货地址）。所以「平台是否接受这套字段」仍属未验证，只保证形状与官方页面逐字一致。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from .trade import body_of, decode_payload, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 新增退货地址。bundle 偏移 3143703：
#: `$6 = e => U6({api:"…delivery.address.add", data:e, callbackOptions:{successText:"退货地址新增成功"}})`
ADD_API = 'mtop.alibaba.idle.seller.platform.merchant.delivery.address.add'
#: 修改退货地址。bundle 偏移 3143951：
#: `Z6 = e => U6({api:"…delivery.address.update", data:e, callbackOptions:{successText:"退货地址更新成功"}})`
UPDATE_API = 'mtop.alibaba.idle.seller.platform.merchant.delivery.address.update'
#: 删除退货地址。bundle 偏移 3144220：`J6 = ({contactId:t}) => U6({api:"…address.delete", data:{contactId:t}})`
DELETE_API = 'mtop.alibaba.idle.seller.platform.merchant.delivery.address.delete'
#: 设为默认退货地址。bundle 偏移 3144501：
#: `Q6 = ({contactId:t}) => U6({api:"…address.set.default", data:{contactId:t}, callbackOptions:{errorText:"设置默认退货地址异常"}})`
#: （注意它只有 errorText，没有 successText —— 官方靠重拉列表确认结果。）
SET_DEFAULT_API = 'mtop.alibaba.idle.seller.platform.merchant.delivery.address.set.default'

#: 四个接口的版本号（bundle 原文全是 `v:"1.0"`）。
DEFAULT_VERSION = '1.0'

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 字符偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'add': {
        'api': ADD_API,
        'data': '{contactName, mobilePhone, provinceName, cityName, districtName, detailAddress,'
                ' defaultAddr?, areaId?}（官方还发 division 级联数组，我们不伪造、故意不发；'
                'areaId 的码源见 xianyu/division.py）',
        'evidence': 'bundle @3143703 声明 / 组件 @3780900~3782550 的 onOk() 组装 payload',
    },
    'update': {
        'api': UPDATE_API,
        'data': '{contactId, contactName, mobilePhone, provinceName, cityName, districtName,'
                ' detailAddress, defaultAddr?, areaId?}',
        'evidence': 'bundle @3143951 声明 / 组件 @3781200 `editForm.setValues({...e, …})`'
                    '（编辑表单初值直接来自列表行，所以带 contactId）',
    },
    'delete': {
        'api': DELETE_API,
        'data': '{contactId}',
        'evidence': 'bundle @3144220 声明 / 调用点 `J6({contactId:t})`',
    },
    'set_default': {
        'api': SET_DEFAULT_API,
        'data': '{contactId}',
        'evidence': 'bundle @3144501 声明 / 调用点 `Q6({contactId:t})`',
    },
}

#: 本模块**只登记写接口**，全部都在这里（`assert_write` 用它挡住误用）。
ALL_APIS = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 官方表单字段的校验上限（bundle 偏移 3776524 / 3778559 的 schema：`max 15` / `phone` / `max 100`）。
CONTACT_NAME_MAX = 15
DETAIL_ADDRESS_MAX = 100
MOBILE_DIGITS = 11


class UnknownApi(ValueError):
    """不在 `ALL_APIS` 里的接口名 —— 说明调用方拼错了，别发出去。"""


class WriteNotConfirmed(RuntimeError):
    """协议层不给默认执行的写入口：构造请求前必须显式 `confirm=True`。"""


def assert_write(api: str) -> str:
    """只允许本模块登记的四个写接口通过。"""
    if api not in ALL_APIS:
        raise UnknownApi(f'{api} 不在退货地址写接口白名单里')
    return api


# --------------------------------------------------------------------------- 工具


def _text(value: Any) -> str:
    """空值一律 ''，数字转字符串（contactId 真机可能是 int，必须保精度）。"""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return ''


def _need_confirm(confirm: Any) -> None:
    if confirm is not True:
        raise WriteNotConfirmed('写操作需要显式确认（confirm=True）')


# --------------------------------------------------------------------------- 请求构造


def _address_fields(contact_name: Any, mobile_phone: Any, province_name: Any, city_name: Any,
                    district_name: Any, detail_address: Any, area_id: Any = None,
                    default_addr: Any = None) -> Dict[str, Any]:
    """官方字段名逐字（camelCase）；没给的可选字段**不放键**（不是发空串）。"""
    data: Dict[str, Any] = {
        'contactName': _text(contact_name),
        'mobilePhone': _text(mobile_phone),
        'provinceName': _text(province_name),
        'cityName': _text(city_name),
        'districtName': _text(district_name),
        'detailAddress': _text(detail_address),
    }
    if _text(area_id):
        data['areaId'] = _text(area_id)
    if isinstance(default_addr, bool):
        data['defaultAddr'] = default_addr
    return data


def add_request(contact_name: Any, mobile_phone: Any, province_name: Any, city_name: Any,
                district_name: Any, detail_address: Any, area_id: Any = None,
                default_addr: Any = None, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """新增退货地址（`confirm=True` 才构造）。"""
    _need_confirm(confirm)
    return assert_write(ADD_API), _address_fields(
        contact_name, mobile_phone, province_name, city_name, district_name, detail_address,
        area_id, default_addr)


def update_request(contact_id: Any, contact_name: Any, mobile_phone: Any, province_name: Any,
                   city_name: Any, district_name: Any, detail_address: Any, area_id: Any = None,
                   default_addr: Any = None, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """修改退货地址（`confirm=True` 才构造）。`contactId` 是官方必带字段。"""
    _need_confirm(confirm)
    key = _text(contact_id)
    if not key:
        raise ValueError('退货地址 contact_id 不能为空')
    data = _address_fields(contact_name, mobile_phone, province_name, city_name, district_name,
                           detail_address, area_id, default_addr)
    data['contactId'] = key
    return assert_write(UPDATE_API), data


def delete_request(contact_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """删除退货地址（`confirm=True` 才构造）。"""
    _need_confirm(confirm)
    key = _text(contact_id)
    if not key:
        raise ValueError('退货地址 contact_id 不能为空')
    return assert_write(DELETE_API), {'contactId': key}


def set_default_request(contact_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """设为默认退货地址（`confirm=True` 才构造）。"""
    _need_confirm(confirm)
    key = _text(contact_id)
    if not key:
        raise ValueError('退货地址 contact_id 不能为空')
    return assert_write(SET_DEFAULT_API), {'contactId': key}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名取请求（`WRITE_APIS` 的键），便于服务层统一分发与单测。"""
    if action == 'add':
        return add_request(kwargs.get('contact_name'), kwargs.get('mobile_phone'),
                           kwargs.get('province_name'), kwargs.get('city_name'),
                           kwargs.get('district_name'), kwargs.get('detail_address'),
                           kwargs.get('area_id'), kwargs.get('default_addr'),
                           confirm=kwargs.get('confirm'))
    if action == 'update':
        return update_request(kwargs.get('contact_id'), kwargs.get('contact_name'),
                              kwargs.get('mobile_phone'), kwargs.get('province_name'),
                              kwargs.get('city_name'), kwargs.get('district_name'),
                              kwargs.get('detail_address'), kwargs.get('area_id'),
                              kwargs.get('default_addr'), confirm=kwargs.get('confirm'))
    if action == 'delete':
        return delete_request(kwargs.get('contact_id'), confirm=kwargs.get('confirm'))
    if action == 'set_default':
        return set_default_request(kwargs.get('contact_id'), confirm=kwargs.get('confirm'))
    raise UnknownApi(f'未知的退货地址写动作: {action}')


# --------------------------------------------------------------------------- 响应解析


def envelope(raw: Any) -> Dict[str, Any]:
    """客户端返回值（`{'success','data','message','ret'}`）里的**平台业务信封**。

    与 `trade_write` 同一约定：业务信封 `{code,msg,data}` 在 `raw['data']` 里。
    """
    decoded = decode_payload(raw)
    body = body_of(decoded)
    return body if isinstance(body, dict) else {}


def business(raw: Any) -> Any:
    """平台业务体里的 `data` 键（这四个接口官方**没有读**它，可能根本不存在）。

    注意 `envelope()` 已经把 `data.module` / `data.data` / `data` 三种容器摊平过一层，
    所以这里再取 `data` 是「摊平后的业务体里还嵌了一层 data」的情况。
    """
    env = envelope(raw)
    return env.get('data')


def code_of(raw: Any) -> str:
    """平台业务码。

    业务码在**外层** `raw['data']['code']`，所以先读外层、再看摊平后的业务体：
    `body_of` 在 `data.data` 是 dict 时会钻进内层（见 `trade.body_of`），
    `{success: true, data: {code: 'FAIL_X', data: {…}}}` 这种形状会把业务码弄丢、
    让 `is_success` 误判成功（2026-10-16 大审查指出；单层形状见 `logs/write64/b1h_delete_probe.json`）。
    """
    decoded = decode_payload(raw)
    outer = decoded.get('data') if isinstance(decoded, dict) else None
    if isinstance(outer, dict):
        code = _text(outer.get('code'))
        if code:
            return code
    return _text(envelope(raw).get('code'))


def message_of(raw: Any) -> str:
    """平台失败原文：**外层** `msg` 优先，其次 `message`；客户端自己失败时退回 `ret[0]`。

    与 `code_of` 同理：`body_of` 可能钻进 `data.data`，外层原文要单独读一次，
    否则嵌套容器下的平台原话会丢（只能退回兜底文案）。
    """
    decoded = decode_payload(raw)
    outer = decoded.get('data') if isinstance(decoded, dict) else None
    for source in (outer, envelope(raw)):
        if isinstance(source, dict):
            text = _text(source.get('msg')) or _text(source.get('message'))
            if text:
                return text
    decoded = decode_payload(raw)
    if isinstance(decoded, dict) and decoded.get('success') is False:
        ret = decoded.get('ret')
        first = ret[0] if isinstance(ret, list) and ret else ''
        return _text(decoded.get('message')) or _text(first)
    return envelope_message(raw) or envelope_error(raw)


def is_success(raw: Any) -> bool:
    """成功判据：客户端层没报错 **且** 平台业务码是 `success`。

    与 `item_write` 不同：这四个接口的成功体官方**根本没读**（它调完就重拉列表），
    所以不能拿「业务体非空」当判据（`set.default` 成功时可能就是一个空对象）。
    平台没给 `code` 时以客户端层结论为准。
    """
    decoded = decode_payload(raw)
    if not isinstance(decoded, dict):
        return False
    if decoded.get('success') is False:
        return False
    code = code_of(raw).lower()
    if code:
        return code == 'success'
    return True


def failure_reason(raw: Any, fallback: str = '') -> str:
    """失败原因：平台 `msg` 原文优先，没有再用兜底文案。"""
    return message_of(raw) or fallback


def contact_id_of(raw: Any) -> Optional[str]:
    """平台回显的 `contactId`（新增成功后才有意义）。

    官方前端新增后是**重拉列表**找新条目（它从不读这个响应体），所以我们也不指望它一定有。
    拿不到一律 `None` —— **不造** 一个假 id（服务层会补一条 `no_contact_id` warning）。
    """
    env = envelope(raw)
    bodies = [env]
    nested = env.get('data')
    if isinstance(nested, dict):
        bodies.append(nested)
    for body in bodies:
        for key in ('contactId', 'contact_id', 'id'):
            value = _text(body.get(key))
            if value:
                return value
    return None


#: 动作名登记（服务层 / AI 工具层共用的稳定名字）。
ACTIONS: Sequence[str] = tuple(WRITE_APIS.keys())
