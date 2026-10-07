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


#: 图片条目里可能放 URL 的键（聊天图片实测用 ``url``；商品 ``imageInfos`` 用
#: ``major``/``origin``/``thumb``）。按优先级取第一个非空值。
IMAGE_URL_KEYS = ('url', 'picUrl', 'major', 'origin', 'thumb')

#: 语音地址/时长的可能键名。**真机没抓到过语音帧**（库里 806 条历史消息 0 条语音），
#: 这几个键是从 ``contentType == 3`` 这条线索推的，所以「有就取」，不认键名之外的东西。
AUDIO_URL_KEYS = ('url', 'audioUrl', 'audio_url', 'voiceUrl', 'playUrl', 'src')
AUDIO_DURATION_KEYS = ('duration', 'audioDuration', 'voiceDuration', 'length')

#: 「像秒数」的时长区间：只在里面才把时长显示出来（见 `_voice_summary`）。
VOICE_SECONDS_RANGE = (1, 600)


def _image_urls(image: Any) -> List[str]:
    """从 ``image`` 里取出所有图片 URL（保序去重）。

    ``pics`` 里每一项**可能是 str 也可能是 dict**：旧项目真实发图帧里是
    ``{'url','width','height','type'}``（``XianyuAutoAsync.py:11471-11487``），
    而有的版本直接给裸 URL 字符串 —— 两种都得认，认错一种就是「图片消息变空消息」。
    """
    if isinstance(image, str):
        return [image.strip()] if image.strip() else []
    if not isinstance(image, dict):
        return []
    pics = image.get('pics')
    pics = pics if isinstance(pics, (list, tuple)) else []
    urls: List[str] = []
    for pic in pics:
        if isinstance(pic, dict):
            url = _first_str(pic, *IMAGE_URL_KEYS)
        else:
            url = pic if isinstance(pic, str) else ''
        url = str(url or '').strip()
        if url and url not in urls:
            urls.append(url)
    if not urls:
        # 没有 ``pics`` 或里面全是空值：兜底 ``image`` 自己就是地址
        url = _first_str(image, *IMAGE_URL_KEYS).strip()
        if url:
            urls.append(url)
    return urls


def _interpret_audio(decoded: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """从语音帧里取 ``(audio_url, audio_duration)``，取不到给 ``('', None)``。

    **不做单位换算**：时长到底是秒还是毫秒没有真机证据，猜错就是给用户看假数据，
    所以原样回一个 int（或 None），显示口径交给 `_voice_summary`。
    """
    source: Any = None
    for key in ('audio', 'voice', 'audioInfo', 'voiceInfo'):
        value = decoded.get(key)
        if isinstance(value, (dict, str)) and value:
            source = value
            break
    if isinstance(source, str):
        return source.strip(), None
    if source is None:
        source = decoded  # 有的帧把地址/时长直接摊在 content 顶层
    if not isinstance(source, dict):
        return '', None

    url = _first_str(source, *AUDIO_URL_KEYS).strip()
    duration: Optional[int] = None
    for key in AUDIO_DURATION_KEYS:
        value = source.get(key)
        if value is None or isinstance(value, bool) or value == '':
            continue
        if isinstance(value, (int, float)):
            duration = int(value)
            break
        text = str(value).strip()
        if text.isdigit():
            duration = int(text)
            break
    return url, duration


#: 语音时长的单位**没有真机证据**，只能按大小猜：大于这个数当毫秒，否则当秒。
#: 没人会发 300 秒以上的语音，所以这个阈值不会误伤。
#:
#: **这是全站唯一一处判定。** 服务层 `backend/app/services/chats.py::audio_seconds`
#: 刻意改成调用本文件里的同一个函数：以前两层各写一套（协议层当秒、服务层当毫秒），
#: 一条 400 的语音会同时显示成「[语音] 400″」和「0.4 秒」—— 用户看到的是界面自己打自己。
AUDIO_MS_THRESHOLD = 300


def audio_seconds(value: Any) -> Optional[float]:
    """语音时长统一成**秒**（保留一位小数）；取不到 / 非正数给 ``None``。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > AUDIO_MS_THRESHOLD:
        number = number / 1000.0
    return round(number, 1)


def _voice_summary(duration: Optional[int]) -> str:
    """语音摘要：``[语音]``，时长看得出来是几秒时带上 ``3″``。

    单位未验证（见 `_interpret_audio`），所以只在 ``VOICE_SECONDS_RANGE`` 里显示数字
    —— 显示错一个数字比不显示更糟。**换算必须走 `audio_seconds()`**，这样摘要里的
    秒数和服务层交给前端的 `audio_duration` 永远是同一个数。
    """
    seconds = audio_seconds(duration)
    low, high = VOICE_SECONDS_RANGE
    if seconds is None or not (low <= seconds <= high):
        return '[语音]'
    text = f'{seconds:.1f}'.rstrip('0').rstrip('.')
    return f'[语音] {text}″'


def _system_text(content_type: Any) -> str:
    """认不出的 ``contentType`` 也要给一句可读文案。

    空串在界面上就是「(空消息)」，所以这里**绝不返回空串**。旧项目注释里
    ``contentType=14`` 是「tip 类型消息」（订单/物流提示，`order_status_handler.py:162`），
    归到系统消息；其余（卡片、商品卡、小程序…）一律叫卡片消息。
    """
    return '[系统消息]' if content_type == 14 else '[卡片消息]'


def _sender_uid_of(message: Any) -> str:
    """从一条消息里取**发送者 uid**（``extension.senderUserId`` / ``sender.uid``）。

    为什么先要 uid：``reminderTitle`` 是「最后一条消息**发送者**」的昵称，发送者是买家
    还是账号自己都可能是它（我发最后一条时它就是账号自己的昵称）。不确认发送者就把它
    当买家名，用户看到的会话列表会整列显示他自己的昵称。
    """
    if not isinstance(message, dict):
        return ''
    extension = message.get('extension')
    extension = extension if isinstance(extension, dict) else {}
    sender = message.get('sender')
    sender = sender if isinstance(sender, dict) else {}
    return _strip_goofish(
        _first_str(extension, 'senderUserId', 'senderId')
        or _first_str(sender, 'uid', 'userId', 'id')
    )


def _sender_nick_of(message: Any) -> str:
    """一条消息里**发送者**的昵称（``extension.reminderTitle`` / ``senderNick``）。

    返回的昵称不区分敌我（发送者是谁就是谁），纯数字的占位名直接丢掉 ——
    归属判定由调用方（`parse_conversation` / `parse_message_item`）负责。
    """
    if not isinstance(message, dict):
        return ''
    extension = message.get('extension')
    extension = extension if isinstance(extension, dict) else {}
    nick = _first_str(extension, 'reminderTitle', 'senderNick', 'senderNickName')
    if not nick:
        nick = _first_str(message, 'senderNick', 'senderNickName', 'reminderTitle')
    nick = str(nick or '').strip()
    return '' if nick.isdigit() else nick


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

    ``msg_type`` 取值 ``text`` / ``image`` / ``voice`` / ``card`` / ``system``。
    **图片和语音没有文本时文本回空串**（不编造正文，展示口径由调用方决定），
    但 ``msg_type`` 一定给出来 —— 认不出类型时也回一句可读文案（`_system_text`），
    空串在界面上就是那句「(空消息)」。图片**一张 URL 都抠不出来**时回 ``[图片]``
    （否则服务层会因为「既没正文也没图」把这条整条丢掉）。

    语音的地址/时长不在这里返回（三元组签名不能动），用 `_interpret_audio`。
    """
    content_type = decoded.get('contentType')
    text_value = decoded.get('text')
    # 语音先判：显式类型优先于同帧里可能残留的 text/image 字段
    if content_type == 3 or decoded.get('audio') or decoded.get('voice'):
        return '', [], 'voice'
    # 显式 contentType == 2 也要排在文本前面：真机帧里可能同时带一个空的 ``text`` 键，
    # 先走文本分支就会把图片整条丢掉（HEAD 起就有的坑，task-43 审查发现）。
    if content_type == 2:
        return _image_result(decoded)
    if content_type == 1 or text_value is not None:
        if isinstance(text_value, dict):
            return str(text_value.get('text') or ''), [], 'text'
        return str(text_value or ''), [], 'text'

    if decoded.get('image') is not None:  # 没给类型，但有 image 字段
        return _image_result(decoded)

    if decoded.get('title') or decoded.get('template'):
        return str(decoded.get('title') or decoded.get('template')), [], 'card'
    # 认不出的类型也给一句可读文案（不是空串），这样这类帧在界面上不会变成「(空消息)」。
    # ⚠️ 代价是它会被服务层当成一条买家消息：`services/chats.py` 的闸门只看
    # content/images/audio_url 非空，不按帧类型过滤，auto 模式会话会据此自动回复。
    # 「哪些 contentType 才允许当消息」需要按真机帧再收口（仓里还没有入站帧原文）。
    return _system_text(content_type), [], 'system'


def _image_result(decoded: Dict[str, Any]) -> Tuple[str, List[str], str]:
    """图片帧归一化：能抠出 URL 就「正文空 + ``images``」，一张都没有才给 ``[图片]``。"""
    images = _image_urls(decoded.get('image'))
    legacy_url = str(decoded.get('picUrl') or '').strip()
    if legacy_url and legacy_url not in images:
        images.append(legacy_url)
    return ('', images, 'image') if images else ('[图片]', [], 'image')


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
            if msg_type == 'voice':
                return _voice_summary(_interpret_audio(decoded)[1])
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

    ``buyer_name`` / ``buyer_avatar`` **只认买家**：会话条目里根本没有买家昵称字段，
    能拿到的只有 ``lastMessage.message.extension.reminderTitle``（= 最后一条消息发送者
    的昵称）。所以先用 ``extension.senderUserId`` / ``sender.uid`` 确认发送者不是
    ``my_id``，拿不到发送者或发送者就是自己时返回空串。
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

    # 昵称与头像的归属必须是**买家**：`reminderTitle` / `senderAvatar` 属于「最后一条
    # 消息的发送者」，自己发最后一条时那就是账号自己的昵称/头像（真机上报：客户名称整列
    # 显示成用户自己）。所以先取发送者 uid，确认不是自己（且不是 buyer_id 本身）才认；
    # 判定不了就留空 —— 宁可让前端兜底显示「买家 <id>」，也不能把错名字写进库。
    sender_id = _sender_uid_of(last_message) or _strip_goofish(
        _first_str(last_wrapper, 'senderUserId', 'senderId')
    )
    # my_id 判定得了就只认「发送者 != 我」；判定不了（my_id 为空）时唯一可用的证据是
    # 「发送者 != buyer_id」—— 这时连 buyer_id 本身都可能是账号自己，所以格外保守。
    if my_id:
        sender_is_buyer = bool(sender_id) and sender_id != my_id
    else:
        sender_is_buyer = bool(sender_id) and sender_id != buyer_id
    buyer_name = ''
    if sender_is_buyer:
        buyer_name = _sender_nick_of(last_message) or _sender_nick_of(last_wrapper)

    return {
        'cid': cid,
        'buyer_id': buyer_id,
        'buyer_name': buyer_name,
        'buyer_avatar': _first_str(
            extension, 'avatar', 'avatarUrl', 'senderAvatar'
        ) or (_first_str(last_extension, 'senderAvatar') if sender_is_buyer else ''),
        'item_id': _strip_goofish(_first_str(extension, 'itemId')),
        'item_title': _first_str(extension, 'itemTitle'),
        # 摘要要从 ``lastMessage`` 那一层取：整条 userConvs 项里并没有 ``message`` 键，
        # 直接传 raw 会永远解析出空字符串（会话列表「最后一条消息」全空白）。
        'last_message': extract_message_summary(last_wrapper or raw),
        'last_time': pick_number('modifyTime', 'createAt'),
        'unread': pick_number('redPoint'),
    }


def parse_message_item(model: Any, my_id: str = '') -> Optional[Dict[str, Any]]:
    """把 ``listUserMessages`` 里的一条消息归一化成工作台字段。

    除正文/图片外还回发送者信息：``sender_id``、``is_self``、``sender_nick``（只有确认
    发送者不是自己时才有值 = 买家昵称）、``sender_nick_raw``（不分敌我，用于学习账号自己
    的昵称）。

    媒体字段（task-43 补）：``images``（图片 URL 列表，可能多张）、``msg_type``
    （``text``/``image``/``voice``/``card``/``system``）、``audio_url`` 与
    ``audio_duration``（只有语音帧有值；时长原样给 int，**单位未验证**）。
    语音没有正文时 ``content`` 写成 ``[语音] 3″`` 这样的可读摘要，图片仍保持
    「正文空 + ``images`` 有值」的既有口径（`extract_message_summary` 才是给列表用的）。
    """
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
    audio_url, audio_duration = '', None
    decoded = _load_content(custom.get('data'))
    if decoded:
        text, images, msg_type = _interpret_content(decoded)
        if msg_type == 'voice':
            # 只对语音帧取地址/时长：卡片/订单帧顶层也有通用 ``url``/``length``，
            # 不加这个判断会把商品链接当成语音地址（服务层 `_display_msg_type`
            # 看到 audio_url 非空就判成语音，前端会渲染出播放器）。
            audio_url, audio_duration = _interpret_audio(decoded)
    if not text and not images:
        summary = custom.get('summary') or custom.get('degrade')
        text = str(summary or '')
    if not text and msg_type == 'voice':
        # 语音没有正文，给一句可读摘要；否则服务层会当成「空内容」把这条丢掉
        text = _voice_summary(audio_duration)
    if not msg_type:
        msg_type = 'image' if images else ('text' if text else 'system')

    my_id = _strip_goofish(my_id)
    is_self = bool(my_id) and sender_id == my_id
    sender_nick_raw = _sender_nick_of(message)
    # `sender_nick` 只放**能确认是买家**的昵称：`sync_im_conversations` 拿它补
    # `conversations.peer_nickname`，空着顶多显示「买家 <id>」，写错了就是用户看到的
    # 「客户名称显示不对」。确认不了（自己的消息 / 没有发送者 uid）一律空串。
    sender_nick = (
        sender_nick_raw if (sender_nick_raw and sender_id and my_id and not is_self) else ''
    )

    return {
        'msg_id': _first_str(message, 'messageId', 'msgId', 'uuid'),
        'cid': _strip_goofish(_first_str(message, 'cid', 'conversationId')),
        'buyer_id': sender_id,
        'sender_id': sender_id,
        'is_self': is_self,
        # 发送者昵称：`sender_nick` 已确认不是自己（买家昵称），`sender_nick_raw` 不区分敌我
        # （同步时用它学习「账号自己的昵称」，才能认出库里的脏昵称）。
        'sender_nick': sender_nick,
        'sender_nick_raw': sender_nick_raw,
        'content': text,
        'images': images,
        'msg_type': msg_type,
        'audio_url': audio_url,
        'audio_duration': audio_duration,
        'time': int(message.get('createAt') or message.get('time') or 0),
    }


def parse_incoming_message(payload: Any) -> Optional[Dict[str, Any]]:
    """从一帧 IM 推送里抠出一条买家消息。

    返回 ``{'msg_id','cid','buyer_id','seller_id','content','images','msg_type',
    'image_url','audio_url','audio_duration','item_id','sender_nick'}``；
    **返回 None 表示这一帧不是可处理的消息**（心跳回帧、纯协议回帧等），
    调用方据此跳过，不要凭空造会话。

    `sender_nick` 是这一帧发送者的昵称（可能是我自己发的回显帧）—— 归属由服务层判定，
    协议层不猜。

    `msg_id` 是平台侧的幂等键，服务层拿它去重（见 `services.chats.handle_incoming`）。

    媒体字段（task-43 补，与 `parse_message_item` 同一口径）：
    ``images`` 是图片 URL 列表（可能多张）、``msg_type`` ∈
    ``text``/``image``/``voice``/``card``/``system``、``image_url`` 是首图
    （服务层已经在找这个名字，见 `services/chats.dispatch_im_message`）、
    ``audio_url``/``audio_duration`` 只有语音帧有值（时长**单位未验证**）。

    **文本为空但有图片/语音时照样返回一条消息**（以前这里只取文本，图片/语音帧被当成
    「不是消息」直接丢）。语音帧的 ``content`` 写成 ``[语音] 3″`` 这样的摘要；图片帧保持
    「``content`` 空 + ``images`` 有值」的口径，服务层已按
    ``content or images or audio_url`` 三者之一判「是不是消息」
    （`services/chats.py` 的 `dispatch_im_message`），所以两种都能落库。
    纯空白正文（``'   '``）仍然返回 ``None``。

    ⚠️ **真机 WS 帧的形状还没确认**：仓里日志里入站帧全是
    ``{"syncPushPackage": {"data": [{"bizType": 40, "data": "<base64 二进制>"}]}}``
    且没解出过一条消息（生产库 806 条消息的 ``external_id`` 全是 ``.PNM``，即
    `listUserMessages` 同步路径），所以本函数认的 ``body[].content.custom.data``
    形状目前只有自造帧在验证。
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

    content_raw = item.get('content')
    custom = content_raw.get('custom') if isinstance(content_raw, dict) else None
    decoded = _load_content(custom.get('data')) if isinstance(custom, dict) else None

    text, images, msg_type = '', [], ''
    audio_url, audio_duration = '', None
    if decoded:
        text, images, msg_type = _interpret_content(decoded)
        if msg_type == 'voice':
            # 只对语音帧取地址/时长：卡片/订单帧顶层也有通用 ``url``/``length``，
            # 不加这个判断会把商品链接当成语音地址（服务层 `_display_msg_type`
            # 看到 audio_url 非空就判成语音，前端会渲染出播放器）。
            audio_url, audio_duration = _interpret_audio(decoded)
    if not text and not images:
        # 兜底：``content.text`` / 裸字符串正文（有的推送没有 custom 包一层），
        # 再兜底 ``custom.summary``（会话列表用的那句摘要）和顶层的 ``text``。
        plain = _decode_content(content_raw).strip()
        if not plain and isinstance(custom, dict):
            plain = str(custom.get('summary') or custom.get('degrade') or '').strip()
        if not plain:
            raw_text = item.get('text')
            plain = raw_text.strip() if isinstance(raw_text, str) else ''
        if plain:
            text = plain
            if not msg_type:
                msg_type = 'text'
    if not text and msg_type == 'voice':
        # 语音没有正文，给一句可读摘要：服务层拿 content 当「是不是消息」的闸门
        # （`services/chats.dispatch_im_message`），空串会被直接丢掉。
        text = _voice_summary(audio_duration)
    # 正文一律 strip 后再判空：纯空白（``'   '``）不算消息 —— HEAD 就是这么做的，
    # 否则 base64 形状的空文本帧会凭空多出一条 `content='   '` 的消息。
    text = text.strip()
    if not text and not images:
        return None
    if not msg_type:
        msg_type = 'image' if images else ('text' if text else 'system')

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
        'content': text,
        'images': images,
        'msg_type': msg_type,
        'image_url': images[0] if images else '',
        'audio_url': audio_url,
        'audio_duration': audio_duration,
        'item_id': item_id,
        'sender_nick': _sender_nick_of(item),
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

    return _send_frame(cid, toid, my_id, {'contentType': 1, 'text': {'text': text}})


def _send_frame(cid: str, toid: str, my_id: str, inner: Dict[str, Any]) -> Dict[str, Any]:
    """``sendByReceiverScope`` 的外层信封 —— 文本帧与图片帧只差 ``inner``。

    三个必须点见 `build_send_frame`；``inner`` 一律 ``base64(json)`` 进
    ``content.custom.data``。
    """
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


def build_send_image_frame(cid: str, toid: str, my_id: str,
                           image_url: str) -> Dict[str, Any]:
    """``/r/MessageSend/sendByReceiverScope`` 发图片帧（``contentType: 2``）。

    ⚠️ **这个形状是按 ``contentType=2`` 的解析反推的，真机未验证** —— 只保证外层信封
    与 `build_send_frame` 逐字段一致，差别只在 ``inner``：``contentType: 2`` +
    ``image.pics = [image_url]``。

    旧项目（``XianyuAutoAsync.py:11471-11487``）真实发图帧里 ``pics`` 放的是
    ``{'url','width','height','type': 0}`` 字典，且要求先上传到闲鱼 CDN 再发；这里按任务书
    放**裸 URL 字符串**（``_interpret_content`` 两种都认）。真机若被拒，优先怀疑这里缺
    ``width``/``height``。

    其余三个必须点与文本帧相同：``content.custom.data`` 是 ``base64(json)``；
    ``actualReceivers`` 要同时带对方和**自己**；``cid``/``toid`` 都带 ``@goofish``。
    """
    image_url = str(image_url or '').strip()
    if not image_url:
        raise ValueError('图片地址不能为空')
    return _send_frame(cid, toid, my_id, {'contentType': 2, 'image': {'pics': [image_url]}})


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
