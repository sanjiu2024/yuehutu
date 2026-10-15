"""数据组（数据总览 / 粉丝数据 / 客服数据）只读服务。

三条硬规则（task-52）：

1. **日期区间**：`start` / `end` 必须是 `YYYYMMDD` 且 `start <= end`，否则
   `invalid_fields`（与 `services/promotion.py::_date_pair` 逐字同口径）；缺省是「最近 7 天」。
2. **平台没给的指标一律 `None`**，前端显示「-」，**禁止 0 顶替**。0 会让用户以为
   「真的没人看」，而真机实测的真相是：普通账号对 `datacompass` 的 `singleuser` /
   `fans` / `cs` 族一律返回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`
   （原始片段见 `docs/contracts/datacenter.md`）。所以「一屏 -」才是如实回答。
3. **缓存**：与商品统计同一策略（拿不到就 null、不造 0），但**不落库** —— 数据组的
   缓存表要动 `backend/app/core/db.py` 的 SCHEMA，而它在写范围外（冻结）。因此这里用
   进程内 TTL 缓存，只缓存**成功**结果，键是 `(页面, 账号, start, end)`；失败结果
   绝不进缓存，否则一次风控抖动会钉死 5 分钟。

协议层在 `backend/app/xianyu/datacenter.py`（模块级 async 函数，调用方传 client），
通过 `common.protocol_module('datacenter')` 懒导入 —— 协议层不存在时给 `not_ready`
（503），而不是把「没装协议层」当成「指标是 0」。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from ..core import db
from . import common, platform_errors

#: 进程内缓存存活秒数。数据组页面的指标是「天粒度」，5 分钟内重复进出页面不必再打平台。
CACHE_TTL_SECONDS = 300

#: 缓存条目上限（键含账号与区间，只靠 TTL 惰性淘汰的话长期运行会只增不减）。
CACHE_MAX_ENTRIES = 64

#: 三个页面各自的协议函数名 = 对外 kind（`_page` 用同一个字符串找协议函数）
KINDS = ('overview', 'fans', 'customer_service')

_CACHE: Dict[Tuple[str, str, str, str], Tuple[float, Dict[str, Any]]] = {}


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def clear_cache() -> None:
    """清空进程内缓存（测试用；也供以后「强制刷新」接）。"""
    _CACHE.clear()


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """Cookie / 登录态前置检查，与 `services/promotion.py` 同口径。

    先看库再看协议层：Cookie 已经没了的时候，直接说「去账号页扫码」，比抛一个
    mtop 的 `FAIL_SYS_TOKEN_EMPTY` 有用得多。
    """
    row = db.query_one(
        'SELECT cookie, login_state FROM account_credentials WHERE account_id = ?',
        (account_id,),
    )
    if row is None or not _text(row['cookie']).strip():
        return common.fail(
            f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录',
            'verification_required', login_state='need_relogin',
        )
    if _text(row['login_state']).strip() == 'need_relogin':
        return common.fail(
            f'账号 {account_id} 登录态已过期，需要重新扫码登录',
            'verification_required', login_state='need_relogin',
        )
    return None


def _client(account_id: str):
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


def _date_pair(start: str, end: str):
    today = datetime.now().date()
    a = _text(start).strip() or (today - timedelta(days=6)).strftime('%Y%m%d')
    b = _text(end).strip() or today.strftime('%Y%m%d')
    if len(a) != 8 or len(b) != 8 or not a.isdigit() or not b.isdigit() or a > b:
        return None, common.fail('日期必须是 YYYYMMDD，且开始日期不能晚于结束日期', 'invalid_fields')
    return (a, b), None


class _BoundProtocol:
    """把「模块级协议函数」和「调用它的 client」绑成一个可交给 `call_method` 的对象。

    **为什么必须包一层**：`common.call_method` 在协议层抛登录态过期时，会用**第一个
    参数**去回写 `need_relogin`（读它的 `cookie_id` / `account_id`）。直接传协议模块的话，
    模块身上没有账号身份，掉线就写不回库里 —— 真机表现就是「协议报 Session过期，界面还
    显示已登录」，用户只能自己猜到要重新扫码。

    转发方向要说清楚：**协议函数**转发到模块（`__getattr__`），**账号身份**只从 client
    上取 `cookie_id` / `account_id` / `persist_account_id` 这两个 property —— client 的其它
    属性（如 `cookie_str`）不在这里转发，取会 `AttributeError`。
    """

    def __init__(self, module: Any, client: Any) -> None:
        self._module = module
        self._client = client

    @property
    def account_id(self) -> str:
        # 真 `XianyuClient` **没有** `account_id` 属性（只有 `cookie_id` 和
        # `persist_account_id`，见 client.py），少这一层退回就恒为 ''，回写会被静默跳过。
        return str(getattr(self._client, 'account_id', '')
                   or getattr(self._client, 'persist_account_id', '') or '')

    @property
    def cookie_id(self) -> str:
        return str(getattr(self._client, 'cookie_id', '') or '')

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


#: kind → 协议模块里「本页契约键」元组的属性名。契约键以协议层为唯一真相，
#: 服务层不再抄一份（抄了就会和 METRIC_ALIASES 一起漂移）。
_METRIC_KEYS_ATTR = {
    'overview': 'OVERVIEW_METRICS',
    'fans': 'FANS_METRICS',
    'customer_service': 'CS_METRICS',
}


def _metrics_problem(module: Any, kind: str, payload: Any):
    """协议层不给 `metrics`、给空 `{}`、或缺契约键 → 协议异常（502），不是「成功但没数据」。

    **为什么必须按契约键校验**：`{}` 是 dict，`isinstance(..., dict)` 挡不住它；而契约
    承诺「平台没给的键**也一定出现**（值为 `null`）」，所以缺键就是协议层坏了，必须 502
    让人看见 —— 200 + 一屏 `-` 会让用户以为「今天真的一个数据都没有」。
    """
    if not isinstance(payload, dict):
        return common.fail('数据组协议层没有返回指标字段', 'protocol_error')
    metrics = payload.get('metrics')
    if not isinstance(metrics, dict) or not metrics:
        return common.fail('数据组协议层没有返回指标字段', 'protocol_error')
    attr = _METRIC_KEYS_ATTR.get(kind, '')
    required = set(getattr(module, attr, None) or ())
    if required and not required <= set(metrics):
        missing = sorted(required - set(metrics))
        return common.fail(f'数据组协议层缺指标字段：{", ".join(missing[:5])}', 'protocol_error')
    return None


def _call(name: str, client: Any, *args: Any):
    """调协议层的 `name(client, start, end, seller_id)`。

    协议层缺失 → `not_ready`（503）；协议层抛异常 → `call_failed` 翻信封
    （登录态过期给 409 + `need_relogin`，其余给 502 `protocol_error`）。
    """
    module = common.protocol_module('datacenter')
    if module is None:
        return None, common.not_ready('协议层未提供数据组接口（backend.app.xianyu.datacenter）')
    status, value = common.call_method(_BoundProtocol(module, client), (name,), client, *args)
    if status == common.CALL_OK:
        # 成功也可能「空着手回来」：协议层改名/结构变了时 metrics 会是 None 或 {}，
        # 那是协议异常，不是「今天没人来」（见 `_metrics_problem`）。
        problem = _metrics_problem(module, name, value)
        return (None, problem) if problem is not None else (value, None)
    failure = common.call_failed(status, value)
    return None, failure or common.fail('数据组接口调用失败', 'protocol_error')


#: 协议层的错误标签 → 用户看得懂的名字。warnings 是给用户看的，不能漏内部调用名
#: （`overview` / `items` / `begin_time` 这种词只对开发者有意义）。
_LABELS = {
    'overview': '数据总览',
    'items': '商品数据',
    'begin_time': '数据起始日',
    'fans': '粉丝数据',
    'insights': '粉丝画像',
    'cs': '客服数据',
    # 商品数据页（`commodity`）自己的两个标签：列表与指标列定义
    'commodity': '商品数据',
    'columns': '商品指标列',
    # 经营卡片（`card`）：协议层的错误标签就是 kind（一次只查一张，所以只会出现一个）
    'trade': '交易数据',
    'seller': '经营概况',
    'repurchase': '复购',
    'refund': '退款概览',
    # 客服明细三张表：协议层一次只调一个接口，标签就是 kind
    'detail': '客服明细',
    'diversion': '订单分流明细',
    'evaluation': '客户评价明细',
}


def _warnings(errors: Dict[str, str]) -> list:
    """把逐接口错误翻成人话。无权限单独说，因为「一屏 -」的根因就是它。

    判据与「没权限」那句人话都不在这里：判据走 `platform_errors.is_permission`
    （按错误码词边界认，不再就地写 `'UNAUTHORIZED' in text`），文案走
    `platform_errors.permission_hint`。同一句人话全站只有一份（审计问题 13）。
    这里只补两件本地才知道的事：哪个接口、后果是「指标显示 -」。
    """
    messages = []
    for label, text in (errors or {}).items():
        name = _LABELS.get(label, label)
        if platform_errors.is_permission(text):
            messages.append(
                f'平台拒绝访问{name}接口（对应指标显示 -）：'
                f'{platform_errors.permission_hint(text)}'
            )
        else:
            messages.append(f'平台{name}接口调用失败：{text}，对应指标显示 -。')
    return messages


#: 平台把接口都答了、却一组画像分组都没给（真机上是「改了字段名」）时说的话。
#: 注意**只在没有任何接口报错时**才加这句：账号没数据权限时已经有权限 warning，
#: 再叠一句「画像没解析出来」是噪音。
WARN_FANS_INSIGHTS_MISSING = (
    '平台没有返回粉丝画像分组（性别 / 粉丝类型 / 省份 / 八大人群），这一块暂时显示不出来。'
)


def _wrap(kind: str, account_id: str, dates: Tuple[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        'account_id': account_id,
        'kind': kind,
        'period': {'from': dates[0], 'to': dates[1]},
        'metrics': payload.get('metrics') or {},
        'begin_time': payload.get('begin_time'),
        'real_date_range': payload.get('real_date_range'),
        'warnings': _warnings(payload.get('errors') or {}),
        'source': 'platform',
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }
    if kind == 'fans':
        result['insights'] = payload.get('insights')
        result['insights_breakdown'] = payload.get('insights_breakdown')
        if payload.get('insights_breakdown') is None and not (payload.get('errors') or {}):
            result['warnings'].append(WARN_FANS_INSIGHTS_MISSING)
    if payload.get('suspect_keys'):
        result['unrecognized_keys'] = payload['suspect_keys']
    return result


def _cache_key(kind: str, account_id: str, start: str, end: str) -> Tuple[str, str, str, str]:
    return (kind, account_id, start, end)


def _cache_get(key: Tuple[str, str, str, str]) -> Optional[Dict[str, Any]]:
    item = _CACHE.get(key)
    if item is None:
        return None
    stamp, value = item
    if time.time() - stamp > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return value


def _remember(key: Tuple[str, str, str, str], value: Dict[str, Any]) -> None:
    """写入缓存，并按上限淘汰最旧的一条（只靠 TTL 惰性淘汰会只增不减）。"""
    if len(_CACHE) >= CACHE_MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (time.time(), value)


def _page(kind: str, account_id: str, start: str, end: str) -> Dict[str, Any]:
    dates, failure = _date_pair(start, end)
    if failure:
        return failure
    key = _cache_key(kind, account_id, dates[0], dates[1])
    # 先看缓存再建客户端：命中时省掉查库 + 新建 XianyuClient，而且不会因为本地登录态
    # 刚好过期就用 409 盖掉 5 分钟内刚拿到的成功结果（缓存只该省网络调用）。
    hit = _cache_get(key)
    if hit is not None:
        return hit
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(kind, client, dates[0], dates[1], account_id)
    if failure:
        # metrics 为 None / {} / 缺契约键时 `_call` 已经在这里回了 502，
        # 不会走到下面变成「200 + 一屏 -」的假成功（见 `_metrics_problem`）。
        return failure
    result = _wrap(kind, account_id, dates, payload)
    _remember(key, result)
    return result


def overview(account_id: str, start: str = '', end: str = '') -> Dict[str, Any]:
    """数据总览（曝光 / 访问 / 浏览 / 在线商品 / 询单）。"""
    return _page('overview', account_id, start, end)


def fans(account_id: str, start: str = '', end: str = '') -> Dict[str, Any]:
    """粉丝数据（总数 / 新增 / 流失 / 活跃 + 画像洞察）。"""
    return _page('fans', account_id, start, end)


def customer_service(account_id: str, start: str = '', end: str = '') -> Dict[str, Any]:
    """客服数据（询单人数 / 询单量 / 3 分钟响应率 / 平均响应时长 / 未回复 / 满意度）。"""
    return _page('customer_service', account_id, start, end)


# ---------------------------------------------------------------------------
# 商品数据（官方 `/seller-data/commodity`）—— 只读、分页、可排序
# ---------------------------------------------------------------------------
#: 每页条数上限。官方只给 10/20/50 三档，这里按最大值卡住 —— 别让 AI 一次拉一万行
#: （那既慢又容易触发风控）。
COMMODITY_PAGE_SIZES: Tuple[int, ...] = (10, 20, 50)
COMMODITY_DEFAULT_PAGE_SIZE = 20
COMMODITY_MAX_PAGE_SIZE = 50

WARN_COMMODITY_LIST_MISSING = ('平台没有返回商品列表字段（可能改了字段名），'
                               '这一页显示不出商品，请点「刷新」重试。')
WARN_COMMODITY_COLUMNS_MISSING = ('平台没有返回指标列定义，只能看到商品本身的字段'
                                  '（标题 / 图片 / 商品 ID），指标列暂时显示不出来。')


def _int_arg(label: str, value: Any, minimum: int, maximum: int):
    """整数参数校验：非整数 / 越界 → `invalid_fields`（400）。

    **绝不悄悄改成默认值**：AI 传了 `size=500` 而平台只吃 50，静默改成 50 会让它以为
    「我拉到 500 条了」，据此做的判断全错。
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None, common.fail(f'{label}必须是整数', 'invalid_fields')
    if number < minimum or number > maximum:
        return None, common.fail(f'{label}必须在 {minimum}~{maximum} 之间', 'invalid_fields')
    return number, None


def _wrap_commodity(account_id: str, payload: Dict[str, Any], page: int,
                    page_size: int) -> Dict[str, Any]:
    """把协议层结果包成对外契约。**`rows` / `columns` 认不出时保持 `None`**。"""
    warnings = _warnings(payload.get('errors') or {})
    if payload.get('rows') is None:
        warnings.append(WARN_COMMODITY_LIST_MISSING)
    if payload.get('columns') is None:
        warnings.append(WARN_COMMODITY_COLUMNS_MISSING)
    dates = payload.get('dates') or []
    total = payload.get('total')
    pages = None
    if isinstance(total, (int, float)) and page_size > 0:
        pages = int((total + page_size - 1) // page_size)
    return {
        'account_id': account_id,
        'kind': 'commodity',
        'period': {
            'from': dates[0] if dates else '',
            'to': dates[1] if len(dates) > 1 else (dates[0] if dates else ''),
        },
        'date_type': payload.get('date_type') or '',
        'rows': payload.get('rows'),
        'columns': payload.get('columns'),
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': pages,
        'real_date_range': payload.get('real_date_range'),
        'warnings': warnings,
        'source': 'platform',
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }


def commodity(account_id: str, start: str = '', end: str = '', item_id: str = '',
              item_name: str = '', category_info: Any = None, is_dp_stk: str = '',
              mkt_act_id: str = '', page: Any = 1, size: Any = None,
              order_by: str = '', order: str = '') -> Dict[str, Any]:
    """商品数据：一行一个商品 + 平台给的指标列（只读）。

    与另外三页的三点不同（都是官方页面行为，不是我们的选择）：

    * **默认是「今天」**（`recent1d`），不是最近 7 天 —— 官方页面的时间筛选默认今天；
    * **分页 + 排序**由平台做，所以**不进缓存**（键要含全部参数，缓存价值低而误命中
      代价高：翻页时给你上一页的缓存就是「点了没反应」）；
    * **列由平台定义**（`item.indicators`），我们不写死指标键 —— 官方表头是可变的，
      写死就会漏掉平台新增的列。
    """
    page_number, failure = _int_arg('页码', page, 1, 100000)
    if failure:
        return failure
    page_size, failure = _int_arg(
        '每页条数',
        COMMODITY_DEFAULT_PAGE_SIZE if size in (None, '') else size,
        1, COMMODITY_MAX_PAGE_SIZE,
    )
    if failure:
        return failure
    order = str(order or '').strip().lower()
    if order not in ('', 'asc', 'desc'):
        return common.fail('排序方向只能是 asc 或 desc', 'invalid_fields')
    order_by = str(order_by or '').strip()
    if order and not order_by:
        return common.fail('指定排序方向时必须同时给出排序字段', 'invalid_fields')
    module = common.protocol_module('datacenter')
    if module is None:
        return common.not_ready('协议层未提供数据组接口（backend.app.xianyu.datacenter）')
    client, failure = _client(account_id)
    if failure:
        return failure
    status, value = common.call_method(
        _BoundProtocol(module, client), ('commodity',), client,
        _text(start).strip(), _text(end).strip(), account_id, category_info,
        _text(item_id).strip(), _text(item_name).strip(), _text(is_dp_stk).strip(),
        _text(mkt_act_id).strip(), page_number, page_size, order_by, order,
    )
    if status != common.CALL_OK:
        return common.call_failed(status, value) or common.fail(
            '商品数据接口调用失败', 'protocol_error')
    return _wrap_commodity(account_id, value, page_number, page_size)


# ---------------------------------------------------------------------------
# 数据总览页的四张「经营卡片」（交易数据 / 经营概况 / 复购 / 退款概览）—— 只读
# ---------------------------------------------------------------------------
#: 四张卡片的 kind。与协议层 `CARD_KINDS` 同序，但服务层**自己再列一份**：
#: 契约（400 的合法值）不该由协议层的实现细节决定。
CARD_KINDS: Tuple[str, ...] = ('trade', 'seller', 'repurchase', 'refund')

WARN_CARD_EMPTY = ('平台没有返回这张卡片的数据（可能改了字段名），这一张暂时显示不出来，'
                   '请点「刷新」重试。')


def _card_dates(start: str, end: str):
    """卡片的时间入参：两头都没给 → `[]`（协议层会发 `recent1d`＝今天）。

    **缺一头就用另一头顶上**，与协议层 `card_dates` 逐字一致 —— 两边算法不一样的话，
    界面上显示的区间会与真正发出去的参数对不上（那是比报错更难发现的错）。
    """
    a = _text(start).strip()
    b = _text(end).strip()
    if not a and not b:
        return [], None
    a = a or b
    b = b or a
    if len(a) != 8 or len(b) != 8 or not a.isdigit() or not b.isdigit() or a > b:
        return None, common.fail('日期必须是 YYYYMMDD，且开始日期不能晚于结束日期', 'invalid_fields')
    return [a, b], None


def _wrap_card(account_id: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """把协议层结果包成对外契约。

    `metrics` / `banner` / `series` 是**列表**，认不出时给 `[]`（这一页的「有结构但没
    条目」与「结构都没有」用 `found` 区分，`found=False` 时额外给一条 warning）。
    """
    warnings = _warnings(payload.get('errors') or {})
    if not payload.get('found') and not warnings:
        # 已经有「平台拒绝访问…」时不再补这条 —— 那条才是真因，两条一起说会把
        # 「没权限」说成「可能改了字段名」，把人往错方向带。
        warnings.append(WARN_CARD_EMPTY)
    dates = payload.get('dates') or []
    if not dates:
        # 没给日期时**线上参数是 `recent1d`（不带 dateRange）**，但界面上要显示一个区间。
        # 显示用今天、发出去的仍是不带日期的 recent1d —— 这两件事故意不同（显示值不能反过来
        # 决定线上参数，否则会把「今天」变成 `customDate` 查询，平台行为就变了）。
        today = datetime.now().strftime('%Y%m%d')
        dates = [today, today]
    return {
        'account_id': account_id,
        'kind': kind,
        'title': payload.get('title'),
        'api': payload.get('api'),
        'period': {
            'from': dates[0] if dates else '',
            'to': dates[1] if len(dates) > 1 else (dates[0] if dates else ''),
        },
        'date_type': payload.get('date_type') or '',
        'metrics': payload.get('metrics') or [],
        'banner': payload.get('banner') or [],
        'series': payload.get('series') or [],
        'promotion': payload.get('promotion'),
        'extra': payload.get('extra') or {},
        'real_date_range': payload.get('real_date_range'),
        'warnings': warnings,
        'source': 'platform',
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }


def card(account_id: str, kind: str = 'trade', start: str = '', end: str = '') -> Dict[str, Any]:
    """数据总览页的一张经营卡片（只读）。

    与前面三页的两点不同（都是官方页面行为）：

    * **默认是「今天」**（`recent1d`），不是最近 7 天；
    * **入参用 `dateRange`（`|` 连接）而不是 `date`** —— 官方在这四张卡片上就是另一套
      写法，协议层照抄，服务层不统一（统一会让其中一边发出平台不认的参数）。

    一次只查一张：官方页面就是四个独立请求，各自失败不影响其它三张。
    """
    kind = _text(kind).strip().lower()
    if kind not in CARD_KINDS:
        return common.fail(
            f'不支持的数据卡片：{kind}（可选 {" / ".join(CARD_KINDS)}）', 'invalid_fields')
    dates, failure = _card_dates(start, end)
    if failure:
        return failure
    module = common.protocol_module('datacenter')
    if module is None:
        return common.not_ready('协议层未提供数据组接口（backend.app.xianyu.datacenter）')
    client, failure = _client(account_id)
    if failure:
        return failure
    status, value = common.call_method(
        _BoundProtocol(module, client), ('card',), client,
        kind, _text(start).strip(), _text(end).strip(), account_id,
    )
    if status != common.CALL_OK:
        return common.call_failed(status, value) or common.fail(
            '数据卡片接口调用失败', 'protocol_error')
    return _wrap_card(account_id, kind, value)


# ---------------------------------------------------------------------------
# 客服数据页的三张明细表：客服明细 / 订单分流明细 / 客户评价明细 —— 只读
# ---------------------------------------------------------------------------
#: 服务层**自己再列一份** kind：契约的合法值（400 的边界）不该由协议层实现细节决定。
CS_LIST_KINDS: Tuple[str, ...] = ('detail', 'diversion', 'evaluation')
CS_LIST_TITLES: Dict[str, str] = {
    'detail': '客服明细',
    'diversion': '订单分流明细',
    'evaluation': '客户评价明细',
}
CS_LIST_DEFAULT_PAGE_SIZE = 10
CS_LIST_MAX_PAGE_SIZE = 50

WARN_CS_LIST_MISSING = ('平台没有返回这张明细表的数据（可能改了字段名），'
                        '这一张暂时显示不出来，请点「刷新」重试。')


def _wrap_cs_list(account_id: str, kind: str, payload: Dict[str, Any], page: int,
                  page_size: int) -> Dict[str, Any]:
    """把协议层结果包成对外契约。**`rows` 认不出时保持 `None`**（不是空数组）。"""
    warnings = _warnings(payload.get('errors') or {})
    if payload.get('rows') is None and not warnings:
        # 已经有「平台拒绝访问…」时不再补这条 —— 那条才是真因，两条一起说会把
        # 「没权限」说成「可能改了字段名」，把人往错方向带（与 `_wrap_card` 同一口径）。
        warnings.append(WARN_CS_LIST_MISSING)
    dates = payload.get('dates') or []
    if not dates:
        # 显示用今天，但**线上参数仍是不带日期的 `recent1d`**（显示值不能反过来决定
        # 线上参数，否则会把「今天」变成 `customDate` 查询，平台行为就变了）。
        today = datetime.now().strftime('%Y%m%d')
        dates = [today, today]
    total = payload.get('total')
    pages = None
    if isinstance(total, (int, float)) and page_size > 0:
        pages = int((total + page_size - 1) // page_size)
    return {
        'account_id': account_id,
        'kind': kind,
        'title': CS_LIST_TITLES[kind],
        #: 列由官方表头定义（`key` + 中文 `title`），前端按这个顺序渲染
        'columns': [{'key': key, 'title': title}
                    for key, title in (payload.get('columns') or ())],
        'rows': payload.get('rows'),
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': pages,
        'period': {
            'from': dates[0] if dates else '',
            'to': dates[1] if len(dates) > 1 else (dates[0] if dates else ''),
        },
        'date_type': payload.get('date_type') or '',
        'real_date_range': payload.get('real_date_range'),
        'warnings': warnings,
        'source': 'platform',
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }


def cs_list(account_id: str, kind: str = 'detail', start: str = '', end: str = '',
            page: Any = 1, size: Any = None, order_by: str = '',
            order: str = '') -> Dict[str, Any]:
    """客服数据页的一张明细表（只读）。

    与商品数据页同一取舍：**分页与排序由平台做，所以不进缓存**（键要含全部参数，
    缓存价值低而误命中代价高：翻页时给你上一页的缓存就是「点了没反应」）。
    """
    kind = str(kind or 'detail').strip().lower()
    if kind not in CS_LIST_KINDS:
        return common.fail('明细类型只能是 detail / diversion / evaluation', 'invalid_fields')
    page_number, failure = _int_arg('页码', page, 1, 100000)
    if failure:
        return failure
    page_size, failure = _int_arg(
        '每页条数',
        CS_LIST_DEFAULT_PAGE_SIZE if size in (None, '') else size,
        1, CS_LIST_MAX_PAGE_SIZE,
    )
    if failure:
        return failure
    order = str(order or '').strip().lower()
    if order not in ('', 'asc', 'desc'):
        return common.fail('排序方向只能是 asc 或 desc', 'invalid_fields')
    order_by = str(order_by or '').strip()
    if order and not order_by:
        return common.fail('指定排序方向时必须同时给出排序字段', 'invalid_fields')
    module = common.protocol_module('datacenter')
    if module is None:
        return common.not_ready('协议层未提供数据组接口（backend.app.xianyu.datacenter）')
    client, failure = _client(account_id)
    if failure:
        return failure
    status, value = common.call_method(
        _BoundProtocol(module, client), ('cs_list',), client,
        kind, _text(start).strip(), _text(end).strip(), account_id,
        order_by, order, page_number, page_size,
    )
    if status != common.CALL_OK:
        return common.call_failed(status, value) or common.fail(
            '客服明细接口调用失败', 'protocol_error')
    return _wrap_cs_list(account_id, kind, value, page_number, page_size)
