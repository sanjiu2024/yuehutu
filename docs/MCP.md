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
     起在**随机空闲端口**（`config.DEFAULT_PORT = 0`），配死的 `XYWB_BASE_URL` 对不上 ——
     插件会自己去读 `data/backend_port.txt`，**不需要你配地址**，见下面
     「后端端口是随机的怎么办（桌面形态，已支持）」。
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
- **不用配 `XYWB_BASE_URL`**：桌面形态端口是随机的，插件会自己读
  `data/backend_port.txt` 找到后端。只有当你想让插件连**另一个**地址（比如固定端口跑的
  开发后端）时才填它 —— 填了就压过端口文件。
- **改完 `mcp-bridge.config.json` 必须重启 DSH 宿主**（该文件只在插件加载时读一次）。
- 挂上后在 DSH 里应该能看到 `mcp_servers` / `mcp_tools` / `mcp_call` 三个工具，先
  `mcp_tools(server="xianyu")` 看目录，再 `mcp_call` 调用。

### 后端端口是随机的怎么办（桌面形态，已支持）

桌面应用的后端端口是**每次启动现挑的空闲端口**（`desktop/main.py:191` 调
`pick_free_port()`，`config.DEFAULT_PORT = 0`），所以不可能靠 `XYWB_BASE_URL` 配死 ——
这次是 18090，下次可能是 52311。

**后端启动时会把实际端口写进一个文件**，插件读这个文件自己找到它：

```
%APPDATA%\xianyu-ai-workbench\data\backend_port.txt     # 内容就是端口，例如 18090
```

（写盘方在 `backend/app/main.py::_write_port_file`，lifespan 里执行；
`config.py` 里叫 `PORT_PATH`。用 `XIANYU_APP_DIR` 改了数据目录时跟着一起改。）

插件侧的解析顺序（`mcp_server/client.py::resolve_base_url`）：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | 显式参数 | 只有代码里调用时才用到 |
| 2 | `XYWB_BASE_URL` 环境变量 | 你在 DSH 配置里写了就用你的，**压过端口文件** |
| 3 | `data/backend_port.txt` | 读出来 `strip()` 后拼 `http://127.0.0.1:<port>` |
| 4 | `http://127.0.0.1:8080` | 兜底默认值 |

所以**桌面形态不用配 `XYWB_BASE_URL`**，插件会自己找。文件缺失、是空的、内容不是数字、
端口越界、甚至不是 UTF-8，都只是**继续往下回退**，不会抛异常、不会崩（见下面测试）。

开发态你固定 `--port 8080` 起后端时，第 3 步读到的也是 8080，两条路都通。

**但端口文件是「上一次启动」的**：桌面应用关掉后文件还在，插件会连一个已经没人在听的端口 ——
那时报的是 `backend_unreachable`，先确认应用开着即可。多个后端实例共用同一个数据目录时，
文件只有最后启动那个的端口。

**地址只在插件启动时解析一次**：`BackendClient` 构造时定死 base_url，后端换端口后需要重启
插件进程（改 DSH 配置本就要重启宿主）。

命令行自查当前解析结果：

```
C:\Python310\python.exe mcp_server\server.py --selftest
```

输出里的 `base_url` 与 `地址来源` 就是它实际会连的地址和来源（`env:XYWB_BASE_URL` /
`file:...backend_port.txt` / `default (...)`）。

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
另外 `RouteRegistrationTest` 会把 MCP 的每条路由拿去和 `backend/app/api/*.py` 里真实
注册的装饰器对账，`test_toolspec_fallback_is_not_silent` 挡住「TOOL_SPECS 加载失败 →
对齐测试整体跳过 → 套件假绿」这条缝。

---

## 4. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `backend_unreachable` | 后端没起，或地址不对。先 `curl http://127.0.0.1:8080/api/health`（桌面形态请先看 `data/backend_port.txt` 里的端口，或跑 `--selftest` 看 `地址来源`） |
| 连到了但像是「上一次」的后端 | `data/backend_port.txt` 是上次启动留下的。确认桌面应用现在开着；多实例共用一个数据目录时文件只记最后启动那个 |
| `backend_timeout` | 后端在跑但某个操作超过 30s（同步商品/调 AI 会比较慢）。调大 `XYWB_TIMEOUT`（秒） |
| `unauthorized` | `mcp_token.txt` 过期/不对。后端每次启动会重写它 —— 重启后端即可；或设 `XYWB_TOKEN` |
| `http_404` | 那个 REST 端点还没实现（并行开发期正常）。看 `detail.url` 是哪个 |
| `http_422` | 参数不对。MCP 侧只透传路由认识的字段，多给的字段会被丢掉 |
| `unknown_tool` | 工具名写错了，错误里会列出全部可用工具 |
| 客户端收不到任何响应 | stdout 被别的东西污染了。`mcp_server/server.py` 只往 stdout 写 JSON-RPC 行，日志一律走 stderr |
| `unauthorized` 但 token 文件看着正常 | 文件被写成了非 UTF-8（编辑器存成 GBK/UTF-16）。MCP 会忽略它并按「无 token」处理，日志里的 `token来源` 会写明 `(不是 UTF-8，已忽略)` |

手工体检（不接客户端）：

```
C:\Python310\python.exe C:\Users\Administrator\Projects\xianyu-ai-workbench\mcp_server\server.py --selftest
```

会打印 base_url、**地址来源**（`env:XYWB_BASE_URL` / `file:...backend_port.txt` / `default (...)`）、
token 来源、工具定义来源、工具清单、别名抽查，以及 `/api/health` 的返回。

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
| 桌面形态随机端口：插件读 `data/backend_port.txt` 找到后端 | ✅ 验过（2026-10-06 更新）：`resolve_base_url` 按「显式参数 > `XYWB_BASE_URL` > 端口文件 > 8080」解析；`BaseUrlResolutionTest` 9 个用例覆盖真实端口、`strip()`、env 优先、文件缺失、空/非数字/越界/非 UTF-8 全部回退不抛。**注意**：验的是「读端口文件」这条链路，不是「真桌面应用 + 真 DSH 宿主」的完整端到端 —— 后者仍受上一行限制 |
| `items_get` 直连 `GET /api/items/{account_id}/{item_id}` | ✅ 验过（2026-10-06 更新）：端点已落地，真机返回业务码 `not_found`（不是 404）；services 未落地时写的「列表 + 本地精确过滤」兜底仍在，有单测覆盖 |
| 21 条路由与后端注册的端点一致 | ✅ 验过：真后端 + 真 MCP 子进程逐工具打一遍，**无任何 404/405/422**；另有 `RouteRegistrationTest` 用 AST 对账守着 |
| `TOOL_SPECS` 与 MCP 工具集对齐 | ✅ 验过：`TOOL_SPECS` 20 个 + MCP 独有 `agents_messages` = `tools/list` 21 个；新增工具漏路由会红 |
