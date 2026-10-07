"""商品数据（「商品数据」页）—— 本地能算的算准，平台拿不到的一律 `null`。

**这个模块存在的理由**：用户第 5 轮要求商品页加一个「商品数据」视图（曝光 / 浏览 /
访客 / 订单 / 买家 / 支付）。闲鱼**没有**公开的、稳定的商品级数据接口 —— 平台侧
指标能不能拿到取决于 `backend/app/xianyu/client.py` 上有没有 `item_stats`
（`proto` 正在调研），所以这里的第一原则是：

1. **本地真实能算的算准**：会话数 / 消息数 / 最近消息时间来自 `conversations`、
   `messages`；订单数 / 买家数 / 支付数来自 `orders`。这些是真数据。
2. **拿不到的一律 `null`**（前端显示「-」）。**禁止补 0，也禁止任何假数据** ——
   0 会被读成「真的没有人看过」，那是编出来的结论。所以本地计数为 0 时也返回
   `null`：本地没有这条记录，和「发生过 0 次」是两件事。
3. 平台接口不存在 / 调用失败，**不拖垮整个接口**：降级成 `source='local'` +
   `warnings` 里写清中文原因。只有 `?source=platform`（调用方显式要求平台数据）
   才会把平台失败升级成 502，这也是契约里 `protocol_error` 唯一可达的路径。

**口径与来源标注**：响应里的 `source` 表示**平台指标这一块**来自哪里：
`'platform'` = 平台接口成功返回；`'local'` = 平台指标全部 `null`、只用本地统计。
不论 `source` 是什么，`orders`/`buyers`/`paid_orders` 在没有平台数据时都是本地口径。

不打真实闲鱼平台：协议层一律通过 `common.get_client` 拿，测试注入假实现。
"""
from __future__ import annotations

import inspect
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core import db
from . import common

#: 顶层响应里 `source` 的两个取值
SOURCE_PLATFORM = 'platform'
SOURCE_LOCAL = 'local'

#: 只有平台能给的指标 —— 本地无论怎么算都是猜，所以没有平台数据时留 None。
#: `wants`（想要人数）来自卖家工作台接口的 `wantNum`（见 `xianyu/client.py` 的
#: `ITEM_STATS_FIELDS`），页面暂不展示，但**不能在这里丢掉**：丢掉等于「平台给了、
#: 我们假装没看见」，以后加列时又要回头查一遍。
PLATFORM_METRICS = ('exposure', 'visits', 'visitors', 'wants')

#: `?source=` 允许的取值：auto=能拿平台就拿、local=完全不碰平台、platform=必须拿到
SOURCE_MODES = ('auto', 'local', 'platform')
WARN_PLATFORM_MISMATCH = '平台返回的商品数据不属于 {item_id}（响应里标的是 {claimed}），已按本地统计显示'

#: 本地从 `orders` 表算出来的指标
LOCAL_ORDER_METRICS = ('orders', 'buyers', 'paid_orders')

#: 本地从 `conversations`/`messages` 表算出来的指标（契约外的附加键，见模块末说明）
LOCAL_CHAT_METRICS = ('conversations', 'messages', 'last_message_at')

#: 算「已支付」的订单状态。平台侧的订单状态文案是中文，本地 upsert 时原样落库，
#: 所以这里中英并列；`pending`/`unknown`/`closed`/`cancelled` 一律不算支付。
PAID_STATES = (
    'paid', 'wait_ship', 'shipped', 'delivered',
    '已付款', '待发货', '已发货', '已签收', '已完成',
)

#: 平台响应里的字段别名（协议层命名未定，先按常见几种认；认不出就不填）。
#: `exposureNum` 是曝光、`showNum`/`showCount` 是展示量（= `visits`）—— 和
#: `xianyu/client.py::parse_item_stats` 的改名保持一致（那边就是 `showNum → visits`）。
#: 同一个词根落在两个指标上，用户看到的就是「浏览数忽大忽小」，所以宁可按协议层对齐。
METRIC_ALIASES: Dict[str, Tuple[str, ...]] = {
    'exposure': ('exposure', 'exposureNum', 'exposureCount', 'expose', 'exposeCount',
                 'impressions', '曝光', '曝光量'),
    'visits': ('visits', 'visitCount', 'visit', 'pv', 'pageView',
               'showNum', 'showCount', '浏览', '浏览量'),
    'visitors': ('visitors', 'visitorCount', 'visitor', 'uv', 'uniqueVisitors',
                 '访客', '访客数'),
    'wants': ('wants', 'wantNum', 'wantCount', 'want', '想要', '想要人数'),
    'orders': ('orders', 'orderCount', 'orderNum', 'orderCnt', '订单', '订单数'),
    'buyers': ('buyers', 'buyerCount', 'buyerNum', '买家', '买家数'),
    'paid_orders': ('paid_orders', 'paidOrders', 'paidCount', 'paidNum', 'payCount',
                    '支付', '支付数', '付款数'),
}

WARN_NO_PLATFORM = '平台未提供商品数据接口，仅显示本地统计'
WARN_PLATFORM_FAILED = '平台商品数据接口调用失败（{reason}），已降级为本地统计'
WARN_PLATFORM_UNPARSED = '平台商品数据接口返回的内容里没有可识别的指标，已按本地统计显示'
WARN_PLATFORM_PARTIAL = ('平台响应里有没认出的字段（{keys}），这些指标已忽略；'
                         '如果它们是曝光/浏览类指标，请把它们加进 item_stats.METRIC_ALIASES')
#: **不要写「请先在『订单』页同步」** —— 侧栏九个页面里没有订单页（订单服务在
#: `services/orders.py`，只有接口没有界面），用户按提示去找只会以为软件坏了。
#: 只说「还没有订单数据」以及为什么这几列是 `-`，不指路到不存在的地方。
WARN_NO_ORDERS = ('本地还没有订单数据（orders 表里没有该账号的记录）：订单 / 买家 / 支付指标'
                  '暂不可用，这几列显示 -。当前版本还没有订单页，先不用管它们。')
WARN_UNLINKED_ORDERS = '本地有 {count} 笔订单没有关联商品（orders.item_id 为空），未计入任何商品的统计'
#: 平台这个接口**只能按商品逐个查**（真机实测：没有账号级的批量入口）。一个 45 件商品的
#: 账号如果每次都全量查，就是一次 45 连击 mtop —— 真机补库存那次就是这么触发风控的
#: （`stock_refresh_stopped="risk"`）。所以这里硬性限流，并把「只查了前几件」如实告诉用户，
#: 而不是偷偷少查或者把账号打到熔断。
PLATFORM_ITEM_LIMIT = 10
WARN_PLATFORM_CAPPED = ('平台这个接口只能按商品逐个查，一次查太多会触发平台风控：'
                        '本次只查了前 {done} 件（这个账号共 {total} 件），其余先用本地统计。'
                        '想查具体某件商品，用搜索框把列表缩小到它再刷新。')

#: 平台字段名里带这些词根、却又不在别名表里的，值得专门提一句（见 `_suspect_metric_keys`）。
#: 只挑指标词根，不把 `itemId`/`title`/`code` 这些结构性字段也报出来 —— 那种 warning
#: 每次请求都会响，用户会学会无视它。
_METRIC_HINTS = (
    'exposure', 'expose', 'show', 'impression', 'visit', 'pv', 'uv', 'visitor',
    'want', 'order', 'buyer', 'pay',
    '曝光', '浏览', '访客', '想要', '订单', '买家', '支付',
)


# --------------------------------------------------------------------------- 小工具


def _now_ms() -> int:
    """UTC 毫秒时间戳（契约要求；前端拿它显示「更新于」）。"""
    return int(time.time() * 1000)


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _number(value: Any) -> Optional[float]:
    """把协议层返回值收敛成数字；`None`/空串/布尔/非数字字符串一律 None（不猜）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().replace(',', '')
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _tidy(value: Optional[float]) -> Optional[float]:
    """整数就回整数（前端不希望看到 12.0）。"""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else float(value)


def _count_or_none(value: Any) -> Optional[int]:
    """本地计数：**0 归 `null`**。

    这是本模块最容易被「顺手改成 0」的地方，所以单独成函数并写死含义：
    本地库没有这条记录，只能说明「我们不知道」，不能说明「没发生过」。
    """
    number = _number(value)
    if number is None or number <= 0:
        return None
    return int(number)


def _images_of(raw: Any) -> List[str]:
    """`items.images` 是 JSON 字符串（DDL 默认 `'[]'`）。解析失败就当空数组。"""
    if isinstance(raw, list):
        return [str(u) for u in raw if isinstance(u, (str, int))]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(u) for u in value if isinstance(u, (str, int))]


def _price_of(raw: Any) -> Optional[float]:
    """`items.price` 是**存下来的商品属性**（不是统计量），按库里的值原样给。"""
    number = _number(raw)
    return _tidy(number)


# --------------------------------------------------------------------------- 前置校验


def _account_exists(account_id: str) -> bool:
    return db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is not None


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """账号没有可用 Cookie 时返回 409 信封，否则 None。

    **为什么不信 `common.get_client` 的错误**：它把「没 Cookie」也归成
    `xianyu_not_ready`(503) —— 那个码是给「协议层没装好」用的；对用户来说
    「这个账号要重新扫码」是 409 `verification_required`，前端据此弹重新登录。
    """
    row = db.query_one(
        'SELECT cookie, login_state FROM account_credentials WHERE account_id = ?',
        (account_id,),
    )
    cookie = _text(row['cookie']).strip() if row is not None else ''
    state = _text(row['login_state']).strip() if row is not None else ''
    if not cookie:
        return common.fail(
            f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录',
            'verification_required', login_state='need_relogin',
        )
    if state == 'need_relogin':
        return common.fail(
            f'账号 {account_id} 登录态已过期，需要重新扫码登录',
            'verification_required', login_state='need_relogin',
        )
    return None


# --------------------------------------------------------------------------- 本地统计


def _item_rows(account_id: str, item_id: str = '') -> List[Any]:
    """该账号的商品行（可只用一条）。列名以 `core/db.py` 的 DDL 为准。"""
    sql = ('SELECT item_id, title, price, status, images FROM items WHERE account_id = ?')
    params: List[Any] = [account_id]
    if item_id:
        sql += ' AND item_id = ?'
        params.append(item_id)
    sql += ' ORDER BY id'
    return list(db.query(sql, params) or [])


def _local_chat_metrics(account_id: str) -> Dict[str, Dict[str, Any]]:
    """`{item_id: {conversations, messages, last_message_at}}`（只算真有的）。"""
    result: Dict[str, Dict[str, Any]] = {}
    conv_rows = db.query(
        'SELECT item_id, COUNT(*) AS n FROM conversations '
        "WHERE account_id = ? AND COALESCE(item_id, '') <> '' GROUP BY item_id",
        (account_id,),
    )
    for row in conv_rows or []:
        result.setdefault(_text(row['item_id']), {})['conversations'] = row['n']
    msg_rows = db.query(
        'SELECT c.item_id AS item_id, COUNT(m.id) AS n, MAX(m.created_at) AS last_at '
        'FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id '
        "WHERE c.account_id = ? AND COALESCE(c.item_id, '') <> '' GROUP BY c.item_id",
        (account_id,),
    )
    for row in msg_rows or []:
        bucket = result.setdefault(_text(row['item_id']), {})
        bucket['messages'] = row['n']
        bucket['last_message_at'] = row['last_at']
    return result


def _local_order_metrics(account_id: str, item_ids: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """`({item_id: {orders, buyers, paid_orders}}, 该账号是否同步过订单)`。

    `orders` 表有 `account_id`+`item_id`+`buyer_id`+`status`（见 DDL），所以这三项是
    本地真能算的；**「同步过没有」要单独判**：表里一行都没有时，`0` 是假信息。
    """
    metrics: Dict[str, Dict[str, Any]] = {}
    synced = _count_or_none((db.query_one(
        'SELECT COUNT(*) AS n FROM orders WHERE account_id = ?', (account_id,)) or {'n': 0})['n']) is not None
    if not synced:
        return metrics, False
    if not item_ids:
        return metrics, True
    placeholders = ','.join('?' for _ in PAID_STATES)
    rows = db.query(
        'SELECT item_id, COUNT(*) AS orders, '
        "COUNT(DISTINCT CASE WHEN COALESCE(buyer_id, '') <> '' THEN buyer_id END) AS buyers, "
        f'SUM(CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END) AS paid_orders '
        "FROM orders WHERE account_id = ? AND COALESCE(item_id, '') <> '' GROUP BY item_id",
        tuple(PAID_STATES) + (account_id,),
    )
    for row in rows or []:
        metrics[_text(row['item_id'])] = {
            'orders': row['orders'],
            'buyers': row['buyers'],
            'paid_orders': row['paid_orders'],
        }
    return metrics, True


def _unlinked_order_count(account_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM orders WHERE account_id = ? AND COALESCE(item_id, '') = ''",
        (account_id,),
    )
    return int((row or {'n': 0})['n'] or 0)


# --------------------------------------------------------------------------- 平台指标


def _pick_metric(payload: Dict[str, Any], key: str) -> Optional[float]:
    for alias in METRIC_ALIASES.get(key, (key,)):
        if alias in payload:
            number = _number(payload.get(alias))
            if number is not None:
                return number
    return None


def _normalize_platform(value: Any) -> Optional[Dict[str, float]]:
    """从平台响应里抠出**认得出**的指标；一个都认不出返回 None（绝不瞎填）。

    协议层的响应形状还没定（`proto` 在调研），所以这里只做「尽力而为」：
    容忍 list 包一层、dict 里再套 `data`/`result`/`item`/`stats` 一层。
    """
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if not isinstance(value, dict):
        return None
    found: Dict[str, float] = {}
    for key in PLATFORM_METRICS + ('orders', 'buyers', 'paid_orders'):
        number = _pick_metric(value, key)
        if number is not None:
            found[key] = number
    if found:
        return found
    for nest in ('data', 'result', 'item', 'stats'):
        nested = value.get(nest)
        if isinstance(nested, (dict, list)):
            deeper = _normalize_platform(nested)
            if deeper:
                return deeper
    return None


def _platform_for_item(value: Any, item_id: str) -> Optional[Dict[str, float]]:
    """**不带 `item_id` 调用**时用：必须能从响应里认出「这条商品那一份」。

    认不出来就返回 `None`（宁可降级为本地统计）—— 因为一个账号级的汇总数字
    （「本店总曝光 20481」）被按到每条商品头上，就是**假数据**，比没有数据更糟。
    """
    if isinstance(value, dict):
        bucket = value.get(item_id)
        if isinstance(bucket, (dict, list)):
            return _normalize_platform(bucket)
        for nest in ('data', 'result', 'items', 'stats', 'list'):
            nested = value.get(nest)
            if isinstance(nested, (dict, list)):
                found = _platform_for_item(nested, item_id)
                if found:
                    return found
        return None
    if isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict):
                continue
            ident = entry.get('item_id') or entry.get('itemId') or entry.get('id')
            if _text(ident) == item_id:
                return _normalize_platform(entry)
    return None


#: 响应里「这些数属于哪条商品」的字段名
ID_KEYS = ('item_id', 'itemId', 'id')


def _claimed_ids(value: Any, depth: int = 3) -> set:
    """响应里**自称**属于哪些商品。

    只认显式的 id 字段，**不看 dict 的键** —— 否则一个正常的指标字典
    （`{'exposure': 20481}`）会把 `exposure` 当成商品 id，把好数据全判成张冠李戴。
    """
    found: set = set()
    if depth <= 0:
        return found
    if isinstance(value, dict):
        for key in ID_KEYS:
            ident = value.get(key)
            if isinstance(ident, (str, int)) and not isinstance(ident, bool):
                found.add(_text(ident))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                found |= _claimed_ids(nested, depth - 1)
    elif isinstance(value, list):
        for entry in value:
            found |= _claimed_ids(entry, depth - 1)
    return found


def _suspect_metric_keys(value: Any, depth: int = 3) -> List[str]:
    """响应里「像指标、但一个别名都没命中」的字段名（去重、保序、最多 6 个）。

    别名表认不出某个字段时，早先是**完全静默**的：平台明明回了
    ``{'exposureNum': 100, 'showNum': 20}``，接口却回 200 + 四个指标全 null +
    零 warning，用户只会觉得「这功能坏了」。这里把可疑字段名捞出来交给 warning，
    下次平台换字段名时至少能一眼看出该往别名表里加什么。
    """
    known = {alias for aliases in METRIC_ALIASES.values() for alias in aliases}
    known |= set(PLATFORM_METRICS) | set(LOCAL_ORDER_METRICS)
    found: List[str] = []

    def walk(node: Any, level: int) -> None:
        if level <= 0:
            return
        if isinstance(node, list):
            for entry in node[:5]:
                walk(entry, level - 1)
            return
        if not isinstance(node, dict):
            return
        for key, val in node.items():
            name = _text(key)
            lowered = name.lower()
            if name not in known and any(hint in lowered for hint in _METRIC_HINTS):
                if name and name not in found:
                    found.append(name)
                continue
            if isinstance(val, (dict, list)):
                walk(val, level - 1)

    walk(value, depth)
    return found[:6]


def _accepts_item_id(method: Any) -> Optional[bool]:
    """这个方法收得下 item_id 吗？`None` = 判断不了（按「收得下」处理）。"""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return None
    for param in signature.parameters.values():
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD,
                          param.VAR_POSITIONAL):
            return True
        if param.kind == param.KEYWORD_ONLY and param.name in ID_KEYS + ('item',):
            return True
    return False


def _call_item_stats(client: Any, item_id: str) -> Tuple[str, Any, str, bool]:
    """调协议层的商品数据接口。返回 `('ok'|'expired'|'failed', 值, 原因, 是否按 item 查)`。

    签名还没定（task-36 在调研），所以先看签名再决定要不要带 `item_id`；**只有签名
    判断不出来时**才退回「先带再不带」两次尝试。

    两处「把真实原因吞掉」的写法别再改回去（独立审查 D1）：
    1. 签名明说收得下 `item_id` 时，不再补一次「不带参数」的调用。真协议层
       `xianyu/client.py::item_stats` 收到空 `item_id` 直接失败，第二次调用会把
       「第一次为什么失败」盖成「成功但没有内容」，端到端只剩一句「认不出」。
    2. `CALL_OK` 但值是 `None` 也算失败（`parse_item_stats` 认不出就返回 None），
       并保留第一次的真实 reason，而不是当成一次成功的空响应。

    **第四项很重要**：不带参数拿到的是账号级响应，不能直接当成这条商品的数据。
    """
    method = getattr(client, 'item_stats')
    accepts = _accepts_item_id(method)
    if accepts is False:
        attempts: Sequence[Tuple[bool, Tuple[Any, ...]]] = ((False, ()),)
    elif accepts is None:
        attempts = ((True, (item_id,)), (False, ()))
    else:
        attempts = ((True, (item_id,)),)
    reason = '协议层未实现 item_stats'
    for by_item, args in attempts:
        status, value = common.call_method(client, ('item_stats',), *args)
        if status == common.CALL_OK:
            if value is None:
                reason = ('平台商品数据接口没有返回可用内容（协议层未说明原因：'
                          '常见是卖家工作台会话拿不到这个接口族）')
                continue
            return 'ok', value, '', by_item
        failure = common.call_failed(status, value) or {}
        if failure.get('code') == 'verification_required':
            return 'expired', failure, '', by_item
        reason = _text(failure.get('message')) or _text(value) or '未知错误'
    return 'failed', None, reason, False


def _platform_metrics(
    client: Any, item_ids: Sequence[str], strict: bool,
    notes: Optional[List[str]] = None,
) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Optional[Dict[str, Any]], str]:
    """`(每商品的平台指标 | None, 失败信封 | None, 降级原因)`。

    `strict=True`（调用方写了 `?source=platform`）时平台失败才升级成 502；
    默认 `auto` 一律降级——本地统计还能用，不该被平台接口拖垮。

    `notes` 是可选的旁路信息（调用方传自己的 warnings 列表）：用来记「响应里
    有没认出的字段」这类**不影响成败**但用户该知道的事。

    注意：平台接口是**按商品**调的，所以商品多的账号会有 N 次调用。这是平台接口
    只有商品粒度时的必然代价；真要优化得等协议层给出账号级接口。
    """
    if not callable(getattr(client, 'item_stats', None)):
        if strict:
            return None, common.not_ready('协议层尚未提供商品数据接口（client.item_stats）'), ''
        return None, None, WARN_NO_PLATFORM
    per_item: Dict[str, Dict[str, Any]] = {}
    for item_id in item_ids:
        kind, value, reason, by_item = _call_item_stats(client, item_id)
        if kind == 'ok':
            if by_item:
                # 按商品查，但响应里明说属于**别的**商品（或别的商品的数据被当成了
                # 账号汇总）→ 认领不一致，宁可降级也不能张冠李戴。
                claimed = _claimed_ids(value)
                if claimed and item_id not in claimed:
                    if strict:
                        return None, common.fail(
                            f'平台返回的商品数据不属于请求的商品 {item_id}'
                            f'（响应里是 {"/".join(sorted(claimed))[:80]}）', 'protocol_error'), ''
                    return None, None, WARN_PLATFORM_MISMATCH.format(
                        item_id=item_id, claimed='/'.join(sorted(claimed))[:80])
                # 响应可能是「按 item_id 分桶的整本账」，也可能是这一条商品的指标
                metrics = _platform_for_item(value, item_id) or _normalize_platform(value)
            else:
                metrics = _platform_for_item(value, item_id)
            if metrics is None:
                if strict:
                    return None, common.fail(
                        f'平台返回的商品数据无法识别: {_text(value)[:200]}', 'protocol_error'), ''
                return None, None, WARN_PLATFORM_UNPARSED
            suspicious = _suspect_metric_keys(value)
            if suspicious and notes is not None:
                note = WARN_PLATFORM_PARTIAL.format(keys='、'.join(suspicious))
                if note not in notes:
                    notes.append(note)
            per_item[item_id] = metrics
            continue
        if kind == 'expired':
            return None, value, ''
        if strict:
            return None, common.fail(f'平台商品数据接口调用失败: {reason}', 'protocol_error'), ''
        return None, None, WARN_PLATFORM_FAILED.format(reason=reason)
    return per_item, None, ''


# --------------------------------------------------------------------------- 组装


def _merge(item_id: str, row: Any, chats: Dict[str, Any], orders: Dict[str, Any],
           orders_synced: bool, platform: Dict[str, Any]) -> Dict[str, Any]:
    chat = chats.get(item_id) or {}
    order = orders.get(item_id) or {}

    def local(key: str) -> Optional[int]:
        if key in ('conversations', 'messages'):
            return _count_or_none(chat.get(key))
        return _count_or_none(order.get(key)) if orders_synced else None

    def pick(key: str) -> Optional[float]:
        """平台有就用平台（更新、更权威），否则用本地真值，都没有 → None。"""
        number = _number(platform.get(key))
        if number is not None:
            return _tidy(number)
        return local(key)

    return {
        'item_id': item_id,
        'title': _text(row['title']),
        'images': _images_of(row['images']),
        'price': _price_of(row['price']),
        'status': _text(row['status']) or 'unknown',
        # 平台指标：本地拿不到 → None（前端显示「-」）
        'exposure': _tidy(_number(platform.get('exposure'))),
        'visits': _tidy(_number(platform.get('visits'))),
        'visitors': _tidy(_number(platform.get('visitors'))),
        # 「想要人数」平台给了就必须带出来：别名表认、协议层也解析，只在这里丢掉
        # 等于「平台给了、我们假装没看见」（独立审查 D2）。
        'wants': _tidy(_number(platform.get('wants'))),
        # 订单指标：本地 orders 表可算，平台给了就优先用平台的
        'orders': pick('orders'),
        'buyers': pick('buyers'),
        'paid_orders': pick('paid_orders'),
        # 契约外的本地附加键（会话口径），前端不用也不影响
        'conversations': local('conversations'),
        'messages': local('messages'),
        'last_message_at': chat.get('last_message_at') or None,
    }


def account_stats(account_id: str = '', item_id: str = '', source: str = '') -> Dict[str, Any]:
    """`GET /api/items/{account_id}/stats` 的服务层实现。"""
    account = _text(account_id).strip()
    if not account:
        return common.fail('缺少账号 id', 'bad_request')
    if not _account_exists(account):
        return common.fail(f'账号不存在: {account}', 'not_found')

    wanted_item = _text(item_id).strip()
    rows = _item_rows(account, wanted_item)
    if wanted_item and not rows:
        return common.fail(f'商品不存在: {wanted_item}', 'not_found')

    mode = _text(source).strip().lower() or 'auto'
    if mode not in SOURCE_MODES:
        return common.fail(
            f'不支持的 source: {source}（可选 {"/".join(SOURCE_MODES)}）', 'invalid_fields')

    warnings: List[str] = []
    platform: Dict[str, Dict[str, Any]] = {}
    data_source = SOURCE_LOCAL

    if mode != 'local':
        # 只有真要碰平台时才校验登录态：`source=local` 不调平台，没 Cookie 也该能用
        credential_failure = _credential_failure(account)
        if credential_failure is not None:
            return credential_failure
        client, client_failure = common.get_client(account)
        if client is None:
            return client_failure or common.not_ready()
        # 限流：见 `PLATFORM_ITEM_LIMIT` 的注释。`?item_id=` 查单件时天然只查一件。
        platform_rows = rows[:PLATFORM_ITEM_LIMIT]
        if len(rows) > len(platform_rows):
            warnings.append(WARN_PLATFORM_CAPPED.format(done=len(platform_rows), total=len(rows)))
        found, failure, reason = _platform_metrics(
            client, [str(r['item_id']) for r in platform_rows], mode == 'platform', warnings)
        if failure is not None:
            return failure
        if found is None:
            if reason:
                warnings.append(reason)
        else:
            platform = found
            data_source = SOURCE_PLATFORM

    chats = _local_chat_metrics(account)
    orders, orders_synced = _local_order_metrics(account, [str(r['item_id']) for r in rows])
    if not orders_synced:
        warnings.append(WARN_NO_ORDERS)
    unlinked = _unlinked_order_count(account)
    if unlinked:
        warnings.append(WARN_UNLINKED_ORDERS.format(count=unlinked))

    items = [
        _merge(str(row['item_id']), row, chats, orders, orders_synced, platform.get(str(row['item_id'])) or {})
        for row in rows
    ]
    return {
        'items': items,
        'total': len(items),
        'account_id': account,
        'updated_at': _now_ms(),
        'source': data_source,
        'warnings': warnings,
        'metrics': {
            'platform': list(PLATFORM_METRICS),
            'local': list(LOCAL_ORDER_METRICS) + list(LOCAL_CHAT_METRICS),
        },
    }
