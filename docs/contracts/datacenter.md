# 数据组（数据总览 / 商品数据 / 粉丝数据 / 客服数据）只读契约

> 本文件是 `backend/app/xianyu/datacenter.py` / `backend/app/services/datacenter.py` /
> `backend/app/api/datacenter.py` 的对外契约，**只覆盖只读能力**。
>
> 证据来源（2026-10-10，账号 `2217970177348`，全部可复核）：
> - **真机只读探针**（2 轮，共 12 次直连 mtop，2.5s 间隔，命中风控词立即中止）：
>   `logs/seller_discover/datacenter_probe.json`、`logs/seller_discover/datacenter_probe2.json`；
>   脚本 `logs/seller_discover/probe_datacenter.py`、`logs/seller_discover/probe_datacenter2.py`。
> - **官方数据微应用 bundle**（静态 CDN，零风控）：
>   `https://g.alicdn.com/idle-pc/idle-seller-data/0.0.11/js/main.js`（4,168,486 字节），
>   本地副本 `logs/seller_discover/datacenter_index.html` + `datacenter_main.js`，
>   从里面抽出 56 个 mtop token（调用形态 `xge({api, v:'1.0', data:{...}})`），
>   其中 **46 个是真 API 名**（`mtop.alibaba.*` 44 + `mtop.taobao.*` 2；datacompass 族 39），
>   另 10 个是 `mtop.config.*` / `mtop.request` 这类模块路径。
> - **契约测试**：`backend/tests/test_datacenter_api.py`（195 用例，样本即真机原文形状）。
>
> 商品数据页（`/seller-data/commodity`，2026-10-13 加）的额外证据：官方菜单树
> `logs/seller_discover/menu.json` 里这一页是**我们最后一个没复刻的官方叶子页**；
> 接口与列定义来自同一份官方 bundle 的静态提取（零平台请求，见 §3.1）。

## 0. 一句话结论

`get.begin.time` **能读**；`datacompass` 的 `singleuser` / `fans` / `cs` 族对**这个账号
一律无权限**——所以三个页面如实返回 `null`（前端显示「-」），**不是**本项目的 bug：

| 能力 | 真机结果 | 结论 |
| --- | --- | --- |
| 数据起始日 `get.begin.time` | `{"code":"success","data":"20260201","msg":"成功"}` | 可读 |
| 数据总览 `singleuser.browse.summary` | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | **无权限** |
| 数据总览 `singleuser.item.summary` | 同上 | **无权限** |
| 粉丝数据 `fans.summary` | 同上（不给 `dateType` 也一样） | **无权限** |
| 客服数据 `cs.overview.summary` | 不给 `dateType` → `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数dateType`；给了 → 无权限 | **`dateType` 必填 + 无权限** |

两条口径，缺一不可：

1. **平台没给的指标是 `null`，不是 0**。画成 0 用户会以为「昨天真的没人看」；
   只有平台**真的**返回 0 时才原样保留 0（`0` 与 `null` 是两件事，测试各锁一条）。
2. **`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 不是掉线**：这类失败**不写** `need_relogin`，
   用户不会被逼着重新扫码。只有 `SessionExpiredError`（`FAIL_SYS_SESSION_EXPIRED`）
   才回写 `need_relogin` 并给 409。

## 1. 已实现（只读）

### 1.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/datacenter/{account_id}/overview` | 数据总览：曝光 / 访问 / 浏览 / 在线商品 / 询单 |
| GET | `/api/datacenter/{account_id}/commodity` | 商品数据：一行一个商品 + 平台给的指标列（官方「商品数据」页） |
| GET | `/api/datacenter/{account_id}/fans` | 粉丝数据：总数 / 新增 / 流失 / 活跃 / 订单 / 买家 + 画像洞察 |
| GET | `/api/datacenter/{account_id}/customer-service` | 客服数据：询单人数 / 询单量 / 3 分钟响应率 / 平均响应时长 / 未回复 / 满意度 |
| GET | `/api/datacenter/{account_id}/card/{kind}` | 经营卡片：`kind` ∈ `trade` / `seller` / `repurchase` / `refund`（官方数据总览页那四张卡） |
| GET | `/api/datacenter/{account_id}/cs/{kind}` | 客服明细表：`kind` ∈ `detail`（客服明细）/ `diversion`（订单分流明细）/ `evaluation`（客户评价明细） |

- 六个端点都要求登录（`Depends(current_user)`），响应走全局信封
  `{success: true, data: {...}}` / `{success: false, message, code, login_state?}`。
- Query：`start`、`end`，都是 `YYYYMMDD`；缺省为「今天往前 7 天」（`start=今天-6`、`end=今天`）。
  非法值（`2026-10-04` / 开始晚于结束 / 位数不对 / 非数字）一律 `400 invalid_fields`，
  且**一次平台都不打**。
- 状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、
  `verification_required`→409（登录态过期，附 `login_state: "need_relogin"`）、
  `xianyu_not_ready`→503（协议层未就绪）、`protocol_error`→502。
- **没有任何写方法**：POST/PUT/PATCH/DELETE 一律 405（`test_routes_are_read_only` 锁死）。
- 挂载：`backend/app/main.py` 的 `ROUTE_MODULES` 已含 `datacenter`（Lead 加的行）。

### 1.2 每个响应都带

`account_id`、`kind`（`overview` / `fans` / `customer_service`）、
`period: {from, to}`（实际请求区间）、`metrics`（见 §2）、
`begin_time`（平台给的数据起始日，认不出为 `null`）、
`real_date_range`（平台回的**真实**区间，`extendInfo.realDateRange`，认不出为 `null`）、
`warnings: []`（逐接口失败的人话解释）、`source: "platform"`、
`fetched_at`（`YYYY-MM-DD HH:MM:SS`，UTC）。

`kind == 'fans'` 时另有 `insights`（旧字段：没有名字的画像列表，认不出为 `null`）与
`insights_breakdown`（**第 51 批**：性别 / 粉丝类型 / 省份 / 八大人群四组具名列表）：

```jsonc
{
  "groups": {
    "gender":     {"key": "gender",     "title": "性别分布", "platform_key": "buyerGenderList",
                   "items": [{"name": "男", "ratio": 0.38, "ratio_text": null, "raw_label": "M", "raw": {}}]},
    "fans_type":  {"key": "fans_type",  "title": "粉丝类型", "platform_key": "fansTypeList",   "items": []},
    "province":   {"key": "province",   "title": "省份分布", "platform_key": "buyerProvinceList", "items": []},
    "eight_type": {"key": "eight_type", "title": "八大人群", "platform_key": "buyerEightTpList",  "items": []}
  },
  "raw_keys": ["buyerGenderList", "fansTypeList", "buyerProvinceList", "buyerEightTpList"]
}
```

- **一组都认不出就是 `null`**（不是 `{}`、不是空数组），并附中文 warning
  （权限不足时**只留权限 warning**，不叠这一句）。
- `name` 是界面用的中文名（性别 `M/F` → 男/女且男在前；粉丝类型去掉 `A_` 前缀并按 A→G 排；
  八大人群按官方顺序表，表外的排最后）；`raw_label` 是平台原值。
- `ratio` 是 0~1 浮点或 `null`（**平台没给就是 null，不补 0**）；`ratio_text` 是平台原话（`'12.34%'`）。
  每组按占比从高到低排，没给比例的项排最后。
- 官方会把缺失的粉丝类型档位补成 `0.00%`，**我们不补**（仓库硬口径：不造 0）。

`kind == 'commodity'` 时另有 `items` / `columns` / `total` / `page` / `size` / `pages`（见 §1.3）。
响应里出现「像指标但一个别名都没命中」的字段名时，另有 `unrecognized_keys`（最多 8 个）——
这是**字段名猜错时的自证**，不是错误。

### 1.3 商品数据页（`kind == 'commodity'`）的额外约定

- Query 除 `start`/`end` 外还有 `item_id`（精确）、`item_name`（模糊）、`page`（从 1）、
  `size`（10 / 20 / 50，缺省 20）、`order_by`、`order`（`asc` / `desc`）。
  非法值（页码 < 1、`size` 不在枚举、`order` 不是 asc/desc）→ `400 invalid_fields`，**一次平台都不打**。
- **默认区间是「今天」**（`start == end == 今天`），跟官方这一页的默认一致 ——
  与总览/粉丝/客服的「今天往前 7 天」**故意不同**，别统一。
- `columns` 是**平台给的列定义**（`[{name, title, type}]`），我们**不翻译、不补列**：
  列名和单位都是平台说了算。平台没给（或给了空数组）时是 `null` / `[]`，
  前端退回「按第一行的键显示」并明确告知「这些列头是字段名，不是官方中文标题」。
- `items` 是**一行一个商品**（`itmId` / `itmName` / `itmPicUrl` + 各指标列）。
  **`items === null` 表示平台没返回行**（没权限 / 服务繁忙），与 `items: []`（真的没有商品）
  是两件事，文案必须分开。
- `total` / `pages` 认不出就是 `null`，**不补 0**；`total` 真的是 0 时原样保留 0。
- **官方渲染规则**（从 bundle 抄的，不是我们发明的）：**第一行该列的值 `< 1` 时，整列渲染成
  「原样数字 + `%`」**（官方不做乘 100）。前端照抄，并在列头提示里说明。
- 只读：官方这一页有「导出/下载」接口（`…datacompas.export.item.list` 一族，轮询取文件 URL），
  **本批故意不做** —— 它会把整份数据导到本地文件，属于数据外带，需要单独评估。

### 1.4 经营卡片（`kind == 'card'`）的额外约定

官方「数据总览」页在核心指标下面还有四张卡，各是一个独立接口。我们**照抄这个结构**：

| `kind` | 平台接口 | 卡片标题 |
| --- | --- | --- |
| `trade` | `mtop.alibaba.idle.seller.pc.datacompass.flow.detail` | 交易数据 |
| `seller` | `mtop.alibaba.idle.seller.pc.datacompass.singleuser.seller.summary` | 经营概况 |
| `repurchase` | `mtop.alibaba.idle.seller.pc.datacompass.singleuser.repurchase.summary` | 复购 |
| `refund` | `mtop.alibaba.idle.seller.pc.datacompass.refund.summary` | 退款概览 |

- Query 只有 `start` / `end`（`YYYYMMDD`，缺省「今天往前 7 天」）；`kind` 不在上表枚举里
  → `400 invalid_fields`，**一次平台都不打**。
- 响应：`{account_id, kind, title, period: {from, to}, metrics[], banner[], series[],
  promotion, extra, real_date_range, warnings[], source: "platform", fetched_at}`。
- **一次一张卡**（四个独立请求，不是一次拿四张）：某一张没权限/平台报错时，
  只有那一张显示错误与 warning，另外三张照常显示。
- **指标名以平台为准**：`label` 为 `null` 时前端显示平台字段名（`platform_name`），
  **绝不自己编中文名** —— 平台新增指标时编出来的名字会骗人。`metrics` 里每项
  `{key, label, platform_name, value, ratio, ratio_format, cycle, last}`。
- 平台没给的值是 `null`，前端显示「-」，**不补 0**；`metrics`/`banner`/`series` 拿不到一律 `[]`
  （这一张没有数据），`real_date_range` 拿不到是 `null`。
- **平台「拒绝访问」是有效答复，不是故障**（与总览/粉丝/客服同一口径）：真机上这个账号对四张卡
  都回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` ⇒ 这一张给 **200 + 一句人话 warning**
  （`平台拒绝访问交易数据接口：这个账号没有「数据」权限…`），不是 502。
  *这是真机抓出来的*：早先协议层直接抛，界面上四张卡各是一个红框、框里糊着
  `协议层调用失败: MTOP 调用失败 [api]: FAIL_BIZ_…` 原文，而同一页的核心指标却是「人话 + 一屏 -」。
  登录态过期（409）、链路/风控故障（502）**仍然照抛** —— 那些必须说实话。
- `extra` 是**没登记过的字段**原样带出（不翻译、不丢），前端折叠在「平台原始字段」里；
  `series` 的点用 `points: [{date, value}]`，前端画折线时**跳过非数字点**（不插 0，插 0 会画出假的谷底）。
- 只读：这四张卡官方都只有查询接口，我们没有任何写方法（`test_routes_are_read_only` 锁死）。

### 1.5 客服明细三张表（`kind == 'detail' | 'diversion' | 'evaluation'`）

官方「客服数据」页在 6 个指标下面还有三张表，各是一个独立接口（同一套入参/响应形状）：

| `kind` | 平台接口 | 表标题 |
| --- | --- | --- |
| `detail` | `mtop.alibaba.idle.seller.pc.datacompass.cs.detail.list` | 客服明细 |
| `diversion` | `mtop.alibaba.idle.seller.pc.datacompass.cs.diversion.list` | 订单分流明细 |
| `evaluation` | `mtop.alibaba.idle.seller.pc.datacompass.cs.evaluation.list` | 客户评价明细 |

- Query：`start` / `end`（`YYYYMMDD`，缺省「今天」—— 与另外三页的「今天往前 7 天」**故意不同**，
  官方客服页默认就是当天）+ `page`（≥1，缺省 1）+ `size`（1~50，缺省 10；`10/20/50` 是官方分页档位）
  + `order_by` + `order`（`asc` / `desc`）。
  `kind` 不在枚举里 → `400 invalid_fields`；`order` 不是 `asc/desc`、或给了 `order` 却没给
  `order_by` → `400 invalid_fields`，**一次平台都不打**。`page` / `size` 越界由 `Query(ge/le)`
  先拦成 **422**（与商品数据页同一分工：FastAPI 拦边界、服务层拦业务取值）。
- 响应：`{account_id, kind, title, columns: [{key, title}], rows, total, page, page_size, pages,
  period: {from, to}, date_type, real_date_range, warnings[], source: "platform", fetched_at}`。
- **列名与中文标题全部取平台表头**（`columns[].key/title`，逐字来自官方表头的 `dataIndex` +
  中文标题）：我们不翻译、不增删 —— 平台加列就多显示一列，平台改列名就跟着改。
  三张表的列分别是 12 / 3 / 6 列（客服明细含 `acctName`「账号名称」…`respRate1min`「1分钟响应率」；
  分流明细 `divTp`「分流类型」/`dtlTp`「明细分类」/`sessCnt`「会话数」；评价明细 `buyerUserName`
  「客户名称」/`buyerNick`/`subUserName`/`reviewTime`/`reviewScore`/`dissatReason`「不满意原因」）。
- **`rows === null` 与 `rows === []` 是两件事**：前者是「平台没给列表字段」（界面明说不是「没有数据」），
  后者才是「这段时间没有数据」（空表）。`total` 认不出是 `null`；平台真的给 `0` 就原样保留 `0`
  （`pages` 相应为 0）。
- **平台「拒绝访问」是有效答复**（与总览/粉丝/卡片同一口径）：真机这个账号三张表都回
  `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` ⇒ 200 + 一句人话 warning（`平台拒绝访问客服明细接口：…`），
  且**不再补「可能改了字段名」那条** —— 两条一起说会把「没权限」说成「字段名变了」，把人往错方向带。
- 只读：三个接口官方都只有查询接口（`test_routes_are_read_only` 锁死）。
- 未验证：三张表的**真机返回值**还没见过（账号无「数据」权限）；列名与中文标题来自官方 bundle 静态提取，
  响应路径 `data.data.data.list` 逐字取自官方前端。

## 2. 指标键（前端只能依赖这些键）

平台没给的键**也一定出现**，值为 `null`。

**`overview`（9 个）**：`exposure`（曝光次数）、`exposure_visitors`（曝光人数）、
`visits`（商品访问次数）、`visit_visitors`（访问人数）、`browse`（浏览次数）、
`browse_visitors`（浏览人数）、`online_items`（在线商品数）、`inquiry_visitors`（询单人数）、
`inquiry_reply_rate_3min`（3 分钟询单响应率，平台原样百分比如 `33.33`）。

**`fans`（6 个）**：`fans_total`、`fans_new`、`fans_lost`、`fans_active`、
`fans_orders`、`fans_buyers`。

**`customer_service`（6 个）**：`inquiry_visitors`、`inquiry_count`、`reply_rate_3min`、
`avg_reply_seconds`（秒）、`unreplied_count`、`satisfaction`。

官方页面对照值（账号 `2217970177348` 的**官方页面**上显示的当期数字，用于有权限账号复验时对账）：
商品访问次数 124 / 访问人数 47 / 曝光次数 1971 / 曝光人数 1298 / 浏览次数 97 / 浏览人数 45 /
在线商品数 45 / 询单人数 8 / 3 分钟询单响应率 33.33%。真机接口对这个账号全部无权限，
所以这组数字**没有**被本项目接口取到过，属于「待有权限账号复验」。

## 3. 协议层（mtop）

| 用途 | mtop 名 | 入参 |
| --- | --- | --- |
| 数据起始日 | `mtop.alibaba.idle.seller.datacompass.get.begin.time` | 无 |
| 总览（曝光/访问/浏览） | `mtop.alibaba.idle.seller.pc.datacompass.singleuser.browse.summary` | `dateType` + `selectedSellerId`（+ 自定义区间时的 `ms`/`date`） |
| 总览（商品维度） | `...pc.datacompass.singleuser.item.summary` | 同上 |
| 粉丝概览 | `...pc.datacompass.fans.summary` | 同上 |
| 粉丝画像 | `...pc.datacompass.fans.insights.query` | 同上 |
| 客服概览 | `...pc.datacompass.cs.overview.summary` | 同上 |

入参由 `date_data(start, end, seller_id)` 构造：**两头都不给**才用 `{'dateType': 'recent1d'}`；
只要给了 `start` 或 `end`（**单日 `start == end` 也算**）就切 `dateType='month'`
并加 `ms=start`、`date=[start, end]`，缺哪一头用另一头顶。
**理由**：只发 `recent1d` 平台只能回「今天」，而 `period` 却标成用户要的那天——对外撒谎。
拼法与官方 bundle 里 `singleuser.browse.summary` / `flow.detail` 的写法一致。
服务层 `_date_pair` 永远会给出一对日期（缺省补「今天往前 7 天」），所以**经 HTTP 的
请求实际总是带日期**，`recent1d` 只在协议层被裸调用时出现。
`dateType` **必填**——真机上不给直接 `FAIL_SYS_BIZPARAM_MISSED`。

字段名别名表 `METRIC_ALIASES` 是**按官方口径假设**的（真机拿不到有权限账号的响应），
所以：认不出 → `null` + 进 `unrecognized_keys`，**绝不猜值、绝不兜底 0**。

## 4. 真机原始片段（逐字）

```
# logs/seller_discover/datacenter_probe.json
get.begin.time   mtop.alibaba.idle.seller.datacompass.get.begin.time
  ret: ["SUCCESS::调用成功"]
  data_raw: {"code": "success", "data": "20260201", "msg": "成功"}

browse.summary   mtop.alibaba.idle.seller.pc.datacompass.singleuser.browse.summary
  data: {"dateType": "recent1d"}
  error: XianyuError: MTOP 调用失败 [mtop.alibaba.idle.seller.pc.datacompass.singleuser.browse.summary]: FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问

item.summary     mtop.alibaba.idle.seller.pc.datacompass.singleuser.item.summary
  data: {"dateType": "recent1d"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问

seller.summary   mtop.alibaba.idle.seller.pc.datacompass.singleuser.seller.summary
  data: {"dateType": "recent1d"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问

repurchase.summary  mtop.alibaba.idle.seller.pc.datacompass.singleuser.repurchase.summary
  data: {"dateType": "recent1d"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问

flow.detail      mtop.alibaba.idle.seller.pc.datacompass.flow.detail
  data: {"dateType": "recent1d"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问

fans.summary     mtop.alibaba.idle.seller.pc.datacompass.fans.summary
  data: {}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问

cs.overview.summary  mtop.alibaba.idle.seller.pc.datacompass.cs.overview.summary
  data: {}
  error: ... FAIL_SYS_BIZPARAM_MISSED::缺少业务参数dateType

merchant.info.query  mtop.alibaba.idle.seller.pc.datacompass.merchant.info.query
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问
```

```
# logs/seller_discover/datacenter_probe2.json（第二轮：带参数复验）
browse.summary+seller        data: {"dateType": "recent1d", "selectedSellerId": "2217970177348"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问
cs.overview.summary+dateType data: {"dateType": "recent1d"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问   # 补上 dateType 后，参数错变成权限错
fans.summary+dateType        data: {"dateType": "recent1d"}
  error: ... FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问
```

## 5. 失败时的行为

- **部分接口失败**：其它接口的指标照给，失败的那个贡献 `null`，并在 `warnings` 里说人话
  （**只用中文业务名**：数据总览 / 商品数据 / 粉丝数据 / 粉丝画像 / 客服数据，不漏
  `overview` 这种内部调用名）：无权限 → 「平台拒绝访问数据总览接口：这个账号没有
  「数据」权限，对应指标显示 -。换个有数据权限的卖家账号再试。」；其它 → 「平台数据
  总览接口调用失败：<原文>，对应指标显示 -。」**不是**一屏「-」就完事——根因必须能看见。
- **全部接口都失败**：分两种。
  * **全是「无权限」**（真机现状）：**不算致命**，照样 200 + 一屏 `null` + 每页一句人话。
    理由：`fans` 只有 2 个调用、`cs` 只有 1 个，按「全坏即抛」这两个页面会**永远** 502，
    用户连「这个账号没有数据权限」都看不到。
  * **含其它错误**：502 `protocol_error`，`message` 带平台原文（如 `...缺少业务参数dateType`）；
    混合失败时 **SessionExpired 优先**于业务错（它要触发 409 + 回写 `need_relogin`，
    被排在前面的业务错吞掉的话，掉线的账号界面还显示已登录）。
- **协议层「成功但空着手」**（结构变了 / 别名全崩）：一律 502 `protocol_error`，
  **不是** 200 + 一屏 `-` 让用户以为今天真的没人来。三种形状都会被拦：
  `metrics` 不是 dict、`metrics` 是 `{}`（`{}` 是 dict，`isinstance` 挡不住，单独判）、
  `metrics` 缺本页契约键（报「数据组协议层缺指标字段：…」）。
  契约键清单**以协议模块为唯一真相**（`overview`→`OVERVIEW_METRICS`、`fans`→`FANS_METRICS`、
  `customer_service`→`CS_METRICS`，经 `getattr` 取），服务层不抄第二份——抄了就会和
  `METRIC_ALIASES` 一起漂移；协议模块取不到时退回只判「空 dict」。
- **登录态过期**：409 `verification_required` + `login_state: "need_relogin"`，
  并回写 `account_credentials.login_state`（见 §6 的 `_BoundProtocol`）。
- **缓存**：进程内 `_CACHE`，键 `(kind, account_id, start, end)`，TTL 300 秒，上限 64 条
  （满了淘汰最旧的一条），**只缓存成功结果**（失败不缓存，否则用户重试也看不到变化）；
  不落库。**读在建客户端之前**：命中缓存直接返回，既不付建客户端的代价，本地登录态变脏
  也不会把 5 分钟内的成功结果盖成 409。

## 6. 实现要点（为什么这么做）

- 协议层用「模块级 async 函数 + 调用方传 `client`」而不是往 `xianyu/client.py` 加方法：
  该文件是冻结文件，且数据组接口名/入参/响应结构与 `item_stats` 完全不同族。
- `_BoundProtocol`（`services/datacenter.py`）：把协议模块与 client 绑在一起再交给
  `common.call_method`。**理由**：`call_method` 在登录态过期时会用**第一个参数**去回写
  `need_relogin`（读它的 `cookie_id` / `account_id`）；直接传协议模块的话模块没有账号身份，
  掉线就写不回库——真机表现是「协议报 Session过期，界面还显示已登录」。
  转发方向：**协议函数**转发到模块，**账号身份**只从 client 上取
  `cookie_id` / `account_id` / `persist_account_id`（真 `XianyuClient` **没有** `account_id`
  属性，必须退回 `persist_account_id`，否则恒为 `''`、回写被静默跳过）；client 的其它属性
  （如 `cookie_str`）不转发。
- `_collect` 逐接口容错：只有**全坏**才抛异常，抛哪个看**严重度**——SessionExpired 优先
  （要触发 409 + 回写 `need_relogin`），其次取调用顺序里的第一个；抛的都是**原始异常**
  （类名/原文保留），好让 `common.call_failed` 认出登录态过期并翻对信封。
  例外：**全是「无权限」不抛**，见 §5。
- `_collect` 的 label 就是页内身份：`raw` **只按 label 写**，取 `real_date_range` /
  `suspect_keys` 时必须用本页标签（总览页 `overview`/`items`、粉丝页 `fans`、客服页 **`cs`**）。
  客服页曾误读 `raw['overview']`，导致 `real_date_range` 恒 `None`、别名认错时也没有
  `unrecognized_keys` 自证。
- 协议层返回的裸字符串也能认：`_mtop` 会剥掉外层信封（`result.get('data')`），
  所以 `begin.time` 回来的是 `'20260201'` 这种**裸日期**，`_begin_from` 两种形状都要吃。

## 7. 跑法与未验证项

```bash
# 仓库根目录
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\test_datacenter_api.py"
# 或
cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\Python310\python.exe backend\tests\run_all.py"
```

**未验证（需要另一个有「数据」权限的卖家账号，或有权限账号的真机响应）**：

1. 有权限时 `browse.summary` / `item.summary` / `fans.summary` / `cs.overview.summary`
   的**真实响应字段名**——`METRIC_ALIASES` 目前是假设的；认不出会进 `unrecognized_keys`，
   届时按它补别名即可（不需要改契约的键名）。
2. **⚠ 运行期风险（当前最大）**：服务层 `_date_pair` 永远补全两端日期，所以 **HTTP 入口
   发出的每个请求都是 `dateType='month'` + `ms` + `date=[d1,d2]`**，而真机只验过
   `{"dateType":"recent1d"}`（§4）。若平台不接受 `month` 形态，三个页面会一起 502/全 null。
   复验法（对照法）：拿有权限账号打一次 `month` 形态，错误从 `FAIL_SYS_BIZPARAM_MISSED`
   变成 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 就说明**参数被接受了**、只是没权限（参考 §4 里
   `cs.overview.summary` 补 `dateType` 前后的对照）。
3. ~~`fans.insights.query` 的返回结构（画像列表埋在哪个键下）~~ —— **第 51 批已解决**：是四组具名列表
   （`buyerGenderList` / `fansTypeList` / `buyerProvinceList` / `buyerEightTpList`，见 §1.2），
   字段名与官方顺序表都从 bundle 逐字取证；但**没有有权限账号的真机样本**，所以
   `usrRatio` / `usrRatioFormat` 究竟哪个出现、`profileVal` 的完整取值表仍待真机确认。
4. 「字段名猜对了但平台回了**字符串数字**」时 `find_number` 判 `None` 且**不进**
   `unrecognized_keys`——用户看到的和无权限一模一样，自证机制不会提示。
