"""官方「数据」组（数据总览 / 粉丝数据 / 客服数据）的只读协议。

为什么单开一个模块、而不是往 `client.py` 里加方法：

* 商品数据走 `client.py::item_stats`（`mtop.alibaba.idle.seller.pc.item.stats.query`），
  数据组其余三个 tab 走的是**另一族** mtop（`...pc.datacompass.*`），接口名、入参形状、
  响应结构都不一样；
* `client.py` 在本任务里是冻结文件（task-52 写范围外），所以这里用「模块级 async 函数 +
  调用方传入 client」的形态，服务层通过 `common.protocol_module('datacenter')` 懒导入。

接口名与入参来自官方微应用 bundle（`https://g.alicdn.com/idle-pc/idle-seller-data/0.0.11/js/main.js`，
本地副本 `logs/seller_discover/datacenter_main.js`，从里面抽出 56 个 mtop 名）。调用形态是
`xge({api, v:'1.0', data:{...}})`，对应本仓库的 `client.mtop(api, data)`。

**真机证据（2026-10-10，账号 2217970177348，只读 2 轮，原始片段见
`docs/contracts/datacenter.md`）**：

* `mtop.alibaba.idle.seller.datacompass.get.begin.time` → `{"code":"success","data":"20260201","msg":"成功"}`
  —— 数据起始日就是 `data` 里那个 8 位日期。
* `...pc.datacompass.cs.overview.summary` data=`{}` → `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数dateType`；
  补上 `dateType` 后变成 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。**说明 `dateType` 必填**，
  而权限是账号维度的事。
* `...singleuser.browse.summary` / `singleuser.item.summary` / `fans.summary`
  （`dateType` 单给、再加 `selectedSellerId` 都一样）→ `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。

结论：**这个账号没有数据权限**，所以指标只能如实返回 None。协议层因此不写任何「兜底 0」，
也不猜字段：认不出就是 None。而且「无权限」是平台的**有效答复**（不是链路故障）：
`_collect` 不因为它把整个请求判死，页面拿到的是 200 + 一屏 None + 一句
「这个账号没有『数据』权限」，而不是一个看不出所以然的 502。
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

#: 数据起始日（唯一一个在真机上确认能通的接口）
BEGIN_TIME_API = 'mtop.alibaba.idle.seller.datacompass.get.begin.time'
#: 数据总览：曝光 / 访问 / 浏览
OVERVIEW_API = 'mtop.alibaba.idle.seller.pc.datacompass.singleuser.browse.summary'
#: 数据总览：在线商品数等商品维度
OVERVIEW_ITEM_API = 'mtop.alibaba.idle.seller.pc.datacompass.singleuser.item.summary'
#: 粉丝数据：概览
FANS_API = 'mtop.alibaba.idle.seller.pc.datacompass.fans.summary'
#: 粉丝数据：粉丝画像 / 洞察
FANS_INSIGHTS_API = 'mtop.alibaba.idle.seller.pc.datacompass.fans.insights.query'
#: 客服数据：概览（询单人数 / 响应率）
CS_OVERVIEW_API = 'mtop.alibaba.idle.seller.pc.datacompass.cs.overview.summary'

#: 官方页面只有两种 `dateType`；自定义区间用 `month` + `ms`/`date`
DATE_TYPE_DAY = 'recent1d'
DATE_TYPE_MONTH = 'month'

#: 平台响应里的字段别名。**协议层命名未定**（真机拿不到有权限账号的响应），
#: 所以按 `services/item_stats.py::METRIC_ALIASES` 的做法先认常见几种，认不出就是 None。
#: 键名与商品统计保持同一套词根（exposure/visits/visitors），避免同一个词在两个页面
#: 指两个指标。
METRIC_ALIASES: Dict[str, Tuple[str, ...]] = {
    'exposure': ('exposure', 'exposureNum', 'exposureCount', 'expose', 'exposeCount',
                 'impressions', '曝光', '曝光次数', '曝光量'),
    'exposure_visitors': ('exposureVisitors', 'exposureUv', 'exposeUv', 'exposureUserCount',
                          '曝光人数', '曝光访客数'),
    'visits': ('visits', 'visitCount', 'visit', 'visitNum', 'pv', 'pageView',
               'showNum', 'showCount', '商品访问次数', '访问次数', '浏览量'),
    'visit_visitors': ('visitors', 'visitorCount', 'visitor', 'uv', 'visitUv',
                       'uniqueVisitors', '访问人数', '访客数'),
    'browse': ('browse', 'browseCount', 'browseNum', 'browsePv', '浏览次数'),
    'browse_visitors': ('browseVisitors', 'browseUv', 'browseUserCount', '浏览人数'),
    'online_items': ('onlineItems', 'onlineItemCount', 'onSaleCount', 'onSaleNum',
                     'onlineNum', '在线商品数'),
    'inquiry_visitors': ('inquiryVisitors', 'inquiryUserCount', 'consultUv', 'askUv',
                         '询单人数', '咨询人数'),
    'inquiry_reply_rate_3min': ('replyRate3min', 'threeMinReplyRate', 'replyRate',
                                'responseRate', '3分钟询单响应率', '三分钟响应率'),
    'fans_total': ('fansTotal', 'totalFans', 'fanCount', 'fansCount', '粉丝总数', '粉丝数'),
    'fans_new': ('fansNew', 'newFans', 'newFanCount', 'increaseFans', '新增粉丝', '新增粉丝数'),
    'fans_lost': ('fansLost', 'lostFans', 'lostFanCount', 'decreaseFans', '流失粉丝', '取关人数'),
    'fans_active': ('fansActive', 'activeFans', 'activeFanCount', '活跃粉丝', '活跃粉丝数'),
    'fans_orders': ('fansOrders', 'fansOrderCount', 'fanOrderCount', 'fansOrderNum',
                    '粉丝订单数', '粉丝成交笔数'),
    'fans_buyers': ('fansBuyers', 'fansBuyerCount', 'fanBuyerCount', 'fansBuyerNum',
                    '粉丝买家数', '粉丝下单人数'),
    'inquiry_count': ('inquiryCount', 'inquiryNum', 'consultCount', 'askCount', '询单量', '咨询量'),
    'reply_rate_3min': ('replyRate3min', 'threeMinReplyRate', 'replyRate', 'responseRate',
                        '3分钟响应率', '三分钟响应率', '响应率'),
    'avg_reply_seconds': ('avgReplySeconds', 'avgReplyTime', 'averageReplyTime',
                          '平均响应时长', '平均响应秒数'),
    'unreplied_count': ('unrepliedCount', 'noReplyCount', 'unansweredCount', '未回复数', '未回复'),
    'satisfaction': ('satisfaction', 'satisfactionRate', 'goodRate', '满意度', '好评率'),
}

#: 对外契约的指标键（三个页面各一套）。**这三个元组就是前端能依赖的全部键**：
#: 平台没给的键也一定出现，值为 None（前端显示「-」）。
OVERVIEW_METRICS: Tuple[str, ...] = (
    'exposure',                 # 曝光次数
    'exposure_visitors',        # 曝光人数
    'visits',                   # 商品访问次数
    'visit_visitors',           # 访问人数
    'browse',                   # 浏览次数
    'browse_visitors',          # 浏览人数
    'online_items',             # 在线商品数
    'inquiry_visitors',         # 询单人数
    'inquiry_reply_rate_3min',  # 3 分钟询单响应率（平台原样百分比，如 33.33）
)
FANS_METRICS: Tuple[str, ...] = (
    'fans_total',   # 粉丝总数
    'fans_new',     # 新增粉丝
    'fans_lost',    # 流失粉丝
    'fans_active',  # 活跃粉丝
    'fans_orders',  # 粉丝订单数
    'fans_buyers',  # 粉丝买家数
)
CS_METRICS: Tuple[str, ...] = (
    'inquiry_visitors',    # 询单人数
    'inquiry_count',       # 询单量
    'reply_rate_3min',     # 3 分钟响应率（平台原样百分比）
    'avg_reply_seconds',   # 平均响应时长（秒）
    'unreplied_count',     # 未回复数
    'satisfaction',        # 满意度 / 好评率（平台原样百分比）
)

#: 平台字段名里带这些词根、却又没被别名表认出来的，值得单独提一句（见 `suspect_keys`）
_METRIC_HINTS = ('count', 'num', 'cnt', 'rate', 'uv', 'pv', 'total')


def date_data(start: str = '', end: str = '', seller_id: str = '') -> Dict[str, Any]:
    """构造数据组接口的入参。

    官方页面默认 `dateType='recent1d'`（今天），切到自定义区间时用 `'month'` 并把
    区间塞进 `ms`/`date`（`date` 是数组，`ms` 取区间首日）。`selectedSellerId` 是
    「看哪个卖家」——多店铺账号才需要，单账号时官方页面也会带上自己的 id。

    **只要用户明确指定了日期就必须带上日期**（含 `start == end` 的单日）：`recent1d`
    的请求体里一个日期都没有，平台只能回「今天」；那样服务层还把它标成用户要的那一天
    就是对外撒谎。缺哪头用另一头顶上，两边都不传才是「今天」。
    """
    payload: Dict[str, Any] = {'dateType': DATE_TYPE_DAY}
    if seller_id:
        payload['selectedSellerId'] = str(seller_id)
    if start or end:
        begin = start or end
        finish = end or start
        payload['dateType'] = DATE_TYPE_MONTH
        payload['ms'] = begin
        payload['date'] = [begin, finish]
    return payload


def walk_dicts(value: Any, depth: int = 0) -> Iterator[Dict[str, Any]]:
    """深度优先遍历嵌套 dict/list，产出每一个 dict（指标可能埋在 `data.data` 里）。"""
    if depth > 6:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk_dicts(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from walk_dicts(item, depth + 1)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def find_number(payload: Any, aliases: Tuple[str, ...]) -> Optional[float]:
    """按别名找第一个数值。找不到返回 None（**绝不返回 0**）。"""
    wanted = {str(key).lower() for key in aliases}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and _is_number(value):
                return float(value)
    return None


def find_text(payload: Any, aliases: Tuple[str, ...]) -> Optional[str]:
    """按别名找第一个非空字符串（粉丝画像这类文本字段用）。"""
    wanted = {str(key).lower() for key in aliases}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and isinstance(value, str) and value.strip():
                return value.strip()
    return None


def find_list(payload: Any, aliases: Tuple[str, ...]) -> Optional[List[Any]]:
    """按别名找第一个非空列表。找不到返回 None。"""
    wanted = {str(key).lower() for key in aliases}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and isinstance(value, list) and value:
                return value
    return None


def extract_metrics(payload: Any, keys: Tuple[str, ...]) -> Dict[str, Optional[float]]:
    """把平台响应翻成「键 → 数值 / None」。认不出的一律 None。"""
    return {key: find_number(payload, METRIC_ALIASES.get(key, (key,))) for key in keys}


def suspect_keys(payload: Any, keys: Tuple[str, ...]) -> List[str]:
    """响应里像指标、却一个别名都没命中的字段名（只用于提示，不参与取值）。"""
    known = set()
    for key in keys:
        known.update(str(item).lower() for item in METRIC_ALIASES.get(key, (key,)))
    found: List[str] = []
    for mapping in walk_dicts(payload):
        for name in mapping:
            low = str(name).lower()
            if low in known or name in found:
                continue
            if any(hint in low for hint in _METRIC_HINTS):
                found.append(str(name))
    return found[:8]


async def _mtop(client: Any, api: str, data: Any) -> Any:
    """调一次 mtop 并剥掉外层信封，返回平台响应里的 `data`。

    `client.mtop` 失败时会抛 `XianyuError`（含 `FAIL_*` 原文），这里不吞：
    服务层要靠这个原文判断「无权限 / 未登录 / 风控」。
    """
    result = await client.mtop(api, data)
    if isinstance(result, dict):
        return result.get('data')
    return result


def _begin_from(payload: Any) -> Optional[str]:
    """从 `get.begin.time` 的响应里取起始日（真机形状：`{"code":"success","data":"20260201"}`）。

    **注意裸字符串这一路**：`_mtop` 会把外层信封剥掉（`result.get('data')`），所以
    `begin_time()` 拿到的其实是裸的 `'20260201'`，不是那个 dict —— 只认 dict 的话
    真机上永远返回 None（端到端用例 `test_real_protocol_permission_denied_gives_null_not_502`
    就是这么抓出来的）。
    """
    if isinstance(payload, str) and len(payload) == 8 and payload.isdigit():
        return payload
    for mapping in walk_dicts(payload):
        value = mapping.get('data') if isinstance(mapping, dict) else None
        if isinstance(value, str) and len(value) == 8 and value.isdigit():
            return value
    return None


async def begin_time(client: Any) -> Optional[str]:
    """数据起始日（`YYYYMMDD`）。真机实测唯一能通的接口，认不出返回 None。"""
    return _begin_from(await _mtop(client, BEGIN_TIME_API, None))


def _real_date_range(payload: Any) -> Optional[List[str]]:
    """平台实际返回的日期区间（`extendInfo.realDateRange`，官方页面用它标图例）。"""
    for mapping in walk_dicts(payload):
        value = mapping.get('realDateRange')
        if isinstance(value, list) and value:
            return [str(item) for item in value]
    return None


def _error_text(exc: BaseException) -> str:
    return f'{type(exc).__name__}: {exc}'


def _is_permission_error(exc: BaseException) -> bool:
    """平台明确答复「你没权限」—— 这是**有效回答**，不是链路故障。"""
    text = _error_text(exc)
    return 'UNAUTHORIZED' in text or '无权限' in text


def _is_session_error(exc: BaseException) -> bool:
    """登录态过期。决定信封时必须优先它，否则会被业务错吞成 502、还不回写 need_relogin。"""
    text = _error_text(exc)
    return 'SessionExpired' in text or 'SESSION_EXPIRED' in text


def _pick_exc(failed: Dict[str, BaseException]) -> BaseException:
    for exc in failed.values():
        if _is_session_error(exc):
            return exc
    return next(iter(failed.values()))


async def _collect(client: Any, calls: Tuple[Tuple[str, str, Any], ...],
                   keys: Tuple[str, ...]):
    """逐个调平台接口，返回 `(指标, 原始响应, 错误)`。

    容错口径分两层：

    * **单个接口坏掉只影响它自己贡献的指标**，其余照常；
    * **全坏**时再看坏在哪：如果全是「平台拒绝访问」（`FAIL_BIZ_IDLE_USER_UNAUTHORIZED`
      之类），那是平台给的有效答复 —— 如实返回一屏 `None` + 人话 warning（200），
      这正是本模块存在的意义（真机上 `fans` / `cs` 两个页面各自只有 1~2 个接口，
      若按「全坏即抛」处理，它们会**永远** 502，用户连「没权限」都看不到）。
      只有出现链路 / 签名 / 登录态这类故障才抛异常，让 `protocol_error`（502）说实话。

    抛的是**最严重**的那个原始异常（登录态过期优先于业务错），这样
    `common.call_failed` 仍能按类名认出「登录态过期」并回写账号状态。
    """
    metrics: Dict[str, Optional[float]] = {key: None for key in keys}
    raw: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    failed: Dict[str, BaseException] = {}
    for label, api, data in calls:
        try:
            payload = await _mtop(client, api, data)
        except Exception as exc:  # noqa: BLE001 - 逐接口容错，原文进 errors
            errors[label] = _error_text(exc)
            failed[label] = exc
            continue
        raw[label] = payload
        for key, value in extract_metrics(payload, keys).items():
            if value is not None and metrics.get(key) is None:
                metrics[key] = value
    if failed and len(failed) == len(calls):
        if not all(_is_permission_error(exc) for exc in failed.values()):
            raise _pick_exc(failed)
    return metrics, raw, errors


async def overview(client: Any, start: str = '', end: str = '',
                   seller_id: str = '') -> Dict[str, Any]:
    """数据总览：起始日 + 曝光 / 访问 / 浏览 / 在线商品 / 询单。"""
    data = date_data(start, end, seller_id)
    metrics, raw, errors = await _collect(client, (
        ('overview', OVERVIEW_API, dict(data)),
        ('items', OVERVIEW_ITEM_API, dict(data)),
        ('begin_time', BEGIN_TIME_API, None),
    ), OVERVIEW_METRICS)
    return {
        'metrics': metrics,
        'begin_time': _begin_from(raw.get('begin_time')),
        'real_date_range': _real_date_range(raw.get('overview')) or _real_date_range(raw.get('items')),
        'suspect_keys': suspect_keys(raw.get('overview'), OVERVIEW_METRICS),
        'errors': errors,
        'raw': raw,
    }


async def fans(client: Any, start: str = '', end: str = '',
               seller_id: str = '') -> Dict[str, Any]:
    """粉丝数据：粉丝总数 / 新增 / 流失 / 活跃 + 画像洞察列表。"""
    data = date_data(start, end, seller_id)
    metrics, raw, errors = await _collect(client, (
        ('fans', FANS_API, dict(data)),
        ('insights', FANS_INSIGHTS_API, dict(data)),
    ), FANS_METRICS)
    return {
        'metrics': metrics,
        'insights': find_list(raw.get('insights'), ('insights', 'insightList', 'fanInsights', 'list')),
        'real_date_range': _real_date_range(raw.get('fans')),
        'suspect_keys': suspect_keys(raw.get('fans'), FANS_METRICS),
        'errors': errors,
        'raw': raw,
    }


async def customer_service(client: Any, start: str = '', end: str = '',
                           seller_id: str = '') -> Dict[str, Any]:
    """客服数据：询单人数 / 询单量 / 3 分钟响应率 / 平均响应时长 / 未回复 / 满意度。

    官方还有 `cs.detail.list`、`cs.diversion.list`、`cs.evaluation.list`（明细、分流、
    评价）与一堆导出接口 —— 那是「客服数据」页里的子 tab，本任务只复刻概览。
    """
    data = date_data(start, end, seller_id)
    metrics, raw, errors = await _collect(client, (
        # 标签别复用 'overview'：它是「数据总览」页的标签，warnings 会把它翻成人话
        ('cs', CS_OVERVIEW_API, dict(data)),
    ), CS_METRICS)
    return {
        'metrics': metrics,
        # 取值必须用本页自己的标签 'cs'（`_collect` 只按 label 写 `raw`）——写成 'overview'
        # 的话这两个字段永远空转：`real_date_range` 恒 None、别名认错时也没有
        # `unrecognized_keys` 自证，CS 页 6 个指标全是假设别名却查不出问题。
        'real_date_range': _real_date_range(raw.get('cs')),
        'suspect_keys': suspect_keys(raw.get('cs'), CS_METRICS),
        'errors': errors,
        'raw': raw,
    }
