"""全局配置与路径。桌面应用形态：所有可写数据放在 %APPDATA%\\xianyu-ai-workbench。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = 'xianyu-ai-workbench'
APP_TITLE = '闲鱼 AI 工作台'
VERSION = '0.1.0'

# 打包成 exe 后 sys.frozen 为真，此时用 exe 所在目录旁边的 data（便携）还是 APPDATA？
# 选择 APPDATA：exe 可能放在 Program Files（不可写）。
_APPDATA = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
# 测试/CI 用 XIANYU_APP_DIR 把**整个数据目录**挪到临时位置。
# 只改 DB_PATH 是不够的：desktop_credentials.json 会写真实 APPDATA，把生产管理员的
# 密码覆盖成临时库的随机密码，导致桌面自动登录 500（见 docs/PACKAGING.md 6.5）。
APP_DIR = Path(os.environ['XIANYU_APP_DIR']) if os.environ.get('XIANYU_APP_DIR') else Path(_APPDATA) / APP_NAME
DATA_DIR = APP_DIR / 'data'
LOGS_DIR = APP_DIR / 'logs'
BROWSER_DIR = APP_DIR / 'browser_profiles'   # Playwright 持久化 profile

DB_PATH = DATA_DIR / 'app.db'
LOG_PATH = LOGS_DIR / 'app.log'
SECRET_PATH = DATA_DIR / 'local_secret.txt'
CRED_PATH = DATA_DIR / 'desktop_credentials.json'
MCP_TOKEN_PATH = DATA_DIR / 'mcp_token.txt'
# 桌面形态后端端口是随机挑的，MCP 插件靠这个文件找到它（固定 8080 连不上）。
PORT_PATH = DATA_DIR / 'backend_port.txt'

# 开发态（未打包）时前端源码目录，用于提示构建
REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIST = REPO_ROOT / 'frontend' / 'dist'


def resolve_frontend_dist() -> Path:
    """前端产物目录。

    打包后资源被解到 `sys._MEIPASS`（PyInstaller --onedir 时就是 exe 同级目录），
    打包时 frontend/dist 会被放到 `_MEIPASS/frontend/dist`。
    找不到就退回源码目录，让开发态行为不变。
    """
    candidates = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(Path(meipass) / 'frontend' / 'dist')
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).resolve().parent / 'frontend' / 'dist')
    candidates.append(FRONTEND_DIST)
    for path in candidates:
        if (path / 'index.html').exists():
            return path
    return candidates[0]

HOST = '127.0.0.1'
DEFAULT_PORT = 0            # 0 = 让内核挑空闲端口（桌面应用必须，避免和别的东西撞）
BACKEND_READY_TIMEOUT = 30  # 秒

# 本地会话（网页登录）有效期，与旧项目一致：空闲 24h 滑动、绝对 30 天
SESSION_IDLE_SECONDS = 24 * 60 * 60
SESSION_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60

DEFAULT_SETTINGS = {
    'ai.enabled': 'true',
    'ai.default_provider_id': '',
    'chat.auto_reply_scope': 'all',       # all|items_only|off
    'chat.human_takeover_seconds': '0',   # 人工回复后 N 秒内 AI 不插话，0=不限制
    'risk.auto_slider': 'false',          # 自动滑块默认关（旧项目实测 37 次 0 成功）
    'risk.cooldown_steps': '60,180,300,600,1200',
    'risk.refresh_interval_seconds': '1200',
    'ui.theme': 'light',
}


def ensure_dirs() -> None:
    for path in (APP_DIR, DATA_DIR, LOGS_DIR, BROWSER_DIR):
        path.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    """日志同时写文件（排障用）与控制台（开发用）；桌面模式下控制台不可见也无妨。"""
    import sys as _sys

    from loguru import logger

    ensure_dirs()
    logger.remove()
    logger.add(str(LOG_PATH), rotation='10 MB', retention=5, encoding='utf-8', level='DEBUG')
    logger.add(_sys.stderr, level='INFO', colorize=False)


def is_frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))
