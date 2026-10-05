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

## 6. 路径与 frozen 说明

- 开发态：`config.REPO_ROOT` 由 `backend/app/core/config.py` 的 `__file__` 推出（仓库根），`FRONTEND_DIST = <repo>\frontend\dist`。
- **frozen 态**：`core/config.py` 的 `REPO_ROOT` 会算到 `_internal\backend\app\core` 往上三级 —— 指不到打包内的前端目录。因此 `desktop/main.py` 在 `sys.frozen` 为真时**在外壳侧覆盖** `config.FRONTEND_DIST = _MEIPASS / 'frontend' / 'dist'`（在 `create_app()` 被调用前生效，因为 `backend.app.main` 是模块级 `app = create_app()`……注意：外壳 import 后才覆盖，见下方「已知限制」）。
- 同理 `sys.path` 会被加上 `_MEIPASS`，让 `backend` 包可导入。

### 已知限制（需要 Lead 决定是否改 `backend/app/**`）

1. `backend/app/main.py` 末尾有模块级 `app = create_app()`，它**在 import 时就执行**，此时 `desktop/main.py` 还没来得及覆盖 `config.FRONTEND_DIST`。frozen 下如果 `core/config.py` 算出的路径恰好不存在，应用会挂上**占位页路由**而不是静态托管。
   - 现状：外壳已覆盖 `config.FRONTEND_DIST`，但只在 import 之后 —— 所以占位页/静态托管的选择可能发生在覆盖之前。
   - 建议改法（**外壳不动 core，需 Lead 拍板**）：把 `config.py` 里 `FRONTEND_DIST` 改成函数 `frontend_dist()`，或在 `main.py` 的 `create_app()` 里读 `sys._MEIPASS`：
     ```python
     if getattr(sys, 'frozen', False):
         FRONTEND_DIST = Path(sys._MEIPASS) / 'frontend' / 'dist'
     else:
         FRONTEND_DIST = REPO_ROOT / 'frontend' / 'dist'
     ```
2. `desktop/main.py` 里的 `config.FRONTEND_DIST` 覆盖保留作双保险；若 Lead 按上面改了 `core/config.py`，这行可删。

## 7. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 窗口白屏 / 打不开 | 缺 **WebView2 运行时**。装 [Evergreen Bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/)，本机已装 154.0.4258.53 |
| 双击 exe 没反应、没日志 | `console=False`，看不到 stdout。看 `%APPDATA%\xianyu-ai-workbench\logs\app.log`；排查时先用 `--no-window --port 18080` 跑 |
| 杀毒报毒 / 被删 | PyInstaller 无签名 exe 常见误报。加白名单；正式分发要买代码签名证书 |
| 路径带空格 / 中文 | 脚本全部用 `Join-Path` + 引号；spec 里用 `Path` 对象，支持 `C:\Program Files\...` 与中文路径 |
| `ModuleNotFoundError: backend` | 打包时 `pathex` 没加仓库根，或运行时 `sys.path` 没加 `_MEIPASS`（本仓库两者都已处理） |
| 路由 404（新模块没注册） | `backend/app/main.py` 的 `ROUTE_MODULES` 名单由 Lead 维护，新增模块要请 Lead 加 |
| 打包体积大（~150MB+） | 主要是 playwright / pythonnet / WebView2 依赖；已排除 tkinter/Qt。真要瘦身先去掉 playwright 再打 |
| 端口被占 | 桌面模式默认 `port=0` 自动挑空闲端口，正常不会撞；`--port` 指定端口时才会 |

## 8. 验收记录

见 task-5 报告：`build_frontend.ps1` → `frontend\dist\index.html`；`build_exe.ps1` → `dist\闲鱼AI工作台\闲鱼AI工作台.exe`；产物 `--no-window` 启动后 `/api/health` 返回 200（`frozen:true`）。
