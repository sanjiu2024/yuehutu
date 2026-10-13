# 契约（先冻结，后并行）

> 这份文件是**唯一接口真相**。所有并行工作都按它写；要改先改这里，并通知所有相关方。
> 目标：闲鱼卖家 AI 工作台 —— Windows 桌面应用（PyInstaller onedir，双击 exe），多 AI 编排，人工可接管。

## 0. 形态与技术栈（已定）

| 项 | 决定 |
|---|---|
| 交付 | Windows 桌面应用，双击运行，含托盘；PyInstaller 打成 **onedir**：`dist\闲鱼AI工作台\闲鱼AI工作台.exe` + 同级 `_internal\`（2026-10-06 实测产物是目录形态，不是单文件；缺了 `_internal` 就起不来） |
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
      -- stock 语义（第九批真机）：商品**列表卡里没有库存字段**，真库存在
      --   `client.item_detail()` 的 `data.itemDO.quantity`。因此
      --   `stock = -1` 表示**未知**（前端显示 `-`），`stock >= 0` 才是平台真实库存；
      --   不许再用 `1` 冒充（曾经 `normalize_list_card` 取不到就写 1，用户看到 45 件库存全是 1）。
      --   `sync_items` 的库存**永远以本次结果为准**：本次拿不到就写 `-1`，不保留库里旧值
      --   （旧代码写死的假 1 否则会永久滞留：-1 保留旧值 → 假 1 连补详情队列都进不去）。
      --   紧接着 `_refresh_unknown_stock` 把 `stock < 0` 的行逐件补真值（0.6s/件、
      --   单次上限 60 件、遇风控/登录态过期立即 break 并回 `stock_refresh_stopped`）；
      --   本地手改过的库存被同步重置为 -1 是预期行为（平台才是真相）。
      --   `images`/`description` 不同：它们仍遵守「本次为空则不覆盖本地」。
      -- images 是 **JSON 字符串数组**，元素是图片直链、**主图在首位**；
      --   出站统一规范成 `https://`（真机给的是 `http://img.alicdn.com/...`，
      --   页面跑在 127.0.0.1 上会被浏览器的混合内容策略拦掉）。
      --   编辑保存时允许 `{images:[url,...]}`：协议层把它翻成平台的 `imageInfoDOList`
      --   （list of dict，全部字符串值：`major`/`widthSize`/`heightSize`/`type`/`url`/`extraInfo`，
      --   第一张 `major="true"`），**空数组报错**（不等于清空商品图）。
      -- 图片抽取只认「看起来是图片」的 URL（`client._looks_like_image_url`）：忽略 query/fragment 后
      --   路径必须以 `.jpg/.jpeg/.png/.webp/.gif/.bmp/.heic/.heif` 结尾，其余一律丢弃、过滤后为空就返回空。
      --   真机踩过的坑（第十批）：商品详情响应里每个 `imageInfo` 条目旁边会一起回来一条
      --   「以图搜同款」**网页**链接（`h5.m.goofish.com/wow/moyu/...?extra={"imageInfo":{...}}`，
      --   `source=itempic`、`reqFromPage=idle_detail`），深扫时被当成图片收进 images —— 45 件里
      --   42 件被污染（59 条网页 URL）。它不只影响缩略图：用户一保存就会把网页 URL 当
      --   `imageInfoDOList[].url` 推给平台，会把商品图写坏。`.heic` 必须留在白名单里：
      --   真机 62 张真图里有 3 张 `.heic`，严格白名单会把这 3 件清成 0 张图。
      -- 已卖出商品不得被「编辑」重新上架（`services/items.py` 的 `SOLD_WARNING`）：`status == 'sold'`
      --   时**不推送平台**，改动只落本地，返回 `synced=false`、warning 明说原因、
      --   `local_only_fields` 里列出被拦下的字段（批量走同一条路径，进 `warnings` 而不是 `ok`）。
      --   真机踩过的坑：对一件 `itemStatusStr="卖掉了"` 的商品做「原样回灌」编辑，平台接受且回帧
      --   `publishScene="pcMainPublish"`，随后同步里它变成 `on_sale`（被重新上架）。
      --   闸门目前只看**本地** `items.status`；本地还没同步过（而平台其实已卖出）时仍会推 ——
      --   要彻底防住需编辑前多拉一次详情，尚未实现。
      -- status 语义（第十批真机）：列表卡 `cardData.itemStatus` 是数字，真机 45 件里 44 件是 `0`、
      --   唯一 `1` 恰好是详情 `itemStatusStr="卖掉了"` 的那件 ⇒ **`0 = on_sale`、`1 = sold`**；
      --   其它数字原样 `str(value)` 返回（不猜），缺失/空串按 `on_sale`（「没报下架 ≠ 确认在售」）。
      --   旧代码把 `1` 当 `on_sale` 且用 `or` 链吞掉 falsy 的 `0`，会把每件在售商品判成下架，已修；
      --   列表与详情两条路径必须用同一套映射，否则同一商品两处状态互相打架。

-- 商品数据（曝光/浏览/想要）：接口真实存在，但**要卖家工作台会话**（第十一批调研结论）
平台接口：`mtop.alibaba.idle.seller.pc.item.stats.query` v`1.0`，`data={itemId}`，响应体在
`resp.data.data`，只有三个字段：`exposureNum`（曝光次数）、`showNum`（浏览次数）、
`wantNum`（想要人数）—— 对应本项目的 `exposure`/`visits`/`wants`（见 `xianyu/client.py`
的 `ITEM_STATS_FIELDS`）。用户参考的竞品那 7 列里，曝光人数/浏览人数/下单人数/支付人数/
支付订单数**平台没有**，也**没有任何时间窗口参数**（近 1/7/30 天取不到）。
真机证据（只读 9 次、无风控）：同一个 Cookie 打 `mtop.idle.web.user.page.head` 是
`SUCCESS::调用成功`，打上面这条是 `FAIL_SYS_SESSION_EXPIRED::Session过期`（换 spm、补
`Origin`/`Referer: https://seller.goofish.com` 都一样）；假接口对照回
`FAIL_SYS_API_NOT_FOUNDED` ⇒ 网关先查注册表再查会话，**API 名是真的**。原因是卖家工作台走
`passport.goofish.com` 的 `appEntrance=seller_web` 入口并要求商家身份
（`mtop.alibaba.idle.seller.platform.user.business.identity.get`），与本项目现在的
`www.goofish.com` 会话不是一份。
⇒ 所以「商品数据」页现在**只能显示本地口径**（会话数/消息数/订单/买家/支付，来自本地库）+
如实标「平台未提供」；要真拿到平台数据，需要另做一条卖家工作台登录入口（新功能，且会多一次
扫码 —— 与本项目「不能老登录」的诉求冲突，需用户点头后再做）。
`client.item_stats()` 拿不到数据时**抛 `ItemStatsUnavailable`（带原因）**，不再静默
`return None`（第十二批修，独立审查 D1）：早先的「吞掉」让四种完全不同的失败在接口层
长得一模一样（都是「平台返回的商品数据无法识别: 」+ 空原因），用户无法区分「账号掉线」
「只是缺卖家会话」「接口下线」「响应没指标」。
- `ItemStatsUnavailable` **故意不继承** `SessionExpiredError`：`services/common.py`
  会把 `SessionExpiredError` 判成登录态过期并 `mark_need_relogin()`，把一个好账号
  标成「需重新扫码」。同时 `common._is_session_expired()` 按 **MRO 类名**优先放行
  `ItemStatsUnavailable`，因为它的消息里会带平台原文（可能含 `Session过期` 字样），
  文案兜底会误判。
- 四种原因各自成句：缺 itemId「缺少 itemId：商品数据接口只能按商品调用」；
  会话拿不到「需要卖家工作台会话：这个接口族不接受网页版 Cookie（账号本身没掉线）」；
  其它协议错「接口不可用: <平台原文>」；响应认不出「平台响应里没有可识别的商品数据指标
  （data=...）」。
- `?source=platform` 失败时 502 的 `message` 必须带真实原因（`平台商品数据接口调用失败:
  <原因>`），冒号后不许是空的；`?source=auto` 一律降级成 `source:"local"` + 对应 warning。
- 服务层别名表认平台原名 `exposureNum`/`showNum`/`wantNum`（`item_stats.METRIC_ALIASES`），
  认不出的「像指标」字段（含 exposure/visit/want/曝光/浏览/想要 等词根）要留一条
  `WARN_PLATFORM_PARTIAL` warning，不许静默丢掉；`item_id`/`title`/`code` 这类结构性
  字段不报（每次都响的 warning 等于没有 warning）。
- **这个接口只能按商品逐个查，所以服务层硬性限流**（第十四批）：`?source=auto|platform`
  时一次最多查 `item_stats.PLATFORM_ITEM_LIMIT`（=10）件商品，超出的部分用本地统计，
  并留一条 `WARN_PLATFORM_CAPPED` warning 写明「本次只查了前 N 件（这个账号共 M 件）」。
  理由：一个 45 件商品的账号点一次「商品数据」= 45 连击 mtop，真机补库存那次就是这样
  触发平台风控的。`?item_id=<单件>` 不受限流影响（限流只为挡住「全量连击」）。
  **不许**为了让数字好看而偷偷少查又不说明。
- 信封剥**多层**（第十四批）：`_stats_payloads()` 按 `data` 键最多剥
  `client._STATS_ENVELOPE_DEPTH`（=4）层，裸指标 dict 与任意层数的信封都认。
  只剥一层的话，平台再包一层就会重现「明明有 367 次曝光却报没有可识别的指标」。

-- AI 供应商与角色
providers(id INTEGER PK, name TEXT, base_url TEXT, api_key TEXT, default_model TEXT,
          enabled INTEGER DEFAULT 1, is_default INTEGER DEFAULT 0, created_at, updated_at)
          -- api_key **落库是密文**（审计 A3）：`backend/app/core/crypto.py` 的
          -- `enc:v1:<b64>` 格式，密钥从 `security.local_secret()` 派生（不是新发明的密钥源）。
          -- 历史遗留的明文行在 `GET/POST/PUT /api/providers` 上做一次性幂等迁移。
          -- 出站仍然只回 `api_key_masked`，任何接口都不许回明文。
          -- ⚠ 删掉 `<data>/local_secret.txt` = 所有 API Key 都解不开，只能重新填一次
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
              -- peer_nickname **只允许买家昵称**；拿不到就存空串（前端兜底显示 `买家 <peer_user_id>`）。
              --   第九批真机：会话条目里根本没有买家昵称字段，`lastMessage.message.extension.reminderTitle`
              --   是**那条消息发送者**的昵称 —— 我发最后一条时它就是账号自己的昵称。
              --   曾经无条件取它当 buyer_name，用户整列看到自己的名字（「客户名称显示不对」）。
              --   判定发送者用 `lastMessage.message.sender.uid` / `extension.senderUserId` 与 my_id 比对；
              --   同步到买家来信时要用来信昵称**修正**库里已有的错误昵称。
messages(id INTEGER PK, conversation_id INTEGER, direction TEXT,  -- in|out
         sender TEXT,                  -- buyer|ai:<role>|human
         content TEXT, msg_type TEXT DEFAULT 'text', item_id TEXT,
         meta TEXT, sent_ok INTEGER, created_at)
agent_messages(id INTEGER PK, conversation_id INTEGER, from_agent TEXT, to_agent TEXT,
               kind TEXT, topic TEXT, payload TEXT, created_at)
agent_runs(id INTEGER PK, conversation_id INTEGER, role TEXT, provider_id INTEGER,
           model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
           latency_ms INTEGER, status TEXT, error TEXT, created_at)
agent_chat_messages(id INTEGER PK, thread TEXT DEFAULT 'master', account_id TEXT, role TEXT,
                    content TEXT, tool_calls TEXT, provider_id INTEGER, model TEXT,
                    latency_ms INTEGER, tokens INTEGER, status TEXT DEFAULT 'ok',
                    error TEXT, created_at)
                    -- 「主管 AI」的对话记录（第七批）。以前只活在浏览器内存里，刷新即丢。
                    -- 失败也落库（status='error' + error 原文）：用户要能看见「为什么失败」。
item_stats_cache(account_id TEXT, item_id TEXT, exposure REAL, visits REAL, visitors REAL,
                 wants REAL, orders REAL, buyers REAL, paid_orders REAL,
                 source TEXT DEFAULT 'platform', error TEXT DEFAULT '', fetched_at TIMESTAMP,
                 PRIMARY KEY(account_id, item_id))
                 -- 商品数据接口只能按商品逐个查、一次多了必触发风控 → 后台慢慢拉，页面读这里。
                 -- 拿不到的指标存 NULL（不许存 0）；失败行 source='error' + error 原文。

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
POST   /api/auth/desktop          {+ 头 X-Desktop-Secret} -> {token,user}   -- 桌面外壳自动登录，免手输密码
POST   /api/auth/logout
GET    /api/auth/me
GET    /api/health                -> {ok:true,version,frozen,...}
GET    /api/system/info

GET    /api/accounts              -> [{id,nickname,enabled,auto_reply,login_state,runtime_state,remark}]
       -- 三个**可选**筛选参数（2026-10-11 加，不传时行为与以前逐字一致）：
       --   `enabled=true|false`、`q=`（昵称/备注/账号 id 模糊，大小写不敏感）、`limit=`（0=不限）。
       --   加它们是因为 MCP 侧的 `accounts_list` 一直宣称支持这三个参数
       --   （`mcp_server/client.py::CANONICAL_SPECS` + `TOOL_ROUTES`），而端点过去根本不收 ——
       --   FastAPI 对未知 query **静默忽略**，模型以为筛过了、其实拿到全量。同批还修了
       --   `finance_income`/`finance_expense` 的同类漂移（工具参数 `cycle_from`/`cycle_to`
       --   在端点上叫 `from`/`to`，现由 `TOOL_ROUTES[..]['query_rename']` 翻译）。
       --   守卫测试：`backend/tests/test_mcp_server.py::RouteParamAlignmentTest`
       --   （把每条 MCP 路由声明的 query/body 键跟 `app.openapi()` 的真实签名对账）。
POST   /api/accounts/qr/start     -> {ticket,qr_image_b64,expires_in}
GET    /api/accounts/qr/{ticket}  -> {state:pending|scanned|confirmed|expired|failed,account_id?}
PATCH  /api/accounts/{id}         {enabled?,auto_reply?,remark?}   -- enabled 就是 IM 入站的生命周期
DELETE /api/accounts/{id}         -> {deleted:true,purged:true,counts:{conversations,messages},
                                      data_purged:{purged:true,counts:{items,item_stats_cache,
                                      operation_logs,orders,ai_prompts,shipments}}}
      -- 删号**连带清掉该账号的 conversations 与 messages**（审计 C4，以前删完还能看到该账号的会话）；
      --   先摘 IM 会话再删行。清理失败不假装成功：回 `purged:false` + `error`。
      --   `counts` 由 `services/chats.py::purge_account_data(account_id)` 给出（幂等，第二次 0/0）。
      -- **删号 = 删掉这台机器上属于它的所有本地行**（第十六批）：`items` / `item_stats_cache` /
      --   `operation_logs` / `orders` / `shipments` / 账号级 `ai_prompts` 由
      --   `services/purge.py::purge_account_data()` 清（`shipments` 挂在 `orders.order_id` 上而不是
      --   `account_id`，只能按订单号删）。唯一例外是 `agent_chat_messages`（主管 AI 的对话，
      --   `thread='master'`，不属于任何账号）。真机现象：删掉账号 `3998190171` 之后商品页仍列出
      --   它的 39 件商品、`operation_logs` 还剩 15 行。
      --   确实删掉商品行时广播 `items.updated`（`action:'account_deleted'`），开着的商品页静默重拉。
      -- 启动自愈：`main.lifespan` 在风控回灌之后调 `purge.purge_orphans()`，清掉老版本留下的孤儿行
      --   （判断依据只有账号表：`account_id <> '' AND account_id NOT IN (SELECT id FROM accounts)`，
      --   不猜时间/前缀，所以不会误伤「刚导入还没同步完」的正常账号）。
POST   /api/accounts/{id}/relogin -> 触发重新扫码/续期（成功后重新挂 IM）
POST   /api/accounts/{id}/refresh-profile -> {account_id,nickname,avatar_url}
      -- 用 `client.user_profile()`（`mtop.idle.web.user.page.head`）刷新账号资料并**窄写**落库。
      --   第九批真机：user_profile 过去只在扫码成功那一瞬被调用，所以 Cookie 导入/续期恢复的账号
      --   `accounts.nickname` 恒为空 → 用户看到 `(未命名)`。落库**不能**用 `store.upsert_account`
      --   （INSERT OR REPLACE 会抹掉 cookie/enabled），必须窄写。协议层不可用回 `xianyu_not_ready`。
POST   /api/accounts/{id}/captcha -> 打开人工验证浏览器窗口（滑块）
GET    /api/accounts/{id}/status
POST   /api/accounts/qr/poll      -- 兼容别名（旧名，等价于 GET /api/accounts/qr/{ticket}）
POST   /api/accounts/{id}/toggle  -- 兼容别名（旧名，等价于 PATCH 的 auto_reply）

# 登录态过期的统一口径（审计 B2：过去没有任何回写路径，前端永远显示「已登录」）
`backend/app/xianyu/client.py` 抛 `SessionExpiredError` 时，`services/common.py::call_method()`
识别它 → `store.mark_need_relogin()` 回写 + `events.publish('accounts.changed',
{reason:'session_expired',login_state:'need_relogin'})`，响应给 `code='verification_required'`
且**保留平台原文**。前端据此弹「重新扫码」，而不是当成偶发网络错误让人一直重试。
`xianyu_not_ready` 与它不同：那是「协议层此刻不可用」，用户不必重登。

GET    /api/items?account_id=&status=&keyword=&page=&size=&sort=&order=
GET    /api/items/{account_id}                 -> 该账号的商品列表（带 status/keyword/page/size/sort/order）
      -- `status` 三个分区（第十三批）：`onsale` = 平台在售**且库存不是 0**（库存未知算在售）；
      --   `offline` = 非在售状态**或库存 = 0** —— 售罄与已卖出都进这个分区（用户要求
      --   「售罄的商品应该在下架分区里，那样好编辑」）；`sold` = 平台已卖出。
      --   其它值仍按 `status = ?` 精确匹配（旧行为不变）。
      --   历史 bug：前端发 `onsale`/`offline`、库里存 `on_sale`/`sold`，这两个筛选永远 `total=0`。
      -- `sort` ∈ `updated|price|stock|title|status|item_id`（白名单在 `services/items.py::SORT_COLUMNS`，
      --   前端只发字段名、不拼 SQL；非法值回落到 `updated`），`order` ∈ `asc|desc`。
      --   库存排序时**未知（NULL / <0）永远沉底**（升序降序都一样），否则点「库存」先看到一堆 `-`。
      -- 响应 `data` 里额外带 `counts:{all,onsale,offline,sold}`：**不受当前 status 筛选影响**
      --   （`_status_counts` 用 `SUM(CASE WHEN)`；它的参数顺序必须是 `onsale+offline+sold+list(params)`，
      --   因为 CASE WHEN 在 SELECT 里排在 WHERE 之前 —— 写反会永远回全 0，已有回归锁）。
      --   前端据此把下拉框写成「在售（N）/ 已下架 · 含售罄 / 已卖出（N）」。列表中
      --   「在售 + 库存 0」的行标 `售罄` 徽章（它落在已下架分区里，再标「在售」会像筛选坏了）。
GET    /api/items/{account_id}/{item_id}       -> 单条商品（MCP 的 items_get 直连这条，别再全表拉）
      -- 单条商品**必须**带账号上下文。曾经存在过一条扁平的 `GET /api/items/{item_id}`，
      --   它与账号列表路径形状完全相同、被 FastAPI 按声明顺序吃掉，已删除：同一个
      --   单段路径不可能既是「账号列表」又是「商品详情」，只能保留一个。
DELETE /api/items/{account_id}/{item_id}       -> {deleted:true,account_id,item_id}
      -- **只删本地记录**：闲鱼上架着的商品不受影响，下次「同步商品」若平台仍返回它就会被
      --   写回（前端文案必须这么告诉用户）。会话/消息/订单**都不删** —— 它们是聊天与交易
      --   历史。严格配对 `(account_id,item_id)`，跨账号或不存在一律 404 `not_found`；
      --   成功写 operation_logs action='delete_item' 并发 `items.updated`（payload.action='delete'）。
      --   平台侧**有**删除接口（`...pc.item.batch.delete`，见下面的 `POST …/batch-delete`），
      --   这一条故意保持「只动本地」的语义；UI 上是两个菜单项：行内「删除」走 batch-delete
      --   （真删）、「只从本地列表移除」走这一条。
GET    /api/items/{account_id}/stats?item_id=&source=cache|auto|local|platform
      -> {success:true,data:{items:[{item_id,title,images,price,status,
             exposure,visits,visitors,wants,orders,buyers,paid_orders,
             conversations,messages,last_message_at}],
             total,account_id,updated_at,source,warnings,metrics}}
      -- 「商品数据」页的接口。**拿不到的数一律 `null`，前端显示 `-`；禁止 0 或假数据**
      --   （0 只有在平台真的回 0 时才出现，`item_stats.py::_count_or_none()` 就是这条的守卫）。
      -- `source`：`cache`（前端默认）=只读本机 `item_stats_cache`，**一个平台请求都不发**；
      --   `local`=纯本地统计（不碰协议层、不校验 Cookie）；`auto`=能拿平台就拿、失败降级为
      --   local 并在 `warnings` 里说清原因；`platform`=必须拿到，否则 502。
      --   为什么要 `cache`（用户原话「我觉得商品数据应该让他自己慢慢拉取，而不是说我点过来
      --   的时候他再拉取」）：这个接口族**只能按商品逐个查**、一次多了必触发风控，所以改成
      --   **后台慢慢拉、页面只读缓存**（`services/stats_worker.py` + `main.py::_stats_pull_loop`，
      --   默认每 `XYWB_STATS_INTERVAL`=300s 拉 `XYWB_STATS_LIMIT`=5 件、间隔 `XYWB_STATS_PAUSE`=3s，
      --   启动后先等 `XYWB_STATS_FIRST_DELAY`=60s）。`platform` 仍然保留给「我现在就要看」。
GET    /api/items/{account_id}/stats/progress
      -> {success:true,data:{account_id,total,cached,failed,last_at}}
      -- 后台补到哪了。**绝不碰平台**（只数在售件数与缓存行数），所以页面可以随标签页放心刷新。
      --   `total` 只数在售商品、`cached` 只数 `source <> 'error'` 的行、`failed` 是拉取失败的行数
      --   （失败行下一轮自动重试）；`last_at` 没有就是 `null`。
      -- 缓存行的口径：拿不到的指标存 **NULL**（不许存 0）；失败行存 `source='error'` + `error`
      --   原文，这样「为什么这一件没有数」在库里查得到。写缓存 `INSERT OR REPLACE` 且刷新
      --   `fetched_at`，失败行**不会**盖掉上一轮的成功数据（见 `test_stats_worker.py`）。
      -- `warnings` 是给用户看的诚实说明（例如「平台未提供商品数据接口，仅显示本地统计」）。
      -- `metrics.platform` 目前是 `exposure/visits/visitors/wants`，其中平台真名与来源见 §2。
      -- **信封必须剥**（第十三批，用户截图暴露的真 bug）：平台真机回的是
      --   `{'code':'success','data':{'exposureNum':367,'showNum':24,'wantNum':1},'msg':'成功'}`，
      --   协议层 `parse_item_stats` 以前只看顶层，认不出指标就判「没有可识别的商品数据指标」并降级本地。
      --   现在 `_stats_payloads()` 返回 `[data, data['data']]` 两个候选、逐个尝试，裸指标 dict 与
      --   两层信封都认；真认不出时错误里带上信封的 `code`/`msg`，不再只说「无法识别: 」。
      -- 路由**必须挂在 `/api/items/{account_id}/{item_id}` 之前**（`main.py` 的
      --   `ROUTE_MODULES` 里 `item_stats` 排在 `items` 前），否则被动态段吞成「商品不存在」。
      -- 后台一轮的失败处理（第十五批真机修）：`pull_once` 返回里每账号有 `stopped`
      --   ∈ `null | 'risk' | 'session' | 'unavailable'`。**`risk`/`session`/`unavailable`
      --   都立刻 `break` 这个账号**（系统性问题换件重试不会有不同结果，只会白打 mtop：
      --   真机曾每 300s 打 10 次注定失败的请求，把 `risk_state.consecutive_hits` 从 1 推到 2）。
      --   失败件仍然落一行 `source='error'`，否则下一轮第一个挑中的还是它。
      -- 出网不吃本机代理（第十五批真机修）：`xianyu/net.py::async_client()` 是
      --   `proxy=None` **加** `trust_env=False`。只写 `proxy=None` 不够 —— httpx 在
      --   `trust_env=True`（默认）时仍会读 `HTTP(S)_PROXY`；真机上本机代理对
      --   `h5api.m.goofish.com` 握手即断（`ConnectError: EOF occurred in violation of protocol`），
      --   直连是通的。连接类失败时 `client._net_problem()` 会把本机代理变量名列出来。
POST   /api/items/{account_id}/sync            -> 从闲鱼拉取在售商品入库
POST   /api/items/{account_id}/publish         {title,price,description,images,category_id,...}
POST   /api/items/{account_id}/{item_id}/edit  {price?,original_price?,title?,description?,stock?,images?,ai_enabled?}
      -- `images` 是 URL 数组（主图首位），协议层翻成平台 `imageInfoDOList` 提交（见 §2 items 注释）
      -- `original_price`（划线价）在 `PUSHABLE_EDIT_FIELDS` 里，会推到平台。
      -- `ai_enabled` **平台没有这个字段**（服务层按 `local_only` 处理），但它真会落库 ——
      --   前端编辑弹窗以前这个开关从来没进过 patch，拨一下、保存、回来还是原样（「改了不成功」）。
      --   现在只有真的拨动过才带上，免得白报一条「仅本地生效」。
      -- 已卖出（`status='sold'`）的商品：平台不接受编辑，服务层 `SOLD_WARNING` 闸门让
      --   `synced=false` + `warning`，**本地照改**。前端编辑弹窗对这类商品要提前讲清
      --   「本地会变、闲鱼上的不会变，这不是保存失败」，并把按钮写成「保存到本地」。
      -- **只提交真的改动过的字段**（前端硬性要求）：请求里出现的每个字段都会进
      --   `updated_fields`/`local_only_fields`，所以「原样回提整表单」会让用户看到一堆
      --   自己没动过的字段名（真机反馈：「仅本地生效：description, price, stock, title」），
      --   而且 `original_price` 会被静默写成 0。空输入框 = 这次不改。
      -- 服务层自身是「patch 合并到库里那条商品再回提整份原表单」（`_apply_patch`），
      --   所以省略没变的字段完全安全，不需要前端凑齐整份表单。
POST   /api/uploads/images                     multipart/form-data: file(单张) + account_id(可选)
      -> {success:true,data:{url,file_id,width,height,name,local_path}}
      -- 把本地图片传到闲鱼图床，拿到可直接放进商品/消息的直链。限制：jpg/jpeg/png/webp，≤10MB。
      --   本地留档原图原字节在 `config.DATA_DIR/'uploads'`（名字 `uuid4().hex + 扩展名`），
      --   `local_path` 是它的绝对路径；只把**送协议层的那一份**转 RGB JPEG 临时文件、用完即删。
      --   失败信封：400 invalid_fields/bad_request（扩展名/内容/空文件/超 10MB/转码失败）、
      --   404 not_found（account_id 不存在）、409 verification_required（Cookie 失效，带
      --   `login_state:"need_relogin"`）、502 protocol_error、503 xianyu_not_ready（无可用账号）。
      --   无论成败都写 operation_logs action='upload_image'；失败一定删本地文件（不留孤儿）。
      --   选号：`account_id` 缺省 → 第一个 `enabled=1` 且有 Cookie 的账号；一个都没有时
      --   **退一步用任意有 Cookie 的账号**（`accounts.enabled` 默认 0，刚扫码还没点启用的账号
      --   不该连图都传不上；已有回归锁 test_disabled_account_with_cookie_is_still_usable）。
GET    /api/uploads/local/{name}               -> 本地留档原图（给前端缩略图/预览用）
      -- 鉴权可走 `Authorization: Bearer` **或 `?token=<jwt>`**（`<img src>` 带不了请求头）。
      -- `name` 白名单 `^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$` 且 `resolve()` 后必须仍在目录内
      --   （防路径穿越）；缺失 404 `{success:false,code:'not_found'}`。
POST   /api/items/{account_id}/{item_id}/delist | /relist
POST   /api/items/{account_id}/batch-edit      {item_ids:[...],patch:{price_delta|price_set|title_tpl|...}}
      -- patch 里的库存有三种写法，语义**不能混**：
      --   `stock`       绝对目标值（前端已经算好）；`stock_set` 设为固定值（等价，允许在库存未知时用）；
      --   `stock_delta` 相对增减（正=增、负=减）—— **库存未知（`stock < 0`）时拒绝**，回
      --   `code='unknown_stock'` + 原文「该商品库存未知（平台列表没有返回库存），不能用
      --   「增加/减少」；请改用「设为固定值」，或先同步商品把库存取回来」。理由：未知当 0
      --   会把「加 5 件」静默写成 5 件，那是改坏用户的真实库存。批量里被拒的商品进 `failed`，
      --   其余照常处理（恒等式 `total == ok + local_only + noop + len(failed)`）。
      --   负值一律钳到 0（`max(0, ...)`），不写负数库存 —— 这条只管**补丁字段**
      --   `stock_set`/`stock_delta`/`price_set`/`price_delta`。直接写字段（`{"stock": -5}`
      --   回「库存不能为负」、`{"price": -1}` 回「价格不能为负」）是另一条路：
      --   400 `invalid_fields`，**不钳**（字段是绝对值，用户明确给了个负数就是输错了，
      --   静默改成 0 会改坏真实库存）。
      --   补丁数字字段的入参校验（第十二批修，独立审查 D4）：`null`/字符串非数字/列表/
      --   布尔/NaN/Inf 一律 400 `invalid_fields` + 「字段 <key> 需要数字，收到: <值>」；
      --   `stock_set`/`stock_delta` 还要求整数（`2.5` 静默截成 `2` 也是编数字）。
      --   这条**不是**洁癖：前端「增减」输入框为空时 `Math.max(0, NaN)` 得 `NaN`，
      --   `JSON.stringify(NaN)` 就是 `null` —— 真会发出来。旧行为是 `int(float(None))`
      --   抛 TypeError → 单条 500、批量整单 500（连哪条出错都不说）；现在批量里逐条进
      --   `failed`（`code='invalid_fields'`），其余照常处理。
POST   /api/items/{account_id}/batch-publish   {items:[...]}
GET    /api/items/{account_id}/operation-logs?limit=
POST   /api/items/publish ; POST /api/items/batch ; POST /api/items/downshelf ; POST /api/items/reshelf
POST   /api/items/sync ; GET /api/items/stats ; GET /api/items/operation-logs
      -- 上面这一组是**不带账号上下文**的扁平入口，保留给 UI 的「全部商品」视图

POST   /api/items/{account_id}/batch-offline          {item_ids, confirm}
POST   /api/items/{account_id}/batch-delete           {item_ids, confirm}
POST   /api/items/{account_id}/{item_id}/quick-edit   {price?, quantity?, confirm}
      -- 第二十二批新增。走**官方商品管理页**的接口，不是旧的「整份原表单回提 / 逐件循环 /
      -- 只删本地」：
      --   `batch-offline` -> `mtop.alibaba.idle.seller.pc.item.batch.offline`
      --   `batch-delete`  -> `mtop.alibaba.idle.seller.pc.item.batch.delete`
      --   `quick-edit`    -> `mtop.alibaba.idle.seller.pc.item.info.update`
      -- 证据来自官方微应用 bundle `idle-pc/seller-item/0.0.20`（单行压缩 JS 的**字符偏移**，
      -- 行号无意义）；每条接口的证据串在 `backend/app/xianyu/item_write.py::WRITE_APIS[*].evidence`。
      --   `itemIds` 官方发的是**逗号串**（调用点原文 `H0({itemIds: T.join(",")})`）不是数组；
      --   `quick_update` 无 SKU 时是扁平 `{itemId, quantity, price}`，**没给的字段不放键**
      --   （官方发的是 `undefined` 不是空串）。
      -- 成功判据逐字照抄 bundle：`res.data.data` 为真（空 dict / 空 list / None 全算失败）。
      --   批量响应里的逐条清单是 `res.data.data.itemProcessResultList`（元素含 `success`），
      --   另有 `res.data.failCount`。**`ok` 只数平台明确回报成功的 id** —— 没回报的 id 一律算
      --   失败（`code='no_per_item_report'`）；平台接受整批但没给清单时 `ok=total` 且
      --   `per_item_report=false`（不假装逐条确认过）。拿不到 `failCount` 时回 `null`，不补 0。
      -- 三步顺序**必须**是「确认 → 字段校验 → 建客户端」：未确认时一个平台请求都不发；
      --   字段错时不建客户端（掉线账号 + 空 `item_ids` 也要先说「item_ids 不能为空」，
      --   而不是「去重新登录」）。同口径见 `services/item_templates.py`，回归锁
      --   `test_item_write_api.py::test_field_validation_runs_before_credential_check`。
      -- `quick-edit` **不碰 `status`** —— 这正是旧的整份回提的缺陷（编辑已卖出商品会把它重新
      --   上架）。它只改 `price`/`quantity`：价格必须 >0、库存必须 ≥0 整数，两者至少给一个。
      -- `batch-delete` 平台成功后才删本地 `items` 行；本地没有该行时进 `warnings`
      --   （`code='local_missing'`，不是 failed —— 平台侧确实删掉了）。
      -- 返回：`{total, ok, failed:[{id,code,message}], warnings:[{id,code,message}], status}`
      --   （`status` = `off_shelf` / `deleted`）；`quick-edit` 成功
      --   `{item_id, price, quantity, synced, warning}`（`price`/`quantity` 优先用平台回读值
      --   `res.data.data`，拿不到才用请求值）。
      -- 错误码：未确认 400 `confirmation_required`；字段错 400 `invalid_fields`；
      --   掉线 409 `verification_required`（并回写 `login_state='need_relogin'`）；
      --   平台业务失败 / 无权限 502 `protocol_error`（message 带平台 `msg` 原文，
      --   **无权限不改登录态** —— `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 不是掉线）。
      -- **`item_ids` 数组与逗号串两种入参都收**（UI 给数组、MCP 给逗号串），统一由
      --   `xianyu/item_write.py::normalize_item_ids` 归一；上限 200 条（`BATCH_WRITE_LIMIT`）。
      -- 协议层实现口径：`backend/app/xianyu/item_write.py` 是**纯函数**（构造 `(api, data)`
      --   二元组 + 独立的响应解析函数），**不自己调客户端**；真正发请求统一走
      --   `common.call_method(client, ('mtop',), api, data, v='1.0')`（与 `xianyu/trade.py`
      --   同族）。这样纯函数能在无客户端、无网络下逐字测，也不重复实现 `common.call_method`。
      --   解析函数收的是**客户端返回值** `{'success','data','message','ret'}`（业务信封
      --   `{code,msg,data}` 在 `raw['data']`），与 `xianyu/trade.py::envelope_error` 同约定。
      -- 4 条「bundle 里声明了但调用点 0」的接口登记在 `NOT_IMPLEMENTED` 里**故意不实现**：
      --   `...pc.item.delete`（单个删除）、`...platform.item.operate.online`（上架）、
      --   `...pc.item.price.update`、`...pc.item.quantity.update` —— 官方商品管理页自己都不用它们。

GET    /api/chats/conversations?account_id=&mode=&limit=   （= GET /api/chats，两者等价）
GET    /api/chats/{conversation_id}/messages?limit=&before_id=
      -- 每条消息除正文外带媒体与 AI 溯源字段：`images[]`（图片直链，保序）、`msg_type`
      --   （`text`/`image`/`voice`/`card`/`system`）、`audio_url`、`audio_duration`，
      --   以及 `ai_role`/`ai_model`/`ai_elapsed_ms`（这条回复是哪个角色、哪个模型、花了多久）。
      -- **语音时长只有一个换算实现**：`xianyu/im.py::audio_seconds()`（>300 当毫秒、≤0/非数字
      --   给 None），`services/chats.py::audio_seconds()` 委托它。两边各写一份会差 1000 倍
      --   （值落 301~600 时）。低于 1 秒或高于 600 秒只显示 `[语音]`，不编时长。
POST   /api/chats/{conversation_id}/send       {content}        -- 人工回复
POST   /api/chats/{conversation_id}/mode       {mode}
PUT    /api/chats/{conversation_id}/ai         {enabled}        -- 人工接管开关
POST   /api/chats/{conversation_id}/read
POST   /api/chats/{conversation_id}/ai-draft   -> {draft}       -- 生成建议但不发送
POST   /api/chats/classify                     {content} -> {intent,replies[]}  -- 规则话术（不花 LLM）
POST   /api/chats/sync                         {account_id,limit?,message_limit?}
      -> {account_id,scanned,conversations_new,messages,messages_skipped,skipped}
      -- 把**平台上已经存在**的历史会话与最近消息拉回本地。IM 入站只处理「正在发生」
      --   的推送帧，进程启动前就有的会话不会自己进库；IM 挂载成功时会自动回填一次
      --   （`im_service.start()`），这个接口给用户手动补一次。**幂等**：会话按
      --   (account_id, buyer_id) upsert，消息按平台 msg id（`messages.meta.external_id`）
      --   去重，重复调用只增加 `messages_skipped`。
      -- 失败是统一信封：没 Cookie -> `xianyu_not_ready`，登录态过期 ->
      --   `verification_required`（同时写 login_state='need_relogin'），平台报错 ->
      --   `protocol_error`。单个会话拉消息失败只进 `warnings`，不整体失败。
      -- 路由**必须声明在 `/api/chats/{conversation_id}` 之前**，否则被动态段吞成 422。
POST   /api/chats/incoming                     {account_id,content,buyer_id,item_id?,conversation_id?,msg_id?,...}
      -> {message,ai_reply,ai_result,ai_send,mode,duplicate}
      -- 买家来信的统一入口；给了 msg_id 即幂等。
      -- 媒体帧（第七批）：`content` 只在**没有任何媒体**时必填；图片/语音走 meta 白名单
      --   `image_url`/`image`/`audio_url`/`audio_duration`（`api/chats.py::_incoming_meta`）。
      --   纯图片帧过去会被入站闸门（要求 `content` 非空）整条丢掉，现在照常落库，
      --   正文落占位符 `[图片]`/`[语音]`（`services/chats.py::_media_placeholder`）。
      -- ai_result.code 说明「为什么这条来信没有产生 AI 回复」。**case 很重要**：它是
      --   入站路径上唯一能看见闸门原因的地方（`ai_send` 只在 AI 真跑过时才非空）。
      --   保证给出的只有下面三个**闸门** code：
      --   'mode_not_auto'          该会话处于人工接管/关闭，AI 一律不生成
      --   'account_auto_reply_off' 账号级 enabled=0 或 auto_reply=0，AI 一律不生成
      --   'system_message'         这条是系统/卡片帧（`msg_type ∈ {system,card}`），不是买家说的话
      --   第三个是第七批加的：协议层放开未知 contentType 后，`[卡片消息]`/`[系统消息]`
      --   会从「跳过」变成「触发 AI」—— 对商品卡开口等于自言自语，还白烧一次大模型。
      --   **只拦自动回复，不拦落库**（落库正是「不再显示 (空消息)」的前提）。
      --   其余失败（没有可用 provider、LLM 报错、返回格式无法识别）一律
      --   `success=False` + `message`（人类可读原文）；只有抛异常那条路径额外带
      --   `code='ai_error'`，正常失败**没有** code，前端不能假定一定有。
      --   账号行不存在时**放行**（IM 帧派发不因缺行而静默丢弃），此时不会给出上述 code。
      -- ai_send 只在 mode=auto 且真的尝试发送时出现：
      --   {sent:bool, code?:'missing_cid'|'risk_cooldown'|'risk_blocked'|'protocol_error',
      --    message?:str, cid?:str}；code 出现即表示「AI 生成了但没有发出去」，
      --   且一定同时写 operation_logs(action='ai_reply_send'|'ai_reply_blocked')。
      -- mode != 'auto'（人工接管）时一律不发送，ai_send 不出现。
WS     /api/ws?token=...                                        -- 新消息/状态推送
       -- 服务端推送的 topic 全量清单（前端按 topic 订阅后静默重拉，不刷新页面）：
       --   chats.message        {conversation_id,message_id,direction,sender,content}
       --   chats.mode_changed   {conversation_id,mode}
       --   chats.ai_reply       {conversation_id,message_id,role,text}
       --   accounts.changed     {account_id, patch?|action?}
       --   items.updated        {account_id, item_ids[], action:'publish'|'edit'|'status'|'sync'|'batch_edit'}
       --   providers.changed    {provider_id?, is_default?, action:'create'|'update'|'delete'|'set_default'|'roles'}
       --                        **不许**带 api_key 明文
       --   prompts.changed      {action:'create'|'update'|'delete', prompt_id, scope?}
       --   risk.changed         {account_id, reason?}
       --   risk.cleared         {account_id, source?}
       --   agent.message        {from_role,to_role,kind:'ask'|'answer'|'notify',...}
       --   agent.reply_failed   {conversation_id,role,error}
       --   agent.cross_failed   {conversation_id,error}
       --   agent.master.tools   {tool,ok,...}
       -- 前端页面订阅关系是契约的一部分：概览/账号/风控订阅 accounts.changed 与
       -- risk.*；商品页订阅 items.updated；AI 配置订阅 providers.changed；提示词页
       -- 订阅 prompts.changed；AI 沟通订阅 agent.*；会话页订阅 chats.*。
       -- 发布事件绝不能影响已写库的业务变更（各路由的 _publish 整体吞异常）。
       -- 回归测试：backend/tests/test_ws_events.py 用真 websocket_connect 断言
       -- accounts.changed 与 prompts.changed 真的投递到客户端。

GET    /api/agents/roles ; PUT /api/agents/roles/{role}
GET    /api/agents/roles/{role}/prompt-preview -> 该角色实际生效的系统提示词（三级合并后的结果）
GET    /api/agents/prompts ; POST /api/agents/prompts ; PUT/DELETE /api/agents/prompts/{id}
POST   /api/agents/master/chat                 {message,conversation_id?} -> {reply,tool_calls[]}
POST   /api/agents/reply                       -- 对某条消息立即触发一次 AI 回复（UI 的「让 AI 回」）
GET    /api/agents/runs?limit=&offset=&role= ; GET /api/agents/messages?conversation_id=&limit=&offset=
GET    /api/agents/chat/messages?thread=master&limit=50&before_id=
      -> {success:true,data:{items:[{id,thread,account_id,role,content,tool_calls,provider_id,
             model,latency_ms,tokens,status,error,created_at}],total,has_more}}
      -- 主管 AI 的**对话记录**（表 `agent_chat_messages`，`backend/app/agents/history.py`）。
      --   按 id 升序返回最新一页；`before_id` 往上翻更早，`total` 是该 thread 总数。
      --   用户问「ai 的会话记录呢」问的就是它：以前这段对话只活在浏览器内存里，
      --   页面自己都写着「对话只保存在本页，刷新即丢失」。
      --   **失败也要落库**（`status='error'` + `error` 原文），否则用户看不到
      --   「它其实已经改了什么 / 为什么失败」。`tool_calls` 恒为 JSON **数组**
      --   （合法但非数组的 JSON 会被告警并按 `[]` 存，避免写读不对称）。
DELETE /api/agents/chat/messages?thread=master   -> {thread,deleted}
      -- 清空该 thread（真删本机记录）。删除行数走 `db.tx()` 读游标 `rowcount`，
      --   **不许**用 `db.execute()` 的返回值（它是 `lastrowid or rowcount`，DELETE 不重置
      --   lastrowid，会拿到上一次 INSERT 的 id 这种假数字）。

GET    /api/providers ; POST /api/providers ; PUT/DELETE /api/providers/{id}
POST   /api/providers/{id}/test                -> {ok,latency_ms,model,error?}
POST   /api/providers/{id}/default ; PUT /api/providers/{id}/default

GET    /api/orders?account_id=&status= ; POST /api/orders/sync
GET    /api/orders/{order_id} ; PUT /api/orders/{order_id}/shipment ; POST /api/orders/{order_id}/deliver
GET    /api/orders/{order_id}/logistics ; GET /api/orders/stats

GET    /api/risk/status ; GET /api/risk/events ; GET|PUT /api/risk/settings
POST   /api/risk/clear ; POST /api/risk/{account_id}/clear
      -- 清冷却**只有服务端人工验证成功（`risk_service.mark_verified`）之后才放行**：
         请求体里的 `verified` 会被忽略，客户端自证无效
GET    /api/settings ; PUT /api/settings       {key:value,...}
```

> 这份清单必须与 `scripts/list_routes.py` 的实测输出对得上（2026-10-08 实测 **82 条路径**）。
> `backend/tests/test_contract_routes.py` 是**契约闸门**：这里写的路径必须在后端真实注册且方法一致；
> 前端源码里出现的每个 `/api/...` 调用也必须在后端找得到。加路由**必须**同步这里和那个测试。

**批量操作的结果口径（所有 `batch-*` 接口一致）**：批量**永远返回 HTTP 200 + 逐条结果**，
不用状态码表达「部分失败」，`ok` 的口径是「本地已改**且**已推送到闲鱼」：

```json
{"total": 3, "ok": 1, "synced": 1, "local_only": 1, "updated": 3,
 "warnings": [{"id": "b2", "code": "not_pushed", "message": "协议层尚未就绪"}],
 "failed":   [{"id": "b3", "code": "account_mismatch", "message": "商品不属于该账号"}],
 "skipped":  [], "fields": {"price_set": 9.9}}
```

- `ok`/`synced`：本地改了、平台也推成功了。
- `warnings`/`local_only`：**本地改了但没推出去**（`code=not_pushed`）—— UI 必须显示成「仅本地生效」，绝不能报成功。
- `failed`/`skipped`：完全没改（含跨账号 `code=account_mismatch`、商品不存在）。
- `noop`（新）：本次请求**没有任何实际改动**（目前只有 `{"images": []}` 这一种，语义是「本次不改图」而不是清空商品图）：
  既不写库也不推平台，单条返回 `{item, synced: true, updated_fields: [], noop: true, warning: null}`；
  批量汇总里 `noop` 单独计数、**不计入 `ok`**，恒等式 `total == ok + local_only + noop + len(failed)`。
- 服务层结果**故意不带 `success` 字段**：带了 `success:false` 会被 `services/common.py::respond` 判成失败业务码（503），
  与「200 + 逐条结果」的契约冲突。

**单条编辑（`POST /api/items/{account_id}/{item_id}/edit`）的诚实同步口径**：`synced = warning is None and not local_only`。

- `local_only_fields: string[]`：本次请求里解析到、但**不在 `PUSHABLE_EDIT_FIELDS`**（`price`/`original_price`/`stock`/`title`/`description`/`status`/`free_shipping`/`images`）里的字段（如 `ai_enabled`/`category_id`）。
  它们只落本地 SQLite、从不推闲鱼；只要非空，`synced=false` 且 `warning` 必须点名字段（文案「以下字段仅本地生效，未推送到闲鱼：…」）。
  曾经 `images` 不在集合里：用户同时改标题+图片时 `payload={title}` 推送成功 → 报「已修改并同步 1 个」，而图片只在本地 —— 典型的假绿，已修。
- `items.images` 列恒为 URL 字符串数组（保序去重、https）且**只含图片直链**（非图片 URL 在抽取时就被丢掉）；请求允许传 `{url,width,height}` 对象（前端刚上传的图带真实尺寸），**写库只留 URL**，推协议层用完整对象（`images_to_image_infos` 把 `width/height` 变成 `widthSize/heightSize`，缺尺寸填 `'0'` 占位）。
- **已卖出商品（`status == 'sold'`）不推送编辑**：改动只落本地，`synced=false`、
  `warning='该商品已卖出，平台不接受编辑，改动仅本地生效'`、被拦下的字段进 `local_only_fields`；
  批量同样进 `warnings`（`code='not_pushed'`）而不是 `ok`。理由见 §2 items 注释（真机对已卖出商品
  提交编辑会让平台把它重新上架）。
- **平台错误码的「人话解释」集中在一个模块**：`services/platform_errors.py`（第七批）。编辑路径
  （`items.py::_platform_error_hint`）与**发布路径**（`publish_item` 里 `platform_errors.decorate(problem, value)`）
  都走它。三条口径：①**只收录真机/日志里真实出现过的码**，没见过的原样透传、`explain()` 返回空串
  （讲错原因比不讲更糟）；②按**词边界**匹配，不是子串包含（`..._CAN_NOT_PUBLISH_V2` 不许命中
  `..._CAN_NOT_PUBLISH`）；③只做解释，**不改业务判定** —— `decorate()` 绝不改 `code`（`code` 决定
  HTTP 状态码，见 `common.FAIL_STATUS`），只在 `message` 后追加一句中文、补
  `platform_code`/`platform_hint`，命中风控再加 `risk: true`。
  目前收录：`FAIL_BIZ_IDLE_UNKNOWN_THROWABLE`（真机发布失败的裸文案，会告诉你「售价/库存填得离谱」
  并让你先改回去、隔几分钟再试）、`FAIL_BIZ_JSON_ERROR`、`FAIL_SYS_USER_VALIDATE`、`RGV587_ERROR`、
  `FAIL_SYS_SESSION_EXPIRED`、`FAIL_SYS_TOKEN_EXOIRED`（**平台自己的拼写就是 EXOIRED**）、
  `FAIL_SYS_API_NOT_FOUND`、`FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH`。
  平台拒绝不影响本地落库：改动照样写进 SQLite，`synced=false` 如实上报。


GET    /api/finance/{account_id}/income?from=YYYYMM&to=YYYYMM&biz_type=&page=1&size=20
GET    /api/finance/{account_id}/expense?   （同形状，参数名与口径完全一样）
GET    /api/finance/{account_id}/invoices?page=1&size=20&biz_types=a,b
GET    /api/finance/{account_id}/purchaser
      -- 「财务」页（第十七批）：收入账单 / 支出账单 / 申请发票 / 开票信息，**四个页面全是只读**。
      --   没有任何写接口 —— 开票申请在官方卖家工作台提交，平台侧不可逆，这里不代发
      --   （`POST/PUT/PATCH/DELETE` 一律 405，有回归 `test_finance_routes_are_read_only`）。
      -- income/expense -> {success:true,data:{account_id,kind,bill_code,bill_name,
      --     period:{from,to},biz_type,summary,bills,total,has_next,page,size,
      --     biz_categories,warnings,fetched_at,source:'platform'}}
      --   `summary`={currency,income,income_refund,expense,expense_refund,net_income,
      --     net_expense,agg_counts}（收入页只用 income/income_refund/agg_counts，支出页用 expense 那三个）；
      --   `bills[]`={bill_cycle,bill_no,biz_name,biz_type,amount,income,income_refund,
      --     expense,expense_refund,currency,agg_counts}；`has_next`/`total` 取自平台**顶层**
      --   `hasNext`/`totalCount`；`biz_categories` 是业务大类树（第一级给筛选下拉、`children`
      --   是商品类目，**不能**当 `bizType` 用）。
      -- invoices -> {...,groups,total_amount,total_count,has_next,requests,request_total,
      --     meta,warnings,fetched_at,source}；`groups[].bills[].can_invoice_amount` 平台给的是
      --   **字符串**（`"0.40"`），协议层统一成数字；`choice_type` 在平台响应里是嵌套 dict
      --   （`{'choiceType':'CAN_CHOICE'}`）。`requests` **原样透传**（真机这个列表是空的，
      --   字段名一个都没见过，不许猜映射）；`has_next` 平台不给，用平台自己的 `total_count`
      --   算（`page*size < total_count`），算不出来是 `null`。
      -- purchaser -> {account_id,purchaser_name,purchaser_type,support_invoice_types,
      --     warnings,fetched_at,source}（真机：万春华 / 个人 / ['数字化电子普票']）。
      -- **数字拿不到一律 `null`**（前端 `-`），不补 0 —— 0.00 是真实金额，两者混起来用户会以为账对不上。
      -- **登录态过期如实 409** `verification_required` + `login_state=need_relogin`：
      --   真机实测（10/10 次成功）这族接口用**网页版 Cookie** 就能调通，不受 `seller.pc.*`
      --   那种「要卖家工作台会话」的限制，所以回 `FAIL_SYS_SESSION_EXPIRED` 就是账号真掉线，
      --   **不能**像商品数据那样降级成本地统计（那会把掉线瞒过去）。
      --   没有 Cookie / `login_state='need_relogin'` 时**一次平台都不打**，直接 409。
      -- 参数校验：账期只认 `YYYYMM`（`from`/`to` 都空 = **上一个自然月**；格式错或 from>to → 400
      --   `invalid_fields`）；`page<1` 或 `size` 不在 1~50 → 400；`kind` 只认 income/expense。
      --   **默认上个月而不是本月**：平台当月账单要等次月 5 号之后才生成，查本月必然拿到
      --   `BILL_QUERY_DELAY_ERROR::暂无法查看 2026 年 10 月的账单，请在 2026-11-05 后查看`
      --   （真机 2026-10-09 实测；官方卖家工作台打开时用的也是上个月）。这个码在
      --   `services/platform_errors.py` 里有人话，502 的 `message` 会带上「改成上一个月再查」。
      -- **业务大类必须传**（真机 `INVALID_PARAM::业务大类不能为空`）：`biz_type` 没给时服务层
      --   先用 metadata 的**第一级**大类编码（收入账单是 `goods_income`），再退支出账单的兜底
      --   `ILDE_LOCAL_SERVICE_TECH_FEE_STD_PROCESS`（官方页面默认选中的「同城服务软件服务费」），
      --   两者都没有才 502。**不要自己写死大类编码** —— 编码以平台 metadata 为准。
      -- 降级：metadata 失败只加 `warnings`（分类下拉空）；summary 与 detail 只成功一个时加
      --   `warnings` 并返回那半边；两个都失败才 502 `protocol_error`。发票同理（meta 失败只 warning）。
      -- `fetched_at` 是 **UTC** 的 `'%Y-%m-%d %H:%M:%S'`（`formatTime` 对不带时区的这种串补 Z
      --   再本地化，写本地时间页面会白多 8 小时）。财务**不落库、不缓存**（金额过期比没有更糟）。
      -- 协议层真名与参数形状（`mtop.idle.finance.*`）：账单族包在 `{'request': {...}}`、
      --   发票族包在 `{'mtopRequest': ...}`，其中 `invoice.bill.query` 要**字符串化 JSON**、
      --   `invoice.request.list.query` 要**对象** —— 三种写法都是真机抓包得来的，写错只回含糊错误码。
      --   抓包与真值证据见 `docs/HANDOFF.md` 第十七批。

GET    /api/promotion/{account_id}/overview?start=YYYYMMDD&end=YYYYMMDD
GET    /api/promotion/{account_id}/campaigns?start=&end=&page=1&size=20
GET    /api/promotion/{account_id}/reports?  （计划报表 + 商品报表，参数同上）
GET    /api/promotion/{account_id}/account?start=&end=
GET    /api/promotion/{account_id}/funds?start=&end=&page=1&size=10&account_type=0&operate_type=0
GET    /api/promotion/{account_id}/fund-operations?account_type=0
GET    /api/promotion/{account_id}/recharge/{order_id}
GET    /api/promotion/{account_id}/withdraw/{order_id}
      -- 资金明细（第三十三批，**全部只读**）：官方 bundle 里**不存在**「发起充值 / 发起提现」的
      --   mtop 接口（`bp.account.log.operates` 也只是「查询可操作项」）⇒ 钱的实际划转在支付宝侧
      --   完成，本工作台只查询、不下单，因此这一批**零资金写风险**。
      -- funds -> {success:true,data:{account_id,period:{from,to},page,page_size,operate_type,
      --     operate_type_text,total,funds,warnings,fetched_at,source}}
      --   `funds` 来自 `bp.account.log.query`（`{requestJson}`），平台 `data.result[]` 原样 +
      --   `data.total`；**拿不到列表时 `funds` 是 `null` 而不是 `[]`**（前端显示「既不是没有流水
      --   也不是 0」的告警），`amountOfYuan` 是**元字符串**原样，`createTime` 形如 `20260901120000`。
      --   `operate_type` 只认 0/1/2/3（全部/现金/礼金/红包），越界 400 `invalid_fields`。
      -- fund-operations -> {...,operations}（`bp.account.log.operates`，只读；平台没回给 `null`）
      -- recharge/{order_id} -> {...,order_id,status,status_text,detail,warnings}；`detail` 是
      --   `bp.account.recharge.query` 的 `data` 原样（`rechargeOrderNo`/`channelTradeNo`/
      --   `rechargeType`/`rechargeCreateTime`/`rechargeCompleteTime`），状态枚举 ING/SUCCESS/FAIL
      --   的中文由服务层给（平台原话：「您还未支付」/「充值已到账，可在支付宝中查看扣款明细」/
      --   「充值失败，请刷新后尝试重新充值」）。
      -- withdraw/{order_id} -> 同上，接口 `bp.account.withdraw.query`（`sourceAccount`/
      --   `alipayAccount`/`operator`/`remark`/`withdrawTime`/`expirePacketAmountOfYuan`），
      --   状态原话：「正在提现中，请稍后至支付宝余额中查收」/「提现已到账，请至支付宝余额中查收」。
      -- 两条详情 GET 的 `order_id` 为空 → 400 `invalid_fields`（零平台请求）。
GET    /api/security-center/{account_id}
      -- 「超级擦亮（推广）」与「安全中心」页（第十八批）：**全部只读**。
      --   没有任何写接口 —— 充值、提现、申诉、上传资质、创建/暂停/修改擦亮计划一律不做
      --   （`POST/PUT/PATCH/DELETE` 405；`idle.ad.*` 里的 `campaign.edit/delete/pause/startV2`、
      --   `activity.apply`、`qualification.packages.submit`、`bp.account.log.operates`、
      --   `idle.pc.security.appeal.*` 一个都不接）。
      -- overview -> {success:true,data:{account_id,period:{from,to},home,account,metrics,
      --     fetched_at,source:'platform'}}
      --   `home`=平台 `mtop.taobao.idle.ad.home` 原样（`campaignInfo.{allCampaignCount,
      --     systemPauseCampaignCount,userPauseCampaignCount}`、`allAccountBalanceDetails[].balance`
      --     单位是**分**）；`account`=`bp.account.card.query`（`totalUsableBalanceOfYuan` 等是**元字符串**）；
      --   `metrics`=`member.data.query`（`data`/`dataDetailVOList`/`previousData`）。
      -- campaigns -> {...,campaigns:{result[],total,nextPage}}；`result[].adDataDetailVO` 的
      --   `cost`/`realCost`/`payAmount`/`costPerClick`/`cpm` 单位是**分**，`showPv`/`clickPv` 是次数，
      --   `ctr`/`cvr` 是百分数（原样，不除 100）；`entityList[]` 是挂在这个计划下的宝贝
      --   （`imgUrl`/`title`/`price`）。
      -- reports -> {...,plans:{result[],total},items:{result[],total}}；计划指标在
      --   `result[].idleAdGroupDataVO.data`，商品指标在 `result[].adData`，商品行自带
      --   `itemId`/`title`/`imgUrl`/`price`。
      -- account -> {...,account,settle}（`settle`=`bp.settle.settle.query`，金额是**元字符串**：
      --   `totalAmountOfYuan`/`cashAmountOfYuan`/`giftAmountOfYuan`/`redPacketAmountOfYuan`）。
      -- security-center -> {success:true,data:{account_id,score,violations,fetched_at,source}}
      --   `score`=`idle.pc.security.score.get`（闲气值在 `data.totalScore`）；`violations`=
      --   `idle.pc.security.violation.list`（`data[].punishId/punishTitle/punishReason/punishResult/
      --   punishTime(ms)/point/appealTimeLimit`，`totalCount`/`hasNext`）。
      --   ⚠ 真机结构比看起来深一层：**PC 文案不在顶层**，而在
      --   `data[].viewConfig.viewConfig.{punishTitleFromPc,punishReasonFromPc,punishResultFromPc,
      --   operationSuggestFromPc}`；顶层 `punishReason` 真机是 **null**（只读顶层会显示成 `-`，
      --   2026-10-10 打包版踩过）。官方申诉入口在 `data[].viewConfig.appealConfig.appealUrl`
      --   （`canAppeal:false` 时是空串，如申诉期已过的记录），前端只 `window.open` 该链接，
      --   自身**不提交**申诉。
      -- **安全中心必须带站点 CODE 头**：官方微应用把工作台 URL 上的 `?site=COMMONPRO` 放进请求头
      --   `idle_site_biz_code`（另有 `idle_user_group_member_id: ''`），并用
      --   `origin/referer=https://seller.goofish.com`。**不带时平台回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，
      --   `data.errorMsg` 是「站点CODE不能为空」；带空串是「用户无权限访问当前站点[]」** ——
      --   两个都不是账号掉线，别误判成掉线去让用户重扫码。真机 2026-10-10 验证：带上 `COMMONPRO`
      --   后 score.get / violation.list 都 SUCCESS（闲气值 100、违规 3 条）。客户端入口
      --   `XianyuClient._security_headers()`，`mtop(..., extra_headers=...)` 是通用逃生口。
      -- 日期与分页：`start`/`end` 只认 `YYYYMMDD`（格式错或 from>to → 400 `invalid_fields`；
      --   都空 = 最近 7 天）；`size` 必须 1~50（越界 400）。报表族参数照官方 bundle 调用点：
      --   `{startDate,endDate,pageNum,pageSize,includeEndDate:true,allPeriod:false}`；
      --   `campaign.search` 另有可选 `titleLike`。
      -- 登录态过期如实 409 `verification_required` + `login_state=need_relogin`；没有 Cookie 时
      --   **一次平台都不打**。**数字拿不到一律 `null`**（前端 `-`），不补 0。
      -- 推广/安全中心都**不落库、不缓存**（余额与投放数据过期比没有更糟）。
      -- 协议层真名、参数形状与真值证据见 `docs/HANDOFF.md` 第十八批。

--------------------------------------------------------------------------
第十九 / 二十批：官方卖家工作台复刻（新增路由组，2026-10-10 ~ 10-11）
--------------------------------------------------------------------------
-- 每组都有独立的契约文件（`docs/contracts/<name>.md`），那里才是字段级真相
-- （信封、null 口径、平台错误码、真机证据）。这里只列路由，便于一眼看全。

【只读 · 交易】`docs/contracts/trade.md`
GET /api/trade/{account_id}/orders?page=&size=&include_counts=
GET /api/trade/{account_id}/refunds?page=&size=
GET /api/trade/{account_id}/evaluations?page=&size=
GET /api/trade/{account_id}/complaints?page=&size=
GET /api/trade/{account_id}/addresses
       -- 退货地址字段：bundle 里**有两套读法，后端两套都认**（真机这条对我们无权限，
          拿不到样本，所以不判定哪一套是真的）—— 只读页那套
          `{addressId,name,phoneNum,address(拼好的整串),defaultAddr}` →
          `address_id/name/phone/address/is_default`；写页编辑弹窗那套
          `{contactId,contactName,mobilePhone,provinceName,cityName,districtName,detailAddress}`
          → `contact_id/contact_name/mobile_phone/province_name/city_name/district_name/detail_address`，
          **平台给了才出现**（空值不出现，绝不凭空造字段）。

【读 + 写 · 小铺与通知】`docs/contracts/shop.md`、`docs/contracts/notify.md`
GET /api/shop/{account_id}/sub-accounts?page=&size=
GET /api/shop/{account_id}/cs-routing
GET /api/notifications/{account_id}        -- ⚠ 前缀是 /api/notifications（不是 /api/notify）
POST /api/shop/{account_id}/cs-routing/rule   {distribution_method,repeat_customer,offline_message,specified_account_id?,specified_account_nick?,service_time?,confirm}
POST /api/shop/{account_id}/cs-groups         {operate_type:create|update,group_id?,group_name,mode,specified_type?,specified_type_name?,type_data?,customer_uids?,confirm}
POST /api/shop/{account_id}/cs-groups/delete  {group_id,confirm}
GET  /api/shop/{account_id}/profile
POST /api/shop/{account_id}/profile/nick      {nick,confirm}
POST /api/shop/{account_id}/profile/desc      {desc,confirm}
POST /api/shop/{account_id}/profile/avatar    {avatar_url,confirm}
GET  /api/sub-accounts/{account_id}/roles
POST /api/sub-accounts/{account_id}                   {nick,mobile,custom_name?,role_ids,password,confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/delete    {confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/mobile    {mobile,confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/roles     {role_ids,confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/password  {password,confirm}
       -- 店铺资料族（`mtop.alibaba.idle.shop.manage.*`）：官方读侧是**扁平**结构、
          写侧成功判据看 `result.success`（失败读 `result.data.message`）；
          昵称 4~12 字、头像必须 http(s)、简介非空；平台没给的字段一律 null。
       -- 子账号族（`...platform.sub.account.*`）：成功判据看 `data.code == 'success'`（失败读 `msg`）；
          `role_ids` 收整数数组或逗号串；手机号 `^1[3-9]\d{9}$`、密码字母+数字 8~15 位；
          **密码由本机服务端用 `sub.account.sub.pk` 的公钥做 RSAES-PKCS1-v1_5 加密后提交**
          （前端发明文，日志/响应/事件都不含密码；拿不到公钥就 502 且零提交）；
          平台没回显 subId → null + warning，不造 id。
       -- 三条 profile 写 + 五条子账号写都在闸门内（`confirm` 原样交服务层判）；
          两条只读（`/profile`、`/roles`）**故意没有 confirm**。
       -- 两条 mtop：`...customer.routing.rule.operate`、`...customer.group.operate`（v1.0，
          `routingRules` / `serviceRange` / `customers` 都是 **JSON 字符串**）；
          `repeat_customer` 是**枚举字符串**（与 distribution_method 同一套 4 值，不是布尔）；
          分组 `mode` 官方写侧小写、读侧大写 ⇒ **原样透传不转换**；
          `confirm` 原样交服务层判（HTTP 层不做 bool()，`bool('false')` 是 True）；
          成功判据**两层都看**（外层 success + 内层 data.code 只认 success），
          只看外层会把「外层成功、内层失败」判成保存成功；
          平台没回显 rule/group_id → null + 中文 warning（不造值）；
          这两条 mtop **继续留在 `xianyu/shop.py::WRITE_APIS` 黑名单**（只读路径仍一条不许碰）。

【只读 · 数据】`docs/contracts/datacenter.md`
GET /api/datacenter/{account_id}/overview?start=YYYYMMDD&end=YYYYMMDD
GET /api/datacenter/{account_id}/commodity?start=&end=&item_id=&item_name=&page=&size=&order_by=&order=
       -- 「商品数据」页（第三十四批）：官方菜单树里我们最后一个没复刻的叶子页（/seller-data/commodity）。
       --   默认区间是**今天**（跟官方一致），与总览/粉丝/客服的「今天往前 7 天」故意不同。
       --   响应多出 items / columns / total / page / size / pages：
       --     `columns` 是**平台给的列定义**（[{name,title,type}]），不翻译、不补列；
       --     平台没给（或给空数组）是 null / []，前端退回「按第一行的键显示」并说明列头不是官方标题。
       --     `items === null` = 平台没返回行（无权限/繁忙），与 `items: []`（真的没有商品）是两件事。
       --     `total` / `pages` 认不出给 null 不补 0；`total` 真的是 0 时原样保留 0。
       --   官方渲染规则（从 bundle 抄的）：第一行该列的值 < 1 时整列渲染成「原样数字 + %」，不乘 100。
       --   官方这一页的导出/下载接口（…export.item.list 一族）**故意不做**（数据外带，需单独评估）。
GET /api/datacenter/{account_id}/fans?start=&end=
GET /api/datacenter/{account_id}/customer-service?start=&end=
GET /api/datacenter/{account_id}/card/{kind}?start=&end=
       -- 「经营卡片」四张（第三十五批）：kind ∈ trade / seller / repurchase / refund，
       --   对应 flow.detail / singleuser.seller.summary / singleuser.repurchase.summary / refund.summary。
       --   **一次一张卡**：某张没权限/平台报错只影响那一张，另外三张照常显示（官方页面结构如此）。
       --   响应多出 title / metrics / banner / series / promotion / extra / real_date_range：
       --     指标名**以平台为准**（label 为 null 时显示平台字段名 platform_name），**不自己编中文**；
       --     平台没给的值是 null（前端显示「-」，不补 0）；metrics/banner/series 拿不到一律 []；
       --     `extra` 是没登记过的字段原样带出（不翻译、不丢），前端折叠在「平台原始字段」里；
       --     `series[].points` 是非数字点可跳过的折线点，前端画线时**不插 0**（插 0 会画出假谷底）。
       --   kind 不在枚举里 → 400 invalid_fields，一次平台都不打。
       --   **平台「拒绝访问」是有效答复不是故障**（与总览/粉丝/客服同一口径）：
       --     真机这个账号对四张卡都回 FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问
       --     ⇒ 这一张给 200 + 一句人话 warning（不是 502，更不是把 mtop 原文糊在卡片上）；
       --     登录态过期（409）与链路/风控故障（502）仍然照抛。
GET /api/datacenter/{account_id}/cs/{kind}?start=&end=&page=&size=&order_by=&order=
       -- 「客服数据」页三张明细表（第三十六批）：kind ∈ detail（客服明细）/ diversion（订单分流明细）
       --   / evaluation（客户评价明细），对应 cs.detail.list / cs.diversion.list / cs.evaluation.list。
       --   默认区间是**今天**（官方客服页就是当天，与总览/粉丝的「今天往前 7 天」故意不同）。
       --   响应多出 title / columns / rows / total / page / page_size / pages / date_type：
       --     `columns` 是**平台表头**（[{key,title}]，逐字来自官方 dataIndex + 中文标题），
       --       我们不翻译、不增删 —— 平台加列就多一列；
       --     `rows === null` 是「平台没给列表字段」（界面明说不是「没有数据」），`rows: []` 才是真的没有；
       --     `total` 认不出给 null 不补 0；平台真的给 0 就原样保留 0（pages 相应为 0）。
       --   kind 不在枚举里 / order 不是 asc|desc / 给了 order 却没给 order_by → 400 invalid_fields，
       --     一次平台都不打；page/size 越界由 Query(ge/le) 先拦成 **422**（与商品数据页同一分工）。
       --   平台「拒绝访问」是有效答复（同一口径）：200 + 一句人话 warning，
       --     且**不再补「可能改了字段名」那条**（两条一起说会把「没权限」说成「字段名变了」）。
       --   未验证：三张表的真机返回值还没见过（账号无「数据」权限）。

【读 + 写 · 运费模版】`docs/contracts/item_templates.md`
GET  /api/item-templates/{account_id}?page=&size=&template_name=
GET  /api/item-templates/{account_id}/{template_id}
POST /api/item-templates/{account_id}/create                {template_name,item_prov,item_city,valuation,is_default,details,template_details,confirm}
POST /api/item-templates/{account_id}/{template_id}/modify  {同上 + version,confirm}
POST /api/item-templates/{account_id}/{template_id}/delete  {confirm}
POST /api/item-templates/{account_id}/unbind                {item_ids,mode:free_ship|template|fixed,template_id,post_price,confirm}
       -- ⚠ 官方 `post-temple` 是**运费模版**，不是「发布模板」；前缀特意避开 /api/items/，
       --   否则会被 `GET /api/items/{account_id}/{item_id}` 把 `templates` 当成 item_id 吞掉。
       -- `details[]` 元素 = {receive_division_name,first_price,excess_price,first,excess,is_default?,operate?}，
       --   协议层 `build_detail_entry` 转成官方 camelCase（`operate` 缺省 `add`），未知键原样透传。
       -- 成功判据是**平台业务码** `res.data.code === 'success'`（bundle 原文），不是 HTTP 200。
       -- `post_price` 单位**元**，协议层乘 100 成 `postPriceInCent`。
       -- 写函数一律**扁平命名参数**（与 trade_write 同族），因为 AI/MCP 的 `_filter_args`
       --   只传目标函数签名里有的键 —— 收 `body: dict` 会让 AI 侧字段全被丢掉。

【只读 · 营销】`docs/contracts/marketing.md`
GET /api/marketing/{account_id}/items?item_id=&date_type=&date_range=
GET /api/marketing/{account_id}/tasks?biz_scene=&query_scene=&page=&size=&item_ids=
GET /api/marketing/{account_id}/price-competitiveness?biz_scene=&item_id=&sku_prices=&commission_rate=
GET /api/marketing/{account_id}/records?task_id=&biz_scene=&query_scene=

【只读 · 资质】`docs/contracts/qualification.md`
GET /api/qualification/{account_id}/detail
GET /api/qualification/{account_id}/submitted-records?page=&size=
GET /api/qualification/{account_id}/categories
GET /api/qualification/{account_id}/templates
GET /api/qualification/{account_id}/reusable

【写 · 交易】`docs/contracts/trade_write.md`   —— 全部 `POST`，body 一律要 `confirm: true`
POST /api/trade/{account_id}/orders/ship                    {trade_ids,mail_no,cp_code,address_id,confirm}
POST /api/trade/{account_id}/orders/{order_id}/ship-dummy    {trade_text,pic_urls,confirm}
POST /api/trade/{account_id}/orders/{order_id}/adjust-price  {modify_fee,new_transport_fee,confirm}  -- 单位**元**
POST /api/trade/{account_id}/refunds/{refund_id}/agree       {auth_token,confirm}
POST /api/trade/{account_id}/refunds/{refund_id}/refuse      {order_id,refuse_reason_id,proof_desc,proof_urls,negotiation,confirm}
POST /api/trade/{account_id}/evaluations                     {trade_ids,feedback,rate,image_urls,anonymous,confirm}  -- 无等级传 -1
POST /api/trade/{account_id}/complaints/{complain_id}/refuse {proof_desc,proof_urls,confirm}
POST /api/trade/{account_id}/complaints/{complain_id}/proof  {proof_desc,proof_urls,kind,confirm}
       -- ★ **路由层不许写 `bool(body.get('confirm'))`**：`bool('false')` 是 `True`，会把
          「我明确拒绝」当成「我确认」。第 38 批真机探针抓到 11 处这么写过
          （`backend/app/api/trade_write.py` 8 处 + `backend/app/api/security_write.py` 3 处），
          现已全部改成**原样透传** `confirm=body.get('confirm')`，判断权只在服务层 `_truthy`。
          服务层同样**不许**裸 `if not confirm:`（`not 'false'` 是 `False`，同样会放行）。

【写 · 订单处理】`docs/contracts/trade_write.md` §10   —— 关单 / 延时 / 提醒 / 运费退款 / 撤销投诉
GET  /api/trade/{account_id}/orders/{order_id}/close-reasons
       -> {order_id, reasons, raw_keys, warnings, api, account_id, source, fetched_at}
       -- **只读、故意没有 `confirm`**（登记在 `trade_write.READ_APIS`，不进 `WRITE_APIS`）。
       -- `mtop.taobao.idle.trade.order.close.reason.get` v1.0 `{bizOrderId}`；
          官方读 `data.closeReasons`（**字符串数组**，消费点 `map(r => ({label:r,value:r}))`）。
       -- **真机已验（2026-10-13，打包版 + 账号 2217970177348）**：200 且 `reasons` 是官方那五条
          （不想卖了 / 宝贝已出售 / 买家联系不上 / 与买家协商一致 / 其他原因），
          `raw_keys` 含 `closeReasons`，`warnings: []`、`source: platform`。
       -- **`reasons === null`（平台没给列表键，可以手填）与 `[]`（平台明确说这个订单没有可选原因）
          是两件事**，前端文案必须分开说，不能都写成「没有原因」。
POST /api/trade/{account_id}/orders/{order_id}/close         {close_reason,confirm}
POST /api/trade/{account_id}/orders/{order_id}/delay         {days,order_ids?,confirm}
POST /api/trade/{account_id}/orders/{order_id}/remind        {order_ids?,remind_all?,confirm}
POST /api/trade/{account_id}/refunds/{refund_id}/postage-pay {order_id,pay_type?,amount?,confirm}
POST /api/trade/{account_id}/complaints/{complain_id}/revoke {confirm}
       -- 五条 mtop（bundle 偏移见 `docs/contracts/trade_write.md` §10）：
          `mtop.taobao.idle.trade.close.by.seller` **v2.0** `{tid,bizOrderId,closeReason}`
          （官方**把同一个订单号同时塞进 `tid` 与 `bizOrderId`**，我们照做）、
          `mtop.taobao.idle.trade.seller.delay.confirm` v1.0 `{orderIdList,delayTime}`、
          `mtop.taobao.idle.trade.consign.remind` v1.0 `{orderIdList,remindAllOrder}`、
          `mtop.taobao.idle.merchant.postage.refund.pay` v1.0 `{orderId,refundId,payType}`
          （**这一步是真的付钱**，官方还带 `ttid`）、
          `mtop.taobao.idle.cco.shop.complain.apply.revoke` v1.0 `{complainId}`。
       -- `days` 只认官方四档 **3 / 5 / 7 / 10**；协议层换算 `delayTime = days × 86400` 秒。
       -- `order_ids` 收数组或逗号串（去重保序、上限 50）；**留空就不发这个键**，
          后端用路径里那一个订单号（空列表 ≠ 就用这一单）。
       -- `pay_type` ∈ `alipay` / `deposit`；`amount` 只用于确认框与回执展示，**不进请求体**。
       -- 批量回执读 `data.module.{totalNum,successNum,failOrderInfos[{orderId,errorMsg}]}`，
          认不出给 `null`（**不补 0**）；`confirm` 只认 `True`/`'true'`/`1`，HTTP 层原样透传。

【写 · 退货地址】`docs/contracts/trade_write.md` §8
POST /api/trade/{account_id}/addresses                        {contact_name,mobile_phone,province_name,city_name,district_name,detail_address,area_id?,default_addr?,confirm}
POST /api/trade/{account_id}/addresses/{contact_id}/update     {同上}
POST /api/trade/{account_id}/addresses/{contact_id}/delete     {confirm}
POST /api/trade/{account_id}/addresses/{contact_id}/default    {confirm}
       -- 四条 mtop（bundle 偏移 3143703 / 3143951 / 3144220 / 3144501）：
          `…merchant.delivery.address.{add,update,delete,set.default}`。
       -- 官方表单里还有 `division`（级联 `{label,value}` 数组）与隐藏 `areaId`（区级区域码）；
          **我们没有区域码来源**（`mtop.taobao.idle.division.all.get` 对本机账号无权限），
          所以不伪造：只发省 / 市 / 区三个**名字**，`area_id` 留空就**根本不发这个键**。
       -- 官方这四个接口**根本没读响应体**（只给了 toast 文案），所以成功判据是
          「客户端没报错 **且** 平台 `code == 'success'`」；拿不到 contact_id 时给 `null`
          + warning `no_contact_id`，**不造**假 ID。
       -- `confirm` 只认 `True`/`'true'`/`1`（服务层 `_truthy`，与 items / item_templates /
          MCP 同口径）；HTTP 层把 body 里的 `confirm` **原样**交服务层，不做 `bool(...)` ——
          `bool('false')` 是 True，会把用户明确的拒绝当成同意。

【读 · 买家改地址申请】`docs/contracts/trade_write.md` §9
GET  /api/trade/{account_id}/orders/{order_id}/modify-address
       -> {order_id, status, deliver_id, can_operate, now_address, modify_address,
           decrypted, privacy_flagged, warnings:[{code,message}], platform_message, api,
           account_id, source, fetched_at}
       -- `mtop.idle.merchant.order.get.modify.address.info`（bundle 偏移 3101551）→
          `data.module = {modifyStatus, modifyAddressInfoVO, nowAddressInfoVO, modifyDeliverId}`；
          官方组件初值就是 `'NONE'`，**只有 `modifyStatus === 'WAITING'` 才给 `modifyDeliverId`**。
       -- **只查不改，所以是 GET、故意没有 `confirm`**；协议层登记在
          `order_address.READ_APIS`，不进 `WRITE_APIS`（免得被写闸门误伤）。
       -- `status` 用平台原文，拿不到是 `null`（**不编** `'NONE'`）；`deliver_id` 只在 WAITING 认；
          `can_operate === (status === 'WAITING' && bool(deliver_id))`；两个 VO 原样透传，
          平台没给是 `null`（不是 `{}`）。
       -- 隐私号订单（`nowAddressInfoVO.privacyFlag`）会再调一次
          `mtop.idle.trade.order.detail.privacy.address`（**必须带 `valueType:'string'`**，
          bundle 偏移 2127351，`{bizOrderId, pageSource:'modify_address'}`）换明文 →
          `decrypted = {name, suffix, phone_number, address}`；**解不开就是 `null` + 中文 warning**
          （`privacy_decrypt_failed`），**不编明文**，解密失败也不会把整个只读页判成失败。

【写 · 改地址审批】`docs/contracts/trade_write.md` §9
POST /api/trade/{account_id}/orders/{order_id}/modify-address/agree   {deliver_id,confirm}
POST /api/trade/{account_id}/orders/{order_id}/modify-address/refuse  {deliver_id,confirm}
       -> {order_id, deliver_id, action, api, platform_success, platform_message, platform_body, …}
       -- 两条 mtop：`mtop.idle.merchant.order.address.modify.{agree,refuse}`
          （bundle 偏移 3102802 / 3103173），参数逐字 `{bizOrderId, deliverId}`。
       -- **这是会真的改订单收货地址的写操作**：`deliver_id` 必须来自同一条订单的只读查询。
       -- `confirm` 只认 `True`/`'true'`/`1`（`_truthy`）；HTTP 层把 body 里的 `confirm`
          **原样**交服务层，不做 `bool(...)`。
       -- 三步顺序 **确认 → 字段校验 → 建客户端**；协议层再挡一次空订单号 / 空 `deliverId`
          （抛出来，不发出去）。
       -- 平台拒绝（如 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）→ 502 原文透出，**不碰登录态**。
       -- AI 侧：`trade_order_address_request`（只读、无 `confirm`）、
          `trade_order_address_agree` / `trade_order_address_refuse`（写、`required` 含 `confirm`）。

【读 · 退款凭证】`docs/contracts/trade_write.md` §7
GET  /api/trade/{account_id}/refunds/{refund_id}/verify-token
       -> {verify_url, token, scene, refund_id, api}
       -- `mtop.idle.alipay.verify.url.query`（bundle 偏移 2801524 / 2804850，参数逐字
          `{bizId:refundId, scene:'REFUND_PC', callBackUrl:…/seller-trade/refund-manage}`）。
       -- **只查不改，所以是 GET、故意没有 `confirm`**：`token` 是「同意退款」要回传的
          `auth_token`（退款列表帧里没有这个字段，真机 `btnList[].clickEvent.data.url` 是空串），
          `verify_url` 是官方让用户完成支付宝身份验证的页面（官方嵌 150px iframe）。
       -- 平台回空串时 `token`/`verify_url` 一律 `null`（不拿 `''` 冒充可用凭证）。
       -- 平台拒绝走 `protocol_error`/502，**不碰登录态**（官方这条只在 `isAdmin` 为真时才发）。
       -- 协议层登记在 `trade_write.READ_APIS`，**故意不进 `WRITE_APIS`**（否则会被只读黑名单
          与写闸门误伤）；AI 侧工具 `trade_refund_verify_token` 同样没有 `confirm`。

【写 · 推广（投放计划）】`docs/contracts/ad_write.md`
GET  /api/promotion/write/actions                            -> 动作目录（哪些能调、哪些平台没有）
POST /api/promotion/write/{account_id}/{action}               {confirm:true,payload:{...}}
       -- `confirm` **只认字面量 true**（`1`/`"true"`/缺省都是 400 `confirm_required`）。
       -- **充值/提现不提供**：官方 bundle 里没有网页版下单接口，官方按钮原文就是
       --   「请进入闲鱼App内操作」；`actions` 目录里如实返回 `unsupported` + 原因，一次平台调用都不发。

【写 · 资质】`docs/contracts/qualification.md`
POST /api/qualification/{account_id}/packages/submit   {confirm:true,request:{...}}
POST /api/qualification/{account_id}/oss-credential    {confirm:true,request:{...}}

【写 · 安全中心申诉】`docs/contracts/security_write.md`
POST /api/security-center/{account_id}/appeal/form       {form_id}            -- 只读语义，**故意不要 confirm**
POST /api/security-center/{account_id}/appeal/submit     {parameter,confirm}
POST /api/security-center/{account_id}/appeal/cro/submit {punish_id,proof_items,confirm}
POST /api/security-center/{account_id}/appeal/urge       {punish_id,confirm}
       -- 站点 CODE 头与只读侧同一条路径（复用 `item_templates.site_headers(client)`）。
       -- `confirm` 的口径一句话：**只钉有副作用的那一步** —— 读表单不钉，提交申诉才钉。
       -- AI 侧 4 个工具同名：`security_appeal_form`（只读、无 `confirm`）+
         `security_appeal_submit` / `security_appeal_submit_cro` / `security_appeal_urge`（都要 `confirm`）。

【读 + 写 · 草稿箱】`docs/contracts/drafts.md`   —— 官方 seller-item 的「草稿箱」tab
GET  /api/drafts/{account_id}?page=&size=            -- 分页只允许 10 / 20 / 50
GET  /api/drafts/{account_id}/{draft_id}             -- fields 是平台原样 payload，键名不翻译
POST /api/drafts/{account_id}/{draft_id}/delete      {confirm}
POST /api/drafts/{account_id}/batch-delete           {draft_ids,confirm}
       -- `draft_ids` **两种入参都收**：UI 传数组、AI 传逗号串；到平台一律是逗号串
       --   （bundle @4098620 原文 `q0({draftIds: T.join(",")})`）。
       -- **空草稿箱算成功**（`items: []` / `total: 0` / 无 warning）—— 判据不是「业务体为真」，
       --   而是「客户端没报错 + 平台 code=='success'」；官方对这两个删除接口根本没读响应体。
       -- 平台只给 `sucCount`/`failCount`、不说是哪几条 ⇒ `failed[]` 恒为空 + warning
       --   `no_per_item_report`；两个计数都没有 ⇒ `ok: null` + warning `no_counts`（不编 0）。
       -- `draft.update` **故意不实现**（payload 是整份发布表单的 `inputJson`，形状无证据），
       --   登记在 `xianyu/draft.py::NOT_IMPLEMENTED`，测试断言它既不在 READ_APIS 也不在 WRITE_API_SET。
       -- 单开 `/api/drafts` 的原因与 `/api/item-templates` 相同：`/api/items/{account_id}/{item_id}`
       --   会把 `drafts` 当 `item_id` 吃掉。
       -- AI 侧 4 个工具：`drafts_list` / `drafts_get`（只读）+ `drafts_delete` /
       --   `drafts_batch_delete`（要 `confirm`）。

【读 + 写 · 话术（官方 IM 工作台的「快捷回复」）】`docs/contracts/quick_reply.md`
GET  /api/quick-reply/{account_id}/groups                    -- 分组 + 每组的短语
POST /api/quick-reply/{account_id}/groups                    {group_name,confirm}
POST /api/quick-reply/{account_id}/groups/{group_id}         {group_name,confirm}
POST /api/quick-reply/{account_id}/groups/{group_id}/delete  {confirm}
POST /api/quick-reply/{account_id}/phrases                   {group_id,content,confirm}
POST /api/quick-reply/{account_id}/phrases/{content_id}      {group_id,content,confirm}
POST /api/quick-reply/{account_id}/phrases/{content_id}/delete {group_id,confirm}
       -- 三个平台接口：`idlemessage.quickreply.list.get.v1`（**不带参数**）、
       --   `…quickreply.group.operate`、`…quickreply.content.operate`（后两个都 `needLoginPC:true`）。
       -- 官方上限照抄：分组名 ≤10 字（输入框 `maxLength:10`）、短语内容 ≤200 字
       --   （`maxLength:200` 且 onChange 里再 `slice(0,200)`）；本地超限 → 400 `invalid_fields`，零平台调用。
       -- 成功判据是**平台回显 id**（`groupInfo.groupId` / `contentInfo.contentId`）；只有它存在才算
       --   `saved:true`。平台收了却没回显 ⇒ `saved:false` + 中文 warning（`no_group_id` /
       --   `no_content_id`），**既不谎报成功也不谎报失败**；删除动作另给 `deleted`。
       -- `groups === null`（平台没给列表键）与 `groups: []`（真的没有分组）是两件事，前端分开提示。
       -- 这一族只有一个列表接口 ⇒ **平台拒绝不做容错降级**，如实 502 + 人话（与经营卡片不同）。
       -- 单开 `/api/quick-reply` 而不并进 `/api/chats`：那边是本地会话/消息两张表，
       --   话术是平台侧配置，跟本地数据无关。
       -- AI 侧 7 个工具：`quick_reply_list`（只读、无 `confirm`）+ `quick_reply_group_create` /
       --   `quick_reply_group_update` / `quick_reply_group_delete` / `quick_reply_phrase_create` /
       --   `quick_reply_phrase_update` / `quick_reply_phrase_delete`（都要 `confirm`）。

【写接口的三条硬口径（所有 *_write 一律遵守）】
1. 每个写函数第一个参数是 `confirm`，未确认直接失败（`confirmation_required` /
   `confirm_required`），**一次平台请求都不发**；
2. 平台没返回的字段一律 `null`，不补 `0`/`false`/`{}`；
3. 无权限（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 等）走 `protocol_error` / HTTP 502，
   **不改 `login_state`、不写 `need_relogin`** —— 无权限 ≠ 掉线。

【AI 侧】`TOOL_SPECS` 108 条 / `MASTER_TOOLS` 107 条（去掉 `master_chat` 防递归），
其中 **51 条是写工具**（`required` 必含 `confirm`、MCP method 必为 `POST`、description 带中文警示）；
`mcp_server/client.py::TOOL_ROUTES` 115 条。五方对账（specs / master / routes / service targets / handlers）
+ 「路由 path·query·body ↔ 真实 FastAPI 端点签名」用 **`scripts/reconcile_tools.py`** 一键跑，必须 `RECON OK`：
```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& set XIANYU_APP_DIR=%TEMP%\xywb_recon&& C:\Python310\python.exe scripts\reconcile_tools.py"
-> specs=114 master=113 routes=115 targets=94 handlers=114 / write_tools=51 / route_checked=115 / RECON OK
```
> 新增写工具后**必须**同步改两处，否则一定出现假阳性/漏网：① `scripts/reconcile_tools.py` 里的
> `WRITE_PREFIX` 元组；② `backend/tests/test_agents_cross.py` 的工具名清单（**按 `TOOL_SPECS`
> 真实顺序**）与写工具 `confirm` 断言；③ 若新增/删除写路由，还要同步
> `backend/tests/test_mcp_server.py` 里两处硬编码的写路由条数。

--------------------------------------------------------------------------
错误响应统一：`{"success":false,"message":"...","code":"..."}`，HTTP 用语义状态码（4xx 业务、5xx 内部）。成功统一 `{"success":true,"data":{...}}`。
**404/405 也必须走这个信封**（挂的是 Starlette 的 `HTTPException`，不是 FastAPI 的子类）。

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

**角色判定**（审计 A2：`bargain`/`vision` 曾经是**死角色**）：`backend/app/agents/roles.py::role_for_conversation()` 先按**被钉住的角色**决定（`master`/`bargain`/`cross`/`vision` —— 卖家在 `conversations.agent_role` 里显式指定的容器角色不被覆盖），否则**看图 → `vision`、砍价词 → `bargain`**，都没命中才回退 `item`/`cs`。所以：`services/chats.py::ensure_conversation()` **不许预写死一个合法 `agent_role`**（`agent_role=''` 表示「没钉，按规则判」），`meta.image_url` 必须透传（要走白名单，见 `api/chats.py::_incoming_meta`）—— 少了任何一条，规则永远短路，两个角色永远选不上。

**提示词三级覆盖**：`ai_prompts`（scope=global/account/item）**三级是拼接不是替换**：角色基础提示词 + global + account + item 各挑一条，按顺序拼到 system 上。**同一级里**的挑法（`prompts.py::_pick`）：先看 `role` 是否精确等于当前角色（写了具体角色的优先，`role=''` 的通用条靠后），同样精确时比 `priority`（大者胜），再相同取 `id` 大的（最新）。默认提示词写在 `backend/app/agents/defaults.py`，**用户可在 UI 里改**。
**去重键是 `(role, scope, account_id, item_id, name)`**（审计 A5）：同名 = 同一条（重复保存幂等更新），不同名 = 多条共存；以前键里没有 `name`，第二条会**静默覆盖**第一条。
`scope=account/item` 却缺 `account_id`/`item_id` → 400 `code='invalid_fields'` + `invalid_fields:{字段:原因}`（审计 A6，不允许存在永远不生效的孤儿行）。

**提供者路由**：每个 role 指定 `provider_id + model`（`ai_roles`）；未指定则用 `providers.is_default=1`。所有调用统一走 `backend/app/agents/llm.py::chat()`（OpenAI 兼容 `/chat/completions`），带超时、重试 1 次、token 计量 → 落 `agent_runs`。

**IM 入站接线（买家来信的唯一来源）**

对外函数就两个，都在 `backend/app/services/im_service.py`：

| 函数 | 作用 | 未就绪时 |
|---|---|---|
| `im_service.start(account_id)` | 建/复用 IM 会话，并把 `chats.attach_im_session()` 注册到协议层会话的入站 handler 上 | 返回失败信封（`xianyu_not_ready` 等），**不抛异常** |
| `im_service.stop(account_id)` | 摘掉并关闭该账号的 IM 会话 | 返回信封 |

- **后端启动时自己拉起**：`backend/app/main.py::_start_im_services()` 在 lifespan 里对每个「`accounts.enabled=1` 且有非空 Cookie」的账号调一次 `im_service.start()`。放在后台线程里跑 —— 它要建真实 IM 连接，不能拖住后端就绪。
- **运行期也必须拉起**（审计 C1/B1，这是「对话区同步不过来对话」的真因）：`backend/app/api/accounts.py` 在四个位置调 `_start_im()` / `_stop_im()` —— 扫码登录成功、`PATCH`/`toggle` 把 `enabled` 打开、`relogin` 成功后重新挂、`enabled` 关闭与 `DELETE` 时摘掉。**只靠 lifespan 那一次的话，运行期登录进来的账号在本进程内永远收不到买家消息，必须重启后端。**
- **幂等**：同一个账号重复 start 只留一个 handler（按函数对象去重 + 会话上的 `ATTACHED_ACCOUNTS_KEY` 集合）；同一平台的 `msg_id` 重复投递只处理一次。
- **不发送就不许静默**：AI 回复没发出去一定带 `code`，并写 `operation_logs`（风控拦截另写 `risk_events.kind='ai_reply_blocked'`）、`messages.sent_ok=0`、`messages.meta.send_error`。
- 缺 `cid`（HTTP 手动灌入的来信没有平台会话 id）→ `code='missing_cid'`。
- **人工回复走真签名**：`services/chats.py::_push_message()` 调的协议层接口是 `send_message(cid, text, toid=None)`（`xianyu/client.py`）。
- **缺 `cid` 绝不用买家 id 冒充**（审计 C2）：`conversations.peer_user_id` 是买家 user id，**不是** IM 会话 id，拿它顶上等于把消息往错的会话发。取不到真 `cid` 时：`code='missing_cid'`、**不构造协议层客户端、不发送**、`sent_ok=false` + `meta.send_error` 如实写「未发送」。`send_manual` 的顺序是**先算 cid 再取客户端**（cid 是会话固有属性，与协议层此刻是否就绪无关）。

## 5. DSH 插件（MCP）

`mcp_server/server.py`：stdio JSON-RPC MCP server，工具集与主管 AI 的 tool 集**同一份定义**（`backend/app/agents/tools.py::TOOL_SPECS`）。

> **工具名以本节为准**（2026-10-06 裁决，mcp 与 agents 两处不一致时按这里）：§5 名单是规范名；
> `TOOL_SPECS` 必须用这套名字。MCP 侧可额外接受动词式别名（`list_items` → `items_list` 等），
> 但 `tools/list` 只暴露规范名，避免同一个动作出现两个工具让模型选错。
>
> 名单共 **21** 个；`tools/list` 实测数量必须等于这里数出来的个数（`test_mcp_server.py` 与
> `--selftest` 都会打这个数）。

```
accounts_list / accounts_set_enabled / accounts_relogin
items_list / items_get / items_edit / items_publish / items_batch_edit / items_sync
chats_list / chats_read / chats_send / chats_set_mode
orders_list / orders_deliver
risk_status / risk_clear
providers_list / providers_upsert
agents_messages                   -- 读跨商品协查/主 AI 之间的 agent_messages
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

## 8. 前端设计系统（第十一批定，UI 改动一律遵守）

- **图标**：全部来自 `frontend/src/icons.tsx`（内联 SVG，`stroke=currentColor`、`strokeWidth=1.7`、
  24×24、`size` 默认 18、`aria-hidden`），用法 `import { IconBox } from '../icons'`。
  **全站不许再出现 emoji 当图标**（登录页的 🐟 已换成 `Logo`）。`icons.tsx` 由 Lead 独占维护。
- **颜色/间距/圆角/阴影**：只用 CSS 变量。`styles.css` 的 `:root` 原有 20 个变量**名字与语义冻结**
  （`--bg/--bg-soft/--panel/--panel-2/--border/--border-soft/--text/--text-dim/--text-mute/--accent/
  --accent-hover/--ok/--warn/--danger/--ask/--answer/--notify/--radius/--radius-sm/--shadow`）；
  第十一批追加了间距 `--sp-1..6`、字号 `--fs-11/12/13/14/16/20`（第十二批又补了页面级
  标题/正文实际在用的 `--fs-15/17/19`，所以现在共九档：**11/12/13/14/15/16/17/19/20**，
  **没有 18**：写别的档位要新加变量，不许硬编码 `font-size: 15px`；历史遗留的其它硬编码
  13.5/15.5/26/30px 不强制回改）、`--radius-lg/--radius-pill`、
  `--shadow-sm/md/lg`、成对语义色（`--ok-soft/--ok-line` 等）、表面 `--surface/--row-hover/
  --row-selected/--overlay`、`--focus`、`--dur-*/--ease`、`--sidebar-w`。**不许删改既有 class 名**
  （其它页面在用）；新样式写进各自页面的 CSS 文件（`components.css`、`pages/pages.css`、
  各页 `xxx.css`）并在页面里 `import`。
- **主题**：`document.documentElement.dataset.theme` = `dark`（默认）| `light`，持久化在
  `localStorage['xywb_theme']`；`main.tsx` 在 `createRoot` **之前**调 `initTheme()`（否则浅色用户
  先闪一下深色）。浅色块**只覆盖变量**、不写选择器；两块都要写 `color-scheme`（否则复选框/
  下拉弹层/原生滚动条仍按深色渲染）。
- **实时刷新**：一律 `useAsync(loader, deps, immediate, topics)` 的第 4 参订阅 WS topic，
  **不许引入轮询**。
- **不引第三方依赖**：打包要离线可用，图表/拖拽/图标都自己写。
