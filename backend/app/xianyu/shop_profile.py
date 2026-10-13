"""店铺资料（官方「店铺管理」微应用）——只读详情 + 昵称 / 简介 / 头像三条写接口。

证据全部来自本地 bundle 副本（**零平台请求**）：
`logs/seller_discover/bundles/sub_account_main.js`（2,209,739 字符单行压缩 JS，
只有字符偏移，行号无意义）。

- 读：`mtop.alibaba.idle.shop.manage.detail`，偏移 1179664 附近
  `Zg = () => Qg({api:'mtop.alibaba.idle.shop.manage.detail', v:'1.0', data:{}, showErrorToast:true})
   .then(t => t.data)`；消费方 `hG({data})` 直接读
  `data.userAvatar / data.userNick / data.shopDesc / data.industry / data.alipayAccount /
   data.businessType / data.proShopValidTime`。
- 写（包装统一是 `Qg({api, v:'1.0', data, showErrorToast:true})`，调用方判 `result.success`，
  失败读 `result.data.message`）：
  - 昵称 @2186401 `rG = e => Qg({api:'…shop.manage.nick.update', v:'1.0', data:{userNick:e}})`
    调用点 @2191244：先 trim，再校验 `长度 <4 → 「昵称不能少于4个字符」`、
    `长度 >12 → 「昵称不能超过12个字符」`，成功 toast「昵称修改成功」。
  - 简介 @2186648 `oG = e => Qg({api:'…shop.manage.desc.update', v:'1.0', data:{shopDesc:e}})`
  - 头像 @2186150 `nG = e => Qg({api:'…shop.manage.avatar.update', v:'1.0', data:{userAvatar:e}})`
    调用点 @2192091：`e.fileList` 上传成功后取 `fullUrl || url || picUrl || fileName` 当 URL，
    成功 toast「头像修改成功」。

**一次真机写请求都没发过。** 平台对这几个接口是否还有别的必填项、简介的长度上限、
头像 URL 是否必须是自家图床，都还没有真机样例 ⇒ 只做官方 bundle 里能逐字对上的校验，
其余交给平台拒并把平台原话透出来。
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, Optional, Tuple

from .trade import _text, decode_payload

DETAIL_API = 'mtop.alibaba.idle.shop.manage.detail'
NICK_API = 'mtop.alibaba.idle.shop.manage.nick.update'
DESC_API = 'mtop.alibaba.idle.shop.manage.desc.update'
AVATAR_API = 'mtop.alibaba.idle.shop.manage.avatar.update'

DEFAULT_VERSION = '1.0'

#: 官方原文：`长度 <4 → 昵称不能少于4个字符`、`长度 >12 → 昵称不能超过12个字符`。
NICK_MIN = 4
NICK_MAX = 12

#: 官方 bundle 里只给了昵称的长度限制，简介没有 ⇒ 这里只要求非空，长度交给平台判。
DESC_MAX = None

WRITE_APIS: Dict[str, Dict[str, Any]] = {
    'nick': {
        'api': NICK_API,
        'data': "{'userNick': <trim 后的昵称>}",
        'evidence': 'bundle @2186401 定义 / @2191244 调用点（trim + 4~12 字校验）',
    },
    'desc': {
        'api': DESC_API,
        'data': "{'shopDesc': <简介>}",
        'evidence': 'bundle @2186648 定义',
    },
    'avatar': {
        'api': AVATAR_API,
        'data': "{'userAvatar': <图片 URL>}",
        'evidence': 'bundle @2186150 定义 / @2192091 调用点（取 fullUrl||url||picUrl||fileName）',
    },
}

READ_APIS: Tuple[str, ...] = (DETAIL_API,)

ALL_APIS: FrozenSet[str] = frozenset(
    [DETAIL_API] + [spec['api'] for spec in WRITE_APIS.values()]
)

ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)

#: 平台字段 → 我们的键。平台没给的**不出现**（不补 None 之外的假值）。
PROFILE_KEYS: Tuple[Tuple[str, str], ...] = (
    ('userNick', 'user_nick'),
    ('shopDesc', 'shop_desc'),
    ('userAvatar', 'user_avatar'),
    ('industry', 'industry'),
    ('alipayAccount', 'alipay_account'),
    ('businessType', 'business_type'),
    ('proShopValidTime', 'pro_shop_valid_time'),
)


class UnknownApi(ValueError):
    """协议层不认识的接口。"""


class WriteNotConfirmed(ValueError):
    """写接口没有拿到显式的 `confirm is True`。"""


def assert_write(api: str) -> None:
    if api not in ALL_APIS - set(READ_APIS):
        raise UnknownApi(f'{api} 不是店铺资料的写接口')


def assert_read(api: str) -> None:
    if api not in READ_APIS:
        raise UnknownApi(f'{api} 不是店铺资料的只读接口')


def _need_confirm(confirm: Any) -> None:
    """只有 `confirm is True` 才算点头（与其它写协议层同口径）。"""
    if confirm is not True:
        raise WriteNotConfirmed('店铺资料的写操作必须显式 confirm=True')


def code_of(raw: Any) -> str:
    """平台业务码（拿不到回空串）。"""
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return ''
    return _text(raw.get('code'))


def _required_text(label: str, value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError(f'{label}不能为空')
    return text


def nick_request(nick: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """改店铺昵称。官方校验：trim 后 4~12 个字符。"""
    _need_confirm(confirm)
    text = _required_text('昵称', nick)
    if len(text) < NICK_MIN:
        raise ValueError(f'昵称不能少于{NICK_MIN}个字符')
    if len(text) > NICK_MAX:
        raise ValueError(f'昵称不能超过{NICK_MAX}个字符')
    return NICK_API, {'userNick': text}


def desc_request(desc: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """改店铺简介。官方 bundle 没给长度上限 ⇒ 只要求非空。"""
    _need_confirm(confirm)
    text = _required_text('简介', desc)
    if DESC_MAX is not None and len(text) > DESC_MAX:
        raise ValueError(f'简介不能超过{DESC_MAX}个字符')
    return DESC_API, {'shopDesc': text}


def avatar_request(avatar_url: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """改店铺头像（传图片 URL，不传二进制）。"""
    _need_confirm(confirm)
    text = _required_text('头像图片地址', avatar_url)
    if not text.lower().startswith(('http://', 'https://')):
        raise ValueError('头像图片地址必须以 http:// 或 https:// 开头')
    return AVATAR_API, {'userAvatar': text}


_BUILDERS = {
    'nick': nick_request,
    'desc': desc_request,
    'avatar': avatar_request,
}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    builder = _BUILDERS.get(action)
    if builder is None:
        raise UnknownApi(f'未知动作：{action}（可用：{", ".join(ACTIONS)}）')
    return builder(**kwargs)


def payload_of(raw: Any) -> Dict[str, Any]:
    """取出店铺资料业务体。

    官方消费方直接读 `result.userNick`，而错误信息在 `result.data.message`
    （@2192091）⇒ 平台 `data` 可能是扁平的，也可能再嵌一层 `data`。两种都认：
    只要外层已经有任何一个已知资料键，就用外层。
    """
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return {}
    data = decode_payload(raw.get('data'))
    if not isinstance(data, dict):
        return {}
    known = {key for key, _ in PROFILE_KEYS}
    if known & set(data):
        return data
    inner = decode_payload(data.get('data'))
    if isinstance(inner, dict):
        return inner
    return data


def is_success(raw: Any) -> bool:
    """客户端层没报错、且平台层没说不成功。

    店铺管理族的平台信封是 `{success: bool, ...}`（不是 `{code:'success'}`），
    所以这里两种判据都看：有 `success` 布尔就以它为准，有 `code` 就只认 `success`。
    """
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return False
    if raw.get('success') is False:
        return False
    data = decode_payload(raw.get('data'))
    if isinstance(data, dict):
        if isinstance(data.get('success'), bool) and data.get('success') is False:
            return False
        code = code_of(data)
        if code and code.lower() != 'success':
            return False
    return True


def failure_reason(raw: Any) -> str:
    """失败原因：先看客户端层 message/ret，再看平台层 message / msg / data.message。"""
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return ''
    message = _text(raw.get('message'))
    if message:
        return message
    ret = raw.get('ret')
    if isinstance(ret, list) and ret:
        text = _text(ret[0])
        if text:
            return text
    data = decode_payload(raw.get('data'))
    if isinstance(data, dict):
        for key in ('message', 'msg'):
            text = _text(data.get(key))
            if text:
                return text
        inner = decode_payload(data.get('data'))
        if isinstance(inner, dict):
            for key in ('message', 'msg'):
                text = _text(inner.get(key))
                if text:
                    return text
    return ''


def parse_profile(raw: Any) -> Dict[str, Any]:
    """只读详情 → 我们的键。

    平台没给的键**不出现**（调用方补 `None`），也不做任何单位换算与猜测。
    `raw` 是完整业务体，留给前端/排障用。
    """
    payload = payload_of(raw)
    profile: Dict[str, Any] = {}
    for source_key, target_key in PROFILE_KEYS:
        value = payload.get(source_key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        profile[target_key] = value
    profile['raw'] = payload or None
    return profile


def parse_write(raw: Any, action: str) -> Dict[str, Any]:
    """写接口的返回：只回「平台有没有说不成功」，不回显提交内容。"""
    return {
        'action': action,
        'api': WRITE_APIS[action]['api'],
        'updated': is_success(raw),
        'platform_message': failure_reason(raw) or None,
    }
