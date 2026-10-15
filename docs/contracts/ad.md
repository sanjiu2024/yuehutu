# 推广（投放）补齐接口契约（`/api/promotion/ad`）

> 覆盖范围：官方「推广（投放）」页面里 **`client.py` + `services/promotion.py` + `services/ad_write.py`
> 没盖到的那几块** —— 投放账户信息 / 投放包 / 商品搜索 V2 / 自定义出价估算 / 投放单元（人群包）
> 列表与数据 / 计划操作日志 / 推广消息中心（列表 / 详情 / 未读 / 标记已读）；
> 第 63 批追加 **优惠券列表 / 优惠券汇总 / 账户卡片 V2 / 活动详情渲染** 4 条只读（见 §9）。
> 协议层 `backend/app/xianyu/ad.py`、服务层 `backend/app/services/ad.py`、路由层
> `backend/app/api/ad.py`（`backend/app/main.py` 的 `ROUTE_MODULES` 含 `'ad'`）。
>
> 证据来源：
>
> * 官方 bundle `https://g.alicdn.com/idle-pc/fish-ad-pc/0.0.12/js/main.js`
>   （2026-10-11 拉取，3,760,912 字节，单行压缩 JS，共 86 个 mtop 名；本地副本
>   `C:\Users\Administrator\.dsh-tmp\adapp\ad-main.js`，**未入库**）；
>   bundle 是单行压缩的，所以下面每条都给「**字符偏移 + 可复现搜索串**」两样：
>   偏移会随版本变，搜索串不会。
> * 契约测试：`backend/tests/test_ad_api.py`（HTTP + 服务层 + 端到端 + 幂等）、
>   `backend/tests/test_xianyu_ad.py`（协议层请求形状）。
> * **真机证据（2026-10-16，打包版 18400 + 真实 Cookie，账号 `2217970177348`）**：11 条只读打了 9 条 ——
>   **6 条 200**（`apis` 静态目录 / `account-info` / `groups` / `groups/data` / `messages` / `messages/unread`），
>   **3 条被平台拒**（`package-info`、`campaign.log.query` → `E_AD_BP_COMMON_SYSTEM_EXCEPTION`；`item.searchV2` → `E_AD_BP_COMMON_PARAM_NULL`），
>   **2 条未打**（`bid.estimates` 缺合法 `entityType/entityList`；`message.detail` 该账号 `message.list` 的 `total=0`）。
>   唯一写接口（消息已读）**未打**（本批统一不做写试打；它不改交易状态，属「零副作用」档，可先打）。原始响应见 `logs/probe60/`（`logs/` 不入仓）。
>   因此：平台没返回的字段一律 `None`，**不用 0 顶替**；响应 `data` 原样透出，不做「认字段」的猜测。
> * **真机复打（2026-10-16 第二轮，in-process 只读探针，同一账号）**：上面 3 条被拒的 + 1 条未打的
>   **全部打通（4/4）** —— 根因是「官方调用点恒定带的业务键没给全」，逐条证据见 §8。修复落在
>   `backend/app/xianyu/ad.py`（`BODY_DEFAULTS` / `BODY_REQUIRED` / `MissingRequiredParams`）与
>   `backend/app/services/ad.py`（残包翻 400 `invalid_fields`、`entity_type`/`entity_list` 便利入口、`_payload_only`）。
> * **真机只读复打（第 63 批，2026-10-16，in-process 只读探针，同一账号）**：4 条新接口逐条打到 ——
>   `coupon.info.query`（官方初值）与 `coupon.statistic.query` **✅ 200**；
>   `account.card.v2` **❌ `FAIL_SYS_UNAUTHORIZED_ENTRANCE::API访问入口未授权`**；
>   `activity.detail.rendering` **❌ `NO_APPLY_ACTIVITY_PERMISSION::活动报名时间已结束或者您不在本期活动名单内`**（后两条是平台原话，路由本身语义正确，见 §9.3 / §9.4）。
>   原始响应：`logs/probe63/kurumi/ad_*.json` + `ad_probe63_all.json` + `ad_service63.json`（`logs/` 不入仓）。

## 0. 一句话结论

| 能力 | 接口（mtop） | 真机结果 | 结论 |
| --- | --- | --- | --- |
| 投放账户信息 | `mtop.taobao.idle.ad.account.info` | ✅ 200（`{cashBalance:3358,totalBalance:3358,usableBalance:3358}`） | 接了 |
| 投放包信息 | `mtop.taobao.idle.ad.package.info` | ❌ 502 `E_AD_BP_COMMON_SYSTEM_EXCEPTION` → **✅ 200（2026-10-16 复打，见 §8）** | 接了 |
| 商品搜索 V2 | `mtop.taobao.idle.ad.item.searchV2` | ❌ 502 `E_AD_BP_COMMON_PARAM_NULL`（缺必填） → **✅ 200（2026-10-16 复打，见 §8）** | 接了 |
| 自定义出价估算 | `mtop.taobao.idle.ad.customize.bid.estimates` | 未打（缺合法 entityType/entityList） → **✅ 200（2026-10-16 复打，见 §8）** | 接了 |
| 投放单元列表 | `mtop.taobao.idle.ad.group.list` | ✅ 200（不传 campaignId 也能通） | 接了 |
| 投放单元数据 | `mtop.taobao.idle.ad.group.data.query` | ✅ 200 | 接了 |
| 计划操作日志 | `mtop.taobao.idle.ad.campaign.log.query` | ❌ 502 `E_AD_BP_COMMON_SYSTEM_EXCEPTION` → **✅ 200（2026-10-16 复打，见 §8）** | 接了 |
| 推广消息列表 | `mtop.taobao.idle.ad.message.list` | ✅ 200（该账号 total=0） | 接了 |
| 推广消息详情 | `mtop.taobao.idle.ad.message.detail` | 未打（该账号 total=0，无 id） | 接了 |
| 推广消息未读数 | `mtop.taobao.idle.ad.message.unread.info` | ✅ 200（unreadCount） | 接了（**唯一不套 requestJson** 的一条） |
| 标记推广消息已读（写） | `mtop.taobao.idle.ad.message.read` | 未打（本批统一不做写试打） | 接了（confirm 闸门 + 幂等键） |
| 优惠券列表 | `mtop.alibaba.idle.ad.bp.coupon.info.query` | ✅ 200（`{pageNum:1,pageSize:10,result:[],total:0,totalPage:0}`；裸 `{}` 则 502 `UNKNOWN_FAIL_CODE`） | 接了（第 63 批，见 §9.1） |
| 优惠券汇总 | `mtop.alibaba.idle.ad.bp.coupon.statistic.query` | ✅ 200（三个金额字段均为字符串 `"0"`） | 接了（第 63 批，见 §9.2） |
| 账户卡片 V2（活动奖励页） | `mtop.alibaba.idle.ad.account.card.v2` | ❌ 502 `FAIL_SYS_UNAUTHORIZED_ENTRANCE::API访问入口未授权` | 接了（路由在；平台未授权，载荷未验证，见 §9.3） |
| 活动详情渲染 | `mtop.taobao.idle.ad.activity.detail.rendering` | ❌ 502 `NO_APPLY_ACTIVITY_PERMISSION::活动报名时间已结束或者您不在本期活动名单内` | 接了（路由在；该账号不在活动名单，见 §9.4） |
| 报表下载（创建异步导出任务） | `mtop.taobao.idle.ad.bp.data.download` | 未打（**偏写，只登记不接**） | **不接**（服务端建任务 / 落库，见 §6、§9.5） |
| 商品推荐位 | `mtop.taobao.idle.ad.item.recommend` | 未打（本批不接） | **不接**（无入参、bundle @3300908 `data:{}`；属选品页装饰性推荐位，见 §6） |

**与已有能力的查重（等价就不重复实现）**：`ad_home` / `ad_account_card`（`bp.account.card.query`）/
`ad_member_data` / `ad_campaigns`（`campaign.search`）/ `ad_campaign_metrics`（`campaign.data.batch.query`）/
`ad_item_metrics`（`item.data.batch.query`）/ `ad_settle` / `ad_account_log`（`bp.account.log.query`）/
`ad_log_operates` / `ad_recharge_detail` / `ad_withdraw_detail` 都在 `backend/app/xianyu/client.py:1068-1150`、
服务在 `backend/app/services/promotion.py`；写族（计划增删改停启 / 活动报名领奖 / 流水操作）在
`backend/app/services/ad_write.py`。本批 11 条**都不与它们重复**（`account.info` ≠ `bp.account.card.query`；
`item.searchV2` ≠ `item.data.batch.query`；`group.list`/`group.data.query` ≠ `campaign.search`/
`campaign.data.batch.query`；`campaign.log.query` ≠ `bp.account.log.query`）。

## 1. 接口总表

所有只读接口都是 **GET**（查询串参数）；唯一写接口 `message.read` 是 **POST**。

| kind | mtop 接口 | 落点函数（`services/ad.py`） | 路由 | bundle 偏移 / 搜索串 |
| --- | --- | --- | --- | --- |
| `account_info` | `...ad.account.info` | `account_info()` | `GET /api/promotion/ad/{account_id}/account-info` | @3301473 `rqe(e)` / `mtop.taobao.idle.ad.account.info` |
| `package_info` | `...ad.package.info` | `package_info()` | `GET .../package-info` | @3301020 `eqe(e)` / `mtop.taobao.idle.ad.package.info` |
| `item_search` | `...ad.item.searchV2` | `item_search()` | `GET .../item-search` | @3301167 `tqe(e)` / `mtop.taobao.idle.ad.item.searchV2` |
| `customize_bid_estimates` | `...ad.customize.bid.estimates` | `customize_bid_estimates()` | `GET .../bid-estimates` | @3301315 `nqe(e)` / `mtop.taobao.idle.ad.customize.bid.estimates` |
| `group_list` | `...ad.group.list` | `group_list()` | `GET .../groups` | @3253585 `gWe(e)` / `mtop.taobao.idle.ad.group.list` |
| `group_data` | `...ad.group.data.query` | `group_data()` | `GET .../groups/data` | @3253874 `yWe(e)` / `mtop.taobao.idle.ad.group.data.query` |
| `campaign_log` | `...ad.campaign.log.query` | `campaign_log()` | `GET .../campaign-log` | @1665384 `IW(e)` / `mtop.taobao.idle.ad.campaign.log.query` |
| `message_list` | `...ad.message.list` | `message_list()` | `GET .../messages` | @3678207 `XQe(e)` / `mtop.taobao.idle.ad.message.list` |
| `message_detail` | `...ad.message.detail` | `message_detail()` | `GET .../messages/{message_id}` | @3678352 `KQe(e)` / `mtop.taobao.idle.ad.message.detail` |
| `message_unread` | `...ad.message.unread.info` | `message_unread()` | `GET .../messages/unread` | @703173 / `mtop.taobao.idle.ad.message.unread.info` |
| `message_read`（写） | `...ad.message.read` | `message_read()` | `POST .../messages/read` | @3678499 `ZQe(e)` / `mtop.taobao.idle.ad.message.read` |

| `coupon_info` | `...idle.ad.bp.coupon.info.query` | `coupon_info()` | `GET .../coupons` | @3246590 `sWe(e)` / `mtop.alibaba.idle.ad.bp.coupon.info.query` |
| `coupon_statistic` | `...idle.ad.bp.coupon.statistic.query` | `coupon_statistic()` | `GET .../coupons/statistic` | @3246428 `aWe()` / `mtop.alibaba.idle.ad.bp.coupon.statistic.query` |
| `account_card_v2` | `...idle.ad.account.card.v2` | `account_card_v2()` | `GET .../account-card-v2` | @3268292 `UWe(e)` / `mtop.alibaba.idle.ad.account.card.v2` |
| `activity_detail` | `...idle.ad.activity.detail.rendering` | `activity_detail()` | `GET .../activity-detail` | @3268518 `YWe()` / `mtop.taobao.idle.ad.activity.detail.rendering` |

另有 `GET /api/promotion/ad/apis`：接口目录（只读 kind 清单 + 写动作），给工具面/AI 自描述用。
第 63 批后只读 kind 共 **14 条**（顺序 = `services/ad.py:READ_KINDS` = `xianyu/ad.py:READ_APIS`，测试 `test_read_kinds_match_protocol` 盯着）；
同一目录里还透出 `unsupported: [{api, reason}]`，登记 `mtop.taobao.idle.ad.bp.data.download`（@3115691，偏写，**不进 `READ_APIS`**，见 §9.5）。

**线上调用形状**：除 `message.unread.info` 与第 63 批的 `activity.detail.rendering` 外都是
`{api, v:'1.0', data:{requestJson: JSON.stringify(<业务对象>)}}`（bundle 里的 `yp({...})` 包装），
对应 `client.mtop(api, {'requestJson': request}, v='1.0', spm_cnt='a21ybx.im.0.0')`。
`message.unread.info`（`backend/app/xianyu/ad.py:185-188`）与 `activity.detail.rendering`（官方 @3268518 `data:{}`）
的官方 `data` 是 **空对象**、不套 `requestJson`；这两条的 `READ_APIS[kind]['request_json']` 都是 `False`。
bundle 里 `group.list` / `campaign.log.query` 的包装**没有写 `v`**，但本仓库 `mtop.call_params`
（`backend/app/xianyu/mtop.py:98`）把 `v` 当固定键、`client.mtop` 也没有「不带 v」的表达方式，
平台默认同样是 1.0，所以统一发 `v=1.0`（不另造参数）。

## 2. 只读接口逐条口径

响应统一信封（成功）：

```json
{"success": true, "data": {
  "account_id": "acc", "kind": "group_list", "api": "mtop.taobao.idle.ad.group.list",
  "data": { /* 平台 data 原样 */ }, "fetched_at": "2026-10-16 00:00:00", "source": "platform"
}}
```

### 2.1 `account-info`（投放账户信息）

* 查询串：`order_price`、`promotion_mode`、`bid_type`、`scene`，**给哪个带哪个**（空值不带）。
* 官方调用点 @3488564 / @3672101 传 `{orderPrice, promotionMode, bidType, scene}`，`scene` 取
  `EAccountRequestScene`（如 `CREATION_REAL_TIME` / `CREATION_SUCCESS`）。
* 响应 `data` 原样透出（2026-10-16 真机确认账户信息是 `{cashBalance,totalBalance,usableBalance}`；其余仍未验证，不认字段）。

### 2.2 `package-info`（投放包信息）

* 入参是**整包查询对象**，用 JSON body 表达：`GET` + `{"promotionMode": "CUSTOMIZE", ...}`。
  GET 带 body 不是所有客户端都发得出来，所以**再给一个兜底**：`?payload=<JSON 对象字符串>`
  （body 优先）。`payload` 解析不出对象 → 400 `invalid_fields`（**不静默当空对象查**）。
* body / payload 都为空时按空对象查（官方 `initPlanInfo` @3309064 也是这么发的）。
* 官方读 `data.packageTargetVOList` / `data.bidTargets` / `data.recPromotionMode` / `data.supportOrder`。

### 2.3 `item-search`（商品搜索 V2）

* 查询串：`item_id`、`search_key`、`campaign_id`、`need_tabs`、`sort_field`、`sort_type`、
  `page_num`、`page_size`。**`item_id` 与 `search_key` 至少给一个**，都不给 → 400 `invalid_fields`。
* `campaign_id` 转 int；`need_tabs` 转 bool（`true/1/yes`）。
* 官方调用点 @3408019；响应 `data.result.{tabList,itemList}` + `data.total`。

### 2.4 `bid-estimates`（自定义出价估算）

* 同 `package-info`：JSON body 或 `?payload=<JSON>`，**必须有请求体**（空 body → 400 `invalid_fields`）。
* 官方调用点 @3313962 / @3314544 / @3315969 传
  `{entityType, entityList, bidTarget, bidPrice, noBid, pcFirstRequest, campaignId?}`；
  响应 `data.customizeBidRecommendVOList` / `data.bidTargets`。

### 2.5 `groups`（投放单元 / 人群包列表）

* 查询串：`start_date`、`end_date`（`YYYYMMDD`，形状不对 → 400）、`page_num`、`page_size`、
  `campaign_id`、`group_status`、`promotion_mode`、`title_like`（→ `campaignTitleLike`）。
* **恒带 `includeEndDate: true`**（官方 @3454098 / @3493409 都带；少了它平台按「不含结束日」算）。
* `campaign_id` / `group_status` 转 int；响应 `data.result`。

### 2.6 `groups/data`（单个投放单元的分日数据）

* 查询串：`ad_group_id`（**必填**，空 → 400）、`start_date`、`end_date`、`all_period`。
* **恒带 `includeEndDate: true`**；`all_period` 给了才带（官方传 `false`）。
* 响应含 `data` 与 `previousData`（官方在「昨天→今天」时拍成
  `dataDetailVOList` / `previousDataDetailVOList`）—— 两个都原样透出，不做合并。

### 2.7 `campaign-log`（计划操作日志）

* 查询串：`start_date`、`end_date`、`campaign_id`、`page_num`、`page_size`、
  `operator_type`、`operation_type`。
* `campaign_id` **恒带**（官方 @1794033 是 `Number(r)||0`，没选计划就是 `0`）；
  `operator_type` / `operation_type` **没选就不带**（带 0 与不带不是一回事，官方枚举里 0 = 全部）。
* 枚举（bundle @1784368 / @1784654）：`operatorType` 0=全部 / 1=用户 / 2=系统；
  `operationType` 0=全部 / 1=添加 / 2=更新。协议层 `OPERATOR_TYPES` / `OPERATION_TYPES` 抄了这份。

### 2.8 `messages` / `messages/{id}` / `messages/unread`

* 列表：`page_num`、`page_size`；官方 @3698592，响应 `data.total` + 列表。
* 详情：`message_id` **必填**（空 → 400），官方 @3696148，响应 `data.title` / `data.content`。
* 未读：**不接受任何参数**（官方 @703173 的 `data` 是空对象），响应 `data.unreadCount`。
  路由注册顺序上 `/messages/unread` 在 `/messages/{message_id}` **之前**，否则会被当成
  `message_id='unread'`（`backend/app/api/ad.py:171` 在 `177` 之前，测试 `test_message_unread_takes_no_args` 盯着）。

## 3. 写接口：`POST /api/promotion/ad/{account_id}/messages/read`

请求体：`{"confirm": true, "message_ids": [1, 2]}`（`message_ids` 也接受 `"1,2"` / `"[1,2]"`）。

* **confirm 闸门**：只认字面 `true`（`common.confirm_required(..., strict=True)`）。
  没确认 → 400 `confirm_required`，**一次协议调用都不发**（`backend/app/services/ad.py:420-424`）。
  协议层还有第二道闸门 `xianyu/ad.py:_require_confirm`：即使服务层被绕过，没 `confirm=True`
  也直接抛 `WriteNotConfirmed`，服务层翻成 400 `confirm_required`（不是 502）。
* **幂等**：带 `Idempotency-Key` 时同一键 + 同一指纹只打平台一次，重复请求回放上次响应并带
  `idempotent_replay: true`（`backend/app/services/common.py:331` `idempotent_call`）；不带键则原样执行。
  指纹用 `{messageIds: [...]}`，所以**参数校验在算指纹之前**完成（闸门/参数拒绝不占键）。
* 成功响应 `data`：`{account_id, kind:'message_read', api, data, fetched_at, source, confirmed:true,
  written_at}`。
* **操作留痕**：成功与失败都写 `operation_logs`（`action='message_read'`、`item_id=逗号连接的 id`）；
  平台可能已经生效、只是响应丢了 —— 失败也留痕（与 `trade_write` 同口径）。
* 写接口**不**接受批量其它写操作：`backend/app/services/ad.py:489`（`execute`）只认 `message_read`，其它动作 → 404 `not_found`。

## 4. 错误语义

| 场景 | HTTP | code | 说明 |
| --- | --- | --- | --- |
| 参数形状不对（日期非 `YYYYMMDD`、`page_num<1`、`ad_group_id` 空、`item_id`+`search_key` 都空、`message_id` 空、`message_ids` 空） | 400 | `invalid_fields` | 校验在打平台之前，**一次协议调用都不发** |
| 写接口没确认 | 400 | `confirm_required` | 只认字面 `true` |
| 未知 kind / 未知写动作 | 404 | `not_found` | |
| 账号不存在 | 404 | `not_found` | |
| 账号没有 Cookie / 登录态已过期（`need_relogin`） | 409 | `verification_required` + `login_state:'need_relogin'` | 前置检查，不打平台 |
| 协议层认出的登录态过期（`FAIL_SYS_SESSION_EXPIRED` 等） | 409 | `verification_required` + `login_state:'need_relogin'` | `common.call_method` 回写账号状态 |
| 协议层模块导入不到 | 503 | `xianyu_not_ready` | **不是**「平台没数据」，页面不该显示成空 |
| 平台拒绝（无权限 / 风控 / 其它 `FAIL_*`）、协议层抛异常、响应非 dict | 502 | `protocol_error` | 平台原文透出；**无权限 ≠ 掉线**，不写 `need_relogin` |

## 5. 真机探针清单（原口径：Lead 在打包版上跑；**2026-10-16 已在 in-process 只读探针上跑完 4 条，见 §8**）

前置：账号已完成扫码登录（库里 `account_credentials.cookie` 非空、`login_state='active'`）；
`{acc}` 换成真实账号 id。**读接口**按下面最小参数逐个打，看 `success` 与 `data` 形状；
`data` 为空 dict 也算「平台给了答复但没字段」（如实记录，不要当 0）。

**2026-10-16 已按本清单打过一轮**（结果见 §0 的真机证据块与 `logs/probe60/`）：#1 / #5 / #6 / #8 / #9 回 200；
#2 / #3 / #7 被平台拒（错误码如实透出）；#4 / #10 未打（缺合法枚举 / 该账号无消息）；#11（写）未打。

| # | method + path | 最小参数 | 关注点 |
| --- | --- | --- | --- |
| 1 | `GET /api/promotion/ad/{acc}/account-info` | `?scene=CREATION_REAL_TIME` | 是否要 `orderPrice`/`promotionMode`；返回是否有权限字段 |
| 2 | `GET /api/promotion/ad/{acc}/package-info` | `?payload={"promotionMode":"CUSTOMIZE"}` | `packageTargetVOList` / `bidTargets` 是否出现 |
| 3 | `GET /api/promotion/ad/{acc}/item-search` | `?item_id=<在售商品id>&page_num=1&page_size=10` | `result.tabList` / `result.itemList` / `total` |
| 4 | `GET /api/promotion/ad/{acc}/bid-estimates` | `?payload={"entityType":"ITEM","entityList":["<id>"],"bidTarget":"...","bidPrice":100,"noBid":false,"pcFirstRequest":true}` | `entityType`/`bidTarget` 的合法枚举值（**最容易猜错的一条**） |
| 5 | `GET /api/promotion/ad/{acc}/groups` | `?start_date=20261001&end_date=20261010&page_num=1&page_size=10` | `result` 形状；是否必须 `campaignId` |
| 6 | `GET /api/promotion/ad/{acc}/groups/data` | `?ad_group_id=<从 #5 拿>&start_date=20261001&end_date=20261010&all_period=false` | `data` / `previousData` 是否都在 |
| 7 | `GET /api/promotion/ad/{acc}/campaign-log` | `?start_date=20261001&end_date=20261010&campaign_id=0&page_num=1&page_size=10` | `campaign_id=0`（全部）平台是否接受 |
| 8 | `GET /api/promotion/ad/{acc}/messages` | `?page_num=1&page_size=10` | 列表字段名 + `total` |
| 9 | `GET /api/promotion/ad/{acc}/messages/unread` | 无 | 是否真返回 `unreadCount`（**唯一不套 requestJson 的一条**） |
| 10 | `GET /api/promotion/ad/{acc}/messages/{id}` | `{id}` 取 #8 里的消息 id | `title` / `content` |
| 11 | `POST /api/promotion/ad/{acc}/messages/read` | body `{"confirm":true,"message_ids":[<#8 的 id>]}` + 头 `Idempotency-Key: probe-1` | 先不带 `confirm` 验 400；再带；再同键重发验回放 |

## 6. 未做 / 不接

* `mtop.taobao.idle.ad.item.recommend`（@3300908 `QGe()`）：**不接**。官方包装无入参
  （`data:{}`）、只在选品页当装饰性推荐位用（调用点 @3414824 读
  `data.result.{supportAd,itemId,promotionTag,checked}`），对「复刻页面能力 + AI 可调用」没有增量；
  真要接也只是再加一个无参只读接口，留给下一批。
* 充值与提现：**没有写接口**。bundle 里只有 `.recharge.query` / `.withdraw.query` 两条只读，
  已在 `client.py` 覆盖（`services/ad_write.py` 的 `UNSUPPORTED` 也明确不做）。
* 计划/活动的写操作（增删改停启、报名领奖、流水操作）：**已由 `services/ad_write.py` 覆盖**，本批不重复。
* `mtop.taobao.idle.ad.bp.data.download`（@3115691 `XFe(e)`）：**只登记、不接**。官方「下载报表」按钮调它**创建服务端异步导出任务**（响应读 `data.downloadUrl` / `data.taskId` / `data.status`，没有 `downloadUrl` 时拿 `taskId` 每 60s 轮询；`targetType=CAMPAIGN|MEMBER|ITEM`）—— 有副作用/落库，不是读现成文件。登记在协议层常量 `BP_DATA_DOWNLOAD_API`（不进 `READ_APIS`）与服务层 `UNSUPPORTED`，`GET /apis` 的 `catalog().unsupported` 透出，逐条口径见 §9.5。
* 本批**没有**把新路由接进 AI 工具面（`backend/app/agents/tools.py`）与 MCP（`mcp_server/client.py`）：
  那两个文件是冻结文件（Lead 独占）。路由清单见 §1，交给 Lead 接线。

## 7. 测试

```
XIANYU_APP_DIR=$TEMP/xywb_adcov PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_ad_api.py
XIANYU_APP_DIR=$TEMP/xywb_adcov PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_ad.py
```

* `test_ad_api.py`（**63** 个用例，第 63 批后；新增 4 条新接口的 HTTP / 服务层 / 端到端用例，见 §9.6）：HTTP 信封 / 参数形状 / 错误语义（400/404/409/502/503）/
  只读探测（对**真 app** 验非 GET 405）/ 写闸门（`confirm` 严格档、协议层二次闸门）/
  幂等键回放 / 操作留痕 / 端到端（真协议层 + 假 mtop）/ 真 app 幂等。
* `test_xianyu_ad.py`（**15** 个用例，第 63 批后）：协议层 `api` 名、`requestJson` 内容、`v/spm_cnt`、
  未读数不套 `requestJson`、写闸门拒绝时**一个字节都不发**；官方默认值补齐（`BODY_DEFAULTS`）、
  调用方值覆盖默认值、缺必填键在协议层就被 `MissingRequiredParams` 拦下（**一个字节都不发**）、
  服务层 `read()` 不把便利键 `entity_type`/`entity_list` 透传成 payload。
* 闸门白名单：`backend/tests/test_write_routes_have_gates.py` 通过（`POST /messages/read` 的服务函数
  签名含 `confirm` 且含 `confirm_required` 标记），**没有**往 `GATE_FREE_ALLOWLIST` 加条目。
## 8. 真机复打（2026-10-16 第二轮）：4 条 502 的根因与修复

- 账号：`2217970177348`（月狐兔_，`login_state=active`）；方式：**in-process 只读探针**
  `/c/Users/Administrator/.dsh-tmp/ad_probe62.py`，走 `services/ad.py` 只读入口 + 真机 mtop；
  **只读、串行、每条间隔 ≥ 2.2s，没有任何写操作**。未走打包版 18400（那是修复前的冻结代码，没有重打包）。
- 原始响应：`logs/fail62/kurumi/ad_*.json` + `history.jsonl`（`logs/` 不入仓）。
- 信封口径先排除：四条官方**全部**套 `requestJson`（`yp({api, v:'1.0', data:{requestJson: JSON.stringify(e)}})`），
  与本仓库 `client.mtop(api, {'requestJson': ...})` 一致 —— **不存在 `inputJson` / `param` 之类的另一层信封**。
  502 的根因是 `requestJson` 里的业务键缺了，不是信封错了。

| 接口 | bundle 偏移（调用点） | 修复前 | 修复后发出的 requestJson | 平台结果 | 判读 |
| --- | --- | --- | --- | --- | --- |
| `item.searchV2` | @3301167（@3408019） | 502 `E_AD_BP_COMMON_PARAM_NULL::必填参数为空` | `{"tab":"platReco","needTabs":true,"itemId":"1047949964912","pageNum":1,"pageSize":10}` | 200 `undefined`，`data` = `nextPage, result, total, totalPage` | **已解**：缺 `tab` / `needTabs` |
| `package.info` | @3301020（@3677643 / @3396844） | 502 `E_AD_BP_COMMON_PARAM_NULL::必填参数为空` | `{"isGoodsSelectedTabChange":true,"entityType":1,"entityList":[{"entityId":"1047949964912","catId":"50023914","price":15.0,"areaTarget":false,"startTime":"2026-10-16 03:45:45"}]}` | 200 `undefined`，`data` = `bidTargets, optimizeGoalList, packageTargetVOList, recBidTarget, recBidTargetExperiment, recPromotionMode, supportOrder` | **已解**：缺 `entityType` / `entityList`，且 `catId` 必须是真实类目 ID |
| `customize.bid.estimates` | @3301315（@3315969 / @3314544 / @3313962） | 本地 `invalid_fields`（必填键没给） | `{"pcFirstRequest":true,"entityType":1,"entityList":[{"entityId":"1047949964912","catId":"50023914","price":15.0,"areaTarget":false,"startTime":"2026-10-16 03:45:45"}]}` | 200 `undefined`，`data` = `bidTargets, customizeBidRecommendVOList, entityType, optimizeGoalList, recBidTarget, recBidTargetExperiment` | **已解**：缺 `entityType` / `entityList` |
| `campaign.log.query` | @1665384（@1794033） | 502 `E_AD_BP_COMMON_SYSTEM_EXCEPTION::闲鱼太累了，请稍后再试～` | `{"operatorType":0,"operationType":0,"startDate":"20261009","endDate":"20261016","campaignId":0,"pageNum":1,"pageSize":5}` | 200 `undefined`，`data` = `nextPage, result, total, totalPage` | **已解**：缺 `operatorType` / `operationType`（官方初值 0） |

HTTP 状态口径：mtop 业务码走 `ret`（成功 `SUCCESS::调用成功`，业务失败 `FAIL::E_AD_BP_COMMON_*`），HTTP 一律 200；
本仓库 API 层把业务失败翻成 502（`services/common.py:644 call_failed`），把协议层拦下的残包翻成 400 `invalid_fields`
（`services/ad.py` 的 `_call` 特判 `MissingRequiredParams`，照 `WriteNotConfirmed` → `confirm_required` 的先例）。

### 8.1 `item.searchV2` —— 缺 `tab` / `needTabs`

- 官方 payload 原文（@3408019）：`i = {...Le(F,['queryVal','sortField','sortType']), needTabs:!(y&&y.length>0), ...('itemId'===C?{itemId:e}:{searchKey:e}), campaignId:c, ...(t&&{sortField:t}), ...(n&&{sortType:n})}`；状态初值 @3406850 `KYe={pageNum:1,pageSize:10,tab:'platReco',queryVal:''}`。
- 复打发出的：`{"tab":"platReco","needTabs":true,"itemId":"1047949964912","pageNum":1,"pageSize":10}`（耗时 0.5s）
- 判读：**已解**。`tab`（+`needTabs`）是官方恒定带的键，缺了就是 `E_AD_BP_COMMON_PARAM_NULL`；协议层补 `tab='platReco'` / `needTabs=true`（`BODY_DEFAULTS`）。

### 8.2 `package.info` —— 缺 `entityType` / `entityList`，且 `catId` 必须是真实类目 ID

- 官方 payload 原文（@3677643）：`l({entityList:t, entityType:r, isGoodsSelectedTabChange:e}, f)`，其中 `t=a.map(e=>({entityId:e.itemId, catId:e.catId, price:e.price, areaTarget:false, startTime:e.publishTime}))`；@3396844 `m({entityList:e, entityType:p, isGoodsSelectedTabChange:!0})`；`entityType` 枚举 @1430758 是**数字** `GOOD=1` / `CLUE=2`。
- 复打发出的：`{"isGoodsSelectedTabChange":true,"entityType":1,"entityList":[{"entityId":"1047949964912","catId":"50023914","price":15.0,"areaTarget":false,"startTime":"2026-10-16 03:45:45"}]}`（耗时 0.22s）
- 三步 A/B（都真机打到）：
  1. 只发 `{promotionMode:'CUSTOMIZE'}`（第一轮）→ `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`；
  2. 补齐 `entityType=1` + `entityList` 但 `catId=0`（本地 `items` 表没有 `cat_id` 列，误取 0）→ `E_AD_BP_COMMON_SYSTEM_EXCEPTION::闲鱼太累了，请稍后再试～`；
  3. `catId` 取真实类目（`items.category_id=50023914`）→ 200 + `packageTargetVOList` / `bidTargets` / `recPromotionMode` / `supportOrder`。
- ⇒ **`catId` 必须是真实类目 ID**；给 0 时平台回的是 SYSTEM_EXCEPTION 而不是 PARAM_NULL，很容易误判成「参数够了、平台自己挂了」。

### 8.3 `customize.bid.estimates` —— 缺 `entityType` / `entityList`

- 官方 payload 原文：@3315969 `{entityType:materialType, entityList:getEntityList(), pcFirstRequest:true, campaignId?, noBid?}`；@3314544 `{entityType, entityList, noBid, pcFirstRequest:true}`；@3313962 `{entityType, entityList, bidTarget, bidPrice, noBid, pcFirstRequest:false, campaignId}`；`getEntityList()`（@3305143）每项 = `{entityId:item.itemId, catId:item.catId, price:item.price, areaTarget, startTime:item.publishTime}`。
- 复打发出的：`{"pcFirstRequest":true,"entityType":1,"entityList":[{"entityId":"1047949964912","catId":"50023914","price":15.0,"areaTarget":false,"startTime":"2026-10-16 03:45:45"}]}`（耗时 0.28s）
- 判读：**已解**。与 `package.info` 同形：`entityType` + `entityList` 必填，`catId=0` 同样回 SYSTEM_EXCEPTION；`pcFirstRequest` 协议层默认 `true`（出价那条走 `payload` 覆盖成 `false`）。

### 8.4 `campaign.log.query` —— 缺 `operatorType` / `operationType`（干净的 A/B）

- 官方 payload 原文（@1794033）：`o={startDate:u[0], endDate:u[1], pageNum:f.pageNum, pageSize:f.pageSize, campaignId:r?Number(r):0, operatorType:s.operatorType, operationType:s.operationType}`，初值 `s={operatorType:0, operationType:0}`，日期区间初值是**近 7 天**。
- 复打发出的：`{"operatorType":0,"operationType":0,"startDate":"20261009","endDate":"20261016","campaignId":0,"pageNum":1,"pageSize":5}`（耗时 0.15s）
- 同一账号、同一日期区间、同一 `campaignId=0`，**只差这两个枚举键**：不带 → `E_AD_BP_COMMON_SYSTEM_EXCEPTION`（`logs/probe60/ad_campaign_log.json`），带 0 → 200 + `data.result`。
- ⇒ 协议层 `BODY_DEFAULTS` 给 `campaign_log` 补 `operatorType=0` / `operationType=0`（官方初值就是 0）。
- 这条**推翻了** §1 之后 `test_ad_api.py::test_campaign_log_omits_unset_enums` 里「带 0 与不带不是一回事」的反向理解 —— 真机上恰恰是「不带就挂」。该测试断言的是**服务层**参数（服务层仍然不带，未动，不在本次写盘范围）；协议层补默认值对它是不可见的。

### 8.5 关键教训

1. **信封没问题，缺的是业务键**：四条全套 `requestJson`，先排除「我们平铺发了参数」这个假设。
2. **PARAM_NULL 与 SYSTEM_EXCEPTION 是两类问题**：前者是「键没给」，后者也可能是「键给了但值没用」（`catId=0`）。只看错误码会把值域问题误判成平台故障。
3. **官方恒定带的键要照抄，不要按需给**：`tab` / `needTabs` / `operatorType` / `operationType` / `pcFirstRequest` / `isGoodsSelectedTabChange` 官方每次调用都带。
4. **默认值只填官方自己填的**：`BODY_DEFAULTS` 每一条都能在 bundle 调用点找到出处（`tab='platReco'` @3406850、枚举 0 @1794033 初值、`pcFirstRequest:true` @3315969），没有一个是猜的业务参数。
5. **残包不发**：`BODY_REQUIRED` 在协议层拦下缺键 → 400 `invalid_fields`，而不是把 502 甩给调用方。
6. **便利键不许透传**：第一次复打时 `read()` 把 `entity_type` / `entity_list` 当成整包 payload 发给了平台（`request_json` 里两种命名同时出现），现在由 `_payload_only` 剔除。

### 8.6 仍未验证 / 存疑

- 只用**一个账号 + 一个商品**（`item_id=1047949964912`、`category_id=50023914`、`price=15.0`）验证；多类目、线索包（`entityType=2` / `CLUE`）、`bidPrice` 那条出价路径、`searchKey` 搜索路径都没打真机。
- `price` 口径（元 vs 分）没单独验证：`items.price=15.0`（元）平台接受了；官方 `e.price` 来自选品接口的 item 对象，两边是否同口径未确认。
- `campaign.log.query` 只验证了 `campaignId=0`（全部计划），指定某个 `campaignId` 未验证。
- 打包版 18400 是修复前的冻结代码、**未重打包**，这四条在打包版上仍是 502；要在打包版验证得重新打包（本次未做）。
- 全量测试套件没跑，只跑了 `backend/tests/test_xianyu_ad.py`（13 条 OK）与 `backend/tests/test_ad_api.py`（50 条 OK）。

### 8.7 复现

```bash
export PYTHONPATH=. PYTHONIOENCODING=utf-8
/c/Python310/python.exe /c/Users/Administrator/.dsh-tmp/ad_probe62.py   # 只读探针：4 条串行、间隔 2.2s
/c/Python310/python.exe backend/tests/test_xianyu_ad.py                 # 协议层形状 + 官方默认值 + 残包拦截
/c/Python310/python.exe backend/tests/test_ad_api.py                    # 服务/接口层（不在本次写盘范围）
```

## 9. 第 63 批：投放域 4 条只读接口（+1 条只登记不接）

- 新增落点：协议层 `backend/app/xianyu/ad.py`（`READ_APIS` 10 → **14** 条、`BODY_DEFAULTS` 补 `coupon_info`、`activity_detail` 的 `request_json=False`）、
  服务层 `backend/app/services/ad.py`（`READ_KINDS` 同步 14 条 + 4 个显式函数 + `read()` 分发分支 + `UNSUPPORTED`）、
  路由层 `backend/app/api/ad.py`（4 条 GET，见 §1）。
- 真机口径：账号 `2217970177348`（月狐兔_，`login_state=active`），**in-process 只读探针**（脚本 `%TEMP%\probe63_kurumi.py` 打协议层、`%TEMP%\probe63_svc.py` 打服务层），
  数据目录用 `sqlite3` 的 `Connection.backup()` **一致性拷贝**到 `%TEMP%\xywb_live63`（不碰生产库），串行、每条间隔 ≥2.5s、**无任何写操作**。
- 原始响应落盘：`logs/probe63/kurumi/ad_<label>.json`（协议层逐条）+ `ad_probe63_all.json` + `ad_service63.json`（`logs/` 不入仓）。

### 9.1 `coupon-info`（优惠券列表）

- 官方（@3246590 `sWe(e)`）：`yp({api:'mtop.alibaba.idle.ad.bp.coupon.info.query', v:'1.0', data:{requestJson:JSON.stringify(e)}, showErrorToast:!1, needLoginPC:!0})`；
  调用点 @3265220 的初值 `{pageNum:1,pageSize:10,couponType:0,source:'BONUS'}`，响应读 `data.total` / `data.result`。
- 本地：`GET .../coupons?page_num=&page_size=&coupon_type=&source=` + 可选 JSON body / `?payload=`（整包兜底，坏 JSON → 400 `invalid_fields`，body 优先）；
  `page_num`/`page_size` 走 `_page`、`coupon_type` 走 `_int`，非法 → 400 `invalid_fields`。
- **官方初值进了 `BODY_DEFAULTS`**：空 options → `{"pageNum":1,"pageSize":10,"couponType":0,"source":"BONUS"}`（**不是空对象**）。
- 真机 A/B（同账号、同接口，只差这一层）：
  - 官方初值 → **200**，`data = {"pageNum":1,"pageSize":10,"result":[],"total":0,"totalPage":0}`（0.33s；该账号券列表为空）；
  - 裸 `{}`（绕开默认值，`requestJson='{}'`）→ **502 `UNKNOWN_FAIL_CODE::系统开小差了，请稍候重试`**（0.06s）。
  - ⇒ **平台不接受空 requestJson**，默认值必须由协议层补；「空 options 是 200」是**本仓库路由**的语义（补齐官方初值后再打平台），不是平台对空对象的语义。
- 服务层真机：`coupon_info(ACC)` 与 `coupon_info(ACC, page_num='2', page_size='5', coupon_type='3')` 都 200，`data.pageNum/pageSize` 回显请求值（2 / 5）。
- 枚举（只登记、协议层不校验，`backend/app/xianyu/ad.py`）：`COUPON_TYPES{0 全部,2 礼金,3 红包,4 新人券,5 代理商礼金}`（@3243192）、
  `COUPON_SOURCES{BONUS 生效中,COUPON 未生效,HISTORY 已失效}`（@3243498）、
  `COUPON_STATUS{1 未生效,2 生效中,3 已用完,4 已作废,5 未生效已过期,6 生效中已过期,7 冻结}`（@3244737）。

### 9.2 `coupon-statistic`（优惠券汇总）

- 官方（@3246428 `aWe()`）：`data:{requestJson:JSON.stringify({})}` —— **恒发 `{}`、无入参**。
- 本地：`GET .../coupons/statistic`，无查询串、无 body（服务层 `coupon_statistic(account_id)` 不接 body，协议层 `coupon_statistic(client, body=None)` 只是签名兼容）。
- 真机：**200**，`data = {"activatedCouponAmountOfYuan":"0","expiringCouponAmountOfYuan":"0","specialCouponAmountOfYuan":"0"}`（0.07s）—— 三个金额字段都是**字符串** `"0"`。
- 路由顺序：`/coupons/statistic` 必须注册在 `/coupons` 之前（两条路径都显式写死，测试 `test_coupons_statistic_path_not_swallowed_by_coupons` 盯着）。

### 9.3 `account-card-v2`（账户卡片 V2，活动奖励页）

- 官方（@3268292 `UWe(e)`）：`yp({api:'mtop.alibaba.idle.ad.account.card.v2', v:'1.0', needLogin:!0, data:{requestJson:JSON.stringify(e)}})`；
  导出名 `requestAccountCard`，是路由 `home/service`（活动奖励页）的 loader export（@505082 / @3756651）。
- **载荷 `e` 在别的 chunk 里，本 bundle 取不到**：所以本地**不设** `BODY_DEFAULTS`，`GET .../account-card-v2` 的 body / `?payload=` 原样转发，**空对象照发**（空 body → `requestJson='{}'`）。
- 与既有 `client.py:137 AD_ACCOUNT_CARD_API='mtop.taobao.idle.ad.bp.account.card.query'`（`client.py:1079 ad_account_card`，载荷 `{scene:'PC_HOME'}`，`services/promotion.py:78/149` 用）**不是同一个接口**：
  一个是 `alibaba.idle`、一个是 `taobao.idle`；本批没有合并、也没有复用。
- 真机：空对象 → **502 `FAIL_SYS_UNAUTHORIZED_ENTRANCE::API访问入口未授权`**（0.06s，平台原话；mtop 层 HTTP 仍是 200，本仓库翻 502 `protocol_error`）。
  ⇒ 本账号打到这个接口时**平台拒绝访问入口**（是账号维度还是应用/入口维度未确认）；路由本身参数校验通过、请求确实发出去了。**载荷形状与成功响应都仍未验证**。
- 不确定项：官方包装的 `needLogin:!0` 与 `coupon.info` 的 `needLoginPC:!0` 是否等价未验证；`e` 的键名（`scene` 之类）未取到，留给能取到那个 chunk 的批次。

### 9.4 `activity-detail`（活动详情渲染）

- 官方（@3268518 `YWe()`）：`yp({api:'mtop.taobao.idle.ad.activity.detail.rendering', v:'1.0', needLogin:!0, data:{}})` —— **无入参、不套 `requestJson`**（与 `message.unread.info` 同形）；调用点 @3281232 / @3723161。
- 本地：`GET .../activity-detail`，无查询串、无 body；协议层发 `data={}`，`READ_APIS['activity_detail']['request_json'] = False`。
- 真机：**502 `NO_APPLY_ACTIVITY_PERMISSION::活动报名时间已结束或者您不在本期活动名单内`**（0.06s，平台原话）。
  ⇒ 这是**平台侧业务拒绝**（该账号不在本期活动名单 / 报名时间已结束），不是参数错：接口本身不需要参数，换有资格的账号就会走通。
- 不确定项：成功响应的字段名（活动详情结构）本账号拿不到，未验证。

### 9.5 `mtop.taobao.idle.ad.bp.data.download` —— 偏写，只登记不接

- bundle @3115691 `XFe(e)`：`yp({api:'mtop.taobao.idle.ad.bp.data.download', data:{requestJson:JSON.stringify(e)}, showErrorToast:!1})`（**包装没写 `v`**）；
  三个调用点 @3118414 / @3174876 / @3236322 全在「下载报表」按钮里，载荷 `{targetType:'CAMPAIGN'|'MEMBER'|'ITEM', startDate:'YYYYMMDD', endDate:'YYYYMMDD', includeEndDate:true, groupByDay:bool, taskId?}`。
- 判据：响应读 `data.downloadUrl` / `data.taskId` / `data.status`，`status=FAILED` 报错；没有 `downloadUrl` 时官方拿 `taskId` **每 60s 轮询** —— 它**创建的是服务端异步导出任务**（有副作用 / 落库），不是读一个现成文件。
- 因此**只登记、不接**：协议层常量 `BP_DATA_DOWNLOAD_API`（`backend/app/xianyu/ad.py`，**不进 `READ_APIS`**，测试 `test_read_apis_table_covers_every_constant` 断言它不在表里）
  + 服务层 `UNSUPPORTED`（`backend/app/services/ad.py`），并在 `GET /apis` 的 `catalog().unsupported = [{api, reason}]` 里透出。
- 真机**未打**（偏写接口不做试打，与本项目「写接口不试打」口径一致）。

### 9.6 第 63 批测试

```bash
XIANYU_APP_DIR=$TEMP/xywb_kurumi63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_xianyu_ad.py
XIANYU_APP_DIR=$TEMP/xywb_kurumi63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_ad_api.py
```

- `test_xianyu_ad.py`：**15 条 OK**（新增 `test_activity_detail_sends_empty_data_without_request_json`、`test_coupon_info_fills_official_filter_state`；
  `test_read_apis_table_covers_every_constant` 期望列表加 4 条、`no_request_json={'message_unread','activity_detail'}`、并断言 `BP_DATA_DOWNLOAD_API` 不在 `READ_APIS` 里）。
- `test_ad_api.py`：**63 条 OK**（新增 9 条 HTTP/服务层用例 + 4 条端到端；`test_routes_are_read_only` 路径清单加 `coupons` / `coupons/statistic` / `account-card-v2` / `activity-detail`，`test_apis_catalog` 断言 `unsupported`）。
  其中 `test_coupon_info_empty_options_is_200` 锁住「空 options 是 200 不是失败」（服务层参数为空、由 `BODY_DEFAULTS` 补官方初值）。
- 回归（同批全 OK）：`test_contract_routes`(3) / `test_write_routes_have_gates`(4) / `test_confirm_gate_single_point`(9) / `test_protocol_wiring`(16) / `test_agents_tools_workbench`(32) / `test_mcp_routes_match_openapi`(7)。

### 9.7 仍未验证 / 存疑（第 63 批）

- `account_card_v2` 的**载荷形状**与成功响应（平台未授权，见 §9.3）；`activity_detail` 的成功响应字段（账号不在活动名单，见 §9.4）。
- `coupon_info` 只验证了空列表账号（`total=0`）：`result` 里单条券的字段名、`couponType` 各枚举、`source` 各枚举都未在真机见到。
- `coupon_statistic` 三个金额字段全是 `"0"`，**非零金额的格式（元/分、小数位）未验证**。
- 只用**一个账号**验证；`logs/probe63/` 未入库；打包版**未重打包**（禁改禁跑口径），这 4 条在打包版上不存在。
