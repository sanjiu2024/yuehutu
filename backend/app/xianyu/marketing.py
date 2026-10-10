"""营销页协议层（**只读**）。

对应官方卖家工作台 `/marketing`（bundle `seller-marketing@0.0.1`）。四条只读接口：

======================  ==============================================================
标签                     mtop 接口
======================  ==============================================================
``item_metrics``        ``mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list``
``tasks``               ``mtop.taobao.idle.bpro.marketing.info.enhance.query.pc``
``competitiveness``     ``mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query``
``operation_records``   ``mtop.taobao.idle.bpro.marketing.operation.record.list.pc``
======================  ==============================================================

写接口（``mtop.taobao.idle.bpro.marketing.task.operate.pc`` /
``mtop.taobao.idle.bpro.marketing.task.batch.operate.pc``）只登记进 :data:`WRITE_APIS`，
**本模块不实现、也没有任何调用点** —— 营销页的投放/报名是花钱的动作，本任务只复刻只读面。

三条硬口径（与已交付的其它复刻页完全一致）：

1. **平台没返回的指标一律 ``None``**，前端显示「-」，**禁止 0 顶替**（0 是真实值）。
   取值全部走 :func:`find_number`，它找不到时返回 ``None``，不会兜 0。
2. **``FAIL_BIZ_IDLE_USER_UNAUTHORIZED`` 是「无权限」，不是掉线**：这类失败**不写**
   ``need_relogin``。若**所有**接口都因无权限失败，那是平台给的有效答复 —— 如实返回
   一屏 ``None`` + 人话 warning（200），而不是 502（见 :func:`_collect`）。
3. **只读**：没有 POST/PUT/PATCH/DELETE。

证据口径：接口名、参数键、响应字段全部来自官方 bundle
（``https://g.alicdn.com/idle-pc/seller-marketing/0.0.1/marketing-main.js``，3,516,103
字符**单行**压缩 JS；本地落盘 ``C:\\Users\\Administrator\\.dsh-tmp\\adapp\\marketing-main.js``）。
**行号无意义**，逐条证据（字符偏移 + 复现用字符串 + 置信度）登记在
``docs/contracts/marketing.md``。**响应字段是从压缩后的消费代码反推的**（「页面怎么读」），
不是从响应样本来的（「服务端怎么答」），所以四个接口置信度**全部只有「中」**。

公共包装函数（偏移 1433790）为四个接口统一了
``v:"1.0"`` / ``type:"POST"`` / ``appKey:"34839810"`` / ``dataType:"json"``，
**业务字段直接放 ``data``**（与 ``xianyu/shop.py`` 同族，**不是** ``client.py`` 里
``idle.ad.*`` 那套 ``requestJson`` 包装）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- 接口常量

#: 商品数据罗盘指标（只读）
MARKET_ITEM_LIST_API = 'mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list'

#: 营销信息（增强查询，只读）
MARKET_INFO_API = 'mtop.taobao.idle.bpro.marketing.info.enhance.query.pc'

#: 价格竞争力（只读）
#
# 名字必须带 `bpro.marketing.` 这一段：官方营销微应用 bundle
# （`marketing-main.js`，3,516,103 字符）里
# `mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query` 出现 1 次
# （偏移 1434255），而短名 `mtop.taobao.idle.item.price.competitiveness.info.query`
# 出现 **0 次** —— 照短名实现，真机会直接回 `FAIL_SYS_API_NOT_FOUNDED`。
PRICE_COMPETITIVENESS_API = 'mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query'

#: 操作记录（只读）。同上，`bpro.marketing.` 不能省：长名 1 次（偏移 1436108）、短名 0 次。
OPERATION_RECORD_API = 'mtop.taobao.idle.bpro.marketing.operation.record.list.pc'

#: **只登记、不实现**的写接口。存在这个元组只是为了「名字有据可查」+ 测试能钉住
#: 「本模块一个写接口都没调」。任何代码路径都不许取用它里面的元素去发请求。
WRITE_APIS: Tuple[str, ...] = (
    'mtop.taobao.idle.bpro.marketing.task.operate.pc',
    'mtop.taobao.idle.bpro.marketing.task.batch.operate.pc',
)

# --------------------------------------------------------------------------- 参数枚举

#: 接口 1 的时间范围。默认 ``recent1d``（官方页面 `useState("recent1d")`）。
DATE_TYPES: Tuple[str, ...] = ('recent1d', 'recent7d', 'recent30d', 'customDate')
DEFAULT_DATE_TYPE = 'recent1d'
#: ``customDate`` 时 ``dateRange`` 的形状：``YYYYMMDD|YYYYMMDD``（竖线分隔）。
CUSTOM_DATE_RANGE_SEPARATOR = '|'
#: 官方 UI 只允许「今天 - 90 天 ~ 今天」。
CUSTOM_DATE_MAX_BACK_DAYS = 90

#: 接口 2 / 3 共用的业务场景枚举（bundle `Qk`，只有这 2 个值）。
BIZ_SCENES: Tuple[str, ...] = ('TRAFFIC_DRIVEN_REPRICING', 'TRAFFIC_ORDER_BOOST')
DEFAULT_BIZ_SCENE = 'TRAFFIC_ORDER_BOOST'

#: 接口 2 的查询场景全集（bundle `Yk`，15 个值逐字抄）。
QUERY_SCENES: Tuple[str, ...] = (
    'DEFAULT',
    'OPPORTUNITY_ITEM_RECOMMEND',
    'DISCOUNT_BOOST_SET',
    'DISCOUNT_BOOSTING_TASK',
    'DISCOUNT_BOOSTED_TASK',
    'ORDER_BOOST_FIRST_PAGE_BOOSTING',
    'ORDER_BOOST_FIRST_PAGE_PAUSE',
    'ORDER_BOOST_SECOND_PAGE_BOOSTING',
    'ORDER_BOOST_SECOND_PAGE_CAN_SIGN_UP',
    'ORDER_BOOST_SET',
    'ORDER_BOOSTING_SET',
    'ORDER_BOOSTING_SET_PAUSE',
    'ADMITTANCE_CHECK',
    'ORDER_BOOST_OPERATION_RECORD',
    'ORDER_BOOST_SECOND_PAGE_CAN_SIGN_UP_FILTER',
)
DEFAULT_QUERY_SCENE = 'ORDER_BOOST_SET'
#: 接口 4（操作记录）唯一调用点用的 queryScene。
RECORD_QUERY_SCENE = 'ORDER_BOOST_OPERATION_RECORD'

#: 写接口的 ``operation`` 字符串枚举（bundle `Wk`）—— **只登记**。
OPERATIONS: Tuple[str, ...] = (
    'TOB_START_TASK', 'TOB_ADJUST_PRICE', 'TOB_ADJUST_COMMISSION', 'TOB_PAUSE_TASK',
    'TOB_RESUME_TASK', 'TOB_ABORT_TASK', 'TDR_START_TASK', 'TDR_ADJUST_PRICE',
)
#: 孪生数值枚举（bundle `Kk`，与 `Wk` 并存）—— **只登记**。
OPERATION_CODES: Dict[str, int] = {
    'TOB_START_TASK': 1000,
    'TOB_ADJUST_PRICE': 1001,
    'TOB_ADJUST_COMMISSION': 1002,
    'TOB_PAUSE_TASK': 1003,
    'TOB_RESUME_TASK': 1004,
    'TOB_ABORT_TASK': 1005,
    'TOB_SCHEDULER_PAUSE': 1006,
    'TOB_SCHEDULER_COMMISSION': 1007,
    'TDR_START_TASK': 2000,
    'TDR_ADJUST_PRICE': 2001,
}

#: 接口 2 的分页默认值（三处独立佐证：`PB` 与两张 tab 常量表）。
DEFAULT_PAGE_NUM = 1
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50

# --------------------------------------------------------------------------- 契约键

#: 契约键元组。**前端能依赖的全部键就是这些**：平台没给的键也一定出现，值为 ``None``
#: （前端显示「-」）。服务层按这些元组校验协议层有没有偷懒（缺键 = 502）。
#:
#: 接口 1：响应取 ``data.list[0]``，指标键就是平台字段名（bundle 指标枚举表 `uj`）。
ITEM_METRICS: Tuple[str, ...] = (
    'show_pv', 'ipv', 'show_uv', 'ipv_uv', 'pay_qty', 'pay_amt', 'net_pay_amt', 'est_pay_fee',
)
#: 接口 2：响应取 ``result.taskPageResult || result.itemPageResult``。
TASK_METRICS: Tuple[str, ...] = ('total',)
#: 接口 2 的原样透传标志位：``nextPage`` 可能是 bool 也可能是页码（**未验证**），别猜类型。
TASK_FLAGS: Tuple[str, ...] = ('next_page',)
#: 接口 3：响应取 ``result.trafficBoostStr``（**字符串**，不是数字）。
COMPETITIVENESS_TEXTS: Tuple[str, ...] = ('traffic_boost_text',)
#: 接口 4：响应取 ``result.result``（数组）。**条目内部字段零证据**，故无契约键。
RECORD_METRICS: Tuple[str, ...] = ()

#: 别名表：平台字段名（小写）→ 我方契约键。键名尽量与既有页面共用词根。
METRIC_ALIASES: Dict[str, Tuple[str, ...]] = {
    'show_pv': ('showpv',),
    'ipv': ('ipv',),
    'show_uv': ('showuv',),
    'ipv_uv': ('ipvuv',),
    'pay_qty': ('payqty',),
    'pay_amt': ('payamt',),
    'net_pay_amt': ('netpayamt',),
    'est_pay_fee': ('estpayfee',),
    'total': ('total',),
}

#: 字符串类契约键的别名表。
TEXT_ALIASES: Dict[str, Tuple[str, ...]] = {
    'traffic_boost_text': ('trafficbooststr',),
}

#: 原样透传类契约键的别名表。
FLAG_ALIASES: Dict[str, Tuple[str, ...]] = {
    'next_page': ('nextpage',),
}

#: 列表字段的候选名（按别名找第一个非空 list）。接口 1 是 `data.list`，
#: 接口 2 是 `<pageResult>.result`，接口 4 是 `result.result`。
LIST_ALIASES: Tuple[str, ...] = ('list', 'result', 'items', 'records')

#: 响应里像指标、却一个别名都没命中的字段名（只提示，不参与取值）。
_METRIC_HINTS: Tuple[str, ...] = (
    'count', 'qty', 'amt', 'pv', 'uv', 'num', 'total', 'score', 'rate',
)

# --------------------------------------------------------------------------- 通用取值


def walk_dicts(value: Any, depth: int = 0) -> Iterator[Dict[str, Any]]:
    """深度优先遍历嵌套 dict/list，产出每一个 dict（指标常埋在 `data.data` 里）。"""
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
    """``bool`` 不是数字 —— 否则 ``True`` 会把「没数据」悄悄变成 1。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def find_value(payload: Any, aliases: Tuple[str, ...]) -> Any:
    """按别名找**原样**值（不转型、不过滤空串），找不到返回 ``None``。"""
    wanted = {str(key).lower() for key in aliases}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and value is not None:
                return value
    return None


def find_number(payload: Any, aliases: Tuple[str, ...]) -> Optional[float]:
    """按别名找第一个数值。找不到返回 ``None``（**绝不返回 0**）。"""
    wanted = {str(key).lower() for key in aliases}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and _is_number(value):
                return float(value)
    return None


def find_text(payload: Any, aliases: Tuple[str, ...]) -> Optional[str]:
    """按别名找第一个非空字符串。找不到返回 ``None``。"""
    wanted = {str(key).lower() for key in aliases}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and isinstance(value, str) and value.strip():
                return value.strip()
    return None


def find_list(payload: Any, aliases: Tuple[str, ...] = ()) -> Optional[List[Any]]:
    """按别名找第一个非空 list。找不到返回 ``None``（不是 ``[]``）。

    返回 ``[]`` 会被前端理解成「平台说这里没有数据」，而 ``None`` 才是「没拿到」。
    """
    wanted = {str(key).lower() for key in (aliases or LIST_ALIASES)}
    for mapping in walk_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in wanted and isinstance(value, list) and value:
                return value
    return None


def extract_metrics(payload: Any, keys: Tuple[str, ...]) -> Dict[str, Optional[float]]:
    """把平台响应翻成「契约键 → 数值 / None」。认不出的一律 None。"""
    return {key: find_number(payload, METRIC_ALIASES.get(key, (key,))) for key in keys}


def extract_texts(payload: Any, keys: Tuple[str, ...]) -> Dict[str, Optional[str]]:
    """把平台响应翻成「契约键 → 字符串 / None」。"""
    return {key: find_text(payload, TEXT_ALIASES.get(key, (key,))) for key in keys}


def extract_flags(payload: Any, keys: Tuple[str, ...]) -> Dict[str, Any]:
    """原样透传的标志位（不猜类型）。"""
    return {key: find_value(payload, FLAG_ALIASES.get(key, (key,))) for key in keys}


def suspect_keys(payload: Any, keys: Tuple[str, ...]) -> List[str]:
    """响应里像指标、却一个别名都没命中的字段名（只用于提示，不参与取值）。"""
    known = set()
    for key in keys:
        known.update(str(item).lower() for item in METRIC_ALIASES.get(key, (key,)))
        known.update(str(item).lower() for item in TEXT_ALIASES.get(key, (key,)))
        known.update(str(item).lower() for item in FLAG_ALIASES.get(key, (key,)))
    found: List[str] = []
    for mapping in walk_dicts(payload):
        for name in mapping:
            low = str(name).lower()
            if low in known or low in found:
                continue
            if any(hint in low for hint in _METRIC_HINTS):
                found.append(low)
    return found[:8]


# --------------------------------------------------------------------------- 参数构造
#
# 键名全部来自官方 bundle 的调用点，逐字照抄（**不要**改成 `sellerId` 之类猜测键）。


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def item_list_data(item_id: Any, date_type: str = DEFAULT_DATE_TYPE,
                   date_range: str = '') -> Dict[str, Any]:
    """接口 1 的 data：``itmId``（**官方拼写少一个 e**，偏移 1775069）/ ``dateType`` /
    ``dateRange``（预设时 ``""``；``customDate`` 时 ``"YYYYMMDD|YYYYMMDD"``）。"""
    return {
        'itmId': _text(item_id),
        'dateType': date_type or DEFAULT_DATE_TYPE,
        'dateRange': date_range or '',
    }


def info_data(biz_scene: str = DEFAULT_BIZ_SCENE, query_scene: str = DEFAULT_QUERY_SCENE,
              page_num: int = DEFAULT_PAGE_NUM, page_size: int = DEFAULT_PAGE_SIZE,
              item_ids: Any = None) -> Dict[str, Any]:
    """接口 2 的 data：``{bizScene, queryScene, pageNum, pageSize[, itemIdListStr]}``。

    ``itemIdListStr`` 是 **JSON 字符串**（偏移 2142992），不是数组。
    """
    data: Dict[str, Any] = {
        'bizScene': biz_scene or DEFAULT_BIZ_SCENE,
        'queryScene': query_scene or DEFAULT_QUERY_SCENE,
        'pageNum': int(page_num),
        'pageSize': int(page_size),
    }
    ids = [_text(value) for value in (item_ids or []) if _text(value)]
    if ids:
        data['itemIdListStr'] = json.dumps(ids, ensure_ascii=False)
    return data


def competitiveness_data(biz_scene: str, item_id: Any, sku_prices: Any = None,
                         commission_rate: Any = None) -> Dict[str, Any]:
    """接口 3 的 data：``{bizScene, itemId, currentSkuPriceMapStr[, currentCommissionRate]}``。

    ``currentSkuPriceMapStr`` 是 **JSON 字符串** ``{"<skuId>":<priceYuan>}``（偏移 1579913）；
    键名 ``skuId`` 是**推断**、值的单位（元 vs 分）**未验证**。
    ``currentCommissionRate`` 只在 ``TRAFFIC_ORDER_BOOST`` 出现。
    """
    prices = {str(key): value for key, value in (sku_prices or {}).items()}
    data: Dict[str, Any] = {
        'bizScene': biz_scene or DEFAULT_BIZ_SCENE,
        'itemId': _text(item_id),
        'currentSkuPriceMapStr': json.dumps(prices, ensure_ascii=False),
    }
    if _text(commission_rate) != '':
        data['currentCommissionRate'] = commission_rate
    return data


def record_data(task_id: Any, biz_scene: str = DEFAULT_BIZ_SCENE,
                query_scene: str = RECORD_QUERY_SCENE) -> Dict[str, Any]:
    """接口 4 的 data：``{taskId, bizScene, queryScene}`` —— **无分页、无时间范围**。

    整块由 ``taskId`` 一次性拉全量（bundle 里唯一 1 个调用点）。
    """
    return {
        'taskId': _text(task_id),
        'bizScene': biz_scene or DEFAULT_BIZ_SCENE,
        'queryScene': query_scene or RECORD_QUERY_SCENE,
    }


# --------------------------------------------------------------------------- 调用与容错


async def _mtop(client: Any, api: str, data: Any, envelope_key: str = 'data') -> Any:
    """调一次 mtop 并剥掉外层信封。

    接口 1 的数据在 ``data``（``{"code":"success","data":{...}}``，与
    ``xianyu/datacenter.py`` 同款信封）；接口 2/3/4 在 ``result``。信封里**没有**该键时
    原样返回整个信封 —— 宁可让上层取到错位，也不要静默丢数据。

    ``client.mtop`` 失败时抛 ``XianyuError``（含 ``FAIL_*`` 原文），这里**不吞**：
    服务层要靠原文判断「无权限 / 未登录 / 风控」。
    """
    result = await client.mtop(api, data)
    if isinstance(result, dict) and envelope_key in result:
        inner = result.get(envelope_key)
        return inner if inner is not None else {}
    return result


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


async def _collect(client: Any, calls: Tuple[Tuple[str, str, Any, str], ...]):
    """逐个调平台接口，返回 ``(raw, errors)``。

    容错分两层（与 ``xianyu/datacenter.py::_collect`` 同口径）：

    * **单个接口坏掉只影响它自己**，其余照常；
    * **全坏**时再看坏在哪：全是「平台拒绝访问」→ 如实返回空 raw + 人话 warning（200）；
      只要有一条是链路 / 签名 / 登录态故障，就抛**最严重**的那个原始异常，让
      ``common.call_failed`` 给出 502 或 409 + ``need_relogin``。

    每个 ``calls`` 元素是 ``(label, api, data, envelope_key)``。
    """
    raw: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    failed: Dict[str, BaseException] = {}
    for label, api, data, envelope_key in calls:
        try:
            raw[label] = await _mtop(client, api, data, envelope_key)
        except Exception as exc:  # noqa: BLE001 - 逐接口容错，原文进 errors
            errors[label] = _error_text(exc)
            failed[label] = exc
    if failed and len(failed) == len(calls):
        if not all(_is_permission_error(exc) for exc in failed.values()):
            raise _pick_exc(failed)
    return raw, errors


def _blank(keys: Tuple[str, ...]) -> Dict[str, Any]:
    return {key: None for key in keys}


# --------------------------------------------------------------------------- 四个只读页面
#
# 命名与路由对齐：`item_metrics` / `tasks` / `competitiveness` / `operation_records`
# 就是 `GET /api/marketing/{account_id}/<name>` 里的 `<name>`（服务层用同一个字符串找
# 这里的函数，见 `services/common.py::protocol_module`）。


async def item_metrics(client: Any, item_id: Any, date_type: str = DEFAULT_DATE_TYPE,
                       date_range: str = '') -> Dict[str, Any]:
    """接口 1：商品数据罗盘指标（**只读**）。响应取 ``data.list[0]``。"""
    raw, errors = await _collect(client, (
        ('item_metrics', MARKET_ITEM_LIST_API,
         item_list_data(item_id, date_type, date_range), 'data'),
    ))
    payload = raw.get('item_metrics')
    rows = find_list(payload, ('list',))
    head = rows[0] if rows and isinstance(rows[0], dict) else {}
    return {
        'metrics': extract_metrics(head, ITEM_METRICS) if payload is not None
        else _blank(ITEM_METRICS),
        'texts': {},
        'flags': {},
        'items': rows,
        'suspect_keys': suspect_keys(payload, ITEM_METRICS),
        'errors': errors,
        'raw': raw,
    }


async def tasks(client: Any, biz_scene: str = DEFAULT_BIZ_SCENE,
                query_scene: str = DEFAULT_QUERY_SCENE, page_num: int = DEFAULT_PAGE_NUM,
                page_size: int = DEFAULT_PAGE_SIZE, item_ids: Any = None) -> Dict[str, Any]:
    """接口 2：任务 / 商品分页（**只读**）。

    响应取 ``result.taskPageResult || result.itemPageResult``（一个答任务分页、一个答商品
    分页），两者都带 ``result``（数组）/ ``total`` / ``nextPage``；另有
    ``result.userTrafficTaskInfo``，原样带上。
    """
    raw, errors = await _collect(client, (
        ('tasks', MARKET_INFO_API,
         info_data(biz_scene, query_scene, page_num, page_size, item_ids), 'result'),
    ))
    payload = raw.get('tasks')
    page: Dict[str, Any] = {}
    page_kind: Optional[str] = None
    if isinstance(payload, dict):
        for branch, label in (('taskPageResult', 'task'), ('itemPageResult', 'item')):
            if isinstance(payload.get(branch), dict):
                page, page_kind = payload[branch], label
                break
    rows = page.get('result') if isinstance(page.get('result'), list) else None
    return {
        'metrics': extract_metrics(page, TASK_METRICS) if payload is not None
        else _blank(TASK_METRICS),
        'texts': {},
        'flags': extract_flags(page, TASK_FLAGS) if payload is not None
        else _blank(TASK_FLAGS),
        'tasks': rows or None,
        'page_kind': page_kind,
        'user_task_info': payload.get('userTrafficTaskInfo') if isinstance(payload, dict) else None,
        'suspect_keys': suspect_keys(page, TASK_METRICS + TASK_FLAGS),
        'errors': errors,
        'raw': raw,
    }


async def competitiveness(client: Any, biz_scene: str, item_id: Any,
                          sku_prices: Any = None,
                          commission_rate: Any = None) -> Dict[str, Any]:
    """接口 3：价格竞争力（**只读**）。响应取 ``result.trafficBoostStr``（字符串）。"""
    raw, errors = await _collect(client, (
        ('competitiveness', PRICE_COMPETITIVENESS_API,
         competitiveness_data(biz_scene, item_id, sku_prices, commission_rate), 'result'),
    ))
    payload = raw.get('competitiveness')
    return {
        'metrics': {},
        'texts': extract_texts(payload, COMPETITIVENESS_TEXTS) if payload is not None
        else _blank(COMPETITIVENESS_TEXTS),
        'flags': {},
        'items': None,
        'suspect_keys': suspect_keys(payload, COMPETITIVENESS_TEXTS),
        'errors': errors,
        'raw': raw,
    }


async def operation_records(client: Any, task_id: Any, biz_scene: str = DEFAULT_BIZ_SCENE,
                            query_scene: str = RECORD_QUERY_SCENE) -> Dict[str, Any]:
    """接口 4：任务操作记录（**只读**）。

    响应取 ``result.result``（数组）。**条目内部字段零证据** —— 原样透传，不解析、不编键。
    """
    raw, errors = await _collect(client, (
        ('operation_records', OPERATION_RECORD_API,
         record_data(task_id, biz_scene, query_scene), 'result'),
    ))
    payload = raw.get('operation_records')
    return {
        'metrics': {},
        'texts': {},
        'flags': {},
        'records': find_list(payload, ('result',)) if payload is not None else None,
        'suspect_keys': suspect_keys(payload, RECORD_METRICS),
        'errors': errors,
        'raw': raw,
    }
