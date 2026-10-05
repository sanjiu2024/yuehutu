# 会话与消息链路 只读审计报告

- 被审仓库：`C:\Users\Administrator\Projects\xianyu-ai-workbench`
- 被审版本：`git rev-parse HEAD` → `2f3425ad1078d5c5b0527510e380814dfc150784`（工作区除本报告外无改动，`git status --short` 仅 `?? docs/audit/`）
- 审计人：teammate `audit-chats`；审计方式：**只读**（未修改任何源码、未 git commit、未打真实闲鱼、未调真实 LLM）
- 运行时隔离：`XIANYU_APP_DIR=C:\Users\Administrator\AppData\Local\Temp\xywb_audit_chats`（另加 `_gates` / `_login` / `_delacc2` 三个临时库）；**未触碰** `%APPDATA%\xianyu-ai-workbench`
- 被测后端：端口 **18237**（18091 被其他 teammate 占用），启动命令见第 0 节
- 结论一句话：**WebSocket 实时链路本身是通的（实测能收到事件）、前端订阅与刷新逻辑也是对的；但真机最容易出现的「对话区同步不过来对话」不是 WS 坏了，而是「买家消息根本没有入口」——IM 入站只在后端进程启动那一刻挂载，运行期扫码登录的账号永远不会挂载，也不存在任何轮询兜底。**

---

## 0. 环境与可复现命令

启动后端（本报告所有 HTTP/WS 实测都打在它上面）：

```powershell
$env:XIANYU_APP_DIR='C:\Users\Administrator\AppData\Local\Temp\xywb_audit_chats'
cmd /c "cd /d C:\Users\Administrator\Projects\xianyu-ai-workbench && set XIANYU_APP_DIR=C:\Users\Administrator\AppData\Local\Temp\xywb_audit_chats&& set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& set XYWB_PORT=18237&& C:\Python310\python.exe -m backend.app.main --port 18237 > C:\Users\Administrator\AppData\Local\Temp\xywb_audit_chats\backend18237.out 2>&1"
```

启动成功证据（`Get-Content ...\backend18237.out -Tail 25` 节选）：

```
INFO:     Started server process [28304]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:18237 (Press CTRL+C to quit)
INFO:     127.0.0.1:62216 - "WebSocket /api/ws?token=..." [accepted]
```

取桌面 token（所有脚本统一用这段）：

```python
secret=open(os.path.join(APP,'data','local_secret.txt'),encoding='utf-8').read().strip()
req=urllib.request.Request(BASE+'/api/auth/desktop',data=b'{}',
    headers={'Content-Type':'application/json','X-Desktop-Secret':secret})
tok=json.loads(urllib.request.urlopen(req).read())['data']['token']
```

说明：**本报告的终端输出为 GBK/mojibake**（`PYTHONIOENCODING` 与 cmd 控制台编码不一致），下文引用的中文消息文本以源码原文核对给出（均标注 `文件:行`），英文/结构字段为原样输出。命令均以 `[Convert]::ToBase64String` + `python -c "import base64;exec(...)"` 方式执行（here-string 直传 `python -c` 会被 PowerShell 吞掉内层双引号，报 `SyntaxError: '(' was never closed`）。

---

## 1. 逐项判定总览

| # | 审计项 | 判定 | 证据小节 |
|---|---|---|---|
| A | 买家新消息能否进入系统（真机「收到来信」） | **缺陷（严重）**：运行期登录的账号不入站 | §2 |
| B | 对话区不刷新自动出现新对话 | **链路通、触发源缺**：WS+前端 OK，但生产上没人发消息 | §3 |
| C | 对话区不刷新自动出现新消息 | 同上（`chats.message` → `xywb:refresh-messages`） | §3 |
| D | 会话列表 `/api/chats/conversations` | 正常（含 `last_message`、`unread`、`total`） | §4 |
| E | 消息列表 `/api/chats/{id}/messages` | 正常 | §4 |
| F | 人工发送 `/api/chats/{id}/send` | **半残**：签名历史缺陷已修（实测正确），但 `cid` 兜底用 `peer_user_id` 有发错会话风险；协议层未就绪时只本地留存 | §5 |
| G | AI 自动回复投递闸门（6 条） | 5 条实测可达，`mode_not_auto` 生产不可达（死分支） | §6 |
| H | `POST /api/chats/incoming` 契约 | **缺陷（中）**：不存在的账号也能建会话（幽灵会话）；HTTP 路径恒无 `cid` → AI 永远 `missing_cid` | §7 |
| I | 平台消息幂等（`msg_id`） | 正常 | §8 |
| J | 模式切换 / 已读 / 分类 | 正常（模式切换实测有 WS 事件） | §9 |
| K | AI 草稿 `/api/chats/{id}/ai-draft` | 无 Provider 时 `400 ai_error`（可理解为提示，但与「未就绪」语义不一致） | §9 |
| L | 删除账号后的会话残留 | **缺陷（中低）**：会话/消息不清理，「全部账号」仍显示 | §10 |
| M | 无真实登录账号时 UI 表现（应看到什么/实际看到什么） | 全新库诚实（空态）；注入过消息则出现「幽灵会话」；登录后仍空（§2） | §6b |

---

## 2. 【严重】IM 入站只在进程启动时挂载 → 运行期扫码登录收不到任何买家消息

### 结论
买家私信的**唯一**入口是协议层 IM 会话的入站回调。生产代码里挂载这个回调的地方**只有一处**：`backend/app/main.py:84` 的 `_start_im_services()`，它由 lifespan 在**进程启动时**执行一次（`backend/app/main.py:91-127`，逐个查「已启用 **且已有 Cookie**」的账号调 `im_service.start`）。

运行期走完扫码登录（Cookie 刚写库、账号刚建）时**没有任何代码**去调 `im_service.start()`，`accounts.changed` 事件也只发给 WebSocket，没有进程内订阅者去补挂。也就是说：

- 用户「启动后端 → 扫码登录成功」→ **本次进程内永远不会收到买家消息**，对话区只会一直显示空态「账号在线并收到买家消息后，会话会出现在这里。」；
- 必须**重启后端**，启动期的 `_start_im_services()` 才会把这个已有 Cookie 的账号挂上；
- 重启后若再重新登录/换号，又回到上面的状态。

这正好解释用户说的「对话区同步不过来对话」——不是同步慢，是**没有消息可同步**。

### 证据 1：全仓只有一处生产调用点
```powershell
# grep 工具：pattern = im_service|_start_im_services|ensure_im, path = backend/
```
关键命中（原样）：

```
backend/app/main.py:84:    _start_im_services()
backend/app/main.py:91:def _start_im_services() -> None:
backend/app/main.py:117:                result = im_service.start(account_id)
backend/app/services/common.py:162:def _bind_im_service(account_id: str, client: Any) -> Any:
backend/app/api/accounts.py  —— 0 命中（无 im_service / ensure_im）
```
`backend/app/services/chats.py:808` 与 `backend/tests/test_im_loop.py` 是注释/测试，不是生产调用点。

### 证据 2：扫码登录成功路径实测不挂载（打桩协议层，走真实 `_poll_ticket`）
用真实的 `backend.app.api.accounts._poll_ticket()`，只把 `qr_login.poll_qr_login` 换成返回 `status=success` 的假实现，并给 `im_service.start` 记数：

```python
from backend.app.api import accounts as api_accounts
from backend.app.services import im_service
calls={'im_start':0,'topics':[]}
im_service.start=lambda aid:(calls.__setitem__('im_start',calls['im_start']+1) or {'success':True})
events.publish=lambda topic,payload=None:calls['topics'].append(topic)
class FakeQR:
    async def poll_qr_login(self,ticket):
        return {'status':'success','cookie':'unb=999; cookie2=abc','user_id':'runtime_acc','nickname':'runtime'}
api_accounts.qr_login=FakeQR()
out=asyncio.run(api_accounts._poll_ticket('ticket-1'))
print('topics_published',calls['topics'])
print('im_service_start_calls_after_login',calls['im_start'])
```

关键输出（原样）：

```
poll_ticket_result {"state": "confirmed", "account_id": "runtime_acc"}
topics_published ['accounts.changed']
im_service_start_calls_after_login 0
```

→ 登录成功只做了「落库 + 发 `accounts.changed`」，**IM 入站挂载调用次数 = 0**。

### 证据 3：没有任何「拿到账号后补挂」的订阅者
```
backend/app/api/ws.py:22:            queue = await events.subscribe()
backend/app/core/events.py:42:async def subscribe() -> 'asyncio.Queue[Dict[str, Any]]'
```
`events.subscribe` 的生产调用方只有 WS 端点；`accounts.changed` 的 4 个发布点（`backend/app/api/accounts.py:164/213/226/310`）没有任何进程内消费者。协议层 `fetch_conversations`（`backend/app/xianyu/im.py:518`、`backend/app/xianyu/client.py:355`）**没有任何服务层轮询兜底**（全仓 grep `sync_conversations|_sync_loop|while True` 只命中 WS 心跳循环与 IM 读循环）。

### 根因
- `backend/app/api/accounts.py:149-164`：登录成功分支只 `store.upsert_account(...)` + `_publish('accounts.changed', ...)`，**缺 `im_service.start(user_id)`**。
- `backend/app/main.py:91-127`：`_start_im_services()` 是启动期一次性快照，没有「登录后/启用后补挂」的入口。
- `backend/app/services/common.py:162-179` 的 `_bind_im_service` 只在**取客户端时**挂钩子，而钩子要等协议层 `_ensure_im()` 建会话才回调（`backend/app/xianyu/client.py:310`）；服务层自己不去主动建会话，所以「挂消息」这条路上无人触发。

### 建议最小修法（任选其一，推荐 1+3）
1. 在 `backend/app/api/accounts.py:164` 之后补一次挂载（放到后台线程，别阻塞登录响应）：
   ```python
   threading.Thread(target=im_service.start, args=(user_id,), daemon=True).start()
   ```
   同一处理也加到 `PATCH /{account_id}`（重新启用，`:213` 附近）与 `POST /{account_id}/relogin`（`:246` 附近）。
2. 或让 `_start_im_services()` 可被重复调用，并在 `accounts.changed` 上挂一个进程内订阅者（`events.subscribe`）做补挂 + 退避重试。
3. 无论选哪个，都应让 `im_service.start()` 的失败可观测：现在是 `logger.warning('IM 入站未挂载')`（`backend/app/main.py:117-127`），前端「账号」页看不到，用户无从判断。

---

## 3. 对话区能否不刷新自动出现新对话/新消息

### 结论（分两段，别混为一谈）
- **链路层：通。** 实测 WS 能收到 `chats.message` / `chats.mode_changed`，字段与前端消费的一致；前端 `Chats.tsx:86-100` 的订阅 → `conversations.reload()` → 当前会话再派发 `xywb:refresh-messages` → `ChatWindow`（`frontend/src/pages/Chats.tsx:237`）重拉消息，逻辑正确。
- **产品层：真机上基本不成立。** 因为 §2：买家消息进不来，所以「自动出现新对话」这件事在生产路径上没有触发源。**用户感觉到的「同步不过来」= §2 缺陷**，不是 WS 缺陷。

### 证据 1：端到端 WS 实测（真实后端 18237 + 真实 token）
```python
async with websockets.connect('ws://127.0.0.1:18237/api/ws?token='+tok) as ws:
    r=api('/api/chats/incoming',{'account_id':'audit_acc','buyer_id':'peer_ws',
                                 'content':'hello-from-buyer','msg_id':'ws-1'})
    ev=json.loads(await asyncio.wait_for(ws.recv(),timeout=6))
    print('WS_EVENT',json.dumps({'topic':ev.get('topic'),
        'payload_keys':sorted((ev.get('payload') or {}).keys()),'ts':ev.get('ts') is not None}))
```
关键输出（原样）：

```
WS_CONNECTED
HTTP_INCOMING {"ok": true, "conv": 2, "mode": "auto", "ai_result": null}
WS_EVENT {"topic": "chats.message", "payload_keys": ["content", "conversation_id", "direction", "message_id", "sender"], "ts": true}
HTTP_LIST_count 2
```

模式切换同样有事件：
```python
print('C3_mode',brief(api(f'/api/chats/{cid}/mode',{'mode':'manual'})))
ev=json.loads(await asyncio.wait_for(ws.recv(),timeout=8)); print('C3_ws_event',ev.get('topic'))
```
```
C3_mode {"ok": true, "code": null, "http_status": null}
C3_ws_event chats.mode_changed
```

### 证据 2：前端消费逻辑（代码）
- 订阅 topic 列表 `frontend/src/pages/Chats.tsx:88`：`['chats.message','chats.mode_changed','chats.ai_reply','items.updated','risk.changed']` —— 与后端实际发布的 topic **完全对上**（`backend/app/services/chats.py:263/343/369/384/549` 等）。
- `frontend/src/pages/Chats.tsx:90` `conversations.reload()`；`:91-96` 仅当 `payload.conversation_id === activeId` 时派发 `xywb:refresh-messages`；`frontend/src/hooks/useAsync.ts:65` `reload = () => setTick(n=>n+1)`（真的会重新发请求）。
- 事件到达前不刷新也能看到新会话：`Chats.tsx:75` `list = conversations.data?.conversations ?? []`，reload 后 React 重渲染列表。
- 服务端：`backend/app/api/ws.py:15-40`（token 校验失败 `close(code=4401)`；`events.subscribe()` + pump）。
- `backend/app/core/events.py` 的 `publish` 在 `_loop is None` 时**直接 return**（`events.bind_loop` 在 lifespan 里调，正常启动顺序下已绑定）。

### 根因
「新对话自动出现」缺的不是 WS，而是 **§2 的入站挂载**；另外注意 `frontend/src/pages/Chats.tsx:100` 的 effect 依赖是 `[activeId]`，每次切换会话都会重订阅一次（有 cleanup，功能正常，但会造成 WS 订阅抖动，非缺陷）。

### 建议最小修法
先修 §2；另外把「入站未挂载」暴露到 UI（例如 `/api/health` 或账号运行态里带 `im_mounted: false`），否则用户只会继续以为是「同步坏了」。

---

## 4. 会话列表 / 消息列表读取

### 结论
正常。列表返回 `{conversations,total}`，每条带 `last_message`；消息按时间返回。

### 证据
```python
raw=api('/api/chats/conversations?limit=5')['data']
convs=raw['conversations']
mraw=api(f'/api/chats/{cid}/messages')['data']
```
关键输出（原样）：

```
C0_shape ["conversations", "total"]
C1_conversations [{"id": 2, "peer": "peer_ws", "mode": "auto", "unread": 1, "has_last": true},
                  {"id": 1, "peer": "audit_buyer", "mode": "auto", "unread": 3, "has_last": true}]
C2_messages [{"dir": "in", "sender": "buyer", "content": "hello-from-b"}]
```

### 根因
无缺陷。`backend/app/services/chats.py:146 list_conversations` + `backend/app/api/chats.py:41-60`（`''` 与 `'/'` 两个路径都注册，兼容早先契约）。

---

## 5. 人工发送 `/api/chats/{id}/send`：签名已修，但 `cid` 兜底有发错会话风险

### 结论
- 历史缺陷（`send_message(account_id, peer_user_id, text)` 三参数错位）**已修好**：实测调用参数与真签名 `send_message(cid, text, toid=None)`（`backend/app/xianyu/client.py:345`）一致。
- 但 `_platform_cid()` 在取不到平台 `cid` 时**退回 `peer_user_id`**（`backend/app/services/chats.py:291`），而 `peer_user_id` 是买家 user id，**不等于** IM 会话 id。真机上若历史消息里没存过 `cid`（例如会话是通过 HTTP `/api/chats/incoming` 建的、或 IM 挂载前建的），人工回复会被发到**错误的会话 id**——轻则协议层报错、重则发错人。这是本条最值得关注的点。
- 协议层未就绪时：消息仍落库、`sent:false` + warning，符合注释设计。

### 证据 1：参数对齐（打桩协议层，走真实 `send_manual`）
```python
class FakeClient:
    def send_message(self,cid,text,toid=None):
        rec['args']=(cid,text,toid); return {'success':True,'cid':cid}
common.get_client=lambda aid:(FakeClient(),None)
r=chats.send_manual(conv,'manual-reply-content')
print('A1_send_manual',json.dumps({'sent':r.get('sent'),'warning':r.get('warning')}))
print('A1_recorded_args',rec['args'])
```
```
A1_send_manual {"sent": true, "warning": null}
A1_recorded_args ('audit_peer', 'manual-reply-content', None)
```
（`conversation.peer_user_id = 'audit_peer'`、无历史 `cid` → 兜底退回 `peer_user_id`，正好同时印证上面的风险。）

### 证据 2：真机 HTTP 路径（该临时账号无 Cookie）
```python
print('C4_send',json.dumps({k:v for k,v in (api(f'/api/chats/{cid}/send',{'content':'hello-seller'}).get('data') or {}).items() if k in ('sent','warning','mode')}))
```
```
C4_send {"sent": false, "mode": "manual", "warning": "<GBK mojibake>"}
```
warning 原文即 `backend/app/services/chats.py:332`：`'协议层尚未就绪，消息仅本地留存'`。

### 证据 3：AI 投递路径的 `cid` 用法正确
`deliver_ai_reply(..., cid=incoming_meta.get('cid'))`（`backend/app/services/chats.py:553-556`）用的是 IM 帧里的真 `cid`，实测打桩输出 `A7_recorded_args ('c1', 'x', None)`、`B2_recorded_args ('REAL_CID_1', 'AI-auto-reply', None)`。

### 建议最小修法
`backend/app/services/chats.py:291` 不要把 `peer_user_id` 当 `cid`：取不到 `cid` 时返回 `''`，让 `_push_message`（`:302-304`）走「拿不到平台会话 id，消息仅本地留存」的显式分支，同时在 UI 把这句 warning 显示出来。若确实需要兜底，应改为「用 `peer_user_id` 反查平台会话」而不是直接当会话 id 用。

---

## 6. AI 自动回复投递闸门逐条可达性（`deliver_ai_reply`）

`deliver_ai_reply` 定义在 `backend/app/services/chats.py:649-715`；**唯一生产调用方**是 `backend/app/services/chats.py:554`（在 `handle_incoming` 内，且位于 `if conversation['mode'] == 'auto':`（`:538`）之内）。

| 闸门 | 代码位置 | 实测 code | 生产可达性 |
|---|---|---|---|
| `mode_not_auto` | `chats.py:672-676` | `mode_not_auto` | **不可达**（唯一调用方已在 auto 分支内；仅测试 `backend/tests/test_im_loop.py:293/333/349` 可达） |
| `empty_text` | `chats.py:677-680` | 未单独构造（代码路径直读） | 可达 |
| `missing_cid` | `chats.py:681-685` | `missing_cid` | **可达且高危**（HTTP `/api/chats/incoming` 路径恒无 cid → AI 回复永远发不出） |
| `risk_cooldown` | `chats.py:686-690` | `risk_cooldown` | 可达 |
| `xianyu_not_ready`（无客户端/无方法） | `chats.py:692-699`、`:707` | `xianyu_not_ready` | 可达 |
| `protocol_error` / `risk_blocked` | `chats.py:709-712` | `protocol_error` / `risk_blocked` | 可达 |

### 证据（打桩协议层 + 直接调 `deliver_ai_reply`；`risk` 用真实 `backend.app.xianyu.risk.registry`）
```python
chats.set_mode(conv,'manual')                                     # 会话切非 auto
print('A2b_mode_manual',json.dumps(chats.deliver_ai_reply(conv,m['id'],'x',cid='c9')))
chats.set_mode(conv,'auto')
print('A3_missing_cid',json.dumps(chats.deliver_ai_reply(conv,m['id'],'x',cid='')))
risk_mod.registry.get('audit_acc').block_for(60,1,'probe')
print('A4_risk_cooldown',json.dumps(chats.deliver_ai_reply(conv,m['id'],'x',cid='c1')))
risk_mod.registry.get('audit_acc').reset()
cur['client']=FakeClient('raise')          # send_message 抛 RuntimeError('boom')
print('A5_protocol_error',...)
cur['client']=FakeClient('riskraise')      # 抛 RuntimeError('RGV587_ERROR warning')
print('A6_risk_blocked',...)
common.get_client=lambda aid:(None,{'success':False,'code':'xianyu_not_ready','message':'no cookie'})
print('A9_no_client',...)
```
关键输出（原样，中文已被服务端写库，终端为 mojibake）：

```
A2b_mode_manual   {"sent": false, "code": "mode_not_auto", ...}
A3_missing_cid    {"sent": false, "code": "missing_cid", ...}
A4_risk_cooldown  {"sent": false, "code": "risk_cooldown", ...}
A5_protocol_error {"sent": false, "code": "protocol_error", ...}
A6_risk_blocked   {"sent": false, "code": "risk_blocked", ...}
A7_ok             {"sent": true,  "code": "ok", ...}      # A7_recorded_args ('c1','x',None)
A8_call_missing   {"sent": false, "code": "xianyu_not_ready", ...}
A9_no_client      {"sent": false, "code": "xianyu_not_ready", ...}
A7_row            {'sent_ok': 1, 'meta': '{"ai_generated": true}'}
```
真实路径（`handle_incoming` + 打桩 `chats._ai_reply` 产出回复文本，不调真实 LLM）：
```
B1_http_incoming_ai_send {"sent": false, "code": "missing_cid", ...}      # 无 cid 的 HTTP 来信
B2_im_style_ai_send      {"sent": true,  "code": "ok", ...}                # meta={'cid':'REAL_CID_1'}
B2_recorded_args ('REAL_CID_1', 'AI-auto-reply', None)
```

### 建议最小修法
1. `mode_not_auto` 是防御性死分支：可保留，但**别把它当已覆盖的路径**；若要真覆盖，应允许「非 auto 会话的 AI 建议投递」另开一个显式入口。
2. 针对 `missing_cid`：IM 帧解析出的 `cid` 要**持久化到 `messages.meta`**（`backend/app/services/chats.py:795` 已经写进 meta 了，但没有回填到会话/买家消息的 meta，导致 `_platform_cid` 下次仍可能取不到）；更稳的是在 `ensure_conversation` 记一列/一条 meta 存平台 cid。

---

## 6b. 没有真实登录账号时，对话区「应该看到什么 / 实际看到什么」

### 结论
- **全新库（从未登录过）**：`/api/accounts` 返回 `[]` → 账号下拉只有「全部账号」；`/api/chats/conversations` 返回 0 条 → 对话区显示空态文案（`frontend/src/pages/Chats.tsx`：`账号在线并收到买家消息后，会话会出现在这里。`）。**此时 UI 是诚实的**。
- **只要有人/有代码往 `/api/chats/incoming` 注入过消息**（例如外部调用、其他 teammate 的 smoke、`scripts/smoke_write.py` 的 seed）：这些**账号并不存在的会话**照样出现在列表里（§7），用户会看到「有会话、有消息、但发送永远失败（无 Cookie）」——这是最容易让用户判定成「功能不正常」的状态。
- **扫码登录成功后（Cookie 已落库）**：账号出现在下拉里，但如 §2 所述本次进程内**不会**收到买家消息，对话区依然是空的 → 这就是用户看到的「同步不过来对话」。

### 证据（全新临时库，走真实服务层函数）
```python
os.environ['XIANYU_APP_DIR']=r'C:\Users\Administrator\AppData\Local\Temp\xywb_audit_chats_fresh'
config.ensure_dirs(); db.init_db()
print('fresh_accounts', json.dumps(store.list_accounts()))
print('fresh_conversations', json.dumps(chats.list_conversations()))
```
关键输出（原样）：

```
fresh_accounts []
fresh_conversations {"conversations": 0, "total": 0}
```

登录成功后（§2 同一实验）账号侧的样子：
```
poll_ticket_result {"state": "confirmed", "account_id": "runtime_acc"}
topics_published ['accounts.changed']
im_service_start_calls_after_login 0
```

### 根因
同 §2（入站不挂载）与 §7（注入路径不校验账号）。UI 没有「IM 未挂载」这个状态的任何表达：`frontend/src/pages/Chats.tsx` 只有空态文案，没有「账号未收信/未挂载」的提示；`/api/accounts` 的 `_decorate`（`backend/app/api/accounts.py:103-110`）也没有 IM 挂载态字段。

### 建议最小修法
1. 修 §2 后，给账号契约加 `im_mounted: bool`（`backend/app/api/accounts.py:103 _decorate` + `im_service.active_accounts()`，后者已存在于 `backend/app/services/im_service.py:54`）。
2. 对话区空态分两种文案：「无账号/未登录」vs「账号在线的收信通道未挂载，请重启或点『重连』」。

---

## 7. `POST /api/chats/incoming`：不校验账号存在 + HTTP 路径恒无 cid

### 结论
- **缺陷（中）**：`account_id` 不存在也能成功建会话并落消息（返回 `200`），产生「幽灵会话」；这些会话会出现在「全部账号」列表里，点进去却没有任何账号可发消息。
- **设计事实**：HTTP 注入的来信没有平台 `cid`，所以 `mode=auto` 时 AI 回复**必然** `missing_cid`（见 §6）。若把这当「买家来信入口」用于真机，AI 永远不会自动回复。

### 证据
```python
st,b=api('/api/chats/incoming',{'account_id':'nope_acc','buyer_id':'b1','content':'x'})
print(st, json.dumps(b,ensure_ascii=False)[:260])
st,b=api('/api/chats/conversations')
print(json.dumps([c['account_id'] for c in b['data']['conversations']],ensure_ascii=False))
```
关键输出（原样）：

```
/api/chats/incoming {"account_id": "nope_acc", "buyer_id": "b1", "content": "x"} -> 200
  {"success": true, "data": {"message": {"id": 9, "conversation_id": 4, "direction": "in", "sender": "buyer", ..., "meta": "{\"source\": \"api\"}"}, "ai_reply": null, ...}}
/api/chats/conversations -> 200 ["del_acc", "nope_acc", "audit_acc", "audit_acc", "audit_acc"]
```

### 根因
- `backend/app/services/chats.py:508 ensure_conversation(...)` 只按 `(account_id, peer_user_id)` upsert，**不校验 `accounts` 存在**（其文档字符串也明说「不创建 accounts 行」）；`backend/app/api/chats.py:99-128` 的入站分支只校验 `buyer_id` / `direction`。
- `backend/app/services/chats.py:795` `meta={'source':'im','cid':...}` 只在 IM 分支带 cid；HTTP 分支的 meta 是 `{'source':'api'}`。

### 建议最小修法
- `backend/app/api/chats.py` 入站分支加一步：`if conversation_id in (None,'',0) and not store.get_account(account_id): return invalid_fields('账号不存在')`（或 `ensure_conversation` 内校验并返回 `not_ready`）。
- HTTP 入站既然是「外部注入/调试」语义，建议在响应里明确标注 `ai_send.code=missing_cid` 的原因（现已返回 code，可接受）。

---

## 8. 平台消息幂等（`msg_id`）

### 结论
正常。同一条平台消息重复送达不会重复落库/重复触发 AI。

### 证据
```python
d1=api('/api/chats/incoming',{'account_id':'audit_acc','buyer_id':'peer_dup','content':'dup-test','msg_id':'dup-1'})['data']
d2=api('/api/chats/incoming',{'account_id':'audit_acc','buyer_id':'peer_dup','content':'dup-test','msg_id':'dup-1'})['data']
print(json.dumps({'first_dup':d1.get('duplicate'),'second_dup':d2.get('duplicate'),
                  'same_msg':(d1.get('message') or {}).get('id')==(d2.get('message') or {}).get('id')}))
```
```
C8_idempotent {"first_dup": false, "second_dup": true, "same_msg": true}
```

### 根因
无缺陷。`backend/app/services/chats.py:515-523`（`find_incoming_by_external_id`）+ `:526-527` 写 `messages.meta.external_id`。

---

## 9. 模式切换 / 已读 / 分类 / AI 草稿

| 接口 | 实测 | 说明 |
|---|---|---|
| `POST /api/chats/{id}/mode` | `200 ok` + WS `chats.mode_changed` | 正常 |
| `POST /api/chats/{id}/read` | `200 ok` | 正常（未读清零，见 §4 `unread` 计数） |
| `POST /api/chats/classify` | `200 ok` | 正常 |
| `POST /api/chats/{id}/ai-draft` | `400 {"code":"ai_error","message":"还没有可用的 AI 提供者，请先在「AI 设置」里填一个 API Key"}` | 语义上更像「未就绪」，但已给出可操作提示，判定为**可接受** |

原样输出：
```
C3_mode {"ok": true, "code": null, "http_status": null}
C3_ws_event chats.mode_changed
C5_ai_draft {"ok": null, "code": null, "http_status": 400}
C6_read {"ok": true, "code": null, "http_status": null}
C7_classify {"ok": true, "code": null, "http_status": null}
```
（`C5` 的 `ai_error` 来自同一端点第二次带 body 的复测：`/api/chats/2/ai-draft {} -> 400 {"success": false, "message": "<...>", "code": "ai_error"}`。）

---

## 10. 【中低】删除账号不清理会话与消息，「全部账号」仍显示

### 结论
`store.delete_account` 只清 `account_credentials` / `risk_state` / `risk_events` / `accounts`，**不删 `conversations` 与 `messages`**；删号后这些会话仍出现在「全部账号」视图里（前端 `frontend/src/pages/Chats.tsx:116` 的「全部账号」选项）。

### 证据（走真实函数，单库无并发干扰）
```python
store.upsert_account(account_id='acc_del', nickname='x', cookie='unb=1; a=b', login_state=store.STATE_ACTIVE)
conv=chats.ensure_conversation('acc_del','buyer_del','','','auto','cs')['id']
chats.append_message(conv,'buyer','hi',mark_unread=True)
print('BEFORE_convs',json.dumps(convs()))
print('delete_account_ok',store.delete_account('acc_del'))
print('AFTER_accounts',json.dumps([a['id'] for a in store.list_accounts()]))
print('AFTER_convs',json.dumps(convs()))
print('AFTER_messages_count',db.query_one('SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?',(conv,))['n'])
```
关键输出（原样）：

```
BEFORE_convs [{"id": 1, "account_id": "acc_del"}]
delete_account_ok True
AFTER_accounts []
AFTER_convs [{"id": 1, "account_id": "acc_del"}]
AFTER_messages_count 1
AFTER_creds_count 0
```

### 根因
`backend/app/xianyu/store.py:137-151`（无 `DELETE FROM conversations/messages`）；表定义里会话/消息对 `accounts` 没有 `ON DELETE CASCADE`（`accounts`→`account_credentials` 有）。

### 建议最小修法
在 `store.delete_account` 事务内补：
```python
db.execute('DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE account_id = ?)', (account_id,))
db.execute('DELETE FROM conversations WHERE account_id = ?', (account_id,))
```
或给 `conversations.account_id` 加外键 `ON DELETE CASCADE` 并在 UI 提示「将同时删除历史会话」。注意：**是否有意保留历史**属产品决策，但当前是「无提示地残留并且还能被看见」，至少要在 UI 过滤掉账号已不存在的会话。

---

## 11. 未验证项（明确标注，勿当结论）

1. **真机闲鱼帧里的 `cid` 是否为非空**：未打真实平台，`parse_incoming_message` 只在单测/假帧上验证过（`backend/app/xianyu/im.py:140`）。若真机帧缺 `cid`，`B2` 的成功路径在真机上会退化成 `missing_cid`。
2. **真机上 `im_service.start()` 是否真的建连成功**（Cookie 有效性、`_ensure_im()` 是否被平台接受）：需真实账号，未验证。启动日志里「IM 入站已挂载/未挂载」是唯一的观测点（`backend/app/main.py:117-127`）。
3. **浏览器内的端到端不刷新验证**：未跑 Playwright，§3 是「服务端 WS 事件实测 + 前端消费代码」两侧分别验证 + 代码对齐，不是浏览器实测。若要更强的证据，需要注入 `localStorage` token 后用 `scripts/ui_smoke.py` 的方式跑一次。
4. **IM 会话断线重连后的入站是否仍有效**（`backend/app/xianyu/im.py:473` 的读循环重连路径）：未验证。
5. **`vite dev` 下 WS 代理端口固定 8080 与动态后端端口不一致**（`frontend/vite.config.ts:11-16` 代理到 `127.0.0.1:8080`，而后端可 `--port 0` 动态挑端口，见 `backend/app/main.py:130-142`）：开发形态下可能导致 WS 连不上；生产形态（后端托管 `frontend/dist`，`backend/app/main.py:207-209`）同源无此问题——**未实测 dev 形态**。

---

## 12. 建议修复顺序（按用户感知）

1. **§2 IM 入站挂载**（登录/重登/启用后补挂 + 失败可见）——直接决定「对话区有没有对话」。
2. **§5 `_platform_cid` 兜底**（别把 `peer_user_id` 当平台会话 id）——决定「人工回复有没有发对人」。
3. **§7 入站账号校验**（消掉幽灵会话）。
4. **§10 删号残留**（至少 UI 过滤）。
5. §6 的 `mode_not_auto` 死分支与 `missing_cid` 的 cid 持久化（防御性 + 真机 AI 自动回复能否发出）。
