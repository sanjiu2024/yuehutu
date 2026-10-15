"""官方「推广（投放）」页面缺的 mtop 接口（协议层）。

为什么单开一个模块、而不是往 `client.py` 里加方法：

* `client.py` 已覆盖推广首页 / 账户卡片 / 会员指标 / 计划搜索 / 计划与商品报表 /
  结算 / 资金流水（`ad_home` / `ad_account_card` / `ad_member_data` / `ad_campaigns` /
  `ad_campaign_metrics` / `ad_item_metrics` / `ad_settle` / `ad_account_log` /
  `ad_log_operates` / `ad_recharge_detail` / `ad_withdraw_detail`），
  但它们**盖不住推广页面的投放包 / 人群包 / 计划日志 / 消息中心**这几块；
* `client.py` 在本任务里是冻结文件（task-14 写范围外），所以这里用「模块级 async 函数 +
  调用方传入 client」的形态，服务层通过 `common.protocol_module('ad')` 懒导入。

接口名与入参来自官方超级擦亮微应用 bundle
`https://g.alicdn.com/idle-pc/fish-ad-pc/0.0.12/js/main.js`（2026-10-11 拉取，3,760,912 字节，
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\adapp\\ad-main.js`，共 86 个 mtop 名）。
每个常量下面都写了 bundle 里的**字符偏移**与可复现的搜索串。

调用形态（bundle 里的 `yp({api, ...})` 包装）：

* 绝大多数：`{api, v:'1.0', data:{requestJson: JSON.stringify(<业务对象>)}}`
  —— 对应本仓库 `client.mtop(api, {'requestJson': request}, v='1.0', spm_cnt=AD_SPM)`；
* `message.unread.info` 是例外：官方 `data` 为空、**不套 requestJson**。

**真机证据（2026-10-16 更新）：4 条投放域只读接口已真机打通**（账号 2217970177348，
in-process 探针，只读、串行、间隔 >= 2.2s），逐条的 bundle 偏移、官方 payload 原文、发出的
requestJson 与平台判读写在 `docs/contracts/ad.md`。结论：`item.searchV2` 缺 `tab`/`needTabs`、
`package.info` / `customize.bid.estimates` 缺 `entityType`/`entityList`（且 `catId` 必须是
真实类目 ID，给 0 会得到 `E_AD_BP_COMMON_SYSTEM_EXCEPTION`）、`campaign.log.query` 缺
`operatorType`/`operationType`（官方初值 0）。因此：

* 官方恒定带的键由 `BODY_DEFAULTS` 补默认值、`BODY_REQUIRED` 拦残包（见下）；

* 平台没返回的字段一律 `None`，**不补 0**；
* 响应原样透出 `data`，不在这里做「指标认字段」的猜测（与 `datacenter.py` 不同：那边是
  真机确认了无权限、只能认字段；这边连响应形状都还没在真机上看过，多猜一层只会更难排查）。

写类只有 `message.read`（标记推广消息已读），与 `ad_write.py` 同规矩：必须显式
`confirm=True`，漏了就抛 `WriteNotConfirmed`，一个字节都不发。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: 投放账户信息（推广页/计划创建页读「这个账号能不能投、按什么模式投」）
ACCOUNT_INFO_API = 'mtop.taobao.idle.ad.account.info'
#: 投放包信息（`initPlanInfo` 用它拿 packageTargetVOList / bidTargets）
PACKAGE_INFO_API = 'mtop.taobao.idle.ad.package.info'
#: 商品搜索 V2（选品页；比 `item.data.batch.query` 多了 tab / 排序 / 搜索词）
ITEM_SEARCH_API = 'mtop.taobao.idle.ad.item.searchV2'
#: 自定义出价估算（出价页；bundle 里三处调用点共用）
CUSTOMIZE_BID_ESTIMATES_API = 'mtop.taobao.idle.ad.customize.bid.estimates'
#: 投放单元（人群包 / 计划分组）列表
GROUP_LIST_API = 'mtop.taobao.idle.ad.group.list'
#: 投放单元数据（单个人群包的分日数据 + 上一周期对比）
GROUP_DATA_API = 'mtop.taobao.idle.ad.group.data.query'
#: 计划操作日志（计划详情页的「操作记录」）
CAMPAIGN_LOG_API = 'mtop.taobao.idle.ad.campaign.log.query'
#: 推广消息中心：列表 / 详情 / 标记已读 / 未读数
MESSAGE_LIST_API = 'mtop.taobao.idle.ad.message.list'
MESSAGE_DETAIL_API = 'mtop.taobao.idle.ad.message.detail'
MESSAGE_READ_API = 'mtop.taobao.idle.ad.message.read'
MESSAGE_UNREAD_API = 'mtop.taobao.idle.ad.message.unread.info'

#: 活动奖励页的券列表（bundle @3246590 `sWe(e)`；调用点 @3265220）。
BP_COUPON_INFO_API = 'mtop.alibaba.idle.ad.bp.coupon.info.query'
#: 活动奖励页的券统计（bundle @3246428 `aWe()`；调用点 @3255626）。
BP_COUPON_STATISTIC_API = 'mtop.alibaba.idle.ad.bp.coupon.statistic.query'
#: 投放账户卡片 V2（bundle @3268292 `UWe(e)`，官方导出名 requestAccountCard，是活动奖励页
#: 路由 'home/service' 的 loader export @505082 / @3756651）。**与 `client.py` 里已有的
#: `AD_ACCOUNT_CARD_API='mtop.taobao.idle.ad.bp.account.card.query'` 不是同一条接口**
#: （`alibaba.idle` vs `taobao.idle`），不要互相顶替。
ACCOUNT_CARD_V2_API = 'mtop.alibaba.idle.ad.account.card.v2'
#: 活动奖励详情渲染（bundle @3268518 `YWe()`；调用点 @3281232 / @3723161）——
#: 官方 `data` 为空、**不套 requestJson**（与 `message.unread.info` 同形），无参。
ACTIVITY_DETAIL_RENDERING_API = 'mtop.taobao.idle.ad.activity.detail.rendering'

#: 投放数据报表下载（bundle @3115691 `XFe(e)`；调用点 @3118414 / @3174876 / @3236322
#: 都在「下载报表」按钮里）—— **偏写，只登记不接**：
#: 载荷 `{targetType:'CAMPAIGN'|'MEMBER'|'ITEM', startDate, endDate, includeEndDate:true,
#: groupByDay, taskId?}`，响应 `data.downloadUrl` / `data.taskId` / `data.status`
#: （FAILED 报错；没有 downloadUrl 时官方拿 taskId 每 60s 轮询）。
#: 即：它在服务端**创建异步导出任务**（有副作用 / 落库），不是「读一个现成文件」，
#: 所以本仓库不接 —— 理由写在 `docs/contracts/ad.md`。
BP_DATA_DOWNLOAD_API = 'mtop.taobao.idle.ad.bp.data.download'

#: 读取接口目录：`kind` -> 接口名 + 入参说明。
#: `request_json=False` 的两条（未读数 / 活动详情渲染）官方 `data` 是空对象。
READ_APIS: Dict[str, Dict[str, Any]] = {
    'account_info': {'api': ACCOUNT_INFO_API, 'request_json': True},
    'package_info': {'api': PACKAGE_INFO_API, 'request_json': True},
    'item_search': {'api': ITEM_SEARCH_API, 'request_json': True},
    'customize_bid_estimates': {'api': CUSTOMIZE_BID_ESTIMATES_API, 'request_json': True},
    'group_list': {'api': GROUP_LIST_API, 'request_json': True},
    'group_data': {'api': GROUP_DATA_API, 'request_json': True},
    'campaign_log': {'api': CAMPAIGN_LOG_API, 'request_json': True},
    'message_list': {'api': MESSAGE_LIST_API, 'request_json': True},
    'message_detail': {'api': MESSAGE_DETAIL_API, 'request_json': True},
    'message_unread': {'api': MESSAGE_UNREAD_API, 'request_json': False},
    'coupon_info': {'api': BP_COUPON_INFO_API, 'request_json': True},
    'coupon_statistic': {'api': BP_COUPON_STATISTIC_API, 'request_json': True},
    'account_card_v2': {'api': ACCOUNT_CARD_V2_API, 'request_json': True},
    'activity_detail': {'api': ACTIVITY_DETAIL_RENDERING_API, 'request_json': False},
}

#: `campaign.log.query` 的枚举（bundle @1784368 / @1784654：0=全部）
OPERATOR_TYPES = {0: '全部', 1: '用户', 2: '系统'}
OPERATION_TYPES = {0: '全部', 1: '添加', 2: '更新'}

#: 活动奖励页券列表的枚举（bundle 里的 TS enum，**只做登记、不在协议层校验**：
#: 平台对未知枚举值的反应还没在真机上看过，本地拦下来只会把平台的话盖掉）。
#: @3243192 `EActivityRewardType`（券类型）：
COUPON_TYPES = {0: '全部', 2: '礼金', 3: '红包', 4: '新人券', 5: '代理商礼金'}
#: @3243498 `ESourceType`（来源分组；@3244819 `ARGS` 把它映射成 ARGS 文案
#: effective / pending / expired）：
COUPON_SOURCES = {'BONUS': '生效中', 'COUPON': '未生效', 'HISTORY': '已失效'}
#: @3244737 `ECouponStatus`（单张券的状态）：
COUPON_STATUS = {1: '未生效', 2: '生效中', 3: '已用完', 4: '已作废',
                 5: '未生效已过期', 6: '生效中已过期', 7: '冻结'}

#: 官方调用点**恒定带**的业务键与首次加载时的默认值（逐条偏移见各函数 docstring）。
#: 只填官方自己填的默认值，**不替调用方猜业务参数**；调用方给了值就以调用方为准。
BODY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'package_info': {'isGoodsSelectedTabChange': True},
    'item_search': {'tab': 'platReco', 'needTabs': True},
    'customize_bid_estimates': {'pcFirstRequest': True},
    # 官方 @1794033 的初值 s={operatorType:0, operationType:0} 是**恒定带**的：
    # 真机 2026-10-16 A/B —— 不带这两键回 E_AD_BP_COMMON_SYSTEM_EXCEPTION，
    # 带 0 就正常返回 data.result（见 docs/contracts/ad.md）。
    'campaign_log': {'operatorType': 0, 'operationType': 0},
    # 官方 @3265220 的初值 FWe={pageNum:1,pageSize:10,couponType:WVe.ALL,source:GVe.ACTIVE}
    # 是**页面筛选状态**（改 tab / 翻页都在这个对象上改），首次加载恒定带。
    # 真机 2026-10-16 A/B 见 docs/contracts/ad.md：空对象与这份初值都能回 200，
    # 差别在返回的 result 是全部券还是「生效中」那一组 —— 所以按官方初值补默认值。
    'coupon_info': {'pageNum': 1, 'pageSize': 10, 'couponType': 0, 'source': 'BONUS'},
}

#: 少一条就**不发**：平台对残包只回 `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`（502），
#: 本地拦下来翻成 400 `invalid_fields` 才说得清是谁的问题。
#: `item_search` 另外要求 `itemId` / `searchKey` 至少一个（见 `_prepare`）。
BODY_REQUIRED: Dict[str, tuple] = {
    'package_info': ('entityType', 'entityList'),
    'item_search': ('tab',),
    'customize_bid_estimates': ('entityType', 'entityList'),
    'campaign_log': ('startDate', 'endDate'),
}


class MissingRequiredParams(ValueError):
    """业务参数没给全。协议层最后的闸门之一：宁可不发，也不把残包打上平台。"""


def _prepare(kind: str, body: Any) -> Dict[str, Any]:
    """官方默认值 -> 必填校验 -> 真正发出去的业务对象。"""
    merged: Dict[str, Any] = dict(BODY_DEFAULTS.get(kind, {}))
    merged.update(_body(body))
    missing = [key for key in BODY_REQUIRED.get(kind, ()) if not merged.get(key)]
    if kind == 'item_search' and not (merged.get('itemId') or merged.get('searchKey')):
        missing.append('itemId 或 searchKey')
    if missing:
        raise MissingRequiredParams(
            f'{kind} 缺少官方必填参数：{"、".join(missing)}'
            '（官方调用点恒定带，缺了平台回 E_AD_BP_COMMON_PARAM_NULL）')
    return merged


class WriteNotConfirmed(RuntimeError):
    """写接口没有拿到显式确认。协议层最后的闸门：宁可不发，也不许误发。"""


def _require_confirm(confirm: Any) -> None:
    if confirm is not True:
        raise WriteNotConfirmed('写接口必须显式 confirm=True 才执行')


def _body(body: Any) -> Dict[str, Any]:
    """业务参数统一成 dict：非 dict（None / 列表 / 标量）一律当空对象。"""
    return dict(body) if isinstance(body, dict) else {}


def _json_request(body: Any) -> str:
    """`idle.ad.*` 统一形状里的 requestJson 字符串。"""
    return json.dumps(_body(body), separators=(',', ':'), ensure_ascii=False)


def _payload(result: Any) -> Optional[Dict[str, Any]]:
    payload = result.get('data') if isinstance(result, dict) else None
    return payload if isinstance(payload, dict) else None


async def _mtop(client: Any, api: str, data: Any) -> Any:
    """发一次 mtop 并剥掉外层信封，返回平台响应里的 `data`。

    `client.mtop` 失败会抛 `XianyuError`（含 `FAIL_*` 原文），这里**不吞**：
    服务层要靠原文区分「无权限 / 未登录 / 风控」。

    `v` 一律走 `client.mtop` 的默认值 `'1.0'`。bundle 里 `group.list` @3253585 与
    `campaign.log.query` @1665384 的包装**没有写 v**，但本仓库 `mtop.call_params`
    （backend/app/xianyu/mtop.py:98）把 `v` 当固定键、`client.mtop` 也没有「不带 v」
    的表达方式 —— 平台对这两个接口的默认版本同样是 1.0，所以形状一致，不另造参数。
    """
    from .client import AD_SPM  # 延迟导入：测试会替换 xianyu.client 模块
    result = await client.mtop(api, data, v='1.0', spm_cnt=AD_SPM)
    if isinstance(result, dict):
        return result.get('data')
    return result


async def account_info(client: Any, body: Any = None) -> Any:
    """投放账户信息。bundle @3301473 `rqe(e)`；调用点 @3488564 / @3672101 传
    `{orderPrice, promotionMode, bidType, scene}`（`scene` 取 `EAccountRequestScene`，
    如 `CREATION_REAL_TIME` / `CREATION_SUCCESS`）。"""
    return await _mtop(client, ACCOUNT_INFO_API, {'requestJson': _json_request(_prepare('account_info', body))})


async def package_info(client: Any, body: Any = None) -> Any:
    """投放包信息。bundle @3301020 `eqe(e)`；官方调用点 @3677643 / @3396844 传
    `{entityList, entityType, isGoodsSelectedTabChange}`：

    * `entityType` 是**数字**（@1430758 枚举 `GOOD=1` / `CLUE=2`，页面默认 GOOD）；
    * `entityList` 每项 = `{entityId:item.itemId, catId:item.catId, price:item.price,
      areaTarget:false, startTime:item.publishTime}`（@3677643 的 `a.map(...)`）；
    * `isGoodsSelectedTabChange` 首次加载为 `true`（@3396844 `!0`）。

    响应 `data.packageTargetVOList` / `data.bidTargets` / `data.recPromotionMode` /
    `data.supportOrder`。真机 2026-10-16 三步证据（见 docs/contracts/ad.md）：
    只发 `{promotionMode}` → `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`；
    补齐两个必填键但 `catId=0` → `E_AD_BP_COMMON_SYSTEM_EXCEPTION`；
    `catId` 给真实类目 ID → 200 + `packageTargetVOList`。
    **`catId` 必须是真实类目 ID**（本地 `items.category_id`），这里不替调用方编。
    """
    return await _mtop(client, PACKAGE_INFO_API,
                       {'requestJson': _json_request(_prepare('package_info', body))})


async def item_search(client: Any, body: Any = None) -> Any:
    """商品搜索 V2。bundle @3301167 `tqe(e)`；状态初值 @3406850
    `KYe={pageNum:1,pageSize:10,tab:'platReco',queryVal:''}`；调用点 @3408019 组
    `{pageNum, pageSize, tab, needTabs:!(tabList.length>0), itemId|searchKey,
    campaignId, sortField?, sortType?}` —— `tab` 与 `needTabs` 官方**恒定带**，
    默认值 `tab='platReco'` / `needTabs=true`（首次加载）。

    真机 2026-10-16：不发 `tab` 时平台回 `E_AD_BP_COMMON_PARAM_NULL::必填参数为空`。
    响应 `data.result.{tabList,itemList}` + `data.total`。
    """
    return await _mtop(client, ITEM_SEARCH_API,
                       {'requestJson': _json_request(_prepare('item_search', body))})


async def customize_bid_estimates(client: Any, body: Any = None) -> Any:
    """自定义出价估算。bundle @3301315 `nqe(e)`；三个调用点 @3315969 / @3314544 /
    @3313962 传 `{entityType, entityList, pcFirstRequest, bidTarget?, bidPrice?,
    noBid?, campaignId?}`：

    * `entityList` 由 `getEntityList()`（@3305143）拼 `{entityId:item.itemId,
      catId:item.catId, price:item.price, areaTarget, startTime:item.publishTime}`；
    * `pcFirstRequest`：按 entityList / bidMethod 调用是 `true`，按 bidPrice 调用是 `false`
      （默认给 `true`，出价那条走 `payload` 覆盖）。

    响应 `data.customizeBidRecommendVOList` / `data.bidTargets`。真机 2026-10-16：
    与 `package.info` 同形 —— `catId=0` 回 `E_AD_BP_COMMON_SYSTEM_EXCEPTION`，
    真实类目 ID 回 200（见 docs/contracts/ad.md）。
    """
    return await _mtop(client, CUSTOMIZE_BID_ESTIMATES_API,
                       {'requestJson': _json_request(_prepare('customize_bid_estimates', body))})


async def group_list(client: Any, body: Any = None) -> Any:
    """投放单元列表。bundle @3253585 `gWe(e)` —— 官方包装不带 v（见 `_mtop` 的口径）；
    调用点 @3454098 传 `{pageNum,pageSize,startDate,endDate,campaignId,includeEndDate:true,groupStatus?}`，
    @3493409 追加 `excludeGroupStatus / campaignTitleLike / promotionMode / bidTargets`；
    响应 `data.result`。"""
    return await _mtop(client, GROUP_LIST_API, {'requestJson': _json_request(_prepare('group_list', body))})


async def group_data(client: Any, body: Any = None) -> Any:
    """投放单元数据。bundle @3253874 `yWe(e)`；调用点 @3449844 传
    `{startDate,endDate,includeEndDate:true,adGroupId,allPeriod:false}`；
    响应含 `data` 与 `previousData`（官方在「昨天→今天」时拍成
    `dataDetailVOList` / `previousDataDetailVOList`）。"""
    return await _mtop(client, GROUP_DATA_API, {'requestJson': _json_request(_prepare('group_data', body))})


async def campaign_log(client: Any, body: Any = None) -> Any:
    """计划操作日志。bundle @1665384 `IW(e)` —— 官方包装不带 v（见 `_mtop` 的口径）；调用点 @1794033 传
    `{startDate,endDate,pageNum,pageSize,campaignId:Number(r)||0,operatorType,operationType}`：
    日期初值是**近 7 天** `YYYYMMDD`，枚举初值 `{operatorType:0, operationType:0}`
    （@1784368 / @1784654：`operatorType` 0=全部/1=用户/2=系统，`operationType` 0=全部/1=添加/2=更新）。

    本地要求日期区间非空（空串不是合法日期）。真机 2026-10-16 A/B：**不带**这两个枚举键时
    平台回 `E_AD_BP_COMMON_SYSTEM_EXCEPTION::闲鱼太累了，请稍后再试～`，带 0 正常返回
    `data.result`（见 docs/contracts/ad.md）—— 所以 `BODY_DEFAULTS` 补 0。
    响应 `data.result` + `data.total`。
    """
    return await _mtop(client, CAMPAIGN_LOG_API,
                       {'requestJson': _json_request(_prepare('campaign_log', body))})


async def message_list(client: Any, body: Any = None) -> Any:
    """推广消息列表。bundle @3678207 `XQe(e)`；调用点 @3698592 传 `{pageNum,pageSize}`，
    响应 `data.total` + 列表。"""
    return await _mtop(client, MESSAGE_LIST_API, {'requestJson': _json_request(_prepare('message_list', body))})


async def message_detail(client: Any, body: Any = None) -> Any:
    """推广消息详情。bundle @3678352 `KQe(e)`；调用点 @3696148 传 `{messageId}`，
    响应 `data.title` / `data.content`。"""
    return await _mtop(client, MESSAGE_DETAIL_API, {'requestJson': _json_request(_prepare('message_detail', body))})


async def message_unread(client: Any) -> Any:
    """推广消息未读数。bundle @703173 —— 官方 `{api, v:'1.0', needLogin:true, showErrorToast:false}`，
    **data 为空、不套 requestJson**；响应 `data.unreadCount`。"""
    return await _mtop(client, MESSAGE_UNREAD_API, {})


async def coupon_info(client: Any, body: Any = None) -> Any:
    """活动奖励页的券列表。bundle @3246590 `sWe(e)`；调用点 @3265220 `sWe(h)`，
    `h` 的初值 @3265 附近 `FWe={pageNum:1,pageSize:10,couponType:WVe.ALL,source:GVe.ACTIVE}`
    （= `{pageNum:1,pageSize:10,couponType:0,source:'BONUS'}`，见 `BODY_DEFAULTS`）；
    响应读 `data.total` / `data.result`。枚举见 `COUPON_TYPES` / `COUPON_SOURCES` /
    `COUPON_STATUS`（@3243192 / @3243498 / @3244737）。"""
    return await _mtop(client, BP_COUPON_INFO_API,
                       {'requestJson': _json_request(_prepare('coupon_info', body))})


async def coupon_statistic(client: Any, body: Any = None) -> Any:
    """活动奖励页的券统计。bundle @3246428 `aWe()` —— **无参**，官方
    `requestJson` 恒为 `'{}'`；调用点 @3255626 拿它填券状态计数。"""
    return await _mtop(client, BP_COUPON_STATISTIC_API,
                       {'requestJson': _json_request(_prepare('coupon_statistic', body))})


async def account_card_v2(client: Any, body: Any = None) -> Any:
    """投放账户卡片 V2。bundle @3268292 `UWe(e)` —— 官方包装
    `{api, v:'1.0', needLogin:true, data:{requestJson: JSON.stringify(e)}}`。
    **载荷 `e` 在别的 chunk 里，本 bundle 取不到**（见 docs/contracts/ad.md）：
    所以这里只原样透传调用方给的业务对象，不替它猜键、也不设默认值。"""
    return await _mtop(client, ACCOUNT_CARD_V2_API,
                       {'requestJson': _json_request(_prepare('account_card_v2', body))})


async def activity_detail(client: Any) -> Any:
    """活动奖励详情渲染。bundle @3268518 `YWe()` —— 官方
    `{api, v:'1.0', needLogin:true, data:{}}`，**data 为空、不套 requestJson**、无参
    （与 `message_unread` 同形）。"""
    return await _mtop(client, ACTIVITY_DETAIL_RENDERING_API, {})


async def message_read(client: Any, body: Any = None, confirm: Any = False) -> Any:
    """标记推广消息已读（**写**）。bundle @3678499 `ZQe(e)`；调用点 @3699313 传
    `{messageIds:[...]}`。漏了 `confirm=True` 直接抛 `WriteNotConfirmed`。"""
    _require_confirm(confirm)
    return await _mtop(client, MESSAGE_READ_API, {'requestJson': _json_request(body)})
