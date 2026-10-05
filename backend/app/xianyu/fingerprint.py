"""浏览器指纹（UA / Client Hints）的唯一来源。

**为什么必须只有一处**：阿里风控会把 ``x5sec`` 通行 Cookie 与验证时浏览器的
User-Agent、``sec-ch-ua`` 指纹**强绑定**。同一个账号如果「验证时用 A 浏览器、
出站请求声明 B 指纹」，平台会认为凭证被搬运，返回 ``RRXVV_ERROR`` /
``FAIL_SYS_USER_VALIDATE`` —— 表现为「滑块明明过了却一直用不了」。
旧项目为此吃过 issue #62。所以：所有出站请求与所有浏览器都从这里取值。

**绝不自造 UA**：下面这组值不是猜的，是 2026-10-04 在本机正式版 Chrome 上
实测出来的。Chrome 每个大版本的 GREASE 品牌串都不一样（历史上出现过
``Not(A:Brand`` / ``Not)A;Brand`` / ``Not;A=Brand``），随手改一个字就是
「UA 说 148、client hints 说 133」的自相矛盾指纹。升级 Chrome 时必须重新实测，
不能凭印象改。
"""

from __future__ import annotations

# 与本机正式版 Chrome 对齐的大版本。升级 Chrome 后必须重新实测并同步此值。
CHROME_MAJOR = '148'

# 标准 Windows Chrome UA（实测原样，不要格式化改写）
CHROME_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
)

# GREASE 品牌与版本：实测值。148 时代的 GREASE 是 ``Not/A)Brand`` + ``v="99"``。
GREASE_BRAND = 'Not/A)Brand'
GREASE_VERSION = '99'

# 浏览器实测上报的 client hints 原文（仅作记录/对比用，出站用下面的 SEC_CH_UA）。
SEC_CH_UA_MEASURED = f'"{GREASE_BRAND}";v="{GREASE_VERSION}", "Chromium";v="{CHROME_MAJOR}"'

# 出站请求声明的 client hints。
#
# 与 SEC_CH_UA_MEASURED 的差别只有多一个 ``"Google Chrome"`` 品牌：Playwright
# 拉起的 Chrome 会自动从品牌列表里去掉它（自动化模式的标记之一），而**我们这组头
# 是给普通 HTTP 请求声明身份用的**——那类请求本来就不经过浏览器，声明成「正式版
# 非自动化 Chrome」才是自洽的，也是旧项目实测后刻意保留的写法。
SEC_CH_UA = (
    f'"{GREASE_BRAND}";v="{GREASE_VERSION}", '
    f'"Google Chrome";v="{CHROME_MAJOR}", "Chromium";v="{CHROME_MAJOR}"'
)

# 与 SEC_CH_UA 配套的两个头：必须一起出现，缺一个就是「半套指纹」。
SEC_CH_UA_MOBILE = '?0'
SEC_CH_UA_PLATFORM = '"Windows"'

# 指纹类请求头的完整集合。小写键名，httpx 头字典大小写不敏感，这里统一小写
# 是为了与 MTOP 调用处的写法一致，避免出现两份「看起来不一样」的头。
FINGERPRINT_HEADERS = {
    'user-agent': CHROME_UA,
    'sec-ch-ua': SEC_CH_UA,
    'sec-ch-ua-mobile': SEC_CH_UA_MOBILE,
    'sec-ch-ua-platform': SEC_CH_UA_PLATFORM,
}

# 出站请求默认的其余头。Accept-Language 必须与浏览器 locale 一致（见 stealth.py），
# 语言与时区对不上本身就是一条独立的可疑特征。
ACCEPT_LANG = 'zh-CN,zh;q=0.9,en;q=0.8'


def headers(extra: dict | None = None) -> dict:
    """返回一份带指纹的请求头副本；``extra`` 里的同名键覆盖默认值。"""
    merged = dict(FINGERPRINT_HEADERS)
    merged['accept-language'] = ACCEPT_LANG
    if extra:
        merged.update({k.lower(): v for k, v in extra.items()})
    return merged
