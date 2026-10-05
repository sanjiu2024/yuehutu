# 把闲鱼 AI 工作台挂进 DeepSeek Harness（MCP）

这个目录是**用户要求的那件事**：让 DSH（或任何 MCP 客户端）直接指挥闲鱼 AI 工作台。

```
mcp_server/
  server.py    # stdio JSON-RPC MCP server：initialize / tools/list / tools/call
  client.py    # 工具定义（唯一来源 TOOL_SPECS）+ 打本地后端 REST 的薄 HTTP 客户端
  README.md    # 快速上手
docs/MCP.md    # 就是本文件：怎么挂进 DSH + 排错
```

不依赖 `mcp` SDK，纯 Python 3.10 标准库手写协议；后端不可用时返回**结构化错误**，不抛栈。

---

## 1. 前置条件（必须先做）

1. **后端得先跑起来**，MCP 工具才有东西可打。两种方式：
   - 开发态：仓库根目录
     ```
     cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe -m backend.app.main --port 8080"
     ```
     想换端口就加 `--port 9000`，同时把 DSH 的 `XYWB_BASE_URL` 改成同一个地址。
   - 正常态：双击桌面应用（`desktop/main.py` → PyInstaller 打包的 exe）。**注意**：桌面应用
     起在**随机空闲端口**（`config.DEFAULT_PORT = 0`），DSH 里配死的 `XYWB_BASE_URL` 对不上，
     这种情况见下面「后端端口是随机的怎么办」。
2. **token**：后端每次启动会调 `security.refresh_mcp_token()` 写一份长期 token 到
   ```
   %APPDATA%\xianyu-ai-workbench\data\mcp_token.txt
   ```
   MCP server 自己会读这个文件，**正常情况下你什么都不用配**。只有想覆盖时才设 `XYWB_TOKEN`。
3. Python：用 `C:\Python310\python.exe`（后端要求 3.10）。

---

## 2. 挂进 DSH

DSH 的 MCP 是**桥接**的，配置文件是：

```
%USERPROFILE%\.dsh\plugins\mcp-bridge.config.json
```

往 `servers` 里加一段（保留已有的其它 server）：

```json
{
  "servers": {
    "xianyu": {
      "description": "闲鱼 AI 工作台：账号/商品/会话/订单/风控 + master_chat 主管 AI",
      "command": [
        "C:\\Python310\\python.exe",
        "C:\\Users\\Administrator\\Projects\\xianyu-ai-workbench\\mcp_server\\server.py"
      ],
      "timeoutMs": 120000,
      "env": {
        "XYWB_BASE_URL": "http://127.0.0.1:8080",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

要点：

- `command` 第一项是 Python 解释器绝对路径，第二项是 `server.py` **绝对路径**。
  不需要设 `cwd`：`server.py` 会自己把仓库根塞进 `sys.path`，从任何工作目录启动都能找到
  `backend/app/agents/tools.py`（已实测，见第 5 节）。
- 不用配 `XYWB_TOKEN` —— 走 `mcp_token.txt` 即可。要覆盖再加这个 key。
- **改完 `mcp-bridge.config.json` 必须重启 DSH 宿主**（该文件只在插件加载时读一次）。
- 挂上后在 DSH 里应该能看到 `mcp_servers` / `mcp_tools` / `mcp_call` 三个工具，先
  `mcp_tools(server="xianyu")` 看目录，再 `mcp_call` 调用。

### 后端端口是随机的怎么办

桌面应用监听随机端口时，有两条路：

1. 用固定端口跑后端（上面的 `--port 8080`），DSH 指向它 —— 最省事；
2. 或者每次启动后把实际端口写进环境变量/配置里再重启 DSH。

`mcp_server/server.py` 的 base_url 只认 `XYWB_BASE_URL`（默认 `http://127.0.0.1:8080`），
不会去猜端口。

---

## 3. 工具集（21 个，`tools/list` 实际输出）

工具定义**唯一来源**是 `backend/app/agents/tools.py::TOOL_SPECS`（和主管 AI 用同一份），
MCP 侧只把它映射成 MCP 的 `inputSchema` 并补上后端路由。名字以 `docs/CONTRACTS.md` §5 为准：

| 分组 | 工具 |
|---|---|
| 账号 | `accounts_list` / `accounts_set_enabled` / `accounts_relogin` |
| 商品 | `items_list` / `items_get` / `items_edit` / `items_publish` / `items_batch_edit` / `items_sync` |
| 会话 | `chats_list` / `chats_read` / `chats_send` / `chats_set_mode` |
| 订单 | `orders_list` / `orders_deliver` |
| 风控 | `risk_status` / `risk_clear` |
| 供应商 | `providers_list` / `providers_upsert` |
| 主管 AI | `master_chat`（**把用户的话转给主管 AI**，指挥这个软件优先用它） |
| 编排排查 | `agents_messages`（AI 之间的跨商品协查记录；MCP 侧独有，`TOOL_SPECS` 里没有） |

**动词式别名**（任务卡里的 `list_items` / `get_item` / `send_message` / `list_agent_messages` …）
在 `tools/call` 里**仍然能调**（`mcp_server/client.py::ALIASES`），但**不出现在 `tools/list`** ——
同一个动作暴露两个工具会让模型选错。有测试守着这条（`test_no_alias_leaked_into_tools_list`）。

### 对齐状态

`TOOL_SPECS` 已经落地（20 个工具），MCP 当前**实时**从它读定义（`initialize` 的
`instructions` 里会写明来源）。两边的对齐由
`backend/tests/test_mcp_server.py::test_no_toolspec_silently_dropped` 守着：
`TOOL_SPECS` 里新增了工具而 MCP 没补路由时，这个测试会失败并指名道姓。

---

## 4. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `backend_unreachable` | 后端没起，或 `XYWB_BASE_URL` 端口不对。先 `curl http://127.0.0.1:8080/api/health` |
| `backend_timeout` | 后端在跑但某个操作超过 30s（同步商品/调 AI 会比较慢）。调大 `XYWB_TIMEOUT`（秒） |
| `unauthorized` | `mcp_token.txt` 过期/不对。后端每次启动会重写它 —— 重启后端即可；或设 `XYWB_TOKEN` |
| `http_404` | 那个 REST 端点还没实现（并行开发期正常）。看 `detail.url` 是哪个 |
| `http_422` | 参数不对。MCP 侧只透传路由认识的字段，多给的字段会被丢掉 |
| `unknown_tool` | 工具名写错了，错误里会列出全部可用工具 |
| 客户端收不到任何响应 | stdout 被别的东西污染了。`mcp_server/server.py` 只往 stdout 写 JSON-RPC 行，日志一律走 stderr |

手工体检（不接客户端）：

```
C:\Python310\python.exe C:\Users\Administrator\Projects\xianyu-ai-workbench\mcp_server\server.py --selftest
```

会打印 base_url、token 来源、工具定义来源、工具清单、别名抽查，以及 `/api/health` 的返回。

---

## 5. 这份文档里的配置，哪一步验过 / 哪一步没验

诚实交代（2026-10-06）：

| 项 | 状态 |
|---|---|
| `command` 用 `C:\Python310\python.exe` + `mcp_server\server.py` 绝对路径能起来 | ✅ 验过：真实 `Popen` 子进程喂 `initialize` / `tools/list` / `tools/call`，有响应 |
| 从**任意工作目录**启动都能找到 `TOOL_SPECS` | ✅ 验过：`cd C:\` 后跑 `server.py`，`tools/list` 正常返回 21 个工具 |
| 不设 `XYWB_TOKEN`、走 `%APPDATA%\...\mcp_token.txt` | ✅ 验过：真后端 + 真 MCP 子进程，`providers_list` 返回 `{"success":true,"data":[]}` |
| `XYWB_BASE_URL` 指向真后端、`/api/health` 通 | ✅ 验过：`{"success":true,"data":{"ok":true,"version":"0.1.0","frozen":false}}` |
| 后端未起时的结构化错误 | ✅ 验过：`backend_unreachable`，带 `base_url` 与 `url`，`isError: true` |
| **把这段 JSON 真的写进 `mcp-bridge.config.json` 并重启 DSH 宿主** | ❌ **没验**：改那个文件会影响这台机器上所有 dsh 会话，属于宿主级改动，留给机器主人/Lead 决定何时做 |
| 桌面应用随机端口下的 `XYWB_BASE_URL` | ❌ 没验（第 2 节给了两条绕法） |
| `items_get` 直连 `GET /api/items/{account_id}/{item_id}` | ⚠️ 部分验过：端点已进 `CONTRACTS.md` §3，但 services 还没落地，真机返回 404 时会自动退化为「列表 + 本地精确过滤」（有单测覆盖兜底路径） |
