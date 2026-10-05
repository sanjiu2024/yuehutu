# 审计报告：账号登录 / Cookie 复用 / 风控

- 审计人：teammate `audit-accounts`（task-14），**只读审计**，未修改任何源码（`git status --short` 仅有 `?? docs/audit/`）。
- 仓库：`C:\Users\Administrator\Projects\xianyu-ai-workbench`；审计时间 2026-10-06。
- 环境：`C:\Python310\python.exe`，测试须 `set PYTHONPATH=.` + `set PYTHONIOENCODING=utf-8`；所有会写数据的实验一律用临时 `XIANYU_APP_DIR`。
- 真机只读探测：允许范围=passport 取码/轮询；**没有**用真实账号扫码确认，**没有**打真实 LLM。

## 0. 结论总表（逐项真实判定）

| # | 项目 | 判定 | 一句话 |
|---|---|---|---|
| 1 | 取二维码（缺陷①修复复核） | **通**（真机取证） | `qr/start` 真机返回二维码，`query.do` 收到全参数 |
| 2 | 扫码状态机 pending/scanned/confirmed/expired | **通**（mock HTTP 全链路） | 契约状态与前端 `QR_STATE_TEXT` 一一对上 |
| 3 | 扫码确认→建账号落库（缺陷②修复复核） | **mock 级通；真机级未验证** | 唯一真机全链路测试被静默跳过（见 §7-A） |
| 4 | `verification_required` 不再被映射成 `failed` | **通** | 契约输出 `scanned`，轮询不被停 |
| 5 | Cookie 落库 + 重启复用 | **部分通** | 登录 Cookie 落库可用；**过期无回写、mtop 续期不落库** |
| 6 | 账号增删改启停 + 事件 | **部分通** | PATCH/toggle/delete 都发 `accounts.changed`；relogin 不发；IM 不跟随 |
| 7 | QR ticket 过期清理 | **不通** | `cleanup_expired()` 零调用点 |
| 8 | 风控 record_hit / 冷却 / 闸门 | **通** | 协议层命中→库→内存锁→发消息闸门挡下，全链路实测 |
| 9 | `clear` 不自证 verified | **通**（历史缺陷已修） | 带 `{"verified": true}` 也 409 |
| 10 | 人工验证写入 verification_type | **通，但可被旁路** | cookie 含 x5sec 时点一次「验证」即解锁（§7-F） |
| 11 | 账号级「自动回复/启用」开关生效 | **不通** | 开关在整个回复链路里从未被读取 |
| 12 | 登录/启用账号后能收私信 | **不通** | IM 入站只在进程启动时挂载 |
| 13 | Cookie 过期→前端提示重新登录 | **不通** | `SessionExpiredError` 无人捕获，`login_state` 永远 `active` |

---

## 1. 扫码登录全流程：状态映射与前后端对齐

### 1.1 结论
**通**。后端 6 个契约状态（`pending/scanned/confirmed/expired/failed`）由
`backend/app/api/accounts.py:69-88` 的 `_STATE_MAP` + `_to_contract_state` 产出，
前端 `frontend/src/pages/Accounts.tsx:10-16` 的 `QR_STATE_TEXT` 六个键完全覆盖，字符串一一对上。

映射表（`backend/app/api/accounts.py:69-76`）：

| login 内部状态 | 契约 `state` | 前端文案 | 轮询是否继续 |
|---|---|---|---|
| `waiting` | `pending` | 请用闲鱼 App 扫描二维码 | 继续 |
| `scanned` | `scanned` | 已扫码，请在手机上确认登录 | 继续 |
| `verification_required` | **`scanned`**（关键：不是 `failed`） | 已扫码，请在手机上确认登录 | **继续**（正是不能被判死的原因） |
| `success` | `confirmed` | 登录成功！ | 停止（900ms 后关弹窗） |
| `expired` | `expired` | 二维码已过期，请重新获取 | 停止 |
| `cancelled` | `failed` | 登录失败，请重新获取二维码 | 停止 |
| 其他/未知 | **`failed`** | 登录失败，请重新获取二维码 | 停止 |

`frontend/src/pages/Accounts.tsx:304-310`：只有 `'expired'|'failed'` 会置 `doneRef=true` 停轮询。

### 1.2 证据（原样命令 + 关键输出）
```
cd C:\Users\Administrator\Projects\xianyu-ai-workbench
cmd /c "set XIANYU_APP_DIR=%TEMP%\xywb_probe_login_dir&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe %TEMP%\xywb_probe_login.py"
```
探针用 `TestClient(main.app)` + `backend/tests/test_xianyu_login._FakePlatform`（httpx.MockTransport）走完整 HTTP 链路，关键输出：
```
--- qr/start success: True
--- qr/start: {'ticket': True, 'b64_len': 1296, 'expires_in': 299}
--- poll1: {'state': 'pending'}
--- poll2: {'state': 'scanned'}
--- poll3: {'state': 'confirmed', 'account_id': '99887766'}
--- poll4(成功后): {'state': 'confirmed', 'account_id': '99887766'}
--- poll5(成功后): {'state': 'confirmed', 'account_id': '99887766'}
--- accounts: [{'id':'99887766','nickname':'闲鱼测试号','avatar_url':'','enabled':False,'auto_reply':True,
                'login_state':'active','has_cookie':True,'has_x5sec':True,'blocked':False,
                'remaining_seconds':0,'consecutive_hits':0,'runtime_state':'stopped'}]
--- store.get_cookie(99887766)[:60]: _m_h5_tk=token123_1700000000000; cookie2=c2; unb=99887766; x...
--- verify 轮询#1: {'state': 'scanned', 'message': '需要完成手机/人脸验证'}
--- verify 轮询#2: {'state': 'scanned', 'message': '需要完成手机/人脸验证'}
--- 无 unb 轮询: {'state': 'pending', 'message': '扫码确认响应未包含 unb'}
--- 无 unb 后账号数: 1
```
（最后一行说明「CONFIRMED 但响应里没有 unb」不会建空壳账号——这是正确的；但它返回 `pending`+提示文案，
前端会继续显示「请用闲鱼 App 扫描二维码」，此时用户其实已经扫过了，属**文案误导**，见 §7-O。）

### 1.3 状态机分支代码路径（`backend/app/xianyu/login.py:271-358`）
- `CONFIRMED`（:309）：`_apply_cookies` 收 Set-Cookie 与 `data['unb']`；
  - 若 `data.iframeRedirect is True` 且无 unb → 置 `verification_required` + `extend_for_verification()`（+300s，:44-45），返回该状态且**不退出**；
  - 无 unb → `session.failure='扫码确认响应未包含 unb'`，返回 `STATUS_WAITING`（:325-327）；
  - 否则 → `success` → `_fill_profile` → `_success_payload`。
- `NEW`（:332）→ `waiting`；`EXPIRED`（:338）→ 若当前是 `verification_required` 则返回 `verification_required('等待手机端完成验证')`，否则 `expired`+`'二维码已过期'`；
- `SCANED`（:350）→ 仅在当前是 `waiting` 时置 `scanned`；未知/空（:356）→ 返回当前状态不判死。

### 1.4 缺陷①/②修复复核（背景里点名的两处）
- **① 取二维码失败「未能从 mini_login 页面解析出 viewData」**：`login_query()`（`backend/app/xianyu/login.py:61-81`）是参数唯一真源；`_get_login_params`（:188-207）与 `_generate_qr`（:209-230）都发 `params=..., headers=self.headers`。**真机取证通过**（见 §6.1）。
- **② 扫完码被判「二维码已过期」**：`poll()`（:283-291）`client.post(query.do, data=dict(session.params), headers=self.headers)`，
  `session.params` = `loginFormData`（含 `umidToken`/`_csrf_token`/`hsiz`/`fromSite`/`returnUrl`/`mainPage`）+ `umidTag` + `t` + `ck`。
  探针抓到 mock 服务端实际收到的表单键：
  ```
  --- query.do 实际表单体(键): ['_csrf_token','appEntrance','appName','bizParams','ck','fromSite','hsiz',
                               'isMobile','lang','mainPage','returnUrl','t','umidTag','umidToken']
  ```
  修复完整（`umidToken`、`t`、`ck` 都在）。**同类残留点：`_get_mh5tk`（:174-186）的 GET/POST 两个请求既不带 params 也不带 headers** —— 目前真机可用，但属同一类「指纹没发全」的隐患（见 §7-M）。
- **是否存在漏改的第三处**：全仓库 grep `check\.do|refresh|mini_login|generate\.do|query\.do|passport|login_query`
  → **没有 `check.do`，也没有独立的「刷新 Cookie」接口**，passport 相关端点只存在于 `login.py`，所以不存在漏改的同族接口。
  但同一主题下有一处**真实缺口**：Cookie 续期不落库（§4.2）。

---

## 2. Cookie 持久化与「不能老登录」

### 2.1 登录成功后的落库路径（通）
`backend/app/api/accounts.py:142-165 _poll_ticket`：`status=='success'` 且 `user_id` 非空 →
```
store.upsert_account(account_id=user_id, nickname=data.get('nickname') or '',
                     avatar_url=data.get('avatar_url') or '', xianyu_user_id=user_id,
                     cookie=cookie, login_state=store.STATE_ACTIVE)
store.log_operation(user_id, 'qr_login', success=True)
_publish('accounts.changed', {'account_id': user_id, 'reason': 'qr_login'})
```
证据：探针 `store.get_cookie('99887766')` 已见 `_m_h5_tk=token123_...; cookie2=c2; unb=99887766`；
`GET /api/accounts` 里 `login_state='active'`、`has_cookie=true`。
重启复用：`store.get_cookie`（`backend/app/xianyu/store.py:108`）不依赖进程状态，`risk_service.resync_from_db()`（`backend/app/services/risk_service.py:298-322`）在启动时回灌风控冷却 —— **Cookie 复用本身在代码上成立**。

### 2.2 缺陷：mtop 续期的 `_m_h5_tk` 从不落库（严重）
- `backend/app/xianyu/client.py:184`：`self.cookie_str = mtop.merge_token_cookies(self.cookie_str, response)` —— **只改内存**。
- `backend/app/services/common.py:204-211`：`get_client()` 每次调用都 `cls(cookie)` 新建客户端，`cookie` 来自 `store.get_cookie(account_id)`。
- 生产写库只有两处：`backend/app/api/accounts.py:240`（relogin 清空）与 `:284`（人工验证成功）。
  证据（grep 全仓库 `set_cookie|cookie_str|cookies_str` 共 56 处命中，写库仅上述两处）：
  ```
  backend/app/api/accounts.py:240  store.set_cookie(account_id, '', xianyu_user_id=account_id, login_state=...
  backend/app/api/accounts.py:284  store.set_cookie(account_id, result.get('cookie') or cookie, ...)
  ```
- 后果：服务层每次调用都从库里那份**登录时刻**的 token 重新开始；一旦库里 `_m_h5_tk` 过期，
  后续调用会持续用旧 token 签名失败，而且**永远不会自我修复**（每次新建的客户端吸收到的新 token 随对象一起丢掉）。
- 判定：代码路径**已确证**；「真实账号是否因此需要重新登录」**未验证**（要真机跑业务接口，超出允许范围）。

### 2.3 缺陷：Cookie 过期没有任何回写路径（严重）
- `backend/app/xianyu/client.py:119-121` 抛 `SessionExpiredError`；`:285` 图片上传也抛。
- `backend/app/xianyu/risk.py:5-8` 的注释写明「调用方据此把账号置…」，但 **全仓库没有任何生产代码捕获它**：
  ```
  grep -rn "SessionExpiredError" backend/app  →  只有 client.py:52(定义)/:121/:285 与注释、risk.py:47/106/207
  grep -rn "mark_need_relogin|STATE_NEED_RELOGIN" backend  →  生产只有 backend/app/api/accounts.py:242（用户手动 relogin）
                                                           其余只在 backend/tests/test_xianyu_login.py:442-447
  ```
- `backend/app/services/common.py:255-256` 把它统一降级成 `protocol_error` 信封（`协议层调用失败: ...`）。
- 后果：Cookie 真的失效时，`account_credentials.login_state` 仍是 `active`，
  `frontend/src/format.ts:47-71 loginStateLabel('active')` → 「已登录」，用户看不到「需重新扫码」，
  只看到笼统的协议错误。这正是用户「不能老登录」诉求的反面。

---

## 3. 账号增删改启停 与 QR ticket 清理

### 3.1 事件发布（部分通）
| 操作 | 端点 | `_publish('accounts.changed')` | 位置 |
|---|---|---|---|
| 扫码登录成功 | `GET /qr/{ticket}` | ✅ | `backend/app/api/accounts.py:164` |
| PATCH 局部更新 | `PATCH /{id}` | ✅ | `:213` |
| 启停别名 | `POST /{id}/toggle` | ✅ | `:226` |
| 删除 | `DELETE /{id}` | ✅ | `:310` |
| **重新登录** | `POST /{id}/relogin` | ❌ **缺失** | `:230-255`（只写库 + `start_qr_login()`） |

`relogin` 探针证据（临时目录）：
```
--- POST relogin: (200, True)
--- login_state (relogin 后): {'login_state': 'need_relogin', 'relogin_reason': '用户手动触发重新登录', 'cookie_len': 0}
```
库里状态是对的（且与前端 `loginStateLabel('need_relogin')` →「需重新扫码」一致），
但列表页收不到 WS 事件，只能靠 900ms 后的主动刷新/手动刷新。

另：`DELETE`（`:304-311`）**没有 `store.log_operation`**，其余写操作都有。
`runtime_state` 确认**不是 db 列**：`backend/app/api/accounts.py:91-100 _runtime_state()` 由 `blocked/enabled` 派生（`risk/running/stopped`）；
真实列是 `account_credentials.login_state`（`backend/app/xianyu/store.py` 文件头注释 + `:79-98 list_accounts()`）。

### 3.2 新登录账号默认是「已停止」（观察，非缺陷）
`backend/app/xianyu/store.py:41-76 upsert_account` 的 INSERT 默认 `enabled=0, auto_reply=1`（`backend/app/core/db.py:50-51`），
所以刚扫码成功的账号 `enabled=false` → `_runtime_state` → `stopped`，前端「已停止」。
探针输出证实：`'enabled': False, ... 'runtime_state': 'stopped'`。
真机窗口里那个账号是 `enabled: true`（用户手动开过）。这是有意的「不覆盖用户开关」语义，但**新账号需要用户再点一次启用**才会收消息。

### 3.3 缺陷：`cleanup_expired()` 零调用点
```
--- backend.app.xianyu.login 里 cleanup_expired 出现次数: 1     （仅 backend/app/xianyu/login.py:165 定义）
--- backend.app.api.accounts 里 cleanup_expired 出现次数: 0
--- backend.app.main 里 cleanup_expired 出现次数: 0
```
（同一次探针的输出。）ticket 会话对象常驻 `QRLoginManager.sessions`（`login.py:143`），含 cookie 串。
功能上 `get_session()`（`login.py:158-163`）会惰性把过期会话标成 `expired`，所以**不会误判可用**；
但内存只增不减，长期运行缓慢泄漏。

---

## 4. 风控链路（生产调用点 + 闸门）

### 4.1 结论：**通**，全链路实测（历史「零调用」问题已修）
证据（探针脚本输出，同一进程内 HTTP + 协议层混合）：
```
--- risk_state 行数(新账号): 0
--- 协议层异常类型: RiskControlError
--- risk_state: {'account_id': '9001', 'consecutive_hits': 1, 'blocked_until': 1791232570.395408,
                 'verification_type': '', 'last_hit_at': 1791232510.395408}
--- risk.registry.get(9001).is_blocked: True
--- chats._account_in_risk_cooldown: True
--- GET /api/risk/status: {'states': [{'account_id':'9001','consecutive_hits':1,'blocked':True,
     'remaining_seconds':59,...}], 'blocked': ['9001'], 'cooldown_steps': [60,180,300,600,1200],
     'auto_slider': False, 'refresh_interval_seconds': 1200}
--- deliver_ai_reply(冷却中): {'sent': False, 'code': 'risk_cooldown',
     'message': '账号处于风控冷却中，AI 回复未发送（不自动重试）'}
```
对应代码路径：
- 协议层命中：`backend/app/xianyu/client.py:112-128 _check_response()` →
  `risk.registry.trip(cookie_id, message, retry_after)`（`backend/app/xianyu/risk.py:202-212`，先判 `is_session_expired` 再熔断）。
- 服务层落库：`backend/app/services/risk_service.py:283-295` 模块底部 `protocol_risk.set_hit_handler(_on_protocol_hit)`（import 即生效）
  → `record_hit`（:198-231，命中前会 `INSERT INTO risk_state` 建行，`risk_service.py:79-82`）。
- 闸门：`backend/app/services/chats.py:592-603 _account_in_risk_cooldown()` → `deliver_ai_reply`（:649-715）在发消息前拦下，
  `code='risk_cooldown'`、不自动重试。
- 重启回灌：`backend/app/main.py:81 resync_from_db()`；`clear` 后 `registry.reset`。

### 4.2 `clear` 不自证 verified（历史缺陷**已修**）
```
--- POST /api/risk/9001/clear {}: (409, {'success': False,
      'message': '未检测到人工验证成功，拒绝清除冷却', 'code': 'verification_required'})
--- POST /api/risk/clear {verified:true}: (409, {'success': False,
      'message': '未检测到人工验证成功，拒绝清除冷却', 'code': 'verification_required'})
```
请求体里的 `verified: true` 被**明确忽略**（`backend/app/services/risk_service.py:251-277 clear_cooldown()` 只看
`risk_state.verification_type`；`backend/app/api/risk.py:55-76` 不接受自证）。
**但文档是反的**：`backend/app/api/risk.py:7-9` 与 `frontend/src/pages/Risk.tsx:9-10` 的注释仍写「body 要带 `verified: true`」，
与实现相反，会误导后续开发者（§7-K）。

### 4.3 缺陷：`captcha` 端点旁路「不自证」门槛（中等）
```
--- has_x5sec(库里的 cookie): True
--- POST /api/accounts/9001/captcha: {'success': True, 'data': {'id': '9001', 'started': True,
      'message': '已打开人工验证窗口，请在弹出的浏览器里完成滑块；完成后凭证会自动保存'}}
--- captcha 后 risk_state: {'account_id': '9001', 'consecutive_hits': 1, 'blocked_until': 1791232570.395408,
                            'verification_type': 'slider'}
--- captcha 后 registry.is_blocked: False
--- captcha 后 POST /api/risk/9001/clear: (200, {'success': True, 'data': {'account_id': '9001',
      'cleared': True, ...}})
```
被拦下的前提是「冷却中」，而冷却前的账号一旦**曾经成功过一次人工验证**，
`backend/app/api/accounts.py:284` 就把带 `x5sec` 的 Cookie 写回库；
此后 `backend/app/xianyu/captcha.py:189-192` 在 `manual_verify` 开头直接
`return {'ok': True, 'cookie': cookie_str, 'message': 'Cookie 已含 x5sec，无需验证'}`（**不打开浏览器**），
`accounts.py:290` 随即 `risk_service.mark_verified(account_id, 'slider')`，
把 `verification_type` 写成 `slider` —— 于是 `clear` 返回 200。
**净效果：不需要任何人工操作就清掉了风控冷却**，与 `risk.py` 注释里「等于没门槛」的设计意图相反。
（另附一处：`backend/app/xianyu/captcha.py:121-128 verification_url()` 的 docstring 说优先用 `x5secdata` 里的惩罚页地址，
实现却是 `return fallback or LOGIN_URL`，生产调用 `login.py:420-425` 不传 `fallback_url` →
打开的永远是 `captcha.py:34 LOGIN_URL='https://www.goofish.com/'`，不是风控惩罚页。真机未验证是否仍能出现滑块。）

### 4.4 缺陷：账号级「自动回复/启用」开关在整个链路里没被读取（严重）
grep 证据（`backend/app` 全量 `enabled|auto_reply`，121 处命中）：
```
backend/app/main.py:105   "WHERE a.enabled = 1 AND COALESCE(c.cookie,'')<>'' ORDER BY a.id"   ← 唯一读 enabled 的生产点
backend/app/services/chats.py:538   if conversation['mode'] == 'auto':        ← 触发 AI 只看会话 mode
backend/app/services/chats.py:672   if conversation.get('mode') != 'auto':    ← 发送闸门只看会话 mode
```
`accounts.auto_reply` 只在 `store.upsert_account` / `update_account` / `list_accounts` 里读写，
**没有任何业务分支读它**；`accounts.enabled` 只在启动挂 IM 时读。
后果：在 UI 上关掉账号的「自动回复」，AI 照旧自动回；停用账号也不会停止已在收信的会话。
附带：`backend/app/core/config.py:65 'ai.enabled': 'true'`、`:67 'chat.auto_reply_scope': 'all'` **没有任何读取点**（死配置，
`get_setting` 只被 `services/cs.py` 与 `risk_service.py` 使用）。

---

## 5. 缺陷：登录/启用账号后收不到私信（IM 入站只在启动时挂载）

- `im_service.start()` 的唯一生产调用点是 `backend/app/main.py:117`（lifespan 里 `_start_im_services()` 的后台线程，`:91-127`），
  且筛选条件是「启动那一刻 `a.enabled = 1` 且有 cookie」。
- `backend/app/services/im_service.py:150-161 stop()` 与 `:59-62 forget()` **没有任何生产调用点**（只有 `backend/tests/test_im_loop.py:170/175` 用 `forget`）。
- 没有任何 API 或服务层订阅 `accounts.changed` 去挂载/摘除 IM（grep `im_service|active_accounts` 的 15 处命中里，
  `backend/app` 侧只有 `main.py:117` 一次调用、`common.py:162-179 _bind_im_service` 只挂钩子、其余是注释）。
- `client.ensure_im`（`backend/app/xianyu/client.py:322-324`）的生产调用者只有 `im_service.ensure_session`（`:119-132`）。
  懒挂载的副作用路径（`send_message`→`_ensure_im`，`client.py:347`）只被 `backend/app/services/chats.py:305/702` 触发，
  而这两处本身需要先有会话/入站消息；`fetch_messages/fetch_conversations`（`client.py:350-358`）**无生产调用者**。

探针证据（临时目录，`active_accounts()` 是内存会话表的直接读数）：
```
--- im_service.active_accounts (启动后): []
--- PATCH enabled=true: {'success': True, 'data': {... 'enabled': True, 'runtime_state': 'running'}}
--- im_service.active_accounts (启用后): []
--- POST relogin: (200, True)
--- im_service.active_accounts (relogin 后): []
--- DELETE 账号: {'success': True, 'data': {'id': '9001', 'deleted': True}}
--- im_service.active_accounts (删除后): []
```
后果：扫码登录成功 / relogin 成功 / 手动启用账号 / 关闭账号 / 删除账号，**都不会**改变 IM 挂载状态；
用户必须**重启后端进程**才会开始收私信。（「删除/停用后进程内已有会话仍会继续收信」是**代码推断**：
`handle_incoming` 不查账号级开关（§4.4）+ `im_service.stop()` 无调用点；因没有真机 IM 连接，**未验证**。）
「真机 IM 连接是否能成功建起来」**未验证**（需要真实账号 + 长连接，超出允许范围）。

---

## 6. 真机边界：验证过 / 未验证

### 6.1 已验证（只读，允许范围）
临时后端（端口 18333，临时 `XIANYU_APP_DIR`）：
```
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine="cmd /c `"set XIANYU_APP_DIR=$env:TEMP\xywb_live&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe -m backend.app.main --port 18333`""; CurrentDirectory="C:\Users\Administrator\Projects\xianyu-ai-workbench"}
Get-Content "$env:TEMP\xywb_live\data\local_secret.txt"
POST http://127.0.0.1:18333/api/auth/desktop   （头 X-Desktop-Secret: <secret>）
POST http://127.0.0.1:18333/api/accounts/qr/start
GET  http://127.0.0.1:18333/api/accounts/qr/<ticket>   ×3
```
关键输出：
```
success=True ticket=5a080ecd51a64f1fb18358ed28665b49 b64len=1592 expires_in=299   ← 真机 passport 取码成功（缺陷①已证）
{'state': 'pending'} ×3
日志 raw: INT -> NEW raw={'content': {'data': {'qrCodeStatus': 'NEW', 'resultCode': 100}, 'status': 0,
          'success': True}, 'hasError': False}                                    ← 真机 query.do 未报参数错误
```
运行中的桌面窗口（端口 18200，真实 `%APPDATA%\xianyu-ai-workbench`，**只读**）：
```
GET /api/accounts → [{'id':'2217970177348','nickname':'','avatar_url':'','enabled':true,'auto_reply':true,
                      'login_state':'active','xianyu_user_id':'2217970177348','relogin_reason':'',
                      'has_cookie':true,'has_x5sec':false,'blocked':false,'remaining_seconds':0,
                      'consecutive_hits':0,'runtime_state':'running'}]
GET /api/risk/status → {'states':[],'blocked':[],'cooldown_steps':[60,180,300,600,1200],
                        'auto_slider':false,'refresh_interval_seconds':1200}
```
观察：真实账号 `nickname` 为空、`avatar_url` 为空、`has_x5sec=false`
——`_fill_profile`（`login.py:375-394`）失败只 `logger.warning`，账号列表因此没有昵称/头像（§7-J）。

### 6.2 明确未验证
- **真机「扫码确认 + 拿 Cookie」这一步未验证**：不允许动真实账号，所以只做到取码 + `pending` 轮询；
  `scanned → confirmed → 落库` 只有 mock transport 的 HTTP 级证据（§1.2）。
- 真机 IM 长连接、真机 `query.do` 在扫码后的返回、真实 LLM 调用：均未验证。
- 「`_m_h5_tk` 过期后是否必须重新登录」：未验证（§2.2 只确证了不落库）。

### 6.3 测试基线（含一条被静默跳过的关键用例）
```
cd C:\Users\Administrator\Projects\xianyu-ai-workbench
cmd /c "set PYTHONIOENCODING=utf-8&& set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\run_all.py"
→ TOTAL 20 PASS 20 FAIL 0 CASES 381 SKIPPED 1
cmd /c "set PYTHONIOENCODING=utf-8&& set PYTHONPATH=.&& C:\Python310\python.exe backend\tests\test_xianyu_login.py -v"
→ Ran 23 tests，OK (skipped=1)
```
（`pytest` 未安装：`C:\Python310\python.exe -m pytest --version` → `No module named pytest`，只能用 `run_all.py`。）

---

## 7. 缺陷清单（按严重度，含最小修法）

**A（严重）唯一端到端扫码测试被静默跳过 —— 登录链路的自动化守护是空的**
- 证据：`skipped '未生成随机密码，跳过需要登录的用例'`；`backend/tests/test_xianyu_login.py:513-542 test_full_qr_flow_creates_account` 是唯一 HTTP 级全链路用例。
- 根因：`TestClient(main.app)` 进 lifespan 时 `backend/app/main.py:70` 已 `security.ensure_admin()` 写完凭据文件；
  测试的 `_login_token()`（`test_xianyu_login.py:499-511`）再调 `ensure_admin()`，
  `backend/app/core/security.py:63-64` 在「文件密码与库 hash 一致」时 `return username, ''`，空密码 `authenticate` 失败 → `skipTest`。
  `backend/app/core/security.py:93-98` 的 docstring 已点名这是 PACKAGING.md 6.5 的病根。
- 最小修法：`_login_token()` 改为读 `config.CRED_PATH` 里的密码，或直接 `db.query_one('SELECT id FROM users WHERE is_admin=1')` 后 `security.issue_token(int(row['id']))`（探针即用此法）。

**B（严重）Cookie 过期不会置 `need_relogin`**（§2.3）
- 最小修法：在 `backend/app/services/common.py:255` 的 `except` 分支识别 `SessionExpiredError`（或 `backend/app/xianyu/risk.py:106 is_session_expired(str(exc))`），
  调 `store.mark_need_relogin(account_id, 'Cookie 已过期')` + `events.publish('accounts.changed', ...)`，信封 code 用 `verification_required`。

**C（严重）账号启用/停用/删除/登录成功都不改变 IM 挂载**（§5）
- 最小修法：给 `events` 加一个 `accounts.changed` 订阅者（或在 `backend/app/api/accounts.py` 的 `_poll_ticket`/`patch_account`/`toggle_account`/`delete_account`/`relogin` 分支里）调
  `im_service.start(account_id)` / `im_service.stop(account_id)`（stop 已有实现，只差调用）。

**D（严重）`auto_reply` / `enabled` 不参与回复决策**（§4.4）
- 最小修法：`backend/app/services/chats.py:538` 与 `:672` 之前加账号级判据
  （`store.get_account(account_id)` 的 `enabled`/`auto_reply`），未启用或 `auto_reply=0` 时不生成/不发送，并落明确 code。

**E（严重）mtop 续期 Cookie 不落库**（§2.2）
- 最小修法：在 `backend/app/services/common.py:211` 构造 client 后注册「cookie 变更回调」，
  或在 `common.call_method` 返回后把 `client.cookie_str` 与库里的 cookie 比对，变了就 `store.set_cookie(account_id, new_cookie, login_state=<原状态>)`。

**F（中）`captcha` 自证旁路**（§4.3）
- 最小修法：`backend/app/xianyu/captcha.py:189-192` 的短路不要返回 `ok=True`；或 `backend/app/api/accounts.py:283-290` 只在「本次真的开了浏览器并拿到新的 x5sec」时才 `mark_verified`。

**G（中）`verification_url` 不按 docstring 走**（§4.3 末）
- 最小修法：`backend/app/xianyu/captcha.py:121-128` 真去解析 `x5secdata`（`cookie_utils` 已有 `challenge_markers`），
  或删掉 docstring 里「优先用 x5secdata」的承诺，明确「人工验证打开的是首页」。

**H（中）`cleanup_expired()` 零调用点**（§3.3）
- 最小修法：`backend/app/xianyu/login.py:252 start()` 开头调 `self.cleanup_expired()`（一行，无需定时器）。

**I（低）`relogin` 不发 `accounts.changed`、`DELETE` 不写 `log_operation`**（§3.1）
- 最小修法：各补一行 `_publish` / `store.log_operation`。

**J（低）`avatar_url`/`nickname` 拿不到**（§6.1）
- 根因（两层）：①`backend/app/xianyu/login.py:396-403 _success_payload` 只返回 `{status, cookie, nickname, user_id}`，
  `backend/app/api/accounts.py:158` 读的 `data.get('avatar_url')` 永远是空 → 死参数；
  ②即使 `_fill_profile`（`login.py:375-394`）成功，它也**只取 `profile.get('nickname')`**，从不读 `avatar_url`
  ——而 `client.parse_user_profile`（`backend/app/xianyu/client.py:441-475`）明明返回了 `avatar_url`。
  真机上 `_fill_profile` 还整体失败（:393-394 只 `logger.warning`），所以连 nickname 都拿不到。
- 最小修法：`_fill_profile` 里补 `session.avatar_url = profile.get('avatar_url')`，`_success_payload` 带上它，
  并把 `_fill_profile` 的失败写进登录结果（或至少提到 warning 可见处）。

**K（低）文档漂移**：`backend/app/api/risk.py:7-9`、`frontend/src/pages/Risk.tsx:9-10` 仍写「clear 必须带 verified: true」，与实现相反（§4.2）。

**L（低）`_STATE_MAP` 未知状态 → `failed`**（`backend/app/api/accounts.py:82`）：平台引入新 `qrCodeStatus` 时前端会立即判「登录失败」并停止轮询。当前 mock/真机未见触发，属脆弱点。最小修法：未知状态映射成 `pending` + message。

**M（低）`_get_mh5tk` 不带 headers/params**（`backend/app/xianyu/login.py:174-186`）：同类「指纹没发全」残留，真机当前可用。

**N（低）新账号默认 `enabled=0`**（§3.2）：登录成功即「已停止」，用户不手动启用就不收消息；建议登录成功后（或在 UI 上）明确提示。

**O（低）CONFIRMED 无 unb 时契约返回 `pending` + 原始 message**（`backend/app/api/accounts.py:82-87` + `backend/app/xianyu/login.py:325-327`）：
前端拿 `pending` 会继续显示「请用闲鱼 App 扫描二维码」，而用户其实已经扫完并确认过了（探针输出见 §1.2）。
建议：给这种情况一个独立状态（如 `scanned` + 明确 message「已确认但未拿到账号 ID，请重试或重新扫码」）。

---

## 8. 给用户的一句实话

- 用户报的两个登录缺陷（取码、扫完被判过期），**代码层面的修复方向是对的，且取码已在真机取证通过**；
  「扫码确认→落库」只有 mock 级证据（唯一真机全链路测试还被静默跳过），所以不能声称「登录 100% 通」。
- 「一堆问题」里真正会在使用中立刻踩到的是：**登录/启用后不重启就收不到消息**（§5）、**账号级自动回复开关无效**（§4.4）、
  **Cookie 过期不提示重新登录**（§2.3）、**mtop 续期不落库**（§2.2）、**点一次「验证」就能解冷却**（§4.3）。
