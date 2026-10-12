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
   协议层的请求构造器还会再拦一次（`WriteNotConfirmed`），两道闸门。
2. **平台没返回的就是 `null`，不许补 0**：发货计数平台只回 `successNum` 时，
   `total_num` / `fail_num` 必须是 `None`（有用例锁死）。
3. **无权限 ≠ 掉线**：`NO_PERMISSION::用户没有权限` /
   `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` 一律走 `protocol_error`（HTTP 502）并原样透出
   `platform_errors.explain`，**不会**把账号写成 `need_relogin`。只有平台真的回
   `SESSION_EXPIRED`（`common._is_session_expired` 按类名/文案认）才会标掉线。

## 1. HTTP（12 个 POST + 1 个只读 GET，全部要求登录）

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
| **GET** | `/api/trade/{account_id}/refunds/{refund_id}/verify-token` | —（无 body，**无 `confirm`**） | 退款凭证（只读，见 §7） |

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

