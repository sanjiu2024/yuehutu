"""子账号管理（官方「账号管理」微应用）——岗位只读 + 新建 / 删除 / 改手机号 / 改岗位 / 改密码。

证据全部来自本地 bundle 副本（**零平台请求**）：
`logs/seller_discover/bundles/sub_account_main.js`（2,209,739 字符单行压缩 JS）。

- 包装统一是 `Cf({api, v:'1.0', data, showErrorToast:true})`；调用方判 `result.code === 'success'`
  （`result` = 平台 `data`），失败读 `result.msg`。
- 岗位列表 @1106840 附近 `sm = e => Cf({api:'…platform.user.role.list', v:'1.0', data:e||{}})`
  调用点 @1961808：`t.map(e => (e.selected && (n = e.roleId), {label: e.roleName, value: e.roleId}))`
  ⇒ 元素 `{roleId, roleName, selected}`。
- 新建 @1108041 `lm = e => Cf({api:'…common.sub.account.register', v:'1.0', data:e})`
  调用点 @1927428：`lm({nick, mobile, roleIds, customName, encryptedPassword, cryptoKey, ext})`
  （`ext` 只有扩展参数非空时才带，是 `JSON.stringify(扩展参数)`）。
- 删除 @1106840 `am = e => Cf({api:'…platform.sub.account.delete', v:'1.0', data:e})`
  调用点 @1960214：`am({subId})`，成功 toast「删除账号成功」；确认框文案
  「确定要删除账号"<昵称>"吗？」。
- 改手机号 @1105589 `rm = e => Cf({api:'…sub.account.mobilephone.update', …})`
  调用点 @1965179：`rm({subId, mobilePhone})`；官方校验 `/^1[3-9]\\d{9}$/`（@1587715 的 `dT`）。
- 改岗位 @1108447 `um = e => Cf({api:'…sub.account.role.update', …})`
  调用点 @1961808：`um({subId, roleIds})`，成功 toast「岗位更新成功」。
- 改密码 @1106434 `im = e => Cf({api:'…sub.account.password.update', …})`
  调用点 @1963111：`im({subId, encryptedPassword, cryptoKey})`；官方校验
  `/^(?=.*[A-Za-z])(?=.*\\d)[A-Za-z\\d]{8,15}$/`（@1587740 的 `fT`）。
- **密码是客户端加密后提交的**（@1587509 的 `uT`）：

      var uT = function (e, t) {
        var n = new globalThis.JSEncrypt, r = t.publicKey, o = t.cryptoKey;
        n.setPublicKey(r);
        return { encryptedPassword: n.encrypt(e), cryptoKey: o };
      };

  公钥来自 @1106128 `om = () => Cf({api:'…sub.account.sub.pk', v:'1.0', data:{}})`
  → 平台 `data` = `{code:'success', data:{publicKey, cryptoKey}}`，调用点用 `s.data` 取
  `{publicKey, cryptoKey}`。**我们在服务端做同一件事**（纯 Python 实现 RSAES-PKCS1-v1_5，
  不引新依赖）：密码明文只在本机进程内出现，发给平台的是密文 + 平台给的 `cryptoKey`。

**一次真机写请求都没发过**（本机账号对这几个接口大概率也没有权限：只读的
`user.role.list` / `quota.query` 真机回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`）。
"""
from __future__ import annotations

import base64
import binascii
import re
import secrets
from typing import Any, Dict, FrozenSet, Iterable, List, Tuple

from .trade import _text, decode_payload

INFO_LIST_API = 'mtop.alibaba.idle.seller.common.sub.account.info.list'
REGISTER_API = 'mtop.alibaba.idle.seller.platform.common.sub.account.register'
DELETE_API = 'mtop.alibaba.idle.seller.platform.sub.account.delete'
MOBILE_API = 'mtop.alibaba.idle.seller.platform.sub.account.mobilephone.update'
ROLE_API = 'mtop.alibaba.idle.seller.platform.sub.account.role.update'
PASSWORD_API = 'mtop.alibaba.idle.seller.platform.sub.account.password.update'
SUB_PK_API = 'mtop.alibaba.idle.seller.platform.sub.account.sub.pk'
ROLE_LIST_API = 'mtop.alibaba.idle.seller.platform.user.role.list'
PERMISSION_TREE_API = 'mtop.alibaba.idle.seller.platform.account.role.permission.tree'

DEFAULT_VERSION = '1.0'

#: 官方原文（@1587715）：`/^1[3-9]\d{9}$/` → 「请输入正确的11位手机号码」。
MOBILE_RE = re.compile(r'^1[3-9]\d{9}$')

#: 官方原文（@1587740）：`/^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,15}$/`
#: → 「密码必须为字母和数字组合，长度为8到15位」。
PASSWORD_RE = re.compile(r'^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,15}$')

#: 官方 bundle 没给昵称上限（注册表单只标了必填）⇒ 只要求非空，长度交给平台判。
NICK_MAX = None

MAX_ROLE_IDS = 50

WRITE_APIS: Dict[str, Dict[str, Any]] = {
    'create': {
        'api': REGISTER_API,
        'data': "{'nick','mobile','roleIds','customName','encryptedPassword','cryptoKey','ext'?}",
        'evidence': 'bundle @1108041 定义 / @1927428 调用点',
    },
    'delete': {
        'api': DELETE_API,
        'data': "{'subId': <子账号 id>}",
        'evidence': 'bundle @1106840 定义 / @1960214 调用点',
    },
    'update_mobile': {
        'api': MOBILE_API,
        'data': "{'subId','mobilePhone'}",
        'evidence': 'bundle @1105589 定义 / @1965179 调用点',
    },
    'update_roles': {
        'api': ROLE_API,
        'data': "{'subId','roleIds'}",
        'evidence': 'bundle @1108447 定义 / @1961808 调用点',
    },
    'update_password': {
        'api': PASSWORD_API,
        'data': "{'subId','encryptedPassword','cryptoKey'}",
        'evidence': 'bundle @1106434 定义 / @1963111 调用点 + @1587509 的 JSEncrypt 加密',
    },
}

READ_APIS: Tuple[str, ...] = (ROLE_LIST_API,)

WRITE_API_SET: FrozenSet[str] = frozenset(spec['api'] for spec in WRITE_APIS.values())

ALL_APIS: FrozenSet[str] = frozenset(
    [INFO_LIST_API, SUB_PK_API, PERMISSION_TREE_API]
    + list(READ_APIS)
    + list(WRITE_API_SET)
)

ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)


class UnknownApi(ValueError):
    """协议层不认识的接口。"""


class WriteNotConfirmed(ValueError):
    """写接口没有拿到显式的 `confirm is True`。"""


class EncryptionError(ValueError):
    """公钥解不开 / 加密失败。"""


def assert_write(api: str) -> None:
    """只放行**写接口**（`WRITE_APIS` 里各动作的 api）。

    不能用 `ALL_APIS - READ_APIS`：`INFO_LIST_API`/`SUB_PK_API`/`PERMISSION_TREE_API`
    既不在只读白名单里、也不是写接口，那种写法会把它们一起放行。
    """
    if api not in WRITE_API_SET:
        raise UnknownApi(f'{api} 不是子账号管理的写接口')


def assert_read(api: str) -> None:
    if api not in READ_APIS:
        raise UnknownApi(f'{api} 不是子账号管理的只读接口')


def _need_confirm(confirm: Any) -> None:
    if confirm is not True:
        raise WriteNotConfirmed('子账号管理的写操作必须显式 confirm=True')


def _required_text(label: str, value: Any) -> str:
    text = _text(value).strip()
    if not text:
        raise ValueError(f'{label}不能为空')
    return text


def normalize_mobile(value: Any) -> str:
    """官方就是拿原始输入去 match 的，我们多做一步去掉空格与横线（用户常这么粘）。"""
    text = _text(value).replace(' ', '').replace('-', '')
    if not MOBILE_RE.match(text):
        raise ValueError('请输入正确的11位手机号码')
    return text


def check_password(value: Any) -> str:
    text = _text(value)
    if not PASSWORD_RE.match(text):
        raise ValueError('密码必须为字母和数字组合，长度为8到15位')
    return text


def normalize_role_ids(value: Any) -> List[int]:
    if isinstance(value, str):
        items: Iterable[Any] = [part for part in value.replace('，', ',').split(',') if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = value
    elif value is None:
        raise ValueError('岗位不能为空')
    else:
        items = [value]
    role_ids: List[int] = []
    for raw in items:
        text = _text(raw).strip()
        if not text:
            continue
        try:
            number = int(text)
        except (TypeError, ValueError):
            raise ValueError(f'岗位 id 必须是整数：{text}')
        if number <= 0:
            raise ValueError(f'岗位 id 必须大于 0：{text}')
        if number not in role_ids:
            role_ids.append(number)
    if not role_ids:
        raise ValueError('岗位不能为空')
    if len(role_ids) > MAX_ROLE_IDS:
        raise ValueError(f'一次最多提交 {MAX_ROLE_IDS} 个岗位')
    return role_ids


def normalize_sub_id(value: Any) -> str:
    return _required_text('子账号 id', value)


# --------------------------------------------------------------------------- RSA

def _der_read(data: bytes, offset: int) -> Tuple[int, bytes, int]:
    """读一个 DER TLV，返回 (tag, value, 下一个偏移)。只支持定长（不支持 indefinite）。"""
    if offset + 2 > len(data):
        raise EncryptionError('公钥 DER 太短')
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + count > len(data):
            raise EncryptionError('公钥 DER 长度字段不合法')
        length = int.from_bytes(data[offset:offset + count], 'big')
        offset += count
    if offset + length > len(data):
        raise EncryptionError('公钥 DER 越界')
    return tag, data[offset:offset + length], offset + length


def parse_public_key(pem: Any) -> Tuple[int, int]:
    """把 PEM / 裸 base64 的 RSA 公钥解成 `(modulus, exponent)`。

    支持 `SubjectPublicKeyInfo`（`-----BEGIN PUBLIC KEY-----`）与裸
    `RSAPublicKey`（`-----BEGIN RSA PUBLIC KEY-----`）两种。
    """
    text = _text(pem).strip()
    if not text:
        raise EncryptionError('公钥为空')
    body = re.sub(r'-----[^-]+-----', '', text)
    body = ''.join(body.split())
    if not body:
        raise EncryptionError('公钥内容为空')
    try:
        der = base64.b64decode(body, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise EncryptionError(f'公钥不是合法的 base64：{exc}')

    tag, value, _ = _der_read(der, 0)
    if tag != 0x30:
        raise EncryptionError('公钥不是 DER SEQUENCE')
    # SPKI = SEQUENCE { AlgorithmIdentifier(SEQUENCE), BIT STRING }
    # 裸 RSAPublicKey = SEQUENCE { INTEGER modulus, INTEGER exponent }
    first_tag, _, first_end = _der_read(value, 0)
    if first_tag == 0x30:
        bit_tag, bit_value, _ = _der_read(value, first_end)
        if bit_tag != 0x03:
            raise EncryptionError('公钥里没有 BIT STRING')
        if not bit_value or bit_value[0] != 0:
            raise EncryptionError('公钥 BIT STRING 有未用位')
        seq_tag, content, _ = _der_read(bit_value[1:], 0)
        if seq_tag != 0x30:
            raise EncryptionError('公钥里没有 RSAPublicKey SEQUENCE')
    else:
        content = value
    mod_tag, modulus_bytes, offset = _der_read(content, 0)
    exp_tag, exponent_bytes, _ = _der_read(content, offset)
    if mod_tag != 0x02 or exp_tag != 0x02:
        raise EncryptionError('公钥的 modulus/exponent 不是 INTEGER')
    modulus = int.from_bytes(modulus_bytes, 'big')
    exponent = int.from_bytes(exponent_bytes, 'big')
    if modulus <= 0 or exponent <= 0:
        raise EncryptionError('公钥的 modulus/exponent 不合法')
    return modulus, exponent


def encrypt_password(password: Any, public_key: Any) -> str:
    """JSEncrypt 的等价实现：RSAES-PKCS1-v1_5 + base64。

    纯 Python（`pow()` 就是模幂），不引第三方库。随机填充用 `secrets`。
    """
    plain = _text(password).encode('utf-8')
    modulus, exponent = parse_public_key(public_key)
    size = (modulus.bit_length() + 7) // 8
    if size < 11 + len(plain):
        raise EncryptionError('公钥太短，装不下这个密码')
    padding_length = size - 3 - len(plain)
    padding = bytearray()
    while len(padding) < padding_length:
        chunk = secrets.token_bytes(padding_length - len(padding))
        padding.extend(b for b in chunk if b != 0)
    block = b'\x00\x02' + bytes(padding[:padding_length]) + b'\x00' + plain
    if len(block) != size:
        raise EncryptionError('PKCS#1 v1.5 填充长度不对')
    cipher = pow(int.from_bytes(block, 'big'), exponent, modulus)
    return base64.b64encode(cipher.to_bytes(size, 'big')).decode('ascii')


# ----------------------------------------------------------------------- 请求构造

def create_request(nick: Any, mobile: Any, role_ids: Any, encrypted_password: Any,
                   crypto_key: Any, custom_name: Any = None, ext: Any = None,
                   *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """新建子账号。密码必须已经加密（`encrypted_password` + `crypto_key`）。"""
    _need_confirm(confirm)
    text = _required_text('昵称', nick)
    if NICK_MAX is not None and len(text) > NICK_MAX:
        raise ValueError(f'昵称不能超过{NICK_MAX}个字符')
    data: Dict[str, Any] = {
        'nick': text,
        'mobile': normalize_mobile(mobile),
        'roleIds': normalize_role_ids(role_ids),
        'encryptedPassword': _required_text('加密后的密码', encrypted_password),
        'cryptoKey': _required_text('加密用的 cryptoKey', crypto_key),
    }
    custom = _text(custom_name).strip()
    if custom:
        data['customName'] = custom
    extension = _text(ext).strip()
    if extension:
        data['ext'] = extension
    return REGISTER_API, data


def delete_request(sub_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    _need_confirm(confirm)
    return DELETE_API, {'subId': normalize_sub_id(sub_id)}


def mobile_request(sub_id: Any, mobile: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    _need_confirm(confirm)
    return MOBILE_API, {'subId': normalize_sub_id(sub_id), 'mobilePhone': normalize_mobile(mobile)}


def roles_request(sub_id: Any, role_ids: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    _need_confirm(confirm)
    return ROLE_API, {'subId': normalize_sub_id(sub_id), 'roleIds': normalize_role_ids(role_ids)}


def password_request(sub_id: Any, encrypted_password: Any, crypto_key: Any,
                     *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    _need_confirm(confirm)
    return PASSWORD_API, {
        'subId': normalize_sub_id(sub_id),
        'encryptedPassword': _required_text('加密后的密码', encrypted_password),
        'cryptoKey': _required_text('加密用的 cryptoKey', crypto_key),
    }


_BUILDERS = {
    'create': create_request,
    'delete': delete_request,
    'update_mobile': mobile_request,
    'update_roles': roles_request,
    'update_password': password_request,
}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    builder = _BUILDERS.get(action)
    if builder is None:
        raise UnknownApi(f'未知动作：{action}（可用：{", ".join(ACTIONS)}）')
    return builder(**kwargs)


def roles_request_of() -> Tuple[str, Dict[str, Any]]:
    """岗位列表（只读）。官方传 `{}`（`data:e||{}`）。"""
    return ROLE_LIST_API, {}


def public_key_request() -> Tuple[str, Dict[str, Any]]:
    """取密码加密用的公钥（只读、无参数）。"""
    return SUB_PK_API, {}


# ------------------------------------------------------------------------- 解析

def envelope(raw: Any) -> Dict[str, Any]:
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return {}
    data = decode_payload(raw.get('data'))
    return data if isinstance(data, dict) else {}


def business(raw: Any) -> Dict[str, Any]:
    """平台业务体：`data.data` 优先（这几个接口都是标准 `{code,msg,data}` 信封）。"""
    data = envelope(raw)
    inner = decode_payload(data.get('data'))
    if isinstance(inner, dict):
        return inner
    return data


def code_of(raw: Any) -> str:
    return _text(envelope(raw).get('code'))


def message_of(raw: Any) -> str:
    data = envelope(raw)
    text = _text(data.get('msg')) or _text(data.get('message'))
    if text:
        return text
    raw = decode_payload(raw)
    if isinstance(raw, dict):
        text = _text(raw.get('message'))
        if text:
            return text
        ret = raw.get('ret')
        if isinstance(ret, list) and ret:
            return _text(ret[0])
    return ''


def is_success(raw: Any) -> bool:
    """客户端没报错、且平台 `code == 'success'`。

    没有 `code` 键时以客户端结论为准（官方对 `sub.pk` 就没读 code）。
    """
    raw = decode_payload(raw)
    if not isinstance(raw, dict):
        return False
    if raw.get('success') is False:
        return False
    code = code_of(raw)
    if code:
        return code.lower() == 'success'
    return bool(envelope(raw)) or raw.get('success') is True


def failure_reason(raw: Any) -> str:
    return message_of(raw)


def parse_roles(raw: Any) -> Dict[str, Any]:
    """岗位列表：`[{roleId, roleName, selected}]`（官方调用点 @1961808）。

    平台给的不是数组 ⇒ `roles=None` + 调用方加 warning（**不造空数组**）。
    """
    payload = business(raw)
    items = payload.get('roleList') or payload.get('roles') or payload.get('list')
    if items is None and isinstance(payload, list):
        items = payload
    if not isinstance(items, list):
        return {'roles': None, 'total': None}
    roles: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role_id = item.get('roleId')
        if role_id is None:
            role_id = item.get('id')
        if role_id is None:
            continue
        roles.append({
            'role_id': role_id,
            'role_name': _text(item.get('roleName') or item.get('name')) or None,
            'selected': item.get('selected') if isinstance(item.get('selected'), bool) else None,
        })
    return {'roles': roles, 'total': len(roles)}


def parse_public_key_response(raw: Any) -> Dict[str, Any]:
    """`sub.account.sub.pk` → `{public_key, crypto_key}`（拿不到给 None，不造）。"""
    payload = business(raw)
    public_key = _text(payload.get('publicKey') or payload.get('public_key')).strip() or None
    crypto_key = _text(payload.get('cryptoKey') or payload.get('crypto_key')).strip() or None
    return {'public_key': public_key, 'crypto_key': crypto_key}


def parse_write(raw: Any, action: str) -> Dict[str, Any]:
    return {
        'action': action,
        'api': WRITE_APIS[action]['api'],
        'saved': is_success(raw),
        'platform_message': failure_reason(raw) or None,
    }
