"""覆盖 patchright 注册的同名 hook。

`patchright` 这个包在它自己的 `__pyinstaller/` 目录里注册了
`hook-playwright.async_api.py` / `hook-playwright.sync_api.py`，内容是
`datas = collect_data_files("patchright")` —— 只要分析到 `playwright.async_api`
（应用运行时要它），PyInstaller 就会把 **patchright 的 driver** 收进来，
多出一份 102 MB 的 `node.exe`，而打包版根本不用 patchright
（只有源码里跑的 `scripts/ui_smoke.py` / `live_refresh_smoke.py` 用它）。

本目录通过 spec 的 `hookspath` 放在前面，于是这个同名 hook 生效，
只收应用真正要用的 `playwright` driver。
"""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files('playwright')
