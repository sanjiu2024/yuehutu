# 超级擦亮写接口契约（ad_write）

> 状态：**已实现，未挂载**。router 在 `backend/app/api/ad_write.py`，**没有** include 进
> `backend/app/main.py`（不在本任务写范围，挂载由 Lead 统一决定）。测试自己 `include_router`。
> 全部接口**从未对真实平台发起过写请求**，单测一律用假协议层。

## 证据来源

- 官方超级擦亮微应用 bundle：`https://g.alicdn.com/idle-pc/fish-ad-pc/0.0.12/js/main.js`
  （2026-10-11 拉取，3,760,912 字节；entry `index.html` 由 `logs/seller_discover/menu.json` 指向）。
  全量 grep 出 **86 个 `mtop.*` 名**，本文件里每个接口名都在 bundle 里有对应的调用点。
- 只读层先例：`backend/app/xianyu/client.py:123-129`（`AD_*_API` 常量）、
  `backend/app/services/promotion.py`、`docs/CONTRACTS.md`。
- 调用形状统一为 `data: { requestJson: JSON.stringify(<业务对象>) }`，bundle 里写死：

  ```js
  vWe = e => yp({api:"mtop.taobao.idle.ad.campaign.edit",
                 data:{requestJson:JSON.stringify(e)}, showErrorToast:!1, needLoginPC:!0})
  ```

## 写接口清单（全部已核实）

| action | mtop 接口名 | 请求字段 | bundle 证据 |
|---|---|---|---|
| `campaign_edit` | `mtop.taobao.idle.ad.campaign.edit` | `requestJson` = 业务对象 | `{api:"...campaign.edit", data:{requestJson:JSON.stringify(e)}, needLoginPC:!0}` |
| `campaign_delete` | `mtop.taobao.idle.ad.campaign.delete` | 同上 | `{api:"...campaign.delete", data:{requestJson:JSON.stringify({...e})}, needLoginPC:!0}` |
| `campaign_batch_delete` | `mtop.taobao.idle.ad.campaign.batch.delete` | 同上（批量 id 列表） | `{api:"...campaign.batch.delete", data:{requestJson:JSON.stringify({...e})}, needLoginPC:!0}` |
| `campaign_pause` | `mtop.taobao.idle.ad.campaign.pause` | 同上 | `{api:"...campaign.pause", data:{requestJson:JSON.stringify({...e})}, needLoginPC:!0}` |
| `campaign_start` | `mtop.taobao.idle.ad.campaign.startV2` | 同上 | `lqe = e => yp({api:"...campaign.startV2", v:"1.0", data:{requestJson:JSON.stringify(e)}, needLoginPC:!0})` |
| `campaign_start_before` | `mtop.taobao.idle.ad.campaign.start.before` | **无**（`data:{}`，不带 `requestJson`） | `oqe = () => yp({api:"...campaign.start.before", v:"1.0", data:{}, needLoginPC:!0})` |
| `item_campaign_renew` | `mtop.taobao.idle.ad.item.campaign.renew` | `requestJson` | `bWe = e => yp({api:"...item.campaign.renew", data:{requestJson:JSON.stringify(e)}})` |
| `activity_apply` | `mtop.taobao.idle.ad.activity.apply` | `requestJson` | `yp({api:"...activity.apply", v:"1.0", needLogin:!0, data:{requestJson:JSON.stringify(e)}})` |
| `activity_task_execute` | `mtop.taobao.idle.ad.activity.task.execute` | `requestJson` | `yp({api:"...activity.task.execute", v:"1.0", needLogin:!0, data:{requestJson:JSON.stringify(e)}, showErrorToast:!0})` |
| `account_log_operates` | `mtop.taobao.idle.ad.bp.account.log.operates` | `requestJson` | `TW = e => yp({api:"...bp.account.log.operates", v:"1.0", data:{requestJson:JSON.stringify(e)}, needLoginPC:!0})` |

字段名一律由调用方放在 `payload` 里透传：bundle 里业务对象由页面组装（`e`），
协议层只负责 `JSON.stringify`，**不猜字段**、不补默认值。

## 明确「没找到」的接口（不实现，不编名）

**充值下单 / 提现下单：bundle 里不存在。**

- 全部 86 个 mtop 名里，账户资金相关的只有 4 个，**都是只读 query**：
  `mtop.taobao.idle.ad.bp.account.card.query`、`.bp.account.log.query`、
  `.bp.account.recharge.query`、`.bp.account.withdraw.query`。
- `recharge` 的 92 处命中全是 CSS 类名（`rechargeBtn`）、状态枚举
  （`ERechargeStatus`）与 `...recharge.query` 调用点，**没有下单接口**。
- `withdraw` 的 12 处命中同构；官方账户页提现按钮的原文是
  `y.info("请进入闲鱼App内操作")`（`Page_PCAdAccountDetail_Withdraw_Button_Click`）——
  官方自己的网页端就不下单，让用户去 App。
- 关键词 `payOrder` / `cashier` 在 bundle 里 **0 命中**。

因此 `recharge` / `withdraw` 两个 action 在服务层直接返回失败信封：

```
{"success": false, "code": "unsupported",
 "message": "官方 bundle 里没有充值下单接口（只有 mtop.taobao.idle.ad.bp.account.recharge.query 这个只读查询），不编接口名、不代下单；充值请在闲鱼 App 内完成"}
```

**一次协议调用都不会发出**（单测 `test_unsupported_funds_actions_refuse_without_any_call` 锁死）。

## HTTP 契约

### `GET /api/promotion/write/actions`

返回 `{"success": true, "data": {"actions": [{action, api, params, confirm_required}], "unsupported": [{action, reason, confirm_required}]}}`。

**这是静态目录，不分页（第 59 批判定）**。判定依据：`actions_catalog()`
（`backend/app/services/ad_write.py:60-72`）的 `actions` 来自**模块级常量**
`ACTIONS = tuple(protocol.WRITE_APIS)`，而 `WRITE_APIS` 是 `backend/app/xianyu/ad_write.py:39`
写死的字典常量（10 个写接口）；`unsupported` 里也是写死的 `recharge` / `withdraw` 两条。
整条路径**没有数据库查询、没有平台请求、没有任何运行期增长来源**——条目数在进程启动时就固定，
所以「无限增长的集合」这个前提不成立，加 `page`/`size` 只是给固定长度的常量数组套壳。
审计 `docs/audit/write_consistency.md` 问题 10 把它列进「集合型端点缺分页」，属**误判**，
本批只补口径、不改接口。

### `POST /api/promotion/write/{account_id}/{action}`

请求体：

```json
{"confirm": true, "payload": {"campaignId": 12345, "title": "新标题"}}
```

- `confirm` **必须是字面量 `true`**。`1`、`"true"`、`{}`、缺省一律 400 `confirm_required`，
  且**在任何协议调用之前**就返回（协议层 `ad_write._require_confirm` 是第二道闸门，
  直接调用协议层函数也会抛 `WriteNotConfirmed`）。
- 成功：`200 {"success": true, "data": {account_id, action, api, confirmed, result, written_at, source: "platform"}}`。
- `result` 就是平台 `data`，**平台没返回时是 `null`，绝不补 0**。

## 错误码

| code | HTTP | 含义 |
|---|---|---|
| `confirm_required` | 400 | 没给 `confirm: true`；无协议调用 |
| `unsupported` | 400 | 充值 / 提现：官方没有下单接口 |
| `not_found` | 404 | 账号不存在 / 动作名不在白名单 |
| `verification_required` | 409 | Cookie 缺失或账号已标记 `need_relogin`；平台返回 `FAIL_SYS_SESSION_EXPIRED` 也走这里并回写账号状态 |
| `protocol_error` | 502 | 平台拒绝（如 `FAIL_SYS_NO_PERMISSION`）原样透出 |
| `xianyu_not_ready` | 503 | 协议层方法缺失 |

**无权限 ≠ 掉线**：`FAIL_SYS_NO_PERMISSION` 之类只走 `protocol_error`，不写 `need_relogin`
（单测 `test_platform_refusal_is_protocol_error_and_not_logout` 断言 `login_state` 仍为 `active`）。

## 未做 / 待办

- router 未挂进 `main.py`（写范围外）。
- 写操作暂未写 `operation_logs`（留痕）与 `promotion.changed` 事件 —— 本次只要求协议层+服务层+API+单测+契约。
- `campaign.start.before` 只做透传，未把它的返回（是否可启动）与本地状态做校验。

## 幂等键（`Idempotency-Key`，第 63 批接入）

带 `Idempotency-Key` 请求头时，**同一键 + 同一指纹**（`account_id` / action / item_id / payload
的 sha256，见 `backend/app/services/common.py::idempotency_fingerprint`）只打平台一次；重复请求
回放上次响应，响应体里带 `idempotent_replay: true`。同键不同指纹 → `409 conflict`
（`code='conflict'`）。**不带请求头时行为与接入前完全一致**。

请求头由 `backend/app/main.py::IdempotencyKeyMiddleware`（纯 ASGI）放进 contextvar，
服务层写漏斗 `common.idempotent_call(account_id, action, item_id, payload, call)` 读取。

| 写族 | 幂等 action | 接入点 |
| --- | --- | --- |
| 推广（超级擦亮） | 请求体 `action`（如 `campaign_edit`） | `backend/app/services/ad_write.py::_write` |
| 安全中心 | 请求体 `action`（如 `security_appeal_submit`） | `backend/app/services/security_write.py::_submit` |
| 客服分组 / 分流规则 | `cs_group_save` / `cs_group_delete` / `cs_routing_rule_save` | `backend/app/services/shop.py` |
| 退货地址 | 路由 action（`add` / `update` / `delete` / `default`） | `backend/app/services/trade_write.py::_address_submit` |
| 订单发货 / 标记发货 | `update_shipment` / `deliver` | `backend/app/services/orders.py` |

回归用例（每个写族至少一条「同键同载荷只打平台一次 + `idempotent_replay`」）：
`backend/tests/test_ad_write_api.py::AdWriteIdempotencyTest`、
`backend/tests/test_security_write_api.py::SecurityWriteIdempotencyTest`、
`backend/tests/test_shop_write_api.py::ShopWriteIdempotencyTest`、
`backend/tests/test_address_write_api.py::AddressWriteIdempotencyTest`、
`backend/tests/test_services_orders.py::OrdersServiceTest::test_update_shipment_same_key_hits_the_protocol_once`、
`::test_deliver_same_key_writes_once`。
