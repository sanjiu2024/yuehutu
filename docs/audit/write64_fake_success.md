# 第 64 批同型缺陷审计：平台写调用只判客户端层成功（假成功）

口径：扫描范围 `backend/app/services/*.py` 中所有**会打平台写接口**的调用点（`common.call_method(...)` / `client.mtop(...)` / `_call_write` / `_call_api` / `_submit`，且带 `confirm=True`、命中协议层 `WRITE_APIS`、或经 `assert_write` 守卫）。**读接口不计**——读接口的约定是「业务码非 success 仍 200 + `platform_code`」。判据分两层：① 客户端层（`status == common.CALL_OK` / `raw['success']`）；② 平台业务码层（`data.code` / `data.success` / `data.result.success`）。只过 ① 不过 ② 即为本次要找的落点。证据全部来自仓库源码（只读审计，未发任何真机请求）。

## 一、缺陷清单

| 服务文件:行 | 协议模块 | 动作/接口名 | 当前成功判据（原文摘录） | 是否可能假成功 | 建议修法 |
| --- | --- | --- | --- | --- | --- |
| `backend/app/services/ad_write.py:109-125` | `backend/app/xianyu/ad_write.py:62-70`（`_ad_write`） | 推广写族全部 10 个动作：campaign_edit / campaign_delete / campaign_batch_delete / campaign_pause / campaign_start / campaign_start_before / item_campaign_renew / activity_apply / activity_task_execute / account_log_operates | `failure = common.call_failed(status, value)`；协议层 `_ad_write` 只 `return result.get('data') if isinstance(payload, dict) else None`，**没有任何 code 判据** | 需真机取证 | 协议层补 `code_of`/`is_success`（口径照 `xianyu/item_write.py:197`）；服务层 `_write_once` 加业务码判据，失败走 `protocol_error` + `platform_errors` |
| `backend/app/services/ad.py:585-604` | `backend/app/xianyu/ad.py:350-354`（`message_read` → `_mtop`） | 推广消息已读 `message_read`（写，`confirm=True`） | `if status == common.CALL_OK:` → 直接 `common.log_operation(account_id, 'message_read', item_id, True, body)` | 需真机取证 | 与 ad_write 族同一判据；协议层 `ad.py` 全文无 `is_success`/`code_of` |
| `backend/app/services/qualification.py:289` | `backend/app/xianyu/qualification.py:117-121`（`submit_packages` → `_post`） | 提交资质 `mtop.taobao.idle.ad.qualification.packages.submit` | `if status == common.CALL_OK: return value, None`（`_call`，115-131 行） | 是 | 协议层补 `is_success`/`code_of`（该模块**没有**任何 envelope/code 函数）；服务层 `_call` 加业务码判据 |
| `backend/app/services/qualification.py:309` | `backend/app/xianyu/qualification.py:124-128`（`oss_credential` → `_post`） | 取 OSS 上传凭证 `...qualification.get.oss.credential` | 同上（`if status == common.CALL_OK: return value, None`） | 是 | 同上 |
| `backend/app/services/logistics.py:195-239`（`resend_consign`） | `backend/app/xianyu/logistics.py:209-223`（`parse_resend`） | 重新发货 | `failure = envelope_error(raw)` → `'saved': not failure`；该 `envelope_error` 来自 `xianyu/trade.py:213-228`（**只判外层 `raw.get('success') is False`**） | 是 | 协议层 `parse_resend` 加内层 `data.code` 判据（口径照 `xianyu/address_write.py:256-271`）；docstring 自称「官方也是只看 envelope」不能作为不判业务码的依据 |
| `backend/app/services/im_tools.py:157-165`（`clear_unread`） | `backend/app/xianyu/im_tools.py:371-387`（`parse_clean`） | 清未读 `clear_session_unread` / `clear_systems_unread` | `if parsed['platform_success'] is False:` → 失败；`platform_success` 为 None 时只加 warning `im_unread_unconfirmed` | 需真机取证 | `parse_clean` 只认业务体 `success`/`succeed`/`result`，**不认 `code`**；补一条 `code` 非 success ⇒ `platform_success=False` |
| `backend/app/services/orders.py:319-326`（`_update_shipment_once`） | `client` 方法 `ship_order`/`deliver_order`/`update_shipment`/`deliver` | 发货 `update_shipment` | `elif call_status == common.CALL_ERROR: warning = f'协议层发货失败，发货信息仅本地留存: {value}'`；随后**无条件** `common.log_operation(..., True, ...)` | 需真机取证 | 协议层当前 `CALL_MISSING`（未实现）。一旦实现必须补业务码判据，且平台拒绝时**不得**写成功审计、不得把本地 `orders.status` 直接置 `shipped` |
| `backend/app/services/trade_write.py:130-143`（`_submit_once`）+ `66-99`（`_call_api`） | `backend/app/xianyu/trade_write.py:900-909`（`parse_write_result`） | 无业务回执字段的写：`refund_agree`:265 / `refund_refuse`:283 / `ship_dummy`:176 / `adjust_price`:200 / `close_order`:812 / `delay_confirm`:828 / `remind_confirm`:842 / `postage_refund_pay`:861 / `order_cancel`:976 / `close_by_seller_im`:993 / `seller_delay_confirm`:1010 / `user_adjust_price`:1034 | `explain = proto.envelope_error(value)`（`xianyu/trade.py:213-228`，**只判外层**）；`parse_write_result` 只回 `platform_success`/`platform_body`，**不判 `data.code`**；`_submit_once` 无条件 `common.log_operation(..., True, ...)` | 需真机取证 | 逐个接口真机取证响应是否带 `data.code`；确认有则给 `xianyu/trade_write.py` 加统一 `is_success`（外层 + `data.code`）并在 `_call_api:85` 处调用。其中 `ship_offline`:162 用 `parse_ship_result`（`data.successNum`）、`rate_create`:306 用 `parse_rate_result`（`data.module.success`）——回执是计数/布尔，同样未与顶层成功合并判据 |
| `backend/app/services/trade_write.py:727`（`_order_address_write`） | `backend/app/xianyu/order_address.py:273-280`（`parse_write` / `envelope_failure`） | 同意/拒绝修改收货地址 `mtop.idle.merchant.order.address.modify.agree` / `.refuse` | `payload = order_addr_proto.parse_write(raw)`；`envelope_failure` 直接 `return envelope_error(raw) if isinstance(raw, dict) else None`（**外层版**） | 需真机取证 | 同上一行：确认响应形状后补内层 `data.code` 判据 |

## 二、商品写族（`backend/app/services/items.py`）——**只审计，未修改**（用户明确要求不动商品）

| 服务文件:行 | 协议模块 | 动作/接口名 | 当前成功判据（原文摘录） | 是否可能假成功 | 建议修法 |
| --- | --- | --- | --- | --- | --- |
| `backend/app/services/items.py:648-653`（`_push_update`） | `client.edit_item`（真签名 `edit_item(item_id, payload)`） | 商品修改 `edit_item` | `if status == common.CALL_OK: return None, None` —— 只要客户端层没报错就当作「平台改成功」 | 需真机取证 | 补业务码判据（可复用 `xianyu/item_write.py:197 is_success`：要求 `res.data.data` 为真） |
| `backend/app/services/items.py:921-937` | `client.downshelf` / `client.reshelf` | 下架 / 上架 | `if call_status == common.CALL_ERROR:` → 进 `failed[]`；`CALL_OK` 即视为成功并写本地 `items.status` | 需真机取证 | 补业务码判据；平台业务拒绝时不得改本地状态（该处已修过 CALL_ERROR 一支，业务码一支仍缺） |
| `backend/app/services/items.py:1497-1504`（`publish_item`） | `client.publish_item(payload)` | 发布商品 | `problem = common.call_failed(status, value)` —— 只翻客户端层失败信封 | 需真机取证 | 补业务码判据；失败时不得 `INSERT ... status='on_sale'`、不得写成功审计 |
| `backend/app/services/items.py:1078-1082` / `1212-1216` | `backend/app/xianyu/item_write.py:197-206`（`is_success`） | 批量写 / 快捷改价（`mtop` 直调） | `if not proto.is_success(value): return _platform_failure(value, api)` —— 判的是业务体 `res.data.data` 真不真（bundle 原文判据） | 否 | 已安全；这是本仓商品写族里唯一两层都看的写法，可作为其它落点的改法样板 |

商品写族小结：3 个落点只判客户端层（`edit_item` / `downshelf`+`reshelf` / `publish_item`），2 个落点已两层（批量写、快捷改价）。按用户要求**本批未做任何修改**。

## 三、已确认安全（核过确实两层都看）

- `backend/app/services/trade_write.py:526-556`（`_address_submit_once`）：`if not addr_proto.is_success(raw):` —— `xianyu/address_write.py:256-271` 判内层 `data.code`，即第 64 批缺陷原型，已修。
- `backend/app/services/disputes.py:253-300`（`_write`）：`envelope_error` 之后再取 `proto.parse_send(value)['saved']`，`xianyu/dispute.py:362 _business_error` 在 parse 内部判业务体 `code` / `success`。
- `backend/app/services/fans_price.py:184-239`（`set_prices`）：`parse_set`（`xianyu/fans_price.py:406-434`）内含 `_business_error`，业务失败 ⇒ `saved=False`。
- `backend/app/services/im_actions.py:269-334`（`_write`）：`(parser or proto.parse_action)(value)['saved']`；`xianyu/im_actions.py:433-475` 内含 `_business_error`，session 族另认 `platform_success` 三态（明确 false 即失败）。
- `backend/app/services/notify.py:275-324`（`mark_read`）：`parse_mark_read`（`xianyu/notify.py:131-162`）在 `envelope_error` 之后再判业务体 `code` 非 success。
- `backend/app/services/quick_reply.py:176-204`（`_write`）：`proto.is_success(value)`；`xianyu/quick_reply.py:285-298` 判业务体 `code` / `error` 字段。
- `backend/app/services/shop.py:364-379`（`_send_write`）：用 `shop_write_mod.envelope_error`，`xianyu/shop_write.py:351-370` 是**外层 + 内层 `code`** 的两层版（与 `trade.envelope_error` 同名不同实现，别混）。
- `backend/app/services/shop_profile.py:95-116`（`_submit`）：`proto.is_success(value)`；`xianyu/shop_profile.py:188-204` 判外层 + `data['success']` + 业务码。
- `backend/app/services/sub_accounts.py:111-129`（`_submit`）与 `132-173`（`create`）：`proto.is_success(value)`；`xianyu/sub_account.py:407-420`，`code_of`（387-388）取 `raw['data']['code']`；delete / mobile / roles / password 全部走 `_submit`。
- `backend/app/services/drafts.py:221` 与 `:261`（删除 / 批量删除草稿）：`if not proto.is_success(value): return _platform_failure(...)`；`xianyu/draft.py:248-263` 判内层 `code`。
- `backend/app/services/item_templates.py:186-232`（`_write_call` + `_outcome`）：`'ok': code == proto.SUCCESS_CODE`（bundle 原文判据），失败 502 并把平台原文交出去。
- `backend/app/services/items.py:1081` 与 `:1215`：`proto.is_success`（`xianyu/item_write.py:197-206`）。
- `backend/app/services/security_write.py:104-126`（`_submit_once`）：三个写动作都传了 `business` 回调（`appeal_submit`:176 / `appeal_submit_cro`:192 / `appeal_urge`:207），`xianyu/security_write.py:204-216 appeal_submit_failure` 判 `data.appealId`。
- `backend/app/services/trade_write.py:322-332`（`_complain_guard`）与 `:886-894`（`complain_revoke`）：`_submit` 之后再判 `platform_body['success'] is False` + `failReason`（`xianyu/trade_write.py:955-966 complain_failure`）。
- `backend/app/services/trade_write.py:1187-1197`（`smart_form_callback`）：`parse_smart_form_callback` 判 `data.result.success`（`xianyu/trade_write.py:1211-1226`），无回执字段时给 warning 而非冒充成功。
- `backend/app/services/chats.py:629-634` 与 `1289-1302`（发消息 / AI 回复）：服务层判据虽只看 `value.get('success') is False`，但协议层 `xianyu/im.py:1102-1108` 已判业务体 `body.get('reason') or body.get('code')` 并 `raise RuntimeError` ⇒ `common.call_method` 回 `CALL_ERROR`，业务失败到不了成功分支。
- `backend/app/services/uploads.py:299-306`（`upload_image`）：`common.call_failed` 之后要求 `_upload_url(value)` 非空，否则 502 ⇒ 不会假成功（仅失败文案会误称「协议层上传成功但没有返回图片 URL」）。
- `backend/app/services/trade.py:168-200`（`_call`）：进入前有 `trade_proto.assert_read_only(api)` 守卫，该模块**只读**，不属本次范围（其 `envelope_error` 只外层，但只读约定允许）。
- `backend/app/services/marketing.py:16`：注释明确协议层 `WRITE_APIS` 里两个写接口**没有任何调用点**，路由层只注册 GET ⇒ 写族无落点。
- 只读模块（无写落点，逐条核过）：`backend/app/services/finance.py:15`（「四个页面都不提交任何写操作」）、`backend/app/services/items_read.py:5`（「写方法一律 405」）、`backend/app/services/promotion.py:58-86`、`backend/app/services/datacenter.py`（三个调用点均为 `_BoundProtocol` + 查询接口）、`backend/app/services/item_stats.py`、`backend/app/services/accounts.py`、`backend/app/services/security_center.py`、`backend/app/services/im_service.py`。

## 四、根因与覆盖面

- 根因单点：`backend/app/xianyu/trade.py:213-228 envelope_error` **只判外层** `raw.get('success') is False`。`dispute.py` / `fans_price.py` / `im_actions.py` / `logistics.py` / `notify.py` / `security_write.py` / `order_address.py` / `compensate.py` 全部 `from .trade import envelope_error` —— 凡是**只**依赖它、又没有额外业务码判据的落点，就是本清单第一节的 5、8、9 三行。
- 与它同名的 `backend/app/xianyu/shop_write.py:351-370 envelope_error` 是两层版，服务层 `services/shop.py` 用的是这一份 —— 同名的两个实现是本缺陷类最容易被误判的地方。
- 覆盖面：`services/*.py` 里 `call_method` 81 处、`client.mtop` 3 处、`_call_write` 4 处、`_call_api` 15 处已逐处核对读写性质与成功判据；写落点合计 20 个服务文件 / 40+ 个动作，全部落入上表。

预算使用情况：≤20 分钟 / ≤12 轮 —— 实际约 24 分钟 / 13 轮（协议层判据与服务层落点逐条取证完成）。超预算原因：`trade_write.py` 写族有 40+ 个动作、判据分散在 `_call_api` + 各 `parse_*` + 各服务函数三处，逐条读完才敢下结论。**未取证项**：`data.code` 双层信封在 ad_write / ad.message_read / qualification / logistics.resend / trade_write 无回执族 / items 商品写族 上的真实响应形状，均标注「需真机取证」，未编结论。
