# 纠纷协商（联系纠纷小二）契约

官方闲鱼卖家工作台把这块挂在「投诉管理」详情页里，弹一个面板：拉历史留言、发文字、
发图举证、催办、标记已读。我们单开 `/api/disputes`（7 条：3 读 + 4 写）。

> **这一族和 `/api/im` 不是一回事**：`/api/im` 的会话是**买家的 IM 会话**（cid）；
> 这里的 `{conversation_id}` 是**纠纷会话 id**（平台 `conversationId`，由
> `GET …/conversation` 拿到）。两个 id 都叫「会话」，别混。

协议层 `backend/app/xianyu/dispute.py`（纯函数 `(api, data, version)` 三元组，零平台请求），
服务层 `backend/app/services/disputes.py`，路由层 `backend/app/api/disputes.py`。

## 1. 平台接口（全部静态取证自官方 bundle）

| 动作 | mtop api | 版本 | 入参 |
| --- | --- | --- | --- |
| 取/建会话 | `mtop.taobao.idle.shop.dispute.message.get.conversation` | `1.0` | `{orderId?, refundId?, disputeId?}`（至少一个） |
| 历史留言 | `mtop.taobao.idle.shop.dispute.message.query.history` | `1.0` | `{conversationId, beginTime?, pageSize:20}` |
| 未读留言 | `mtop.taobao.idle.shop.dispute.message.query.unread.list` | `1.0` | `{conversationId}` |
| 发文字 | `mtop.taobao.idle.shop.dispute.message.send` | `1.0` | `{conversationId, sceneType:'userActive', content: JSON 串, contentType:'PLAIN_TEXT'}` |
| 发图举证 | `mtop.taobao.idle.shop.dispute.message.send` | `1.0` | 同上，`contentType:'IMAGE'`，`content` 是 `{url,ext,width,height,altText,thumbURL,sendKey}` 的 JSON 串 |
| 催办 | `mtop.taobao.idle.shop.dispute.message.send` | `1.0` | 同上，`content` 是 `{"text":"催促处理维权"}` |
| 标记已读 | `mtop.taobao.idle.shop.dispute.message.mark.read` | `1.0` | `{conversationId, msgId, sequence: 整数}` |

- `content` 用 `json.dumps(..., ensure_ascii=False, separators=(',', ':'))`（贴近官方 `JSON.stringify`）。
- 文字上限 **2000** 字；`conversationId` / `msgId` 上限 128；图片 `ext` 缺省 `JPG`；
  `thumbURL` 缺省取原图；`sendKey` 始终带上。
- **平台回来的 `content` 两种形态都认**（JSON 字符串或对象）—— 由
  `proto.normalize_message` 归一，服务层不自己判。

## 2. HTTP 契约

### 2.1 `GET /api/disputes/{account_id}/conversation?order_id=&refund_id=&dispute_id=`

只读。三个单号至少给一个（都不给 → 400 `invalid_fields`）。

`data` 键：`account_id` / `api` / `submitted` / `conversation_id` / `conversation_id_key` /
`found` / `body_keys` / `warnings` / `source` / `fetched_at`。

**平台没给 `conversationId` → 502**（不是 200 带空串）：拿不到会话 id，后面每一步都做不了。

### 2.2 `GET /api/disputes/{account_id}/{conversation_id}/messages?begin_time=`

只读。`begin_time` 传上一页返回的 `current_time`。

`data` 键：`account_id` / `api` / `conversation_id` / `begin_time` / `messages` / `count` /
`has_more` / `current_time` / `list_key` / `body_keys` / `warnings` / `source` / `fetched_at`。

- **`messages` 三态**：数组 = 真的这些留言；`null` + `dispute_messages_missing` warning =
  平台没给列表键（**不代表没有留言**）。
- `has_more` 在 `messages is None` 时也是 `null`（不猜）。
- 平台没给 `currentTime` → `dispute_current_time_missing` warning（「还有更多」只能按条数猜）。

### 2.3 `GET /api/disputes/{account_id}/{conversation_id}/unread`

只读，形状同 2.2（少 `begin_time` / `has_more` / `current_time`），warning 码
`dispute_unread_missing`。

### 2.4 四条写（`POST`）

| 路径 | body | 说明 |
| --- | --- | --- |
| `/{account_id}/{conversation_id}/messages` | `text`, `confirm` | 发文字 |
| `/{account_id}/{conversation_id}/messages/image` | `url`, `ext?`, `width?`, `height?`, `alt_text?`, `thumb_url?`, `send_key?`, `confirm` | 发图举证 |
| `/{account_id}/{conversation_id}/urge` | `confirm` | 催办（固定文案 `催促处理维权`） |
| `/{account_id}/{conversation_id}/mark-read` | `msg_id`, `sequence`, `confirm` | 标记已读（`sequence` 必须是整数） |

成功 `data` 键：`account_id` / `action` / `api` / `conversation_id` / `saved` / `msg_id` /
`sequence` / `tips` / `submitted` / `platform_message` / `warnings` / `source` / `fetched_at`。

- **`saved` 只在自己这侧确认「平台接受了」时为 `true`**；平台业务体拒绝 → 502（不是 `saved:false`）。
- 平台接受但没回 `msgId` / `sequence` → `dispute_no_echo` warning（**不是失败**，官方也只 toast
  一句成功）；这时 `msg_id` / `sequence` 是 `null`，**别拿本地数字冒充平台序号**。
- 四条写成功后写 `operation_logs`（action `dispute_send_text` / `dispute_send_image` /
  `dispute_urge` / `dispute_mark_read`）并播 `disputes.changed`（前端 `useAsync` 第 4 参订阅）。

## 3. 确认口径与三步顺序

写路径固定三步：**确认 → 字段校验 → 建客户端**。

- `confirm` **只认**字面 `True` / `'true'` / `1`（服务层 `_truthy`）；`'false'` / `0` / 空 /
  `None` / 其它一律未确认 → 400 `confirmation_required`，**零平台请求**。
- 路由层把 `confirm` **原样**交服务层（`body.get('confirm')`），**不做 `bool()`** ——
  `bool('false')` 是 `True`（第 38 批真缺陷的教训）。
- 服务层判过确认后**归一成 `True`** 再交协议层（协议层 `_need_confirm` 是严格的 `is True`）。

## 4. 错误码

| 情况 | HTTP | code |
| --- | --- | --- |
| 未确认 / `'false'` | 400 | `confirmation_required` |
| 字段缺失（单号全空 / `text` 空 / `sequence` 非整数 / id 超长） | 400 | `invalid_fields` |
| 账号不存在 | 404 | `not_found` |
| 登录态过期 | 409 | `verification_required`（并回写 `need_relogin`） |
| 平台拒绝 / 业务体失败（`FAIL_BIZ_*` / `NO_PERMISSION`） | 502 | `protocol_error`（**不碰 `login_state`**） |
| 没有 Cookie / 账号未就绪 | 503 | `xianyu_not_ready` |

**只读路径的平台拒绝不降级**：这一族三个只读接口真机上都被拒过
（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` / `NO_PERMISSION`），被拒就如实 502 ——
把「没权限」装成「没有留言」是假数据。

## 5. AI 侧（4 写 + 3 读，共 7 个工具）

`disputes_conversation` / `disputes_messages` / `disputes_unread`（只读）、
`disputes_send_text` / `disputes_send_image` / `disputes_urge` / `disputes_mark_read`（写，
`required` 含 `confirm`，description 带 `_WRITE_HINT`）。MCP 路由见
`mcp_server/client.py` 的 `TOOL_ROUTES`。

## 6. 测试

`backend/tests/test_disputes_api.py`（77 条）：协议层（接口名/版本/白名单/四条请求形状/
`content` JSON 串/确认严格性/解析两种 `content` 形态/缺键为 None）、只读三态、确认闸门
（遍历本模块全部 POST 路由 × `FALSE_LIKE` 值，全 400 且零调用）、字段校验（在建客户端之前）、
写成功（请求体逐字、日志与事件、一次调用）、写失败（平台拒绝 502 且不写 `need_relogin`、
业务体拒绝带平台原话、没回执只 warning）、路由（GET/POST 方法、未知会话 404）。
