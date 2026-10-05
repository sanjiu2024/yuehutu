"""Cookie 字符串的纯函数层。

这里刻意不依赖任何网络/DB，因为它是**判定登录是否真的成功**的唯一依据：
``x5sec`` 是风控挑战的通过凭证，``x5secdata`` / ``x5sectag`` / ``x5step`` 是
「挑战进行中」的标记。两者共存 = 挑战没走完，服务端会回
``FAIL_SYS_USER_VALIDATE``。

最容易写错的一步是「把挑战标记当成成功」：旧实现只要 Cookie 里有 x5secdata
就认为滑块过了，于是用户在页面上明明还卡着滑块，程序却报「验证成功」，之后
每次调用都撞风控。所以 ``has_x5sec`` 只认 ``x5sec`` 这一个名字。
"""

from __future__ import annotations

# 挑战进行中的标记。它们出现在 Cookie 里只说明「服务端要求你过滑块」，
# **不代表已经过了**。
CHALLENGE_MARKERS = ('x5secdata', 'x5sectag', 'x5step')

# 通过凭证。名字就是这一个，没有别名。
PASS_COOKIE = 'x5sec'


def parse_cookie_string(cookies: str) -> dict:
    """把 ``a=1; b=2`` 解析成 dict。

    容错优先：空段、没有 ``=`` 的段直接跳过，值里含 ``=`` 时只切第一个
    （``_m_h5_tk`` 的 token 里就带下划线和点，值里出现 ``=`` 也不奇怪）。
    旧项目 ``trans_cookies`` 在空串时抛 ValueError，导致调用方到处 try；
    这里返回空 dict，由调用方自己判断「没有 unb 就是没登录」。
    """
    result: dict[str, str] = {}
    if not cookies:
        return result
    for part in str(cookies).split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        name, value = part.split('=', 1)
        name = name.strip()
        if not name:
            continue
        result[name] = value.strip()
    return result


def cookie_string(cookies: dict) -> str:
    """把 dict 拼回 ``a=1; b=2``。空 dict 返回空串。"""
    return '; '.join(f'{k}={v}' for k, v in (cookies or {}).items())


def has_x5sec(cookies: str) -> bool:
    """是否持有风控通过凭证。

    **这是登录/验证成功的唯一判据**，名字大小写不敏感（平台偶尔回 ``X5Sec``）。
    """
    return PASS_COOKIE in {k.lower() for k in parse_cookie_string(cookies)}


def challenge_markers(cookies: str) -> list:
    """返回当前存在的挑战标记名（小写），用于诊断「卡在哪一步」。"""
    present = {k.lower() for k in parse_cookie_string(cookies)}
    return [m for m in CHALLENGE_MARKERS if m in present]


def drop_challenge_markers(cookies: str) -> str:
    """清掉挑战标记，**仅当已经拿到 x5sec 时**。

    为什么必须带这个条件：挑战还没完成时，``x5secdata`` 里带着惩罚页地址，
    人工验证流程要靠它决定打开哪个 URL。若此时就清掉，用户点「去验证」会
    落到首页而不是滑块页，永远过不去。只有确认拿到了通行凭证，这些标记
    才是纯粹的垃圾（留着反而会让下一次请求被判定为「带着旧挑战」）。
    """
    if not has_x5sec(cookies):
        return cookies
    parsed = parse_cookie_string(cookies)
    for marker in CHALLENGE_MARKERS:
        parsed.pop(marker, None)
    return cookie_string(parsed)


def is_blocked_cookie(cookies: str) -> bool:
    """Cookie 是否处于「挑战未完成」状态。

    判据是**有挑战标记但没有通过凭证**。已经拿到 x5sec 的 Cookie 即使还残留
    标记也不算被拦（正常流程里 drop_challenge_markers 会顺手清掉）。
    """
    if not cookies:
        return False
    return bool(challenge_markers(cookies)) and not has_x5sec(cookies)


def merge_cookies(base: str, extra) -> str:
    """把响应里的新 Cookie 合并进原串。

    ``extra`` 可以是 dict / httpx Cookies / 可迭代的 ``(name, value)``。
    同名**后到的覆盖先到的**——这是响应 Set-Cookie 的正常语义（服务端在
    续期 ``_m_h5_tk``）。注意与 captcha 里「同名首次优先」的区别：那条规则
    是浏览器 cookie jar 的跨域跳转场景，不是 HTTP 响应合并。
    """
    merged = parse_cookie_string(base)
    if extra is None:
        return cookie_string(merged)
    items = extra.items() if hasattr(extra, 'items') else extra
    for name, value in items:
        if name:
            merged[str(name)] = '' if value is None else str(value)
    return cookie_string(merged)
