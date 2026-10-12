"""MTOP 调用底座：签名、参数拼装、响应判定。

MTOP 是阿里的网关协议，三条容易踩的坑都在这里处理：

1. **签名**：``sign = md5(f"{token}&{t}&{appKey}&{data}")``，其中 token 是
   ``_m_h5_tk`` 里**下划线之前**的那段。``t`` 与签名里的 ``t`` 必须是同一个
   毫秒时间戳，重新生成就会 sign 不匹配。
2. **成功判定**：只能判 ``'SUCCESS' in ret``，**绝不能判「不是 FAIL 就算成功」**。
   接口名写错时服务端返回 ``FAIL_SYS_API_NOT_FOUND`` 之类，宽松判定会把它当成
   成功，然后上层拿着空数据一路往下跑，最后报一个完全无关的错误。
3. **Cookie 续期**：响应 Set-Cookie 里的 ``_m_h5_tk`` / ``_m_h5_tk_enc`` 必须
   合并回 Cookie 串，否则下一次调用用的还是旧 token，签名直接失效。
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from . import cookies as cookie_utils

# 网关入口。h5api.m.goofish.com 是闲鱼 H5 侧的正式域名。
API_BASE = 'https://h5api.m.goofish.com/h5/'
# 图片上传走独立域名。
UPLOAD_URL = 'https://stream-upload.goofish.com/api/upload.api'

APP_KEY = '34839810'
UPLOAD_APPKEY = 'fleamarket'
JSV_VERSION = '2.7.2'
DEFAULT_SPM_CNT = 'a21ybx.publish.0.0'
DEFAULT_TIMEOUT = 25

# 只有这两个 Cookie 会在响应里续期，也只需要合并这两个。
TOKEN_COOKIES = ('_m_h5_tk', '_m_h5_tk_enc')

# 「签名令牌不对」这一类失败。**它们是可以自愈的**：失败响应本身就会
# Set-Cookie 下发新的 ``_m_h5_tk``，带上它重签一次即可 —— 与
# ``FAIL_SYS_SESSION_EXPIRED``（cookie2/unb 真死，只能重新扫码）是两回事。
# 真机取证：库里 Cookie 没有 ``_m_h5_tk`` 时第一次调用必然返回
# ``FAIL_SYS_TOKEN_EMPTY::令牌为空``，同一次响应就带回
# ``_m_h5_tk=369401f2…``，第二次调用即 SUCCESS。
TOKEN_ERROR_MARKERS = (
    'FAIL_SYS_TOKEN_EMPTY',
    'FAIL_SYS_TOKEN_EXPIRED',
    'FAIL_SYS_TOKEN_EXOIRED',  # 平台自己的拼写错误，别以为是笔误删掉
    '令牌为空',
    '令牌过期',
)


def is_token_error(message_or_ret: Any) -> bool:
    """判断失败信息是不是「签名令牌不对」这一类（可带新令牌重试）。"""
    if isinstance(message_or_ret, (list, tuple, set)):
        text = ' '.join(str(item) for item in message_or_ret)
    else:
        text = str(message_or_ret or '')
    return any(marker in text for marker in TOKEN_ERROR_MARKERS)


def generate_sign(t: str, token: str, data: str) -> str:
    """生成 MTOP 签名。

    ``data`` 必须是**实际发出去的那个 JSON 字符串**（不能是 dict 再序列化一次，
    键顺序变了签名就对不上）。
    """
    raw = f'{token}&{t}&{APP_KEY}&{data}'
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def token_from_cookies(cookies_str: str) -> str:
    """从 Cookie 串里取签名用的 token。

    没有 ``_m_h5_tk`` 时返回空串——首次调用本来就会失败一次拿不到 token，
    这是 MTOP 的正常握手（失败响应里会带回 ``_m_h5_tk``）。
    """
    value = cookie_utils.parse_cookie_string(cookies_str).get('_m_h5_tk', '')
    return value.split('_')[0] if value else ''


def call_params(api: str, version: str, data: str, token: str, t: str,
                spm_cnt: str = DEFAULT_SPM_CNT,
                value_type: Optional[str] = None) -> Dict[str, str]:
    """拼好一份完整的 MTOP query 参数（含 sign）。

    `value_type` 只有极少数接口需要（官方 bundle 里 `mtop.idle.trade.order.detail.
    privacy.address` 明确写了 `valueType:'string'`），**默认 None 表示一个字节都不加** ——
    绝不能给所有请求都塞上，那会改变其它接口的响应形状。
    """
    params = {
        'jsv': JSV_VERSION,
        'appKey': APP_KEY,
        't': t,
        'sign': generate_sign(t, token, data),
        'v': version,
        'type': 'originaljson',
        'accountSite': 'xianyu',
        'dataType': 'json',
        'timeout': str(DEFAULT_TIMEOUT * 1000),
        'api': api,
        'sessionOption': 'AutoLoginOnly',
        'spm_cnt': spm_cnt,
    }
    if value_type:
        params['valueType'] = str(value_type)
    return params


def api_url(api: str, version: str) -> str:
    return f'{API_BASE}{api}/{version}/'


def merge_token_cookies(cookies_str: str, response) -> str:
    """把响应里续期的 ``_m_h5_tk`` / ``_m_h5_tk_enc`` 合并回 Cookie 串。"""
    updates = {}
    for name in TOKEN_COOKIES:
        value = None
        try:
            value = response.cookies.get(name)
        except Exception:
            value = None
        if value:
            updates[name] = value
    if not updates:
        return cookies_str
    return cookie_utils.merge_cookies(cookies_str, updates)


def parse_ret(payload: Any) -> Tuple[bool, str, list]:
    """解析 MTOP 响应，返回 ``(是否成功, 消息, ret 列表)``。

    成功 = ``ret`` 里**任意一项包含 'SUCCESS'``。判「非 FAIL」是错的：接口名
    写错时返回的不是 FAIL 而是「接口不存在」，会被误判成成功。
    """
    if not isinstance(payload, dict):
        return False, f'MTOP 响应不是 JSON 对象: {type(payload).__name__}', []
    ret = payload.get('ret') or []
    if isinstance(ret, str):
        ret = [ret]
    ret_list = [str(item) for item in ret]
    message = '; '.join(ret_list) or 'MTOP 未返回 ret'
    success = any('SUCCESS' in item for item in ret_list)
    return success, message, ret_list


def encode_data(data: Any) -> str:
    """把 data 参数序列化成发送用的字符串。

    ``separators`` 固定成紧凑写法：MTOP 的签名对**字符串逐字节**敏感，多一个
    空格签名就变了。``ensure_ascii=False`` 让中文保持原样，避免转义后长度对不上
    服务端预期。
    """
    if isinstance(data, str):
        return data
    return json.dumps(data or {}, ensure_ascii=False, separators=(',', ':'))


def generate_mid() -> str:
    """IM 请求的 mid。格式沿用旧项目实测值：随机数 + 毫秒时间戳 + ' 0'。"""
    return f'{int(1000 * random.random())}{int(time.time() * 1000)} 0'


def generate_uuid() -> str:
    """IM 消息 uuid。负数开头 + 毫秒时间戳，与旧项目实测一致。"""
    return f'-{int(time.time() * 1000)}1'


def generate_device_id(user_id: str) -> str:
    """IM 设备 ID：标准 36 位 UUID + ``-<user_id>``。

    形状照旧项目实测值（标准 UUID 而不是自己拼十六进制：自己拼的分段长度不对，
    看着像 UUID 但位数不对，服务端可能校验）。

    **必须是确定性的**（这里用 uuid5 而不是 uuid4），原因有二：

    1. 服务端会拿 ``/reg`` 帧里的 ``did`` 与**取 IM token 时用的 deviceId**
       比对，不一致直接回
       ``401 {"code": "4000001", "reason": "device id or appkey is not equal",
       "developerMessage": "#reg error : refers to auth"}``；
    2. token 会缓存在库里、跨进程复用，只有 device id 稳定，重启后重连才不会
       突然「设备对不上」。
    """
    return f'{uuid.uuid5(uuid.NAMESPACE_URL, f"xywb-im-device:{user_id}")}-{user_id}'
