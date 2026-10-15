# coverage63 核销报告：coverage57 §3「可实现」77 条逐条状态

- 工作流：coverage63（第 63 批，共享任务板 task-25）
- 仓库：`C:\Users\Administrator\Projects\xianyu-ai-workbench`（HEAD `511ce58`）
- 对照文档：`docs/audit/coverage57.md` §3（该文件第 36-112 行，正好 77 行）
- 口径：**逐条**给状态，每条一行；序号沿用 coverage57 §3 的行序，便于逐行对账

## 一、结论

| 状态 | 条数 |
| --- | --- |
| 已实现（协议层常量 + 真实调用点） | 39 |
| 仅登记（常量已定义，但没有调用路径） | 2 |
| 未实现（`backend/app` 里连字面量都没有） | 34 |
| 不可实现（有真机平台拒绝硬证据） | 2 |
| **合计** | **77** |

逐条证据见 §三。口径与复跑方式见 §二 —— **本表由脚本按同一口径生成，不是手填**。

## 二、判定口径（可复跑）

1. **已实现** = 协议层（`backend/app/xianyu/*.py`）有 `大写常量 = "<mtop 名>"` 的定义，且该常量在 `backend/app` 里**有定义行以外的引用**（注册表 / `client.mtop(...)` 调用 / 服务层引用）—— 即真有一条可调用路径。
2. **仅登记** = 常量定义了，但引用只出现在**只读名单 / 不支持表**里（`WRITE_APIS`、`UNSUPPORTED`），没有调用路径。
3. **未实现** = 连字面量都没有（注释里提到接口名不算，那是「登记」不是「实现」）。
4. **不可实现** = 有**真机**平台拒绝硬证据（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`），且日志能指到具体那条 api。

扫描方式（复跑：在仓库根执行）：

```python
import io, os, re
names = [逐行取 docs/audit/coverage57.md 第 36-112 行的 mtop 名]   # 77 条
src = {p: io.open(p, encoding="utf-8").read()
       for p in 遍历 backend/app/{xianyu,services,api,agents}/*.py}
for name in names:
    cons = [(m.group(1), p) for p, t in src.items()
            for m in re.finditer(r"^([A-Z][A-Z0-9_]*)\s*=\s*['\"]" + re.escape(name) + r"['\"]", t, re.M)]
    # 常量有定义 + 定义行以外还有引用 => 已实现；只有定义 => 仅登记；无定义 => 未实现
```

## 三、逐条核销（77 条）

### 3.1 已实现（39 条）

| # | 接口名 | 状态 | 结论 + 证据 |
| --- | --- | --- | --- |
| 1 | ``mtop.alibaba.idle.ad.account.card.v2`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:73`（`ACCOUNT_CARD_V2_API`），调用点 `backend/app/xianyu/ad.py:102`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3268292` | |
| 2 | ``mtop.alibaba.idle.ad.bp.coupon.info.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:66`（`BP_COUPON_INFO_API`），调用点 `backend/app/xianyu/ad.py:100`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3246590` | |
| 3 | ``mtop.alibaba.idle.ad.bp.coupon.statistic.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:68`（`BP_COUPON_STATISTIC_API`），调用点 `backend/app/xianyu/ad.py:101`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3246428` | |
| 5 | ``mtop.alibaba.idle.pc.service.status.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/shop.py:152`（`PC_SERVICE_STATUS_API`），调用点 `backend/app/xianyu/shop.py:162`；建议归属「`backend/app/services/shop.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141260` | |
| 6 | ``mtop.alibaba.idle.seller.pc.common.item.search`` | 已实现 | **已实现**：定义 `backend/app/xianyu/items_read.py:42`（`SEARCH_API`），调用点 `backend/app/xianyu/draft.py:40`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3071597` | |
| 7 | ``mtop.alibaba.idle.seller.pc.shop.stats.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/datacenter.py:1378`（`SHOP_STATS_API`），调用点 `backend/app/xianyu/datacenter.py:1390`；建议归属「`backend/app/services/datacenter.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1906144` | |
| 8 | ``mtop.alibaba.idle.seller.platform.idle.category.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/items_read.py:44`（`CATEGORY_API`），调用点 `backend/app/xianyu/items_read.py:22`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/datacenter_main.js:1:2494726` | |
| 9 | ``mtop.alibaba.idle.seller.platform.item.operate.report.get`` | 已实现 | **已实现**：定义 `backend/app/xianyu/items_read.py:46`（`OPERATE_REPORT_API`），调用点 `backend/app/xianyu/items_read.py:23`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3081363` | |
| 10 | ``mtop.alibaba.idle.seller.platform.item.publish.brands`` | 已实现 | **已实现**：定义 `backend/app/xianyu/item_publish.py:53`（`BRANDS_API`），调用点 `backend/app/xianyu/item_publish.py:69`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3073434` | |
| 11 | ``mtop.alibaba.idle.seller.platform.item.publish.categories`` | 已实现 | **已实现**：定义 `backend/app/xianyu/item_publish.py:51`（`CATEGORIES_API`），调用点 `backend/app/xianyu/item_publish.py:312`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3832682` | |
| 12 | ``mtop.alibaba.idle.seller.platform.item.publish.models`` | 已实现 | **已实现**：定义 `backend/app/xianyu/item_publish.py:55`（`MODELS_API`），调用点 `backend/app/xianyu/item_publish.py:69`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3073829` | |
| 13 | ``mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/logistics.py:240`（`DELIVERY_PLACE_DETAIL_API`），调用点 `backend/app/xianyu/logistics.py:243`；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`同类已实现 mtop.alibaba.idle.seller.platform.merchant.delivery.address.add @ backend/app/api/trade_write.py:228` | |
| 14 | ``mtop.alibaba.idle.seller.platform.order.backfill.report`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:174`（`BACKFILL_REPORT_API`），调用点 `backend/app/xianyu/trade_write.py:784`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:2608322` | |
| 18 | ``mtop.cainiao.geography.frontend.divisionsinfo.get`` | 已实现 | **已实现**：定义 `backend/app/xianyu/division.py:35`（`DIVISIONS_API`），调用点 `backend/app/xianyu/division.py:59`；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3387045` | |
| 33 | ``mtop.idle.pc.idleitem.preget`` | 已实现 | **已实现**：定义 `backend/app/xianyu/items_read.py:50`（`PREGET_API`），调用点 `backend/app/xianyu/items_read.py:25`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3062139` | |
| 34 | ``mtop.taobao.idle.ad.account.info`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:46`（`ACCOUNT_INFO_API`），调用点 `backend/app/xianyu/ad.py:90`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301473` | |
| 35 | ``mtop.taobao.idle.ad.activity.detail.rendering`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:76`（`ACTIVITY_DETAIL_RENDERING_API`），调用点 `backend/app/xianyu/ad.py:103`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3268518` | |
| 37 | ``mtop.taobao.idle.ad.campaign.log.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:58`（`CAMPAIGN_LOG_API`），调用点 `backend/app/xianyu/ad.py:96`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:1665384` | |
| 38 | ``mtop.taobao.idle.ad.customize.bid.estimates`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:52`（`CUSTOMIZE_BID_ESTIMATES_API`），调用点 `backend/app/xianyu/ad.py:93`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301315` | |
| 39 | ``mtop.taobao.idle.ad.group.data.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:56`（`GROUP_DATA_API`），调用点 `backend/app/xianyu/ad.py:95`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3253874` | |
| 40 | ``mtop.taobao.idle.ad.group.list`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:54`（`GROUP_LIST_API`），调用点 `backend/app/xianyu/ad.py:94`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3253585` | |
| 43 | ``mtop.taobao.idle.ad.item.searchV2`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:50`（`ITEM_SEARCH_API`），调用点 `backend/app/xianyu/ad.py:92`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301167` | |
| 44 | ``mtop.taobao.idle.ad.message.detail`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:61`（`MESSAGE_DETAIL_API`），调用点 `backend/app/xianyu/ad.py:98`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3678352` | |
| 45 | ``mtop.taobao.idle.ad.message.list`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:60`（`MESSAGE_LIST_API`），调用点 `backend/app/xianyu/ad.py:97`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3678207` | |
| 46 | ``mtop.taobao.idle.ad.message.read`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:62`（`MESSAGE_READ_API`），调用点 `backend/app/xianyu/ad.py:354`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3678499` | |
| 47 | ``mtop.taobao.idle.ad.message.unread.info`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:63`（`MESSAGE_UNREAD_API`），调用点 `backend/app/xianyu/ad.py:99`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:703173` | |
| 49 | ``mtop.taobao.idle.ad.package.info`` | 已实现 | **已实现**：定义 `backend/app/xianyu/ad.py:48`（`PACKAGE_INFO_API`），调用点 `backend/app/xianyu/ad.py:91`；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301020` | |
| 62 | ``mtop.taobao.idle.merchant.unconsign.detail`` | 已实现 | **已实现**：定义 `backend/app/xianyu/logistics.py:233`（`UNCONSIGN_DETAIL_API`），调用点 `backend/app/xianyu/logistics.py:243`；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3138431` | |
| 65 | ``mtop.taobao.idle.seller.platform.item.pc.category.property.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/item_publish.py:57`（`CATEGORY_PROPERTY_API`），调用点 `backend/app/xianyu/item_publish.py:69`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3074576` | |
| 66 | ``mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/items_read.py:48`（`SEARCH_TERM_API`），调用点 `backend/app/xianyu/items_read.py:24`；建议归属「`backend/app/services/common.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3075363` | |
| 67 | ``mtop.taobao.idle.seller.platform.item.pc.search.categories`` | 已实现 | **已实现**：定义 `backend/app/xianyu/item_publish.py:59`（`SEARCH_CATEGORIES_API`），调用点 `backend/app/xianyu/item_publish.py:69`；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3073053` | |
| 68 | ``mtop.taobao.idle.seller.platform.pc.item.status.statistics`` | 已实现 | **已实现**：定义 `backend/app/xianyu/datacenter.py:1385`（`ITEM_STATUS_STATISTICS_API`），调用点 `backend/app/xianyu/datacenter.py:1396`；建议归属「`backend/app/services/common.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3075766` | |
| 69 | ``mtop.taobao.idle.trade.close.by.seller`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:142`（`CLOSE_BY_SELLER_IM_API`），调用点 `backend/app/xianyu/trade_write.py:709`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`同类已实现 mtop.taobao.idle.trade.merchant.close.by.seller @ backend/app/services/trade_write.py:734` | |
| 70 | ``mtop.taobao.idle.trade.common.sku.selector`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:168`（`SKU_SELECTOR_API`），调用点 `backend/app/xianyu/trade_write.py:776`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1841743` | |
| 72 | ``mtop.taobao.idle.trade.order.cancel`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:133`（`ORDER_CANCEL_API`），调用点 `backend/app/xianyu/trade_write.py:692`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`同类已实现 mtop.taobao.idle.trade.merchant.close.by.seller @ backend/app/services/trade_write.py:734` | |
| 73 | ``mtop.taobao.idle.trade.seller.delay.confirm`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:152`（`SELLER_DELAY_CONFIRM_API`），调用点 `backend/app/xianyu/trade_write.py:727`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1832390` | |
| 74 | ``mtop.taobao.idle.trade.user.adjust.price`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:161`（`USER_ADJUST_PRICE_API`），调用点 `backend/app/xianyu/trade_write.py:768`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1831797` | |
| 76 | ``mtop.taobao.merchant.heracles.inno.smart.form.callback`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:201`（`SMART_FORM_CALLBACK_API`），调用点 `backend/app/xianyu/trade_write.py:895`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:802665` | |
| 77 | ``mtop.taobao.merchant.heracles.inno.smart.form.query`` | 已实现 | **已实现**：定义 `backend/app/xianyu/trade_write.py:188`（`SMART_FORM_QUERY_API`），调用点 `backend/app/xianyu/trade_write.py:819`；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:798939` | |

### 3.2 仅登记（2 条）

| # | 接口名 | 状态 | 结论 + 证据 |
| --- | --- | --- | --- |
| 4 | ``mtop.alibaba.idle.pc.service.open`` | 仅登记 | **仅登记**：只有常量定义 `backend/app/xianyu/shop.py:159`，且只被塞进 `WRITE_APIS` 只读名单（:163）—— 没有调用路径（它是开通服务的写入口）；建议归属「`backend/app/services/shop.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141359` | |
| 36 | ``mtop.taobao.idle.ad.bp.data.download`` | 仅登记 | **仅登记**：只有常量定义 `backend/app/xianyu/ad.py:85`，且只出现在 `backend/app/services/ad.py:51` 的 `UNSUPPORTED` 表里 —— 官方用它建异步导出任务（有副作用），只登记不接；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3115691` | |

### 3.3 未实现（34 条）

| # | 接口名 | 状态 | 结论 + 证据 |
| --- | --- | --- | --- |
| 15 | ``mtop.alibaba.idle.seller.platform.search.box`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3076161` | |
| 16 | ``mtop.alibaba.idle.service.open`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/shop.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1805203` | |
| 17 | ``mtop.alibaba.idle.service.status.query`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/shop.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1804926` | |
| 19 | ``mtop.cainiao.ld.detail.tradeid.ordercode.mailno.rescode.get.xy`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:955017` | |
| 20 | ``mtop.idle.idleitem.draft.edit`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`同类已实现 mtop.alibaba.idle.item.pc.draft.update @ backend/app/xianyu/draft.py:44` | |
| 21 | ``mtop.idle.idleitem.draft.publish`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142356` | |
| 22 | ``mtop.idle.idleitem.security.check`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142217` | |
| 23 | ``mtop.idle.item.first.publish.service.cards.list`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2989577` | |
| 24 | ``mtop.idle.kgraph.sku.name.recommend`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2715095` | |
| 25 | ``mtop.idle.pc.backend.idleitem.edit`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142809` | |
| 28 | ``mtop.idle.pc.backend.idleitem.publish`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141789` | |
| 29 | ``mtop.idle.pc.backend.publish.service.cards.list`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141016` | |
| 30 | ``mtop.idle.pc.first.hand.idleitem.edit`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1806275` | |
| 31 | ``mtop.idle.pc.first.hand.idleitem.edit.detail`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3062866` | |
| 32 | ``mtop.idle.pc.first.hand.idleitem.publish`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1805706` | |
| 41 | ``mtop.taobao.idle.ad.init.user.site.info`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3617477` | |
| 42 | ``mtop.taobao.idle.ad.item.recommend`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3300908` | |
| 48 | ``mtop.taobao.idle.ad.new.item.bid.recommend`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/ad_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3255163` | |
| 50 | ``mtop.taobao.idle.deposit`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2978137` | |
| 51 | ``mtop.taobao.idle.item.pc.category.list`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1804740` | |
| 52 | ``mtop.taobao.idle.item.priceadvice`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1806594` | |
| 53 | ``mtop.taobao.idle.kgraph.pc.property.recommend`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141448` | |
| 54 | ``mtop.taobao.idle.kgraph.property.search`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2289736` | |
| 55 | ``mtop.taobao.idle.kgraph.text.diagnosis`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142034` | |
| 56 | ``mtop.taobao.idle.local.poi.get`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4140864` | |
| 57 | ``mtop.taobao.idle.logistics.merchant.consign.page.render`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:844126` | |
| 58 | ``mtop.taobao.idle.logistics.merchant.excel.consign.offline`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:872982` | |
| 59 | ``mtop.taobao.idle.logistics.merchant.oss.sts.get`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`同类已实现 mtop.taobao.idle.logistics.merchant.consign.offline @ backend/app/api/trade_write.py:40` | |
| 60 | ``mtop.taobao.idle.logistics.merchant.oss.url.get`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/logistics.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:872867` | |
| 61 | ``mtop.taobao.idle.merchant.dispute.create.page`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/common.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:634240` | |
| 63 | ``mtop.taobao.idle.pc.item.publish.lbs.address`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4143292` | |
| 64 | ``mtop.taobao.idle.pc.item.publish.lbs.prov.city.address`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4143418` | |
| 71 | ``mtop.taobao.idle.trade.merchant.order.close.reason.get`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/trade_write.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:883384` | |
| 75 | ``mtop.taobao.idleitem.pc.backend.cpv.change.strategy`` | 未实现 | `backend/app` 全仓无该 mtop 名字面量（命中 0）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4143149` | |

### 3.4 不可实现（2 条）

| # | 接口名 | 状态 | 结论 + 证据 |
| --- | --- | --- | --- |
| 26 | ``mtop.idle.pc.backend.idleitem.editdetail`` | 不可实现 | **不可实现**：`logs/publish_family_probe.json` 真机 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（官方写入口，本账号无权限）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142714` | |
| 27 | ``mtop.idle.pc.backend.idleitem.preget`` | 不可实现 | **不可实现**：`logs/publish_family_probe.json` 真机 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`（官方写入口，本账号无权限）；建议归属「`backend/app/services/items.py`」；coverage57 记的 bundle 线索：`载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142594` | |
## 四、对 `scripts/coverage_audit.py` 的两处纠正

`scripts/coverage_audit.py:127-139` 的 `DENIAL_EVIDENCE` 给 5 条缺口挂了「平台拒绝」证据，逐条核后**只有 2 条成立**：

1. `mtop.idle.pc.backend.idleitem.edit` / `mtop.idle.pc.backend.idleitem.publish` —— **子串误命中**。
   该表 needle 是 `idleitem.edit` / `idleitem.publish`，而 `logs/publish_family_probe.json` 真正存在的记录只有 3 条：
   `mtop.idle.pc.idleitem.editDetail`（ok=True）、`mtop.idle.pc.backend.idleitem.editdetail`（无权限）、
   `mtop.idle.pc.backend.idleitem.preget`（无权限）。needle `idleitem.edit` 命中的是 `...idleitem.editdetail` 那条，
   于是这两条被**误判成有拒绝证据**；它们其实从未被真机探过 ⇒ 本报告判「未实现」。
2. `mtop.alibaba.idle.seller.pc.common.item.search` —— needle `common.item.search` 在 `logs/read_sweep.json` 里**零命中**。
   该文件里出现 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 的只有 4 条，都不是它：第 49 行 `mtop.alibaba.idle.item.pc.draft.search`、
   第 281 行 `mtop.alibaba.idle.seller.common.sub.account.info.list`、第 289 行 `mtop.alibaba.idle.seller.platform.user.role.list`、
   第 297 行 `mtop.alibaba.idle.seller.platform.merchant.delivery.address.list.query` ⇒ 也判「未实现」。

## 五、复跑

    cd C:/Users/Administrator/Projects/xianyu-ai-workbench
    XIANYU_APP_DIR=$TEMP/xywb_coverage63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_logistics_api.py
    XIANYU_APP_DIR=$TEMP/xywb_coverage63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_shop_api.py
    XIANYU_APP_DIR=$TEMP/xywb_coverage63 PYTHONPATH=. PYTHONIOENCODING=utf-8 /c/Python310/python.exe backend/tests/test_datacenter_api.py

第 61 批 5 条只读接口的 service 层端到端用例（本批新增，全部落在上面三个文件）：

- `backend/tests/test_logistics_api.py::LogisticsCoverage61Test`（7 例）—— `unconsign_detail` / `delivery_place_detail`
- `backend/tests/test_shop_api.py::ShopCoverage61Test`（5 例）—— `pc_service_status`
- `backend/tests/test_datacenter_api.py::Coverage61ReadTest`（4 例）—— `shop_stats` / `item_status_statistics`

## 六、真机只读探针（2026-10-16 06:27，账号 2217970177348）

口径：`XIANYU_APP_DIR=$TEMP/xywb_live63`（用 `sqlite3` 的 `Connection.backup()` 做**一致性拷贝**，
生产库一个字节都没动）；`sys.path.insert(0, 'backend')` 后 `from app.services import common`；
`common.get_client('2217970177348')` 返回**二元组** `(XianyuClient, None)`（第一个是客户端、第二个是错误）。
原始响应全文落盘：`logs/live63_probe.json`（128 行，UTF-8）。

| # | 调用 | 真机结果 | 说明 |
| --- | --- | --- | --- |
| 1 | `logistics.delivery_place_detail('2217970177348')` | `protocol_error` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问` | 接口**已实现**，但该账号真机**无权限** |
| 2 | `datacenter.item_status_statistics('2217970177348')` | `data.code='success'`，在卖 44 / 下架 1 | 真机成功，协议层与解析对得上 |
| 3 | `logistics.unconsign_detail(ACC, '3317095524016013282')` | `content/image_list/raw` 全 `None` + `no_evidence` warning | 交易关闭单，平台确实没有凭证 |
| 4 | `logistics.unconsign_detail(ACC, '3316918549013103465')` | `content=''`、`image_list` 1 条凭证图 | **真机证实「空串 ≠ 缺失」** |
| 5 | `datacenter.shop_stats(ACC, 'sess-probe-1')` | `data.code='SESSION_INVALID'` / `会话信息获取失败` | 需要**真实** `sessionId`，假 id 平台会回业务错 |
| 6 | `datacenter.shop_stats(ACC, '')` | `invalid_fields`（`session_id 不能为空`） | 服务层闸门，**零平台调用** |
| 7 | `shop.pc_service_status(ACC, 'YB')` | `protocol_error` / `FAIL_BIZ_COMMON_SYSTEM_ERROR::系统错误请稍候再试` | `'YB'` 不是有效 `serviceCode`；有效值**未取证** |

三条要点：

1. 第 4 条是本次新增用例 `test_unconsign_detail_empty_values_are_not_missing` 的**真机依据**：
   平台返回的 `content` 是空字符串而不是 `null`，而 `imageList` 有图 —— 所以「字段在、值为空」
   必须与「字段缺失」分开处理（服务层只在两者**都**为 `None` 时才发 `no_evidence`）。
2. `delivery_place_detail` 是「已实现但真机无权限」：代码路径完整（协议层常量 + 服务层 + 路由 + 用例），
   但账号 2217970177348 打不通 —— 计「已实现」，可用性受限要单列，别混进「不可实现」。
3. `shop_stats` 与 `pc_service_status` 都缺**入参形状**（真实 `sessionId` / 有效 `serviceCode`），
   当前只能验到「服务层闸门 + 平台错误透传」这一层；拿真值要等前端页面把参数带出来（本批不做）。
