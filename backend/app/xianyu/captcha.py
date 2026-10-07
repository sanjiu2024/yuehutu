"""人工验证：拉起系统 Chrome 让用户自己过滑块，然后取回 Cookie。

**为什么必须用系统 Chrome（``channel='chrome'``）而不是 Playwright 自带的
Chromium**：阿里风控会检查浏览器指纹与版本。Playwright 自带的 Chromium 版本
常年落后、且带明显的自动化特征，用户在那上面手动划滑块也过不了，或者过了
但拿不到 ``x5sec``。旧项目为此把验证流程整个换成系统 Chrome + 持久化 profile。

三个关键点：

1. **持久化 profile 用 ``manual_<cookie_id>``，且刻意不复用自动滑块的
   ``slider_<cookie_id>``** —— 同一 profile 被两个进程同时打开会撞 Chrome 的
   singleton 锁，第二个进程直接起不来。
2. **不伪造 UA**。用真实 Chrome 就声明真实身份；伪造 UA 会与
   ``navigator.userAgent`` 对不上，反而暴露。
3. **取回 Cookie 后硬校验 ``x5sec``**。滑块消失 ≠ 验证通过（用户可能只是把
   滑块划歪了、或者页面自己收起了挑战）。没有 ``x5sec`` 就必须明确报失败，
   绝不能报成功 —— 这是旧项目最贵的一课：报成功后上层拿着一张没通过风控的
   Cookie 继续跑，每个请求都撞风控，用户看到的是「验证完了还是用不了」。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from ..core import config
from . import cookies as cookie_utils
from . import stealth

# 验证入口。惩罚页通常会把用户带到首页再弹滑块。
LOGIN_URL = 'https://www.goofish.com/'

DEFAULT_TIMEOUT = 300
CAPTCHA_PRESENT_TIMEOUT = 20

# 滑块选择器。主页面与 iframe 都要查（阿里滑块常包在 iframe 里）。
CAPTCHA_SELECTORS = (
    '#nocaptcha',
    '#scratch-captcha-btn',
    '.scratch-captcha-container',
    '.scratch-captcha-slider',
    '[id*="captcha"]',
    '.nc-container',
)

# 刻意不带 ``--disable-blink-features=AutomationControlled`` 之外的伪装参数；
# 这些参数都是「让真实 Chrome 更像人手动打开」的常规项，不涉及指纹伪造。
LAUNCH_ARGS = (
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--no-first-run',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-blink-features=AutomationControlled',
    '--no-default-browser-check',
    '--disable-popup-blocking',
    '--disable-search-engine-choice-screen',
)


class ManualVerificationError(RuntimeError):
    """人工验证失败。``reason`` 用于前端提示。"""

    def __init__(self, message: str, reason: str = 'unknown'):
        super().__init__(message)
        self.reason = reason


def profile_dir(cookie_id: str) -> str:
    """人工验证用的持久化 profile 目录。

    放在 ``config.BROWSER_DIR`` 下（``%APPDATA%\\xianyu-ai-workbench\\
    browser_profiles``），而不是旧项目的 ``os.getcwd()/browser_data`` ——
    打包成桌面应用后 cwd 可能是任意位置，甚至只读。
    """
    path = os.path.join(str(config.BROWSER_DIR), f'manual_{cookie_id}')
    os.makedirs(path, exist_ok=True)
    return path


def to_playwright_cookies(cookies_str: str) -> list:
    """Cookie 串 → Playwright 的 cookie 结构。"""
    result = []
    for name, value in cookie_utils.parse_cookie_string(cookies_str).items():
        result.append({
            'name': name,
            'value': value,
            'domain': '.goofish.com',
            'path': '/',
        })
    return result


def from_playwright_cookies(items: list) -> str:
    """Playwright cookie 列表 → Cookie 串。

    **同名首次优先**：验证流程会从 punish 页跳到 taobao.com，跳转后浏览器写入
    一套 taobao 访客 Cookie（``cookie2`` / ``_tb_token_`` / ``_m_h5_tk`` 等与
    登录态同名但值不同）。若后者覆盖前者，登录态当场被冲掉，换 token 直接
    ``FAIL_SYS_SESSION_EXPIRED``。只取 goofish/taobao 域的项。
    """
    pairs = []
    seen = set()
    for item in items or []:
        name = item.get('name')
        if not name or name in seen:
            continue
        domain = item.get('domain') or ''
        if domain and 'goofish' not in domain and 'taobao' not in domain:
            continue
        seen.add(name)
        pairs.append(f"{name}={item.get('value', '')}")
    return '; '.join(pairs)


def verification_url(cookie_str: str, fallback: Optional[str] = None) -> str:
    """决定打开哪个 URL。

    优先用 Cookie 里 ``x5secdata`` 携带的惩罚页地址（服务端就是让我们去那儿
    过挑战）；没有就用调用方给的兜底，再没有才回首页。
    """
    fallback = fallback or LOGIN_URL
    return fallback


async def _wait_for_captcha(page, timeout: float) -> bool:
    """等滑块元素出现（主页面 + 各 frame）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in page.frames:
            for selector in CAPTCHA_SELECTORS:
                try:
                    element = await frame.query_selector(selector)
                    if element and await element.is_visible():
                        logger.info(f'检测到滑块元素: {selector}')
                        return True
                except Exception:
                    continue
        await asyncio.sleep(1)
    return False


async def _open_context(playwright, cookie_id: str, headless: bool):
    """打开持久化上下文。

    先试系统 Chrome（``channel='chrome'``）；机器上没装正式版 Chrome 时回退
    到 Playwright 自带 Chromium —— 能跑起来总比直接报错好，但要 warning 说明
    「这种情况下很可能过不了风控」。
    """
    kwargs = {
        'user_data_dir': profile_dir(cookie_id),
        'headless': headless,
        'args': list(LAUNCH_ARGS),
        'locale': stealth.LOCALE,
        'timezone_id': stealth.TIMEZONE_ID,
        'viewport': {'width': 1440, 'height': 900},
    }
    try:
        return await playwright.chromium.launch_persistent_context(
            channel='chrome', **kwargs
        )
    except Exception as exc:
        logger.warning(f'系统 Chrome 启动失败（{exc}），回退 Playwright Chromium；'
                       f'此模式下风控通过率显著降低')
        return await playwright.chromium.launch_persistent_context(**kwargs)


def _driver_problem() -> str:
    """playwright 自带 driver（node.exe + package/cli.js）是否就位；缺了返回给用户看的原因。

    **为什么要在启动前自己查**：driver 缺文件时 `async_playwright()` 抛的是
    `FileNotFoundError [WinError 2] 系统找不到指定的文件`，这句话对用户毫无意义，
    而且发生在 `async with` 的入口 —— 连本函数里那圈 `try/except` 都够不着。
    PyInstaller 只收 Python 模块、不收数据文件（`collect_data_files`）时就是这个症状。
    """
    try:
        import playwright
    except ImportError:  # pragma: no cover - 调用方已经先查过 import
        return '未安装 playwright，无法进行人工验证'
    base = Path(getattr(playwright, '__file__', '') or '.').parent / 'driver'
    node = base / ('node.exe' if sys.platform == 'win32' else 'node')
    cli = base / 'package' / 'cli.js'
    if not node.exists() or not cli.exists():
        return (
            '内置浏览器驱动不完整（缺少 playwright driver），无法弹出滑块验证。'
            '这是打包时漏收数据文件导致的：请用源码方式运行，或重新打包后再试。'
        )
    return ''


async def manual_verify(cookie_id: str, cookie_str: Optional[str] = None,
                        timeout: float = DEFAULT_TIMEOUT,
                        headless: bool = False,
                        fallback_url: Optional[str] = None) -> Dict[str, Any]:
    """拉起浏览器让用户过滑块，返回 ``{"ok", "cookie", "message"}``。

    **成功判据只有一个：拿回来的 Cookie 里有 ``x5sec``。**

    ``cookie_str`` 省略时从库里读该账号的现有 Cookie（前端只传账号 id 的场景）。
    """
    if not cookie_id:
        return {'ok': False, 'cookie': cookie_str or '', 'message': '缺少账号 ID'}
    if cookie_str is None:
        from . import store

        cookie_str = store.get_cookie(cookie_id) or ''
    if cookie_utils.has_x5sec(cookie_str):
        # 已经有通行凭证就没必要再弹浏览器 —— 用户点「验证」时往往已经过了，
        # 再弹一次只会让他困惑。
        return {'ok': True, 'cookie': cookie_str, 'message': 'Cookie 已含 x5sec，无需验证'}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {'ok': False, 'cookie': cookie_str,
                'message': '未安装 playwright，无法进行人工验证'}

    missing = _driver_problem()
    if missing:
        # 打包版最容易踩这个：只 `collect_submodules('playwright')` 而不收 driver
        # 数据文件时，Python 层 import 完全正常，一启动就 `[WinError 2]`，
        # 用户看到的只是「点了没反应」（真机 operation_logs 连续 7 条 manual_verify 失败）。
        logger.error('人工验证不可用: %s', missing)
        return {'ok': False, 'cookie': cookie_str, 'message': missing}

    cleaned = cookie_utils.drop_challenge_markers(cookie_str)
    target = verification_url(cleaned, fallback_url)

    async with async_playwright() as playwright:
        context = None
        try:
            context = await _open_context(playwright, cookie_id, headless)
            page = context.pages[0] if context.pages else await context.new_page()
            # 只藏自动化标记，不伪造身份（见 stealth.py 的教训）。
            await page.add_init_script(stealth.STEALTH_SCRIPT)
            try:
                await context.add_cookies(to_playwright_cookies(cleaned))
            except Exception as exc:
                logger.warning(f'写入 Cookie 失败（继续，可能是域名不匹配）: {exc}')

            await page.goto(target, wait_until='domcontentloaded', timeout=60000)
            await _wait_for_captcha(page, CAPTCHA_PRESENT_TIMEOUT)

            # 等用户手动完成。这里不能用 wait_for_selector 等滑块消失 ——
            # 滑块消失有多种原因（划歪了、挑战超时），不能当成成功信号。
            deadline = asyncio.get_event_loop().time() + timeout
            new_cookies = cleaned
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2)
                try:
                    new_cookies = from_playwright_cookies(await context.cookies())
                except Exception:
                    continue
                if cookie_utils.has_x5sec(new_cookies):
                    break

            if not cookie_utils.has_x5sec(new_cookies):
                # 硬校验失败：绝不报成功。
                return {
                    'ok': False,
                    'cookie': new_cookies,
                    'message': '滑块已消失，但没有拿到 x5sec 通过凭证；请重新验证',
                }
            return {'ok': True, 'cookie': new_cookies, 'message': '验证成功，已取得 x5sec'}
        except Exception as exc:
            logger.error(f'人工验证异常: {exc}')
            return {'ok': False, 'cookie': cookie_str, 'message': f'人工验证异常: {exc}'}
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
