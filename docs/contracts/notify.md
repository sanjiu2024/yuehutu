# 通知中心只读契约

> 本文件是 `backend/app/xianyu/notify.py` / `backend/app/services/notify.py` /
> `backend/app/api/notify.py` 的对外契约，**只覆盖只读能力**。
> 模块文件名是 `notify.py`，**HTTP 路径是 `/api/notifications`**（复数，与前端页面语义一致）。
>
> 证据来源（2026-10-10，账号 `2217970177348`，全部可复核）：
> - **真机只读探针**：`logs/seller_discover/notify_probe.json`
>   （`notify.sessions.list` + `redpoint.query` 原文响应）。
> - **headless Chrome 抓包**：`logs/seller_discover/notify_capture.json` /
>   `notify_capture2.json`（跳 `#/notification-center` 18 秒的真实 mtop 流量）、
>   渲染结果 `notify_page2.txt`。脚本 `C:\Users\Administrator\.dsh-tmp\capture_notify.py`。
> - **契约测试**：`backend/tests/test_notify_api.py`（19 用例，样本即真机原文）。

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

- 要求登录（`Depends(current_user)`），响应走全局信封
  `{success: true, data: {...}}` / `{success: false, message, code, login_state?}`。
- 状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、
  `verification_required`→409、`protocol_error`→502、`xianyu_not_ready`→503。
- **没有任何写方法**：POST/PUT/PATCH/DELETE 一律 405（`test_notify_routes_are_read_only` 锁死）。
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

## 2. 只读保证

官方能「全部标记已读」（`mtop.taobao.idlemessage.pc.notifys.read`，抓包时我误触一条
通知，页面确实发出了这个请求）—— 本项目只复刻「看」，该接口在
`backend/app/xianyu/notify.py` 的 `WRITE_APIS` 里显式列出但**故意不实现**；
`test_no_write_api_is_ever_called` 逐个检查实际 mtop 调用必须落在 `READ_APIS` 里。

## 3. 挂载

`backend/app/main.py` 的 `ROUTE_MODULES` 已挂 `'notify'`（模块名），
`test_real_app_mounts_notify_router` 会同时断言 `/api/notifications/{account_id}` 存在、
且旧的 `/api/notify/{account_id}` **不存在**。
