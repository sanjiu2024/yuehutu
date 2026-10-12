# -*- coding: utf-8 -*-
"""客服分流写接口（官方卖家工作台「小铺 → 客服分流」页）—— 协议层。

覆盖两个官方微应用接口（静态 bundle 反解，**零平台请求**）：

1. `mtop.taobao.idlemessage.customer.routing.rule.operate` —— 保存分流规则
   （分配方式 / 消费者重复进线策略 / 离线消息处理方式 / 服务时间）。
2. `mtop.taobao.idlemessage.customer.group.operate` —— 新建、修改、删除客服分组。

**接口来源**：官方客服分流微应用
`https://g.alicdn.com/idle-pc/idle-seller-cs-dispatch/…/js/main.js`（1,168,301 字符单行压缩 JS）。
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\csdispatch\\main.js`，**证据用字符偏移**
（单行文件行号无意义），偏移与调用点原文见每个常量与 `WRITE_APIS[*]['evidence']`。

**为什么单独开一个模块**：`backend/app/xianyu/shop.py` 是「小铺」只读协议，它把这两个
operate 接口登记在自己的 `WRITE_APIS` 黑名单里（只读路径必须拦住它们，那个黑名单保留不动）；
写协议独立成模块便于逐条标注证据，也让 `assert_write()` 的白名单边界清晰。

**两个接口的共同包装形状**（与 `xianyu/trade.py` 那族 `U6({api, v, data})` 不同）：
业务字段直接放 `data`，但**其中复合结构要自己 `JSON.stringify`**（不是让平台序列化）：

- 分流规则：`data = {routingRules: JSON.stringify(rule)}`
- 分组：`data = {operateType, groupId?, groupName, serviceRange: JSON.stringify(range),
  customers: JSON.stringify(list)}`

所以这里返回 `(api, data)` 二元组，由服务层统一
`common.call_method(client, ('mtop',), api, data, v='1.0')` 发出去。

**成功判据的诚实说明**：官方前端**从不读这两个接口的响应体** —— 保存成功靠
`toast('保存成功')` + `postMessage({type:'ROUTING_RULE_SAVED'|'GROUP_SAVED'})`，
之后**重拉列表**确认（`customer.routing.rule.info.get` / `customer.group.get`）。
所以解析层只做「客户端没报错 且 平台没报错码」这一层判断，绝不假装拿到了新配置；
响应里真的带了 `routingRules` / `groupId` 才回显，拿不到就是 `None`（不造数据）。

**未验证边界**：一次真机写请求都没发过 —— 这会真的改动卖家的客服分流配置与分组，
红线不允许为了验证去动它。字段形状与官方页面逐字一致，但「平台是否接受」仍属未验证。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence, Tuple

from .trade import body_of, decode_payload, envelope_message
from .trade import envelope_error as trade_envelope_error

# --------------------------------------------------------------------------- 接口常量

#: 保存分流规则。bundle 偏移 571596：
#: `Ry = e => …({api:"mtop.taobao.idlemessage.customer.routing.rule.operate", v:"1.0", data:e, …})`
#: 调用点偏移 1151612：`e = {routingRules: {distributionMethod: a, repeatCustomer: l,
#: offlineMessage: m, …(m === SPECIFIED_ACCOUNT && {specifiedAccountId: Number(_),
#: specifiedAccountNick: h}), …(P && {serviceTime: P})}}` → `Ry(e)`。
ROUTING_RULE_API = 'mtop.taobao.idlemessage.customer.routing.rule.operate'

#: 新建 / 修改 / 删除客服分组。bundle 偏移 572287：
#: `Dy = e => …({api:"mtop.taobao.idlemessage.customer.group.operate", v:"1.0",
#: data: (0, d._)({}, e, {serviceRange: JSON.stringify(e.serviceRange),
#: customers: JSON.stringify(e.customers)}), …})`。
#: 保存调用点偏移 1129731；删除调用点偏移 1149031（`{operateType:'delete', groupId}`）。
GROUP_API = 'mtop.taobao.idlemessage.customer.group.operate'

#: 两个接口的版本号（bundle 原文都是 `v:"1.0"`）。
DEFAULT_VERSION = '1.0'

# --------------------------------------------------------------------------- 枚举（逐字来自 bundle）

#: 分配方式 `Iy`（bundle 偏移 571100 附近定义，`Ay` 是它的「值 → 中文」表）。
#: 中文标签（官方原文）：LOW_SATURATION_FIRST「优先分配给当前饱和度低的客服」、
#: LOW_DAILY_COUNT_FIRST「优先分配给当日接待量少的客服」、LAST_SERVED_AGENT「最近联系客服优先」、
#: COMBINED_LOW_LOAD「正常分流」。
DISTRIBUTION_METHODS: Tuple[str, ...] = (
    'LOW_SATURATION_FIRST',
    'LOW_DAILY_COUNT_FIRST',
    'LAST_SERVED_AGENT',
    'COMBINED_LOW_LOAD',
)

#: 消费者重复进线策略 `repeatCustomer`：**与分配方式共用同一套 `Iy` 值**
#: （证据：加载路径偏移 1157330 是 `Ay[c.repeatCustomer]`，而 `Ay` 只登记了上面四个键）。
#: 官方表单可能只给其中子集，但 bundle 里没有可直接引用的子集定义，所以这里**不缩窄**，
#: 传了非法值时由平台自己拒绝，错误原文原样透出。
REPEAT_CUSTOMER_METHODS: Tuple[str, ...] = DISTRIBUTION_METHODS

#: 离线消息处理方式 `Iy`：MAIN_ACCOUNT「离线消息发给主账号」、
#: SPECIFIED_ACCOUNT「离线消息发送给指定账号」。
OFFLINE_MESSAGES: Tuple[str, ...] = ('MAIN_ACCOUNT', 'SPECIFIED_ACCOUNT')

#: 分组服务范围 `mode`。**大小写不确定，故意原样透传**：
#: 写侧枚举 `Dj` 的值是小写（`UNSPECIFIED="unspecified"`、`SPECIFIED="specified"`，bundle 偏移 571400 附近），
#: 而读侧渲染判的是大写（`"SPECIFIED"===t.mode` / `"DEFAULT"===t.mode`，偏移 1147696）。
#: 平台两侧大小写可能本来就不一致 ⇒ 我们**不做大小写转换**（转错了平台会拒，但那是我们猜的），
#: 把调用方给的值原样发出去，并接受下面这一组已知写法。
GROUP_MODES: Tuple[str, ...] = (
    'unspecified', 'specified', 'UNSPECIFIED', 'SPECIFIED', 'DEFAULT',
)

#: 分组写操作类型（官方 `g === Lj.EDIT ? 'update' : 'create'`，删除是 `'delete'`）。
GROUP_OPERATE_TYPES: Tuple[str, ...] = ('create', 'update', 'delete')

#: `specifiedConfig.type` 的官方缺省值（bundle 原文 `type: P || "ORDER"`）。
SPECIFIED_TYPE_DEFAULT = 'ORDER'

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'routing_rule': {
        'api': ROUTING_RULE_API,
        'data': '{routingRules: JSON 字符串 {distributionMethod, repeatCustomer, offlineMessage,'
                ' specifiedAccountId?, specifiedAccountNick?, serviceTime?{startTime,endTime}}}',
        'evidence': 'bundle @571596 声明 / 保存调用点 @1151612（`Ry({routingRules: …})`）'
                    '/ 加载路径 @1157330 读 `e.data.routingRules`',
    },
    'group_save': {
        'api': GROUP_API,
        'data': '{operateType: "create"|"update", groupId?, groupName,'
                ' serviceRange: JSON 字符串 {mode, specifiedConfig?{type,typeName,typeData}},'
                ' customers: JSON 字符串 [{customerUid}]}',
        'evidence': 'bundle @572287 声明 / 保存调用点 @1129731（`Dy({operateType, groupName,'
                    ' serviceRange, customers})`）/ 客服候选列表来自 @875113 的'
                    ' `e.data.customers[]`（`customerUid` 用 `Number()` 转）',
    },
    'group_delete': {
        'api': GROUP_API,
        'data': '{operateType: "delete", groupId}（**不带** serviceRange / customers ——'
                ' 官方删除路径只传这两个键）',
        'evidence': 'bundle @1149031 调用点 `Dy({operateType:"delete", groupId: e.groupId})`',
    },
}

#: 本模块只登记写接口，全部都在这里（`assert_write` 用它挡住误用）。
ALL_APIS = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 分组名称长度上限。bundle 里没有可引用的 schema 常量，这里取官方输入框的可见宽度 30 字，
#: 仅作本地兜底（平台拒绝时错误原文原样透出）。
GROUP_NAME_MAX = 30

#: 服务时间的时刻格式（官方表单是 `HH:MM` 字符串，如 `08:00` / `23:00`）。
SERVICE_TIME_FORMAT = 'HH:MM'


class UnknownApi(ValueError):
    """不在 `ALL_APIS` 里的接口名 —— 说明调用方拼错了，别发出去。"""


class WriteNotConfirmed(RuntimeError):
    """协议层不给默认执行的写入口：构造请求前必须显式 `confirm=True`。"""


def assert_write(api: str) -> str:
    """只允许本模块登记的两个写接口通过（分组的新增/修改/删除共用 `GROUP_API`）。"""
    if api not in ALL_APIS:
        raise UnknownApi(f'{api} 不在客服分流写接口白名单里')
    return api


# --------------------------------------------------------------------------- 工具


def _text(value: Any) -> str:
    """空值一律 ''，数字转字符串（`customerUid` 真机可能是 int，必须保精度）。"""
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


def _json(value: Any) -> str:
    """平台要的是 **JSON 字符串**（官方用 `JSON.stringify`）。

    用 `ensure_ascii=False` 与 `separators=(',', ':')` 贴近 `JSON.stringify` 的输出：
    中文分组名不转义、无多余空格。转义与否对平台解析结果等价，但字节上更像官方。
    """
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _one_of(label: str, value: Any, allowed: Sequence[str]) -> str:
    text = _text(value)
    if text not in allowed:
        raise ValueError(f'{label} 只能是 {" / ".join(allowed)}，收到 {value!r}')
    return text


def _account_ref(value: Any) -> Any:
    """账号引用：纯数字转 `int`（官方就是 `Number(_)`），其余原样保留字符串。"""
    text = _text(value)
    if text.isdigit():
        return int(text)
    return text


# --------------------------------------------------------------------------- 请求构造


def routing_rule_request(distribution_method: Any, repeat_customer: Any, offline_message: Any,
                         specified_account_id: Any = None, specified_account_nick: Any = None,
                         service_time: Any = None, *,
                         confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """保存分流规则（`confirm=True` 才构造）。

    `specified_account_id` / `specified_account_nick` **只在** `offline_message ==
    'SPECIFIED_ACCOUNT'` 时才有意义（官方原文就是 `m === SPECIFIED_ACCOUNT && {…}`）；
    此时两者都必须给，否则本地直接拒绝（官方也是先 `warning('请选择指定的客服账号')` 就不发）。
    """
    _need_confirm(confirm)
    method = _one_of('分配方式 distribution_method', distribution_method, DISTRIBUTION_METHODS)
    repeat = _one_of('重复进线策略 repeat_customer', repeat_customer, REPEAT_CUSTOMER_METHODS)
    offline = _one_of('离线消息处理方式 offline_message', offline_message, OFFLINE_MESSAGES)

    rule: Dict[str, Any] = {
        'distributionMethod': method,
        'repeatCustomer': repeat,
        'offlineMessage': offline,
    }
    if offline == 'SPECIFIED_ACCOUNT':
        account_id = _text(specified_account_id)
        nick = _text(specified_account_nick)
        if not account_id:
            raise ValueError('离线消息发给指定账号时必须给 specified_account_id')
        if not nick:
            raise ValueError('离线消息发给指定账号时必须给 specified_account_nick')
        rule['specifiedAccountId'] = _account_ref(account_id)
        rule['specifiedAccountNick'] = nick

    if service_time is not None:
        if not isinstance(service_time, dict):
            raise ValueError('service_time 必须是 {"start_time": "HH:MM", "end_time": "HH:MM"}')
        start = _text(service_time.get('start_time'))
        end = _text(service_time.get('end_time'))
        if not start or not end:
            raise ValueError(f'service_time 的 start_time / end_time 都不能为空（格式 {SERVICE_TIME_FORMAT}）')
        rule['serviceTime'] = {'startTime': start, 'endTime': end}

    return assert_write(ROUTING_RULE_API), {'routingRules': _json(rule)}


def group_save_request(group_name: Any, mode: Any, operate_type: Any = 'create',
                       group_id: Any = None, specified_type: Any = None,
                       specified_type_name: Any = None, type_data: Any = None,
                       customer_uids: Any = None, *,
                       confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """新建 / 修改客服分组（`confirm=True` 才构造）。

    - `operate_type='update'` 必须给 `group_id`；`create` 给了也不发（官方只在编辑态带）。
    - `mode` 属于 `GROUP_MODES`（大小写原样透传，见该常量的说明）。
    - `mode` 是 specified 时才带 `specifiedConfig`（官方 `g === Dj.SPECIFIED && {specifiedConfig: …}`）；
      `type_data` 为空数组时**不放这个键**（官方 `x.length > 0 ? x : void 0`）。
    - `customer_uids` 里的每一项按官方 `{customerUid: Number(uid)}` 转换。
    """
    _need_confirm(confirm)
    kind = _one_of('操作类型 operate_type', operate_type, ('create', 'update'))
    name = _text(group_name)
    if not name:
        raise ValueError('分组名称 group_name 不能为空')
    if len(name) > GROUP_NAME_MAX:
        raise ValueError(f'分组名称最多 {GROUP_NAME_MAX} 个字，收到 {len(name)} 个')
    scope = _one_of('服务范围 mode', mode, GROUP_MODES)

    key = _text(group_id)
    if kind == 'update' and not key:
        raise ValueError('修改分组时必须给 group_id')

    service_range: Dict[str, Any] = {'mode': scope}
    if scope.lower() == 'specified':
        config: Dict[str, Any] = {'type': _text(specified_type) or SPECIFIED_TYPE_DEFAULT}
        type_name = _text(specified_type_name)
        if type_name:
            config['typeName'] = type_name
        if isinstance(type_data, (list, tuple)) and len(type_data) > 0:
            config['typeData'] = list(type_data)
        service_range['specifiedConfig'] = config

    customers = []
    for uid in (customer_uids or []):
        text = _text(uid).strip()
        if not text:
            continue
        customers.append({'customerUid': _account_ref(text)})

    data: Dict[str, Any] = {
        'operateType': kind,
        'groupName': name,
        'serviceRange': _json(service_range),
        'customers': _json(customers),
    }
    if kind == 'update':
        data['groupId'] = key
    return assert_write(GROUP_API), data


def group_delete_request(group_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """删除客服分组（`confirm=True` 才构造）。官方只传 `operateType` + `groupId`。"""
    _need_confirm(confirm)
    key = _text(group_id)
    if not key:
        raise ValueError('分组 group_id 不能为空')
    return assert_write(GROUP_API), {'operateType': 'delete', 'groupId': key}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名取请求（`WRITE_APIS` 的键），便于服务层统一分发与单测。"""
    if action == 'routing_rule':
        return routing_rule_request(
            kwargs.get('distribution_method'), kwargs.get('repeat_customer'),
            kwargs.get('offline_message'), kwargs.get('specified_account_id'),
            kwargs.get('specified_account_nick'), kwargs.get('service_time'),
            confirm=kwargs.get('confirm'))
    if action == 'group_save':
        return group_save_request(
            kwargs.get('group_name'), kwargs.get('mode'), kwargs.get('operate_type', 'create'),
            kwargs.get('group_id'), kwargs.get('specified_type'),
            kwargs.get('specified_type_name'), kwargs.get('type_data'),
            kwargs.get('customer_uids'), confirm=kwargs.get('confirm'))
    if action == 'group_delete':
        return group_delete_request(kwargs.get('group_id'), confirm=kwargs.get('confirm'))
    raise UnknownApi(f'{action} 不是客服分流写动作（可选：{", ".join(ACTIONS)}）')


# --------------------------------------------------------------------------- 响应解析
#
# 约定与 `xianyu/trade.py` / `xianyu/address_write.py` 一致：入参是**客户端的返回值**
# `{'success', 'data', 'message', 'ret'}`，平台业务信封 `{code, msg, data}` 在 `raw['data']`。


def envelope(raw: Any) -> Dict[str, Any]:
    """业务信封（`raw['data']`）里的字典；不是字典就给空字典。"""
    body = body_of(raw)
    return body if isinstance(body, dict) else {}


def business(raw: Any) -> Dict[str, Any]:
    """摊平后的业务体：`decode_payload` 解掉字符串 JSON，非字典给空字典。"""
    decoded = decode_payload(raw)
    return decoded if isinstance(decoded, dict) else {}


def code_of(raw: Any) -> str:
    """平台业务码（大写比较留给调用方）。"""
    env = envelope(raw)
    return _text(env.get('code'))


def envelope_error(raw: Any) -> str:
    """平台是否失败（`''` 表示成功）：客户端层与业务信封**两层都看**。

    比 `xianyu/trade.py::envelope_error` 多看一眼内层 —— 真机出现过
    「外层 `success` 为真、内层 `data.code` 却是失败」的双层信封（只读侧的口径见
    `services/shop.py::_unwrap` 与 `test_shop_api.py::test_sub_accounts_business_error_inside_envelope_is_502`）。
    写接口如果只看外层，**平台拒绝会被当成保存成功** —— 这是最危险的一类假成功。

    业务码只认 `success`（大小写不敏感），与只读侧的 `_unwrap` 同口径；
    业务体里**没有** `code` 键时以客户端结论为准（官方前端根本不读这两个响应体，
    成功体可能是空对象）。
    """
    outer = trade_envelope_error(raw)
    if outer:
        return outer
    env = envelope(raw)
    code = _text(env.get('code')).strip()
    if code and code.lower() != 'success':
        return _text(env.get('msg')) or _text(env.get('message')) or code
    return ''


def message_of(raw: Any) -> str:
    """失败原因：平台 `msg` → 客户端 `message` → `ret[0]`。

    两个接口的官方前端都不读响应体，所以客户端自己失败时平台信封是空的；
    不退到 `ret[0]` 的话用户只会看到一句「平台没有返回成功」。
    """
    platform = _text(envelope(raw).get('msg')) or _text(business(raw).get('msg'))
    if platform:
        return platform
    client_message = _text(envelope_message(raw))
    if client_message:
        return client_message
    if isinstance(raw, dict):
        ret = raw.get('ret')
        if isinstance(ret, (list, tuple)) and ret:
            return _text(ret[0])
    return ''


def is_success(raw: Any) -> bool:
    """成功判据：客户端没报错 **且** 平台没有报错码。

    **不要求业务体里有数据** —— 官方从不读这两个响应体，保存成功可能只回一个空对象。
    有 `code` 时只认 `success`；没有 `code` 时以客户端结论为准。
    """
    if not isinstance(raw, dict):
        return False
    if raw.get('success') is False:
        return False
    code = code_of(raw)
    if code:
        return code.lower() == 'success'
    return True


def failure_reason(raw: Any, fallback: str = '') -> str:
    """失败原因：平台 `msg` 原文优先，没有再用兜底文案。"""
    return message_of(raw) or fallback


def saved_rule_of(raw: Any) -> Optional[Dict[str, Any]]:
    """平台回显的分流规则（有就回显，没有一律 `None`）。

    官方前端保存后是**重拉** `rule.info.get` 看结果，所以我们不指望这里一定有内容；
    拿不到就是 `None`，**不造** 一份「看起来保存成功了」的规则。
    """
    for body in (envelope(raw), business(raw)):
        rule = body.get('routingRules')
        if isinstance(rule, dict):
            return rule
        if isinstance(rule, str) and rule.strip():
            try:
                parsed = json.loads(rule)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def group_id_of(raw: Any) -> Optional[str]:
    """平台回显的分组 id（新增成功后才有意义）；拿不到一律 `None`。"""
    for body in (envelope(raw), business(raw)):
        for key in ('groupId', 'group_id', 'id'):
            value = _text(body.get(key))
            if value:
                return value
    return None


#: 动作名登记（服务层 / AI 工具层共用的稳定名字）。
ACTIONS: Sequence[str] = tuple(WRITE_APIS.keys())
