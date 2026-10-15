# 通知中心只读契约

> 本文件是 `backend/app/xianyu/notify.py` / `backend/app/services/notify.py` /
> `backend/app/api/notify.py` 的对外契约，覆盖**读**与**唯一一条确认写**（标记已读）。
> 模块文件名是 `notify.py`，**HTTP 路径是 `/api/notifications`**（复数，与前端页面语义一致）。
>
> 证据来源（2026-10-10，账号 `2217970177348`，全部可复核）：
> - **真机只读探针**：`logs/seller_discover/notify_probe.json`
>   （`notify.sessions.list` + `redpoint.query` 原文响应）。
> - **headless Chrome 抓包**：`logs/seller_discover/notify_capture.json` /
>   `notify_capture2.json`（跳 `#/notification-center` 18 秒的真实 mtop 流量）、
>   渲染结果 `notify_page2.txt`。脚本 `C:\Users\Administrator\.dsh-tmp\capture_notify.py`。
> - **契约测试**：`backend/tests/test_notify_api.py`（32 用例，样本即真机原文）。

## 0. 一句话结论（重要）

**通知中心没有「列表」接口。** 页面上那些条目（「超时未付款，系统关闭了订单」
「买家已确认收货，交易成功」…）**不是 REST 拉来的**：18 秒抓包期间页面只调了两个业务
接口，条目由 IM/ACCS 长连接推送（`idlemessage.pc.login.token` / `accs.token` 那条链路）。

REST 侧只有两样东西，都是真值：

| 能力 | 真机结果 | 结论 |
| --- | --- | --- |
| 频道树 `idlemessage.pc.notify.sessions.list` | `SUCCESS`，`notifyInfoList`（`全部` / `重要` / `交易履约{订单,售后,物流,评价}` / `违规处理{…}` / `小铺经营{…}`） | 可读 |
| 未读总数 `idlemessage.pc.redpoint.query` | `SUCCESS`，`{needDecryptKeys: [], total: 0}`（**平台明说的 0**） | 可读 |
| 通知条目列表 | 不存在 | `items: []` + 中文 warning，**绝不造条目** |
| `idle.web.seller.page.my` | `FAIL_SYS_API_NOT_FOUNDED::请求API不存在` | bundle 里的**死代码**（该 API 未在 appKey 上注册），本模块不调 |

因此本接口给的是「频道 + 未读总数」，条目留给后续 IM 推送链路（新功能，不在本次范围）。

## 1. 已实现（只读）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/notifications/{account_id}` | 频道树 + 未读总数。Query：`page`（默认 1）、`size`（默认 20，限 1–100） |
| POST | `/api/notifications/{account_id}/read` | **标记单条已读**。Body：`{"message_id": "...", "confirm": true}`；闸门在服务层第一行（缺 `confirm` → 400 `confirmation_required`，**零平台请求**） |

- 要求登录（`Depends(current_user)`），响应走全局信封
  `{success: true, data: {...}}` / `{success: false, message, code, login_state?}`。
- 状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、
  `verification_required`→409、`protocol_error`→502、`xianyu_not_ready`→503。
- **写能力只有「标记已读」这一条**，且必须带 `confirm`：其余子路径的 POST 一律 405（`test_notify_routes_are_one_read_plus_one_confirmed_write` 锁死；注意 GET 打不存在的路径才 404，POST 打任何不存在的子路径也是 405 ⇒ **405 不能当「路由存在」的证据**）。
- `page`/`size` 目前是**空操作**：REST 没有可分页的列表，两个参数只做校验并原样回显，
  好让前端契约先固定下来（`size=0` / `size=101` / `size=x` / `page=y` / `page=0` / `page=-1`
  → `400 invalid_fields`，且**一次平台都不打**）。

### 1.1 返回字段

| 字段 | 来源 | 口径 |
| --- | --- | --- |
| `account_id` | 入参 | |
| `channels` | 平台 `notifyInfoList` **原样透传** | **缺 `notifyInfoList` 键 → `null`** + 中文 warning（缺键 ≠ 没有频道）；平台给了空数组才给 `[]` |
| `unread.total` | 平台 `total` | 平台明说的 `0` 照用；**键不存在 → `null`**（不是 0）+ 中文 warning。**未读数接口挂了也不影响整页**：频道照给、`unread.total` 是 `null` + warning（早先一次抖动会整页 502，与小铺的 best-effort 口径不一致，已统一） |
| `items` | —— | 恒为 `[]`（见 §0），并附「条目经 IM 长连接推送」的 warning |
| `page` / `page_size` | 入参 | 校验后回显 |
| `warnings` | 本层 | 中文 |
| `fetched_at` | 本层 | UTC `%Y-%m-%d %H:%M:%S` |
| `source` | 本层 | 恒为 `'platform'` |

## 2. 写能力的边界（2026-10-15 第 58 批起）

官方能「全部标记已读」（`mtop.taobao.idlemessage.pc.notifys.read`，抓包时我误触一条
通知，页面确实发出了这个请求）。**2026-10-15 起本项目复刻了这一条**：协议层
`backend/app/xianyu/notify.py:44 WRITE_APIS` / `:47 WRITE_VERSIONS` 登记，服务层
`backend/app/services/notify.py::mark_read` 是唯一写入口，协议层 `_need_confirm` 只认字面 `True`。

口径（真机验证过）：

- 平台回 `{"success": true, "data": {"success": true}, "message": "SUCCESS::调用成功"}` ⇒ `saved=true`
  并落 `operation_logs`（`action='notify_mark_read'`）；证据 `logs/notify_read58/07b_raw_mtop_response.json`。
- 业务体**没有** `success` 键 ⇒ `saved=false` + warning `notify_mark_read_no_echo`，**不写审计**；
  平台明说 `success:false` 或信封失败 ⇒ 502 `protocol_error` + 照抄平台原话，**不回写 `login_state`**。
- 真机探针：`scripts/probe_notify_read.py`（不带 `--yes` 只跑闸门、一个平台请求都不发；带 `--yes` 才会真的改平台状态）。
- 证据目录：`logs/notify_read58/`（含审计行 `08_audit_rows.json`）。

## 3. 挂载

`backend/app/main.py` 的 `ROUTE_MODULES` 已挂 `'notify'`（模块名），
`test_real_app_mounts_notify_router` 会同时断言 `/api/notifications/{account_id}` 存在、
且旧的 `/api/notify/{account_id}` **不存在**。
