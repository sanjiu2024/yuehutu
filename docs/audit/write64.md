# 第 64 批审计：不可逆写族真机验证（商品写族除外）

> 用户指令（m05059）：「2 不要动我的商品」= 批准方案②（打不可逆写族真机验证），
> **商品写族一律不调用**（`api/items`、`api/drafts`、`api/item-templates` 三个前缀）。
> 证据目录：`logs/write64/`（探针脚本 + 原始响应 JSON，`logs/` 在 `.gitignore` 里）。

## 0. 口径与环境

- 账号 `2217970177348`（月狐兔_），in-process 真机；`XIANYU_APP_DIR=$TEMP/xywb_live64/appdata`
  是生产库 `%APPDATA%/xianyu-ai-workbench/data/app.db`（602112 B，22 表）的 `sqlite3.Connection.backup()`
  一致性拷贝 —— 探针**不动生产库**。
- 真机 id 一律用**假 id**（本账号订单号改尾四位），19 位数字：`FAKE_ORDER='3317095524016019999'`、
  `FAKE_ORDER2='3316454043276019998'`、`FAKE_REFUND='3317095524016019997'`、`FAKE_DELIVER='9999999999999999998'`、
  `FAKE_COMPLAIN='9999999999999999997'`、`FAKE_PUNISH='999999999999'`、`FAKE_CAMPAIGN=999999999999`、
  `FAKE_ITEM='1085479546000'`、`FAKE_CONV='FAKE_CONV_64'`。
- 教训：20 位假 id（`99999999999999999999`）会本地 `OverflowError: Python int too large to convert to SQLite INTEGER`
  （SQLite int64 上限 9.22e18），平台侧回 `UNEXPECTED_EXCEPTION::未知异常` ⇒ 19 位才对。
- 基线（`logs/write64/b0_state.json`）：orders 15 条（交易关闭 5 / 交易成功 9 / 待发货 1）；
  `conversations` 无 cid 列（cid 由 `services/chats.py::_platform_cid` 读 `messages.meta.cid` 解析）；
  通知 `items=[]`、`unread.total=0`（平台没有 REST 列表接口，只有频道与未读数）。

## 1. A 组：零副作用真机探针（40 条）

### 1.1 闸门 22/22 全过（`probe_a1.py` → `a1.json`）

每条写路由**缺 `confirm`** 时都回本地 `confirmation_required`、**零平台请求**。`orders.deliver` 没有
`confirm` 参数（闸门在 API 层）。这条是「写接口默认不会误伤」的地基。

### 1.2 平台原话（形状被接受，错误都是业务语义）

| 动作 | 平台返回 | 判定 |
|---|---|---|
| `close_order` / `close_by_seller_im` / `seller_delay_confirm` / `order_address_agree` | `NOT_SELLER` 您不是卖家 / 非此订单卖家 | ✓ 语义正确 |
| `order_cancel` | `FAIL_BIZ_BAD_REQUEST` 您不是买家 | ✓ |
| `adjust_price` | 您无权修改这个订单 | ✓ |
| `ship_dummy` | `ORDER_STATUS_ERROR` 订单状态不正确 | ✓ |
| `refund_agree` | `VERIFY_NOT_PASS` 核身未通过 | ✓ |
| `logistics_resend_consign` | `CONSIGN_CAN_NOT_CONFIRM_OPERATE_ORDER` 无法确定待操作订单 | ✓ |
| `orders_deliver` / `orders_update_shipment` | 本地 `not_found` 订单不存在 | ✓（假单号在本地就查不到） |
| `order_address_refuse` | `UNEXPECTED_EXCEPTION::未知异常` | ⚠ 可疑（见 §5） |
| `refund_refuse` | `QUERY_RP3_REFUSE_REASON::拒绝失败` | ⚠ 可疑 |
| `add_memo` | `COMMON_SYSTEM_ERROR::备注失败` | ⚠ 可疑 |
| `complain_proof` / `complain_revoke` | `SYSTEM_ERROR::系统异常请稍后重试` | ⚠ 可疑 |

### 1.3 平台回 SUCCESS 但**零副作用**（假 id 被逐单拒绝）

- `delay_confirm`：`failNum=1 failOrderInfos=[{errorCode:'SYSTEM_ERROR',orderId:假id}] successNum=0`；
- `ship_offline`：`ORDER_NOT_BELONG_TO_USER 订单不属于当前用户 successNum=0`；
- `rate_create`：`errorMsg:'登陆用户不是交易双方' successOrderIds=[]`。

⇒ 这三条是「**批量接口 + 逐单错误**」形状：只看信封 `success` 会当成成功（服务层必须看 `successNum`/
`failNum`）。已登记为欠账（§5）。

### 1.4 本地校验拦下（要补参数才到平台）

`user_adjust_price` SKU 价格不能为空、`complain_refuse` 举证图片不能为空、`smart_form_callback` 表单内容不能为空。

### 1.5 A2（`probe_a2.py` → `a2.json`，17 条）：申诉 / 投放 / 纠纷

- 申诉：`appeal_submit` → `FAIL_BIZ_CLIENT_PARAM_INVALID::参数非法`（⚠ 可疑：`{punishId, appealReason}` 可能缺官方键）；
  `appeal_submit_cro` → `FAIL_BIZ_PUNISH_NOT_EXIST` ✓；`appeal_urge` → `FAIL_BIZ_APPEAL_NOT_EXIST` ✓。
- 投放：`campaign_delete` / `campaign_batch_delete` → `E_AD_CAMPAIGN_CAMPAIGN_NOT_FOUND_ERROR::计划不存在` ✓；
  `campaign_start` → `E_AD_BP_COMMON_SYSTEM_EXCEPTION::闲鱼太累了`、`campaign_pause` → `COMMON_SERVER_ERROR::服务繁忙`（⚠ 可疑）；
  `campaign_edit` → `E_AD_BP_COMMON_PARAM_MISS::缺少必填参数`（⚠ 只给了 campaignId+title）；
  `item_campaign_renew` / `activity_apply` → `E_AD_BP_COMMON_PARAM_ILLEGAL::参数不合法`；
  `activity_task_execute` → `E_AD_BP_COMMON_PARAM_MISS`；`campaign_start_before`（无 payload）与
  `account_log_operates` → **平台成功**（后者 `result: []`）。
- 纠纷 4 条（假 conversation id）全部 `QUERY_CONVERSATION_FAIL::查询对话失败，请重试` ✓ 零副作用。

## 2. B 组：可逆低风险真机写

### 2.1 退货地址全环 → 抓到 BUG ① 与 BUG ②（都已修）

真机全环（`probe_b1g_cycle.py` → `b1g_address_cycle.json`）：基线 `total=0` → add（缺 confirm 拦下）→
add 成功 → **list `total=1`** → update → `set default` → delete。

**BUG ①（已修）列表解析漏掉双层信封**：真机原文（`probe_b1e_list_raw.py` → `b1e_address_list_raw.json`）是
`{'success': true, 'data': {'code': 'success', 'data': [ {contactId:3941114028, ...}, ... ], 'msg': '成功'}}`，
而 `backend/app/xianyu/trade.py:265 items_of` 只认 `body.items` / 顶层数组 / `data.items` ⇒ **有地址却显示 0 条**。
账号没有地址时内层是 **`null`（不是 `[]`）**。修：`items_of` 认内层 `data.data` 数组 + `body_of`/`parse_addresses`/
`services/trade.py::addresses` docstring 更正；新测试 `backend/tests/test_xianyu_address.py`（10 例）+
`backend/tests/test_trade_api.py` 真机原文 2 例。

**BUG ②（已修）平台业务失败藏在成功信封里**：`probe_b1h_delete.py` → `b1h_delete_probe.json` 原文
`{'success': true, 'data': {'code': 'CAN_NOT_DELETE_DEFAULT_ADDRESS', 'msg': '请不要删除默认的退货地址…'}}` ——
HTTP/客户端层 `success=true`，业务码在 `data.code`；`services/trade_write.py::_address_submit_once` 从不调用
同模块已有的 `xianyu/address_write.py::is_success` ⇒ 平台拒绝被当成成功、还写了**成功审计**。修：加第二层判据
（非 success ⇒ `common.fail('平台接口失败: …', 'protocol_error', platform_errors=…)` + `log_operation(..., False, …)`）。
测试 `backend/tests/test_address_write_api.py` +3 例（47 例 OK）。

**顺带更正第 61 批的过期结论**：退货地址**不是**「这个账号没有权限」—— `defaultAddr=true` 的新增才卡权限
（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`，`logs/write61/05d_address_add_full_raw.json`），`false` 能建。

**账号残留（必须如实记录）**：账号里现在留着一条探针建的**默认**退货地址 `contactId 3947862027` /
`'第64批探针C改'` / `13800000004` / 浙江省杭州市西湖区文一西路 969 号 7 幢 709 室。平台规则不允许删默认地址
（`CAN_NOT_DELETE_DEFAULT_ADDRESS`），`address_update` 也**不能把 `defaultAddr` 改回 false**（`b1i_cleanup_retry.json`），
`set.default` 指向不存在的 contactId 只会 `QUERY_DELIVERY_ADDRESS_ERROR`（无副作用，`b1j_cleanup_attempts.json`）。
清理办法：**先新增一条真实地址并设为默认，再删这条**（官方 App 或本项目的 add → `/{cid}/default` → delete）。
批次开始前该账号没有退货地址。

**幂等陷阱**：服务层写幂等键 = (account, action, contact_id, 请求体指纹) ⇒ **同载荷重复调用是回放、不打平台**
（B1i 必须换新 `detailAddress` 才真的改了平台）；要绕开幂等就用协议层直打（`addr_proto.request_of(...)` + `client.mtop(...)`）。

### 2.2 IM 会话族 → 抓到 BUG ③（已修）

**BUG ③（已修）`sessionId` 的**类型**按接口分**：`probe_b2_im.py` → `b2_im.json` 显示三条会话写接口
（`customer.leave/rejoin/deliver.session`）全部 502 `FAIL_SYS_BIZPARAM_TYPE_ERROR::业务参数类型错误sessionId`。
对照实验（`probe_b2b_session_id.py` → `b2b_session_id_type.json`）：同一请求把 `sessionId` 换成**数字**
（假 id 999999999999）立刻变成业务语义 `FAIL_BIZ_SESSION_NOT_FOUND_ERROR::会话不存在` ⇒ 这三条要数字。
另三条（`probe_b2c_session_id2.py` → `b2c_session_id_type2.json`）：`pc.blacklist.add`（v2.0）与 `.remove`（v1.0）
字符串被 `FAIL_BIZ_interceptor_unknow_err` 吞掉、数字才回干净的业务拒绝 `FAIL_BIZ_PARAM_INVALID::参数无效`；
`pc.blacklist.query` 与 `message.card.send` **两种都收** ⇒ 保持字符串。
修法：`backend/app/xianyu/im_actions.py::_numeric_session_id`（5 条用；非数字本地 `ValueError` 说清原因），
`blacklist_query_request` / `card_send_request` 继续 `_session_id`；`backend/app/services/im_actions.py::_write`
出参 `session_id` 用**调用方文本**、`submitted` 保留**真实请求体**（数字）。
测试 `backend/tests/test_im_actions_api.py` 121 例 OK（新增 2 例：五条发数字、非数字本地拦下）。

**服务层真机验收**（`probe_b2d_service.py` → `b2d_service_session_id.json`）：五条接口全部从 TYPE_ERROR 变成业务语义
（3× `FAIL_BIZ_SESSION_NOT_FOUND_ERROR`、2× `FAIL_BIZ_PARAM_INVALID`），请求体里是 `{"sessionId": 999999999999}`（int）；
非数字 `'64FAKE0001'` / `'64FAKE0001@goofish'` ⇒ 本地 `invalid_fields`（「会话 id 必须是纯数字…」）且 **`sent=[]`（零平台请求）**；
只读对照 `blacklist_status('67561675177')` 仍 `code=None`、请求体保持字符串。

**没做的**：`set_reception`（真机读回 `status: null` + `reception_status_missing`，读不到原值 ⇒ 改了不可逆）；
通知已读（`unread.total=0`、`items=[]`，没有真 id）；店铺资料（平台 502 `FAIL_BIZ_COMMON_SYSTEM_ERROR3::闲鱼太累了`）；
纠纷催办（没有真实纠纷会话）。

## 3. 三份静态审计（子代理产出，已入仓）

| 文件 | 覆盖 | 结论 |
|---|---|---|
| `docs/audit/write64_params.md` | 105 条非商品写路由逐条入表 | 形状可信（bundle，无真机）≈60 / 不适用 ≈33 / 未知 15 / 缺必填 2 |
| `docs/audit/write64_gates.md` | 105 行主表 + 17 条缺口 | **闸门零缺口**（无一处「先打平台再判 confirm」）；P0 平台写无幂等 7 处、P1 9 处、P2 文档漂移 |
| `docs/audit/write64_fake_success.md` | 20 个服务文件 / 40+ 动作的成功判据 | 缺陷 9 行；根因单点 `backend/app/xianyu/trade.py:213-228 envelope_error` **只判外层** |

缺必填 2 条（`write64_params.md`）：`POST /api/notifications/{acc}/read`（平台只收 `{messageId}`，缺 id 变体回的是
`FAIL_BIZ_IM_PC_READ_STATUS_SYNC_FAILED` —— 又一类「参数问题伪装成平台故障」）、`POST /api/trade/{acc}/addresses`
（`areaId` 值域只证了裸 `divisionId`）。

## 4. 本批没打的东西（诚实边界）

- 交易族不可逆写（cancel / close-im / delay-single / adjust-price-sku、deliver）、退款、评价、投诉申诉、资质、
  `ad_write` execute、`smart-form/submit`：**除假 id 探测外一条都没真打**（假 id 已被平台按业务语义拒绝，零副作用）。
- 账号设置族（登录族 4 条、子账号 5 条、快捷回复 6 条、黑名单写、资质提交/OSS 凭证）与 `uploads/images`：本批未打。
- **商品写族：用户明令不碰** —— 只做了静态审计（`write64_fake_success.md` 单独一节）。

## 5. 本批新增欠账（已同步 `docs/交接文档.md` §10）

1. 批量接口的**逐单结果**没判：`delay_confirm` / `ship_offline` / `rate_create` 回信封成功但 `successNum=0`；
2. 静态审计的 9 行假成功（qualification ×2、logistics.resend、ad_write 10 动作、ad.message_read、im_tools.clean、
   `orders.py` 发货「本地优先」、trade_write 12 个无回执动作 + ship_offline/rate_create、order_address agree/refuse）；
3. `write64_gates.md` 的 P0/P1 幂等与审计缺口（7 + 9 处）；
4. 退货地址账号残留（§2.1）；
5. 12 条「可疑」平台文案（`UNEXPECTED_EXCEPTION` 等）待逐条复核参数形状。
