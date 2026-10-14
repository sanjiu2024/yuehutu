"""IM 工具族：会话搜索 / 商品搜索 / 清未读（官方卖家工作台 IM 页的抽屉与维护动作）。

平台接口（全部 v1.0，证据来自本地 bundle 副本，**本批零平台请求**）：

- **会话搜索** `mtop.taobao.idlemessage.pc.session.search`（`needLoginPC: true`）：
  `data: {keyword, pageNumber, pageSize}`。官方调用点
  `C:\\Users\\Administrator\\.dsh-tmp\\seller_scan\\js_008_vendors.js` @1872570（函数 `y7`）：
  ```js
  (0, e3.G)({api: 'mtop.taobao.idlemessage.pc.session.search', v: '1.0', needLoginPC: true,
              data: {keyword: eu, pageNumber: ef, pageSize: em}})   // ef 默认 0、em 默认 50
  ```
  响应 `resp.data`，失败兜底 `{success: false, total: 0, curPage: 0, sessionList: []}` ⇒ 我们认
  `sessionList`（别名 `sessions`/`list`）、`total`、`curPage`。

- **商品搜索** `mtop.taobao.idlemessage.pc.tool.item.search`：
  `data: {sessionId, pageSize: 20, pageNumber|currentPage, searchItemRole, queryWord}`。
  `searchItemRole` 只有两个值（官方两个 tab 逐字）：`'own'` =「我的宝贝」、`'other'` =「TA的宝贝」。
  **两个官方调用点分页字段不一致**：旧组件用 `pageNumber` 且读响应 `nextPage`
  （js_008_vendors.js @1810742、js_006_p_im_index.js @3003926），新组件用 `currentPage` 且读
  `hasMore`（@1816833 / @3009966）⇒ 我们**两个请求键都发（值相同）、两个响应键都认**，
  并把这条偏离写进契约（不是猜值，是同一个页码的两个官方别名）。

- **清未读** `mtop.taobao.idlemessage.pc.session.unread.clean`（`needLoginPC: true`，**不带 data**）：
  官方调用点 js_006_p_im_index.js @2103155 —— 先取登录用户 id，拿到才调，调完直接 `resolve()`，
  **完全不看响应**。`mtop.taobao.idlemessage.pc.systems.unread.clean`（`needLogin: true`，不带 data）：
  js_008_vendors.js @1082751 里被定义成一个只有 `api` / `v` / `needLogin` 的类。

**诚实边界**：三条接口一次真机请求都没发过（本机账号登录态已过期），参数形状只有 bundle 证据 +
离线单测；商品搜索的响应元素字段名是**防御式**读取（认不出就给 `None` 并保留 `raw`）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .trade import _first, body_of, decode_payload, envelope_error, envelope_message

SESSION_SEARCH_API = 'mtop.taobao.idlemessage.pc.session.search'
ITEM_SEARCH_API = 'mtop.taobao.idlemessage.pc.tool.item.search'
UNREAD_CLEAN_API = 'mtop.taobao.idlemessage.pc.session.unread.clean'
SYSTEMS_UNREAD_CLEAN_API = 'mtop.taobao.idlemessage.pc.systems.unread.clean'

DEFAULT_VERSION = '1.0'

#: 商品搜索的两个角色（官方两个 tab 逐字）。
SESSION_ROLES: Tuple[str, ...] = ('own', 'other')
SESSION_ROLE_LABELS: Dict[str, str] = {'own': '我的宝贝', 'other': 'TA的宝贝'}

KEYWORD_MAX = 64
PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 50

#: 清未读的两种口径（买家会话 / 系统通知）。
UNREAD_KINDS: Tuple[str, ...] = ('session', 'system')
UNREAD_KIND_LABELS: Dict[str, str] = {'session': '买家会话', 'system': '系统通知'}

READ_APIS: Tuple[str, ...] = (SESSION_SEARCH_API, ITEM_SEARCH_API)

READ_VERSIONS: Dict[str, str] = {
    SESSION_SEARCH_API: DEFAULT_VERSION,
    ITEM_SEARCH_API: DEFAULT_VERSION,
}

WRITE_APIS: Dict[str, Dict[str, str]] = {
    'clear_session_unread': {
        'api': UNREAD_CLEAN_API,
        'version': DEFAULT_VERSION,
        'data': '（不带 data）',
        'evidence': 'js_006_p_im_index.js @2103155：`G({api, v:"1.0", needLoginPC:true})`，'
                    '官方拿到登录用户 id 才调，调完只 resolve()、完全不看响应',
    },
    'clear_systems_unread': {
        'api': SYSTEMS_UNREAD_CLEAN_API,
        'version': DEFAULT_VERSION,
        'data': '（不带 data）',
        'evidence': 'js_008_vendors.js @1082751：`class tq { api=..., v="1.0", needLogin=true }`',
    },
}

WRITE_VERSIONS: Dict[str, str] = {item['api']: item['version'] for item in WRITE_APIS.values()}
WRITE_API_SET = frozenset(WRITE_VERSIONS)
ALL_APIS: Tuple[str, ...] = tuple(dict.fromkeys(READ_APIS + tuple(WRITE_VERSIONS)))
ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)

#: 会话元素的键名别名（平台给哪个认哪个，认不出就是 None）。
SESSION_ID_KEYS = ('cid', 'sessionId', 'session_id')
SESSION_TITLE_KEYS = ('title', 'nick', 'sessionTitle', 'name')
SESSION_TYPE_KEYS = ('sessionType', 'session_type', 'bizType')
SESSION_UNREAD_KEYS = ('unread', 'unreadCount', 'redPoint')
SESSION_LAST_KEYS = ('lastMessage', 'lastMsg', 'last_message', 'summary')
SESSION_ICON_KEYS = ('icon', 'avatar', 'headImg')
SESSION_LIST_KEYS = ('sessionList', 'sessions', 'list')
SESSION_TOTAL_KEYS = ('total',)
SESSION_PAGE_KEYS = ('curPage', 'currentPage', 'pageNumber')

#: 商品元素的键名别名。
ITEM_ID_KEYS = ('itemId', 'item_id', 'id')
ITEM_TITLE_KEYS = ('title', 'itemTitle', 'name')
ITEM_PIC_KEYS = ('picUrl', 'itemPicUrl', 'pic', 'image', 'imageUrl')
ITEM_PRICE_KEYS = ('price', 'priceText', 'soldPrice', 'currentPrice')
ITEM_STATUS_KEYS = ('itemStatus', 'status', 'itemStatusText')
ITEM_LIST_KEYS = ('items', 'itemList', 'list')
ITEM_MORE_KEYS = ('hasMore', 'has_more')
ITEM_NEXT_KEYS = ('nextPage', 'next_page', 'next')


class UnknownApi(ValueError):
    """动作名不在本模块的写清单里。"""


class WriteNotConfirmed(ValueError):
    """写操作没有显式确认。"""


def assert_write(api: str) -> str:
    """写接口白名单校验（只认 ``WRITE_API_SET``，不用 ``ALL_APIS - READ_APIS`` 这种推导）。"""
    if api not in WRITE_API_SET:
        raise UnknownApi('不是本模块的写接口: %s' % api)
    return api


def assert_read(api: str) -> str:
    if api not in READ_APIS:
        raise UnknownApi('不是本模块的读接口: %s' % api)
    return api


def _need_confirm(confirm: Any) -> None:
    """协议层只认字面量 ``True``（服务层负责把 ``'true'`` / ``1`` 归一后再传进来）。"""
    if confirm is not True:
        raise WriteNotConfirmed('这是写操作，会真的改动闲鱼上的会话数据，必须显式 confirm=true')


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    return ''


def _keyword(value: Any) -> str:
    text = _text(value)
    if len(text) > KEYWORD_MAX:
        raise ValueError('搜索关键词最多 %d 个字' % KEYWORD_MAX)
    return text


def _page(value: Any) -> int:
    if value in (None, ''):
        return 1
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise ValueError('页码必须是整数')
    if page < 1:
        raise ValueError('页码从 1 开始')
    return page


def _size(value: Any) -> int:
    if value in (None, ''):
        return PAGE_SIZE_DEFAULT
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise ValueError('每页条数必须是整数')
    if size < 1 or size > PAGE_SIZE_MAX:
        raise ValueError('每页条数只能是 1~%d' % PAGE_SIZE_MAX)
    return size


def _role(value: Any) -> str:
    text = _text(value).lower() or 'own'
    if text not in SESSION_ROLES:
        raise ValueError('商品归属只能是 own（我的宝贝）或 other（TA的宝贝）')
    return text


def _session_id(value: Any) -> str:
    text = _text(value)
    if not text:
        raise ValueError('会话 id 不能为空')
    return text


def _unread_kind(value: Any) -> str:
    text = _text(value).lower() or 'session'
    if text not in UNREAD_KINDS:
        raise ValueError('清未读的 kind 只能是 session（买家会话）或 system（系统通知）')
    return text


def session_search_request(keyword: Any = '', page: Any = 1, size: Any = PAGE_SIZE_DEFAULT
                           ) -> Tuple[str, Dict[str, Any], str]:
    """会话搜索（读）：`{keyword, pageNumber, pageSize}`。"""
    api = assert_read(SESSION_SEARCH_API)
    data = {'keyword': _keyword(keyword), 'pageNumber': _page(page), 'pageSize': _size(size)}
    return api, data, READ_VERSIONS[api]


def item_search_request(session_id: Any, role: Any = 'own', keyword: Any = '',
                        page: Any = 1, size: Any = PAGE_SIZE_DEFAULT
                        ) -> Tuple[str, Dict[str, Any], str]:
    """商品搜索（读）：两个官方分页键都发（同一个页码的两个别名，见模块 docstring）。"""
    api = assert_read(ITEM_SEARCH_API)
    number = _page(page)
    data = {
        'sessionId': _session_id(session_id),
        'pageSize': _size(size),
        'pageNumber': number,
        'currentPage': number,
        'searchItemRole': _role(role),
        'queryWord': _keyword(keyword),
    }
    return api, data, READ_VERSIONS[api]


def clear_session_unread_request(*, confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """清买家会话未读（写）：**不带 data**。"""
    _need_confirm(confirm)
    api = assert_write(UNREAD_CLEAN_API)
    return api, {}, WRITE_VERSIONS[api]


def clear_systems_unread_request(*, confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """清系统通知未读（写）：**不带 data**。"""
    _need_confirm(confirm)
    api = assert_write(SYSTEMS_UNREAD_CLEAN_API)
    return api, {}, WRITE_VERSIONS[api]


_BUILDERS = {
    'clear_session_unread': clear_session_unread_request,
    'clear_systems_unread': clear_systems_unread_request,
}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any], str]:
    """按动作名分派构造器（未知动作抛 :class:`UnknownApi`）。"""
    builder = _BUILDERS.get(_text(action))
    if builder is None:
        raise UnknownApi('未知动作: %s' % action)
    return builder(**kwargs)


def _body(raw: Any) -> Dict[str, Any]:
    body = body_of(decode_payload(raw))
    return body if isinstance(body, dict) else {}


def _list_of(body: Dict[str, Any], keys: Sequence[str]) -> Tuple[Optional[List[Any]], str]:
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return value, key
    return None, ''


def _int_of(body: Dict[str, Any], keys: Sequence[str]) -> Optional[int]:
    for key in keys:
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip('-').isdigit():
            return int(value.strip())
    return None


def _bool_of(body: Dict[str, Any], keys: Sequence[str]) -> Tuple[Optional[bool], str]:
    for key in keys:
        if key in body:
            value = body.get(key)
            if isinstance(value, bool):
                return value, key
            if isinstance(value, str):
                text = value.strip().lower()
                if text in ('true', 'false'):
                    return text == 'true', key
            if value in (0, 1):
                return bool(value), key
    return None, ''


def normalize_session(entry: Any) -> Dict[str, Any]:
    """会话元素 → 归一字段（认不出的一律 ``None``，并保留 ``raw``）。"""
    if not isinstance(entry, dict):
        return {'session_id': None, 'title': None, 'session_type': None, 'unread': None,
                'last_message': None, 'icon': None, 'raw': entry}
    session_id = _first(entry, *SESSION_ID_KEYS)
    title = _first(entry, *SESSION_TITLE_KEYS)
    session_type = _first(entry, *SESSION_TYPE_KEYS)
    unread = _first(entry, *SESSION_UNREAD_KEYS)
    last = _first(entry, *SESSION_LAST_KEYS)
    icon = _first(entry, *SESSION_ICON_KEYS)
    if isinstance(last, dict):
        last = _first(last, 'text', 'content', 'summary', 'title')
    if isinstance(session_type, str) and session_type.strip().lstrip('-').isdigit():
        session_type = int(session_type.strip())
    return {
        'session_id': _text(session_id) or None,
        'title': _text(title) or None,
        'session_type': session_type if isinstance(session_type, int) else None,
        'unread': unread if isinstance(unread, int) and not isinstance(unread, bool) else None,
        'last_message': _text(last) or None,
        'icon': _text(icon) or None,
        'raw': entry,
    }


def normalize_item(entry: Any) -> Dict[str, Any]:
    """商品元素 → 归一字段（防御式：认不出的给 ``None`` 并保留 ``raw``）。"""
    if not isinstance(entry, dict):
        return {'item_id': None, 'title': None, 'pic_url': None, 'price': None, 'status': None,
                'raw': entry}
    item_id = _first(entry, *ITEM_ID_KEYS)
    title = _first(entry, *ITEM_TITLE_KEYS)
    pic = _first(entry, *ITEM_PIC_KEYS)
    price = _first(entry, *ITEM_PRICE_KEYS)
    status = _first(entry, *ITEM_STATUS_KEYS)
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        price_text = ('%.2f' % (price / 100.0)) if float(price).is_integer() and price >= 100 else str(price)
    else:
        price_text = _text(price) or None
    return {
        'item_id': _text(item_id) or None,
        'title': _text(title) or None,
        'pic_url': _text(pic) or None,
        'price': price_text,
        'status': _text(status) or None,
        'raw': entry,
    }


def parse_session_search(raw: Any) -> Dict[str, Any]:
    """会话搜索响应：列表 / 总数 / 当前页，键认不出就是 ``None``（不是空列表）。"""
    body = _body(raw)
    entries, list_key = _list_of(body, SESSION_LIST_KEYS)
    return {
        'sessions': None if entries is None else [normalize_session(item) for item in entries],
        'total': _int_of(body, SESSION_TOTAL_KEYS),
        'cur_page': _int_of(body, SESSION_PAGE_KEYS),
        'list_key': list_key,
        'found': bool(body),
        'body_keys': sorted(str(key) for key in body),
    }


def parse_item_search(raw: Any) -> Dict[str, Any]:
    """商品搜索响应：列表 + 两种分页键都认（``hasMore`` / ``nextPage``）。"""
    body = _body(raw)
    entries, list_key = _list_of(body, ITEM_LIST_KEYS)
    has_more, more_key = _bool_of(body, ITEM_MORE_KEYS)
    return {
        'items': None if entries is None else [normalize_item(item) for item in entries],
        'has_more': has_more,
        'next_page': _int_of(body, ITEM_NEXT_KEYS),
        'more_key': more_key,
        'list_key': list_key,
        'found': bool(body),
        'body_keys': sorted(str(key) for key in body),
    }


def parse_clean(raw: Any) -> Dict[str, Any]:
    """清未读响应：官方根本不看响应 ⇒ 只给三态 ``platform_success``，不替平台下结论。

    ``platform_message`` 优先用**业务体**里的文案（平台把理由写在 ``data.message`` 里），
    业务体没有才退回信封文案 —— 否则失败时会透出 ``SUCCESS::调用成功`` 这种自相矛盾的话。
    """
    body = _body(raw)
    success, key = _bool_of(body, ('success', 'succeed', 'result'))
    business_message = _text(_first(body, 'message', 'msg', 'errorMsg', 'failReason'))
    return {
        'platform_success': success,
        'platform_success_key': key,
        'platform_message': business_message or envelope_message(raw),
        'error': envelope_error(raw),
        'body_keys': sorted(str(key) for key in body),
        'found': bool(body),
    }
