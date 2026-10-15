"""通知中心协议层 —— 读（频道树 + 未读）+ 写（标记单条已读）。

真机抓包（2026-10-10，`logs/seller_discover/notify_capture.json`，
headless Chrome 打开 `https://seller.goofish.com/?site=COMMONPRO#/notification-center`）：

- 频道树 `mtop.taobao.idlemessage.pc.notify.sessions.list`，body `{}`
  真机响应 `data = {"notifyInfoList": [{"key","title","icon","sessionId","children"?}], "success": true}`
  （全部 / 重要 / 交易履约{订单,售后,物流,评价} / 违规处理{处罚,申诉,举报} /
   小铺经营{资金管理,账号信息,商品信息,工具服务,流量运营,平台活动,政策规则,经营方法,调研问卷}）
- 未读计数 `mtop.taobao.idlemessage.pc.redpoint.query`，
  body `{"sessionTypes": "1,19,15,32,65,3,44,51,52,24", "fetch": 50}`
  真机响应 `data = {"needDecryptKeys": [], "needDecryptKeysV2": [], "total": 0}`

**通知条目本身没有 REST 接口**：抓包 18 秒内页面渲染出了 10 条通知
（`logs/seller_discover/notify_page2.txt`），但全程只有上面两个 mtop 调用，
条目来自 IM 长连接（同批还建了 `...pc.login.token` / `...pc.accs.token`）的推送。
所以复刻只覆盖「频道 + 未读」，条目列表如实返回空数组 + 说明，不编造。

**写接口（标记已读）有真机请求原文**：`logs/seller_discover/notify_capture2.json:93-95` ——
`POST https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.notifys.read/1.0/`，
`data={"messageId":"4338118620042.PNM"}`（**一次一条**；URL 里的 `/1.0/` 就是版本号）。
那次抓包**只落了请求、没落响应**。2026-10-15 用真实 Cookie 在进程内补打了一次
（请求/响应原文 `logs/notify_read58/07b_raw_mtop_response.json`），平台回执是
`{"success": true, "data": {"success": true}, "message": "SUCCESS::调用成功"}` ——
业务体里**有** `success: true`（这条通知当时确实被标成已读了）。
服务层仍按「平台没明确回执就不声称生效」处理（见 `backend/app/services/notify.py::mark_read`），
那条「平台沉默」分支是防御性兜底，不是主路径。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .trade import body_of, decode_payload, envelope_error, envelope_message

NOTIFY_SESSIONS_API = 'mtop.taobao.idlemessage.pc.notify.sessions.list'
REDPOINT_QUERY_API = 'mtop.taobao.idlemessage.pc.redpoint.query'

#: 标记一条通知已读（**写**）。真机请求原文见模块 docstring。
NOTIFYS_READ_API = 'mtop.taobao.idlemessage.pc.notifys.read'

#: 真机抓包里的 `sessionTypes` 原值（页面一次问 10 类未读）。
DEFAULT_SESSION_TYPES = '1,19,15,32,65,3,44,51,52,24'
DEFAULT_FETCH = 50

READ_APIS = (NOTIFY_SESSIONS_API, REDPOINT_QUERY_API)

#: 写接口：标记已读。**已实现**（见 `mark_read_request`）—— 只读路径绝不调它。
WRITE_APIS = (NOTIFYS_READ_API,)

#: 写接口的版本号：真机 URL 是 `.../notifys.read/1.0/`（只有这一条写接口，别抄别处的版本）。
WRITE_VERSIONS: Dict[str, str] = {NOTIFYS_READ_API: '1.0'}

#: `messageId` 的长度上限（我们自己的防呆；真机样例 `4338118620042.PNM`）。
MESSAGE_ID_MAX = 128


class UnknownApi(ValueError):
    """调用了本模块不认识的接口。"""


class WriteNotConfirmed(ValueError):
    """写接口没有显式 `confirm=True`。"""


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _need_confirm(confirm: Any) -> None:
    """写闸门：只有**真的是** `True` 才算点头。

    刻意不用 `bool(confirm)` —— `bool('false')` 是 `True`（第 38 批那条真缺陷的教训）。
    """
    if confirm is not True:
        raise WriteNotConfirmed('这是写操作，会真的改动闲鱼上的通知状态，必须显式 confirm=true')


def _message_id(value: Any) -> str:
    """通知消息 id：只去首尾空白，不做任何格式猜测（真机样例是 `<数字>.PNM`）。"""
    text = _text(value).strip()
    if not text:
        raise ValueError('通知消息 id 不能为空（message_id 取自通知条目）')
    if len(text) > MESSAGE_ID_MAX:
        raise ValueError(f'通知消息 id 太长了（超过 {MESSAGE_ID_MAX} 个字符）')
    return text


# --------------------------------------------------------------------------- 请求构造


def mark_read_request(message_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """标记一条通知已读：`{messageId}`，版本 1.0（真机 URL `/1.0/`）。"""
    _need_confirm(confirm)
    if NOTIFYS_READ_API not in WRITE_VERSIONS:  # pragma: no cover - 常量表自检
        raise UnknownApi(f'不是本模块的写接口：{NOTIFYS_READ_API}')
    return NOTIFYS_READ_API, {'messageId': _message_id(message_id)}, WRITE_VERSIONS[NOTIFYS_READ_API]


# --------------------------------------------------------------------------- 解析


def _body(raw: Any) -> Dict[str, Any]:
    """取出业务体（`trade.body_of` 认得 `data.module` / `data.data` / `data` 三种容器）。"""
    body = body_of(raw)
    return body if isinstance(body, dict) else {}


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


def parse_mark_read(raw: Any) -> Dict[str, Any]:
    """标记已读的回执（三态）。

    `platform_success`：
    - `True` / `False` = 平台在业务体里**明确**说了（认 `success` / `succeed` / `result`）；
    - `None` = 平台没这个字段 —— **不猜成成功**（与第 46 批接待状态同款口径）。

    `saved` 只在「平台没有拒绝」时才是 True；业务体里 `code` 有值且不是 `success`
    时也算失败，平台原话进 `platform_message`。**「有没有明确回执」由调用方判**
    （服务层见 `platform_success is None` 就按「可能没生效」处理）。
    """
    raw = decode_payload(raw)
    body = _body(raw)
    success, success_key = _first_key(body, ('success', 'succeed', 'result'))
    platform_success = _as_bool(success)
    message = _text(body.get('message') or body.get('msg'))
    saved = not envelope_error(raw)
    code = body.get('code')
    if isinstance(code, str) and code.strip() and code.strip().lower() != 'success':
        saved = False
        message = message or code.strip()
    if platform_success is False:
        saved = False
        message = message or '平台返回 success=false（这条通知没有被标记已读）'
    return {
        'saved': saved,
        'platform_success': platform_success,
        'platform_success_key': success_key,
        'platform_message': message,
        'body_keys': sorted(str(key) for key in body),
        'found': bool(body),
    }


# --------------------------------------------------------------------------- 只读调用


async def _data(client: Any, api: str, body: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """调一次 mtop 并只取 `data` 段（拿不到就 None，由服务层如实降级）。

    dict / list 都放行 —— 与 `backend/app/xianyu/shop.py` 同一约定：有的接口
    业务载荷本身就是数组，只认 dict 会把整个列表静默吞掉。
    """
    result = await client.mtop(api, body if body is not None else {}, v='1.0')
    if not isinstance(result, dict):
        return None
    data = result.get('data')
    return data if isinstance(data, (dict, list)) else None


async def notify_channels(client: Any) -> Optional[Any]:
    """通知频道树（`notifyInfoList`）。"""
    return await _data(client, NOTIFY_SESSIONS_API, {})


async def unread_counts(client: Any,
                        session_types: str = DEFAULT_SESSION_TYPES,
                        fetch: int = DEFAULT_FETCH) -> Optional[Any]:
    """各类通知的未读计数（`total` / `needDecryptKeys`）。"""
    return await _data(client, REDPOINT_QUERY_API, {
        'sessionTypes': str(session_types),
        'fetch': int(fetch),
    })
