# 契约（先冻结，后并行）

> 这份文件是**唯一接口真相**。所有并行工作都按它写；要改先改这里，并通知所有相关方。
> 目标：闲鱼卖家 AI 工作台 —— Windows 桌面应用（单 exe），多 AI 编排，人工可接管。

## 0. 形态与技术栈（已定）

| 项 | 决定 |
|---|---|
| 交付 | Windows 桌面应用，双击运行，含托盘；最终用 PyInstaller 打成单 exe |
| 外壳 | PyWebView（WebView2，已装 154.0.4258.53）+ pystray 托盘 |
| 后端 | Python 3.10 + FastAPI + uvicorn（线程内起在 127.0.0.1:<随机空闲端口>） |
| 前端 | React 18 + TypeScript + Vite 5（构建产物由后端静态托管） |
| 存储 | SQLite（`%APPDATA%\xianyu-ai-workbench\data\app.db`） |
| 协议层 | **搬运**旧项目 `Projects\xianyu-super-butler` 已验证的模块（登录/扫码/风控/指纹/会话） |

启动链路：`desktop/main.py` → 起后端线程 → 等 `/api/health` 就绪 → `webview.create_window()` 打开 `http://127.0.0.1:<port>/` → 托盘常驻。

## 1. 目录与写入范围（并行时不许越界）

```
xianyu-ai-workbench/
  backend/app/core/      # config / db / security / events   —— 骨架层（Lead 独占）
  backend/app/xianyu/    # 闲鱼协议层（搬运+改造）            —— 协议工作流独占
  backend/app/agents/    # 多 AI 编排                        —— Agent 工作流独占
  backend/app/services/  # 商品/聊天/订单/客服业务            —— 业务工作流独占
  backend/app/api/       # 路由（薄层，只做参数校验与调度）    —— 各工作流按模块名独占
  desktop/               # PyWebView 外壳 + 托盘 + 打包脚本    —— 外壳工作流独占
  frontend/              # React UI                          —— 前端工作流独占
  mcp_server/            # DSH 插件（MCP stdio server）       —— 插件工作流独占
  backend/tests/         # 测试（按模块名分文件，互不重叠）
```

**共享文件规则**：`backend/app/main.py`（路由注册）、`backend/requirements.txt`、`docs/CONTRACTS.md` 由 Lead 修改；其他人要加路由，只在 `backend/app/api/<你的模块>.py` 里写 `router`，然后**发消息给 Lead** 要求注册。

## 2. 数据库（SQLite，`PRAGMA journal_mode=WAL`）

时间戳统一 `CURRENT_TIMESTAMP`（UTC）—— 与旧项目约定一致，**不要**改成本地时间。

```sql
users(id INTEGER PK, username TEXT UNIQUE, password_hash TEXT, is_admin INTEGER DEFAULT 0, created_at, last_seen)
sessions(token TEXT PK, user_id INTEGER, created_at, last_seen, expires_at)
settings(key TEXT PK, value TEXT, updated_at)

-- 闲鱼账号
accounts(id TEXT PK,                  -- 闲鱼 unb（唯一账号标识）
         nickname TEXT, avatar_url TEXT, remark TEXT,
         enabled INTEGER DEFAULT 0, auto_reply INTEGER DEFAULT 1,
         created_at, updated_at)
account_credentials(account_id TEXT PK REFERENCES accounts(id) ON DELETE CASCADE,
         cookie TEXT, xianyu_user_id TEXT, login_state TEXT DEFAULT 'unknown',
         relogin_reason TEXT, last_login_at, updated_at)

-- 商品
items(id INTEGER PK, account_id TEXT, item_id TEXT UNIQUE, title TEXT, price REAL,
      original_price REAL, stock INTEGER, status TEXT, category_id TEXT,
      images TEXT, description TEXT, ai_enabled INTEGER DEFAULT 1,
      updated_at, synced_at, UNIQUE(account_id, item_id))

-- AI 供应商与角色
providers(id INTEGER PK, name TEXT, base_url TEXT, api_key TEXT, default_model TEXT,
          enabled INTEGER DEFAULT 1, is_default INTEGER DEFAULT 0, created_at, updated_at)
ai_roles(role TEXT PK,               -- master|item|cs|bargain|cross|vision
         provider_id INTEGER, model TEXT, temperature REAL DEFAULT 0.7,
         system_prompt TEXT, enabled INTEGER DEFAULT 1, updated_at)
ai_prompts(id INTEGER PK, role TEXT, scope TEXT,      -- global|account|item
           account_id TEXT, item_id TEXT, name TEXT, content TEXT,
           priority INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, updated_at)

-- 会话与消息
conversations(id INTEGER PK, account_id TEXT, peer_user_id TEXT, peer_nickname TEXT,
              item_id TEXT, mode TEXT DEFAULT 'auto',   -- auto|manual|off
              agent_role TEXT DEFAULT 'item', unread INTEGER DEFAULT 0,
              last_message_at, created_at, UNIQUE(account_id, peer_user_id))
messages(id INTEGER PK, conversation_id INTEGER, direction TEXT,  -- in|out
         sender TEXT,                  -- buyer|ai:<role>|human
         content TEXT, msg_type TEXT DEFAULT 'text', item_id TEXT,
         meta TEXT, sent_ok INTEGER, created_at)
agent_messages(id INTEGER PK, conversation_id INTEGER, from_agent TEXT, to_agent TEXT,
               kind TEXT, topic TEXT, payload TEXT, created_at)
agent_runs(id INTEGER PK, conversation_id INTEGER, role TEXT, provider_id INTEGER,
           model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
           latency_ms INTEGER, status TEXT, error TEXT, created_at)

-- 订单/发货/风控/审计
orders(id INTEGER PK, account_id TEXT, order_id TEXT UNIQUE, item_id TEXT, buyer_id TEXT,
       amount REAL, status TEXT, raw TEXT, created_at, updated_at)
shipments(id INTEGER PK, order_id TEXT, company TEXT, tracking_no TEXT, status TEXT,
          created_at, updated_at)
risk_state(account_id TEXT PK, consecutive_hits INTEGER DEFAULT 0, blocked_until REAL DEFAULT 0,
           last_hit_at REAL DEFAULT 0, verification_type TEXT, updated_at)
risk_events(id INTEGER PK, account_id TEXT, kind TEXT, message TEXT, created_at)
operation_logs(id INTEGER PK, account_id TEXT, item_id TEXT, action TEXT, success INTEGER,
               detail TEXT, created_at)
```

## 3. REST API（前缀 `/api`；除 `/api/auth/*` 与 `/api/health` 外都要 `Authorization: Bearer <token>`）

```
POST   /api/auth/login            {username,password} -> {token,user}
POST   /api/auth/logout
GET    /api/auth/me
GET    /api/health                -> {ok:true,version,...}

GET    /api/accounts              -> [{id,nickname,enabled,auto_reply,login_state,runtime_state,remark}]
POST   /api/accounts/qr/start     -> {ticket,qr_image_b64,expires_in}
GET    /api/accounts/qr/{ticket}  -> {state:pending|scanned|confirmed|expired|failed,account_id?}
PATCH  /api/accounts/{id}         {enabled?,auto_reply?,remark?}
DELETE /api/accounts/{id}
POST   /api/accounts/{id}/relogin -> 触发重新扫码/续期
POST   /api/accounts/{id}/captcha -> 打开人工验证浏览器窗口（滑块）

GET    /api/items?account_id=&status=&q=&limit=&offset=
POST   /api/items/{account_id}/sync            -> 从闲鱼拉取在售商品入库
POST   /api/items/{account_id}/publish         {title,price,description,images,category_id,...}
POST   /api/items/{account_id}/{item_id}/edit  {price?,title?,description?,stock?,images?}
POST   /api/items/{account_id}/{item_id}/delist | /relist
POST   /api/items/{account_id}/batch-edit      {item_ids:[...],patch:{price_delta|price_set|title_tpl|...}}
POST   /api/items/{account_id}/batch-publish   {items:[...]}
GET    /api/items/{account_id}/operation-logs?limit=

GET    /api/chats/conversations?account_id=&mode=&limit=
GET    /api/chats/{conversation_id}/messages?limit=&before_id=
POST   /api/chats/{conversation_id}/send       {content}        -- 人工回复
POST   /api/chats/{conversation_id}/mode       {mode}
POST   /api/chats/{conversation_id}/ai-draft   -> {draft}       -- 生成建议但不发送
WS     /api/ws?token=...                                        -- 新消息/状态推送

GET    /api/agents/roles ; PUT /api/agents/roles/{role}
GET    /api/agents/prompts ; POST /api/agents/prompts ; PUT/DELETE /api/agents/prompts/{id}
POST   /api/agents/master/chat                 {message,conversation_id?} -> {reply,tool_calls[]}
GET    /api/agents/runs?limit= ; GET /api/agents/messages?conversation_id=

GET    /api/providers ; POST /api/providers ; PUT/DELETE /api/providers/{id}
POST   /api/providers/{id}/test                -> {ok,latency_ms,model,error?}
POST   /api/providers/{id}/default

GET    /api/orders?account_id=&status= ; POST /api/orders/sync ; POST /api/orders/{order_id}/deliver
GET    /api/orders/{order_id}/logistics
GET    /api/risk/status ; POST /api/risk/{account_id}/clear   -- 只有人工验证成功后才能清
GET    /api/settings ; PUT /api/settings       {key:value,...}
```

错误响应统一：`{"success":false,"message":"...","code":"..."}`，HTTP 用语义状态码（4xx 业务、5xx 内部）。成功统一 `{"success":true,"data":{...}}`。

## 4. 多 AI 编排（这是本项目的核心卖点）

**角色**

| role | 职责 | 输入 | 输出 |
|---|---|---|---|
| `master` | 唯一的「主管 AI」：与**卖家（用户）**对话、理解意图、调用工具干活、把任务派给其它 AI | 用户消息 + 工具集 | 文本回复 + tool_calls |
| `item` | 某个**商品**的客服 AI，按商品维度人格与话术 | 买家消息 + 商品上下文 | 回复文本 |
| `cs` | 通用客服：订单、物流、退换、售后政策 | 买家消息 + 订单上下文 | 回复文本 |
| `bargain` | 议价：在卖家设定的底价/幅度内还价 | 买家消息 + 价格规则 | 回复文本 + 建议价 |
| `cross` | **跨商品协查**：买家在 A 商品里问 B 商品时，去问 B 的商品 AI | 问题 + 全店商品索引 | 答复文本 |
| `vision` | 看图（买家发图问货） | 图片 URL + 问题 | 描述/答复 |

**跨商品沟通协议**（`agent_messages` 表落库，深度上限 2，超时 3s）：

```json
{"from":"item:1047949964912","to":"cross","kind":"ask",
 "topic":"other_item_info","conversation_id":88,
 "payload":{"question":"这个多少钱","candidate_items":["1047950000001"]}}
```
`kind` ∈ `ask|answer|notify`。`cross` 收到后：① 用 `payload.candidate_items` 或商品索引匹配 → ② 向目标 `item:<item_id>` 发 `ask` → ③ 汇总为 `answer` 返回原 AI。所有消息落 `agent_messages`，前端「AI 之间沟通」页可见。

**提示词三级覆盖**：`ai_prompts`（scope=global/account/item，priority 大者胜）→ 与 `ai_roles.system_prompt` 拼接。默认提示词写在 `backend/app/agents/defaults.py`，**用户可在 UI 里改**。

**提供者路由**：每个 role 指定 `provider_id + model`（`ai_roles`）；未指定则用 `providers.is_default=1`。所有调用统一走 `backend/app/agents/llm.py::chat()`（OpenAI 兼容 `/chat/completions`），带超时、重试 1 次、token 计量 → 落 `agent_runs`。

## 5. DSH 插件（MCP）

`mcp_server/server.py`：stdio JSON-RPC MCP server，工具集与主管 AI 的 tool 集**同一份定义**（`backend/app/agents/tools.py::TOOL_SPECS`）：

```
accounts_list / accounts_set_enabled / accounts_relogin
items_list / items_get / items_edit / items_publish / items_batch_edit / items_sync
chats_list / chats_read / chats_send / chats_set_mode
orders_list / orders_deliver
risk_status / risk_clear
providers_list / providers_upsert
master_chat                       -- 把我的话转给主管 AI
```
调用方式：HTTP 打 `http://127.0.0.1:<port>/api/...`，token 从 `%APPDATA%\xianyu-ai-workbench\data\mcp_token.txt` 读（后端启动时写入）。这样 DSH 里就能直接指挥这个软件。

## 6. 搬运清单（从旧项目搬，禁止整包复制）

| 旧文件 | 新位置 | 处理 |
|---|---|---|
| `utils/qr_login.py` | `backend/app/xianyu/qr_login.py` | 保留 `_sanitize_no_proxy()` 修复 |
| `app/services/session_store.py` | `backend/app/core/security.py` 合并 | 会话落库+滑动续期 |
| `utils/risk_control.py` | `backend/app/xianyu/risk.py` | 熔断阶梯 + `is_session_expired` |
| `utils/user_agents.py` | `backend/app/xianyu/fingerprint.py` | **实测值**（Chrome 148 / `Not/A)Brand`） |
| `utils/browser_stealth.py` | `backend/app/xianyu/stealth.py` | 同上 |
| `utils/manual_captcha.py` | `backend/app/xianyu/captcha.py` | 系统 Chrome + 持久化 profile + x5sec 硬校验 |
| `app/db_manager.py` 的 Cookie 读写 | `backend/app/xianyu/store.py` | 只搬必要的，别搬 7000 行 |
| `utils/xianyu_mtop.py` 等协议请求 | `backend/app/xianyu/mtop.py` | 按需 |

**原则**：只搬「协议/风控/登录」这些踩过坑的部分，**业务逻辑全部重写**（旧项目那套 9000 行单体不要）。

## 7. 验收判据（整体）

1. `python -m backend.app.main` 起得来，`GET /api/health` 200；`desktop/main.py` 能打开桌面窗口。
2. 冒烟：登录 → 扫码/搬运账号 → 商品同步 → 回复台收发 → AI 自动回复（可 mock provider）→ 主管 AI 对话 → MCP 工具可被外部调用。
3. 测试：`backend/tests/` 全绿（unittest，`PYTHONPATH=.`，每个文件独立进程）。
4. 风控不退化：搬过来的指纹/风控/会话测试全过（这些是最容易写坏的部分）。
5. 打包：`scripts/build_exe.ps1` 产出单个 exe，双击能起窗口。
