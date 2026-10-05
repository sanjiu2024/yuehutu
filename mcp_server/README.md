# mcp_server —— 闲鱼 AI 工作台的 DSH 插件

让 DeepSeek Harness（或任何 MCP 客户端）直接指挥这个工作台：查账号、改商品、回买家消息、
看订单与风控，或者用 `master_chat` 把用户的话转给「主管 AI」让它去干活。

- **完整挂载说明与排错 → [`../docs/MCP.md`](../docs/MCP.md)**
- `server.py`：stdio JSON-RPC（`initialize` / `tools/list` / `tools/call`），手写协议，
  **不依赖 `mcp` SDK**，只用 Python 3.10 标准库。
- `client.py`：工具定义 + 打本地后端 `/api/...` 的薄 HTTP 客户端。

## 快速上手

```powershell
# 1) 先起后端（MCP 只是它的遥控器）
cd C:\Users\Administrator\Projects\xianyu-ai-workbench
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe -m backend.app.main --port 8080"

# 2) 体检（不接客户端，打印工具清单 + 后端连通性）
C:\Python310\python.exe mcp_server\server.py --selftest
```

挂进 DSH：把 `docs/MCP.md` §2 的 JSON 片段加进
`%USERPROFILE%\.dsh\plugins\mcp-bridge.config.json` 的 `servers`，然后**重启 DSH 宿主**。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `XYWB_BASE_URL` | 空 | 想连**别的**地址时才设。不设就按下面顺序自动找后端 |
| `XYWB_TOKEN` | 空 | 覆盖 token；不设就读 `%APPDATA%\xianyu-ai-workbench\data\mcp_token.txt` |
| `XYWB_TIMEOUT` | `30` | 单次 HTTP 超时（秒），同步商品/调 AI 时可以调大 |
| `XIANYU_APP_DIR` | 空 | 覆盖数据目录（默认 `%APPDATA%\xianyu-ai-workbench`），测试/多实例用 |

**后端地址怎么定的**（`client.py::resolve_base_url`）：
显式参数 > `XYWB_BASE_URL` > `%APPDATA%\xianyu-ai-workbench\data\backend_port.txt` > `http://127.0.0.1:8080`。

桌面形态端口是随机挑的，所以**默认不用配 `XYWB_BASE_URL`** —— 后端每次启动会把实际端口写进
`backend_port.txt`（`backend/app/main.py::_write_port_file`），插件读它就能找到。
文件缺失/空/非数字/越界/非 UTF-8 一律继续回退，不抛异常。
`--selftest` 会打印实际 `base_url` 与 `地址来源`。细节见 `docs/MCP.md` 第 2 节。

## 工具集

工具定义**唯一来源**是 `backend/app/agents/tools.py::TOOL_SPECS`（和主管 AI 用同一份定义），
MCP 侧把它映射成 MCP 的 `inputSchema`；该文件不存在/导入失败时退回 `client.py::CANONICAL_SPECS`
内置兜底清单。**当前 `TOOL_SPECS` 已落地**，`initialize` 的 `instructions` 里会写明本次用的是哪一份。

规范名以 `docs/CONTRACTS.md` §5 为准，共 21 个（`agents_messages` 为 MCP 侧独有）：

```
accounts_list / accounts_set_enabled / accounts_relogin
items_list / items_get / items_edit / items_publish / items_batch_edit / items_sync
chats_list / chats_read / chats_send / chats_set_mode
orders_list / orders_deliver
risk_status / risk_clear
providers_list / providers_upsert
master_chat
agents_messages
```

动词式别名（`list_items` / `get_item` / `send_message` / `list_agent_messages` …）在 `tools/call`
里仍可用，但**不出现在 `tools/list`**。

## 后端路由依赖

MCP 的 21 条路由**全部**已与后端注册的端点对齐（`backend/tests/test_mcp_server.py::
RouteRegistrationTest` 用 AST 读 `backend/app/api/*.py` 的装饰器来守这条，不启动后端）。
其中 lead 在 2026-10-06 点名的 5 条，路径与 `docs/CONTRACTS.md` §3 逐字一致：

| 工具 | 路由 |
|---|---|
| `items_batch_edit` | `POST /api/items/{account_id}/batch-edit` |
| `accounts_relogin` | `POST /api/accounts/{account_id}/relogin` |
| `accounts_set_enabled` | `PATCH /api/accounts/{account_id}` |
| `items_get` | `GET /api/items/{account_id}/{item_id}` |
| `chats_list` | `GET /api/chats/conversations` |

真机联调（真后端 + 真 MCP 子进程，21 个工具逐个打一遍）**没有任何 404/405/422**：
返回的都是业务码（`not_found` / `validation_error` / `xianyu_not_ready` / `bad_request` …），
说明路由全部命中、参数被后端接受。业务结果取决于账号是否登录，不影响路由正确性。

## 测试

```powershell
cd C:\Users\Administrator\Projects\xianyu-ai-workbench
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_mcp_server.py"
```

55 个用例：tools/list 契约、tools/call 的 URL/token/参数拼装、后端不可用的结构化错误、
协议层（通知不回包 / 坏 JSON / **非 UTF-8 字节**都不打死进程）、路由与后端注册对账、
**后端地址解析**（端口文件回退、env 优先、非法内容不抛），
以及**真实子进程冒烟**（`Popen([sys.executable, 'mcp_server/server.py'])` 走完整握手）。
