# 闲鱼 AI 工作台

Windows 桌面应用：**卖家的闲鱼助手** —— 商品批量管理 + 多 AI 客服（每个商品一个常驻 AI，主管 AI 统一调度）+ 人工接管 + 风控保命。

## 它是什么

| 能力 | 说明 |
| --- | --- |
| 多 AI 分工 | `master` 主管 AI 对接你；每个商品一个 `item` AI；`cs`/`bargain`/`cross`(跨商品协查)/`vision` 各司其职 |
| 跨商品沟通 | 买家在 A 商品里问 B 商品，`item:A` → `cross` → `item:B` 自动协查，全过程可在「AI 沟通」页看到 |
| 人工接管 | 任何会话都能一键接管，AI 让位；你发完继续交给 AI |
| API Key 统一管理 | 所有 AI 角色共用一份提供者/Key 配置，可给不同角色指定不同模型 |
| 商品批量 | 批量改价/改标题/改描述/上下架/发布 |
| 风控 | 熔断阶梯、x5sec 校验、系统 Chrome 人工验证、登录态不过期 |

## 跑起来（开发）

```powershell
# 依赖
C:\Python310\python.exe -m pip install -r backend\requirements.txt

# 后端（开发模式，前端还没构建时给占位页）
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe -m backend.app.main --port 8080"

# 前端
cd frontend && npm install && npm run dev

# 桌面窗口（推荐：自动起后端 + 开窗口）
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe desktop\main.py"
```

## 测试

```powershell
# 全量基线（72 个测试文件 / 3087 个用例，逐个子进程跑）
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\run_all.py"

# 单个文件
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_agents_cross.py"
```

真实 HTTP 冒烟（需要后端已在跑）：

```powershell
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe scripts\smoke_api.py 18090"     # 只读 17 条
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe scripts\smoke_write.py 18090"   # 写路径 11 条
```

测试约定：**不打真实闲鱼平台、不调真实 LLM**（协议层走 MockTransport，AI 层 mock
`llm.chat`）；测试把整个数据目录挪到临时目录（`XIANYU_APP_DIR`），不会碰你的真实库。

## 文档

| 文件 | 内容 |
| --- | --- |
| [docs/交接文档.md](docs/交接文档.md) | **接手先看这份**：五分钟跑起来、目录地图、四条关键链路、六条硬口径、能力清单、真机证据、待办与避坑手册 |
| [docs/HANDOFF.md](docs/HANDOFF.md) | 按批次的开发日志：九条诉求逐条对照、真实命令与输出、**已验证 / 未验证**边界 |
| [docs/CONTRACTS.md](docs/CONTRACTS.md) | 契约：19 张表 DDL / REST 清单 / 编排协议 / MCP 工具集 |
| [docs/PACKAGING.md](docs/PACKAGING.md) | 打包、产物路径、常见坑 |
| [docs/MCP.md](docs/MCP.md) | 挂进 DeepSeek Harness 的配置与排错 |

## 结构

```
backend/app/core/      配置 / DB / 认证 / 事件总线（Lead 独占）
backend/app/xianyu/    闲鱼协议层（登录、Cookie、签名、风控）
backend/app/agents/    多 AI 编排（角色、提示词、LLM、工具、跨商品协议）
backend/app/services/  业务服务（商品、聊天、客服、订单）
backend/app/api/       路由（每个模块一个文件）
desktop/               PyWebView 桌面外壳 + 托盘
frontend/              React + TS 界面
mcp_server/            DeepSeek Harness 插件（MCP stdio）
docs/CONTRACTS.md      契约：表结构 / API / 编排协议 / 工具集（唯一接口真相）
```

数据目录：`%APPDATA%\xianyu-ai-workbench\`（`data/app.db`、`logs/app.log`、`browser/`）。
可用环境变量 `XIANYU_APP_DIR` 把它整体挪走（测试与多实例用）。

## 打包

```powershell
pwsh scripts\build_frontend.ps1   # 先构建前端（js 文件名带内容 hash，必须重跑）
pwsh scripts\build_exe.ps1        # 再打包
```

产物 `dist\闲鱼AI工作台\闲鱼AI工作台.exe`（onedir 形态，约 190 MB —— 主因是
`patchright` 自带的 `node.exe` driver）。首次运行会自动建库、生成随机管理员密码
（`data\desktop_credentials.json`）与本地免密密钥（`data\local_secret.txt`）。
