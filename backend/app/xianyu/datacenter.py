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

import json
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


# ---------------------------------------------------------------------------
# 粉丝画像分组（`fans.insights.query` 的具名列表）
# ---------------------------------------------------------------------------
#
# 官方「粉丝数据」页把 `fans.insights.query` 响应 `data.data` 里的**具名列表**渲染成
# 四张图（bundle `datacenter_main.js` 偏移 2452720 的 `rwe()`，解构出
# `buyerGenderList` / `fansTypeList` / `buyerProvinceList` / `buyerEightTpList`）。
#
# 本仓库原先只按 `insights` / `insightList` / `fanInsights` / `list` 找「一个列表」——
# 真机响应里**没有这些键**，于是粉丝画像一栏永远是空的：页面看着「正常」，
# 其实是数据根本没被解析出来。这一段就是补这个缺口。
#
# 两个必须小心的口径：
# * 这些列表里 `profileVal` 是**标签**（`'M'` / `'A_流失粉'` / `'学生群体'`），不是数字；
# * 比例有两个来源：`usrRatio`（0~1 浮点）与 `usrRatioFormat`（`'12.34%'` 字符串，
#   官方直接喂给图表）。两个都如实透出，一个都没有就是 None，**不补 0**。

#: 我们的键 → (平台列表键, 页面上的标题)
FANS_INSIGHT_GROUPS: Tuple[Tuple[str, str, str], ...] = (
    ('gender', 'buyerGenderList', '性别分布'),
    ('fans_type', 'fansTypeList', '粉丝类型'),
    ('province', 'buyerProvinceList', '省份分布'),
    ('eight_type', 'buyerEightTpList', '八大人群'),
    ('age', 'buyerAgeList', '年龄分布'),
    ('active', 'buyerActiveList', '活跃度分布'),
)

#: 官方把 `profileVal` 的 `M`/`F` 翻成中文，且 `M` 排在前（bundle 偏移 2490798）
FANS_GENDER_LABELS: Dict[str, str] = {'M': '男', 'F': '女'}
FANS_GENDER_ORDER: Tuple[str, ...] = ('M', 'F')

#: 官方粉丝七型：展示时去掉 `A_` 前缀、按 A→G 排序
#: （官方还会把缺失档位补成 `0.00%`；我们**只排序不补**，与本仓「不造 0」的硬口径一致）
FANS_TYPE_ORDER: Tuple[str, ...] = (
    'A_流失粉', 'B_僵尸粉', 'C_沉默粉', 'D_活跃粉', 'E_亲密粉', 'F_超亲密粉', 'G_忠诚粉',
)

#: 官方八大人群顺序表（bundle 偏移 2491628，注意 `未来之星` 是 0）
FANS_EIGHT_ORDER: Dict[str, int] = {
    '未来之星': 0, '学生群体': 1, '小康青年': 2, '小资青年': 3,
    '家有儿女': 4, '勤俭持家': 5, '资深中产': 6, '退休长辈': 7, '模糊人群': 8,
}

#: 画像条目里「标签」可能在的键
_PROFILE_LABEL_KEYS = ('profileVal', 'name', 'label', 'title', 'key', 'type')
#: 比例（0~1 浮点）
_PROFILE_RATIO_KEYS = ('usrRatio', 'ratio', 'rate')
#: 比例（已格式化字符串，如 `'12.34%'`）
_PROFILE_RATIO_TEXT_KEYS = ('usrRatioFormat', 'ratioFormat', 'rateFormat')


def _profile_label(entry: Dict[str, Any]) -> Optional[str]:
    for key in _PROFILE_LABEL_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _profile_ratio(entry: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """返回 `(ratio, ratio_text)`；一个都没有就是 `(None, None)`，**不补 0**。"""
    text: Optional[str] = None
    for key in _PROFILE_RATIO_TEXT_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    for key in _PROFILE_RATIO_KEYS:
        value = entry.get(key)
        if _is_number(value):
            return float(value), text
    if text and text.endswith('%'):
        try:
            return float(text[:-1]) / 100.0, text
        except ValueError:
            return None, text
    return None, text


def _profile_items(entries: Any, *, label_map: Optional[Dict[str, str]] = None,
                   strip_prefix: bool = False) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        raw_label = _profile_label(entry)
        name = label_map.get(raw_label, raw_label) if (label_map and raw_label) else raw_label
        if strip_prefix and name and len(name) > 2 and name[1] == '_':
            name = name[2:]
        ratio, ratio_text = _profile_ratio(entry)
        items.append({
            'name': name,
            'ratio': ratio,
            'ratio_text': ratio_text,
            'raw_label': raw_label,
            'raw': entry,
        })
    return items


def _order_index(raw_label: Optional[str], order: Tuple[str, ...]) -> int:
    """官方枚举顺序；认不出的排到最后（官方也是 `999`）。"""
    if raw_label in order:
        return order.index(raw_label)  # type: ignore[arg-type]
    return len(order)


def _by_ratio_desc(item: Dict[str, Any]) -> Tuple[int, float]:
    """比例大的在前；没给比例的排最后（**不当 0 参与排序**）。"""
    ratio = item.get('ratio')
    if isinstance(ratio, (int, float)):
        return (0, -float(ratio))
    return (1, 0.0)


def _find_profile_list(payload: Any, platform_key: str) -> Optional[List[Any]]:
    for mapping in walk_dicts(payload):
        value = mapping.get(platform_key)
        if isinstance(value, list) and value:
            return value
    return None


def parse_fans_insights(payload: Any) -> Optional[Dict[str, Any]]:
    """把 `fans.insights.query` 的响应翻成画像分组。

    返回 `{'groups': {我们的键: {key, title, platform_key, items}}, 'raw_keys': [...]}`；
    **平台一组都没给时返回 `None`**（不是空 dict —— 空 dict 会让上层把「没解析出来」
    当成「今天没人来」）。
    """
    groups: Dict[str, Any] = {}
    raw_keys: List[str] = []
    for our_key, platform_key, title in FANS_INSIGHT_GROUPS:
        entries = _find_profile_list(payload, platform_key)
        if entries is None:
            continue
        if our_key == 'gender':
            items = _profile_items(entries, label_map=FANS_GENDER_LABELS)
            items.sort(key=lambda item: _order_index(item.get('raw_label'), FANS_GENDER_ORDER))
        elif our_key == 'fans_type':
            items = _profile_items(entries, strip_prefix=True)
            items.sort(key=lambda item: _order_index(item.get('raw_label'), FANS_TYPE_ORDER))
        elif our_key == 'eight_type':
            items = _profile_items(entries)
            items.sort(key=lambda item: FANS_EIGHT_ORDER.get(
                str(item.get('raw_label') or ''), len(FANS_EIGHT_ORDER)))
        else:
            items = _profile_items(entries)
            items.sort(key=_by_ratio_desc)
        raw_keys.append(platform_key)
        groups[our_key] = {
            'key': our_key,
            'title': title,
            'platform_key': platform_key,
            'items': items,
        }
    if not groups:
        return None
    return {'groups': groups, 'raw_keys': raw_keys}


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
    """粉丝数据：粉丝总数 / 新增 / 流失 / 活跃 + 画像分组。

    `insights` 是老的「找一个列表」路径（真机响应里通常为 None，保留是为了不破坏既有调用方）；
    `insights_breakdown` 才是官方页面真正渲染的具名分组（见 `parse_fans_insights`）。
    """
    data = date_data(start, end, seller_id)
    metrics, raw, errors = await _collect(client, (
        ('fans', FANS_API, dict(data)),
        ('insights', FANS_INSIGHTS_API, dict(data)),
    ), FANS_METRICS)
    return {
        'metrics': metrics,
        'insights': find_list(raw.get('insights'), ('insights', 'insightList', 'fanInsights', 'list')),
        'insights_breakdown': parse_fans_insights(raw.get('insights')),
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


# ---------------------------------------------------------------------------
# 商品数据（官方 `/seller-data/commodity`）—— 一行一个商品 + 平台给的指标列
# ---------------------------------------------------------------------------
#: 商品数据：列表（官方包装 `zSe`，`data:{...}` 直传）
COMMODITY_LIST_API = 'mtop.alibaba.idle.seller.pc.datacompass.item.list'
#: 商品数据：指标列定义（平台决定表头有哪些列、`orderBy` 该填什么）
COMMODITY_INDICATORS_API = 'mtop.alibaba.idle.seller.pc.datacompass.item.indicators'
#: 官方页面 `tabType` 只有一个取值（bundle 里唯一调用点 `M("all")`，在时间筛选的
#: `onChange` 里 —— 所以它不是「用户在两个 tab 之间切」，就是常量）
COMMODITY_TAB_TYPE = 'all'
#: 自定义区间的 `dateType`（官方页面自己的枚举值，与 `date_data` 的 `month` 不是一套）
COMMODITY_DATE_CUSTOM = 'customDate'
#: 官方每页条数选项（`showSizeChanger` 开着，`[10,20,50]`）
COMMODITY_PAGE_SIZES: Tuple[int, ...] = (10, 20, 50)
COMMODITY_DEFAULT_PAGE_SIZE = 20
#: 每行都有的固定字段（其余列由 `item.indicators` 决定，列名不翻译）
COMMODITY_ROW_KEYS: Tuple[str, ...] = ('itmId', 'itmName', 'itmPicUrl', 'accountName')


def category_info_str(value: Any) -> str:
    """官方 `X()`：类目筛选入参是**字符串化的数组**（falsy → `'[]'`）。

    官方写法 `JSON.stringify(list.map(JSON.parse))` 说明「数组元素本身又是 JSON 字符串」
    也吃。这里两种都收：元素是 dict 直接用、是字符串就尽量解析（解不出来按原样留着，
    平台要拒就让它拒，**不悄悄丢数据**）。
    """
    if not value:
        return '[]'
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return '[]'
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError('类目筛选必须是 JSON 数组')
    if not isinstance(value, (list, tuple)):
        raise ValueError('类目筛选必须是 JSON 数组')
    items: List[Any] = []
    for item in value:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                pass
        items.append(item)
    return json.dumps(items, ensure_ascii=False, separators=(',', ':'))


def commodity_dates(start: str = '', end: str = '') -> Tuple[str, List[str]]:
    """官方页面的日期入参：给了区间 → `('customDate', [start, end])`；否则今天。

    缺哪头用另一头顶上，理由同 `date_data`：`recent1d` 的请求体里一个日期都没有，
    平台只能回「今天」，把它标成用户要的那一天就是对外撒谎。
    """
    if start or end:
        begin = start or end
        finish = end or start
        return COMMODITY_DATE_CUSTOM, [begin, finish]
    return DATE_TYPE_DAY, []


def commodity_list_request(date_type: str = DATE_TYPE_DAY, dates: Optional[List[str]] = None,
                           seller_id: str = '', category_info: Any = None,
                           item_id: str = '', item_name: str = '', is_dp_stk: str = '',
                           mkt_act_id: str = '', page: int = 1,
                           page_size: int = COMMODITY_DEFAULT_PAGE_SIZE,
                           order_by: str = '', order: str = '') -> Dict[str, Any]:
    """构造 `item.list` 的入参。**键名逐字照 bundle**，一个都不能少、也不能改名。

    官方那行（`datacenter_main.js` 偏移 2596900 附近）：

    ```js
    {selectedSellerId: Y, tabType: E, dateRange: 'customDate'===f ? r.join('|') : '',
     ms: 'month'===f ? r[0] : '', dateType: f, categoryInfoStr: X(L.categoryInfoStr),
     itmName: L.itmName, itmId: L.itmId, isDpStk: L.isDpStk, mktActId: k||'',
     page: z.current, pageSize: z.pageSize, orderBy: W.sortField, order: W.sortOrder}
    ```

    两个容易写错的点：`dateRange` 用 `|` 连接（不是 `-`、不是数组）；`isDpStk` 是页面上
    「是否掉包」的下拉值，可能是 `''`（不筛）而不是布尔。
    """
    dates = [str(item) for item in (dates or [])]
    payload: Dict[str, Any] = {
        'selectedSellerId': str(seller_id or ''),
        'tabType': COMMODITY_TAB_TYPE,
        'dateRange': '|'.join(dates) if date_type == COMMODITY_DATE_CUSTOM and dates else '',
        'ms': dates[0] if date_type == DATE_TYPE_MONTH and dates else '',
        'dateType': date_type,
        'categoryInfoStr': category_info_str(category_info),
        'itmName': str(item_name or ''),
        'itmId': str(item_id or ''),
        'isDpStk': str(is_dp_stk or ''),
        'mktActId': str(mkt_act_id or ''),
        'page': int(page),
        'pageSize': int(page_size),
        'orderBy': str(order_by or ''),
        'order': str(order or ''),
    }
    return payload


def parse_commodity_list(payload: Any) -> Dict[str, Any]:
    """解析 `item.list`：`data.list` + `data.total` + 顶层 `extendInfo`。

    `_mtop` 已剥掉客户端外层信封，所以 `payload` 就是平台 `data`（官方包装读
    `e.data.data.list`，对应这里的 `payload['data']['list']`）。

    **认不出列表键时 `rows=None`（不是 `[]`）**：`[]` 会被读成「这个卖家一件商品都没有」，
    而真实情况更可能是平台改了字段名 —— 服务层据此给人话 warning。
    """
    business = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(business, dict):
        business = None
    rows: Optional[List[Any]] = None
    found = ''
    for source, key in ((business, 'list'), (payload, 'list'), (business, 'itemList'),
                        (business, 'result'), (payload, 'itemList')):
        if isinstance(source, dict):
            value = source.get(key)
            if isinstance(value, list):
                rows = value
                found = key
                break
    total: Optional[float] = None
    for source in (business, payload):
        if isinstance(source, dict) and _is_number(source.get('total')):
            total = float(source['total'])
            break
    extend = payload.get('extendInfo') if isinstance(payload, dict) else None
    if not isinstance(extend, list):
        extend = None
    return {'rows': rows, 'total': total, 'extend_info': extend, 'found_key': found}


def parse_commodity_indicators(payload: Any) -> Dict[str, Any]:
    """解析 `item.indicators`：平台决定「商品数据」表有哪些列。

    官方形状：`data` 是分组数组，每组 `{name, subFields:[{name,title,key}]}`；页面把
    `subFields` 摊平、每列打上 `type = 组名`（`type` 只是表头分组），排序用的 `orderBy`
    是**列自己的 `name`**，`dataIndex` 也是 `name`。

    认不出时 `columns`/`groups` 都是 `None`（不是空表 —— 空表会让前端以为「这个账号
    没有可看的指标」）。
    """
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, list):
        data = payload if isinstance(payload, list) else None
    if not isinstance(data, list):
        return {'columns': None, 'groups': None}
    columns: List[Dict[str, Any]] = []
    groups: List[Dict[str, Any]] = []
    for group in data:
        if not isinstance(group, dict):
            continue
        name = str(group.get('name') or '')
        fields: List[Dict[str, Any]] = []
        sub = group.get('subFields')
        if isinstance(sub, list):
            for field in sub:
                if not isinstance(field, dict):
                    continue
                field_name = str(field.get('name') or '')
                column = {
                    'name': field_name,
                    'title': str(field.get('title') or field_name),
                    'key': str(field.get('key') or field_name),
                    'group': name,
                }
                fields.append(column)
                columns.append(column)
        groups.append({'name': name, 'fields': fields})
    if not columns:
        return {'columns': None, 'groups': None}
    return {'columns': columns, 'groups': groups}


async def commodity(client: Any, start: str = '', end: str = '', seller_id: str = '',
                    category_info: Any = None, item_id: str = '', item_name: str = '',
                    is_dp_stk: str = '', mkt_act_id: str = '', page: int = 1,
                    page_size: int = COMMODITY_DEFAULT_PAGE_SIZE, order_by: str = '',
                    order: str = '') -> Dict[str, Any]:
    """商品数据：一行一个商品 + 平台给的指标列定义（**只读**）。

    两个接口各自容错（列定义拿不到不该让整页空掉），但**两个都坏**时按 `_collect`
    的口径处理：全是「平台拒绝访问」就如实返回 + 人话 warning，否则抛最严重的那个
    （登录态过期优先），让服务层回 502 / 409。
    """
    date_type, dates = commodity_dates(start, end)
    request = commodity_list_request(date_type=date_type, dates=dates, seller_id=seller_id,
                                     category_info=category_info, item_id=item_id,
                                     item_name=item_name, is_dp_stk=is_dp_stk,
                                     mkt_act_id=mkt_act_id, page=page, page_size=page_size,
                                     order_by=order_by, order=order)
    errors: Dict[str, str] = {}
    failed: Dict[str, BaseException] = {}
    raw: Dict[str, Any] = {}
    for label, api, data in (('commodity', COMMODITY_LIST_API, dict(request)),
                             ('columns', COMMODITY_INDICATORS_API, None)):
        try:
            raw[label] = await _mtop(client, api, data)
        except Exception as exc:  # noqa: BLE001 - 逐接口容错，原文进 errors
            errors[label] = _error_text(exc)
            failed[label] = exc
    if failed and len(failed) == 2 and not all(_is_permission_error(exc) for exc in failed.values()):
        raise _pick_exc(failed)
    parsed = parse_commodity_list(raw.get('commodity'))
    indicators = parse_commodity_indicators(raw.get('columns'))
    return {
        'rows': parsed['rows'],
        'total': parsed['total'],
        'extend_info': parsed['extend_info'],
        'found_key': parsed['found_key'],
        'columns': indicators['columns'],
        'groups': indicators['groups'],
        'real_date_range': _real_date_range(raw.get('commodity')),
        'date_type': date_type,
        'dates': dates,
        'errors': errors,
        'raw': raw,
    }


# ---------------------------------------------------------------------------
#: 数据总览页的四张「经营卡片」：官方是四个独立接口，但共用同一套入参形状
#: （`datacenter_main.js` 偏移 4077870 / 4062125 / 4067298 / 4099924 四处调用点）：
#:
#: ```js
#: {dateRange: 'customDate' === f ? dates.join('|') : undefined,
#:  dateType: f || 'recent1d', selectedSellerId: t,
#:  ms: 'month' === f ? dates[0] : undefined}
#: ```
#:
#: 注意这跟本模块前面三个接口（`date_data` 用 `month` + `ms` + `date`）**不是一套**：
#: 官方自己在这两处写法就不一样（商品数据页与这四张卡片用 `customDate` + `dateRange`，
#: 总览/粉丝/客服三页用 `month` + `ms` + `date`）。我们照抄各自的写法，不强行统一 ——
#: 统一会让其中一边发出平台不认的参数。
CARD_TRADE_API = 'mtop.alibaba.idle.seller.pc.datacompass.flow.detail'
CARD_SELLER_API = 'mtop.alibaba.idle.seller.pc.datacompass.singleuser.seller.summary'
CARD_REPURCHASE_API = 'mtop.alibaba.idle.seller.pc.datacompass.singleuser.repurchase.summary'
CARD_REFUND_API = 'mtop.alibaba.idle.seller.pc.datacompass.refund.summary'
CARD_APIS: Dict[str, str] = {
    'trade': CARD_TRADE_API,
    'seller': CARD_SELLER_API,
    'repurchase': CARD_REPURCHASE_API,
    'refund': CARD_REFUND_API,
}
CARD_KINDS: Tuple[str, ...] = ('trade', 'seller', 'repurchase', 'refund')
#: 官方页面上的卡片标题（逐字抄，不自己起名）
CARD_TITLES: Dict[str, str] = {
    'trade': '交易数据',
    'seller': '经营概况',
    'repurchase': '复购',
    'refund': '退款概览',
}

#: 官方指标中文标签表 —— `h_t`（bundle 偏移 4052415，全部账号口径）
SELLER_LABELS: Dict[str, str] = {
    'payAmt': '支付金额（元）',
    'payOrdCnt': '支付订单数',
    'aov': '笔单价',
    'showUv': '商品曝光人数',
    'ipvUv': '商品浏览人数',
    'payByrCnt': '成交人数',
    'vstPv': '商品访问次数',
    'vstUv': '商品访问人数',
    'showPv': '商品曝光次数',
    'ipv': '商品浏览次数',
    'rfdOrdCnt': '发起退款笔数',
    'rfdAmt': '发起退款金额（元）',
}
#: `v_t`（bundle 偏移 4057997，单个账号口径）—— 我们永远只查一个账号，实际用的是这张
SELLER_LABELS_SINGLE: Dict[str, str] = {
    'vstPv': '商品访问次数',
    'vstUv': '商品访问人数',
    'showPv': '商品曝光次数',
    'showUv': '商品曝光人数',
    'ipv': '商品浏览次数',
    'ipvUv': '商品浏览人数',
    'payOrdCnt': '支付笔数',
    'payAmt': '支付金额（元）',
    'rfdOrdCnt': '发起退款笔数',
    'rfdAmt': '发起退款金额（元）',
    'rpr': '复购率',
    'rptOrdCnt': '复购订单数',
    'rptByrCnt': '复购人数',
}
#: 复购卡的标签表 `x`（bundle 偏移 4065877）
REPURCHASE_LABELS: Dict[str, str] = {
    'rpr': '复购率',
    'rptOrdCnt': '复购订单数',
    'rptByrCnt': '复购人数',
}
#: 退款概览卡的标题是官方**写死**在构造点（bundle 偏移 4099924 起）里的，逐字抄；
#: `crtRfdRate`/`crtDsptRate`/`succRfdRate` 的官方标题分别是「退款发起率」「维权发起率」
#: 「退款完结率」，`dealingRfdCntRate` 是「处理中的笔数占比」。
REFUND_LABELS: Dict[str, str] = {
    'crtRfdCnt': '发起退款笔数',
    'crtRfdRate': '退款发起率',
    'crtRfdAmt': '退款金额（元）',
    'crtDsptCnt': '维权笔数',
    'crtDsptRate': '维权发起率',
    'crtDsptAmt': '维权金额（元）',
    'succRfdCnt': '成功退款笔数',
    'succRfdRate': '退款完结率',
    'succRfdAmt': '退款金额（元）',
    'dealingRfdCnt': '处理中的退款笔数',
    'dealingRfdCntRate': '处理中的笔数占比',
    'dealingRfdAmt': '退款金额（元）',
}


def card_dates(start: str = '', end: str = '') -> Tuple[str, List[str]]:
    """四张卡片的时间入参：给了区间 → `('customDate', [start, end])`；否则今天。"""
    if start or end:
        return COMMODITY_DATE_CUSTOM, [start or end, end or start]
    return DATE_TYPE_DAY, []


def card_request(date_type: str = DATE_TYPE_DAY, dates: Optional[List[str]] = None,
                 seller_id: str = '') -> Dict[str, Any]:
    """构造四张卡片的入参（键名逐字照 bundle；空值**不放键**，等价于官方的 `undefined`）。

    官方那份 `data:{...}` 里有 `dateRange: undefined` / `ms: undefined`，经
    `JSON.stringify` 会整键消失，所以这里只在对应 `dateType` 下才放键 —— 少放键与
    「放一个空串」在平台上不是一回事。
    """
    payload: Dict[str, Any] = {'dateType': date_type or DATE_TYPE_DAY}
    if seller_id:
        payload['selectedSellerId'] = str(seller_id)
    if date_type == COMMODITY_DATE_CUSTOM and dates:
        payload['dateRange'] = '|'.join(dates)
    if date_type == DATE_TYPE_MONTH and dates:
        payload['ms'] = dates[0]
    return payload


def card_labels(kind: str, seller_id: str = '') -> Dict[str, str]:
    """这一张卡片该用哪张官方标签表。

    交易数据卡（`flow.detail`）没有独立标签表，它的指标名与经营概况是同一套平台名
    （`payAmt`/`payOrdCnt`/`aov`/`rfdAmt` …），所以复用经营概况的表；表里没有的键
    返回 `label=None`，前端显示平台字段名 —— **不猜中文**。
    """
    if kind == 'refund':
        return dict(REFUND_LABELS)
    if kind == 'repurchase':
        return dict(REPURCHASE_LABELS)
    return dict(SELLER_LABELS_SINGLE if seller_id else SELLER_LABELS)


def _metric_entry(key: str, value: Any, labels: Dict[str, str]) -> Dict[str, Any]:
    """把一个平台指标变成我们的条目。**绝不造值**：拿不到就是 `None`。

    平台的指标对象形如 `{name, data, ratioFormat, dataFormat, cycle, lastDataFormat}`；
    也有直接给标量的（`flow.detail` 的部分键）。两种都收，原样保留能拿到的部分。
    """
    entry: Dict[str, Any] = {
        'key': key,
        'label': labels.get(key),
        'value': None,
        'ratio': None,
        'ratio_format': None,
        'cycle': None,
        'last': None,
        'platform_name': None,
        'raw': value,
    }
    if isinstance(value, dict):
        name = value.get('name')
        entry['platform_name'] = name if isinstance(name, str) and name.strip() else None
        data = value.get('data')
        if _is_number(data) or isinstance(data, str):
            entry['value'] = data
        for field, target in (('ratioFormat', 'ratio'), ('dataFormat', 'ratio_format'),
                              ('cycle', 'cycle'), ('lastDataFormat', 'last')):
            raw = value.get(field)
            if isinstance(raw, str) and raw.strip():
                entry[target] = raw
    elif _is_number(value) or isinstance(value, str):
        entry['value'] = value
    return entry


def _card_body(payload: Any) -> Dict[str, Any]:
    """卡片响应体：`_mtop` 已经剥掉客户端外层信封，所以这里是 `{extendInfo, data}`。"""
    if isinstance(payload, dict):
        body = payload.get('data')
        if isinstance(body, dict):
            return body
    return {}


def _bench_series(body: Dict[str, Any], labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """`graphDataList` → 每个指标一条折线（官方每行是 `{ds, <指标名>: 值}`）。"""
    bench = body.get('graphBannerBenchData')
    if not isinstance(bench, dict):
        return []
    rows = bench.get('graphDataList')
    if not isinstance(rows, list):
        return []
    order: List[str] = []
    points: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get('ds')
        for key, value in row.items():
            if key == 'ds':
                continue
            if key not in points:
                points[key] = []
                order.append(key)
            if _is_number(value) or isinstance(value, str):
                points[key].append({'date': date, 'value': value})
    return [{'key': key, 'label': labels.get(key), 'points': points[key]} for key in order]


def _banner_entries(body: Dict[str, Any], labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """`graphBannerBenchData.bannerDataList` → 卡片顶部那排指标（官方按 `name` 认）。"""
    bench = body.get('graphBannerBenchData')
    if not isinstance(bench, dict):
        return []
    rows = bench.get('bannerDataList')
    if not isinstance(rows, list):
        return []
    entries: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get('name')
        if not isinstance(key, str) or not key.strip():
            continue
        entries.append(_metric_entry(key, row, labels))
    return entries


def parse_card(payload: Any, kind: str, labels: Dict[str, str]) -> Dict[str, Any]:
    """解析四张卡片中的一张。字段拿不到就是 `None`/空列表，**不补 0、不编标题**。

    - `seller` / `repurchase`：`graphBannerBenchData.bannerDataList`（顶部指标）+
      `graphDataList`（折线）+ 可选 `promotionInfoList`（经营概况的推广位，原样透传）。
    - `trade`：`data` 里除 `itemFlowTransferData` 外的键当指标，`itemFlowTransferData`
      原样透传（官方把它交给另一个子组件渲染）。
    - `refund`：`data` 里的每个键都是一个指标对象。
    """
    body = _card_body(payload)
    labels = dict(labels or {})
    banner = _banner_entries(body, labels)
    series = _bench_series(body, labels)
    metrics: List[Dict[str, Any]] = []
    extra: Dict[str, Any] = {}
    if kind == 'refund':
        for key, value in body.items():
            metrics.append(_metric_entry(key, value, labels))
    else:
        for key, value in body.items():
            if key == 'graphBannerBenchData':
                continue
            if isinstance(value, (dict, list)):
                # 不是「一个指标」的东西（`itemFlowTransferData`、`promotionInfoList` …）
                # 原样放进 `extra`：既不当指标渲染，也不丢掉 —— 平台加字段时能看见。
                extra[key] = value
                continue
            metrics.append(_metric_entry(key, value, labels))
    promotion = body.get('promotionInfoList')
    return {
        'kind': kind,
        'title': CARD_TITLES.get(kind, kind),
        'api': CARD_APIS.get(kind, ''),
        'metrics': metrics,
        'banner': banner,
        'series': series,
        'promotion': promotion if isinstance(promotion, list) else None,
        'extra': extra,
        'real_date_range': _real_date_range(payload),
        'found': bool(body),
    }


async def card(client: Any, kind: str = 'trade', start: str = '', end: str = '',
               seller_id: str = '') -> Dict[str, Any]:
    """四张经营卡片（**只读**）。`kind` 不认识时抛 `ValueError`（服务层转 400）。

    **平台「拒绝访问」是有效答复，不是故障**（与 `_collect` 同一口径）：真机上这个账号对
    四张卡都回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。若照抛，界面上会同时出现
    四个红框、每个框里都是 `协议层调用失败: MTOP 调用失败 [api]: FAIL_BIZ_…` 的原文，
    而同一页的核心指标却是「人话 + 一屏 -」—— 同一页两套口径。所以这里只把**无权限**
    翻成 `errors`（服务层给人话 warning，200），其余（登录态过期、链路、风控）照抛。
    """
    if kind not in CARD_APIS:
        raise ValueError('不支持的数据卡片：%s（可选 %s）' % (kind, '/'.join(CARD_KINDS)))
    date_type, dates = card_dates(start, end)
    request = card_request(date_type=date_type, dates=dates, seller_id=seller_id)
    errors: Dict[str, str] = {}
    payload: Any = None
    try:
        payload = await _mtop(client, CARD_APIS[kind], dict(request))
    except Exception as exc:  # noqa: BLE001 - 只放行「无权限」，其余照抛
        if not _is_permission_error(exc):
            raise
        errors[kind] = _error_text(exc)
    if payload is None:
        parsed: Dict[str, Any] = {
            'kind': kind,
            'title': CARD_TITLES.get(kind, kind),
            'api': CARD_APIS.get(kind, ''),
            'metrics': [],
            'banner': [],
            'series': [],
            'promotion': None,
            'extra': {},
            'real_date_range': None,
            'found': False,
        }
    else:
        parsed = parse_card(payload, kind, card_labels(kind, seller_id))
    parsed.update({
        'date_type': date_type,
        'dates': dates,
        'raw': payload,
        'errors': errors,
    })
    return parsed


# ---------------------------------------------------------------------------
# 客服数据页的三张明细表：客服明细 / 订单分流明细 / 客户评价明细 —— 只读
# ---------------------------------------------------------------------------
#: 三个列表接口（bundle 静态提取，偏移 2257163 起）。官方包装 `wge(api, data)`：
#: `xge({api, v:'1.0', data:{...t}, showErrorToast:true})`，并按
#: `api + ':' + JSON.stringify(data)` 做**在途去重**（同一请求没回来前不重复发）。
#: 另有导出族（`cs.detail.export` / `cs.diversion.export` / `cs.evaluation.export`
#: 与 `cs.excel.url`）**故意不做**：那要轮询生成 Excel 文件。
CS_DETAIL_API = 'mtop.alibaba.idle.seller.pc.datacompass.cs.detail.list'
CS_DIVERSION_API = 'mtop.alibaba.idle.seller.pc.datacompass.cs.diversion.list'
CS_EVALUATION_API = 'mtop.alibaba.idle.seller.pc.datacompass.cs.evaluation.list'

CS_LIST_APIS: Dict[str, str] = {
    'detail': CS_DETAIL_API,
    'diversion': CS_DIVERSION_API,
    'evaluation': CS_EVALUATION_API,
}
CS_LIST_KINDS: Tuple[str, ...] = ('detail', 'diversion', 'evaluation')
CS_LIST_TITLES: Dict[str, str] = {
    'detail': '客服明细',
    'diversion': '订单分流明细',
    'evaluation': '客户评价明细',
}

#: 官方表头（`dataIndex` + 中文标题，逐字取自 bundle 的列定义）。**不翻译、不增删**：
#: 平台加列我们照原样多显示一列，平台改列名我们跟着改 —— 写死一份映射表，平台改版后
#: 就会「标题是旧的、数据是新的」，那比少显示一列难发现得多。
CS_LIST_COLUMNS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    'detail': (
        ('acctName', '账号名称'),
        ('consultUv', '咨询人数'),
        ('visitorCnt', '接待人数'),
        ('consultItmCnt', '咨询商品数'),
        ('avgRespTm', '平均响应时长'),
        ('custSatCnt', '客户服务满意度'),
        ('custSatRate', '客户服务满意率'),
        ('validReviewCnt', '有效评价数'),
        ('validPosReviewCnt', '有效好评数'),
        ('validNegReviewCnt', '有效差评数'),
        ('respRate3min', '3分钟响应率'),
        ('respRate1min', '1分钟响应率'),
    ),
    'diversion': (
        ('divTp', '分流类型'),
        ('dtlTp', '明细分类'),
        ('sessCnt', '会话数'),
    ),
    'evaluation': (
        ('buyerUserName', '客户名称'),
        ('buyerNick', '客户nick'),
        ('subUserName', '被评价的客服'),
        ('reviewTime', '评价时间'),
        ('reviewScore', '评分'),
        ('dissatReason', '不满意原因'),
    ),
}

CS_LIST_PAGE_SIZES: Tuple[int, ...] = (10, 20, 50)
CS_LIST_DEFAULT_PAGE_SIZE = 10


def cs_list_request(kind: str, date_type: str = DATE_TYPE_DAY,
                    dates: Optional[List[str]] = None, seller_id: str = '',
                    order_by: str = '', order: str = '', page: int = 1,
                    page_size: int = CS_LIST_DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
    """官方入参逐字（bundle 调用点 2387031 / 2372357 等）：

    `{selectedSellerId, dateRange, ms, dateType, orderBy, order, page, pageSize}`。

    `dateRange` 只在自定义区间时给（`'|'.join(dates)`，**竖线**），`ms` 只在
    `dateType == 'month'` 时给首日 —— 官方那两处用的是 `undefined`（键根本不在），
    所以这里给空串而不是 `None`：`None` 会被序列化成 `null`，那是另一种语义。
    """
    if kind not in CS_LIST_APIS:
        raise ValueError(f'未知的客服明细类型：{kind}')
    values = [str(item) for item in (dates or []) if str(item).strip()]
    return {
        'selectedSellerId': seller_id,
        'dateRange': '|'.join(values) if date_type == COMMODITY_DATE_CUSTOM else '',
        'ms': values[0] if date_type == DATE_TYPE_MONTH and values else '',
        'dateType': date_type,
        'orderBy': order_by,
        'order': order,
        'page': page,
        'pageSize': page_size,
    }


def parse_cs_list(payload: Any, kind: str) -> Dict[str, Any]:
    """解析客服明细响应。**注意双层 `data`**。

    `_mtop` 已经剥掉客户端信封，剩下平台体 `{extendInfo, data}`；业务码与列表都在
    `payload['data']['data']` 里（官方前端写的是 `v.data.data.data.list`）。
    列表键认不出 → `rows=None`（**不是 `[]`**：「平台没给列表」与「这段时间一条都没有」
    对用户是两件事）；`total` 只认数值，`0` 保留。
    """
    result: Dict[str, Any] = {'rows': None, 'total': None, 'found_key': None}
    body = payload.get('data') if isinstance(payload, dict) else None
    inner = body.get('data') if isinstance(body, dict) else None
    if isinstance(inner, dict):
        code = body.get('code')
        if code in (None, '') or str(code).lower() == 'success':
            for key in ('list', 'rows', 'result'):
                value = inner.get(key)
                if isinstance(value, list):
                    result['rows'] = value
                    result['found_key'] = key
                    break
            total = inner.get('total')
            if _is_number(total):
                result['total'] = int(total)
    result['columns'] = CS_LIST_COLUMNS[kind]
    result['real_date_range'] = _real_date_range(payload)
    return result


async def cs_list(client: Any, kind: str = 'detail', start: str = '', end: str = '',
                  seller_id: str = '', order_by: str = '', order: str = '',
                  page: int = 1,
                  page_size: int = CS_LIST_DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
    """客服数据页的一张明细表（**只读**）。

    默认 `recent1d`（今天，与官方一致）；给了区间就 `customDate` + `dateRange`。

    **平台「拒绝访问」是有效答复，不是故障**（与 `card` / `_collect` 同一口径）：真机上
    这个账号对客服明细也回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`。照抛的话界面上
    会出现红框 + mtop 原文，而同一页的核心指标是「人话 + 一屏 -」—— 同一页两套口径。
    所以这里只把**无权限**翻成 `errors`（服务层给人话 warning，200），其余照抛。
    """
    if kind not in CS_LIST_APIS:
        raise ValueError('不支持的客服明细类型：%s（可选 %s）'
                         % (kind, '/'.join(CS_LIST_KINDS)))
    date_type, dates = commodity_dates(start, end)
    request = cs_list_request(kind=kind, date_type=date_type, dates=dates, seller_id=seller_id,
                              order_by=order_by, order=order, page=page, page_size=page_size)
    errors: Dict[str, str] = {}
    payload: Any = None
    try:
        payload = await _mtop(client, CS_LIST_APIS[kind], dict(request))
    except Exception as exc:  # noqa: BLE001 - 只放行「无权限」，其余照抛
        if not _is_permission_error(exc):
            raise
        errors[kind] = _error_text(exc)
    parsed = parse_cs_list(payload, kind)
    return {
        'kind': kind,
        'rows': parsed['rows'],
        'columns': parsed['columns'],
        'total': parsed['total'],
        'found_key': parsed['found_key'],
        'real_date_range': parsed['real_date_range'],
        'date_type': date_type,
        'dates': dates,
        'raw': payload,
        'errors': errors,
    }
