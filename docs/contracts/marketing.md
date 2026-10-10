# 营销（官方 `/marketing` 页）只读契约

> 本文件是 `backend/app/xianyu/marketing.py` / `backend/app/services/marketing.py` /
> `backend/app/api/marketing.py` 的对外契约，**只覆盖只读能力**（写接口只登记、不实现）。
>
> 证据来源（2026-10-11，全部可复核）：
> - **官方营销微应用 bundle**（静态 CDN，零风控）：
>   `https://g.alicdn.com/idle-pc/seller-marketing/0.0.1/` 的 `marketing-main.js`，
>   **3,516,103 字符的单行压缩 JS**，本地落盘
>   `C:\Users\Administrator\.dsh-tmp\adapp\marketing-main.js`。
>   **行号无意义**（整份是一个单行），本文件所有证据一律写成
>   **「字符偏移 + 复现用字符串搜索」**：偏移是 `IndexOf` 首次命中的字符下标，
>   复现方式是用引号里的字面串直接搜该文件。
> - 仓库侧客户端：`backend/app/xianyu/client.py:318`
>   `async def mtop(self, api, data=None, v='1.0', ...)`；
>   站点 CODE 头常量 `SECURITY_SITE_BIZ_CODE = 'COMMONPRO'`（`backend/app/xianyu/client.py:146`）。
> - **真机未验证**：本文档四个只读接口的响应结构**没有真机样本**，全部是从 bundle 里
>   **压缩后的消费代码**反推的（「页面怎么读」），不是从响应样本来的（「服务端怎么答」）。
>   置信度因此**全部只能给「中」**，见 §8。

## 0. 一句话结论

营销页有 **4 个只读接口**，全部走同一个公共包装函数，`data` 里**直传业务字段**
（不是 `requestJson`）。四个接口**没有一个**有 TS 类型、mock 或注释，
所以按判据四个接口置信度**全部只能给「中」，没有一个够得上「高」**。

| # | mtop 接口名 | 用途 | 偏移 | 置信度 |
| --- | --- | --- | --- | --- |
| 1 | `mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list` | 单商品一屏指标 | 1436561 | **中** |
| 2 | `mtop.taobao.idle.bpro.marketing.info.enhance.query.pc` | 任务分页 / 商品分页 / 流量任务信息 | 1433790 | **中** |
| 3 | `mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query` | 价格竞争力（涨幅提示文案） | 1434255 | **中** |
| 4 | `mtop.taobao.idle.bpro.marketing.operation.record.list.pc` | 操作记录（全量拉取） | 1436108 | 参数**中** / 响应条目内部字段**低** |

## 1. 口径（四条硬性口径，缺一不可）

1. **平台没返回的指标一律 `null`**（前端显示「-」），**禁止用 0 顶替**——
   画成 0 用户会以为「真的一个都没有」；**平台真的回了 0 必须保 0**（`0` 与 `null`
   是两件事）。
2. **无权限 ≠ 掉线**：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 这类
   「全部接口都失败且都是权限错」时给 **`200` + 一屏 `null` + 中文 warning**，
   **绝不**写 `need_relogin`。只有**登录态故障**（`SessionExpired` / `SESSION_EXPIRED`）
   才 `409 verification_required` 并回写 `need_relogin`；**链路 / 签名故障**给
   `502 protocol_error`。
3. **全部只读**：任何 `POST` / `PUT` / `PATCH` / `DELETE` 一律 **`405`**。
4. **缺 Cookie 时零平台调用**，直接 `409`。

## 2. 公共包装函数（偏移 1433790 附近同一段）

四个接口共用同一个包装器，形态**逐字**如下（偏移 1433790）：

```js
{
  v: "1.0",
  data: {},                    // 业务字段直接放这里
  type: "POST",
  appKey: "34839810",
  accountSite: "xianyu",
  dataType: "json",
  timeout: 20000,
  needLogin: false,
  sessionOption: "AutoLoginOnly",
  ext_headers: { idle_site_biz_code, idle_user_group_member_id }
}
```

→ `window.lib.mtop.request(c)`。

- **业务字段直接放 `data`，不是 `requestJson`**：与 `backend/app/xianyu/shop.py:17`
  同族，**不要**与 `backend/app/xianyu/client.py:933` 的 `idle.ad.*` 那套混。
- 四个接口的 `type` 都是 `POST`（**mtop 层面的 POST**，与 §1.3 的「HTTP 只读」不冲突：
  对外 HTTP 一律 GET）。
- 接口 2 / 3 另有 `needLoginPC: true`（见各自小节）；接口 1 / 4 的 `data` 里
  **看不到登录态**（见 §9 未确认项 9）。
- ⚠ **`needLogin:false` + `sessionOption:"AutoLoginOnly"` 不等于免登录**，
  不能据此下「不需要登录」的结论。

### 2.1 踩坑记录：两个写错的接口名（bundle 里 -1，不存在）

| 错误写法（bundle 里**不存在**） | 正确写法 |
| --- | --- |
| `mtop.taobao.idle.item.price.competitiveness.info.query` | `mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query` |
| `mtop.taobao.idle.operation.record.list.pc` | `mtop.taobao.idle.bpro.marketing.operation.record.list.pc` |

两个短名都**漏了 `bpro.marketing.` 中缀**，在该 bundle 里搜索命中数为 **-1**（即不存在）。
真机这么打会直接回 `FAIL_SYS_API_NOT_FOUNDED`。

## 3. 已实现（只读）

### 3.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/marketing/{account_id}/items?item_id=&date_type=&date_range=` | 单商品一屏指标（接口 1） |
| GET | `/api/marketing/{account_id}/tasks?biz_scene=&query_scene=&page=&size=&item_ids=` | 任务 / 商品分页（接口 2） |
| GET | `/api/marketing/{account_id}/price-competitiveness?item_id=&biz_scene=&sku_prices=&commission_rate=` | 价格竞争力（接口 3） |
| GET | `/api/marketing/{account_id}/records?task_id=&biz_scene=&query_scene=` | 操作记录（接口 4） |

- **路径名沿用首版**（HEAD 的 MCP 路由表 `mcp_server/client.py:303-318` 与前端
  `frontend/src/pages/Marketing.tsx` 都按这四条写死，改名会打断它们）；订正的是
  **参数、契约键与响应形状**。⚠ 因此 `/items` 是**单商品**指标（必填 `item_id`，
  **不吃 `page`/`size`**），`/records` 也**不吃分页**（官方接口 4 无分页无时间范围）——
  两处消费方若还按首版传 `page`/`size`，会拿到 400，需各自订正。

- `APIRouter(prefix='/api/marketing')`，四条**全是 GET**。
- 四个端点都要求登录（`Depends(current_user)`），响应走全局信封
  `{success: true, data: {...}}` / `{success: false, message, code, login_state?}`；
  状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、
  `verification_required`→409（登录态过期，附 `login_state: "need_relogin"`）、
  `protocol_error`→502、`xianyu_not_ready`→503。
- **没有任何写方法**：POST/PUT/PATCH/DELETE 在这四条路径上一律 **405**。
- ⚠ **路由尚未挂进 `backend/app/main.py` 的 `ROUTE_MODULES`**：由 Lead 追加
  `'marketing'`。挂载之前这四条路径在真实 app 里**不存在**。

### 3.2 仓库侧分层

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 协议层 | `backend/app/xianyu/marketing.py` | 四个**模块级 async 函数**：`item_metrics(client, item_id, date_type, date_range)` / `tasks(client, biz_scene, query_scene, page_num, page_size, item_ids)` / `competitiveness(client, biz_scene, item_id, sku_prices, commission_rate)` / `operation_records(client, task_id, biz_scene, query_scene)` |
| 服务层 | `backend/app/services/marketing.py` | 编排、容错、口径落地（`null` 不顶 0、无权限不写 `need_relogin`） |
| 路由层 | `backend/app/api/marketing.py` | `APIRouter(prefix='/api/marketing')`，四条 GET（见 §3.1） |

协议层用「模块级 async 函数 + 调用方传 `client`」，而不是往
`backend/app/xianyu/client.py` 加方法——与数据组 / 小铺组同族做法。

### 3.3 服务层的对外形状与校验（2026-10-11 订正后）

对外 `kind`（= 路由末段）→ 协议函数名：`items`→`item_metrics`、
`tasks`→`tasks`、`price-competitiveness`→`competitiveness`、
`records`→`operation_records`。（服务层 `KIND_ALIASES` 另收
`item-metrics` / `operation-records` 这两个订正期间短暂用过的长名。）

**成功信封**（`data` 里除下列字段外，还回显当次请求的参数）：

| 字段 | 口径 |
| --- | --- |
| `account_id` / `kind` / `source`（恒 `"platform"`） / `fetched_at` | 与其它复刻页同款 |
| `metrics` | 契约键 → 数值 / `null`（`items`、`tasks`） |
| `texts` | 契约键 → 字符串 / `null`（`price-competitiveness` 的 `traffic_boost_text`） |
| `flags` | **原样透传**（`tasks` 的 `next_page`：可能是 bool 也可能是页码，未验证） |
| `items` / `tasks` / `records` | 列表，**空数组与「没拿到」都归一成 `null`** |
| `page_kind` | `"task"` 或 `"item"`（`tasks` 二选一命中了哪个 `pageResult`） |
| `user_task_info` | `result.userTrafficTaskInfo` 原样 |
| `warnings` | 逐接口中文告警；**无权限**单独一条：「平台拒绝访问「营销商品数据」接口：这个账号没有「营销」业务权限…」 |
| `unrecognized_keys` | 响应里像指标却一个别名都没命中的键（仅提示） |

**本地 400 校验**（参数形态置信度只有「中」，错的挡在本地比让平台猜更好）：

- `item_id` / `task_id` 必填；
- `date_type` ∈ `DATE_TYPES`；`customDate` 时 `date_range` 必须是 `YYYYMMDD|YYYYMMDD`，
  非 `customDate` 时**不许**传 `date_range`；
- `biz_scene` ∈ `BIZ_SCENES`（2 值）、`query_scene` ∈ `QUERY_SCENES`（15 值）；
- `page` 从 1 起、`size` 限 1~50（缺省 **10**，对齐官方 `pageSize`）；
- `sku_prices` 必须是 **JSON 对象**；`commission_rate` 只在 `biz_scene=TRAFFIC_ORDER_BOOST` 时允许。

**502 的口径**：协议层不给结构化数据、`metrics`/`texts` 为空 dict、或**缺任一契约键**
→ `protocol_error`。`records` 没有可校验的指标键（条目内部字段零证据），
改为钉住「`records` 这个键一定在」。

**测试**（`backend/tests/test_marketing_api.py`，**68 用例**）分三组：
`MarketingApiTest`（假协议层：信封 / 只读 405 / 校验 / 三条硬口径）、
`MarketingEndToEndTest`（**只换 client、真协议层参与**：信封剥离、`itmId` 逐字键名、
真 0 保留、无权限 200+warning、链路错 502）、
`MarketingProtocolTest`（纯函数：`find_number` 忽略 bool / 保 0 / 读嵌套，
`find_list` 空数组→`None`，参数构造逐字键名，枚举全集，`_collect` 容错分层）。


## 4. 接口 1：`mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list`

- **偏移 1436561**，`v=1.0` / **POST**。
- **参数**（`data` 直传 3 键）：

| 参数 | 类型 | 口径 |
| --- | --- | --- |
| `itmId` | string | 商品 id。**注意拼写少一个 `e`**（证据偏移 1775069，**唯一 1 个调用点，单点证据**） |
| `dateType` | string | 默认 `"recent1d"`；另有 `"recent7d"` / `"recent30d"` / `"customDate"` |
| `dateRange` | string | 预设时 `""`；`customDate` 时 `"YYYYMMDD\|YYYYMMDD"` |

- 自定义区间 UI **只允许「今天-90 天 ~ 今天」**。
- **响应**：`code === "success"` 时取 **`data.list[0]`** 作为一屏指标对象。
  信封形状 `{"code":"success","data":{...}}`，与 `backend/app/xianyu/datacenter.py:18`
  同款。
- **指标键全集**（就是字段名）：

| 平台字段 | 仓库字段 | 含义 |
| --- | --- | --- |
| `showPv` | `show_pv` | 曝光次数 |
| `ipv` | `ipv` | 商品访问次数 |
| `showUv` | `show_uv` | 曝光人数 |
| `ipvUv` | `ipv_uv` | 访问人数 |
| `payQty` | `pay_qty` | 支付件数 |
| `payAmt` | `pay_amt` | 支付金额 |
| `netPayAmt` | `net_pay_amt` | 净支付金额 |
| `estPayFee` | `est_pay_fee` | 预估支付费用 |

- **置信度：中**（有调用点 + 响应信封 + 指标键枚举表，但**无 TS 类型 / 无 mock**）。

## 5. 接口 2：`mtop.taobao.idle.bpro.marketing.info.enhance.query.pc`

- **偏移 1433790**，`v=1.0` / **POST**，**`needLoginPC: true`**。
- **参数**：

| 参数 | 类型 | 口径 |
| --- | --- | --- |
| `bizScene` | string | 只有 **2 个值**：`TRAFFIC_DRIVEN_REPRICING` / `TRAFFIC_ORDER_BOOST` |
| `queryScene` | string | 全集见下表 |
| `pageNum` | number | 默认 `1`（**三处独立佐证**） |
| `pageSize` | number | 默认 `10`（**三处独立佐证**） |
| `itemIdListStr` | string（可选） | **JSON 字符串**，商品 id 数组（证据偏移 2142992） |

- 共 **8 个调用点**。
- **`queryScene` 全集（15 个）**：`DEFAULT`、`OPPORTUNITY_ITEM_RECOMMEND`、
  `DISCOUNT_BOOST_SET`、`DISCOUNT_BOOSTING_TASK`、`DISCOUNT_BOOSTED_TASK`、
  `ORDER_BOOST_FIRST_PAGE_BOOSTING`、`ORDER_BOOST_FIRST_PAGE_PAUSE`、
  `ORDER_BOOST_SECOND_PAGE_BOOSTING`、`ORDER_BOOST_SECOND_PAGE_CAN_SIGN_UP`、
  `ORDER_BOOST_SET`、`ORDER_BOOSTING_SET`、`ORDER_BOOSTING_SET_PAUSE`、
  `ADMITTANCE_CHECK`、`ORDER_BOOST_OPERATION_RECORD`、
  `ORDER_BOOST_SECOND_PAGE_CAN_SIGN_UP_FILTER`。
- **响应**：**顶层 `result`**，二选一：

| 键 | 含义 | 偏移 |
| --- | --- | --- |
| `result.taskPageResult` | 答「任务分页」 | 1436926 |
| `result.itemPageResult` | 答「商品分页」 | 1437028 |
| `result.userTrafficTaskInfo` | 用户流量任务信息 | 1437354 |

- 每个 `pageResult` 上有：`result`（数组）、`total`（总数）、`nextPage`（「还有下一页」，
  偏移 1451620）。
- **单条 item 被页面展开成**：
  `{...basicInfo, itemTask, taskTips, canApplyAgain, commissionSettingDTO, priceCompetitivenessInfo, validationInfo, trackParams}`；
  `basicInfo` 内含 `skuList` / `itemId` / `price.priceSet` /
  `skuPriceCompetitivenessInfo.limitPrice.priceYuan`。
- **置信度：中**。

## 6. 接口 3：`mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query`

- **偏移 1434255**，`v=1.0` / **POST**，**`needLoginPC: true`**。
- **参数**（**5 个调用点**，全部 4 键，第 4 键可选）：

| 参数 | 类型 | 口径 |
| --- | --- | --- |
| `bizScene` | string | 同上 **2 个值**：`TRAFFIC_DRIVEN_REPRICING` / `TRAFFIC_ORDER_BOOST` |
| `itemId` | string | 商品 id |
| `currentSkuPriceMapStr` | string | **JSON 字符串** `{"<skuId>":<priceYuan>}`。键名 `skuId` 是**推断**，**未验证**（证据偏移 1579913） |
| `currentCommissionRate` | number（可选） | 成单宝服务费率；**只在 `TRAFFIC_ORDER_BOOST` 出现** |

- 调用全部有 **500ms 防抖**。
- **响应**：**顶层 `result.trafficBoostStr`**（string，涨幅提示文案，偏移 1577972）。
- **置信度：中**。

## 7. 接口 4：`mtop.taobao.idle.bpro.marketing.operation.record.list.pc`

- **偏移 1436108**，`v=1.0` / **POST**。
- **参数**（全 bundle **唯一 1 个调用点**，3 键，**无分页、无时间范围**）：

| 参数 | 值 |
| --- | --- |
| `taskId` | 任务 id（**整块由 `taskId` 一次性拉全量**） |
| `bizScene` | `"TRAFFIC_ORDER_BOOST"` |
| `queryScene` | `"ORDER_BOOST_OPERATION_RECORD"`（偏移 1439293） |

- **响应**：**顶层 `result.result`**（数组；**空数组走空态**）。
- **置信度**：**参数 = 中**（唯一调用点、无类型）；**响应条目内部字段 = 低**
  （**未找到任何证据**，必须原样写成未确认，见 §9 未确认项 1）。

## 8. 只登记、不实现的写接口

以下两个写接口**只登记、不实现**（没有任何调用点，路由层不注册对应方法）。

### 8.1 `mtop.taobao.idle.bpro.marketing.task.operate.pc`

- `needLoginPC: true`，`data: {...e, sourceId}`，
  **`sourceId` = hash query 的 `sourceId` \|\| `"default"`**。
- 已知 **2 种** `data` 形态：

| 形态 | 字段 |
| --- | --- |
| 调价 | `{bizScene:"TRAFFIC_ORDER_BOOST", itemId, operation:"TOB_ADJUST_PRICE", skuOperateRequestStr: JSON.stringify([{skuId, adjustPrice}])}` |
| 调佣金 | `{bizScene:"TRAFFIC_ORDER_BOOST", itemId, operation:"TOB_ADJUST_COMMISSION", takeRate}` |

- **响应**：`result.success`（bool）/ `result.errorMsg`。

### 8.2 `mtop.taobao.idle.bpro.marketing.task.batch.operate.pc`

- `needLoginPC: true`，`data: {...e, sourceId}`。
- 已知 **2 种**形态：

| 形态 | 字段 |
| --- | --- |
| 成单宝起任务 | `{bizScene:"TRAFFIC_ORDER_BOOST", operation:"TOB_START_TASK", taskOperateRequestStr:"<JSON string>"}` |
| 降价起任务 | `{bizScene:"TRAFFIC_DRIVEN_REPRICING", operation:"TDR_START_TASK", taskOperateRequestStr:"<JSON string>"}` |

- **响应**：`result.{successCount, failureCount, itemOperateResponses}`。

### 8.3 `operation` 字符串枚举 + 孪生数值枚举

| 字符串 | 数值 |
| --- | --- |
| `TOB_START_TASK` | `1000` |
| `TOB_ADJUST_PRICE` | `1001` |
| `TOB_ADJUST_COMMISSION` | `1002` |
| `TOB_PAUSE_TASK` | `1003` |
| `TOB_RESUME_TASK` | `1004` |
| `TOB_ABORT_TASK` | `1005` |
| `TOB_SCHEDULER_PAUSE` | `1006` |
| `TOB_SCHEDULER_COMMISSION` | `1007` |
| `TDR_START_TASK` | `2000` |
| `TDR_ADJUST_PRICE` | `2001` |

## 9. 字段映射（平台 → 仓库，逐字）

| 平台字段 | 仓库字段 | 备注 |
| --- | --- | --- |
| `showPv` | `show_pv` | |
| `ipv` | `ipv` | 同名 |
| `showUv` | `show_uv` | |
| `ipvUv` | `ipv_uv` | |
| `payQty` | `pay_qty` | |
| `payAmt` | `pay_amt` | |
| `netPayAmt` | `net_pay_amt` | |
| `estPayFee` | `est_pay_fee` | |
| `total` | `total` | |
| `nextPage` | `next_page` | **原样透传**，可能是 bool 也可能是页码，**未验证** |
| `trafficBoostStr` | `traffic_boost_text` | |

## 10. 置信度与判据

| 接口 | 置信度 | 判据 |
| --- | --- | --- |
| 1 `...user.market.item.list` | **中** | 有调用点 + 响应信封 + 指标键枚举表；**无 TS 类型 / 无 mock** |
| 2 `...info.enhance.query.pc` | **中** | 8 个调用点 + 分页键三处佐证；**无 TS 类型 / 无 mock** |
| 3 `...price.competitiveness.info.query` | **中** | 5 个调用点 + 响应键；**无 TS 类型 / 无 mock** |
| 4 `...operation.record.list.pc` | 参数**中** / 条目内部字段**低** | 唯一调用点、无类型；条目内部字段零证据 |

**判据**：**没有任一接口有 TS 类型 / mock / 注释** → 按判据四个接口置信度
**全部只能给「中」，没有一个够得上「高」**。

### 10.1 已复核的字符串证据（偏移 → 复现用搜索串）

用字符串 `IndexOf` 在该 bundle 里**逐个亲自复核过**下列名字/字段**全部存在**
（各 1 次，除注明外）：

| 偏移 | 字符串 | 归属 |
| --- | --- | --- |
| 1433790 | 接口 2 名 | 接口 2 |
| 1434255 | 接口 3 名 | 接口 3 |
| 1436108 | 接口 4 名 | 接口 4 |
| 1436561 | 接口 1 名 | 接口 1 |
| 1436926 | `taskPageResult` | 接口 2 响应 |
| 1437028 | `itemPageResult` | 接口 2 响应 |
| 1437354 | `userTrafficTaskInfo` | 接口 2 响应 |
| 1451620 | `nextPage` | 接口 2 响应 |
| 1577972 | `trafficBoostStr` | 接口 3 响应 |
| 1579913 | `currentSkuPriceMapStr` | 接口 3 入参 |
| 1775069 | `itmId` | 接口 1 入参（唯一 1 个调用点，单点证据） |
| 1771559 | `recent1d` | 接口 1 入参 |
| 1771710 | `showPv` | 接口 1 响应 |
| 1772458 | `ipvUv` | 接口 1 响应 |
| 1773694 | `netPayAmt` | 接口 1 响应 |
| 1774091 | `estPayFee` | 接口 1 响应 |
| 2142992 | `itemIdListStr` | 接口 2 入参 |
| 1439293 | `ORDER_BOOST_OPERATION_RECORD` | 接口 4 入参 |

## 11. 未确认项（原样保留，不要试图补全）

1. **接口 4 响应条目的内部字段名**（时间 / 操作类型 / 操作人等）——**零证据**。
2. **`itmId` 的确切语义**：判定是商品 id，但**不排除是别的**。
3. **接口 1 后端还接受哪些 `dateType`**（除 `recent1d` / `recent7d` / `recent30d` /
   `customDate` 之外）。
4. **`currentSkuPriceMapStr` 的键名**（推断 `skuId`）**与值格式**
   （元 vs 分、字符串 vs 数字，**未验 `iE`**）。
5. **`Yk` 枚举里 `DEFAULT` / `DISCOUNT_BOOST_SET` / `ADMITTANCE_CHECK` /
   `ORDER_BOOST_SECOND_PAGE_*` 是否也用于接口 2**。
6. **仓库 `backend/app/xianyu/mtop.py` 的 appKey 是否就是 `34839810`**。
7. **没有任一接口有 TS 类型 / mock / 注释** → 按判据四个接口置信度**全部只能给「中」，
   没有一个够得上「高」**。
8. **所有响应字段都是从压缩后的消费代码反推的**（「页面怎么读」），
   **不是从响应样本来的**（「服务端怎么答」）——字段可能存在于兄弟位置却被解构丢失。
9. **接口 1、4 的 `data` 里看不到登录态**，但包装器强制 `needLogin:false` +
   `sessionOption:"AutoLoginOnly"`，**这不等于免登录**，不能据此下结论。

## 12. 与旧版本文档的差异（改动记录）

本文件替换了此前的同名草稿，**旧草稿与本轮已核实证据不一致**，逐条列明以免混用：

| 旧草稿（已作废） | 本轮证据 |
| --- | --- |
| 路由名 | **不改**：四条路径沿用旧草稿（HEAD 的 MCP 路由表与前端页已写死）；只订正参数与契约键 |
| 路由参数 | `/items` 改成**单商品**指标（必填 `item_id`，不吃分页）、`/records` 改成必填 `task_id`（官方接口 4 无分页） |
| `page` 默认 1 / `size` 默认 20（限 1–50） | 平台侧 `pageNum` 默认 1 / `pageSize` 默认 10 |
| 接口名出现次数记作「1 次」 | 接口 2 有 **8 个**调用点、接口 3 有 **5 个**、接口 1 与接口 4 各 **1 个** |
| 契约键 `item_count` / `running_count` / `total_count` / `low_count` / `high_count` / `avg_score` / `record_count` | 本轮证据里**没有**这些键；接口 1 的指标键是 §4 的 8 个 |
| 证据写「按字面串 grep 的出现次数」，无偏移 | 一律 **字符偏移 + 复现用字符串搜索**（行号无意义） |
| 未确认项 3 条（响应无真机样本等） | 未确认项 **9 条**（见 §11） |
