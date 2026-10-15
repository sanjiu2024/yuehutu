# 交易组**写**接口契约（发货 / 改价 / 退款处理 / 评价 / 投诉处理）

> 本文件是 `backend/app/xianyu/trade_write.py` / `backend/app/services/trade_write.py` /
> `backend/app/api/trade_write.py` 的对外契约。
>
> **⚠️ 全部接口名与参数形状都是「静态提取」，没有一条经过真机验证。**
> 证据来源只有一个：官方交易微应用 bundle
> `https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3,879,584 字节，纯 ASCII，
> 字符偏移 = 字节偏移）。本地副本 `C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`，
> 提取产物同目录 `mtop_names.json` / `shapes2.json`，报告
> `C:\Users\Administrator\.dsh-tmp\trade_write_discovery.md`。
> **写接口一律禁止对真实平台试打**（会真的发货 / 真的退款），所以「未真机验证」不是遗漏，是红线。
>
> 只读能力见 `docs/contracts/trade.md`；本文件只讲写。

## 0. 三条硬红线

1. **没确认不写**：每个写函数/每个 HTTP 端点都带 `confirm`（默认 `false`）。
   未确认时服务层直接返回 `confirmation_required`（HTTP 400），**一个平台请求都不发**。
   交易写族认**宽松档** `confirm`（`common.confirm_truthy`：布尔 true / 字符串 true / 数字 1 都认），因为 AI 与 MCP 客户端可能传字符串；
   投放消息已读那条走**严格档** `strict=True`（只认字面 `true`）。两档都是第 59 批 `715d52e` 定下的既有设计，严格档另有 `services/ad_write.py:78`、`services/qualification.py:279,299`；两档都要求显式确认，缺省一律 400。
   协议层的请求构造器还会再拦一次（`WriteNotConfirmed`），两道闸门。
2. **平台没返回的就是 `null`，不许补 0**：发货计数平台只回 `successNum` 时，
   `total_num` / `fail_num` 必须是 `None`（有用例锁死）。
3. **无权限 ≠ 掉线**：`NO_PERMISSION::用户没有权限` /
   `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` 一律走 `protocol_error`（HTTP 502）并原样透出
   `platform_errors.explain`，**不会**把账号写成 `need_relogin`。只有平台真的回
   `SESSION_EXPIRED`（`common._is_session_expired` 按类名/文案认）才会标掉线。

### 0.1 幂等（**可选**请求头 `Idempotency-Key`，第五十九批）

写请求可以带一个 `Idempotency-Key` 头（值自拟、≤200 字符，建议 UUID）。**不带就是加机制之前的行为**，逐字一致。

- 同一个键 + 同一个请求（指纹 = 账号 + action + item_id + body）**只打平台一次**；重复请求**回放**上次那份响应，
  额外带 `"idempotent_replay": true`，**不再打平台、不再写第二条 `operation_logs`**。
- 同一个键配**不同**请求 → `code: 'conflict'`（HTTP 409），既不打平台也不覆盖原记录。
- **失败响应也会被记住**（平台可能已生效、只是响应丢了）⇒ 想重试请**换一个新键**。
- 没 `confirm`、参数不全的请求在指纹之前就被闸门拦掉，**不占键**；补上确认后同一个键仍可正常提交。
- 落点：`backend/app/main.py::IdempotencyKeyMiddleware` → `backend/app/services/common.py::idempotent_call` →
  本族唯一漏斗 `backend/app/services/trade_write.py::_submit`（本文件 §1 表里的 **26 个 POST 全部覆盖**）。
- 边界：同键并发只在本进程内串行（多 worker 部署挡不住，要靠主键抢占才能根治，本批没做）；
  键是调用方的责任 —— 不能复用到别的请求，也不做限流。
- 完整语义与其它写族的覆盖情况见 `docs/CONTRACTS.md` §9；用例 `backend/tests/test_idempotency.py`（14 条）。

## 1. HTTP（26 个 POST + 8 个只读 GET，全部要求登录）

| 方法 | 路径 | body | 说明 |
| --- | --- | --- | --- |
| POST | `/api/trade/{account_id}/orders/ship` | `{trade_ids, mail_no, cp_code, address_id, confirm}` | 订单发货（自己联系物流） |
| POST | `/api/trade/{account_id}/orders/{order_id}/ship-dummy` | `{trade_text, pic_urls, confirm}` | 无需物流发货（虚拟发货） |
| POST | `/api/trade/{account_id}/orders/{order_id}/adjust-price` | `{modify_fee, new_transport_fee, confirm}` | 订单改价，**单位：元** |
| POST | `/api/trade/{account_id}/refunds/{refund_id}/agree` | `{auth_token, confirm}` | 同意退款 |
| POST | `/api/trade/{account_id}/refunds/{refund_id}/refuse` | `{order_id, refuse_reason_id, proof_desc, proof_urls, negotiation, confirm}` | 拒绝退款 |
| POST | `/api/trade/{account_id}/evaluations` | `{trade_ids, feedback, rate, image_urls, anonymous, confirm}` | 卖家评价（回评走同一个接口） |
| POST | `/api/trade/{account_id}/complaints/{complain_id}/refuse` | `{proof_desc, proof_urls, confirm}` | 投诉拒绝（举证反驳） |
| POST | `/api/trade/{account_id}/complaints/{complain_id}/proof` | `{proof_desc, proof_urls, kind, confirm}` | 投诉举证，`kind` 见 §3 |
| POST | `/api/trade/{account_id}/addresses` | `{contact_name, mobile_phone, province_name, city_name, district_name, detail_address, area_id?, default_addr?, confirm}` | 退货地址新增（见 §8） |
| POST | `/api/trade/{account_id}/addresses/{contact_id}/update` | 同上 | 退货地址编辑（见 §8） |
| POST | `/api/trade/{account_id}/addresses/{contact_id}/delete` | `{confirm}` | 退货地址删除，**不可撤销**（见 §8） |
| POST | `/api/trade/{account_id}/addresses/{contact_id}/default` | `{confirm}` | 设为默认退货地址（见 §8） |
| POST | `/api/trade/{account_id}/orders/{order_id}/modify-address/agree` | `{deliver_id, confirm}` | 同意买家改地址，**会真的改订单收货地址**（见 §9） |
| POST | `/api/trade/{account_id}/orders/{order_id}/modify-address/refuse` | `{deliver_id, confirm}` | 拒绝买家改地址（见 §9） |
| POST | `/api/trade/{account_id}/orders/{order_id}/close` | `{close_reason, confirm}` | 卖家关闭订单，**不可撤销**（见 §10） |
| POST | `/api/trade/{account_id}/orders/{order_id}/delay` | `{days, order_ids?, confirm}` | 延长收货时间，`days` ∈ 3/5/7/10（见 §10） |
| POST | `/api/trade/{account_id}/orders/{order_id}/remind` | `{order_ids?, remind_all?, confirm}` | 提醒买家确认收货（见 §10） |
| POST | `/api/trade/{account_id}/refunds/{refund_id}/postage-pay` | `{order_id, pay_type?, amount?, confirm}` | 支付运费退款，**这一步是真的付钱**（见 §10） |
| POST | `/api/trade/{account_id}/complaints/{complain_id}/revoke` | `{confirm}` | 撤销投诉申请（见 §10） |
| POST | `/api/trade/{account_id}/orders/{order_id}/memo` | `{memo, memo_group?, confirm}` | 订单备注，**不对买家展示**（见 §12） |
| **GET** | `/api/trade/{account_id}/refunds/{refund_id}/verify-token` | —（无 body，**无 `confirm`**） | 退款凭证（只读，见 §7） |
| **GET** | `/api/trade/{account_id}/orders/{order_id}/modify-address` | —（无 body，**无 `confirm`**） | 买家改地址申请（只读，见 §9） |
| **GET** | `/api/trade/{account_id}/orders/{order_id}/close-reasons` | —（无 body，**无 `confirm`**） | 关单可选原因（只读，见 §10） |
| POST | `/api/trade/{account_id}/orders/{order_id}/cancel` | `{close_reason, confirm}` | IM 侧取消订单（见 §13.1） |
| POST | `/api/trade/{account_id}/orders/{order_id}/close-im` | `{close_reason, confirm}` | IM 侧关闭订单，**另一个 mtop 名**（见 §13.1） |
| POST | `/api/trade/{account_id}/orders/{order_id}/delay-single` | `{days, confirm}` | 单订单延长收货，`days` ∈ 3/5/7/10（见 §13.1） |
| POST | `/api/trade/{account_id}/orders/{order_id}/adjust-price-sku` | `{skus, confirm}` | 按 SKU 改价，`skus` = `{skuKey: 元}`（见 §13.1） |
| POST | `/api/trade/{account_id}/smart-form/submit` | `{callback_code, ext, confirm}` | 智能表单提交，**会真的提交表单**（见 §13.1） |
| **GET** | `/api/trade/{account_id}/items/{item_id}/sku-selector` | —（无 body，**无 `confirm`**） | 商品 SKU 选择器（只读，见 §13.2） |
| **GET** | `/api/trade/{account_id}/orders/{order_id}/backfill-report` | —（无 body，**无 `confirm`**） | 验货/回填报告（只读，见 §13.2） |
| **GET** | `/api/trade/{account_id}/smart-form/query?params=` | —（`params` 走 query，**无 `confirm`**） | 智能表单查询（只读，见 §13.2） |

- body 用 `Dict[str, Any] = Body(default={})` 收，字段校验交给服务层 → 统一信封；
  用 FastAPI 的必填字段校验会先返回 422 的裸 body。
- 成功响应：`{"success": true, "data": {...}}`；失败：`{"success": false, "code", "message", ...}`。
- 每个成功/失败的写操作都写一条 `operation_logs`（`action` = 见下表的「action」列）。

## 2. 每个接口的 mtop 名、请求字段与证据

字段名与单位**逐字来自 bundle 的调用点**（偏移是该字符串在 `main.js` 中的字节偏移）。

### 2.1 发货 `ship_offline` — `mtop.taobao.idle.logistics.merchant.consign.offline` v1.0

| 字段 | 类型 | 来源与说明 |
| --- | --- | --- |
| `orderInfos` | **JSON 字符串** | `JSON.stringify([{tradeId}])`；bundle 偏移 **2624843** |
| `mailNo` | string | 运单号 |
| `cpCode` | string | 物流公司编码（bundle 里是字典里选的 code，不是中文名） |
| `addressId` | number | 发货地址 id；bundle 里过了一遍 `Number(...)`，本项目 `_int_or_none` |

响应读 `data.{totalNum, successNum}` → `total_num` / `success_num`（`failNum` 一并透出，缺则 `None`）。
`action = 'ship_offline'`。

### 2.2 无需物流发货 `ship_dummy` — `mtop.taobao.idle.logistics.merchant.consign.dummy` v1.0

`orderId` / `tradeText` / `picList`（**JSON 字符串**，图片 URL 数组）/ `newUnconsign`（官方固定 `true`）。
bundle 偏移 **3085774**。`action = 'ship_dummy'`。

### 2.3 改价 `adjust_price` — `mtop.taobao.idle.trade.merchant.user.adjust.price` v1.0

| 字段 | 类型 | 来源与说明 |
| --- | --- | --- |
| `orderId` | string | 订单号 |
| `modifyFee` | string | **分**：bundle 里是 `Math.round(100*价格).toString()`，偏移 **2530439** |
| `newTransportFee` | string | **分**，同上 |

- 服务层与 HTTP 层一律收**元**（`'39.90'`），`yuan_to_fen()` 是**全项目唯一**一处换算
  （`'39.90'` → `'3990'`，`0.01` → `'1'`）。
- 官方前端拒绝「商品价格为 0」→ 本项目 `modify_fee = 0` 直接 `invalid_fields`（不发平台）。
  运费可以为 0（包邮）。
- `action = 'adjust_price'`。

### 2.4 同意退款 `refund_agree` — `mtop.taobao.idle.merchant.refund.agree.refund` v1.0

`refundId` + `authToken`（偏移 **2800533** / **2803882**）。
`authToken` 由平台在退款详情/渲染接口里下发，**必须原样回传**；服务层不猜、不缓存，
缺了直接 `invalid_fields`。`action = 'refund_agree'`。

### 2.5 拒绝退款 `refund_refuse` — `mtop.taobao.idle.merchant.refund.refuse` v1.0

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `refundId` / `orderId` / `refuseReasonId` | string | 必填；`refuseReasonId` 来自**只读**接口 `mtop.taobao.idle.merchant.refund.refuse.render` 的 `refuseReasonList[].refuseReasonId`（本模块不调它） |
| `refuseProof` | **JSON 字符串** | `{desc, proofMultiMediaList:[{mediaTypeEnum:'IMAGE', url}]}`；**desc 与 urls 都为空就整个键不传**（官方是 `void 0`） |
| `negotiationApply` | **JSON 字符串** | `{negotiationRefundFee, idleNegotiationType}`；没有内容就不传 |

bundle 偏移 **2687681**。`action = 'refund_refuse'`。

### 2.6 卖家评价 `rate_create` — `mtop.taobao.idle.merchant.rate.create` v1.0

`tradeIdList`（数组）/ `imageUrls`（数组）/ `rate`（评价等级，**不需要等级时官方传 `-1`**，本项目默认 `-1`）
/ `feedback`（评价内容，必填）/ `anonymous`。bundle 偏移 **2789045**（调用点 **2790105**）。
响应读 `data.module.{success, successOrderIds, failOrderInfos}` →
`rate_success` / `success_order_ids` / `fail_order_infos`。`action = 'rate_create'`。

> **bundle 里卖家侧没有独立的「回复买家评价」接口** —— 回评就是再调一次 `rate.create`。

### 2.7 投诉拒绝 `complain_refuse` — `mtop.taobao.idle.cco.shop.complain.refuse` v1.0

`complainId` / `proofDesc` / `proofMediaList`（**JSON 字符串**，`[{type:'image', url}]`）。
bundle 偏移 **2827294**。`action = 'complain_refuse'`。

**业务级失败**：这个接口失败时顶层 `success` 仍是 `true`，失败在 `data.success === false` +
`data.failReason`（官方前端就是这么判的）。服务层把它翻成 `protocol_error`（502），
不碰登录态。有用例（`test_business_level_failure_is_not_reported_as_success`）。

### 2.8 投诉举证 `complain_proof` — 三个名字按 `kind` 分派

| `kind` | mtop 名 | bundle 偏移 |
| --- | --- | --- |
| `sellerSubmitProof`（默认） | `mtop.taobao.idle.cco.shop.complain.submit.proof` | 2832609 |
| `submitActiveProof` | `mtop.taobao.idle.cco.shop.complain.submit.active.proof` | 2832677 |
| `submitPassiveProof` | `mtop.taobao.idle.cco.shop.complain.submit.passive.proof` | 2832753 |

data 形状同 §2.7；`kind` 不在上表 → `invalid_fields`，不发平台。`action = 'complain_proof:<kind>'`。

## 3. 失败码（`common.FAIL_STATUS` + 本模块新增）

| code | HTTP | 触发 |
| --- | --- | --- |
| `confirmation_required` | 400 | 没带 `confirm=true`（新增码，故意不进 `FAIL_STATUS` 的已知表 → 落 400 默认档） |
| `invalid_fields` | 400 | 必填缺失 / 金额非数字或为负 / 改价 0 元 / 未知 `kind` |
| `verification_required` | 409 | 账号缺 Cookie、`login_state=need_relogin`、平台真的回 `SESSION_EXPIRED` |
| `not_found` | 404 | 账号不存在 |
| `protocol_error` | 502 | 平台失败信封（含无权限）原样透出，带 `platform_errors.{api, explain, message}` |

## 4. 未验证 / 已知风险（交给 Lead 决策）

1. **全部 8 个接口名与字段形状未经真机验证**（红线：写接口不许试打）。首次真机使用时，
   建议先用**只读**路径确认 `refuseReasonList`、`authToken`、`addressId` 的真实来源。
2. **部分写接口可能是服务端动态下发的**：bundle 里 UI 动作枚举有
   `agreeRefundGoodsApply` / `agreeRefundApply` / `sellerConfirm` 等（`main.js:2102100`），
   但对应 mtop 名不在 bundle；偏移 2800533 附近有通用分支
   `hO({api: b.apiName, v: b.apiVersion, data: b.params})`。**「同意退货退款」很可能走这条动态下发**，
   本次没有实现（无静态证据就不猜接口名）。
3. **`orderInfos` 的内部结构是推测**：bundle 只内联到 `JSON.stringify([{tradeId}])` 这一层，
   多单发货是否有别的字段（如数量、子单）未验证。
4. **`rate.create` 的 `rate=-1` 语义是推测**（bundle 注释外推：不需要等级）。
5. **只读层的写黑名单缺口已由 Lead 补上**：`backend/app/xianyu/trade.py` 的 `WRITE_APIS`
   现在收录了发货两个名字与投诉举证的 `submit.active.proof` / `submit.passive.proof`；
   `test_trade_write_api.py::WriteContractTest::test_write_apis_are_blacklisted_in_the_read_only_layer`
   断言「无缺口 + 全被拦」，缺口一回来测试就红。
6. **路由与 AI 工具已挂**：`backend/app/main.py` 挂了 `trade_write`（`/api/trade` 前缀），
   `backend/app/agents/tools.py` + `mcp_server/client.py` 有 8 个写工具与 1 个只读工具
   `trade_refund_verify_token`；五方对账 `reconcile_lead.py` 必须 `RECON OK`。

## 5. 测试

```
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_trade_write_api.py"
```

44 个用例（`backend/tests/test_trade_write_api.py`），覆盖：8 条路由的未确认闸门（且断言 0 次平台调用）、各接口 data 形状与版本、
元→分换算、空 proof/negotiation 不传键、业务级失败、平台失败信封透出 + 登录态不变、
缺字段/缺 Cookie/掉线/账号不存在、平台没返回的计数是 `None`；
外加 §7 退款凭证的 11 条（形状、没有闸门、空串→`None`、无权限是 502 且不改登录态、
无 Cookie 零平台请求、404/401、路由是 GET、只读接口不在写黑名单、解析器不编 token）。
**§8 退货地址另有 39 条**在 `backend/tests/test_address_write_api.py`：17 条纯协议函数
（四条 API 名逐字、写白名单与只读黑名单互斥、四种非法 `confirm` 全抛、可选键只在给了才出现、
`is_success` 五态、`contact_id_of` 不造 ID）、4 条未确认闸门（零平台调用）、7 条字段校验
（含「校验先于凭据检查」）、8 条 happy path（手机号去空格横线、`areaId` 只在给了才发、
无回显 → `no_contact_id`）、7 条平台失败（无权限 502 且不改登录态、掉线 409 并回写、
无 Cookie 零调用、404/401）、4 条路由 method 全 POST。
**全部用假协议层（`install_fake_protocol`），不打真实闲鱼平台。**

## 7. 退款凭证（**只读**）`refund_verify_token`

```
GET /api/trade/{account_id}/refunds/{refund_id}/verify-token
-> {verify_url, token, scene, refund_id, api, platform_success, platform_message}
```

**为什么要有它**：同意退款的 `auth_token` 不在退款列表帧里 —— 真机
`rightVO.btnList[].clickEvent.data.url` 是空串，用户在我们界面上无处可抄（写面板只能报
「还缺必填项：平台操作令牌 auth_token」）。官方前端（bundle 偏移 **2801524** 同意退款弹窗、
**2804850** 拒绝退款弹窗，参数逐字相同）在弹窗打开时先查一次：

```js
{api:'mtop.idle.alipay.verify.url.query', v:'1.0', data:{
  bizId: refundId, scene:'REFUND_PC',
  callBackUrl:'https://seller.goofish.com/?site=COMMONPRO#/seller-trade/refund-manage'}}
```

响应读 `data.module.verifyUrl`（官方塞进 150px 高 iframe 让用户完成支付宝身份验证）与
`data.module.token`（原样传给 `refund.agree.refund` 的 `authToken`）。该查询在官方**只在
`window.__PC_SELLER_WORKBENCH__.userInfo.isAdmin` 为真时**才发，非管理员态用空 token 提交。

**口径**：

- **只查不改 ⇒ GET、故意没有 `confirm`**（判据一句话：confirm 只钉有副作用的那一步）；
- 协议层登记在 `trade_write.READ_APIS`，**故意不进 `WRITE_APIS`**（否则会被只读层写黑名单
  与写闸门误伤），`verify_url_request()` 的签名里根本没有 `confirm` 参数；
- 平台回空串时 `token`/`verify_url` 一律 `null`（不拿 `''` 冒充可用凭证），
  **不生成、不缓存、不落库**；
- 平台拒绝 → `protocol_error`/502 原样透出，**不碰 `login_state`**（无权限 ≠ 掉线）；
- UI：`frontend/src/pages/Orders.tsx` 的「交易写操作 · 同意退款」面板里多一块「退款凭证」，
  按钮「获取凭证」把 token 填进 `auth_token`、把 `verifyUrl` 显示成「打开验证页」链接；
  平台没给 token 时明说「平台这次没有返回凭证（官方只在管理员态才下发）」；
- AI 侧：只读工具 `trade_refund_verify_token`（无 `confirm`），MCP 路由
  `GET /api/trade/{account_id}/refunds/{refund_id}/verify-token`。

**顺带取证的邻居（**未实现**，别当成已做）**：偏移 **2838769** 的 `compensate.service.pay`
（补偿服务费支付，`scene:'DEPOSIT_PC'`）**钱相关，明确不做**；买家改地址审批流
（`mtop.idle.merchant.order.address.modify.{agree,refuse}`）与发货地设置
（`...delivery.place.{detail.query,info.set}`）—— 都还没做。退货地址的写接口**已经做了**，见 §8。

**离线取证方式**：`C:\Users\Administrator\.dsh-tmp\verify_token_probe.py` 用 Playwright 把
退款列表与凭证接口换成桩，断言「给了 token → 框里就是它 + 链接可见」「回空串 → 不填框 +
显示提示」，两种场景都断言 `non_get=[]`（整轮一个非 GET 的 `/api/` 请求都没发）。
结果：`VERIFY TOKEN ALL OK (2/2)`，截图 `logs/verify_token/`。

## 8. 退货地址（**写**）`address_add` / `address_update` / `address_delete` / `address_set_default`

```
POST /api/trade/{account_id}/addresses                       -> {action, contact_id, api, warnings}
POST /api/trade/{account_id}/addresses/{contact_id}/update
POST /api/trade/{account_id}/addresses/{contact_id}/delete
POST /api/trade/{account_id}/addresses/{contact_id}/default
```

**四条 mtop（bundle 偏移，声明原文照抄）**：

| action | api | data |
| --- | --- | --- |
| `add`（偏移 3143703） | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.add` | `{contactName, mobilePhone, provinceName, cityName, districtName, detailAddress, areaId?, defaultAddr?}` |
| `update`（3143951） | `…address.update` | 同上 + `contactId` |
| `delete`（3144220） | `…address.delete` | `{contactId}` |
| `set_default`（3144501） | `…address.set.default` | `{contactId}` |

官方声明里带 `callbackOptions:{successText:'退货地址新增成功', errorText:'退货地址新增异常'}`，
`set.default` **只有 `errorText`**（成功靠重拉列表确认）—— 也就是说这四个接口官方**根本没读响应体**。

**口径**：

- **三步顺序**：确认 → 字段校验 → 建客户端（与 `services/item_templates.py` 同口径）。
  理由：确认排最前，否则用户会以为「填对了就能发」；字段校验纯本地免费，掉线时也该先说
  「收件人不能为空」而不是「去重新登录」。钉它的用例：
  `AddressFieldValidationTest::test_validation_runs_before_credential_check`。
- **`confirm` 只认 `True`/`'true'`/`1`**（服务层 `_truthy`）；HTTP 层把 body 的 `confirm`
  **原样**交服务层，**不做 `bool(...)`** —— `bool('false')` 是 True，会把用户明确的拒绝当同意。
- **成功判据**：客户端没报错 **且** 平台 `code == 'success'`；平台没给 `code` 时以客户端结论为准
  （这四个接口不读响应体，`set.default` 成功可能是空对象）。
- 拿不到 `contact_id` → `null` + warning `no_contact_id`（**不造**假 ID，让用户重拉列表）。
- **诚实偏离（区域码）**：官方表单还有 `division`（级联 `{label,value}` 数组）与隐藏 `areaId`
  （区级区域码）。我们没有码源（`mtop.taobao.idle.division.all.get` 对本机账号
  `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）⇒ **不伪造区域码、也不发形状不对的 `division`**：
  只发省 / 市 / 区三个**名字**，`area_id` 留空就**根本不发这个键**。平台若因此拒绝，
  错误原文原样显示，不会把失败说成成功。
- **只读侧字段**：`normalize_address` 认**两套键**（bundle 里只读页与写页读法不同）——
  只读页 `{addressId,name,phoneNum,address,defaultAddr}`，写页编辑弹窗
  `{contactId,contactName,mobilePhone,provinceName,cityName,districtName,detailAddress}`；
  后一套**平台给了才出现**（空值不出现），所以 UI 能预填就预填、不能就留空让用户重填，
  绝不自己拼地址串。钉它的用例：`backend/tests/test_trade_api.py::AddressNormalizeTest`（8 条）。
- **UI**：`frontend/src/pages/ReturnAddresses.tsx` 四个入口（新增 / 编辑 / 设为默认 / 删除），
  唯一写出口 `postAddress()` 里 `confirm: true` 是字面量，调用方无法覆盖；提交前
  `ConfirmDialog danger` 逐条列出最终 body。
- **AI 侧**：`trade_address_add` / `trade_address_update` / `trade_address_delete` /
  `trade_address_set_default`（四条都要求 `confirm`），MCP 路由与上表同路径、method 全 POST。

## 9. 买家改地址审批（**读 + 写**）`order_address_info` / `order_address_agree` / `order_address_refuse`

买家在订单上申请改收货地址，卖家要同意或拒绝。官方在订单页内做这个审批，我们此前只有订单列表。

### 9.1 只读：`GET /api/trade/{account_id}/orders/{order_id}/modify-address`

`mtop.idle.merchant.order.get.modify.address.info` v1.0（bundle 偏移 3101551）`{bizOrderId}`
→ `data.module = {modifyStatus, modifyAddressInfoVO, nowAddressInfoVO, modifyDeliverId}`。

```text
-> {order_id, status, deliver_id, can_operate, now_address, modify_address, decrypted,
    privacy_flagged, warnings:[{code,message}], platform_message, api, account_id, source, fetched_at}
```

- **只查不改 ⇒ GET、故意没有 `confirm`**（判据一句话：confirm 只钉有副作用的那一步），
  协议层登记在 `order_address.READ_APIS`，**不进 `WRITE_APIS`**（否则会被写闸门误伤）。
- `status` 用平台原文，拿不到是 `null`（**不编** `'NONE'` —— 官方组件初值才是 `'NONE'`）；
  **只有 `modifyStatus === 'WAITING'` 才认 `deliver_id`**（平台只在那一档给 `modifyDeliverId`）；
  `can_operate === (status === 'WAITING' && bool(deliver_id))`；两个 VO 原样透传，
  平台没给是 `null`（不是 `{}`）。
- **隐私号订单**：`nowAddressInfoVO.privacyFlag` 为真时平台给的是打码字段，官方会再调一次
  `mtop.idle.trade.order.detail.privacy.address`（bundle 偏移 2127351 的 `nP` 助手，
  **必须带 `valueType:'string'`**，`{bizOrderId, pageSource}`，改地址场景 `pageSource='modify_address'`）
  换明文 → `decrypted = {name, suffix, phone_number, address}`。
  **解不开就是 `null` + warning `privacy_decrypt_failed`**，`now_address` 仍是平台原样给的打码字段
  —— 不编明文、不把打码字段当明文。**解密失败不会把整个只读页判成失败**（用户至少还能看到字段）。
- 平台拒绝（如 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）→ 502 `protocol_error` 原文透出，
  **不碰登录态**（无权限 ≠ 掉线）。钉它的用例：`test_read_platform_refusal_is_502_and_does_not_mark_relogin`。

### 9.2 写：同意 / 拒绝

```text
POST /api/trade/{account_id}/orders/{order_id}/modify-address/agree   {deliver_id, confirm}
POST /api/trade/{account_id}/orders/{order_id}/modify-address/refuse  {deliver_id, confirm}
-> {order_id, deliver_id, action, api, platform_success, platform_message, platform_body, source, fetched_at}
```

- `mtop.idle.merchant.order.address.modify.agree`（偏移 3102802）/ `…modify.refuse`（偏移 3103173），
  参数逐字 `{bizOrderId, deliverId}`；官方 toast 原文「已同意修改地址」/「已拒绝修改地址」。
- **这是会真的改订单收货地址的写操作**（货可能寄到别处）⇒ `deliver_id` 必须来自同一条订单的
  只读查询；`confirm` 只认 `True`/`'true'`/`1`（`_truthy`），HTTP 层把 body 的 `confirm`
  **原样**交服务层，不做 `bool(...)`。
- 三步顺序 **确认 → 字段校验 → 建客户端**；协议层是最后一道：空订单号 / 空 `deliverId`
  在构造请求时就抛 `ValueError`，**不发出去**。
- 平台拒绝 → 502 原文透出且**不碰登录态**；`SessionExpiredError` 才 409 + 回写 `need_relogin`。

### 9.3 测试与未验证

`backend/tests/test_order_address_api.py` **41 条**（协议 17 + 只读 10 + 写闸门 12 + 路由 2）：
未确认零平台请求（`'false'`/`0`/`''`/`None` 都不算点头）、字段校验先于凭据检查、隐私解密
`value_type='string'` 真发出、解密失败降级成 warning、不编明文/不补 0、读接口是 GET。

**一次真机写请求都没发过**：改地址审批要买家先发起申请，不能为了验证去改用户的真实订单。
UI 侧 `frontend/src/pages/Orders.tsx::ModifyAddressModal`（同意/拒绝三重闸 + 二次确认）；
AI 侧 `trade_order_address_request`（读）/ `trade_order_address_agree` / `trade_order_address_refuse`（写）。

## 10. 订单处理（**读 + 写**）：关单 / 延时 / 提醒 / 运费退款 / 撤销投诉

第 38 批新增。证据全部来自本地 bundle 副本（**零平台请求**）：
IM 侧 `C:\Users\Administrator\.dsh-tmp\bundle_scan\js_006_p_im_index.js`（3,071,011 字符，偏移 2586009~2586275）、
卖家工作台 `C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`（3,879,584 字符，偏移见下）。

### 10.1 只读：关单原因 `GET /api/trade/{account_id}/orders/{order_id}/close-reasons`

| 项 | 值 |
| --- | --- |
| mtop | `mtop.taobao.idle.trade.order.close.reason.get` v1.0 |
| 请求 | `{bizOrderId}`（偏移 2586009，包装 `cG`） |
| 响应 | 官方消费点（偏移 2598721）读 **`data.closeReasons`**，是**字符串数组**，`map(r => ({label: r, value: r}))` |
| 我们的返回 | `{order_id, reasons, raw_keys, warnings, api, account_id, source, fetched_at}` |

- **`reasons === null` 与 `reasons === []` 是两件事**：前者是「平台没返回列表键」（可以手填），
  后者是「平台明确回了一个空数组」（这个订单现在没有可选原因）。钉它的用例在
  `backend/tests/test_trade_write_api.py::CloseAndDelayTest`。
- **真机已验**（2026-10-13，打包版 `--port 18400` + 真账号 `2217970177348`，只发 GET）：
  `GET /api/trade/2217970177348/orders/3317095524016013282/close-reasons` → **200**、
  `close_reasons: ["不想卖了","宝贝已出售","买家联系不上","与买家协商一致","其他原因"]`、
  `raw_keys: [bizOrderId, closeReasons, needDecryptKeys, needDecryptKeysV2, serverDecryptKeys, serverTime]`、
  `warnings: []`、`source: platform` ⇒ 接口名、参数形状与「字符串数组」的解析都被平台证实。
- 只读、**故意没有 `confirm`**，登记在 `trade_write.READ_APIS`，不进 `WRITE_APIS`（免得被写闸门误伤）。

### 10.2 五条写接口

| 动作 | mtop | 版本 | 请求字段（逐字） | 偏移 |
| --- | --- | --- | --- | --- |
| `close` | `mtop.taobao.idle.trade.merchant.close.by.seller` | **2.0** | `{tid, bizOrderId, closeReason}` | 2762506 |
| `delay` | `mtop.taobao.idle.trade.merchant.batch.delay.confirm` | 1.0 | `{orderIdList, delayTime}` | 2758810 |
| `remind` | `mtop.taobao.idle.trade.merchant.batch.remind.confirm` | 1.0 | `{orderIdList, remindAllOrder}` | 2023848 |
| `postage_pay` | `mtop.taobao.idle.merchant.postage.refund.pay` | 1.0 | `{orderId, refundId, payType}`（+ `ttid`） | 2835556 |
| `complain_revoke` | `mtop.taobao.idle.cco.shop.complain.apply.revoke` | 1.0 | `{complainId}` | 2814089 |

> **名字来源**：这五条**逐字取自卖家工作台 bundle**（`C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`，
> 3,879,584 字符），不是 IM bundle 里那套同族名字 —— IM 侧另有
> `mtop.taobao.idle.trade.close.by.seller` v2.0（偏移 2585829）、`…trade.seller.delay.confirm` v1.0（偏移 2586275）、
> `…trade.consign.remind`，它们的 payload 形状与这里不同（单订单、字段名不同），**我们没用**。
> 选卖家工作台那一套的理由：登录态与 Cookie 就是那个站点的，且 `{orderIdList, delayTime}` 这种批量形状
> 与我们页面的「勾几单一起做」一致。

- **关单把同一个订单号同时塞进 `tid` 与 `bizOrderId`**（官方原文 `{tid: e, bizOrderId: e, closeReason: t}`）——
  这不是笔误，我们照做（只发 `bizOrderId` 平台不认）。官方文案：「确认关闭后，请确保已和买家协商达成一致，
  否则您将面临投诉或赔偿」；未选原因时官方提示「请选择订单取消原因~」。
- `delayTime` 是**秒**：官方 `60 * days * 60 * 24`，`days` 只给四档 **3 / 5 / 7 / 10**。
  批量回执读 `data.module.{totalNum, successNum, failOrderInfos[{orderId, errorMsg}]}`，缺 → `null`（**不补 0**）。
- `postage_pay` 的 `payType` ∈ `alipay` / `deposit`；官方还带 `ttid:"1@tbwang_mac_1.0.0#pc"` 与 `needLoginPC:!0`。
  **这一步是真的付钱**（从支付宝余额或保证金里扣给买家）⇒ 确认框必须写明，`amount` 只用于展示、**不进请求体**。
- `order_ids` 收数组或逗号串（去重保序、上限 50）；**留空就不发这个键**，后端用路径里那一个订单号。
- 五条都在服务层走 `_confirm_gate` → 字段校验（协议层构造）→ `_client`；成功/失败都写 `operation_logs`
  （`trade_close_order` / `trade_delay_confirm` / `trade_remind_confirm` / `trade_postage_refund_pay` /
  `trade_complain_revoke`），成功才 `publish('trade.changed')`。
- **本批顺手修掉一个真缺陷**：`backend/app/api/trade_write.py`（8 处）与
  `backend/app/api/security_write.py`（3 处）曾把 `confirm` 写成 `bool(body.get('confirm'))`，
  而 `bool('false')` 是 `True` ⇒ 用户传 `"false"` 反而**打开了写闸门**。真机探针
  （`C:\Users\Administrator\.dsh-tmp\confirm_false_probe.py`，用不存在的账号保证零平台写请求）
  实测 `trade.ship_dummy` + `{"confirm": "false"}` 返回 **404 `not_found`**（闸门被绕过、直接走到建客户端），
  正确应为 400 `confirmation_required`。现已全部改成原样透传；服务层同理不许裸 `if not confirm:`
  （`backend/app/services/{trade_write,security_write}.py` 共 10 处也一并改成 `_truthy(confirm)`）。
  钉它的用例：`backend/tests/test_trade_write_api.py::ConfirmGateBypassTest`（**遍历本模块全部 19 条 POST 路由**
  × 10 个假确认值 `('false','FALSE',' false ','no','yes','0',0,'',None,2)`，全部要求 400 且零平台调用）。

### 10.3 测试与未验证

`backend/tests/test_trade_write_api.py` **118 条**（第 38 批 +30：`ConfirmGateBypassTest` + `CloseAndDelayTest`）；
`backend/tests/test_security_write_api.py` **22 条**（+1 条假确认值遍历）。

**未验证**：这五条写接口**一次真机请求都没发过**（关单/延时/提醒/付运费/撤诉都会真的改动真实订单或真的扣钱，
不能为了验证去动用户的订单）。只验到：路由注册（`openapi` 178 路径 / 192 操作）、body 被端点接受、
`confirm` 闸门拦得住假确认值、界面上不点最终确认就零写请求、`close-reasons` 只读接口的真机响应形状。

## 11. 赔付 / 维权（**读 + 写**）：拒绝赔付

### 11.1 只读：详情与拒绝原因候选

| 用途 | HTTP | 平台接口 | 版本 |
| --- | --- | --- | --- |
| 赔付详情 | `GET /api/trade/{account_id}/orders/{order_id}/compensations/{service_code}` | `mtop.taobao.idle.merchant.compensate.service.detail.query` | v1.0（**带 `valueType=string`**） |
| 拒绝原因候选 | `GET /api/trade/{account_id}/compensations/{service_code}/reasons` | `mtop.taobao.idle.merchant.compensate.service.refuse.render` | v1.0 |

详情响应 `data.{account_id, order_id, service_code, found, service_info, elements, amount, amount_yuan,
title, desc, warnings, fetched_at, source}`；`found=false` 表示**平台没给出这笔赔付**（不是「没有问题」）。
**金额单位是分**：`amount` 原样透出，`amount_yuan` 是换算后的元，两个都可能为 `null`（**不补 0**）。

原因响应 `data.{account_id, service_code, reason_choices, reason_desc, warnings, fetched_at, source}`；
`reason_choices` 三态：有值 / `[]`（平台明确回空数组）/ `null`（平台没返回那个键，**不是「没有原因」**）。

### 11.2 写：拒绝赔付

`POST /api/trade/{account_id}/orders/{order_id}/compensations/{service_code}/refuse`，平台接口
`mtop.taobao.idle.merchant.compensate.service.refuse` v1.0。请求体：

```json
{
  "reason_choices": ["商品无质量问题"],
  "reason_desc": "说明（≤200 字，可空）",
  "proof_urls": ["https://.../1.png"],
  "is_manual": false,
  "confirm": true
}
```

后端把它拼成官方形状：`{orderId, serviceCode, sellerRefuseReason: "{\"reasonChoices\":[...],\"reasonDesc\":\"...\"}",
refuseProofs: "[\"https://.../1.png\"]", isManual}`（两个 JSON 串都用紧凑分隔符）。

规则：
- `confirm` 必须**字面量 `true`**（三层顺序仍是 确认 → 字段校验 → 建客户端）；
- `reason_choices` **至少一个**（候选只能来自 `reasons` 接口，我们不编）；数组与逗号串都收；
- `proof_urls` **至少一张**（官方硬要求「请上传凭证」）；
- `reason_desc` 超过 200 字 → 400 `invalid_fields`；
- 平台 `data.success === false` → 502 `protocol_error` 并把 `platform_message` 透出（不谎报成功）。

响应 `data.{action: 'compensate_refuse', saved, order_id, service_code, submitted, warnings, fetched_at}`；
成功记一条 `operation_logs`（action `trade_compensate_refuse`），不落库、不发 `items.*` 事件。

### 11.3 测试与未验证

`backend/tests/test_trade_write_api.py` 新增 45 条（协议形状 / 只读三态 / 确认闸门遍历假确认值 /
字段校验早于凭据检查 / 写成功与四类失败），全文件 **118 条**。

**未验证**：这条写接口**一次真机请求都没发过** —— 拒绝赔付会真的改动平台上的维权单据，不能为了验证去动用户的单子。
只验到：路由注册、body 被端点接受、`confirm` 闸门拦得住假确认值、界面不点最终确认就零写请求。
另有两条只读接口的真机响应形状也**没有**样例（本机账号对赔付族接口可能无权限）。

## 12. 订单备注（**写**）`add_memo` — `mtop.taobao.idle.merchant.add.memo` v1.0

`POST /api/trade/{account_id}/orders/{order_id}/memo`，body `{memo, memo_group?, confirm}`。

| 字段 | 类型 | 来源与说明 |
| --- | --- | --- |
| `memo` | string | 备注正文，官方 `maxLength={50}`；**不对买家展示** |
| `memo_group` | int 1~5 | 官方标记位（平台上五面小旗，**没有文字名**），默认 1 |
| `confirm` | bool | 必须**字面量 `true`**，否则 400 `confirmation_required` 且零平台请求 |

- 官方五条校验逐条照抄（空 / 换行 / emoji / 白名单外符号 / 超 50 字），文案就是平台原文；
  服务层在建客户端**之前**校验完（纯本地、免费）。
- 成功 `data = {action: 'trade_add_memo', saved, memo, memo_group, api, warnings}`；
  平台拒绝（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 等）走 502 `protocol_error`，**不动 `login_state`**。
- 读回：`normalize_order` 透出 `memo` / `memo_group`（`commonData` 与 `merchantCommonData` 两处都认），
  平台没给是 `null`（不是空串）。
- 测试：`backend/tests/test_trade_write_api.py::MemoTest`（协议形状 / 五条校验 / 闸门 / 审计 / 路由只 POST）
  与 `backend/tests/test_trade_api.py::OrderMemoNormalizeTest`（读回四态）。
- 未验证：真机未执行（会真的写平台备注），读回也没有真机样本。

### 11. 改价渲染（**只读**，第五十批）

- `GET /api/trade/{account_id}/orders/{order_id}/adjust-price/render` —— 无 `confirm`。
- 平台：`mtop.taobao.idle.trade.merchant.adjust.price.render` v1.0，`data = {orderId}`，读 `data.module`。
- 「改后商品价格」初值按官方口径 **订单总价 − 运费** 计算，并如实标注 `item_price_derived=true`；
  缺总价或运费 ⇒ `null` + warning，**不补 0**。
- 平台没给 module ⇒ 200 + `found:false` + warning（不是 502）。
- 无权限等平台拒绝 ⇒ 502 `protocol_error`，**不写 `need_relogin`**。

## 13. 第六十批：IM 侧订单处理 + SKU 选择器 / 验货报告 / 智能表单（5 写 + 3 只读）

> **证据来源（两条，都是静态提取，零平台请求）**：
> 1. 卖家工作台交易微应用 bundle
>    `https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3,879,584 B），
>    本地副本 `C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`；
> 2. **IM 聊天页 bundle** `js_008_vendors.js`（5,857,382 B），
>    本地副本 `C:\Users\Administrator\.dsh-tmp\seller_scan\js_008_vendors.js` ——
>    本批 4 条写接口里有 4 条**只**出现在这一份里（工作台 bundle 里 `NOT FOUND`）。
>
> **⚠️ 本批 8 条接口全部未经真机验证**：5 条写接口一律禁止对真实平台试打（会真的取消订单 /
> 真的提交表单）；3 条只读接口见 §13.5 的真机清单（Lead 会拿真实 itemId/orderId 打）。
>
> 偏移都是**字符偏移**（两份 bundle 都是纯 ASCII，字符偏移 = 字节偏移），
> 用 `grep -bo '<接口名>'` 可复现；下面每条都给了「可复现的搜索串」。

### 13.1 五条写接口

| 路由 | 服务层函数 | 平台接口 | 版本 | 请求载荷 | 幂等 action | 证据（偏移 + 搜索串） |
| --- | --- | --- | --- | --- | --- | --- |
| `POST /api/trade/{account_id}/orders/{order_id}/cancel` | `services.trade_write.order_cancel` | `mtop.taobao.idle.trade.order.cancel` | **2.0** | `{tid, bizOrderId, closeReason}`（`tid` = `bizOrderId` = 订单号） | `trade_order_cancel` | `js_008_vendors.js` **1832140**，串 `mtop.taobao.idle.trade.order.cancel` |
| `POST /api/trade/{account_id}/orders/{order_id}/close-im` | `services.trade_write.close_by_seller_im` | `mtop.taobao.idle.trade.close.by.seller` | **2.0** | 同上（逐字同形状） | `trade_close_by_seller_im` | `js_008_vendors.js` **1831960**，串 `mtop.taobao.idle.trade.close.by.seller` |
| `POST /api/trade/{account_id}/orders/{order_id}/delay-single` | `services.trade_write.seller_delay_confirm` | `mtop.taobao.idle.trade.seller.delay.confirm` | 1.0 | `{orderId, delayTime: str(days*86400)}` | `trade_seller_delay_confirm` | `js_008_vendors.js` **1832406**（定义）/ **1854657**（调用 `g6({delayTime:String(86400*Number(e9)),orderId:eI})`） |
| `POST /api/trade/{account_id}/orders/{order_id}/adjust-price-sku` | `services.trade_write.user_adjust_price` | `mtop.taobao.idle.trade.user.adjust.price` | 1.0 | `{orderId, <skuKey>: '<分>'}`（**每个 SKU 一个键**） | `trade_user_adjust_price` | `js_008_vendors.js` **1831813**（定义）/ **1854181**（调用，价格 `yj(price).mul(100).toString()`） |
| `POST /api/trade/{account_id}/smart-form/submit` | `services.trade_write.smart_form_callback` | `mtop.taobao.merchant.heracles.inno.smart.form.callback` | 1.0 | `{callbackCode, extJson}`（`extJson` 是 JSON 串，里面的 `formData` **又是**一个 JSON 串 ⇒ 双层） | `trade_smart_form_callback` | `main.js` **3081308** 与 `js_008_vendors.js` **802676**（两处逐字相同） |

共同规则（与 §0 三条红线一致）：

- **没 `confirm` 一个字都不发**：端点 body 里的 `confirm` 必须**字面量 `true`**，否则 400
  `confirmation_required` 且零平台请求；协议层构造器再拦一次（`WriteNotConfirmed`）。
  5 条都带 `confirm` 形参 ⇒ `backend/tests/test_write_routes_have_gates.py` 自然认可，**没有改白名单**。
- **幂等**：5 条都走 `_submit` ⇒ 带 `Idempotency-Key` 时同键同载荷只打平台一次、回放带
  `idempotent_replay: true`；同键不同载荷 → 409 `conflict`。
- 字段校验在**建客户端之前**（免费）：订单号空 → 400 `invalid_fields`；
  `days` 不在 `(3,5,7,10)` → 400；`skus` 键名空或叫 `orderId` → 400；`close_reason` 空 → 400。

`order.cancel` / `close.by.seller` **不是**已有 `mtop.taobao.idle.trade.merchant.close.by.seller`（§10）的别名：
三个接口名在 bundle 里都是**独立字符串**，工作台 bundle 里根本没有前两个名字。
同一 UI 里两者按一个开关二选一：`js_008_vendors.js` **1853279/1853504** 原文
`eF?g5({tid:eI,closeReason:eG,bizOrderId:eI}).then(...):g4({tid:eI,...})`（`g5` = `close.by.seller`，`g4` = `order.cancel`）。
`eF` 在 **1849855** 被赋值为 `(0,e$.Fn)(eP)`（`eP` = `commonData.seller`）—— **`eF` 的具体语义没定论**（见 §13.6）。

### 13.2 三条只读接口

| 路由 | 服务层函数 | 平台接口 | 版本 | 请求载荷 | 响应读法 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /api/trade/{account_id}/items/{item_id}/sku-selector` | `services.trade_write.sku_selector` | `mtop.taobao.idle.trade.common.sku.selector` | 1.0 | `{itemId}` | `data.{components,skuInfo}`；`components` 里按 `render` 归到 `skuItemInfo` / `specificationInfo` / `quantityInfo`（各取 `.data`） | `js_008_vendors.js` **1841759** |
| `GET /api/trade/{account_id}/orders/{order_id}/backfill-report` | `services.trade_write.backfill_report` | `mtop.alibaba.idle.seller.platform.order.backfill.report` | 1.0 | `{orderId}` | 官方只在 `data.code === 'success'` 时取 `data.data` | `main.js` **2608355** |
| `GET /api/trade/{account_id}/smart-form/query?params=` | `services.trade_write.smart_form_query` | `mtop.taobao.merchant.heracles.inno.smart.form.query` | 1.0 | 页面 URL 的查询参数对象（官方 `cO(t.url)`），**原样透传** | `data.result.formSchema.schemaData`（**JSON 字符串**，解一层）+ `data.result.formData` | `main.js` **3077683** / `js_008_vendors.js` **798950** |

三条共同口径：

- **总是**返回 `warnings`（没警告就是 `[]`），与 §11.1 / §10 的只读接口一致。
- 平台拒绝（`NO_PERMISSION` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）⇒ 502 `protocol_error`，
  **不动 `login_state`**（无权限 ≠ 掉线）。
- 平台没给的东西一律 `null` + warning，**不补 0、不编空对象**：
  `sku_selector_missing`（没 `components`）/ `backfill_report_unavailable`（`data.code` 不是 `success`，消息带原文 code）/
  `backfill_report_empty`（`code=success` 但没 `data`）/ `smart_form_schema_missing`（没 `formSchema`）/
  `smart_form_no_receipt`（提交顶层成功但业务体没给 `result.success`）。
- `smart_form_query` 的 `params` 走 query：**值里的 `=`/`&` 必须 URL 转义**
  （`?params=formId%3D9%26bizType%3Dorder`），否则 FastAPI 只拿到 `params=formId=9`。
  之所以用 GET 而不是 POST：非 GET 会被写路由闸门测试当成写路由要求 `confirm`，
  而这条**是只读**，不该为了迁就测试去加假闸门。
- 智能表单提交的**成功判据是业务体的 `data.result.success`**（官方前端就是读它），
  **不是**信封顶层 `success`；业务级失败原样透出 `form_message`（官方 toast「填写成功」同理）。

### 13.3 服务层实现口径

- 写类：全部复用 §0.1 的唯一漏斗 `services/trade_write.py::_submit`
  （`_client` → 字段校验 → `common.idempotent_call` → `_call_api` → 解析 → `common.respond`），
  所以 confirm 闸门、幂等、`operation_logs` 审计、平台失败透出**自动**生效。
- 只读类：走新增的 `_read_api(account_id, api, data, version, parse)` 骨架
  （`_client` → `common.call_method(client, ('mtop',), api, data, v=version)` → 解析 → 打戳）。
- 协议层：`backend/app/xianyu/trade_write.py`，模块级纯函数 + client 作为入参，
  **没有**往 `backend/app/xianyu/client.py` 加方法。

### 13.4 测试

```
XIANYU_APP_DIR=$TEMP/xywb_tw_cov PYTHONPATH=. PYTHONIOENCODING=utf-8 \
  /c/Python310/python.exe backend/tests/test_trade_write_api.py            # Ran 210 tests OK
XIANYU_APP_DIR=$TEMP/xywb_tw_cov PYTHONPATH=. PYTHONIOENCODING=utf-8 \
  /c/Python310/python.exe backend/tests/test_xianyu_trade_write.py         # Ran 11 tests OK
```

- `backend/tests/test_trade_write_api.py`：本批新增 4 个类 —— `ImOrderWriteTest`（协议形状 / 路由 /
  幂等同键只打一次 / 审计 detail）、`SkuSelectorTest`、`BackfillReportTest`、`SmartFormTest`，
  并给 `WriteConfirmGateTest` 补了 5 条「无 confirm 必须 400 且零平台请求」。
- `backend/tests/test_xianyu_trade_write.py`（本批新建）：**协议层真实往返**（`httpx.MockTransport` 假 mtop
  服务端）—— 请求真发出去、`api`/`v`/`data` 逐字断言、解析器喂真实信封；
  另锁一条「`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 从 `client.mtop` 抛 `XianyuError`（`ret` 原文），
  服务层认成 `protocol_error` 而不是掉线」。

### 13.5 真机可验证清单（Lead 打真机用）

**能打（只读，零副作用）**：

| 方法 | 路径 | 最小参数 | 预期 |
| --- | --- | --- | --- |
| GET | `/api/trade/{account_id}/items/{item_id}/sku-selector` | 真实 `item_id` | 200；有 `components` ⇒ `found:true`；无权限 ⇒ 502 `protocol_error` |
| GET | `/api/trade/{account_id}/orders/{order_id}/backfill-report` | 真实 `order_id` | 200（`report_ok` 真/假都算正常回话）；无权限 ⇒ 502 |
| GET | `/api/trade/{account_id}/smart-form/query?params=formId%3D9%26bizType%3Dorder` | 任意 `params` | 200；`schema` 可能 `null` + `smart_form_schema_missing` |

**负例探针（只读，零副作用）**：`item_id` / `order_id` 用**不存在的号** ⇒ 期望 200 + 空体（或平台
业务 code），**不是** 500；这一条只验「不猜数据、不 500」。

**不能打（会产生真实副作用，一律禁止）**：

- `orders/{order_id}/cancel` —— 真的取消订单；
- `orders/{order_id}/close-im` —— 真的关闭订单；
- `orders/{order_id}/delay-single` —— 真的延长收货；
- `orders/{order_id}/adjust-price-sku` —— 真的改价（会通知买家）；
- `smart-form/submit` —— 真的提交表单（可能触发平台工单）。

### 13.6 未做 / 不确定项（如实记）

1. **`eF` 开关语义没定论**：`order.cancel` 与 `close.by.seller` 在同一 UI 里按 `eF` 二选一，
   但 `eF` 具体判什么（卖家身份？订单来源？IM 场景？）**没查清**。本批按「两个接口名都真实存在、
   各给一条路由」处理，**不替平台决定什么时候用哪个** —— 选择权留给调用方。
2. **`close_reason` 的合法取值**只能来自 §10 的 `close-reasons` 接口；本批不做本地白名单，
   原样透传（编一份原因列表就是猜数据）。
3. **`user.adjust.price` 的 SKU 键名**由调用方原样给：官方调用点是「遍历页面上的 SKU 表」拼键，
   键名含义（skuId？规格串？）**没有从 bundle 里确认到**，所以后端不校验、不转换键名，只转金额单位。
4. **金额单位口径**（`_sku_price_fen`）：整数（含 `'2'`）按**分**原样用，非整数（`'12.50'` / `1.5`）按**元** ×100。
   官方调用点先 `.mul(100)` 再 `toString()` ⇒ 平台侧收的是**分的字符串**；但「调用方给的是元还是分」
   只能靠小数位判断，这是**启发式**，写进契约以免误用。
5. **`extJson` 双层字符串**照官方原样发（外层 `extJson` 是串，里面的 `formData` 也是串）；
   我们只保证「给 dict 就序列化、给串就校验是合法 JSON 后原样发」，**不重组**官方字段。
6. **`backfill.report` 的 `data.code` 除了 `'success'` 还有哪些值**没样本；本批把非 `success`
   一律当「不可用」+ warning 透出原文 code，不猜含义。
7. **智能表单 `params` 的字段名**全部透传，不做白名单 —— 官方就是拿页面 URL 的 query 直接发。
- AI 侧只读工具 `trade_adjust_price_render`。
