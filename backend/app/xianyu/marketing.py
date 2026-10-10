"""营销页协议层（**只读**）。

对应官方卖家工作台 `/marketing`（bundle `seller-marketing@0.0.1`）。四条只读接口：

======================  ==============================================================
标签                     mtop 接口
======================  ==============================================================
``items``               ``mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list``
``info``                ``mtop.taobao.idle.bpro.marketing.info.enhance.query.pc``
``competitiveness``     ``mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query``
``records``             ``mtop.taobao.idle.bpro.marketing.operation.record.list.pc``
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

证据口径：接口名与响应字段形状见 ``docs/contracts/marketing.md``。**参数键名凡是没拿到
bundle 调用点证据的，都在下面标了 ``# 证据缺口``** —— 不要把它们当成已证实的事实。
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- 接口常量

#: 营销商品列表（只读）
MARKET_ITEM_LIST_API = 'mtop.alibaba.idle.seller.pc.datacompass.user.market.item.list'

#: 营销信息（增强查询，只读）
MARKET_INFO_API = 'mtop.taobao.idle.bpro.marketing.info.enhance.query.pc'

#: 价格竞争力（只读）
#
# 名字必须带 `bpro.marketing.` 这一段：官方营销微应用 bundle
# （`logs/seller_discover/marketing_src/js_main.js`，3,516,103 字节）里
# `mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query` 出现 1 次，
# 而短名 `mtop.taobao.idle.item.price.competitiveness.info.query` 出现 **0 次** ——
# 早先照短名实现，真机会直接回 `FAIL_SYS_API_NOT_FOUNDED`。
PRICE_COMPETITIVENESS_API = 'mtop.taobao.idle.bpro.marketing.item.price.competitiveness.info.query'

#: 操作记录（只读）。同上，`bpro.marketing.` 不能省：长名 1 次、短名 0 次。
OPERATION_RECORD_API = 'mtop.taobao.idle.bpro.marketing.operation.record.list.pc'

#: **只登记、不实现**的写接口。存在这个元组只是为了「名字有据可查」+ 测试能钉住
#: 「本模块一个写接口都没调」。任何代码路径都不许取用它里面的元素去发请求。
WRITE_APIS: Tuple[str, ...] = (
    'mtop.taobao.idle.bpro.marketing.task.operate.pc',
    'mtop.taobao.idle.bpro.marketing.task.batch.operate.pc',
)

# --------------------------------------------------------------------------- 契约键

#: 四个页面的契约键元组。**前端能依赖的全部键就是这些**：平台没给的键也一定出现，
#: 值为 ``None``（前端显示「-」）。服务层按这个元组校验协议层有没有偷懒（缺键 = 502）。
ITEMS_METRICS: Tuple[str, ...] = ('item_count',)
INFO_METRICS: Tuple[str, ...] = ('running_count', 'total_count')
COMPETITIVENESS_METRICS: Tuple[str, ...] = ('low_count', 'high_count', 'avg_score')
RECORDS_METRICS: Tuple[str, ...] = ('record_count',)

#: 别名表：平台字段名 → 我方契约键。**协议层命名未定**（真机拿不到有权限账号的响应），
#: 所以按 `services/item_stats.py::METRIC_ALIASES` 与 `xianyu/datacenter.py` 的同一做法
#: 先认常见几种，认不出就是 None（绝不兜 0）。键名尽量与既有页面共用词根。
METRIC_ALIASES: Dict[str, Tuple[str, ...]] = {
    'item_count': ('itemcount', 'totalcount', 'total', 'count'),
    'running_count': ('runningcount', 'running', 'onlinecount'),
    'total_count': ('totalcount', 'total', 'count'),
    'low_count': ('lowcount', 'lowercount', 'weakcount'),
    'high_count': ('highcount', 'highercount', 'strongcount'),
    'avg_score': ('avgscore', 'averagescore', 'score', 'competitiveness'),
    'record_count': ('recordcount', 'totalcount', 'total', 'count'),
}

#: 列表字段的候选名（按别名找第一个非空 list）。
LIST_ALIASES: Tuple[str, ...] = (
    'list', 'items', 'itemlist', 'records', 'recordlist', 'tasks', 'tasklist', 'data',
)

#: 响应里像指标、却一个别名都没命中的字段名（只提示，不参与取值）。
_METRIC_HINTS: Tuple[str, ...] = (
    'count', 'num', 'total', 'score', 'rate', 'price', 'amount', 'ratio',
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
    return isinstance(value, (int, float)) and not isinstance(value, bool)


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


# --------------------------------------------------------------------------- 参数构造
#
# ⚠️ **证据缺口**：下面四个 `*_data()` 的键名来自官方 bundle 的调用点；本任务在拿到
# bundle 证据前**不得**声称它们已被证实。没证实的一律在 `docs/contracts/marketing.md`
# 的「证据缺口」表里列出来。分页键的真机形状官方是懒加载列表，真机没发出来。


def item_list_data(page: int = 1, page_size: int = 20, seller_id: str = '') -> Dict[str, Any]:
    """营销商品列表的参数。``pageNum`` / ``pageSize`` 与官方页面分页组件同口径。"""
    data: Dict[str, Any] = {'pageNum': int(page), 'pageSize': int(page_size)}
    if seller_id:
        data['sellerId'] = str(seller_id)
    return data


def info_data(seller_id: str = '') -> Dict[str, Any]:
    """营销信息（增强查询）。官方页面进入时只有这一个必填上下文。"""
    data: Dict[str, Any] = {}
    if seller_id:
        data['sellerId'] = str(seller_id)
    return data


def competitiveness_data(item_ids: Any = None, seller_id: str = '') -> Dict[str, Any]:
    """价格竞争力。官方页面按商品维度查，所以把 `itemIds` 透传（空则不带该键）。"""
    data: Dict[str, Any] = {}
    ids = [str(item) for item in (item_ids or []) if str(item).strip()]
    if ids:
        data['itemIds'] = ids
    if seller_id:
        data['sellerId'] = str(seller_id)
    return data


def record_data(page: int = 1, page_size: int = 20, seller_id: str = '') -> Dict[str, Any]:
    """操作记录（只读流水）。"""
    data: Dict[str, Any] = {'pageNum': int(page), 'pageSize': int(page_size)}
    if seller_id:
        data['sellerId'] = str(seller_id)
    return data


# --------------------------------------------------------------------------- 调用与容错


async def _mtop(client: Any, api: str, data: Any) -> Any:
    """调一次 mtop 并剥掉外层信封，返回平台响应里的 ``data``。

    ``client.mtop`` 失败时抛 ``XianyuError``（含 ``FAIL_*`` 原文），这里**不吞**：
    服务层要靠原文判断「无权限 / 未登录 / 风控」。
    """
    result = await client.mtop(api, data)
    if isinstance(result, dict):
        return result.get('data')
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


async def _collect(client: Any, calls: Tuple[Tuple[str, str, Any], ...],
                   keys: Tuple[str, ...]):
    """逐个调平台接口，返回 ``(指标, 原始响应, 错误)``。

    容错分两层（与 ``xianyu/datacenter.py::_collect`` 同口径）：

    * **单个接口坏掉只影响它自己贡献的指标**，其余照常；
    * **全坏**时再看坏在哪：全是「平台拒绝访问」→ 如实返回一屏 ``None`` + 人话
      warning（200）；只要有一条是链路 / 签名 / 登录态故障，就抛**最严重**的那个
      原始异常，让 ``common.call_failed`` 给出 502 或 409 + ``need_relogin``。
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


# --------------------------------------------------------------------------- 四个只读页面
#
# 命名与路由对齐：`items` / `tasks` / `competitiveness` / `records` 就是
# `GET /api/marketing/{account_id}/<name>` 里的 `<name>`（服务层用同一个字符串找这里的
# 函数，见 `services/common.py::protocol_module`）。


async def items(client: Any, page: int = 1, page_size: int = 20,
                seller_id: str = '') -> Dict[str, Any]:
    """营销商品列表：条数 + 原始行（**只读**）。"""
    metrics, raw, errors = await _collect(client, (
        ('items', MARKET_ITEM_LIST_API, item_list_data(page, page_size, seller_id)),
    ), ITEMS_METRICS)
    return {
        'metrics': metrics,
        'items': find_list(raw.get('items')),
        'suspect_keys': suspect_keys(raw.get('items'), ITEMS_METRICS),
        'errors': errors,
        'raw': raw,
    }


async def tasks(client: Any, seller_id: str = '') -> Dict[str, Any]:
    """营销信息（进行中 / 总数）。官方页面进入时只发这一个请求。"""
    metrics, raw, errors = await _collect(client, (
        ('tasks', MARKET_INFO_API, info_data(seller_id)),
    ), INFO_METRICS)
    return {
        'metrics': metrics,
        'tasks': find_list(raw.get('tasks')),
        'suspect_keys': suspect_keys(raw.get('tasks'), INFO_METRICS),
        'errors': errors,
        'raw': raw,
    }


async def competitiveness(client: Any, item_ids: Any = None,
                          seller_id: str = '') -> Dict[str, Any]:
    """价格竞争力：低/高竞争力条数 + 平均分 + 原始行。"""
    metrics, raw, errors = await _collect(client, (
        ('competitiveness', PRICE_COMPETITIVENESS_API,
         competitiveness_data(item_ids, seller_id)),
    ), COMPETITIVENESS_METRICS)
    return {
        'metrics': metrics,
        'items': find_list(raw.get('competitiveness')),
        'suspect_keys': suspect_keys(raw.get('competitiveness'), COMPETITIVENESS_METRICS),
        'errors': errors,
        'raw': raw,
    }


async def records(client: Any, page: int = 1, page_size: int = 20,
                  seller_id: str = '') -> Dict[str, Any]:
    """操作记录（只读流水）：条数 + 原始行。"""
    metrics, raw, errors = await _collect(client, (
        ('records', OPERATION_RECORD_API, record_data(page, page_size, seller_id)),
    ), RECORDS_METRICS)
    return {
        'metrics': metrics,
        'records': find_list(raw.get('records')),
        'suspect_keys': suspect_keys(raw.get('records'), RECORDS_METRICS),
        'errors': errors,
        'raw': raw,
    }
