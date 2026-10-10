# 小铺组（子账号管理 / 客服分流）只读契约

> 本文件是 `backend/app/xianyu/shop.py` / `backend/app/services/shop.py` /
> `backend/app/api/shop.py` 的对外契约，**只覆盖只读能力**。
>
> 证据来源（2026-10-10，账号 `2217970177348`，全部可复核）：
> - **真机只读探针**：`logs/seller_discover/shop.json`（子代理跑的原文 body/响应）、
>   `logs/seller_discover/shop_notify_probe.json`（我补探的 8 个接口，间隔 3s，零风控）。
>   探针脚本 `C:\Users\Administrator\.dsh-tmp\probe_shop_notify.py`。
> - **官方微应用 bundle**（静态 CDN，零风控）：
>   `idle-seller-account-management/0.0.8/js/main.js`、
>   `idle-seller-cs-dispatch/0.0.4/js/main.js`（落盘在 `logs/seller_discover/bundles/`，
>   接口名清单 `logs/seller_discover/shop_bundles_main.json`）。
> - **契约测试**：`backend/tests/test_shop_api.py`（32 用例，样本即真机原文）。

## 0. 一句话结论

平台是**双层信封**：外层 `{success, ret, message, data}`，业务层在 `data` 里再包
`{code:"success", data, msg}`（前端判定 `data.code`，失败弹 `data.msg`）；
但也有接口**根本没有**内层信封（`customer.group.get` / `routing.rule.info.get`），
这时整段 `data` 就是载荷。服务层两种都认。

| 能力 | 真机结果 | 结论 |
| --- | --- | --- |
| 子账号列表 `common.sub.account.info.list` | `SUCCESS`（该账号 `list: []`, `total: 0`） | 可读；`pageNo` **必传**，缺了报 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数pageNo` |
| 客服分流规则 `customer.routing.rule.info.get` | `SUCCESS`，`routingRules` 是**对象**（只有 `serviceTime`） | 可读；无 `offlineMessage` / `serviceRange` |
| 客服分组 `customer.group.get` | `SUCCESS`，`csGroupInfos: []`, `total: 0` | 可读 |
| 客服列表 `customers.info.get` | `SUCCESS` 但 `data: {}`（**连 `customers` 键都没有**） | 可读但**拿不到列表** → `agents: null` |
| 子账号配额 `common.sub.account.quota.query` | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | 账号对该业务无权限 → `quota: null` |
| 角色列表 `platform.user.role.list` | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | 同上 → `roles: null` |
| 小铺昵称 `platform.usergroup.nick.get` | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::查询店群用户信息` | 同上（本模块不调） |
| 店铺详情 `idle.shop.manage.detail` | `FAIL_BIZ_COMMON_SYSTEM_ERROR3::闲鱼太累了…` | 平台侧报错（本模块不调） |

`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 是**账号对这个业务没权限**，不是掉线 ——
所以它**不写 `need_relogin`**，用户不会被逼着重新扫码（同批探测里不带站点头的
`customer.group.get` 是成功的，排除了「缺站点头」的解释）。

## 1. 已实现（只读）

### 1.1 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/shop/{account_id}/sub-accounts` | 子账号列表。Query：`page`（默认 1）、`size`（默认 20，限 1–100）→ 平台 `pageNo`/`pageSize` |
| GET | `/api/shop/{account_id}/cs-routing` | 客服分流：规则 + 分组 + 客服列表。Query：`page`、`size`（只作用于客服分组） |

- 两个端点都要求登录（`Depends(current_user)`），响应走全局信封
  `{success: true, data: {...}}` / `{success: false, message, code, login_state?}`。
- 状态码由 `common.FAIL_STATUS` 决定：`invalid_fields`→400、`not_found`→404、
  `verification_required`→409、`protocol_error`→502、`xianyu_not_ready`→503。
- **没有任何写方法**：POST/PUT/PATCH/DELETE 一律 405（`test_shop_routes_are_read_only` 锁死）。
- 分页参数在 API 层收成 `str` 再交给服务层校验，非法值一律 `400 invalid_fields`
  （`size=0` / `size=101` / `size=abc` / `page=x` / `page=0` / `page=-1`），
  且**一次平台都不打**（早先 `page=0` 被 `max(1,...)` 静默吞成 1，而 `size=0` 报 400，
  同类错误两种结果，已统一）。

### 1.2 返回字段

`sub-accounts`：

| 字段 | 来源 | 口径 |
| --- | --- | --- |
| `account_id` | 入参 | |
| `page` / `page_size` | 平台 `pageNo`/`pageSize`（缺则回显入参） | |
| `total` / `total_page` / `has_next_page` | 平台 `total`/`totalPage`/`hasNextPage` | 平台明说的数字照用（真机 `0` 是真值）；缺 → `null` |
| `items` | 平台 `list` **原样透传** | **缺键 → `null`** + 中文 warning（缺键 ≠ 「没有子账号」）；平台给了空数组才给 `[]`。**不重命名、不补字段**：该账号 `list` 为空，列表行字段名**没有真机样例**（bundle 里只见到 `subId`/`mobilePhone`/`roleId` 等线索），宁可让前端拿到什么渲染什么 |
| `quota` | `quota.query`（best-effort） | 失败/无权限 → `null` + 中文 warning，**不是 0** |
| `roles` | `user.role.list`（best-effort） | 载荷是**数组**（`[{roleId, roleName}]`，bundle 静态证据）；**任何非数组**（失败、给了对象/标量）→ `null` **必附中文 warning**（真机该账号是 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`） |
| `warnings` | 本层 | 中文，逐条说明哪个字段为什么是 null |
| `fetched_at` | 本层 | UTC `%Y-%m-%d %H:%M:%S` |
| `source` | 本层 | 恒为 `'platform'` |

`cs-routing`：

| 字段 | 来源 | 口径 |
| --- | --- | --- |
| `routing.raw` | 平台 `routingRules` 原样 | 真机是对象不是数组；**连 `routingRules` 都没有 → `null`**（不返回 `{}` 冒充） |
| `routing.service_time` | `serviceTime` → `{start, end}` | 平台给了 `serviceTime` 才给对象，否则 `null`（不返回 `{'start':null,'end':null}` 冒充） |
| `routing.offline_message` / `routing.service_range` | `offlineMessage` / `serviceRange` | 真机没有 → `null` + `WARN_ROUTING_PARTIAL`；**连 `routingRules` 都没返回**时用另一条 `WARN_ROUTING_MISSING`（「整个没返回」和「只返回部分」是两回事，不共用一条 warning） |
| `groups` / `groups_total` | `csGroupInfos` / `total` | 真机 `[]` / `0`（平台真值）；**缺 `csGroupInfos` → `null`** + 中文 warning（缺键 ≠ 没有分组） |
| `agents` | `customers` 键 | **键不存在 → `null`（不是 `[]`）**，平台给了空数组才给 `[]`——「缺键 ≠ 没有客服」，附中文 warning |

## 2. 只读保证

`backend/app/xianyu/shop.py` 里 `READ_APIS` 是白名单、`WRITE_APIS` 是同一批 bundle
里的**写接口**（注册/删除子账号、改手机号/密码/角色、改分流规则、改客服分组、
图片上传/OSS 签名、店铺验证、短信、退款管理 —— 名单来自
`logs/seller_discover/shop_bundles_main.json`）。写接口**故意不实现**，列出来是为了让
「只读」可被断言：`test_no_write_api_is_ever_called` 跑完两个页面后，逐个检查假客户端
记录的 mtop 调用必须落在 `READ_APIS` 里。

## 3. 挂载

`backend/app/main.py` 的 `ROUTE_MODULES` 已挂 `'shop'`，
`test_real_app_mounts_shop_router` 会断言真实 app 里两条路径都在：
`/api/shop/{account_id}/sub-accounts`、`/api/shop/{account_id}/cs-routing`
（**不做 skip**：谁把 `'shop'` 从 `ROUTE_MODULES` 里删掉，该用例直接变红）。
