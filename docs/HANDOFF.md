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
-> TOTAL 20  PASS 20  FAIL 0  CASES 380  SKIPPED 1
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_xianyu_login.py"
-> Ran 22 tests ... OK (skipped=1)
```

```text
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe scripts\ui_smoke.py --port 18097"
-> UI SMOKE ALL OK  (9/9 页通过)
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe scripts\smoke_api.py 18090"
-> SMOKE ALL OK        （17 条只读接口全 200 且统一信封；无 token 401）
```

```text
cmd /c "set PYTHONPATH=.&& C:\Python310\python.exe scripts\smoke_write.py 18090"
-> WRITE SMOKE ALL OK  （11/11，见下）
```

### 写路径冒烟逐条结果

| 项 | 结果 |
| --- | --- |
| ① 新增 provider / 设为默认 | 200 `{"success":true,...}` |
| ② PATCH 账号 + 列表确认 | 200，备注与开关都生效 |
| ③ 发布 / 同步商品 | **503** `code=xianyu_not_ready`（协议层依赖不可用，非 500） |
| ④ `POST /api/items/{id}/batch-edit` | 200，逐条 `{total,ok,synced,local_only,warnings[],failed[],skipped[]}` 汇总 |
| ⑤ 人工发消息 + 落库 | 200，`GET /api/chats/1/messages` 能读回；`sent_ok=false` + warning（没 Cookie） |
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

## 四、已验证 / 未验证（诚实边界）

**已验证**
- 全部后端逻辑：**20 个测试文件、380 个用例全绿、0 失败**（唯一 1 个 skip 是
  `test_xianyu_login.py` 里本来就有的平台相关跳过）。含真实子进程 MCP 冒烟、
  真实 WS 事件投递、协议层接线（不注入假工厂，真走 store → XianyuClient）、
  IM 入站闭环（帧 → 落库 → AI 回复 → 真的发回去）、风控回灌与衰减。
- 真实 HTTP：只读 17 条 + 写路径 11 条，均结构化信封、无 500。
- **真实浏览器逐页冒烟 9/9 页通过**（`scripts/ui_smoke.py`，Edge/WebView2 引擎，
  临时数据目录，逐页断言未白屏、未掉回登录页、无 JS 报错，截图在 `logs/ui_smoke/`）。
  它抓到了后端全绿时仍然「UI 完全不可用」的登录死循环。
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
