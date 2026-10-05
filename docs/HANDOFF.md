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
| 4 | 商品批量修改 | `services/items.py::batch_update` | `test_services_items.py` + 冒烟 ④ |
| 5 | 统一 API Key 管理 | `providers` 表 + 「AI 配置」页 | 冒烟 ① |
| 6 | 主 AI 对接我 / 插件让 DSH 控制 | `POST /api/agents/master/chat` + `mcp_server/` | 冒烟 ⑦；`test_mcp_server.py` 55 项；端口文件端到端 |
| 7 | 除 AI 外我自己也能回消息 | `POST /api/chats/{id}/send` | 冒烟 ⑤（消息落库） |
| 8 | 不能老登录、风控要做好 | `xianyu/risk.py` 熔断 + `xianyu/login.py` 人工验证 | `test_xianyu_cookies.py` 37 项 |
| 9 | 集成闲鱼客服 / 网页版功能 | `services/cs.py` 意图分类 + 快捷回复 | `test_services_cs.py` 20 项 |

## 二、验收基线（真实命令与输出）

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\run_all.py"
-> TOTAL 24  PASS 24  FAIL 0  CASES 504  SKIPPED 1
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_xianyu_login.py"
-> Ran 23 tests ... OK (skipped=1)
```

```text
cmd /c "cd frontend && npx tsc --noEmit"
-> TSC_EXIT=0
cmd /c "cd frontend && npm run build"
-> BUILD_EXIT=0   frontend/dist/assets/index-CZsX4qcD.js (231.51 kB)
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe scripts\ui_smoke.py --port 18097"
-> UI SMOKE ALL OK  (9/9 页通过)   浏览器：Edge（与桌面版 WebView2 同引擎）
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

## 四、已验证 / 未验证（诚实边界）

**已验证**
- 全部后端逻辑：**24 个测试文件、504 个用例全绿、0 失败**（唯一 1 个 skip 是
  `test_xianyu_login.py` 里本来就有的平台相关跳过）。含真实子进程 MCP 冒烟、
  真实 WS 事件投递、协议层接线（不注入假工厂，真走 store → XianyuClient）、
  IM 入站闭环（帧 → 落库 → AI 回复 → 真的发回去）、风控回灌与衰减。
- 真实 HTTP：只读 17 条 + 写路径 11 条，均结构化信封、无 500。
- **真实浏览器逐页冒烟 9/9 页通过**（`scripts/ui_smoke.py`，Edge/WebView2 引擎，
  临时数据目录，逐页断言未白屏、未掉回登录页、无 JS 报错，截图在 `logs/ui_smoke/`）。
  它抓到了后端全绿时仍然「UI 完全不可用」的登录死循环。
- **真实浏览器实时刷新 2/2 阶段通过**（`scripts/live_refresh_smoke.py`）：后端写账号
  备注 / 新增提示词后，页面在 0.21 秒内自己更新，且 `navigation` 计数不变。
- **真后端 + 真 `XianyuClient` + `httpx.MockTransport` 逐条打商品接口**（不碰真平台）：
  列表/同步 `count:2`、发布 `item_id:"PUB777"`、改价 2500→1999 `synced:true`、
  上下架 `ok:1 synced:1`、批量 `ok:2 synced:2`，并抓到全部出站 mtop 请求
  （版本号与 `publishScene`/价格单位分/类目与地址原样保留均可核对）。
- **真实桌面窗口**（`python desktop/main.py --port 18099`，真 WebView2）：
  自动登录后直接进概览页，左栏「实时连接已建立」、右上角「实时在线」。
- 前端构建产物被后端托管（`GET /` 返回真 `index.html`）；`npx tsc --noEmit` 0 error。
- **打包产物真跑**（`dist\闲鱼AI工作台\闲鱼AI工作台.exe`，真双击等价方式启动）：
  `/api/health` 200 `frozen:true`；`GET /` 真 index.html；`assets/index-*.js`
  200 且与 `frontend/dist` SHA256 一致（旧 hash 404）；`/api/settings` 401（API
  路由未被静态挂载吃掉）；`POST /api/auth/desktop` 200（桌面免密登录可用）。
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
- **真实闲鱼平台只碰了「扫码取码」这一步**：二维码能出、能被手机扫，但**扫码之后的
  登录确认与拿 Cookie 没走过**（要用户本人拿手机扫）。发布/编辑/上下架/发消息/收私信
  在真实账号上是否成功，必须等登录实测；协议层其余部分全部走 MockTransport。第 11、
  12、13 条修的是「接线本身断掉」，接线通了不等于平台协议对。
- **真实 LLM 一次没调**：所有 AI 测试都 mock 了 `llm.chat`。
- **真实买家来信没收到过**：IM 入站闭环只在假协议客户端上验过（`ensure_im` /
  `send_message` 两个口子打桩），真 WS 收帧形状与 `_read_loop → dispatch` 的端到端
  没打过真平台。
- **真机滑块/验证码没做过**：`captcha` 成功分支（写 `mark_verified` 清冷却）只在
  单测里造过。
- 桌面 exe 在**无 WebView2** 的机器上的表现（本机已装 154.0.4258.53）。
- 带窗口的托盘交互未在**打包产物**上人工点过（开发态真窗口已验）。
- 前端仍无单元测试运行器：UI 只有真浏览器冒烟这一道闸门（9 页 × 白屏/登录态/
  控制台报错），页面内部的交互细节没有自动化覆盖。
- **MCP 注册要重启 DSH 宿主才生效**：配置已写、预检已过，但本会话是改配置前启动的，
  宿主级生效与否要重启后才能确认（留给机器主人决定时机）。

## 五、下一步（按优先级）

1. 用户在「AI 配置」页填一个真实 API Key 并设为默认 → 主管 AI 与商品 AI 才能真回话。
2. 用户扫码登录一个真实闲鱼账号 → 才能实测商品发布/编辑、真实收发消息、真实风控。
3. 重启 DSH 宿主让 MCP 插件 `xianyu` 生效，再用主管 AI 直接操作工作台。
4. 桌面 exe 在干净机器上冒烟（缺 WebView2 时要给出可读提示）。
