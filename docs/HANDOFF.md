# 交接与验收记录

> 本项目是**全新重写**，不是 `Projects/xianyu-super-butler` 的改造。旧项目只作为协议层
> 搬运来源（登录/Cookie/签名/风控那些踩过坑的实现）。接口真相在 `docs/CONTRACTS.md`，
> 打包细节在 `docs/PACKAGING.md`，DSH 插件在 `docs/MCP.md`。

## 一、用户的原始诉求（逐条对照）

| # | 诉求（原话要点） | 落地位置 | 验证方式 |
| --- | --- | --- | --- |
| 1 | 能自己买一个商品、配一个 AI 回复 | `agents/roles.py` 角色 `item`；`ai_prompts` 三级覆盖 | `test_agents_prompts.py` 三级优先级 |
| 2 | 同一商品里买家问别的商品时 AI 能互相沟通 / 主 AI 控制 | `agents/runtime.py::cross_ask`、`agent_messages` | `test_agents_cross.py` 断言 ask/answer 落库 |
| 3 | 助词（提示词）自己填 | 「提示词」页 + `ai_prompts` 表 | 写接口冒烟 ⑧ |
| 4 | 商品批量修改 | `services/items.py::batch_update`（改价/标题/描述/库存）；**第二十二批追加官方写接口** `batch_offline` / `batch_delete` / `quick_edit`（批量下架、批量删除、快捷改价改库存） | `test_services_items.py` + 冒烟 ④；`test_item_write_api.py` 47 项 |
| 5 | 统一 API Key 管理 | `providers` 表 + 「AI 配置」页 | 冒烟 ① |
| 6 | 主 AI 对接我 / 插件让 DSH 控制 | `POST /api/agents/master/chat` + `mcp_server/` | 冒烟 ⑦；`test_mcp_server.py` 55 项；端口文件端到端 |
| 7 | 除 AI 外我自己也能回消息 | `POST /api/chats/{id}/send` | 冒烟 ⑤（消息落库） |
| 8 | 不能老登录、风控要做好 | `xianyu/risk.py` 熔断 + `xianyu/login.py` 人工验证 | `test_xianyu_cookies.py` 37 项 |
| 9 | 集成闲鱼客服 / 网页版功能 | `services/cs.py` 意图分类 + 快捷回复；**官方卖家工作台逐页复刻**：财务四页、超级擦亮、安全中心（含申诉写）、交易五页（订单/退款/评价/投诉页内直接处理；**订单页含关单 / 延长收货 / 提醒确认收货 / 支付运费退款 / 撤销投诉五个写操作**）、小铺四页（**客服分流含规则保存与分组增删**、**店铺资料含昵称/简介/头像修改**、**子账号含新建/删除/改手机号/改岗位/改密码**）、数据三页（总览含四张经营卡片、商品数据含动态列与排序、粉丝、**客服数据含三张明细表：客服明细/订单分流明细/客户评价明细**）、**话术（官方 IM 工作台的「快捷回复」：分组与短语的增删改，读 + 写）**、通知、营销、资质、运费模版（含写）、退货地址（含写）、商品管理（含批量下架/删除/快捷改价改库存、**草稿箱**）、买家改地址审批（同意/拒绝） | `test_services_cs.py` 20 项；`test_finance_api.py`、`test_promotion_api.py`、`test_security_write_api.py`、`test_trade_api.py`、`test_trade_write_api.py`、`test_address_write_api.py`、`test_order_address_api.py`、`test_drafts_api.py`、`test_shop_api.py`、`test_shop_write_api.py`、`test_shop_profile_api.py`、`test_sub_accounts_api.py`、`test_quick_reply_api.py`、`test_notify_api.py`、`test_datacenter_api.py`、`test_marketing_api.py`、`test_item_templates_api.py`、`test_item_write_api.py`；真机探针见第十七~三十七批 |

### 官方卖家工作台菜单树 → 我们的页面（逐叶对照）

来源：`logs/seller_discover/menu.json`（`mtop.alibaba.idle.seller.platform.sys.menu.query` 的真机
响应，`siteName=idleFishSellerSite`、`bizCode=professionalSeller`），用
`C:\Users\Administrator\.dsh-tmp\menu_tree.py` 打出来；官方一共 **7 个一级组、19 个二级页**。

| 官方一级 | 官方二级（title） | 官方 path | 我们的位置 |
| --- | --- | --- | --- |
| 数据 `jylp` | 数据总览 | `/seller-data/data` | 侧栏「数据总览」`data-overview` |
| | 商品数据 | `/seller-data/commodity` | **商品页的「商品数据」tab**（用户 m06014 明确要求「商品页里面再加一页」，所以刻意不做成独立一级页） |
| | 粉丝数据 | `/seller-data/fanData` | 「粉丝数据」`fans` |
| | 客服数据 | `/seller-data/customerService` | 「客服数据」`cs-data` |
| 小铺 `shop` | 子账号管理 | `/seller-account/sub-account` | 「子账号」`sub-accounts` |
| | 客服分流 | `/im-cs-dispatch/customer-routing-service` | 「客服分流」`cs-routing` |
| | 安全中心 | `/seller-sc/home` | 「安全中心」`security-center` |
| 商品 `item` | 商品发布 | `/seller-item/publish` | 商品页的「发布商品」弹窗 |
| | 商品管理 | `/seller-item/goods-manage` | 「商品」`items` |
| | 运费模版 | `/seller-item/post-temple` | 「运费模版」`item-templates`（含增删改） |
| 交易 `trade` | 订单管理 | `/seller-trade/order-manage` | 「订单」`orders` |
| | 退款管理 | `/seller-trade/refund-manage` | 「退款」`refunds` |
| | 评价管理 | `/seller-trade/evaluation-manage` | 「评价」`evaluations` |
| | 投诉管理 | `/seller-trade/complaint-manage` | 「投诉」`complaints` |
| | 退货地址 | `/seller-trade/refund-address` | 「退货地址」`return-addresses` |
| 推广 `marketing` | 超强擦亮 | `/seller-ad/home` | 「超级擦亮」`promotion` |
| 财务 `finance` | 收入账单 / 支出账单 / 申请发票 / 开票信息 | `/seller-finance/*` | 「财务」`finance` 四个 tab |
| 通用 `common` | 通知 | `/notification-center` | 「通知中心」`notifications` |
| | 消息 | `/im` | 「会话」`chats` |

**官方菜单里没有、我们额外做的**：`marketing`（营销，来自 `seller-marketing` 微应用，官方把它
当成推广组本身而不是一页）、`qualification`（资质管理，官方放在擦亮账户设置里、不在工作台
菜单）、以及 `overview`/`accounts`/`agents`/`master`/`providers`/`prompts`/`risk`（AI 与风控
是我们自己的能力，官方没有）。⇒ **官方 19 个二级页全部有对应落地位置**。

**这些页里哪些能「动手」**（写操作一律二次确认，未确认零平台请求）：商品页（编辑 / 批量下架 /
批量删除 / 快捷改价改库存 / 发布）、运费模版页（增删改 + 给商品设运费）、订单页（发货 / 改价 /
退款处理 / 评价 / 投诉处理）、**退款页与评价页与投诉页各自行内的写按钮**（第二十三批接上，
之前这三页只读且文案还写着「一律 405」）、超级擦亮（投放管理）、资质管理（提交材料）、
安全中心（申诉提交 / 举证 / 催办）、**订单页的「改地址」审批**（同意 / 拒绝买家申请的修改收货地址，
第二十六批）。**同意退款要的 `auth_token` 不用手抄**：面板里的
「获取凭证」走只读接口 `mtop.idle.alipay.verify.url.query` 向平台要一次（第二十三批）。

## 二、验收基线（真实命令与输出）

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& set XIANYU_APP_DIR=%TEMP%\xywb_cp45_full&& C:\Python310\python.exe backend\tests\run_all.py"
-> TOTAL 59  PASS 59  FAIL 0  CASES 2412  SKIPPED 2
   （唯一 skip 仍在 test_xianyu_login.py：security.py:63-64 的空密码分支）
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_xianyu_login.py"
-> Ran 29 tests ... OK (skipped=2)
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_mcp_server.py"
-> Ran 65 tests ... OK        （含 RouteParamAlignmentTest：MCP 路由参数 ↔ 真实端点签名、
                                WriteToolConfirmGateTest：64 条写路由不带 confirm 一律零 HTTP 请求）
```

```text
cmd /c "cd frontend && npx tsc --noEmit"
-> TSC_EXIT=0
cmd /c "cd frontend && npm run build"
-> BUILD_EXIT=0   ✓ 97 modules transformed.
   frontend/dist/assets/index-BHJSXv64.js (624.98 kB, gzip 196.32 kB)
   frontend/dist/assets/index-B011Gghx.css (152.64 kB, gzip 24.02 kB)
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& set XIANYU_APP_DIR=%TEMP%\xywb_recon45&& C:\Python310\python.exe scripts\reconcile_tools.py"
-> specs=137 master=136 routes=138 targets=125 handlers=137
   write_tools=64  route_checked=138
   RECON OK：五方一致，且每条路由的 path/query/body 都被真实端点接受。
   （新增写工具后必须同步改本脚本的 WRITE_PREFIX 元组，否则必然假阳性）
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe scripts\ui_smoke.py --port 18493"
-> UI SMOKE ALL OK  (29/29 页通过)   浏览器：Edge（与桌面版 WebView2 同引擎）
   截图目录：logs/ui_smoke
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe scripts\contrast_audit.py --port 18393 --threshold 4.0"
-> NON_GET_API_REQUESTS=[]
   [dark] BAD_PAGES 0/27: []
   [light] BAD_PAGES 0/27: []
   报告：logs/contrast_audit/report.json
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe scripts\a11y_audit.py --port 18395"
-> NON_GET_API_REQUESTS=[]
   == 无可访问名字的按钮：0 类
   == 没有标签的输入框：0 类
   焦点不可见的页面：[]
   报告：logs/a11y_audit/report.json
   （两个审计脚本都不给 --port 时打的是打包版 18400）
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe scripts\live_refresh_smoke.py --port 18121"
-> LIVE REFRESH SMOKE ALL OK (2/2 阶段通过：账号页与提示词页都在无刷新的情况下自动更新)
   accounts: text_appeared=true seconds=0.21 nav_before=1 nav_after=1
   prompts : text_appeared=true seconds=0.21 nav_before=1 nav_after=1
```

```text
cmd /c "set PYTHONPATH=.&& set XIANYU_APP_DIR=<临时目录>&& C:\Python310\python.exe scripts\smoke_api.py 18130"
-> SMOKE ALL OK        （17 条只读接口全 200 且统一信封；无 token 401）
```

```text
cmd /c "set PYTHONPATH=.&& set XIANYU_APP_DIR=<临时目录>&& C:\Python310\python.exe scripts\smoke_write.py 18130"
-> WRITE SMOKE ALL OK  （11/11，见下）
```

> 注意：冒烟脚本的凭据路径跟随 `XIANYU_APP_DIR`，**给后端设了临时数据目录时，
> 必须给冒烟进程设同一个**，否则它去读真实 `%APPDATA%` 的凭据文件、密码对不上，
> 表现为 `login -> 401 用户名或密码不正确`（是调用姿势错，不是缺陷）。

### 写路径冒烟逐条结果

| 项 | 结果 |
| --- | --- |
| ① 新增 provider / 设为默认 | 200 `{"success":true,...}` |
| ② PATCH 账号 + 列表确认 | 200，备注与开关都生效 |
| ③ 发布 / 同步商品 | **503** `code=xianyu_not_ready`「账号 e2e_acc 还没有 Cookie，请先在「账号」页完成扫码登录」（不是 500） |
| ④ `POST /api/items/{id}/batch-edit` | 200，逐条 `{total,ok,synced,local_only,warnings[],failed[],skipped[]}` 汇总 |
| ⑤ 人工发消息 + 落库 | 200，`GET /api/chats/1/messages` 能读回；`meta.send_error` 如实写「无法确定平台会话 id（cid），消息仅本地留存、未发送」（**绝不用买家 id 冒充会话 id**） |
| ⑥ `POST /api/chats/1/ai-draft` | 400 `invalid_fields`（会话没有买家消息，契约行为） |
| ⑦ `POST /api/agents/master/chat` | 502 `调用模型失败：网络错误`（provider 指向死端口，未 500） |
| ⑧ 写商品级提示词 | 200，`scope=item` 落库 |
| ⑨ 风控 status / clear | 200 / **409** `verification_required`（未验证不给清冷却） |
| ⑩ operation-logs | 200，写操作逐条留痕 |
| ⑪ 真实 WebSocket | 收到 `{"topic":"accounts.changed",...}` |

**安全约束**：provider `base_url` 指向 `http://127.0.0.1:9`（discard 端口），账号
cookie 留空 → 全程不打真实闲鱼平台、不花真实 API 费用。

## 三、集成暴露并修掉的真缺陷

> 说明：第三批（11~24）来自两名干净上下文的独立复核 + 一次真实浏览器逐页冒烟。
> 它们的共同点是：**后端全绿、`tsc` 0 error、打包 exe `/api/health` 200 全都抓不到**。

1. **桌面自动登录被测试污染搞坏**（`fix(auth)` `21001c2`）：测试只改 `DB_PATH`，
   临时库的随机密码覆盖了真实 `desktop_credentials.json`，真实库 hash 还是旧的 →
   `POST /api/auth/desktop` 永远 500「管理员账号异常」，用户看到「窗口开了但没登录」。
   修：`XIANYU_APP_DIR` 挪走整个数据目录 + `ensure_admin()` 自愈 + `desktop_user()`。
2. **账号变更不发事件**（同上提交）：`PATCH`/`toggle`/`delete`/扫码成功都不 publish，
   前端账号页与 WS 订阅者收不到刷新 —— 真实 WS 冒烟表现为「连上 6 秒收不到事件」。
3. **跨商品匹配中文永远命中 0**（`agents/runtime.py`）：原按空格切词，「蓝色牛仔裤 全新」
   切出整串，买家问「这条牛仔裤多少钱」命中 0 → `cross_used=False`，核心卖点静默失效。
   改为字符 2-gram + 停用词 + 最小分。
4. **`services/chats.py` 把 async 编排层当同步调**：`reply_for_message` 是 `async def`，
   原来不 await → `RuntimeWarning: coroutine ... was never awaited`、`ai_result=None`。
5. **协议层 `Set-Cookie` 必须一条一个头**：合成 `a=1; b=2` 被 httpx 当属性解析，
   真实场景会丢掉 `x5sec` → 登录永远判失败。
6. **`NO_PROXY` 含 `[::1]` → httpx 构造即抛 `InvalidURL`**：`xianyu/net.py` 导入时消毒
   （去方括号，不丢条目 —— 丢了本地回环会走代理）。这条在旧项目里曾让整个扫码登录瘫痪。
7. **打包后 `--no-window` 静默崩溃**（`019d1b6`）：`console=False` 下 `sys.stdout` 是
   `None`，uvicorn 日志配置执行 `sys.stdout.isatty()` 抛 `AttributeError`，后端线程死掉
   且零输出（双击后「什么都没发生」）。修复前连跑 3 次全 exit 2、日志 0 字节。
8. **MCP 插件在桌面形态永远连不上**（`4a327a4` + `41701be`）：`--port 0` 由内核挑端口，
   插件只能靠写死的 8080。后端写 `data/backend_port.txt`，插件四级回退解析。
9. **集成前的两个 Lead 范围缺陷**（`777d71a`）：静态托管挂到 `'/'` 会让
   `app.openapi()` 只剩 14 条路由（API 被静态挂载吃掉），改挂 `''`；`config.FRONTEND_DIST`
   在 frozen 下指不到打包内路径，新增 `resolve_frontend_dist()` 三级查找。
10. **MCP server 五个真缺陷**（独立复核复现）：stdin 一个非 UTF-8 字节打死 server；
    `providers_upsert` 路径未转义可穿越；token 文件非 UTF-8 打死 `BackendClient()`；
    `items_get` 只取第一页；external schema 整块覆盖丢 6 个工具参数。

### 第三批：独立复核 + 真浏览器冒烟抓到的（每条都已修 + 有回归）

11. **协议层根本没接线**（`f476f0a` 起）：`common.get_client` 先找 `module.get_client`
    工厂（生产 `xianyu/client.py` 里**不存在**），退回 `cls(account_id)`；而真签名是
    `XianyuClient(cookie_str, client=None, transport=None)`。结果商品发布/编辑/上下架/
    发消息**全部只写本地 SQLite**，用户以为在操作闲鱼。缺的就是
    `account_id → store.get_cookie(account_id) → XianyuClient(cookie_str)` 这一步。
    **测试为什么没抓到**：`test_services_items.py` 自己注入了一个生产里不存在的工厂，
    假客户端还照抄了错误签名。
12. **买家来信零入口**：`chats.handle_incoming` 是唯一入口，生产调用点为零；协议层
    `ImSession` 也没有入站分发。后端能推 WS 事件、能查数据，**收不到一句真实买家消息**。
    修：协议层 `on_im_session` 钩子 + `im_service.start/stop` + 后端 lifespan 对
    「已启用且有 Cookie」的账号自动挂载（后台线程，不拖住就绪）。
13. **人工接管发错对象**：`_push_message` 按 `(account_id, peer_user_id, text)` 调协议层，
    真签名是 `send_message(cid, text, toid=None)` —— 把**账号 id 当会话、买家 id 当正文**。
14. **批量操作反向假成功**：`update_item` 算了 warning，`batch_update` 只做 `ok += 1`，
    warning 从未进汇总；前端又完全不看返回，直接 toast「已批量修改 N 个商品」。
    另一个方向是跨账号：`POST /api/items/{account_id}/batch-edit` 忽略路径账号，
    拿 A 的路径能改到 B 的商品。
15. **风控两套状态不通**：协议层 `RiskRegistry` 真拦请求但只在内存，UI 读的 `risk_state`
    表里 `record_hit`/`mark_verified` **生产零调用点** → `/api/risk/status` 永远无命中；
    用户真过了滑块也清不掉冷却；后端重启后协议层锁丢失而 UI 说还在冷却。
16. **`POST /api/risk/clear` 读请求体的 `verified`** → 「人工验证成功才能清冷却」这道
    门槛等于让用户自证，真实 UI 永远走不到 `409 verification_required`。
17. **登录后被立刻踢回登录页（最严重的一条）**：`api.ts` 的 401 分支对 `anonymous:true`
    也生效，而 `App.tsx` 用匿名方式打**需要鉴权**的 `/api/system/info` → 必然 401 →
    `clearToken()` + `setAuthed(false)`。**用户登录成功、界面闪一下又回登录页**，
    整个 UI 不可用 —— 而后端 20/20、`tsc` 0 error、`npm run build` 成功、打包 exe
    `/api/health` 200 **全部照旧绿**。真浏览器逐页冒烟首跑 9/9 全红才暴露。
18. **桌面自动登录从来没生效**：外壳在 `window.events.loaded` 写 localStorage，而这个
    事件在前端 bundle 执行**之后**才触发，`App.tsx` 的 `authed` 初值早已读完。改成
    启动时把 token 放进 URL 查询串，前端模块加载阶段收下并抹掉。
19. **`bargain`/`vision` 是死角色**：`role_for_conversation` 永不返回它们，「AI 分工细」
    这条诉求实际不成立。接入两条真实信号（发图 → vision、讲价 → bargain）。
20. **跨商品匹配误命中成色词**：停用词表收了「全新」「包邮」却没收「九成新」，而且
    **只清洗标题不清洗问题** —— 买家问一句「是九成新吗」能同时命中两个商品。
21. **MCP 地址只解析一次**：插件进程若比后端先起，会一直指向死端口/8080 直到重启宿主。
22. **未知路由 404 不走统一信封**：`@app.exception_handler(HTTPException)` 挂的是 FastAPI
    的 `HTTPException`（子类），抓不到 Starlette 路由层抛的父类。
23. **后端退出不删端口文件**：`backend_port.txt` 指向死端口，比没有文件更难排查。
24. **基线 runner 的假绿**：只按 returncode 记账，unittest 跑 0 个用例仍 exit 0；skip
    被当成通过。现在解析 `Ran N tests`/`skipped=N`，0 用例直接 FAIL。

### 第四批：用户真机第一次点「扫码添加账号」抓到的（25）

25. **扫码登录漏传查询参数，二维码永远出不来**（用户实测原话：
    「获取二维码失败: 未能从 mini_login 页面解析出 viewData」）。请求
    `passport.goofish.com/mini_login.htm` 时没带 `appName` 等参数，服务端**不报错**，
    返回 200 + 一页「非法请求 [appNameError]」，正文里没有 `window.viewData`，
    于是真因被伪装成「解析不出 viewData」。带参数后同一地址返回 28709 字节正常页面，
    `viewData.loginFormData` 齐全。
    - 修：`backend/app/xianyu/login.py` 新增 `login_query()` 作为参数唯一真源
      （`lang/appName/appEntrance/styleType/bizParams/notLoadSsoView/notKeepLogin/
      isMobile/qrCodeFirst/stie/rnd`，`rnd` 每次新生成），`_get_login_params` 与
      `_generate_qr` 两处共用它。
    - 回归：假闲鱼平台现在**复刻真实服务端行为** —— 没有 `appName` 就返回
      「非法请求 [appNameError]」页面，所以「漏传参数」这条路径在测试里能被抓住
      （此前测试对漏参数完全无感，这正是它漏到用户手上的原因）。
    - 真机证据见第四节。

### 第五批：用户真机第一次扫码（26、27）

26. **扫完码被判「二维码已过期」，登不上**（用户实测原话：「扫完码以后就给一个
    二维码已过期没有登录上」）。轮询 `/newlogin/qrcode/query.do` 时只发了
    `t/ck/appName/fromSite/lang` 五个字段。**未扫码阶段这样也能拿到
    `qrCodeStatus=NEW`**，所以从接口上看一切正常；但手机扫完确认时服务端认不出这次
    轮询，直接回 `EXPIRED`，现象就是「扫完就过期」。旧项目在这台机器上实测可用的
    实现是把 `session.params` 整个发过去（含 `umidToken`/`_csrf_token`/`hsiz`/
    `bizParams`）。
    - 修：`poll()` 改 `data=dict(session.params)`；并补 `headers=self.headers`，
      与创建二维码时同一套浏览器指纹。顺带修掉一个同类问题：
      `QRLoginManager.__init__` 里建好的 `self.headers` **之前一处都没用过**，
      登录会话前后指纹不一致 —— 现在 mini_login / generate.do / query.do 三处都带。
    - 修：状态变化时把原始响应写日志（`raw={...}`），下次状态不对能直接看服务端
      说了什么，不用再猜。
    - 回归：假平台现在**要求**轮询带 `umidToken`，缺了就回 `EXPIRED` —— 复刻真机
      现象，把这条路径变成真闸门。**先验证过它有齿**：把 `poll` 改回 5 字段，
      `test_xianyu_login.py` 立刻 5 个用例变红，其中一个正是 `'expired' != 'success'`。
27. **轮询偶发失败一次就判死整次登录**：前端 `catch` 里直接 `doneRef.current = true`
    + 弹错停轮询。轮询要跑满二维码有效期（2 秒一次 ≈ 150 次），中间任意一次网络
    抖动或后端瞬时 5xx 就把弹窗打死 —— 用户明明扫上了也登不上，且看起来像
    「二维码坏了」。改成连续失败才判死（<5 次继续轮询，只提示「正在重试」）。

### 第六批：四路只读审计 + 逐条修复（I / C / A / B / E）

> 触发：用户说「你仔细检查一下你的功能是不是都是正常的？比如说对话区同步不过来对话
> 或又或者说商品无法拉取过来发布我还没测试」——**用户两条怀疑都是真的**。
> 做法：四名队友各审一条链路（只读，报告在 `docs/audit/{chats,items,accounts,ai}.md`），
> 每条结论都带原样命令与输出；随后按写范围互不重叠的方式四路并行修复。
> 这一批的共同教训：**前面的测试全是「假客户端顺着实现签名」**，所以 500+ 用例全绿
> 而真机一打就穿。

**商品链路（I，`backend/app/xianyu/client.py` + `services/items.py`）**

28. **拉取商品根本没实现**（阻断）：`services/items.py:464` 调 `list_items`，真
    `XianyuClient` 上**没有这个属性**，`common.call_method` 按理应回 503，但服务层先
    自己 hasattr 判断再抛，于是表现为「协议层尚未实现该方法」。用户「商品拉不过来」
    的第一层原因就是这个。修：真实现 `list_items`（`mtop.idle.web.xyh.item.list` v1.0
    + `spm=a21ybx.personal.0.0`，读 `itemTopicList`）。
29. **发布商品签名不匹配**：`services/items.py:521` 传 `(account_id, payload)`，真签名
    `async def publish_item(self, payload)`（`client.py:237`）→ 真机是
    **502 `protocol_error`「takes 2 positional arguments but 3 were given」**。修：按真
    签名调用，并补齐发布链（badwords v2.0 → property.recommend v2.0 → cards.list v1.0
    → publish v1.0，`publishScene=mainPublish`）。
30. **编辑/改价 arity 错 + 只本地生效**：`edit_item` 传 3 个位置参数，真签名 2 个；
    更隐蔽的是**即使不报错也只是本地 SQLite 改了、没推平台**，接口却回
    `ok/synced`（审计 I4「假通」）。修：`edit_item` 走「editDetail v1.0 取原表单 →
    只打补丁 → edit v1.0 提交」（`publishScene=pcMainPublish`），保留
    `origPriceInCent`/类目/地址；`ok/synced` 只在真推成功后给，
    没推出去的进 `warnings/local_only` 带 `code=not_pushed`。
31. **上下架没有接口**：`downshelf/reshelf/set_status` 真类都不存在（下架是
    `mtop.taobao.idle.item.downshelf` **v2.0**；上架**没有独立接口** = editDetail 改
    `itemStatus` 再 edit 提交）。
32. **违禁词预检把任何抖动都判成违禁词**（修复时顺带发现，审计没抓到）：
    `check_bad_words` 在**异常文本**里搜 `'BADWORD'`，而异常文本本身就含 api 名
    `mtop.taobao.idleitem.badwords.prepubcheck` → 预检只要抖一下就永久拦住发布。
    修：只扫平台返回的 ret 列表。
33. **前端把单条编辑结果当批量结果读**：`{item,synced,warning}` 被当 `BatchResult`
    读，warning 被吞、提示「没有可处理的商品（共 0 个）」，用户以为没生效。
34. **`GET /api/items/{account_id}` 不存在**（被 `GET /api/items/{account_id}/{item_id}`
    吞成 404）。修：新增账号列表接口；**同时发现它和旧的扁平 `GET /api/items/{item_id}`
    形状完全相同会互相遮蔽**，核查后扁平单条没有任何生产调用方（前端与 MCP 都不用），
    按契约裁决**删掉扁平单条**并在 `docs/CONTRACTS.md` §3 写明理由。
35. **测试为什么没拦住**：`test_services_items.py` 的 FakeClient 签名**顺着实现写**，
    `inspect.signature` 对拍根本没做过。修：新增 `FakeClientSignatureTest`，
    用 `inspect.signature` 把假客户端和真类逐方法对拍。

**会话链路（C，`services/chats.py` + `api/chats.py`）**

36. **运行期登录的账号永不挂载 IM 入站**（用户「对话区同步不过来」的根因）：
    `im_service.start` 的生产调用点只有后端 lifespan 启动那一刻扫 `enabled=1` 的一次性
    挂载。用户扫码登录成功后**本进程内永远收不到买家消息，必须重启后端**。修：扫码成功、
    `PATCH enabled=true/false`、`toggle`、`relogin` 四个入口都挂/摘 IM；`start` 幂等。
37. **人工接管会把账号 id 当会话、买家 id 当正文**：`_platform_cid` 取不到平台 cid 就
    退回 `peer_user_id`（买家 user id ≠ IM 会话 id）。修：**绝不冒充** —— 拿不到 cid 就
    不发、如实回 `code=missing_cid` + `meta.send_error`，`operation_logs.success` 从
    恒 True 改成如实。
38. **账号级开关完全无效**：`enabled` 只有 lifespan 读过一次，`auto_reply` 无人读。
    修：新增 `_account_reply_gate()`，`handle_incoming` 与 `deliver_ai_reply` 两个入口
    都过闸（`code=account_auto_reply_off`；账号行不存在时放行）。
39. **幽灵会话**：`POST /api/chats/incoming` 不校验账号存在，`account_id='nope_acc'`
    照样 200 建会话。修：新建会话时才校验 → 404 `not_found`。
40. **删账号不清理会话与消息**：修：`chats.purge_account_data(account_id)`（幂等，
    返回 `{conversations,messages}`），`DELETE /api/accounts/{id}` 响应带
    `{deleted,purged,counts}`。
41. **bargain / vision 是死角色**：每次建会话都写死合法 `agent_role`，导致角色判定
    规则里「已指定角色」恒命中 → 议价 AI 与看图 AI 永不启用；图片字段也根本没透传。
    修：`ensure_conversation` 不再预写死角色；`roles.role_for_conversation` 改为
    「PINNED_ROLES 钉住 → 图片→vision、砍价词→bargain → 回退 item/cs」；`meta` 白名单
    透传 `image_url/image/cid/msg_type/source` + 从原始帧有界递归抠图。

**账号与登录态（B，`api/accounts.py` + `services/common.py` + `im_service.py`）**

42. **Cookie 过期没有任何回写路径**：协议层抛 `SessionExpiredError`，全仓无生产捕获，
    被统一降级成 `protocol_error` → 前端**永远显示「已登录」**，用户只会看到「AI 不回
    消息」。修：`common.call_method` 错误分支识别过期（三路判断：isinstance / 类名 /
    文案），写回 `login_state='need_relogin'` + publish `accounts.changed
    {reason:'session_expired'}`，响应给 **409 `verification_required`** 且保留平台原文。
43. **captcha 自证旁路**：库里 cookie 带 x5sec 时，`POST /api/accounts/{id}/captcha`
    不开浏览器就 `mark_verified('slider')`，随后清冷却直接 200 —— 等于**用户自己说一句
    「我验证过了」就能解除风控闸门**。修：必须真走到人工验证成功才算。
44. **`_m_h5_tk` 续期从不落库**：mtop 令牌只在内存改，客户端每次新建 → 反复
    重新取票。修：落库回写。
45. **唯一那个端到端扫码测试被静默跳过**（根因是 `ensure_admin()` 空密码分支），
    所以「扫码 → 建账号 → 落库」这条最关键的路径其实**从来没有自动化闸门**。
    修：夹具改为真实凭据路径，不再静默跳过。

**AI 编排（A，`agents/**` + `api/providers.py` + 新增 `core/crypto.py`）**

46. **跨商品匹配不按账号过滤**：`agents/runtime.py:208` 全表 `SELECT … FROM items
    LIMIT 500` 无 `WHERE account_id=?`，实测 acc1 的会话把 acc2 的商品拉进协查并按
    它的价格回答（**串店铺，最严重的一条**）。修：带 `account_id` 过滤。
47. **API Key 明文落库**：`SELECT … FROM providers` 能直接读到 `sk-stub-SECRET-…`，
    只有出站才 mask。修：新增 `backend/app/core/crypto.py`（CTR + HMAC-SHA256，
    密钥由 `security.local_secret()` 派生，前缀 `enc:v1:`），写库前加密、出站只回
    masked、历史明文行在 GET/POST/PUT providers 上一次性幂等迁移。
    **后果须知**：删掉 `<数据目录>/local_secret.txt` 会导致所有 API Key 解不开
    （UI 会提示重新填）。
48. **同商品同角色的第二条提示词静默覆盖第一条**（去重键不含 `name`）。修：键加 `name`。
49. **`scope=item` 但 `item_id` 为空可以创建孤儿行**（永远不生效）。修：400
    `invalid_fields` 并说明原因。
50. **`llm.chat()` 未关 `trust_env`**：本机 `NO_PROXY` 含 `[::1]` 时抛
    `InvalidURL: Invalid port: ':1]'`，报错完全不可诊断。修：`trust_env=False` +
    `net.sanitize_no_proxy()`。
51. **提示词三级叠加的前端文案说反了**（写成「角色不匹配优先级更高」）。修：按真实
    `_pick` 顺序改写为「**三级是拼接不是替换**；同级先看角色是否精确匹配，再看
    priority（越大越优先），都相同取最新」。

**全站实时刷新（E，Lead 自查发现，不在任何队友的审计范围里）**

52. **除会话页外，全站不实时刷新**：全仓只有 `pages/Chats.tsx` 订阅了 WS，
    `accounts.changed` / `risk.cleared` / `agent.message` / `agent.cross_failed` /
    `agent.reply_failed` / `agent.master.tools` 六个**已发布**的 topic 在 UI 侧
    无订阅者，`items.updated` **后端从来没发布过**。表现：改了商品/提示词/账号，
    界面一动不动，用户得手动刷新 —— 这正是用户报「对话区同步不过来」的同类现象。
    修：
    - `hooks/useAsync.ts` 新增第 4 参 `topics`，订阅后**静默重拉**（不打回加载态、
      不清空旧数据、不闪屏），各页接上自己的 topic；
    - 后端补齐事件源：商品写操作发 `items.updated`，提示词增删改发 `prompts.changed`，
      角色映射发 `providers.changed`；
    - 真浏览器证据：`scripts/live_refresh_smoke.py` —— 后端改数据后**页面文案自己出现**，
      且 `performance.getEntriesByType('navigation').length` 前后不变（证明是 WS 推送
      重拉，不是整页刷新）；账号页与提示词页都 0.21 秒内更新。
    - 契约：`docs/CONTRACTS.md` §3 新增 **WS topic 全量清单**并写明「前端页面订阅关系
      是契约的一部分」；`test_ws_events.py` 新增用例，POST 提示词后必须收到
      `prompts.changed`（真 WS 连接）。

> 这一批还留下一条**没有修**的项：`docs/CONTRACTS.md` §3 的 74 条路径中，
> `test_contract_routes.py::CONTRACT_ROUTES` 仍是手写副本，尚未自动解析契约文件。
> 下一轮补。

### 第七批：用户第一次拿真机跑打包 exe 抓到的（53、54）

> 这两条是**用户自己启动 `dist\闲鱼AI工作台\闲鱼AI工作台.exe`、扫码登录真账号、点
> 「同步商品」**才暴露出来的。前六批全部验收（24 个测试文件全绿、tsc 0 error、
> 9 页浏览器冒烟、打包 exe `/api/health` 200）都抓不到它们，因为它们只在**真 Cookie**
> 上现形：测试里的假 Cookie 自带 `_m_h5_tk`，假协议客户端也没有同名冲突。

53. **取 IM token 的方法被同名实例属性遮蔽，IM 入站永远挂不上**（`c761b1e`）。
    真机日志 `WARNING backend.app.main:worker:125 - 账号 2217970177348 的 IM 入站未挂载:
    协议层调用失败: 'NoneType' object is not callable`。根因：`client.py` 里
    `self._im_token: Optional[str] = None` 与 `async def _im_token(self)` 同名，实例属性
    把绑定方法顶掉 → `await self._im_token()` 就是 `None()`。**与机器、网络、闲鱼版本
    无关，任何环境必然复现**，即「对话区一条买家消息都收不到」的最后一个根因。
    修：属性改名 `_im_access_token`（原位留注释），方法名与调用点不动。回归锁见
    `test_protocol_wiring.py::MethodShadowingTest`：不是针对 `_im_token` 特判，而是用
    `inspect.getmembers(type(client))` 对比 `vars(client)`，**任何**实例属性遮蔽同名
    可调用方法都会被抓住。

54. **mtop 缺 `_m_h5_tk` 握手：首次调用必然失败，且失败响应下发的新令牌从不落库**
    （`58398cb`）。用户点「同步商品」看到 `协议层调用失败: MTOP 调用失败
    [mtop.idle.web.xyh.item.list]: FAIL_SYS_TOKEN_EMPTY::令牌为空`；同一原因还导致
    登录后 `_fill_profile` 直接失败（`取账号资料失败（不影响登录）: ... 令牌为空`）。
    根因：扫码拿到的真 Cookie 里**根本没有 `_m_h5_tk`**（真机键只有 `_samesite_flag_` /
    `_tb_token_` / `cookie2` / `t` / `XSRF-TOKEN` / `csg` / `sgcookie` / `tracknick` / `unb`），
    平台的设计就是第一次回 `FAIL_SYS_TOKEN_EMPTY` 并**在失败响应里 Set-Cookie 下发新
    令牌**；而 `mtop()` 只把新令牌合进内存、既不重试也不落库，下次 `get_client` 新建
    客户端就丢光 —— 商品列表永远拉不回来（这就是审计 B5「`_m_h5_tk` 从不落库」）。
    修（三步，各最小）：`mtop.is_token_error()` 把「是不是令牌问题」变成可测纯函数
    （含平台自己的拼写错误 `EXOIRED`）；`store.refresh_token_cookies()` **窄写**，只更新
    `cookie`/`updated_at`，**绝不复用 `set_cookie`**（那是扫码成功语义，会把
    `login_state` 改成 active 并清 `relogin_reason`）；`mtop()` 最多两次尝试，只有确属
    令牌问题**且响应确实下发了与本次不同的新令牌**时才重试（所以不会死循环），成功
    或拿到新令牌就落库，`account_id` 由 `common._bind_im_service` 注入 `persist_account_id`。
    区分口径对齐旧项目 `xianyu-super-butler/XianyuAutoAsync.py:2478-2503`：
    「令牌过期」可自愈，「Session过期」才需要重扫 —— 绝不把可自愈的问题伪装成需要重新登录。
    回归测试 `MtopTokenHandshakeTest`（6 条）：首次无令牌恰发两次、第二次按新令牌签名、
    令牌真落到账号行、刷新不把 `need_relogin` 翻回 active、非令牌失败与 Session过期都不重试、
    服务器一直回令牌为空时不死循环。

> 这一批还留下一条**没有修**的项：`docs/CONTRACTS.md` §3 的 74 条路径中，
> `test_contract_routes.py::CONTRACT_ROUTES` 仍是手写副本，尚未自动解析契约文件。
> 下一轮补。

### 第八批：用户第二次真机实测「消息一个没读取过来」抓到的（55、56、57）

> 用户报「我账号上面的消息一个没读取过来」，会话页 `共 0 个会话`。账号本身是好的：
> `login_state=active`、`has_cookie=true`、`runtime_state=running`，启动日志也已经有
> `账号 2217970177348 的 IM 入站已挂载`（即 53 已修好）。真机抓帧才看到根因在**注册**这一步。

55. **IM 注册用的设备 ID 与取 token 用的不是同一个，`/reg` 必然被拒**（`082a2a2`）。
    真机回帧原文 `{"code": 401, "body": {"reason": "device id or appkey is not equal", ...}}`；
    而 `ImSession.request()` 当时**完全不看回帧 code**，调用方拿到 `{}`，与「平台确实没有会话」
    不可区分 → 界面只显示「没有会话」，真实原因被完全吞掉。
    根因：取 token 时的 `deviceId` 与 `/reg` 帧里的 `did` 是**两处各自随机生成的 uuid4**。
    修：`mtop.generate_device_id()` 改成确定性（账号 + 固定盐派生），
    `client._im_device_id()` 成为唯一真源，取 token 与 `_ensure_im()` 都用它；
    `request()` 新增 `_raise_on_error()`，`code >= 400` 一律抛 `ImError` 并带平台原文
    （`/reg` 单独提示「did 必须与取 token 的 deviceId 相同」——正是这次的坑）。
    顺带把回帧形状吃全：`fetch_conversations` 读 `body['userConvs']`、
    `fetch_messages` 兼容 `body['data']`、`start_timestamp` 默认 `LATEST_TIMESTAMP`
    （传 0 不代表「最新」，平台可能直接回空）。
    回归 `test_xianyu_im.py`（16 条）：设备 ID 确定性 / `/reg` 401 与 400 分支 /
    真机会话与消息形状 / 两种回帧包装。

56. **历史会话从来不回填：IM 入站只处理「正在发生」的推送帧**（`1e7f8a9`）。
    进程启动之前就存在的会话，无论在线多久都不会自己进库——所以用户真机上永远是 0 个会话。
    修：新增 `chats.sync_im_conversations()`（取客户端 → `fetch_conversations` → 逐会话
    `ensure_conversation` → `fetch_messages` 落消息；缺 `buyer_id` 跳过；单会话失败只进
    `warnings` 不整单失败），并按 `messages.meta.external_id` 精确去重（入站出站都判）→
    重复点按钮不会灌重复消息；未读数只在平台值更大时对齐。
    `im_service.start()` 挂载成功时自动回填一次；用户侧给了
    `POST /api/chats/sync`（必须声明在 `GET /{conversation_id}` **之前**，否则被动态路由吞掉）
    和会话页工具栏的「同步历史会话」按钮。

57. **会话列表的「最后一条消息」永远是空字符串**（同 `082a2a2`，被 55 的测试当场抓出）。
    `im.parse_conversation()` 把**整条 userConvs 项**交给 `extract_message_summary()`，
    而后者内部取 `model['message']`——该项没有 `message` 键，实际内容在 `lastMessage` 里。
    修：改传 `last_wrapper or raw`。

> **这一批的诚实边界**：55/56/57 都有本地回归（`test_xianyu_im.py` 16 条 +
> `test_services_chats.py::ImBackfillTest` 6 条，均不发网络），但**真机 IM 复验没能做完**——
> 复验时 mtop 取 IM token 被平台风控拦下：`FAIL_SYS_USER_VALIDATE; RGV587_ERROR::SM::
> 哎哟喂,被挤爆啦,请稍后重试`（06:37 首次命中，06:49 仍未解）。这个冷却很可能需要真人过一次
> 滑块，**触发原因就是短时间多次取 token（15 分钟内约 5 次，含我自己的复验探针）**，
> 所以停手不再打 mtop。冷却过后请重跑一次：启动应用 → 点「同步历史会话」，
> 应看到 `success=true` 且 `conversations_new > 0`，会话页出现真实买家会话。

> 仍未修（跨批次累计）：`docs/CONTRACTS.md` §3 的 74 条路径中，
> `test_contract_routes.py::CONTRACT_ROUTES` 仍是手写副本，尚未自动解析契约文件。

### 第九批：用户第三次真机实测「怎么只显示20个商品」+「消息一个没读取过来」（58~64）

> 这一批全部是**真机抓帧**抓出来的：先拿真账号打接口，把回帧原文打出来再改代码。
> 打包 exe 与真实账号的复验结果写在每条后面。

58. **商品列表永远只有 20 件**（`674b565`）。
    真机回帧：`mtop.idle.web.xyh.item.list` 的 `nextPage` 是**布尔** `true`，
    且 `totalCount` **恒为 0**。旧代码 `next_page = int(data.get('nextPage') or 0)`
    → `int(True) == 1`，再判断 `next_page in (0, -1, number)`，第一页正好
    `number == 1` → 立刻 break。**一个账号不管多少件，永远只入库第一页 20 件。**
    修：`_has_next_page()` 兼容布尔/数字两种形态，循环翻页到「平台说没有下一页 /
    这一页没有新商品 / 到达 `ITEM_LIST_MAX_PAGES = 25`」，页间停 0.3 秒。
    复验：`POST /api/items/2217970177348/sync` → `count=45`，
    `GET /api/items/2217970177348` → `total=45`，`/api/items/stats` → `on_sale=45`。

59. **IM 注册回 `code=200`，我们当成失败**（`a3affb1`）。
    真机 `{"headers":{"reg-uid":"2217970177348@goofish",...},"code":200,
    "body":{"unitName":"PNM",...}}` —— 200 就是成功。上一批为 55 加的
    `if code: raise ImError('IM 注册未成功')` 把 200 判成失败，日志
    `IM 注册未成功(code=200)`，**IM 一条都挂不上**。改成只有非 2xx 才抛，
    原因从 `body.reason/developerMessage` 取。
    复验：启动日志出现 `账号 2217970177348 的 IM 入站已挂载`。

60. **`userConvs` 条目是三层嵌套，我们只认一层 → 32 条会话全解析成 `None`**（同 `a3affb1`）。
    真机形状：外层 `{'singleChatUserConversation': {...}, 'type': 2}`；
    `cid` / `pairFirst` / `pairSecond` / `extension` 只在**最内层**
    `singleChatConversation`，而 `modifyTime` / `redPoint` / `lastMessage` 在**中间层**。
    只认一层时 `parse_conversation()` 返回 None，界面永远是「没有会话」。
    修：`pick()` / `pick_number()` 内层优先、外层兜底；群聊（只有
    `groupChatUserConversation`）直接返回 None，不硬造空会话。

61. **消息列表的真机键名是 `body['userMessageModels']`**（同 `a3affb1`）。
    旧代码只认 `data`/`messages`/`list`。真机回帧：
    `{"code":200,"body":{"nextCursor":...,"hasMore":false,
    "userMessageModels":[{"readStatus":2,"message":{"messageId":"4340883858291.PNM",...}}]}}`。
    顺带证实 `start_timestamp` 必须是 `LATEST_TIMESTAMP`（`9007199254740991`），
    传 0 平台直接回空。

62. **IM 连接随事件循环一起销毁：每次调用都 `asyncio.run()` 一个临时 loop**（同 `a3affb1`）。
    IM 是「一个 WebSocket + 一个读循环任务」，生命周期长过多半个进程；临时 loop
    一关，连接就没了。真机现象是「会话能拉回来 32 条，但每条会话拉消息都报
    `协议层调用失败: no close frame received or sent`」。
    修：`services/common.py` 新增进程级 `shared_loop()`（daemon 线程
    `xywb-asyncio` 里 `run_forever`），`run_awaitable()` 用
    `run_coroutine_threadsafe` 投进去；已经站在共享循环里的调用退回临时线程跑，
    避免自己等自己死锁。
    复验：`POST /api/chats/sync` → `scanned=32, messages=337`，会话页 27 个会话。

63. **共享循环线程的数据库连接陈旧**（`91e7e26`）。
    `db.get_conn()` 按线程缓存连接，而 `reset_connection()` 只重置当前线程：
    长期活着的 `xywb-asyncio` 线程仍握着**上一个数据目录**的连接，编排层于是
    永远读旧库（测试里表现为「会话 1 不存在」、`ai_reply` 为 None）。
    修：连接与它对应的 `DB_PATH` 一起存线程本地，路径变了就 close 重连。
    这是 `test_chats_inbound.py` 三例回归的根因，修完全量基线回到全绿。

64. **平台例行帧打成 WARNING，把真线索淹掉**（同 `a3affb1`）。
    真机上每 30 秒一条心跳回帧（`type=` 空、`body=null`）、每分钟一次
    `/s/sync` + `/s/vulcan`（`bizType 370` 是商品草稿推送，与私信无关），
    全是 WARNING。修：`NON_MESSAGE_FRAME_TYPES = ('/s/sync', '/s/vulcan', '')`
    按 DEBUG 记，陌生帧（可能是买家消息）仍按 WARNING 留痕。

> **这一批的真机证据链**：`45` 件商品、`32` 个平台会话 / `337` 条历史消息入库、
> 会话页 27 个会话、IM 入站已挂载。全部来自打包后的 exe 打真实平台，
> 不是探针自说自话。
> **诚实边界**：买家用自己账号主动发一条新私信、触发「入站帧 → 落库 → AI 回复 →
> 发回平台」的实时链路，仍然没有被真人验证过——本批只验证了「连接活着 + 历史能拉回」。

### 第十批：用户第四次真机实测（账户名 / 客户名 / 库存 / 图片）抓到的（65~72）

65. **账号昵称/头像只有扫码那一瞬拉过**（`48aee1f`）。`client.user_profile()` 早就有
    （`mtop.idle.web.user.page.head`），但只在扫码成功那一刻调用一次；Cookie 导入 /
    令牌自愈恢复的账号 `accounts.nickname` 恒空，账号页那列就一直是「(未命名)」。
    修：`store.update_account_profile()` 窄写（空值不清旧值，**不用 `upsert_account`
    回填** —— 它是 INSERT OR REPLACE，会抹掉 cookie/enabled）+ 新增
    `services/accounts.py`（`refresh_profile(force)`、30 分钟节流、启动后台补全）+
    `POST /api/accounts/{id}/refresh-profile`。
    真机复验（数据目录副本，不碰用户真实库）：`nickname='月狐兔_'`、
    `avatar_url=https://img.alicdn.com/...`、`refreshed=true`，并落库。

66. **商品列表卡图片全空**（`d60f0fa`）。真商品在 `data.cardList`（`itemTopicList` 是空
    列表），每卡 `cardData.picInfo` 是 **dict**、`detailParams.imageInfos` 是 **JSON
    字符串**，旧代码只认 list ⇒ 45 件全无图（用户报「商品只显示了名称图片呢」）。
    修：dict / list / JSON 字符串三形态统一解析，`major=true` 排首位、`http→https`、
    按 URL 去重保序。

67. **库存拿 `1` 冒充，而且假 1 永远自愈不了**（`d60f0fa`）。列表卡根本没有库存字段
    （`detailParams.postInfo` 是「包邮」那类中文文案，不是数量），真库存在
    `item_detail` 的 `data.itemDO.quantity`（真机 1087247101297=1、1087258469593=66）。
    旧代码取不到就写 1 ⇒ 用户看到 45 件库存全是 1。修法分两步：①拿不到写 `-1`
    （未知，前端显示「-」）+ 同步后逐件补详情（0.6s/件、单次上限 60 件、遇风控/登录态
    过期立即 `break`）；②未知库存**必须覆盖**旧值（原先 `CASE WHEN excluded.stock < 0
    THEN items.stock ELSE excluded.stock END` 会保留旧代码写死的假 1，而补详情队列只挑
    `stock < 0`，假 1 连队列都进不去 —— 重新同步多少次都是「全是 1」）。
    `images`/`description` 仍保持「本次为空不覆盖本地」，只有 `stock` 改成无条件覆盖。

68. **`itemStatus` 语义正好反了，还会被 `or` 链吞掉 `0`**（`d60f0fa`）。真机 45 件里
    44 件 `itemStatus=0`，唯一的 `1` 恰好是详情 `itemStatusStr="卖掉了"` 的那件
    ⇒ **0=on_sale、1=sold**。旧代码 `'on_sale' if int(value) == 1 else 'off_shelf'`
    把这件已卖出的显示成「在售」；更糟的是 `status=0` 会被判成 `off_shelf`→`downshelf`。
    修：0/1 按真机语义映射、其它数字原样返回（不猜）、`bool` 显式拦截、
    缺失/空串按 `on_sale`（「没报下架 ≠ 确认在售」）；列表与详情两条路径共用同一套映射
    （原先列表给 `on_sale`、详情给 `None`，写同一列互相打架）。

69. **把「最后一条消息发送者」的昵称当成买家名**（`c784c80`）。真机会话条目里**没有**
    买家昵称字段，只有 `lastMessage.message.extension.reminderTitle` —— 它是那条消息
    发送者的昵称：买家发最后一条时是买家名，我自己发最后一条时就是我自己
    （用户会话列表 27 条里 17 条显示成「月狐兔_」＝他自己的昵称，报「客户名称显示不对」）。
    修：只有发送者不是 `my_id` 才认（`sender.uid` 与 `extension.senderUserId` 两条真机
    路径），否则空串；IM 同步增加修正 pass 回填/纠正库里已有的错误昵称；
    账号自己的昵称永远进不了 `conversations.peer_nickname`。

70. **编辑商品改图没有任何协议支持**（`d60f0fa`）。`_local_changes_to_edit` 只认 7 个
    字段，传 `images` 直接抛「协议层不支持修改字段」。修：`images` →
    平台 `imageInfoDOList`（5 个标量全字符串、首张 `major="true"`、尺寸缺失填 `'0'`
    占位不硬编码 1254）；服务层写库只留 URL 字符串数组（保序去重）、推平台用完整对象。
    真机复验（原样回灌同一件商品的图，只改形状不改内容）：真实尺寸 `1254×1254` 提交
    → 平台接受，回帧与原型帧逐键一致；同一件用占位 `'0'` 提交**也被接受**
    （前端拿不到尺寸时的兜底路径安全）。

71. **`synced` 假绿：图片只在本地却报「已修改并同步」**（`d60f0fa`，由前端队友审计报出）。
    `PUSHABLE_EDIT_FIELDS` 不含 `images`，同时改标题+图片时 `payload={title}` 推送成功
    ⇒ `synced=true`，而图片只落本地。修：`synced = warning is None and not local_only`，
    新增 `local_only_fields`；`images: []` 语义是「本次不改图」→ 单条 `noop:true`、
    批量加 `noop` 计数（不计入 `ok`，恒等式 `total == ok + local_only + noop + len(failed)`）。

72. **本地图片上传根本没有 HTTP 接口**（`cc389a9`，新增能力）。协议层
    `client.upload_image()` 早就写好了但没人暴露，用户想换商品图只能自己找外部图床。
    新增 `POST /api/uploads/images`（multipart，本地留档原图原字节、只把送协议层那份转
    RGB JPEG 用完即删）+ `GET /api/uploads/local/{name}`（Bearer 或 `?token=`，
    防路径穿越）。真机复验：16×16 PNG → HTTP 200 真图床 URL
    `https://img.alicdn.com/imgextra/i2/O1CN01C9CauifNhGK020sS_!!2217970177348-0-fleamarket.jpg`。

73. **图片字段混进「搜同款」网页 URL**（`2f50c26`）。重打包后的 exe 跑真机同步后，45 件
    商品的 `images` 全变成「真图 + 网页」交替：那个网页是
    `https://h5.m.goofish.com/wow/moyu/moyu-project/idle-photo-search/pages/home?...`，
    共 59 条。根因是详情响应里**每个 `imageInfo` 条目旁边**都会一起回来一条「以图搜同款」
    的跳转链接（`extra={"imageInfo":{"bizCode":"idledetail","reqFromPage":"idle_detail",
    "source":"itempic",...}}`），深扫时把该 dict 里任何 http 串都当图片收 —— 45 件里正好
    42 件被污染（＝当次 `stock_refreshed=42`，即走详情补库存的那批）。危害不止缩略图：
    用户一保存就会把网页 URL 当 `imageInfoDOList[].url` 推给平台，会把商品图写坏。
    修：抽取只认忽略 query/fragment 后以 `.jpg/.jpeg/.png/.webp/.gif/.bmp/.heic/.heif`
    结尾的 URL，过滤后为空就返回空列表。白名单必须留 `.heic/.heif`：真机 62 张真图里有
    3 张 HEIC，严格白名单会把那 3 件商品清成 0 张图。库里已被污染的 42 行用一次性脚本
    按同一规则清洗干净（`总行数 45 需清洗 42 → 清洗后仍污染条目 0 清洗后无图商品 0`，
    API 复核 `items=45 junk=0 with_images=45`）。

74. **「编辑」会把已卖出的商品重新上架**（`2f50c26`）。`1087247101297` 真机列表卡
    `itemStatus=1`、详情 `itemStatusStr="卖掉了"`；Lead 为验证「编辑改图」协议对它做了一次
    **原样回灌**（图片内容不变，只验形状）的 `edit_item`，平台接受且回帧带
    `publishScene="pcMainPublish"`；随后的真机同步里它的 `status` 变成 `on_sale`
    —— 很可能被平台重新上架了。修：推送编辑前加保守闸门，`items.status == 'sold'` 时
    不推平台、`synced=false` + warning（改动仅本地生效），批量同理。
    教训：**验证写接口不能挑「已卖出」这类状态敏感的商品**。闸门目前只看本地
    `items.status`；本地还没同步过（而平台其实已卖出）时仍会推，要彻底防住需编辑前
    多拉一次详情，尚未实现。

> **这一批的真机证据链**：`user_profile` 真机返回昵称+头像并落库；逐页抓列表
> 20/20/5 + `nextPage` 真值 ⇒ 45 件（`totalCount` 恒 0，分页不能靠它）；`itemStatus`
> 分布 44×0 / 1×1 且那件详情写着「卖掉了」；`edit_item` 带 `imageInfoDOList` 真机写入成功
> （真实尺寸与占位 `'0'` 两种都过）；上传接口拿到真图床直链。
> **重打包后的 exe 真机端到端（2026-10-06 23:5x，用户真实数据目录）**：
> - 账号昵称/头像：`POST /api/accounts/2217970177348/refresh-profile` → `refreshed=true`，
>   用户真实库里 `nickname='月狐兔_'`、`avatar_url=https://img.alicdn.com/...` ⇒
>   用户报的「账户名称没有显示」**已在真机修好**。
> - 商品：`POST /api/items/2217970177348/sync` → `count=45 stock_refreshed=42
>   stock_refresh_stopped=risk`（32s，风控在第 43 件前触发并从那里 break）。库里库存
>   变成真实值（1×16、999×13、100×5、10000×3、66、65、21、9、199、-1×3），
>   `images` 45/45 非空 ⇒「库存显示不对」「商品列表没有图片」两条**已在真机修好**
>   （图片里混着缺陷 73 的网页 URL —— 已由 `2f50c26` 的抽取规则修掉并清洗干净，见缺陷 73）。
> - 会话：`POST /api/chats/sync`（**必须带 JSON body，否则 422**）连续三次
>   `FAIL_SYS_USER_VALIDATE; RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试` ⇒ 27 条会话里
>   那 17 条 `peer_nickname='月狐兔_'`（自己的昵称）**还是脏的**：「客户名称显示不对」
>   代码已修、真机数据未修。→ 风控冷却期无法靠同步修正，于是用一次性脚本按「只清不猜」
>   把 legacy 脏值清空（`peer_nickname` 等于账号自己的昵称 / 平台评价提醒文案 / `peer_user_id`
>   等于账号自己 的行，共 20 行），API 复核：27 条会话里 **20 条 `peer_nickname=''`**
>   （前端显示「买家 <id>」）、7 条是真实发送者昵称；等一次成功的「同步历史会话」
>   会用买家真实昵称把空值补上。
> **诚实边界**：①买家昵称修复的**真机**复验被风控冷却挡住 —— 库里那 17 条还是脏的，
> 只验证到「协议层解析 + 同步修正 pass + 假帧端到端 + 副本库端到端」；等冷却退后点一次
> 「同步历史会话」即可确认（冷却期不要反复打 mtop，每次探测都会续期）。②库存 45 件一次
> 补齐的真实耗时 ≈32s 含风控中断，真实风控阈值未量化，中断路径只有单测。③真鼠标
> 拖拽/排序/粘贴没有真人操作过（tsc + 打包 + 真 HTTP 路由的对象形状已验，最坏是带提示的
> 错误卡片而不是白屏）。④缺陷 74 是 Lead 验证写入时真实触发的一次平台状态变更：已如实
> 告知用户（回复「我没有商品卖完了的」），据此**没有对平台做任何补救写操作**（不下架、
> 不改回），该商品的当前在售状态保持原样。

### 第十一批：用户第五次真机实测（UI 重设计 / 批量改数量 / 商品数据页）（75~83）

> 用户原话要点：批量修改里**没有数量**；商品页要**再加一页看商品数据**；发布商品弹窗
> 「设计不太好、用起来怪怪的」；**整个软件的 UI 为什么用 emoji 表情**，「找点图片之类的
> 也好啊」；把整个 UI 重新设计一遍 + 细节都优化；最后再来一遍审查。

75. **全站用 emoji 当图标**（用户直接点名）。侧边栏九项、空态、按钮、提示全靠 emoji，
    在不同系统/字体下大小与基线都不一致，也不专业。修：新增 `frontend/src/icons.tsx`
    —— 48 个内联 SVG 线性图标（`stroke=currentColor`、`strokeWidth=1.7`、24×24、
    `size` 默认 18、`aria-hidden`）+ 品牌 `Logo` + `ICONS` 登记表，`App.tsx` 导航/登录页
    （原 `🐟`）、`components.tsx`（空态/提示/开关/徽标）与九个页面全部改用它。
    **坑**：esbuild 会把 emoji 兼容别名表里的 `\u{...}` 转义**变成字面 emoji 塞进产物**
    （删表前 bundle 里 41 个 emoji 字形、删表后 0），所以别名表直接删掉，
    `resolveIcon` 认不出时回落盒子图标 + `console.warn`，不静默。

76. **批量修改没有「数量」，后端也只有「设为固定值」**（用户报「怎么没有修改数量」）。
    前端批量弹窗只有价格/标题/描述三块；后端 `PATCH_KEYS` 只有 `stock_set`，没有增减。
    修：①新增 `stock_delta`（正=增、负=减）；②**未知库存（`stock < 0`）时拒绝 delta**，
    回 `code='unknown_stock'` + 「该商品库存未知（平台列表没有返回库存），不能用「增加/
    减少」；请改用「设为固定值」，或先同步商品把库存取回来」—— 未知当 0 会把「加 5 件」
    静默写成 5 件，那是**改坏用户的真实库存**；`stock_set` 在未知时仍然允许（把未知覆盖成
    已知是合理操作）；③负数结果一律 `max(0, ...)` 钳到 0；④单条与批量共用同一处解析；
    ⑤`item_stats()` 的 `inventory_value` 改成 `SUM(CASE WHEN stock >= 0 THEN price*stock
    ELSE 0 END)`（未知库存不再把总价值算小/算负）。前端 `patchFor()` 三种请求体：
    固定值 `{stock_set}`、增减且库存已知→前端算绝对值 `{stock}`、增减且库存未知 `{stock_delta}`。

77. **商品页没有「商品数据」**（用户要求加一页）。修：新增
    `backend/app/services/item_stats.py` + `backend/app/api/item_stats.py` 的
    `GET /api/items/{account_id}/stats`（本地口径：会话数/消息数/订单/买家/支付 +
    `warnings` 如实说明），前端商品页加「商品数据」tab（拿不到显示 `-`，0 是真实值照显）。
    **路由顺序坑**：`main.py` 的 `ROUTE_MODULES` 里 `item_stats` **必须排在 `items` 前**，
    否则 `GET /api/items/{account_id}/{item_id}` 会把 `/stats` 当 item_id 吃掉
    （真机探针实测挂错顺序 → 404「商品不存在: probe_acc/stats」）。
    **不许编数字**：`_count_or_none()` 把 0 变 `None`；平台若只给账号级汇总又认不出属于
    哪件商品，就全 null + warning（用例 `test_account_level_response_is_not_attributed_to_every_item`）。

78. **商品数据接口：平台有，但我们这份会话打不通**（调研结论，见 `docs/CONTRACTS.md` §2）。
    卖家工作台接口 `mtop.alibaba.idle.seller.pc.item.stats.query` v1.0 只给三个字段
    （曝光次数/浏览次数/想要人数），用户参考的竞品那 7 列里另外 5 列平台**根本不提供**，
    也**没有时间窗口参数**。真机只读 9 次无风控：同一 Cookie 打 `user.page.head` 成功、
    打 seller 接口 `FAIL_SYS_SESSION_EXPIRED`，假接口对照回 `FAIL_SYS_API_NOT_FOUNDED`
    ⇒ API 名真实存在，缺的是**卖家工作台会话**（`appEntrance=seller_web` + 商家身份）。
    所以这一页现在只能显示本地口径并**如实标注**；要拿平台数据需另做一条卖家工作台登录
    入口（多一次扫码，与「不能老登录」冲突，**等用户点头再做**）。
    协议层落地：`client.item_stats()` 对 `SessionExpiredError` **本地吞掉返回 None**
    （否则会把只是缺卖家会话的好账号标成 `need_relogin`），`RiskControlError` 照抛。

79. **前端调用了后端不存在的接口**：商品行内「更多 → 删除」调
    `DELETE /api/items/{account}/{item_id}`，后端**根本没有这条路由**（`isNotReady` 时
    只弹个 toast，等于点了没反应）。修：`services/items.py::delete_item()` + API 路由。
    **只删本地记录**：平台侧没有删除商品接口，闲鱼上架着的商品不受影响，下次同步若平台
    仍返回它就会被写回 —— 前端文案必须这么告诉用户；会话/消息/订单**都不删**（那是聊天与
    交易历史）；严格配对 `(account_id,item_id)`，跨账号或不存在一律 404 `not_found`。

80. **`useAsync` 静默重拉不复位 `loading`**（inbound 审计报出）。首屏请求还在飞时来了 WS
    事件，会把上一跑 cancel（它的 `finally` 直接 return），而静默这一跑又不置 `loading`
    ⇒ 列表**永久转圈**，真实错误也被盖住。修：`finally` 无条件 `setLoading(false)`。

81. **FastAPI `_IncludedRouter` 路由对象坑**（写测试时踩到）。`create_app().routes` 的顶层
    元素是 `_IncludedRouter`（`.path is None`），真正的 `APIRoute` 在嵌套 `.routes` 里；
    只读顶层会得到「一条 API 路由都没有」的假象（顶层 17 条里 12 条空串 + 1 个 `Mount('')`）
    ⇒ 挂载断言**假失败**。修：递归 `collect(routes, out)`（走 `getattr(r,'routes')` 或
    `original_router.routes`）。`test_contract_routes.py` 的 `walk()` 本来就是这么写的。

82. **两处「看起来是缺陷其实不是」**（如实记下，免得下次重踩）：①`frontend/src/pages/risk.css`
    注释里的 `var(--xxx)` 占位符被令牌审计脚本误报「引用了未定义的变量」—— 脚本没剥 CSS
    注释；②`frontend/src/pages/Items.tsx` 的裸 `<span className="spacer" />` 在
    `styles.css` 里**没有对应规则**（`.grow` 只存在于 `.notice .grow`），元素等于不存在
    ⇒ 改 `<span style={{ flex: 1 }} />`。

83. **平台 `wantNum`（想要人数）在服务层被静默丢掉**（proto 交付时留下）。协议层解析出了
    `wants`，但服务层 `PLATFORM_METRICS` 里没有它 ⇒ 「平台给了、我们假装没看见」。
    修：加进 `PLATFORM_METRICS` 与 `METRIC_ALIASES`（页面暂不展示，但数据不再丢）。
    同批还修了 `frontend/src/hooks/useAsync.ts` 与两处空态/骨架屏竞态（见 80/82）。

> **这一批的真机/构建证据链**：`npx tsc --noEmit` EXIT=0；`npm run build` BUILD_EXIT=0
> （57 modules，`dist/assets/index-_I2zWEuE.css 97.10 kB`、`index-D3DLNsqw.js 314.68 kB`）；
> 全站 emoji 计数 0（源码与产物都查）；令牌审计「引用未定义」= 0；`styles.css` 原有 20 个
> 变量名与原值 **CHANGED=0**、既有 class **REMOVED=0**（新样式全部写进各页自己的 CSS 文件）；
> 后端起在 18097 实测 `/api/health` 200、`/` 200、js/css 200 且含 `html[data-theme=light]`。

### 第十二批：第十一批的独立复审（两个只读审查员）与复审后的修复（84~89）

> 做法：UI 与后端各派一个**干净上下文**的只读审查员，自己写临时脚本真跑（前端 11 条硬项、
> 后端 10 条硬项），只报告不改文件。前端结论「可以交付」（11/11 通过，8 条低危）；
> 后端查出 3 个真缺陷 + 4 条文档/测试问题。下面是**复审之后**的修复。

84. **`item_stats` 的四种失败在接口层完全一样**【后端审查 D1，最高优先】。协议层
    `client.item_stats()` 拿不到数据时 `return None`，服务层的「先带 item_id 再不带头」二次
    尝试又被这个 `None` 早退成「成功但没有内容」⇒ **风控、会话过期、接口下线、响应没指标**
    四种情况端到端都是同一句 502 `{"message":"平台返回的商品数据无法识别: "}`（冒号后是空的），
    而且失败时 mtop 被调了两次。修：①新增 `ItemStatsUnavailable(XianyuError)`（**故意不继承
    `SessionExpiredError`**，否则好账号会被 `mark_need_relogin()`），四种原因各自成句；
    ②`common._is_session_expired()` 按 MRO 类名**优先放行**它（消息里会带平台原文，可能含
    `Session过期` 字样，文案兜底会误判）；③签名能收下 `item_id` 时**只调一次**，不再补探测；
    ④`CALL_OK` + `None` 也算失败并保留第一次的真实 reason。
    **这一条的价值不在文案好看**：以前用户看到「无法识别」只能猜，现在能一眼分清
    「账号要重新扫码」和「这个接口族本来就不给网页版 Cookie」——后者重新扫码也没用。
85. **`wants`（想要人数）在服务层被静默丢掉**（上一批 83 号声称已修，**是假的**：只往
    `PLATFORM_METRICS`/`METRIC_ALIASES` 里加了名字，`_merge()` 的返回字典根本没输出它，
    全文件 grep `wants` 命中 0，测试也没断言）【审查 D2】。修：`_merge` 补 `'wants'`；
    新增 `test_wants_reaches_the_response` / `test_wants_missing_is_none_not_zero`
    —— 端到端断言真的出现在响应里，平台没给时是 `None` 而不是 0。
    教训：**「加进常量表」不等于「端到端可见」**，上一批的结论缺一条端到端断言。
86. **别名表认不出平台原名 + 认不出时完全静默**【审查 D3】。`METRIC_ALIASES` 缺
    `exposureNum`/`showNum`（协议层改名前的原名），且 `showCount` 挂在 `exposure` 下而
    协议层把它映射到 `visits` ⇒ 同一个词根落在两个指标上。修：别名表补齐并把 `showCount`
    移到 `visits`；新增 `_suspect_metric_keys()` + `WARN_PLATFORM_PARTIAL`：响应里有
    「像指标但没认出」的字段时留一句 warning（含字段名），`item_id`/`title`/`code` 这类
    结构性字段**不报**（每次都响的 warning 等于没有 warning）。
87. **补丁数字字段非法 → 500**【审查 D4】。`{"stock_set":"abc"}` / `{"stock_delta":null}` /
    `{"stock_delta":[]}` 一路透传到 `int(float(value))` 抛异常：单条 500，**批量整单 500**
    （连哪条出错都不说）。而且这不是理论问题：前端「增减」输入框为空时
    `Math.max(0, NaN)` 得 `NaN`，`JSON.stringify(NaN)` **就是 `null`**。修：`normalize_fields`
    里对 `NUMERIC_PATCH_KEYS` 用 `_patch_number()` 校验（`null`/非数字串/列表/布尔/NaN/Inf
    一律 400 `invalid_fields` + 「字段 <key> 需要数字，收到: <值>」；`stock_set`/`stock_delta`
    还要求整数 —— `2.5` 静默截成 `2` 也是编数字），批量里逐条进 `failed` 且恒等式仍成立。
88. **文档与实现打架的三处**【审查 D5/D6/D7】：①CONTRACTS 写「负值一律钳到 0」，
    但 `{"stock": -5}` 实际回 400 —— 两条路本来就不同（**补丁字段**钳 0、**直接写字段**
    报错），现在文档分开写清；②CONTRACTS §8 写「字号 `--fs-11..20`」，实际只有
    `11/12/13/14/16/20` 六档，已改成精确列表并写明「不许硬编码 `font-size: 15px`」；
    ③HANDOFF 的用例数 783 是旧值，实际 803。
89. **审查还抓到「测试的假保证」**：`test_platform_call_passes_item_id_once_without_probing`
    的 docstring 宣称「不许先带 item_id 再不带探测签名」，但它只覆盖**成功路径**，
    而失败路径恰好就是被探测掩盖的那条。修：新增
    `test_no_second_probe_call_when_signature_accepts_item_id`、
    `test_ok_but_none_is_a_failure_with_reason`、`test_failure_reasons_are_distinguishable`
    （三种失败必须给出三句不同的话）、`test_strict_platform_failure_message_carries_the_real_reason`
    （502 的 message 冒号后不许为空）。
    **教训：docstring 里的承诺必须有对应用例，否则它就是下一批人的错误地图。**

> **第十二批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_lead_runall`）
> `TOTAL 27  PASS 27  FAIL 0  CASES 803  SKIPPED 2`（第十一批是 783）；
> `test_xianyu_items.py` 81 用例、`test_item_stats_api.py` 39 用例、
> `test_items_api.py` 31 用例、`test_services_items.py` 134 用例，各自单跑全绿。
> 复审前端一侧：`frontend/src/styles.css` 冻结变量 20/20 未变、class 名删 0 增 0、
> 未定义 `var()` 0 个、`components.tsx` 13 个导出 props 完全一致、产物 0 emoji。

### 第十三批：用户第六次真机实测（排序 / 售罄归下架 / 编辑页 / 数据页信封）（90~96）

> 用户原话（m07455）：「有商品排序的…按商品数量排序，价格排序或者库存排序之类的排。就点一下，
> 可以切换。」「还有售罄的商品应该在下架分区里面。那样的话好编辑。」「编辑页面还需要再优化
> 一下」「这个编辑的时候为什么会弹这个？而且改动为什么会不成功？」。附的三张截图里还暴露了
> 两个我们没测到的真 bug（93、94）。

90. **状态筛选从来没生效过**【用户截图 + 真机库核对】。前端下拉框发 `onsale`/`offline`，库里
    真值是 `on_sale`/`sold`（`services/items.py` 写入的两个值），于是 `?status=onsale` 与
    `?status=offline` **永远 `total=0`**（只有 `sold` 恰好能匹配）。真机库：3 个账号 95 件商品，
    `3998190171` 的 39 件**全是 `sold`**（34 件库存未知）。修：`_status_clause()` 定义三个分区
    ——`onsale` = 在售状态**且库存不是 0**（**库存未知算在售**，否则刚同步的商品会集体消失）、
    `offline` = 非在售**或库存 = 0**、`sold` = 已卖出；其它值仍精确匹配（旧行为不变）。
    响应新增 `counts:{all,onsale,offline,sold}`，下拉框直接写「在售（N）/ 已下架 · 含售罄 /
    已卖出（N）」——**用户不用点开就知道每个分区有多少件**。
91. **售罄归入「已下架」分区**【用户诉求 ②】。这一条在服务层就是 90 里的 `stock = 0 → offline`；
    前端补两处：①列表里「在售 + 库存 0」的行标 `售罄` 徽章（它落在已下架分区里，再挂「在售」
    会让人以为筛选坏了）；②表格下 legend 明说「售罄（库存 0）和已卖出的商品都在「已下架」
    分区里」。**没有**在平台上做任何补救写操作 —— 用户明确说过「我没有商品卖完了的」。
92. **表头排序**【用户诉求 ①】。`sort` ∈ `updated|price|stock|title|status|item_id`、`order` ∈
    `asc|desc`，白名单在 `services/items.py::SORT_COLUMNS`（**前端只发字段名，绝不拼 SQL**），
    非法值回落 `updated`。库存排序时 `(stock IS NULL OR stock < 0)` **永远沉底**（升序降序都
    一样）：未知库存不是 0，排在最前面会让用户以为它们缺货。前端一个 `SortableTh` 组件同时
    服务列表（后端排）与商品数据页（一次返回全部商品，内存排，拿不到的数值也沉底），
    带 `aria-sort`、点击同列换方向、换列时标题升序其余降序。
93. **商品数据页的信封没剥**【用户截图暴露的真 bug】。平台真机回的是
    `{'code':'success','data':{'exposureNum':367,'showNum':24,'wantNum':1},'msg':'成功'}`，
    而 `parse_item_stats` 只看顶层 ⇒ 判「没有可识别的商品数据指标」并把整个页面降级成本地
    （用户看到的正是那句 warning）。修：`_stats_payloads()` 给出 `[data, data['data']]` 两个
    候选逐个尝试（裸指标 dict 与两层信封都认）；真认不出时错误里带信封的 `code`/`msg`，
    不再只说「无法识别: 」（冒号后为空）。新增 `test_envelope_shape_is_unwrapped` /
    `test_envelope_on_the_wire_reaches_the_caller` 锁住真机那个形状。
94. **提示指向一个不存在的页面**：`WARN_NO_ORDERS` 写「请先在【订单】页同步」，但侧栏九个页面
    里**没有订单页**（订单表真机 0 行）。改成如实说明「当前版本还没有订单页，先不用管它们」。
    **教训：文案里出现的每一个入口都必须真的存在。**
95. **编辑弹窗的 AI 开关是假的**【用户诉求 ④「改动为什么会不成功」的一半】。`ai_enabled` 从
    `useState` 到 `Switch` 都在，**但从来没进过 patch**：拨一下、保存、回来还是原样。服务层
    其实早就支持它（`EDITABLE_FIELDS` 有、按 `local_only` 处理、真落库）。修：只有真的拨动过
    才带上这个键（否则每次编辑都白报一条「仅本地生效」）。
96. **编辑页重做 + 把「为什么弹」讲清楚**【用户诉求 ③④】。①已卖出的商品在弹窗顶部显著说明
    「平台不接受对已卖出商品的编辑 ⇒ 本地会更新、**闲鱼上的不会变**，这不是保存失败」，
    并给出出路（先在列表里「上架」重新挂上去 / 复制一份重新发布）；②保存按钮文案变
    「保存到本地」、页脚写明「已卖出的商品闲鱼不接受编辑：保存只更新本地记录」；③toast 也
    换成人话（`toast.info('本地已保存。这个商品在闲鱼上已卖出…不是保存失败。')`），
    不再丢一句「仅本地生效」让人以为保存失败；④新增**改动预览**（字段 / 原值 → 新值），
    一个字都没改时保存按钮直接禁用 ——「点了没反应」和「改坏了」都要在点之前就知道；
    ⑤补上 `原价（划线价）` 输入（服务层 `PUSHABLE_EDIT_FIELDS` 一直支持，UI 之前没有入口）；
    ⑥价格分支从 `price !== ''` 改成 `price.trim() !== ''`（`Number(' ') === 0` 会静默把价格
    改成 0）。
97. **商品数据页的汇总卡只发有数据的那几张**：6 个指标里平台只给 3 个，以前固定画 4 张卡
    （2 张永远 `-`）纯属噪音；现在只给「至少一个商品拿得到值」的指标发卡，卡片可点排序，
    并在 legend 里说明「平台这个接口只给曝光 / 浏览 / 想要」。
98. **平台的拒绝原因要翻译成人话**【顺着用户诉求 ④ 往下查 `operation_logs` 才发现的第二个原因】。
    日志里真实存在一条：账号 `2221063792739` 的商品 `1087852912806`（小尾猫猫封口贴）把库存
    改成 100 时，平台回 `FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH::亲，您不能
    发布多库存宝贝哦` —— 这件商品当初是按「单件库存」发布的，平台不允许它变成多件。
    以前只报「协议层修改失败，改动仅本地生效: <一长串错误码>」，用户同样只会以为软件坏了。
    现在 `PLATFORM_EDIT_HINTS` 把**真机见过的**错误码翻译成一句中文解释（没见过的错误码
    **原样透传、不编解释**，有测试锁住），本地库存照旧改好，不因为平台拒绝就丢改动。
    用户诉求 ④ 的两个原因至此都能对号入座：①商品在闲鱼上真的已卖出（`operation_logs`
    里 `3998190171/1087577639387`「专拍」那条，2026-10-08 02:21 本地时间）；②这件商品是
    单件库存发布，平台不让改多件。
99. **编辑弹窗只提交真的改动过的字段**（用户那句「以下字段仅本地生效：description, price,
    stock, title」的直接来源）。`EditItemModal::save()` 以前无条件把 `title`/`description`
    塞进 patch，并且只要输入框非空就带 `price`/`original_price`/`stock` —— 用户只改了一个
    字段，返回的 `local_only_fields` 也会把那四个都列出来，看着像「我什么都没动它却改了一堆」；
    更糟的是 `original_price` 会被静默写成 `0`。现在 patch 的构造口径与弹窗里的「改动预览」
    完全一致：**值真的变了才进 patch**（`title`/`description` 也不例外），空输入框一律表示
    「这次不改」。已卖出商品的提示因此只列用户真的改过的那几个字段。

> **第十三批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_r6c`）
> `TOTAL 27  PASS 27  FAIL 0  CASES 817  SKIPPED 2`（第十二批 803）；
> `test_services_items.py` 145、`test_xianyu_items.py` 83、`test_items_api.py` 33、
> `test_item_stats_api.py` 39。前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0
> （57 modules，最终产物 `dist/assets/index-CxlIsVf9.js` 321.18 kB / `index-Cuic_y7D.css` 99.24 kB；
> 收尾前那一次是 `index-Dmp5pOuD.js` 321.03 kB）。
> 真机数据核对（只读脚本，不打平台）：3 账号 95 商品、108 会话、956 消息、订单 0 行。
> 用户诉求 ④ 的两个原因来自真实 `operation_logs` 行（不是推测）。

### 第十四批：第十三批的独立复审（两个只读审查员）与复审后的硬化（100~103）

> 第十三批交付后派了两个**独立只读审查员**（一个审后端、一个审前端，各自真跑脚本、
> 不许改仓库）。两份报告结论都是「可以交付，0 个必须修的问题」；下面 100~103 是
> 复审报出来的口子 + 我在复审过程中自己发现的一个**真隐患**。

100. **商品数据接口会把账号打到熔断**【Lead 顺着复审自己发现的真隐患】。
     `services/item_stats.py::account_stats` 在 `mode != 'local'` 时对**该账号全部商品**
     逐个调 `client.item_stats` —— 一个 45 件商品的账号点一次「商品数据」就是 **45 连击
     mtop**。这不是理论风险：真机补库存那次（第十批）就是这样触发风控的
     （`stock_refresh_stopped="risk"`）。修：`PLATFORM_ITEM_LIMIT = 10`，一次最多查 10 件，
     超出的部分用本地统计，并留一条 `WARN_PLATFORM_CAPPED` 如实写明「本次只查了前 10 件
     （这个账号共 45 件），其余先用本地统计；想查具体某件商品，用搜索框把列表缩小到它再刷新」。
     `?item_id=<单件>` 不受限流影响。**教训：凡是「按商品逐个查」的接口，页面上一次点击
     都可能变成几十连击 —— 限流要写在服务层，不能指望用户手快。**
101. **信封再包一层就会重演第十一批那次事故**【复审 D2】。`_stats_payloads()` 原来只剥
     一层 `data`：真机是两层（外层信封 + 内层指标），修好之后如果平台**再包一层**，
     「明明有 367 次曝光却报没有可识别的商品数据指标」就会原样重现（代价是整条降级成本地
     统计 + 用户看到一句错的原因）。修：按 `data` 键最多剥 `_STATS_ENVELOPE_DEPTH = 4` 层，
     到顶就停（再深就不是信封而是业务数据了）。`test_deeper_envelope_is_also_unwrapped`
     锁住三层能解析、超过上限返回 `None`（不猜）。
102. **同前缀的新错误码不许套用旧解释**【复审 D4】。`_platform_error_hint` 原来用
     `if code in text` 做**子串包含**，于是平台将来出现
     `FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH_V2` 会命中「单件库存」那条
     解释 —— 用户会拿着一个**错的理由**去想办法。修：改成**词边界匹配**
     （前后不紧邻 `[A-Za-z0-9_]`），既能穿过日志里 `MTOP 调用失败 [api]: CODE::文案`
     这种 `: ` 分隔的前缀，又不会把 `_V2` 当成旧码；文案里出现同一个词根也不算命中。
     `test_same_prefix_but_new_error_code_is_not_translated` 锁住三种情形。
103. **前端三个小口子**【复审 D1/D2/D3，都是会被用户看见或让扫描一直报警的】。
     ①编辑弹窗保存按钮的 `title` 三元顺序改成与页脚一致（上传中 > 已卖出 > 没改动 > 正常）：
     原来「没有改动的已卖出商品」按钮文案是「保存到本地」、hover 却提示「先改点东西」，
     两处打架；②`items.css` 8 处 `color-mix` 补上字面量兜底（行 38/76/432/469/480/509/543/715）
     —— 文件头自己定的约定是「rgba 在前当老内核降级、主题感知的 `color-mix` 在后」，
     这 8 处只有 `color-mix`，老内核上整条声明被丢弃（背景/边框变透明），现在全文件
     13 条 `color-mix` 声明 **100% 有兜底**（有脚本可复查）；③`icons.tsx` 注释里的鱼形
     emoji 换成文字 —— dist 产物本来就 0 emoji，但源码层留着会让「零 emoji」的扫描一直报警。
     （复审的 D4 是 `items.css` 里的**存量**硬编码颜色/px 字号，明确不混进本轮，另开一轮清。）

> **第十四批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_hard2`）
> `TOTAL 27  PASS 27  FAIL 0  CASES 821  SKIPPED 2`（第十三批 817；新增 4 条：
> `test_item_stats_api.py` 39→41 的两条限流用例、`test_xianyu_items.py` 83→84 的多层信封、
> `test_services_items.py` 145→146 的同前缀新码）。
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（57 modules，
> `dist/assets/index-kmUrJvdH.js` 321.20 kB / `index-iTNo5yli.css` 99.44 kB）。
> `scripts/ui_smoke.py --port 18099` **9/9 页**、`scripts/live_refresh_smoke.py --port 18127`
> **2/2 阶段**。真机数据接口探测（只打 1 次 mtop）：商品 `1087258469593` 回
> `exposure=367 / visits=24 / wants=1` —— 第十一批那个信封修复在**真实平台**上成立，
> 用户截图里那句「没有可识别的商品数据指标」已不复现。
> 两个独立审查员的结论与全部证据（含他们真跑出来的数字）见会话记录；他们**没有**改过仓库
> 任何文件（`git status` 在他们跑完后仍与审查前一致）。

104. **商品数据接口「点一下打一次平台」是设计错误**【用户第七轮原话：「我觉得商品数据应该让他
     自己慢慢拉取，而不是说我点过来的时候他再拉取」】。这个接口族**只能按商品逐个查**
     （`item_stats.PLATFORM_ITEM_LIMIT = 10` 只是把一次点击的上限压到 10 次 mtop），
     用户每进一次「商品数据」页就是 10 次 mtop —— 真机上直接把账号打到风控
     （`RGV587_ERROR::SM::哎哟喂,被挤爆啦`）。修：新增 `services/stats_worker.py` +
     `main.py::_stats_pull_loop`，**后台慢慢拉**（默认每 300s 拉 5 件、间隔 3s、启动后先等 60s，
     四个值都能用 `XYWB_STATS_*` 环境变量覆盖），拉到的写 `item_stats_cache`；
     前端「商品数据」页默认走新的 `?source=cache`（**一个平台请求都不发**），
     只读缓存 + `GET /api/items/{account_id}/stats/progress` 显示「后台已缓存 N / M 件」。
     想看此刻的值仍然可以显式点「平台」。**缓存行的诚实口径**：拿不到的指标存 NULL（不许存 0）、
     失败行存 `source='error'` + `error` 原文且**不会盖掉上一轮的成功数据**。
     真浏览器探针（`C:\Users\Administrator\.dsh-tmp\stats_cache_probe.py`，临时库 + 真 Edge）：
     打开页面抓到的 stats 请求**恰好两条** —— `?source=cache` 与 `/stats/progress`，
     `platform`/`auto` **0 次**；再显式点「平台」时**真的**发出 `?source=platform`（反向验证）。
105. **「主管 AI」的对话以前根本没存**【用户第七轮：「ai 的会话记录呢」】。页面自己写着
     「对话只保存在本页，刷新即丢失」，而 `agent_runs` 里的调用记录界面上又看不到。
     修：`agents/history.py` + `agent_chat_messages` 表 + `GET|DELETE /api/agents/chat/messages`，
     前端进页面先读历史（`before_id` 向上翻页）、显示模型/耗时/token，**失败那一轮也显示**
     （`status='error'` + `error` 原文 + 「重发这条」），「清空对话」改成真的 `DELETE`。
     真浏览器探针（`C:\Users\Administrator\.dsh-tmp\master_history_probe.py`）：预置 4 行历史
     → 页面渲染 4 个气泡、失败行显示 `LLMError: 上游返回 500（provider_id=1）；这一轮没有改动
     任何商品`、meta 显示 `deepseek-chat · 1.2s · 321 tokens`；点「清空对话」→ 抓到
     `DELETE /api/agents/chat/messages` 且**库里剩余 0 行**。
106. **入站放开未知 contentType 后，「系统/卡片帧」会触发 AI 自动回复**（协议层交付时提出的
     待决策项）。对商品卡开口等于自言自语，还白烧一次大模型。修：`services/chats.py` 新增
     `SYSTEM_MSG_TYPES = ('system','card')`，命中就回 `code='system_message'`
     「这条不是买家说的话（系统/卡片消息），AI 不自动回复」——**只拦自动回复、不拦落库**
     （落库正是「不再显示 (空消息)」的前提）。回归用例断言假 runtime 一被调用就失败。
107. **语音时长有两套换算，差 1000 倍**（协议层 `_voice_summary` 当秒、服务层
     `AUDIO_MS_THRESHOLD=300` 当毫秒；值落 301~600 时一个说 4.2 秒、一个说 0.4 秒）。
     修：只留一份实现 `xianyu/im.py::audio_seconds()`，服务层**委托**它（取不到才用本地兜底），
     并有测试锁住「两边算出来必须一样」。
108. **打包版点「手动验证」必然失败**（真机 `operation_logs` id=165~171 连续 7 条
     `[WinError 2] 系统找不到指定的文件`）。根因：playwright 的 driver 是 `node.exe` +
     `package/cli.js`，属**数据文件**，`desktop.spec` 只收了 `hiddenimports`（模块），
     漏了 `datas`。修：`collect_data_files('playwright'/'patchright')`，并给
     `captcha.py` 加 `_driver_problem()` 前置检查 —— 缺驱动时**直接给一句人话**
     （「内置浏览器驱动不完整…这是打包时漏收数据文件导致的」），不再抛到
     `async with` 入口外面。代价：driver 约 100 MB，产物从 ~9.6 MB 涨到 ~110 MB
     （浏览器内核仍在 `%LOCALAPPDATA%\ms-playwright`，不重复打包）。
109. **平台错误码的人话解释集中到一个模块**（`services/platform_errors.py`）。发布路径过去把
     `协议层调用失败: FAIL_BIZ_IDLE_UNKNOWN_THROWABLE::发布失败，请稍后重试吧~` 原样甩给用户，
     用户唯一能做的就是**反复点**（越点越重风控）。现在按**词边界**匹配真机见过的码，
     在 `message` 后追加一句可行动的中文（例：售价 99999999/库存 19999999 → 「先把售价、库存
     改回正常范围，隔几分钟再试一次」），并补 `platform_code`/`platform_hint`（风控再加
     `risk: true`）。**没见过的码原样透传、不编解释**；**绝不改 `code`**（它决定 HTTP 状态码）。
110. **平台调用一直在吃本机环境代理**（2026-10-08 打包版真机抓到）。本机 env 有
     `HTTPS_PROXY=http://127.0.0.1:7897`，该代理对 `h5api.m.goofish.com` 握手即断
     （`ConnectError: EOF occurred in violation of protocol (_ssl.c:1007)`），而**直连同一个
     域名是通的**（`GET /` 404）。`net.async_client()` 只写了 `proxy=None` —— 这**单独用不够**：
     httpx 的 `_get_proxy_map(proxy, allow_env_proxies)` 在 `proxy is None` 且 `trust_env=True`
     （默认）时仍然 `get_environment_proxies()`。于是模块 docstring 里「我们不使用系统代理」
     一直不是真的；`agents/llm.py` 早就因同类问题（审计 AI-7）关掉了 `trust_env`，平台这条
     路径漏了。修：`async_client()` 加 `trust_env=False`；`_net_problem()` 在
     `httpx.TransportError` 且本机设了代理时补一句人话（列出变量名与值 + 该怎么办）。
111. **后台拉取在系统性失败上不收手**（同一批真机日志）。两个账号每 300s 一轮、每账号 5 件
     **全部**失败（`item_stats_cache` 5 行 `source='error'`），而报告里 `stopped` 还是 `None`，
     于是每 5 分钟重复 10 次注定失败的 mtop，把 `risk_state.consecutive_hits` 从 1 推到 2 ——
     **为了拉数据反而把账号往风控里推**。修：新增 `STOP_UNAVAILABLE`，`session` 与
     `unavailable` 都 `break`（原来 session 只记 `stopped` 却继续拉剩下 4 件）；失败件照旧
     落一行 error，否则下一轮第一个挑中的还是它。修后真机同一轮变成
     `{'2217970177348': {'pulled': 0, 'failed': 1, 'stopped': 'unavailable'}, …}`（10 次 mtop → 2 次）。
112. **网络异常没有类名，排障等于没说**。httpx 的 `ReadError`/`ConnectError` 只要不传 message，
     `str()` 就是空的，于是 `item_stats_cache.error` 里全是 `接口不可用: MTOP 请求异常: `，
     连接失败 / 超时 / TLS 错误长得一模一样。修：`client._net_problem()` 在空消息时退回类名
     （真机因此一眼看出是 `ConnectError`，直接指向第 110 条）。

> **第十五批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_r7_net_all`）
> `TOTAL 30  PASS 30  FAIL 0  CASES 934  SKIPPED 2`（第十四批 821；本轮新增
> `test_stats_worker.py` / `test_agent_history.py` / `test_platform_errors.py` 三个文件
> 与 chats / im / login / items 的 12 条回归用例）。前端 `npx tsc --noEmit` EXIT=0、
> `npm run build` EXIT=0
> （59 modules，`index-BtOoYIBj.js` 331.97 kB / `index-RKsxVLgj.css` 103.56 kB）。
> 两个真浏览器只读探针（临时数据目录 + 真 Edge，不碰真平台/真 LLM）：
> 商品数据缓存优先 **OK**、主管 AI 历史 **OK**（命令与输出见 104/105 两条）。

### 第十六批：用户第九次真机实测「删除的账号数据怎么还在？」（113）

113. **删号只清了账号/风控/会话三组表，商品与日志留在库里**（2026-10-09 真机）。
     用户原话：「删除的账号数据怎么还在？」截图里商品页「全部账号」下 `共 95 条`，
     多行已售出商品的账号后缀是 `3998190171`。只读盘点真库（`mode=ro`）：`accounts`
     只剩 2 行，而 `items.account_id='3998190171'` 有 **39 行**、`operation_logs` 15 行
     （该账号的会话/风控/凭证早就被清了）。根因：`xianyu/store.py::delete_account()` 只删
     `accounts`/`account_credentials`/`risk_state`/`risk_events`，
     `services/chats.py::purge_account_data()` 只删 `conversations`/`messages`，
     `items`/`item_stats_cache`/`operation_logs`/`orders`/`shipments`/账号级 `ai_prompts`
     没有任何人清理，于是 `account_id` 指向一个不存在的账号。修：新增
     `backend/app/services/purge.py`（`purge_account_data()` 幂等；`orphan_account_ids()`
     只按「`account_id` 不在 `accounts` 表里」判断，不猜时间/前缀；`purge_orphans()` 供启动
     自愈；`shipments` 挂在 `orders.order_id` 上而不是 `account_id`，必须先按订单号删），
     `api/accounts.py` 的 DELETE 增加 `data_purged`，并在确实删掉商品行时广播
     `items.updated`（`action:'account_deleted'`，开着的商品页静默重拉）；
     `main.lifespan` 在风控回灌之后调 `purge_orphans()`。
     **重启一次应用就会把真机上那 39 件商品 + 15 行日志清掉**（启动自愈，无需手点）。
     口径写进 `docs/CONTRACTS.md` §3：删号 = 删掉这台机器上属于它的所有本地行，唯一例外是
     `agent_chat_messages`（主管 AI 的对话，不属于任何账号）。

> **第十六批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_purge_all`）
> `TOTAL 31  PASS 31  FAIL 0  CASES 941  SKIPPED 2`（第十五批 934；新增
> `tests/test_services_purge.py` 6 条 + `tests/test_accounts_im.py` 1 条 API 级用例）。

### 第十七批：用户第十次真机实测（官方卖家工作台「财务」四页截图）（114~116）

用户这次**没有文字**，只发了 4 张官方 `seller.goofish.com` 的财务页截图（收入账单
`收入金额合计 ¥40.11 / 流水笔数 4 / 202609 / 业务大类 交易货款`、支出账单
`业务大类 同城服务软件服务费 / 流水笔数 0 / 暂无数据`、申请发票 `待申请发票 2 /
软件服务费 0.40元 / 基础软件服务费 0.24元`、开票信息 `发票抬头 万春华 / 纳税类型 个人`）。
按原始诉求⑨「集成闲鱼客服与网页版功能」解读为「把这四页做进工作台」，仓库里
`grep 账单|发票|财务|invoice` 只命中三处文案（FAQ/提示词/测试语料），**财务功能从零做**。

114. **财务接口族必须靠真浏览器发现**（2026-10-09）。`web_search` 在本机没有 key，
    只能自己发现。用库里真账号 Cookie（`2217970177348`，489 字符）打开
    `https://seller.goofish.com/` 是**通的**（落地 `#/seller-data/data`，标题「数据总览」，
    正文含真实数字），于是真 Chrome headless 注入 cookie 抓 mtop。**踩了 5 个坑**：
    ①猜 hash 路由 `#/finance/income-bill` 一律被 SPA 重定向回 `#/seller-data/data`；
    ②`get_by_text('财务', exact=True).click()` 超时；③JS `closest('a,li,div[class*=menu]')`
    点了父级 wrap，React handler 挂在元素自己身上，URL 不变、零 mtop；④
    `locator('div[class^="subMenuItem"]').click(force=True)` 点击成功但不换页；⑤真实鼠标
    事件（坐标 `{"收入账单":{x:85,y:575}}` 正确、`visible:true`）仍不换页。
    **正解**：路由名只能从菜单接口拿 —— `mtop.alibaba.idle.seller.platform.sys.menu.query`
    的 `data.data[0].accessiblePathList` 里写着 `/seller-finance/income-bill`、
    `/seller-finance/expense-bill`、`/seller-finance/invoice-apply`、`/seller-finance/basic-info`，
    直接 goto 这些 hash 路由就能拿到各页的 mtop 调用与响应体。

115. **财务族不受「卖家工作台会话」限制（决定性可行性判定）**。`client.py` 里记着
    `seller.pc.*`（如 `mtop.alibaba.idle.seller.pc.item.stats.query`）要卖家工作台会话，
    但用**项目自己的客户端**（`XianyuClient(cookie)` + `mtop(api, data, v='1.0')`）对
    财务族打 10 次只读调用 → **`failures=0/10`，全部 `SUCCESS::调用成功`**。所以这族接口
    回 `FAIL_SYS_SESSION_EXPIRED` 就是账号**真掉线**，服务层必须如实 409 +
    `need_relogin`，**不能**像 `item_stats` 那样本地降级（会把掉线瞒过去）。
    协议层真名：账单族 `mtop.idle.finance.metadata.query` /
    `mtop.idle.finance.monthbill.{income,expense}.{summary,detail}.query`；
    发票族 `mtop.idle.finance.invoice.{metadata.get,bill.query,request.list.query}` +
    `mtop.idle.finance.invoice.purchaser.info.get`。参数形状三处怪癖：账单族包在
    `{'request': {...}}`、发票族包在 `{'mtopRequest': ...}`，其中 `invoice.bill.query` 要
    **字符串化 JSON**、`invoice.request.list.query` 要**对象**。

116. **「不传 bizType 就取全部」是错的**（真机 502 抓到）。第一次真机探针
    （`finance_service_probe.py`）不传 `bizType` → `协议层调用失败: MTOP 调用失败
    [mtop.idle.finance.monthbill.income.summary.query]: INVALID_PARAM::业务大类不能为空`。
    修：默认值从**平台自己的 metadata** 取第一级大类（收入账单 `categories[0].code` =
    `goods_income`，名字「交易货款」），支出账单兜底到官方默认选中的
    `ILDE_LOCAL_SERVICE_TECH_FEE_STD_PROCESS`（同城服务软件服务费），都没有才 502
    并明说「取不到分类列表」。**不要自己写死大类编码。**
    另：探针脚本 `finance_biztype_probe.py` 把 metadata 的整棵类目树（`goods_income`
    下挂着上百个**商品类目** `126866937`…）逐个大类打真接口，**把账号打到风控**
    （`risk_state.consecutive_hits=1`、`blocked_until` 约 2 分钟后、`login_state` 仍 active）。
    ⇒ **绝不要对真平台遍历类目/商品列表**，探针一次最多 3~4 次调用并留间隔。
    改成只 3 次调用的 `finance_min_probe.py` 后拿到与用户截图**逐字对上**的真值：
    `biz_type='goods_income'`、`bill_name='订单收入'`、`income 40.11 / net_income 40.11 /
    agg_counts 4`、`total=1`、`has_next=True`、`bills[0].bill_no='2026090000664051081999891099'`
    `biz_name='交易货款'`、`warnings=[]`。

117. **默认账期是本月 ⇒ 首次打开财务页必 502**（打包版真机取证 `finance_shots.py` 抓到）。
    收入/支出两个 tab 默认 `from=202610&to=202610`，平台当月账单还没生成，回
    `BILL_QUERY_DELAY_ERROR::暂无法查看2026年10月的账单，请在2026-11-05后查看`
    （income 与 expense 各一条 502）。**用户自己截图的官方页面用的就是上个月的
    `202609`。** 修两处：①服务层 `_cycle_pair` 与前端 `latestBillMonth()` 的默认值都改成
    **上一个自然月**（`(datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y%m')`
    / `new Date(y, m-1, 1)`），账期说明也写明「平台当月账单要次月 5 号后才有」；
    ②`services/platform_errors.py` 收录 `BILL_QUERY_DELAY_ERROR`（真机见过的码），
    财务服务层 `_call` 的失败原因过一遍 `platform_errors.explain()`，于是 502 的
    `message` 会带「改成上一个月再查」。回归：`test_default_cycle_is_previous_month`、
    `test_bill_delay_error_is_explained_in_plain_words`、
    `test_bill_query_delay_says_when_the_bill_will_exist`。
    同时刻真机拿到的正确数据（与用户截图逐字对上）：申请发票 = 软件服务费 `0.40` +
    基础软件服务费 `0.24`（杭州闲鱼信息技术有限公司、税率 0.06、状态 VALID）、
    `开票金额合计 0.64`、`共 2 条`；开票信息 = 发票抬头 `万春华` / 纳税类型 `个人` /
    支持票种 `数字化电子普票`；`fetched_at` 显示为本地时间（UTC 口径正确）。

> **第十七批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_fin_fix2`）
> `TOTAL 33  PASS 33  FAIL 0  CASES 999  SKIPPED 2`（第十六批 941；新增
> `tests/test_xianyu_finance.py` 28 条协议层用例 + `tests/test_finance_api.py` 29 条
> 服务层/HTTP 用例 + `tests/test_platform_errors.py` 25 条里的 `BILL_QUERY_DELAY_ERROR`
> 一条）。前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0
> （61 modules，`index-DlUnL6LY.js 343.86 kB` + `index-B6VwuY8J.css 105.82 kB`）；
> `scripts/ui_smoke.py --port 18097` → **10/10 页通过**（新增财务页，`PAGES` 已补）。

### 第十八批：用户第十一次真机实测（官方「超级擦亮」+「安全中心」截图）（118~121）

用户这次同样**没有文字**，只发了 12 张截图：超级擦亮（推广）首页 / 擦亮计划表 / 充值页 /
账户报表 / 计划报表 / 商品报表 / 账户明细 / 活动奖励 / 资质管理 / 新建擦亮计划（两步）+
安全中心（闲气值 100 分 + 3 条「发布违禁商品信息」违规、扣 0 分、处罚完结）。
按原始诉求⑨「集成闲鱼客服与网页版功能」解读为「把这两块**只读**部分做进工作台」，
**充值 / 提现 / 申诉 / 上传资质 / 新建或修改擦亮计划一律不做**（写平台、涉钱、不可逆）。

118. **别在真机上点微应用，直接下载 bundle 读接口名**（2026-10-09）。菜单接口
    `mtop.alibaba.idle.seller.platform.sys.menu.query` 的响应里每个一级菜单都带
    `entry`（微应用 bundle 的 alicdn 地址）+ `hashType: true` + `version`，于是
    `https://g.alicdn.com/idle-pc/fish-ad-pc/0.0.12/js/main.js`（3,760,912 字节）与
    `https://g.alicdn.com/idle-pc/idle-seller-security-center/0.0.6/js/main.js`
    （4,441,635 字节）可以直接下下来静态扫 `mtop.` 字面量：超级擦亮 **86 个**、
    安全中心 **34 个**、营销 28 个。**这条路一个平台请求都不发，完全不碰风控**，
    比在 headless 里点菜单可靠得多。真路由同样来自菜单接口的 `accessiblePathList`：
    `/seller-ad/home`、`/seller-sc/home`（另 38 条官方页面清单见该响应）。
    **踩坑**：①微应用顶栏导航在 headless 下没渲染，「擦亮/账户/报表」全部
    `TimeoutError`；②hash-only goto 不会重载页面，7 个猜的路由 goto 后 URL 原样保留
    ——**不能据此判断路由存在**；③PowerShell 内联 python 带 `\"` 会
    `ParserError: 字符串缺少终止符`，含引号的脚本一律写成 `.py` 文件再跑；
    ④`Get-ChildItem -Filter a,b` 报参数类型错（`-Filter` 只收单值）。

119. **安全中心不是没权限，是缺站点 CODE 头**（这是本轮最值钱的一条）。
    同一账号 `2217970177348` 的 Cookie：`ad.home`、`finance.summary` 都是
    `SUCCESS`，唯独 `idle.pc.security.score.get` 回
    `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。差点误判成「账号掉线 / 安全中心没资格」。
    真因：官方微应用把工作台 URL 上的 `?site=COMMONPRO` 放进**请求头**
    `idle_site_biz_code`（另有 `idle_user_group_member_id: ''`）并用
    `origin/referer=https://seller.goofish.com`。取证方式是真 Chrome headless +
    `captcha.to_playwright_cookies(cookie)` 打开 `https://seller.goofish.com/?site=COMMONPRO#/seller-sc/home`
    抓请求头（落 `logs/seller_discover/site_code.json`）。裸调时平台回的
    `data.errorMsg` 是「**站点CODE不能为空**」，带空串头是「用户无权限访问当前站点[]」——
    两句话都指向头，不指向账号。修法：`client.py` 新增 `SECURITY_SITE_BIZ_CODE='COMMONPRO'`、
    `XianyuClient._security_headers()`，并给 `mtop()` 加通用逃生口
    `extra_headers=None`（`if extra_headers: headers.update(extra_headers)`）。
    **带上头之后 score.get / score.record.list / violation.list 全部 SUCCESS**
    （闲气值 `totalScore:100`、违规 `punishId:187988498827`、`action.actionUrl` 指向官方申诉表单）。
    回归用例 `test_site_code_permission_error_is_not_reported_as_logout` 专门钉住
    「这个错必须落 502 `protocol_error`，**不能**落 409 `verification_required`」——
    否则用户会被反复叫去重新扫码，而问题一直在客户端缺头。
    （`window.__PC_SELLER_WORKBENCH__` 在这个页面上是 `undefined`，站点 CODE 只能从请求头取。）

120. **推广只读接口的真名、参数与单位**。协议层真名（`mtop.taobao.idle.ad.*`）：
    `home`（`data:{}`）、`bp.account.card.query`（`{scene:'PC_HOME'}`）、
    `member.data.query`、`bp.account.log.query`、`bp.account.recharge.query`、
    `bp.account.withdraw.query`、`bp.settle.settle.query`、`bp.coupon.info/statistic.query`、
    `campaign.search`、`campaign.data.batch.query`、`campaign.log.query`、`group.list`、
    `group.data.query`、`item.data.batch.query`、`item.recommend`、`package.info`、
    `message.list`、`qualification.*`；**写接口一个都不接**（`campaign.edit/delete/pause/
    startV2/batch.delete`、`item.campaign.renew`、`activity.apply`、`qualification.packages.submit`、
    `bp.account.log.operates`、`message.read`）。除 `home` 外全部包成
    `{'requestJson': json.dumps(body)}`，报表族的 body 是
    `{startDate,endDate,pageNum,pageSize,includeEndDate:true,allPeriod:false}`
    （`campaign.search` 另有可选 `titleLike`）。
    **单位是两种，必须分清**：`idle.ad.*` 指标里的 `cost`/`realCost`/`payAmount`/
    `costPerClick`/`cpm` 是**分**（真机 `cost:5990` 与官方计划报表的 `59.9` 对得上，
    换算只在前端 `cents()` 做，服务层原样透传）；`settle` 与 `bp.account.card.query`
    的金额是**元字符串**（`'169.27'`、`'33.58'`）；`showPv`/`clickPv` 是次数，
    `ctr`/`cvr` 平台已经给了百分数。真机形状（`logs/promotion_probe/readonly_shapes.json`）：
    `campaign.search` → `{result[],total:9,nextPage}`（行含 `campaignId/title/
    campaignTypeName/statusDescCode/startTime/endTime/adGroupCount/entityList[]/adDataDetailVO`）；
    `campaign.data.batch.query` → `result[].idleAdGroupDataVO.data`（total 6）；
    `item.data.batch.query` → `result[{itemId,title,imgUrl,price,adData,previousData}]`（total 26）。

121. **两页都做成只读镜像，并且失败必须如实**。服务层 `services/promotion.py` 4 个视图
    （`overview`/`campaigns`/`reports`/`account`）+ `services/security_center.py`；
    路由 `GET /api/promotion/{account_id}/{overview,campaigns,reports,account}` 与
    `GET /api/security-center/{account_id}`，`POST/PUT/PATCH/DELETE` 一律 405。
    日期只认 `YYYYMMDD`（都空 = 最近 7 天，格式错或 from>to → 400 `invalid_fields`）、
    `size` 限 1~50、安全中心 `page/size` 走 FastAPI `ge/le`（越界 422）。
    没有 Cookie / `need_relogin` 时**一次平台都不打**，直接 409 `verification_required`；
    平台报错 → 502 `protocol_error`，**绝不把拿不到画成 0**。两页都**不落库、不缓存**
    （余额与投放数据过期比没有更糟）。前端 `pages/Promotion.tsx` 四页签（概览/擦亮计划/
    数据报表/账户，按页签懒加载）+ `pages/SecurityCenter.tsx`（闲气值卡片 + 违规表，
    删掉了原来直接倾倒 JSON 的 `<pre>`），新样式各自进 `promotion.css`/`security-center.css`
    （不动 `styles.css` 的既有 class 与令牌名）。

122. **打包版复验抓出两个「字段映射错」的真机 bug**（`0be41d4` 之后、同一轮修完，`822b206` + `21625b1`）。
    静态扫 bundle 能拿到接口名，但**响应字段的嵌套层级只有真机响应说了算**：
    1. 安全中心「违规原因」恒为 `-`。顶层 `punishReason` 真机是 **null**，PC 文案在
       `viewConfig.viewConfig.punishReasonFromPc`（"发布的商品内容中含有违禁信息"）。
       第一次改错了层级（写成 `viewConfig.contentType.*`，那里是空的），
       最后靠**逐路径扫真机响应**才定位：
       `body/data/violations/data[i]/viewConfig/viewConfig/{punishTitleFromPc,punishReasonFromPc,
       punishResultFromPc,operationSuggestFromPc}`。教训：嵌套字段别靠缩进猜，把响应按路径打出来。
    2. 页脚「数据更新于」两页都直接打印 `fetched_at`（UTC 串，真机显示 `2026-10-09 21:48:39`，
       本地是 `2026-10-10 05:48`）→ 改走 `formatTime()`；并且 `security_center.read()`
       根本没返回 `fetched_at`，页脚显示 `-` → 补上（与推广侧一致，回归用例
       `test_payload_carries_fetched_at` 钉住）。
    3. 顺带修掉两处噪音/缺失：「状态 1」是无意义数字，换成官方申诉入口按钮
       （`viewConfig.appealConfig.appealUrl`，`canAppeal:false` 时是空串，如申诉期已过；
       拿不到再退 `action.actionTypes[0].linkUrl`），只 `window.open`，工作台**不提交**申诉；
       原因单元格挂 `operationSuggestFromPc` 作 title 提示。
    打包版真机复验（frozen exe `--port 18400`）：违规原因三行都有真文案、前两行有「去申诉」、
    第三行（申诉期已过）为 `-`、页脚 `2026-10-10 06:41:48`（本地时间）、
    `page_errors: []` / `http_errors: []`、只读请求仍是 5 条。

> **第十八批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_promo_full3`）
> `TOTAL 35  PASS 35  FAIL 0  CASES 1027  SKIPPED 2`（第十七批 999；新增
> `tests/test_promotion_api.py` 24 条 + `tests/test_xianyu_promotion.py` 4 条协议层用例），
> 补 `test_payload_carries_fetched_at` 后 **CASES 1028**（`%TEMP%\xywb_sec_ft2`）。
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（65 modules，
> `index-DMcFX2PI.js 356.99 kB` + `index-DVjUX-r9.css 108.24 kB`）；修完两个显示 bug 后
> `index-8P2tsRqB.js 357.89 kB`；
> `scripts/ui_smoke.py --port 18117` → **12/12 页通过**（新增 `promotion`、`security-center`），
> 修复后 `--port 18137` 仍 **12/12**；
> 打包版 `dist\闲鱼AI工作台\闲鱼AI工作台.exe` **9,835,514 字节 / 2026-10-10 06:38:58**
> （修复前 9,835,435 / 05:43:10），产物目录 190.3 MB；`logs/promo_shots/` 是打包版截图与
> 请求清单（`page_errors: []` / `http_errors: []`）。
> 真机五路由（真数据目录 + `POST /api/auth/desktop`）**全部 HTTP 200**：
> overview（余额 3358 分 / 暂停 3 + 系统暂停 6）、campaigns total 9、
> reports plans 6 + items 26、account settle 169.27、security-center 闲气值 100 + 违规 3 条；
> 证据 `logs/promotion_probe/report.json`、`logs/seller_discover/site_code.json`。

### 第十九批：用户要求「把整个闲鱼工作台复刻下来」（122~128）

用户原话（`m11617`）：「你就按照咸鱼上面的复刻就行了 充值之类的更改接口也可以碰 顺便留个接口给AI调用的，
我以后打算用AI管理 把整个闲鱼的工作台给他复刻下来 我后面懒得一个一个发截图了 以及你感觉有哪些不足的
地方也可以进行优化调优，然后修复之类的自己检查有没有bug 不要做完的就给我自己多检查检查 还有自己看看
有些地方符不符合人类的使用习惯之的进行优化 还有UI不要跟我写很丑，要稍微写好看点」

122. **官方微应用是「静态可读」的 —— 这条改变了整个复刻方式**。官方工作台每个一级菜单在
     `mtop.alibaba.idle.seller.platform.sys.menu.query` 的响应里都带 `entry`（微应用 bundle 的
     alicdn 地址）+ `hashType:true`。把 bundle 拉下来 grep `mtop\.` 就能拿到**全部接口名**，
     一个平台请求都不发（因此完全不碰风控）。本批据此挖出交易 / 小铺 / 通知 / 数据 / 运费模版 /
     营销 / 资质 / 超强擦亮 / 安全中心 的只读接口清单。
     **教训**：先在真机点菜单试图抓包是错的方向 —— headless 下 SPA 菜单用 locator / JS /
     真实鼠标三种点法都**点了不换页**（坐标与 `visible` 都对），而路由名只能从菜单接口拿、不能猜
     （真前缀是 `/seller-finance/`、`/seller-ad/`、`/seller-sc/`，猜的 `/finance/` 一律被重定向回
     `#/seller-data/data`）。

123. **交易只读 5 端点**（`backend/app/xianyu/trade.py` + `services/trade.py` + `api/trade.py`，
     26 用例）：订单 / 退款 / 评价 / 投诉 / 退货地址。真机抓到的形状差异都写进了契约：
     `order.count` 的计数在 `data.module.countInfoList[]`（`queryCode` = ALL/TRADE_SUCCESS/
     TRADE_CLOSED/NOT_SHIP）；`refund.list` 是**双层** `data.data.items[]`，退款总额在
     `data.data.ext.totalRefundFee`；`rate.list` 的评价在 `rateItemVOList`；金额一律是**字符串**
     `"39.90"`。分页键 `totalCount`/`nextPage`/`lastEndRow`/`serverTime` 在 `data` 的**兄弟层**。
     投诉与退货地址接口真机回 `NO_PERMISSION::用户没有权限` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`
     —— **如实透出，不当成 0 条、也不标掉线**。协议层 `assert_read_only()` + 19 个写接口黑名单。

124. **通知中心根本没有列表接口**（`docs/contracts/notify.md`）。官方只给了频道树
     `pc.notify.sessions.list` + 未读 `pc.redpoint.query`，条目本身走 IM/ACCS 长连接。
     所以契约是 `channels` + `unread` + **`items: []` + warning**，而不是伪造一个空列表。
     另：抓包时误触通知标签，页面自己发出写接口 `mtop.taobao.idlemessage.pc.notifys.read`
     —— 代码从未调用它，已钉进 `WRITE_APIS`（**披露而不是假装没发生**）。

125. **数据中心：这个账号没有「数据」权限**。`mtop.alibaba.idle.seller.datacompass.*` 族
     （overview / fans / customer-service）真机一律 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`；
     参数形状经核对是对的（不给 `dateType` 会先报 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数dateType`，
     补上才变成无权限）。所以三个端点返回**指标 null + 人话 warning**
     「这个账号没有『数据』权限（不是 0）」，前端显示 `-`。**null ≠ 0：平台真给 0 时要保留 0。**

126. **`post-temple` 是运费模版，不是「发布模板」**（纠偏）。官方 bundle 里中文计数
     「模版」= 49、「模板」= 0、「发布模板」= 0 —— 真正的「模板」是草稿箱
     `#/publish/success?draftId=…&scene=draft`。契约与页面都按运费模版写。
     真机该账号运费模版数 0（`{"data":[],"totalCount":0}`）。

127. **12 个新页面 + 导航接线**（订单 / 退款 / 评价 / 投诉 / 退货地址 / 子账号 / 客服分流 /
     数据总览 / 粉丝数据 / 客服数据 / 运费模版 / 通知中心）。**这里又踩了同一类「半接线」bug**：
     `frontend/src/App.tsx` 的 `readHash()` 是 `PAGE_TITLES[raw] ? raw : 'overview'`，而
     `PAGE_TITLES` 由 `NAV` 派生 —— `item-templates` 没进 NAV，于是 `#/item-templates`
     **静默回落到概览页**、侧栏也没有入口（渲染分支白写了）。教训：**「渲染分支写了、NAV 没写」
     是半接线**，冒烟测试必须比对「NAV key ⊆ 渲染分支」。现在 `App.tsx` 的 NAV / 渲染分支 /
     `scripts/ui_smoke.py` 的 PAGES 三方一致（26 页）。

128. **`_data` 只放行 dict，把平台的裸数组吞成了 None**（真 bug，服务层自查抓到）。
     `platform.user.role.list` 真机返回的是裸数组 `[{roleId,roleName,selected}]`，旧 `_data`
     只 `isinstance(data, dict)` 才认，于是数组被丢掉**而且不给 warning** —— 页面显示「没有角色」
     和「拿不到」看起来一模一样。改成 `isinstance(data, (dict, list))`，并给「缺键的列表」补
     中文 warning（真机给空数组的仍然给 `[]`）。同批还修了 `quota` 缺业务参数
     `BIZ_TYPE='COMMONPRO'`、协议函数缺失（半升级）走 `CALL_MISSING` → **503**。

> **第十九批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_r2_audit` 起，
> 最终 `%TEMP%\xywb_r2_full2`）→ `TOTAL 40  PASS 40  FAIL 0  CASES 1162  SKIPPED 2`；
> 新增 `tests/test_trade_api.py` 26 条、`test_shop_api.py` 30 条、`test_notify_api.py` 17 条、
> `test_datacenter_api.py` 41 条、`test_item_templates_api.py` 13 条。
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（82 modules，
> `index-BLqTHqIn.css 119.75 kB` + `index-Ctj8g4W_.js 433.96 kB`）；
> `scripts/ui_smoke.py` → **24/24 页**（12 页新增）；
> 提交 `29f08c7`（46 文件 / +11119 行）。
> 真机证据：`logs/seller_discover/menu.json`（38 条真路由 + 10 个微应用 entry）。

### 第二十批：写接口 + AI 可调用接口 + 营销/资质页（129~135）

129. **营销两个接口名漏了 `bpro.marketing.` 这一段**（真 bug）。原常量写成
     `mtop.taobao.idle.item.price.competitiveness.info.query` /
     `mtop.taobao.idle.operation.record.list.pc`；用官方营销 bundle 逐字 grep：
     长名 `mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query` 出现 **1 次**、
     短名 **0 次**（`operation.record.list.pc` 同理）⇒ 真机会直接回 `FAIL_SYS_API_NOT_FOUNDED`。
     原测试只按常量符号引用，所以没抓住。**修法**：改长名 + 新增
     `test_mtop_names_match_the_official_bundle` 逐字冻结四个只读名（防再漂）。
     证据落盘 `logs/seller_discover/marketing_src/`（`js_main.js` 3,516,103 字节 + `apis.json`）。

130. **22 个新只读工具只写在 `TOOL_SPECS` 里，MCP 侧永远看不到**（真 bug，fail-closed 测试抓到）。
     `mcp_server/client.py::TOOL_ROUTES` 没登记它们 ⇒ `tools/list` 不暴露。
     同批还修了一条**静默死测试**：`backend/tests/test_agents_tools_workbench.py` 缺
     `if __name__ == '__main__': unittest.main()` 入口，而 `run_all.py:58` 是
     `subprocess.run([sys.executable, path])` 逐文件当脚本跑 ⇒ 该文件 3 个测试类**从未执行过**，
     `run_all.py:76-79` 把「0 用例」记成 FAIL。**教训：任何 `test_*.py` 缺入口都会静默 0 用例。**

131. **写接口落地**（四个模块，全部带 `confirm` 闸门 + `WRITE_APIS` 黑名单 + 假协议层单测）：
     交易 8 个（发货 / 代发 / 改价 / 同意退款 / 拒绝退款 / 评价 / 投诉拒绝 / 投诉举证，33 用例）、
     推广 10 个（投放计划增删改停启 + 续投 + 活动报名 + 账户流水操作，21 用例）、
     资质 2 个（提交材料 / 取 OSS 凭证，20 用例）、安全中心申诉 4 个（取表单 / 提交 / CRO 提交 /
     催办，21 用例）。**关键发现**：交易微应用所有业务 mtop 都走本地封装、默认 `type:"POST"`
     —— 只读接口在**传输层同样是 POST**，「读/写」只能靠接口名语义 + 调用点行为判断。
     另一条：**「同意退货退款」很可能没有静态接口名**（UI 动作枚举有
     `agreeRefundGoodsApply`/`agreeRefundApply`/`sellerConfirm`，但 mtop 名不在 bundle，
     附近是通用分支 `hO({api:b.apiName, v:b.apiVersion, data:b.params})` ⇒ 疑似**服务端动态下发**）
     —— 无证据不猜名，本次**未实现**。

132. **充值 / 提现没有网页版下单接口**（诚实边界，不是没做完）。官方超级擦亮 bundle 的 86 个
     mtop 里，账户资金只有 4 个只读 query；`recharge` 的 92 处命中全是 CSS 类名/枚举；
     `payOrder`/`cashier` **0 命中**；官方提现按钮原文就是「请进入闲鱼App内操作」。
     所以 `GET /api/promotion/write/actions` 把 `recharge`/`withdraw` 放进 `unsupported` 并给出原因，
     **一次协议调用都不发**（用户说「充值之类的更改接口也可以碰」，但平台确实没给网页入口）。

133. **AI 可调用写接口 12 个**（`backend/app/agents/tools.py` + `mcp_server/client.py`）：
     11 个写（`trade_write_*` × 8、`promotion_write_execute`、`qualification_submit_packages`、
     `qualification_oss_credential`）+ 1 个只读 `promotion_write_actions`。硬约束由测试钉住：
     写工具 `required` 必含 `confirm`、MCP `method` 必为 `POST`、description 必含 `confirm=true`；
     **工具层不做确认判定、原样透传**（拦截来自服务层 `confirmation_required`/`confirm_required`，
     用「参数齐全但 `confirm=false`」的调用证明）。`MASTER_TOOLS` = `TOOL_SPECS` 去掉
     `master_chat`（防递归），最终 `TOOL_SPECS=58 / MASTER_TOOLS=57 / TOOL_ROUTES=59 / WRITE=12`。

134. **营销契约返工（参数形状订正）**。旧契约钉的是**猜错**的参数形态（`sellerId`/`pageNum`）
     与猜错的契约键（`item_count`/`running_count`…）。新证据：`itmId`（**官方确实少一个 e**）/
     `dateType`/`dateRange`、`bizScene`+`queryScene`+`pageNum`+`pageSize`+`itemIdListStr`（JSON 字符串）、
     `itemId`+`currentSkuPriceMapStr`、`taskId`+`queryScene`；`DATE_TYPES =
     ('recent1d','recent7d','recent30d','customDate')`、`CUSTOM_DATE_RANGE_SEPARATOR='|'`、
     `CUSTOM_DATE_MAX_BACK_DAYS=90`。证据口径改为**字符偏移**（bundle 是单行压缩 JS，行号无意义）。
     **诚实边界**：四个接口置信度**只有「中」** —— 响应字段是从压缩消费代码反推「页面怎么读」，
     不是响应样本「服务端怎么答」。前端 4 个面板已按新契约重做（`frontend/src/pages/Marketing.tsx`
     36703 B），必填缺失一律**一个请求都不发**、只给黄条。

135. **★ MCP 路由参数漂移（本批最后抓到的真 bug，影响 AI 与 MCP 两条路）**。
     FastAPI 对**未知 query 参数是静默忽略**的，所以路由声明写错键名不会报错、不会 4xx，
     只会让筛选条件失效、悄悄返回默认范围的数据。Lead 用独立对账脚本
     （`C:\Users\Administrator\.dsh-tmp\reconcile_lead.py`，把每条 MCP 路由声明的 query/body 键
     跟 `app.openapi()` 的**真实端点签名**对账）抓到三处：
     - `finance_income` / `finance_expense` 声明 `cycle_from`/`cycle_to`，而端点是
       `backend/app/api/finance.py:44,60` 的 `Query(alias='from'/'to')` ⇒ 实际只认 `from`/`to`。
       **发 `?cycle_from=…` 会被静默忽略，账期悄悄回落默认值 —— 结果错但不报错。**
       修法：`TOOL_ROUTES` 新增 `query_rename`（工具参数名 → HTTP 线上名）翻译表，
       `_dispatch_route` 按它组 query。注意**主管 AI 路径不受影响**（走 `_dispatch_service`
       直接调服务层 `finance.account_bills(..., cycle_from=…)`，签名就是 `cycle_from`）。
     - `accounts_list` 声明 `enabled`/`q`/`limit`，而 `backend/app/api/accounts.py:206`
       的 `list_accounts` **不接受任何 query** ⇒ 三个筛选条件同样被静默忽略。修法：给端点
       **补上这三个可选筛选参数**（不传时行为与以前逐字一致），并让主管 AI 的
       `_accounts_list` 用同一口径；这样 MCP / AI / HTTP 三条路对外宣称的能力才是真的。
     - 守卫：新增 `backend/tests/test_mcp_server.py::RouteParamAlignmentTest`（4 条）把这一类
       漂移变成**不可能再悄悄发生** —— 它遍历 `TOOL_ROUTES`，把声明的每个 query/body 键与真实
       端点签名比对（body 是自由 dict 时跳过），另加 1 条端到端用例断言财务账期真的发出
       `from`/`to` 而不是 `cycle_from`/`cycle_to`。
     另外 `frontend/src/App.tsx` 的「推广」组新增 `marketing`（营销）与 `qualification`（资质管理）
     两个导航项 + 两条渲染分支，`scripts/ui_smoke.py` 同步到 **26 页**。
     附：`confirm` 的口径本批定死 —— **只钉有副作用的那一步**。所以
     `POST /api/security-center/{account_id}/appeal/form`（语义只读）**故意不要 confirm**；
     给只读操作加确认只会训练用户无脑点确认、稀释真危险操作的信号。

> **第二十批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_r3_full_final`）
> → **`TOTAL 46  PASS 46  FAIL 0  CASES 1356  SKIPPED 2`**（第十九批 1162 → 第二十批 1343 → 修完
> 漂移 1356）；`backend/tests/test_mcp_server.py` → **Ran 60 tests ... OK**；
> `backend/tests/test_accounts_im.py` → **Ran 15 tests ... OK**（含新增的筛选参数用例）；
> 独立对账 `reconcile_lead.py` → `specs=58 master=57 routes=59 targets=38 handlers=58
> route_checked=59 ALIASES=22`、`RECON OK`；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（86 modules，
> `index-DCPlrc-0.js 481.76 kB` + `index-RSxYiLGu.css 127.22 kB`）；
> `scripts/ui_smoke.py --port 18211` → **26/26 页通过**；
> 提交：`3a59bb7`（营销只读）、`b5e8865`（交易写）、`b467b6b`（推广写）、`b025c25`（资质）、
> `f70e441`（22 个只读工具补 MCP 路由）、`6c89cfd`（主管 AI 接营销工具）、`6401563`（挂四个 router +
> 补只读层写黑名单）。
> 诚实边界：写接口的字段形状全部来自**官方 bundle 静态提取**，**一次真机写请求都没发过**
> （红线：不许改真实平台数据）；`trade_write` 的 8 个端点、`ad_write` 的 10 个动作、
> `qualification` 的 `request` 字段名、`security_write` 的 `parameter` 形状都还没被真机响应验证过。

### 第二十一批：运费模版写能力（136~139）

136. **官方 `seller-item` bundle 首次拿到，运费模版的写接口入参从「猜」变成「逐字有据」**。
      `https://g.alicdn.com/idle-pc/seller-item/0.0.20/` 的 `js/main.js` 是 4,402,065 字节单行压缩 JS
     （落盘 `C:\Users\Administrator\.dsh-tmp\adapp\item-main.js`，79 个 mtop 名）。四条写接口的
     请求体与调用点偏移（bundle 是单行，**行号无意义，只有字符偏移可复核**）：
     `create` `@2204883` 声明 / `@2211045` 调用 → `{templateName, itemProv, itemCity, valuation,
     isDefault, templateDetails}`；`modify` `@2204655` / `@2244766` → 同上 + `templateId` + `version`；
     `del` `@2203984` / `@4129523` → `{templateId}`；`unbind` `@4114059` → `{itemIds, supportFreight,
     onlyTakeSelf, needAutoFreight, isFreeShip, idleTemplateId?, postPriceInCent?}`。
     **`templateDetails` 是 JSON 字符串**，元素 `{isDefault, receiveDivisionName, firstPrice,
     excessPrice, first, excess, operate:'add'}`；**成功判据是平台业务码 `res.data.code === 'success'`**
     （失败取 `res.data.msg`）—— 不是 HTTP 200，也不看顶层 `code`。
     顺带发现我们**完全没有**的商品写接口族（本批未做，登记备查）：
     `mtop.alibaba.idle.seller.pc.item.{batch.delete,batch.offline,delete,offline,info.update,
     price.update,quantity.update}`、`...platform.item.operate.online`、草稿族 `draft.{get,search,
     update,delete}`。注意 `quantity.update`/`price.update` 在本 bundle 里**调用点 0**（死代码），不值得跟。

137. **AI 路径会静默丢掉所有业务字段**（真 bug，本批最值钱的一条）。四个写服务函数原来签名是
     `create_template(account_id, body=None, confirm=None)`，而 `agents/tools.py::_filter_args`
     **只把「目标函数签名里存在的键」传给服务层** ⇒ 走主管 AI / MCP 时 `template_name`、`item_ids`
     这些**全被丢弃**，只剩 `account_id` 与 `confirm`，表现为「AI 说它填了，服务层收到空」。
     **修法**：四个函数改成与 `trade_write.py` 同族的**扁平命名参数**
     （`create_template(account_id, template_name, item_prov, item_city, valuation, is_default,
     details, template_details, confirm)` 等），HTTP 层也改成逐字段 `body.get('x')` 再扁平传参。
     这是**第二次**踩同一族坑（第二十批是 `cycle_from`/`cycle_to` 的 query 漂移），已写进
     `docs/CONTRACTS.md` 的硬口径：**写函数的参数必须逐字段展开，不许收 `body: dict`**。

138. **运费模版写接口落地**（`backend/app/xianyu/item_templates.py` 重写 + 服务层写段 + 4 条 POST
     + 4 个 AI 工具 + MCP 路由 + 20 条新用例，`ItemTemplatesWriteTest` 让总数 13 → 33）。
     口径：`confirm` 只认 `True`/`'true'`/`1`；`_write_facade` **先判确认、再建客户端**（未确认
     零平台请求，测试里每个用例都断言 `fake.calls == []`）；`post_price` 单位**元**、协议层乘 100
     成 `postPriceInCent`；空的选填字段**不发**（官方是 `undefined` 而不是空串）；平台业务码不是
     `success`（或压根没有 `code`）→ **502 `protocol_error`**，绝不伪装成功。
     `unbind` 三分支：`free_ship` → `isFreeShip:true`；`template` → `isFreeShip:false,
     idleTemplateId:<id>, supportFreight:false`；`fixed` → `isFreeShip:false, postPriceInCent:100*元`。
     **三步顺序是「确认 → 字段校验 → 建客户端」**，而且是被打包版真机探针抓出来的：初版把
     「确认 + 建客户端」写成一个 `_write_facade`，于是 `confirm:true` + 空 body 在掉线账号上
     返回 **409 `verification_required`**（先去查凭据了），而交易/推广那批同场景返回 400
     `invalid_fields`。两个后果：①用户填错一个字要先被赶去扫码，扫完才被告知字段错；
     ②同一批写接口的报错顺序不一致。已拆成 `_confirm_gate(action, confirm)`（纯本地）
     + `_write_facade(account_id)`（建客户端），并加
     `test_field_validation_runs_before_credential_check` 把顺序钉死。

139. **运费模版页从只读改成可写**（`frontend/src/pages/ItemTemplates.tsx` 整体重写 + 新增
     `itemtemplates.css`）。唯一写出口是一个 `post(path, body)`，里面 `confirm: true` 是**字面量**
     （不在 UI 里拼），失败把后端 message 原样 toast 并留在面板里；四种危险动作都过
     `ConfirmDialog danger` 且文案写明「这会**真的**…」+ 本次实际提交的字段值；
     新建/编辑弹窗的运费明细表可增删行，本地校验（名称非空且 ≤30 字、valuation 必须是数字、
     `fixed` 模式金额必须 >0）不通过就 `disabled` 按钮。样式只用既有 CSS 令牌、不改
     `styles.css` 任何既有选择器；刻意避开 `color-mix()`（老 WebView2 不支持），选中态用
     `box-shadow: inset 0 0 0 1px var(--accent)`。

140. **打包版截图审出：侧栏副标题在谎称运费模版页「只读」**。`frontend/src/App.tsx` 的
     NAV `desc` 还是老文案「运费模版列表与明细（只读）」，而页面正文已经写着「可以新建 /
     修改 / 删除模版……每个写操作都会先弹一次确认」——标题栏与正文互相打脸。改准三条：
     运费模版 →「运费模版增删改与给商品设置运费（写操作要二次确认）」、订单 →「买家订单、
     发货、改价与退款（写操作要二次确认）」、安全中心 →「闲气值、违规记录与申诉入口
     （提交申诉要二次确认）」。只动三个 desc 字符串，不动 key（`PAGE_TITLES` 由 `NAV` 派生，
     key 不进 `NAV` 会被 `readHash()` 静默回落 overview）。

141. **打包版截图审出：表单刚打开就先骂用户**。新建模版弹窗一个字都没填，底下已经挂着
     黄条「模版名称不能为空」；「给商品设置运费」一展开就写「至少要填一个商品 ID」。
     改成只有用户真填过东西（dirty）才把问题说出来，按钮该灰还是灰：`formDirty`（编辑态恒
     true；新建态看名称/发货省/发货市/计价方式/设为默认/明细里的收货地区与首费续费；**刻意
     不看 `details` 的 `first`/`excess`**，它们默认就是 `'1'`，拿它们判断会永远 dirty）与
     `unbindDirty`（商品 ID/运费方式/模版/固定运费金额）。校验规则本身与 `disabled` 逻辑一字未动。

> **第二十一批证据链**：`backend/tests/test_item_templates_api.py` → **`Ran 34 tests ... OK`**
> （`XIANYU_APP_DIR=%TEMP%\xywb_tpl_order`；原 13 → 33 → 34）；`backend/tests/run_all.py`
> → **`TOTAL 46  PASS 46  FAIL 0  CASES 1377  SKIPPED 2`**（第二十批 1356 → 1376 → 1377；
> 140/141 落盘后再跑一次仍是 `TOTAL 46  PASS 46  FAIL 0  CASES 1377  SKIPPED 2`）；
> 改扁平签名后复跑 `test_agents_cross` + `test_agents_tools_workbench` + `test_mcp_server`
> + `test_item_templates_api` → **`Ran 158 tests ... OK`**；独立五方对账 `reconcile_lead.py`
> → `specs=62 master=61 routes=63 targets=42 handlers=62 write_tools=15 route_checked=63`、
> **`RECON OK`**（它同时校验每条路由的 path/query/body 与真实 FastAPI 端点签名一致）；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-gVPWSzu6.js 493.69 kB` → 140/141 后 `index-BOItszoH.js 494.17 kB`，
> `index-Ckuj6T5I.css 128.94 kB` 未变）；
> `scripts/ui_smoke.py --port 18233` → **26/26 页通过**；
> **打包版 confirm 闸门真机探针**（`probe_write_gate.py` 对 `127.0.0.1:18400`，19 个写端点
> × `{}` / `{confirm:false}` / `{confirm:true}`）→ **`GATE FAILURES: 0 / 19`**：未确认一律
> 400（`confirmation_required` / `confirm_required`），确认后无一再被 confirm 类拒绝
> （转 `invalid_fields` 或 409 `verification_required`）；语义只读、故意无闸门的
> `POST …/appeal/form` 仍是 400 `invalid_fields`（证明它没被闸门挡）。
> **打包版逐页冒烟**（`packaged_ui_smoke.py` 复用 `scripts/ui_smoke.py::run_browser`，不启后端、
> 直接打真实数据目录）→ **`PACKAGED UI SMOKE 26/26 页通过`**，截图 `logs/packaged_ui_smoke/`；
> 写界面视觉自检 `tpl_shots.py` → `logs/tpl_shots/{01-list,02-create-modal,03-create-filled,
> 04-unbind-panel,05-unbind-fixed}.png`，`page_errors: []`、`PROBLEMS: []`。
> 提交：`e3cba28`（写接口三件套 + 契约）、`4c5c8ef`（4 个 AI 写工具 + MCP 路由）、
> `bfe6bff`（运费模版页可写 + CSS）、`ebe9312` + `032721e`（文档）、
> `c39557d`（报错顺序）、`c40de47` + `53503de`（140/141 两处 UI 修正）。
> 诚实边界：`create`/`modify`/`delete`/`unbind` 的字段形状全部来自**官方 bundle 静态提取**，
> **一次真机写请求都没发过**（红线：不许改真实平台数据）；本机两个账号都 `need_relogin`，
> 写端点的真机响应（尤其是 `templateDetails` 平台是否接受字符串、`unbind` 的 `itemIds` 分隔符）
> 仍未被真机验证。140/141 的界面改动只在**打包版**上用 Playwright 点开截图看过，
> **没点过提交**（确认框之后的真实提交路径仍未被任何一次点击走到）。

### 第二十二批：商品写接口（官方批量下架 / 批量删除 / 快捷改价改库存）（142~147）

用户授权写接口（m11617）后，商品页那三个写操作终于换成官方接口。**这一批的价值不在
「多了三个按钮」，在于替掉了三种会改坏真实数据的做法**：

| 操作 | 旧做法 | 旧做法的真实危害 | 新做法（官方接口） |
| --- | --- | --- | --- |
| 批量下架 | 前端逐件循环 `POST …/delist` | N 件 = N 个平台请求，更容易撞风控 | `…pc.item.batch.offline`，**一个请求** |
| 批量删除 | `DELETE /api/items/{acc}/{item_id}` | **只删本地**，平台商品还在 —— 本地记录一删就成了幽灵 | `…pc.item.batch.delete`，平台删成功后才删本地行 |
| 快捷改价 / 改库存 | 整份原表单回提（`edit_item`） | **副作用：编辑已卖出商品会把它重新上架** | `…pc.item.info.update`，只改这两个字段、**不碰 `status`** |

**142（证据）**：四条接口名与参数形状全部来自官方微应用 bundle
`idle-pc/seller-item/0.0.20`（4,402,065 字符**单行**压缩 JS ⇒ 只有字符偏移，行号无意义；
本地副本 `C:\Users\Administrator\.dsh-tmp\adapp\item-main.js`，提取脚本
`item_write_params.py` / `item_write_callsites.py`，报告
`C:\Users\Administrator\.dsh-tmp\adapp\item_write_evidence.md`）。**零平台请求**。
包装统一是 `gC({api, v:'1.0', data, showErrorToast:true, needLoginPC:true})`，业务字段
**直接放 `data`**（不是 `idle.ad.*` 那套 `requestJson`）。关键调用点原文：

- `offline` @3078164 / 调用点 @4084449 → `z0({itemId: r})`，确认框「是否确定停止售卖，下架商品」
- `batch.offline` @3078561 / @4097558 → `H0({itemIds: T.join(",")})` —— **逗号串不是数组**
- `batch.delete` @3079755 / @4099090 → `W0({itemIds: T.join(",")})`
- `info.update` @3076546 / @4100255 → 无 SKU `N0({itemId, quantity, price})`（扁平三字段）；
  有 SKU `N0({itemId, itemSkuListStr: JSON.stringify(skuList)})`
- 成功判据逐字照抄：`res.data.data` 为真；批量逐条清单 `res.data.data.itemProcessResultList`
  （元素含 `success`），另有 `res.data.failCount`

**143（4 条死代码登记但不实现）**：`…pc.item.delete`（单个删除 @3078964）、
`…platform.item.operate.online`（上架 @3077754）、`…pc.item.price.update` @3077352、
`…pc.item.quantity.update` @3076947 —— bundle 里**声明了但调用点 0**，官方商品管理页
自己都不用它们。登记进 `item_write.py::NOT_IMPLEMENTED` 是为了下次有人扫到这些名字时
知道「已经查过了、是死代码」，而不是又去猜参数。

**144（协议层）**：新建 `backend/app/xianyu/item_write.py`。**实现口径与任务卡原文有一处
偏离，这里如实记下**：任务卡写的是 `offline_item(client, item_id)` 这类**自己调客户端**的
函数，实际落成**纯函数**（构造 `(api, data)` 二元组 + 独立的响应解析函数），发请求统一走
`common.call_method(client, ('mtop',), api, data, v='1.0')`。理由：①与 `xianyu/trade.py`
同族，服务层不用为商品单独写一套调用姿势；②纯函数能在**无客户端、无网络**下逐字测
（协议层 15 条用例没有一个是 mock 客户端换来的）；③协议层再实现一遍 `call_method`
只会多一条会漂移的路径。**HTTP 契约与服务层签名与任务卡逐字一致**，偏离只在这一层。

**145（服务层，三步顺序）**：`backend/app/services/items.py` 追加 `batch_offline` /
`batch_delete` / `quick_edit`，三步顺序固定 **确认 → 字段校验 → 建客户端**：

- 未确认 → 400 `confirmation_required`，**零平台请求**
- 字段错（空 `item_ids` / 价格库存都没给 / 价格 ≤0 / 库存非整数）→ 400 `invalid_fields`，
  **不建客户端**。这条不是洁癖：掉线账号 + 空 `item_ids` 时若先建客户端，用户看到的是
  「去重新登录」，等他真去扫了码回来才发现真正的问题是「你没选商品」
- `ok` **跟平台走**：平台逐条回报成功才算 ok；**没回报的 id 一律算失败**
  （`code='no_per_item_report'`）—— 平台没提这条，就不能替它说成功
- 平台接受整批但没给逐条清单时 `ok=total` 且 `per_item_report=false`，**不假装逐条确认过**
- `batch_delete` 平台成功后才删本地 `items` 行；本地缺行进 `warnings`（`code='local_missing'`）
  而不是 `failed` —— 平台侧确实删掉了，本地没这行只是数据本来就不全
- `quick_edit` 优先用平台回读值（`updated_values`），**不碰 `status`**
- 拿不到 `failCount` 回 `null`，**不补 0**（0 与「平台没给」是两回事）

**146（HTTP 与测试）**：`backend/app/api/items.py` 追加三条 `POST`（全 `Body(default={})`），
路由顺序照 `:219-227` 的注释踩过的坑排（`/batch-offline`、`/batch-delete` 必须在
`/{account_id}/{item_id}` **之前**，否则会被当成 `item_id` 吞掉）。新建
`backend/tests/test_item_write_api.py` **47 条**（协议层 15 + API 层 32）。

**147（UI 与 AI 两侧）**：商品页三个写入口（`Items.tsx` +429/-13、`items.css` +119，
唯一写出口 `postItemWrite` 把 `confirm: true` 写在 `...body` 之后，调用方无法覆盖）；
AI 侧 3 个写工具（`items_batch_offline` / `items_batch_delete` / `items_quick_edit`，
`required` 带 `confirm`、description 带中文警示）。MCP 的确认闸门复用在
`9786045` 统一到 `client.py::_dispatch_route` 最前面那一条，所以这三个工具不带
`confirm` 时连一个 HTTP 请求都不会发。

**这一批自己审出来的 5 个真问题**（都不是「功能没做」，是「做了但会骗用户」）：
① `failed[]`/`warnings[]` 只显示第一条 message，N>1 时其余 id/code/原因全丢 ⇒ 逐条列；
② 选择被搜索/状态筛选滤掉时按钮可点却静默无反应 ⇒ 按钮 disabled + 明确提示
「有 N 个已选商品不在当前筛选结果里」；③ 平台没回报的 id 被算成成功 ⇒ 改判失败；
④ 掉线账号的字段错误被凭据检查抢先 ⇒ 三步顺序固定，回归锁
`test_field_validation_runs_before_credential_check`；⑤ 同一个「删除」有两种语义（见 148）。

**148（收尾自查：一个词两种语义）**：行内菜单的「删除」走
`DELETE /api/items/{account_id}/{item_id}`（**只删本地**），而这一批新加的「批量删除」走
`batch-delete`（**平台真删**）—— 同一个词，两个入口，用户没法预期点下去会发生什么；
而且 `removeItem` 的注释还写着「平台侧没有删除接口」，这句在挖出
`...pc.item.batch.delete` 之后就已经是假话了。现在：行内「删除」与「批量删除」**走同一条路径**
（真删），旧的「只删本地」保留成单独菜单项「**只从本地列表移除**」（确认框写明
「闲鱼上的商品不会被删除、也不会被下架，下次同步可能又拉回来」）；后端那条 DELETE 端点的
docstring 与 `docs/CONTRACTS.md` 同步订正成「平台侧**有**删除接口，这一条是**故意**保持
『只动本地』的语义」。同一轮把四条 NAV `desc` 改准：商品页 →「商品列表、编辑、批量下架/删除
与快捷改价改库存」；退款/评价/投诉三页**本身只读**，写能力都在「订单」页的
`TradeWritePanel`（订单行末「发货」「改价」，`in_refund` 行多一个「退款」），所以文案改成
指路（「处理入口在『订单』页…」），否则用户在只读页上找不到入口。
**已知未修的 UX 缺口**（如实记下）：那三页自己没有写入口。彻底修法是把 `TradeWritePanel`
引到三页并指定初始 tab（要动 3 个页面文件 + CSS），本轮选了「文案指路」这个最小改法。

**149（打包版截图自检抓出的第 6 个真问题：一个「只改价」会顺手改库存）**：
快捷改价/库存弹窗的两个输入框都**预填了当前值**，而提交逻辑是「填了哪个就发哪个」，
于是「只想改价」必然把库存也写回平台。本地库存一旦比平台旧（同步不到、或平台被别人
买走几件），这一下就把平台库存改回旧值 —— 确认框里显示的还是「库存 100 → 100」，
用户完全看不出来；弹窗自己那句「留空的那个不会被改动」在预填的前提下也从没生效过。
改成按「**和当前值是否不同**」判断：`priceChanged`/`quantityChanged` 只在值真的与
`item.price`/`item.stock` 不同（或当前值未知）时为真，请求体只放改动过的键；两个都没
改动时按钮 `disabled`；弹窗新增「本次会提交：价格 ¥49.90 → ¥123.00」一行，没改动时明说
「还没改动 —— 把要改的数字改成别的，另一个保持原样就不会被提交」并分别标注
「价格没改动，不提交」「库存没改动，不提交」；二次确认框的「改前 → 改后」也只列会提交的
字段 + 一句「没列出来的保持闲鱼上的原值」。
**取证方式值得抄**：`C:\Users\Administrator\.dsh-tmp\item_write_shots.py` 打开写界面、
**不点最终确认**，并在 `page.on('request')` 里记录所有非 GET 的 `/api/` 请求 ——
最后一行 `non-GET api requests (should be none): []` 就是「整轮一个写请求都没发」的证据，
比「我保证没点」强。截图在 `logs/item_write_shots/`（`04-quick-modal` 显示未改动状态、
`06-quick-confirm-over-modal` 只列价格那一行、`07-back-to-modal-after-cancel` 取消后回到弹窗）。
**脚本踩坑**：确认框的「取消」在 DOM 里排在 Modal 的「取消」**之后**，用
`.first` 会点到被 `.modal-mask` 遮住的那个，Playwright 报
`<div class="modal-mask"> intercepts pointer events` —— 那是脚本选错元素，不是层级 bug；
确认框的按钮 `.last` 能正常点到，说明层级是对的。

> **第二十二批证据链**：`backend/tests/test_item_write_api.py` → **`Ran 47 tests in 0.838s OK`**
> （`XIANYU_APP_DIR=%TEMP%\xywb_item_write`；过程中修掉三处测试自身的错：
> ①解析函数收的是**客户端返回值** `{'success','data',…}`，测试要先 `wrap(payload)` 包一层；
> ②`install_fake_client(client, with_factory=True)` 注入的 `client.py::get_client` 工厂会
> **绕过 Cookie 检查**，要测凭据检查必须 `with_factory=False`；③`events.subscribe()` 不收
> 回调参数（它是 `async def subscribe() -> asyncio.Queue`），断言事件要用 `events.recent()`）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_item_write_full`）→
> **`TOTAL 47  PASS 47  FAIL 0  CASES 1429  SKIPPED 2`**（第二十一批 1382 → 1429）；
> 独立五方对账 `reconcile_lead.py` → `specs=65 master=64 routes=66 targets=45 handlers=65
> write_tools=18 route_checked=66`、**`RECON OK`**；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-DIIUhmey.js 499.66 kB` / `index-wjPANuoK.css 130.52 kB`）；
> `scripts/ui_smoke.py --port 18255` → **26/26 页通过**。
> 提交：`a9fcc32`（后端三件套 + 47 条测试）、`fc205b0`（商品页三个写入口 + CSS）、
> `a8cfb3b`（3 个 AI 写工具 + MCP 路由）、`38680a4`（契约与本文档）、
> `d34c915`（收尾：统一删除语义 + 四条 NAV desc 指路；改完 `npx tsc --noEmit` EXIT=0、
> `npm run build` EXIT=0（87 modules，`index-3Hwhy2kM.js 500.43 kB`）、
> `run_all.py` 仍 `TOTAL 47 PASS 47 FAIL 0 CASES 1429`）。
> 诚实边界：三个接口的字段形状**全部来自官方 bundle 静态提取**，**一次真机写请求都没发过**
> （红线：不许改真实平台数据）；本机两个账号都 `need_relogin`
> （`FAIL_SYS_SESSION_EXPIRED::Session过期`，cookie 还在 486/492 字节），所以
> **平台对这三个请求的真实响应、`itemProcessResultList` 的真实字段名、
> `info.update` 在真实 SKU 商品上的行为，全部未被真机验证**；UI 那三处也没在浏览器里
> 手动点过（只跑过逐页冒烟，冒烟不点写按钮）。

### 第二十三批：交易三页写入口 + 退款凭证（auth_token）只读接口（150~153）

150. **只读页在说假话**：`grep` 全站「只读 / 405 / 点不动」文案时发现 `Refunds.tsx:307` 写着
     「同意 / 拒绝退款是写接口，**本项目一律 405**」，而写接口早在
     `docs/contracts/trade_write.md` 里实现了。**根因不是代码缺失，是文案没跟上代码** ——
     用户按字面理解就会去平台网页操作，页内入口形同不存在。改法：三页各加一列写按钮
     （退款「处理退款」→ `refund-agree`、评价「评价买家」→ `rate`、投诉「处理投诉」→
     `complain-refuse`），复用订单页的 `TradeWritePanel`（`useState<WriteDraft | null>` +
     末尾 `{write && <TradeWritePanel … />}`），并逐句改掉假话。
     两处细节：① `Complaints.tsx` 行里的键是 **`complaint_id`**（不是面板预填用的
     `complainId`），要 `read(row,'complaint_id','complain_id','id')` 映射；
     ② `Evaluations.tsx` 不能承诺「回复评价」—— 平台卖家侧只有
     `mtop.taobao.idle.merchant.rate.create`（卖家评价买家），**没有**回复买家评价的接口。
     `ReturnAddresses.tsx` 的「一律 405」也改成诚实版：「**没有实现**（本页只读）：要先能从
     官方 bundle 挖出写接口名与入参，也要确认不碰风控敏感路径；在那之前宁可只读，也不猜一个
     接口去改用户的收货地址」。（顺带取证：退货地址**确实有**官方写接口
     `...merchant.delivery.address.{add,update,delete,detail.query,set.default}`，偏移
     3143576~3144501 —— 所以那句「没有实现」是准确的，只是还没做。）
     提交 `2abb038`（4 文件 +120/-8）。
151. **「同意退款」的 `auth_token` 用户无处可抄**：离线探针截图显示写面板只报
     「还缺必填项：平台操作令牌 auth_token」，而退款列表帧里根本没有这个字段
     （真机 `rightVO.btnList[].clickEvent.data.url` 是空串）。去官方交易 bundle 取证
     （`C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`，3879584 B）：
     **`authToken` 来自 `mtop.idle.alipay.verify.url.query`**，同意退款弹窗偏移 **2801524**、
     拒绝退款弹窗偏移 **2804850**，两处参数逐字相同
     `{bizId: refundId, scene:'REFUND_PC', callBackUrl:'…/#/seller-trade/refund-manage'}`，
     响应读 `data.module.verifyUrl`（官方嵌 150px iframe 让用户完成支付宝身份验证）与
     `data.module.token`；官方**只在 `window.__PC_SELLER_WORKBENCH__.userInfo.isAdmin` 为真时**
     才发这个查询，非管理员态用空 token 提交。同一族里偏移 **2838769** 的
     `compensate.service.pay`（`scene:'DEPOSIT_PC'`）是**钱相关，明确不做**。
152. **退款凭证只读接口落地**（协议 + 服务 + 路由 + 前端 + 探针）：
     `backend/app/xianyu/trade_write.py` 加 `VERIFY_URL_API`/`VERIFY_URL_VERSION`/
     `VERIFY_SCENE_REFUND`/`VERIFY_CALLBACK_URL`、`READ_APIS`、`verify_url_request(refund_id)`
     （签名里**没有** `confirm`）、`parse_verify_result(raw)`（取 `body['module']`，
     空串一律 `None`，不拿 `''` 冒充可用凭证）；`backend/app/services/trade_write.py` 加
     `refund_verify_token(account_id, refund_id)`（不生成、不缓存、不落库；平台拒绝走
     `protocol_error`/502 且**不碰登录态** —— 无权限 ≠ 掉线）；
     `backend/app/api/trade_write.py` 加 **`GET /{account_id}/refunds/{refund_id}/verify-token`**。
     **口径**：只查不改 ⇒ GET、故意没有 `confirm`（判据一句话：**confirm 只钉有副作用的那一步**）；
     协议层登记在 `READ_APIS`、**故意不进 `WRITE_APIS`**，免得被只读层写黑名单与写闸门误伤。
     前端在 `Orders.tsx` 的同意退款面板里加「退款凭证 verify-token」一块（`fetchVerify()` 把
     token 填进 `auth_token`、`verify_url` 显示成「打开验证页」链接；平台没给 token 时明说
     「官方只在管理员态才下发」），样式 `.tr-write-verify` 插在 `trade.css` 的 `@media` 之前。
     提交 `13212e9`（后端 4 文件 +233）、`ec8cd3c`（前端 2 文件 +81/-2）。
153. **AI 侧也补上**：只读工具 `trade_refund_verify_token`（`required` 只有
     `account_id`/`refund_id`、**没有 `confirm`**、description 明写「平台没给令牌时 token 是
     null，不要自己编」）+ `_SERVICE_TARGETS` + `_HANDLERS` + MCP 路由
     `GET /api/trade/{account_id}/refunds/{refund_id}/verify-token`，提交 `49403f7`
     （3 文件 +31/-1）。**踩坑**：`backend/tests/test_agents_cross.py` 的
     `tools.TOOL_NAMES` 是**按 `TOOL_SPECS` 真实顺序**逐项断言的 list，把新工具名放在组尾会
     `AssertionError: Lists differ` —— 必须插到它前面的那个工具名之后。

> **第二十三批证据链**：`backend/tests/test_trade_write_api.py` →
> **`Ran 44 tests in 0.867s OK`**（原 33 条 + 11 条：`RefundVerifyTokenTest` 8 条断言
> 形状/没有闸门/空串→`None`/无权限是 502 且 `login_state` 仍 `active`/无 Cookie 零平台请求/
> 404/401/路由是 GET，`VerifyTokenProtocolTest` 3 条断言签名无 `confirm`、
> `VERIFY_URL_API` 在 `READ_APIS` 且不在 `WRITE_APIS`、解析器不编 token）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests OK`**；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_verify_full2`）→
> **`TOTAL 47  PASS 47  FAIL 0  CASES 1440  SKIPPED 2`**；
> 独立五方对账 `reconcile_lead.py`（`XIANYU_APP_DIR=%TEMP%\xywb_recon4`）→
> `specs=66 master=65 routes=67 targets=46 handlers=66 write_tools=18 route_checked=67`、
> **`RECON OK`**（只读工具没被算成写工具）；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-CXzeA2jP.js 504.01 kB` / `index-mh5rQzhQ.css 130.97 kB`）；
> `scripts/ui_smoke.py --port 18277` → **26/26 页通过**；
> 两个离线 Playwright 探针（都断言整轮 `non-GET /api/ 请求 == []`）：
> `trade_write_entry_probe.py` → **`TRADE WRITE ENTRY ALL OK (3/3)`**（三页写入口都能打开、
> 单号已预填、没点最终确认就一个写请求都不发，截图 `logs/trade_write_entry/`）、
> `verify_token_probe.py` → **`VERIFY TOKEN ALL OK (2/2)`**（给了 token → 框里就是它 +
> 「打开验证页」可见；回空串 → 不填框 + 显示提示，截图 `logs/verify_token/`）。
> 提交：`2abb038`（三页写入口 + 说真话）、`13212e9`（退款凭证后端）、`ec8cd3c`（前端获取凭证）、
> `49403f7`（AI 只读工具）。
> 诚实边界：`mtop.idle.alipay.verify.url.query` 的**真实响应未被真机验证**（本机两账号都
> `need_relogin`），`module.token`/`module.verifyUrl` 的字段名来自 bundle 的读取点而非响应样本；
> `auth_token` 也**没有被真机用去提交过退款**（红线：不改真实平台数据、不动钱）。
> 探针里两个坑值得记：① 第二轮点按钮报 `.modal-mask intercepts pointer events` 是因为
> 第一轮没关面板而 `page.goto` 同 hash 不刷新 —— 加 `page.reload()` 即可，**不是层级 bug**；
> ② 按 placeholder 找输入框会假失败（placeholder 已改成提示语），要按 `label.tr-field` 的
> 文案定位。

### 第二十四批：安全中心文案对齐 + 申诉接进 AI 工具 + 对账脚本入仓（154~156）

154. **安全中心页也在说假话**（与第 150 条同源：文案没跟上代码）。`frontend/src/pages/SecurityCenter.tsx:89`
    的副标题写着「（只读，不会替你申诉）」、`:133` 的底部 Notice 写着「工作台不会替你提交任何申诉
    或上传材料」，而 `POST /api/security-center/{account_id}/appeal/{submit,cro/submit,urge}` 三条写接口
    **第二批就实现了**（带 confirm 闸门）。**修法**：副标题改成「闲气值与违规记录（本页只读；申诉提交要
    显式确认）」；Notice 改成说清三件事 —— 本页只读、页面上没有提交入口（想自己在官方页面提交就点
    「去申诉」）、申诉**也做成了接口与 AI 工具**但必须显式 `confirm`，不确认一个平台请求都不会发。
    （JSX 里 `{account_id}` 要写成 `&#123;account_id&#125;`。）
    > 顺带审计：`grep` 扫 `frontend/src/pages/*.tsx` 的 `只读|405|点不动|在平台操作|不支持|暂时` 共 101 处命中，
    > 逐条看过，**其余都是诚实的**（`CsData`/`DataOverview`/`Fans`/`Finance`/`Marketing`/`CsRouting`/
    > `SubAccounts`/`ReturnAddresses` 那些页确实只读、写接口确实没实现）；`Chats.tsx:812-819` 的
    > 「后端会话消息接口尚未可用」也不是假话 —— `notReady` 来自 `useAsync.ts:73` 的 `isNotReady(err)`
    > （路由缺失才置位），不是「没选账号」。

155. **写接口做了、AI 调不到**：申诉三条 POST 存在，但 `backend/app/agents/tools.py` 里只有只读的
    `security_center_status`，AI 一个申诉工具都没有。**修法**：新增 4 个工具（插在
    `trade_write_complain_proof` 之后，**顺序必须与 `TOOL_SPECS` 一致** —— `test_agents_cross.py` 是按
    真实顺序逐项比对的）——`security_appeal_form`（**只读**，读申诉表单，`required: ['account_id','form_id']`，
    **故意没有 confirm**）、`security_appeal_submit` / `security_appeal_submit_cro` / `security_appeal_urge`
    （三条写，`required` 含 `confirm`）；`_SERVICE_TARGETS` 与 `_HANDLERS` 各 4 条；
    `mcp_server/client.py` 的 `TOOL_ROUTES` 加 4 条（form 是 POST 但无 confirm，另三条带）。
    测试三处硬编码清单同步：`test_agents_cross.py` 的工具名清单（**按真实顺序插**，放到组尾会
    `AssertionError: Lists differ`）与写工具清单（只读的 form 不进）、`test_agents_tools_workbench.py` 的
    `WRITE_TOOLS`/`_CONFIRM_FALSE_CALLS`、`test_mcp_server.py` 里两处写路由条数 **18 → 21**
    （不改会报 `AssertionError: 21 != 18 : 写工具数量变了就要同步复核这份清单`）。
    > 口径沿用第一批就定下的判据：**`confirm` 只钉有副作用的那一步** —— 读表单不钉，提交申诉才钉。

156. **对账脚本原来只在临时目录里，文档却要求「新增写工具必须同步改它」**：把
    `reconcile_lead.py` 入仓为 **`scripts/reconcile_tools.py`**，唯一改动是硬编码的仓库根换成
    `ROOT = Path(__file__).resolve().parents[1]`。它比"只比工具名清单"更强：**把每条 MCP 路由声明的
    query/body 键跟真实端点签名对账**（从 `app.openapi()` 取 `parameters` 与 `requestBody` schema），
    所以能自动抓「路由参数漂移」—— 例如 `finance_income` 曾声明 `cycle_from`/`cycle_to` 而端点只认
    `from`/`to`，发过去被 FastAPI **静默忽略**（结果错但不报错）。跑法写进 docstring：
    `cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& set XIANYU_APP_DIR=%TEMP%\xywb_recon&&
    C:\Python310\python.exe scripts\reconcile_tools.py"`，必须打印 `RECON OK`。
    `WRITE_PREFIX` 元组补进两个新写工具；docstring 里写明**新增写工具必须同步改这个元组**，
    否则必然假阳性（"只读工具却带 confirm 参数"——本批之前已经踩过两次）。

> **第二十四批证据链**：`scripts/reconcile_tools.py`（`XIANYU_APP_DIR=%TEMP%\xywb_recon6`）→
> `specs=70 master=69 routes=71 targets=50 handlers=70` / `write_tools=21` / `route_checked=71` /
> **`RECON OK：五方一致，且每条路由的 path/query/body 都被真实端点接受。`**；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests in 3.519s OK`**（`EXIT=0`）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_appeal_full`）→
> **`TOTAL 47  PASS 47  FAIL 0  CASES 1440  SKIPPED 2`**（用例数不变 —— 4 个新工具是被既有循环测试
> 覆盖的，没有新增用例）；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-DHN0PnNK.js 504.15 kB` / `index-mh5rQzhQ.css 130.97 kB`）；
> 提交：`9ade196`（安全中心文案）、`25f5b8d`（申诉 AI 工具 + 对账脚本入仓）。
> 诚实边界：4 个申诉工具与 `scripts/reconcile_tools.py` 都只做了离线验证 —— **申诉一次都没真发过**
> （本机两账号都 `need_relogin`，且红线是不改真实平台数据）；MCP 侧只验到「不带 confirm 被拦、
> 带 confirm 后停在 409 凭据检查」，没有真机跑通一条申诉。
> 踩坑：`pwsh` 里 `python.exe -c "…"` 带中文/引号会被 PowerShell 解析器打断
> （`ParserError: 缺少终止符`）⇒ 复杂片段改用 here-string 或写成 `.py` 文件；`Select-String` 接管道会让
> `$LASTEXITCODE` 变 1，判退出码必须重定向到文件再读。
>
> 打包版复验（`dist\闲鱼AI工作台\闲鱼AI工作台.exe` = **9990560 字节 / 2026-10-12 07:13:42**，
> `--port 18400` → `/api/health` 200 `frozen:true`）：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 22`**（语义只读的 `sec.appeal_form` 仍 400 `invalid_fields`）；
> `packaged_ui_smoke.py` → **`PACKAGED UI SMOKE 26/26 页通过`**；
> `check_assets.py` → `GET /` 引用 `['index-DHN0PnNK.js','index-mh5rQzhQ.css']`、remote/local SHA256
> 两边相同（`5f02a124d717` / `ce9323192809`）、**`ASSETS_MATCH=True`**；
> `scripts/ui_smoke.py --port 18299` → **26/26 页通过**。

### 第二十五批：退货地址写接口 + 只读解析认两套键名 + 探针不再打真平台（157~161）

157. **退货地址是「只读」这句话本身是错的**。`frontend/src/pages/ReturnAddresses.tsx` 的文件头写着
     「新增 / 编辑 / 删除退货地址是写接口，本项目一律 405」—— 写接口不是不存在，是我们没做。
     去官方交易 bundle（本地副本 `C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`，3,879,584 字符
     **单行**压缩 JS，只有字符偏移、行号无意义）取证，拿到完整一族：
     `mtop.alibaba.idle.seller.platform.merchant.delivery.address.{list.query,detail.query,add,update,delete,set.default}`
     （偏移 3142733 / 3143576 / 3143703 / 3143951 / 3144220 / 3144501）。声明原文：
     `add`/`update` 带 `callbackOptions.successText:'退货地址新增/更新成功'`、`delete` 是 `{contactId}`、
     `set.default` **只有 `errorText`**（成功靠重拉列表确认）、`detail.query` 是 `{contactId}`。
     编辑弹窗（偏移 3780900~3782550）的 `onModalShow(e)` 直接把列表行摊进表单
     （`division:[e.provinceName,e.cityName,e.districtName]`），提交时把级联结果摊成
     `{...values, areaId, provinceName, cityName, districtName}`。

158. **诚实偏离：区域码不伪造**。官方 payload 里还有 `division`（三元素 `{label,value}` 级联数组）与隐藏的
     `areaId`（区级区域码）。我们没有码源 —— `mtop.taobao.idle.division.all.get` 对本机账号回
     `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`。所以**只发省 / 市 / 区三个名字**，`areaId` 只在调用方真的给了才发，
     `division` 一个字节都不发（宁可平台明确拒绝，也不发一个形状不对的级联数组去赌）。
     平台拒绝的原因原样透出（`platform_errors.api/code/message`）。

159. **`confirm` 一律原样交服务层判，路由层不做 `bool()`**。四条 address 路由原来写
     `confirm=bool(body.get('confirm'))` —— 而 `bool('false')` 是 `True`，用户明确写 `"confirm": "false"`
     会被当成同意。改成 `confirm=body.get('confirm')`，服务层用统一口径 `_truthy`（只有 `True`/`'true'`/`1`
     算点头，与 `services/items.py`、`services/item_templates.py`、`mcp_server/client.py` 同口径）。
     旧 8 条 trade 写端点这次**没动**（一次提交只做一件事）。

160. **只读解析把逐字段全丢了（真缺陷）**。`backend/app/xianyu/trade.py::normalize_address` 只映射
     `{address_id,name,phone,address,is_default}`（= bundle **只读页**那套 `{addressId,name,phoneNum,address,defaultAddr}`），
     而**写页编辑弹窗**按 `contactId/contactName/mobilePhone/provinceName/cityName/districtName/detailAddress/defaultAddr`
     逐个字段读。两处读法冲突、真机 `list.query` 又无权限（拿不到样本），所以**不猜哪一套是真的**：
     前 5 键的回退键扩成 `_first(item,'addressId','contactId','id')` 等，再**仅当平台真的给了**才追加
     `contact_id`/`contact_name`/`mobile_phone`/`province_name`/`city_name`/`district_name`/`detail_address`
     （空白字符串不出现，**绝不凭空造字段、也不自己拼地址串**）。前端 `openEdit` 同步：能预填就预填，
     预填不了才让用户重填，两种情况的说明文案不同。

161. **探针自己变成了写请求的来源（流程缺陷，最该记的一条）**。`probe_write_gate.py` 扩到 26 个写端点后，
     `confirm:true` 那一腿是拿**真账号**打的 —— 而账号 `2217970177348` 已在 2026-10-11 23:03 重新扫码
     （`operation_logs` id 191~194 `relogin`/`qr_login`/`refresh_profile`），登录态是 `active`，
     于是它**真的把三条写请求打到了闲鱼**：id 196 `ship_dummy` → `订单状态不正确::ORDER_STATUS_ERROR`，
     id 197/198 `address.delete`/`address.set_default` → `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。
     **没有造成任何改动**（平台全拒），但「验证闸门」的探针不该是写请求的来源。修法：确认那一腿把路径里的
     真账号换成 `SAFE_ACC='9999999999999'` ⇒ 闸门放行后本地查库直接 404 `not_found`，**零平台请求**。
     重跑 `GATE FAILURES: 0 / 26`。顺带确认一个真事实：退货地址的写接口对这个**已登录**账号也回
     `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` —— 与只读 `list.query` 同一个权限问题，**不是代码 bug**。

> **第二十五批证据链**：`backend/tests/test_address_write_api.py` →
> **`Ran 39 tests in 0.521s OK`**（`EXIT=0`）；`backend/tests/test_trade_api.py` →
> **`Ran 34 tests in 0.418s OK`**（原 26 + 新增 `AddressNormalizeTest` 8 条）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_addr_full`）→
> **`TOTAL 48  PASS 48  FAIL 0  CASES 1487  SKIPPED 2`**（第 24 批 47/1440 → +39 +8）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`UNITTEST_EXIT=0`**（task-76 先前那 13 条 failure 全转绿）；
> `scripts/reconcile_tools.py`（`XIANYU_APP_DIR=%TEMP%\xywb_recon_t76`）→
> `specs=74 master=73 routes=75 targets=54 handlers=74` / `write_tools=25` / `route_checked=75` /
> **`RECON OK`**；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-B0lhDz8I.js 511.47 kB` / `index-DKqV08Ps.css 131.84 kB`）；
> 提交：`86c5491`（退货地址写接口三件套 + 39 条测试）、`f015490`（只读解析认两套键名）、
> `aec7a9c`（退货地址页四个写入口）、`445440f`（4 个写工具接进 AI 与 MCP）。
> 诚实边界：**退货地址的写接口一次都没真机成功过** —— 本机这个账号对它没有权限
> （`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，与只读同一个问题），所以「平台接受了我们的 payload」
> 这件事仍未验证；区域码 `areaId` 也没有码源（`division.all.get` 无权限）。UI 侧未做浏览器实跑。
> 踩坑：① `npx tsc --noEmit` **必须在 `frontend/` 目录里跑** —— 在仓库根跑会解析到另一个 tsc
> （`Version 7.0.2`）并打印帮助文本、`TSC_EXIT=1`（假失败）；② 改文件前必须用 `read` **工具**读过
> （用 `pwsh` + python 读不算），否则 `edit` 报 `file changed since it was read`；
> ③ 假客户端注入异常要用 `fake.errors[api] = SessionExpiredError(...)`，塞进 payloads 会被当成响应体、
> 测试假绿。
>
> 打包版复验（`dist\闲鱼AI工作台\闲鱼AI工作台.exe` = **10001391 字节 / 2026-10-12 08:34:30**，
> `--port 18400` → `/api/health` 200 `frozen:true`）：
> `probe_write_gate.py`（26 个写端点，确认腿换假账号）→ **`GATE FAILURES: 0 / 26`**；
> `packaged_ui_smoke.py` → **`PACKAGED UI SMOKE 26/26 页通过`**；
> `check_assets.py` → `GET /` 引用 `['index-B0lhDz8I.js','index-DKqV08Ps.css']`、remote/local SHA256
> 两边相同（`073172fe4946` / `d1e8d5b13265`）、**`ASSETS_MATCH=True`**；
> `scripts/ui_smoke.py --port 18311` → **26/26 页通过**。

### 第二十六批：买家改地址审批（读 + 同意/拒绝）+ 隐私号解密（162~166）

162. **官方订单页的「买家申请修改收货地址」审批是有写能力的**。我们此前只有订单列表，用户在页面里
     既看不到申请、也没法同意/拒绝，只能回官方点。去官方交易 bundle 取证拿到三条接口与逐字参数
     （本地副本单行压缩 JS，只有字符偏移）：`mtop.idle.merchant.order.get.modify.address.info`
     v1.0 `{bizOrderId}`（@3101551）→ `data.module = {modifyStatus, modifyAddressInfoVO,
     nowAddressInfoVO, modifyDeliverId}`；`…order.address.modify.agree` `{bizOrderId,deliverId}`（@3102802）；
     `…modify.refuse` 同参数（@3103173）。官方组件初值就是 `'NONE'`，**只在 `modifyStatus === 'WAITING'`
     时才把 `modifyDeliverId` 与两个 VO 传下去** ⇒ 我们也只在那一档认 `deliver_id`，
     `can_operate` 由服务层算好给前端。

163. **隐私号订单必须调解密接口，而且必须带 `valueType:'string'`**。`nowAddressInfoVO.privacyFlag`
     为真时平台给的是打码字段，官方会再调 `mtop.idle.trade.order.detail.privacy.address`
     （@2127351 的 `nP` 助手，`{bizOrderId, pageSource}`，改地址场景 `pageSource='modify_address'`）
     换明文。这个接口**必须带 `valueType:'string'`**（业务体是 JSON 字符串）—— 我们的 mtop 客户端
     默认不带，所以给 `call_params`/`mtop` 加了**可选** `value_type`，**只有传了才加 `valueType`**
     （给别的接口塞上会改变它们的响应形状）。解不开就 `decrypted: null` + 中文 warning，
     `now_address` 仍是平台原样给的打码字段：**不编明文、也不把打码字段当明文**；
     解密失败**不会**把整个只读页判成失败（用户至少还能看到字段）。

164. **只读查询故意不给 `confirm` 闸门**。判据一句话：**confirm 只钉有副作用的那一步**。
     `GET …/modify-address` 只查不改，加闸门只会稀释真危险操作的信号（用户被训练成无脑点确认）。
     协议层把它登记在 `order_address.READ_APIS`，**不进 `WRITE_APIS`** —— 否则既会被写闸门误伤、
     又会被写黑名单挡住。同一批里 `confirm` 只认 `True`/`'true'`/`1`（`_truthy`），HTTP 层
     把 body 的 `confirm` **原样**交服务层、不做 `bool(...)`：`bool('false')` 是 `True`，
     对「改真实收货地址」这种操作，宽松判真会出人命。

165. **工具名不能让「读名是写名的前缀」**。读工具叫 `trade_order_address_request`、
     写工具叫 `trade_order_address_agree`/`trade_order_address_refuse` —— 如果读工具叫
     `trade_order_address_...` 而 `scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 里加了
     `'trade_order_address_'`，只读工具就会被误判成写工具、报「只读工具却带 confirm 参数」。
     所以 `WRITE_PREFIX` 只加两个**完整**写工具名，不加前缀。这条与第二十四批 ④ 是同一类坑。

166. **协议层是最后一道**：`agree_request`/`refuse_request` 在空订单号 / 空 `deliverId` 时直接抛
     `ValueError`，不发出去。服务层的 `_missing` 也会挡（三步顺序 **确认 → 字段校验 → 建客户端**），
     但协议层是最后一站，漏一个空字符串过去就可能把改地址请求发到平台上。

> **第二十六批证据链**：`backend/tests/test_order_address_api.py` → **`Ran 41 tests in 0.449s OK`**（`EXIT=0`）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_oa_full`）→
> **`TOTAL 49  PASS 49  FAIL 0  CASES 1534  SKIPPED 2`**（第二十五批 48/1487 → +41，另有 6 条
> 主管 AI 写操作纪律用例落在第二十四~二十五批之间）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests ... OK`**、`UNITTEST_EXIT=0`；
> `scripts/reconcile_tools.py`（`XIANYU_APP_DIR=%TEMP%\xywb_recon_t78`）→
> `specs=77 master=76 routes=78 targets=57 handlers=77` / `write_tools=27` / `route_checked=78` /
> **`RECON OK`**、`RECON_EXIT=0`；
> 前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-F91U0U67.css 132.75 kB` / `index-DQjHBzUp.js 518.46 kB`）；
> `scripts/ui_smoke.py --port 18333` → **`UI SMOKE ALL OK (26/26 页通过)`**；
> 提交：`16b9a5e`（协议层 + 服务层 + 3 条路由 + 41 条测试）、`ec33b0b`（订单页「改地址」审批入口）、
> `121a9ab`（AI 工具 1 读 + 2 写 + MCP 路由）。
> 诚实边界：**同意/拒绝一次真机请求都没发过** —— 改地址审批要买家先发起申请，不能为了验证
> 去改用户真实订单的收货地址；隐私号解密也没有真机样本（本机账号的订单都没有 `privacyFlag`），
> 「平台接受我们的 payload」仍未验证。UI 侧只做了离线探针/冒烟，没有真机实跑。
>
> 打包版复验（exe `10,010,659` 字节 / 2026-10-12 09:52:14，`--port 18400` `/api/health` 200
> `frozen:true`）：`/openapi.json` = **142 条路径 / 155 个操作**（新增三条改地址审批已挂载）；
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 28`**；`packaged_ui_smoke.py` → **26/26 页通过**；
> `check_assets.py` → `GET /` 引用 `['index-DQjHBzUp.js','index-F91U0U67.css']`，remote/local
> SHA256 相同（`d1dc70905cde` / `b177818cc5d0`），**`ASSETS_MATCH=True`**。

### 第二十七批：只读路径的平台拒绝补人话（167）

167. **用户会被平台原文骗去白扫一次码**（真机巡检发现）：只读接口把平台的拒绝原样甩出来
   （`NO_PERMISSION::用户没有权限`、`FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`），
   看起来跟「登录过期」一模一样 —— 用户会去重新扫码，扫完照样无权限。
   人话映射（`backend/app/services/platform_errors.py` 的 `PLATFORM_ERROR_HINTS`，每条都带
   真机证据注释）新增 5 条：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`、`NO_PERMISSION`、
   `E_AD_BP_COMMON_SYSTEM_EXCEPTION`、`COMMON_SERVER_ERROR`、`ORDER_STATUS_ERROR`。
   前两条都明写「**不是登录掉线**，重新扫码不会变好」；`ORDER_STATUS_ERROR` 的措辞必须
   通用（这码在发货/改价/同意退款都会回，写成「不允许发货」就讲错了）。
   **一处收口**：`backend/app/services/common.py` 的 `respond()`（全项目唯一的响应序列化出口）
   在失败分支调 `platform_errors.decorate(result)` —— 平台拒绝会从二十几个服务各自的 `_call`
   里冒出来，逐处补必然漏；`decorate` **不改 `code`**，状态码不受影响。
   新增 `ReadPathRefusalTest` 6 条（含词边界：`FAIL_SYS_NO_PERMISSION` 不该命中裸码
   `NO_PERMISSION` 的解释）+ `test_order_status_error_stays_generic`。

> **第二十七批证据链**：`backend/tests/test_platform_errors.py` → **`Ran 32 tests ... OK`**；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_hints_full2`）→
> **`TOTAL 49  PASS 49  FAIL 0  CASES 1541  SKIPPED 2`**（既有断言都是 `assertIn` 风格，
> 追加人话没打破任何测试）；提交 `701a7f5`。
> 诚实边界：只覆盖**真机见过**的 5 个码，其它平台码仍然原样透出（宁可原样，也不猜意思）。

### 第二十八批：UI 细节自查 —— 对比度、可访问性、表单标签关联（168~171）

168. **深色主题的次要文字太灰**：`--text-mute` 深色 `#6b7a99` 在 panel 上只有 3.96:1
   （panel-2 上 3.4:1）、浅色 `#78849d` 在白底上 3.76:1 —— 品牌副标题、导航分组标题、
   页头描述、字段提示、表格里的次要行都是这个色（11~12px 的小字最吃亏）。改成深色
   `#828fa8`（5.25 / 4.53）、浅色 `#667290`（4.80 / 4.51）：**只改值，令牌名与其余令牌不动**。
169. **深色 `--accent` 两头不讨好**：白字压在 `#3b82f6` 上只有 3.68:1（主按钮、分段按钮
   选中态），而它当**文字**放在淡蓝 chip 上更低（3.65 / 3.28）—— 一个变量救不了两头。
   深色另加一段专用覆盖（放在浅色主题块之后，浅色完全不受影响）：主按钮 `#2563eb`
   （白字 5.17:1，**含禁用态** —— 禁用只是压暗 opacity，底色仍是 accent）、hover
   `#1d4ed8`、分段按钮选中态同色、`.ac-inline-link` 与 `.pv-default-chip` 改用 `--ask`。
170. **`Field` 的 `<label>` 与控件是兄弟节点**（真缺陷）：点标签不会聚焦输入框，读屏只说
   「组合框」而说不出这个框是干什么的（`<select>` 没有 placeholder 可退，最明显）。
   `children` 是**单个**元素且自己还没 `id` 时，用 `useId()` 给它补 `id`、给 `<label>`
   补 `htmlFor`；其余形状行为不变。另给「商品」页的账号/状态筛选与「会话」页的账号/模式
   筛选补 `aria-label`，商品列表的全选与逐行复选框补上名字。
171. **两个审计脚本入仓**（原来躺在临时目录，各查出一个真问题）：`scripts/contrast_audit.py`
   （26 页 × 深/浅两套主题，背景沿父链逐层 alpha 合成，否则半透明 badge 会被当实色）、
   `scripts/a11y_audit.py`（无可访问名字的按钮 / 没有标签的输入框 / 无 alt 图片 / 焦点环）。
   两者都只读，并断言整轮没有非 GET 的 `/api/` 请求；不给 `--port` 就打打包版。

> **第二十八批证据链**：`scripts/contrast_audit.py --port 18393 --threshold 4.0` →
> `[dark] BAD_PAGES 0/26: []`、`[light] BAD_PAGES 0/26: []`、`NON_GET_API_REQUESTS=[]`；
> `scripts/a11y_audit.py --port 18395` → 「无可访问名字的按钮 0 类、没有标签的输入框 0 类、
> 焦点不可见的页面 []」、`NON_GET_API_REQUESTS=[]`；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_contrast_full`）→
> **`TOTAL 49  PASS 49  FAIL 0  CASES 1541  SKIPPED 2`**；
> `scripts/ui_smoke.py --port 18397` → **`UI SMOKE ALL OK (26/26 页通过)`**；
> 前端 `cd frontend && npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（87 modules，
> `index-CLcnv55b.css 133.03 kB` / `index-B8urkkWX.js 518.71 kB`）；
> 提交：`a66cfcf`（深色对比度补正）、`dbcf62f`（标签关联 + 可访问名字）、`4da7d86`（审计脚本入仓）。
> 诚实边界：两个脚本查的是 DOM 与计算样式，查不出布局错位、动效、触屏手感；对比度阈值
> 取 4.0（正文标准，大字本可放宽到 3.0），深色最低值正好 ≈4.0，**没有余量**。
>
> 打包版复验（exe `10,011,663` 字节 / 2026-10-13 02:45:08，`--port 18400` `/api/health` 200
> `frozen:true`）：`scripts/contrast_audit.py --threshold 4.0`（不带 `--port`，打的就是打包版）
> → `[dark] BAD_PAGES 0/26`、`[light] BAD_PAGES 0/26`、`NON_GET_API_REQUESTS=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、`NON_GET_API_REQUESTS=[]`；
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 28`**；`packaged_ui_smoke.py` → **26/26 页通过**；
> `check_assets.py` → `GET /` 引用 `['index-B8urkkWX.js','index-CLcnv55b.css']`，remote/local SHA256
> 相同（`aeea3e6e7024` / `6f4b4d343470`），**`ASSETS_MATCH=True`**。

### 第二十九批：草稿箱（列表 / 详情 / 删除）（172~176）

172. **官方「草稿箱」不是菜单页，是商品管理的第三个 tab**（`seller-item` 里 `type===2`）。
    从 bundle 钉住的行键是 `draftId`（商品行是 `itemId`）、表格列 `accountName`（草稿读
    `imageInfoDOList[0].url`）、`quantity`、`gmtCreateStr`（商品是 `gmtCreate`）、`operation`，
    以及「编辑草稿」的路由 `#/seller-item/publish?draftId=<draftId>`。这些都没写进实现以外的地方 ——
    我们只复刻**能力**（列表 / 详情 / 删除），不复刻它的页面路由。
173. **接口族与参数形状**（`backend/app/xianyu/draft.py`，逐字来自 bundle 字符偏移）：
    `draft.search`（@3072333，`{pageNo,pageSize}`，读 `e.data.data.itemSearchResponseList` + `total`）、
    `draft.get`（@3063462，`{draftId}`）、`draft.delete`（@1806143，`{draftId}`）、
    `draft.batch.delete`（@3080157，`{draftIds: T.join(",")}` —— **逗号串**，读 `sucCount`/`failCount`）。
    分页只允许 **10 / 20 / 50**（官方草稿 tab 的分页器就这三档）。
    `draft.update` **故意不实现**：它的 payload 是整份发布表单的 `inputJson`，字段形状没有证据
    —— 猜一个去打平台等于拿用户的草稿做实验；登记在 `NOT_IMPLEMENTED` 让「不是漏了」可查。
174. **真相一：空草稿箱是正常结果**。官方对两个删除接口**根本没读响应体**，对 search 只是
    `itemSearchResponseList || []`。所以判据不能是「业务体为真」，必须是「客户端没报错 +
    平台 `code == 'success'`」—— 否则一件草稿都没有的新账号会看到失败。
175. **真相二：平台不逐条回报**。批删只有 `sucCount`/`failCount`，不说是哪几条 ⇒ `failed[]`
    **恒为空数组** + warning `no_per_item_report`；两个计数都没有 ⇒ `ok: null` + warning `no_counts`
    （不是 0、更不是「全部成功」）。这一条是「宁可不答，也不编」的直接体现。
176. **UI 落在商品页的第三个 tab**（不是新侧栏页）：`frontend/src/pages/DraftBox.tsx` +
    `drafts.css`，`Items.tsx` 只加 import / tab 类型 / 一个按钮 / 一个渲染分支。批量结果逐条列出
    failed 与 warnings，`quantity` 为空显示 `-`（不补 0），详情 `fields === null` 与 `{}` 文案分开。
177. **凭据语义与其它服务对齐**（打包版闸门探针抓到的）：草稿服务一开始自己调
    `common.get_client`，于是「账号不存在」也落成 503 `xianyu_not_ready`，而
    `items` / `item_templates` / `trade_write` 全都是 **账号不存在 → 404 `not_found`**、
    **没有 Cookie / 已判定要重登 → 409 `verification_required`**。改成复用
    `services/trade.py::_client`（同包私有助手，只此一份判定），不再自己写第二套。
    抓法值得记：闸门探针的「确认腿」故意换成**不存在的账号**（`SAFE_ACC`），
    于是「闸门放行后落到哪一层」被暴露出来 —— 别的端点回 404 / 400，只有草稿回 503。
178. **草稿箱真机只读探针**（第一次拿真账号打真平台，只发 GET）：
    `GET /api/drafts/2217970177348?page=1&size=20` → **502 `protocol_error`**，
    平台原文 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`，人话提示正确显示
    「这个账号没有开通这个功能的权限……**不是登录掉线**」；`size=30` → **400 `invalid_fields`**
    （本地就拦，零平台调用）。**这是账号没权限，不是代码 bug** —— 与退货地址写接口、
    子账号、资质同族；但也顺带证明**参数形状被平台接受了**（它没报参数错，是走到权限判定才拒的）。
    列表里没有草稿，所以详情**故意跳过**（不猜 `draft_id` 打平台）。

> **第二十九批证据链**：`backend/tests/test_drafts_api.py` → **`Ran 47 tests in 0.668s OK`**（`EXIT=0`）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_drafts_full2`）→
> **`TOTAL 50  PASS 50  FAIL 0  CASES 1588  SKIPPED 2`**；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server backend.tests.test_agents_tools_workbench`
> → **`Ran 130 tests in 3.857s OK`**、`UNITTEST_EXIT=0`；
> `scripts/reconcile_tools.py` → `specs=81 master=80 routes=82 targets=61 handlers=81`、
> `write_tools=29`、`route_checked=82`、**`RECON OK`**；
> 前端 `cd frontend && npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（89 modules，
> `index-BZVYw-fj.css 136.44 kB` / `index-C_tiY-6h.js 529.29 kB`）；
> `scripts/ui_smoke.py --port 18411` → **`UI SMOKE ALL OK (26/26 页通过)`**。
> 打包版复验（exe `10,028,165` 字节 / 2026-10-13 04:42:58，`--port 18400` `/api/health` 200
> `frozen:true`）：`/openapi.json` = **146 条路径 / 159 个操作**（4 条 `/api/drafts` 全在）；
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 30`**（`drafts.delete` 确认腿 404 `not_found`、
> `drafts.batch_delete` 400 `invalid_fields`；语义只读的 `sec.appeal_form` 仍 400 `invalid_fields`）；
> `packaged_ui_smoke.py` → **26/26 页通过**；`check_assets.py` → `ASSETS_MATCH=True`
> （`index-BZVYw-fj.css` = `03f2288946a9`、`index-C_tiY-6h.js` = `87f233ebab08`，remote=local）；
> 真机只读探针 `drafts_read_probe.py` → 列表 502 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（账号无权限）；
> 草稿箱 tab 截图复核 `drafts_tab_shots.py` → `PROBLEMS: []`（见第三十批 179：那一轮抓到页面上
> 真的显示出了字面 `**`）。
> 诚实边界：**一次写请求都没发过**（本机账号缺平台业务权限，且删草稿不可撤销）；上面所有形状
> 都是 bundle 反推 + 假协议层测试；草稿详情的 `fields` 键名未钉住，所以前端摊平成「字段 / 值」两列。

### 第三十批：草稿箱 tab 视觉复核抓到的「字面 markdown 星号」（179）

179. **页面上真的显示出了两个星号**。草稿箱 tab 的截图复核（新探针
    `drafts_tab_shots.py`：选中账号 → 点「草稿箱」→ 截图，并断言整轮
    `non-GET api requests: []`）把平台的拒绝提示原样打出来时，看到的是
    「……。**不是登录掉线**，重新扫码不会变好……」。原因是这些文案里写了 markdown 强调，
    而界面上是**当纯文本渲染**的（不解析 markdown）⇒ 用户看到字面 `**`。
    全仓扫了一遍（`scan_markdown2.py`：只取**非 docstring 的字符串常量**）：
    - 会显示给用户的 6 处：`services/platform_errors.py` 的
      `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` / `NO_PERMISSION` 两条人话；
      `services/drafts.py` 的 `list_key_missing` 与 `no_per_item_report` 两条 warning；
      `frontend/src/pages/ItemTemplates.tsx`（删除模版 / 设置运费两个确认框）、
      `frontend/src/pages/Orders.tsx`（改地址 404 提示）、
      `frontend/src/pages/SecurityCenter.tsx`（申诉说明，那里还写了字面反引号 `` `confirm` ``）。
    - 故意**不动**的：`agents/tools.py` 的工具 description 与 `agents/defaults.py` 的
      主管提示词 —— 那是给**模型**看的，markdown 强调在那里是有效的，而且有测试逐字钉着。
    改法：用户侧一律改写成 `<b>`（前端）或直接去掉星号（后端文案），并在
    `platform_errors.py` 的注释里写明「hint 会原样出现在界面上，不能写 `**加粗**`」。

> **第三十批证据链**：`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_md_fix`）→
> **`TOTAL 50  PASS 50  FAIL 0  CASES 1588  SKIPPED 2`**；
> `backend/tests/test_platform_errors.py` → `Ran 32 tests in 0.002s OK`；
> 前端 `cd frontend && npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（89 modules，
> `index-BZVYw-fj.css 136.44 kB` / `index-C1ns98T1.js 529.44 kB`）。
> 打包版复验（exe `10,028,158` 字节 / 2026-10-13 05:09:47，`--port 18400` `/api/health` 200
> `frozen:true`）：`drafts_tab_shots.py` → **`PROBLEMS: []`**、`page_errors: []`、
> `non-GET api requests: []`，截图 `logs/drafts_tab_shots/02-drafts-tab.png` 里那句已是
> 「这不是登录掉线」；`probe_write_gate.py` → **`GATE FAILURES: 0 / 30`**；
> `packaged_ui_smoke.py` → **26/26 页通过**；`check_assets.py` → `ASSETS_MATCH=True`
> （`index-BZVYw-fj.css` + `index-C1ns98T1.js`，remote=local）。
> 诚实边界：这一批只修「看得见的字面 markdown」，**没有**做全面文案润色；
> 草稿箱 tab 的截图复核是在**本机账号无草稿权限**的状态下做的（列表显示的是平台的拒绝提示，
> 有草稿时的表格 / 批量选择 / 删除确认框仍只经过类型检查与页面级冒烟）。

### 第三十一批：客服分流写能力（读 + 写）（180~184）

180. **官方能写、我们只读的又一处**。小铺域的覆盖审计（`shop_domain_apis.py` 扫 5 份本地
     bundle 副本，零平台请求）发现客服分流微应用里还有两条写接口没做：
     `mtop.taobao.idlemessage.customer.routing.rule.operate`（保存分流规则）与
     `mtop.taobao.idlemessage.customer.group.operate`（客服分组增/改/删）。
     参数形状全部来自 bundle 字符偏移取证（`cs_enums.py` / `cs_logic.py` / `cs_enums2.py`，
     bundle 副本 `C:\Users\Administrator\.dsh-tmp\csdispatch\main.js`，1,168,301 字符单行）。
181. **`repeatCustomer` 是枚举字符串，不是布尔**（差点猜错）。官方加载路径是
     `Ay[c.repeatCustomer]`，而 `Ay` 是「枚举值 → 中文」表且只登记了 4 个键
     ⇒ 它与 `distributionMethod` 共用同一套值（`LOW_SATURATION_FIRST` /
     `LOW_DAILY_COUNT_FIRST` / `LAST_SERVED_AGENT` / `COMBINED_LOW_LOAD`）。
     官方表单可能只给子集，但 bundle 里**没有**可引用的子集定义 ⇒ 不缩窄，非法值由平台拒。
182. **分组 `mode` 两侧大小写不一致**：官方写侧枚举是**小写**
     （`UNSPECIFIED="unspecified"` / `SPECIFIED="specified"`），渲染判定却用大写
     （`"SPECIFIED"===t.mode` / `"DEFAULT"===t.mode`）。没有平台侧证据判断哪个对
     ⇒ **原样透传调用方给的值、不做大小写转换**（`GROUP_MODES` 两种写法都收），
     并把这条不确定性写进协议层 docstring 与契约。
183. **★ 假成功：只看外层信封会把平台拒绝当保存成功**。`xianyu/trade.py::envelope_error`
     只判客户端层 `success is False`，而真机存在「外层成功、内层 `data.code` 失败」的
     双层信封（只读侧早有证据：`services/shop.py::_unwrap` 与
     `test_shop_api.py::test_sub_accounts_business_error_inside_envelope_is_502`）。
     `shop_write.py` 因此自建 `envelope_error`：客户端层与业务码**两层都看**
     （业务码只认 `success`，大小写不敏感；没有 `code` 键时以客户端结论为准 ——
     官方前端根本不读这两个响应体，成功体可能是空对象）。
     这是首跑测试 `test_platform_business_error_inside_envelope_is_502` 报 `200 != 502` 暴露的。
184. **两个小口径**：① `group_save_request` 的 `customerUid` 空串要先 `strip()` 再判空
     （`_text('  ')` 不 strip，首跑测试因此拿到 `[{'customerUid': 111}, {'customerUid': '  '}]`）；
     ② 这两条 mtop **继续留在 `xianyu/shop.py::WRITE_APIS` 黑名单**里 ——
     黑名单管的是「只读路径一条都不许碰」，写路径走 `shop_write.py` 自己的白名单，
     于是 `test_no_write_api_is_ever_called` 仍然有效。

> **第三十一批证据链**：`backend/tests/test_shop_write_api.py` → **`Ran 60 tests in 0.641s OK`**（`EXIT=0`）；
> `backend/tests/test_shop_api.py` → `Ran 32 tests OK`；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_shop_full2`）→
> **`TOTAL 51  PASS 51  FAIL 0  CASES 1648  SKIPPED 2`**；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests in 3.723s OK`**（`EXIT=0`）；
> `scripts/reconcile_tools.py`（`XIANYU_APP_DIR=%TEMP%\xywb_recon_lead83`）→
> `specs=84 master=83 routes=85 targets=64 handlers=84`、`write_tools=32`、`route_checked=85`、
> **`RECON OK`**；前端 `npx tsc --noEmit` EXIT=0、`npm run build` EXIT=0（89 modules、
> `index-DdzdJ7mX.css 137.08 kB` / `index-B1_YjI2o.js 542.32 kB`）；
> `scripts/ui_smoke.py --port 18431` → **`UI SMOKE ALL OK (26/26 页通过)`**。
> 打包版复验（exe `10,041,768` 字节 / 2026-10-13 06:10:55，`--port 18400` `/api/health` 200
> `frozen:true`）：`probe_write_gate.py`（已扩到 **33 个写端点**）→ **`GATE FAILURES: 0 / 33`**
> （三条新端点未确认一律 400 `confirmation_required`，确认腿 400 `invalid_fields`
> = 字段校验在建客户端之前；语义只读的 `sec.appeal_form` 仍 400 `invalid_fields`）；
> `packaged_ui_smoke.py` → **26/26 页通过**；`check_assets.py` → `ASSETS_MATCH=True`
> （`index-DdzdJ7mX.css` remote=local=`83fbc40b7015`、`index-B1_YjI2o.js` remote=local=`b1644edcc78b`）；
> **新探针 `cs_routing_shots.py`**（只开弹窗、不点最终确认）→ `PROBLEMS: []`、`page_errors: []`、
> **`non-GET api requests: []`**，截图 `logs/cs_routing_shots/03-rule-confirm.png` 里确认框写着
> 「这一条会**真的**改动闲鱼上的客服分流配置（不是只改本地）。账号：2217970177348」
> 并逐条列出本次提交的字段（`distribution_method` / `repeat_customer` / `offline_message` /
> `confirm = true`）。
> 诚实边界：**客服分流的写接口一次真机请求都没发过**（红线），三条 POST 只验到
> 「路由已注册 + body 被端点接受 + confirm 闸门拦得住 + 界面不点确认就不发请求」；
> 官方的 `routingRules` / `csGroupInfos` **回显形状没有真机样例**，所以 `rule` / `group_id`
> 拿不到时一律 `null` + 中文 warning（不造值）；分组 `mode` 若读侧回显 `DEFAULT`，
> 页面上需要手选（页面只给 `unspecified` / `specified` 两个选项）。


### 第三十二批：店铺资料 + 子账号（读 + 写）（185~191）

- **店铺资料**：`mtop.alibaba.idle.shop.manage.detail`（读）+ `.nick.update` / `.desc.update` / `.avatar.update`（写）。
  官方读侧返回**扁平**结构（`userNick`/`shopDesc`/`userAvatar`/`industry`/`alipayAccount`/`businessType`/
  `proShopValidTime`），写侧成功判据是 `result.success`、失败读 `result.data.message` —— 与子账号族
  （看 `data.code == 'success'` / `msg`）**不是同一套信封**，所以协议层按动作分别判。
  昵称 `trim` 后 4~12 字、头像必须 `http(s)://`、简介非空（官方没有长度上限）。
- **子账号**：`...platform.user.role.list`（读）+ `...platform.common.sub.account.register` /
  `...platform.sub.account.{delete,mobilephone.update,role.update,password.update}`（写）+
  `...platform.sub.account.sub.pk`（取加密公钥）。手机号 `^1[3-9]\d{9}$`（先去空格与横线）、
  密码 `^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,15}$`、岗位去重保序上限 50。
  **密码由本机服务端加密**：`xianyu/sub_account.py::encrypt_password` 用纯 Python 实现
  RSAES-PKCS1-v1_5（`secrets` 造非零填充），前端明文只发到本机后端；**日志 / 响应 / 事件都不含密码**；
  公钥拿不到 ⇒ 502 且**零提交**（不拿明文去试）。
- **10 条 HTTP**：`GET /api/shop/{account_id}/profile`、`POST …/profile/{nick,desc,avatar}`、
  `GET /api/sub-accounts/{account_id}/roles`、`POST /api/sub-accounts/{account_id}`、
  `POST …/{sub_id}/{delete,mobile,roles,password}`。两条只读**故意没有 confirm**；八条写都在闸门内。
- **10 个 AI 工具**（2 读 + 8 写）：`shop_profile_get` / `sub_accounts_roles_list`（只读）+
  `shop_profile_update` / `sub_accounts_create` / `sub_accounts_delete` / `sub_accounts_update_mobile` /
  `sub_accounts_update_roles` / `sub_accounts_update_password`；MCP 侧 8 条 POST 的 body 都含 `confirm`。
- **UI**：新增「店铺资料」页（`frontend/src/pages/ShopProfile.tsx` + `shopprofile.css`，第 27 页，
  挂进 `App.tsx` 的「小铺」组），`SubAccounts.tsx` 加写入口（新建 / 删除 / 改手机号 / 改岗位 / 改密码，
  写密码时只发明文到本机后端），`subaccounts.css` 追加 `sa-w-` 前缀样式段。
- **修掉的真缺陷**：① `xianyu/sub_account.py::assert_write` 原先判
  `api not in ALL_APIS - set(READ_APIS)`，会把 `INFO_LIST_API` / `SUB_PK_API` / `PERMISSION_TREE_API`
  这三个「既非只读白名单也非写接口」的 API 放行 ⇒ 改成只认 `WRITE_API_SET`（`WRITE_APIS` 里各动作的 api）；
  ② `common.log_operation` 的 action 列取 `kind or action`，`shop_profile.py` / `sub_accounts.py` 原先传了
  `kind='shop'` ⇒ 操作日志全显示成 `shop`、看不出改的是哪一项 ⇒ 去掉全部 7 处 `kind`。

> **第三十二批证据链**：`backend/tests/test_shop_profile_api.py` → **`Ran 47 tests in 0.549s OK`**（`EXIT=0`）；
> `backend/tests/test_sub_accounts_api.py` → **`Ran 63 tests in 0.7s OK`**（`EXIT=0`，内嵌 1024 位测试密钥、
> 断言 PKCS#1 v1.5 能解回明文、断言密码不出现在响应与日志里）；
> `backend/tests/test_drafts_api.py` → **`Ran 47 tests in 0.6s OK`**；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_sa_full3`）→
> **`TOTAL 53  PASS 53  FAIL 0  CASES 1758  SKIPPED 2`**（加入 `test_services_common.py` 后为 54 / 1762）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server backend.tests.test_agents_tools_workbench`
> → **`Ran 140 tests ... OK`**；`scripts/reconcile_tools.py` →
> `specs=94 master=93 routes=95 targets=74 handlers=94`、`write_tools=40`、`route_checked=95`、**`RECON OK`**；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`（91 modules、
> `index-DAKrof5s.js 608061` / `index-BAUr5YpJ.css 139919`）；
> `scripts/ui_smoke.py --port 18451` → **`UI SMOKE ALL OK (27/27 页通过)`**。

### 第三十三批：推广资金明细（流水 / 可操作项 / 充值详情 / 提现详情，全部只读）（192~197）

- **动机**：用户截图里有「充值 / 提现 / 账户流水」入口，而官方工作台把这些放在超级擦亮微应用里。
  先按老规矩**只从本地 bundle 取证**（`C:\Users\Administrator\.dsh-tmp\find_recharge.py`、
  `recharge_payloads.py`、`recharge_detail_fields.py`，零平台请求）。
- **取证结论（决定了这一批的边界）**：`mtop.taobao.idle.ad.bp.account.{recharge,withdraw}.query`、
  `bp.account.log.query`、`bp.account.log.operates`、`bp.settle.settle.query` 全是**查询**；
  **整个 bundle 里不存在「发起充值 / 发起提现」的 mtop 接口**（`log.operates` 也只是「查询可操作项」）
  ⇒ 钱的实际划转在支付宝侧完成，本工作台**只查询、不下单**，这一批零资金写风险。
- **4 条 HTTP（全 GET）**：`/api/promotion/{account_id}/funds`、`/fund-operations`、
  `/recharge/{order_id}`、`/withdraw/{order_id}`。
  - `funds` 走 `bp.account.log.query`（payload 是 `{requestJson}`，官方默认 `pageNum=1`/`pageSize=10`、
    日期取近 7 天、`accountType=0`、`operateType=0`）；`operate_type` 只认 0/1/2/3
    （全部 / 现金 / 礼金 / 红包），越界 400 `invalid_fields`。
  - **`funds` 拿不到列表时是 `null` 而不是 `[]`** —— 前端据此显示「既不是『没有流水』也不是 0」的告警，
    这是本项目「null ≠ 0」口径在列表上的延伸（`[]` 会被用户读成「确实没有」）。
  - 充值/提现详情各自映射官方状态枚举（ING/SUCCESS/FAIL）的中文原话，`detail` 原样带回平台字段。
- **4 个 AI 工具（全只读，无 confirm）**：`promotion_funds` / `promotion_fund_operations` /
  `promotion_recharge_detail` / `promotion_withdraw_detail`；`TOOL_SPECS` 94 → **98**、
  `MASTER_TOOLS` 93 → **97**、`TOOL_ROUTES` 95 → **99**，写工具仍是 **40**（本批一个写接口都没加）。
- **UI**：`frontend/src/pages/Promotion.tsx` 加第 5 个标签「资金明细」（类型筛选 + 本地分页 + 流水表 +
  逐行「查看详情」弹窗）；`account` 标签上那句「平台没有网页版下单接口」的说明改成指向新标签。
  新增 `flowTime()` 把平台的 `20260901120000` 显示成 `2026-09-01 12:00:00`（认不出来原样显示，不猜）。

> **第三十三批证据链**：`backend/tests/test_promotion_api.py` → **`Ran 45 tests ... OK`**（新增
> `PromotionFundsTest` 20 条，含 `funds=null` 与 `funds=[]` 的区别、`operate_type` 越界 400、
> 空 `order_id` 400 且零平台请求）；`python -m unittest backend.tests.test_promotion_api
> backend.tests.test_agents_cross backend.tests.test_mcp_server backend.tests.test_agents_tools_workbench`
> → **`Ran 175 tests in 4.472s OK`**（`UNITS_EXIT=0`）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_promo_full`）→
> **`TOTAL 54  PASS 54  FAIL 0  CASES 1782  SKIPPED 2`**；`scripts/reconcile_tools.py` →
> `specs=98 master=97 routes=99 targets=78 handlers=98`、`write_tools=40`、`route_checked=99`、**`RECON OK`**；
> `create_app().openapi()` → **163 条路径**，4 条新路由全在；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`（92 modules、
> `index-BghE5P7m.js 615151` / `index-BAUr5YpJ.css 139919`）；
> `scripts/ui_smoke.py --port 18461` → **`UI SMOKE ALL OK (27/27 页通过)`**。
> **打包版复验**（`build_exe.ps1` EXIT=0 → `dist\闲鱼AI工作台\闲鱼AI工作台.exe` = 10,072,080 字节 /
> 2026-10-13 09:12:01，`--port 18400` `/api/health` 200 `frozen:true`）：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 41`**；`packaged_ui_smoke.py` →
> **`PACKAGED UI SMOKE 27/27 页通过`**；`check_assets.py` → `GET /` 引用
> `['index-BAUr5YpJ.css','index-BghE5P7m.js']`、remote=local（`9b6a69149412` / `a9173841f002`）、
> **`ASSETS_MATCH=True`**；`C:\Users\Administrator\.dsh-tmp\funds_tab_shots.py`（只点「资金明细」标签，
> 不点任何确认）→ `PROBLEMS: []`、`page errors: []`、**非 GET 请求 0 条**。

### 第三十四批：商品数据页（官方最后一个没复刻的叶子页，只读）（198~202）

- **动机（覆盖审计）**：把官方菜单树（`logs/seller_discover/menu.json` 的 `accessiblePathList`，
  7 组 19 个叶子页）与我们已复刻的 27 页逐条对照，**只剩 `/seller-data/commodity`（商品数据）
  没有对应页面** —— 它一直在官方「数据」组里，而我们的数据组只有总览/粉丝/客服三页。
- **取证（零平台请求）**：全部来自本地 bundle 副本 `logs/seller_discover/datacenter_main.js`
  （4,168,486 字符，官方 `idle-seller-data` 微应用）：
  - 列表 `mtop.alibaba.idle.seller.pc.datacompass.item.list`（`data:{...}`，响应读
    `e.data.data.list` + `e.data.data.total`）；列定义
    `…pc.datacompass.item.indicators`（响应 `e.data.data[].subFields[]`，拍平成
    `{name,title,type}`，官方还把它缓存进 `localStorage['commodityColumns']`）。
  - **调用参数逐个钉住**：`{selectedSellerId, tabType, dateRange, ms, dateType, categoryInfoStr,
    itmName, itmId, isDpStk, mktActId, page, pageSize, orderBy, order}`；`dateRange` 只在自定义
    区间时给（`join('|')`）、`ms` 只在按月时给、`categoryInfoStr` 是**JSON 字符串**、
    `pageSize` 官方给 10/20/50、排序 `orderBy`=列名 + `order`=asc/desc。
  - **官方渲染规则**：第一行该列的值 `< 1` 时整列渲染成「原样数字 + `%`」（**不乘 100**）——
    我们照抄，并在列头 `title` 里写明，避免用户以为是我们算错。
  - 官方这一页的**导出/下载**接口（`…export.item.list` 一族 + 轮询 `excel.url.get`）
    **故意不做**：它把整份数据导成本地文件，属于数据外带，要单独评估。
- **4 处落盘**：
  - 协议层 `backend/app/xianyu/datacenter.py`：`COMMODITY_LIST_API` / `COMMODITY_INDICATORS_API`
    + `commodity_request()` / `parse_commodity_list()` / `parse_commodity_columns()`。
  - 服务层 `backend/app/services/datacenter.py`：`commodity(account_id, start, end, item_id,
    item_name, page, size, order_by, order)`（**默认区间是今天**，跟官方一致，与另外三页
    「今天往前 7 天」故意不同；`items`/`columns` 拿不到给 `null`、`total`/`pages` 认不出给 `null`）。
  - 路由 `GET /api/datacenter/{account_id}/commodity`（挂载在既有的 `datacenter` 模块里）。
  - AI 侧 1 个只读工具 `datacenter_commodity`（无 `confirm`）+ 1 条 MCP GET 路由
    （query 8 个键：`start/end/item_id/item_name/page/size/order_by/order`）。
- **UI**：新增第 28 页「商品数据」（`frontend/src/pages/CommodityData.tsx` + `commodity.css`，
  `cm-` 前缀，94 modules）：账号选择 + 日期（今天/近 7 天/近 30 天）+ 商品 ID/标题筛选 +
  **动态列**（列名与列数都由平台给，我们不自造）+ 列头点击排序（升/降/取消三态）+
  本地分页（10/20/50）+ 商品信息列 sticky 在最左（缩略图 + 标题 + 商品 ID）。
  三个「诚实」分支：`items === null`（平台没返回行）与 `items: []`（真的没有商品）文案分开；
  `columns` 缺失/空数组时退回「按第一行的键显示」并说明「这些列头是字段名，不是官方中文标题」；
  拿不到的单元格显示 `-` 并压暗，与真实的 0 在视觉上分开。

> **第三十四批证据链**：`backend/tests/test_datacenter_api.py` → **`Ran 89 tests ... OK`**
> （新增 `CommodityTest` 18 条：默认今天、8 个 query 透传、`items=null` 与 `items=[]` 的区别、
> `total=0` 保留而缺失给 null、`columns` 缺失/空、页码与 size 越界 400 且零平台调用、
> 平台拒绝 502 且 `login_state` 仍 active、无 Cookie 409、未知账号 404、路由挂载形状）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests in 3.645s OK`**（`UNITS_EXIT=0`）；
> `scripts/reconcile_tools.py` → `specs=99 master=98 routes=100 targets=79 handlers=99`、
> `write_tools=40`（本批只加只读工具）、`route_checked=100`、**`RECON OK`**；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_cm_full`）→
> **`TOTAL 54  PASS 54  FAIL 0  CASES 1827  SKIPPED 2`**；
> `create_app().openapi()` → **164 条路径 / 177 个操作**（第三十三批 163 条）；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`
> （94 modules、`index-CaV4g4Pd.js 624764` / `index-DotwG7pK.css 141937`）；
> `scripts/ui_smoke.py --port 18471` → **`UI SMOKE ALL OK (28/28 页通过)`**；
> `scripts/contrast_audit.py --port 18473 --threshold 4.0` → **`[dark] BAD_PAGES 0/28`、
> `[light] BAD_PAGES 0/28`**、`NON_GET_API_REQUESTS=[]`；
> `scripts/a11y_audit.py --port 18475` → 无可访问名字的按钮 0 / 没有标签的输入框 0 /
> 无 alt 的图片 0 / 焦点环全部可见、`NON_GET_API_REQUESTS=[]`。

### 第三十五批：数据总览页四张经营卡片（只读）（203~207）

- **动机**：第三十四批把官方菜单树的 19 个叶子页对齐完，但**数据总览页本身只做了「核心指标」一块**——
  官方这一页在核心指标下面还有四张卡（交易数据 / 经营概况 / 复购 / 退款概览），各是一个独立接口。
  本批照抄官方结构补齐这四张，**全部只读**。
- **取证**（`logs/seller_discover/datacenter_main.js`，静态提取，零平台请求）：
  `mtop.alibaba.idle.seller.pc.datacompass.flow.detail`（交易数据，偏移 4077870）、
  `…singleuser.seller.summary`（经营概况，4062125）、`…singleuser.repurchase.summary`（复购，4067298）、
  `…refund.summary`（退款概览，4099924）。四张卡**共用同一套入参形状**
  （`{dateRange: customDate ? dates.join('|') : undefined, dateType, selectedSellerId, ms}`），
  与商品数据页那套（`dateRange`/`ms` 条件拼接）同源。
  另注：`platform.datacompass.refund.summary`（4121925）只出现在一个**调试面板**里
  （旁边是 `platform.account.mock.cancel`、写死 `selectedSellerId:"1001"`/`ms:"202506"`）⇒ **不是真页面，不做**；
  `platform.datacompass.refund.item.list`（退款原因分析表格）是真页面，**留给后续批次**。
- **协议层**（`backend/app/xianyu/datacenter.py`）：`CARD_APIS`（四个 kind → api）、`CARD_KINDS`、
  `CARD_TITLES`、`SELLER_LABELS`/`SELLER_LABELS_SINGLE`/`REPURCHASE_LABELS`/`REFUND_LABELS`（官方中文标题表，
  逐字抄自 bundle）、`card_dates()`、`card_request()`、`card_labels()`、`parse_card(payload, kind, labels)`
  与 `async def card(client, kind, start, end, seller_id)`。
- **服务层**（`backend/app/services/datacenter.py`）：`CARD_KINDS`、`_card_dates`（区间校验，非法 400 且零平台调用）、
  `_wrap_card`（`metrics`/`banner`/`series` 拿不到一律 `[]`；平台没登记过的字段进 `extra` 原样带出）、
  `card(account_id, kind, start, end)`。**一次一张卡**：某张没权限/报错只影响那一张，
  另外三张照常显示（官方页面结构如此，前端也用四个独立 `useAsync`）。
- **HTTP**：新增 `GET /api/datacenter/{account_id}/card/{kind}`（`kind` 不在枚举 → 400 `invalid_fields`）。
- **AI 侧**：新增只读工具 `datacenter_card`（无 `confirm`）+ MCP 路由
  `GET /api/datacenter/{account_id}/card/{kind}`（path_params `account_id`/`kind`，query `start`/`end`）。
- **UI**：`frontend/src/pages/DataOverview.tsx` 在核心指标卡下面加「经营卡片」区（四个 `CardBlock`，
  `dc-opcards` 网格；指标名**以平台为准**——`label` 为 null 时显示平台字段名，**不自己编中文**；
  平台没给的值显示「-」不补 0；折线跳过非数字点**不插 0**，插 0 会画出假的谷底；
  未登记字段折叠在「平台原始字段」里）+ `datacenter.css` 追加 `dc-opcard`/`dc-spark`/`dc-metric-row` 等规则。

> **第三十五批证据链**：`backend/tests/test_datacenter_api.py` → **`Ran 145 tests ... OK`**
> （新增 `CardTest`：四个 kind 的接口名与标题、区间校验 400 且零平台调用、`kind` 非法 400、
> 指标名回落平台字段名、`extra` 原样带出、`metrics`/`banner`/`series` 缺失给 `[]`、
> `real_date_range` 给 null、**平台拒绝 → 200 + 人话 warning（不是 502，且不许再顺嘴说「可能改了字段名」）**、
> 无 Cookie 409、未知账号 404、路由挂载形状）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests OK`**（`UNITS_EXIT=0`）；
> `scripts/reconcile_tools.py` → `specs=100 master=99 routes=101 targets=80 handlers=100`、
> `write_tools=40`（本批只加只读工具）、`route_checked=101`、**`RECON OK`**；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_card_full2`）→
> **`TOTAL 54  PASS 54  FAIL 0  CASES 1883  SKIPPED 2`**；
> `create_app().openapi()` → 5 条 `/api/datacenter/*`（含新 `card/{kind}`）；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`
> （94 modules、`index-C43KMt5l.js 629169` / `index-CMIURv5_.css 143354`）；
> `scripts/ui_smoke.py --port 18481` → **`UI SMOKE ALL OK (28/28 页通过)`**
> （`data-overview` 653 字符，截图里四张卡各带自己的错误提示 —— 演示账号无 Cookie，
> **四张卡的错误是彼此独立的**，页面没有因此空白）。

### 第三十五批补：真机探针抓到「同一页两套错误口径」（208）

**动机**：第三十五批的四张卡片只有单元测试和 bundle 证据，**一次真机请求都没发过**（写在「未验证」里）。
真机只读探针一跑，抓到的不是「卡片能不能用」，而是**同一页上两套互相矛盾的错误口径**。

- **探针**：`C:\Users\Administrator\.dsh-tmp\cards_read_probe.py`（把真实数据目录拷到临时根 →
  源码起后端 → 桌面密钥换 token → 只发 GET；报告 `logs/cards_read_probe.json`）。
- **真机结果**：账号 `2217970177348` 当时 `login_state='active'`（用户 2026-10-13 01:22 重新扫过码）
  ⇒ 四个接口**都真打到了平台**，且**都没有 `FAIL_SYS_API_NOT_FOUNDED`**（接口名是对的），
  但四个 kind 一致回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`
  —— 与数据总览/粉丝/客服**同族**（这个账号没有「数据」权限）。
- **抓到的缺陷**：协议层 `card()` 对平台拒绝**直接抛** ⇒ 服务层 502 ⇒ 界面上四张卡
  各是一个红框、框里糊着 `协议层调用失败: MTOP 调用失败 [mtop.alibaba.idle.seller.pc.datacompass
  .flow.detail]: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` 的**平台原文**；
  而**同一页**的核心指标块却是「一句人话 + 一屏 `-`」（那是第三十三批给只读路径统一加的
  `platform_errors.decorate`）。同一页两套口径，用户看到的就是「一块好好说话、四块甩原文」。
- **为什么单元测试没抓住**：假协议层**早就按「回 `errors` 不抛」写**（`install_fake_protocol`
  里 `card` 的实现），服务层测试因此一直是绿的；只有**协议层真函数**在真机那条路径上会抛。
  ⇒ 补的三条测试必须**直接调 `datacenter_proto.card()`**（用 `FakeMtopClient(errors=…)`）：
  无权限 → `found=False` + `errors['trade']` 带原文；**链路/风控故障照抛**（`FAIL_SYS_TRAFFIC_LIMIT`）；
  **登录态过期照抛**（`common.call_failed` 按类名认 `SessionExpiredError` → 409 + 回写 `need_relogin`）。
- **修法**（两处，都不改 HTTP 形状）：协议层 `card()` 把「平台拒绝访问」这一类
  （`UNAUTHORIZED` / `NO_PERMISSION` / `无权限`）收进返回值的 `errors`，不再抛；
  服务层 `_LABELS` 把四个 `kind` 翻成中文（交易数据/经营概况/复购/退款概览），
  并且**有权限类 warning 时不再追加 `WARN_CARD_EMPTY`**（「没权限」和「平台可能改了字段名」
  同时出现会把人往错方向带）。
- **真机复验口径**：`cards_read_probe.py` 与 `read_sweep.py`（41 条只读端点、9 处平台拒绝）
  证明**所有平台拒绝都被 `decorate` 成人话**（「不是登录掉线，重新扫码不会变好」），
  界面文案与真机一致；四张卡的指标真值仍然拿不到（账号无数据权限），
  所以「卡片能不能显示真数据」**依旧未验证**，只是**失败姿势与同页其它块一致了**。

> **第三十五批补证据链**：`backend/tests/test_datacenter_api.py` →
> **`Ran 145 tests in 3.731s OK`**（+4：`test_card_permission_denied_returns_errors_instead_of_raising`、
> `test_card_other_failures_still_raise`、`test_card_session_expired_still_raises`、
> `test_service_warnings_have_chinese_labels_for_cards`；并修好一处**测试自身**的
> `NameError`：`test_service_warnings_use_chinese_labels_not_internal_names` 断言被上一次编辑
> 挂到了隔壁函数体里）；`backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_card_full2`）→
> **`TOTAL 54  PASS 54  FAIL 0  CASES 1883  SKIPPED 2`**；
> 真机：`cards_read_probe.py` → 修复前四个 kind 全 502（界面四块红框 + mtop 原文），
> 修复后**全 200 + 一句人话 warning**（`平台拒绝访问交易数据接口：这个账号没有「数据」权限，
> 对应指标显示 -。换个有数据权限的卖家账号再试。`，四个 kind 的中文名都对，
> 且**没有**再追加「平台可能改了字段名」那条）；`read_sweep.py` → 41 条只读端点、
> `SERVER_ERRORS 9` **全是平台拒绝**、无 500、无解析异常。
> 重打包（**10,084,760 B / 2026-10-13 11:12:35**）后打包版复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 41`**；`packaged_ui_smoke.py` → **28/28 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-C43KMt5l.js` / `index-CMIURv5_.css`）；
> `scripts/contrast_audit.py` → `[dark] 0/28`、`[light] 0/28`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、`焦点不可见的页面：[]`、`NON_GET=[]`；
> `cards_shots.py` → 卡片区 343 字符、`PROBLEMS: []`，深浅两套截图里四张卡各显示
> 「这一张暂时没有数据。」+ 那句人话 warning（与同页核心指标同一姿势）。

### 第三十六批：客服数据页三张明细表（只读）（209~213）

**动机**：第三十五批补完「数据总览」的四张经营卡片后，数据组只剩官方「客服数据」页的三张表没做 ——
官方那一页在 6 个指标下面还有「客服明细 / 订单分流明细 / 客户评价明细」三张表，各是一个独立接口。
至此官方菜单树 7 组 19 个叶子页在**页面结构**上全部有对应实现（不是「接口全部拿到过真数据」，见 §四）。

**取证（零平台请求，bundle = `logs/seller_discover/datacenter_main.js`，4,168,486 字符单行）**：
- 三个接口名逐字：`mtop.alibaba.idle.seller.pc.datacompass.cs.detail.list` /
  `…cs.diversion.list` / `…cs.evaluation.list`（官方包装 `wge(e,t)` 带 in-flight 去重缓存 `_ge`）。
- 入参 8 个：`{selectedSellerId, dateRange, ms, dateType, orderBy, order, page, pageSize}`，
  `dateRange` 只在 `dateType === 'customDate'` 时 `dates.join('|')`（竖线）、
  `ms` 只在 `dateType === 'month'` 时 `dates[0]` —— 与经营卡片/商品数据同一套约定。
- 响应是**双层 `data`**：官方前端写的是 `resp.data.data.data.list`（`extendInfo.realDateRange`
  在同层），且先判 `resp.data.data.code === 'success'`。列表键缺失时官方默认 `[]`，
  我们**故意保留 `None`**（「平台没给列表字段」与「真的没有数据」必须分得开）。
- 列名与中文标题**逐字取自官方表头**：客服明细 12 列（`acctName`「账号名称」…`respRate1min`
  「1分钟响应率」）、分流明细 3 列（`divTp`「分流类型」/`dtlTp`「明细分类」/`sessCnt`「会话数」）、
  评价明细 6 列（`buyerUserName`「客户名称」/`buyerNick`/`subUserName`/`reviewTime`/
  `reviewScore`/`dissatReason`「不满意原因」）。**不翻译、不增删** —— 平台加列就多一列。

**落盘（4 处）**：
- `backend/app/xianyu/datacenter.py`：常量 `CS_DETAIL_API` / `CS_LIST_APIS` / `CS_LIST_KINDS` /
  `CS_LIST_TITLES` / `CS_LIST_COLUMNS` / `CS_LIST_PAGE_SIZES` / `CS_LIST_DEFAULT_PAGE_SIZE` /
  `CS_LIST_MAX_PAGE_SIZE`；`cs_list_request()`、`parse_cs_list()`、`async cs_list()`。
  **平台「拒绝访问」是有效答复**（与同页另外三块同一口径）：`_is_permission_error` 命中时不抛，
  把 `errors[kind]` 带回服务层 → 200 + 人话 warning；登录态过期与其它故障**仍然照抛**。
- `backend/app/services/datacenter.py`：`CS_LIST_TITLES` / `WARN_CS_LIST_MISSING` / `_int_arg()` /
  `_wrap_cs_list()` / `cs_list()`。`rows` 认不出保持 `None`；`total` 认不出 `None`（**0 保留**）；
  `pages = ceil(total / size)`；日期为空时 `period` 显示今天但**线上参数仍是不带日期的 `recent1d`**
  （显示值不能反过来决定线上参数）。已经有权限 warning 时**不再补**「可能改了字段名」那条。
- `backend/app/api/datacenter.py`：`GET /{account_id}/cs/{kind}`（`page`/`size` 用 `Query(ge/le)`，
  越界 422；`order` 取值与 `kind` 白名单在服务层 → 400 `invalid_fields`）。
- 前端 `frontend/src/pages/CsData.tsx`：从「只有 6 个指标」扩成 4 个 tab（客服指标 / 客服明细 /
  订单分流明细 / 客户评价明细），三张表共用一个 `CsListTable`（列取 `columns`、`null` 单元格显示 `-`、
  `rows === null` 与 `[]` 文案分开、本地分页、`useAsync` 不轮询）；`datacenter.css` 追加
  `.dc-tabs/.dc-list-*/.dc-table/.dc-cell-null/.dc-pager`。AI 侧加 1 个只读工具 `datacenter_cs_list`
  + MCP GET 路由（`test_agents_cross` 工具名清单、`test_agents_tools_workbench` 的 `NEW_TOOLS`、
  `mcp_server/client.py` 的 `TOOL_ROUTES` 三处同步）。

**首跑 8 处失败（都是我写的测试/假件的问题，已修）**：① 假协议层照抄一份死 payload ⇒
「三张表各自的列」永远相等（假绿）⇒ 改成**按请求的 `kind` 重建** `kind`/`columns`；
② `FakeMtopClient(errors={'mtop': …})` —— 假客户端的 `errors` 是**按接口名**索引的，
所以三条「权限拒绝/其它失败/登录过期」的协议层测试全都没触发异常 ⇒ 改成
`errors={datacenter_proto.CS_LIST_APIS['detail']: …}`；③ `args_of('cs_list')` 记的是
`(client, kind, start, end, seller_id, order_by, order, page, page_size)`，索引写成 `[9]`/`[8:10]`
⇒ 改成 `[8]`/`[7:9]`；④ `page=0` / `size=999` 由 FastAPI 的 `Query(ge/le)` 先拦成 **422**
（不是服务层 400）⇒ 断言改成 422 + 保留 `order` 的 400。

> **第三十六批证据链**：`backend/tests/test_datacenter_api.py` → **`Ran 195 tests ... OK`**
> （139 → 195，+56：`CsListTest` 的协议 11 条 + 服务/HTTP 17 条，其中 `test_route_is_mounted_on_the_real_app`
> 用 `create_app().openapi()` 而不是手写遍历 —— 子路由被 `_include_router` 包在 `path=None` 的
> `_IncludedRouter` 里，手写遍历一条都走不到、用例会变成永远通过的空断言）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests ... OK`**、`UNITS_EXIT=0`；
> `scripts/reconcile_tools.py` → `specs=101 master=100 routes=102 targets=81 handlers=101`、
> `write_tools=40`、`route_checked=102`、**`RECON OK`**、`RECON_EXIT=0`；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_cslist_full`）→
> **`TOTAL 54  PASS 54  FAIL 0  CASES 1933  SKIPPED 2`**（1883 → 1933）；
> `create_app().openapi()` = **166 条路径 / 179 个操作**、`MCP_TOOL_ROUTES` = 102；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`
> （94 modules、`index-C5sZJrHk.js 576.97 kB` / `index-CE1STJ7C.css 143.83 kB`）；
> `scripts/ui_smoke.py --port 18491` → **`UI SMOKE ALL OK (28/28 页通过)`**；
> `scripts/contrast_audit.py --port 18501 --threshold 4.0` → `[dark] 0/28`、`[light] 0/28`、
> `NON_GET=[]`；`scripts/a11y_audit.py --port 18503` → 三类问题全 0、`焦点不可见的页面：[]`、`NON_GET=[]`；
> 视觉复核 `cs_list_shots.py`（源码起后端、只切 tab、**不点任何写按钮**）→ 4 个 tab 名称逐字正确、
> 每个 tab 有独立标题与只读说明、`page errors: []`、`non-GET api requests: []`、`PROBLEMS: []`，
> 截图 `logs/cs_list_shots/{01-overview,02-detail,03-diversion,04-evaluation}.png`
> （演示账号故意没有 Cookie，所以列表 tab 显示的是 409 的诚实提示，不是空表）；
> 真机只读探针 `cs_list_read_probe.py`（报告 `logs/cs_list_read_probe.json`）→ 三个 kind **全 200**
> + 人话权限 warning（平台认下接口名与参数形状、账号没有「数据」权限）、`rows/total` 仍 `null`，
> 另两条本地拦 `cs-bogus` / `cs-order-without-by` → 400 `invalid_fields`（零平台调用）。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,088,481 B / 2026-10-13 12:10:39**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 41`**；`packaged_ui_smoke.py` → **28/28 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-C5sZJrHk.js` / `index-CE1STJ7C.css`）；
> `scripts/contrast_audit.py --threshold 4.0` → `[dark] 0/28`、`[light] 0/28`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、`焦点不可见的页面：[]`、`NON_GET=[]`；
> **打包版**只读探针 `cs_list_packaged_probe.py`（打 18400，只发 GET）→ 三条 `cs/{kind}` 全 200、
> 标题与列数正确（客服明细 12 列 / 订单分流明细 3 列 / 客户评价明细 6 列）、`rows`/`total` 为 `null`
> + 人话权限 warning，两条本地拦 400 `invalid_fields`。

### 第三十七批：话术（官方 IM 工作台的「快捷回复」）读 + 写（214~221）

**动机**：用户诉求「①一个商品配一个 AI 回复 ③助词自己填」里，「话术」是官方 IM 工作台的一等公民；
上一批（第三十六批）把官方菜单树的**页面**补齐后，本批从「官方写接口覆盖审计」里挑出
`mtop.taobao.idlemessage.quickreply.{group,content}.operate`（bundle 里存在、我们完全没实现、
且是**平台侧配置**而非本地数据）来补。

**取证**（`C:\Users\Administrator\.dsh-tmp\write_coverage_audit.py` + `quickreply_*.py`，零平台请求）：
14 份本地 bundle 里共 362 个 `mtop.*` 名字，我们登记了 188 个，**213 个没登记**，其中末段像写操作的有 32 条；
本批只做话术族（其余按产品价值排序留在后续批次）。三个接口、参数形状、`needLoginPC:true`、
官方两个输入框上限（分组名 10、短语 200）与官方确认框原文都从 bundle 逐字抄下，
写进 `docs/contracts/quick_reply.md`。

**落盘**：`backend/app/xianyu/quick_reply.py`（协议层：三个接口常量、6 条 `WRITE_APIS`、
`assert_write` 只认 `WRITE_API_SET`、请求构造与解析）→ `backend/app/services/quick_reply.py`
（`list_groups` + 6 个写函数，三步顺序 **确认 → 字段校验 → 建客户端**；平台收了但没回显 id 时
`saved:false` + 中文 warning，**不谎报成功也不谎报失败**）→ `backend/app/api/quick_reply.py`
（`prefix='/api/quick-reply'`，1 读 + 6 写）→ `backend/app/main.py` 挂 `'quick_reply'`
→ `backend/tests/test_quick_reply_api.py`（58 条）→ 前端 `frontend/src/pages/QuickReply.tsx`
+ `quickreply.css`（第 29 页，导航「运营」组）→ AI 侧 7 个工具 + MCP 7 条路由。

**★ 本批顺手修掉的真 bug（由前端队友报出、我复核并修）**：`backend/app/main.py` 的
`StarletteHTTPException` 处理器把**路由层**的 404/405 也套成统一信封 ⇒ `frontend/src/api.ts`
的 `isFrameworkError` 恒为 false ⇒ `notReady` 永不成立 ⇒ 页面把英文 `Not Found` 当业务错误显示，
**全站「后端未就绪」空态机制失效**。修法：新增 `_path_registered(request)`（遍历
`request.app.router.routes`，**跳过 `Mount` 与非 `APIRoute`** —— `/` 上挂着托管前端的
`StaticFiles` Mount，它匹配任何路径，算进来会让「没注册」永远判成「注册过」），
路由层 404/405 回 `code='route_not_found'` + 中文文案；前端把 `route_not_found` 一并当 `notReady`。
`backend/tests/test_api_hardening.py` 三条钉住（含 `test_business_not_found_keeps_its_code`：
业务 404 仍是 `not_found`，不许被这次改动吞掉）。

> **第三十七批证据链**：`backend/tests/test_quick_reply_api.py` → **`Ran 58 tests ... OK`**；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → **`Ran 130 tests ... OK`**；
> `scripts/reconcile_tools.py` → `specs=108 master=107 routes=109 targets=88 handlers=108`、
> `write_tools=46`、`route_checked=109`、**`RECON OK`**；
> `backend/tests/run_all.py` → **`TOTAL 55  PASS 55  FAIL 0  CASES 1992  SKIPPED 2`**（1933 → 1992）；
> `create_app().openapi()` = **172 条路径 / 186 个操作**（6 条 `/api/quick-reply/*` = 7 个操作）、
> `MCP_TOOL_ROUTES` = 109、`TOOL_SPECS` = 108、`MASTER_TOOLS` = 107；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`
> （96 modules、`index-d3X2Qg1j.js 587.87 kB` / `index-C-5jTLtH.css 147.28 kB`）；
> `scripts/ui_smoke.py --port 18491` → **`UI SMOKE ALL OK (29/29 页通过)`**（`quick-reply` 467 字符）；
> `scripts/contrast_audit.py --port 18501 --threshold 4.0` → `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；
> `scripts/a11y_audit.py --port 18503` → 三类问题全 0、`焦点不可见的页面：[]`、`NON_GET=[]`。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,103,783 B / 2026-10-13 13:14:00**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 47`**（含 6 条话术写端点）；
> `packaged_ui_smoke.py` → **29/29 页**（`quick-reply` 464 字符）；`check_assets.py` →
> `ASSETS_MATCH=True`（`index-C-5jTLtH.css` `80a0c271f4d3`、`index-d3X2Qg1j.js` `92e1771e0198`）；
> `scripts/contrast_audit.py --threshold 4.0` → `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、`焦点不可见的页面：[]`、`NON_GET=[]`。
> **真机只读**：`qr_read_probe.py`（打 18400、只发 GET）→ `GET /api/quick-reply/2217970177348/groups`
> **200**、`groups: []`、`count: 0`、`warnings: []`（接口名与调用形状被平台接受；真的空列表）。

### 第三十八批：订单处理（关单 / 延时 / 提醒 / 运费退款 / 撤销投诉）读 + 写（222~231）

**动机**：第 37 批的「官方写接口覆盖审计」列出 32 条末段像写操作、我们还没实现的 mtop。挑出
**订单侧最常用的五条**（关单、延长收货、提醒确认收货、支付运费退款、撤销投诉）+ 一条只读
（关单可选原因），它们都在官方订单页的按钮背后，属于「卖家每天真的会点」的动作。

**取证**（全部离线，零平台请求）：IM 侧 `C:\Users\Administrator\.dsh-tmp\bundle_scan\js_006_p_im_index.js`
（3,071,011 字符）与卖家工作台 `C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`（3,879,584 字符）；
脚本 `trade_write_evidence{,2,3,4}.py` + `show_evidence3.py`（都写 `.py` 再跑，PowerShell 内联
`python -c "…引号…"` 会被解析器打断）。逐字钉住：

- `close.reason.get` v1.0 `{bizOrderId}` → `data.closeReasons`（**字符串数组**，官方 `map(r=>({label:r,value:r}))`）；
- `close.by.seller` **v2.0** `{tid, bizOrderId, closeReason}` —— 官方把**同一个订单号同时塞进两个字段**；
- `seller.delay.confirm` v1.0 `{orderIdList, delayTime}`（`delayTime = 60*days*60*24` 秒，`days` ∈ 3/5/7/10）；
- `consign.remind` v1.0 `{orderIdList, remindAllOrder}`；
- `postage.refund.pay` v1.0 `{orderId, refundId, payType}` + `ttid` + `needLoginPC`（**真的付钱**）；
- `complain.apply.revoke` v1.0 `{complainId}` → `data.success` / `data.failReason`。
- 官方 `dispute.create`（维权创建）表单要先拉 `dispute.create.page`，**本批不做**。

**落盘 5 处**：`backend/app/xianyu/trade_write.py`（+5 写接口、`WRITE_APIS` 10→15、新增
`order_id_list` / 六个请求构造器 / `parse_close_reasons` / `parse_batch_result`）→
`backend/app/services/trade_write.py`（5 个写函数 + 1 个只读，审计名 `trade_close_order` 等）→
`backend/app/api/trade_write.py`（+6 条路由，模块 16→22 条）→
`backend/app/agents/tools.py` + `mcp_server/client.py`（6 个工具 / 6 条 MCP 路由）→
`frontend/src/pages/Orders.tsx` + `trade.css`（第 6 个写标签「订单处理」+ 关单原因三态 select）。

**★ 顺手抓到的真缺陷（第 222 条）**：`backend/app/api/trade_write.py`（8 处）与
`backend/app/api/security_write.py`（3 处）把 `confirm` 写成 `bool(body.get('confirm'))`，
而 `bool('false')` 是 `True` ⇒ 用户传 `"false"` 反而**打开了写闸门**；服务层 `trade_write.py`（7 处）
与 `security_write.py`（3 处）还裸写 `if not confirm:`（`not 'false'` 是 `False`，同样放行）。
真机探针 `confirm_false_probe.py`（打打包版、**用不存在的账号 `9999999999999` 保证零平台写请求**）
实测 `trade.ship_dummy` + `{"confirm": "false"}` → **404 `not_found`**（闸门被绕过、直接走到建客户端），
正确应为 400 `confirmation_required`。21 处全部改成原样透传 + `_truthy(confirm)`。

> **第三十八批证据链**：
> `backend/tests/test_trade_write_api.py` → **`Ran 74 tests ... OK`**（+30，含 `ConfirmGateBypassTest`
> **遍历本模块全部 19 条 POST 路由** × 10 个假确认值 `('false','FALSE',' false ','no','yes','0',0,'',None,2)`，
> 全部要求 400 `confirmation_required` 且零平台调用）；
> `backend/tests/test_security_write_api.py` → **`Ran 22 tests ... OK`**（+1）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server backend.tests.test_agents_tools_workbench`
> → `Ran 130 tests OK`；`scripts/reconcile_tools.py` → `specs=114 master=113 routes=115 targets=94 handlers=114`、
> `write_tools=51`、`route_checked=115`、`RECON OK`；
> `backend/tests/run_all.py` → **`TOTAL 55  PASS 55  FAIL 0  CASES 2023  SKIPPED 2`**（1992 → 2023）；
> `create_app().openapi()` = **178 条路径 / 192 个操作**（6 条新路由全在）；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npm run build` → `BUILD_EXIT=0`（96 modules、
> `index-oqk1Jlhv.js 595.10 kB` / `index-B4KMbKO0.css 147.41 kB`）；
> `scripts/ui_smoke.py --port 18441` → **`UI SMOKE ALL OK (29/29 页通过)`**。
> 闸门探针扩到 **52 个写端点**（`probe_write_gate.py` 新增 `trade.close`/`delay`/`remind`/
> `postage_pay`/`complain_revoke` 五条，47 → 52）。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,110,779 B / 2026-10-13 14:29:51**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版五项复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 52`**（五条新端点未确认一律 400
> `confirmation_required`；`trade.ship_dummy` 确认腿 404 `not_found` —— 正是第 222 条缺陷修好后的样子，
> 修之前它传 `"false"` 也会走到这一步）；`packaged_ui_smoke.py` → **29/29 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-B4KMbKO0.css` `e7e1266c8ae9`、
> `index-oqk1Jlhv.js` `4986702cf0df`）；`scripts/contrast_audit.py --threshold 4.0` →
> `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；`scripts/a11y_audit.py` → 三类问题全 0、
> `焦点不可见的页面：[]`、`NON_GET=[]`。
> **真机只读**（`close_reasons_probe.py`，打 18400、桌面密钥换 token、只发 GET）：
> `GET /api/trade/2217970177348/orders/3317095524016013282/close-reasons` → **200**、
> `close_reasons: ["不想卖了","宝贝已出售","买家联系不上","与买家协商一致","其他原因"]`、
> `raw_keys: [bizOrderId, closeReasons, needDecryptKeys, needDecryptKeysV2, serverDecryptKeys, serverTime]`、
> `warnings: []`、`source: platform` ⇒ 关单原因这条只读接口**在真账号上是通的、拿到的是官方那五个原因**；
> 同一轮验证写闸门：`POST .../orders/1/close` + `{confirm: false}` → **400 `confirmation_required`**
> （中文文案「关闭订单 是写操作，需要显式确认（confirm=true）后才会提交」）。

### 第三十九批：物流（发货选公司 / 单号认公司 / 修改运单号）（232~236）

**动机**：第 37/38 批把「交易写」补齐后，第 39 批从「官方 bundle 里有、我们没实现、末段像写」
的 22 条候选里挑形状最干净、且贴合已复刻页面（订单管理 → 发货）的一族：物流。

**取证**（零平台请求，`C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js` 3,879,584 字符 +
`seller_scan\js_008_vendors.js` 5,857,382 字符）：
- 公司字典 `com.taobao.idle.logistics.new.get.companies` v1.0，**不带 data**（`{api, v}`），
  官方读 `data.items[].{code,name}` 并 `map(e => ({label: e.name, value: e.code}))`，默认值 `OTHER`。
- 单号认公司 `mtop.taobao.idle.logistics.guess.mailno` v1.0 `{mailNo}` → `data.unionCode` / `data.cpName`；
  官方输入框 **500ms 防抖**，认出的公司若不在下拉里就 `append` 进去。
- 改运单号 `mtop.taobao.idle.logistics.merchant.consign.resend` v1.0 `{tradeId, mailNo, cpCode}`
  （`main.js` @2766767「修改物流信息」弹窗，成功 toast「修改成功」）。

**落盘（4 处）**：新建 `backend/app/xianyu/logistics.py`（协议层：`companies_request` 不带 data、
`guess_request`、`resend_request(order_id, mail_no, cp_code='', *, confirm)`；
解析用 `trade.body_of` 认 `data.module`/`data.data`/`data` 三种容器）、
`backend/app/services/logistics.py`（三步顺序 + `_client` 统一凭据判定）、
`backend/app/api/logistics.py`（`prefix='/api/logistics'`，2 GET + 1 POST）、
`backend/app/main.py` 的 `ROUTE_MODULES` 追 `'logistics'`；
`backend/tests/test_logistics_api.py` **49 条**。

**★ 首跑抓到的三件事**（都在本地，零平台请求）：
1. `decode_payload` **只解 JSON 字符串、不拆层级**；我第一版手写 `raw['data']` 在**双层回执**
   上取不到值（`parse_resend(RESEND_OK)['cp_code']` 得到 `None`）⇒ 改用 `trade.body_of`。
2. 「平台收了但没回显公司编码」**不是失败**：官方也是只看信封就 toast「修改成功」，
   所以语义定为 `saved=True` + `echoed=False` + 一条中文 warning（原先把 `saved` 判成 False，
   等于把成功说成失败）。
3. `WRITE_ROUTES` 是三元组 `(method, path, body)`，测试里按二元组解包会 `ValueError`。

> **第三十九批证据链**：
> `backend/tests/test_logistics_api.py` → **`Ran 49 tests ... OK`**（首跑 2 failures + 1 error，全是上面三件事）；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server backend.tests.test_agents_tools_workbench`
> → `Ran 130 tests OK`；`scripts/reconcile_tools.py` → `specs=117 master=116 routes=118 targets=97
> handlers=117`、`write_tools=52`、`route_checked=118`、`RECON OK`；
> `backend/tests/run_all.py` → **`TOTAL 56  PASS 56  FAIL 0  CASES 2072  SKIPPED 2`**（2023 → 2072）；
> **真机只读探针**（`C:\Users\Administrator\.dsh-tmp\logistics_read_probe.py`：拷真实数据目录到临时根 →
> 源码起后端 18521 → 桌面密钥换 token → 只发 GET + 一条 `confirm:false` 的闸门检查）：
> `GET /api/logistics/2217970177348/companies` → **200**、**335 家公司**（样例 海信物流 / 天喜物流 / 中通冷链）；
> `GET …/guess?mail_no=SF1234567890` → **200**、`cp_code='SF'`、`cp_name='顺丰速运'`；
> `POST …/orders/<真实订单号>/resend` + `{confirm:false}` → **400 `confirmation_required`**
> （中文文案「修改运单号 是写操作，需要显式确认（confirm=true）后才会提交到闲鱼」）⇒
> **接口名与参数形状被平台接受、两条只读接口真机拿到真数据、写闸门在真机生效且零平台写请求**。
> 补丁脚本踩坑（记一笔）：`patch_lg39.py` 的 marker 只写在 tools.py/路由里，导致
> `test_agents_cross.py` / `test_agents_tools_workbench.py` 被重复插入 3~6 份同名条目
> （`test_tool_names_match_contract` 报 `First extra element 117: 'trade_close_order'`），
> 用 `fix_lg_lists.py` / `fix_wb39.py` 收敛成一份，并把名字按 `TOOL_SPECS` 的真实顺序
> 放到 `trade_complain_revoke` 之后。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,122,701 B / 2026-10-13 15:10:43**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版五项复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 53`**（`logistics.resend` 未确认 400
> `confirmation_required`、确认腿 400 `invalid_fields`）；`packaged_ui_smoke.py` → **29/29 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-D0-oMDFd.js` `dc326c94d9e0` /
> `index-DpVg3Xto.css` `405780f12bad`）；`scripts/contrast_audit.py --threshold 4.0` →
> `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；`scripts/a11y_audit.py` → 三类问题全 0、
> `焦点不可见的页面：[]`、`NON_GET=[]`。
> 发货表单视觉复核（`C:\Users\Administrator\.dsh-tmp\order_ship_shots.py`：打打包版 18400、
> 选真实账号、开写面板 → 发货 → 修改运单号，**不点最终确认**）：`PROBLEMS: []`、
> `page errors: []`、**非 GET 请求 `[]`**；深/浅两套截图里快递公司下拉有 **336 个选项**
> （335 家公司 + 占位），填 `SF1234567890` 后失焦 → 公司自动变成 **顺丰速运（SF）**、
> 状态行写「平台认出：顺丰速运（SF），已自动选中。」、缺必填项提示随之变成「还缺必填项：订单号」；
> 截图 `logs/order_ship_shots/{01,02}-{dark,light}-*.png`。

### 第四十批：IM 会话操作（拉黑状态 / 拉黑买家 / 解除 / 发商品卡片）（237~240）

237. 只读 1 条 + 写 3 条：`GET /api/im/{account_id}/blacklist`、`POST …/blacklist`、
     `POST …/blacklist/remove`、`POST …/cards`；协议层 `backend/app/xianyu/im_actions.py`
     （**版本按动作分别传**，bundle 逐字）：`mtop.taobao.idlemessage.pc.blacklist.query` **v1.0**、
     `.blacklist.add` **v2.0**、`.blacklist.remove` **v1.0**、
     `mtop.taobao.idlemessage.message.card.send` **v1.0** `{sessionId, itemId}`
     （偏移与官方 toast 文案见常量注释与 `WRITE_APIS[...]['evidence']`）。
238. `is_in_black` / `show_black_list` 平台没返回这个键时是 **null**，不补 `false`（服务层补中文 warning）；
     写返回 `{action, saved[, deleted][, item_id][, session_id]}` —— 解除拉黑多一个 `deleted:true`。
239. **会话标识有两条路**：`session_id`（平台 cid）或 `conversation_id`（本地会话行 id，由后端从最新一条
     消息的 `meta.cid` 解析）—— `conversations` 表**没有 cid 列**（`backend/app/core/db.py:119-131`），
     会话页手里只有本地行 id，只能传它。两个都传以 `session_id` 为准；一个都没有 → 400 `invalid_fields`；
     会话不存在或属于别的账号 → 404 `not_found`；会话还没有 cid → 400 `missing_cid`
     （文案让用户先点「同步历史会话」）。**不替平台加/去 `@goofish` 后缀**（库里存的是去掉后缀的 cid）。
240. AI 侧 4 个工具（`im_blacklist_status` 只读 + `im_blacklist_add` / `im_blacklist_remove` /
     `im_card_send` 要 `confirm`）+ MCP 4 条路由；`scripts/reconcile_tools.py` 的 `WRITE_PREFIX`
     补 3 个写工具名，`backend/tests/test_mcp_server.py` 两处硬编码 52 → 55。
     会话页（`frontend/src/pages/Chats.tsx` + `frontend/src/pages/chats.css`）：头部两个入口
     （拉黑 / 解除拉黑、发商品卡片）、拉黑状态小字（**查到 / 没查到 / 平台没返回**三种说法分开）、
     两个确认框都走 `ConfirmDialog` / `Modal`，写操作**唯一出口** `runImWrite` 一律
     `confirm: true` + `conversation_id`。

> **第四十批证据链**：`backend/tests/test_im_actions_api.py` → `Ran 64 tests ... OK`
> （含 `ImConversationIdTest` 10 条：解析 cid / `session_id` 优先 / 写走解析结果 /
> 没有 cid 400 `missing_cid` 且零平台调用 / 未知会话 404 / 跨账号 404 / 两个都不传 400）；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_im40_full`）→
> `TOTAL 57  PASS 57  FAIL 0  CASES 2136  SKIPPED 2`；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server backend.tests.test_agents_tools_workbench`
> → `Ran 130 tests ... OK`；`test_mcp_server.py` 单跑 → `Ran 65 tests ... OK`；
> `scripts/reconcile_tools.py` → `specs=121 master=120 routes=122 targets=101 handlers=121 /
> write_tools=55 / route_checked=122 / RECON OK`；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npx vite build` → `BUILD_EXIT=0`
> （96 modules，`index-ASnrPafL.js` 601.33 kB / `index-DtLJInxc.css` 147.66 kB）；
> `scripts/ui_smoke.py --port 18441` → **29/29 页通过**。
> UI 取证（`C:\Users\Administrator\.dsh-tmp\im_panel_shots.py`：临时库 + 演示数据 + 补一条
> `meta.cid`；**只打开弹窗与确认框，绝不点确认**）→ `PROBLEMS: []`、`page errors: []`、
> 只读请求 1 条（`GET /api/im/demo_acc/blacklist?conversation_id=1`）、**`POST /api/im` 0 条**
> （红线：没有显式确认，一个写请求都不许发）；截图
> `logs/im_panel_shots/{01-chats-dark,02-card-modal-dark,03-blacklist-confirm-dark,04-chats-light}.png`。
> 顺手修掉一个真缺陷：`mcp_server/client.py` 里第三十九批的物流 `TOOL_ROUTES` 块被**重复插入了 5 份**
> （补丁脚本没去重，五方对账与单测都没查出来 —— 对账是**字典**、重复键自然只留一份），
> 用 `C:\Users\Administrator\.dsh-tmp\fix_mcp_dup.py` 收敛成一份。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,137,125 B / 2026-10-13 22:32:23**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 56`**（3 条 IM 写接口都 400 `confirmation_required`，
> 确认腿用不存在的账号 → 400 `invalid_fields`，零平台写请求）；`packaged_ui_smoke.py` → **29/29 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-ASnrPafL.js` / `index-DtLJInxc.css`）；
> `scripts/contrast_audit.py --threshold 4.0` → `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、`焦点不可见的页面：[]`、`NON_GET=[]`。
> **打包版真机只读探针**（`C:\Users\Administrator\.dsh-tmp\im_read_packaged.py`，**只发 GET**）→
> `PROBLEMS: []`：`GET /api/im/2217970177348/blacklist?conversation_id=<真实会话行>` → 200，
> 后端把本地会话行解析成平台 cid `66978575394`，`is_in_black=false`、`show_black_list=true`、
> `warnings=[]`、`source=platform`；未知会话 → 404 `not_found`；两个 id 都不传 → 400 `invalid_fields`。
> 诚实边界：**3 条写接口一次真机请求都没发过**（拉黑 / 解除 / 发商品卡片都会真的动平台数据）；
> `blacklist.add` 是 v2.0、`card.send` 的 `sessionId` 是否要 `@goofish` 后缀、以及平台是否接受
> 这族入参，仍属未验证。

### 第四十一批：纠纷协商（联系纠纷小二）（241~246）

241. `backend/app/xianyu/dispute.py`（新）：协议层 5 个平台接口（**全部 v1.0**）——
     `mtop.taobao.idle.shop.dispute.message.get.conversation` / `…query.history` /
     `…query.unread.list` / `…message.send` / `…message.mark.read`。`send` 的
     `data = {conversationId, sceneType:'userActive', content: JSON 串, contentType:
     'PLAIN_TEXT'|'IMAGE'}`，`content` 用 `json.dumps(ensure_ascii=False, separators=(',',':'))`
     贴近官方 `JSON.stringify`；文字上限 2000 字、id 上限 128、图片 `ext` 缺省 `JPG`、
     `thumbURL` 缺省取原图、`sendKey` 始终带。三元组 `(api, data, version)` 约定
     （同 `im_actions.py`）、`assert_write` 只认 `WRITE_API_SET`、`_need_confirm` 严格 `is True`、
     `parse_*` 认不出就 `None`（**不补 0 / 空串**）；平台回来的 `content` 两种形态
     （JSON 字符串 / 对象）都归一。
242. `backend/app/services/disputes.py`（新）：写路径三步顺序 **确认 → 字段校验 → 建客户端**；
     `confirm` 用 `_truthy` 认 `True`/`'true'`/`1`，**传给协议层前归一成 `True`**；
     只读三态（`messages` 是数组 = 真的这些留言 / `null` + warning = 平台没给列表键）；
     取会话拿不到 `conversationId` → **502**（不是 200 带空串）；写成功记 `operation_logs`
     （`dispute_send_text` / `dispute_send_image` / `dispute_urge` / `dispute_mark_read`）
     并播 `disputes.changed`；平台接受但没回 `msgId`/`sequence` → 只给 `dispute_no_echo`
     warning（**不是失败**，且不许拿本地数字冒充平台序号）。
     **只读路径的平台拒绝不降级**（真机见过 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` / `NO_PERMISSION`
     ⇒ 如实 502 + 人话）：把「没权限」装成「没有留言」是假数据。
243. `backend/app/api/disputes.py`（新，7 条 = 3 读 + 4 写）+ `backend/app/main.py` 的
     `ROUTE_MODULES` 追加 `'disputes'`（排在 `'im_actions'` 之后）。路由层把 `confirm`
     **原样**交服务层（`body.get('confirm')`，**不做 `bool()`** —— 第 38 批那条真缺陷的教训）。
244. `backend/tests/test_disputes_api.py`（新，**77 条**）：协议层形状 / 只读三态 / 确认闸门
     （遍历本模块**全部 POST 路由** × `FALSE_LIKE` 值，全 400 `confirmation_required` 且零调用）/
     字段校验（在建客户端之前）/ 写成功（请求体逐字、日志与事件、一次调用）/ 写失败
     （平台拒绝 502 且**不写 `need_relogin`**、业务体拒绝带平台原话、没回执只 warning）/ 路由。
245. AI 侧 7 个工具（`disputes_conversation` / `disputes_messages` / `disputes_unread` 只读 +
     `disputes_send_text` / `disputes_send_image` / `disputes_urge` / `disputes_mark_read` 写）
     + MCP 7 条路由；计数 `TOOL_SPECS` 128 / `MASTER_TOOLS` 127 / `TOOL_ROUTES` 129 / 写工具 **59**；
     `scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 补 4 个**完整**写工具名（故意不加
     `'disputes_'` 宽前缀，否则只读的 `disputes_conversation` 会被误判成写工具）；
     `backend/tests/test_mcp_server.py` 两处硬编码 55 → 59。
246. 前端投诉页（`frontend/src/pages/Complaints.tsx` + `frontend/src/pages/complaints.css`，
     由 `fix-im` 交付、Lead 复验）：投诉详情里的「联系纠纷小二」面板 —— 拉会话 / 历史留言 /
     未读 / 发文字 / 发图举证 / 催办 / 标记已读，写操作**唯一出口**一律 `confirm: true`。

> **第四十一批证据链**：`backend/tests/test_disputes_api.py` → `Ran 77 tests ... OK`；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_d41_full`）→
> `TOTAL 58  PASS 58  FAIL 0  CASES 2213  SKIPPED 2`；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → `Ran 130 tests ... OK`；
> `scripts/reconcile_tools.py` → `specs=128 master=127 routes=129 targets=108 handlers=128 /
> write_tools=59 / route_checked=129 / RECON OK`；
> `create_app().openapi()` = **190 条路径 / 206 个操作**（6 条 `/api/disputes/*` = 7 个操作全在）；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npx vite build` → `BUILD_EXIT=0`
> （97 modules，`index-3WxsizZZ.js` 609.95 kB = 磁盘 672,349 B / `index-CbU9mCPj.css`
> 150.51 kB = 磁盘 150,515 B）。
> 诚实边界：**这一族 7 条接口一次真机请求都没发过**（本轮红线是零平台请求，形状全部来自官方
> bundle 静态取证）；平台是否接受这族入参、真机上纠纷会话 id 的取得方式、`mark.read` 的
> `sequence` 语义，仍属未验证。
>
> **重打包 + 打包版复验 + 界面取证（收尾补）**：`build_exe.ps1 -SkipFrontend` → exe
> **10,156,009 B / 2026-10-13 23:53:47**（`--port 18400`，`/api/health` 200 `frozen:true`）；
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 60`**（4 条纠纷写未确认 400 `confirmation_required`，
> 确认腿用不存在的账号 ⇒ 400 `invalid_fields` / 404 `not_found`，零平台写请求）；
> `packaged_ui_smoke.py` → **29/29 页**；`check_assets.py` → `ASSETS_MATCH=True`
> （`index-3WxsizZZ.js` `bf0244a29695` / `index-CbU9mCPj.css` `b5af0adf0f99`）；
> `scripts/contrast_audit.py --threshold 4.0` → `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、焦点环全可见；打包版路由探针
> `d41_packaged_probe.py` → `PROBLEMS []`（未知账号取会话 404 `not_found`、三个单号都不传
> 400 `invalid_fields`、四条写未确认 400 `confirmation_required`、确认腿 404 `not_found`）。
> **界面取证（`C:\Users\Administrator\.dsh-tmp\d41_panel_shots.py`，`page.route` 拦两个 API 喂假数据、
> 零平台请求）**：`PROBLEMS []` —— 面板渲染出会话 id / 双方消息 / 平台提示 / 四个写入口，
> **`dispute_posts []`**（只开了确认框、没点确认）、`page_errors []`；截图
> `logs/d41_panel_shots/{01-complaints-dark,02-dispute-panel-dark,03-urge-confirm-dark,04-dispute-panel-light}.png`。
> **★ 这次取证抓出一个真缺陷**：面板四个写按钮（发送 / 发送图片 / 标记已读 / 催办）**点一下就发**，
> 没有二次确认 —— 而列表入口的提示语自己写着「写操作都要二次确认」⇒ 既缺确认又是假话。
> 修法：新增 `pending` 状态 + `ConfirmDialog`（逐条列出要发什么），确认后才走唯一写出口
> `runDisputeWrite`；`frontend/src/pages/Complaints.tsx`（tsc `TSC_EXIT=0`、`vite build BUILD_EXIT=0`、
> `ui_smoke.py` 29/29 页），契约补进 `docs/CONTRACTS.md` 第 41 批块。

### 第四十二批：粉丝价（全部粉 / 老粉 / 已购粉）（247~251）

247. 后端三层（`backend/app/xianyu/fans_price.py` / `backend/app/services/fans_price.py` /
     `backend/app/api/fans_price.py`）：官方 `mtop.alibaba.idle.seller.platform.item.operate.fansprice.get`
     与 `…fansprice.set`（都 v1.0，逐字来自 `item-main.js` @3080605 / @3081007）。**写是「整份替换」**：
     官方只把**有值的组**拼进 `fansPriceList`（@4101088），所以留空 = 清空；价格口径沿用官方文案
     （最多 2 位小数、必须 > 0）。同一路径 1 读 + 1 写；`ROUTE_MODULES` 里 `'fans_price'` 必须
     **插在 `'items'` 之前**（同前缀先注册者先匹配）。
248. `backend/tests/test_fans_price_api.py` **67 条**（协议 12 / 只读 8 / 闸门 6 / 校验 6 /
     happy path 8 / 失败 8 / 路由 3），文件头列了本族最容易犯的七个错（整份替换被当成改一组、
     未确认就发、`bool('false')`、平台没返回补成 `[]`、拿不到的价格补 0、价格校验漏项、
     字段校验排在凭据检查之后）。
249. AI 侧 2 个工具：`items_fans_price`（只读）+ `items_set_fans_price`（写，description 必须写明
     「整份替换：只想改一组时要把另外两组当前值一起给」）；`mcp_server/client.py` 2 条 `TOOL_ROUTES`；
     `scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 补**完整**写工具名（不加 `items_` 宽前缀，
     否则只读工具会被误判成写工具）；`backend/tests/test_mcp_server.py` 两处硬编码 59 → 60。
     **★ 这次接线顺手修掉一个真缺陷**：第 39 批的补丁脚本把物流那 13 行 `TOOL_ROUTES`
     连续重复插了 5 份，对账脚本用的是字典（重复键只留一份）、单测只断言条数，两边都查不出来
     ⇒ 用 `fix_mcp_dup.py` 收敛成一份（折叠前 5 → 折叠后 1）。
250. 前端（`fix-im` 交付、Lead 复验）：`frontend/src/pages/Items.tsx` 行内「粉丝价」→
     `FansPriceModal`（预填当前三组值、**只填一次**、四种空态分开说、底部实时列「本次会提交
     （整份替换）」）→ `ConfirmDialog`（逐条列出改成多少 / 哪几组会被清空）；唯一写出口
     `postFansPrice` → `postItemWrite`（`confirm: true` 字面量）；`frontend/src/pages/items.css`
     八个 `.it-fans-*` 类（只用既有变量）。
251. 文档：`docs/contracts/fans_price.md`（新建，平台接口 / HTTP 面 / 校验 / 三态 / 三步顺序 /
     界面口径 / 测试 / AI 侧 / 未验证）+ `docs/CONTRACTS.md` 第 42 批契约块 + AI 侧计数
     130 / 129 / 131（写 60）。

> **第四十二批证据链**：`backend/tests/test_fans_price_api.py` → `Ran 67 tests ... OK`；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_fp42_full2`）→
> `TOTAL 59  PASS 59  FAIL 0  CASES 2280  SKIPPED 2`；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → `Ran 130 tests ... OK`；
> `scripts/reconcile_tools.py` → `specs=130 master=129 routes=131 targets=110 handlers=130 /
> write_tools=60 / route_checked=131 / RECON OK`；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npx vite build` → `BUILD_EXIT=0`
> （97 modules，`index-8xdDx-7r.js` 615.75 kB / `index-BX9YNMmk.css` 151.22 kB）；
> `scripts/ui_smoke.py --port 18471` → **29/29 页通过**。
> **界面取证（`C:\Users\Administrator\.dsh-tmp\fp42_panel_shots.py`，`page.route` 拦
> `**/api/items/*/*/fans-price*` 喂假数据、零平台请求）**：`PROBLEMS []` —— 弹窗渲染出
> 「闲鱼上的当前配置」三组值、三个输入框**预填** `12.50/11.00/10.00`、底部「本次会提交（整份替换）」；
> 二次确认框写出「会在闲鱼上真的改掉，不可撤销」+ 逐条组价 + 「没填的组会被清空」；
> **`fans_posts []`**（只开确认框、没点确认）、`page_errors []`；截图
> `logs/fp42_panel_shots/{01-fans-modal-dark,02-fans-confirm-dark,03-fans-modal-light}.png`。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,171,806 B / 2026-10-14 01:08:57**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版**六项**复验全绿：
> `probe_write_gate.py`（已扩到含粉丝价那条写）→ **`GATE FAILURES: 0 / 61`**
> （`items.set_fans_price` 未确认与 `confirm:false` 都是 400 `confirmation_required`，
> 确认腿换不存在账号后 400 `invalid_fields`）；`packaged_ui_smoke.py` → **29/29 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-8xdDx-7r.js` `c6565b0efcf2`、
> `index-BX9YNMmk.css` `bee41e32dea0`）；`scripts/contrast_audit.py --threshold 4.0` →
> `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；`scripts/a11y_audit.py` → 三类问题全 0、
> 焦点不可见页面 `[]`；**打包版粉丝价探针**（`fp42_packaged_probe.py`，只用不存在的账号
> `9999999999999`，零平台请求）→ `PROBLEMS []`（GET 未知账号 404 `not_found` 而不是
> `route_not_found` ⇒ 路由确实进了冻结包；未确认 400、`confirm:false` 400、三组全空 400）。
> 诚实边界：**写接口一次真机请求都没发过**（会真的改平台上三组粉丝价）；只读接口的真机形状
> 也没有样例（本机账号在数据域一直回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，粉丝价属同一权限域）。

### 第四十三批：客服会话接管（转交 / 结束 / 重新发起）（252~257）

252. 选题来自写覆盖审计：官方卖家工作台 IM 会话头部有「转交 / 结束 / 重新发起」三个按钮
     （`js_008_vendors.js` @2030569 / @2036368 / @1229870 的调用点与「转交」按钮 alt 为证），
     属「小铺 → 客服」域、全是会话级写操作、**不碰钱**。同族里 `…pc.tool.remark`（会话备注）
     **故意不做**：它要 `sessionType`，而我们既没解析也没存这个字段（`xianyu/im.py`、
     `services/chats.py` 里 grep 为 0），猜一个值就是造假。
253. 协议层 `backend/app/xianyu/im_actions.py`（294 → 402 行）：三条 `WRITE_APIS`
     （`session_deliver` / `session_leave` / `session_rejoin`，**全 v1.0**）+ 构造器
     `session_deliver_request(session_id, target_uid, *, confirm)` → `{sessionId, targetUid}`、
     `session_leave_request` / `session_rejoin_request` → `{sessionId}`；新校验器 `_target_uid`
     （只 strip / 非空 / ≤64，**刻意不校验数字格式**）；新解析器 `parse_session_action`：在
     `parse_action` 之上用 `_first_key(body, ('success','succeed','result'))` 得出三态
     `platform_success`，**平台明确说 `false` 时把 `saved` 也改成 `False`**（信封成功也不行）。
254. 服务层 `backend/app/services/im_actions.py`：`_write(...)` 多一个 `parser` 参数（默认
     `proto.parse_action`）；新增 `session_deliver` / `session_leave` / `session_rejoin`
     （审计动作 `im_session_deliver` / `im_session_leave` / `im_session_rejoin`，成功才
     `log_operation` + `publish('chats.changed')`）；三条都走既有的「确认 → 字段校验
     （`_resolve_session`）→ 建客户端」顺序。路由层 `backend/app/api/im_actions.py` 加 3 条
     `POST /api/im/{account_id}/sessions/{transfer,leave,rejoin}`（`confirm` 原样交服务层）。
255. `backend/tests/test_im_actions_api.py` 54 → **90 条**（新增 `SESSION_WRITE_ROUTES` 与
     会话接管夹具：官方形状逐字、`target_uid` 空/超长、`platform_success` 三态、rejoin 回执、
     三条 × `FALSE_LIKE` 全 400 `confirmation_required` 且零平台调用）。
256. **★ 界面取证抓到两个真缺陷（都在前端，已修）**：① `frontend/src/pages/Chats.tsx` 里
     `ConfirmDialog` 渲染在**弹窗之前** —— 两者都是 `position: fixed` 且 z-index 相同，同级里
     后渲染的盖在上面，于是点「转交」后确认框被「转交会话」弹窗盖住、**用户什么都看不到**
     （探针里点确认框直接 30 秒超时）；② 「发商品卡片」的「发送」按钮**直接发平台**，没有二次
     确认（全站唯一一个漏掉的写操作）。修法：确认框挪到所有弹窗**之后**并加注释说明 z 序铁律；
     发卡片改走 `ConfirmDialog`（新增 `confirmKind='card'` 与 `confirmSpec` 分支）。
257. 文档：`docs/CONTRACTS.md` 第 43 批契约块（含 z 序铁律）+ AI 侧计数 133 / 132 / 134（写 63）。

> **第四十三批证据链**：`backend/tests/test_im_actions_api.py` → `Ran 90 tests ... OK`；
> `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_im43_full`）→
> `TOTAL 59  PASS 59  FAIL 0  CASES 2306  SKIPPED 2`；
> `python -m unittest backend.tests.test_agents_cross backend.tests.test_mcp_server
> backend.tests.test_agents_tools_workbench` → `Ran 130 tests ... OK`；
> `scripts/reconcile_tools.py` → `specs=133 master=132 routes=134 targets=113 handlers=133 /
> write_tools=63 / route_checked=134 / RECON OK`；
> `cd frontend && npx tsc --noEmit` → `TSC_EXIT=0`；`npx vite build` → `BUILD_EXIT=0`
> （97 modules，`index-Csa2uoAK.js` 618.85 kB / `index-CaWemXNE.css` 151.30 kB）；
> `scripts/ui_smoke.py --port 18481` → **29/29 页通过**。
> **界面取证（`C:\Users\Administrator\.dsh-tmp\im43_panel_shots.py`，`page.route` 拦
> `**/api/shop/*/cs-routing*` 喂假客服列表、零平台请求）**：`PROBLEMS []` —— 头部三个按钮都在；
> 转交弹窗列出两个客服（`小甲（221111）` / `客服小乙（222222）`）；点「转交」后确认框**在弹窗之上**
> （`转交这个会话？ | 会把和「演示买家」的会话转交给 小甲（221111）…会真的改平台上的会话归属。`）；
> 「结束会话」确认框 `结束和「演示买家」的会话？ …会真的改平台上的会话状态。`；
> 发卡片「发送」先弹 `给「演示买家」发商品卡片？ …发出去不能撤回。`；
> **`im_posts []`**（只开确认框、一次写请求都没发）、`page_errors []`；截图
> `logs/im43_panel_shots/{01-chats-dark,02-transfer-modal-dark,03-transfer-confirm-dark,
> 04-leave-confirm-dark,05-card-modal-dark,06-card-confirm-dark,07-chats-light}.png`。
> 诚实边界：**三条写接口一次真机请求都没发过**（会真的改平台上会话归属/状态）；
> `target_uid` 的真机形状也没有样例（客服列表接口在本机账号回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,175,166 B / 2026-10-14 02:20:32**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版六项复验全绿：
> `probe_write_gate.py`（已扩到 **64 个写端点**）→ **`GATE FAILURES: 0 / 64`**（3 条会话接管写未确认
> 一律 400 `confirmation_required`，确认腿 400 `invalid_fields`）；`packaged_ui_smoke.py` → **29/29 页**；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-Csa2uoAK.js` / `index-CaWemXNE.css` 各 remote=local）；
> `scripts/contrast_audit.py --threshold 4.0` → `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 三类问题全 0、焦点环全可见、`NON_GET=[]`；
> **打包版会话接管只读探针** `C:\Users\Administrator\.dsh-tmp\im43_packaged_probe.py`
> （**零平台请求**：账号用不存在的 `9999999999999`，只打「未确认 / 缺字段」这类本地就拦下的路径）
> → `PROBLEMS []` 九条：四条未确认（含 `confirm:false`）400 `confirmation_required`、
> 缺 `target_uid` 400 `invalid_fields`、两个会话 id 都不传 400 `invalid_fields`、
> `conversation_id` 指向不存在的账号 404 `not_found`。
> 顺带钉下一个方法码口径（新发现，已写进探针注释）：打包版对「**GET 打到只有 POST 的路径**」
> 统一回 **404 `route_not_found`**（不是 405）——`/` 上托管前端的 `StaticFiles` 挂载先匹配上并回 404，
> 再被统一信封包装；`GET /api/im/{acc}/cards`、`/sessions/leave`、`/api/disputes/{acc}/cid-1/urge`
> 三条实测一致，前端把它当「后端未就绪」空态。**能用的 GET 路径不受影响**：
> `GET /api/im/{acc}/blacklist` 回业务码 400 `invalid_fields`、`GET /api/disputes/{acc}/cid-1/messages`
> 回 404 `not_found`。

### 第四十四批：发布前预检（只读）+ 修掉 8 个 AI 工具的分派断链（258~262）

258. **发布前预检接口**（只读）：`POST /api/items/{account_id}/publish/precheck` —— 只跑发布链路的
     前半段（图片准备 → 违禁词预检 → 分类推荐 → 组载荷），把「真发布时会提交的载荷」摆出来，
     **绝不调用** `...publish.service.cards.list`（注册分类路径）与 `mtop.idle.pc.idleitem.publish`。
     落点：`backend/app/xianyu/client.py::publish_precheck`（复用 `_publish_images` / `check_bad_words` /
     `recommend_category` / `build_label_ext` / `build_publish_payload`）、
     `backend/app/services/items.py::publish_precheck`、`backend/app/api/items.py` 1 条 POST。
259. **AI 侧 1 个只读工具** `items_listing_precheck` + MCP 路由 1 条。工具名不能叫
     `items_publish_precheck` —— 只读工具的名字里不许出现 `publish` 这类写动作词（测试会拦）。
260. **★ 真缺陷：8 个 AI 工具在主管 AI 路径上一调就失败** —— `accounts_relogin` / `items_edit` /
     `items_publish` / `items_batch_edit` / `items_sync` / `chats_send` / `chats_set_mode` /
     `orders_deliver` 的 handler 是 `lambda a: _dispatch_service('xxx', a)`，而 `_SERVICE_TARGETS`
     里没有 `xxx` ⇒ `_resolve_service` 退到表外分支 `getattr(backend.app.services, 'xxx')` 找不到，
     回 `{"ok": false, "error": "业务模块还没实现 xxx()"}`。之前只有第 56 批那批新工具被查过，
     老工具没人查（`_HANDLERS` 有名字就以为能跑）。
261. 修法：`_SERVICE_TARGETS` 补 9 条真目标；工具面参数与服务面签名不一致的 5 个在**服务层**加适配
     （`items.edit_from_args` / `publish_from_args` / `publish_precheck_from_args` /
     `batch_edit_from_args`、`chats.send_from_args`）—— 适配若放工具层，
     `test_agents_tools_workbench.py::test_spec_properties_are_accepted_by_the_service_function`
     永远过不去；`accounts_relogin` 的服务层逻辑原来只写在路由 `api/accounts.py:396`，抽出
     `services/accounts.relogin(account_id)` 让路由与 AI 共用一处实现。
262. 两条回归测试：`test_every_tool_resolves_a_service_target`（源码里按名字分派过的工具都必须解析
     得通）、`test_dispatch_gap_tools_reach_their_service`（用间谍证明这 8 个真的调到了服务层）。

> **第四十四批证据链**：`C:\Users\Administrator\.dsh-tmp\check_ai_targets.py` →
> `logs/ai_targets_check.txt` 打出 `HANDLERS=133 TARGETS=113` 与 8 个「表外分派」的工具名；
> `dump_dispatch_gap.py` → `logs/ai_dispatch_gap.txt` 逐个比对工具 spec 与服务层签名。
> 预检三个测试文件：`test_xianyu_items.py` → **`Ran 95 tests ... OK`**（新类 `PublishPrecheckCallTest`
> 8 条：只读前缀、绝不碰 cards/publish、载荷等于发布载荷去掉 `uniqueCode`、违禁词命中 →
> `ready:false` + 逐步骤 detail、分类失败 → warning 不阻断、用户指定类目优先、缺标题/缺图零调用）；
> `test_services_items.py` → **`Ran 194 tests ... OK`**（新类 `PublishPrecheckTest` 7 条）；
> `test_items_api.py` → **`Ran 35 tests ... OK`**（路由 200 / 校验 400 / 鉴权 401）。
> AI 侧三个测试文件 → **`Ran 131 tests ... OK`**（含新增两条回归）；
> `scripts/reconcile_tools.py` → `specs=134 master=133 routes=135 targets=122 handlers=134 /
> write_tools=63 / route_checked=135 / RECON OK`；
> 全量 `backend/tests/run_all.py`（`XIANYU_APP_DIR=%TEMP%\xywb_ai44_full`）→
> **`TOTAL 59  PASS 59  FAIL 0  CASES 2366  SKIPPED 2`**。
> 接口面（`C:\Users\Administrator\.dsh-tmp\surface44.py` → `logs/surface44.log`）：
> openapi **195 路径 / 212 操作**、MCP 135 条路由（63 条写）、AI 规格 134 / 主管 133 / 写 63、
> `_SERVICE_TARGETS` 122、`_HANDLERS` 134。
> 诚实边界：**预检本身一次真机请求都没发过**（只有离线单测）；它是只读的，
> 既不注册分类也不发布，所以「真发布能不能成功」仍未验证。
> **第 44 批补（打包版探针抓到的真缺陷）**：`C:\Users\Administrator\.dsh-tmp\fp44_packaged_probe.py`
> 打 18400（只用不存在的账号 `9999999999999`，零平台请求）时发现「账号 id 打错」回的是
> **503 `xianyu_not_ready`** 而不是 404 —— 根因是预检直接用 `common.get_client`，它把
> 「账号不存在」和「账号没 Cookie」**都**报成 503。修法：预检改用 `services.trade._client`
> （不存在 → 404 `not_found`，没 Cookie / 需重登 → 409），与 trade / drafts / fans_price /
> disputes / im_actions 同口径；`test_services_items.py` 的 `PublishPrecheckTest` 与
> `test_items_api.py` 补上「账号要在库里」的夹具与两条 404 断言。
> 界面口径：发布弹窗加「发布前预检」按钮（`frontend/src/pages/Items.tsx` 的 `runPrecheck`
> + `PRECHECK_STEP_LABELS` + `buildPayload` 抽成一处），三种状态分开说、**预检不挡发布**；
> `items.css` 新增 `.it-precheck*` 十个类（只用既有变量、零 emoji）。
> 复验：`cd frontend && npx tsc --noEmit` EXIT=0；`npx vite build` EXIT=0
> （`index-70l17fQf.js` 685,250 B / `index-aeg2IrM1.css` 152,425 B）；
> 全量 `backend/tests/run_all.py` → **`TOTAL 59  PASS 59  FAIL 0  CASES 2368  SKIPPED 2`**。
> 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,178,915 B / 2026-10-14 03:45:13**，
> `--port 18400` `/api/health` 200 `frozen:true`）后打包版六项复验全绿：
> `probe_write_gate.py` → **`GATE FAILURES: 0 / 64`**；`fp44_packaged_probe.py` → **`PROBLEMS []`**
> （桌面登录 200；空 payload 400 `bad_request`；缺图片 400 `invalid_fields`；未知账号 404 `not_found`；
> 不鉴权 401；带 `confirm:false` 也 404 `not_found` —— 预检是只读的，`confirm` 被忽略）；
> `check_assets.py` → `ASSETS_MATCH=True`（`index-70l17fQf.js` `f4ef2d576f14` /
> `index-aeg2IrM1.css` `265e2ed9b3c2`）；`packaged_ui_smoke.py` → **29/29 页**；
> `scripts/contrast_audit.py --threshold 4.0` → `[dark] 0/29`、`[light] 0/29`、`NON_GET=[]`；
> `scripts/a11y_audit.py` → 无可访问名字的按钮 0 类、没有标签的输入框 0 类、焦点不可见页面 `[]`。
> 界面取证 `C:\Users\Administrator\.dsh-tmp\fp44_panel_shots.py`（起源码后端 + 拦截
> `**/api/uploads/images*` 与 `**/api/items/*/publish/precheck*` ⇒ **零平台请求**）→ `PROBLEMS []`：
> 账号选中后点「发布前预检」，只发出 **1 条** `POST /api/items/demo_acc/publish/precheck`，
> **`publish_posts []`**（一次真发布请求都没发），面板逐步骤渲染「图片准备 / 违禁词预检 / 分类推荐」
> 与人话结论 + 「不会注册分类路径，更不会发布。」，`page_errors []`；
> 截图 `logs/fp44_panel_shots/{01-publish-modal-dark,02-precheck-result-dark,03-page-dark}.png`。
> （`console` 里 `ERR_NAME_NOT_RESOLVED` 是假图床 URL 的预期产物，不是缺陷。）

### 第四十五批：赔付 / 维权（拒绝赔付）读 + 写（263~267）

263. **选题**：写覆盖审计在 362 个 bundle mtop 名里仍有 24 条「像写操作」没做；赔付族里的
     `compensate.service.refuse`（拒绝赔付）是官方卖家工作台维权详情页的真实动作，且**不碰钱**
     （同族的 `compensate.service.pay` 是真的付钱，故意不做）。
264. **协议层**（`backend/app/xianyu/compensate.py`，新增）：三个平台接口逐字 ——
     `mtop.taobao.idle.merchant.compensate.service.detail.query`（**带 `valueType=string`**）、
     `mtop.taobao.idle.merchant.compensate.service.refuse.render`（`data.module.reasonChoices` 是字符串数组）、
     `mtop.taobao.idle.merchant.compensate.service.refuse`（`{orderId, serviceCode, sellerRefuseReason: JSON串,
     refuseProofs: JSON串, isManual}`）。金额单位是**分**：`amount` 原样、`amount_yuan` 换算，
     拿不到一律 `null`；凭证与拒绝原因都必填（官方原话「请上传凭证」，说明 ≤200 字）。
265. **服务层与路由**（`backend/app/services/trade_write.py` 三个函数 + `backend/app/api/trade_write.py` 三条路由）：
     `GET …/orders/{order_id}/compensations/{service_code}`（详情）、
     `GET …/compensations/{service_code}/reasons`（拒绝原因候选）、
     `POST …/orders/{order_id}/compensations/{service_code}/refuse`（拒绝赔付）。
     三层顺序仍是 确认 → 字段校验 → 建客户端；账号前置检查复用 `services.trade._client`（账号不存在 404、凭据问题 409）。
266. **AI 侧**：3 个工具（2 只读 + 1 写）+ 3 条 MCP 路由；对账
     `specs=137 master=136 routes=138 targets=125 handlers=137 / write_tools=64 / route_checked=138`。
267. **界面**：订单页写面板新增「赔付维权」标签（第 15 个写端点）—— 先点「读取详情与原因」（两个只读 GET）再选原因，
     没选原因时提交按钮禁用；确认框逐条列出原因、说明与凭证张数；唯一写出口仍是 `postWrite`（`confirm: true` 字面量）。
     顺手修掉一个真缺陷：`.tr-field input` 的 `min-width:150px` 会把原因复选框撑成一整列（截图里看得见）⇒
     `.tr-comp-item input` 收回到 14px。

> **第四十五批证据链**：
> - bundle 证据：`C:\Users\Administrator\.dsh-tmp\seller_scan\js_008_vendors.js`（5,857,382 字符）里
>   `compensate.service.refuse` 的调用点逐字为
>   `eM({orderId, serviceCode, sellerRefuseReason: JSON.stringify({reasonChoices:[…], reasonDesc}),
>   refuseProofs: JSON.stringify(urls), isManual})`，`refuse.render` 给 `reasonChoices` 当选项。
>   `compensate.service.pay` 需要 `authToken`，而手上只有 `REFUND_PC` / `DEPOSIT_PC` 两个 scene 的证据 ⇒ **故意不做**。
> - 离线自检：`C:\Users\Administrator\.dsh-tmp\check_cp45.py` → `CHECKS=49 PROBLEMS=0`（构造器逐字、确认严格性、
>   校验文案、解析三态与四类失败）。
> - 单测：`backend/tests/test_trade_write_api.py` **118 条**（+45：协议形状 / 只读三态 / 假确认值遍历 /
>   字段校验早于凭据检查 / 写成功与四类失败）；三个 AI 侧测试文件 `Ran 132 tests ... OK`；
>   全量 `TOTAL 59  PASS 59  FAIL 0  CASES 2412  SKIPPED 2`。
> - 界面取证：`C:\Users\Administrator\.dsh-tmp\cp45_panel_shots.py` → `PROBLEMS []`：三个 GET（列表 / 详情 / 原因）
>   各一次、`refuse_posts []`（只开确认框、不点确认）、3 个原因复选框渲染正确、确认框含原因与「1 张凭证图」；
>   截图 `logs/cp45_panel_shots/{01-write-panel-dark,02-compensate-tab-dark,03-compensate-loaded-dark,
>   04-compensate-confirm-dark,05-compensate-light}.png`。
> - 重打包（`build_exe.ps1 -SkipFrontend`，exe **10,189,021 B / 2026-10-14 04:55:56**，
>   `--port 18400` `/api/health` 200 `frozen:true`）后打包版复验全绿：
>   `probe_write_gate.py`（已含这条写端点，**65 个**）→ **`GATE FAILURES: 0 / 65`**；
>   `packaged_ui_smoke.py` → **29/29 页**；`check_assets.py` → `ASSETS_MATCH=True`
>   （`index-BHJSXv64.js` remote=local=`27ae7af68529`、`index-B011Gghx.css` remote=local=`c0fb43b5148b`）；
>   `scripts/contrast_audit.py --threshold 4.0` → `[dark] BAD_PAGES 0/29`、`[light] BAD_PAGES 0/29`、`NON_GET=[]`；
>   `scripts/a11y_audit.py` → 无可访问名字的按钮 0、没标签的输入框 0、无 alt 图片 0、焦点不可见页面 `[]`。
> - **打包版只读探针**（`C:\Users\Administrator\.dsh-tmp\cp45_packaged_probe.py`，只用不存在的账号
>   `9999999999999`，**零平台请求**）→ `PROBLEMS: []` 十条：两个只读 GET 对未知账号都 404 `not_found`；
>   拒绝赔付不带 `confirm` / `confirm:false` 都 400 `confirmation_required`；
>   带 `confirm:true` 但缺字段、缺原因、`confirm:'true'` 字符串都 400 `invalid_fields`（**字段校验早于建客户端**）；
>   未知路径与「GET 打到只写路径」都 404 `route_not_found`（对照，证明确实是路由在、不是没进包）。
> - **诚实边界**：这条写接口**一次真机请求都没发过**（拒绝赔付会真的改动平台上的维权单据，不能为了验证去动用户的单子）；
>   两条只读接口也**没有真机响应样例**（本机账号对赔付族接口可能无权限）。只验到：路由注册、
>   body 被端点接受、`confirm` 闸门拦得住假确认值、界面不点最终确认就零写请求。

## 四、已验证 / 未验证（诚实边界）

**已验证**
- 全部后端逻辑：**59 个测试文件、2412 个用例全绿、0 失败**（2 个 skip：`test_xianyu_login.py`
  里本来就有的平台相关跳过、以及需要真人配合的那条）。含真实子进程 MCP 冒烟、
  真实 WS 事件投递、协议层接线（不注入假工厂，真走 store → XianyuClient）、
  IM 入站闭环（帧 → 落库 → AI 回复 → 真的发回去）、风控回灌与衰减。
- **UI 细节有两个可重跑的审计脚本**（`scripts/contrast_audit.py`、`scripts/a11y_audit.py`）：
  29 页 × 深/浅两套主题的文字对比度全部 ≥4.0:1；无可访问名字的按钮 0、没有标签的
  输入框 0、无 alt 的图片 0、焦点环全部可见；两者整轮都是 `NON_GET_API_REQUESTS=[]`。
  打包版实测（不带 `--port`，打的就是 18400 上那份）：**`[dark] BAD_PAGES 0/29`、
  `[light] BAD_PAGES 0/29`**（第三十七批把新页加进清单后实测；28 页时是 0/28、27 页时是 0/27）。
  另有第三十批那个一次性扫描（`C:\Users\Administrator\.dsh-tmp\scan_markdown2.py`：
  按 tokenize 取非 docstring 字符串，找用户可见的字面 `**` 与反引号）—— 它抓到 6 处并已修，
  但**没入仓**（一次性核对用，重跑价值低）。
- **商品数据真的从平台拉回来了**（2026-10-08，打包版 + 真账号 `2221063792739`）：
  `app.log` 一轮 `{'2221063792739': {'pulled': 5, 'failed': 0, 'stopped': None}}`，
  `item_stats_cache` 出现 5 行 `source='platform'` 真值（曝光/浏览/想要分别
  789/62/3、573/154/2、60/15/0、14/14/0、9/5/0；`visitors` 平台不给 → NULL 不编 0）。
  前端「商品数据」页读缓存显示 **曝光次数合计 11,512 / 浏览次数合计 1,320、
  已汇总 10/11 个商品**，且打开页面只发 `source=cache` + `/stats/progress` 两条请求
  （**零平台调用**）。这条链把第 110~112 三个缺陷一次证实（含真因是环境代理）。
- **删号残留已在真机清干净**（2026-10-09，打包版 + 真库）：重启前只读盘点
  `ORPHAN_ROWS=54`（`items.account_id='3998190171'` 39 行 + `operation_logs` 15 行），
  重启打包版后 `ORPHAN_ROWS=0`，日志 `purge_orphans: 清掉 1 个已删除账号的残留本地数据
  （共 54 行）: {'3998190171': {'items': 39, 'operation_logs': 15, …}}`；商品页「全部账号」
  从 `共 95 条` 变成 `共 56 条`（= 两个在售账号的 45 + 11），页面里再也搜不到 `3998190171`，
  只读探针（真 Edge）`has_deleted_account: false`、`page_errors: []`。
- 真实 HTTP：只读 17 条 + 写路径 11 条（早期探针）；打包版接口面现在
  `/openapi.json` = **198 条路径 / 215 个操作**（第 26 批 142/155，第 29 批 +4 条草稿箱；
  第三十二批 +10 条店铺资料/子账号；第三十三批 +4 条推广资金明细；第三十四批 +1 条商品数据；第三十五批 +1 条经营卡片；
  第三十六批 +1 条客服明细表 `GET /api/datacenter/{account_id}/cs/{kind}`；
  第三十七批 +6 条话术 `/api/quick-reply/*`（7 个操作）；第三十八批 +6 条订单处理
  `/api/trade/{account_id}/orders|refunds|complaints/*`；第三十九批 +3 条物流
  `/api/logistics/{account_id}/{companies,guess,orders/{order_id}/resend}`；
  第四十批 +3 条 IM 会话操作 `/api/im/{account_id}/{blacklist,blacklist/remove,cards}`（4 个操作）；
  第四十一批 +6 条纠纷协商 `/api/disputes/*`（7 个操作）；第四十二批 +1 条粉丝价
   `GET|POST /api/items/{account_id}/{item_id}/fans-price`（2 个操作）；
  第四十三批 +3 条会话接管 `/api/im/{account_id}/sessions/{transfer,leave,rejoin}`；
   第四十四批 +1 条发布前预检 `POST /api/items/{account_id}/publish/precheck`；
   第四十五批 +3 条赔付 / 维权 `/api/trade/{account_id}/{orders/{order_id}/compensations/{service_code},compensations/{service_code}/reasons,orders/{order_id}/compensations/{service_code}/refuse}`），
  均结构化信封、无 500。
- **AI 工具面（离线对账，`scripts/reconcile_tools.py`）**：`TOOL_SPECS` 134 / `MASTER_TOOLS` 133 /
  `TOOL_ROUTES` 134，其中 **63 条写工具**；对账同时校验每条 MCP 路由的 path/query/body 都被真实
  FastAPI 端点接受 → `RECON OK`。MCP 端到端冒烟（真起 `mcp_server/server.py` 子进程打打包版）
  `tools/list` 返回 63 个工具（第 6 批的旧数字；`TOOL_ROUTES` 现在 134 条，本次未重跑该冒烟）。
- **真实浏览器逐页冒烟 29/29 页通过**（`scripts/ui_smoke.py`，Edge/WebView2 引擎，
  临时数据目录，逐页断言未白屏、未掉回登录页、无 JS 报错，截图在 `logs/ui_smoke/`）。
  它抓到了后端全绿时仍然「UI 完全不可用」的登录死循环。
- **打包版（frozen exe）逐页冒烟 29/29 页通过**（`C:\Users\Administrator\.dsh-tmp\
  packaged_ui_smoke.py`，复用 `scripts/ui_smoke.py::run_browser` 但不启后端、直接打
  `127.0.0.1:18400` 与**真实 `%APPDATA%` 数据目录**，截图 `logs/packaged_ui_smoke/`）。
  与上一条的区别：上一条跑的是源码 + 临时库，这一条跑的是**用户手上那个 exe**。
- **打包版 confirm 闸门 65 个写端点全部生效**（`probe_write_gate.py`）：未确认一律 400
  （`confirmation_required` / `confirm_required`），**`GATE FAILURES: 0 / 65`**（探针随批次扩过：
  19 → 22 → 26 → 28 → 30 → 33 → 41 → 47 → 52 → 53 → 56 → 60 → 61 → 64 → 65）；确认那一腿**故意换成不存在的
  账号**（`SAFE_ACC`），闸门放行后本地查库直接 404 `not_found` / 400 `invalid_fields` / 409 —— 既证明闸门放行，
  又保证**零平台请求**（2026-10-12 00:35 之前那版拿真账号打过，三条写请求真到了闲鱼，见第二十五批 161）。
  第 32 批新增八条（`shop.profile_{nick,desc,avatar}` / `sub.{create,delete,mobile,roles,password}`）：
  未确认 400 `confirmation_required`，确认腿除 `sub.delete` 是 404 `not_found` 外都是 400 `invalid_fields`。
  第 37 批新增六条话术写（`qr.group_{create,update,delete}` / `qr.phrase_{create,update,delete}`）；
  第 38 批新增五条订单处理写（`trade.close` / `delay` / `remind` / `postage_pay` / `complain_revoke`）；
  第 39 批新增一条物流写（`logistics.resend`）。
  第 40 批新增三条 IM 写（`im.blacklist_add` / `im.blacklist_remove` / `im.card_send`）：
  未确认 400 `confirmation_required`，确认腿 400 `invalid_fields`（账号用的是不存在的那个）。
  第 43 批新增三条会话接管写（`im.session_deliver` / `im.session_leave` / `im.session_rejoin`）：
  未确认与 `confirm:false` 都 400 `confirmation_required`，确认腿 400 `invalid_fields`；
  同一批的只读探针还钉下「GET 打到只有 POST 的路径 → 404 `route_not_found`」这条方法码口径。
- **真实浏览器实时刷新 2/2 阶段通过**（`scripts/live_refresh_smoke.py`）：后端写账号
  备注 / 新增提示词后，页面在 0.21 秒内自己更新，且 `navigation` 计数不变。
- **真后端 + 真 `XianyuClient` + `httpx.MockTransport` 逐条打商品接口**（不碰真平台）：
  列表/同步 `count:2`、发布 `item_id:"PUB777"`、改价 2500→1999 `synced:true`、
  上下架 `ok:1 synced:1`、批量 `ok:2 synced:2`，并抓到全部出站 mtop 请求
  （版本号与 `publishScene`/价格单位分/类目与地址原样保留均可核对）。
- **真机（用户本人 + 真账号 + 打包 exe + 真实 `%APPDATA%` 数据）**：
  - 扫码登录**真的走完**：日志 `04:21:15 二维码状态: ... NEW -> SCANED` →
    `04:21:17 ... SCANED -> CONFIRMED raw={... 'loginResult': 'success' ... 'resultCode': 100 ...}`
    → 账号 `2217970177348` 建成且 `login_state=active`、`has_cookie=true`（缺陷 26 的
    修法在真机生效）。
  - 修完 53/54 后重启 exe，启动日志第一次出现
    `INFO backend.app.main:worker:122 - 账号 2217970177348 的 IM 入站已挂载`（此前每次
    只有「未挂载」WARNING）。
  - `POST /api/items/2217970177348/sync` → **200 `{"count": 20}`**，`GET /api/items?...`
    `total=20`、`/api/items/stats` `{"total":20,"by_status":{"on_sale":20},"inventory_value":4710.18}`
    —— **20 件真商品入库**；用真浏览器打开 `http://127.0.0.1:52200/#/items` 截图确认
    商品页「商品列表 共 20 条」、真标题/价格/在售状态/编辑·下架按钮齐全
    （`C:\Users\Administrator\.dsh-tmp\items_ui_real.png`）。
  - 令牌真的落库：`accounts.cookie` 里出现 `_m_h5_tk=3041b6979105a1a5b54bf2c8a64da9b7_1791245258792`，
    且整轮探针**不再手工重试**（重试发生在 `mtop()` 内部）。
- **真实桌面窗口**（`python desktop/main.py --port 18099`，真 WebView2）：
  自动登录后直接进概览页，左栏「实时连接已建立」、右上角「实时在线」。
- 前端构建产物被后端托管（`GET /` 返回真 `index.html`）；`npx tsc --noEmit` 0 error。
- **打包产物真跑**（`dist\闲鱼AI工作台\闲鱼AI工作台.exe`，真双击等价方式启动）：
  `/api/health` 200 `frozen:true`；`GET /` 真 index.html；`assets/index-*.js`
  200 且与 `frontend/dist` SHA256 一致（旧 hash 404）；`/api/settings` 401（API
  路由未被静态挂载吃掉）；`POST /api/auth/desktop` 200（桌面免密登录可用）。
  第八批重新打包后复验：产物 **9557793 字节**、`GET /` 里是 `index-x6fHQ3NA.js`、
  `POST /api/chats/sync` 401（= 已注册，不是 404）。
  **第十二批（本轮 UI 重做 + 审查修复之后）重新打包的最新值**：产物
  **9604897 字节**（`2026-10-07 06:18:36`）、`GET /` 里是 `index-AwCzqtxS.js`
  （332724 字节）+ `index-DAvta1sn.css`（97294 字节），与 `frontend/dist/assets`
  逐文件一致（SHA256 前缀 `EEB6B25024B1` / `BEFEF71FDF7D` 两边相同；`scripts/build_exe.ps1`
  自己也会打印「打包内前端: …（与快照逐文件一致）」）；`--port 18400` 上
  `/api/health` 200 `frozen:true`、`MainWindowTitle=闲鱼 AI 工作台`。
  上面第八批那行、以及本轮更早那次（9604905 字节 / `index-BfXGSc4s.js`，
  那是 P1/P7/P8 修之前的包）都是**历史记录**，别再当成当前产物。
  **第十三批（用户第六次实测的排序 / 售罄归下架 / 编辑页之后）重新打包的最新值**：产物
  **9608365 字节**（`2026-10-08 03:38:39`）、`GET /` 里是 `index-CxlIsVf9.js`
  + `index-Cuic_y7D.css`，与 `frontend/dist/assets` 逐文件一致（SHA256 前缀
  `E01170020E21` / `190900B87F1D`）；`--port 18400` 上 `/api/health` 200
  `frozen:true`。第十二批那次（9604897 字节 / `index-AwCzqtxS.js`）同样是历史记录。
  **第十四批（限流 / 多层信封 / 错误码词边界 / 前端三个小口子之后）重新打包的最新值**：产物
  **9609153 字节**（`2026-10-08 04:11:08`）、`GET /` 里是 `index-kmUrJvdH.js`（321.20 kB）
  + `index-iTNo5yli.css`（99.44 kB），与 `frontend/dist/assets` 逐文件一致（SHA256 前缀
  `D31F09CA6B20` / `F16E87DA0462`；`build_exe.ps1` 自报「打包内前端: …（与快照逐文件一致）」）；
  `--port 18400` 上 `/api/health` 200 `frozen:true`。第十三批那次（9608365 字节 /
  `index-CxlIsVf9.js`）同样是历史记录。
  **第二十一批（运费模版写能力 + 两处 UI 修正之后）重新打包的最新值**：产物
  **9976391 字节**（`2026-10-11 12:12:17`）、`GET /` 里是 `index-BOItszoH.js`（494.17 kB）
  + `index-Ckuj6T5I.css`（128.94 kB），与 `frontend/dist/assets` **逐字节一致**（两边 SHA256
  都是 `3432e2ade61a…` / `c4b85d1302c9…`，用 `C:\Users\Administrator\.dsh-tmp\check_assets.py`
  对 `http://127.0.0.1:18400/assets/*` 与磁盘各算一次比对，`ASSETS_MATCH=True`）；
  `--port 18400` 上 `/api/health` 200 `frozen:true`。第十四批那次（9609153 字节 /
  `index-kmUrJvdH.js`）同样是历史记录。
- **扫码登录真的通了**（唯一一处真打闲鱼平台的路径，且不涉及任何账号凭据）：
  - 源码直调：`_get_login_params` 拿到完整 `loginFormData`（`_csrf_token`/`umidToken`/
    `hsiz`/`bizParams=taobaoBizLoginFrom=web` 等），`_generate_qr` 拿到
    `code_content` 107 字符（`https://passport.goofish.com/qrcodeCheck.htm?lgToken=...`）。
  - **打包 exe 上**（`--no-window`）`POST /api/accounts/qr/start` → **200**，
    `ticket` + `expires_in=299` + 1222 字节真二维码 PNG（base64 1632 字符，可正常扫描）。
  - 修之前同一接口在同一个 exe 上是 **502 `http_error`**
    「获取二维码失败: 未能从 mini_login 页面解析出 viewData」—— 即用户看到的那条。
- **MCP 插件发现随机端口**（Lead 复跑）：后端 `--port 19107`（`XYWB_PORT=19107`）→
  `data/backend_port.txt` 内容 `19107`；**不设** `XYWB_BASE_URL` 真起
  `mcp_server/server.py` 子进程 → stderr 自报 `base_url=http://127.0.0.1:19107`
  `token 来源=file` `工具定义=backend.app.agents.tools.TOOL_SPECS`；`tools/list`
  返回 21 个工具；`tools/call providers_list` 返回真实 provider 列表；
  `items_get` 传 `account_id=../../etc` 返回 `code=not_found`（未穿越）。
- MCP 插件**已写进** `C:\Users\Administrator\.dsh\plugins\mcp-bridge.config.json`
  （`servers.xianyu`，备份在同目录 `.bak`），并用预检脚本按配置原样起过进程：
  `cwd=C:\Windows` + 显式清空 `PYTHONPATH` 仍能 `initialize` 并列出 21 个工具。

**未验证（不要当成已完成）**
- **物流「修改运单号」这个写接口一次真机请求都没发过**（第三十九批）：它会真的改动平台上已发货
  订单的物流信息，所以不能为了验证去动用户的订单。只验到「路由注册成功（`openapi` 181 路径 /
  195 操作）、body 被真实端点接受（对账脚本逐条核 path/query/body）、`confirm` 闸门拦得住
  （打包版闸门探针 53 条里含这条：`{confirm:false}` → 400 `confirmation_required`）、界面上不点
  最终确认就零写请求」。**没验到**：平台是否接受 `{tradeId,mailNo,cpCode}`、回执里有没有可用的
  编码回显（我们按「`echoed=False` 只加一条中文 warning、不谎称成功也不谎称失败」处理）、
  `cpCode` 回落到 `OTHER` 时平台认不认。
  **两条只读接口已经在真机上通了**：`logistics_read_probe.py`（打打包版/源码后端、只发 GET）
  拿到 `GET …/companies` → 200、**335 家快递公司**（海信物流 / 天喜物流 / 中通冷链 …）、
  `warnings: []`；`GET …/guess?mail_no=SF1234567890` → 200、`cp_code='SF'`、`cp_name='顺丰速运'`；
  `mail_no=ZZZ000` → 200、平台自己猜成 `EMS`（这条反过来说明「认不出」的分支要拿更假的单号才触发）。
  发货表单的下拉、「失焦认公司」、三种文案与确认框**没有在浏览器里人工点过**（只过了 `tsc` 与生产构建）。
- **订单处理的 5 个写接口一次真机请求都没发过**（第三十八批）：关单 / 延长收货 / 提醒确认收货 /
  支付运费退款 / 撤销投诉，全部会真的改动真实订单或**真的扣钱**（`postage.refund.pay` 从支付宝余额或
  保证金里扣给买家），所以不能为了验证去动用户的订单。只验到「路由注册成功（`openapi` 181 路径 /
  195 操作）、body 被真实端点接受、`confirm` 闸门拦得住（打包版闸门探针 53 条里含这 5 条）、
  界面上不点最终确认就零写请求」。**没验到**：平台是否接受
  `{tid,bizOrderId,closeReason}`（官方把同一个订单号同时塞进两个字段）/ `{orderIdList,delayTime}` /
  `{orderIdList,remindAllOrder}` / `{orderId,refundId,payType}` 这套 payload、
  `data.module.{totalNum,successNum,failOrderInfos}` 的真实回显形状。
  **关单原因那条只读接口已经在真机上通了**：`close_reasons_probe.py`（打打包版 18400、只发 GET）
  拿到 200 与官方五个原因（不想卖了 / 宝贝已出售 / 买家联系不上 / 与买家协商一致 / 其他原因），
  `raw_keys` 里确实有 `closeReasons`，`warnings: []`、`source: platform` ⇒ 接口名、参数形状与
  「字符串数组」的解析都被平台证实；同一轮 `POST .../orders/1/close` + `{confirm:false}` 回
  400 `confirmation_required`，证明新路由在打包版里已挂上且闸门生效。
- **话术（快捷回复）7 个写接口一次真机请求都没发过**（第三十七批）：只验到「路由注册成功、
  body 被真实端点接受（对账脚本逐条核 path/query/body）、`confirm` 闸门拦得住（打包版闸门探针
  47 条里含这 6 条）、界面上不点最终确认就零写请求」。**没验到**：平台是否接受
  `{groupName,operateType}` / `{groupId,contentId,operateType}` 这套 payload、
  `groupInfo.groupId` / `contentInfo.contentId` 的真实回显形状、「平台收了但不回显 id」那条分支
  （`no_group_id` / `no_content_id`）、有分组时列表接口的真实 `groups` 结构与字段名。
  **列表接口已经在真机上通了**：账号 `2217970177348` 重新扫码（`login_state='active'`）后，打包版
  只读探针（`C:\Users\Administrator\.dsh-tmp\qr_read_probe.py`，桌面密钥换 token、只发 GET）拿到
  `GET /api/quick-reply/2217970177348/groups` → **200**、`groups: []`、`count: 0`、**`warnings: []`**
  ⇒ 接口名与「不带参数」的调用形状被平台接受、账号有这一族的权限，且这是**真的空列表**
  （不是「键没认出来」——那会回 `groups: null` + `list_key_missing` warning）。
  分组/短语表格在真机上的渲染、批量选择、删除确认框也没有人工鼠标点过。
- **数据总览页四张经营卡片没有取到过真数据**（第三十五批）：真机只读探针
  （`C:\Users\Administrator\.dsh-tmp\cards_read_probe.py`，打打包版 18400、桌面密钥换 token、只发 GET）
  证明**四个接口名都被平台认下来了**（没有 `FAIL_SYS_API_NOT_FOUNDED`），但账号 `2217970177348`
  对四张卡都回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`（与数据总览/粉丝/客服同族，
  这个账号没有「数据」权限）⇒ **指标值、折线、`bannerDataList`/`graphDataList` 的真实字段名仍未知**。
  探针顺带抓到一个真缺陷并已修：协议层原先直接抛，界面上四张卡各是一个红框、框里糊着
  `协议层调用失败: MTOP 调用失败 [api]: FAIL_BIZ_…` 原文，而同一页的核心指标却是「人话 + 一屏 -」；
  现在四张卡与其它三页同口径（200 + 一句人话 warning），登录态过期/链路故障仍照抛。
  （探针那次 `login_state` 已是 `active` —— 用户 2026-10-13 01:22 重新扫过码。）
- **推广资金明细的 4 条只读接口没能在真机上跑通**（第三十三批）：探针脚本
  `C:\Users\Administrator\.dsh-tmp\funds_read_probe.py`（拷真实数据目录到临时根 → 从源码起后端 →
  桌面密钥换 token → **只发 GET**）打过去时，账号 `2217970177348` 已经是
  `login_state=need_relogin` ⇒ 四条都停在 409 `verification_required`（这是**正确行为**：
  没有一次平台请求被发出）。所以「平台是否接受 `{requestJson}` 里的 `pageNum`/`pageSize`/
  日期区间」「`data.result` 的字段名」目前只有 bundle 证据，等用户重新扫码后才能真机复核。
  空 `order_id` 的 400 `invalid_fields` 由单元测试覆盖（路径里留空段会先被路由判 404，属预期）。
- **客服数据页三张明细表的真值没取到过**（第三十六批）：三个接口（`cs.detail.list` /
  `cs.diversion.list` / `cs.evaluation.list`）的真机只读探针
  `C:\Users\Administrator\.dsh-tmp\cs_list_read_probe.py`（拷真实数据目录到临时根 → 从源码起后端 →
  桌面密钥换 token → **只发 GET**，报告 `logs/cs_list_read_probe.json`）结果是
  **三条都 200 + 一句人话 warning**：「平台拒绝访问客服明细/订单分流明细/客户评价明细接口：
  这个账号没有「数据」权限，对应指标显示 -。换个有数据权限的卖家账号再试。」
  ⇒ 说明**平台认下了这三个接口名与参数形状**（它没报参数错，走到权限判定才拒），
  但 `rows` 仍是 `null`（平台没给列表字段）、`total` 也是 `null`。
  同一次探针还验到两条**本地拦**（零平台调用）：`kind=bogus` → 400 `invalid_fields`
  「明细类型只能是 detail / diversion / evaluation」；只给 `order` 不给 `order_by` →
  400「指定排序方向时必须同时给出排序字段」。
  所以「三张表的真实列值」「`data.data.list` 真形状」「`extendInfo.realDateRange` 是否真有」
  「`total` 是否给数字」目前仍只有 bundle 证据 + 我们钉住的表头（12/3/6 列）；
  `rows === null` 与 `rows: []` 的分工在真机上还没见过一次。
  视觉复核（`cs_list_shots.py`，源码起后端、只切 tab、**不点任何写按钮**）确认 4 个 tab 名称、
  独立标题、只读说明都渲染正确，`page errors: []`、`non-GET api requests: []`、`PROBLEMS: []`，
  截图 `logs/cs_list_shots/{01-overview,02-detail,03-diversion,04-evaluation}.png`。
- **★ 探针/审计脚本必须先确认端口空闲**（第三十六批踩到的坑）：`cs_list_read_probe.py` 第一版用
  `PORT = 18473`，而那个端口**被上一次对比度审计留下的源码后端占着** ⇒ 探针其实在跟**旧代码**说话，
  五条请求全回 `404 not_found`（`/api/health` 与 `/api/auth/desktop` 却都 200，所以「后端起来了」的
  判断也会骗人）。现在探针先 `connect()` 探一次端口、占用就直接拒绝跑，并换了干净端口复跑。
  同一批清理时发现 `18381/18383/18385/18391/18393/18395/18473/18475/18501/18503` 十个残留后端
  仍在监听 —— 凡是从源码起后端的脚本，收尾都要确认子进程真的退出了。
- **客服分流的写接口一次真机请求都没发过**（第三十一批）：三条 POST（规则保存 /
  分组保存 / 分组删除）只验到「路由已注册 + body 被端点接受 + confirm 闸门拦得住 +
  界面上不点最终确认就一个写请求都不发」；平台是否接受 `HH:MM` 的 `serviceTime`、
  字符串数组 `customer_uids`、数组 `typeData` 都还不知道。官方的 `routingRules` /
  `csGroupInfos` 回显形状也没有真机样例，所以 `rule` / `group_id` 拿不到时一律 `null`
  + 中文 warning（不造值）。
- **真实闲鱼平台上已验证的只有「读」这一半**：扫码登录、拿 Cookie、拉在售商品列表
  （20 件真商品）都在真账号上跑通了。**写的一半仍没在真实账号上做过**：发布/编辑改价/
  上架下架/发消息/收私信是否被平台逐字节接受，必须等人工在真机上点一次；协议层其余
  部分仍走 MockTransport。第 11、12、13、28~31 条修的是「接线本身断掉」，接线通了
  不等于平台协议对。
- **店铺资料与子账号的写接口一次真机请求都没发过**（第三十二批）：10 条 HTTP（3 条店铺资料写 +
  5 条子账号写 + 2 条只读）只验到「路由已注册 + body 被端点接受 + 未确认一律 400 且零平台请求 +
  界面上不点最终确认就零写请求」；只读侧 `shop.manage.detail` 在真机是 200 有数据，而
  `sub.account.info.list` / `user.role.list` 对该账号是 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（没权限）。
  未验证的点：子账号密码的 **RSAES-PKCS1-v1_5 加密**平台是否接受（公钥格式只有 bundle 证据，
  没有真机 `sub.account.sub.pk` 响应样例；加密算法本身用内嵌测试密钥解回明文验过）、
  岗位列表真机形状（`roleId`/`roleName`）、店铺资料写回显。
- **真实 LLM 一次没调**：所有 AI 测试都 mock 了 `llm.chat`。
- **真实买家来信没收到过**：IM 入站现在在真机上**挂载成功了**（缺陷 53 修好的），
  但真平台的第一帧长什么样、`_read_loop → dispatch → 落库 → AI 回复 → 发回` 的整条
  链路**没有真实买家**验过；闭环本身只在假协议客户端上验过（`ensure_im` /
  `send_message` 两个口子打桩）。
- **WS 入站解析目前只有自造帧在验证**（第七批查清，比之前写得更准确）：真机 `app.log`
  4693 行里 `syncPushPackage` 745 次、`_warn_unparsed_frame` 4297 次、`contentType`
  **出现 0 次**；库里 `messages` 806 行的 `external_id` **806/806 以 `.PNM` 结尾**
  —— 也就是说**真机上还没有任何一条消息是从 WS 入站路径落库的**，全部来自
  `listUserMessages` 同步路径。所以「图片/语音/卡片帧」的解析规则是在自造帧上验证的，
  真机第一帧到了可能要按实际字段再调一次。
- **语音时长与 WebView2 播放未真机验证**：`audio_seconds()` 的毫秒/秒阈值（300）是启发式，
  真实平台给的单位还没见过；语音在 WebView2 里能否出声也没试过。
- **真机 IM 注册与历史回填没复验完**（第八批）：`/reg` 401
  `device id or appkey is not equal` 的根因（55）与历史会话不回填（56）都已在本地回归锁住，
  但复验时 mtop 取 IM token 撞上平台风控 `RGV587_ERROR`，冷却没过。**需要真人过一次滑块
  （或等冷却）后再跑一次「同步历史会话」，才能说真机这半边通了。**
  另外注意：短时间多次取 token 本身就会触发这个冷却（我们的复验探针就是触发源之一）。
- **真机滑块/验证码没做过**：`captcha` 成功分支（写 `mark_verified` 清冷却）只在
  单测里造过。
- 桌面 exe 在**无 WebView2** 的机器上的表现（本机已装 154.0.4258.53）。
- 带窗口的托盘交互未在**打包产物**上人工点过（开发态真窗口已验）。
- 前端仍无单元测试运行器：UI 只有真浏览器冒烟这一道闸门（10 页 × 白屏/登录态/
  控制台报错），页面内部的交互细节没有自动化覆盖。
- **MCP 注册要重启 DSH 宿主才生效**：配置已写、预检已过，但本会话是改配置前启动的，
  宿主级生效与否要重启后才能确认（留给机器主人决定时机）。
- **商品页三个写接口（批量下架 / 批量删除 / 快捷改价改库存）一次真机请求都没发过**
  （第二十二批）：四条 mtop 名与参数形状全部来自官方 bundle 静态提取，`itemIds` 的
  逗号串形态、`itemProcessResultList` 的真实字段名、`info.update` 在**真实 SKU 商品**上的
  行为（无 SKU 分支是扁平三字段、有 SKU 分支要 `itemSkuListStr`）都没被平台确认过。
  **账号现状（2026-10-12 读真实库）**：只剩一个账号 `2217970177348`（月狐兔_），
  `login_state='active'`、cookie 在、`last_login_at='2026-10-11 23:03:37'`；
  另一个账号 `3998190171`（卒奥aa）已被用户删除，其残留本地数据由 `purge_orphans` 清掉。
  真机点击这三个时要重点看：①批量下架是不是真的一个请求就全下架了；
  ②批量删除后平台侧商品是否真的消失（旧实现只删本地）；③改价/改库存后商品**没有被
  重新上架**（旧实现整份回提会把已卖出商品重新上架，这是新接口要解决的正是这个问题）。
- **退货地址的写接口对这个账号没有权限**（第二十五批，真机取证）：`…delivery.address.delete` /
  `set.default` 在**已登录**状态下回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`，
  与只读 `list.query` 是同一个权限问题 —— 实现正确，账号没有这个权限。所以「平台接受了我们的
  payload」这件事仍未验证；`areaId` 也没有码源（`division.all.get` 同样无权限）。
- **「验证闸门」的探针一度是写请求的来源**（第二十五批 161，已修）：2026-10-12 00:35 那版
  `probe_write_gate.py` 拿真账号 + `confirm:true` 打过去，三条写请求真的到了闲鱼
  （`ship_dummy` → `ORDER_STATUS_ERROR`；`address.delete`/`set.default` → 无权限），
  **平台全拒、零改动**，但流程本身是缺陷。现在确认腿换成不存在的账号（`SAFE_ACC`），
  闸门放行后本地 404，零平台请求。以后写任何「验证写接口」的探针都必须遵守这条。
- **推广 / 安全中心只做了「读」的四个视图，官方还有 6 块没做**（第十八批当时的边界）：
  充值页、账户明细流水、活动奖励、资质管理、新建/编辑擦亮计划、申诉入口。
  **第十九/二十批之后这条已经过时**：写接口（擦亮计划暂停/删除、申诉提交/催办、
  资质提交/OSS 凭据、充值/提现明确标记为「平台没有网页版下单接口」）与资质管理页都已落地，
  逐条口径见 `docs/CONTRACTS.md` 与 `docs/contracts/*.md`；**但那些写接口同样只有
  静态提取 + 闸门探针，没有一次真机提交**。已做的四个只读视图（余额与首页、擦亮计划表、
  计划+商品报表、结算）在真机取到真值。
- **推广与安全中心没有任何缓存**：每次打开页面都实打实打平台（`idle.ad.*` 与
  `idle.pc.security.*` 各 2~4 次），刷新频繁时会不会触发平台限流**没测过**；
  商品数据那套「后台 worker 慢慢拉 + 本地缓存」的降级策略**没有**用在这两页。

## 五、下一步（按优先级）

1. **登录态已经是好的**：真机账号 `2217970177348`（月狐兔_）在 `2026-10-11 23:03:37`
   重新扫码成功，`login_state='active'`、cookie 在库、`refresh_profile` 拿到 `nickname='月狐兔_'`。
   原先那条「Cookie 在 2026-10-07 被判过期、要先重新扫码」**已经过时**（另注：账号
   `3998190171`（卒奥aa）已被用户删除）。仍然需要登录态的功能现在可以直接验。
2. 用户在「AI 配置」页填一个真实 API Key 并设为默认 → 主管 AI 与商品 AI 才能真回话。
3. 真机实测「写」的一半：在已登录的真账号上发布一件商品 / 改一次价 / 上架下架各一次
   （读的一半——拉商品、IM 入站挂载——已在真机验证）。发布失败时注意看新加的人话解释
   （`platform_errors.py`）。**优先先点第二十二批那三个（商品页的批量下架 / 批量删除 /
   行内改价改库存）**：它们是唯一「旧做法会改坏真实数据」的三个口子，而且一次请求就能
   验完（不像发布要填一整个表单）。**别拿退货地址当第一批试写**：这个账号对
   `…delivery.address.*` 没有权限（真机回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`），
   试了也只会得到「无权限」，验不出 payload 对不对。
4. 重启 DSH 宿主让 MCP 插件 `xianyu` 生效，再用主管 AI 直接操作工作台。
5. 风控冷却过后，真机点一次「同步历史会话」（会话页工具栏）：应看到
   `success=true` 且 `conversations_new > 0`，会话页出现真实买家会话 —— 这是第八批
   唯一还没在真机上闭环的实证。
6. 桌面 exe 在干净机器上冒烟（缺 WebView2 时要给出可读提示）；注意产物现在含
   playwright driver，约 110 MB。
