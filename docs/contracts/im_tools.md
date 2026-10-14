# IM 工具族（会话搜索 / 商品搜索 / 清未读）契约（读 + 读 + 写）

> 为什么并进 `/api/im` 而不是单开前缀：这三件事都是**会话页里的工具**（官方 IM 页的搜索框、
> 会话头部的商品抽屉、未读角标的「清未读」），跟 `im_actions` 的拉黑 / 发卡片 / 会话接管是同一个页面域。
> 分开的只是**代码模块**（`backend/app/xianyu/im_tools.py`），HTTP 前缀共用 `/api/im`。
>
> 与 `/api/chats` 的区别：`/api/chats` 管**本地会话与消息**（落库、AI 回复、发消息），
> 本族管**平台侧的查询与未读状态**，不落库任何会话。

证据来源（零平台请求，全部静态提取）：`C:\Users\Administrator\.dsh-tmp\seller_scan\js_008_vendors.js`
（5,857,382 字符，官方 IM 卖家端 bundle）与 `js_006_p_im_index.js`（3,071,011 字符，官方 IM 主 bundle）。

## 1. 四个平台接口

| 用途 | mtop | 版本 | 请求 | 响应 |
| --- | --- | --- | --- | --- |
| 会话搜索 | `mtop.taobao.idlemessage.pc.session.search` | 1.0 | `{keyword, pageNumber, pageSize}` | `resp.data`（`sessionList[]` / `total` / `curPage`） |
| 商品搜索 | `mtop.taobao.idlemessage.pc.tool.item.search` | 1.0 | `{sessionId, pageSize, pageNumber/currentPage, searchItemRole, queryWord}` | 列表 + 分页标志 |
| 清会话未读 | `mtop.taobao.idlemessage.pc.session.unread.clean` | 1.0 | **不带 data** | 官方**不看响应** |
| 清系统未读 | `mtop.taobao.idlemessage.pc.systems.unread.clean` | 1.0 | **不带 data** | 官方**不看响应** |

官方调用点逐字：

- 会话搜索 `js_008_vendors.js` @1872570（函数 `y7`）：`pageNumber` 默认 **0**、`pageSize` 默认 **50**；
  失败兜底 `{success: false, total: 0, curPage: 0, sessionList: []}`。
- 商品搜索**两个官方组件分页字段不一致**：旧组件发 `pageNumber` 并读响应 `nextPage`
  （`js_008_vendors.js` @1810742、`js_006_p_im_index.js` @3003926）；新组件发 `currentPage` 并读
  `hasMore`（@1816833 / @3009966）。**我们的偏离**：请求里两个键都发（值相同），响应里两个键都认
  —— 这不是猜值，是同一页码的两个官方别名。
- `searchItemRole` 只有 `own`（我的宝贝）/ `other`（TA的宝贝），就是官方两个 tab 的逐字文案；
  结果卡片以 `itemId` 为 key。
- 清未读（`js_006_p_im_index.js` @2103155）：先取登录用户 id，拿到才调，**调完直接 `resolve()`**；
  系统未读（`js_008_vendors.js` @1082751）是一个 `class {api, v:'1.0', needLogin:true}`。
- **没做**：会话备注 `mtop.taobao.idlemessage.pc.tool.remark` 需要 `sessionType`（数字，来自会话对象
  `bizType` 或推送帧 `sessionInfo.sessionType`），而我们的 `messages.meta` 只存
  `cid / external_id / image_url / images / msg_type / source`（真库抽样 40 条，`sessionType` 命中 0）
  ⇒ 没有正当来源就不做（猜一个数字发上去就是造假）。

## 1.1 HTTP 面（2 读 + 1 写）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/im/{account_id}/sessions/search?keyword=&page=&size=` | 在平台上搜会话 |
| GET | `/api/im/{account_id}/item-search?session_id=&conversation_id=&role=&keyword=&page=&size=` | 搜这个会话里的商品（`role` = `own` / `other`） |
| POST | `/api/im/{account_id}/unread/clean` | 清未读，body `{kind, confirm}`（`kind` = `session` / `system`） |

错误码：`400 confirmation_required`（未确认）/ `400 invalid_fields`（关键词超长、页码非法、`kind` 认不出）/
`400 missing_cid`（商品搜索只给了 `conversation_id` 而这个会话还没有平台 cid）/
`404 not_found`（账号不存在 / 会话不存在或跨账号）/ `409 verification_required`（无 Cookie 或已判定需重登）/
`502 protocol_error`（平台拒绝，附人话）/ `503 xianyu_not_ready`（协议层缺失）。

`session_id`（平台 cid）与 `conversation_id`（本地会话行 id）**二选一**，只给后者时后端用既有的
`backend/app/services/im_actions.py::_resolve_session` 换 cid（会话页永远拿不到 cid，让调用方猜是错的）。

## 1.2 参数与本地校验（全部零平台请求）

- `keyword` strip 后 ≤ **64** 字（`KEYWORD_MAX`）；空关键词合法（= 平台侧不带筛选）；
- `page` ≥ **1**（FastAPI `Query(ge=1)`，`0` 会被框架拦成 **422**）；`size` ∈ `[1, 50]`（`PAGE_SIZE_MAX`）；
- `role` 只认 `own` / `other`（大小写不敏感，落库统一小写）；`kind` 只认 `session` / `system`；
- `confirm` **只在服务层判**，且只认 `confirm is True`（`'false'` / `0` / `''` / `None` 一律不放行）。
  路由层**不许**写 `bool(body.get('confirm'))` —— `bool('false')` 是 `True`。

## 1.3 三态（与「不造数据」的对照）

| 情况 | 返回值 |
| --- | --- |
| 平台返回了列表 | `sessions` / `items` = 数组（元素字段拿不到的给 `null`，界面显示 `-`） |
| 平台明确返回空 | `sessions: []` / `items: []`（真的没有） |
| 平台没给列表键 | `sessions: null` / `items: null` + warning `im_sessions_missing` / `im_items_missing`（**不等于「没有」**） |
| 总数拿不到 | `total: null` + warning `im_session_total_missing`（**不拿当前页条数冒充总数**） |
| 还有没有下一页拿不到 | `has_more: null`（界面写明「平台没给标志」，不当成「没有下一页」） |
| 清未读：平台没回回执字段 | `saved: true` + `platform_success: null` + warning `im_unread_unconfirmed`（官方本来就不看响应） |
| 清未读：平台明确 `success: false` | 502 `protocol_error`，**本地未读一个都不动** |
| 平台拒绝（无权限等） | 502 `protocol_error` + 平台原话，**不碰 `login_state`** |

## 1.4 三步顺序与副作用

写接口一律 **确认（`_confirm_gate`）→ 字段校验（协议层构造，纯本地）→ 建客户端**；
字段校验排在凭据检查之前。清未读是**账号级**（官方接口没有单会话参数）：

- 成功后 `UPDATE conversations SET unread = 0 WHERE account_id = ? AND unread > 0`（仅 `kind='session'`；
  `kind='system'` 不动本地），并播 `chats.changed`；
- 审计动作 `im_unread_clean`；失败（平台拒绝）时**不写库、不播事件**。

## 2. 界面口径

`frontend/src/pages/Chats.tsx` + `frontend/src/pages/chats.css`：

- 工具栏（`.ch-toolbar`）一个**本地过滤框**（买家 / 商品 / 最后一条消息）+「在平台搜」+「清未读」；
  本地过滤与平台搜索是**两件事**，页面上分开放；
- 平台搜索结果块（`.ch-remote*`）是**只读**的，并写明原因：「平台搜索结果只用于查看：它没有本地会话行
  id，点不开右侧会话」—— 不假装能点开（`RemoteSession.session_id` 与本地行 id 是两种东西）；
- 「清未读」**必须**弹 `ConfirmDialog`，文案写明「平台的『清未读』接口是账号级的：一次清掉这个账号
  **所有**会话的未读，不是只清当前这一个」，`danger`；
- 会话头部「商品」按钮开抽屉（`Modal`）：两个 tab（我的宝贝 / TA的宝贝，`DRAWER_ROLES`），
  **懒加载**（第 3 参 `false`，只在点开时查一次，订阅 `items.updated` 静默重拉，**不轮询**）；
  三态分开说（`items === null` 平台没返回列表键 / `[]` 真的没有 / 有数据）；
  `has_more` 为 `null` 时页脚写明「平台没给『还有没有下一页』的标志」，不当成没有下一页；
- 唯一写出口 `runImWrite`（请求体 `confirm: true` 字面量）；`saved === false` 一律按失败提示。

## 3. 测试

`backend/tests/test_im_tools_api.py`：**55 条**。协议（接口名逐字 / 读写集合不相交 / `assert_write` 只认写 /
四个请求形状与版本 / 关键词与页码校验 / 八种伪 `confirm` / `request_of` 分派 / 三种解析的缺键为 `None`
与别名）+ 会话搜索 + 商品搜索（`conversation_id` 换 cid）+ 清未读（未确认零调用、成功清本地未读、
系统未读、平台 `success:false` 不动本地、非法 `kind`）+ 路由（含 openapi 断言）。

## 4. AI 侧

3 个工具：`im_session_search`、`im_item_search`（只读、无 `confirm`）与 `im_clear_unread`（写、必带 `confirm`）。
写工具的 description 里**必须**写明「这是账号级操作：会清掉这个账号所有会话的未读」——
这是这个接口最容易造成误伤的地方。对账口径：`scripts/reconcile_tools.py` 的 `WRITE_PREFIX` 加的是
完整写工具名（`im_clear_unread`），故意不加 `im_` 宽前缀，否则只读工具会被误判成写工具。

## 5. 未验证（诚实边界）

- 三条接口**一次真机请求都没发过**：只验到路由挂载 + 闸门（`confirm:false` → 400
  `confirmation_required`，零平台写请求）+ 假客户端下的协议形状 + 界面取证（只开确认框、不点确认）。
- `total` / `has_more` / 会话标题字段的**真实键名**仍只有 bundle 静态证据（本机账号在 IM 域可能
  回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 或 `409` 需重新扫码）；解析按存在性读，拿不到就是 `null`。
- 商品搜索的两个分页键（`pageNumber` + `currentPage`）是**同时发**的：平台如果对未知键报错，
  这条会失败——目前只有静态证据，没有真机回执。
