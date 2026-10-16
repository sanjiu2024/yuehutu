# 第 64 批 · 非商品写路由「载荷形状」依据（write64_params）

> **本文件是第 64 批「不可逆写族真机零副作用探针」的**唯一**前置产物：只做静态审计，未改任何代码、未发任何真机请求、未联网。**
> 生成日期：**2026-10-16**。范围：**非商品写路由**（OpenAPI 里所有非 GET/HEAD 且不属于 `api/items` / `api/drafts` / `api/item-templates` 的路由）。
> 商品域（`api/items` 26 条、`api/drafts` 4 条、`api/item-templates` 4 条）**一律不在本表内** —— 用户明确要求不要动他的商品。

## 0. 口径说明（怎么判的）

### 0.1 判定标签（5 个，前 3 个是任务规定的，后 2 个是本表为可读性加的子档，含义写死在这里）

| 判定 | 含义 | 需要什么证据 |
| --- | --- | --- |
| `形状可信（真机）` | 载荷被**真机**接受过（成功，或参数校验已过、只卡权限/业务） | `logs/**/*.json` 里的真机响应 |
| `形状可信（bundle）` | 载荷**逐字**取自官方 bundle 的调用点（键名、大小写、嵌套层级照抄），**但没有真机证据** | `docs/contracts/*.md` 记录的 bundle 偏移，或 `logs/*evidence*.txt` 里的调用点原文 |
| `缺必填` | 有证据表明我们**少发了平台恒定必填业务键** | 真机回 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数X` / `E_AD_BP_COMMON_PARAM_NULL`，或 bundle 调用点里有我们没带的恒定键 |
| `未知` | 拿不到该接口的平台键名证据 | 无（**宁可写未知，不臆造字段名**） |
| `不适用（本地/只读）` | 该路由**根本不发闲鱼平台写请求**（纯本地 DB / 本地配置 / 只读刷新 / 外部 LLM） | 路由实现 `file:line` |

### 0.2 证据优先级（严格按此顺序取，低优先级不覆盖高优先级）

1. **仓库内已有的真机响应 / 探针日志** —— `logs/write61/`、`logs/fail62/`、`logs/probe63/`、`logs/live63_probe.json`、`logs/write_probe58/`、`logs/pkg_probe58/`、`logs/seller_discover/`、`logs/probe60/`、`logs/probe61/`。
2. **`docs/audit/*.md` 与 `docs/contracts/*.md` 里已记录的 bundle 线索**（含 bundle 偏移与调用点原文）。
3. **代码里的常量定义**（`backend/app/xianyu/*.py` 的 `*_API` 常量、`WRITE_APIS` 表、`BODY_DEFAULTS` / `BODY_REQUIRED`）。

### 0.3 真机错误码怎么读（第 62 批的教训，第 64 批判定必须照此执行）

- `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数X` ⇒ **我们缺 X**。不是权限、不是平台故障。（`logs/fail62/perm/31_backfill_report_with_hdr.json:7`、`logs/write61/05b_address_add_with_area.json:4`）
- `E_AD_BP_COMMON_PARAM_NULL::必填参数为空` ⇒ **缺键，但平台不告诉你键名**，必须去 bundle 对调用点。（`logs/probe60/ad_item_search.json:6`）
- `E_AD_BP_COMMON_SYSTEM_EXCEPTION` / `FAIL_BIZ_COMMON_SYSTEM_ERROR` ⇒ **可能是「键给了但值没用」**（例如 `catId=0`），**不要当平台故障**。（`docs/contracts/ad.md:309`、`docs/contracts/ad.md:290`）
- `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` ⇒ 先怀疑**请求头** `idle_site_biz_code: COMMONPRO` 没带，不是账号没权限。（`docs/audit/api_failures62.md:10-17`、`docs/audit/permission_rootcause62.md`）

### 0.4 计数

非商品写路由共 **105** 条（按 OpenAPI 的 `paths` × 非 GET/HEAD 方法统计，排除 items/drafts/item-templates 三个前缀）。

---

## 1. 主表

> `我们构造处 file:line` = 协议层真正拼 `data` 的那个函数（不是服务层、不是路由）。
> 「平台必填字段」只写**有出处的键名**；拿不到就写「未知」。

### 1.1 auth / settings / 账号（10 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/auth/login | —（本地） | — | `backend/app/api/auth.py:19` | 不适用（本地） | `backend/app/api/auth.py:19` |
| POST /api/auth/desktop | —（本地） | — | `backend/app/api/auth.py:29` | 不适用（本地） | `backend/app/api/auth.py:29` |
| POST /api/auth/logout | —（本地） | — | `backend/app/api/auth.py:42` | 不适用（本地） | `backend/app/api/auth.py:42` |
| PUT /api/settings | —（本地） | — | `backend/app/api/system.py:44` | 不适用（本地） | `backend/app/api/system.py:44` |
| POST /api/accounts/qr/start | 未知（登录族，非 mtop 业务 API） | 未知 | `backend/app/api/accounts.py:251` | 未知 | `backend/app/api/accounts.py:251` |
| POST /api/accounts/qr/poll | 未知（登录族） | 未知 | `backend/app/api/accounts.py:300` | 未知 | `backend/app/api/accounts.py:300` |
| POST /api/accounts/{account_id}/refresh-profile | 平台只读（资料刷新） | — | `backend/app/api/accounts.py:320` | 不适用（POST 但只读） | `backend/app/api/accounts.py:320` |
| PATCH /api/accounts/{account_id} | —（本地 DB） | — | `backend/app/api/accounts.py:348` | 不适用（本地） | `backend/app/api/accounts.py:348` |
| POST /api/accounts/{account_id}/toggle | —（本地 DB） | — | `backend/app/api/accounts.py:379` | 不适用（本地） | `backend/app/api/accounts.py:379` |
| POST /api/accounts/{account_id}/relogin | 未知（登录族） | 未知 | `backend/app/api/accounts.py:396` | 未知 | `backend/app/api/accounts.py:396` |
| POST /api/accounts/{account_id}/captcha | 未知（验证码族） | 未知 | `backend/app/api/accounts.py:427` | 未知 | `backend/app/api/accounts.py:427` |
| DELETE /api/accounts/{account_id} | —（本地 DB） | — | `backend/app/api/accounts.py:473` | 不适用（本地） | `backend/app/api/accounts.py:473` |

### 1.2 chats / uploads（9 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/chats/classify | —（本地/AI） | — | `backend/app/api/chats.py:120` | 不适用（本地） | `backend/app/api/chats.py:120` |
| POST /api/chats/incoming | —（本地 DB） | — | `backend/app/api/chats.py:129` | 不适用（本地） | `backend/app/api/chats.py:129` |
| POST /api/chats/sync | 平台只读（会话同步） | — | `backend/app/api/chats.py:213` | 不适用（只读） | `backend/app/api/chats.py:213` |
| POST /api/chats/{conversation_id}/send | 未知（走 `client.send_message`，不是 mtop 名） | 未知 | `backend/app/services/chats.py:615-629` `_push_message` | 未知 | `backend/app/services/chats.py:618`（真客户端签名 `send_message(cid, text, toid=None)`，原文指向 `backend/app/xianyu/client.py:345`） |
| PUT /api/chats/{conversation_id}/ai | —（本地 DB） | — | `backend/app/api/chats.py:272` | 不适用（本地） | `backend/app/api/chats.py:272` |
| POST /api/chats/{conversation_id}/ai-draft | —（本地/AI） | — | `backend/app/api/chats.py:282` | 不适用（本地） | `backend/app/api/chats.py:282` |
| POST /api/chats/{conversation_id}/mode | —（本地 DB） | — | `backend/app/api/chats.py:293` | 不适用（本地） | `backend/app/api/chats.py:293` |
| POST /api/chats/{conversation_id}/read | —（本地 DB） | — | `backend/app/api/chats.py:299` | 不适用（本地） | `backend/app/api/chats.py:299` |
| POST /api/uploads/images | 未知（闲鱼图床上传，非 mtop） | 未知 | `backend/app/api/uploads.py:51-67` → `uploads.handle_upload` | 未知 | `backend/app/api/uploads.py:67` |

### 1.3 agents / providers / risk（16 条，全部本地或外部 LLM）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| PUT /api/agents/roles/{role} | —（本地 DB） | — | `backend/app/api/agents.py:76` | 不适用（本地） | `backend/app/api/agents.py:76` |
| POST /api/agents/prompts | —（本地 DB） | — | `backend/app/api/agents.py:119` | 不适用（本地） | `backend/app/api/agents.py:119` |
| PUT /api/agents/prompts/{prompt_id} | —（本地 DB） | — | `backend/app/api/agents.py:144` | 不适用（本地） | `backend/app/api/agents.py:144` |
| DELETE /api/agents/prompts/{prompt_id} | —（本地 DB） | — | `backend/app/api/agents.py:176` | 不适用（本地） | `backend/app/api/agents.py:176` |
| DELETE /api/agents/chat/messages | —（本地 DB） | — | `backend/app/api/agents.py:248` | 不适用（本地） | `backend/app/api/agents.py:248` |
| POST /api/agents/master/chat | —（外部 LLM） | — | `backend/app/api/agents.py:263` | 不适用（非闲鱼平台） | `backend/app/api/agents.py:263` |
| POST /api/agents/reply | 未知（会走 chats 的 IM 发送路径） | 未知 | `backend/app/api/agents.py:330` → `backend/app/services/chats.py:1288-1294` | 未知 | `backend/app/services/chats.py:1289` |
| POST /api/providers | —（本地 DB） | — | `backend/app/api/providers.py` | 不适用（本地） | `backend/app/api/providers.py` |
| PUT /api/providers/{provider_id} | —（本地 DB） | — | `backend/app/api/providers.py` | 不适用（本地） | `backend/app/api/providers.py` |
| DELETE /api/providers/{provider_id} | —（本地 DB） | — | `backend/app/api/providers.py` | 不适用（本地） | `backend/app/api/providers.py` |
| POST /api/providers/{provider_id}/test | —（外部 LLM） | — | `backend/app/api/providers.py` | 不适用（非闲鱼平台） | `backend/app/api/providers.py` |
| PUT /api/providers/{provider_id}/default | —（本地 DB） | — | `backend/app/api/providers.py` | 不适用（本地） | `backend/app/api/providers.py` |
| POST /api/providers/{provider_id}/default | —（本地 DB） | — | `backend/app/api/providers.py` | 不适用（本地） | `backend/app/api/providers.py` |
| PUT /api/risk/settings | —（本地 DB） | — | `backend/app/api/risk.py:49` | 不适用（本地） | `backend/app/api/risk.py:49` |
| POST /api/risk/clear | —（本地 DB） | — | `backend/app/api/risk.py:55` | 不适用（本地） | `backend/app/api/risk.py:55` |
| POST /api/risk/{account_id}/clear | —（本地 DB） | — | `backend/app/api/risk.py:65` | 不适用（本地） | `backend/app/api/risk.py:65` |

### 1.4 orders（3 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/orders/sync | 平台只读（`trade.merchant.sold.get` 等） | — | `backend/app/api/orders.py:52` | 不适用（只读） | `docs/contracts/trade.md:87`、`docs/contracts/trade.md:103` |
| PUT /api/orders/{order_id}/shipment | —（本地 DB） | — | `backend/app/api/orders.py:63` | 不适用（本地） | `backend/app/api/orders.py:63` |
| POST /api/orders/{order_id}/deliver | —（本地 DB） | — | `backend/app/api/orders.py:75` | 不适用（本地） | `backend/app/api/orders.py:75` |

### 1.5 api/trade 写族（26 条，本批重点）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/trade/{acc}/orders/ship | `mtop.taobao.idle.logistics.merchant.consign.offline` v1.0 | 未知（契约只记我方入参，未逐字记平台键名） | `backend/app/xianyu/trade_write.py:404` `ship_offline_request` | 未知 | `backend/app/xianyu/trade_write.py:36`；`docs/contracts/trade_write.md:49`、`:90` |
| POST /api/trade/{acc}/orders/{oid}/ship-dummy | `mtop.taobao.idle.logistics.merchant.consign.dummy` v1.0 | 未知 | `backend/app/xianyu/trade_write.py:417` `ship_dummy_request` | 未知 | `backend/app/xianyu/trade_write.py:41`；`docs/contracts/trade_write.md:50`、`:102` |
| POST /api/trade/{acc}/orders/{oid}/adjust-price | `mtop.taobao.idle.trade.merchant.user.adjust.price` v1.0 | `{orderId, modifyFee, newTransportFee}`（金额是**分**的字符串） | `backend/app/xianyu/trade_write.py:429` `adjust_price_request` | 形状可信（bundle） | 官方调用点原文：`hO({api:"mtop.taobao.idle.trade.merchant.user.adjust.price",v:"1.0",data:{orderId:e,modifyFee:t,newTransportFee:n}})` → `logs/quickreply_evidence.txt:37`；`docs/contracts/trade_write.md:107` |
| POST /api/trade/{acc}/refunds/{rid}/agree | `mtop.taobao.idle.merchant.refund.agree.refund` v1.0 | 未知（需要平台操作令牌 `authToken`） | `backend/app/xianyu/trade_write.py:440` `refund_agree_request` | 未知 | `backend/app/xianyu/trade_write.py:51`；`docs/contracts/trade_write.md:52`、`:121`、`:224` |
| POST /api/trade/{acc}/refunds/{rid}/refuse | `mtop.taobao.idle.merchant.refund.refuse` v1.0 | `refundId` / `orderId` / `refuseReasonId`（契约明标必填） | `backend/app/xianyu/trade_write.py:450` `refund_refuse_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:131`；`backend/app/xianyu/trade_write.py:57` |
| POST /api/trade/{acc}/evaluations | `mtop.taobao.idle.merchant.rate.create` v1.0 | `tradeIds` / `rate` / `feedback`（必填）/ `anonymous` | `backend/app/xianyu/trade_write.py:489` `rate_create_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:140`（bundle 偏移 2789045 / 调用点 2790105）；`backend/app/xianyu/trade_write.py:63` |
| POST /api/trade/{acc}/complaints/{cid}/refuse | `mtop.taobao.idle.cco.shop.complain.refuse` v1.0 | 未知 | `backend/app/xianyu/trade_write.py:513` `complain_refuse_request` | 未知 | `backend/app/xianyu/trade_write.py:69`；`docs/contracts/trade_write.md:55`、`:146` |
| POST /api/trade/{acc}/complaints/{cid}/proof | `mtop.taobao.idle.cco.shop.complain.submit.proof`（变体 `submit.active.proof` / `submit.passive.proof`） | 未知 | `backend/app/xianyu/trade_write.py:520` `complain_proof_request` | 未知 | `backend/app/xianyu/trade_write.py:74-76`；`docs/contracts/trade_write.md:159-161` |
| POST /api/trade/{acc}/addresses | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.add` | `areaId`、`defaultAddr`（**平台硬必填**）+ `contactName`/`mobilePhone`/`provinceName`/`cityName`/`districtName`/`detailAddress` | `backend/app/xianyu/address_write.py:152` `add_request`（字段表 `:133` `_address_fields`） | **缺必填（已在第 61 批本地预检补上）** | 真机缺 `areaId`：`logs/write61/05_address_add.json:5`；真机缺 `defaultAddr`：`logs/write61/05b_address_add_with_area.json:4`；补齐后卡权限（参数校验已过）：`logs/write61/05d_address_add_full_raw.json:17`；`docs/contracts/trade_write.md:293-307` |
| POST /api/trade/{acc}/addresses/{cid}/update | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.update` | 同 add + `contactId`（官方必带） | `backend/app/xianyu/address_write.py:162` `update_request`（`data['contactId']` `:172`） | 形状可信（bundle）；平台必填未真机取证 | `backend/app/xianyu/address_write.py:46`、`:172`；`docs/contracts/trade_write.md:58`、`:274` |
| POST /api/trade/{acc}/addresses/{cid}/delete | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.delete` | `{contactId}` | `backend/app/xianyu/address_write.py:176` `delete_request`（`:182`） | 形状可信（bundle） | `backend/app/xianyu/address_write.py:48`、`:182`；`docs/contracts/trade_write.md:59` |
| POST /api/trade/{acc}/addresses/{cid}/default | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.set.default` | `{contactId}` | `backend/app/xianyu/address_write.py:185` `set_default_request`（`:191`） | 形状可信（bundle） | `backend/app/xianyu/address_write.py:52`、`:191`；`docs/contracts/trade_write.md:60` |
| POST /api/trade/{acc}/orders/{oid}/modify-address/agree | `mtop.idle.merchant.order.address.modify.agree` | `{bizOrderId, deliverId}` | `backend/app/xianyu/order_address.py:177` `agree_request`（`:181`） | 形状可信（bundle） | `backend/app/xianyu/order_address.py:47`、`:181`；`docs/contracts/trade_write.md:61`、`:368` |
| POST /api/trade/{acc}/orders/{oid}/modify-address/refuse | `mtop.idle.merchant.order.address.modify.refuse` | `{bizOrderId, deliverId}` | `backend/app/xianyu/order_address.py:184` `refuse_request`（`:188`） | 形状可信（bundle） | `backend/app/xianyu/order_address.py:49`、`:188`；`docs/contracts/trade_write.md:62`、`:368` |
| POST /api/trade/{acc}/orders/{oid}/close | `mtop.taobao.idle.trade.merchant.close.by.seller` **v2.0** | `{tid, bizOrderId, closeReason}` | `backend/app/xianyu/trade_write.py:552` `close_order_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:416`；官方调用点原文 `hO({api:"mtop.taobao.idle.trade.merchant.close.by.seller",v:"2.0",data:{tid:e,bizOrderId:e,closeReason:t}})` → `logs/trade_write_evidence4.txt:28` |
| POST /api/trade/{acc}/orders/{oid}/delay | `mtop.taobao.idle.trade.merchant.batch.delay.confirm` v1.0 | `{orderIdList, delayTime}` | `backend/app/xianyu/trade_write.py:585` `delay_confirm_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:417`（bundle 偏移 2758810） |
| POST /api/trade/{acc}/orders/{oid}/remind | `mtop.taobao.idle.trade.merchant.batch.remind.confirm` v1.0 | `{orderIdList, remindAllOrder}` | `backend/app/xianyu/trade_write.py:596` `remind_confirm_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:418`；官方调用点 `hO({api:"mtop.taobao.idle.trade.merchant.batch.remind.confirm",v:"1.0",data:{orderIdList:e,remindAllOrder:t}})` → `logs/trade_write_evidence4.txt:33` |
| POST /api/trade/{acc}/refunds/{rid}/postage-pay | `mtop.taobao.idle.merchant.postage.refund.pay` v1.0 | `{orderId, refundId, payType}`（+ `ttid`） | `backend/app/xianyu/trade_write.py:605` `postage_refund_pay_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:419`（bundle 偏移 2835556） |
| POST /api/trade/{acc}/complaints/{cid}/revoke | `mtop.taobao.idle.cco.shop.complain.apply.revoke` v1.0 | `{complainId}` | `backend/app/xianyu/trade_write.py:621` `complain_revoke_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:420`；官方调用点 `hO({api:"mtop.taobao.idle.cco.shop.complain.apply.revoke",v:"1.0",data:{complainId:a.complainId},needLoginPC:!0})` → `logs/quickreply_evidence.txt:12` |
| POST /api/trade/{acc}/orders/{oid}/compensations/{sc}/refuse | `mtop.taobao.idle.merchant.compensate.service.refuse` v1.0 | `{orderId, serviceCode, sellerRefuseReason, refuseProofs, isManual}`；`sellerRefuseReason`/`refuseProofs` 是 **JSON 字符串**；**凭证必填** | `backend/app/xianyu/compensate.py:220` `refuse_request`（data 构造 `:241-247`） | 形状可信（bundle） | `backend/app/xianyu/compensate.py:226`、`:240`、`:241-247`；平台原话「请上传凭证」→ `logs/cp45_panel_shots/report.json:11`；`docs/contracts/trade_write.md:478` |
| POST /api/trade/{acc}/orders/{oid}/memo | `mtop.taobao.idle.merchant.add.memo` v1.0 | 未知 | `backend/app/xianyu/trade_write.py:631` `add_memo_request` | 未知 | `backend/app/xianyu/trade_write.py:231`；`docs/contracts/trade_write.md:512` |
| POST /api/trade/{acc}/orders/{oid}/cancel | `mtop.taobao.idle.trade.order.cancel` **v2.0** | `{tid, bizOrderId, closeReason}`（`tid` = `bizOrderId` = 订单号） | `backend/app/xianyu/trade_write.py:677` `order_cancel_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:561`（bundle 调用点 `js_008_vendors.js:1832140`） |
| POST /api/trade/{acc}/orders/{oid}/close-im | `mtop.taobao.idle.trade.close.by.seller` **v2.0** | 同上（逐字同形状） | `backend/app/xianyu/trade_write.py:695` `close_by_seller_im_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:562`（`js_008_vendors.js:1831960`） |
| POST /api/trade/{acc}/orders/{oid}/delay-single | `mtop.taobao.idle.trade.seller.delay.confirm` v1.0 | `{orderId, delayTime: String(days*86400)}` | `backend/app/xianyu/trade_write.py:712` `seller_delay_confirm_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:563`（官方 `g6({delayTime:String(86400*Number(e9)),orderId:eI})` @1854657） |
| POST /api/trade/{acc}/orders/{oid}/adjust-price-sku | `mtop.taobao.idle.trade.user.adjust.price` v1.0 | `{orderId, <skuKey>: '<分>'}`（**每个 SKU 一个顶层键**，不是数组） | `backend/app/xianyu/trade_write.py:733` `user_adjust_price_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:564`（官方价格 `yj(price).mul(100).toString()` @1854181） |
| POST /api/trade/{acc}/smart-form/submit | `mtop.taobao.merchant.heracles.inno.smart.form.callback` v1.0 | `{callbackCode, extJson}`（`extJson` 是 JSON 串，里面 `formData` **又是** JSON 串 ⇒ 双层） | `backend/app/xianyu/trade_write.py:862` `smart_form_callback_request` | 形状可信（bundle） | `docs/contracts/trade_write.md:565`（`main.js:3081308` 与 `js_008_vendors.js:802676` 两处逐字相同） |

### 1.6 api/shop（6 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/shop/{acc}/cs-routing/rule | `mtop.taobao.idlemessage.customer.routing.rule.operate` v1.0 | `{routingRules: <JSON 字符串>}`；内层恒定键 `distributionMethod`/`repeatCustomer`/`offlineMessage`；`offlineMessage='SPECIFIED_ACCOUNT'` 时 `specifiedAccountId`+`specifiedAccountNick` **两者都必填** | `backend/app/xianyu/shop_write.py:203` `routing_rule_request`（rule 构造 `:218-242`） | 形状可信（bundle） | `docs/contracts/shop.md:116`、`:122`；`backend/app/xianyu/shop_write.py:218-231` |
| POST /api/shop/{acc}/cs-groups | `mtop.taobao.idlemessage.customer.group.operate` v1.0 | `{operateType, groupName, serviceRange: <JSON 串>, customers: <JSON 串>}`（`update` 再加 `groupId`） | `backend/app/xianyu/shop_write.py:245` `group_save_request`（data `:288-296`） | 形状可信（bundle） | `docs/contracts/shop.md:117`；`backend/app/xianyu/shop_write.py:288-296` |
| POST /api/shop/{acc}/cs-groups/delete | 同上 | `{operateType:'delete', groupId}`（官方只传这两个） | `backend/app/xianyu/shop_write.py:299` `group_delete_request`（`:305`） | 形状可信（bundle） | `docs/contracts/shop.md:117`；`backend/app/xianyu/shop_write.py:305` |
| POST /api/shop/{acc}/profile/nick | `mtop.alibaba.idle.shop.manage.nick.update` v1.0 | `{userNick}`（官方校验 trim 后 4~12 字） | `backend/app/xianyu/shop_profile.py:123` `nick_request`（`:131`） | 形状可信（bundle） | `backend/app/xianyu/shop_profile.py:33`、`:131`；`docs/contracts/shop.md:204` |
| POST /api/shop/{acc}/profile/desc | `mtop.alibaba.idle.shop.manage.desc.update` v1.0 | `{shopDesc}` | `backend/app/xianyu/shop_profile.py:134` `desc_request`（`:140`） | 形状可信（bundle） | `backend/app/xianyu/shop_profile.py:34`、`:140`；`docs/contracts/shop.md:204` |
| POST /api/shop/{acc}/profile/avatar | `mtop.alibaba.idle.shop.manage.avatar.update` v1.0 | `{userAvatar}`（URL，不是二进制） | `backend/app/xianyu/shop_profile.py:143` `avatar_request`（`:149`） | 形状可信（bundle） | `backend/app/xianyu/shop_profile.py:35`、`:149`；`docs/contracts/shop.md:204` |

### 1.7 api/notifications（1 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/notifications/{acc}/read | `mtop.taobao.idlemessage.pc.notifys.read` v1.0 | `{messageId}`（协议层认为这是唯一键） | `backend/app/xianyu/notify.py:91` `mark_read_request`（body `:96`） | **缺必填（疑似，错误码被伪装成平台故障）** | 真机（`messageId` 缺失变体）回的是 `FAIL_BIZ_IM_PC_READ_STATUS_SYNC_FAILED::服务忙，请稍后重试。||PC端已读状态同步失败` —— **不是** `FAIL_SYS_BIZPARAM_MISSED`：`logs/write61/04_notify_read_missing_id.json:5`、`logs/write61/04b_notify_read_raw.json:5`；`docs/contracts/notify.md:63` |

### 1.8 api/promotion（2 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/promotion/write/{acc}/{action} | 10 个动作（`ad_write.py:23-36`）：`campaign_edit`/`campaign_delete`/`campaign_batch_delete`/`campaign_pause`/`campaign_start`(startV2)/`campaign_start_before`/`item_campaign_renew`/`activity_apply`/`activity_task_execute`/`account_log_operates` | 恒定信封键 `requestJson`（= `JSON.stringify(业务对象)`）—— **除 `campaign_start_before` 外**；该动作官方 `data:{}`、**不带 `requestJson`**。内层业务对象的必填键**未知**（由调用方透传） | `backend/app/xianyu/ad_write.py:39` `WRITE_APIS` + `ad_write.py:73`（start_before）/ `:82-110`（各动作） | 形状可信（bundle） | `docs/contracts/ad_write.md:25-34`（每行都给 bundle 调用点原文）；`backend/app/xianyu/ad_write.py:74`、`:83` |
| POST /api/promotion/ad/{acc}/messages/read | `mtop.taobao.idle.ad.message.read` | `{messageIds:[...]}`（走 `requestJson` 信封；`message_id` 必填） | `backend/app/api/ad.py:250` → `backend/app/xianyu/ad.py:350` `message_read`（`:352`） | 形状可信（bundle） | `backend/app/xianyu/ad.py:62`、`:350-352`；`docs/contracts/ad.md:168`（官方 @3696148） |

### 1.9 api/qualification（2 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/qualification/{acc}/packages/submit | `mtop.taobao.idle.ad.qualification.packages.submit` | 未知（恒定信封是 `{requestJson: <业务对象>}`，业务对象字段名未知） | `backend/app/xianyu/qualification.py:55`（api 常量）+ `:73` `_post`（信封 `:76`） | 未知 | `backend/app/xianyu/qualification.py:55`、`:76`；`docs/contracts/qualification.md:58` |
| POST /api/qualification/{acc}/oss-credential | `mtop.taobao.idle.ad.qualification.get.oss.credential` | 未知（同上） | `backend/app/xianyu/qualification.py:56` + `:73` `_post`（`:76`） | 未知 | `backend/app/xianyu/qualification.py:56`、`:76`；`docs/contracts/qualification.md:59` |

### 1.10 api/security-center（4 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/security-center/{acc}/appeal/form | `mtop.idle.pc.security.appeal.form.get` v1.0 | `{formId}` | `backend/app/xianyu/security_write.py:99` `appeal_form_request` | 形状可信（bundle，**契约标注「未验证」**） | `docs/contracts/security_write.md:47`、`:61`；`backend/app/xianyu/security_write.py:54` |
| POST /api/security-center/{acc}/appeal/submit | `mtop.idle.pc.security.appeal.submit` v1.0 | `{parameter}`（`parameter` 是 JSON 串） | `backend/app/xianyu/security_write.py:107` `appeal_submit_request` | 形状可信（bundle，未验证） | `docs/contracts/security_write.md:48`；`backend/app/xianyu/security_write.py:59` |
| POST /api/security-center/{acc}/appeal/cro/submit | `mtop.idle.security.appeal.submit` v1.0 | `parameter = JSON.stringify({punishId: Number(...), proofItems:[{code,name,index,details:[{value,desc}]}]})` | `backend/app/xianyu/security_write.py:148` `appeal_submit_cro_request`（`:157-159`） | 形状可信（bundle，未验证） | `docs/contracts/security_write.md:50`、`:85`；`backend/app/xianyu/security_write.py:158` |
| POST /api/security-center/{acc}/appeal/urge | `mtop.idle.pc.security.appeal.urge` v1.0 | `{punishId: Number(...)}`（官方做了 `Number()`） | `backend/app/xianyu/security_write.py:162` `appeal_urge_request`（`:168`） | 形状可信（bundle，未验证） | `docs/contracts/security_write.md:49`；`backend/app/xianyu/security_write.py:168` |

### 1.11 api/sub-accounts（5 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/sub-accounts/{acc} | `mtop.alibaba.idle.seller.platform.common.sub.account.register` | `{nick, mobile, roleIds, encryptedPassword, cryptoKey}`（可选 `customName`/`ext`） | `backend/app/xianyu/sub_account.py:293` `create_request`（data `:301-307`） | 形状可信（bundle） | `backend/app/xianyu/sub_account.py:51`、`:301-307` |
| POST /api/sub-accounts/{acc}/{sub_id}/delete | `mtop.alibaba.idle.seller.platform.sub.account.delete` | `{subId}` | `backend/app/xianyu/sub_account.py:317` `delete_request`（`:319`） | 形状可信（bundle） | `backend/app/xianyu/sub_account.py:52`、`:319` |
| POST /api/sub-accounts/{acc}/{sub_id}/mobile | `mtop.alibaba.idle.seller.platform.sub.account.mobilephone.update` | `{subId, mobilePhone}` | `backend/app/xianyu/sub_account.py:322` `mobile_request`（`:324`） | 形状可信（bundle） | `backend/app/xianyu/sub_account.py:53`、`:324` |
| POST /api/sub-accounts/{acc}/{sub_id}/roles | `mtop.alibaba.idle.seller.platform.sub.account.role.update` | `{subId, roleIds}` | `backend/app/xianyu/sub_account.py:327` `roles_request`（`:329`） | 形状可信（bundle） | `backend/app/xianyu/sub_account.py:54`、`:329` |
| POST /api/sub-accounts/{acc}/{sub_id}/password | `mtop.alibaba.idle.seller.platform.sub.account.password.update` | `{subId, encryptedPassword, cryptoKey}` | `backend/app/xianyu/sub_account.py:332` `password_request`（`:335-338`） | 形状可信（bundle） | `backend/app/xianyu/sub_account.py:55`、`:335-338` |

### 1.12 api/quick-reply（6 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/quick-reply/{acc}/groups | `mtop.taobao.idlemessage.quickreply.group.operate` v1.0 | create：`{groupName, operateType}` | `backend/app/xianyu/quick_reply.py:188` `group_request`（`:200`） | 形状可信（bundle） | `backend/app/xianyu/quick_reply.py:43`、`:200`；`docs/contracts/quick_reply.md:15` |
| POST /api/quick-reply/{acc}/groups/{group_id} | 同上 | update：`{groupId, groupName, operateType}` | `backend/app/xianyu/quick_reply.py:188`（`:202-206`） | 形状可信（bundle） | `backend/app/xianyu/quick_reply.py:202-206`；`docs/contracts/quick_reply.md:15` |
| POST /api/quick-reply/{acc}/groups/{group_id}/delete | 同上 | delete：`{groupId, operateType}` | `backend/app/xianyu/quick_reply.py:188`（`:207`） | 形状可信（bundle） | `backend/app/xianyu/quick_reply.py:207`；`docs/contracts/quick_reply.md:15` |
| POST /api/quick-reply/{acc}/phrases | `mtop.taobao.idlemessage.quickreply.content.operate` v1.0 | create：`{groupId, content, operateType}` | `backend/app/xianyu/quick_reply.py:210` `content_request`（`:223-227`） | 形状可信（bundle） | `backend/app/xianyu/quick_reply.py:44`、`:223-227`；`docs/contracts/quick_reply.md:16` |
| POST /api/quick-reply/{acc}/phrases/{content_id} | 同上 | update：`{groupId, contentId, content, operateType}` | `backend/app/xianyu/quick_reply.py:210`（`:229-234`） | 形状可信（bundle） | `backend/app/xianyu/quick_reply.py:229-234`；`docs/contracts/quick_reply.md:16` |
| POST /api/quick-reply/{acc}/phrases/{content_id}/delete | 同上 | delete：`{groupId, contentId, operateType}` | `backend/app/xianyu/quick_reply.py:210`（`:235-239`） | 形状可信（bundle） | `backend/app/xianyu/quick_reply.py:235-239`；`docs/contracts/quick_reply.md:16` |

### 1.13 api/logistics（1 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/logistics/{acc}/orders/{oid}/resend | `mtop.taobao.idle.logistics.merchant.consign.resend` v1.0 | `{tradeId, mailNo, cpCode}`（`cpCode` 留空用官方默认 `OTHER`） | `backend/app/xianyu/logistics.py:143` `resend_request`（data `:154-158`） | 形状可信（bundle） | `docs/contracts/logistics.md:21`、`:41`；`backend/app/xianyu/logistics.py:50`、`:147`、`:154-158` |

### 1.14 api/im（8 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/im/{acc}/blacklist | `mtop.taobao.idlemessage.pc.blacklist.add` **v2.0** | `{sessionId}` | `backend/app/xianyu/im_actions.py:280` `blacklist_add_request`（`:284`） | 形状可信（bundle） | `backend/app/xianyu/im_actions.py:59`、`:284` |
| POST /api/im/{acc}/blacklist/remove | `mtop.taobao.idlemessage.pc.blacklist.remove` v1.0 | `{sessionId}` | `backend/app/xianyu/im_actions.py:287` `blacklist_remove_request`（`:291`） | 形状可信（bundle） | `backend/app/xianyu/im_actions.py:62`、`:291` |
| POST /api/im/{acc}/cards | `mtop.taobao.idlemessage.message.card.send` v1.0 | `{sessionId, itemId}` | `backend/app/xianyu/im_actions.py:294` `card_send_request`（`:299`） | 形状可信（bundle） | `backend/app/xianyu/im_actions.py:66`、`:299` |
| POST /api/im/{acc}/sessions/transfer | `mtop.taobao.idlemessage.customer.deliver.session` v1.0 | `{sessionId, targetUid}` | `backend/app/xianyu/im_actions.py:302` `session_deliver_request`（`:307-308`） | 形状可信（bundle） | `backend/app/xianyu/im_actions.py:72`、`:307-308` |
| POST /api/im/{acc}/sessions/leave | `mtop.taobao.idlemessage.customer.leave.session` v1.0 | `{sessionId}` | `backend/app/xianyu/im_actions.py:311` `session_leave_request`（`:316`） | 形状可信（bundle） | `backend/app/xianyu/im_actions.py:75`、`:316` |
| POST /api/im/{acc}/sessions/rejoin | `mtop.taobao.idlemessage.customer.rejoin.session` v1.0 | `{sessionId}` | `backend/app/xianyu/im_actions.py:319` `session_rejoin_request`（`:324`） | 形状可信（bundle） | `backend/app/xianyu/im_actions.py:79`、`:324` |
| POST /api/im/{acc}/reception | `mtop.taobao.idlemessage.pc.repect.status.edit` v1.0 | `{status}` | `backend/app/xianyu/im_actions.py:333` `reception_set_request`（`:338`） | 形状可信（bundle）+ **真机 `FAIL_BIZ_NOT_SUB_ACCOUNT`**（该接口只对子账号开放，本账号是主账号 ⇒ 不是 bug） | `backend/app/xianyu/im_actions.py:95`、`:338`；真机：`logs/write_probe58/reception_write.json:12`；`docs/audit/api_failures62.md:69` |
| POST /api/im/{acc}/unread/clean | `mtop.taobao.idlemessage.pc.session.unread.clean`（另一动作 `...pc.systems.unread.clean`） | **不带 `data`**（官方原样：`needLoginPC: true` + 无 data） | `backend/app/xianyu/im_tools.py:223` `clear_session_unread_request` / `:230` `clear_systems_unread_request` | 形状可信（bundle） | `backend/app/xianyu/im_tools.py:23-25`、`:40-41`、`:223`、`:230` |

### 1.15 api/disputes（4 条）

| 路由 | 协议 api | 平台必填字段 | 我们构造处 file:line | 判定 | 证据 file:line |
| --- | --- | --- | --- | --- | --- |
| POST /api/disputes/{acc}/{cid}/messages | `mtop.taobao.idle.shop.dispute.message.send` v1.0 | `{conversationId, sceneType:'userActive', content: <JSON 串>, contentType:'PLAIN_TEXT'}` | `backend/app/xianyu/dispute.py:298` `send_text_request`（`:303-308`） | 形状可信（bundle） | `backend/app/xianyu/dispute.py:56`、`:303-308`；`docs/contracts/disputes.md:20` |
| POST /api/disputes/{acc}/{cid}/messages/image | 同上 | 同上 + `contentType:'IMAGE'`、`content` = `{url,ext,width,height,altText,thumbURL,sendKey}` 的 JSON 串、**总是带顶层 `sendKey`**（官方空串也带） | `backend/app/xianyu/dispute.py:311` `send_image_request`（`:318-324`） | 形状可信（bundle） | `backend/app/xianyu/dispute.py:315`、`:318-324`；`docs/contracts/disputes.md:21` |
| POST /api/disputes/{acc}/{cid}/urge | 同上 | `content = JSON.stringify({text:'催促处理维权'})` | `backend/app/xianyu/dispute.py:327` `urge_request`（`:331-336`） | 形状可信（bundle） | `backend/app/xianyu/dispute.py:335`；`docs/contracts/disputes.md:22` |
| POST /api/disputes/{acc}/{cid}/mark-read | `mtop.taobao.idle.shop.dispute.message.mark.read` v1.0 | `{conversationId, msgId, sequence: 整数}` | `backend/app/xianyu/dispute.py:283` `mark_read_request`（`:291-295`） | 形状可信（bundle） | `backend/app/xianyu/dispute.py:51`、`:291-295`；`docs/contracts/disputes.md:23` |

---

## 2. 缺必填 / 未知 清单

### 2.1 已确认「缺恒定必填键」（第 61/62 批已修，第 64 批**不要再当新发现**）

| # | 接口 | 缺什么 | 凭什么说缺 | 建议 |
| --- | --- | --- | --- | --- |
| 1 | `mtop.taobao.idle.ad.item.searchV2`（GET，只读） | `tab` / `needTabs` | 真机 `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`（`logs/probe60/ad_item_search.json:6`、`logs/probe60/round2/ad_item_search_by_key.json:6`）；bundle 调用点恒定带 `{tab:'platReco', needTabs:true}`（`docs/contracts/ad.md:267`、`:280`） | **已修**：`backend/app/xianyu/ad.py:123` `BODY_DEFAULTS` + `:141` `BODY_REQUIRED`；真机 200 |
| 2 | `mtop.taobao.idle.ad.package.info`（GET，只读） | `entityType` / `entityList`；且 `catId` 必须是**真实类目 ID** | 真机 `E_AD_BP_COMMON_PARAM_NULL`（`logs/probe60/ad_package_info.json:6`）；`catId=0` 时回的是 `E_AD_BP_COMMON_SYSTEM_EXCEPTION`（`docs/contracts/ad.md:287-290`） | **已修**（`docs/contracts/ad.md:268`） |
| 3 | `mtop.taobao.idle.ad.customize.bid.estimates`（GET，只读） | `entityType` / `entityList`；`pcFirstRequest` 官方默认 `true` | 本地 `invalid_fields` + bundle 调用点（`docs/contracts/ad.md:269`、`:296`） | **已修** |
| 4 | `mtop.taobao.idle.ad.campaign.log.query`（GET，只读） | `operatorType=0` / `operationType=0` | 真机 `E_AD_BP_COMMON_SYSTEM_EXCEPTION`（`logs/probe60/ad_campaign_log.json:6`）；bundle 初值就是 0（`docs/contracts/ad.md:303`） | **已修** |
| 5 | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.add`（**写**） | `areaId`、`defaultAddr` | 真机先回 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数areaId`（`logs/write61/05_address_add.json:5`），补 `areaId` 后回 `缺少业务参数defaultAddr`（`logs/write61/05b_address_add_with_area.json:4`），两个都补后卡权限（参数校验已过，`logs/write61/05d_address_add_full_raw.json:17`） | **已修**：服务层本地预检 `_address_fields_problem`（`docs/contracts/trade_write.md:301-307`），缺则**一个平台请求都不发** |

### 2.2 第 64 批**新增**的「疑似缺必填，且错误码被伪装」

| # | 路由 | 缺什么 | 凭什么说缺（**关键**） | 建议 |
| --- | --- | --- | --- | --- |
| 1 | `POST /api/notifications/{acc}/read` | 疑似缺 `messageId` 之外的恒定键（或 `messageId` 值域不对） | 真机（缺 id 变体）**没有**回 `FAIL_SYS_BIZPARAM_MISSED`，而是回 `FAIL_BIZ_IM_PC_READ_STATUS_SYNC_FAILED::服务忙，请稍后重试。||PC端已读状态同步失败` —— 这**正是第 62 批那类「伪装成系统异常」**：`logs/write61/04_notify_read_missing_id.json:5`、`logs/write61/04b_notify_read_raw.json:5`。协议层当前只发 `{messageId}`（`backend/app/xianyu/notify.py:96`） | 打真机前先抓官方通知中心点「已读」的请求体（`docs/contracts/notify.md:63` 记了该 URL，但没记 body）；拿到真 body 再打，否则会把 `SYNC_FAILED` 误判成平台故障 |
| 2 | `POST /api/trade/{acc}/smart-form/submit`（写）与其只读同族 `smart.form.query` | `extJson` 内层 `formData` 是否还有恒定键 | 只读同族真机回兜底 `UNKNOWN_FAIL_CODE::系统开小差了，请稍候重试`，**必填键不是 `orderId`**（`docs/contracts/trade_write.md:691`）；且契约明确写「缺 `bizCode`/`scene` 之类的必填键」这一推断**不是实证**（`docs/contracts/trade_write.md:739-740`） | 打真机前先取一次 `smart.form.query` 的真实 URL 参数（`smart_form_params_from_url`，`backend/app/xianyu/trade_write.py:822`），别硬编码键名 |
| 3 | `POST /api/trade/{acc}/addresses`（add/update） | `areaId` 的**值域**（写法） | 真机只证明**裸 `divisionId`**（`330106`）能过平台必填校验；`province/city/district` 三段式写法**未取证**（`docs/contracts/trade_write.md:315`） | 沿用第 61 批的裸 `divisionId` 写法，别改 |

### 2.3 「未知」清单（打真机前必须先拿到载荷证据，否则极易把参数问题误判成平台故障）

| # | 路由 | 协议 api | 缺什么证据 | 建议补什么 |
| --- | --- | --- | --- | --- |
| 1 | POST /api/trade/{acc}/orders/ship | `...logistics.merchant.consign.offline` | 平台键名（契约只记我方入参） | 抓 bundle 调用点；先对 `docs/contracts/trade_write.md:90` |
| 2 | POST /api/trade/{acc}/orders/{oid}/ship-dummy | `...logistics.merchant.consign.dummy` | 同上 | `docs/contracts/trade_write.md:102` |
| 3 | POST /api/trade/{acc}/refunds/{rid}/agree | `mtop.taobao.idle.merchant.refund.agree.refund` | 平台键名 + `authToken` 来源 | `docs/contracts/trade_write.md:121`、`:224` |
| 4 | POST /api/trade/{acc}/complaints/{cid}/refuse | `mtop.taobao.idle.cco.shop.complain.refuse` | 平台键名 | `docs/contracts/trade_write.md:146` |
| 5 | POST /api/trade/{acc}/complaints/{cid}/proof | `...complain.submit.proof`（三变体） | 平台键名 + 选哪个变体 | `docs/contracts/trade_write.md:159-161` |
| 6 | POST /api/trade/{acc}/orders/{oid}/memo | `mtop.taobao.idle.merchant.add.memo` | 平台键名 | `docs/contracts/trade_write.md:512` |
| 7 | POST /api/qualification/{acc}/packages/submit | `...qualification.packages.submit` | `requestJson` 内层业务对象字段名 | `docs/contracts/qualification.md:58` |
| 8 | POST /api/qualification/{acc}/oss-credential | `...qualification.get.oss.credential` | 同上 | `docs/contracts/qualification.md:59` |
| 9 | POST /api/chats/{cid}/send | 非 mtop（`client.send_message`） | 平台侧真正的写接口与键名 | `backend/app/services/chats.py:615-629`、`backend/app/xianyu/client.py:345` |
| 10 | POST /api/agents/reply | 同上（走 chats 发送路径） | 同上 | `backend/app/services/chats.py:1288-1294` |
| 11 | POST /api/uploads/images | 闲鱼图床（非 mtop） | 上传接口与响应字段 | `backend/app/api/uploads.py:51-67` |
| 12 | POST /api/accounts/qr/start · qr/poll · relogin · captcha | 登录族 | 登录链路接口名 | `backend/app/api/accounts.py:251`、`:300`、`:396`、`:427` |
| 13 | POST /api/promotion/write/{acc}/{action} | 10 个动作 | **内层业务对象**的必填键（信封 `requestJson` 已确定） | `docs/contracts/ad_write.md:25-34` 只给了 `requestJson` 信封；内层要抓官方页面提交体 |

### 2.4 打真机前的三条硬规矩（从第 62 批的失败里得来）

1. **官方恒定带的键要照抄，不要按需给** —— `tab` / `needTabs` / `operatorType` / `operationType` / `pcFirstRequest` / `isGoodsSelectedTabChange` 官方每次调用都带（`docs/contracts/ad.md:310`）。
2. **`PARAM_NULL` 与 `SYSTEM_EXCEPTION` 是两类问题** —— 前者是「键没给」，后者也可能是「键给了但值没用」；只看错误码会把值域问题误判成平台故障（`docs/contracts/ad.md:309`）。
3. **别把 `FAIL_SYS_BIZPARAM_MISSED` 当鉴权失败** —— `...order.backfill.report` 补 `idle_site_biz_code` 头后从 `UNAUTHORIZED` 变成「缺 `orderId`」（`docs/audit/permission_rootcause62.md:139`）。

---

## 3. 未完成项 / 本文件的边界

- 本表**没有**逐条核对 `logs/write61/` 与 `logs/probe63/` 的全部 JSON 响应原文；只按 §0.2 的优先级取了能直接支撑判定的那些行。
- `POST /api/providers/*` 6 条只按「本地 DB / 外部 LLM」判「不适用」，未逐行核对实现（文件为 `backend/app/api/providers.py`，路由装饰器行号本轮未取）。
- `api/trade` 的 26 条里，`ship` / `ship-dummy` / `refund.agree` / `complain.refuse` / `complain.proof` / `add.memo` 六条的**平台键名仍未知** —— 这是第 64 批打真机前最该补的六条。
- 本文件**不含任何真机调用结果**（第 64 批尚未开打），也**不含任何未在 §0.2 优先级里找到出处的字段名**。
