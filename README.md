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
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_skeleton.py"
```

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

## 打包

```powershell
pwsh scripts\build_exe.ps1
```

产物 `dist\闲鱼AI工作台\闲鱼AI工作台.exe`（目录形态，启动快、便于热更）。
