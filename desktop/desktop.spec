# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：闲鱼 AI 工作台（目录形态 onedir）。

由 scripts/build_exe.ps1 调用：
    C:\\Python310\\python.exe -m PyInstaller --noconfirm --clean desktop\\desktop.spec

要点：
- 入口 desktop/main.py；仓库根加进 pathex，让 `backend` 包可被分析到。
- frontend/dist 作为 data 收进产物的 `frontend/dist`（即 `_internal/frontend/dist`），
  后端 core/config.py 的 `resolve_frontend_dist()` 第一顺位就找 `_MEIPASS/frontend/dist`。
- 路由模块是 importlib 动态导入的，静态分析看不到 → 用 collect_submodules('backend') 兜住。
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(os.environ.get('XYWB_REPO_ROOT') or Path(SPECPATH).resolve().parents[1]).resolve()
# XYWB_FRONTEND_DIST 仅用于自检/冒烟打包（正常情况下不用设）
FRONTEND_DIST = Path(os.environ.get('XYWB_FRONTEND_DIST') or REPO_ROOT / 'frontend' / 'dist')

if not (FRONTEND_DIST / 'index.html').exists():
    raise SystemExit(
        f'前端产物缺失: {FRONTEND_DIST / "index.html"}\n先跑: pwsh scripts\\build_frontend.ps1'
    )

datas = [(str(FRONTEND_DIST), 'frontend/dist')]

hiddenimports = [
    'backend',
    'backend.app',
    'backend.app.core',
    'backend.app.core.config',
    'backend.app.core.db',
    'backend.app.core.events',
    'backend.app.core.security',
]
hiddenimports += collect_submodules('backend')
hiddenimports += collect_submodules('uvicorn')

for optional_pkg in ('webview', 'pystray', 'PIL', 'patchright', 'loguru'):
    try:
        hiddenimports += collect_submodules(optional_pkg)
    except Exception as exc:  # noqa: BLE001 - 可选依赖没装就跳过
        print(f'[spec] 跳过可选包 {optional_pkg}: {exc}')

# 不打包 tkinter/Qt 那套（pywebview 在 Windows 走 WebView2/winforms）
excludes = [
    'tkinter',
    'PyQt5',
    'PyQt6',
    'PySide2',
    'PySide6',
    'matplotlib',
    'pytest',
    'IPython',
    'notebook',
]

a = Analysis(
    [str(REPO_ROOT / 'desktop' / 'main.py')],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='闲鱼AI工作台',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='闲鱼AI工作台',
)
