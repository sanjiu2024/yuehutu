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
| `mtop.alibaba.idle.seller.platform.idle.category.query` | dc bundle `@2494759` 只有**声明**、调用点 0；且那个 bundle 是「数据」组，与商品发布无关 |
| `mtop.taobao.idle.item.pc.category.list` | js `@1804756`，请求 `{params: JSON.stringify({industryId, sourceId})}`：来自 bundle **老区段**（`@1804740` 附近是老登录/PC 初始化代码，不是发布页），且 `industryId` 来源未证；发布页已有 `publish.categories` |
| `mtop.alibaba.idle.seller.pc.common.item.search` | js bundle 调用点 0；它属于**商品列表**域（不是发布表单数据源）。字段形状已登记在 `docs/contracts/item_templates.md` §4，留给「商品管理页」那一批 |
| `mtop.alibaba.idle.seller.platform.item.operate.report.get` | js `@3081396` 只有声明、调用点 0，请求字段形状未知（不猜参数） |
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
