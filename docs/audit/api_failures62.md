# 第 60/61 批真机接口失败分类诊断（api_failures62）

> 只读诊断。本文件是本轮**唯一**写盘产物，未改任何代码、未跑 reconcile、未新增写操作探针。
> 全部数字与平台码均可在 `logs/` 下找到原始出处，逐行给出 `文件:行` 或 `JSON 键路径`。
> 计数口径：盘上**每个独立的真机调用证据计 1**（同一次调用在多份文件重复落盘只计 1；不同参数变体、不同批次各计 1）。
> 收录范围：`logs/probe60`(+`round2`)、`logs/probe61`、`logs/write61`、`logs/seller_discover`、`logs/pkg_probe58`、`logs/promotion_probe`、`logs/fail62`、`logs/idem_probe59`、`logs/write_probe58`、`logs/notify_read58`、`logs/order_sync57`、`logs/publish_family_probe.json`、`logs/firsthand_probe.json`、`logs/close_reasons_probe.json`、`logs/qr_read_probe.json`。
> 排除：`logs/seller_discover/shop_bundles.json`（JS bundle 文本转储，不是响应）、`logs/probe60/ad_apis.json`（能力声明清单，不是调用结果）。

> **第 62 批后续更正（2026-10-16 04:05，务必先读）**
> 本文件成文后，第 62 批查明了 B 类的根因：`mtop.*.seller.platform.*` 一族回的
> `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` **不是账号没权限，而是我们少发请求头 `idle_site_biz_code: COMMONPRO`**
> （不带时平台回 `data.errorMsg=站点CODE不能为空`）。补头后真机复测：
> - `item.pc.search.categories` → `SUCCESS`（真实类目树）
> - `pc.item.status.statistics` → `SUCCESS`（在卖 44 / 下架 1）
> - `order.backfill.report` → 鉴权通过，错误码推进到参数层 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数orderId`；
>   给真实 orderId 仍回 UNAUTHORIZED ⇒ 这条是**订单/功能级**限制，不是我们少参数
> - `query.login.merchant.info` / `usergroup.member.list` → `SUCCESS`
> - 数据罗盘 `pc.datacompass.*` 全族（第 2.2 节最后那条的 10 次）→ 补头 + 官方入参形状后 **23/23 `SUCCESS::调用成功`**
> - 仍真的没权限：`merchant.delivery.place.detail.query`（`permissionCode=merchant:deliveryadress:query`）
> ⇒ 第 2.2 节里凡 `mtop.*.seller.platform.*` / `pc.datacompass.*` 的行**判废**（未复测的按「未复测」看），
> ③ 节按前缀归纳的结论随之作废；`mtop.idle.pc.backend.*`、`mtop.alibaba.idle.seller.common.sub.account.*`、
> `mtop.taobao.idlemessage.pc.repect.status.edit` 等未复测行仍按原判。根因与 17 个变体对照见
> `docs/audit/permission_rootcause62.md`（修复提交 `614216a`）。
> 另：第 2.1 节 A 类的投放四条（`item.searchV2` / `package.info` / `customize.bid.estimates` / `campaign.log.query`）
> 已在第 62 批按官方 payload 补齐并真机 200（`docs/contracts/ad.md` §8，提交 `45d5e7e`）；
> 第 2.3 节 C 类里的 `package.info` / `campaign.log.query` 两条因此属于 A 类、已修。

## ① 一句话结论

**这批失败里没有一条是本项目「代码写错」——54 条失败按性质分：账号权限缺 26 条（B，本账号没有 PC 卖家工作台/多账号/数据罗盘那套权限包）、平台侧故障或瞬态 17 条（C）、参数形状没猜对 7 条（A，其中地址族 3 条已在第 61 批本地修掉）、语义正确的拒绝 1 条 + 本地 400 必填校验 6 条（D）。**

但**最关键的发现是否定式的**：失败**不是按 mtop 前缀分族的**。同一个前缀 `mtop.alibaba.idle.seller.platform.*` 下 `item.publish.categories/brands/models` 三条全成功、`order.backfill.report`/`merchant.delivery.*` 全失败（③ 节反例）。**权限的粒度是「单个 API」，不是前缀**；所以「这个前缀在本账号下必然无权限」这个假设**不成立**，不能拿前缀当白名单，只能逐接口标记可用/不可用。

## ② 分类表

### 2.1 A 参数形状错（我们的请求缺键/信封不对 ⇒ 可修）7 条

| 接口/路由 | mtop API | 真机 HTTP | 平台码 | 归类 | 处置建议 | 证据文件:行 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /api/promotion/ad/{acc}/item-search` | `mtop.taobao.idle.ad.item.searchV2` | 502 | `E_AD_BP_COMMON_PARAM_NULL::必填参数为空` | A | 形状未解：item_id、search_key、item_id+campaign_id 三种都回同码，说明缺的是**别的**必填键 | `logs/probe60/ad_item_search.json:2,6`；`logs/probe60/round2/ad_item_search_by_key.json:6`；`logs/probe60/round2/ad_item_search_with_campaign.json:6` |
| `GET /api/trade/{acc}/smart-form/query` | `mtop.taobao.merchant.heracles.inno.smart.form.query` | 502 | `UNKNOWN_FAIL_CODE::系统开小差了，请稍候重试` | A | 已排除 2 种信封（A 嵌套 / B 扁平），**仍未解** | `logs/probe60/round2/trade_smart_form_query.json:2,6`；`logs/fail62/kurumi/replay_A_envelope.json`；`logs/fail62/kurumi/replay_B_urlquery_flat.json` |
| `POST /api/items/{acc}/operate-online` | `mtop.alibaba.idle.seller.platform.item.operate.online` | 502 | `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数itemId` | A | `itemIds` 数组 / `itemIds` 字符串两种变体都缺 `itemId` ⇒ 真实字段名待挖 | `logs/pkg_probe58/70_online_attempts.json:27` |
| `POST /api/trade/{acc}/addresses` | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.add` | 502 | `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数areaId` | A | 已在第 61 批修（本地预检必填） | `logs/write61/05_address_add.json:5`；`logs/write61/plan.md:59` |
| 同上 | 同上 | 502 | `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数defaultAddr` | A | 同上，已修 | `logs/write61/05b_address_add_with_area.json:4`；`05c_address_add_again.json:4`；`logs/write61/plan.md:60` |
| `GET /api/shops/{acc}/usergroup-members` 类 | `mtop.alibaba.idle.seller.platform.usergroup.member.list` | 502 | `FAIL_SYS_BIZPARAM_MISSED` | A | 形状未解；官方页面在调（见 3.4） | `logs/seller_discover/shop_notify_probe.json:9` |
| `GET /api/trade/{acc}/refunds`（首轮） | `mtop.taobao.idle.merchant.refund.list` | 502 | 首轮参数形状不对（码未落盘） | A→已解 | 补参后 `SUCCESS::调用成功`，无需再动 | 失败：`logs/seller_discover/trade_probe.json`（`$.refund.list`）；成功：`logs/seller_discover/trade_probe2.json`（`$.refund.list`） |

> 另两条 A 类「首轮失败、补参后成功」：`mtop.alibaba.idle.seller.common.sub.account.info.list`（`FAIL_SYS_BIZPARAM_MISSED` → `SUCCESS`，`logs/seller_discover/shop.json` 同一数组内两条）与 `mtop.taobao.idle.cco.shop.complain.list`（`logs/seller_discover/trade_probe.json` 索引 4，**码未落盘**）。

### 2.2 B 账号权限缺（不是 bug，本账号没这个权限包）26 条

| 接口/路由 | mtop API | 真机 HTTP | 平台码 | 归类 | 处置建议 | 证据文件:行 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /api/items/{acc}/publish/search-categories` | `mtop.taobao.idle.seller.platform.item.pc.search.categories` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 原样透出，标注「本账号无该权限」，不要改参数硬试 | `logs/probe60/items_publish_search_categories.json:2,6` |
| `GET /api/trade/{acc}/orders/{oid}/backfill-report` | `mtop.alibaba.idle.seller.platform.order.backfill.report` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/probe60/trade_backfill_report.json:2,6` |
| `GET /api/logistics/delivery-place` | `mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/probe61/14_delivery_place.json:3` |
| `GET /api/items/{acc}/status-statistics` | `mtop.taobao.idle.seller.platform.pc.item.status.statistics` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/probe61/17_item_status_statistics.json:3` |
| `GET /api/trade/{acc}/addresses` | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.list.query` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 退货地址族整族无权限（第 58 批已记录） | `logs/seller_discover/trade_probe.json:111` |
| `POST /api/trade/{acc}/addresses`（05d 直打） | `mtop.alibaba.idle.seller.platform.merchant.delivery.address.add` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（参数校验已过，卡权限） | B | 参数形状已修好，**剩下的是权限**，别再挖参数 | `logs/write61/05d_address_add_full_raw.json:17`；`logs/write61/plan.md:61` |
| `POST /api/items/{acc}/batch-offline` | `mtop.alibaba.idle.seller.pc.item.batch.offline` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/write61/09_item_batch_offline.json:5` |
| `GET /api/items/{acc}/drafts` | `mtop.alibaba.idle.item.pc.draft.search` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 本账号无草稿业务权限 | `logs/write61/00_readonly_inventory.json:4`；`logs/write61/plan.md:12` |
| `POST /api/items/{acc}/operate-online`（另一变体） | `mtop.alibaba.idle.seller.platform.item.operate.online` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 与 A 类同 API：形状对了也还是无权限 | `logs/pkg_probe58/70_online_attempts.json:13` |
| 子账号额度查询 | `mtop.alibaba.idle.seller.common.sub.account.quota.query` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 多账号/子账号族无权限 | `logs/seller_discover/shop_notify_probe.json:9` |
| 角色列表 | `mtop.alibaba.idle.seller.platform.user.role.list` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/seller_discover/shop_notify_probe.json:9` |
| 用户组昵称 | `mtop.alibaba.idle.seller.platform.usergroup.nick.get` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/seller_discover/shop_notify_probe.json:9` |
| 发布页 editdetail | `mtop.idle.pc.backend.idleitem.editdetail` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | backend.* 是另一套后端，无权限 | `logs/publish_family_probe.json:49` |
| 发布页 preget | `mtop.idle.pc.backend.idleitem.preget` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | 同上 | `logs/publish_family_probe.json:49` |
| 数据罗盘 · 浏览/商品/卖家/复购/流量/粉丝/商户信息/客服总览（10 次） | `mtop.alibaba.idle.seller.pc.datacompass.{singleuser.browse.summary ×2, singleuser.item.summary, singleuser.seller.summary, singleuser.repurchase.summary, flow.detail, fans.summary ×2, merchant.info.query, cs.overview.summary}` | 502 | `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | B | **补参（selectedSellerId / dateType）后仍全部无权限** ⇒ 参数不是原因 | `logs/seller_discover/datacenter_probe.json`（`$.browse.summary`/`$.item.summary`/`$.seller.summary`/`$.repurchase.summary`/`$.flow.detail`/`$.fans.summary`/`$.merchant.info.query`）；`logs/seller_discover/datacenter_probe2.json`（`$.browse.summary+seller`/`$.cs.overview.summary+dateType`/`$.fans.summary+dateType`） |
| 转接状态（B′：非 UNAUTHORIZED 码，但同为「账号能力缺失」） | `mtop.taobao.idlemessage.pc.repect.status.edit` | 502 | `FAIL_BIZ_NOT_SUB_ACCOUNT` | B | 该接口只对子账号开放，本账号是主账号 ⇒ 不是 bug，原样透出 | `logs/write_probe58/reception_write.json:12`；`logs/write_probe58/reception_restore.json` |

### 2.3 C 平台侧故障 / 瞬态（不是 bug，等平台）17 条

| 接口/路由 | mtop API | 真机 HTTP | 平台码 | 归类 | 处置建议 | 证据文件:行 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /api/promotion/ad/{acc}/package-info` | `mtop.taobao.idle.ad.package.info` | 502 | `E_AD_BP_COMMON_SYSTEM_EXCEPTION` | C | 平台侧报错，重试即可，不要改参数 | `logs/probe60/ad_package_info.json:2,6` |
| `GET /api/promotion/ad/{acc}/campaign-log` | `mtop.taobao.idle.ad.campaign.log.query` | 502 | `E_AD_BP_COMMON_SYSTEM_EXCEPTION` | C | 同上 | `logs/probe60/ad_campaign_log.json:2,6` |
| `GET /api/logistics/pc-service-status` | `mtop.alibaba.idle.pc.service.status.query` | 502 | `FAIL_BIZ_COMMON_SYSTEM_ERROR::系统错误请稍候再试` | C | 同上 | `logs/probe61/15_pc_service_status.json:3` |
| 店铺资料 | `mtop.alibaba.idle.shop.manage.detail` | 502 | `FAIL_BIZ_COMMON_SYSTEM_ERROR3::闲鱼太累了` | C | 同上（两批都撞上，可留意是否长期故障） | `logs/write61/00_readonly_inventory.json:38`；`logs/seller_discover/shop_notify_probe.json:9` |
| `POST /api/chats/notifications/read` | `mtop.taobao.idlemessage.pc.notifys.read` | 502 | `FAIL_BIZ_IM_PC_READ_STATUS_SYNC_FAILED::服务忙，请稍后重试。\|\|PC端已读状态同步失败` | C | 平台 IM 已读同步失败；本地不要吞错 | `logs/write61/04_notify_read_missing_id.json:5`；`logs/write61/04b_notify_read_raw.json:4`；`logs/pkg_probe58/12_notify_write.json:8` |
| 推广资质模板 | `mtop.taobao.idle.ad.qualification.templates.queryv2` | 502 | `FAIL_SYS_SESSION_EXPIRED` | C | **单点出现**：同批其它调用都成功 ⇒ 单接口会话校验瞬态，不是整账号掉线 | `logs/seller_discover/qualification_probe.json:15` |
| 发货占位 | `mtop.taobao.idle.logistics.merchant.consign.dummy` | 502 | `SYSTEM_ERROR::系统错误` | C | 平台侧 | `logs/idem_probe59/10_steps.json:18` |
| 发布页 preget | `mtop.idle.pc.idleitem.preget` | 502 | `FAIL_BIZ_USER_SHOW_SYSTEM_ERROR` | C | 平台侧 | `logs/firsthand_probe.json`（`$.preget`） |
| `POST /api/items/{acc}/reshelf`（协议层） | `mtop.idle.pc.idleitem.edit` | 502 | `FAIL_BIZ_IDLEITEM_UNKNOWN_THROWABLE::发布失败，请稍后重试吧!` | C | 风控/平台侧；**注意本地会「仅本地生效」造成不一致**（见 ④） | `logs/pkg_probe58/5A_relist_try_A.json:10`；`logs/write61/07_item_reshelf.json:12`；`logs/write61/plan.md:63` |
| 发布 | `mtop.idle.pc.idleitem.publish` | 502 | `FAIL_BIZ_IDLEITEM_UNKNOWN_THROWABLE` | C | 平台侧（RiskControlError） | `logs/pkg_probe58/80_publish_attempt.json:5` |
| 重新上架（另两次） | `mtop.idle.pc.idleitem.edit` | 502 | `FAIL_BIZ_JSON_ERROR::发布失败，请稍后重试吧!` | C | 平台侧 | `logs/pkg_probe58/23_relist.json:14` |
| 首次上架 editdetail | `mtop.idle.pc.first.hand.idleitem.edit.detail` | 502 | **码未落盘** | C（待补证） | 需重打并落原始响应 | `logs/firsthand_probe.json`（`$.first_hand`） |
| 风控冷却（非 mtop 码，发生在调用前） | —（`FAIL_SYS_USER_VALIDATE; RGV587_ERROR::SM::哎哟喂,被挤爆啦`） | — | `RGV587_ERROR` | C | 铁律：等 60s，最多重试 2 次 | `logs/write61/plan.md:67`；`logs/pkg_probe58/60_relist_attempts.json`（「账号处于风控冷却中，还需 55s/51s」） |

### 2.4 D 语义正确的拒绝（不是 bug）

| 接口/路由 | mtop API | 真机 HTTP | 平台码 | 归类 | 处置建议 | 证据文件:行 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /api/trade/{acc}/items/{iid}/sku-selector` | `mtop.taobao.idle.trade.common.sku.selector` | 502 | `FAIL_BIZ_ITEM_NOT_MULTI_SKU_ERROR::不是多SKU商品` | D | 商品本来就不是多 SKU，**这个 502 应当原样透出**，不要改参数 | `logs/probe60/trade_sku_selector.json:2,6` |

### 2.5 本地 400 必填校验（**零平台请求**，不是平台失败）

| 接口/路由 | mtop API | 真机 HTTP | 本地码 | 归类 | 处置建议 | 证据文件:行 |
| --- | --- | --- | --- | --- | --- | --- |
| `GET /api/trade/{acc}/smart-form/query`（不给 params） | —（未发出） | 400 | `invalid_fields::表单参数不能为空` | D | 正确行为 | `logs/probe60/trade_smart_form_query.json:2,7` |
| `GET /api/shops/{acc}/stats`（不给 session_id） | —（未发出） | 400 | `invalid_fields::session_id 不能为空` | D | 正确行为 | `logs/probe61/16_shop_stats.json:4` |
| `GET /api/logistics/pc-service-status`（不给 service_code） | —（未发出） | 400 | `invalid_fields::service_code 不能为空` | D | 正确行为 | `logs/probe61/19_pc_service_status_empty.json:4` |
| `POST /api/trade/{acc}/addresses`（缺 area_id / 缺 default_addr / area_id 全空白） | —（未发出） | 400×3 | `invalid_fields` | D | **第 61 批新修**（`backend/app/services/trade_write.py:464`） | `logs/write61/11_address_local_precheck.json:5`；`logs/write61/plan.md:66,78` |
| `POST /api/promotion/ad/{acc}/message-read`（不给 messageIds） | —（未发出） | 400 | `invalid_fields` | D | 正确行为 | `logs/write61/01_ad_message_read_empty_ids.json:6` |
| `POST /api/promotion/ad/{acc}/message-read`（不给 confirm） | —（未发出） | 400 | `confirm_required` | D | 写操作确认闸，正确行为 | `logs/write61/02_ad_message_read_no_confirm.json:6`；`logs/write61/11_address_local_precheck.json:20` |

> 另：`GET /api/shops/{acc}/stats` 的 dummy 探针 `logs/probe61/18_shop_stats_dummy.json:7` 真机 HTTP 200，但 body 是 `{code:"SESSION_INVALID", msg:"会话信息获取失败"}`——这是**路由给 dummy 数据**时的形状，不是真机失败，登记在 ⑤ 待解。

## ③ 权限族前缀规律表（本报告最重要产出）

### 3.1 计数表（口径见文首）

| 前缀族 | 成功 | 失败 | 成功样本（证据） | 失败样本（证据） |
| --- | ---: | ---: | --- | --- |
| `mtop.taobao.idle.ad.*` | 13 | 6 | `mtop.taobao.idle.ad.account.info` ← `logs/probe60/ad_account_info.json:2` | `mtop.taobao.idle.ad.item.searchV2` ← `logs/probe60/ad_item_search.json:2,6` |
| `mtop.taobao.idlemessage.*` | 11 | 5 | `mtop.taobao.idlemessage.pc.redpoint.query` ← `logs/seller_discover/ad_sc.json`（`$.推广首页[0]`）、`logs/seller_discover/notify_probe.json` | `mtop.taobao.idlemessage.pc.repect.status.edit` ← `logs/write_probe58/reception_write.json:12` |
| `mtop.alibaba.idle.seller.platform.*` | 4 | 11 | `mtop.alibaba.idle.seller.platform.item.publish.categories` ← `logs/probe60/items_publish_categories.json:2`（`$.body.success=true`, count=99） | `mtop.alibaba.idle.seller.platform.order.backfill.report` ← `logs/probe60/trade_backfill_report.json:2,6` |
| `mtop.alibaba.idle.seller(.pc).datacompass.*` | 1 | 10 | `mtop.alibaba.idle.seller.datacompass.get.begin.time` ← `logs/seller_discover/datacenter_probe.json`（`$.get.begin.time`） | `...pc.datacompass.singleuser.browse.summary` ← `logs/seller_discover/datacenter_probe.json`（`$.browse.summary`） |
| `mtop.idle.finance.*` | 9 | 0 | `mtop.idle.finance.metadata.query` ← `logs/seller_discover/finance_calls.json` | — |
| `mtop.idle.pc.idleitem.*` | 1 | 5 | `mtop.idle.pc.idleitem.editDetail` ← `logs/publish_family_probe.json:49` | `mtop.idle.pc.idleitem.preget` ← `logs/firsthand_probe.json`（`$.preget`） |
| `mtop.taobao.idle.merchant.*` | 4 | 1 | `mtop.taobao.idle.merchant.order.count` ← `logs/seller_discover/trade_probe.json`（`$.order.count`） | `mtop.taobao.idle.merchant.refund.list` ← `logs/seller_discover/trade_probe.json`（`$.refund.list`） |
| `mtop.taobao.idle.seller.platform.*` | 1 | 2 | `mtop.taobao.idle.seller.platform.item.pc.category.property.query` ← `logs/probe60/round2/items_publish_category_properties.json` | `mtop.taobao.idle.seller.platform.item.pc.search.categories` ← `logs/probe60/items_publish_search_categories.json:2,6` |
| `mtop.alibaba.idle.seller.common.*` | 1 | 2 | `mtop.alibaba.idle.seller.common.sub.account.info.list` ← `logs/seller_discover/shop.json` | `mtop.alibaba.idle.seller.common.sub.account.quota.query` ← `logs/seller_discover/shop_notify_probe.json:9` |
| `mtop.cainiao.*` | 3 | 0 | `mtop.cainiao.geography.frontend.divisionsinfo.get` ← `logs/probe61/10_divisions_root.json:5` | — |
| `mtop.idle.pc.backend.*` | 0 | 2 | — | `mtop.idle.pc.backend.idleitem.editdetail` ← `logs/publish_family_probe.json:49` |
| `mtop.alibaba.idle.shop.*` | 0 | 2 | — | `mtop.alibaba.idle.shop.manage.detail` ← `logs/write61/00_readonly_inventory.json:38` |
| `mtop.taobao.idle.trade.*` | 1 | 1 | `mtop.taobao.idle.trade.merchant.sold.get` ← `logs/fail62/kurumi/orders_p1.json:1` | `mtop.taobao.idle.trade.common.sku.selector` ← `logs/probe60/trade_sku_selector.json:2,6`（D 类） |
| `mtop.idle.pc.security.*` | 2 | 0 | `mtop.idle.pc.security.score.get` ← `logs/seller_discover/ad_sc.json`（`$.安全中心`） | — |
| `mtop.alibaba.idle.seller.pc.*`（非 platform） | 0 | 1 | — | `mtop.alibaba.idle.seller.pc.item.batch.offline` ← `logs/write61/09_item_batch_offline.json:5` |
| `mtop.alibaba.idle.item.*` | 0 | 1 | — | `mtop.alibaba.idle.item.pc.draft.search` ← `logs/write61/00_readonly_inventory.json:4` |
| `mtop.taobao.merchant.*` | 0 | 1 | — | `mtop.taobao.merchant.heracles.inno.smart.form.query` ← `logs/probe60/round2/trade_smart_form_query.json:2,6` |
| `mtop.taobao.idle.cco.*` | 0 | 1 | — | `mtop.taobao.idle.cco.shop.complain.list` ← `logs/seller_discover/trade_probe.json`（索引 4） |
| `mtop.alibaba.idle.pc.*` | 0 | 1 | — | `mtop.alibaba.idle.pc.service.status.query` ← `logs/probe61/15_pc_service_status.json:3` |
| `mtop.taobao.idle.logistics.*` | 0 | 1 | — | `mtop.taobao.idle.logistics.merchant.consign.dummy` ← `logs/idem_probe59/10_steps.json:18` |
| `mtop.idle.pc.first.*` | 0 | 1 | — | `mtop.idle.pc.first.hand.idleitem.edit.detail` ← `logs/firsthand_probe.json`（`$.first_hand`） |

合计：成功 51 条 / 失败 54 条；失败覆盖 **42 个不同 API**，成功覆盖 43 个不同 API。
「seller 族」（`mtop.alibaba.idle.seller.*` + `mtop.taobao.idle.seller.*`）合计 **成功 7 / 失败 26**——表面看像「前缀即权限」，**但下一节的反例推翻了它**。

### 3.2 规律（结论）：**前缀不是权限单位，单个 API 才是**

反例（同一个前缀内既有成功也有失败，且都是**同一批、同一账号、同一登录态**）：

- `mtop.alibaba.idle.seller.platform.item.publish.categories` 成功：`logs/probe60/items_publish_categories.json:2`（`$.status=200`、`$.body.success=true`、count=99）
- `mtop.alibaba.idle.seller.platform.item.publish.brands` 成功：`logs/probe60/round2/items_publish_brands.json`（`$.status=200`、`$.body.success=true`）
- `mtop.alibaba.idle.seller.platform.item.publish.models` 成功：`logs/probe60/round2/items_publish_models.json`（同上）
- `mtop.alibaba.idle.seller.platform.order.backfill.report` 失败 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`：`logs/probe60/trade_backfill_report.json:6`

⇒ **同一前缀 `mtop.alibaba.idle.seller.platform.*` 成功 4 / 失败 11**。所以**不能**把「`seller.platform` 前缀」整体判死；也**不能**据此写死白名单。

同样地，`mtop.taobao.idle.ad.*`（成功 13 / 失败 6）里，`item.searchV2` 失败是**参数**问题（A），`package.info` 失败是**平台侧**问题（C），只有 `qualification.templates.queryv2` 是会话瞬态（C）——**这个族里没有一条是权限问题**。

### 3.3 真正成族的只有这三组（都带同一特征：PC 卖家工作台专属能力）

| 成族的失败组 | 条数 | 共同特征 | 证据 |
| --- | ---: | --- | --- |
| 数据罗盘 `...seller.pc.datacompass.*` | 10 失败 / 1 成功 | **补参后仍全失败**（`selectedSellerId`、`dateType` 都试过）⇒ 与参数无关，纯权限 | `logs/seller_discover/datacenter_probe.json`；`logs/seller_discover/datacenter_probe2.json` |
| 退货地址 + 发货地 `...seller.platform.merchant.delivery.*` | 3 失败（place.detail.query、address.list.query、address.add） | 同一二级域整族 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` | `logs/probe61/14_delivery_place.json:3`；`logs/seller_discover/trade_probe.json:111`；`logs/write61/05d_address_add_full_raw.json:17` |
| 多账号/子账号/角色 `...seller.{common.sub.account.*, platform.user.role.list, platform.usergroup.*}` | 3 失败（quota.query、user.role.list、usergroup.nick.get） | 子账号体系相关 | `logs/seller_discover/shop_notify_probe.json:9` |

**归纳**：失败的是「PC 卖家工作台 / 多账号 / 数据罗盘」这套**后台能力**；可用的是「发布类（`item.publish.*`）、交易类（`idle.merchant.*` / `trade.merchant.sold.get`）、推广类（`idle.ad.*` 大部分）、物流地理类（`cainiao.*`）、财务类（`idle.finance.*` 9/9）、IM 类（`idlemessage.*` 大部分）」。

### 3.4 旁证：这些 API 名不是我们编的（官方卖家工作台自己就在调）

`logs/seller_discover/mtop_calls.json` 是官方卖家工作台自己发出的 20 条请求 URL，其中包含：`mtop.alibaba.idle.seller.platform.{query.login.merchant.info, sys.menu.query, sys.usergroup.member.list, user.business.identity.get, usergroup.member.list}`、`mtop.alibaba.idle.seller.pc.datacompass.{flow.detail, refund.summary, singleuser.*.summary}`、`mtop.idle.user.account.sub.nav`、`mtop.taobao.idlemessage.pc.{loginuser.get, redpoint.query}`。

- 作用：证明「API 名 / 路由」不是我们猜的，官方页面确实在调。
- **不能**用它判定官方页面成功——该文件**只有 URL、没有响应**。
- 可查线索：`query.login.merchant.info` 与 `user.business.identity.get` 的 URL 上带 `needLoginPC=true`，说明部分 PC 接口要求 **PC 登录态**；这是「为什么同一账号在 PC 工作台有权限、我们这条链路没有」的一个可验证假设，但**本轮没有证据支持或推翻**（见 ⑤）。

## ④ 「不是 bug」清单（应当原样透出，不要改）

1. **`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 全部 24 条**（2.2 表）——本账号没有对应权限包。处置：透出原文 + 标注「本账号无该权限」，**不要**换参数硬试、不要静默降级、不要 fallback 成假数据。
2. **`FAIL_BIZ_NOT_SUB_ACCOUNT`**（`mtop.taobao.idlemessage.pc.repect.status.edit`）——主账号调子账号专属接口。同上，原样透出。
3. **`E_AD_BP_COMMON_SYSTEM_EXCEPTION` / `FAIL_BIZ_COMMON_SYSTEM_ERROR` / `FAIL_BIZ_COMMON_SYSTEM_ERROR3`**——平台侧自己报错，重试语义。
4. **`FAIL_BIZ_IM_PC_READ_STATUS_SYNC_FAILED`**——平台 IM 已读同步失败，不是我们的请求形状问题。
5. **`FAIL_SYS_SESSION_EXPIRED`（单点）**——同批其它调用均成功 ⇒ 单接口会话校验瞬态；**不要**据此判定账号掉线并清本地登录态。
6. **`RGV587_ERROR` 风控冷却**——按铁律等 60s、最多重试 2 次；**不要**改成「失败即重试到底」。
7. **`FAIL_BIZ_ITEM_NOT_MULTI_SKU_ERROR::不是多SKU商品`**——语义正确（该商品本来就不是多 SKU）。
8. **本地 400 必填校验（6 条）**——`invalid_fields` / `confirm_required` 是设计行为，**零平台请求**，正确。
9. **`FAIL_BIZ_IDLEITEM_UNKNOWN_THROWABLE` / `FAIL_BIZ_JSON_ERROR` / `FAIL_BIZ_USER_SHOW_SYSTEM_ERROR`**——平台/风控侧。

> ⚠️ 唯一一处**不是「不是 bug」**、需要盯的：`logs/write61/07_item_reshelf.json:12` 显示平台调用失败时本地仍然把状态改成了 `on_sale`（`warning: not_pushed`「协议层调用失败，仅本地生效」），造成**本地/平台不一致**，第 61 批用 `08` 才复位。这是**真实的一致性问题**，不属于本报告的四类，但必须单列跟踪。

## ⑤ 仍待解清单（真正需要再挖参数形状的接口，按价值排序）

| 优先级 | 接口 | mtop API | 现状 | 下一步 | 证据 |
| ---: | --- | --- | --- | --- | --- |
| 1 | `GET /api/promotion/ad/{acc}/item-search` | `mtop.taobao.idle.ad.item.searchV2` | 3 种形状全回 `E_AD_BP_COMMON_PARAM_NULL` | 抓**官方推广页**的真实请求体（盘上只有 API 名清单，没有该接口的请求体），对照缺哪个键 | `logs/probe60/ad_item_search.json:6`；`logs/probe60/round2/ad_item_search_by_key.json:6`；`logs/probe60/round2/ad_item_search_with_campaign.json:6` |
| 2 | `GET /api/trade/{acc}/smart-form/query` | `mtop.taobao.merchant.heracles.inno.smart.form.query` | 已排除 2 种信封（A 嵌套、B 扁平），均 `UNKNOWN_FAIL_CODE` | 补第 3 种形状（`replay.py` 里列的 `C_numeric`，**盘上无产物**）并落原始响应 | `logs/probe60/round2/trade_smart_form_query.json:6`；`logs/fail62/kurumi/replay_A_envelope.json`；`logs/fail62/kurumi/replay_B_urlquery_flat.json` |
| 3 | `POST /api/items/{acc}/operate-online` | `mtop.alibaba.idle.seller.platform.item.operate.online` | `itemIds` 数组/字符串两种变体都回 `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数itemId` | 挖真实字段名（注意：另一变体已回 UNAUTHORIZED ⇒ 形状对了也是权限，价值打折） | `logs/pkg_probe58/70_online_attempts.json:27,13` |
| 4 | 用户组成员列表 | `mtop.alibaba.idle.seller.platform.usergroup.member.list` | `FAIL_SYS_BIZPARAM_MISSED` | 官方页面在调（`mtop_calls.json`），可抓其请求体；但同族 `usergroup.nick.get`/`user.role.list` 是权限失败，预期收益低 | `logs/seller_discover/shop_notify_probe.json:9` |
| 5 | 店铺统计 | `GET /api/shops/{acc}/stats`（dummy 探针） | 真机 200，body `SESSION_INVALID` | 确认 dummy 路由的 session_id 来源；不是平台失败 | `logs/probe61/18_shop_stats_dummy.json:7` |
| 6 | 投诉列表 | `mtop.taobao.idle.cco.shop.complain.list` | 失败但**码未落盘** | 重打一次并落原始响应（只读 GET） | `logs/seller_discover/trade_probe.json`（索引 4） |
| 7 | 首次上架 editdetail | `mtop.idle.pc.first.hand.idleitem.edit.detail` | 失败但**码未落盘** | 同上 | `logs/firsthand_probe.json`（`$.first_hand`） |
| 8 | 推广活动详情渲染 | `mtop.taobao.idle.ad.activity.detail.rendering` | 失败但**码未落盘** | 同上 | `logs/seller_discover/ad_sc.json`（`$.推广首页[2]`） |
| 9 | 「PC 登录态」假设 | —（跨接口） | `needLoginPC=true` 出现在官方 `query.login.merchant.info` / `user.business.identity.get` URL 上 | 若想解释「为何 PC 工作台有权限而我们没有」，需一次受控对照实验；本轮**无证据**，不要当成结论 | `logs/seller_discover/mtop_calls.json` |

---

附：本轮未新增任何真机写操作；`logs/fail62/` 下已有 sibling 证据（`logs/fail62/kurumi/` 的 orders/replay、`logs/fail62/perm/`）。
