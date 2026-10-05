# 打包与运行（闲鱼 AI 工作台）

> 负责人：桌面外壳工作流（task-5）。写入范围：`desktop/**`、`scripts/**`、`docs/PACKAGING.md`、`desktop/desktop.spec`。

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

产物 `frontend\dist\index.html`（+ `assets/`）。后端启动时若发现该目录存在就静态托管它（`app.mount('/')`），否则给占位页。

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

> ⚠️ `.gitignore` 里有 `*.spec` —— `desktop/desktop.spec` 是打包的唯一配置，必须进仓库。
> 需要 Lead 在 `.gitignore` 补一行 `!desktop/desktop.spec`（或删掉 `*.spec`）。

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

实测输出：

```
StatusCode: 200
{"success":true,"data":{"ok":true,"version":"0.1.0","frozen":true}}
```

其它参数：`--port N`（默认 0=自动挑空闲端口）、`--no-tray`。

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

## 6. 路径与 frozen 说明（外壳侧解决，**不需要改 `backend/app/core/config.py`**）

- 开发态：`config.REPO_ROOT` 由 `backend/app/core/config.py` 的 `__file__` 推出（仓库根），`FRONTEND_DIST = <repo>\frontend\dist`。
- **frozen 态**：`core/config.py` 的 `REPO_ROOT` 会算到 `_internal\backend\app\core` 往上三级，指不到打包内的前端目录。`desktop/main.py` 因此做了两件事：
  1. 把 `sys.path` 加上 `_MEIPASS`，让 `backend` 包可导入；
  2. 把 `config.FRONTEND_DIST` 覆盖成 `_MEIPASS / 'frontend' / 'dist'`。
- 但 `backend/app/main.py` 末尾是**模块级** `app = create_app()`，import 时就执行完 —— 那时 `FRONTEND_DIST` 还是开发态路径（frozen 下不存在），应用挂的是**占位页**路由。
  所以外壳在覆盖之后调用 `_ensure_frontend_mount()`：丢掉占位路由/旧静态挂载，重新 `app.mount('/', StaticFiles(dist, html=True))`。
- 实测（冒烟打包，假 dist）：frozen exe 起来后 `/` 返回的就是打包内那个 `index.html`，不是占位页。

```text
[desktop] 前端静态托管: ...\_internal\frontend\dist
HEALTH 200: {"success":true,"data":{"ok":true,"version":"0.1.0","frozen":true}}
ROOT 200: <!doctype html><title>FAKE-DIST-OK</title>
```

> 若将来 Lead 愿意改 `backend/app/**`，更干净的做法是在 `create_app()` 里判断 `sys.frozen` 后直接读 `sys._MEIPASS`；届时外壳这两行可以删。

## 6.5 ⚠️ 已知缺陷：跑测试会破坏桌面自动登录（已复现，需 Lead 修 `backend/**`）

**现象**：打包产物第一次跑自动登录正常；跑过一次 `backend/tests/test_skeleton.py` 后，再启动产物，`POST /api/auth/desktop` 返回
`{"success":false,"message":"管理员账号异常","code":"http_error"}`（HTTP 500），窗口打开后是未登录状态。

**机制**（`backend/tests/test_skeleton.py:24-47` + `backend/app/core/security.py:40-61`）：

1. 测试的 `setUpClass` 只把 `config.DB_PATH` / `config.MCP_TOKEN_PATH` 指到临时目录，
   **没有改 `config.CRED_PATH`**（= `%APPDATA%\xianyu-ai-workbench\data\desktop_credentials.json`）。
2. 临时库里没有 admin，测试触发 `security.ensure_admin()` → 走「新建管理员」分支 →
   生成**随机新密码**并 `cred_path.write_text(...)`，**覆盖了真实的凭据文件**。
3. 真实 `app.db` 里的 admin 密码还是旧的，而凭据文件已经是新密码 →
   `desktop_login()` 里 `security.authenticate(*security.ensure_admin())` 返回 `None` → 500。

**实测证据**（2026-10-06）：

```text
运行 test_skeleton.py 前: {"username":"admin","password":"fiOQH6s-8QIn"}
运行后                   : {"username":"admin","password":"bS5m9fPJ46UH"}
该密码与真实库 hash 匹配 : False
ensure_admin() 返回      : ('admin', 'bS5m9fPJ46UH')
authenticate() 结果      : None      ← 就是 500 的来源
```

**影响面**：任何人在开发机上跑过 `backend/tests/*.py`（`test_agents_cross.py`、`test_agents_providers.py`、
`test_agents_prompts.py` 都直接 `INSERT INTO users` 再 `DELETE FROM users`，同样会留下不一致）之后，
桌面自动登录就坏了 —— 而用户看到的是「窗口打开了但没登录」，很难联想到是测试造成的。

**临时恢复办法**（不改代码，用户侧）：

```powershell
# 关掉所有产物进程，然后删掉凭据文件 + 清空 users/sessions，下次启动会重新生成一对一致的
$d = "$env:APPDATA\xianyu-ai-workbench\data"
Remove-Item "$d\desktop_credentials.json"
C:\Python310\python.exe -c "import sqlite3,os;c=sqlite3.connect(os.path.join(os.environ['APPDATA'],'xianyu-ai-workbench','data','app.db'));c.execute('DELETE FROM sessions');c.execute('DELETE FROM users');c.commit()"
```

实测该恢复路径有效：`users=0` + 无凭据文件时启动，`ensure_admin()` 返回 `('admin','t0-PGPb9Idx_')`，
`authenticate()` 返回 `{'id': 2, 'username': 'admin', 'is_admin': True}`，`POST /api/auth/desktop` → HTTP 200。

**建议的正规修法**（属 `backend/**`，归 Lead，外壳不动）：

- 测试侧：`setUpClass` 里把 `config.CRED_PATH` 也指到临时目录（和 `DB_PATH` 一样），
  并在 `tearDownClass` 还原；或干脆用环境变量把整个 `APP_DIR` 指到临时目录。
- 生产侧（更稳）：`security.ensure_admin()` 在「凭据文件里的密码验不过」时不要静默返回坏密码，
  应该把 admin 密码**重置成凭据文件里的那个**（`UPDATE users SET password_hash=?`），让两边自愈。

## 7. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 窗口白屏 / 打不开 | 缺 **WebView2 运行时**。装 [Evergreen Bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/)，本机已装 154.0.4258.53 |
| 双击 exe 没反应、没日志 | `console=False`，看不到 stdout。看 `%APPDATA%\xianyu-ai-workbench\logs\app.log`；排查时先用 `--no-window --port 18080` 跑 |
| **窗口开了但没登录 / `管理员账号异常`** | 跑过 `backend/tests/*.py` 导致凭据文件被覆盖 —— 见上面 6.5 节 |
| 杀毒报毒 / 被删 | PyInstaller 无签名 exe 常见误报。加白名单；正式分发要买代码签名证书 |
| 路径带空格 / 中文 | 脚本全部用 `Join-Path` + 引号；spec 里用 `Path` 对象，支持 `C:\Program Files\...` 与中文路径 |
| `ModuleNotFoundError: backend` | 打包时 `pathex` 没加仓库根，或运行时 `sys.path` 没加 `_MEIPASS`（本仓库两者都已处理） |
| 路由 404（新模块没注册） | `backend/app/main.py` 的 `ROUTE_MODULES` 名单由 Lead 维护，新增模块要请 Lead 加 |
| 打包体积大（~150MB+） | 主要是 playwright / pythonnet / WebView2 依赖；已排除 tkinter/Qt。真要瘦身先去掉 playwright 再打 |
| 端口被占 | 桌面模式默认 `port=0` 自动挑空闲端口，正常不会撞；`--port` 指定端口时才会 |

## 8. 验收记录

见 task-5 报告：`build_frontend.ps1` → `frontend\dist\index.html`；`build_exe.ps1` → `dist\闲鱼AI工作台\闲鱼AI工作台.exe`；产物 `--no-window` 启动后 `/api/health` 返回 200（`frozen:true`）。
