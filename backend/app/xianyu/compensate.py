# -*- coding: utf-8 -*-
"""赔付 / 维权服务的处理（官方卖家工作台订单页的「赔付」弹窗）。

三个接口，bundle 取证见 `logs/pick45*.txt`：

1. 赔付详情（只读）
   ``gM({api:'mtop.taobao.idle.merchant.compensate.service.detail.query', v:'1.0',
        data:{orderId:eK, serviceCode:eJ}, valueType:'string'})``
   —— 响应 `resp.data.module`，里面有 `serviceInfo{serviceIntroduce, ccoJudgeRuleIntroduce,
   serviceCode}`、`elements[{title:{text}, desc}]`、`amount`（**分**，官方渲染 `div(100)`）、
   以及 `orderId`。**必须带 `valueType:'string'`**（原文如此），否则响应形状不是官方看到的那一种。

2. 拒绝原因表单（只读）
   ``gP(serviceCode) → {api:'…compensate.service.refuse.render', v:'1.0', data:{serviceCode}}``
   —— 响应 `resp.data.module.reasonChoices`（**字符串数组**，官方直接 map 成可点选项）。

3. 拒绝赔付（写）
   ``gM({api:'…compensate.service.refuse', v:'1.0', needLoginPC:!0, data:{orderId, serviceCode,
        sellerRefuseReason:JSON.stringify({reasonChoices:[选中项], reasonDesc:说明}),
        refuseProofs:JSON.stringify(图片 URL 数组), isManual}})``
   —— 官方的两条硬约束：**凭证必填**（`if(!imageUrls.length){ message.error('请上传凭证'); return }`）、
   说明框 `maxLength:200`。成功后 toast「操作成功」。

**故意不做**的是同族的 `mtop.taobao.idle.merchant.compensate.service.pay`（`{amount, orderId,
serviceCode, authToken}`）：它**真的付钱**，`authToken` 来自 `mtop.idle.alipay.verify.url.query`，
而官方那个调用点用的 `scene` 在本地 bundle 里只见到 `REFUND_PC`（我们已实现）与 `DEPOSIT_PC`，
**赔付的 scene 没取到**。宁可不做，也不拿猜的 scene 去调支付宝验证链接。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence, Tuple

from .trade import body_of, decode_payload, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 赔付详情。bundle @831798 `eM({orderId:eK,serviceCode:eJ})`。
DETAIL_API = 'mtop.taobao.idle.merchant.compensate.service.detail.query'
#: 拒绝原因表单。bundle @827668 `eP=e=>G({api:'…refuse.render',v:'1.0',data:{serviceCode:e}})`。
REFUSE_FORM_API = 'mtop.taobao.idle.merchant.compensate.service.refuse.render'
#: 拒绝赔付。bundle 模块 26675：`eM=e=>({api:'…compensate.service.refuse',v:'1.0',data:e,needLoginPC:!0})`。
REFUSE_API = 'mtop.taobao.idle.merchant.compensate.service.refuse'

#: 三个接口的版本号（bundle 原文全是 `v:"1.0"`）。
DEFAULT_VERSION = '1.0'

#: 详情接口**必须**带 `valueType:'string'`（原文如此）。
DETAIL_VALUE_TYPE = 'string'

#: 官方说明框上限（`maxLength={200}`）。
REASON_DESC_MAX = 200

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'refuse': {
        'api': REFUSE_API,
        'data': ('{orderId, serviceCode, sellerRefuseReason(JSON 串 {reasonChoices, reasonDesc}), '
                 'refuseProofs(JSON 串 图片 URL 数组), isManual}'),
        'version': DEFAULT_VERSION,
        'evidence': ("bundle 模块 26675 `eM({orderId:eF,serviceCode:eP,"
                     "sellerRefuseReason:JSON.stringify({reasonChoices:[eQ],reasonDesc:eG}),"
                     "refuseProofs:JSON.stringify(eK.current?.getImageUrls()),isManual:eU})`；"
                     "凭证必填「请上传凭证」，成功 toast「操作成功」"),
    },
}

#: 只读接口。单独列出来，免得被当成写接口加进黑名单，也免得有人给它加 confirm 闸门。
READ_APIS: Tuple[str, ...] = (DETAIL_API, REFUSE_FORM_API)

#: 本模块登记的写接口（`assert_write` 用它挡住误用；**不要**用 `ALL_APIS - READ_APIS`）。
WRITE_API_SET = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 全部接口。
ALL_APIS = frozenset((DETAIL_API, REFUSE_FORM_API, REFUSE_API))

#: 动作名 → 写接口名（服务层的 `request_of` 分派用）。
ACTIONS: Dict[str, str] = {'compensate_refuse': REFUSE_API}

#: 详情里我们**确认见过读法**的键（其余键原样进 `raw_keys`，**不猜**）。
SERVICE_INFO_KEYS: Tuple[str, ...] = (
    'serviceIntroduce', 'ccoJudgeRuleIntroduce', 'serviceCode', 'serviceName', 'serviceStatus',
)
ELEMENT_KEYS: Tuple[str, ...] = ('title', 'desc', 'elements')
AMOUNT_KEYS: Tuple[str, ...] = ('amount', 'serviceAmount', 'compensateAmount')
REASON_CHOICES_KEYS: Tuple[str, ...] = ('reasonChoices', 'reason_choices', 'refuseReasons')
TITLE_KEYS: Tuple[str, ...] = ('title', 'serviceName', 'name')
DESC_KEYS: Tuple[str, ...] = ('desc', 'description', 'serviceIntroduce')


class UnknownApi(ValueError):
    """不在 `WRITE_API_SET` 里的写接口名 —— 说明调用方拼错了，别发出去。"""


class WriteNotConfirmed(RuntimeError):
    """协议层不给默认执行的写入口：构造请求前必须显式 `confirm=True`。"""


# --------------------------------------------------------------------------- 小助手


def assert_write(api: str) -> str:
    """确认这是本模块登记过的**写**接口，返回它本身（方便链式用）。"""
    if api not in WRITE_API_SET:
        raise UnknownApi(f'不是本模块登记的写接口: {api}')
    return api


def assert_read(api: str) -> str:
    """确认这是本模块登记过的**只读**接口。"""
    if api not in READ_APIS:
        raise UnknownApi(f'不是本模块登记的只读接口: {api}')
    return api


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _or_none(value: Any) -> Optional[Any]:
    return None if value is None else value


def _as_bool(value: Any) -> Optional[bool]:
    """三态布尔：认得出就 True/False，认不出就 None（**不猜**）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'yes', 'y', 'success'):
            return True
        if lowered in ('false', '0', 'no', 'n', 'fail'):
            return False
    return None


def _need_confirm(confirm: Any) -> None:
    """写入口的确认闸门：**只有 `confirm is True` 才算确认过**。

    `'false'` / `0` / `None` 一律不放行 —— `bool('false')` 是 `True`，所以这里绝不写 `if not confirm`。
    """
    if confirm is not True:
        raise WriteNotConfirmed('这个接口会真的改动平台上的赔付结果，必须显式传 confirm=true')


def _json(value: Any) -> str:
    """官方用 `JSON.stringify(...)`；这里用紧凑分隔符贴近原文（不带空格）。"""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _id(value: Any, label: str, limit: int = 64) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f'{label}不能为空')
    if len(text) > limit:
        raise ValueError(f'{label}太长了（最多 {limit} 个字符）')
    return text


def _choices(value: Any) -> list:
    """拒绝原因：收数组，也收逗号串（AI 侧传串）。去空、去重、保序。"""
    items: list = []
    if value is None:
        return items
    if isinstance(value, str):
        items = [part.strip() for part in value.split(',')]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [_text(part) for part in value]
    else:
        items = [_text(value)]
    result: list = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _urls(value: Any) -> list:
    """凭证图片 URL：收数组，也收换行/逗号串。"""
    items: list = []
    if value is None:
        return items
    if isinstance(value, str):
        text = value.replace('\r', '\n').replace(',', '\n')
        items = [part.strip() for part in text.split('\n')]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [_text(part) for part in value]
    else:
        items = [_text(value)]
    result: list = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


# --------------------------------------------------------------------------- 请求构造


def detail_request(order_id: Any, service_code: Any) -> Tuple[str, Dict[str, Any]]:
    """赔付详情请求：`{orderId, serviceCode}`（官方 `eM({orderId, serviceCode})`）。"""
    return DETAIL_API, {
        'orderId': _id(order_id, '订单号'),
        'serviceCode': _id(service_code, '赔付服务号'),
    }


def refuse_form_request(service_code: Any) -> Tuple[str, Dict[str, Any]]:
    """拒绝原因表单请求：`{serviceCode}`。"""
    return REFUSE_FORM_API, {'serviceCode': _id(service_code, '赔付服务号')}


def refuse_request(order_id: Any, service_code: Any, reason_choices: Any,
                   reason_desc: Any = '', proof_urls: Any = (),
                   is_manual: Any = False, *, confirm: Any = False) -> Tuple[str, Dict[str, Any]]:
    """拒绝赔付请求（官方形状逐字）。

    - `sellerRefuseReason` 是 **JSON 字符串**：`{"reasonChoices":[...],"reasonDesc":"..."}`；
    - `refuseProofs` 是 **JSON 字符串**（图片 URL 数组）——官方凭证必填；
    - `reasonDesc` 官方最多 200 字；
    - `isManual` 是布尔（官方的 `refuse_and_cco` / `refuse_new` 两种动作传 true）。
    """
    _need_confirm(confirm)
    assert_write(REFUSE_API)
    choices = _choices(reason_choices)
    if not choices:
        raise ValueError('至少选一个拒绝原因（候选来自 refuse.render 的 reasonChoices）')
    text = _text(reason_desc)
    if len(text) > REASON_DESC_MAX:
        raise ValueError(f'拒绝说明最多 {REASON_DESC_MAX} 个字，现在有 {len(text)} 个')
    proofs = _urls(proof_urls)
    if not proofs:
        raise ValueError('拒绝赔付必须上传凭证（至少一张图片 URL）——平台原话是「请上传凭证」')
    data = {
        'orderId': _id(order_id, '订单号'),
        'serviceCode': _id(service_code, '赔付服务号'),
        'sellerRefuseReason': _json({'reasonChoices': choices, 'reasonDesc': text}),
        'refuseProofs': _json(proofs),
        'isManual': bool(is_manual),
    }
    return REFUSE_API, data


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名构造写请求（服务层只传动作名，避免把接口名散在各处）。"""
    if action not in ACTIONS:
        raise UnknownApi(f'不知道这个动作: {action}')
    if action == 'compensate_refuse':
        return refuse_request(**kwargs)
    raise UnknownApi(f'动作 {action} 还没接上构造器')  # pragma: no cover - 目前只有一个动作


# --------------------------------------------------------------------------- 解析


def _module_of(raw: Any) -> Optional[Dict[str, Any]]:
    """把响应剥到 `module` 这一层。

    `valueType:'string'` 会让 `data` 变成 **JSON 字符串**，所以这里逐层解：
    `data` 可能是字符串（先 `json.loads`）、可能是 `{module:{…}}`、也可能本身就是 module
    （`data.data.module` 这种再套一层的也认）。**认不出就是 None，不造数据**；
    空字典也算「没有」。
    """
    value = decode_payload(raw)
    data: Any = None
    if isinstance(value, dict):
        data = decode_payload(value.get('data'))
        if not isinstance(data, dict):
            body = decode_payload(body_of(value))
            data = body if isinstance(body, dict) else None
    if isinstance(data, dict):
        inner = decode_payload(data.get('data')) if isinstance(data.get('data'), (dict, str)) else None
        for candidate in (data, inner):
            if isinstance(candidate, dict) and 'module' in candidate:
                module = decode_payload(candidate.get('module'))
                if isinstance(module, dict):
                    return module or None
        if isinstance(inner, dict):
            return inner or None
        return data or None
    body = decode_payload(body_of(value))
    return body if isinstance(body, dict) and body else None


def parse_detail(raw: Any) -> Dict[str, Any]:
    """赔付详情。认不出的键一律 None（不补 0、不补空串）。"""
    module = _module_of(raw)
    if module is None:
        return {
            'found': False,
            'order_id': None,
            'service_code': None,
            'service_info': None,
            'elements': None,
            'amount': None,
            'amount_yuan': None,
            'title': None,
            'desc': None,
            'raw_keys': [],
        }
    info = module.get('serviceInfo')
    info = info if isinstance(info, dict) else None
    elements = module.get('elements')
    elements = elements if isinstance(elements, list) else None
    first = elements[0] if elements and isinstance(elements[0], dict) else {}

    amount: Optional[Any] = None
    for key in AMOUNT_KEYS:
        if key in module and module[key] is not None:
            amount = module[key]
            break
    amount_yuan: Optional[str] = None
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        amount_yuan = '%.2f' % (amount / 100.0)

    def pick(source: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
        for key in keys:
            text = _text(source.get(key))
            if text:
                return text
        return None

    title = None
    raw_title = first.get('title')
    if isinstance(raw_title, dict):
        title = _text(raw_title.get('text')) or None
    elif raw_title is not None:
        title = _text(raw_title) or None
    if title is None:
        title = pick(module, TITLE_KEYS)
    if title is None and info is not None:
        title = pick(info, TITLE_KEYS)

    desc = pick(first, DESC_KEYS)
    if desc is None:
        desc = pick(module, DESC_KEYS)

    return {
        'found': True,
        'order_id': _text(module.get('orderId')) or None,
        'service_code': _text(module.get('serviceCode')) or None,
        'service_info': info,
        'elements': elements,
        'amount': amount,
        'amount_yuan': amount_yuan,
        'title': title,
        'desc': desc,
        'raw_keys': sorted(str(key) for key in module.keys()),
    }


def parse_refuse_form(raw: Any) -> Dict[str, Any]:
    """拒绝原因候选。**拿不到键给 None**（不是空数组）。"""
    module = _module_of(raw)
    choices: Optional[list] = None
    if module is not None:
        for key in REASON_CHOICES_KEYS:
            value = module.get(key)
            if isinstance(value, list):
                choices = [_text(item) for item in value if _text(item)]
                break
    return {
        'found': module is not None,
        'reason_choices': choices,
        'raw_keys': sorted(str(key) for key in module.keys()) if module else [],
    }


def parse_refuse(raw: Any) -> Dict[str, Any]:
    """拒绝赔付的回执。

    官方只看「promise 有没有 reject」就 toast 成功，所以这里：平台**明确**说失败
    （`success:false` / `result:false`）才算 `saved=False` 并带上平台原话；其余按已受理。
    """
    body = decode_payload(body_of(decode_payload(raw)))
    explicit: Optional[bool] = None
    message = envelope_message(raw)
    if isinstance(body, dict):
        for key in ('success', 'succeed', 'result'):
            if key in body:
                explicit = _as_bool(body.get(key))
                if explicit is not None:
                    break
        for key in ('failReason', 'message', 'msg', 'errorMsg'):
            text = _text(body.get(key))
            if text:
                message = text
                break
    return {
        'saved': True if explicit is None else explicit,
        'platform_success': explicit,
        'platform_message': message,
        'envelope': raw if isinstance(raw, dict) else None,
    }


def envelope_failure(raw: Any) -> Optional[str]:
    """信封级失败说明（服务层判「平台拒绝了」用；无权限也会在这里现形）。"""
    return envelope_error(raw)
