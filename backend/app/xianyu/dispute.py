# -*- coding: utf-8 -*-
"""纠纷协商（官方「联系纠纷小二」）：纠纷会话 + 历史留言 + 未读 + 发文字/发图举证 + 催办 + 标记已读。

**接口来源（静态 bundle 反解，零风控）**：官方卖家工作台 bundle
`https://g.alicdn.com/idle-pc/idle-seller-workbench/…/js/vendors.js`（5,857,382 B，单行压缩 JS）。
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\seller_scan\\js_008_vendors.js`，
**证据用字符偏移**：模块 `73043`（懒加载名 `contactDisputeXiaoer`，见 @959706 的 chunk 映射），
模块体在偏移 650905~662500。

**它是什么**：在纠纷/维权单里和平台「小二」沟通的会话（不是买家的 IM 会话，那条走
`backend/app/xianyu/im.py`）。官方入口挂在投诉详情页，弹一个会话面板：
拉历史留言、发文字、发图举证、催办、把消息标记已读。

**这一族全是 v1.0**（与 `xianyu/im_actions.py` 那族「版本逐条不同」不一样），
但构造器仍统一返回 **三元组 `(api, data, version)`**，服务层照返回值原样发
`common.call_method(client, ('mtop',), api, data, v=version)` —— 保持两个模块的调用形状一致，
免得以后有人把两族抄混。

**`content` 是 JSON 字符串**（本模块最容易抄错的地方）：
* 文字：`contentType:'PLAIN_TEXT'`，`content: JSON.stringify({text})`
* 图片：`contentType:'IMAGE'`，`content: JSON.stringify({url, ext, altText, width, height, thumbURL})`，
  并且官方**总是**带 `sendKey`（没有就是空串）。
反过来，平台返回的消息里 `content` 可能是**已经解析好的对象**（官方渲染直接读 `content.text` /
`content.url`），也可能是字符串 —— 所以解析时两种都认（字符串先 `json.loads`）。

**未验证边界**：一次真机请求都没发过（写接口的验证红线是「零平台写请求」）。
五条接口的参数形状、成功判据、以及「消息里 `content` 到底是字符串还是对象」全部来自 bundle 静态证据。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .trade import body_of, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 取（或创建）纠纷会话（只读）。bundle @650910：
#: `{api:"mtop.taobao.idle.shop.dispute.message.get.conversation", v:"1.0",
#:   data:{orderId, refundId, disputeId}}`，响应读 `data.conversationId`。
CONVERSATION_API = 'mtop.taobao.idle.shop.dispute.message.get.conversation'

#: 拉历史留言（只读）。bundle @651060：`data:{conversationId, beginTime}`，
#: 响应 `data.messageList[]` + `data.currentTime`（下一页把 currentTime 当 beginTime）。
HISTORY_API = 'mtop.taobao.idle.shop.dispute.message.query.history'

#: 未读留言（只读）。bundle @651199：`data:{conversationId}`，响应 `data.result[]`。
UNREAD_API = 'mtop.taobao.idle.shop.dispute.message.query.unread.list'

#: 标记已读（写）。bundle @651335：`data:{conversationId, msgId, sequence}`。
MARK_READ_API = 'mtop.taobao.idle.shop.dispute.message.mark.read'

#: 发留言（写，文字与图片共用这一条）。bundle @651478：`data:{conversationId, sceneType,
#: contentType, content, sendKey?}`；成功读 `data.success` / `data.msgId` / `data.sequence` /
#: `data.tips`，失败读 `data.failReason`。
SEND_API = 'mtop.taobao.idle.shop.dispute.message.send'

#: 场景类型（官方固定 `userActive`，三个调用点（发文字、发图、催办）都是这个值）。
SCENE_TYPE = 'userActive'

#: 文字留言的内容类型。
CONTENT_TEXT = 'PLAIN_TEXT'

#: 图片留言的内容类型。
CONTENT_IMAGE = 'IMAGE'

#: 催办用的固定文案（官方「催办」按钮就是拿这句当文字留言发出去）。
URGE_TEXT = '催促处理维权'

#: 官方一页 20 条（bundle @654969：`ef.length<20 || ef[0].header.sequence===1` 判到底）。
PAGE_SIZE = 20

#: 文字留言长度上限（我们自己的防呆，官方没有明文校验；会话页那边的上限是 2000）。
TEXT_MAX = 2000

#: 会话 id / 消息 id 长度上限（防呆）。
CONVERSATION_ID_MAX = 128
MSG_ID_MAX = 128

#: 官方图片白名单（bundle @651478 附近：`["image/png","image/jpg","image/jpeg"]`，单张 ≤ 3 MiB）。
IMAGE_MIMES: Tuple[str, ...] = ('image/png', 'image/jpg', 'image/jpeg')

#: 图片扩展名（官方 `ef.ext || 'JPG'`，缺省 JPG）。
IMAGE_EXT_DEFAULT = 'JPG'

#: 写能力登记。`data` 是真实字段形状，`version` 是该接口的版本号，`evidence` 是偏移 + 原文要点。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'send_text': {
        'api': SEND_API,
        'version': '1.0',
        'data': "{conversationId, sceneType:'userActive', contentType:'PLAIN_TEXT', "
                "content: JSON.stringify({text})}",
        'evidence': 'bundle @657728（发文字）与 @661287（催办）两个调用点',
    },
    'send_image': {
        'api': SEND_API,
        'version': '1.0',
        'data': "{conversationId, sceneType:'userActive', contentType:'IMAGE', "
                "content: JSON.stringify({url, ext, altText, width, height, thumbURL}), sendKey}",
        'evidence': 'bundle @657728 的 image 分支；白名单 image/png|image/jpg|image/jpeg，≤3MiB',
    },
    'urge': {
        'api': SEND_API,
        'version': '1.0',
        'data': "{conversationId, sceneType:'userActive', contentType:'PLAIN_TEXT', "
                "content: JSON.stringify({text:'催促处理维权'})}",
        'evidence': 'bundle @661287 催办按钮的固定文案',
    },
    'mark_read': {
        'api': MARK_READ_API,
        'version': '1.0',
        'data': '{conversationId, msgId, sequence}',
        'evidence': 'bundle @651335；调用点在 @654969（拿最大 sequence 的那条消息去标记）',
    },
}

#: 只读接口（`assert_read` 用它）。
READ_APIS: Tuple[str, ...] = (CONVERSATION_API, HISTORY_API, UNREAD_API)

#: 只读接口版本号（这一族全是 1.0）。
READ_VERSIONS: Dict[str, str] = {api: '1.0' for api in READ_APIS}

#: 写接口的 api → version。
WRITE_VERSIONS: Dict[str, str] = {entry['api']: entry['version'] for entry in WRITE_APIS.values()}

#: 写接口集合（`assert_write` 只认这里的 api —— 用 `ALL_APIS - READ_APIS` 会把
#: 「登记了但不是写」的接口放行，第 32 批踩过一次）。
WRITE_API_SET = frozenset(WRITE_VERSIONS)

#: 本模块登记的全部接口。
ALL_APIS = frozenset([*READ_APIS, *WRITE_API_SET])

#: 写动作名（服务层/工具层按这个枚举动作）。
ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)

#: 平台可能返回的会话 id 键（官方读的是第一个，后面是防御性别名）。
CONVERSATION_ID_KEYS: Tuple[str, ...] = ('conversationId', 'conversation_id', 'cid')

#: 平台可能返回的历史列表键。
MESSAGE_LIST_KEYS: Tuple[str, ...] = ('messageList', 'message_list', 'messages', 'result', 'list')

#: 平台可能返回的「下一页游标」键。
CURRENT_TIME_KEYS: Tuple[str, ...] = ('currentTime', 'current_time', 'beginTime')

#: 平台可能返回的未读列表键。
UNREAD_LIST_KEYS: Tuple[str, ...] = ('result', 'unreadList', 'unread_list', 'messages', 'list')


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
    """写闸门：只有**真的是** `True` 才算点头（`bool('false')` 是 `True`，第 38 批的教训）。"""
    if confirm is not True:
        raise WriteNotConfirmed('这是写操作，会真的改动闲鱼上的纠纷会话，必须显式 confirm=true')


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any) -> Optional[int]:
    """平台值 → 整数；认不出来原样 None（不猜、不补 0）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip('-').isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _conversation_id(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError('纠纷会话 id 不能为空')
    if len(text) > CONVERSATION_ID_MAX:
        raise ValueError(f'纠纷会话 id 太长了（超过 {CONVERSATION_ID_MAX} 个字符）')
    return text


def _msg_id(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError('消息 id 不能为空')
    if len(text) > MSG_ID_MAX:
        raise ValueError(f'消息 id 太长了（超过 {MSG_ID_MAX} 个字符）')
    return text


def _text_body(value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError('留言内容不能为空')
    if len(text) > TEXT_MAX:
        raise ValueError(f'留言太长了（超过 {TEXT_MAX} 个字符）')
    return text


def _json(payload: Dict[str, Any]) -> str:
    """官方用的是 `JSON.stringify`：紧凑、无空格（`separators` 必须显式指定）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def _image_content(url: Any, ext: Any, width: Any, height: Any, alt_text: Any,
                   thumb_url: Any) -> str:
    """图片留言的 `content`：官方六个键，缺省的按官方默认值补（ext→JPG、尺寸→0）。"""
    link = _text(url).strip()
    if not link:
        raise ValueError('图片地址不能为空')
    return _json({
        'url': link,
        'ext': (_text(ext).strip() or IMAGE_EXT_DEFAULT).upper(),
        'altText': _text(alt_text),
        'width': _as_int(width) or 0,
        'height': _as_int(height) or 0,
        'thumbURL': _text(thumb_url).strip() or link,
    })


# --------------------------------------------------------------------------- 请求构造


def conversation_request(order_id: Any = '', refund_id: Any = '', dispute_id: Any = '',
                         ) -> Tuple[str, Dict[str, Any], str]:
    """取纠纷会话：`{orderId, refundId, disputeId}`（三个都发，没有的就是空串 —— 官方也是全发）。"""
    api = assert_read(CONVERSATION_API)
    data = {
        'orderId': _text(order_id).strip(),
        'refundId': _text(refund_id).strip(),
        'disputeId': _text(dispute_id).strip(),
    }
    if not any(data.values()):
        raise ValueError('订单号、退款单号、纠纷单号至少要有一个')
    return api, data, READ_VERSIONS[api]


def history_request(conversation_id: Any, begin_time: Any = '') -> Tuple[str, Dict[str, Any], str]:
    """拉历史留言：`{conversationId, beginTime}`（首页 `beginTime` 是空串）。"""
    api = assert_read(HISTORY_API)
    return api, {
        'conversationId': _conversation_id(conversation_id),
        'beginTime': _text(begin_time).strip(),
    }, READ_VERSIONS[api]


def unread_request(conversation_id: Any) -> Tuple[str, Dict[str, Any], str]:
    """未读留言：`{conversationId}`。"""
    api = assert_read(UNREAD_API)
    return api, {'conversationId': _conversation_id(conversation_id)}, READ_VERSIONS[api]


def mark_read_request(conversation_id: Any, msg_id: Any, sequence: Any, *,
                      confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """标记已读：`{conversationId, msgId, sequence}`。"""
    _need_confirm(confirm)
    api = assert_write(MARK_READ_API)
    seq = _as_int(sequence)
    if seq is None:
        raise ValueError('sequence 必须是整数')
    return api, {
        'conversationId': _conversation_id(conversation_id),
        'msgId': _msg_id(msg_id),
        'sequence': seq,
    }, WRITE_VERSIONS[api]


def send_text_request(conversation_id: Any, text: Any, *,
                      confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """发文字留言：`content` 是 `JSON.stringify({text})` 字符串。"""
    _need_confirm(confirm)
    api = assert_write(SEND_API)
    return api, {
        'conversationId': _conversation_id(conversation_id),
        'sceneType': SCENE_TYPE,
        'contentType': CONTENT_TEXT,
        'content': _json({'text': _text_body(text)}),
    }, WRITE_VERSIONS[api]


def send_image_request(conversation_id: Any, url: Any, *, ext: Any = IMAGE_EXT_DEFAULT,
                       width: Any = 0, height: Any = 0, alt_text: Any = '',
                       thumb_url: Any = '', send_key: Any = '',
                       confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """发图举证：`content` 是图片六键的 JSON 字符串，并**总是**带 `sendKey`（官方空串也带）。"""
    _need_confirm(confirm)
    api = assert_write(SEND_API)
    return api, {
        'conversationId': _conversation_id(conversation_id),
        'sceneType': SCENE_TYPE,
        'contentType': CONTENT_IMAGE,
        'content': _image_content(url, ext, width, height, alt_text, thumb_url),
        'sendKey': _text(send_key).strip(),
    }, WRITE_VERSIONS[api]


def urge_request(conversation_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any], str]:
    """催办：就是发一句固定文案的文字留言。"""
    _need_confirm(confirm)
    api = assert_write(SEND_API)
    return api, {
        'conversationId': _conversation_id(conversation_id),
        'sceneType': SCENE_TYPE,
        'contentType': CONTENT_TEXT,
        'content': _json({'text': URGE_TEXT}),
    }, WRITE_VERSIONS[api]


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any], str]:
    """按动作名拼请求（服务层只在这里做一次动作 → 构造器的映射）。"""
    builders = {
        'send_text': send_text_request,
        'send_image': send_image_request,
        'urge': urge_request,
        'mark_read': mark_read_request,
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

    成功判据是官方信封 + 业务体里的 `success`；判不出来就返回 ''（宁可说成功也不凭空说失败）。
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
        reason = _text(body.get('failReason') or body.get('message') or body.get('msg'))
        return reason or '平台接口返回失败'
    return ''


def _first_key(body: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[Any, Optional[str]]:
    for key in keys:
        if key in body:
            return body[key], key
    return None, None


def _content_of(content: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """`content` → `(字典, 纯文本, 是否解析过 JSON 字符串)`。

    平台返回的消息里 `content` 可能是对象，也可能是 JSON 字符串（我们发出去的就是字符串），
    所以两种都认：字符串先 `json.loads`，成功且是 dict 就用它；否则当纯文本。
    """
    if isinstance(content, dict):
        return content, None, False
    if isinstance(content, str):
        text = content.strip()
        if text.startswith('{') and text.endswith('}'):
            try:
                parsed = json.loads(text)
            except ValueError:
                return None, content, False
            if isinstance(parsed, dict):
                return parsed, None, True
        return None, content, False
    return None, None, False


def normalize_message(raw: Any) -> Dict[str, Any]:
    """平台消息 → 我们前端的消息对象。

    平台没给的字段一律 `None`（**不补 0 / 空串**），认不出来的原始键放在 `raw` 里给排查用。
    """
    message = raw if isinstance(raw, dict) else {}
    header = message.get('header') if isinstance(message.get('header'), dict) else {}
    content, plain_text, parsed_json = _content_of(message.get('content'))
    content = content or {}

    url = _text(content.get('url')).strip() or None
    image = None
    if url:
        image = {
            'url': url,
            'ext': _text(content.get('ext')).strip() or None,
            'alt_text': _text(content.get('altText')).strip() or None,
            'width': _as_int(content.get('width')),
            'height': _as_int(content.get('height')),
            'thumb_url': _text(content.get('thumbURL')).strip() or None,
        }

    text = _text(content.get('text')).strip() or None
    if text is None and plain_text is not None:
        text = plain_text.strip() or None

    return {
        'msg_id': _text(message.get('msgId') or message.get('msg_id')).strip() or None,
        'sequence': _as_int(header.get('sequence') if 'sequence' in header
                            else message.get('sequence')),
        'sender_role': _text(header.get('senderRole') or message.get('senderRole')).strip() or None,
        'sender_name': _text(header.get('senderName') or message.get('senderName')).strip() or None,
        'sender_avatar': _text(header.get('senderAvatar')
                               or message.get('senderAvatar')).strip() or None,
        'gmt_create': _as_int(header.get('gmtCreate') if 'gmtCreate' in header
                              else message.get('gmtCreate')),
        'tips': _text(header.get('tips') or message.get('tips')).strip() or None,
        'content_type': _text(message.get('contentType')
                              or header.get('contentType')).strip() or None,
        'text': text,
        'image': image,
        'content_parsed': parsed_json,
        'content_keys': sorted(str(key) for key in content),
        'header_keys': sorted(str(key) for key in header),
        'raw': message,
    }


def parse_conversation(raw: Any) -> Dict[str, Any]:
    """`get.conversation` 的回执：`data.conversationId`。"""
    body = _body(raw)
    value, key = _first_key(body, CONVERSATION_ID_KEYS)
    conversation_id = _text(value).strip() or None
    return {
        'conversation_id': conversation_id,
        'conversation_id_key': key,
        'found': bool(conversation_id),
        'body_keys': sorted(str(key) for key in body),
    }


def parse_history(raw: Any) -> Dict[str, Any]:
    """`query.history` 的回执：`data.messageList[]` + `data.currentTime`。"""
    body = _body(raw)
    value, key = _first_key(body, MESSAGE_LIST_KEYS)
    rows: List[Any] = value if isinstance(value, list) else []
    messages = [normalize_message(row) for row in rows]
    current_time, time_key = _first_key(body, CURRENT_TIME_KEYS)
    first_sequence = messages[0]['sequence'] if messages else None
    return {
        'messages': messages,
        'count': len(messages),
        'list_key': key,
        'current_time': _text(current_time).strip() or None,
        'current_time_key': time_key,
        'has_more': bool(messages) and len(messages) >= PAGE_SIZE and first_sequence != 1,
        'body_keys': sorted(str(key) for key in body),
    }


def parse_unread(raw: Any) -> Dict[str, Any]:
    """`query.unread.list` 的回执：`data.result[]`。"""
    body = _body(raw)
    value, key = _first_key(body, UNREAD_LIST_KEYS)
    rows: List[Any] = value if isinstance(value, list) else []
    messages = [normalize_message(row) for row in rows]
    return {
        'messages': messages,
        'count': len(messages),
        'list_key': key,
        'body_keys': sorted(str(key) for key in body),
    }


def parse_send(raw: Any) -> Dict[str, Any]:
    """`send` / `mark.read` 的回执。

    `saved` 表示平台没有拒绝；`msg_id` / `sequence` / `tips` 平台没给就是 `None`。
    """
    failure = _business_error(raw)
    body = _body(raw)
    success = body.get('success')
    saved = not failure
    if isinstance(success, bool):
        saved = success and not failure
    return {
        'saved': saved,
        'msg_id': _text(body.get('msgId') or body.get('msg_id')).strip() or None,
        'sequence': _as_int(body.get('sequence')),
        'tips': _text(body.get('tips')).strip() or None,
        'platform_message': failure or (_text(body.get('failReason')).strip() or None),
        'envelope': envelope_message(raw),
        'body_keys': sorted(str(key) for key in body),
        'found': bool(body),
    }
