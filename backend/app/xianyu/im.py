"""IM（WebSocket）帧构造与会话。

闲鱼的聊天走 ``wss://wss-goofish.dingtalk.com/``，协议是钉钉的 LWP：每条
消息一个 ``{"lwp": <path>, "headers": {...}, "body": [...]}`` 的 JSON 帧。

这里把**帧构造**抽成纯函数，好处是可以在不连真实平台的前提下断言「发出去的
字节长什么样」—— IM 的坑几乎全在帧结构上（少一个 ``@goofish`` 后缀、``data``
忘了 base64、``actualReceivers`` 少了自己），连上真机才发现代价太大。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import fingerprint, mtop

logger = logging.getLogger(__name__)

# 生产域名。注意 ws 的 Host 头必须是这个，写 www.goofish.com 会被拒。
WS_URL = 'wss://wss-goofish.dingtalk.com/'

# 钉钉 IM 客户端声明。``ua`` 沿用旧项目实测值：服务端会校验这个串的格式，
# 自造的 UA 会被判定为「非官方客户端」。注意它与 fingerprint.CHROME_UA 是
# **两个不同的东西**：前者是 IM 客户端身份，后者是网页身份。
IM_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 '
    'DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) '
    'DingWeb/2.1.5 IMPaaS DingWeb/2.1.5'
)

IM_APP_KEY = '444e9908a51d1cb236a27862abc769c9'
IM_WS_VERSION = 'im:3,au:3,sy:6'

# 消息长度上限（服务端限制，超了直接报错）。
MAX_TEXT_LENGTH = 2000

# 拉历史消息时表示「从最新往回」的哨兵值。
LATEST_TIMESTAMP = 9007199254740991


def _with_goofish(value: str) -> str:
    """补 ``@goofish`` 后缀。cid / toid 有时带有时不带，统一一次。"""
    value = str(value or '')
    return value if '@goofish' in value else f'{value}@goofish'


def _strip_goofish(value: Any) -> str:
    """去掉 ``@goofish`` 后缀（入库的 buyer_id / item_id 不要带它）。"""
    return str(value or '').split('@goofish')[0].strip()


def _first_str(source: Dict[str, Any], *keys: str) -> str:
    """按顺序取第一个非空字段（不同版本推送的键名不一样）。"""
    for key in keys:
        value = source.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


def _decode_content(raw: Any) -> str:
    """把 LWP 消息的 ``content`` 归一化成纯文本。

    三种在线上都出现过：``{"text": {"text": "..."}}``、直接字符串、以及
    ``{"custom": {"data": base64(json)}}``（和 ``build_send_frame`` 对称）。
    """
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, dict):
        return ''
    text = raw.get('text')
    if isinstance(text, dict):
        return str(text.get('text') or '')
    if isinstance(text, str):
        return text
    custom = raw.get('custom')
    if isinstance(custom, dict) and isinstance(custom.get('data'), str):
        try:
            decoded = base64.b64decode(custom['data']).decode('utf-8')
        except Exception:  # 不是合法 base64 -> 当作解析不出
            return ''
        try:
            return _decode_content(json.loads(decoded))
        except (TypeError, ValueError):
            return decoded
    return ''


class ImError(RuntimeError):
    """IM 层失败（鉴权被拒、RPC 回错误码）。

    单独一个类型，是为了让服务层能把它与「网络断了」区分开：鉴权被拒重连一百次
    也没用，得先把 device id / token 修对。
    """


def _load_content(raw: Any) -> Optional[Dict[str, Any]]:
    """把 ``content.custom.data`` 解成 JSON（可能是明文 JSON，也可能是 base64）。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError):
        pass
    try:
        parsed = json.loads(base64.b64decode(raw).decode('utf-8'))
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - 不是合法 base64/JSON 就当解不出
        return None


def _interpret_content(decoded: Dict[str, Any]) -> Tuple[str, List[str], str]:
    """把解出来的 content JSON 归一化成 ``(文本, 图片URL列表, msg_type)``。

    形状与旧项目实测一致：``contentType`` 1=文本 2=图片 3=语音；有的版本不给
    ``contentType``，只能靠有没有 ``text`` / ``image`` 字段判断。
    """
    content_type = decoded.get('contentType')
    text_value = decoded.get('text')
    if content_type == 1 or text_value is not None:
        if isinstance(text_value, dict):
            return str(text_value.get('text') or ''), [], 'text'
        return str(text_value or ''), [], 'text'

    image = decoded.get('image')
    if content_type == 2 or isinstance(image, dict):
        pics = image.get('pics', []) if isinstance(image, dict) else []
        images = [
            str(pic.get('url'))
            for pic in pics
            if isinstance(pic, dict) and pic.get('url')
        ]
        legacy_url = decoded.get('picUrl')
        if legacy_url and not images:
            images.append(str(legacy_url))
        return '', images, 'image'

    if content_type == 3 or decoded.get('audio'):
        return '[语音消息]', [], 'system'
    if decoded.get('title') or decoded.get('template'):
        return str(decoded.get('title') or decoded.get('template')), [], 'card'
    return '', [], ''


def extract_message_summary(model: Dict[str, Any], limit: int = 80) -> str:
    """会话列表里那一行「最后一条消息」的摘要文本。"""
    message = model.get('message')
    if not isinstance(message, dict):
        message = model if isinstance(model, dict) else {}
    content = message.get('content')
    custom = content.get('custom') if isinstance(content, dict) else None
    if isinstance(custom, dict):
        summary = custom.get('summary') or custom.get('degrade')
        if summary:
            return str(summary)[:limit]
        decoded = _load_content(custom.get('data'))
        if decoded:
            text, images, msg_type = _interpret_content(decoded)
            if text:
                return text[:limit]
            if images or msg_type == 'image':
                return '[图片]'
    raw = _decode_content(content)
    if raw:
        return raw[:limit]
    return ''


def parse_conversation(raw: Any, my_id: str = '') -> Optional[Dict[str, Any]]:
    """把会话列表里的一条 ``userConvs`` 项归一化成工作台字段。

    **真实结构是三层**（真机抓帧 2026-10-06）::

        userConvs[i] = {'singleChatUserConversation': {...}, 'type': ...}
        singleChatUserConversation = {
            'cid'?: ..., 'pairFirst'?: ..., 'pairSecond'?: ..., 'extension'?: ...,
            'lastMessage': {...}, 'modifyTime': ..., 'redPoint': ...,
            'singleChatConversation': {'cid': ..., 'pairFirst': ...,
                                       'pairSecond': ..., 'extension': {...}},
        }

    字段落在哪一层**会随版本变**（实测 ``cid`` / ``pairFirst`` / ``pairSecond`` /
    ``extension`` 只在最内层 ``singleChatConversation`` 里，而 ``lastMessage`` /
    ``modifyTime`` / ``redPoint`` 在中间层）。所以这里「内层优先、外层兜底」逐层找 ——
    只认某一层的结果就是**整个会话列表解析成空**（真机上买家消息一条都看不到）。

    群聊条目（``groupChatUserConversation``）不是单聊，直接跳过：**取不到 cid 就
    返回 None**，绝不造一个空会话出来。
    """
    if not isinstance(raw, dict):
        return None
    if 'singleChatUserConversation' not in raw and 'groupChatUserConversation' in raw:
        return None
    conversation = raw.get('singleChatUserConversation')
    if not isinstance(conversation, dict):
        conversation = raw
    inner = conversation.get('singleChatConversation')
    inner = inner if isinstance(inner, dict) else {}

    def pick(*keys: str) -> str:
        return (
            _first_str(inner, *keys)
            or _first_str(conversation, *keys)
            or _first_str(raw, *keys)
        )

    def pick_number(*keys: str) -> int:
        for source in (conversation, inner, raw):
            for key in keys:
                value = source.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return int(value)
                if isinstance(value, str) and value.strip().lstrip('-').isdigit():
                    return int(value.strip())
        return 0

    cid = _strip_goofish(pick('cid'))
    if not cid:
        return None

    first = _strip_goofish(pick('pairFirst'))
    second = _strip_goofish(pick('pairSecond'))
    my_id = _strip_goofish(my_id)
    buyer_id = second if first == my_id else first
    if not buyer_id or buyer_id == '0':
        buyer_id = _strip_goofish(pick('pairFirst', 'pairSecond'))
    if not buyer_id or buyer_id == '0':
        return None

    extension: Dict[str, Any] = {}
    for source in (conversation, inner, raw):
        candidate = source.get('extension')
        if isinstance(candidate, dict) and candidate:
            extension = candidate
            break

    last_wrapper = conversation.get('lastMessage')
    if not isinstance(last_wrapper, dict):
        last_wrapper = raw.get('lastMessage')
    if not isinstance(last_wrapper, dict):
        last_wrapper = inner.get('lastMessage')
    last_wrapper = last_wrapper if isinstance(last_wrapper, dict) else {}
    last_message = last_wrapper.get('message')
    last_message = last_message if isinstance(last_message, dict) else {}
    last_extension = last_message.get('extension')
    last_extension = last_extension if isinstance(last_extension, dict) else {}

    buyer_name = _first_str(last_extension, 'reminderTitle', 'senderNick', 'senderNickName')
    if buyer_name.isdigit():
        buyer_name = ''
    if not buyer_name:
        buyer_name = _first_str(last_wrapper, 'senderNick', 'senderNickName')

    return {
        'cid': cid,
        'buyer_id': buyer_id,
        'buyer_name': buyer_name,
        'buyer_avatar': _first_str(
            extension, 'avatar', 'avatarUrl', 'senderAvatar'
        ) or _first_str(last_extension, 'senderAvatar'),
        'item_id': _strip_goofish(_first_str(extension, 'itemId')),
        'item_title': _first_str(extension, 'itemTitle'),
        # 摘要要从 ``lastMessage`` 那一层取：整条 userConvs 项里并没有 ``message`` 键，
        # 直接传 raw 会永远解析出空字符串（会话列表「最后一条消息」全空白）。
        'last_message': extract_message_summary(last_wrapper or raw),
        'last_time': pick_number('modifyTime', 'createAt'),
        'unread': pick_number('redPoint'),
    }


def parse_message_item(model: Any, my_id: str = '') -> Optional[Dict[str, Any]]:
    """把 ``listUserMessages`` 里的一条消息归一化成工作台字段。"""
    if not isinstance(model, dict):
        return None
    message = model.get('message')
    if not isinstance(message, dict):
        message = model
    extension = message.get('extension')
    extension = extension if isinstance(extension, dict) else {}
    sender_id = _strip_goofish(_first_str(extension, 'senderUserId', 'senderId'))
    content = message.get('content')
    custom = content.get('custom') if isinstance(content, dict) else None
    custom = custom if isinstance(custom, dict) else {}

    text, images, msg_type = '', [], ''
    decoded = _load_content(custom.get('data'))
    if decoded:
        text, images, msg_type = _interpret_content(decoded)
    if not text and not images:
        summary = custom.get('summary') or custom.get('degrade')
        text = str(summary or '')
    if not msg_type:
        msg_type = 'image' if images else ('text' if text else 'system')

    return {
        'msg_id': _first_str(message, 'messageId', 'msgId', 'uuid'),
        'cid': _strip_goofish(_first_str(message, 'cid', 'conversationId')),
        'buyer_id': sender_id,
        'is_self': bool(my_id) and sender_id == _strip_goofish(my_id),
        'content': text,
        'images': images,
        'msg_type': msg_type,
        'time': int(message.get('createAt') or message.get('time') or 0),
    }


def parse_incoming_message(payload: Any) -> Optional[Dict[str, Any]]:
    """从一帧 IM 推送里抠出一条买家消息。

    返回 ``{'msg_id','cid','buyer_id','seller_id','content','item_id'}``；
    **返回 None 表示这一帧不是可处理的买家文本消息**（心跳回帧、纯协议回帧等），
    调用方据此跳过，不要凭空造会话。

    `msg_id` 是平台侧的幂等键，服务层拿它去重（见 `services.chats.handle_incoming`）。
    """
    if not isinstance(payload, dict):
        return None

    body = payload.get('body')
    item: Any = None
    if isinstance(body, list):
        item = next((entry for entry in body if isinstance(entry, dict)), None)
    elif isinstance(body, dict):
        inner = body.get('message')
        item = inner if isinstance(inner, dict) else body
    if not isinstance(item, dict):
        item = payload  # 有些推送把字段直接摊在顶层

    content = _decode_content(item.get('content'))
    if not content:
        raw_text = item.get('text')
        content = raw_text if isinstance(raw_text, str) else ''
    content = content.strip()
    if not content:
        return None

    extension = item.get('extension')
    item_id = ''
    if isinstance(extension, dict):
        item_id = _first_str(extension, 'itemId', 'item_id')
        if not item_id and isinstance(extension.get('extJson'), str):
            try:
                item_id = _first_str(json.loads(extension['extJson']) or {}, 'itemId', 'item_id')
            except (TypeError, ValueError):
                item_id = ''
    if not item_id:
        item_id = _first_str(item, 'itemId', 'item_id')

    return {
        'msg_id': _first_str(item, 'msgId', 'msg_id', 'messageId', 'message_id', 'uuid', 'id'),
        'cid': _strip_goofish(_first_str(item, 'cid', 'conversationId', 'conversation_id')),
        'buyer_id': _strip_goofish(
            _first_str(item, 'senderId', 'sender_id', 'sender', 'from', 'userId', 'user_id')
        ),
        'seller_id': _strip_goofish(
            _first_str(item, 'receiverId', 'receiver_id', 'to', 'targetId')
        ),
        'content': content,
        'item_id': item_id,
    }


def frame_summary(payload: Any, limit: int = 200) -> Dict[str, str]:
    """给「认不出的入站帧」做一条可检索的摘要：原始 type + 截断后的 body。

    首次上线只能靠它反推真实帧形状（`parse_incoming_message` 返回 None 的帧里混着
    心跳回帧、协议回帧，也可能混着还没适配的买家消息形状）。**只落 type 与截断 body**：
    整帧可能很大，也可能带 cookie / token 一类字段。
    """
    if not isinstance(payload, dict):
        return {'type': type(payload).__name__, 'body': repr(payload)[:limit]}

    headers = payload.get('headers')
    frame_type = ''
    if isinstance(headers, dict):
        frame_type = str(headers.get('lwp') or '')
    if not frame_type:
        frame_type = str(payload.get('lwp') or payload.get('type') or '')
    if not frame_type and isinstance(headers, str):
        frame_type = headers

    body = payload.get('body')
    if isinstance(body, str):
        text = body
    else:
        try:
            text = json.dumps(body, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(body)
    return {'type': frame_type, 'body': text[:limit]}


def build_reg_frame(token: str, device_id: str) -> Dict[str, Any]:
    """``/reg`` 注册帧。没有它，后续所有 RPC 都会被服务端丢弃。"""
    return {
        'lwp': '/reg',
        'headers': {
            'cache-header': 'app-key token ua wv',
            'app-key': IM_APP_KEY,
            'token': token,
            'ua': IM_UA,
            'dt': 'j',
            'wv': IM_WS_VERSION,
            'sync': '0,0;0;0;',
            'did': device_id,
            'mid': mtop.generate_mid(),
        },
    }


def build_ack_frame() -> Dict[str, Any]:
    """``/r/SyncStatus/ackDiff`` 同步位点帧。

    ``pts`` 是**毫秒时间戳再乘 1000**（微秒），旧项目实测如此；写成毫秒会
    导致同步位点错位、收不到消息。
    """
    now_ms = int(time.time() * 1000)
    return {
        'lwp': '/r/SyncStatus/ackDiff',
        'headers': {'mid': mtop.generate_mid()},
        'body': [
            {
                'pipeline': 'sync',
                'tooLong2Tag': 'PNM,1',
                'channel': 'sync',
                'topic': 'sync',
                'highPts': 0,
                'pts': now_ms * 1000,
                'seq': 0,
                'timestamp': now_ms,
            }
        ],
    }


def build_heartbeat_frame() -> Dict[str, Any]:
    return {'lwp': '/!', 'headers': {'mid': mtop.generate_mid()}}


def build_create_chat_frame(toid: str, my_id: str, item_id: str = '') -> Dict[str, Any]:
    """``/r/SingleChatConversation/create`` 建会话帧。

    有些买家在商品页发起咨询前没有会话，必须先 create 再 send；对已有会话
    重复 create 是幂等的。
    """
    return {
        'lwp': '/r/SingleChatConversation/create',
        'headers': {'mid': mtop.generate_mid()},
        'body': [
            {
                'pairFirst': _with_goofish(toid),
                'pairSecond': _with_goofish(my_id),
                'bizType': '1',
                'extension': {'itemId': str(item_id or '')},
                'ctx': {'appVersion': '1.0', 'platform': 'web'},
            }
        ],
    }


def build_send_frame(cid: str, toid: str, my_id: str, text: str) -> Dict[str, Any]:
    """``/r/MessageSend/sendByReceiverScope`` 发文本帧。

    三个必须点：
    - ``content.custom.data`` 是 ``base64(json)``，**不是**明文 JSON；
    - ``actualReceivers`` 要同时带上对方和**自己**，少了自己这条消息不会出现在
      自己的会话里；
    - ``cid`` / ``toid`` 都要带 ``@goofish``。
    """
    text = str(text or '').strip()
    if not text:
        raise ValueError('消息内容不能为空')
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f'消息内容不能超过 {MAX_TEXT_LENGTH} 个字符')

    inner = {'contentType': 1, 'text': {'text': text}}
    encoded = base64.b64encode(
        json.dumps(inner, ensure_ascii=False).encode('utf-8')
    ).decode('utf-8')
    return {
        'lwp': '/r/MessageSend/sendByReceiverScope',
        'headers': {'mid': mtop.generate_mid()},
        'body': [
            {
                'uuid': mtop.generate_uuid(),
                'cid': _with_goofish(cid),
                'conversationType': 1,
                'content': {
                    'contentType': 101,
                    'custom': {'type': 1, 'data': encoded},
                },
                'redPointPolicy': 0,
                'extension': {'extJson': '{}'},
                'ctx': {'appVersion': '1.0', 'platform': 'web'},
                'mtags': {},
                'msgReadStatusSetting': 1,
            },
            {
                'actualReceivers': [
                    _with_goofish(toid),
                    _with_goofish(my_id),
                ],
            },
        ],
    }


def build_list_messages_frame(cid: str, start_timestamp: Optional[int] = None,
                              limit: int = 20) -> Dict[str, Any]:
    """``/r/MessageManager/listUserMessages`` 拉会话消息帧。"""
    if start_timestamp is None:
        start_timestamp = LATEST_TIMESTAMP
    return {
        'lwp': '/r/MessageManager/listUserMessages',
        'headers': {'mid': mtop.generate_mid()},
        'body': [
            _with_goofish(cid),
            False,
            int(start_timestamp),
            max(1, min(int(limit), 100)),
            False,
        ],
    }


def build_conversations_frame(start_timestamp: int = LATEST_TIMESTAMP,
                              limit: int = 50) -> Dict[str, Any]:
    """``/r/Conversation/listNewestPagination`` 拉会话列表帧。

    ``start_timestamp`` 的「最新」哨兵是 ``LATEST_TIMESTAMP``（旧项目实测），
    传 0 不代表最新，可能直接回空。
    """
    return {
        'lwp': '/r/Conversation/listNewestPagination',
        'headers': {'mid': mtop.generate_mid()},
        'body': [int(start_timestamp), max(1, min(int(limit), 100))],
    }


def ws_headers(cookie_str: str) -> Dict[str, str]:
    """握手用的 HTTP 头。"""
    return {
        'Cookie': cookie_str,
        'Host': 'wss-goofish.dingtalk.com',
        'Connection': 'Upgrade',
        'Pragma': 'no-cache',
        'Cache-Control': 'no-cache',
        'User-Agent': fingerprint.CHROME_UA,
        'Origin': 'https://www.goofish.com',
        'Accept-Encoding': 'gzip, deflate, br, zstd',
        'Accept-Language': fingerprint.ACCEPT_LANG,
    }


def _connect_kwargs(headers: Dict[str, str]) -> Dict[str, Any]:
    """``websockets.connect`` 的头参数名跨版本变过。

    websockets 12.x 用 ``additional_headers``；旧版本用 ``extra_headers``。
    这里探测一次签名，避免「传了不认识的 kwarg 直接 TypeError」。
    """
    import inspect

    import websockets

    params = inspect.signature(websockets.connect).parameters
    if 'additional_headers' in params:
        return {'additional_headers': headers}
    if 'extra_headers' in params:
        return {'extra_headers': headers}
    return {}


class ImSession:
    """一条 IM WebSocket 会话：注册 → 请求/响应配对 → 心跳 → 入站分发。

    请求/响应靠 ``headers.mid`` 配对：服务端回帧会原样带回 mid，据此唤醒
    对应的 future。没有这层，多个并发调用会互相吃掉响应。

    **入站分发**：买家主动发来的消息不配对任何 pending future，这类帧交给
    `on_message()` 注册的回调（`dispatch()`）。回调由**服务层**注册
    （`services.chats.attach_im_session`）——协议层不认识 services，保持层次。
    """

    def __init__(self, cookie_str: str, token: str, my_id: str,
                 url: str = WS_URL, timeout: float = 15.0,
                 device_id: Optional[str] = None):
        self.cookie_str = cookie_str
        self.token = token
        self.my_id = my_id
        self.url = url
        self.timeout = timeout
        #: 必须与「取这个 token 时用的 deviceId」**完全一致**：服务端比对两者，
        #: 不一致会回 401 ``device id or appkey is not equal``。调用方（client）
        #: 从同一次 token 请求里把 device id 传进来；单独构造时才自己算一个。
        self.device_id = device_id or mtop.generate_device_id(my_id)
        self._ws = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._heartbeat: Optional[asyncio.Task] = None
        self._handlers: List[Callable[[Dict[str, Any]], Any]] = []

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # -------------------------------------------------------------- 入站回调

    def on_message(self, handler: Callable[[Dict[str, Any]], Any]) -> Callable:
        """注册入站消息回调（也可当装饰器用），返回 handler 本身。"""
        if handler not in self._handlers:
            self._handlers.append(handler)
        return handler

    def off_message(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def dispatch(self, payload: Any) -> int:
        """把一帧**非配对**推送交给所有注册的 handler，返回成功投递数。

        单个 handler 抛异常只记日志，绝不让读循环断掉（否则一条坏消息会让
        整条 IM 连接静默失聪）。
        """
        delivered = 0
        for handler in list(self._handlers):
            try:
                result = handler(payload)
            except Exception as exc:
                logger.warning('IM 入站 handler 抛异常，已跳过: %s', exc)
                continue
            if inspect.isawaitable(result):
                try:
                    asyncio.ensure_future(result)
                except RuntimeError:  # 没有事件循环：关掉协程，避免 "never awaited" 警告
                    close = getattr(result, 'close', None)
                    if callable(close):
                        close()
            delivered += 1
        return delivered

    async def connect(self) -> None:
        import websockets

        self._ws = await websockets.connect(
            self.url, **_connect_kwargs(ws_headers(self.cookie_str))
        )
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await self._register()
        except Exception:
            await self.close()
            raise
        await self._send(build_ack_frame())
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())

    async def _register(self) -> None:
        """发 ``/reg`` 并**等它的回帧**、校验结果。

        旧实现发完就睡一秒，服务端回 401 也没人看 —— 于是「device id 对不上」
        这种硬失败在库里表现成「IM 已挂载、只是没有消息」，用户看到的就是
        一条私信都同步不过来。注册没成功必须当场抛错。

        **成功码是 200 不是 0**（真机回帧 2026-10-06：
        ``{"headers": {"reg-uid": "2217970177348@goofish", ...}, "code": 200,
        "body": {"unitName": "PNM", "timestamp": ...}}``）。这里曾把「非 0」当成
        失败，导致注册明明成功却报「IM 注册未成功(code=200)」、IM 一直挂不上 ——
        凡 2xx 都算成功，只有真正的错误码才抛。
        """
        response = await self.request(build_reg_frame(self.token, self.device_id))
        code = int((response or {}).get('code') or 0)
        if code and not 200 <= code < 300:
            body = (response or {}).get('body') or {}
            reason = ''
            if isinstance(body, dict):
                reason = str(body.get('reason') or body.get('developerMessage') or '')
            raise ImError(f'IM 注册未成功(code={code})' + (f': {reason}' if reason else ''))

    async def close(self) -> None:
        for task in (self._heartbeat, self._reader):
            if task is not None:
                task.cancel()
        self._heartbeat = None
        self._reader = None
        self._fail_pending('连接已关闭')
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send(self, frame: Dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError('IM 未连接')
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                mid = str(((payload or {}).get('headers') or {}).get('mid') or '')
                future = self._pending.pop(mid, None)
                if future is not None and not future.done():
                    future.set_result(payload)
                else:
                    # 不配对任何 pending 请求 -> 服务端主动推来的买家消息
                    self.dispatch(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 连接断开
            self._fail_pending(str(exc))

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                await self._send(build_heartbeat_frame())
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _fail_pending(self, reason: str) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ConnectionError(reason))

    async def request(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """发一帧并等回帧（按 mid 配对）；回帧 code >= 400 一律抛错。

        以前这里不看 ``code``：服务端回 400 时调用方拿到 ``{}``，与「平台确实
        没有会话」不可区分，失败被静默吞成空列表 —— 排查时最费时间的一类假象。
        """
        mid = frame['headers']['mid']
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[mid] = future
        try:
            await self._send(frame)
            response = await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(mid, None)
        self._raise_on_error(frame, response)
        return response

    @staticmethod
    def _raise_on_error(frame: Dict[str, Any], response: Any) -> None:
        """回帧 ``code >= 400`` 时抛出带平台原文的 ``ImError``。"""
        code = int((response or {}).get('code') or 0)
        if code < 400:
            return
        body = (response or {}).get('body')
        body = body if isinstance(body, dict) else {}
        reason = str(
            body.get('reason') or body.get('developerMessage') or body.get('code') or ''
        )
        lwp = str(frame.get('lwp') or '')
        if lwp == '/reg':
            raise ImError(
                f'IM 注册被拒(code={code}): {reason or "平台未给原因"}；'
                f'/reg 的 did 必须与取 token 用的 deviceId 相同'
            )
        raise ImError(f'IM 请求 {lwp} 失败(code={code}): {reason or "平台未给原因"}')

    async def send_message(self, cid: str, toid: str, text: str) -> Dict[str, Any]:
        response = await self.request(build_send_frame(cid, toid, self.my_id, text))
        body = (response or {}).get('body') or {}
        if isinstance(body, dict) and (body.get('reason') or body.get('code')):
            reason = body.get('developerMessage') or body.get('reason') or body.get('code')
            raise RuntimeError(str(reason))
        return response

    async def fetch_messages(self, cid: str, start_timestamp: Optional[int] = None,
                             limit: int = 20) -> List[Dict[str, Any]]:
        """拉一个会话的历史消息（归一化后的字段）。

        真实响应形状（真机抓帧 2026-10-06）是 ``body['userMessageModels']``::

            {"code": 200, "body": {"nextCursor": ..., "hasMore": ...,
                                   "userMessageModels": [{"message": {...}}, ...]}}

        也见过消息直接挂在 ``body`` 下、或包在 ``body['data']`` 里。键名读错的结果
        是**永远拿到空列表** —— 真机上就表现为「会话能列出来，点开一条消息都没有」，
        与「平台确实没有历史消息」不可区分。所以这里把三段都认。
        """
        response = await self.request(
            build_list_messages_frame(cid, start_timestamp, limit)
        )
        body = (response or {}).get('body')
        inner = body.get('data') if isinstance(body, dict) and isinstance(body.get('data'), dict) else body
        if isinstance(inner, dict):
            raw_items = (
                inner.get('userMessageModels')
                or inner.get('messages')
                or inner.get('messageModels')
                or inner.get('list')
                or inner.get('data')
                or []
            )
        else:
            raw_items = inner or []
        parsed = []
        for item in raw_items if isinstance(raw_items, list) else []:
            one = parse_message_item(item, self.my_id)
            if one:
                parsed.append(one)
        return parsed

    async def fetch_conversations(self, start_timestamp: int = LATEST_TIMESTAMP,
                                  limit: int = 50) -> List[Dict[str, Any]]:
        """拉会话列表（归一化后的字段）。

        真实结构是 ``body['userConvs']``，每项外面还裹一层
        ``singleChatUserConversation``；老实现读 ``body['conversations']``，
        即使鉴权成功也永远是空列表。
        """
        response = await self.request(build_conversations_frame(start_timestamp, limit))
        body = (response or {}).get('body')
        if isinstance(body, dict) and isinstance(body.get('data'), dict):
            body = body['data']
        raw_items = body.get('userConvs') if isinstance(body, dict) else body
        if raw_items is None and isinstance(body, dict):
            raw_items = body.get('conversations') or body.get('list') or []
        parsed = []
        for item in raw_items if isinstance(raw_items, list) else []:
            one = parse_conversation(item, self.my_id)
            if one:
                parsed.append(one)
        return parsed
