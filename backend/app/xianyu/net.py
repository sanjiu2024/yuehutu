"""出站网络的公共处理：代理环境消毒 + 统一的 httpx 客户端构造。

**为什么单独一个模块**：``_sanitize_no_proxy()`` 必须在**任何** httpx 客户端
构造之前执行。放在 ``login.py`` 里时，只要有人先 import 了 ``client.py`` 并在
模块级构造客户端，消毒就晚了。抽到这里，谁先 import 谁先消毒，是幂等的。
"""

from __future__ import annotations

import os
import threading

from loguru import logger

# 代理相关的环境变量。httpx 会读它们，而 NO_PROXY 的语法各家实现不一致。
PROXY_ENV_KEYS = ('NO_PROXY', 'no_proxy')

_sanitize_lock = threading.Lock()
_sanitized = False


def sanitize_no_proxy() -> list:
    """修掉 NO_PROXY 里 httpx 解析不了的条目。

    **这是旧项目扫码登录整体瘫痪的真因**：本机 NO_PROXY 含 ``[::1]``，
    httpx 0.28 在**构造阶段**（不是请求阶段）就把它当成 ``host:port`` 解析，
    于是 ``httpx.Client()`` 直接抛::

        InvalidURL: Invalid port: ':1]'

    注意异常发生在构造时，所以「try 住请求再重试」的写法根本救不回来 ——
    客户端压根建不出来。旧项目因此表现为「点扫码没反应」。

    修法是**把方括号去掉**（``[::1]`` → ``::1``），而不是整条丢弃：IPv6 的
    ``::1`` 是本地回环，本来就该绕过代理，丢掉会让我们自己的 127.0.0.1 请求
    走上代理。返回值是被改写的变量名列表，方便日志与测试断言。
    """
    global _sanitized
    with _sanitize_lock:
        changed = []
        for key in PROXY_ENV_KEYS:
            value = os.environ.get(key)
            if not value or '[' not in value:
                continue
            parts = []
            for item in value.split(','):
                item = item.strip()
                if not item:
                    continue
                # 只处理形如 [::1] 或 [::1]:8080 的条目
                if item.startswith('['):
                    end = item.find(']')
                    if end > 0:
                        host = item[1:end]
                        rest = item[end + 1:]
                        item = f'{host}{rest}'
                parts.append(item)
            new_value = ','.join(parts)
            if new_value != value:
                os.environ[key] = new_value
                changed.append(key)
                logger.warning(f'NO_PROXY 含 httpx 无法解析的方括号条目，已消毒: {key}')
        _sanitized = True
        return changed


def sanitized() -> bool:
    return _sanitized


# 导入即消毒。模块级副作用是刻意的：任何 import 这个包的网络模块都会先过这一关。
sanitize_no_proxy()


def timeout(read: float = 60.0, connect: float = 30.0) -> 'object':
    """统一的超时配置。

    read 给到 60s：闲鱼的接口偶尔会慢到 20s+，而 MTOP 的 timeout 参数
    （20s）只约束服务端，客户端的 read 必须比它宽，否则会先掐断自己。
    """
    import httpx

    return httpx.Timeout(connect=connect, read=read, write=30.0, pool=60.0)


def async_client(transport=None, follow_redirects: bool = True):
    """建一个带正确代理设置的 AsyncClient。

    ``proxy=None`` 是刻意的：我们不使用系统代理。旧项目让 httpx 从环境变量
    推断代理，结果 ``NO_PROXY`` 一坏全盘皆输；显式关掉后，行为只取决于
    本函数，不会再被外部环境改动。
    """
    import httpx

    sanitize_no_proxy()
    kwargs = {
        'timeout': timeout(),
        'proxy': None,
        'follow_redirects': follow_redirects,
    }
    if transport is not None:
        kwargs['transport'] = transport
    return httpx.AsyncClient(**kwargs)
