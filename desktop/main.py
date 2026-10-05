"""桌面外壳：起后端线程 + WebView2 窗口 + 托盘。

用法（开发，仓库根目录下）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe desktop\\main.py"

无窗口模式（无头/自检/打包后验证，不加载 pywebview）：
    desktop\\main.py --no-window --port 18080

打包成 exe 见 scripts/build_exe.ps1。窗口关掉不退出，托盘常驻（点托盘「退出」才停）。

路径说明（打包后 sys.frozen 为真）：
- 后端代码以 `backend` 包形式收进 _MEIPASS，这里把 _MEIPASS 加进 sys.path。
- 前端产物作为 data 收在 _MEIPASS/frontend/dist，这里覆盖 config.FRONTEND_DIST
  指过去（core/config.py 在 frozen 下算不出打包内路径，外壳侧覆盖即可，不动 core）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _resolve_base() -> Path:
    """返回「包含 backend 包」的目录：打包后是 _MEIPASS，开发态是仓库根。"""
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


BASE = _resolve_base()
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backend.app import main as backend_main  # noqa: E402
from backend.app.core import config, security  # noqa: E402

# frozen 下前端产物在打包目录内，覆盖 core/config.py 算出来的开发态路径
if getattr(sys, 'frozen', False):
    config.FRONTEND_DIST = BASE / 'frontend' / 'dist'

URL = ''
TOKEN = ''


def _configure_logging() -> None:
    """打包后没有控制台：日志落 APP_DIR/logs/app.log，出错时有据可查。"""
    try:
        config.ensure_dirs()
        from loguru import logger

        logger.add(
            str(config.LOG_PATH),
            rotation='5 MB',
            retention=5,
            encoding='utf-8',
            enqueue=True,
        )
    except Exception as exc:  # noqa: BLE001 - 日志失败不该拦住启动
        print(f'[desktop] 日志初始化失败: {exc}')


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


def start_tray(on_quit, on_show):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        print(f'[desktop] 未安装 pystray/Pillow，跳过托盘（{exc}）')
        return None

    image = Image.new('RGB', (64, 64), '#0f172a')
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill='#38bdf8')
    draw.text((22, 22), '闲', fill='#0f172a')

    def quit_action(icon, item):  # noqa: ARG001
        icon.visible = False
        icon.stop()
        on_quit()

    def show_action(icon, item):  # noqa: ARG001
        on_show()

    menu = pystray.Menu(
        pystray.MenuItem('打开窗口', show_action, default=True),
        pystray.MenuItem('数据目录', lambda icon, item: os.startfile(config.APP_DIR)),  # noqa: SIM115
        pystray.MenuItem('退出', quit_action),
    )
    icon = pystray.Icon('xianyu-ai-workbench', image, config.APP_TITLE, menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def start_backend(port: int) -> threading.Thread:
    thread = threading.Thread(
        target=backend_main.run_server,
        kwargs={'host': config.HOST, 'port': port},
        daemon=True,
        name='xywb-backend',
    )
    thread.start()
    return thread


def run_headless(port: int) -> int:
    """只起后端、不开窗口（打包产物自检 / 无头环境用）。阻塞直到 Ctrl+C。"""
    _configure_logging()
    start_backend(port)
    if not wait_health(port):
        print('[desktop] 后端启动超时，请查看 logs/app.log')
        return 2
    print(f'[desktop] 后端就绪（无窗口模式）: http://{config.HOST}:{port}/')
    print(f'[desktop] 健康检查: http://{config.HOST}:{port}/api/health')
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print('[desktop] 收到 Ctrl+C，退出')
    return 0


def main(argv: list[str] | None = None) -> int:
    global URL, TOKEN

    parser = argparse.ArgumentParser(description=config.APP_TITLE)
    parser.add_argument('--port', type=int, default=0, help='后端端口，0=自动挑空闲端口')
    parser.add_argument('--no-window', action='store_true', help='只起后端，不开 WebView 窗口')
    parser.add_argument('--no-tray', action='store_true', help='不启动托盘')
    args = parser.parse_args(argv)

    port = args.port or backend_main.pick_free_port()

    if args.no_window:
        return run_headless(port)

    try:
        import webview
    except ImportError:
        print('缺少 pywebview：C:\\Python310\\python.exe -m pip install pywebview pystray')
        return 1

    _configure_logging()
    URL = f'http://{config.HOST}:{port}/'

    start_backend(port)
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

    def show_window():
        try:
            window.show()
            window.restore()
        except Exception as exc:  # noqa: BLE001 - 窗口可能已被销毁
            print(f'[desktop] 打开窗口失败: {exc}')

    tray = None if args.no_tray else start_tray(lambda: os._exit(0), show_window)  # noqa: SLF001
    try:
        webview.start()
    finally:
        if tray is not None:
            tray.visible = False
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
