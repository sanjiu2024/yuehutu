# -*- coding: utf-8 -*-
"""IM 会话操作（官方闲鱼客服工作台的会话级动作）：拉黑状态查询 / 拉黑买家 / 解除拉黑 /
给买家发商品卡片 / 转交会话 / 结束会话 / 重新发起聊天。

**接口来源（静态 bundle 反解，零风控）**：官方 IM 微应用
`https://g.alicdn.com/idle-pc/p-im-index/…/js/index.js`（3,071,011 B，单行压缩 JS），
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\bundle_scan\\js_006_p_im_index.js`；
第 43 批的会话接管族来自**卖家工作台** bundle
`C:\\Users\\Administrator\\.dsh-tmp\\seller_scan\\js_008_vendors.js`（5,857,382 B）。
**证据一律用字符偏移**。

**为什么单独开一个模块**：`backend/app/xianyu/im.py` 管的是**消息通道**（WebSocket 收消息、
历史消息、发文本），这几条是**会话级动作**（对某个会话做一件事），调用点、参数形状、版本号
都与消息通道无关；混进 `im.py` 会让「消息通道」那个文件既管长连接又管写操作。

**版本号必须按动作分别传**（这是本模块最容易被抄错的地方）：
* `blacklist.query` → **1.0**
* `blacklist.add` → **2.0**（只有它是 2.0）
* `blacklist.remove` → **1.0**
* `message.card.send` → **1.0**
* `customer.deliver.session` / `customer.leave.session` / `customer.rejoin.session` → **1.0**

所以构造器统一返回 **三元组 `(api, data, version)`**（`xianyu/logistics.py` 那一族是二元组，
因为那一族三条全是 `v:"1.0"`）。服务层照返回值原样发
`common.call_method(client, ('mtop',), api, data, v=version)`。

**两条「同一件事、两套 mtop」的坑（写下来免得以后抄错）**：
* 发卡片：本模块 `message.card.send` 用 `{sessionId, itemId}`（IM bundle 的**商品卡片**）；
  卖家工作台 bundle 里同一个 api 还有一处 `{orderId, sessionId}`（@1905901 附近的
  **订单卡片**）。我们做的是商品卡片，订单卡片那条没有页面入口证据，**没实现**。
* 关单/延迟确认：本模块不做；`backend/app/xianyu/trade_write.py` 用的是卖家工作台那套
  （`trade.merchant.close.by.seller` v2.0 等），IM bundle 里的同族名字形状不同，**故意不用**。

**`sessionId` 原样透传，不替平台加/去 `@goofish` 后缀**：官方 IM 前端在 6 处把展示用的会话 id
做了 `replace('@goofish','')`，但从这些静态代码看不出 mtop 入参要的是带后缀还是不带后缀
（我们库里 `conversations.cid` 存的是**去掉后缀**的那个）。为了不凭空发明，这里只做
「去掉首尾空白」，不做任何后缀变换；真实形状等一次真机只读调用确认。
（消息通道那边是另一回事：`backend/app/xianyu/im.py` 的 WS 帧确实用 `_with_goofish` 补后缀。）

**未验证边界**：一次真机请求都没发过（第 40/43 批的验证红线是「零平台写请求」）。
七条接口的参数形状与成功判据全部来自 bundle 静态证据。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .trade import body_of, decode_payload, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 拉黑状态查询（只读）。bundle 偏移 2619754 附近：
#: `gC({api:"mtop.taobao.idlemessage.pc.blacklist.query", v:"1.0", data:{sessionId:e}})`;
#: 响应读 `data.isInBlack` 与 `data.showBlackList`（官方对后者用 `?? true`）。
BLACKLIST_QUERY_API = 'mtop.taobao.idlemessage.pc.blacklist.query'

#: 拉黑买家（写）。**版本是 2.0**：`{api:"mtop.taobao.idlemessage.pc.blacklist.add", v:"2.0", data:{sessionId}}`。
BLACKLIST_ADD_API = 'mtop.taobao.idlemessage.pc.blacklist.add'

#: 解除拉黑（写）。版本 1.0：`{api:"mtop.taobao.idlemessage.pc.blacklist.remove", v:"1.0", data:{sessionId}}`。
BLACKLIST_REMOVE_API = 'mtop.taobao.idlemessage.pc.blacklist.remove'

#: 给买家发商品卡片（写）。版本 1.0：`{api:"mtop.taobao.idlemessage.message.card.send", v:"1.0",
#: data:{sessionId, itemId}}`，官方失败 toast「发送商品卡片失败」。
CARD_SEND_API = 'mtop.taobao.idlemessage.message.card.send'

#: 把会话**转交**给指定客服（写）。版本 1.0：`{api:"mtop.taobao.idlemessage.customer.deliver.session",
#: v:"1.0", data:{sessionId, targetUid}}`；官方那个按钮的图标 alt 就是「转交」，
#: `targetUid` 来自客服列表（`mtop.taobao.idlemessage.customers.info.get` → `data.customers[].customerUid`，
#: 我们已在 `backend/app/xianyu/shop.py::CS_AGENT_LIST_API` 实现）。
SESSION_DELIVER_API = 'mtop.taobao.idlemessage.customer.deliver.session'

#: **结束**当前会话（写）。版本 1.0：`data:{sessionId}`；官方弹窗标题「确认结束当前会话？」。
SESSION_LEAVE_API = 'mtop.taobao.idlemessage.customer.leave.session'

#: 重新发起聊天（写）。版本 1.0：`data:{sessionId}`；官方成功 toast「重新发起聊天成功」，
#: 并且**判的是业务体里的 `data.success` 布尔**（不是只看信封）——这一族里唯一有回执的。
SESSION_REJOIN_API = 'mtop.taobao.idlemessage.customer.rejoin.session'

#: 会话 id 的长度上限（我们自己的防呆，官方没有明文校验）。
SESSION_ID_MAX = 128

#: 客服 uid 的长度上限（同上；官方 `targetUid` 是数字串）。
TARGET_UID_MAX = 64

#: 写能力登记。`data` 是真实字段形状，`version` 是该接口的版本号（**逐条不同，别抄**），
#: `evidence` 是 bundle 字符偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'blacklist_add': {
        'api': BLACKLIST_ADD_API,
        'version': '2.0',
        'data': '{sessionId}',
        'evidence': 'bundle @2619754 同族包装 gC；add 的 v 是 "2.0"（这一族里唯一的 2.0）',
    },
    'blacklist_remove': {
        'api': BLACKLIST_REMOVE_API,
        'version': '1.0',
        'data': '{sessionId}',
        'evidence': 'bundle @2619754 同族包装 gC，v:"1.0"',
    },
    'card_send': {
        'api': CARD_SEND_API,
        'version': '1.0',
        'data': '{sessionId, itemId}',
        'evidence': 'bundle 同族包装，失败 toast「发送商品卡片失败」',
    },
    'session_deliver': {
        'api': SESSION_DELIVER_API,
        'version': '1.0',
        'data': '{sessionId, targetUid}',
        'evidence': ('bundle js_008_vendors.js @2030569 `xO(sessionId, targetUid)`：'
                     '`{api:"…customer.deliver.session", v:"1.0", data:{sessionId:eu, targetUid:ef}}`；'
                     '同处 @2030560 前一行就是拉客服列表 `customers.info.get`；按钮图标 alt「转交」'),
    },
    'session_leave': {
        'api': SESSION_LEAVE_API,
        'version': '1.0',
        'data': '{sessionId}',
        'evidence': ('bundle js_008_vendors.js @2036368：`{api:"…customer.leave.session", v:"1.0", '
                     'data:{sessionId:eu}}`；官方弹窗标题「确认结束当前会话？」'),
    },
    'session_rejoin': {
        'api': SESSION_REJOIN_API,
        'version': '1.0',
        'data': '{sessionId}',
        'evidence': ('bundle js_008_vendors.js @1229870 `th(eu)` → `data:eu`，调用点 @1230857 '
                     '`th({sessionId:ef})`，成功 toast「重新发起聊天成功」，回执读 `data.success`'),
    },
}

#: 只读接口（`assert_read` 用它）。
READ_APIS: Tuple[str, ...] = (BLACKLIST_QUERY_API,)

#: 只读接口的版本号（这一条是 1.0）。
READ_VERSIONS: Dict[str, str] = {BLACKLIST_QUERY_API: '1.0'}

#: 写接口的 api → version（服务层按 api 查版本）。
WRITE_VERSIONS: Dict[str, str] = {entry['api']: entry['version'] for entry in WRITE_APIS.values()}

#: 写接口集合（`assert_write` 只认这里的 api —— 用 `ALL_APIS - READ_APIS` 会把
#: 「登记了但不是写」的接口放行，第 32 批踩过一次）。
WRITE_API_SET = frozenset(WRITE_VERSIONS)

#: 本模块登记的全部接口（写 + 读）。
ALL_APIS = frozenset([*READ_APIS, *WRITE_API_SET])

#: 写动作名（服务层/工具层按这个枚举动作）。
ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)

#: 平台可能返回的拉黑状态键（官方读的是前两个，后两个是防御性别名）。
IS_IN_BLACK_KEYS: Tuple[str, ...] = ('isInBlack', 'is_in_black', 'inBlack', 'black')
SHOW_BLACK_LIST_KEYS: Tuple[str, ...] = ('showBlackList', 'show_black_list', 'showBlacklist')


class UnknownApi(ValueError):
    """调用了本模块不认识的接口。"""


class WriteNotConfirmed(ValueError):
    """写接口没有显式 `confirm=True`。"""


def assert_write(api: str) -> str:
    """确认 `api` 是本模块登记的**写**接口，返回它（方便串在构造器前面）。"""
    if api not in WRITE_API_SET:
        raise UnknownApi(f'不是本模块的写接口：{api}')
    return api


def assert_read(api: str) -> str:
    """确认 `api` 是本模块登记的**只读**接口。"""
    if api not in READ_APIS:
        raise UnknownApi(f'不是本模块的只读接口：{api}')
    return api


def _need_confirm(confirm: Any) -> None:
    """写闸门：只有**真的是** `True` 才算点头。

    刻意不用 `bool(confirm)` —— `bool('false')` 是 `True`（第 38 批那条真缺陷的教训）。
    """
    if confirm is not True:
        raise WriteNotConfirmed('这是写操作，会真的改动闲鱼上的会话设置，必须显式 confirm=true')


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return str(value)


def _session_id(value: Any) -> str:
    """会话 id：只去首尾空白，**不做 `@goofish` 后缀变换**（见模块 docstring）。"""
    text = _text(value).strip()
    if not text:
        raise ValueError('会话 id 不能为空')
    if len(text) > SESSION_ID_MAX:
        raise ValueError(f'会话 id 太长了（超过 {SESSION_ID_MAX} 个字符）')
    return text


def _item_id(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError('商品 ID 不能为空')
    return text


def _target_uid(value: Any) -> str:
    """转交目标（客服 uid）：只去首尾空白。

    **刻意不做数字校验**：官方 `targetUid` 是数字串，但那是客服账号的 uid，
    我们手里没有权威的格式定义（多账号、多端都一样），猜格式会把能用的值挡在门外。
    留空才报错 —— 「谁都没指定就转交」一定是调用方的错。
    """
    text = _text(value).strip()
    if not text:
        raise ValueError('转交目标客服不能为空（targetUid 取自客服列表）')
    if len(text) > TARGET_UID_MAX:
        raise ValueError(f'转交目标客服太长了（超过 {TARGET_UID_MAX} 个字符）')
    return text


# --------------------------------------------------------------------------- 请求构造


def blacklist_query_request(session_id: Any) -> Tuple[str, Dict[str, Any], str]:
    """拉黑状态查询：`{sessionId}`，版本 1.0。"""
    api = assert_read(BLACKLIST_QUERY_API)
    return api, {'sessionId': _session_id(session_id)}, READ_VERSIONS[api]


def blacklist_add_request(session_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """拉黑买家：`{sessionId}`，**版本 2.0**。"""
    _need_confirm(confirm)
    api = assert_write(BLACKLIST_ADD_API)
    return api, {'sessionId': _session_id(session_id)}, WRITE_VERSIONS[api]


def blacklist_remove_request(session_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """解除拉黑：`{sessionId}`，版本 1.0。"""
    _need_confirm(confirm)
    api = assert_write(BLACKLIST_REMOVE_API)
    return api, {'sessionId': _session_id(session_id)}, WRITE_VERSIONS[api]


def card_send_request(session_id: Any, item_id: Any, *,
                      confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """给买家发商品卡片：`{sessionId, itemId}`，版本 1.0。"""
    _need_confirm(confirm)
    api = assert_write(CARD_SEND_API)
    return api, {'sessionId': _session_id(session_id), 'itemId': _item_id(item_id)}, WRITE_VERSIONS[api]


def session_deliver_request(session_id: Any, target_uid: Any, *,
                            confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """把会话转交给指定客服：`{sessionId, targetUid}`，版本 1.0。"""
    _need_confirm(confirm)
    api = assert_write(SESSION_DELIVER_API)
    return api, {'sessionId': _session_id(session_id),
                 'targetUid': _target_uid(target_uid)}, WRITE_VERSIONS[api]


def session_leave_request(session_id: Any, *,
                          confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """结束当前会话：`{sessionId}`，版本 1.0。"""
    _need_confirm(confirm)
    api = assert_write(SESSION_LEAVE_API)
    return api, {'sessionId': _session_id(session_id)}, WRITE_VERSIONS[api]


def session_rejoin_request(session_id: Any, *,
                           confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """重新发起聊天：`{sessionId}`，版本 1.0。"""
    _need_confirm(confirm)
    api = assert_write(SESSION_REJOIN_API)
    return api, {'sessionId': _session_id(session_id)}, WRITE_VERSIONS[api]


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any], str]:
    """按动作名拼请求（服务层只在这里做一次动作 → 构造器的映射）。"""
    builders = {
        'blacklist_add': blacklist_add_request,
        'blacklist_remove': blacklist_remove_request,
        'card_send': card_send_request,
        'session_deliver': session_deliver_request,
        'session_leave': session_leave_request,
        'session_rejoin': session_rejoin_request,
    }
    builder = builders.get(_text(action).strip())
    if builder is None:
        raise UnknownApi(f'不是本模块的写动作：{action}')
    return builder(**kwargs)


# --------------------------------------------------------------------------- 解析


def _body(raw: Any) -> Dict[str, Any]:
    """取出业务体（`trade.body_of` 认得 `data.module` / `data.data` / `data` 三种容器）。"""
    body = body_of(raw)
    return body if isinstance(body, dict) else {}


def _business_error(raw: Any) -> str:
    """业务层失败文案（客户端信封之外再兜一层）。

    `trade.envelope_error` 只看客户端层 `success is False`；这三条接口在 bundle 里的成功判据
    也就是那个信封（官方 IM 包装只判 `result.code === 'success'` 这类），所以这里只在业务体
    **明确**说失败时才算失败：`code` 有值且不是 `success`，或 `success` 是假值。
    判不出来就返回 ''（宁可说成功也不凭空说失败 —— 与「null 不造 0」同一条纪律）。
    """
    failure = envelope_error(raw)
    if failure:
        return failure
    body = _body(raw)
    if not body:
        return ''
    code = body.get('code')
    if isinstance(code, str) and code.strip() and code.strip().lower() != 'success':
        return _text(body.get('message') or body.get('msg') or code)
    success = body.get('success')
    if success is False or (isinstance(success, str) and success.strip().lower() == 'false'):
        return _text(body.get('message') or body.get('msg')) or '平台接口返回失败'
    return ''


def _first_key(body: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[Any, Optional[str]]:
    for key in keys:
        if key in body:
            return body[key], key
    return None, None


def _as_bool(value: Any) -> Optional[bool]:
    """平台值 → 布尔；认不出来的**原样返回 None**（不猜）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('true', '1', 'yes'):
            return True
        if text in ('false', '0', 'no'):
            return False
        return None
    if isinstance(value, int):
        return bool(value)
    return None


def parse_blacklist(raw: Any) -> Dict[str, Any]:
    """拉黑状态：`is_in_black` / `show_black_list`。

    平台没给键 → `None`（**不是 `False`**）——「平台没说」和「没拉黑」是两件事，
    界面上必须分开显示（第 4 批的 `itemStatus` 反向判错就是这么来的）。
    """
    raw = decode_payload(raw)
    body = _body(raw)
    is_in_black, is_key = _first_key(body, IS_IN_BLACK_KEYS)
    show_black_list, show_key = _first_key(body, SHOW_BLACK_LIST_KEYS)
    return {
        'is_in_black': _as_bool(is_in_black),
        'show_black_list': _as_bool(show_black_list),
        'is_in_black_key': is_key,
        'show_black_list_key': show_key,
        'body_keys': sorted(str(key) for key in body),
        'found': bool(body),
    }


def parse_action(raw: Any) -> Dict[str, Any]:
    """写动作的回执：成功判据是平台信封（回执里没有业务 id 可核对）。

    `saved` 表示平台没有拒绝；`platform_message` **只在失败时**有内容
    （成功时平台也常回 `SUCCESS::调用成功` 这种套话，把它当「平台原话」显示给用户
    会让人以为出了什么事）；平台信封原文另放 `envelope`，只用于排查。
    """
    failure = _business_error(raw)
    body = _body(raw)
    return {
        'saved': not failure,
        'platform_message': failure,
        'envelope': envelope_message(raw),
        'body_keys': sorted(str(key) for key in body),
        'found': bool(body),
    }


def parse_session_action(raw: Any) -> Dict[str, Any]:
    """会话接管族的回执：比 `parse_action` 多认一层「平台自己说成没成功」。

    这一族里 `rejoin` 的回执是 `data.success`（布尔，官方据此弹「重新发起聊天成功」），
    所以这里把 `success` 解析成三态 `platform_success`：
    - `True` / `False` = 平台明确说了；
    - `None` = 平台没有这个键（**不猜**）。

    只要平台明确说 `false`，就算失败（信封成功也不行）——「平台说没成功」比
    「信封看起来没报错」更权威。`platform_message` 也**只在失败时**有内容。
    """
    parsed = parse_action(raw)
    body = _body(decode_payload(raw))
    success, success_key = _first_key(body, ('success', 'succeed', 'result'))
    platform_success = _as_bool(success)
    if platform_success is False and parsed['saved']:
        parsed['saved'] = False
        parsed['platform_message'] = (
            _text(body.get('message') or body.get('msg')) or '平台返回 success=false（这次操作没有生效）')
    returned, returned_key = _first_key(body, ('sessionId', 'session_id'))
    parsed['platform_success'] = platform_success
    parsed['platform_success_key'] = success_key
    parsed['returned_session_id'] = _text(returned) or None
    parsed['returned_session_id_key'] = returned_key
    return parsed
