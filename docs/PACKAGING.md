# 打包与运行（闲鱼 AI 工作台）

> 负责人：桌面外壳工作流（task-5）。写入范围：`desktop/**`、`scripts/**`、`docs/PACKAGING.md`、`desktop/desktop.spec`。

> **脚本怎么跑**：文档里统一写 `pwsh scripts\xxx.ps1`，但**本机没装 PowerShell 7**（只有 Windows PowerShell 5.1）。
> 本机等价写法是 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\xxx.ps1`。
> 所有 `.ps1` 都带 UTF-8 BOM 且兼容 PS 5.1 —— **改这些脚本时务必保留 BOM**，
> 否则 PS 5.1 在 GBK 代码页下会把中文注释解析成语法错误（`字符串缺少终止符`）。

## 0. 形态

- Windows 桌面应用，**PyWebView（WebView2）+ pystray 托盘**，后端 FastAPI/uvicorn 起在 `127.0.0.1:<随机空闲端口>`。
- 打包用 **onedir（目录形态）**，不是 onefile：启动快、便于热更（替换 `_internal` 里的文件即可）。
- 产物：`dist\闲鱼AI工作台\闲鱼AI工作台.exe`。
- 用户数据一律在 `%APPDATA%\xianyu-ai-workbench\`（`data/app.db`、`logs/app.log`、`browser_profiles/`），**不在安装目录**，所以 exe 放 Program Files 也能跑。

## 1. 装依赖

```powershell
C:\Python310\python.exe -m pip install -r backend\requirements.txt
# 桌面外壳 + 打包（requirements.txt 里已含）
C:\Python310\python.exe -m pip install pywebview pystray pyinstaller
```

实测版本（2026-10-06）：

| 包 | 版本 |
|---|---|
| pywebview | 6.2.1 |
| pystray | 0.19.5 |
| pyinstaller | 6.22.3 |
| pyinstaller-hooks-contrib | 2026.8 |
| pythonnet | 3.1.0 |
| Pillow | 12.3.0 |

WebView2 运行时：**154.0.4258.53（已装）**。pywebview 6.x 在 Windows 走 pythonnet + WebView2/winforms。

## 2. 构建前端

```powershell
pwsh scripts\build_frontend.ps1            # npm install + npm run build
pwsh scripts\build_frontend.ps1 -SkipInstall   # 跳过 npm install
```

产物 `frontend\dist\index.html`（+ `assets/`）。后端启动时若发现该目录存在就静态托管它（`app.mount('')`，空挂载点让 API 路由优先），否则给占位页。

## 3. 打包

```powershell
pwsh scripts\build_exe.ps1                 # 先构建前端，再 PyInstaller
pwsh scripts\build_exe.ps1 -SkipFrontend   # 前端已构建过，只打包
```

内部命令等价于：

```powershell
$env:XYWB_REPO_ROOT = (Get-Location).Path
C:\Python310\python.exe -m PyInstaller --noconfirm --clean `
  --workpath build --distpath dist desktop\desktop.spec
```

`desktop\desktop.spec` 做的事：

1. 入口 `desktop/main.py`，`pathex` 加仓库根（这样 `backend` 包能被分析到）。
2. **`frontend/dist` 作为 data 收进产物** → 运行时位于 `_internal\frontend\dist`。
3. 路由模块是 `importlib` 动态导入的，静态分析看不到 → `collect_submodules('backend')` 兜住。
4. 排除 `tkinter`/`PyQt*`/`matplotlib`/`pytest` 等，减小体积。
5. 若 `frontend\dist\index.html` 不存在，spec 直接报错退出（不会打出一个没有界面的包）。

> ✅ `.gitignore` 里虽有 `*.spec`，但已补了 `!desktop/desktop.spec`（`.gitignore:11-12`），
> `desktop/desktop.spec` 与 `desktop/main.py` 都已入库（`git check-ignore` 对 spec 退出码 1 = 未被忽略）。

冒烟打包（不依赖真实前端，只验证打包链路 + frozen 路径）：

```powershell
$env:XYWB_FRONTEND_DIST = "$env:TEMP\xywb_fake_dist"   # 里面放个 index.html 即可
C:\Python310\python.exe -m PyInstaller --noconfirm --clean `
  --workpath build_smoke --distpath dist_smoke desktop\desktop.spec
```

## 4. 运行与自检

双击 `dist\闲鱼AI工作台\闲鱼AI工作台.exe`：起后端线程 → 等 `/api/health` → 开窗口 → 托盘常驻。关窗口不退出，托盘「退出」才停。

无窗口模式（无头环境 / 自检 / CI）：

```powershell
& 'dist\闲鱼AI工作台\闲鱼AI工作台.exe' --no-window --port 18080
Invoke-WebRequest http://127.0.0.1:18080/api/health -UseBasicParsing
```

实测输出（2026-10-06，打包产物 `--no-window --port 18095`）：

```
GET /api/health            -> 200  {"success":true,"data":{"ok":true,"version":"0.1.0","frozen":true}}
GET /                      -> 200  LEN=397  （真 index.html，非占位页）
GET /assets/index-BK87FCtW.js -> 200  LEN=237390
GET /assets/index-7Nah3lHC.css -> 200  LEN=14802
GET /api/settings          -> 401  （API 路由未被静态挂载吃掉）
POST /api/auth/desktop     -> 200  success=True username=admin token_len=43
```

其它参数：`--port N`（默认 0=自动挑空闲端口）、`--no-tray`。

> ⚠️ **`--no-window` 与「双击」**：打包产物 `console=False`（PE Subsystem = WINDOWS_GUI），
> 被 Explorer/`start`/计划任务这类**无控制台句柄**的方式启动时 `sys.stdout`/`sys.stderr` 是 `None`。
> uvicorn 的日志配置会执行 `sys.stdout.isatty()` → `AttributeError` → 后端起不来且**没有任何输出**
> （现象：「双击后过 30 秒什么都没发生」）。`desktop/main.py` 顶部已在任何后端 import 之前把这两个流
> 补成 `os.devnull`，并把关键进度同时写进 `logs/app.log`。若将来再出现「起来了但没反应」，
> **先看 `%APPDATA%\xianyu-ai-workbench\logs\app.log`**，而不是只看 stdout。

开发态等价命令：

```powershell
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe desktop\main.py"
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe desktop\main.py --no-window --port 18080"
```

## 5. 脚本一览（`scripts\`）

| 脚本 | 作用 |
|---|---|
| `_common.ps1` | 公共变量/函数（dot-source，不单独跑）；`XYWB_PYTHON` 可覆盖 Python 路径 |
| `dev_backend.ps1` | 开发态后端（默认 8080） |
| `dev_frontend.ps1` | Vite dev server（5173） |
| `run_dev.ps1` | 一键：前端 dev + 桌面窗口；`-WithBackend` 另起独立后端 |
| `build_frontend.ps1` | `npm install` + `npm run build` → `frontend/dist` |
| `build_exe.ps1` | 构建前端 + PyInstaller onedir 打包 |

## 6. 路径与 frozen 说明

- 开发态：`config.FRONTEND_DIST = <repo>\frontend\dist`。
- **frozen 态**：由 `backend/app/core/config.py::resolve_frontend_dist()` 决定，依次找：
  1. `sys._MEIPASS / frontend / dist`（onedir 下就是 `_internal\frontend\dist`，**打包时的落点**）；
  2. exe 同级 `frontend / dist`（便携/热更：把新前端丢在 exe 旁边即可覆盖，不用重打包）；
  3. 退回源码目录 `config.FRONTEND_DIST`（开发态行为不变）。
- `backend/app/main.py:140-142` 调用的就是这个函数，并且**挂载点用空字符串** `app.mount('', ...)`
  （不是 `'/'`：挂 `'/'` 会匹配所有路径，实测让 `app.openapi()` 里只剩 14 条路由）。
  所以 frozen 下 `create_app()` 一开始挂的就是打包内的真前端，**外壳不需要覆盖 `config.FRONTEND_DIST`，
  也不需要重建挂载**。
- 外壳 `desktop/main.py` 只做一件 frozen 相关的事：把 `_MEIPASS` 加进 `sys.path`，让 `backend` 包可导入。

```text
[desktop] 启动中 frozen=True 前端目录=...\_internal\frontend\dist
HEALTH 200: {"success":true,"data":{"ok":true,"version":"0.1.0","frozen":true}}
ROOT 200: <!doctype html>（真 index.html）
```

> 历史记录：早期外壳曾自己覆盖 `FRONTEND_DIST` 并重建 `app.mount('/')`（`_ensure_frontend_mount()`）。
> 那是在 `resolve_frontend_dist()` 落地**之前**写的权宜之计，现已删除 —— 以 `backend/app/main.py` 的实现为准。

## 6.5 ✅ 历史缺陷：跑测试曾破坏桌面自动登录（**已修**，提交 `21001c2`）

> **状态：已修复。** 下面记的是问题本身与当时的证据，保留作历史；「临时恢复办法」**已不需要**。

**现象**（修复前）：打包产物第一次跑自动登录正常；跑过一次 `backend/tests/test_skeleton.py` 后，再启动产物，`POST /api/auth/desktop` 返回
`{"success":false,"message":"管理员账号异常","code":"http_error"}`（HTTP 500），窗口打开后是未登录状态。

**机制**（`backend/tests/test_skeleton.py:24-47` + `backend/app/core/security.py:40-61`）：

1. 测试的 `setUpClass` 只把 `config.DB_PATH` / `config.MCP_TOKEN_PATH` 指到临时目录，
   **没有改 `config.CRED_PATH`**（= `%APPDATA%\xianyu-ai-workbench\data\desktop_credentials.json`）。
2. 临时库里没有 admin，测试触发 `security.ensure_admin()` → 走「新建管理员」分支 →
   生成**随机新密码**并 `cred_path.write_text(...)`，**覆盖了真实的凭据文件**。
3. 真实 `app.db` 里的 admin 密码还是旧的，而凭据文件已经是新密码 →
   凭据与库 hash 不一致 → 桌面自动登录 500。

**实测证据**（2026-10-06，修复前）：

```text
运行 test_skeleton.py 前: {"username":"admin","password":"fiOQH6s-8QIn"}
运行后                   : {"username":"admin","password":"bS5m9fPJ46UH"}
该密码与真实库 hash 匹配 : False
ensure_admin() 返回      : ('admin', 'bS5m9fPJ46UH')
authenticate() 结果      : None      ← 就是 500 的来源
```

**已落地的修复**（提交 `21001c2`，属 `backend/**`）：

- `core/config.py` 新增 `XIANYU_APP_DIR` 环境变量，可把**整个数据目录**挪走
  （只改 `DB_PATH` 不够，正是根因：`CRED_PATH`/`SECRET_PATH`/`PORT_PATH` 都还在真实 APPDATA）。
- 新增 `backend/tests/testenv.py::use_temp_app_dir()`，`test_skeleton.py` 改用它；
  `run_all.py` 给每个测试子进程一个独立 `XIANYU_APP_DIR`。
- `security.ensure_admin()` 在凭据验不过时**重生成一对并同时落库 + 落盘**（自愈），
  不再无条件信任那个可能来自别的库的凭据文件；新增 `security.desktop_user()`，
  `POST /api/auth/desktop`（`backend/app/api/auth.py:34`）改用它 ——
  原来的 `authenticate(*ensure_admin())` 在「密码本来就一致」时拿不到明文，形状本身就是缺陷。
- 补了 2 条回归测试（凭据被覆盖 / 凭据缺失，都必须 200，且新密码能真登录、旧密码失效）。

**临时恢复办法**（历史记录，**已不需要**，仅在遇到更早的产物时参考）：

```powershell
# 关掉所有产物进程，然后删掉凭据文件 + 清空 users/sessions，下次启动会重新生成一对一致的
$d = "$env:APPDATA\xianyu-ai-workbench\data"
Remove-Item "$d\desktop_credentials.json"
C:\Python310\python.exe -c "import sqlite3,os;c=sqlite3.connect(os.path.join(os.environ['APPDATA'],'xianyu-ai-workbench','data','app.db'));c.execute('DELETE FROM sessions');c.execute('DELETE FROM users');c.commit()"
```

## 7. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 窗口白屏 / 打不开 | 缺 **WebView2 运行时**。装 [Evergreen Bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/)，本机已装 154.0.4258.53 |
| 双击 exe 没反应、没日志 | `console=False`，看不到 stdout。**先看 `%APPDATA%\xianyu-ai-workbench\logs\app.log`**（外壳的启动/超时信息都写这里）；排查时也可用 `--no-window --port 18080` |
| **窗口开了但没登录 / `管理员账号异常`** | 历史缺陷，**已修**（提交 `21001c2`）—— 见 6.5 节。若仍出现，看 `logs\app.log` 与 6.5 的诊断步骤 |
| 杀毒报毒 / 被删 | PyInstaller 无签名 exe 常见误报。加白名单；正式分发要买代码签名证书 |
| 路径带空格 / 中文 | 脚本全部用 `Join-Path` + 引号；spec 里用 `Path` 对象，支持 `C:\Program Files\...` 与中文路径。**实测打包目录名就是中文「闲鱼AI工作台」，可正常工作** |
| 打包后 `/` 是占位页 / 白屏但 API 正常 | 前端没进产物。检查 `dist\闲鱼AI工作台\_internal\frontend\dist\index.html`；`build_exe.ps1` 会逐文件（大小 + SHA256）比对快照与产物，不一致直接报错 |
| 打包时前端目录突然变空 | 前端工作流并发跑了 `npm run build`（vite 先清空 dist）。`build_exe.ps1` 已先把 dist **快照**到 `build\frontend_stage` 再打包，规避了这个竞态 |
| `ModuleNotFoundError: backend` | 打包时 `pathex` 没加仓库根，或运行时 `sys.path` 没加 `_MEIPASS`（本仓库两者都已处理） |
| 路由 404（新模块没注册） | `backend/app/main.py` 的 `ROUTE_MODULES` 名单由 Lead 维护，新增模块要请 Lead 加 |
| 打包体积大（~190MB） | 主要是 playwright / patchright 的浏览器 driver（含 `node.exe` 约 92MB，占一半）/ pythonnet / WebView2 依赖；已排除 tkinter/Qt。**注意别收两份 driver**：见 8 节的 hook 覆盖 |
| 打包版点「手动验证」报 `[WinError 2] 系统找不到指定的文件` | 漏收 playwright driver 这个**数据文件**（`_internal\playwright\driver\node.exe` + `package\cli.js`）。`desktop/desktop.spec` 已 `collect_data_files('playwright')`；`backend/app/xianyu/captcha.py::_driver_problem()` 现在会在弹窗之前就给出人话 |
| 端口被占 | 桌面模式默认 `port=0` 自动挑空闲端口，正常不会撞；`--port` 指定端口时才会 |

## 8. 验收记录

2026-10-06，一键复现（PowerShell 5.1；本机没有 pwsh 7，`powershell.exe -File` 等价）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

- 前端：`tsc --noEmit && vite build` → `dist/index.html` 407 B、`assets/index-7Nah3lHC.css` 14802 B、`assets/index-BK87FCtW.js` 237390 B
- 快照 3 个文件 246.7 KB → PyInstaller onedir → `dist\闲鱼AI工作台\闲鱼AI工作台.exe` **9.06 MB**（9495613 字节），产物目录合计 **189.8 MB**
- 产物内前端与快照**逐文件一致**（大小 + SHA256）
- 产物启动（真·双击等价，`UseShellExecute=$true` 无控制台句柄）`--no-window --port 18095`：
  `/api/health` 200 `frozen:true`、`/` 200 真 index.html、旧资源 `index-CFmNBGgU.js` 404、
  `/api/settings` 401、`POST /api/auth/desktop` 200（`username=admin`, token 43 字符）
- 开发态 `python desktop\main.py --no-window --port 18098`：`/api/health` 200 `frozen:false`、`/` 200

### 8.1 浏览器 driver：必须收，但只能收一份

2026-10-08 实测（`-SkipFrontend` 打包，`exe` 9808684 字节 / 产物目录 **190.3 MB**）：

- **必须收**：driver 是 `node.exe` + `package/cli.js`，属**数据文件**不是模块，只
  `collect_submodules` 收不到。漏了的表现是打包版点「手动验证」直接
  `[WinError 2] 系统找不到指定的文件`（真机 `operation_logs` id=165~171 连续 7 条），
  而源码直跑正常。`desktop/desktop.spec` 因此加了
  `datas += collect_data_files(browser_pkg)`。
- **只能收一份**：`patchright` 在 site-packages 里注册了同名 hook
  （`hook-playwright.async_api.py` / `hook-playwright.sync_api.py`，内容是
  `datas = collect_data_files("patchright")`）。只要分析到 `playwright.async_api`，
  它就把 **patchright 的 driver** 也收进来 —— 而打包版运行时只用 `playwright`
  （patchright 只有源码里跑的 `scripts/ui_smoke.py` / `live_refresh_smoke.py` 用）。
  收两份的实测代价：`dist` **292.6 MB**，`_internal\playwright\driver` 与
  `_internal\patchright\driver` 各 102.3 MB。
- **覆盖办法**：`desktop/hooks/hook-playwright.{async_api,sync_api}.py` 里只写
  `datas = collect_data_files('playwright')`，spec 里
  `hookspath=[str(REPO_ROOT / 'desktop' / 'hooks')]`。构建日志会显示
  `Processing standard module hook 'hook-playwright.async_api.py' from
  '...\xianyu-ai-workbench\desktop\hooks'`（注意来源是本仓库而不是 site-packages）。
  结果 `_internal\patchright\driver` 不再存在，体积回到 190.3 MB，
  `_internal\playwright\driver\node.exe` 与 `package\cli.js` 都在 —— 正是
  `backend/app/xianyu/captcha.py::_driver_problem()` 检查的两个文件。
- 教训：onedir 模式下 exe 只有 9.8 MB，**体积全在 `_internal`**；谈「产物多大」
  要量整个目录，只看 exe 会得出错误结论。
