# 覆盖率审计（第 57 轮修正）· mtop 覆盖面与真实缺口矩阵

> 本文件由 `scripts/coverage_audit.py --doc docs/audit/coverage57.md` 生成，数字与脚本同源。只读 14 个 bundle 副本 + `backend/app/**/*.py` + `logs/**`，零平台请求，不写 backend/frontend。

## 1. 三个总数（修正后口径自洽）

第 52 轮 `logs/cov52.txt` 头三行是 362 / 225 / 176 —— 其中 225 + 176 = 401 ≠ 362。本轮复现确认这是**口径混用**，不是量错。

| 指标 | 值 | 说明 |
| --- | ---: | --- |
| BUNDLE_MT_NAMES（Base） | 362 | 14/14 个 bundle 副本去重（先去尾点合并） |
| IMPLEMENTED_IN_BUNDLE | 187 | backend 侧 mtop 名 ∩ Base |
| MISSING | 175 | Base − Implemented |
| 恒等式 | 187 + 175 = 362 = 362 | 自洽（脚本内置 assert，不成立即 exit 2） |

backend 侧 mtop 名字总数 227，其中 40 个**不在 bundle 内**。旧脚本把这批「我们侧独有」的名字也算进分子（225），分母却是 bundle 集合（362）；401 − 362 = 39 正来自这里，逐个见 §5。

## 2. 缺口四分法统计（合计 175 = MISSING）

| 分类 | 数量 | 判据 |
| --- | ---: | --- |
| 可实现 | 77 | API 形状名；载荷能从 bundle 调用点读出，且同域接口已有实现先例 |
| 无权限或风控 | 43 | 平台白名单/资质/风控放行；有 FAIL_BIZ_IDLE_USER_UNAUTHORIZED 等日志硬证据 |
| 纯站内或 IM 内部名 | 26 | 依赖登录态或前端会话通道（IM 同步/表情/文件 token、站内支付、互动组件） |
| 误收的非 mtop 名 | 29 | mtop SDK 属性或库内部名被正则误收（mtop.request、mtop.config.secure …） |

分类顺序（脚本内 classify()）：先判误收（SDK 属性白名单 + 无第二级域名），再判无权限/风控，再判 IM 内部名，剩下归可实现。

## 3. 可实现（77）

bundle 都是压缩后的**单行**文件，故调用点给 `文件:行:列`，列即行内字符偏移，可在文件里按该偏移直接定位。
「已实现的同类先例」指我们**已经在用**的同域接口，说明载荷形态已知、鉴权链路已跑通，只需把这条补进对应 service；留空表示补做时需从 bundle 调用点现取载荷（调用点所在文件见最后一列）。

| 接口名 | 建议归属模块 | 证据（文件:行） | bundle 来源 |
| --- | --- | --- | --- |
| `mtop.alibaba.idle.ad.account.card.v2` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3268292` | ad-main.js |
| `mtop.alibaba.idle.ad.bp.coupon.info.query` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3246590` | ad-main.js |
| `mtop.alibaba.idle.ad.bp.coupon.statistic.query` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3246428` | ad-main.js |
| `mtop.alibaba.idle.pc.service.open` | `backend/app/services/shop.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141359` | item-main.js, js_main.js |
| `mtop.alibaba.idle.pc.service.status.query` | `backend/app/services/shop.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141260` | item-main.js, js_main.js |
| `mtop.alibaba.idle.seller.pc.common.item.search` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3071597` | item-main.js, js_main.js |
| `mtop.alibaba.idle.seller.pc.shop.stats.query` | `backend/app/services/datacenter.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1906144` | js_008_vendors.js |
| `mtop.alibaba.idle.seller.platform.idle.category.query` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/datacenter_main.js:1:2494726` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.item.operate.report.get` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3081363` | item-main.js, js_main.js |
| `mtop.alibaba.idle.seller.platform.item.publish.brands` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3073434` | item-main.js, js_main.js, main.js |
| `mtop.alibaba.idle.seller.platform.item.publish.categories` | `backend/app/services/items.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3832682` | main.js |
| `mtop.alibaba.idle.seller.platform.item.publish.models` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3073829` | item-main.js, js_main.js, main.js |
| `mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query` | `backend/app/services/logistics.py` | `同类已实现 mtop.alibaba.idle.seller.platform.merchant.delivery.address.add @ backend/app/api/trade_write.py:228` | main.js |
| `mtop.alibaba.idle.seller.platform.order.backfill.report` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:2608322` | main.js |
| `mtop.alibaba.idle.seller.platform.search.box` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3076161` | item-main.js, js_main.js |
| `mtop.alibaba.idle.service.open` | `backend/app/services/shop.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1805203` | item-main.js, js_main.js |
| `mtop.alibaba.idle.service.status.query` | `backend/app/services/shop.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1804926` | item-main.js, js_main.js |
| `mtop.cainiao.geography.frontend.divisionsinfo.get` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3387045` | main.js |
| `mtop.cainiao.ld.detail.tradeid.ordercode.mailno.rescode.get.xy` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:955017` | js_008_vendors.js, main.js |
| `mtop.idle.idleitem.draft.edit` | `backend/app/services/items.py` | `同类已实现 mtop.alibaba.idle.item.pc.draft.update @ backend/app/xianyu/draft.py:44` | item-main.js, js_main.js |
| `mtop.idle.idleitem.draft.publish` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142356` | item-main.js, js_main.js |
| `mtop.idle.idleitem.security.check` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142217` | item-main.js, js_main.js |
| `mtop.idle.item.first.publish.service.cards.list` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2989577` | item-main.js, js_main.js |
| `mtop.idle.kgraph.sku.name.recommend` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2715095` | item-main.js, js_main.js |
| `mtop.idle.pc.backend.idleitem.edit` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142809` | item-main.js, js_main.js |
| `mtop.idle.pc.backend.idleitem.editdetail` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142714` | item-main.js, js_main.js |
| `mtop.idle.pc.backend.idleitem.preget` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142594` | item-main.js, js_main.js |
| `mtop.idle.pc.backend.idleitem.publish` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141789` | item-main.js, js_main.js |
| `mtop.idle.pc.backend.publish.service.cards.list` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141016` | item-main.js, js_main.js |
| `mtop.idle.pc.first.hand.idleitem.edit` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1806275` | item-main.js, js_main.js |
| `mtop.idle.pc.first.hand.idleitem.edit.detail` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3062866` | item-main.js, js_main.js |
| `mtop.idle.pc.first.hand.idleitem.publish` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1805706` | item-main.js, js_main.js |
| `mtop.idle.pc.idleitem.preget` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3062139` | item-main.js, js_main.js |
| `mtop.taobao.idle.ad.account.info` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301473` | ad-main.js |
| `mtop.taobao.idle.ad.activity.detail.rendering` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3268518` | ad-main.js |
| `mtop.taobao.idle.ad.bp.data.download` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3115691` | ad-main.js |
| `mtop.taobao.idle.ad.campaign.log.query` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:1665384` | ad-main.js |
| `mtop.taobao.idle.ad.customize.bid.estimates` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301315` | ad-main.js |
| `mtop.taobao.idle.ad.group.data.query` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3253874` | ad-main.js |
| `mtop.taobao.idle.ad.group.list` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3253585` | ad-main.js |
| `mtop.taobao.idle.ad.init.user.site.info` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3617477` | ad-main.js |
| `mtop.taobao.idle.ad.item.recommend` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3300908` | ad-main.js |
| `mtop.taobao.idle.ad.item.searchV2` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301167` | ad-main.js |
| `mtop.taobao.idle.ad.message.detail` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3678352` | ad-main.js |
| `mtop.taobao.idle.ad.message.list` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3678207` | ad-main.js |
| `mtop.taobao.idle.ad.message.read` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3678499` | ad-main.js |
| `mtop.taobao.idle.ad.message.unread.info` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:703173` | ad-main.js |
| `mtop.taobao.idle.ad.new.item.bid.recommend` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3255163` | ad-main.js |
| `mtop.taobao.idle.ad.package.info` | `backend/app/services/ad_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/adapp/ad-main.js:1:3301020` | ad-main.js |
| `mtop.taobao.idle.deposit` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2978137` | item-main.js, js_main.js |
| `mtop.taobao.idle.item.pc.category.list` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1804740` | item-main.js, js_main.js |
| `mtop.taobao.idle.item.priceadvice` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:1806594` | item-main.js, js_main.js |
| `mtop.taobao.idle.kgraph.pc.property.recommend` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141448` | item-main.js, js_main.js |
| `mtop.taobao.idle.kgraph.property.search` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:2289736` | item-main.js, js_main.js |
| `mtop.taobao.idle.kgraph.text.diagnosis` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4142034` | item-main.js, js_main.js |
| `mtop.taobao.idle.local.poi.get` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4140864` | item-main.js, js_main.js |
| `mtop.taobao.idle.logistics.merchant.consign.page.render` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:844126` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.logistics.merchant.excel.consign.offline` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:872982` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.logistics.merchant.oss.sts.get` | `backend/app/services/logistics.py` | `同类已实现 mtop.taobao.idle.logistics.merchant.consign.offline @ backend/app/api/trade_write.py:40` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.logistics.merchant.oss.url.get` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:872867` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.merchant.dispute.create.page` | `backend/app/services/common.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:634240` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.merchant.unconsign.detail` | `backend/app/services/logistics.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/trade_bundle/main.js:1:3138431` | main.js |
| `mtop.taobao.idle.pc.item.publish.lbs.address` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4143292` | item-main.js, js_main.js |
| `mtop.taobao.idle.pc.item.publish.lbs.prov.city.address` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4143418` | item-main.js, js_main.js |
| `mtop.taobao.idle.seller.platform.item.pc.category.property.query` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3074576` | item-main.js, js_main.js |
| `mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query` | `backend/app/services/common.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3075363` | item-main.js, js_main.js |
| `mtop.taobao.idle.seller.platform.item.pc.search.categories` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3073053` | item-main.js, js_main.js |
| `mtop.taobao.idle.seller.platform.pc.item.status.statistics` | `backend/app/services/common.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:3075766` | item-main.js, js_main.js |
| `mtop.taobao.idle.trade.close.by.seller` | `backend/app/services/trade_write.py` | `同类已实现 mtop.taobao.idle.trade.merchant.close.by.seller @ backend/app/services/trade_write.py:734` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idle.trade.common.sku.selector` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1841743` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idle.trade.merchant.order.close.reason.get` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:883384` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.trade.order.cancel` | `backend/app/services/trade_write.py` | `同类已实现 mtop.taobao.idle.trade.merchant.close.by.seller @ backend/app/services/trade_write.py:734` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idle.trade.seller.delay.confirm` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1832390` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idle.trade.user.adjust.price` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:1831797` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idleitem.pc.backend.cpv.change.strategy` | `backend/app/services/items.py` | `载荷见 bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4143149` | item-main.js, js_main.js |
| `mtop.taobao.merchant.heracles.inno.smart.form.callback` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:802665` | js_008_vendors.js, main.js |
| `mtop.taobao.merchant.heracles.inno.smart.form.query` | `backend/app/services/trade_write.py` | `载荷见 bundle 调用点 ~/.dsh-tmp/seller_scan/js_008_vendors.js:1:798939` | js_008_vendors.js, main.js |

## 4. 无权限或风控（43）

「拒绝证据」为实测日志锚点；未逐条实测的落到通用锚点 `logs/read_sweep.json:49`（该行即 FAIL_BIZ_IDLE_USER_UNAUTHORIZED 的实例）。另有两类结构性证据：`datacompass.*`（数据参谋）整族只在店铺开通该模块后可见，本账号店铺首页无该入口；`...platform.account.mock.apply/cancel`（体验账号）需平台侧子账号资质。

| 接口名 | 拒绝证据（日志:行） | bundle 来源 |
| --- | --- | --- |
| `mtop.alibaba.idle.merchant.autotrade.data.update` | `logs/read_sweep.json:49` | js_008_vendors.js, main.js |
| `mtop.alibaba.idle.merchant.autotrade.trade.api` | `logs/read_sweep.json:49` | js_008_vendors.js, main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.buyerprofile.buyer.summary` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.buyerprofile.first.buyer.summary` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.cs.detail.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.cs.diversion.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.cs.evaluation.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.cs.excel.url` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.cs.overview.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.excel.url.get` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.item.list.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.marketing.data.summary` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.merchant.info.query` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.datacompass.seller.activity.list` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.yanxuan.datacompass.cs.detail.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.yanxuan.datacompass.cs.detail.list` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.yanxuan.datacompass.cs.excel.url` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.yanxuan.datacompass.cs.overview.export` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.pc.yanxuan.datacompass.cs.overview.summary` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.account.mock.apply` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.account.mock.cancel` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.cpv.category` | `logs/read_sweep.json:49` | item-main.js, js_main.js |
| `mtop.alibaba.idle.seller.platform.datacompass.export.item.list` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.datacompass.refund.item.list` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.datacompass.refund.item.reason.detail` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.datacompass.refund.reason.detail` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.datacompass.refund.reason.item.list` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.datacompass.refund.summary` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.alibaba.idle.seller.platform.item.export` | `logs/read_sweep.json:49` | item-main.js, js_main.js |
| `mtop.alibaba.idle.seller.platform.merchant.delivery.address.detail.query` | `logs/read_sweep.json:49` | main.js |
| `mtop.alibaba.idle.seller.platform.query.login.merchant.info` | `logs/read_sweep.json:49` | ad-main.js, datacenter_main.js, sub_account_main.js |
| `mtop.alibaba.idle.seller.platform.user.business.identity.get` | `logs/read_sweep.json:49` | item-main.js, js_main.js |
| `mtop.alibaba.idle.seller.platform.usergroup.member.list` | `logs/read_sweep.json:49` | datacenter_main.js |
| `mtop.idle.pc.security.score.record.list` | `logs/read_sweep.json:49` | sc-main.js |
| `mtop.idle.pc.security.violation.detail` | `logs/read_sweep.json:49` | sc-main.js |
| `mtop.taobao.idle.ad.qualification.submittedRecord.detail.query` | `logs/read_sweep.json:49` | ad-main.js |
| `mtop.taobao.idle.cco.shop.complain.detail` | `logs/read_sweep.json:49` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.cco.shop.complain.return.money.page` | `logs/read_sweep.json:49` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.merchant.biz.jfzz.transfer` | `logs/read_sweep.json:49` | js_008_vendors.js, main.js |
| `mtop.taobao.idle.merchant.refund.service.record` | `logs/read_sweep.json:49` | main.js |
| `mtop.taobao.idle.merchant.refund.sign.query` | `logs/read_sweep.json:49` | ad-main.js, sub_account_main.js |
| `mtop.taobao.idle.mtee.risk.get` | `logs/read_sweep.json:49` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idle.refund.subinfo.query` | `logs/read_sweep.json:49` | ad-main.js, sub_account_main.js |

## 4b. 纯站内或 IM 内部名（26）

集中出现在 IM 页面 bundle（`js_006_p_im_index.js` / `js_008_vendors.js`）与站内导航脚本。其调用依赖**登录态 + 前端会话通道**（如 `mtop.taobao.idlemessage.pc.message.sync` 属 ImSDK 长连接同步，不是商家后台 HTTP 接口）；本工作台走 WebSocket 直连，不需要这些名字。

| 接口名 | bundle 来源 |
| --- | --- |
| `mtop.idle.trade.message.chat.tradeinfo` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.idle.trade.pc.message.headinfo` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.idle.trade.pc.message.headinfo.query` | js_008_vendors.js |
| `mtop.idle.web.user.page.nav` | ad-main.js, js_008_vendors.js |
| `mtop.idle.web.user.panel.customer` | js_008_vendors.js |
| `mtop.order.doPay` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.order.doop` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.order.dopay` | js_008_vendors.js, main.js |
| `mtop.taobao.heracles.inno.interactive.voice.change` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.customers.data.query` | datacenter_main.js |
| `mtop.taobao.idlemessage.face.emoji.load` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.file.token.v1` | js_008_vendors.js |
| `mtop.taobao.idlemessage.instant.log` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.accs.token` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.file.entry.auth` | js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.loginuser.get` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.message.sync` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.session.sync` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.tool.remark` | js_008_vendors.js |
| `mtop.taobao.idlemessage.pc.user.query` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.relation.message.read` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.tool.item.query` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.user.query` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.idlemessage.wx.login.token` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.wx.idlemessage.message.sync` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.taobao.wx.idlemessage.session.sync` | js_006_p_im_index.js, js_008_vendors.js |

## 4c. 误收的非 mtop 名（29）

mtop SDK 对象上的属性访问被 `mtop\.[A-Za-z0-9_.]+` 正则误收：`mtop.request` / `mtop.params` / `mtop.js` / `mtop.config.secure` / `mtop.middlewares.push` 等。这类不是接口，不计入任何缺口。

| 接口名 | bundle 来源 |
| --- | --- |
| `mtop.CLASS` | main.js, sub_account_main.js |
| `mtop.ERROR` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.H5Request` | main.js, sub_account_main.js |
| `mtop.RESPONSE_TYPE` | main.js, sub_account_main.js |
| `mtop.antiCreepRequest` | main.js, sub_account_main.js |
| `mtop.antiFloodRequest` | main.js, sub_account_main.js |
| `mtop.apiName` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.config` | item-main.js, js_008_vendors.js, js_main.js, main.js, sc-main.js, sub_account_main.js |
| `mtop.config.H5Request` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.config.LibMtopRequest` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.config.mainDomain` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.config.sameSite` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.config.secure` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.config.subDomain` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.confirmCancelText` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.confirmConfirmText` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.confirmContent` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.confirmTitle` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.errorListener` | main.js, sub_account_main.js |
| `mtop.extParams` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.js` | item-main.js, js_008_vendors.js, js_main.js, main.js, sc-main.js |
| `mtop.loginRequest` | main.js, sub_account_main.js |
| `mtop.middlewares` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.middlewares.push` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.needConfirm` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.needLogin` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.params` | js_006_p_im_index.js, js_008_vendors.js |
| `mtop.request` | ad-main.js, cs_dispatch_main.js, datacenter_main.js, item-main.js, js_006_p_im_index.js, js_008_vendors.js, js_main.js, main.js, notification_main.js, sc-main.js, sub_account_main.js |
| `mtop.version` | js_006_p_im_index.js, js_008_vendors.js |

## 5. backend 侧有、bundle 内没有的名字（40）

不参与覆盖率分子；旧口径把它们算进 225，是恒等式不成立的直接原因。

```text
mtop.DEFAULT_SPM_CNT
mtop.UPLOAD_APPKEY
mtop.UPLOAD_URL
mtop.alibaba.idle.item.pc.draft.
mtop.alibaba.idle.seller.pc.item.
mtop.alibaba.idle.seller.platform.pro.freight.template.
mtop.api_url
mtop.call_params
mtop.encode_data
mtop.gaia...index.get
mtop.generate_device_id
mtop.generate_mid
mtop.generate_uuid
mtop.idle.finance.
mtop.idle.finance.invoice.bill.query
mtop.idle.finance.invoice.metadata.get
mtop.idle.finance.invoice.purchaser.info.get
mtop.idle.finance.invoice.request.list.query
mtop.idle.finance.metadata.query
mtop.idle.finance.monthbill.expense.detail.query
mtop.idle.finance.monthbill.expense.summary.query
mtop.idle.finance.monthbill.income.detail.query
mtop.idle.finance.monthbill.income.summary.query
mtop.idle.item.publish.service.cards.list
mtop.idle.pc.idleitem.edit
mtop.idle.pc.idleitem.editDetail
mtop.idle.pc.idleitem.publish
mtop.idle.trade.order.detail.
mtop.idle.web.user.page.head
mtop.idle.web.xyh.item.list
mtop.is_token_error
mtop.merge_token_cookies
mtop.parse_ret
mtop.taobao.idle.item.downshelf
mtop.taobao.idle.item.price.competitiveness.info.query
mtop.taobao.idle.pc.detail
mtop.taobao.idlemessage.
mtop.taobao.idlemessage.pc.notify.sessions.list
mtop.taobao.idlemessage.pc.notifys.read
mtop.token_from_cookies
```

## 6. 复跑

```bash
/c/Python310/python.exe scripts/coverage_audit.py                      # 摘要 + 四分法
/c/Python310/python.exe scripts/coverage_audit.py --list               # 逐条缺口
/c/Python310/python.exe scripts/coverage_audit.py --json               # 机器可读
/c/Python310/python.exe scripts/coverage_audit.py --doc docs/audit/coverage57.md
```

