"""桌面外壳：起后端线程 + WebView2 窗口 + 托盘。

用法（开发）：
    set PYTHONPATH=. && python desktop\\main.py

打包成 exe 见 scripts/build_exe.ps1。窗口关掉不退出，托盘常驻（点托盘「退出」才停）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app import main as backend_main  # noqa: E402
from backend.app.core import config, security  # noqa: E402

URL = ''
TOKEN = ''


def wait_health(port: int, timeout: float = config.BACKEND_READY_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    url = f'http://{config.HOST}:{port}/api/health'
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def desktop_login(port: int) -> str:
    """用本地密钥换一个登录 token，免得用户还要输密码。"""
    req = urllib.request.Request(
        f'http://{config.HOST}:{port}/api/auth/desktop', method='POST', data=b''
    )
    req.add_header('X-Desktop-Secret', security.local_secret())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            return body.get('data', {}).get('token', '')
    except Exception as exc:  # noqa: BLE001 - 桌面外壳不该因为拿不到 token 就崩
        print(f'[desktop] 自动登录失败（可手动登录）: {exc}')
        return ''


def start_tray(on_quit):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print('[desktop] 未安装 pystray/Pillow，跳过托盘')
        return None

    image = Image.new('RGB', (64, 64), '#0f172a')
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill='#38bdf8')
    draw.text((22, 22), '闲', fill='#0f172a')

    def quit_action(icon, item):  # noqa: ARG001
        icon.visible = False
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem('打开窗口', lambda icon, item: None, default=True),
        pystray.MenuItem('数据目录', lambda icon, item: os.startfile(config.APP_DIR)),  # noqa: SIM115
        pystray.MenuItem('退出', quit_action),
    )
    icon = pystray.Icon('xianyu-ai-workbench', image, config.APP_TITLE, menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main() -> int:
    global URL, TOKEN

    try:
        import webview
    except ImportError:
        print('缺少 pywebview：C:\\Python310\\python.exe -m pip install pywebview pystray')
        return 1

    port = backend_main.pick_free_port()
    URL = f'http://{config.HOST}:{port}/'

    thread = threading.Thread(
        target=backend_main.run_server,
        kwargs={'host': config.HOST, 'port': port},
        daemon=True,
        name='xywb-backend',
    )
    thread.start()

    if not wait_health(port):
        print('[desktop] 后端启动超时，请查看 logs/app.log')
        return 2

    TOKEN = desktop_login(port)
    print(f'[desktop] 后端就绪: {URL}')

    window = webview.create_window(config.APP_TITLE, URL, width=1440, height=920, min_size=(1024, 700))

    def on_loaded():
        if TOKEN:
            window.evaluate_js(f'window.localStorage.setItem("xywb_token", {TOKEN!r});')

    window.events.loaded += on_loaded

    tray = start_tray(lambda: os._exit(0))  # noqa: SLF001 - 托盘退出直接终止进程
    try:
        webview.start()
    finally:
        if tray is not None:
            tray.visible = False
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
