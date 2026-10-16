# 非商品写路由审计：confirm 闸门 / 写幂等键 / 审计落库（第 64 批）

- **日期**：2026-10-16
- **范围**：`create_app().openapi()` 里全部非 GET/HEAD 路由共 **130** 条，排除商品写族后 **105** 条。
  - 排除：`/api/items/*`（19 条，含 `/api/items/{account_id}/{item_id}/fans-price`）、`/api/drafts/*`（2 条）、`/api/item-templates/*`（4 条），共 25 条 —— 用户明确要求不动商品。
- **性质**：纯静态审计，未改任何代码；唯一写入物是本文件。
- **取证方式**：OpenAPI 路由清单 + AST 扫描（api 层装饰器 → service 层漏斗递归）+ `grep` 实测行号。**表中行号以 grep 实测为准**，扫描器自报行号有 1–6 行漂移，已全部人工校正。

## 口径说明（判据）

1. **闸门**
   - 判据：服务层在该路由落到的**平台调用之前**是否存在确认检查，且该检查**不产生任何平台请求**（`common.confirm_required` 只判确认，不碰库/账号/平台，见 `backend/app/services/common.py:146`、注释在 `common.py:149`）。
   - **宽松码** = `CONFIRM_CODE = 'confirmation_required'`（`common.py:97`），`confirm_truthy` 认字面 `True` / `'true'`（忽略大小写与首尾空白）/ `1`（`common.py:106`）。
   - **严格码** = `STRICT_CONFIRM_CODE = 'confirm_required'`（`common.py:100`），`confirm_accepted(strict=True)` 只认字面 `True`（`common.py:123`）。
   - 全库**严格档只有 3 处**：`services/ad.py:571`（`ad.message_read`）、`services/ad_write.py:77`、`services/qualification.py:278` 与 `298`。
   - 「无（白名单登记）」= 该路由没有闸门，但已在 `backend/tests/test_write_routes_have_gates.py` 的 `GATE_FREE_ALLOWLIST`（该文件 40–83 行）登记为**语义只读 / 本地写 / 会话类**，属**已决**，不是新缺口。
2. **幂等键**
   - 判据：该路由最终落到的服务层漏斗里是否有 `common.idempotent_call`（唯一入口，`common.py:331`；键来自 `Idempotency-Key` 请求头，中间件 `backend/app/main.py::IdempotencyKeyMiddleware` → `common.set_idempotency_key`，`common.py:235`；TTL 7 天 `common.py:228`）。
   - **只有「打平台写」才需要幂等键**；纯本地写（SQLite 落库、缓存、设置）标「本地写不需要」。
   - 注意 `idempotency_lock`（`common.py:251`）是 per-key `threading.Lock`，**只挡本进程**；跨进程/重启后靠 DB 里已记的键 + 指纹回放（`idempotency_replay` `common.py:278`）。
3. **审计**
   - 判据：平台写路径上是否有 `common.log_operation`（`common.py:162`，写 `operation_logs` 表，是全库唯一的审计落库辅助）。
   - 注意 `log_operation` 的 `action` 列取 `kind or action`，所以传 `kind` 会覆盖审计名 —— `services/shop_profile.py:102-103`、`services/sub_accounts.py:126` 有明确注释禁止传 `kind='shop'`。
4. **不算缺口的**：本地写缺闸门/缺幂等、平台**读**缺闸门、会话类路由缺闸门 —— 全部已在白名单登记。

## 主表（105 条非商品写路由）

| 路由 | 闸门（无/宽松/严格） | 闸门位置 file:line | 幂等键（有/无/本地写不需要） | 审计（有/无） |
| --- | --- | --- | --- | --- |
| `POST /api/accounts/qr/start` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/accounts/qr/poll` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/accounts/{account_id}/refresh-profile` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `PATCH /api/accounts/{account_id}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/accounts/{account_id}/toggle` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/accounts/{account_id}/relogin` | 无（白名单登记） | — | 本地写不需要（平台登录，非业务写） | 无 |
| `POST /api/accounts/{account_id}/captcha` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `DELETE /api/accounts/{account_id}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/promotion/ad/{account_id}/messages/read` | 严格 | services/ad.py:571 | 有 services/ad.py:606 | 有 services/ad.py:601 |
| `POST /api/promotion/write/{account_id}/{action}` | 严格 | services/ad_write.py:77 | 有 services/ad_write.py:92 | **无** |
| `PUT /api/agents/roles/{role}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/agents/prompts` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `PUT /api/agents/prompts/{prompt_id}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `DELETE /api/agents/prompts/{prompt_id}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `DELETE /api/agents/chat/messages` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/agents/master/chat` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/agents/reply` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/auth/login` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/auth/desktop` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/auth/logout` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/chats/classify` | 无（白名单登记） | — | 本地写不需要（LLM 分类） | 无 |
| `POST /api/chats/incoming` | 宽松 | services/chats.py:667 | **无** | 有 services/chats.py:709、1209 |
| `POST /api/chats/sync` | 无（白名单登记） | — | 本地写不需要（平台读） | 无 |
| `POST /api/chats/{conversation_id}/send` | 宽松 | services/chats.py:667 | **无** | 有 services/chats.py:709 |
| `PUT /api/chats/{conversation_id}/ai` | 无（白名单登记） | — | 本地写不需要 | 有 services/chats.py:729 |
| `POST /api/chats/{conversation_id}/ai-draft` | 无（白名单登记） | — | 本地写不需要（只生成草稿，不发平台） | 有 services/chats.py:1209 |
| `POST /api/chats/{conversation_id}/mode` | 无（白名单登记） | — | 本地写不需要 | 有 services/chats.py:744 |
| `POST /api/chats/{conversation_id}/read` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/disputes/{account_id}/{conversation_id}/messages` | 宽松 | services/disputes.py:260 | **无** | 有 services/disputes.py:284 |
| `POST /api/disputes/{account_id}/{conversation_id}/messages/image` | 宽松 | services/disputes.py:260 | **无** | 有 services/disputes.py:284 |
| `POST /api/disputes/{account_id}/{conversation_id}/urge` | 宽松 | services/disputes.py:260 | **无** | 有 services/disputes.py:284 |
| `POST /api/disputes/{account_id}/{conversation_id}/mark-read` | 宽松 | services/disputes.py:260 | **无** | 有 services/disputes.py:284 |
| `POST /api/im/{account_id}/blacklist` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/blacklist/remove` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/cards` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/sessions/transfer` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/sessions/leave` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/sessions/rejoin` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/reception` | 宽松 | services/im_actions.py:285 | **无** | 有 services/im_actions.py:323 |
| `POST /api/im/{account_id}/unread/clean` | 宽松 | services/im_tools.py:147 | 本地写不需要 | 无 |
| `POST /api/logistics/{account_id}/orders/{order_id}/resend` | 宽松 | services/logistics.py:202 | **无** | 有 services/logistics.py:221 |
| `POST /api/notifications/{account_id}/read` | 宽松 | services/notify.py:286 | **无** | 有 services/notify.py:310 |
| `POST /api/orders/sync` | 无（白名单登记） | — | 本地写不需要（平台读） | 无 |
| `PUT /api/orders/{order_id}/shipment` | 宽松 | services/orders.py:274 | 有 services/orders.py:290 | 有 services/orders.py:331 |
| `POST /api/orders/{order_id}/deliver` | 无（白名单登记） | — | 本地写不需要 | 有 services/orders.py:368 |
| `POST /api/providers` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `PUT /api/providers/{provider_id}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `DELETE /api/providers/{provider_id}` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/providers/{provider_id}/test` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `PUT /api/providers/{provider_id}/default` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/providers/{provider_id}/default` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/qualification/{account_id}/packages/submit` | 严格 | services/qualification.py:278 | **无** | **无** |
| `POST /api/qualification/{account_id}/oss-credential` | 严格 | services/qualification.py:298 | **无** | **无** |
| `POST /api/quick-reply/{account_id}/groups` | 宽松 | services/quick_reply.py:226 | **无** | 有 services/quick_reply.py:218 |
| `POST /api/quick-reply/{account_id}/groups/{group_id}` | 宽松 | services/quick_reply.py:233 | **无** | 有 services/quick_reply.py:218 |
| `POST /api/quick-reply/{account_id}/groups/{group_id}/delete` | 宽松 | services/quick_reply.py:240 | **无** | 有 services/quick_reply.py:218 |
| `POST /api/quick-reply/{account_id}/phrases` | 宽松 | services/quick_reply.py:247 | **无** | 有 services/quick_reply.py:218 |
| `POST /api/quick-reply/{account_id}/phrases/{content_id}` | 宽松 | services/quick_reply.py:255 | **无** | 有 services/quick_reply.py:218 |
| `POST /api/quick-reply/{account_id}/phrases/{content_id}/delete` | 宽松 | services/quick_reply.py:263 | **无** | 有 services/quick_reply.py:218 |
| `PUT /api/risk/settings` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/risk/clear` | 无（白名单登记） | — | 本地写不需要 | 有 services/risk.py:285 |
| `POST /api/risk/{account_id}/clear` | 无（白名单登记） | — | 本地写不需要 | 有 services/risk.py:285 |
| `POST /api/security-center/{account_id}/appeal/form` | 无（白名单登记） | — | 本地写不需要（平台读） | 有 services/security_write.py:158 |
| `POST /api/security-center/{account_id}/appeal/submit` | 宽松 | services/security_write.py:168 | 有 services/security_write.py:98 | 有 services/security_write.py:125 |
| `POST /api/security-center/{account_id}/appeal/cro/submit` | 宽松 | services/security_write.py:184 | 有 services/security_write.py:98 | 有 services/security_write.py:125 |
| `POST /api/security-center/{account_id}/appeal/urge` | 宽松 | services/security_write.py:199 | 有 services/security_write.py:98 | 有 services/security_write.py:125 |
| `POST /api/shop/{account_id}/cs-routing/rule` | 宽松 | services/shop.py:391 | 有 services/shop.py:411 | 有 services/shop.py:430 |
| `POST /api/shop/{account_id}/cs-groups` | 宽松 | services/shop.py:455 | 有 services/shop.py:476 | 有 services/shop.py:496 |
| `POST /api/shop/{account_id}/cs-groups/delete` | 宽松 | services/shop.py:514 | 有 services/shop.py:526 | 有 services/shop.py:540 |
| `POST /api/shop/{account_id}/profile/nick` | 宽松 | services/shop_profile.py:131 | **无** | 有 services/shop_profile.py:114 |
| `POST /api/shop/{account_id}/profile/desc` | 宽松 | services/shop_profile.py:138 | **无** | 有 services/shop_profile.py:114 |
| `POST /api/shop/{account_id}/profile/avatar` | 宽松 | services/shop_profile.py:145 | **无** | 有 services/shop_profile.py:114 |
| `POST /api/sub-accounts/{account_id}` | 宽松 | services/sub_accounts.py:135 | **无** | 有 services/sub_accounts.py:179 |
| `POST /api/sub-accounts/{account_id}/{sub_id}/delete` | 宽松 | services/sub_accounts.py:196 | **无** | 有 services/sub_accounts.py:127 |
| `POST /api/sub-accounts/{account_id}/{sub_id}/mobile` | 宽松 | services/sub_accounts.py:208 | **无** | 有 services/sub_accounts.py:127 |
| `POST /api/sub-accounts/{account_id}/{sub_id}/roles` | 宽松 | services/sub_accounts.py:221 | **无** | 有 services/sub_accounts.py:127 |
| `POST /api/sub-accounts/{account_id}/{sub_id}/password` | 宽松 | services/sub_accounts.py:235 | **无** | 有 services/sub_accounts.py:127 |
| `PUT /api/settings` | 无（白名单登记） | — | 本地写不需要 | 无 |
| `POST /api/trade/{account_id}/orders/ship` | 宽松 | services/trade_write.py:156 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/ship-dummy` | 宽松 | services/trade_write.py:172 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/adjust-price` | 宽松 | services/trade_write.py:192 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/refunds/{refund_id}/agree` | 宽松 | services/trade_write.py:261 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/refunds/{refund_id}/refuse` | 宽松 | services/trade_write.py:279 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/evaluations` | 宽松 | services/trade_write.py:302 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/complaints/{complain_id}/refuse` | 宽松 | services/trade_write.py:318 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/complaints/{complain_id}/proof` | 宽松 | services/trade_write.py:318 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/addresses` | 宽松 | services/trade_write.py:565 | 有 services/trade_write.py:520 | 有 services/trade_write.py:514 |
| `POST /api/trade/{account_id}/addresses/{contact_id}/update` | 宽松 | services/trade_write.py:584 | 有 services/trade_write.py:520 | 有 services/trade_write.py:514 |
| `POST /api/trade/{account_id}/addresses/{contact_id}/delete` | 宽松 | services/trade_write.py:604 | 有 services/trade_write.py:520 | 有 services/trade_write.py:514 |
| `POST /api/trade/{account_id}/addresses/{contact_id}/default` | 宽松 | services/trade_write.py:615 | 有 services/trade_write.py:520 | 有 services/trade_write.py:514 |
| `POST /api/trade/{account_id}/orders/{order_id}/modify-address/agree` | 宽松 | services/trade_write.py:683 | **无** | 有 services/trade_write.py:711 |
| `POST /api/trade/{account_id}/orders/{order_id}/modify-address/refuse` | 宽松 | services/trade_write.py:685 | **无** | 有 services/trade_write.py:711 |
| `POST /api/trade/{account_id}/orders/{order_id}/close` | 宽松 | services/trade_write.py:784 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/delay` | 宽松 | services/trade_write.py:800 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/remind` | 宽松 | services/trade_write.py:814 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/refunds/{refund_id}/postage-pay` | 宽松 | services/trade_write.py:833 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/complaints/{complain_id}/revoke` | 宽松 | services/trade_write.py:858 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/compensations/{service_code}/refuse` | 宽松 | services/trade_write.py:1263 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/memo` | 宽松 | services/trade_write.py:884 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/cancel` | 宽松 | services/trade_write.py:948 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/close-im` | 宽松 | services/trade_write.py:965 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/delay-single` | 宽松 | services/trade_write.py:982 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/orders/{order_id}/adjust-price-sku` | 宽松 | services/trade_write.py:1000 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/trade/{account_id}/smart-form/submit` | 宽松 | services/trade_write.py:1153 | 有 services/trade_write.py:124 | 有 services/trade_write.py:142 |
| `POST /api/uploads/images` | 无（白名单登记） | — | **无** | **无** |

## 缺口清单

按严重度排序。每条写清「缺什么 + 文件:行 + 建议修法」。

### P0 — 平台写、无幂等，重复提交对买家或平台直接可见

1. **`POST /api/qualification/{account_id}/packages/submit` 既无幂等也无审计**
   - 证据：`backend/app/services/qualification.py:271` `def submit_packages(...)`，严格闸门 `qualification.py:278`，平台调用 `qualification.py:289`；函数体内无 `common.idempotent_call`、无 `common.log_operation`（`grep log_operation backend/app/services/qualification.py` 只命中 36/241 行，均与本函数无关）。
   - 影响：资质提交是**不可逆**动作，超时重试会重复提交。
   - 建议：把 `qualification.py:289` 的平台调用抽成 `_submit_packages_once`，用 `common.idempotent_call(account_id, 'qualification_submit_packages', '', payload, _submit_packages_once)` 包住；成功/失败两分支各补一条 `common.log_operation(account_id, 'qualification_submit_packages', None, ok, detail)`。

2. **`POST /api/qualification/{account_id}/oss-credential` 既无幂等也无审计**
   - 证据：`backend/app/services/qualification.py:295` `def oss_credential(...)`，严格闸门 `qualification.py:298`，平台调用 `qualification.py:309`；无 `idempotent_call`、无 `log_operation`。
   - 建议：同第 1 条，`idempotent_call(account_id, 'qualification_oss_credential', '', payload, ...)` + `log_operation`。

3. **`POST /api/chats/{conversation_id}/send` 无幂等（给真买家发消息）**
   - 证据：`backend/app/services/chats.py:650` `def send_manual(conversation_id, text, *, confirm=None)`，闸门 `chats.py:667`，平台 `common.call_method(client, ('send_message',), cid, text)` 在 `chats.py:629`（被 `send_manual` 调用），审计 `chats.py:709`；无 `idempotent_call`。
   - 影响：客户端超时重试 = 买家收到两条一样的消息。
   - 建议：在 `send_manual` 里把 `chats.py:629` 包进 `common.idempotent_call(account_id, 'send_manual', conversation_id, {'text': text}, _send_once)`。

4. **`POST /api/trade/{account_id}/orders/{order_id}/modify-address/agree` 与 `/refuse` 无幂等**
   - 证据：`backend/app/services/trade_write.py:679` `def _order_address_write(account_id, action, order_id, deliver_id, confirm)`，闸门 `trade_write.py:683`（agree）/`685`（refuse），平台 `_call_write` 在 `trade_write.py:697`，审计 `trade_write.py:699`（失败）/`711`（成功）；**无 `idempotent_call`**。
   - 影响：同意/拒绝修改收货地址是订单状态变更，重复提交会二次改动。
   - 建议：用 `common.idempotent_call(account_id, 'order_address_' + action, order_id, {'deliver_id': deliver_id}, _order_address_write_once)` 包住 `trade_write.py:697`。
   - 附带：`docs/CONTRACTS.md` §9.3（该文件 1719–1724 行）**声称这两条已覆盖幂等**，与代码不符，见 P2-16。

5. **`POST /api/im/{account_id}/cards` 无幂等（向买家发商品卡片）**
   - 证据：`backend/app/services/im_actions.py:269` `def _write(action, label, account_id, audit, confirm, ...)`，闸门 `im_actions.py:285`，平台 `_call` 在 `im_actions.py:304`，审计 `im_actions.py:323`；无 `idempotent_call`。
   - 建议：在 `_write` 里包 `idempotent_call(account_id, 'im_' + action, '', kwargs, _write_once)`，一次覆盖 `cards` 等 7 条 `/api/im/*` 路由（见 P1-12）。

6. **`POST /api/disputes/{account_id}/{conversation_id}/messages` 与 `/messages/image` 无幂等**
   - 证据：`backend/app/services/disputes.py:253` `def _write(action, label, account_id, audit, ...)`，闸门 `disputes.py:260`，平台调用 `disputes.py:134`，审计 `disputes.py:284`；无 `idempotent_call`。
   - 影响：纠纷举证消息重复发送会污染举证记录。
   - 建议：在 `_write` 里包 `idempotent_call(account_id, 'dispute_' + action, conversation_id, payload, _write_once)`，同时覆盖 `/urge`、`/mark-read`（P1-13）。

7. **`POST /api/logistics/{account_id}/orders/{order_id}/resend` 无幂等**
   - 证据：`backend/app/services/logistics.py:195` `def resend_consign(...)`，闸门 `logistics.py:202`，平台调用 `logistics.py:118`，审计 `logistics.py:221`；无 `idempotent_call`。
   - 建议：`idempotent_call(account_id, 'resend_consign', order_id, {...}, _resend_once)`。

### P1 — 平台写、无幂等，或平台写、无审计

8. **`POST /api/promotion/write/{account_id}/{action}` 平台写无审计**（闸门与幂等都有）
   - 证据：`backend/app/services/ad_write.py:75` `def _write(account_id, action, payload=None, confirm=False)`：严格闸门 `ad_write.py:77` → 白名单 `82-85` → 客户端 `86` → `idempotent_call` `92` → `_write_once`（`ad_write.py:109`）在 `ad_write.py:111` 打平台。**整个 `ad_write.py` 里没有任何 `log_operation`**（`grep log_operation|audit backend/app/services/ad_write.py` 返回空）。
   - 影响：推广（改价/改预算等）是花钱的动作，operation_logs 里查不到谁在什么时候做过。
   - 建议：在 `_write_once`（`ad_write.py:109`）的成功与失败分支各补 `common.log_operation(account_id, f'ad_write_{action}', None, ok, detail)`，**不要**传 `kind`（会盖掉 action 名，同 `shop_profile.py:102-103` 的坑）。

9. **`POST /api/shop/{account_id}/profile/nick` / `/desc` / `/avatar` 无幂等**
   - 证据：`backend/app/services/shop_profile.py:95` `def _submit(account_id, action, api, data, extra, audit)`，平台 `common.call_method(...)` 在 `shop_profile.py:100`，审计 `shop_profile.py:104`（失败）/`107`（平台失败）/`114`（成功）；`_write` 在 `shop_profile.py:119`（调 `_submit` 于 `127`）；闸门在公开函数 `shop_profile.py:131`/`138`/`145`。无 `idempotent_call`。
   - 建议：在 `shop_profile.py:119` `_write` 里包 `idempotent_call(account_id, 'shop_profile_' + action, '', fields, _submit)`。

10. **`POST /api/sub-accounts/{account_id}` 及其 4 条子路由无幂等**
    - 证据：`backend/app/services/sub_accounts.py:111` `def _submit(...)`（平台 `sub_accounts.py:116`，审计 `118`/`121`/`127`）；`create` 独立路径：闸门 `sub_accounts.py:135`，平台 `sub_accounts.py:165`，审计 `167`/`171`/`179`；`delete` 闸门 `196`、`update_mobile` `208`、`update_roles` `221`、`update_password` `235`（平台 `244`）。全族无 `idempotent_call`。
    - 影响：建子账号重复提交会建出多个账号；改密码重复提交无大碍但仍是平台写。
    - 建议：`create` 用 `idempotent_call(account_id, 'sub_account_create', '', {'nick','mobile','role_ids'}, _create_once)`；其余 4 条在 `_submit` 里统一包一层。

11. **`POST /api/quick-reply/{account_id}/groups` 等 6 条无幂等**
    - 证据：`backend/app/services/quick_reply.py:176` `def _write(account_id, action, fields)`，平台调用 `quick_reply.py:114`，审计 `quick_reply.py:188`/`191`/`218`；闸门在 6 个公开函数 `quick_reply.py:226`/`233`/`240`/`247`/`255`/`263`。无 `idempotent_call`。
    - 建议：在 `quick_reply.py:176` `_write` 里包 `idempotent_call(account_id, 'quick_reply_' + action, '', fields, _write_once)`。

12. **`POST /api/im/{account_id}/blacklist`、`/blacklist/remove`、`/sessions/transfer`、`/sessions/leave`、`/sessions/rejoin`、`/reception` 无幂等**
    - 证据：同第 5 条漏斗 `backend/app/services/im_actions.py:269`（闸门 `285`、平台 `304`、审计 `323`）。
    - 建议：与第 5 条一并修，`_write` 内包一次即可覆盖全族 7 条。

13. **`POST /api/disputes/{account_id}/{conversation_id}/urge` 与 `/mark-read` 无幂等**
    - 证据：同第 6 条漏斗 `backend/app/services/disputes.py:253`（闸门 `260`、平台 `134`、审计 `284`）。
    - 建议：与第 6 条一并修。

14. **`POST /api/notifications/{account_id}/read` 无幂等**
    - 证据：`backend/app/services/notify.py:275` `def mark_read(account_id, message_id, confirm=None)`，闸门 `notify.py:286`，平台调用 `notify.py:294`，审计 `notify.py:310`；无 `idempotent_call`。
    - 建议：标已读本身幂等，属低危；若要统一，`idempotent_call(account_id, 'notify_mark_read', message_id, {}, _mark_once)`。

15. **`POST /api/chats/incoming` 无幂等（自动回复打平台）**
    - 证据：`backend/app/api/chats.py:130` → `services/chats.py:198` `handle_incoming` → `send_manual`（`chats.py:190` 调用点）→ 平台 `chats.py:629`；另有 AI 回复漏斗 `chats.py:1223` `deliver_ai_reply` → 平台 `chats.py:1289`，审计 `chats.py:1209`。无 `idempotent_call`。
    - 说明：该路由已在白名单登记（入站事件驱动，不是用户点按钮），风险低于第 3 条，故列 P1。
    - 建议：若闲鱼侧会重投同一入站事件，需在 `handle_incoming` 用事件 id 做键。

16. **`POST /api/uploads/images` 是平台写，却既无闸门也无幂等也无审计**
    - 证据：`backend/app/api/uploads.py:67` → `services/uploads.py:295` `common.get_client(account)` + `services/uploads.py:299` `common.call_method(client, ('upload_image',), path)` —— **确实把图上传到闲鱼**，不是纯本地落盘。
    - 现状：该路由在 `GATE_FREE_ALLOWLIST` 里（`backend/tests/test_write_routes_have_gates.py` 40–83 行）。
    - 建议：二选一 ——（a）在 `api/uploads.py:67` 前补一道宽松闸门；（b）保留无闸门，但在白名单注释里把理由从「本地」改成「上传幂等、重传无害」，并补一条 `log_operation(account_id, 'upload_image', None, ok, name)` 以便排查。

### P2 — 文档与代码漂移（非代码缺口）

17. **`docs/CONTRACTS.md` §9.3 与代码不符（两处）**
    - 位置：`docs/CONTRACTS.md` 1719–1724 行。
    - 漂移 a：声称「已覆盖：改地址同意与拒绝」→ 实际 `backend/app/services/trade_write.py:679` `_order_address_write` **没有** `idempotent_call`（见 P0-4）。
    - 漂移 b：声称「未覆盖 `services/orders.py::update_shipment` 与 `::deliver`」→ 实际 `backend/app/services/orders.py:290` 与 `orders.py:358` **都已接** `common.idempotent_call`。
    - 建议：按上面两条改文档，并把「已覆盖清单」补全为：`trade_write._submit`(`trade_write.py:124`)、`trade_write._address_submit`(`trade_write.py:520`)、`orders.update_shipment`(`orders.py:290`)、`orders.deliver`(`orders.py:358`)、`ad_write._write`(`ad_write.py:92`)、`ad.message_read`(`ad.py:606`)、`security_write._submit`(`security_write.py:98`)、`shop` 三条(`shop.py:411`/`476`/`526`)。

## 明确没有缺口的项（防止误判）

- **闸门**：105 条非商品写路由里，除白名单登记项外**全部有闸门**；且**未发现任何「闸门在平台调用之后」**的情况。三处严格档的闸门都先于平台调用：
  - `services/ad.py:571`（闸门）→ `ad.py:579`（平台）
  - `services/ad_write.py:77`（闸门）→ `ad_write.py:111`（平台）
  - `services/qualification.py:278`（闸门）→ `qualification.py:289`（平台）；`qualification.py:298`（闸门）→ `qualification.py:309`（平台）
  - 闸门函数 `common.confirm_required`（`common.py:146`）本身只判确认，不碰库/账号/平台 —— 满足「零平台请求」。
- **本地写（不需要幂等，共 35 条）**：accounts 8 条、agents 7 条、auth 3 条、chats 本地 5 条（`classify`/`ai-draft`/`ai`/`mode`/`read`）、providers 6 条、risk 3 条、settings 1 条、`orders/{order_id}/deliver` 1 条、`im/{account_id}/unread/clean` 1 条。这些**不是平台写**，标「本地写不需要」。
- **平台读（不是写，不需要闸门/幂等，共 3 条）**：`POST /api/chats/sync`、`POST /api/orders/sync`、`POST /api/security-center/{account_id}/appeal/form`。
- **白名单登记（`backend/tests/test_write_routes_have_gates.py` 40–83 行）**：上述本地写与平台读，加 `accounts/{id}/relogin`、`accounts/{id}/captcha`、`chats/{id}/read`、`risk/clear`×2、`uploads/images`，均属已决，不重复报。

## 未完成项 / 口径边界

- 表中 `POST /api/chats/{conversation_id}/ai-draft` 与 `POST /api/security-center/{account_id}/appeal/form` 的**审计列**来自静态扫描的推断（扫描器报 `chats.py:1209` / `security_write.py:158`），**未逐行复核**；这两条都在白名单里、非平台写，不影响缺口清单。
- 幂等键的**运行时**行为（跨进程、重启后回放、指纹冲突返回 `conflict`）未做动态验证，本审计只确认「代码里有没有接 `idempotent_call`」。
- 商品写族（`/api/items/*`、`/api/drafts/*`、`/api/item-templates/*`）按用户要求**未审**，本文件不含它们的行。
