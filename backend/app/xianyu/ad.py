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

**真机证据：未真机验证。** 本模块的 11 个接口都是按 bundle 的调用点形状写的，尚未在真机
（打包版工作台 + 真实 Cookie）上打过探针。因此：

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

#: 读取接口目录：`kind` -> 接口名 + 入参说明。
#: `request_json=False` 的那条（未读数）官方 `data` 是空对象。
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
}

#: `campaign.log.query` 的枚举（bundle @1784368 / @1784654：0=全部）
OPERATOR_TYPES = {0: '全部', 1: '用户', 2: '系统'}
OPERATION_TYPES = {0: '全部', 1: '添加', 2: '更新'}


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
    return await _mtop(client, ACCOUNT_INFO_API, {'requestJson': _json_request(body)})


async def package_info(client: Any, body: Any = None) -> Any:
    """投放包信息。bundle @3301020 `eqe(e)`；调用点 @3309064 `initPlanInfo` 传整个查询对象，
    响应 `data.packageTargetVOList` / `data.bidTargets` / `data.recPromotionMode` /
    `data.supportOrder`。"""
    return await _mtop(client, PACKAGE_INFO_API, {'requestJson': _json_request(body)})


async def item_search(client: Any, body: Any = None) -> Any:
    """商品搜索 V2。bundle @3301167 `tqe(e)`；调用点 @3408019 传
    `{needTabs, itemId|searchKey, campaignId, sortField?, sortType?}`，
    响应 `data.result.{tabList,itemList}` + `data.total`。"""
    return await _mtop(client, ITEM_SEARCH_API, {'requestJson': _json_request(body)})


async def customize_bid_estimates(client: Any, body: Any = None) -> Any:
    """自定义出价估算。bundle @3301315 `nqe(e)`；调用点 @3313962 / @3314544 / @3315969 传
    `{entityType, entityList, bidTarget, bidPrice, noBid, pcFirstRequest, campaignId?}`，
    响应 `data.customizeBidRecommendVOList` / `data.bidTargets`。"""
    return await _mtop(client, CUSTOMIZE_BID_ESTIMATES_API, {'requestJson': _json_request(body)})


async def group_list(client: Any, body: Any = None) -> Any:
    """投放单元列表。bundle @3253585 `gWe(e)` —— 官方包装不带 v（见 `_mtop` 的口径）；
    调用点 @3454098 传 `{pageNum,pageSize,startDate,endDate,campaignId,includeEndDate:true,groupStatus?}`，
    @3493409 追加 `excludeGroupStatus / campaignTitleLike / promotionMode / bidTargets`；
    响应 `data.result`。"""
    return await _mtop(client, GROUP_LIST_API, {'requestJson': _json_request(body)})


async def group_data(client: Any, body: Any = None) -> Any:
    """投放单元数据。bundle @3253874 `yWe(e)`；调用点 @3449844 传
    `{startDate,endDate,includeEndDate:true,adGroupId,allPeriod:false}`；
    响应含 `data` 与 `previousData`（官方在「昨天→今天」时拍成
    `dataDetailVOList` / `previousDataDetailVOList`）。"""
    return await _mtop(client, GROUP_DATA_API, {'requestJson': _json_request(body)})


async def campaign_log(client: Any, body: Any = None) -> Any:
    """计划操作日志。bundle @1665384 `IW(e)` —— 官方包装不带 v（见 `_mtop` 的口径）；调用点 @1794033 传
    `{startDate,endDate,pageNum,pageSize,campaignId:Number(r)||0,operatorType?,operationType?}`；
    枚举 @1784368 / @1784654：`operatorType` 0=全部/1=用户/2=系统，
    `operationType` 0=全部/1=添加/2=更新。"""
    return await _mtop(client, CAMPAIGN_LOG_API, {'requestJson': _json_request(body)})


async def message_list(client: Any, body: Any = None) -> Any:
    """推广消息列表。bundle @3678207 `XQe(e)`；调用点 @3698592 传 `{pageNum,pageSize}`，
    响应 `data.total` + 列表。"""
    return await _mtop(client, MESSAGE_LIST_API, {'requestJson': _json_request(body)})


async def message_detail(client: Any, body: Any = None) -> Any:
    """推广消息详情。bundle @3678352 `KQe(e)`；调用点 @3696148 传 `{messageId}`，
    响应 `data.title` / `data.content`。"""
    return await _mtop(client, MESSAGE_DETAIL_API, {'requestJson': _json_request(body)})


async def message_unread(client: Any) -> Any:
    """推广消息未读数。bundle @703173 —— 官方 `{api, v:'1.0', needLogin:true, showErrorToast:false}`，
    **data 为空、不套 requestJson**；响应 `data.unreadCount`。"""
    return await _mtop(client, MESSAGE_UNREAD_API, {})


async def message_read(client: Any, body: Any = None, confirm: Any = False) -> Any:
    """标记推广消息已读（**写**）。bundle @3678499 `ZQe(e)`；调用点 @3699313 传
    `{messageIds:[...]}`。漏了 `confirm=True` 直接抛 `WriteNotConfirmed`。"""
    _require_confirm(confirm)
    return await _mtop(client, MESSAGE_READ_API, {'requestJson': _json_request(body)})
