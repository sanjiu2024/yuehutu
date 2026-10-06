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
POST   /api/accounts/qr/start     -> {ticket,qr_image_b64,expires_in}
GET    /api/accounts/qr/{ticket}  -> {state:pending|scanned|confirmed|expired|failed,account_id?}
PATCH  /api/accounts/{id}         {enabled?,auto_reply?,remark?}   -- enabled 就是 IM 入站的生命周期
DELETE /api/accounts/{id}         -> {deleted:true,purged:true,counts:{conversations,messages}}
      -- 删号**连带清掉该账号的 conversations 与 messages**（审计 C4，以前删完还能看到该账号的会话）；
      --   先摘 IM 会话再删行。清理失败不假装成功：回 `purged:false` + `error`。
      --   `counts` 由 `services/chats.py::purge_account_data(account_id)` 给出（幂等，第二次 0/0）。
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

GET    /api/items?account_id=&status=&keyword=&page=&size=
GET    /api/items/{account_id}                 -> 该账号的商品列表（带 status/keyword/page/size）
GET    /api/items/{account_id}/{item_id}       -> 单条商品（MCP 的 items_get 直连这条，别再全表拉）
      -- 单条商品**必须**带账号上下文。曾经存在过一条扁平的 `GET /api/items/{item_id}`，
      --   它与账号列表路径形状完全相同、被 FastAPI 按声明顺序吃掉，已删除：同一个
      --   单段路径不可能既是「账号列表」又是「商品详情」，只能保留一个。
DELETE /api/items/{account_id}/{item_id}       -> {deleted:true,account_id,item_id}
      -- **只删本地记录**：平台侧没有「删除商品」接口，闲鱼上架着的商品不受影响，下次
      --   「同步商品」若平台仍返回它就会被写回（前端文案必须这么告诉用户）。会话/消息/
      --   订单**都不删** —— 它们是聊天与交易历史。严格配对 `(account_id,item_id)`，
      --   跨账号或不存在一律 404 `not_found`；成功写 operation_logs action='delete_item'
      --   并发 `items.updated`（payload.action='delete'）。
GET    /api/items/{account_id}/stats?item_id=&source=auto|local|platform
      -> {success:true,data:{items:[{item_id,title,images,price,status,
             exposure,visits,visitors,wants,orders,buyers,paid_orders,
             conversations,messages,last_message_at}],
             total,account_id,updated_at,source,warnings,metrics}}
      -- 「商品数据」页的接口。**拿不到的数一律 `null`，前端显示 `-`；禁止 0 或假数据**
      --   （0 只有在平台真的回 0 时才出现，`item_stats.py::_count_or_none()` 就是这条的守卫）。
      -- `source`：`local`=纯本地统计（不碰协议层、不校验 Cookie）；`auto`（默认）=能拿平台
      --   就拿、失败降级为 local 并在 `warnings` 里说清原因；`platform`=必须拿到，否则 502。
      -- `warnings` 是给用户看的诚实说明（例如「平台未提供商品数据接口，仅显示本地统计」）。
      -- `metrics.platform` 目前是 `exposure/visits/visitors/wants`，其中平台真名与来源见 §2。
      -- 路由**必须挂在 `/api/items/{account_id}/{item_id}` 之前**（`main.py` 的
      --   `ROUTE_MODULES` 里 `item_stats` 排在 `items` 前），否则被动态段吞成「商品不存在」。
POST   /api/items/{account_id}/sync            -> 从闲鱼拉取在售商品入库
POST   /api/items/{account_id}/publish         {title,price,description,images,category_id,...}
POST   /api/items/{account_id}/{item_id}/edit  {price?,title?,description?,stock?,images?}
      -- `images` 是 URL 数组（主图首位），协议层翻成平台 `imageInfoDOList` 提交（见 §2 items 注释）
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

GET    /api/chats/conversations?account_id=&mode=&limit=   （= GET /api/chats，两者等价）
GET    /api/chats/{conversation_id}/messages?limit=&before_id=
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
      -- ai_result.code 说明「为什么这条来信没有产生 AI 回复」。**case 很重要**：它是
      --   入站路径上唯一能看见闸门原因的地方（`ai_send` 只在 AI 真跑过时才非空）。
      --   保证给出的只有下面两个**闸门** code：
      --   'mode_not_auto'          该会话处于人工接管/关闭，AI 一律不生成
      --   'account_auto_reply_off' 账号级 enabled=0 或 auto_reply=0，AI 一律不生成
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
GET    /api/agents/runs?limit= ; GET /api/agents/messages?conversation_id=

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

> 这份清单必须与 `scripts/list_routes.py` 的实测输出对得上（2026-10-06 实测 **74 条路径**）。
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
