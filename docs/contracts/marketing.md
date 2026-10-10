# 营销（官方 `/marketing` 页）只读契约

> 本文件是 `backend/app/xianyu/marketing.py` / `backend/app/services/marketing.py` /
> `backend/app/api/marketing.py` 的对外契约，**只覆盖只读能力**。
>
> 证据来源：
> - 官方营销微应用 bundle：`https://g.alicdn.com/idle-pc/seller-marketing/0.0.1/`，
>   落盘在 `logs/seller_discover/marketing_src/js_main.js`（3,516,103 字节）与
>   `logs/seller_discover/marketing_src/apis.json`。下文「出现 N 次」都是在这个文件里
>   按字面串 grep 的结果，可直接复核。
> - 菜单接口 `mtop.alibaba.idle.seller.platform.sys.menu.query` 的真路由
>   `logs/seller_discover/menu.json`（营销 entry = `idle-pc/seller-marketing/0.0.1`）。
> - **真机未验证**：账号 `2217970177348` 的 Cookie 在 2026-10-11 已过期
>   （`FAIL_SYS_SESSION_EXPIRED`），四个接口的响应结构没有真机样本，见 §4。

## 1. 已实现（只读）

### 1.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/marketing/{account_id}/items` | 营销商品列表。Query：`page`（默认 1）、`size`（默认 20，限 1–50） |
| GET | `/api/marketing/{account_id}/tasks` | 营销任务 / 营销信息。无 Query |
| GET | `/api/marketing/{account_id}/price-competitiveness` | 价格竞争力。Query：`item_ids`（逗号分隔，可空；空则不按商品过滤） |
| GET | `/api/marketing/{account_id}/records` | 操作记录。Query：`page`、`size` |

- 四个端点都要求登录（`Depends(current_user)`），响应走全局信封
  `{success, data}` / `{success:false, message, code}`，状态码由 `common.FAIL_STATUS` 决定：
  `invalid_fields`→400、`not_found`→404、`verification_required`→409、`protocol_error`→502、
  `not_ready`→503。
- **没有任何写方法**：`POST` / `PUT` / `PATCH` / `DELETE` 在这四条路径上一律 **405**
  （`test_routes_are_read_only` / `test_only_get_is_registered_on_the_router` 钉住）。
- `item_ids` 在 API 层就按逗号切成列表再交给服务层；服务层收的是 **list**，不是字符串
  （AI 工具 `marketing_price_competitiveness` 由 `tools._marketing_price_competitiveness`
  负责同一步翻译）。

### 1.2 响应 `data`

```json
{
  "account_id": "2217970177348",
  "kind": "items | tasks | competitiveness | records",
  "metrics": { "...": "见 §2，平台没给的键也在，值是 null" },
  "items":    "kind=items / competitiveness 时的列表，平台没给则 null",
  "tasks":    "kind=tasks 时的列表，平台没给则 null",
  "records":  "kind=records 时的列表，平台没给则 null",
  "warnings": [{"api": "...", "label": "...", "message": "..."}],
  "unrecognized_keys": ["..."],
  "source": "platform",
  "fetched_at": "2026-10-11 05:50:00",
  "page": 1,
  "page_size": 20
}
```

- 列表字段名由 `_LIST_KEY` 决定：`items`→`items`、`tasks`→`tasks`、
  `competitiveness`→`items`、`records`→`records`。
- `page` / `page_size` 只出现在 `items` 与 `records` 两个分页端点。
- `unrecognized_keys` 只在协议层 `suspect_keys` 非空时出现。

## 2. 契约键（前端只能依赖这些）

| kind | 契约键（`*_METRICS`） |
| --- | --- |
| `items` | `item_count` |
| `tasks` | `running_count`、`total_count` |
| `competitiveness` | `low_count`、`high_count`、`avg_score` |
| `records` | `record_count` |

契约键以协议层为唯一真相（`_METRIC_KEYS_ATTR` 按属性名取），服务层不另抄一份。
**平台没返回的键一定出现且值为 `null`**，前端显示「-」。

## 3. 三条硬口径

1. **平台没返回的指标一律 `null`，禁止 0 顶替。** 0 会让用户以为「真的一个都没有」。
   协议层的 `find_number` 认不出就返回 `None`；真 0 保留为 `0`。
2. **`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 不是掉线。** 它只说明该账号对营销业务没权限。
   协议层在所有接口都因无权限失败时**不抛异常**，如实返回一屏 `null` + 人话 warning（200）。
   只有链路 / 签名 / 登录态故障才翻成 502 `protocol_error`（或 409 + `need_relogin`）。
   本模块**绝不**因为无权限写 `need_relogin`。
3. **只读。** `WRITE_APIS` 里的写接口没有任何调用点。

## 4. 接口名（已按 bundle 原文逐字核对）

| kind | mtop 接口名 | bundle 里出现次数 |
| --- | --- | --- |
| `items` | `mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list` | 1 |
| `info`（→ `tasks`） | `mtop.taobao.idle.bpro.marketing.info.enhance.query.pc` | 1 |
| `competitiveness` | `mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query` | 1 |
| `records` | `mtop.taobao.idle.bpro.marketing.operation.record.list.pc` | 1 |

> ⚠ **已修的缺陷（2026-10-11）**：`PRICE_COMPETITIVENESS_API` 与 `OPERATION_RECORD_API`
> 早先漏了 `bpro.marketing.` 这一段，写成
> `mtop.taobao.idle.item.price.competitiveness.info.query` /
> `mtop.taobao.idle.operation.record.list.pc` —— 两个短名在 bundle 里出现 **0 次**，
> 真机会直接回 `FAIL_SYS_API_NOT_FOUNDED`。当时测试只按常量符号引用，所以没抓住；
> 现已补 `test_mtop_names_match_the_official_bundle` 逐字冻结四个名字。

### 4.1 只登记、不实现的写接口

| mtop 接口名 | 说明 |
| --- | --- |
| `mtop.taobao.idle.bpro.marketing.task.operate.pc` | 营销任务操作（花钱动作） |
| `mtop.taobao.idle.bpro.marketing.task.batch.operate.pc` | 营销任务批量操作 |

两者只出现在协议层 `WRITE_APIS` 元组里，**没有任何调用点**，路由层也不注册对应方法。
`test_write_apis_are_registered_but_never_called` 钉住「登记了但一次都没打」。

## 5. AI 可调用接口

| 工具名 | 映射到 | 备注 |
| --- | --- | --- |
| `marketing_items` | `services.marketing.items` | |
| `marketing_tasks` | `services.marketing.tasks` | |
| `marketing_price_competitiveness` | `services.marketing.price_competitiveness` | 工具侧收逗号分隔**字符串**，由 `tools._marketing_price_competitiveness` 切成列表 |
| `marketing_records` | `services.marketing.records` | |

四个工具同时登记在主管 AI 的 `TOOL_SPECS`（`backend/app/agents/tools.py`）与 MCP 的
`TOOL_ROUTES`（`mcp_server/client.py`）；两边名字集合由
`test_mcp_server.test_no_toolspec_silently_dropped` 对账。

## 6. 未验证项（诚实边界）

- **响应结构无真机样本**：账号 Cookie 已过期，`metrics` 别名表与列表键名都是按 bundle
  调用点推断的；真机若返回别的键名，服务层会如实给 `null`（不伪造 0），但前端看不到列表。
- `item_count` / `running_count` / `total_count` / `low_count` / `high_count` /
  `avg_score` / `record_count` 七个契约键**没有真机值核对过**。
- 分页参数 `page` / `size` 的平台合法区间（1–50）沿用其它复刻页的口径，未经本接口真机确认。
