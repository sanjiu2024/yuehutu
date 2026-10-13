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
