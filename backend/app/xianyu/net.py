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

    ``proxy=None`` **单独用是不够的**：httpx 的 `Client.__init__` 里
    `_get_proxy_map(proxy, allow_env_proxies)` 在 `proxy is None` 且
    `trust_env=True`（默认）时，仍然会 `get_environment_proxies()` —— 也就是
    照样读 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`。所以必须同时
    `trust_env=False`，行为才真的只取决于本函数。

    **真机实测（2026-10-08，打包版）**：本机 env 有
    `HTTPS_PROXY=http://127.0.0.1:7897`，而该代理对 `h5api.m.goofish.com`
    握手就断：`ConnectError: EOF occurred in violation of protocol`，
    直连同一个域名 404（通）。于是「商品数据」后台拉取每轮每账号 5 件全失败，
    `item_stats_cache.error` 里只有一句 `接口不可用: MTOP 请求异常:` ——
    看起来像平台拒绝，实际是本机代理不支持这个域名。`agents/llm.py`
    早就因为同一类问题（审计 AI-7）关掉了 `trust_env`，这里补上，两条出网
    路径口径一致。
    """
    import httpx

    sanitize_no_proxy()
    kwargs = {
        'timeout': timeout(),
        'proxy': None,
        'trust_env': False,  # 不读 HTTP_PROXY/HTTPS_PROXY/NO_PROXY，避免被本机代理打挂
        'follow_redirects': follow_redirects,
    }
    if transport is not None:
        kwargs['transport'] = transport
    return httpx.AsyncClient(**kwargs)


def env_proxies() -> dict:
    """当前进程里设了哪些代理环境变量（httpx 会读、我们刻意不读）。

    只用于**把失败讲清楚**：连接不上网关时，如果本机设着代理，很可能就是它
    干的（见 `async_client` 的真机实测），得让用户知道去清掉或配直连。
    """
    keys = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY',
            'https_proxy', 'http_proxy', 'all_proxy')
    return {key: os.environ[key] for key in keys if os.environ.get(key)}
