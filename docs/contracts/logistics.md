# 物流（发货选公司 / 单号认公司 / 修改运单号）契约（读 + 写）

> 为什么单开 `/api/logistics`：这一族是**物流基础设施**（公司字典 + 单号识别 + 改运单号），
> 不属于订单/退款/评价那套交易读写。并进 `/api/trade` 会让「一个前缀里既有订单状态机、
> 又有物流字典」这种边界消失。
>
> 与我们已有的两条发货接口的关系：`backend/app/xianyu/trade_write.py` 的
> `SHIP_OFFLINE_API='mtop.taobao.idle.logistics.merchant.consign.offline'`（真发货）与
> `SHIP_DUMMY_API='…consign.dummy'`（无需物流）**保持不变**；本页是它们的补充：
> 发货时用的公司下拉、单号识别，以及发错之后**改运单号**。

证据来源（零平台请求，全部静态提取）：`C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js`
（3,879,584 字符）与 `C:\Users\Administrator\.dsh-tmp\seller_scan\js_008_vendors.js`（5,857,382 字符）。

## 1. 三个平台接口

| 用途 | mtop | 版本 | 请求 | 响应 |
| --- | --- | --- | --- | --- |
| 快递公司列表 | `com.taobao.idle.logistics.new.get.companies` | 1.0 | **不带 data**（`{api, v}`） | `data.items[]`，元素 `{code, name}`；官方默认值 `OTHER` |
| 单号认公司 | `mtop.taobao.idle.logistics.guess.mailno` | 1.0 | `{mailNo}` | `data.unionCode`（编码）+ `data.cpName`（公司名） |
| 修改运单号 | `mtop.taobao.idle.logistics.merchant.consign.resend` | 1.0 | `{tradeId, mailNo, cpCode}` | 平台信封（无业务 id） |

官方行为：单号输入框 **500ms 防抖**后调 guess；猜出来的公司如果不在下拉里，官方把
`{value: unionCode, label: cpName}` 追加进选项。改运单号在 `trade_bundle/main.js` @2766767
的「修改物流信息」弹窗里，成功 toast「修改成功」。

## 1.1 HTTP 面（3 条，单开 `/api/logistics`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/logistics/{account_id}/companies` | 快递公司列表 |
| GET | `/api/logistics/{account_id}/guess?mail_no=…` | 按单号认公司 |
| POST | `/api/logistics/{account_id}/orders/{order_id}/resend` | 修改运单号 `{mail_no, cp_code, confirm}` |

错误码：`400 confirmation_required`（未确认）/ `400 invalid_fields`（单号空或超 64 字）/
`409 verification_required`（无 Cookie 或已判定需重登）/ `404 not_found`（账号不存在）/
`502 protocol_error`（平台拒绝，附人话）/ `503 xianyu_not_ready`（协议层缺失）。

## 1.2 参数与本地校验

- `mail_no` 必填（strip 后为空 → 400「请输入快递单号」）；上限 64 字（`MAIL_NO_MAX`）。
- `cp_code` 可留空 → 按官方默认 `OTHER` 提交。
- `confirm` **只在服务层判**，且只认 `confirm is True`（`'false'`/`0`/`''`/`None` 一律不放行）。
  路由层**不许**写 `bool(body.get('confirm'))` —— `bool('false')` 是 `True`
  （第 222 条缺陷就是这么绕过写闸门的）。

## 1.3 成功判据（与「不造数据」的对照）

| 情况 | 返回值 |
| --- | --- |
| 平台信封成功 | `saved: true` |
| 平台没回显公司编码 | `echoed: false` + 一条中文 warning（**不是失败**：官方也只 toast「修改成功」） |
| 列表键认不出（`items`/`list`/`companies` 都没有） | `companies: null`（**不是 `[]`**）+ `list_key_missing` warning |
| 平台明确回空列表 | `companies: []` |
| guess 认不出公司 | `cp_code: null`、`cp_name: null` + `no_company_matched` warning，**不猜公司名** |
| 平台拒绝 | 502 `protocol_error` + `platform_errors{api,code,message}`，**不碰 `login_state`** |

## 1.4 三步顺序

写接口一律 **确认（`_confirm_gate`）→ 字段校验（协议层构造，纯本地）→ 建客户端**。
字段校验排在凭据检查之前：单号填错时不应该先听到「去重新登录」。

## 2. 测试

`backend/tests/test_logistics_api.py`：**49 条**（协议 24 / 只读 8 / 闸门 5 / 校验 5 /
happy path 4 / 失败 6 / 路由 3）。其中「三层信封」的回归用例钉住了
`trade.body_of` 的三种容器（`data.module` / `data.data` / `data`）—— 手写一层 `raw['data']`
会在双层回执上悄悄取不到值（第一次跑测试就是栽在这里）。

## 3. AI 侧

3 个工具：`logistics_companies`、`logistics_guess_mailno`（只读、无 `confirm`）+
`logistics_resend_consign`（要 `confirm`，MCP method 为 POST）。
对账口径：`scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 加的是**完整写工具名**
（`logistics_resend_consign`），故意不加 `logistics_` 宽前缀，否则两条只读工具会被误判成写工具。

## 4. 未验证（诚实边界）

- `resend` **一次真机请求都没发过**（会真的改平台物流信息）；只验到路由挂载 + 闸门
  （`confirm:false` → 400 `confirmation_required`，零平台写请求）。
- `cp_code` 用下拉里的编码、以及空编码回落 `OTHER` 时平台是否接受，没有真机样例。
- guess 的失败形状只有「认不出」这一种（真机给 `ZZZ000` 回了 EMS，说明平台会自己猜），
  没遇到平台报错。


## 第 61 批补充：未发货凭证 / 发货地详情（只读）

| method | path | 平台 api |
| --- | --- | --- |
| GET | /api/logistics/{account_id}/orders/{order_id}/unconsign-detail | mtop.taobao.idle.merchant.unconsign.detail |
| GET | /api/logistics/{account_id}/delivery-place | mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query |

**真机证据**：**未真机验证**。只有 bundle 调用点证据：

- unconsign.detail：~/.dsh-tmp/trade_bundle/main.js:1:3138431
  （hO({api:"mtop.taobao.idle.merchant.unconsign.detail", v:"1.0", data:{orderId:v}})），
  官方响应读 resp.data.content 与 resp.data.imageList（PreviewGroup 逐张预览）；
- delivery.place.detail.query：~/.dsh-tmp/trade_bundle/main.js
  （W6 = () => U6({api:"mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query"})，
  **不带 data**），紧邻写接口 merchant.delivery.place.info.set 与只读
  merchant.delivery.address.list.query。

**凭证字段口径**：content / image_list 平台没给就是 null（前端显示「-」）；
平台**明确**给空数组时保留 []（「这一单确实没传图」与「平台没返回这个键」不是一回事）。
两个键都为 null 时服务层加 warnings[].code = no_evidence，**不**把它渲染成「凭证为空」。

**未确定项**：

1. unconsign.detail 除 content / imageList 外是否还有字段未取证 —— 原始业务体放在 raw，
   不猜语义；
2. delivery.place.detail.query 的响应形状**完全未取证**，detail 是原样透出，前端不应依赖具体键；
3. 发货地详情那条在 coverage57 里**没有独立偏移**（bundle 里是个无参箭头函数），
   证据强度低于同族的地址列表接口。

错误码：订单号为空 -> invalid_fields（本地拦，不打平台）。

## 第 61 批补充：区域码（division）

**做什么用**：退货地址新增 / 编辑在真机上被平台拒绝 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数areaId`
（证据链见 `logs/write61/`）—— `areaId` 是**区级**区域码，官方表单里由「所在地区」级联产生。本批把它补齐。

### HTTP 面（只读）

| method | path | 说明 |
| --- | --- | --- |
| GET | `/api/logistics/{account_id}/divisions` | 区域码一级（省 / 市 / 区逐级取）；query `token`（默认 `CN`）、`page_index`（默认 1） |

### 平台接口

`mtop.cainiao.geography.frontend.divisionsinfo.get`（`v='1.0'`，`valueType='division'`），
入参逐字 `{divisionToken: <token>, pageIndex: 1, bizEntrance: 'idle_fish'}`，根 token `'CN'`。
官方调用点：`~/.dsh-tmp/trade_bundle/main.js` 偏移 3,387,049 起（`DivisionSelect`）。

### 响应形状

```json
{"success": true, "data": {
  "account_id": "...", "api": "mtop.cainiao.geography.frontend.divisionsinfo.get",
  "token": "CN^^^北京^^^北京市", "parent_token": "CN^^^北京^^^北京市",
  "divisions": [{"division_id": 110101, "division_token": "CN^^^北京^^^北京市^^^东城区",
                 "label": "东城区", "value": "110101/东城区", "level": 4, "is_leaf": false,
                 "abb_name": "东城", "pinyin": "dong cheng qu"}],
  "count": 17, "total_count": 17, "total_page": 1, "page_index": 1, "page_size": 100,
  "leaf_level": 4, "warnings": [], "source": "platform", "fetched_at": "..."}}
```

- **两层信封**：平台真实数据在 `env['data']['data']`（`env['data']` 本身是 `{data, message, status}`），
  协议层用 `trade.body_of` 摊平（它认得 `data.data` 这层容器）。
- `value` = 官方格式 `divisionId/名称`（bundle：`value: divisionId + '/' + names.CN`）；
  缺 id 或缺名字时给 `''`（**不拼半个**）。
- **`value` 与 `areaId` 的关系**：官方地址提交处（bundle 偏移 3,781,718）
  `areaId = 第三级（区）选项的 value`，`provinceName` / `cityName` / `districtName` = 各级 `label`。
  前端「所在地区」三级级联选到区级后，把该级 `value` 写进表单的 `area_id`（表单里只读展示）。
- 「平台没给」≠「空」：平台没给 `divisions` 键 → `[]` + `warnings[].code = list_key_missing`；
  平台**明确**给空数组 → `[]` 且**无** warning。其它缺字段是 `null`（不是 0 / 空串）。

### 真机证据（2026-10-16，账号 2217970177348，只读）

原始响应 `logs/probe61_division/raw.json`（`L1`/`L2`/`L3` 三层；探针 `logs/probe61_division/probe_fill.py`）：

- `token='CN'` → 31 个省，`totalCount 31 / totalPage 1 / pageSize 100 / parentToken 'CN'`；
- `token='CN^^^北京'` → 1 个市 `CN^^^北京^^^北京市`；
- `token='CN^^^北京^^^北京市'` → 17 个区（如 `CN^^^北京^^^北京市^^^东城区`）。

### 未确定项（诚实边界）

1. **分页**：三级都只遇到 `totalPage = 1`（`pageSize = 100`），**没遇到多页** ——
   `page_index` 原样透传，多页时 `divisions` 的拼接口径**未取证**。
2. **叶子判据**：bundle 用 `isLeaf: level >= 4` 推断；而平台字段 `divisionLeaf` 在 **level=4 的区上实测是
   `false`**（东城区确实没有下级）—— **两者不等价**。所以接口同时透出 `is_leaf`（平台原字段）与 `level`，
   **终止判据用 `level >= 4`**（响应里也回一个 `leaf_level = 4` 给前端），**不要**拿 `divisionLeaf` 收级联。
3. 区域码接口本身可用（真机实测通过）；`backend/app/xianyu/address_write.py:21` 记的
   「`mtop.taobao.idle.division.all.get` 对本机账号无权限」只说明那个 idle 接口没权限，不影响本批。

错误码：`page_index` 不是正整数 / `token` 超过 128 字 -> 400 `invalid_fields`（**本地拦，不打平台**）；
平台拒绝 -> 502 `protocol_error`（原文透出，**不写 `need_relogin`**：无权限 ≠ 掉线）。
