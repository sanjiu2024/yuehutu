"""买家申请修改收货地址（订单内的审批流）—— 协议层。

**这是什么**：买家在付款后、发货前改了收货地址，平台会把这条申请挂在订单上等卖家处理
（同意 / 拒绝）。官方卖家工作台在订单列表里给了一个入口：点开先查这条申请，`modifyStatus`
是 `WAITING` 时把「现在的收件地址」和「买家申请改成的地址」并排给卖家看，再让卖家点同意/拒绝。

**证据（全部来自本地 trade bundle，`C:\\Users\\Administrator\\.dsh-tmp\\trade_bundle\\main.js`，
3,879,584 字符的单行压缩 JS ⇒ 只有字符偏移有意义，行号无意义；取证脚本在 `.dsh-tmp` 下，
零平台请求）**：

* 读，偏移 **3101551**：
  `hO({api:"mtop.idle.merchant.order.get.modify.address.info", v:"1.0", data:{bizOrderId:e}})`
  响应 `data.module = {modifyStatus, modifyAddressInfoVO, nowAddressInfoVO, modifyDeliverId}`；
  官方**只在 `modifyStatus === 'WAITING'` 时**才把 `modifyDeliverId` 与两个 VO 往下传
  （`"WAITING"===r&&(L(a),w(i),S(o))`），组件初值 `modifyStatus = 'NONE'`。
* 同意，偏移 **3102802**：
  `hO({api:"mtop.idle.merchant.order.address.modify.agree", v:"1.0", data:{bizOrderId:e, deliverId:t}})`
  → 成功 toast「已同意修改地址」。
* 拒绝，偏移 **3103173**：同形状的 `…address.modify.refuse` → toast「已拒绝修改地址」。
* 隐私地址解密（`nowAddressInfoVO.privacyFlag` 为真时官方才调），偏移 **2127351** 的 `nP` 助手：
  `hO({api:"mtop.idle.trade.order.detail.privacy.address", v:"1.0", valueType:"string",
       needLoginPC:true, data:{bizOrderId:e, pageSource:t}})`
  改地址场景传 `pageSource='modify_address'`（订单列表那处传 `'order_list'`），
  返回 `{name, suffix, phoneNumber, address}`。
* 官方还有一道闸（偏移 **3101000**）：订单状态是 `NOT_PAY` / `待付款` 时**连查都不查**，
  直接不可操作 —— 这条闸在 UI 侧（订单行数据里有状态），协议层没有对应参数。

**为什么 PrivacyFlag 要单独处理**：平台对「隐私号」订单会把姓名/电话/地址打码，
官方必须先调一次解密接口才拿得到明文。我们**不假装解开了**：解不开就把 `decrypted` 给
`None` 并带一条中文 warning，页面照原样显示平台给的打码字段。

**未验证边界**：一次真机请求都没发过（本机这个账号对交易写接口多数回
`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，而红线是不拿真实订单去试）。所以「平台是否接受这套参数」
仍属未验证，只保证形状与官方页面逐字一致。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .trade import body_of, decode_payload, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 查这条订单上的改地址申请。bundle 偏移 3101551。
INFO_API = 'mtop.idle.merchant.order.get.modify.address.info'
#: 同意修改地址。bundle 偏移 3102802。
AGREE_API = 'mtop.idle.merchant.order.address.modify.agree'
#: 拒绝修改地址。bundle 偏移 3103173。
REFUSE_API = 'mtop.idle.merchant.order.address.modify.refuse'
#: 隐私地址解密（只读）。bundle 偏移 2127351 的 `nP` 助手。
PRIVACY_API = 'mtop.idle.trade.order.detail.privacy.address'

#: 四个接口的版本号（bundle 原文全是 `v:"1.0"`）。
DEFAULT_VERSION = '1.0'

#: 解密接口**必须**带 `valueType:'string'`（bundle 原文如此）。我们的 mtop 客户端默认不带
#: 这个参数，所以这一条要显式传下去，否则响应形状可能不是官方看到的那一种。
PRIVACY_VALUE_TYPE = 'string'

#: 解密接口的 `pageSource`：改地址场景官方传的就是这个字面量。
PAGE_SOURCE_MODIFY = 'modify_address'

#: 只有这个状态才代表「有申请待处理」，也才会给 `modifyDeliverId`。
STATUS_WAITING = 'WAITING'

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 字符偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'agree': {
        'api': AGREE_API,
        'data': '{bizOrderId, deliverId}',
        'evidence': 'bundle @3102802 `…modify.agree … data:{bizOrderId:e, deliverId:t}`；toast「已同意修改地址」',
    },
    'refuse': {
        'api': REFUSE_API,
        'data': '{bizOrderId, deliverId}',
        'evidence': 'bundle @3103173 `…modify.refuse … data:{bizOrderId:e, deliverId:t}`；toast「已拒绝修改地址」',
    },
}

#: 只读接口。单独列出来，免得被当成写接口加进黑名单，也免得有人给它加 confirm 闸门。
READ_APIS: Tuple[str, ...] = (INFO_API, PRIVACY_API)

#: 本模块登记的写接口（`assert_write` 用它挡住误用）。
ALL_APIS = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 官方 VO 里我们**确认见过读法**的字段（偏移 3102200~3102400：`e.name` / `e.phoneNumber` /
#: `e.address` / `e.suffix`，以及 `_namePlaintext` / `_phoneNumberPlaintext` / `_addressPlaintext`
#: 三个明文回退）。页面渲染用它挑「已知键」，其余键原样进「其它字段」——**不猜**。
ADDRESS_FIELDS: Tuple[str, ...] = (
    'name', 'namePlaintext', 'suffix',
    'phoneNumber', 'phoneNumberPlaintext',
    'address', 'addressPlaintext',
    'privacyFlag', 'maskedName',
)


class UnknownApi(ValueError):
    """不在 `ALL_APIS` 里的写接口名 —— 说明调用方拼错了，别发出去。"""


class WriteNotConfirmed(RuntimeError):
    """协议层不给默认执行的写入口：构造请求前必须显式 `confirm=True`。"""


def assert_write(api: str) -> str:
    """只允许本模块登记的两个写接口通过。"""
    if api not in ALL_APIS:
        raise UnknownApi(f'{api} 不在改地址写接口白名单里')
    return api


# --------------------------------------------------------------------------- 工具


def _text(value: Any) -> str:
    """空值一律 ''，数字转字符串（订单号 / deliverId 真机可能是 int，必须保精度）。"""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return ''


def _or_none(value: Any) -> Optional[Any]:
    """空容器 / 空串一律 None（「平台没给」和「平台给了空」在页面上是两回事）。"""
    if value is None:
        return None
    if isinstance(value, (str, bytes, list, tuple, dict, set)) and len(value) == 0:
        return None
    return value


def _dict_or_none(value: Any) -> Optional[Dict[str, Any]]:
    return dict(value) if isinstance(value, dict) and value else None


def _need_confirm(confirm: Any) -> None:
    if confirm is not True:
        raise WriteNotConfirmed('同意 / 拒绝修改地址是写操作，需要显式确认（confirm=True）后才会提交')


# --------------------------------------------------------------------------- 请求构造


def info_request(order_id: Any) -> Tuple[str, Dict[str, Any]]:
    """查改地址申请（**只读**，所以没有 `confirm` 参数）。"""
    return INFO_API, {'bizOrderId': _text(order_id)}


def privacy_request(order_id: Any, page_source: str = PAGE_SOURCE_MODIFY) -> Tuple[str, Dict[str, Any]]:
    """隐私地址解密（**只读**）。`pageSource` 官方在改地址场景传 `modify_address`。"""
    return PRIVACY_API, {
        'bizOrderId': _text(order_id),
        'pageSource': _text(page_source) or PAGE_SOURCE_MODIFY,
    }


def _need_ids(order_id: Any, deliver_id: Any) -> Tuple[str, str]:
    """写接口的底线校验：空订单号 / 空 `deliverId` 一律不发。

    服务层也会挡（`_missing`），这里是**第二道**——协议层是最后一站，漏一个空字符串过去就
    可能把改地址请求发到平台上。
    """
    order = _text(order_id)
    deliver = _text(deliver_id)
    if not order:
        raise ValueError('订单号不能为空')
    if not deliver:
        raise ValueError('deliverId 不能为空')
    return order, deliver


def agree_request(order_id: Any, deliver_id: Any, *, confirm: Any = False) -> Tuple[str, Dict[str, Any]]:
    """同意改地址。未显式 `confirm=True` 直接抛 —— 这是会真的改订单收货地址的操作。"""
    _need_confirm(confirm)
    order, deliver = _need_ids(order_id, deliver_id)
    return assert_write(AGREE_API), {'bizOrderId': order, 'deliverId': deliver}


def refuse_request(order_id: Any, deliver_id: Any, *, confirm: Any = False) -> Tuple[str, Dict[str, Any]]:
    """拒绝改地址。未显式 `confirm=True` 直接抛。"""
    _need_confirm(confirm)
    order, deliver = _need_ids(order_id, deliver_id)
    return assert_write(REFUSE_API), {'bizOrderId': order, 'deliverId': deliver}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名构造请求（`mcp_server` / 主管 AI 那侧传的是字符串动作）。"""
    builders = {'agree': agree_request, 'refuse': refuse_request}
    build = builders.get(_text(action))
    if build is None:
        raise UnknownApi(f'未知的改地址动作: {action!r}')
    return build(**kwargs)


# --------------------------------------------------------------------------- 响应解析


def decode_privacy_payload(value: Any) -> Dict[str, Any]:
    """解开一次客户端返回值，拿到解密后的地址字段。

    解密接口带 `valueType:'string'`，业务体可能是 JSON 字符串 —— `trade.body_of()` 已经
    统一处理了「字符串 / `data.module` / `data.data` / `data` 本身」这几种容器，这里直接用。
    **解不开就给空 dict**：让上层显示「平台没给」，而不是抛异常把整页打挂。
    """
    if not isinstance(value, dict):
        return {}
    body = body_of(value)
    if isinstance(body, dict):
        return dict(body)
    return {}


def parse_info(raw: Any) -> Dict[str, Any]:
    """改地址申请（`data.module` 那一层；`body_of` 已经帮我们下钻到 module）。

    **不假装**：`status` 拿不到就是 `None`（页面显示 `-`，不编一个 `NONE`）；
    `deliver_id` 只有 `status == 'WAITING'` 时才认（平台只在那一档才给）；
    两个 VO 原样透传，平台没给就是 `None`（不是空 dict）。
    """
    raw = decode_payload(raw) if isinstance(raw, dict) else raw
    body = body_of(raw) if isinstance(raw, dict) else {}
    if not isinstance(body, dict):
        body = {}
    now = body.get('nowAddressInfoVO')
    status = _text(body.get('modifyStatus')) or None
    deliver_id = _text(body.get('modifyDeliverId')) or None
    if status != STATUS_WAITING:
        # 官方也只在 WAITING 时用 modifyDeliverId；别的状态下这个值没有意义，不能拿去发请求。
        deliver_id = None
    return {
        'status': status,
        'deliver_id': deliver_id,
        'can_operate': status == STATUS_WAITING and bool(deliver_id),
        'now_address': _dict_or_none(now),
        'modify_address': _dict_or_none(body.get('modifyAddressInfoVO')),
        'privacy_flagged': bool(now.get('privacyFlag')) if isinstance(now, dict) else False,
        'platform_message': envelope_message(raw) if isinstance(raw, dict) else None,
    }


def parse_privacy(raw: Any) -> Optional[Dict[str, Any]]:
    """解密结果 `{name, suffix, phoneNumber, address}`；解不开/平台没给一律 `None`。"""
    body = decode_privacy_payload(raw)
    if not body:
        return None
    payload = {
        'name': _text(body.get('name')) or None,
        'suffix': _text(body.get('suffix')) or None,
        'phone_number': _text(body.get('phoneNumber')) or _text(body.get('phone')) or None,
        'address': _text(body.get('address')) or None,
    }
    if not any(payload.values()):
        return None
    return payload


def parse_write(raw: Any) -> Dict[str, Any]:
    """同意 / 拒绝的结果：只透出平台**确实给了**的东西。"""
    raw = decode_payload(raw) if isinstance(raw, dict) else raw
    success = raw.get('success') if isinstance(raw, dict) else None
    return {
        'platform_success': success if isinstance(success, bool) else None,
        'platform_message': envelope_message(raw) or None,
        'platform_body': body_of(raw) or None,
    }


def parse_write_result(raw: Any) -> Dict[str, Any]:
    """别名，和 `xianyu.trade_write.parse_write_result` 同形状，方便服务层统一调用。"""
    return parse_write(raw)


def envelope_failure(raw: Any) -> Optional[str]:
    """平台级失败原文（`FAIL_BIZ_*` / `NO_PERMISSION` …），成功时 None。"""
    return envelope_error(raw) if isinstance(raw, dict) else None
