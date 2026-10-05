"""测试用的数据目录隔离。

**为什么需要**：`config` 的路径常量在 import 时就算好了。历史上测试只把 `DB_PATH`
指向临时目录，`CRED_PATH` 还是真实 `%APPDATA%\\xianyu-ai-workbench\\data\\desktop_credentials.json`
—— 临时库里没有 admin 时 `security.ensure_admin()` 会生成随机密码并**覆盖真实凭据文件**，
导致桌面外壳自动登录 500「管理员账号异常」（详见 `docs/PACKAGING.md` 6.5）。

用法（测试文件顶部，import config 之后）：

    from backend.tests.testenv import use_temp_app_dir
    ...
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        use_temp_app_dir(cls.tmp.name)

`XIANYU_APP_DIR` 环境变量优先级最高，`run_all.py` 用它给每个测试子进程一个独立目录。
"""
from __future__ import annotations

import os
from pathlib import Path

_MARKER = 'XIANYU_APP_DIR'


def use_temp_app_dir(root: str | Path) -> Path:
    """把整个应用数据目录（DB/凭据/密钥/日志/浏览器 profile）挪到 root。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    os.environ[_MARKER] = str(root)

    from backend.app.core import config, db

    config.APP_DIR = root
    config.DATA_DIR = root / 'data'
    config.LOGS_DIR = root / 'logs'
    config.BROWSER_DIR = root / 'browser_profiles'
    config.DB_PATH = config.DATA_DIR / 'app.db'
    config.LOG_PATH = config.LOGS_DIR / 'app.log'
    config.SECRET_PATH = config.DATA_DIR / 'local_secret.txt'
    config.CRED_PATH = config.DATA_DIR / 'desktop_credentials.json'
    config.MCP_TOKEN_PATH = config.DATA_DIR / 'mcp_token.txt'
    config.ensure_dirs()

    db.reset_connection()
    db.init_db()
    return root


def release_temp_app_dir() -> None:
    os.environ.pop(_MARKER, None)
