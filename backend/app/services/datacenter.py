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
from . import common

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
}


def _warnings(errors: Dict[str, str]) -> list:
    """把逐接口错误翻成人话。无权限单独说，因为「一屏 -」的根因就是它。"""
    messages = []
    for label, text in (errors or {}).items():
        name = _LABELS.get(label, label)
        if 'UNAUTHORIZED' in text or '无权限' in text:
            messages.append(
                f'平台拒绝访问{name}接口：这个账号没有「数据」权限，'
                '对应指标显示 -。换个有数据权限的卖家账号再试。'
            )
        else:
            messages.append(f'平台{name}接口调用失败：{text}，对应指标显示 -。')
    return messages


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
