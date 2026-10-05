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
-> TOTAL 13  PASS 13  FAIL 0
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
| ④ `POST /api/items/{id}/batch-edit` | 200，逐条 `{total,ok,failed[]}` 汇总 |
| ⑤ 人工发消息 + 落库 | 200，`GET /api/chats/1/messages` 能读回 |
| ⑥ `POST /api/chats/1/ai-draft` | 400 `invalid_fields`（会话没有买家消息，契约行为） |
| ⑦ `POST /api/agents/master/chat` | 502 `调用模型失败：网络错误`（provider 指向死端口，未 500） |
| ⑧ 写商品级提示词 | 200，`scope=item` 落库 |
| ⑨ 风控 status / clear | 200 / **409** `verification_required`（未验证不给清冷却） |
| ⑩ operation-logs | 200，写操作逐条留痕 |
| ⑪ 真实 WebSocket | 收到 `{"topic":"accounts.changed",...}` |

**安全约束**：provider `base_url` 指向 `http://127.0.0.1:9`（discard 端口），账号
cookie 留空 → 全程不打真实闲鱼平台、不花真实 API 费用。

## 三、本轮集成暴露并修掉的真缺陷

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

## 四、已验证 / 未验证（诚实边界）

**已验证**
- 全部后端逻辑：13 个测试文件全绿，含真实子进程 MCP 冒烟。
- 真实 HTTP：只读 17 条 + 写路径 11 条，均结构化信封、无 500。
- 真实 WebSocket 事件投递。
- 前端构建产物被后端托管（`GET /` 返回真 `index.html`）；`npx tsc --noEmit` 0 error。
- **打包产物真跑**（`dist\闲鱼AI工作台\闲鱼AI工作台.exe`，真双击等价方式启动）：
  `/api/health` 200 `frozen:true`；`GET /` 真 index.html；`assets/index-BK87FCtW.js`
  200 LEN=237390（旧 hash 404）；`/api/settings` 401（API 路由未被静态挂载吃掉）；
  `POST /api/auth/desktop` 200（桌面免密登录在打包形态下可用）。
- **MCP 插件发现随机端口**：不设 `XYWB_BASE_URL`，真随机端口 62405 的后端被
  `data/backend_port.txt` 找到并成功调用 `providers_list`。

**未验证（不要当成已完成）**
- **真实闲鱼平台一次没打**：协议层全部走 MockTransport。发布/编辑/上下架/发消息
  在真实账号上是否成功，必须等用户扫码登录后实测。
- **真实 LLM 一次没调**：所有 AI 测试都 mock 了 `llm.chat`。
- **前端 UI 未逐页人工点击**（本机无 headless 浏览器）：验到的是 tsc 0 error、
  构建成功、产物内九页标签齐全、后端托管 200、契约测试 3/3。
- 桌面 exe 在**无 WebView2** 的机器上的表现（本机已装 154.0.4258.53）。
- MCP 插件尚未真正写进 `~/.dsh/plugins/mcp-bridge.config.json` 并重启 DSH 宿主
  （宿主级改动，留给机器主人决定）。
- 真实窗口（`webview.start()`）只在开发态 `--no-window` 与打包产物 `--no-window`
  下验过；带窗口的托盘交互未在打包产物上人工点过。

## 五、下一步（按优先级）

1. 用户在「AI 配置」页填一个真实 API Key 并设为默认 → 主管 AI 与商品 AI 才能真回话。
2. 用户扫码登录一个真实闲鱼账号 → 才能实测商品发布/编辑与真实收发的消息。
3. 把 MCP 插件挂进 DSH 宿主配置（需机器主人确认，属宿主级改动）。
4. 桌面 exe 在干净机器上冒烟（缺 WebView2 时要给出可读提示）。
