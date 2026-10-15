# seller.platform 一族 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 根因（62）

- 日期：2026-10-16
- 项目：`C:\Users\Administrator\Projects\xianyu-ai-workbench`（HEAD=5562490）
- 账号：2217970177348
- 性质：只读实验（未改任何代码；未跑 reconcile）

## ① 一句话结论

**根因是 H2（请求头缺失），不是 H1（账号无权限包），也不是 H3（网关/appKey 不同）：**
官方 PC 卖家工作台的 mtop 请求**固定携带 `idle_site_biz_code` 请求头**，而我们的通用 mtop 调用路径
（`backend/app/xianyu/mtop.py` 的 `call_params`）**不带这个头**，服务端因此在**鉴权阶段**直接拒绝，
响应体里把原因写得很直白：`"errorMsg":"站点CODE不能为空"`。
给同一个接口补上 `idle_site_biz_code: COMMONPRO` 后**立刻 `SUCCESS::调用成功`**（同一 cookie、同一 sign 算法、同一域名、同一 appKey、同一 `v`）。

**唯一例外**：`mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query` 即使补上头仍然拒绝，
它要求的是**另一个独立权限码 `merchant:deliveryadress:query`**（`errorMsg:"用户无权限访问当前接口"`）——
这一条才是真实的账号权限缺失（H1 只对这一条成立）。

## ② 证据表

探针：直连 `https://h5api.m.goofish.com/h5/<api>/<v>/`，POST form `data=<json>`，
`sign=md5(token&t&34839810&data)`，cookie 取自 `app.db` 的 `account_credentials`（账号 2217970177348）。
串行、每次间隔 ≥2 秒。原始响应全部落盘在 `logs/fail62/perm/`。

### 2.1 主实验 —— 代表接口 `mtop.taobao.idle.seller.platform.pc.item.status.statistics`（v=1.0）

| # | 变体 | HTTP | 平台码 `ret[0]` | 说明（errorMsg / permissionCode） | 原始响应文件 |
|---|---|---|---|---|---|
| 10 | ① 现状（= 我们客户端的形状） | 200 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | `站点CODE不能为空` / `item:search:count` | `logs/fail62/perm/10_v1_baseline.json` |
| 11 | ② 加官方 headers + ext_querys | 200 | `SUCCESS::调用成功` | 返回真实统计：`itemStatus … total=44` | `logs/fail62/perm/11_v2_official_headers.json` |
| 12 | ③ 官方信封（`type=POST`）+ 官方 headers | 200 | `SUCCESS::调用成功` | 与 ② 同；说明 `type` 不是变量 | `logs/fail62/perm/12_v3_official_envelope_type_POST.json` |
| 13 | ④ 换 `v=2.0` | 200 | `FAIL_SYS_API_NOT_FOUNDED::请求API不存在` | 接口只有 1.0，`v` 不是变量 | `logs/fail62/perm/13_v4_version_2.0.json` |

② 的“官方 headers”= `idle_site_biz_code: COMMONPRO` + `idle_user_group_member_id: ''` +
`origin: https://seller.goofish.com` + `referer: https://seller.goofish.com/`；
“ext_querys” = `spm_pre` + `log_id`（官方 bundle 的 `ext_querys:{spm_cnt,spm_pre,log_id}` 里我们只发了 `spm_cnt`）。

### 2.2 收敛实验 —— 到底是哪一个头

| # | 变体 | HTTP | 平台码 | 结论 | 原始响应文件 |
|---|---|---|---|---|---|
| 20 | 只加 `idle_site_biz_code: COMMONPRO` | 200 | `SUCCESS::调用成功` | **单头即可解锁** | `logs/fail62/perm/20_only_idle_site_biz_code.json` |
| 21 | 只加 `origin` + `referer` | 200 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | origin/referer 无效 | `logs/fail62/perm/21_only_origin_referer.json` |
| 22 | 只加 `spm_pre` + `log_id` | 200 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | 查询串参数无效 | `logs/fail62/perm/22_only_spm_pre_log_id.json` |
| 23 | 加 `idle_site_biz_code: ''`（空值） | 200 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | `用户无权限访问当前站点[]` → **不能为空**，必须是有效站点码 | `logs/fail62/perm/23_empty_site_biz_code_hdr.json` |

### 2.3 横向验证（补上 `idle_site_biz_code: COMMONPRO` 后）

| # | 接口 | HTTP | 平台码 | 结论 | 原始响应文件 |
|---|---|---|---|---|---|
| 30 | `...seller.platform.item.pc.search.categories` | 200 | `SUCCESS::调用成功` | 解锁（body `data.code=success`，返回 `selectIdleCategories`） | `logs/fail62/perm/30_categories_with_hdr.json` |
| 31 | `...seller.platform.order.backfill.report` | 200 | `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数orderId` | **鉴权已过**，只差业务参数 | `logs/fail62/perm/31_backfill_report_with_hdr.json` |
| 32 | `...merchant.delivery.place.detail.query` | 200 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | 仍拒绝；`用户无权限访问当前接口` / `merchant:deliveryadress:query` | `logs/fail62/perm/32_delivery_place_with_hdr.json` |
| 40 | `...seller.platform.query.login.merchant.info` | 200 | `SUCCESS::调用成功` | 本账号**确实是** seller.platform 用户：`displayNick=xy603139480502`, `isAdmin=true`, `isMock=false` | `logs/fail62/perm/40_login_merchant_info.json` |
| 41 | `...seller.platform.usergroup.member.list` | 200 | `SUCCESS::调用成功` | `total=0` → 本账号不是子账号/商家组成员，故 `idle_user_group_member_id` 本就为空 | `logs/fail62/perm/41_usergroup_member_list.json` |
| 42 | `...merchant.delivery.place.detail.query`（再加 `idle_user_group_member_id: 1`） | 200 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | 同 32，组 ID 不是变量 | `logs/fail62/perm/42_delivery_place_with_group_hdr.json` |

### 2.4 对照组（证明探针/sign/cookie 本身没问题）

| # | 接口 | HTTP | 平台码 | 说明 | 原始响应文件 |
|---|---|---|---|---|---|
| 00 | `mtop.idle.web.user.page.head` | 200 | `FAIL_BIZ_CLIENT_PARAM_INVALID::不合法的参数，请检查` | 鉴权已过，只是缺参数 | `logs/fail62/perm/00_control_user_page_head.json` |
| 01 | `mtop.alibaba.idle.seller.pc.item.stats.query` | 200 | `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数itemId` | 鉴权已过，只是缺参数 | `logs/fail62/perm/01_control_item_stats.json` |

**口径说明**：probe60/probe61 里记录的 **502** 是本地 API 把 `protocol_error` 映射成 502 的结果，
**上游 mtop 的 HTTP 状态一律是 200**，真正的判据是 `ret` 里的平台码。看 502 会误导排查方向。

## ③ H1 / H2 / H3 逐条判定

### H1「账号真的没有该权限包」—— **排除（4/5 个接口），仅对 delivery.place 成立**

- 排除依据（最硬的一条）：补上 `idle_site_biz_code` 后，同一账号、同一 cookie、同一 `sign`、
  同一域名与 appKey 下，`...pc.item.status.statistics` 与 `...item.pc.search.categories` 直接 `SUCCESS`，
  并返回了真实数据（`itemStatus` 统计 `total=44`、`selectIdleCategories` 完整类目树）。
  账号若无权限包，**不可能**因为一个头就返回业务数据。
- 反证：`...query.login.merchant.info` 返回 `isAdmin=true`、`isMock=false`，说明本账号**有** PC 卖家身份。
- `...order.backfill.report` 补头后从 `IDLE_USER_UNAUTHORIZED` 变成 `FAIL_SYS_BIZPARAM_MISSED(缺少业务参数orderId)`
  —— 错误码从「鉴权层」推进到「业务参数层」，是鉴权已通过的直接证据。
- **例外**：`...merchant.delivery.place.detail.query` 补头后仍拒绝，且返回**独立权限码**
  `permissionCode:"merchant:deliveryadress:query"`（`errorMsg:"用户无权限访问当前接口"`，措辞与「站点 CODE 为空」不同）。
  → 这一条接口上 **H1 成立**：本账号确实没有该权限。是否只在特定商家类型/子账号下可用，本轮**未定**（未做进一步验证）。
- 「官方 PC bundle 里由哪个页面/微应用调用」已核到：
  - `...pc.item.status.statistics` / `...item.pc.search.categories` → 商品发布/管理微应用
    （`logs/seller_discover/item_templates_src/js_main.js`，走 `gC({api:...,v:"1.0",showErrorToast:true,needLoginPC:true})`）；
  - `...order.backfill.report` → 卖家数据中心「验货报告」微应用
    （`C:\Users\Administrator\.dsh-tmp\trade_bundle\main.js` 偏移 2608339 附近，`hO({api:...,v:"1.0",data:{orderId:e}})`）；
  - `...merchant.delivery.place.detail.query` → 商家发货地设置页
    （同文件偏移 3142423 附近，`W6 = () => U6({api:"mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query"})`）。
  - 这些页面**都是** PC 卖家工作台（`seller.goofish.com`）内的页面，不是子账号专属入口。

### H2「少了官方带的 header 或参数」—— **成立，且精确定位到单个请求头**

- 成立：官方 bundle 里所有 xianyu 侧 mtop 包装器（`hO` / `gC` / `A0→U6`）都写死同一段配置：
  `{v:"1.0",data:{},type:"POST",appKey:"34839810",accountSite:"xianyu",dataType:"json",timeout:2e4,
  needLoginPC:false,showErrorToast:false,ext_querys:{spm_cnt,spm_pre,log_id}}`，
  并额外注入 `ext_headers:{idle_site_biz_code, idle_user_group_member_id}`（取自
  `window.__PC_SELLER_WORKBENCH__.userInfo`），同时设 `lib.mtop.config.mainDomain="goofish.com"`、
  `subDomain="m"`、`LibMtopRequest=true`、`H5Request=true`。
- 精确定位：变体 20/21/22/23 把变量逐个隔离 —— **只有 `idle_site_biz_code`（非空）能解锁**；
  `origin`/`referer` 无效，`spm_pre`/`log_id` 无效，空值无效。
- 服务端自述：`errorMsg:"站点CODE不能为空"`（变体 10）与 `errorMsg:"用户无权限访问当前站点[]"`（变体 23）。
- 我们的现状：`backend/app/xianyu/mtop.py` 的 `call_params()` 只拼 `spm_cnt`，**没有任何 `idle_site_biz_code`**；
  `backend/app/xianyu/client.py:162` 其实**已经有** `SECURITY_SITE_BIZ_CODE = 'COMMONPRO'`，
  但只在 `client.py:1155 _security_headers()`（`client.py:1158`）里用，且**只服务于**
  `mtop.idle.pc.security.score.get` / `mtop.idle.pc.security.violation.list` 两个接口（`extra_headers` 参数），
  通用 `mtop()` 路径拿不到它。
- 未区分的细节（诚实标注）：只测了 `COMMONPRO` 与空串两种值，**没有**区分「任意非空值即可」还是「必须是有效站点码」。
  另外 bundle 里 `idle_site_biz_code:"COMMONPRO"` 出现在
  `window.location.port` 存在时的本地兜底赋值处（`trade_bundle/main.js` 偏移 1536250），
  线上真实值由服务端注入的 `window.__PC_SELLER_WORKBENCH__.userInfo` 决定 —— 两者可能不同，但对我们测过的接口都有效。

### H3「需要不同的 mtop 网关/appKey」—— **排除**

- 官方 bundle 的 URL 拼装（`trade_bundle/main.js` 偏移 2849000 附近，lib-mtop 的 `__processRequestUrl`）：
  `"//" + prefix + "." + subDomain + "." + mainDomain + "/h5/" + api.toLowerCase() + "/" + v + "/"`，
  配 `mainDomain="goofish.com"`、`subDomain="m"`、`prefix="h5api"` → `https://h5api.m.goofish.com/h5/<api>/<v>/`，
  与 `backend/app/xianyu/mtop.py` 的 `API_BASE = 'https://h5api.m.goofish.com/h5/'` **完全一致**。
- appKey：官方 xianyu 包装器写死 `appKey:"34839810"`，与我们的 `APP_KEY = '34839810'` **一致**
  （lib-mtop 的通用兜底值才是 `12574478`，官方 bundle 覆盖掉了它；我们本来就用 34839810）。
- 本轮的实测即为同域名同 appKey 下拿到的 `SUCCESS`，从行为上再证一次。

## ④ 对产品的处置建议

1. **这不是「本账号没有这个权限」，不应作为账号问题如实透出。** 现状把 4 个可用的接口报成
   「本账号没有开通卖家工作台的权限」，会误导用户去换账号/换扫码 —— 而真正原因是我们的请求少一个头。
   修法方向（**本次未改代码**）：在 `backend/app/xianyu/mtop.py` 的通用调用路径（`call_params` 或
   `client.mtop()` 的 header 拼装处）统一补 `idle_site_biz_code`（复用 `client.py:162` 已有的
   `SECURITY_SITE_BIZ_CODE`，或把 `_security_headers()` 拆出可复用的通用头），而不是逐个接口打补丁。
2. **契约里要区分两类失败**：
   - 「站点 CODE 缺失/为空」→ 属于**我们自己的请求构造缺陷**，错误码应是我们侧的 `protocol_error`，
     文案指向内部实现，**不要**告诉用户「你没权限」；
   - 「`permissionCode: merchant:deliveryadress:query`」这类**带具体权限码**的拒绝 → 才是真实的账号权限缺失，
     应如实透出「本账号没有该权限」，并且**把 `permissionCode` 原样带进错误信息**，便于用户和客服对账。
   - 判据（建议写进契约）：上游 `data.permissionCode` 存在且 `errorMsg` 不是「站点CODE不能为空」时，才按权限不足处理。
3. **`mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query` 需要单独结论**：
   本账号确实缺 `merchant:deliveryadress:query`。是否只在特定商家类型/子账号下可用，本轮**未定**；
   在拿到确切结论前，不要把它和上面 4 个接口一起归为「同类 bug」。
4. **别把 `FAIL_SYS_BIZPARAM_MISSED` 当鉴权失败**：`...order.backfill.report` 在补头后返回缺 `orderId`，
   属于正常参数校验；调用方应传业务参数后重试，而不是报权限错误。
5. **`v` 不要乱升**：这些接口只有 `1.0`，`v=2.0` 会得到 `FAIL_SYS_API_NOT_FOUNDED`（变体 13）。
6. **诊断口径**：以后排查此类问题先看 `ret` 平台码和 `data.errorMsg/permissionCode`，
   不要看本地映射出的 502（上游 HTTP 恒为 200）。

## ⑤ 复现方式

原始响应：`logs/fail62/perm/*.json`（每个文件含 `status` / `ret` / `body` / 变体参数）。
复现要点（只读、串行、间隔 ≥2 秒）：

1. 从 `app.db` 的 `account_credentials`（`account_id=2217970177348`）取 `cookie`，从 `_m_h5_tk` 取 token；
2. `t = 毫秒时间戳`，`data` 为发送用的 JSON 字符串，`sign = md5(token + "&" + t + "&" + "34839810" + "&" + data)`；
3. `POST https://h5api.m.goofish.com/h5/<api>/1.0/?jsv=2.7.2&appKey=34839810&t=…&sign=…&v=1.0&type=originaljson&accountSite=xianyu&dataType=json&timeout=25000&api=<api>&sessionOption=AutoLoginOnly&spm_cnt=a21ybx.publish.0.0`，
   body 为 `data=<json>`（form-urlencoded），`Cookie` 用第 1 步的值；
4. 对照实验只改一处：请求头加 `idle_site_biz_code: COMMONPRO`。

## ⑥ 未定 / 存疑

- `idle_site_biz_code` 是「任意非空即可」还是「必须是有效站点码」——只测了 `COMMONPRO` 与空串，**未区分**。
- 线上真实 `idle_site_biz_code` 取值（服务端注入 `window.__PC_SELLER_WORKBENCH__.userInfo`）未取到；
  是否需要与账号站点严格匹配，**未定**。
- `mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query` 的 `merchant:deliveryadress:query`
  在什么商家类型/子账号下可用，**未定**（本轮只证明本账号没有）。
- `mtop.alibaba.idle.seller.platform.pc.item.batch.offline` 是**写接口**，为守只读铁律**未做实测**；
  按同族证据（同一 `hO`/`gC` 包装器、同一头配置）推断应与其余接口同因，但**未经实测确认**。
