# 审计报告：AI 链路（商品 AI / 跨商品协查 / 提示词 / Provider / 主 AI / 人工接管 / MCP）

- 审计员：`audit-ai`（**只读审计，未修改任何源码**；本报告是唯一写入的文件）
- 审计时间：2026-10-06
- 被审对象：`C:\Users\Administrator\Projects\xianyu-ai-workbench` @ `2f3425a`
- 证据来源：**运行中的真实后端**（临时数据目录 `C:\Users\Administrator\AppData\Local\Temp\xaiw-audit-ai\app`，端口 18080）
  + **本地 LLM HTTP 桩**（`127.0.0.1:18081`，OpenAI 兼容 `/chat/completions`，逐条记录收到的请求）+ 进程内纯函数直测
- 边界遵守：**未调真实 LLM**（全部走本地桩）、未打真实闲鱼平台、未 `git commit`、未改源码
- 报告口径：每项给「结论 / 证据（原样命令 + 关键输出）/ 根因（`文件:行`）/ 最小修法」；没跑到的明确写 **未验证**

---

## 0. 直接回答用户原话「是不是都正常？怎么一堆问题？」

用户九条诉求里属于 AI 链路的 6 条，**逐项真实判定**如下（不是「应该没问题」）：

| # | 诉求 | 判定 | 一句话 |
|---|---|---|---|
| ① | 一个商品配一个 AI | **真通** | 商品级提示词真进了给模型的 `system`，`item` 角色真跑通，且能按角色指定 `provider_id`+`model`（实测 model = `item-role-model`） |
| ② | 跨商品 AI 互相沟通 / 主 AI 控制 | **真通，但有一处会答错话的缺陷** | `item:itA → cross → item:itB → cross → item:itA` 四跳全部落库，深度上限与 3s 超时都真实生效；**但跨商品匹配不按账号过滤**，A 号会话会把 B 号的商品拉进来回答（AI-1） |
| ③ | 助词（提示词）自己填 | **真通，但有 3 处语义坑** | 三级覆盖、priority、增删改都真跑通；可是「角色精确匹配压过 priority」（AI-4）、「同级同 owner 静默覆盖」（AI-5）、前端说明文案与实现不符（AI-6）——**用户最容易在这里说「我填了没用」** |
| ⑤ | 统一 API Key 管理 | **API 真通；存储侧不安全** | 增删改/default/test/角色路由全部正常，且**不回传前端明文**；但 **API Key 明文存 SQLite**（AI-3） |
| ⑥ | 主 AI 对接用户 / 插件让 DSH 控制 | **真通** | `POST /api/agents/master/chat` 可对话、可工具调用并 4 轮封顶；MCP `--selftest` 与 `tools/list` 都是 **21** 个工具，9 个只读工具逐个真调成功 |
| ⑦ | 用户也能自己回消息 | **真通** | `manual`/`off` 下买家来信**完全不生成 AI 回复**，`auto` 恢复；人工 `/send` 正常落库 |

**没有发现「整个功能是假的」的项**，但发现 **6 条需要修的缺陷**（AI-1…AI-6），其中最该先修的是 **AI-1（跨账号串台）** 和 **AI-2（bargain/vision 仍是死角色）**。

---

## 1. 复现环境（所有证据都出自这个实例）

### 1.1 本地 LLM 桩

`C:\Users\Administrator\AppData\Local\Temp\xaiw-audit-ai\stub_llm.py`

- 监听 `127.0.0.1:18081`，实现了 OpenAI 兼容 `POST /chat/completions`；
- 把**每个请求原样**（`path` / `Authorization` / 完整 `body`）追加写 `stub_requests.jsonl`，便于事后核对「到底给模型喂了什么」；
- 普通请求回固定内容 `桩回复-OK`；`user` 内容含 `__TOOL__` 时回一个 `items_list` 的 `tool_calls`；含 `__SLOW__` 时先 `sleep(6)`（用来测 3s 超时）。

```
C:\Python310\python.exe C:\Users\Administrator\AppData\Local\Temp\xaiw-audit-ai\stub_llm.py 18081
```

### 1.2 后端（临时数据目录，不污染真实数据）

```
cmd /c "set XIANYU_APP_DIR=C:\Users\Administrator\AppData\Local\Temp\xaiw-audit-ai\app&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe -m backend.app.main --port 18080"
```

用 `Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=...}` 起（PID 29420）。
`GET /api/health` → `{"success":true,"data":{"ok":true,"version":"0.1.0","frozen":false}}`。

token：读 `C:\Users\Administrator\AppData\Local\Temp\xaiw-audit-ai\app\data\local_secret.txt`，
`POST /api/auth/desktop` 带 `X-Desktop-Secret: <secret>` → `data.token`。

### 1.3 Provider 指向本地桩

```
POST /api/providers  {"name":"local-stub","base_url":"http://127.0.0.1:18081","api_key":"sk-stub-SECRET-abcdef123456","default_model":"stub-default-model","is_default":true}
PUT  /api/agents/roles/item  {"provider_id":<id>,"model":"item-role-model"}
```

### 1.4 复现脚本（都在临时目录，未入库）

| 脚本 | 覆盖内容 |
|---|---|
| `e2e_1_prompts.py` | 三级提示词、`item` 角色真跑、桩请求回放 |
| `e2e_2_cross_roles.py` | 跨商品四跳链路、`match_items` 精度、六角色可达性 |
| `e2e_3_roles_master_sec.py` | 角色矩阵、主 AI + 工具调用、深度/超时、Provider 安全 |
| `e2e_4_timeout_mode.py` | 3s 超时（清代理 env）、人工接管 mode、provider test |
| `e2e_5_itB_and_scope.py` | 跨品分支的 item:B 提示词、跨账号匹配 |
| `e2e_6_prompt_order.py` | role 精确匹配 vs priority、重复 POST、孤儿行 |
| `e2e_7_same_level.py` | 同级同 owner 覆盖行为 + 桩复验 |
| `parse_mcp.py` | 解析 MCP stdio 输出 |

---

## 2. ① 一个商品配一个 AI —— **真通**

### 结论

- 商品级提示词**真的**进了给模型的 `system` 消息，顺序 global → account → item，且同级 `priority` 大者胜。
- 商品上下文（标题/售价/原价/库存/描述）单独作为一条 `system` 注入。
- `item` 角色真能跑完全链路，且**按角色指定的 model 生效**（`item-role-model`，不是 provider 的 `stub-default-model`）。

### 证据

灌两件商品（`items` 表，account `acc1`）：`itA`=「苹果 iPhone 13 128G 蓝色」、`itB`=「Levi's 牛仔裤 32码 九成新」。
建 4 条提示词：`global`(priority 1) / `account acc1`(priority 1) / `item itA`(priority 1) / `item itA`(priority 9)。

```
GET /api/agents/roles/item/prompt-preview?item_id=itA&account_id=acc1
```

输出（末尾部分，标记是刻意用 ASCII 写的，便于机器核对）：

```
ITEM_HIGH_PRIORITY_WINS        <- priority 9，胜
ACCOUNT_MARKER_MIDDLE
GLOBAL_MARKER_SHOULD_BE_FIRST
```

注意 `ITEM_LOW_PRIORITY_BAD`（与 ITEM_HIGH 同 scope/owner，priority 1）**没出现** → 同级 priority 大者胜成立。

真跑：

```
POST /api/chats/incoming {"account_id":"acc1","buyer_id":"buyer_audit_1","item_id":"itA","peer_nickname":"审计买家","content":"这个 iPhone 13 还在吗？"}
```

返回：

```json
{"ai_result":{"ok":true,"reply":"桩回复-OK","role":"item","provider_id":3,"model":"item-role-model","usage":{"prompt_tokens":7,"completion_tokens":2},"cross_used":false,"success":true},
 "ai_reply":{"id":2,"sender":"ai:item","sent_ok":false,"meta":"{\"ai_generated\": true, \"send_error\": \"缺少平台会话 id（cid），AI 回复未发送\"}"},
 "mode":"auto","ai_send":{"sent":false,"code":"missing_cid"}}
```

桩侧实际收到的请求（`stub_requests.jsonl` 回放）：

```
--- 桩收到 model = item-role-model | path = /chat/completions | auth = Bearer sk-stub-SECRET-abcdef123456
    [system] …(role 的系统提示词)… GLOBAL_MARKER_SHOULD_BE_FIRST … ACCOUNT_MARKER_MIDDLE … ITEM_HIGH_PRIORITY_WINS
    [user]   买家：这个 iPhone 13 还在吗？
    [system] 【你正在服务的商品】商品标题：苹果 iPhone 13 128G 蓝色 / 当前售价：2999.0 元 / …/ 商品描述：自用机，无拆修
    [user]   这个 iPhone 13 还在吗？
```

**这就是「商品级提示词真的进入了模型输入」的直接证据**（不是读代码猜的）。

### 顺带确认（口径诚实，不算缺陷）

`ai_send.sent=false, code=missing_cid` 是**符合契约**的降级：HTTP 手工灌入的来信没有平台 `cid`，AI 回复只落库不发送。
**未验证**：真实 IM 连接（有 `cid`）下 AI 回复能否真发出去 —— 需要真实闲鱼账号，本次不越界。

---

## 3. ② 跨商品 AI 互相沟通 —— **真通，但会串账号（AI-1）**

### 3.1 四跳链路、深度、超时全部真实生效

```
POST /api/chats/incoming {"account_id":"acc1","buyer_id":"buyer_audit_cross","item_id":"itA","content":"你那个牛仔裤还有吗？"}
```

返回 `role=item, cross_used=true`。`agent_messages` 落库链路：

```
item:itA | cross | ask    | other_item_info | {"question":"你那个牛仔裤还有吗？","candidate_items":["itB"],"depth":1}
cross   | item:itB | ask  | other_item_info | {"question":"…","depth":2,"item_id":"itB"}
item:itB| cross | answer  | other_item_info | {"answer":"桩回复-OK","item_id":"itB","title":"Levi's 牛仔裤 32码 九成新"}
cross   | item:itA | answer| other_item_info | {"answer":"桩回复-OK","question":"…","depth":1,"answered_items":["itB"]}
```

桩侧共 3 次调用，且**第二次确实用的是商品 B 的提示词**：

```
model=item-role-model      system 首行=你是这个闲鱼商品的专属客服 AI…      <- itA 的 AI
model=stub-default-model   system 首行=你是「跨商品协查 AI」：买家在 A 商品的会话里问到了 B 商品…  <- cross
model=item-role-model      system 首行=你是这个闲鱼商品的专属客服 AI…      <- itB 的 AI
```

进一步验证「跨品分支里商品 B 的**商品级提示词**也生效」（给 itB 单独加一条 `ITEM_B_MARKER_ONLY_FOR_B`）：

```
model=item-role-model | 系统提示含 ITEM_B_MARKER_ONLY_FOR_B = True  | 含 GLOBAL = True
model=stub-default-model | 系统提示含 ITEM_B_MARKER_ONLY_FOR_B = False | 含 GLOBAL = True
```

深度上限：

```
cross_ask(depth=3) -> ValueError: 跨商品协查深度超限：3 > 2
```

3s 超时（桩 `sleep(6)`，**已清掉本机代理环境变量**，原因见 AI-6 附注）：

```
cross_ask(depth=1, 桩 sleep 6s) 耗时 3.0s -> '这件商品我这边暂时问不到准确信息，我先帮你记下，稍后让老板确认。'
agent_messages: item:itB | cross | notify | {"error":"TimeoutError: ","item_id":"itB"}
```

→ 超时后**落 `kind=notify`**、返回兜底话术、不让买家空等。`ask|answer|notify` 三种 kind 都实测出现。

`GET /api/agents/messages?conversation_id=<id>` 返回的 4 条，正是 `frontend/src/pages/Agents.tsx:47-82` 聚合「协查链」的输入；
按 `(conversation_id, topic)` 分组成 1 条链，`items=[itA,itB]` → `isCross=true` → 会打上「跨商品」Badge（`Agents.tsx:208,225`）。
**未验证**：前端页面的真实渲染截图（未起 `pnpm dev:web`）。

### 3.2 AI-1（P1）：跨商品匹配**不按账号过滤**，A 号会话会拉 B 号商品

前半部分（`match_items`）是对的 —— 历史「中文永远 0 命中」确实修好了：

```
match_items('你那个牛仔裤还有吗？', exclude itA) -> [('itB','acc1')]
match_items('是九成新吗？', exclude itA)          -> []          <- 停用词挡住了成色词误命中
match_items('全新吗？', exclude itA)              -> []
```

但**加一件属于另一个账号 `acc2` 的商品 `itC`（「苹果 iPhone 13 256G 黑色 全新」）后**：

```
match_items('苹果 iPhone 13 128G 那个还有吗', exclude itA) -> [('itC','acc2')]
POST /api/chats/incoming {"account_id":"acc1","item_id":"itA","content":"iPhone 13 那个手机还有吗？"}
  -> role=item cross_used=true
  agent_messages 目标: [('cross',), ('item:itC',), ('item:itA',)]
```

**买家在 `acc1` 的商品会话里，AI 跑去问 `acc2` 的商品 AI，并可能拿 `acc2` 的价格/库存回答。**

- 根因：`backend/app/agents/runtime.py:208` 的候选集是 `SELECT account_id, item_id, title, price, stock, status FROM items ORDER BY id DESC LIMIT 500` —— **没有 `WHERE account_id = ?`**；
  调用处 `runtime.py:275`（`match_items(content, exclude_item_id=conv['item_id'])`）也没把 `conv['account_id']` 传下去。
- 最小修法：给 `match_items` 增加 `account_id` 参数（`reply_for_message` 传 `conv['account_id']`），SQL 加 `WHERE account_id = ?`；顺手按 `status` 过滤下架/无库存商品。

---

## 4. ③ 六角色可达性 —— **`bargain`/`vision` 仍然是死角色（AI-2）**

### 结论

`roles.py` 的**规则本身是对的**，但**调用方每次都会把 `agent_role` 写死**，导致规则 1 永远短路：

| 角色 | 判定 | 说明 |
|---|---|---|
| `item` | 真通 | §2 实测 |
| `cs` | 真通 | 无 `item_id` 的来信 → `role=cs`（实测） |
| `cross` | 真通 | 协查汇总（实测，model 用 cross 角色） |
| `master` | 真通 | `/api/agents/master/chat`（实测） |
| `bargain` | **不通（死角色）** | 见下 |
| `vision` | **不通（死角色）** | 见下 |

### 证据

纯函数矩阵（`roles.role_for_conversation`）：

```
agent_role=item + 讲价话术 -> item      <- 规则1 短路
agent_role=cs   + 讲价话术 -> cs        <- 规则1 短路
agent_role=''   + 讲价话术 -> bargain   <- 只有 agent_role 为空才轮得到
agent_role=''   + 图片     -> vision    <- 同上
agent_role=item + 图片     -> item      <- 图片也被短路
agent_role=''   + 无商品   -> cs
agent_role=''   + 有商品   -> item
```

HTTP 真跑（会话是正常路径建的，`agent_role='item'`）：

```
POST /api/chats/incoming {... "item_id":"itA","content":"便宜点行吗？少一点可以吗"}
  -> {"role":"item"}  conversations.agent_role = "item"          <- 讲价 AI 没被选中
```

图片消息同理（`messages.meta={"image_url":...}`，走 `POST /api/agents/reply`）：

```
POST /api/agents/reply(agent_role='item', image) -> {"role":"item"}     <- 看图 AI 没被选中
POST /api/agents/reply(agent_role='',     image) -> {"role":"vision"}   <- 手工把 agent_role 置空才有
```

而生产路径**永远**会写入一个合法角色：

- `backend/app/services/chats.py:74` `default_agent_role(item_id)` → `'item'` / `'cs'`
- `chats.py:127` `agent_role or 'item'`（建会话）
- `chats.py:508-511`（`handle_incoming` 自建会话）、`chats.py:788-790`（`dispatch_im_message`）都显式传 `agent_role=default_agent_role(item_id)`

所以 `roles.py:111-113` 的「规则 1」在任何真实来信上都会命中，`vision`（`roles.py:114-115`）与 `bargain`（`roles.py:117-118`）**永远轮不到**。

附：`/api/chats/incoming` 的 `meta` 只透传 `{'source': ...}`（`backend/app/api/chats.py:135`），**HTTP 入口根本没法带图**；
`dispatch_im_message` 的 `meta` 也只有 `{'source','cid'}`（`chats.py:795`）—— 即使角色规则通了，**图片路径也没有生产者**。

- 最小修法（二选一，推荐 B）：
  - A：建会话时**不要**预写 `agent_role`（留空），让规则 2/3 有机会生效；
  - B：在 `handle_incoming`/`reply_for_message` 里把 `default_agent_role` 仅作为「无 `item_id` 时的兜底」，并在 `dispatch_im_message` 里把 IM 帧的图片地址塞进 `meta['image_url']`（协议层 `parse_incoming_message` 需要一并返回该字段）。

---

## 5. ④ 提示词自填（诉求③）—— **真通，但有 3 处会让人以为「我填了没用」**

### 5.1 基本功能真通

- 三级 `scope`（global/account/item）+ `priority` + `enabled` + 增删改：全部实测可用（§2 已给证据）。
- `PUT /api/agents/prompts/{id}` 改 `enabled`、`DELETE` 删除均生效。
- 前端 `frontend/src/pages/Prompts.tsx` 真的挂了这套 API（`Prompts.tsx:43,73,88,277-278`），且读的是 `data.items`（`Prompts.tsx:52`），与后端 `backend/app/api/agents.py:86` 的 `{'items': ...}` **一致**。

### 5.2 AI-4（P2）：**角色精确匹配压过 priority**（前端文案说反了）

```
POST /api/agents/prompts {"scope":"item","item_id":"itA","role":"item","content":"ROLE_SPECIFIC_PRIORITY_1","priority":1}
POST /api/agents/prompts {"scope":"item","item_id":"itA","role":"",    "content":"GENERIC_PRIORITY_9",      "priority":9}
GET  /api/agents/roles/item/prompt-preview?item_id=itA&account_id=acc1
  -> 预览里出现的标记: ['ROLE_SPECIFIC_PRIORITY_1']        <- priority 9 的那条整个消失
```

而同 `scope` 下**只取一条**（不是叠加）：`runtime`/`prompts._pick` 的排序键是 `(role 是否精确匹配, priority, id)`（`backend/app/agents/prompts.py:53`）。

前端却写着「三级提示词会按 **商品 > 账号 > 全局** 的顺序叠加；同一级里 **priority 越大越优先**」（`frontend/src/pages/Prompts.tsx:109`），
并且角色下拉框允许选「某个角色」（`Prompts.tsx:343-346`）。
用户完全可能建一条「所有角色 priority 9」的商品话术，却发现**被另一条 role=item priority 1 的旧话术整条顶掉**，从而报「我填的没生效」。

- 最小修法：`_pick` 改成 `(priority, role 精确匹配, id)`，或把前端文案改成「同级里：先看角色是否精确匹配，再比 priority」。

### 5.3 AI-5（P2）：**同级同 owner 的第二条会静默覆盖第一条**

`upsert_prompt` 的去重键是 `(role, scope, owner)`，**不含 `name`**：

```
POST /api/agents/prompts {"scope":"item","item_id":"itA","name":"话术A","content":"P_ROUND1_AAA","priority":3} -> id=11
POST /api/agents/prompts {"scope":"item","item_id":"itA","name":"话术B","content":"P_ROUND2_BBB","priority":5} -> id=11
GET  /api/agents/prompts 里 itA 的行: id=11 name=话术B priority=5 'P_ROUND2_BBB'   <- 话术A 无提示消失
真跑一次（桩回放）: 含 P_ROUND1_AAA=False / 含 P_ROUND2_BBB=True
```

用户想给同一件商品同一角色写**多段**话术是**做不到**的，而且第一段是**无声丢失**。
根因：`backend/app/agents/prompts.py:115`（先 `SELECT` 再 `INSERT/UPDATE`），且 `ai_prompts` 表**没有 UNIQUE 约束**。
最小修法：要么把 `name` 纳入键并在 UI 上说明「同一商品同一角色只能一条」，要么真支持多条并改成拼接；同时给 `ai_prompts` 加 UNIQUE 并改成 `INSERT ... ON CONFLICT DO UPDATE`（现在是「查-插」两步，并发下会产生重复行）。

### 5.4 AI-6（P3）：孤儿提示词行

```
POST /api/agents/prompts {"scope":"item","name":"orphan","content":"ORPHAN","priority":1}
  -> {"id":10,"scope":"item","account_id":"","item_id":""}
```

`scope=item` 但 `item_id` 为空的行会被接受并长留库里，`system_prompt` 永远查不到它（按 `item_id` 精确匹配）。
前端表单有校验（`Prompts.tsx:258-259`），**API 没有**。最小修法：`create_prompt` 里对 `scope != 'global'` 校验 owner 非空。

---

## 6. ⑤ 统一 API Key 管理 —— **API 真通；Key 明文落库（AI-3）**

### 6.1 功能真通

```
POST /api/providers {...}                          -> 200，返回 id
PUT  /api/agents/roles/item {"provider_id":3,"model":"item-role-model"} -> 200
POST /api/providers/3/test                         -> {"ok":true,"latency_ms":21,"model":"stub-default-model","sample":"桩回复-OK"}
```

角色路由实测生效：`role=item` 用 `item-role-model`，`master`/`cross` 未指定 model → 落 `stub-default-model`（`ai_roles.model` 为空时回退 `providers.default_model`）。
`ai_roles` 六行全部指向 provider 3（含 temperature：item 0.7 / cs 0.6 / vision 0.4 / bargain 0.8 / master 0.3 / cross 0.3）。

### 6.2 **Key 没有明文回前端（这点是好的）**

```
GET /api/providers -> 字段: ["api_key_masked","base_url","created_at","default_model","enabled","has_key","id","is_default","name","updated_at"]
                      api_key_masked = "sk-s********3456"，has_key = true
```

### 6.3 AI-3（P1，安全）：**API Key 明文存 SQLite**

```
sqlite> SELECT id, name, api_key FROM providers;
[(3, 'local-stub', 'sk-stub-SECRET-abcdef123456')]
```

- 根因：`backend/app/api/providers.py` 直接落 `providers.api_key` 原值（`_public` 只在**出站**时 mask，`providers.py:31-42`），DB 表里没有任何加密/DPAPI。
- 影响：`app.db` 被复制/被其它本地程序读取（本机还有 `computer-use`/`minecraft` 等多个 MCP 服务）即等于 Key 泄露；用户在多台机器同步数据目录时更明显。
- 最小修法：落库前用 Windows DPAPI（`CryptProtectData`，与 `core/security.py` 里 Cookie 同级处理）或至少用一个本机派生密钥做对称加密；读取时解密后再用于 `Authorization`。
- **未验证**：真实进程内存/日志里是否也出现明文（本次只验证了 DB 与 HTTP 响应）。

---

## 7. ⑥ 主 AI 对接用户 / 让 DSH 控制 —— **真通**

### 7.1 主 AI 对话 + 工具调用

```
POST /api/agents/master/chat {"message":"帮我看下有哪些账号"}
 -> {"status":200,"reply":"桩回复-OK","model":"stub-default-model","tool_calls":[]}

POST /api/agents/master/chat {"message":"__TOOL__ 帮我看下有哪些账号"}
 -> {"status":200,"耗时s":0.1,
     "reply":"（工具调用轮数已达上限，先停一下）",
     "tool_calls":[{"name":"items_list","result":{"ok":true,"items":[itA,itB]}} ×4]}
```

- 工具调用真跑了（`items_list` 返回真实两件商品）；桩**故意一直回 `tool_calls`**（模拟不收敛的模型），主 AI 在第 4 轮封顶并返回人话，**没有死循环**（`tools.py:356 MAX_TOOL_ROUNDS=4`）。
- 注意：`message` 是必填键名（写成 `text` 会 400「message 不能为空」）—— 前端 `Master.tsx:47` 用的就是 `message`，一致。

### 7.2 MCP（`docs/CONTRACTS.md` §5 为准，21 个）

```
C:\Python310\python.exe mcp_server\server.py --selftest
```

```
工具数量      = 21
工具名        = accounts_list, accounts_set_enabled, accounts_relogin, items_list, items_get, items_edit,
                items_publish, items_batch_edit, items_sync, chats_list, chats_read, chats_send, chats_set_mode,
                orders_list, orders_deliver, risk_status, risk_clear, providers_list, providers_upsert,
                master_chat, agents_messages
--- 别名抽查 ---  list_items -> items_list / get_item -> items_get / send_message -> chats_send / list_agent_messages -> agents_messages
--- 后端连通性 --- {"success": true, "data": {"ok": true, "version": "0.1.0", "frozen": false}}
```

与 `docs/CONTRACTS.md:247-256` 的 21 个规范名**逐一对上**；`agents_messages` 确实是 MCP 独有（`mcp_server/client.py:243 MCP_ONLY_TOOLS`），不在 `TOOL_SPECS`（20 个）里。

`tools/list` 与 9 个只读工具真调（JSON-RPC over stdio，`XYWB_BASE_URL=http://127.0.0.1:18080`、`XYWB_TOKEN=<token>`）：

```
initialize OK: {'name': 'xianyu-ai-workbench', 'version': '0.1.0'}
accounts_list    -> isError=false  {"success":true,"data":[]}
providers_list   -> isError=false  {"success":true,"data":[{"id":3,"name":"local-stub",...}]}
items_list       -> isError=false  {"success":true,"data":{"items":[itB,...]}}
chats_list       -> isError=false  {"success":true,"data":{"conversations":[...]}}
agents_messages  -> isError=false  {"success":true,"data":[{from_agent:"cross",to_agent:"item:itA",kind:"answer",...}]}
risk_status      -> isError=false  {"success":true,"data":{"states":[],"cooldown_steps":[60,180,300,600,1200],...}}
orders_list      -> isError=false  {"success":true,"data":{"orders":[],"total":0}}
chats_read       -> isError=true   {"success":false,"code":"invalid_args","message":"chats_read 缺少必填参数：conversation_id"}
items_get        -> isError=true   {"success":false,"code":"invalid_args","message":"items_get 需要 account_id 与 item_id"}
master_chat      -> isError=false  {"success":true,"data":{"reply":"桩回复-OK","role":"master","model":"stub-default-model","provider_id":3}}
```

缺参时返回结构化 `invalid_args` 而不是崩，说明参数校验是对的。
stdout 保持纯 JSON-RPC（11 个请求 → 11 行响应，日志走 stderr），符合 stdio MCP 要求。

注册文件 `C:\Users\Administrator\.dsh\plugins\mcp-bridge.config.json` 的 `xianyu` 项指向 `mcp_server\server.py`，只设了 `PYTHONIOENCODING=utf-8`，地址/令牌靠 `%APPDATA%\xianyu-ai-workbench\data\{backend_port.txt,mcp_token.txt}` 自动发现 —— 与 `mcp_server/client.py:75-90,441-460` 一致。
**未验证**：重启 DSH 宿主后在 DSH 里真实调用（需要重启宿主，本次不做）。

---

## 8. ⑦ 用户也能自己回消息 / 人工接管 —— **真通**

```
POST /api/chats/{id}/mode {"mode":"manual"} -> 200 mode=manual ai_enabled=False
   POST /api/chats/incoming -> mode=manual  ai_result=None  ai_reply=无     <- AI 完全不生成
POST /api/chats/{id}/mode {"mode":"off"}    -> 200 mode=off    ai_enabled=False
   POST /api/chats/incoming -> mode=off     ai_result=None  ai_reply=无
POST /api/chats/{id}/mode {"mode":"auto"}   -> 200 mode=auto   ai_enabled=True
   POST /api/chats/incoming -> mode=auto    ai_result=item  ai_reply=有     <- 恢复自动回复

POST /api/chats/{id}/send {"text":"亲，这条我自己回你哈"}
 -> {"message":{"id":35,"direction":"out","sender":"human","content":"亲，这条我自己回你哈","sent_ok":false},
     "sent":false, "mode":"manual", "warning":"…未连接或消息发送失败"}
会话最后 4 条: [('out','human',…,0), ('out','ai:item',…,0), ('in','buyer','接管测试 off'), ('in','buyer','接管测试 auto')]
```

- `manual`/`off` 下 AI **真的不生成**（不是生成了没发）——`services/chats.py:538` 的 `if conversation['mode'] == 'auto'` 守卫有效；
- 人工消息 `sender='human'`，AI 消息 `sender='ai:item'`，两者在库里可区分，不互相覆盖；
- `sent_ok=0` 是因为没有真实 IM 连接，**未验证**真实发送。

---

## 9. 其它发现

### 9.1 AI-7（P2，环境相关但可复现）：LLM 调用继承了系统 `HTTP_PROXY`，本机会直接报错

本机环境变量：

```
HTTP_PROXY  = 'http://127.0.0.1:7897'
HTTPS_PROXY = 'http://127.0.0.1:7897'
NO_PROXY    = 'localhost,127.0.0.1,::1,38.76.221.190,198.18.0.174,xn--7ov1ng90a.top,[::1]'
```

同一进程内对比：

```
带代理env   FAIL LLMError 调用模型失败：InvalidURL: Invalid port: ':1]'
清代理env   OK   桩回复-OK item-role-model
```

- 根因：`backend/app/agents/llm.py:210` 用 `httpx.AsyncClient(timeout=...)`，**没关 `trust_env`**，于是把系统代理与 `NO_PROXY` 交给 httpx 解析；`NO_PROXY` 里的 `[::1]` 这一项会让 httpx 在构造 URL 时抛出 `InvalidURL: Invalid port: ':1]'`。
- 影响：装了 Clash/v2ray 之类会自动写 `HTTP_PROXY`（很多人同时写 `[::1]` 到 `NO_PROXY`）的机器上，**所有 AI 调用直接失败**，报错信息与「AI 不回消息」毫无关联，用户根本查不出来。
- 最小修法：本地 provider（`127.0.0.1`/`localhost` 的 `base_url`）用 `httpx.AsyncClient(trust_env=False)`；或至少把异常信息补上「检测到系统代理设置」提示。
- 说明：本报告其它章节的结论**不受此影响** —— 服务进程由 WMI 创建、环境干净，桩调用全部成功；只有我从 shell 直接跑的进程会踩到。§3.1 的超时测试是**清掉代理变量后**跑的。

### 9.2 口径核对（未发现问题的两点）

- `/api/agents/prompts` 返回 `{'items': [...], 'scopes': [...], 'roles': [...]}`（`api/agents.py:86`），前端读 `data.items`（`Prompts.tsx:52`）—— **契约一致**。
- `Agents.tsx:39` 的 `/api/agents/messages` 不传 `limit`，后端默认 100（`runtime.py:76`）—— 链过长时会截断，属可接受的显示上限，非缺陷。

### 9.3 未验证清单（如实登记）

| 项 | 为什么没验证 |
|---|---|
| 真实 IM 连接下 AI 回复能否真发出 | 需要真实闲鱼账号，越界 |
| 人工/自动回复在真机上的实际送达 | 同上 |
| 前端 `Prompts.tsx` / `Providers.tsx` / `Agents.tsx` / `Master.tsx` 的真实浏览器渲染 | 未起 `pnpm dev:web`，本次只做了接口与数据形状核对 |
| 重启 DSH 宿主后在 DSH 里调 `xianyu` MCP | 需要重启宿主，本次不做（已用 stdio 直连等价验证） |
| `ai_prompts` 无 UNIQUE 约束在并发下的重复行 | 未构造并发写入，仅代码层指出（`prompts.py:115` 查-插两步） |
| 真实 LLM（DeepSeek 等）的提示词效果 | 硬规则：只用本地桩 |

---

## 10. 缺陷清单（按建议修复顺序）

| 编号 | 级别 | 一句话 | 位置 |
|---|---|---|---|
| AI-1 | **P1** | 跨商品匹配不按账号过滤，A 号会话会拉 B 号商品参与回答 | `backend/app/agents/runtime.py:208`、`runtime.py:275` |
| AI-2 | **P1** | `bargain` / `vision` 仍是死角色：建会话时写死 `agent_role` 使规则 1 永远短路 | `backend/app/services/chats.py:74,127,508-511,788-790`；`backend/app/agents/roles.py:111-118` |
| AI-3 | **P1（安全）** | API Key 明文存 SQLite | `backend/app/api/providers.py` 落库路径 |
| AI-4 | P2 | 同级里「角色精确匹配」压过 `priority`，前端文案说反，用户会以为填了没用 | `backend/app/agents/prompts.py:53`；`frontend/src/pages/Prompts.tsx:109` |
| AI-5 | P2 | 同级同 owner 的第二条提示词**静默覆盖**第一条；`ai_prompts` 无 UNIQUE、查-插非原子 | `backend/app/agents/prompts.py:115` |
| AI-7 | P2 | LLM 调用继承系统代理，`NO_PROXY` 含 `[::1]` 时报 `InvalidURL`，报错完全不可诊断 | `backend/app/agents/llm.py:210` |
| AI-6 | P3 | `scope=item` 但 `item_id` 空的行可被创建，永不生效 | `backend/app/api/agents.py` create/update |

**没有发现的问题**：三级提示词叠加与 priority、`item`/`cs`/`cross`/`master` 四角色、跨品四跳与 `ask/answer/notify`、深度上限 2、3s 超时、主 AI 工具循环封顶、MCP 21 工具与只读调用、Key 不回传前端、人工接管与 AI 互不打架 —— 这些**都是真的能用**，证据见上文各节。
