"""真实 UI 冒烟：真开一个 Chromium，把九页逐个渲染出来并截图。

**为什么需要它**：后端接口全绿、`npx tsc --noEmit` 0 error、`npm run build` 成功、
打包 exe 的 `/api/health` 200 —— 这些**都不能**证明页面上没有运行时崩溃、白屏或
「接口调不通」。前端此前**零自动化**（`frontend/src` 下没有任何 `*.test.*`），
浏览器里到底渲染成什么样，一直没人看过。这是第一条真开浏览器的检查。

它做三件事：
1. 在**临时数据目录**（`XIANYU_APP_DIR`）里播种一份演示数据 —— 不污染你真实的
   `%APPDATA%\\xianyu-ai-workbench\\data\\app.db`；
2. 起真实后端，真开 Chromium（patchright，本机已装 chromium-1223），逐页访问
   `#/overview` … `#/risk`；
3. 断言每页**真的渲染出了内容**（有导航、有主标题、body 有实质文本、没有命中
   ErrorBoundary 的「这个页面出了点问题」、没有未捕获的页面异常），并截图到
   `logs/ui_smoke/*.png`。

**安全性**：只打本机 `127.0.0.1` 的后端；账号 cookie 留空 + `login_state=need_relogin`
（协议层因此永远构不出客户端，绝不会拿假 cookie 去连真平台）；provider 的
`base_url` 指向 `http://127.0.0.1:9`（discard 端口）。**不打真实闲鱼平台、不调真实 LLM。**

用法：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe scripts\\ui_smoke.py --port 18097"

退出码：全部页面通过为 0，否则 1。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]

PAGES = [
    ('overview', '概览'),
    ('accounts', '账号'),
    ('items', '商品'),
    ('chats', '会话'),
    ('finance', '财务'),
    ('agents', 'AI 沟通'),
    ('master', '主管 AI'),
    ('providers', 'AI 配置'),
    ('prompts', '提示词'),
    ('risk', '风控'),
]

BOUNDARY_TEXT = '这个页面出了点问题'
DEMO_ACCOUNT = 'demo_acc'


def call(base: str, method: str, path: str, token: str = '', body: dict | None = None, extra: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    for key, value in (extra or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


# --------------------------------------------------------------------------- 演示数据


def seed_demo_data(db_path: pathlib.Path) -> None:
    """播种一份**看得见东西**的演示数据。

    账号的 cookie 故意留空、`login_state='need_relogin'`：协议层因此永远构不出
    `XianyuClient`，任何「同步/发布/发消息」都会走结构化失败 —— 既安全，
    又正好让 UI 的「未就绪」分支被真实渲染出来。
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA foreign_keys=ON')

    conn.execute(
        "INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply, remark) VALUES (?,?,?,?,?)",
        (DEMO_ACCOUNT, '演示小铺', 1, 1, '演示用，未登录真实账号'),
    )
    conn.execute(
        "INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state, relogin_reason) "
        "VALUES (?,?,?,?)",
        (DEMO_ACCOUNT, '', 'need_relogin', '演示数据：从未登录'),
    )

    items = [
        ('demo-1001', 'iPhone 13 128G 午夜色 国行', 2350.0, 2999.0, 'on_sale'),
        ('demo-1002', '索尼 WH-1000XM4 头戴降噪耳机', 980.0, 1299.0, 'on_sale'),
        ('demo-1003', '罗技 MX Master 3S 鼠标（已下架）', 420.0, 599.0, 'off_shelf'),
    ]
    for item_id, title, price, original, status in items:
        conn.execute(
            "INSERT OR REPLACE INTO items(account_id, item_id, title, price, original_price, stock, "
            "status, description, ai_enabled) VALUES (?,?,?,?,?,?,?,?,1)",
            (DEMO_ACCOUNT, item_id, title, price, original, 1, status, f'{title} —— 演示描述'),
        )

    conn.execute(
        "INSERT OR REPLACE INTO providers(id, name, base_url, api_key, default_model, enabled, is_default) "
        "VALUES (1,?,?,?,?,1,1)",
        ('演示提供者（不会真的调用）', 'http://127.0.0.1:9', 'sk-demo-not-a-real-key', 'deepseek-chat'),
    )

    conn.execute(
        "INSERT INTO ai_prompts(role, scope, name, content, priority) VALUES ('item','global',?,?,10)",
        ('演示：商品 AI 全局提示词', '你是闲鱼卖家助手，语气随和，只回答与商品相关的问题。'),
    )

    conn.execute(
        "INSERT OR REPLACE INTO conversations(id, account_id, peer_user_id, peer_nickname, item_id, mode, "
        "agent_role, unread) VALUES (1,?,?,?,?,'auto','item',2)",
        (DEMO_ACCOUNT, 'demo_buyer', '演示买家', 'demo-1001'),
    )
    msgs = [
        (1, 'in', 'buyer', '这个还在吗？能便宜点不', None),
        (1, 'out', 'ai:item', '在的～这台是国行自用，2350 已经是很实在的价了', 0),
        (1, 'in', 'buyer', '电池健康度多少？', None),
    ]
    for conv_id, direction, sender, content, sent_ok in msgs:
        conn.execute(
            "INSERT INTO messages(conversation_id, direction, sender, content, sent_ok) VALUES (?,?,?,?,?)",
            (conv_id, direction, sender, content, sent_ok),
        )

    agent_msgs = [
        (1, 'item:demo-1001', 'cross', 'ask', '买家问了耳机，这台还有货吗？'),
        (1, 'cross', 'item:demo-1002', 'answer', '有货，只剩一台，可以正常发。'),
        (1, 'item:demo-1001', 'master', 'notify', '已把耳机的库存情况转告买家。'),
    ]
    for conv_id, from_agent, to_agent, kind, topic in agent_msgs:
        conn.execute(
            "INSERT INTO agent_messages(conversation_id, from_agent, to_agent, kind, topic) VALUES (?,?,?,?,?)",
            (conv_id, from_agent, to_agent, kind, topic),
        )

    conn.execute(
        "INSERT INTO risk_events(account_id, kind, message) VALUES (?,?,?)",
        (DEMO_ACCOUNT, 'hit', '演示事件：一次风控命中（非真实）'),
    )
    conn.execute(
        "INSERT OR REPLACE INTO risk_state(account_id, consecutive_hits, blocked_until, last_hit_at) "
        "VALUES (?,?,?,?)",
        (DEMO_ACCOUNT, 1, 0, 0),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- 后端


def wait_health(base: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        try:
            status, raw = call(base, 'GET', '/api/health')
            if status == 200:
                return json.loads(raw)
            last = f'{status} {raw[:120]}'
        except Exception as exc:  # 后端还没起来
            last = repr(exc)
        time.sleep(0.4)
    raise SystemExit(f'后端在 {timeout:.0f}s 内没有就绪：{last}')


def start_backend(port: int, app_dir: pathlib.Path, log_dir: pathlib.Path) -> subprocess.Popen:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['XIANYU_APP_DIR'] = str(app_dir)
    env['XYWB_PORT'] = str(port)
    out = open(log_dir / 'backend.out.log', 'wb')
    err = open(log_dir / 'backend.err.log', 'wb')
    return subprocess.Popen(
        [sys.executable, '-m', 'backend.app.main', '--port', str(port)],
        cwd=str(REPO), env=env, stdout=out, stderr=err,
    )


# --------------------------------------------------------------------------- 浏览器


def find_browser() -> tuple[str, list[str], str]:
    """选浏览器 —— **优先用 Edge**，因为它就是桌面版 WebView2 的引擎。

    `patchright` 自带的 `install` 会去下它当前版本要求的 Chromium（本机装的是 1223，
    它想要 1243），直接 `launch()` 会报 `Executable doesn't exist`。更重要的是：
    自带那份 Chromium（148）会把 `ws://127.0.0.1` 的握手拦成
    `net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`（HTTP fetch 却放行），
    于是「实时连接未建立」——**这是测试浏览器的行为，不是应用缺陷**：
    实测同一页面在同一台机器的 Edge 154.0.4258.53（＝本机 WebView2 运行时版本）
    里 WS 正常握手。所以默认用 Edge，只有找不到 Edge 时才退回自带 Chromium
    并显式关掉那条检查，免得把浏览器的策略误报成产品 bug。

    返回 `(可执行文件, 额外启动参数, 说明)`。
    """
    # 本机所有「playwright/patchright 启动的 Chromium 家族」都会把 `ws://127.0.0.1`
    # 的握手拦成 `net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`（HTTP fetch 却放行），
    # 于是冒烟里每页都误判成「实时连接未建立」。这是**测试环境**的策略，不是产品缺陷：
    # 真起桌面外壳（WebView2 154，就是本机 Edge 的引擎）时左边栏显示的是
    # 「实时连接已建立」、右上角「实时在线」——所以这里统一关掉这条检查。
    lna_flag = ['--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests']
    for candidate in (
        pathlib.Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))
        / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe',
        pathlib.Path(os.environ.get('ProgramFiles', r'C:\Program Files'))
        / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe',
    ):
        if candidate.exists():
            # 注意：**不要**跑 `msedge.exe --version` —— 在 Windows 上它不会打印版本，
            # 而是直接开一个浏览器窗口，还会把后面 patchright 的启动搞成「现有会话」。
            return str(candidate), lna_flag, 'Edge（与桌面版 WebView2 同引擎）'

    root = pathlib.Path(os.environ.get('LOCALAPPDATA', '')) / 'ms-playwright'

    def newest(pattern: str) -> str:
        hits = sorted(root.glob(pattern))
        return str(hits[-1]) if hits else ''

    for pattern in (
        'chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe',
        'chromium-*/chrome-win64/chrome.exe',
        'chromium-*/chrome-linux/chrome',
    ):
        found = newest(pattern)
        if found:
            return found, lna_flag, 'patchright 自带 Chromium（已关掉 LocalNetworkAccess 检查；Edge 才是产品引擎）'
    raise SystemExit(
        f'没找到可用浏览器（找过 Edge 与 {root}）。装 Edge 或跑 `patchright install chromium` 再试。'
    )


def run_browser(base: str, token: str, out_dir: pathlib.Path) -> list[dict]:
    from patchright.sync_api import sync_playwright

    executable, extra_args, note = find_browser()
    print(f'浏览器：{note}\n        {executable}')
    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=executable, args=extra_args)
        context = browser.new_context(viewport={'width': 1440, 'height': 940})
        # 桌面外壳平时是它把 token 写进 localStorage 的；这里等价地把前置条件造出来
        context.add_init_script(
            "window.localStorage.setItem('xywb_token', %s);" % json.dumps(token)
        )
        page = context.new_page()
        console_errors: list[str] = []
        page_errors: list[str] = []
        http_errors: list[str] = []
        page.on('console', lambda m: console_errors.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: page_errors.append(str(e)))
        page.on(
            'response',
            lambda r: http_errors.append(f'{r.status} {r.request.method} {r.url}') if r.status >= 400 else None,
        )

        for key, label in PAGES:
            console_errors.clear()
            page_errors.clear()
            http_errors.clear()
            url = f'{base}/#/{key}'
            row = {'page': key, 'label': label, 'ok': True, 'problems': []}
            try:
                page.goto(url, wait_until='load', timeout=30000)
                # 等页面把接口打完：等导航项 + 等主标题出现
                page.wait_for_selector('text=' + label, timeout=15000)
                # 外壳每 2s 轮询一次 WS 连接状态，等它至少跑一轮（否则会误判成「离线」）
                page.wait_for_timeout(2600)
                body = page.inner_text('body')
                row['chars'] = len(body.strip())
                if BOUNDARY_TEXT in body:
                    row['problems'].append('命中了 ErrorBoundary')
                if row['chars'] < 200:
                    row['problems'].append(f'页面几乎没渲染出内容（{row["chars"]} 字符）')
                if label not in body:
                    row['problems'].append(f'没看到页面标题「{label}」')
                # 掉回登录页是最隐蔽的失败：页面"渲染成功了"，但你被踢出来了
                if '登录后即可管理账号' in body:
                    row['problems'].append('掉回登录页（token 被清掉了）')
                # 底部状态栏写的就是 WS 实际连没连上（外壳每 2s 刷新）
                if '实时连接未建立' in body:
                    row['problems'].append('WebSocket 没连上（底部显示「实时连接未建立」）')
                shot = out_dir / f'{key}.png'
                page.screenshot(path=str(shot), full_page=True)
                row['shot'] = str(shot)
            except Exception as exc:
                row['problems'].append(f'{type(exc).__name__}: {exc}')
            if page_errors:
                row['problems'].append('未捕获页面异常: ' + ' | '.join(page_errors[:2]))
            # console 里的 4xx 请求失败是**预期**的（演示数据故意让协议层未就绪），
            # 只把真正的 JS 报错当问题
            js_errors = [e for e in console_errors if 'Failed to load resource' not in e and 'net::' not in e]
            if js_errors:
                row['problems'].append('控制台报错: ' + ' | '.join(js_errors[:2]))
            row['ok'] = not row['problems']
            if not row['ok']:
                # 失败时把现场留下来：token 还在不在、哪些请求返回了 4xx/5xx
                try:
                    row['token'] = bool(page.evaluate("window.localStorage.getItem('xywb_token')"))
                except Exception:
                    row['token'] = None
                row['http_errors'] = list(dict.fromkeys(http_errors))[:8]
            results.append(row)
            print(f"{'OK ' if row['ok'] else 'BAD'} {key:<10} {label:<8} {row.get('chars', 0):>5} 字符"
                  f"{'' if row['ok'] else '  <- ' + '; '.join(row['problems'])}")
            if not row['ok']:
                print(f"    token 还在？{row.get('token')}   4xx/5xx 请求：")
                for line in row['http_errors']:
                    print(f'      {line}')

        browser.close()
    return results


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description='真开浏览器逐页渲染九页并截图')
    ap.add_argument('--port', type=int, default=18097, help='后端端口（默认 18097）')
    ap.add_argument('--app-dir', default='', help='数据目录；默认建一个临时目录')
    ap.add_argument('--out', default=str(REPO / 'logs' / 'ui_smoke'), help='截图输出目录')
    ap.add_argument('--keep', action='store_true', help='跑完保留临时数据目录')
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_root = pathlib.Path(args.app_dir) if args.app_dir else pathlib.Path(tempfile.mkdtemp(prefix='xywb_ui_'))
    app_dir = temp_root
    (app_dir / 'data').mkdir(parents=True, exist_ok=True)
    (app_dir / 'logs').mkdir(parents=True, exist_ok=True)

    # config 在 import 期读 XIANYU_APP_DIR，所以必须先设环境变量再 import
    os.environ['XIANYU_APP_DIR'] = str(app_dir)
    os.environ['PYTHONPATH'] = str(REPO)
    sys.path.insert(0, str(REPO))

    dist = REPO / 'frontend' / 'dist'
    if not (dist / 'index.html').exists():
        raise SystemExit(f'前端产物不存在：{dist}（先 cd frontend && npm run build）')

    from backend.app.core import config as core_config  # noqa: E402
    from backend.app.core import db as core_db  # noqa: E402
    from backend.app.core import security  # noqa: E402

    core_db.init_db()
    security.ensure_admin()
    seed_demo_data(pathlib.Path(str(core_config.DB_PATH)))
    print(f'演示数据已播种（数据目录 {app_dir}）—— 你真实的 APPDATA 数据目录没有被碰')

    base = f'http://127.0.0.1:{args.port}'
    proc = start_backend(args.port, app_dir, app_dir / 'logs')
    try:
        health = wait_health(base)
        print(f"后端就绪：{base}  /api/health -> {json.dumps(health, ensure_ascii=False)[:160]}")

        secret = (app_dir / 'data' / 'local_secret.txt').read_text('utf-8').strip()
        status, raw = call(base, 'POST', '/api/auth/desktop', extra={'X-Desktop-Secret': secret})
        if status != 200:
            raise SystemExit(f'桌面自动登录失败：{status} {raw}')
        token = json.loads(raw)['data']['token']
        print('桌面自动登录 OK（X-Desktop-Secret）')

        results = run_browser(base, token, out_dir)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        if not args.keep and not args.app_dir:
            shutil.rmtree(temp_root, ignore_errors=True)

    bad = [r for r in results if not r['ok']]
    print()
    print(f'截图目录：{out_dir}')
    print(f"UI SMOKE {'ALL OK' if not bad else 'FAIL: ' + ', '.join(r['page'] for r in bad)}"
          f'  ({len(results) - len(bad)}/{len(results)} 页通过)')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
