# 交易组（订单 / 退款 / 评价 / 投诉 / 退货地址）只读契约

> 本文件是 `backend/app/xianyu/trade.py` / `backend/app/services/trade.py` /
> `backend/app/api/trade.py` 的对外契约，**只覆盖只读能力**。
>
> 证据来源（2026-10-10，账号 `2217970177348`，全部可复核）：
> - **真机只读探针**：`logs/seller_discover/trade_probe.json`（6 次直连 mtop）、
>   `trade_probe2.json`（refund 重试 + rate 带 queryType）、`trade_keys.txt`（键名摘要）。
>   探针脚本 `C:\Users\Administrator\.dsh-tmp\trade_probe.py`：2.5s 间隔、命中
>   `FAIL_SYS_USER_VALIDATE` / `RGV587_ERROR` / `被挤爆` / `x5sec` 立即中止。
> - **官方交易微应用 bundle**（静态 CDN，零风控）：
>   `https://g.alicdn.com/idle-pc/idle-seller-trade/0.0.26/js/main.js`（3,879,584 字节），
>   扫描产物 `C:\Users\Administrator\.dsh-tmp\trade_scan.out`、`trade_focus*.out`。
> - **契约测试**：`backend/tests/test_trade_api.py`（26 用例，样本即真机原文）。

## 0. 一句话结论

五个页面里 **三个能读**（订单 / 退款 / 评价），**两个平台不给这个账号权限**
（投诉 / 退货地址）——都返回真机原文的失败信封，**不是**本项目的 bug：

| 能力 | 真机结果 | 结论 |
| --- | --- | --- |
| 订单列表 + 页签计数 | `SUCCESS` | 可读 |
| 退款列表 | `SUCCESS` | 可读（容器是双层 `data.data`） |
| 评价列表 | `SUCCESS` | 可读（**本项目账号目前 0 条评价**，`rateItemVOList: []`） |
| 投诉列表 | `NO_PERMISSION::用户没有权限` | **无权限**，原样报 502 |
| 退货地址 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | **无权限**，原样报 502 |

两个无权限接口用的是**卖家工作台（商家身份）**的网关能力，本项目当前 Cookie 是买家侧
`www.goofish.com` 会话，因此平台在鉴权层直接拒绝。要打开需要另取卖家工作台会话（新功能，
不在本次范围）。**不要**把它当成掉线：这类 ret 不写 `need_relogin`，用户不会被逼着重新扫码。

## 1. 已实现（只读）

### 1.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/trade/{account_id}/orders` | 订单列表。Query：`page`（默认 1）、`size`（默认 10，限 1–50）、`include_counts`（默认 false，为真时**额外**调一次 `order.count` 拿页签计数） |
| GET | `/api/trade/{account_id}/refunds` | 退款列表。Query：`page`、`size` |
| GET | `/api/trade/{account_id}/evaluations` | 评价列表（按订单分组）。Query：`page`、`size` |
| GET | `/api/trade/{account_id}/complaints` | 投诉列表。Query：`page`、`size` |
| GET | `/api/trade/{account_id}/addresses` | 退货地址列表。Query：`page`、`size` |

- 五个端点都要求登录（`Depends(current_user)`），响应走全局信封
  `{success: true, data: {...}}` / `{success: false, message, code, platform_errors?}`。
- 状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、
  `verification_required`→409、`protocol_error`→502。
- **没有任何写方法**：POST/PUT/PATCH/DELETE 一律 405（`TradeReadOnlyTest` 锁死）。
- 分页参数在 API 层收成 `str` 再交给服务层校验，**不会**返回 FastAPI 的 422 裸 body；
  非法值一律 `400 invalid_fields`（`page=0` / `size=0` / `size=51` / `page=abc` / `size=x`），
  且**一次平台都不打**。
- ⚠️ **挂载**：`backend/app/main.py:35 ROUTE_MODULES` 目前还没有 `'trade'`，
  所以真实 app 里这五个路由**尚未生效**（Lead 加一行即可，本模块自带的测试自挂 router，
  不受影响）。前端 `frontend/src/pages/{Orders,Refunds,Evaluations,Complaints,ReturnAddresses}.tsx`
  已经调用这些路径。

### 1.2 每个响应都带

`account_id`、`page`、`page_size`、`api`（真实 mtop 名，便于对账）、`source: "platform"`、
`fetched_at`（本地时间字符串）。列表接口另有 `paging`：

```json
{"total_count": 15, "next_page": 2, "last_end_row": 20, "server_time": 1791605956329}
```

**分页键在信封顶层**（`data` 的兄弟键，真机 refund.list / rate.list 实测）；拿不到就是 `null`，
**不补 0**。

## 2. 字段来源路径（每个字段都能指回平台的键）

> 约定：`resp` = mtop 响应信封；`data` = `resp.data`。所有缺失字段一律 `null`。
> **金额原样透传字符串**（真机给的是 `"39.90"`，不是分、不是浮点）——本模块不做任何单位换算。

### 2.1 订单 `orders[]` ← `mtop.taobao.idle.trade.merchant.sold.get` v1.0 `data.module.items[]`

| 输出字段 | 平台路径 |
| --- | --- |
| `order_id` / `item_id` | `commonData.orderId` / `commonData.itemId`（真机是 int，输出转字符串） |
| `order_status` | `commonData.orderStatus`（原文中文，如 `"交易关闭"`） |
| `buyer.user_id` | `buyerInfoVO.buyerId` |
| `buyer.nick` / `name` / `phone` / `address` / `avatar` / `is_privacy` | `buyerInfoVO.userNick` / `.name` / `.phone` / `.address` / `.userIcon` / `.isPrivacy` |
| `item.title` / `item.pic_url` / `item.info_lines` | `itemVO.title` / `.itemPicUrl` / `.itemInfoLines[]`（`{key, value}`） |
| `price.*` | `priceVO.{auctionPrice, buyNum, postFee, discountFee, confirmFee, refundFee, totalPrice}` |
| `times.create_time` / `finish_time` | `commonData.createTime` / `.finishTime` |
| `in_refund` / `seller_rate_status` / `buyer_apply_modify_address` | `commonData.inRefund` / `.sellerRateStatus` / `.buyerApplyModifyAddress` |
| `consign_tips[]` | `commonData.consignTipsText[].text` |
| `columns[]` | `columnVOList[].{name, num, contentVOList[].key.text}` |
| `buttons[]` | `rightVO.btnList[].{name, clickEvent.data.code, buttonDisable, clickEvent.type, clickEvent.data.url}` |

### 2.2 页签计数 ← `mtop.taobao.idle.merchant.order.count` v1.0 `data:{queryType:"ORDER"}`

响应 `data.module.countInfoList[]`，真机字段是 **`count` + `queryCode`**（bundle 里前端自己的
`{title, number, key}` 是本地文案对象，**不是**平台字段）：

| 输出 | 平台路径 |
| --- | --- |
| `counts[code]` | `countInfoList[].count`（键是 `queryCode`） |
| `tabs[].{code, label, count}` | `label` 是本项目的中文映射，未知 code → `label: null`（**不猜**） |

真机 9 个 code：`ALL`=15、`TRADE_SUCCESS`=9、`TRADE_CLOSED`=2、`NOT_SHIP`=1，
其余（`NOT_PAY`/`SHIPPED`/`REFUND`/`NOT_SHIP_EXPIRED`/`NOT_SHIP_ABOUT_TO_EXPIRE`）都是 0。

### 2.3 退款 `refunds[]` ← `mtop.taobao.idle.merchant.refund.list` v1.0

**容器是双层的**：`resp.data.data.items[]`（不是 `data.module.items`，这点和订单相反）。

| 输出字段 | 平台路径 |
| --- | --- |
| `refund_id` | `refundInfoVO.refundId` |
| `order_id` / `item_id` | `commonData.orderId` / `.itemId`（真机是**字符串**） |
| `order_status` | `commonData.orderStatus`（如 `"未发货退款"`） |
| `refund_status` / `refund_status_text` | `commonData.refundStatus`（数字 5） / `refundInfoVO.refundStatus`（文案） |
| `refund_type` / `refund_accept` / `cs_status` / `reason` / `goods_needs` / `in_sale` | `refundInfoVO.refundType` / `.refundAccept` / `.csStatus` / `.reason` / `.goodsNeeds` / `.inSale` |
| `refund_time` | `refundInfoVO.gmtCreate` |
| `remark` / `remark_rich_text[]` | `commonData.orderSimpleRemark` / `.orderSimpleRemarkRichText.data[].{content, type}` |
| `buyer.user_id` / `nick` / `avatar` | `buyerInfoVO.buyerId` / `.userNick` / `.userIcon`（**退款帧没有 name/phone/address**，一律 null） |
| `item.title` / `pic_url` / `info_lines[]` | `itemVO.title` / `.itemPicUrl` / `.itemInfoLines[]` |
| `price.auction_price` / `buy_num` / `refund_fee` / `refund_fee_view` | `priceVO.{auctionPrice, buyNum, refundFee, refundFeeView}` |
| `times.create_time` | `commonData.createTime` |
| `buttons[]` | `rightVO.btnList[].{name, code, clickEvent.data.url}` |
| 顶层 `total_refund_fee` | `resp.data.data.ext.totalRefundFee`（真机 `"468.90"`，字符串） |

### 2.4 评价 `evaluations[]` ← `mtop.taobao.idle.merchant.rate.list` v1.0

列表项是**订单**，评价在 `rateItemVOList`（`data.module.items[]`）：

| 输出字段 | 平台路径 |
| --- | --- |
| `order_id` / `item_id` / `order_status` / `seller_rate_status` | `merchantCommonData.{orderId, itemId, orderStatus, sellerRateStatus}` |
| `buyer.*` | `merchantBuyerVO.{buyerId, userNick, name, phone, address, userIcon, isPrivacy}` |
| `item.title` / `pic_url` / `info_lines[]` | `merchantItemVO.{title, itemPicUrl, itemInfoLines}` |
| `times.*` | `merchantCommonData.{createTime, paySuccessTime, consignTime, finishTime}` |
| `has_rate` | `len(rateItemVOList) > 0`（**空数组 ⇒ `false` + `rates: null`，绝不写 0 条**） |
| `rates[].rate_id` / `rate` / `main` / `feedback` / `level` | `rateItemVOList[].{rateId, rate, main, feedBack, buyerMainRateLevel}` |
| `rates[].create_time` | `rateItemVOList[].gmtCreate`（**原样 `str()`**：真机无评价记录，类型未验证） |
| `rates[].illegal` / `pictures[]` / `service_tags[]` / `rater_avatar` / `seller` | `rateItemVOList[].{illegal, pictCdnUrlList, serviceTags, raterHeadImg, seller}` |

⚠️ `rateItemVOList[]` 的字段名来自 bundle 读法，**本项目账号还没有任何评价**，
所以这一支**没有真机样本**（测试里的 `RATE_WITH_RECORD` 已标注为「按平台字段名构造」）。

### 2.5 投诉 `complaints[]` ← `mtop.taobao.idle.cco.shop.complain.list` v1.0

**无真机样本**（真机返回 `NO_PERMISSION::用户没有权限`）。因此本模块**不编字段名**：
`complaints[]` 里的元素是平台条目的**原样透传**，`status_count_map` ← `resp.data.statusCountMap`
（bundle 读法 `e.data.statusCountMap`）。等有权限的账号抓到真机帧再补映射。

### 2.6 退货地址 `addresses[]` ← `mtop.alibaba.idle.seller.platform.merchant.delivery.address.list.query` v1.0

**无真机样本**（真机返回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`）。
请求**不传 data**（bundle 原文 `hO({api:"...",v:"1.0"})`）。bundle 里有**两套读法，这里两套都认**
（拿不到样本 ⇒ 不判定哪一套是真的）：

- 只读页的 `formatter`：`e.data[]` 元素 `{addressId, name, phoneNum, address, defaultAddr}`
  → `address_id / name / phone / address / is_default`（`is_default` 只在平台给 bool 时才给值，否则 null）；
- 写页编辑弹窗（偏移 3780900~3782550）：按 `contactId` / `contactName` / `mobilePhone` /
  `provinceName` / `cityName` / `districtName` / `detailAddress` **逐个字段**读 → 追加
  `contact_id / contact_name / mobile_phone / province_name / city_name / district_name /
  detail_address`，**平台给了才出现**（空值不出现，绝不凭空造字段）。

`address_id` 的回退键是 `addressId → contactId → id`，`name` 是 `name → contactName`，
`phone` 是 `phoneNum → phone → mobilePhone` —— 两套键名指的是同一个实体。
写接口见 `docs/contracts/trade_write.md` §8。

## 3. 只读边界（硬约束）

- 协议层 `backend/app/xianyu/trade.py` 里 `WRITE_APIS` 列了 **19 个写接口**
  （同意/拒绝退款、邮费退款、创建评价、发货、改价、延长确认、退货地址增删改、设默认、
  发货地保存、短信、投诉举证/撤销/拒绝、卖家关闭订单…），`assert_read_only(api)`
  在**每次调用前**拦截，命中抛 `ReadOnlyViolation`。
- 服务层 `backend/app/services/trade.py:_call()` 第一步就是 `assert_read_only(api)`，
  所以任何写接口都到不了网络层。
- 本模块**不实现**任何写能力，也没有对应的 HTTP 方法（405）。

## 4. 失败语义（必须保持）

| 情况 | HTTP | code | 额外字段 |
| --- | --- | --- | --- |
| 账号不存在 | 404 | `not_found` | — |
| 没有 Cookie / `login_state = need_relogin` | 409 | `verification_required` | `login_state` |
| 登录态过期（`FAIL_SYS_SESSION_EXPIRED`，协议层抛 `SessionExpiredError`） | 409 | `verification_required` | `login_state: "need_relogin"`，并回写库 |
| 平台 `success: false`（含 `NO_PERMISSION` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`） | 502 | `protocol_error` | `platform_errors: {api, explain, message?}` |
| 风控 / 其它异常 | 502 | `protocol_error` | `platform_errors: {api, explain}` |
| 分页参数非法 | 400 | `invalid_fields` | — |

**绝不用空列表或 0 掩盖失败**：平台失败一定是 `success: false` 的 502，不是「没有订单」。

## 5. 未验证 / 已知问题（交接）

1. **投诉 / 退货地址无样本**：只能原样透传（见 2.5 / 2.6），字段映射等有权限的账号再补。
2. **评价记录无样本**：`rateItemVOList[]` 映射来自 bundle，未过真机（见 2.4）。
3. **金额单位**：全部按平台原文透传字符串（`"39.90"`、`"468.90"`），前端展示前不要自己 `/100`。
4. **`sold.get` 的分页键**：已确认 —— 在 `data.module` 里（`trade.paging_of` 会合并
   `body_of`），2026-10-15 真机实测 `paging={total_count:15, next_page:true, last_end_row:0,
   server_time:null}`（`logs/order_sync57/platform_orders.json`，同账号同日 15 单）。
   refund/rate 在信封顶层。代码对两者都做了兼容；订单翻页用 `total_count`，不用 `nextPage`
5. **间歇性客户端异常**：真机 `refund.list` 第 1 次调用抛
   `TypeError: can only concatenate list (not "str") to list`（客户端侧，不是平台 ret），
   重试两次均成功 ⇒ 与 Cookie 续期/累积状态相关，怀疑
   `backend/app/xianyu/client.py` 的续期落库路径（`:387 cookies_str` / `:410 persist_account_id`）。
   **未修**，需 Lead 安排复现（不在本任务写入范围）。
6. **`main.py` 未挂载**：`ROUTE_MODULES` 缺 `'trade'`（见 1.1）。
