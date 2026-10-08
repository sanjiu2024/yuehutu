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
-> TOTAL 31  PASS 31  FAIL 0  CASES 941  SKIPPED 2
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_xianyu_login.py"
-> Ran 23 tests ... OK (skipped=1)
```

```text
cmd /c "cd frontend && npx tsc --noEmit"
-> TSC_EXIT=0
cmd /c "cd frontend && npm run build"
-> BUILD_EXIT=0   frontend/dist/assets/index-BtOoYIBj.js (331.97 kB) + index-RKsxVLgj.css (103.56 kB)
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

## 四、已验证 / 未验证（诚实边界）

**已验证**
- 全部后端逻辑：**31 个测试文件、941 个用例全绿、0 失败**（2 个 skip：`test_xianyu_login.py`
  里本来就有的平台相关跳过、以及需要真人配合的那条）。含真实子进程 MCP 冒烟、
  真实 WS 事件投递、协议层接线（不注入假工厂，真走 store → XianyuClient）、
  IM 入站闭环（帧 → 落库 → AI 回复 → 真的发回去）、风控回灌与衰减。
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
- **真实闲鱼平台上已验证的只有「读」这一半**：扫码登录、拿 Cookie、拉在售商品列表
  （20 件真商品）都在真账号上跑通了。**写的一半仍没在真实账号上做过**：发布/编辑改价/
  上架下架/发消息/收私信是否被平台逐字节接受，必须等人工在真机上点一次；协议层其余
  部分仍走 MockTransport。第 11、12、13、28~31 条修的是「接线本身断掉」，接线通了
  不等于平台协议对。
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
- 前端仍无单元测试运行器：UI 只有真浏览器冒烟这一道闸门（9 页 × 白屏/登录态/
  控制台报错），页面内部的交互细节没有自动化覆盖。
- **MCP 注册要重启 DSH 宿主才生效**：配置已写、预检已过，但本会话是改配置前启动的，
  宿主级生效与否要重启后才能确认（留给机器主人决定时机）。

## 五、下一步（按优先级）

1. **先重新扫码一次**：真机账号 `2217970177348` 的 Cookie 在 `2026-10-07 04:47:16`
   被平台判过期（`FAIL_SYS_SESSION_EXPIRED::Session过期`，日志与
   `account_credentials.relogin_reason` 两边对得上），之后所有需要登录态的接口都会
   如实回「需要重新扫码登录」。这不是程序误判 —— 重新扫一次就恢复。
2. 用户在「AI 配置」页填一个真实 API Key 并设为默认 → 主管 AI 与商品 AI 才能真回话。
3. 真机实测「写」的一半：在已登录的真账号上发布一件商品 / 改一次价 / 上架下架各一次
   （读的一半——拉商品、IM 入站挂载——已在真机验证）。发布失败时注意看新加的人话解释
   （`platform_errors.py`）。
4. 重启 DSH 宿主让 MCP 插件 `xianyu` 生效，再用主管 AI 直接操作工作台。
5. 风控冷却过后，真机点一次「同步历史会话」（会话页工具栏）：应看到
   `success=true` 且 `conversations_new > 0`，会话页出现真实买家会话 —— 这是第八批
   唯一还没在真机上闭环的实证。
6. 桌面 exe 在干净机器上冒烟（缺 WebView2 时要给出可读提示）；注意产物现在含
   playwright driver，约 110 MB。
