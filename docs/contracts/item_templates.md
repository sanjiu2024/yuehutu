# 运费模版（官方 `post-temple` 页）只读契约

> 本文件是 `backend/app/xianyu/item_templates.py` / `backend/app/services/item_templates.py` /
> `backend/app/api/item_templates.py` 的对外契约，**只覆盖只读能力**。
>
> 证据来源（全部是 2026-10-10 的真机/源码证据，不是推测）：
> - 真机抓包（headless 打开 `https://seller.goofish.com/?site=COMMONPRO#/seller-item/post-temple`，
>   账号 `2217970177348`）：`logs/seller_discover/item_templates_post-temple.json`（11 次 mtop 调用）、
>   `item_templates_post-temple-create.json`（12 次）、`item_templates_goods-manage.json`（6 次）、
>   `item_templates_probe.json`（直连 mtop 探参数）。
> - 前端 bundle 原文：`logs/seller_discover/item_templates_src/js_main.js`（4,402,065 字节），
>   下文 `@偏移` 都是这个文件里的字符偏移，可直接 grep 复核。

## 0. 一句话结论：平台**没有**「发布模板」这个功能

官方 `post-temple` 不是「发布模板」，是**运费模版管理**（`post` = 邮 / freight template）：

- 面包屑原文 `@2211787`：`{title:"运费模版管理", ... push("#/seller-item/post-temple")},{title:"创建模版"}`；
  列表页标题 `@4136359`「运费模版管理」，空态 `@4138485`「暂无模版」。
- 该页所有业务调用都是 `mtop.alibaba.idle.seller.platform.pro.freight.template.*`。
- bundle 里中文按 `\uXXXX` 转义串计数：`模版` 出现 49 次（**全部**是运费模版），`模板` 0 次，`发布模板` 0 次；
  标识符里不存在 `publishTemplate` / `itemTemplate` / `postTemplate`。
- 发布侧被当「模板」用的是**草稿箱**：`@3049056` 草稿已保存 → `#/publish/success?draftId=…&scene=draft`，
  `@1813029`「商品草稿保存成功/您可以在草稿箱中找到该商品」，发布页另有 `getShippingTemplate`（选运费模版）。

→ 所以本模块只做运费模版的**读**；「发布模板」如果产品侧确实要，需要另立需求（平台当前不提供）。

## 1. 已实现（只读）

### 1.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/item-templates/{account_id}` | 运费模版列表。Query：`page`（默认 1）、`size`（默认 20，限 1–50）、`template_name`（可选，按名搜索） |
| GET | `/api/item-templates/{account_id}/{template_id}` | 单个模版详情 |

- 前缀用 `/api/item-templates` 而不是挂在 `/api/items` 下：`backend/app/api/items.py` 已有
  `GET /api/items/{account_id}/{item_id}`，同前缀会把 `templates` 当成 `item_id` 吞掉。
- 两个端点都要求登录（`Depends(current_user)`），响应走全局信封 `{success, data}` / `{success:false, message, code}`，
  状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、`verification_required`→409、
  `protocol_error`→502。**没有任何写方法**（POST/PUT/PATCH/DELETE 一律 405）。

列表 `data`：

```json
{
  "account_id": "2217970177348",
  "templates": [
    {
      "template_id": "987654321",
      "template_name": "默认运费模版",
      "raw": { "templateId": 987654321, "templateName": "默认运费模版", "excessPrice": "3", "isDefault": true }
    }
  ],
  "count": 1,
  "total": 3,
  "page": 1,
  "page_size": 20,
  "has_next": true,
  "fetched_at": "2026-10-10T04:10:00Z",
  "source": "platform"
}
```

- `total` / `has_next` 在平台没返回 `totalCount` / `hasNextPage` 时是 **`null`**，不是 0 / false ——
  「一个模版都没有」和「平台没告诉我」必须能分辨。
- `templates[]` 只把**已证实**的 `templateId` / `templateName` 提升成 `template_id` / `template_name`，
  其余字段原样放 `raw`（不猜字段名）。

详情 `data`：`{account_id, template: {template_id, template_name, raw}, fetched_at, source}`；
平台返回空（`{}` / `[]` / `null`）时是 **404 `not_found`**（`平台上没有这个运费模版: <id>`），不返回空壳 200。

### 1.2 平台 mtop 接口（`backend/app/xianyu/item_templates.py`）

| 常量 | api | 版本 | 请求 data | 证据 |
| --- | --- | --- | --- | --- |
| `LIST_API` | `mtop.alibaba.idle.seller.platform.pro.freight.template.query` | 1.0 | `{pageNo, pageSize, templateId?, templateName?}` | bundle `@2203119`；真机响应 `{code, currentPage, data[], hasNextPage, pageSize, totalCount}` |
| `DETAIL_API` | `…pro.freight.template.detail` | 1.0 | `{templateId}` | bundle `@2204414` |
| `ITEM_API` | `…pro.freight.template.item.query` | 1.0 | `{templateId, pageNo?, pageSize?}` | bundle `@2203549`；真机缺 `templateId` → `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数templateId` |
| `REGION_API` | `…pro.freight.template.region` | 1.0 | 无 | bundle `@2205110` |
| `ALL_API` | `mtop.alibaba.idle.seller.platform.freight.template.pc.all` | 1.0 | `{}`（`valueType:"string"`，不分页） | bundle `@4110816` / `@4143044`，元素 `{templateName, templateId, excessPrice?}` → 页面映射 `{label: templateName, value: templateId}` |
| `DIVISION_API` | `mtop.taobao.idle.division.all.get` | 1.0 | 无 | 创建页 `item_templates_post-temple-create.json` |

- 全部调用都带 `extra_headers = {"idle_site_biz_code": "COMMONPRO"}`（借客户端 `_security_headers()`）。
  **必须带**：直连 mtop 不带这个头时平台回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`
  （见 `item_templates_probe.json`），而这是**权限头缺失**，不是账号掉线 —— 服务层刻意把它归为
  `protocol_error`(502)，不会写 `need_relogin`、不会让用户白扫一次码（`test_unauthorized_is_not_treated_as_logout` 钉住）。
- 真机观察（2026-10-10，账号 `2217970177348`）：该账号运费模版数为 0，`query` 返回
  `{"code":"success","currentPage":1,"data":[],"hasNextPage":false,"pageSize":10,"totalCount":0}`。
  → 列表「空」这条路径有真机证据；**非空行的字段表没有真机证据**，所以只提升两个已证实字段。

### 1.3 服务层

`backend/app/services/item_templates.py`：`list_templates(account_id, page, size, template_name)`、
`template_detail(account_id, template_id)`。账号不存在 → 404 `not_found`；账号没有 Cookie / 已掉线 →
409 `verification_required` + `login_state: "need_relogin"`（且**一次平台都不打**）。

### 1.4 挂载

`backend/app/main.py:35 ROUTE_MODULES` 需要加上 `'item_templates'`（`main.py:288-298` 用
`importlib.import_module(f'.api.{name}')` 批量挂载）。本模块自带路由，未加进 `ROUTE_MODULES` 前
HTTP 端点不会出现在应用里；`backend/tests/test_item_templates_api.py` 因此自己 `include_router` 到独立
`FastAPI()`，不依赖挂载状态。

## 2. 未实现（写能力，平台有、我们故意不做）

`backend/app/xianyu/item_templates.py: WRITE_APIS` 里只登记了名字与参数形状，**没有任何实现**：

| api | 请求 data | 证据 |
| --- | --- | --- |
| `…pro.freight.template.create` | `{templateName, isDefault, …运费规则}`（`templateName` ≤30 字符、禁 emoji） | bundle `@2204849` / `@2209672` |
| `…pro.freight.template.modify` | 同 create 的字段 + `templateId` | bundle `@2204655` |
| `…pro.freight.template.del` | `{templateId}` | bundle `@2203984` |
| `mtop.alibaba.idle.seller.platform.freight.template.pc.item.unbind` | `{templateId, itemId?}` | bundle `@2205322` |

要做写能力时另开任务（需要：写前确认、结果如实回执、失败不静默）。

## 3. 平台无此数据

- **「发布模板」/「商品模板」**：平台不提供（见 §0）。发布页真正复用的只有运费模版（`getShippingTemplate`）
  和草稿箱（`draftId`）。
- **运费模版的「使用中商品数」**：列表/详情响应里没有这个字段（`pc.all` 只有 `templateName/templateId/excessPrice?`）。
- **草稿箱独立路由**：bundle 路由表里没有独立草稿页，草稿是 `goods-manage` 的 `type==2` 分支。

## 4. 官方商品管理页（`goods-manage`）比我们多的只读能力

我们这边对应的是 `GET /api/items`（本地库 + `sync_items` 同步）。官方页面只读部分多出这些
（**本次只登记，不实现**；证据 `logs/seller_discover/item_templates_goods-manage.json` + bundle `@4076279` 起）：

| 能力 | 平台 api | 证据要点 |
| --- | --- | --- |
| 状态 tab 计数（在售/已下架/审核中…） | `mtop.taobao.idle.seller.platform.pc.item.status.statistics` | 无参；`data:[{field:'itemStatus', statusValue:'0,-9', total:45, type:1}, …]`（`statusName` 在控制台是乱码，以 `statusValue` 为准；状态码表见 bundle `@2236013 MapStatus`） |
| 筛选用分类树 | `mtop.taobao.idle.seller.platform.item.pc.search.categories` | 无参；`{selectIdleCategories:[{categoryId, categoryLevel, categoryName, leaf, root, childrenCategoryDOs:[…]}]}` |
| 筛选维度字典（退货/发货时效/服务保障…19 项） | `mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query` | 无参；`{returnService:[{key,value}], sendGoodsTime:[…], serviceGuarantee:[…]}` |
| 服务端搜索 + 分页 + 排序 | `mtop.alibaba.idle.seller.pc.common.item.search` | POST `{pageNo, pageSize, bizType:'commonPro', searchRequest:'{}', itemStatus:'0,-9'}`；**`searchRequest` 是 JSON 字符串**，筛选条件塞这里；排序 `orderField`/`orderType`（bundle `@4081384`）；分页 10/20/50 |
| 行内可执行操作（含 `itemEdit`/`updatePrice`/`downShelf` 等） | 同上响应的 `itemOperationInfo.operateItemList[]` / `itemExtendList[]` | 我们目前按状态自己推断能做什么，官方是平台下发的 |
| 草稿箱列表 | `mtop.alibaba.idle.item.pc.draft.search` / `draft.get` | `goods-manage` 的 `type==2` 分支 |
| 导出商品 | `mtop.alibaba.idle.seller.platform.item.export` | bundle `@3081743`（请求/响应字段未确定） |

我们**不比官方少**的：商品列表/详情、单条与批量改价改标题改库存、上下架、发布、操作日志 —— 这些是本地能力，
不依赖官方页面。

## 5. 未确定项（写代码前必须再核）

1. 运费模版**非空行**的完整字段表（本机账号模板数为 0，只有 `templateId`/`templateName` 有证据）。
2. `post-temple` 表单里运费规则数组的字段名（只知道列表列 `dataIndex:"excessPrice"`）。
3. `freight.template.item.query`（模版绑定的商品列表）响应字段。
4. `item.export` 的请求/响应字段。
5. `goods-manage` 搜索表单 `F.current` 的键名（被 `JSON.stringify` 进 `searchRequest`，bundle 里看不到）。
6. tab ↔ 状态码的完整映射表（`MapStatus` 有 8 个码，tab 三元组只见到两组）。
7. 直连 mtop 探参数不可靠：不带站点 CODE 头会先撞 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，
   参数形状**以浏览器抓包 + bundle 调用点为准**。
