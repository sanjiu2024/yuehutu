"""WS 事件驱动的前端自动刷新冒烟：真浏览器 + 真后端 + 真 WebSocket。

为什么需要这个脚本：`scripts/ui_smoke.py` 只能证明「九个页面能渲染出来」，
证明不了「别处改了数据，页面会自己更新」。而「除会话页外全站不实时刷新」
正是只读审计抓到的 E1 缺陷（前端只有会话页订阅了 WS），修完之后必须拿真
浏览器验证，不能只看 `tsc` 通过。

本脚本证明三件事，且全程**不刷新页面**：

1. 账号页：从脚本侧 `PATCH /api/accounts/demo_acc` 改备注，页面应自己冒出
   新备注文字（后端 `api/accounts.py::_publish('accounts.changed')` →
   前端 `useAsync(..., ['accounts.changed'])` 静默重拉）。
2. 提示词页：从脚本侧 `POST /api/agents/prompts` 新建提示词，页面应自己冒
   出这段内容（后端 `api/agents.py::_publish('prompts.changed')`）。
3. 两个阶段都用 `performance.getEntriesByType('navigation').length` 证明
   期间没有发生任何导航 —— 即页面不是靠 reload 刷新的。

用法：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe scripts\\live_refresh_smoke.py --port 18120"
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))

import ui_smoke  # noqa: E402

DEMO_ACCOUNT = 'demo_acc'
CONNECTED_TEXT = '实时连接已建立'
DISCONNECTED_TEXT = '实时连接未建立'
WAIT_REFRESH = 15.0


def nav_count(page) -> int:
    """页面生命周期内发生过的导航次数。刷新会 +1，WS 重渲染不会。"""
    return int(page.evaluate("performance.getEntriesByType('navigation').length"))


def body_text(page) -> str:
    return page.inner_text('body')


def wait_for_text(page, needle: str, timeout: float = WAIT_REFRESH) -> tuple[bool, str, float]:
    """轮询等待文案出现（**不**碰页面，不 reload）。返回 (命中, 最终正文, 用时秒)。"""
    start = time.time()
    seen = ''
    while time.time() - start < timeout:
        seen = body_text(page)
        if needle in seen:
            return True, seen, time.time() - start
        page.wait_for_timeout(200)
    return False, seen, time.time() - start


def wait_page_ready(page, timeout: float = 15.0) -> None:
    """等外壳 + 页面渲染完：导航高亮项出现，且正文有实质内容。

    以前这里等的是具体文案（`text=账号列表` / `text=提示词`）—— 文案是会被改的东西
    （第十一批 UI 重做就把「账号列表」改掉了），拿它当锚点会让脚本在**功能没坏**的时候
    假失败。锚点只认结构与内容量。
    """
    ms = int(timeout * 1000)
    page.wait_for_selector('.nav-item.active', timeout=ms)
    page.wait_for_function("() => document.body.innerText.trim().length > 200", timeout=ms)


def phase_accounts(page, base: str, token: str) -> dict:
    page.goto(f'{base}/#/accounts', wait_until='load', timeout=30000)
    wait_page_ready(page)
    page.wait_for_timeout(2600)  # 外壳每 2s 轮询一次 WS 状态，不等会误判离线

    text = body_text(page)
    connected = CONNECTED_TEXT in text
    if not connected:
        return {'phase': 'accounts', 'ok': False, 'reason': 'WS 未连接，无法验证自动刷新', 'snippet': text[-400:]}

    marker = f'LIVE-REFRESH-{int(time.time())}'
    before_nav = nav_count(page)
    status, raw = ui_smoke.call(base, 'PATCH', f'/api/accounts/{DEMO_ACCOUNT}', token, {'remark': marker})
    hit, seen, took = wait_for_text(page, marker)
    after_nav = nav_count(page)

    return {
        'phase': 'accounts',
        'ok': bool(hit) and before_nav == after_nav,
        'marker': marker,
        'http_status': status,
        'http_body': raw[:160],
        'text_appeared': hit,
        'seconds': round(took, 2),
        'nav_before': before_nav,
        'nav_after': after_nav,
    }


def phase_prompts(page, base: str, token: str) -> dict:
    page.goto(f'{base}/#/prompts', wait_until='load', timeout=30000)
    wait_page_ready(page)
    page.wait_for_timeout(2600)

    text = body_text(page)
    connected = CONNECTED_TEXT in text
    if not connected:
        return {'phase': 'prompts', 'ok': False, 'reason': 'WS 未连接，无法验证自动刷新', 'snippet': text[-400:]}

    marker = f'自动刷新冒烟 {int(time.time())}'
    before_nav = nav_count(page)
    status, raw = ui_smoke.call(
        base,
        'POST',
        '/api/agents/prompts',
        token,
        {'scope': 'global', 'content': marker, 'priority': 0, 'role': '', 'name': 'live-refresh-smoke'},
    )
    hit, seen, took = wait_for_text(page, marker)
    after_nav = nav_count(page)

    return {
        'phase': 'prompts',
        'ok': bool(hit) and before_nav == after_nav,
        'marker': marker,
        'http_status': status,
        'http_body': raw[:160],
        'text_appeared': hit,
        'seconds': round(took, 2),
        'nav_before': before_nav,
        'nav_after': after_nav,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='WS 事件驱动的前端自动刷新冒烟')
    parser.add_argument('--port', type=int, default=18120)
    parser.add_argument('--keep', action='store_true', help='保留临时目录（排查用）')
    args = parser.parse_args()

    work_root = pathlib.Path(tempfile.mkdtemp(prefix='xaiw-live-refresh-'))
    app_dir = work_root / 'app'
    log_dir = work_root / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / 'data').mkdir(parents=True, exist_ok=True)

    proc = None
    browser = None
    results: list[dict] = []
    try:
        # config 在 import 期读 XIANYU_APP_DIR，所以必须先设环境变量再 import
        os.environ['XIANYU_APP_DIR'] = str(app_dir)
        os.environ['PYTHONPATH'] = str(REPO)
        sys.path.insert(0, str(REPO))

        dist = REPO / 'frontend' / 'dist'
        if not (dist / 'index.html').exists():
            print(f'前端产物不存在：{dist}（先 cd frontend && npm run build）')
            return 1

        from backend.app.core import config as core_config  # noqa: E402
        from backend.app.core import db as core_db  # noqa: E402
        from backend.app.core import security  # noqa: E402

        core_db.init_db()
        security.ensure_admin()
        ui_smoke.seed_demo_data(pathlib.Path(str(core_config.DB_PATH)))
        core_db.reset_connection()

        proc = ui_smoke.start_backend(args.port, app_dir, log_dir)
        base = f'http://127.0.0.1:{args.port}'
        health = ui_smoke.wait_health(base)

        secret = (app_dir / 'data' / 'local_secret.txt').read_text(encoding='utf-8').strip()
        status, raw = ui_smoke.call(base, 'POST', '/api/auth/desktop', extra={'X-Desktop-Secret': secret})
        if status != 200:
            print(f'拿桌面 token 失败：{status} {raw[:200]}')
            return 1
        token = json.loads(raw)['data']['token']

        executable, extra_args, browser_desc = ui_smoke.find_browser()
        print(f'后端 {base} frozen={health.get("frozen")} 浏览器={browser_desc}')

        from patchright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=executable, args=extra_args)
            context = browser.new_context(viewport={'width': 1440, 'height': 940})
            context.add_init_script(
                f'window.localStorage.setItem("xywb_token", {json.dumps(token)});'
            )
            page = context.new_page()
            results.append(phase_accounts(page, base, token))
            results.append(phase_prompts(page, base, token))
            browser.close()
            browser = None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # pragma: no cover
                pass
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # pragma: no cover
                proc.kill()
        if args.keep:
            print(f'临时目录保留在 {work_root}')
        else:
            shutil.rmtree(work_root, ignore_errors=True)

    print(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if not r.get('ok')]
    if failed:
        print(f'LIVE REFRESH SMOKE FAILED ({len(failed)}/{len(results)} 阶段失败)')
        return 1
    print(f'LIVE REFRESH SMOKE ALL OK ({len(results)}/{len(results)} 阶段通过：账号页与提示词页都在无刷新的情况下自动更新)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
