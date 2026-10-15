# 运费模版（官方 `post-temple` 页）契约：读 + 写

> 本文件是 `backend/app/xianyu/item_templates.py` / `backend/app/services/item_templates.py` /
> `backend/app/api/item_templates.py` 的对外契约，覆盖**读 + 写**。
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

## 1. 已实现（读 + 写）

### 1.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/item-templates/{account_id}` | 运费模版列表。Query：`page`（默认 1）、`size`（默认 20，限 1–50）、`template_name`（可选，按名搜索） |
| GET | `/api/item-templates/{account_id}/{template_id}` | 单个模版详情 |

- 前缀用 `/api/item-templates` 而不是挂在 `/api/items` 下：`backend/app/api/items.py` 已有
  `GET /api/items/{account_id}/{item_id}`，同前缀会把 `templates` 当成 `item_id` 吞掉。
- 两个端点都要求登录（`Depends(current_user)`），响应走全局信封 `{success, data}` / `{success:false, message, code}`，
  状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、`verification_required`→409、
  `protocol_error`→502。写方法见 §1.3（只读路径上的 PUT/PATCH/DELETE 仍然 405）。

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

✅ `backend/app/main.py:69 ROUTE_MODULES` 已含 `'item_templates'`（批量挂载见 `main.py` 里的
`importlib.import_module(f'.api.{name}')` 循环；此前的「需要加上」是过时结论，2026-10-15 第五十九批复核时改正）。
`backend/tests/test_item_templates_api.py` 仍自己 `include_router` 到独立 `FastAPI()` —— 那是为了不依赖挂载顺序，不是因为没有挂载。

## 2. 写能力（已实现）

### 2.1 HTTP

| 方法 | 路径 | body | 说明 |
| --- | --- | --- | --- |
| POST | `/api/item-templates/{account_id}/create` | `{confirm, template_name, item_prov?, item_city?, valuation?, is_default?, details?, template_details?}` | 新建模版 |
| POST | `/api/item-templates/{account_id}/{template_id}/modify` | 同 create，另可带 `version` | 修改模版 |
| POST | `/api/item-templates/{account_id}/{template_id}/delete` | `{confirm}` | 删除模版（**不可撤销**） |
| POST | `/api/item-templates/{account_id}/unbind` | `{confirm, item_ids, mode, template_id?, post_price?}` | 给商品设置运费 |

- **`confirm` 必须显式为真**：`services/item_templates.py::_truthy` 只认 `True` / `'true'` / `1`，
  其余（`False`/`'false'`/`'yes'`/`0`/`None`/缺省）一律 400 `confirmation_required`，**且一次平台请求都不发**。
- **三步顺序：确认 → 字段校验 → 建客户端**（`_confirm_gate()` → `_check_*()` → `_write_facade()`）。
  确认在最前（未确认时连字段错都不该先报，否则误导用户「填对了就能发」）；字段校验在建客户端之前
  （纯本地、免费，账号掉线时也该先说「名称没填」而不是「去重新登录」）。顺序由
  `test_gate_runs_before_field_validation` 与 `test_field_validation_runs_before_credential_check` 钉住。
- **扁平命名参数**：四个服务函数的签名是 `create_template(account_id, template_name, item_prov, item_city,
  valuation, is_default, details, template_details, confirm)` 这种**逐字段**形式，不是 `body: dict`。
  原因：AI/MCP 路径的 `agents/tools.py::_filter_args` **只传目标函数签名里有的键** ——
  收 `body` 会让 AI 侧除 `account_id`/`confirm` 之外的字段全被静默丢掉。
- `unbind` 的 `mode` 三选一：`free_ship`（包邮）/ `template`（要 `template_id`）/ `fixed`（要 `post_price`，**单位元**，必须 >0）。
  `item_ids` 收数组或逗号串（`proto.normalize_item_ids`）。
- 校验失败（名称空 / >30 字 / 缺 id / mode 非法 / post_price 非正数）→ 400 `invalid_fields`，同样零平台请求。
- **成功判据是平台业务码**：`res.data.code === 'success'`（bundle 原文）。
  顶层成功但业务码不是 `success`（或压根没有 `code`）→ **502 `protocol_error`**，message 带平台原文 `msg`，
  绝不伪装成成功。回执 `data` 里 `template_id` 拿不到就是 `null`。
- 无权限 / 掉线的口径与只读侧一致：`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 家族走 502 且**不改 `login_state`**；
  真过期走 409 `verification_required`。

### 2.2 平台 mtop 接口（`backend/app/xianyu/item_templates.py`）

| 常量 | api | 请求 data | 证据 |
| --- | --- | --- | --- |
| `CREATE_API` | `mtop.alibaba.idle.seller.platform.pro.freight.template.create` | `{templateName, itemProv, itemCity, valuation, isDefault, templateDetails}` | 声明 `@2204883`、调用点 `@2211045` |
| `MODIFY_API` | `…pro.freight.template.modify` | create 全部字段 + `templateId` + `version` | 声明 `@2204655`、调用点 `@2244766` |
| `DELETE_API` | `…pro.freight.template.del` | `{templateId}` | 声明 `@2203984`、调用点 `@4129523` |
| `UNBIND_API` | `mtop.alibaba.idle.seller.platform.freight.template.pc.item.unbind` | 见下 | 调用点 `@4114059` |

`templateDetails` 是 **JSON 字符串**，元素形如：

```json
{"isDefault": false, "receiveDivisionName": "全国", "firstPrice": "8",
 "excessPrice": "3", "first": "1", "excess": "1", "operate": "add"}
```

协议层 `build_detail_entry()` 负责 snake→camel（`receive_division_name` → `receiveDivisionName` …），
`operate` 缺省补 `add`，**未知键原样透传**（平台以后加字段我们不会丢）。

`UNBIND_API` 的三分支（bundle 原文，初始 `{itemIds, supportFreight:true, onlyTakeSelf:false, needAutoFreight:false}`）：

| mode | 提交的差异字段 |
| --- | --- |
| `free_ship` | `isFreeShip: true`（`supportFreight` 保持 true） |
| `template` | `isFreeShip: false`, `idleTemplateId: <template_id>`, `supportFreight: false` |
| `fixed` | `isFreeShip: false`, `postPriceInCent: 100 * post_price`（`supportFreight` 保持 true） |

空的选填字段**不发**（官方是 `undefined` 而不是空串），例如没填 `item_prov` 时请求里就没有这个键。

### 2.3 站点 CODE 头

写请求与只读侧走同一条路径：`item_templates.site_headers(client)`（内部复用
`client.py::_security_headers()`），发 `idle_site_biz_code: COMMONPRO` + `idle_user_group_member_id: ''`。
不带这个头，平台一律回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。

### 2.4 AI 侧

4 个写工具（`agents/tools.py` + `mcp_server/client.py::TOOL_ROUTES`），`required` 都含 `confirm`：

| 工具 | 服务层 | HTTP |
| --- | --- | --- |
| `item_templates_create` | `item_templates.create_template` | `POST /api/item-templates/{account_id}/create` |
| `item_templates_modify` | `item_templates.modify_template` | `POST /api/item-templates/{account_id}/{template_id}/modify` |
| `item_templates_delete` | `item_templates.delete_template` | `POST /api/item-templates/{account_id}/{template_id}/delete` |
| `item_templates_unbind` | `item_templates.unbind_items` | `POST /api/item-templates/{account_id}/unbind` |

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
