"""本地对称加密：让 `providers.api_key` 不再以明文落进 SQLite。

**为什么需要它**：`providers` 表是 API Key 的唯一来源，但审计实测
`SELECT id, name, api_key FROM providers;` 直接得到 `sk-stub-SECRET-abcdef123456` ——
只要用户把 `data/app.db` 拷给别人（或备份进网盘），Key 就泄漏了。原来的
`api/providers.py::_public()` 只在**出站响应**里做掩码，落盘是真身。

**为什么不引 `cryptography`**：项目规则是「只依赖 requirements 里已有的」，而
Python 3.10 标准库没有 AES/ChaCha。所以这里用标准库手搓一个 CTM
（counter + HMAC）流加密：

    keystream_i = HMAC-SHA256(K, b'stream' || nonce || i)
    ciphertext  = plaintext XOR keystream
    tag         = HMAC-SHA256(K, b'tag' || nonce || ciphertext)[:16]

`HMAC-SHA256` 作为 PRF 构造流密码是标准做法（HKDF/CTR 同源），并且**先加密后认证**
（encrypt-then-MAC），篡改密文会在 `decrypt()` 时被 `hmac.compare_digest` 抓到。

**密钥派生**：从既有 `core/security.py::local_secret()`（`data/local_secret.txt`，
进程间共享的本地密钥）派生，**不修改 security.py**。本机单用户场景下，「密钥与密文
在同一台机器的数据目录里」是明确接受的权衡：目标是「Key 不以明文出现在库/备份里」，
不是「防御拿到整台机器的攻击者」—— 后者需要用户主密码，不在本项目范围内。

**兼容明文**：`decrypt()` 对不带前缀的值原样返回。这既让历史明文行不会炸，
也让「迁移」变成一次幂等的原地加密回写（见 `migrate_plaintext()`）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any, Iterable, List, Optional, Tuple

from loguru import logger

from . import db, security

# 前缀带版本号：以后换算法可以靠它区分旧值。
PREFIX = 'enc:v1:'

NONCE_BYTES = 12
TAG_BYTES = 16

# 派生密钥的上下文串，避免和 local_secret 的其它用途撞上
_KEY_CONTEXT = b'xywb:provider-api-key:v1:'

_cached_key: Optional[bytes] = None
_cached_secret: str = ''


class CryptoError(ValueError):
    """密文损坏 / 密钥变了 / 认证标签对不上。"""


def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(_KEY_CONTEXT + str(secret).encode('utf-8')).digest()


def _key() -> bytes:
    """派生加密密钥。缓存住，避免每次调用都读盘。"""
    global _cached_key, _cached_secret
    secret = security.local_secret()
    if _cached_key is None or _cached_secret != secret:
        _cached_key = _derive_key(secret)
        _cached_secret = secret
    return _cached_key


def reset_cache() -> None:
    """测试换数据目录/换 local_secret 后用。"""
    global _cached_key, _cached_secret
    _cached_key = None
    _cached_secret = ''


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, b'stream' + nonce + counter.to_bytes(4, 'big'), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: Any) -> str:
    """加密。空值原样返回空串；**已经是密文的值原样返回**（幂等，防二次加密）。"""
    text = '' if plaintext is None else str(plaintext)
    if not text:
        return ''
    if is_encrypted(text):
        return text
    key = _key()
    nonce = secrets.token_bytes(NONCE_BYTES)
    raw = text.encode('utf-8')
    stream = _keystream(key, nonce, len(raw))
    body = bytes(a ^ b for a, b in zip(raw, stream))
    tag = hmac.new(key, b'tag' + nonce + body, hashlib.sha256).digest()[:TAG_BYTES]
    return PREFIX + base64.urlsafe_b64encode(nonce + body + tag).decode('ascii')


def decrypt(value: Any) -> str:
    """解密。**明文原样返回**（历史数据兼容）；密文损坏抛 `CryptoError`。"""
    text = '' if value is None else str(value)
    if not text or not is_encrypted(text):
        return text
    payload = text[len(PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(payload.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CryptoError(f'密文不是合法的 base64：{exc}') from exc
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise CryptoError('密文长度不足，无法包含 nonce 与认证标签')
    nonce = blob[:NONCE_BYTES]
    body = blob[NONCE_BYTES:-TAG_BYTES]
    tag = blob[-TAG_BYTES:]
    key = _key()
    expected = hmac.new(key, b'tag' + nonce + body, hashlib.sha256).digest()[:TAG_BYTES]
    if not hmac.compare_digest(tag, expected):
        # 密钥被换过（local_secret.txt 被删/重生成）或密文被改过，两种情况都无法解密
        raise CryptoError('API Key 解密失败（认证标签不匹配）：本地密钥可能已变更，请在「AI 设置」里重新填一次 Key')
    stream = _keystream(key, nonce, len(body))
    return bytes(a ^ b for a, b in zip(body, stream)).decode('utf-8')


def mask(value: Any) -> str:
    """掩码。传明文或密文都可以 —— 内部先解密，绝不把密文本身露出去。"""
    try:
        text = decrypt(value)
    except CryptoError:
        return '<无法解密>'
    if not text:
        return ''
    if len(text) <= 8:
        return '*' * len(text)
    return f'{text[:4]}{"*" * 8}{text[-4:]}'


def migrate_plaintext(
    table: str,
    column: str,
    *,
    id_column: str = 'id',
    where: str = '',
    params: Iterable[Any] = (),
) -> int:
    """把 `table.column` 里所有**明文**值就地加密回写，返回迁移行数。

    幂等：已经是 `enc:v1:` 的行不会被再加密（`encrypt()` 会跳过）。
    表名/列名是**代码里写死的常量**，不是用户输入，所以这里拼 SQL 是安全的；
    仍然做一次白名单式校验，避免以后被误用成注入点。
    """
    for ident in (table, column, id_column):
        if not ident.replace('_', '').isalnum():
            raise ValueError(f'非法的表/列名: {ident!r}')

    sql = f'SELECT {id_column}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != \'\''
    if where:
        sql += f' AND ({where})'
    moved = 0
    for row in list(db.query(sql, list(params))):
        raw = row[column]
        if is_encrypted(raw):
            continue
        db.execute(
            f'UPDATE {table} SET {column} = ? WHERE {id_column} = ?',
            (encrypt(raw), row[id_column]),
        )
        moved += 1
    if moved:
        logger.info('已把 {}.{} 的 {} 行明文加密回写', table, column, moved)
    return moved


def encrypt_many(values: Iterable[Any]) -> List[str]:
    return [encrypt(v) for v in values]


def looks_plaintext_key(value: Any) -> bool:
    """判断一个非空值是否还是明文（迁移与测试用）。"""
    text = '' if value is None else str(value)
    return bool(text) and not is_encrypted(text)


__all__ = [
    'PREFIX',
    'CryptoError',
    'encrypt',
    'decrypt',
    'mask',
    'is_encrypted',
    'looks_plaintext_key',
    'migrate_plaintext',
    'encrypt_many',
    'reset_cache',
]
