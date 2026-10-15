# AI/MCP 可调用面收口（第 58 批 / task-8）

**结论**：独立审计 `docs/audit/write_consistency.md` §0 结论 2 / 问题 9 说「123 条写路由里 42 条没有
AI/MCP 面」，逐条复核后：**真正该接的只有 2 条**（单条上下架），已接上并配齐测试；
其余 37 条**有书面理由不接**（纯本地/语义只读 13、一代扁平且前端零调用 5、登录与凭据 5、
配置面 10、本地历史与内部入口 4、其它批次负责 1），另有 1 条（批量发布）属「要接也得另立语义」。
报告点名的「确认真实缺口」里，**超级擦亮写与风控写都不是缺口**（见 §2，含 `文件:行` 证据）。

数字（本批改动后实测）：
- `WRITE_ROUTES=124`（含 task-9 新增的 `POST /api/notifications/{account_id}/read`）
- `NO_MCP_FACE=39` = 改动前 40 − 本批接入 2 + task-9 新增 1
- 五方对账 `specs=150 master=149 routes=151 targets=138 handlers=150 / write_tools=74 / ALIASES=22 / RECON OK`

---

## §1 本批接入（2 条）

| 接口 | 工具名 | 服务层落点 | 闸门 | 测试 |
|---|---|---|---|---|
| `POST /api/items/{account_id}/{item_id}/delist` | `items_delist` | `backend/app/services/items.py:911 downshelf(item_ids, account_id=None, *, confirm=None)` | 服务层第一道（`_confirm_gate` → `common.confirm_required`，items.py:958-960）+ MCP 侧 `confirm` body 闸门 | `backend/tests/test_ai_surface58.py`（14 用例） |
| `POST /api/items/{account_id}/{item_id}/relist` | `items_relist` | `backend/app/services/items.py:920 reshelf(item_ids, account_id=None, *, confirm=None)` | 同上 | 同上 |

为什么是这两条：前端一直在用（`frontend/src/pages/Items.tsx:471` 单条下架、`:511` 单条上架），
而 AI/MCP 面里只有「批量下架 `items_batch_offline` / 批量删除 `items_batch_delete`」和
「批量改 `items_batch_edit`」——**模型想下架一件商品，只能被迫选批量工具**（语义更重、风险更大）。
这是典型的能力级缺口：有写路由、有服务函数、有前端调用，就是没有工具。

改动清单（全部在 task-8 写盘范围内）：

| 文件 | 位置 | 改了什么 |
|---|---|---|
| `backend/app/agents/tools.py` | :2174-2211 | 两条 TOOL_SPECS（`required=[account_id,item_id,confirm]`、`additionalProperties:False`、描述带 `_WRITE_HINT`） |
| `backend/app/agents/tools.py` | :3396-3397 | `_SERVICE_TARGETS`：`items_delist → ['items','downshelf']`、`items_relist → ['items','reshelf']` |
| `backend/app/agents/tools.py` | :3485-3487 | 新增 `_SERVICE_ARG_MAPS`：`item_id → item_ids`（路由路径参数是单数，服务层收序列） |
| `backend/app/agents/tools.py` | :3534 | `_dispatch_service` 里按 `_SERVICE_ARG_MAPS` 改名后再 `_filter_args` |
| `backend/app/agents/tools.py` | :3686-3687 | `_HANDLERS` 两条 lambda |
| `mcp_server/client.py` | :799-809 | `TOOL_ROUTES` 两条：POST + `body=['confirm']`（闸门据此生效） |
| `mcp_server/client.py` | :1310-1333 | `CANONICAL_SPECS` 两条 `input_schema` |
| `scripts/reconcile_tools.py` | :187-188 | `WRITE_PREFIX` 登记 `'items_delist','items_relist'`（**完整工具名**，不用 `items_` 宽前缀 —— 会误伤只读的 `items_fans_price` / `items_listing_precheck` / `items_sync`）。Lead 裁决授权改这一处 |
| `backend/tests/test_ai_surface58.py` | 新文件 | 14 用例：spec/路由/客户端闸门/服务层闸门/派发改名/路由层只透传/五方登记反推 |
| `backend/tests/test_agents_tools_workbench.py` | :573-582 | `WRITE_TOOLS` 登记两条（走「名字↔服务函数↔路由」三方对账） |
| `backend/tests/test_agents_tools_workbench.py` | :899-922 | `test_spec_properties_are_accepted_by_the_service_function` 认 `_SERVICE_ARG_MAPS`（否则合法的改名会被当成「参数被静默丢掉」） |
| `backend/tests/test_agents_cross.py` | :555-557、:601-602 | `test_tool_names_match_contract` 的黄金名单 + 写工具名单各加两条（黄金名单是**逐序**相等，漏加必红） |
| `backend/tests/test_mcp_server.py` | :959-967、:1000-1015 | 去掉写死的 `72`：写工具清单真源是 `agents/tools.py`（集合相等已断言），写死数字会让每个接新工具的批次改同一行、多批次必撞车；改成下限 70 防「清单被清空 ⇒ 假绿」 |

**闸门链路（Lead 追问的 ①，已确认）**：`mcp_server/client.py:1040-1041` 在拼参数**之前**判
`if 'confirm' in (route.get('body') or []) and not _truthy(args.get('confirm'))` ⇒ 回 `confirm_required`、
一个 HTTP 请求都不发；`backend/app/api/items.py:233-247` 的 `delist_item` / `relist_item` **原样透传**
（`items.downshelf([item_id], account_id=account_id, confirm=(payload or {}).get('confirm'))`），
真正的判定在服务层（items.py:958-960）。路由层不重复判一次，避免两处口径漂移。

---

## §2 与 `docs/audit/write_consistency.md` 的差异（3 处，逐条给证据）

1. **「超级擦亮写没有 ad 写工具」不成立**。已有通用 action 工具覆盖 10 个写动作：
   `backend/app/agents/tools.py:2045 promotion_write_actions`（只读动作目录）+
   `:2059 promotion_write_execute`（写，required 含 confirm）；`_SERVICE_TARGETS`
   `tools.py:3386/3387` → `['ad_write','actions_catalog']` / `['ad_write','execute']`；
   `_HANDLERS` `tools.py:3678/3679`；MCP 侧 `mcp_server/client.py:763/767` 有对应 `TOOL_ROUTES`。
   覆盖的动作为 `campaign_edit/delete/batch_delete/pause/start/start_before/item_campaign_renew/
   activity_apply/activity_task_execute/account_log_operates`（`backend/app/xianyu/ad_write.py:73-122`、
   `backend/app/services/ad_write.py:111-148`）。另：`backend/app/api/ad_write.py:9-10` 的 docstring
   说「没有挂进 main.py」也已过期 —— `backend/app/main.py:75` 的 `ROUTE_MODULES` 含 `'ad_write'`，
   `:390-400` 循环 `include_router`。
2. **风控写不是「能力级缺口」**。报告点名的 `backend/app/api/risk.py:49/55/65`：
   `:49 put_settings` → `PUT /api/risk/settings`、`:55 clear` → `POST /api/risk/clear`，两条都是
   **纯本地**（清本机冷却 / 改本机设置，`backend/tests/test_write_routes_have_gates.py:73-78` 已登记为
   「合法无闸门」）；`:65 clear_for_account` → `POST /api/risk/{account_id}/clear` **已有** `risk_clear`
   工具（`mcp_server/client.py:413-416`）。给 AI 面加「清冷却」等于给模型一个绕过风控的按钮，收益为负。
3. **口径差 2 条**：报告说 42，我按「`app.openapi()` 的非 GET 路由 vs `TOOL_ROUTES` 的 path」实测
   改动前为 **40**。差的 2 条未定位到报告的具体口径（可能是报告把 PATCH 单列，或把
   `DELETE /api/items/{account_id}/{item_id}`、`POST /api/accounts/{account_id}/toggle` 一类算了进去），
   本节不做猜测；下面 §3 把实测的 39 条**逐条**给了结论，可逐条复核。

---

## §3 剩余 39 条写路由：逐条「接口 → 接/不接 → 理由」

### A. 纯本地 / 语义只读（13 条，`test_write_routes_have_gates.py:40-83` 已登记为合法无闸门）

| 接口 | 接？ | 理由 |
|---|---|---|
| `POST /api/accounts/{account_id}/captcha` | 不接 | 纯本地清冷却（白名单 :43-44）；接了等于给模型一个「假装过了滑块」的按钮 |
| `POST /api/accounts/{account_id}/refresh-profile` | 不接 | 只读：拉平台资料回写本地（:45-46） |
| `POST /api/chats/classify` | 不接 | 纯本地让 LLM 建议回复（:49-50）；AI 面已有 `master_chat` 与 `chats_send` |
| `POST /api/chats/sync` | 不接 | 只读同步（:51-52）；读工具直接读本地库即可，同步由后端长连接负责 |
| `PUT /api/chats/{conversation_id}/ai` | 不接 | 纯本地开关（:53-54）；接待模式已有 `chats_set_mode` |
| `POST /api/chats/{conversation_id}/ai-draft` | 不接 | 纯本地拟稿（:55-56）；模型自己就能写草稿 |
| `POST /api/chats/{conversation_id}/read` | 不接 | 纯本地清未读计数（:59-60） |
| `POST /api/items/sync` | 不接 | 只读同步 + 一代扁平路径（:61-62）；账号级 `items_sync` 已有面 |
| `POST /api/orders/sync` | 不接 | 只读同步（:69-70） |
| `POST /api/orders/{order_id}/deliver` | 不接 | 纯本地只改状态（:71-72）。真发货的 AI 面是 `orders_deliver` → `PUT /api/orders/{order_id}/shipment`（`mcp_server/client.py:402-407` 注释写明：以前指向这条导致外部 MCP「发货其实从来没发出去过」） |
| `POST /api/risk/clear` | 不接 | 纯本地清冷却（:73-74）；账号级 `POST /api/risk/{account_id}/clear` 已有 `risk_clear`（client.py:413-416） |
| `PUT /api/risk/settings` | 不接 | 纯本地改风控设置（:77-78），无平台副作用 |
| `POST /api/uploads/images` | 不接 | 本地文件换 URL（:81-82）；multipart 上传不适合现有 JSON body 的工具面 |

### B. 一代扁平路径、前端零调用（5 条）

`grep` `frontend/src` 对 `items/batch`、`items/publish`、`items/downshelf`、`items/reshelf` **零命中**；
前端全部走账号级路径（`Items.tsx:557` batch-offline、`:582/:628` batch-delete、`:2336` edit、
`:2695` batch-edit、`:2987` quick-edit、`:3658` publish、`:471/:511` delist/relist）。

| 接口 | 接？ | 理由 |
|---|---|---|
| `POST /api/items/batch` | 不接 | 一代扁平；账号级 `items_batch_offline` / `items_batch_delete` / `items_batch_edit` 已有面 |
| `POST /api/items/publish` | 不接 | 一代扁平；账号级 `items_publish` → `POST /api/items/{account_id}/publish`（client.py:160-165） |
| `POST /api/items/downshelf` | 不接 | 一代扁平；账号级单条下架本批已接 `items_delist` |
| `POST /api/items/reshelf` | 不接 | 一代扁平；账号级单条上架本批已接 `items_relist` |
| `PUT /api/items/{item_id}` | 不接 | 一代扁平；账号级 `items_edit` → `POST /api/items/{account_id}/{item_id}/edit`（client.py:155-159） |

**为它们加重定向工具是纯凑数**：会给模型两套语义相同、只差路径形状的名字，反而提高选错概率。

### C. 登录 / 凭据 / 人机交互（5 条）

| 接口 | 接？ | 理由 |
|---|---|---|
| `POST /api/accounts/qr/start` | 不接 | 扫码登录必须人拿手机扫（`frontend/src/pages/Accounts.tsx:485-539`）；AI 面只能拿到一张没人扫的二维码，还会写 desktop 凭据 |
| `POST /api/accounts/qr/poll` | 不接 | 同上；且前端轮询用的是 `GET /api/accounts/qr/{ticket}`（Accounts.tsx:572），这条本身没人调 |
| `POST /api/auth/desktop` | 不接 | 桌面凭据登录。MCP 面已用 Bearer token（`mcp_server/client.py` BackendClient token）；把登录交给模型 = 把凭据面开放给模型 |
| `POST /api/auth/login` | 不接 | 同上 |
| `POST /api/auth/logout` | 不接 | 同上；模型调它会自毁当前会话 |

### D. 配置面（LLM 供应商 / 角色 / 提示词 / 全局设置，10 条）

判据：这些是「人管 AI」的旋钮，不是「AI 干活」的能力；其中删供应商/改角色提示词还会直接影响模型自己的通道与人格。

| 接口 | 接？ | 理由 |
|---|---|---|
| `DELETE /api/providers/{provider_id}` | 不接 | 误调会删掉模型自己的通道（前端 Providers.tsx:120） |
| `PUT /api/providers/{provider_id}` | 不接 | 改配置（Providers.tsx:174/531）；新增/修改已有 `providers_upsert`（client.py:418-422） |
| `POST /api/providers/{provider_id}/default` | 不接 | 切默认供应商（Providers.tsx:163） |
| `PUT /api/providers/{provider_id}/default` | 不接 | 同上（同义双写） |
| `POST /api/providers/{provider_id}/test` | 不接 | 探活会真花第三方额度（Providers.tsx:141），属运维面 |
| `PUT /api/agents/roles/{role}` | 不接 | 改角色提示词/模型绑定（Agents.tsx:288、Providers.tsx:761）—— 给模型改自己人格的写权限 |
| `POST /api/agents/prompts` | 不接 | 提示词库增（Prompts.tsx:653） |
| `PUT /api/agents/prompts/{prompt_id}` | 不接 | 提示词库改（Prompts.tsx:293/652） |
| `DELETE /api/agents/prompts/{prompt_id}` | 不接 | 提示词库删（Prompts.tsx:277） |
| `PUT /api/settings` | 不接 | 全局设置（Agents.tsx:122），纯本地但影响所有账号行为。**注意它不在 `GATE_FREE_ALLOWLIST`** ⇒ 将来若要接，必须先补 confirm 闸门（本条是给后续批次的提醒，不是本批改动） |

### E. 本地历史与内部入口（4 条）

| 接口 | 接？ | 理由 |
|---|---|---|
| `DELETE /api/agents/chat/messages` | 不接 | 清本地 AI 聊天历史（Master.tsx:305），无平台副作用 |
| `POST /api/agents/reply` | 不接 | **只生成不发送**（`backend/app/api/agents.py:330-340` docstring 明说），纯本地 LLM |
| `POST /api/accounts/{account_id}/toggle` | 不接 | 纯本地改 enabled + 起停 IM（`backend/app/api/accounts.py:379-393`）；前端零调用（`grep '/toggle'` 无命中）；账号启停的 AI 面是 `accounts_set_enabled` → `PATCH /api/accounts/{account_id}`（client.py:139-142） |
| `POST /api/chats/incoming` | 不接 | **买家来信入口**（`backend/app/api/chats.py:12` 注释、`:129-206 handle_incoming`）：平台 IM 推送 / 桥接回放的内部入口。AI 面接它 = 让模型伪造买家来信触发自动回复 ⇒ 明确不接 |

### F. 另有 2 条单独说明

| 接口 | 接？ | 理由 |
|---|---|---|
| `POST /api/items/{account_id}/batch-publish` | 不接（本批） | 账号级批量发布，但**前端零调用**（`grep 'batch-publish' frontend/src` 零命中）；语义是「一批不同表单逐条发布 + 逐行 confirm 覆盖 body confirm」（`backend/app/api/items.py:140-153`），原样套进工具面会让闸门语义变模糊。要接应**另立**语义单一的新工具（一次 confirm 管一批），而不是重定向这条双层 confirm 路径 |
| `POST /api/notifications/{account_id}/read` | 不接（本批） | 由 **task-9（notify-write）** 负责端到端接入；本批不重复接，避免两处改同一批文件 |

---

## §4 验证（真实运行结果）

```
$ PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe scripts/reconcile_tools.py
specs=150 master=149 routes=151 targets=138 handlers=150
specs-not-in-routes=[]
routes-not-in-specs(allow=['agents_messages', 'master_chat'])=[]
write_tools=74
route_checked=151
ALIASES=22
RECON OK：五方一致，且每条路由的 path/query/body 都被真实端点接受。

$ for f in test_mcp_server.py test_agents_tools_workbench.py test_ai_surface58.py \
           test_write_routes_have_gates.py test_mcp_routes_match_openapi.py test_contract_routes.py; do
      PYTHONPATH=. PYTHONIOENCODING=utf-8 XIANYU_APP_DIR=/tmp/xywb_$f /c/Python310/python.exe backend/tests/$f; done
=== backend/tests/test_mcp_server.py                Ran 65 tests ... OK
=== backend/tests/test_agents_tools_workbench.py    Ran 27 tests ... OK
=== backend/tests/test_ai_surface58.py              Ran 14 tests ... OK
=== backend/tests/test_write_routes_have_gates.py   Ran 4 tests ... OK
=== backend/tests/test_mcp_routes_match_openapi.py  Ran 7 tests ... OK
=== backend/tests/test_contract_routes.py           Ran 3 tests ... OK

$ PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/run_all.py
TOTAL 64  PASS 64  FAIL 0  CASES 2767  SKIPPED 2
```

（全量 64 个测试文件、2767 用例全绿；2 个 skip 是 test_xianyu_login.py 里原本就跳过的用例。）

`test_ai_surface58.py` 的 14 个用例覆盖：spec 闸门声明 / required 已声明 / 路由 method+path+path_params+body /
CANONICAL_SPECS 一致 / 路径真注册（用 `app.openapi()`，`app.routes` 里是 `_IncludedRouter` 没有 `.path`）/
没 confirm 不发请求 / confirm=true 打到正确路径与 body / `_SERVICE_TARGETS`+`_HANDLERS` /
`_SERVICE_ARG_MAPS` 改名 / 服务函数签名收 `item_ids`+`confirm` / 派发把 `item_id` 映射成 `item_ids` /
服务层无 confirm 回 `CONFIRM_CODE` / 路由层只透传（AST 检查 `confirm=` 与 `downshelf`/`reshelf`）/
反推「带 confirm 的写工具必须登记进 `WRITE_PREFIX`」。

## §5 未完成 / 不确定

1. **口径差 2 条**（报告 42 vs 实测 40）未定位到报告的具体算法；本批按实测 39 条逐条给了结论。
2. `PUT /api/settings` 不在 `GATE_FREE_ALLOWLIST`，但当前 `test_write_routes_have_gates.py` 是绿的
   —— 说明它的闸门判定走的是另一条口径（非本任务范围），建议后续批次核实。
3. 本批**未**为任何一代扁平路径加重定向工具（刻意的，理由见 §3 B）。
4. `scripts/_probe_ai_gap.py` 是本次审计用的临时探针（只读：`app.openapi()` 的非 GET 路由 vs
   `TOOL_ROUTES` 的 path），**未入仓**，复核时可直接按该口径重写。
