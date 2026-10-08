"""与 `hook-playwright.async_api.py` 同理：挡住 patchright 的 driver。

同步 API 这份是给「谁 import 了 `playwright.sync_api`」兜底的 —— 目前应用运行时
不用它，但少了这个文件的话，patchright 的同名 hook 又会把 102 MB 收回来。
"""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files('playwright')
