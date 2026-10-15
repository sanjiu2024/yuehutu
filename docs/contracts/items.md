# 商品发布/编辑「表单数据源」契约（类目 / 品牌 / 型号 / 属性 / 类目搜索）

> 本文件是 `backend/app/xianyu/item_publish.py` / `backend/app/services/items.py`（「发布数据源」段）/
> `backend/app/api/items.py`（`/{account_id}/publish/*` 五条 GET）的对外契约。
> 第 60 批（task-15）新增；**5 个接口全部只读**。
>
> **证据来源**（都是源码级：官方微应用 bundle 的字符偏移，可 grep 复核）：
> - `C:\\Users\\Administrator\\.dsh-tmp\\trade_bundle\\main.js`（3,879,584 字符，商品发布页侧栏；
>   调用形态 `hO({api, v:'1.0', valueType:'string', data})`）—— 本机审计副本，不在仓库里。
> - `logs/seller_discover/item_templates_src/js_main.js`（4,402,065 字符，商品管理页；
>   调用形态 `gC({api, v:'1.0', data, showErrorToast})`）—— 在仓库里。
> - 复现搜索串：`item.publish.categories` / `item.publish.brands` / `item.publish.models` /
>   `item.pc.category.property.query` / `item.pc.search.categories`。
>
> **真机证据（2026-10-16，打包版 18400 + 真实 Cookie，账号 `2217970177348`）**：5 条里 4 条拿到 200 ——
> `publish/categories` **99 条一级类目**、`publish/brands` **68 条**（元素 **`{value,label}`**）、
> `publish/models` 0 条、`publish/category-properties` 0 条；`publish/search-categories` 被平台拒
> `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。原始响应见 `logs/probe60/round2/`（`logs/` 不入仓）。
> 协议层**不写任何兜底值**（认不出就是空列表 / `None`，绝不用 0 顶替），解析对字段名容错，
> 并把平台的 `code` / `msg` 原样带出来。

## 1. HTTP 接口（5 条 GET，全部要求登录）

| 方法 | 路径 | Query | 平台接口 |
| --- | --- | --- | --- |
| GET | `/api/items/{account_id}/publish/categories` | 无 | `...item.publish.categories` |
| GET | `/api/items/{account_id}/publish/brands` | `category_id`（必填） | `...item.publish.brands` |
| GET | `/api/items/{account_id}/publish/models` | `category_id`、`brand_id`（都必填） | `...item.publish.models` |
| GET | `/api/items/{account_id}/publish/category-properties` | `category_id`（必填） | `...item.pc.category.property.query` |
| GET | `/api/items/{account_id}/publish/search-categories` | 无 | `...item.pc.search.categories` |

- **路径为什么是 `/{account_id}/publish/*`**：account_id 在前，与同族 `POST /{account_id}/publish/precheck`
  一致；`GET /api/items/{account_id}/{item_id}` 是两段，不会吞掉这些三段路径。
  声明顺序仍按 `api/items.py` 文件头那条规矩放在 `{item_id}` 之前（`test_routes_are_registered_before_item_id` 钉住）。
- **全部只读**：不落库、不播事件、没有 `confirm` 闸门（`confirm` 只属于平台写操作）。
  写方法一律 405（`test_route_is_get_only`）。

响应统一走全局信封（`common.respond`），5 个接口共用**同一个 data 形状**：

```json
{
  "success": true,
  "data": {
    "account_id": "2217970177348",
    "kind": "categories",
    "options": [
      {"category_id": "100", "category_name": "手机", "level": 1, "leaf": false,
       "children": [{"category_id": "101", "category_name": "智能手机", "level": 2,
                     "leaf": true, "children": []}]}
    ],
    "count": 1,
    "platform_code": "success",
    "api": "mtop.alibaba.idle.seller.platform.item.publish.categories",
    "fetched_at": "2026-10-16 00:20:00",
    "source": "platform"
  }
}
```

各 `kind` 的 `options` 元素形状：

| kind | options 元素 |
| --- | --- |
| `categories` / `search_categories` | `{category_id, category_name, level, leaf, children:[递归同形]}` |
| `brands` / `models` | `{value, label}`（bundle 原文映射 `{label: valueName, value: valueId}`） |
| `category_properties` | `{property_id, property_name, values:[{value, label}]}` |

- **`options: []` 是 200，不是失败**：平台回了空 / 我们认不出字段时，如实回空列表 +
  `platform_code`，让调用方自己分辨「平台没给」和「链路坏了」。**绝不编候选值**（task-15 硬约束 6）。
- 只提升**已证实**的字段：类目 `categoryId/categoryName/categoryLevel/leaf/childrenCategoryDOs`、
  品牌型号 `valueId/valueName`。认不出字段名的行**丢弃**（一个点不动的空选项比少一行更坏）；
  其余字段没有提升成契约键。
- `platform_code` 是平台业务码 `res.data.code`（拿不到就是 `null`）；`platform_message` 只在
  平台给了 `msg`/`message` 时出现。**平台回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 时仍是 200**
  —— 那是平台的有效答复（链路通、账号没权限），探针据此判「是权限问题」而不是「接口写错了」。

## 2. 错误语义

| 场景 | 状态码 | `code` | 平台请求 |
| --- | --- | --- | --- |
| 缺 `category_id` / `brand_id` | 400 | `invalid_fields` | **0 次**（字段校验在建客户端之前，与 `item_templates` 三步顺序同口径） |
| `account_id` 为空 | 400 | `bad_request` | 0 次 |
| 账号不存在 | 404 | `not_found` | 0 次 |
| 没有 Cookie / `login_state=need_relogin` | 409 | `verification_required`（带 `login_state: need_relogin`） | 0 次 |
| 协议层模块拿不到 | 503 | `xianyu_not_ready` | 0 次 |
| 协议层抛异常（非登录态） | 502 | `protocol_error` | 1 次（失败） |
| 平台业务码不是 `success` | 200 | —（`platform_code` 原样带出） | 1 次 |

- 「协议层没装」与「平台没给数据」**必须能分辨**：前者 503 `xianyu_not_ready`，后者 200 + `options: []`。
  把前者当后者，用户会以为「这个类目下没有品牌」从而以为类目选错了。
- 前置检查复用 `services/trade.py::_client`（全仓唯一一份账号前置检查）：账号 id 打错报 404
  而不是 503「后端未就绪」。
- 登录态过期由 `common.call_method` 回写 `need_relogin`（靠 `_BoundProtocol` 上的
  `cookie_id`/`account_id`，与 `services/datacenter.py` 同一手法）。

## 3. 平台 mtop 接口与入参（`backend/app/xianyu/item_publish.py`）

| 常量 | api | v | valueType | 请求 data | bundle 证据 |
| --- | --- | --- | --- | --- | --- |
| `CATEGORIES_API` | `mtop.alibaba.idle.seller.platform.item.publish.categories` | 1.0 | `string` | **无 data** | trade `@3832720` |
| `BRANDS_API` | `mtop.alibaba.idle.seller.platform.item.publish.brands` | 1.0 | `string` | `{channelCategoryId: <Number>}` | trade `@3833710` |
| `MODELS_API` | `mtop.alibaba.idle.seller.platform.item.publish.models` | 1.0 | `string` | `{brandValueId: <Number>, channelCategoryId: <Number>}` | trade `@3834799` |
| `CATEGORY_PROPERTY_API` | `mtop.taobao.idle.seller.platform.item.pc.category.property.query` | 1.0 | — | `{categoryId: <Number>}` | js `@3074608` |
| `SEARCH_CATEGORIES_API` | `mtop.taobao.idle.seller.platform.item.pc.search.categories` | 1.0 | `string` | **无 data** | js `@3073093` |

响应取值路径（`res` = `client.mtop` 的 `raw`，`res.data` = 平台业务信封，`res.data.data` = 业务体）：

| 接口 | 业务体键 | 说明 |
| --- | --- | --- |
| categories | `selectIdleCategories` | 元素直接当 select 选项用（bundle 原文） |
| brands | `selectIdleBrandValues` | 成功判据 `res.data.code === 'success'` |
| models | `selectIdleModelValues` | 同上 |
| category.property.query | `fineness`（**未证实**） | 见 §6；解析对 `propertyList` 等键名容错 |
| search.categories | `selectIdleCategories`（**未证实是否与发布页一致**） | 兼容「业务体直接是数组」的形状 |

- `Number(...)` 语义：`brands_data`/`models_data`/`property_data` 用 `_as_int`，转不动就是 `None`
  （**不是 0** —— 0 是合法 id 语义，用 0 顶替会发出一次查不到东西的请求）。
- 协议层函数签名（模块级 async，client 作入参，不往 `client.py` 加方法 —— 那个文件本批冻结）：
  `fetch_categories(client)` / `fetch_brands(client, category_id)` /
  `fetch_models(client, category_id, brand_id)` / `fetch_category_properties(client, category_id)` /
  `fetch_search_categories(client)`。

## 4. 不接的候选接口（逐条给理由，避免重复实现）

| 候选 | 为什么不接 |
| --- | --- |
| `mtop.alibaba.idle.seller.platform.idle.category.query` | ~~不接~~ → **已接（第 63 批 task-23，见 §7）**；原理由：dc bundle `@2494759` 只有**声明**、调用点 0；且那个 bundle 是「数据」组，与商品发布无关 |
| `mtop.taobao.idle.item.pc.category.list` | js `@1804756`，请求 `{params: JSON.stringify({industryId, sourceId})}`：来自 bundle **老区段**（`@1804740` 附近是老登录/PC 初始化代码，不是发布页），且 `industryId` 来源未证；发布页已有 `publish.categories` |
| `mtop.alibaba.idle.seller.pc.common.item.search` | ~~不接~~ → **已接（第 63 批 task-23，见 §7）**；原理由：js bundle 调用点 0；它属于**商品列表**域（不是发布表单数据源）。字段形状已登记在 `docs/contracts/item_templates.md` §4，留给「商品管理页」那一批 |
| `mtop.alibaba.idle.seller.platform.item.operate.report.get` | ~~不接~~ → **已接（第 63 批 task-23，见 §7）**；原理由：js `@3081396` 只有声明、调用点 0，请求字段形状未知（不猜参数）。真机已证**必须有 `itemId`** |
| `client.recommend_category`（已有） | 它是「按标题**猜**类目」（`kgraph.property.recommend` v2.0），不是「按类目拿候选值」，**替代不了**本文件这 5 个 |
| 相邻发现（登记备查，本批不实现） | `mtop.taobao.idle.kgraph.property.search`（js `@3074998`，调用点 `@4058080`/`@4058567`：`{inputText:'', channelCatId, propertyId:20000, scene:'seller_platform'}` → `res.data.data.searchList`，元素 `{valueName, valueId}`）；`mtop.alibaba.idle.seller.platform.cpv.category`（js `@3074239`，调用点 0）；`mtop.alibaba.idle.item.pc.draft.search`（草稿箱） |

## 5. 真机可验证清单（Lead 在打包版上打探针用）

最小参数：`{account_id}` 换成已扫码账号；探针要看 `HTTP` + `code` + `platform_code` + `count`。

| # | 方法 + 路径 | 最小参数 | 期望（有权限账号） |
| --- | --- | --- | --- |
| 1 | `GET /api/items/{account_id}/publish/categories` | 无 | 200；`count > 0`；`platform_code == 'success'`；`options[0].children` 非空 |
| 2 | `GET /api/items/{account_id}/publish/brands?category_id=<#1 某个叶子 category_id>` | 类目 id | 200；`count > 0`；`options[0] == {value, label}` |
| 3 | `GET /api/items/{account_id}/publish/models?category_id=<同上>&brand_id=<#2 某个 value>` | 类目 + 品牌 | 200；`count > 0`（有些类目无型号 → `count == 0` 且 `platform_code == 'success'` 也算通过） |
| 4 | `GET /api/items/{account_id}/publish/category-properties?category_id=<同上>` | 类目 id | **形状未证实**：只要 200 且 `platform_code` 有值即可；若 `count == 0` 而平台返回非空，说明键名要改（把原始响应贴回来） |
| 5 | `GET /api/items/{account_id}/publish/search-categories` | 无 | 200；与 #1 对比是否同形（若 `count == 0` 但平台非空 → 键名不同） |
| 6 | 反例：`GET /api/items/{account_id}/publish/brands`（不带 category_id） | 无 | 400 `invalid_fields`，**平台 0 调用** |
| 7 | 反例：`GET /api/items/不存在账号/publish/categories` | 无 | 404 `not_found` |

探针若拿到非空原始响应，**优先把 #4 / #5 的原始 JSON 贴回本文件 §6**，再改解析键名。

## 6. 未确定项（真机后必须回来改这里）

1. `category.property.query` 的业务体键名（只见到 `data.data.fineness` 这条线索，没有消费侧渲染代码可对照）。
2. `search.categories` 的响应是否与 `publish.categories` 同形（同名 `selectIdleCategories`，但那是商品列表筛选页的用法）。
3. ~~`brands` / `models` 的元素字段名~~ —— **已解（2026-10-16 真机）**：`brands` 元素是 **`{value,label}`**（68 条，如 `{value:'642468970',label:'NIORFNIO'}`，都是字符串），不是 `valueId`/`valueName`；`models` 那次 0 条，形状仍未见到。
4. `publish.categories` 的 `valueType:'string'` 是否真的必要（官方这么发，我们照抄；若真机报参数错，去掉它）。
5. 型号 `count == 0` 与「这个类目本来就没型号」的区分：真机（2026-10-16）`models` 与 `category-properties` 都回 **200 + 空数组 + `platform_code=success`**，**两者仍不可分辨**，只能靠 `platform_code`。

## 编辑失败不改本地（第 63 批）

`POST /api/items/{account_id}/{item_id}/edit` 的平台失败语义与上下架（第 61 批）对齐：
**平台 CALL_ERROR 时本地字段一个字节都不改**。

- 顺序：`_push_update`（推平台）**先**，本地 `UPDATE items` **后**；平台没确认就不落库。
- 平台拒绝 → HTTP 200 + `data.synced=false`、`data.updated_fields=[]`、
  `data.failed=[{id, code: 'protocol_error', message}]`，message 是平台原文 + 中文前缀
  `平台修改失败，本地未改动: `，并落一条 `success=0` 的 `operation_logs`。
- 一条都没改成 ⇒ **不发** `items.updated`。
- 协议层没就绪（`CALL_MISSING`，平台一次请求都没发出）仍是既定「本地优先」口径：
  本地照改 + `warning` 说明「协议层尚未实现商品修改，改动仅本地生效」。

回归：`backend/tests/test_items_api.py::ItemEditPlatformFailureApiTest`、
`backend/tests/test_services_items.py`（平台拒绝 / 未知错误码透传两条已按新口径改写）。

## 7. 商品读 5 条（第 63 批 task-23，独立模块 `items_read`）

**为什么另开模块**：`backend/app/services/items.py` 被 task-24 占用，且这 5 条属「商品读/管理」域
（列表、类目字典、筛选词、发布前预取），与发布表单数据源不同域。故新建
`backend/app/xianyu/items_read.py` + `backend/app/services/items_read.py` + `backend/app/api/items_read.py`，
HTTP 前缀独立 `/api/items-read`（**故意不并进 `/api/items`**，不与 task-24 抢同一份路由文件）。

| # | HTTP | 协议层函数 | api | 请求 data | bundle 证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | `GET /api/items-read/{account_id}/search` | `search_items` | `mtop.alibaba.idle.seller.pc.common.item.search` | `{pageNo, pageSize, bizType:'commonPro', searchRequest:'<JSON 字符串>', itemStatus:'0,-9'}` + 可选 `orderField`/`orderType` | js `@3071624`（调用点 1） |
| 2 | `GET /api/items-read/{account_id}/categories` | `category_query` | `mtop.alibaba.idle.seller.platform.idle.category.query` | `params` 原样透传（默认无 data） | dc `@2494750` |
| 3 | `GET /api/items-read/{account_id}/operate-report` | `operate_report` | `mtop.alibaba.idle.seller.platform.item.operate.report.get` | `params` 原样透传（**真机证必须有 `itemId`**） | js `@3081396` |
| 4 | `GET /api/items-read/{account_id}/search-terms` | `search_terms` | `mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query` | 无 data | js `@3075400` |
| 5 | `GET /api/items-read/{account_id}/preget` | `preget` | `mtop.idle.pc.idleitem.preget` | `{itemId, sourceId, publishScene:'firstHandPublish'}` | js `@3062151` |

- 查询参数：1 号 `keyword/page/size/item_status/biz_type/order_field/order_type/search_request`
  （`page` 1..100000、`size` 1..50，越界 **422**）；2/3 号 `params`（**JSON 对象字符串**）；
  4 号无参；5 号 `item_id`/`source_id`/`publish_scene`（`source_id` 缺省空串、`publish_scene` 缺省 `firstHandPublish`）。
- **`searchRequest` 是 JSON 字符串**（不是对象）：bundle 原文是 `JSON.stringify(...)` 后塞进去。
  本模块把 `keyword` 包成 `{"keyword": "..."}` 再 `json.dumps`；调用方也可用 `search_request` 直接给整串。
- 响应解析：列表认 `itemSearchResponseList`（`itemList`/`list`/`rows`/`items` 容错），
  总数认 `total`（`totalCount`/`count` 容错）；认不出就是 `null`/`[]`，**绝不编数**。
- 筛选词只收 `returnService`/`sendGoodsTime`/`serviceGuarantee` 三组（`TERM_GROUPS`），
  元素 `{key,value}` 两键都非空才留。
- 错误语义与 §2 同：账号前置检查（400/404/409）、模块缺失 503 `xianyu_not_ready`、
  协议层抛异常 502 `protocol_error`、**平台业务码非 `success` 仍 200 + `platform_code` 原样带出**。
- 契约键：`{account_id, kind, api, platform_code, fetched_at(UTC), source:'platform'}` + 按需 `platform_message`；
  1 号另有 `rows`/`total`，4 号 `terms`，5 号 `need_up_first_hand_item`/`industry_id`，2/3 号 `data`。
- 协议层函数（模块级 async、client 作入参，不动冻结的 `client.py`）：
  `search_items(client, keyword, page, size, item_status, biz_type, search_request, order_field, order_type)` /
  `category_query(client, params)` / `operate_report(client, params)` / `search_terms(client)` /
  `preget(client, item_id, source_id, publish_scene)`，每个都回 `sent`（实际发出的 data，探针看这个）。

### 7.1 真机只读复打第一轮（2026-10-16，账号 2217970177348 月狐兔_）

DB 一致性拷贝（`sqlite3` 的 `Connection.backup()`）到 `%TEMP%\xywb_live63\data\app.db`（602112 bytes），
生产库只读；原始响应 `logs/items_read63/raw63.json`（脚本 `C:\Users\Administrator\.dsh-tmp\ir63_probe.py`）。

| # | 接口 | 入参 | 结果（逐条如实） |
| --- | --- | --- | --- |
| 1 | `search_items` | 无参默认 | 成功：`platform_code=success`、**rows=20**、**total=44**、message=成功 |
| 2 | `category_query` | 无参 | `platform_code=success`，业务体是**数组**（形状见 §7.2） |
| 3 | `operate_report` | 无参 | **502 `protocol_error`**，平台原文 `MTOP 调用失败 [...operate.report.get]: FAIL_SYS_BIZPARAM_MISSED::缺少业务参数itemId` ⇒ **必须有 `itemId`** |
| 4 | `search_terms` | 无参 | `platform_code=success`，`terms={returnService:2, sendGoodsTime:3, serviceGuarantee:19}`（19 项与旧登记吻合） |
| 5 | `preget` | 空 `itemId` | **502**，平台原文 `FAIL_BIZ_USER_SHOW_SYSTEM_ERROR::亲，小闲鱼有点忙，请稍后重试哦` |
| — | 5 条 GET（HTTP 轮，TestClient 打真实路由） | 无参 | 5 条**全 502** `protocol_error`，message `协议层调用失败: 账号处于风控冷却中，还需 59s` |

### 7.2 真机第二轮（带真实 `itemId`，2026-10-16）

item_id 取自第一轮 `search_items` 的 `rows[0]`：**`1088306808784`**；原始响应 `logs/items_read63/raw63b.json`。

| 接口 | 入参 | 结果（逐条如实） |
| --- | --- | --- |
| `operate_report` | 探针误传 **dict** `{itemId: ...}` | **400 `invalid_fields`** `params 必须是 JSON 对象字符串` —— 服务层只收 JSON **字符串**（探针口径错，非实现缺陷）；带字符串 `'{"itemId":"1088306808784"}'` 的复打**未做成**（见 §8.7） |
| `category_query` | 同上误传 dict | 同上 400 |
| `preget` | 真实 `itemId=1088306808784` | **502**，平台原文 `FAIL_BIZ_USER_SHOW_SYSTEM_ERROR::亲，小闲鱼有点忙，请稍后重试哦`；`client.py` 日志 `[2217970177348] 命中风控，冷却 60s: FAIL_BIZ_USER_SHOW_SYSTEM_ERROR::...` ⇒ **这条平台文案就是本账号的风控信号**（不是「参数错」） |
| `search_terms` | 无参复打 | **502** `protocol_error`，冷却中（`preget` 触发的 60s 冷却） |
| HTTP `/search`、`/search-terms` | TestClient | **502** `protocol_error`，`账号处于风控冷却中，还需 35s / 27s` |

- **风控额度共享且会被平台文案触发**：第一轮的 `preget`（空 id）就已经把账号打进 60s 冷却，
  所以第一轮 HTTP 轮 5 条全 502 的根因是**冷却**，不是路由写错。冷却被映射成 **502 `protocol_error`**
  （语义更像 **429**）；`client.py` 本批冻结、与其它模块同行为，**是否单独映射留给 Lead 定**。
- **新证据（`category_query` 业务体形状）**：数组元素与 §1 的 `categories` 同形 ——
  `{categoryId: 126866937, categoryLevel: 1, categoryName: '3C数码配件', childrenCategoryDOs: [{... 递归 ...}]}`，
  深层节点带 `leaf`/`root`（`false`）。⇒ §8.6 部分已解。
- 探针口径：service 层调用 + `common.respond` 取状态码；HTTP 轮用 `TestClient(create_app())`
  + 在**拷贝库**上临时插入 `users('probe63')` 签发的 token（生产库只读）。

## 8. 未确定项（第 63 批新增，真机后回来改）

6. ~~`idle.category.query` 的业务体形状~~ —— **部分已解（2026-10-16 第二轮）**：是数组，
   元素 `{categoryId, categoryLevel, categoryName, childrenCategoryDOs:[递归]}`（与发布页 `selectIdleCategories` 同形）；
   **但本模块的解析还没按这个形状提升字段**（只回 `data` 原样），要不要提升成 `options` 由 Lead 定。
7. `operate.report.get` 带 `itemId` 后的**响应形状**未知：本轮只证了「不给 itemId → `FAIL_SYS_BIZPARAM_MISSED`」；
   带字符串参数的复打被风控冷却挡住，**未做成**。
8. `preget` 需要真实 `itemId` 才可能成功：真实 id 也回 `FAIL_BIZ_USER_SHOW_SYSTEM_ERROR`，
   而该文案已被 `client.py` 判为风控 ⇒ **本账号当时处于风控**，无法区分「参数不对」与「风控挡住」。
9. `common.item.search` 的 `searchRequest` 里还有哪些筛选键（bundle 里被 `JSON.stringify`，看不到）。
10. 风控冷却（60s）被映射成 502 `protocol_error` 是否要改（更像 429 + `Retry-After`）；`client.py` 冻结，需 Lead 定。

