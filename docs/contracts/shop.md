# 小铺组（子账号管理 / 客服分流）契约（读 + 写）

> 本文件是 `backend/app/xianyu/shop.py` / `backend/app/xianyu/shop_write.py` /
> `backend/app/services/shop.py` / `backend/app/api/shop.py` 的对外契约。
> §0~§3 是**只读**能力，**§4 是写能力**（客服分流规则保存 + 客服分组增删，2026-10-13 落地）。
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
`logs/seller_discover/shop_bundles_main.json`）。**只读路径**一条都不许碰写接口：
`test_no_write_api_is_ever_called` 跑完两个页面后，逐个检查假客户端记录的 mtop 调用
必须落在 `READ_APIS` 里。

其中最后两条（`customer.routing.rule.operate`、`customer.group.operate`）**已经实现**
（见 §5），但**故意继续留在 `WRITE_APIS` 黑名单里** —— 黑名单管的是「只读路径」，
写路径走 `xianyu/shop_write.py` 自己的白名单（`ALL_APIS` 只有这两个接口名）。

## 3. 挂载

`backend/app/main.py` 的 `ROUTE_MODULES` 已挂 `'shop'`，
`test_real_app_mounts_shop_router` 会断言真实 app 里两条路径都在：
`/api/shop/{account_id}/sub-accounts`、`/api/shop/{account_id}/cs-routing`
（**不做 skip**：谁把 `'shop'` 从 `ROUTE_MODULES` 里删掉，该用例直接变红）。
写侧三条 POST 由 `test_shop_write_api.py::ShopWriteRouteTest` 钉住。

## 4. 写能力（已实现，2026-10-13）

证据来源（**零平台请求**）：官方客服分流微应用 bundle
`C:\Users\Administrator\.dsh-tmp\csdispatch\main.js`（1,168,301 字符单行压缩 JS，
所以只有**字符偏移**、行号无意义）；取证脚本 `cs_enums.py` / `cs_logic.py` /
`cs_enums2.py`（输出 `logs/cs_enums.log`、`logs/cs_logic.log`、`logs/cs_enums2.log`）。

### 4.1 两条 mtop

| 动作 | mtop | v | 参数 | bundle 偏移 |
| --- | --- | --- | --- | --- |
| `routing_rule` | `mtop.taobao.idlemessage.customer.routing.rule.operate` | `1.0` | `{routingRules: <JSON 字符串>}` | 声明 @571596；保存调用点 @1151612 |
| `group_save` / `group_delete` | `mtop.taobao.idlemessage.customer.group.operate` | `1.0` | `{operateType, groupId?, groupName?, serviceRange: <JSON 串>, customers: <JSON 串>}` | 声明 @572287；保存 @1129731；删除 @1149031 |

`routingRules` 的字段名逐字照官方：`distributionMethod`、`repeatCustomer`、
`offlineMessage`、`specifiedAccountId`（**数字**，官方写的是 `Number(_)`）、
`specifiedAccountNick`、`serviceTime`。后两个只在 `offlineMessage ===
'SPECIFIED_ACCOUNT'` 时出现，且官方**两者都必填**（校验原文「请选择指定的客服账号」）。

分组保存里 `serviceRange` 是 `{mode, specifiedConfig?}`，`specifiedConfig` 只在
`mode` 为 specified 时出现（`{type, typeName, typeData}`，官方 `type` 缺省 `'ORDER'`，
`typeData` 空数组时官方**不放这个键**）；`customers` 是 `[{customerUid: <数字>}]`。
删除**只有两个键**：`{operateType:'delete', groupId}`。

### 4.2 枚举（官方 `Iy` / `Dj`，中文标签逐字来自 bundle）

| 值 | 中文 |
| --- | --- |
| `LOW_SATURATION_FIRST` | 优先分配给当前饱和度低的客服 |
| `LOW_DAILY_COUNT_FIRST` | 优先分配给当日接待量少的客服 |
| `LAST_SERVED_AGENT` | 最近联系客服优先 |
| `COMBINED_LOW_LOAD` | 正常分流 |
| `MAIN_ACCOUNT` | 离线消息发给主账号 |
| `SPECIFIED_ACCOUNT` | 离线消息发送给指定账号 |

- **`repeatCustomer` 是枚举字符串，不是布尔**：官方加载路径是
  `Ay[c.repeatCustomer]`，而 `Ay` 只登记了上面 4 个分配方式值 ⇒ 与
  `distributionMethod` 共用同一套值。官方表单可能只给子集，但 bundle 里**没有**
  可引用的子集定义 ⇒ 我们不缩窄，非法值由平台拒。
- **分组 `mode` 两侧大小写不一致**：官方写侧枚举是**小写**
  （`UNSPECIFIED="unspecified"` / `SPECIFIED="specified"`），渲染判定却用大写
  （`"SPECIFIED"===t.mode` / `"DEFAULT"===t.mode`）。我们没有平台侧证据判断哪个对，
  ⇒ **原样透传调用方给的值、不做大小写转换**（`GROUP_MODES` 同时接受两种写法）。

### 4.3 HTTP

| 方法 | 路径 | body | 说明 |
| --- | --- | --- | --- |
| POST | `/api/shop/{account_id}/cs-routing/rule` | `{distribution_method, repeat_customer, offline_message, specified_account_id?, specified_account_nick?, service_time?, confirm}` | `service_time` 形如 `{start_time:'08:00', end_time:'23:00'}` |
| POST | `/api/shop/{account_id}/cs-groups` | `{operate_type:'create'\|'update', group_id?, group_name, mode, specified_type?, specified_type_name?, type_data?, customer_uids?, confirm}` | `update` 必须带 `group_id`；`customer_uids` 是字符串数组（后端转 int） |
| POST | `/api/shop/{account_id}/cs-groups/delete` | `{group_id, confirm}` | |

- 返回：规则 `{account_id, action, api, saved, submitted, rule, warnings[], fetched_at, source}`；
  分组 `{…, operate_type, group_id, saved, submitted, warnings[], …}`；删除
  `{…, operate_type:'delete', group_id, deleted, …}`。`rule` / `group_id` 是平台回显，
  **平台没回显就是 `null` + 中文 warning**（不造值）。
- `confirm` **原样交服务层判**（HTTP 层不做 `bool()`）—— `bool('false')` 是 `True`，
  会把用户明确的拒绝当成同意。服务层用 `_truthy`（只有 `True`/`'true'`/`1` 算点头）。
- 三步顺序固定：**确认 → 字段校验（协议层构造）→ 建客户端**。字段校验在建客户端之前，
  所以掉线账号 + 空分组名会先得到 400 `invalid_fields`（而不是「去重新登录」）。
- 错误码：400 `confirmation_required` / 400 `invalid_fields` / 409 `verification_required` /
  404 `not_found` / 502 `protocol_error`（平台拒绝，**不改登录态**，附 `platform_errors`）/ 503。
- **成功判据两层都看**：`shop_write.envelope_error()` 先看客户端层 `success is False`，
  再看业务信封 `data.code`（只认 `success`）。只看外层会把「外层成功、内层失败」的
  双层信封判成保存成功 —— 这是最危险的一类假成功（只读侧同款证据见
  `services/shop.py::_unwrap` 与 `test_shop_api.py::test_sub_accounts_business_error_inside_envelope_is_502`）。

### 4.4 测试

`backend/tests/test_shop_write_api.py`（60 用例）：协议层 27 条纯函数、
确认闸门 5 条、字段校验 7 条、happy path 11 条、失败路径 7 条、路由 2 条，
外加 `test_shop_api.py::test_shop_read_routes_are_get_and_write_routes_are_post`
（读两条 GET + 写三条 POST 逐条钉住）。

### 4.5 未验证

- **一次真机写请求都没发过**：客服分流写接口在真机上的权限与响应形状未知
  （只读侧 `quota.query` / `user.role.list` 对该账号就是 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`）。
- 官方的 `routingRules` / `csGroupInfos` **回显形状**没有真机样例，所以 `rule` /
  `group_id` 拿不到时一律 `null` + warning，不做猜测。



---

## 5. 店铺资料（读 + 写，第三十二批）

### 5.1 接口

```text
GET  /api/shop/{account_id}/profile
     -> {account_id,user_nick,shop_desc,user_avatar,industry,alipay_account,
         business_type,pro_shop_valid_time,raw,warnings,source,fetched_at}
POST /api/shop/{account_id}/profile/nick    {nick,confirm}
POST /api/shop/{account_id}/profile/desc    {desc,confirm}
POST /api/shop/{account_id}/profile/avatar  {avatar_url,confirm}
     -> {account_id,action,api,saved,nick|desc|avatar_url,platform_message,warnings,fetched_at,source}
```

- 平台 mtop：`mtop.alibaba.idle.shop.manage.detail` / `...nick.update` / `...desc.update` / `...avatar.update`（v1.0）。
- 官方读侧返回**扁平**结构（`userNick`/`shopDesc`/`userAvatar`/`industry`/`alipayAccount`/`businessType`/`proShopValidTime`），
  不是嵌套；我们的 `payload_of` 也兼容「外面包一层 `data`」。
- 官方写侧成功判据是 `result.success`，失败读 `result.data.message` —— 与子账号族（看 `code`/`msg`）**不是同一套**，
  所以协议层按动作分别判。
- 校验（官方 bundle 原文）：昵称 `trim` 后 4~12 字；头像必须 `http(s)://`；简介非空（官方没有长度上限）。
- 平台没返回的字段一律 `null`（服务层 7 个键都显式给），前端显示「-」；`raw` 是平台原样 payload，键名不翻译。
- 写入成功**平台不回显整份资料** ⇒ 服务层不回填本地值，前端重新拉一次只读接口。

### 5.2 测试与未验证

`backend/tests/test_shop_profile_api.py`（47 用例）：协议层纯函数、读、确认闸门、字段校验先于凭据、
写 happy path、失败路径、路由形状。

- **一次真机写请求都没发过**（昵称/简介/头像的写入权限与回显形状未知；只读侧 `shop.manage.detail`
  在真机是 200 有数据）。
- 头像是「给地址」，本机不上传文件；`shop.manage.sms.send` / `phone.update` / `verify.update`（实名材料）
  **故意没做**（要短信验证码 / 上传资质，风险与形状都未验证）。

---

## 6. 子账号（读 + 写，第三十二批）

### 6.1 接口

```text
GET  /api/sub-accounts/{account_id}/roles          -> {account_id,roles,total,warnings,api,fetched_at,source}
POST /api/sub-accounts/{account_id}                     {nick,mobile,custom_name?,role_ids,password,confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/delete      {confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/mobile      {mobile,confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/roles       {role_ids,confirm}
POST /api/sub-accounts/{account_id}/{sub_id}/password    {password,confirm}
```

- 平台 mtop（v1.0）：`...platform.user.role.list`（读）、
  `...platform.common.sub.account.register`、`...platform.sub.account.{delete,mobilephone.update,role.update,password.update}`、
  `...platform.sub.account.sub.pk`（取加密公钥）。
- `role_ids` 收**整数数组或逗号串**（含中文逗号）—— UI 传数组、AI 工具传串，两条路都要能用。
- 校验（官方 bundle 原文）：手机号 `^1[3-9]\d{9}$`（先去掉空格与横线；**裸整数也接受**，11 位不丢精度）；
  密码 `^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,15}$`；昵称非空；岗位去重保序、上限 50。
- **密码在服务端加密**：`sub.account.sub.pk` 拿 `{publicKey, cryptoKey}`，用纯 Python 实现
  RSAES-PKCS1-v1_5（`backend/app/xianyu/sub_account.py::encrypt_password`，`secrets` 造非零填充）
  加密后提交；**前端明文发给本机后端**，日志 / 返回值 / 事件里都不含密码。
  公钥拿不到 ⇒ 502 且**零提交**（不会拿明文去试）。
- 平台没回显 `subId` ⇒ `sub_id: null` + 中文 warning，不造 id。
- 写成功一律 `log_operation(account_id, 'sub_account_*', …, kind=None)`（**不要传 `kind`**：
  `log_operation` 的 action 列取 `kind or action`，传了会把审计名盖成 `shop`）+ `publish('shop.changed')`。

### 6.2 测试与未验证

`backend/tests/test_sub_accounts_api.py`（63 用例）：协议层（含内嵌 1024 位测试密钥、解回明文校验）、
读、确认闸门、字段校验先于凭据、写 happy path（含「密码不出现在响应/日志里」）、失败路径、路由形状。

- **一次真机写请求都没发过**；只读侧 `sub.account.info.list` / `user.role.list` 对该账号是
  `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（没权限，不是 bug）。
- 岗位列表 `roleId`/`roleName` 的真机形状未知 ⇒ `roles: null` + warning，不造空数组。


## 第 61 批补充：发布页服务开通状态（只读）

| method | path | 平台 api |
| --- | --- | --- |
| GET | /api/shop/{account_id}/pc-service-status?service_code= | mtop.alibaba.idle.pc.service.status.query |

**真机证据**：**未真机验证**。bundle 调用点
logs/seller_discover/item_templates_src/js_main.js:1:4141260：
ybe = e => gC({api:"mtop.alibaba.idle.pc.service.status.query", data:{serviceCode:e}})。

**注意**：官方这条包装**没写 v**（同族 pc.service.open 也没有），本模块其余接口都带
v:"1.0"；未真机验证，所以先按本模块既有口径带版本号。若平台回
FAIL_SYS_ILLEGAL_ARGUMENT，按真机证据再改。

**响应形状未取证**：data 是平台业务体原样透出；平台没给业务体时是 null + warnings，
**不补 false** —— 补 false 会变成「这项服务没开通」的假事实。

**未确定项**：

1. serviceCode 的取值集合（FAST_REFUND / INSPECTION / ...）没有真机确认，目前原样透传；
2. 响应里表示「已开通」的字段名未取证，前端不应依赖具体键。

**不接的写接口**：mtop.alibaba.idle.pc.service.open（同一 bundle 调用点 :1:4141359，
bbe = e => gC({api:"mtop.alibaba.idle.pc.service.open", data:{...e}})）语义是**开通服务**，
是变更不是查询；且入参 e 的形状只到调用点这一层（bbe(e) 的调用方未取证），
接不出可靠的写闸门，所以本批不接，只登记进 WRITE_APIS 黑名单供「只读路径不碰写接口」断言。

## 幂等键（`Idempotency-Key`，第 63 批接入）

带 `Idempotency-Key` 请求头时，**同一键 + 同一指纹**（`account_id` / action / item_id / payload
的 sha256，见 `backend/app/services/common.py::idempotency_fingerprint`）只打平台一次；重复请求
回放上次响应，响应体里带 `idempotent_replay: true`。同键不同指纹 → `409 conflict`
（`code='conflict'`）。**不带请求头时行为与接入前完全一致**。

请求头由 `backend/app/main.py::IdempotencyKeyMiddleware`（纯 ASGI）放进 contextvar，
服务层写漏斗 `common.idempotent_call(account_id, action, item_id, payload, call)` 读取。

| 写族 | 幂等 action | 接入点 |
| --- | --- | --- |
| 推广（超级擦亮） | 请求体 `action`（如 `campaign_edit`） | `backend/app/services/ad_write.py::_write` |
| 安全中心 | 请求体 `action`（如 `security_appeal_submit`） | `backend/app/services/security_write.py::_submit` |
| 客服分组 / 分流规则 | `cs_group_save` / `cs_group_delete` / `cs_routing_rule_save` | `backend/app/services/shop.py` |
| 退货地址 | 路由 action（`add` / `update` / `delete` / `default`） | `backend/app/services/trade_write.py::_address_submit` |
| 订单发货 / 标记发货 | `update_shipment` / `deliver` | `backend/app/services/orders.py` |

回归用例（每个写族至少一条「同键同载荷只打平台一次 + `idempotent_replay`」）：
`backend/tests/test_ad_write_api.py::AdWriteIdempotencyTest`、
`backend/tests/test_security_write_api.py::SecurityWriteIdempotencyTest`、
`backend/tests/test_shop_write_api.py::ShopWriteIdempotencyTest`、
`backend/tests/test_address_write_api.py::AddressWriteIdempotencyTest`、
`backend/tests/test_services_orders.py::OrdersServiceTest::test_update_shipment_same_key_hits_the_protocol_once`、
`::test_deliver_same_key_writes_once`。
