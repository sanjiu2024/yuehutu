"""营销页只读服务（`/marketing`）。

对外四个 ``kind``（= 路由末段）：``items``、``tasks``、``price-competitiveness``、
``records``。kind → 协议层函数名的映射见 :data:`_PROTOCOL_FUNC`
（`price-competitiveness` 带连字符，不是合法 Python 标识符，必须转名）。

三条硬规则（与 `services/datacenter.py` / `services/promotion.py` 同口径）：

1. **平台没返回的指标一律 `None`**，前端显示「-」，**禁止 0 顶替**。0 会让用户以为
   「真的一个都没有」。协议层的 `find_number` 认不出就返回 `None`。
2. **`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 不是掉线**：它只说明这个账号对营销业务没权限。
   协议层 `_collect` 在「**所有**接口都因无权限失败」时**不抛异常** —— 那是平台给的
   有效答复，如实返回一屏 `None` + 人话 warning（200）。只有出现链路 / 签名 / 登录态
   故障才抛，翻成 502 `protocol_error`（或 409 + `need_relogin`）。本模块据此**绝不**
   因为无权限而写 `need_relogin`。
3. **只读**：协议层 `WRITE_APIS` 里的两个写接口没有任何调用点；路由层也只注册 GET。

协议层在 `backend/app/xianyu/marketing.py`（模块级 async 函数，调用方传 client），
经 `common.protocol_module('marketing')` 懒导入 —— 协议层不存在时给 `not_ready`（503），
而不是把「没装协议层」当成「指标是 0」。

**置信度提醒**：四个接口的参数键与响应字段都是从官方 bundle 反推的，置信度**全部只有
「中」**（详见 `docs/contracts/marketing.md`）。所以本模块对「参数长什么样」做**严格校验**
（错的枚举值直接 400，不去猜），对「响应里有什么」则**一律容忍**（认不出就是 `None`）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ..core import db
from ..xianyu import marketing as proto
from . import common, platform_errors

#: 四个页面各自的对外 kind（= 路由末段）。
KINDS: Tuple[str, ...] = ('items', 'tasks', 'price-competitiveness', 'records')

#: 对外 kind → 协议层函数名。`common.call_method` 拿它去 `getattr(协议模块, 名字)`。
_PROTOCOL_FUNC = {
    'items': 'item_metrics',
    'tasks': 'tasks',
    'price-competitiveness': 'competitiveness',
    'records': 'operation_records',
}

#: 容错别名（订正期间文档里短暂用过这两个长名；对外仍以 :data:`KINDS` 为准）。
KIND_ALIASES = {
    'item-metrics': 'items',
    'competitiveness': 'price-competitiveness',
    'operation-records': 'records',
}

#: kind → 该页在协议层声明的契约键 ``(payload 分类, 元组属性名)``。
#:
#: **契约键以协议层为唯一真相**，服务层不抄一份（抄了就会和别名表一起漂移）。
#: `records` 是空元组：官方 bundle 里**条目内部字段零证据**，协议层只承诺
#: 「`records` 这个键一定在」，故它走 :func:`_contract_problem` 的兜底分支。
_CONTRACT = {
    'items': (('metrics', 'ITEM_METRICS'),),
    'tasks': (('metrics', 'TASK_METRICS'), ('flags', 'TASK_FLAGS')),
    'price-competitiveness': (('texts', 'COMPETITIVENESS_TEXTS'),),
    'records': (),
}

#: kind → 列表字段名（协议层已归一化，服务层只是原样透出给前端）。
_LIST_KEY = {
    'items': 'items',
    'tasks': 'tasks',
    'price-competitiveness': None,
    'records': 'records',
}

#: kind → 用户看得懂的名字（拼 warning / not_ready / 失败文案时用）。
_LABELS = {
    'items': '营销商品数据',
    'tasks': '营销任务列表',
    'price-competitiveness': '价格竞争力',
    'records': '操作记录',
}

#: 分页默认值对齐官方页面（`pageSize` 三处佐证都是 10）。
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """Cookie / 登录态前置检查，与 `services/datacenter.py` 同口径。

    **注意这里给 `need_relogin` 是合规的**：Cookie 根本没了 / 库里已标 `need_relogin`
    是**登录问题**，不是「平台拒绝访问业务」。平台侧的 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED`
    走的是另一条路（协议层不抛 → 200 + warning），两条不会互相污染。
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


class _BoundProtocol:
    """把「模块级协议函数」和「调用它的 client」绑成一个可交给 `call_method` 的对象。

    **为什么必须包一层**：`common.call_method` 在协议层抛登录态过期时，会用**第一个
    参数**去回写 `need_relogin`（读它的 `cookie_id` / `account_id`）。直接传协议模块
    的话，模块身上没有账号身份，掉线就写不回库里 —— 真机表现就是「协议报会话过期，
    界面还显示已登录」，用户只能自己猜到要重新扫码。
    """

    def __init__(self, module: Any, client: Any) -> None:
        self._module = module
        self._client = client

    @property
    def account_id(self) -> str:
        return str(getattr(self._client, 'account_id', '')
                   or getattr(self._client, 'persist_account_id', '') or '')

    @property
    def cookie_id(self) -> str:
        return str(getattr(self._client, 'cookie_id', '') or '')

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


def _contract_problem(module: Any, kind: str, payload: Any):
    """协议层不给契约键、给空 dict、或缺声明的键 → 协议异常（502），不是「成功但没数据」。

    **为什么必须按契约键校验**：`{}` 是 dict，`isinstance(..., dict)` 挡不住它；而契约
    承诺「平台没给的键**也一定出现**（值为 `null`）」，所以缺键就是协议层坏了，必须 502
    让人看见 —— 200 + 一屏 `-` 会让用户以为「营销页真的一条数据都没有」。
    """
    if not isinstance(payload, dict):
        return common.fail('营销页协议层没有返回结构化数据', 'protocol_error')
    spec = _CONTRACT.get(kind) or ()
    for category, attr in spec:
        block = payload.get(category)
        if not isinstance(block, dict) or not block:
            return common.fail('营销页协议层没有返回指标字段', 'protocol_error')
        required = set(getattr(module, attr, None) or ())
        if required and not required <= set(block):
            missing = sorted(required - set(block))
            return common.fail(
                f'营销页协议层缺字段：{", ".join(missing[:5])}', 'protocol_error')
    if not spec:
        # `records` 没有可校验的指标键，改为钉住「列表键一定在」。
        key = _LIST_KEY.get(kind) or 'items'
        if key not in payload:
            return common.fail(f'营销页协议层缺字段：{key}', 'protocol_error')
    return None


def _call(kind: str, client: Any, *args: Any):
    """调协议层里 :data:`_PROTOCOL_FUNC` 指定的函数。

    协议层缺失 → `not_ready`（503）；协议层抛异常 → `call_failed` 翻信封
    （登录态过期给 409 + `need_relogin`，其余给 502 `protocol_error`）。
    """
    module = common.protocol_module('marketing')
    if module is None:
        return None, common.not_ready('协议层未提供营销页接口（backend.app.xianyu.marketing）')
    name = _PROTOCOL_FUNC[kind]
    status, value = common.call_method(_BoundProtocol(module, client), (name,), client, *args)
    if status == common.CALL_OK:
        problem = _contract_problem(module, kind, value)
        return (None, problem) if problem is not None else (value, None)
    failure = common.call_failed(status, value)
    return None, failure or common.fail(
        f'{_LABELS.get(kind, kind)}接口调用失败', 'protocol_error')


#: 协议层错误字典的键是**协议函数名**（`item_metrics`），不是对外 kind
#: （`items`）。warnings 要给人看，不能漏出内部函数名，所以这里再翻一层。
_FUNC_LABELS = {_PROTOCOL_FUNC[kind]: _LABELS[kind] for kind in KINDS}


def _warnings(errors: Dict[str, str]) -> list:
    """把逐接口错误翻成人话。无权限单独说，因为「一屏 -」的根因就是它。

    与 `services/datacenter.py::_warnings` 同一口径：判据走
    `platform_errors.is_permission`、人话走 `platform_errors.permission_hint`，
    本地只补「哪个接口 / 后果」（审计问题 13）。
    """
    messages = []
    for label, text in (errors or {}).items():
        name = _FUNC_LABELS.get(label) or _LABELS.get(label) or label
        if platform_errors.is_permission(text):
            messages.append(
                f'平台拒绝访问「{name}」接口（对应指标显示 -）：'
                f'{platform_errors.permission_hint(text)}'
            )
        else:
            messages.append(f'平台「{name}」接口调用失败：{text}，对应指标显示 -。')
    return messages


def _wrap(kind: str, account_id: str, payload: Dict[str, Any],
          extra: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        'account_id': account_id,
        'kind': kind,
        'metrics': payload.get('metrics') or {},
        'texts': payload.get('texts') or {},
        'flags': payload.get('flags') or {},
        'warnings': _warnings(payload.get('errors') or {}),
        'source': 'platform',
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }
    list_key = _LIST_KEY.get(kind)
    if list_key:
        result[list_key] = payload.get(list_key)
    for key in ('page_kind', 'user_task_info'):
        if key in payload:
            result[key] = payload[key]
    if payload.get('suspect_keys'):
        result['unrecognized_keys'] = payload['suspect_keys']
    result.update(extra)
    return result


# --------------------------------------------------------------------------- 参数校验
#
# 官方 bundle 的枚举值是**有限集合**，传错只会拿到平台侧一个含糊的失败。所以这里
# 直接 400 —— 参数形态的置信度只有「中」，把错的挡在本地比让平台猜更好。


def _enum(value: Any, allowed: Tuple[str, ...], default: str, field: str):
    text = _text(value).strip()
    if not text:
        return default, None
    if text not in allowed:
        return None, common.fail(
            f'{field} 只支持 {" / ".join(allowed)}，收到的是 {text}', 'invalid_fields')
    return text, None


def _page(page: Any, size: Any):
    """分页校验：`pageNum` 从 1 起，`pageSize` 限 1~50（官方默认 10）。"""
    try:
        page_no = max(1, int(_text(page).strip() or 1))
        page_size = int(_text(size).strip() or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        return None, common.fail('分页参数必须是整数', 'invalid_fields')
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        return None, common.fail(
            f'每页条数必须在 1~{MAX_PAGE_SIZE} 之间', 'invalid_fields')
    return (page_no, page_size), None


def _csv(value: Any) -> list:
    """逗号分隔 → 去空去重的列表（`itemIdListStr` 是 JSON 字符串，由协议层再序列化）。"""
    seen, out = set(), []
    for part in _text(value).replace('，', ',').split(','):
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _json_object(value: Any, field: str):
    """把查询串里的 JSON 对象解出来（`sku_prices`）。空值 → `{}`。"""
    text = _text(value).strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None, common.fail(f'{field} 必须是 JSON 对象', 'invalid_fields')
    if not isinstance(parsed, dict):
        return None, common.fail(f'{field} 必须是 JSON 对象', 'invalid_fields')
    return parsed, None


def _required(value: Any, field: str):
    text = _text(value).strip()
    if not text:
        return None, common.fail(f'缺少必填参数 {field}', 'invalid_fields')
    return text, None


# --------------------------------------------------------------------------- 四个只读页面


def _request(kind: str, account_id: str, args: Tuple[Any, ...],
             extra: Dict[str, Any]) -> Dict[str, Any]:
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(kind, client, *args)
    if failure:
        return failure
    return _wrap(kind, account_id, payload, extra)


def item_metrics(account_id: str, item_id: Any = '', date_type: Any = '',
                 date_range: Any = '') -> Dict[str, Any]:
    """商品数据罗盘指标（只读）。

    ``date_type`` 默认 ``recent1d``；``customDate`` 时 ``date_range`` 必须是
    ``YYYYMMDD|YYYYMMDD``（官方 UI 只允许「今天 - 90 天 ~ 今天」）。
    """
    item, failure = _required(item_id, 'item_id')
    if failure:
        return failure
    dtype, failure = _enum(date_type, proto.DATE_TYPES, proto.DEFAULT_DATE_TYPE, 'date_type')
    if failure:
        return failure
    span = _text(date_range).strip()
    if dtype == 'customDate':
        parts = span.split(proto.CUSTOM_DATE_RANGE_SEPARATOR)
        if len(parts) != 2 or any(len(p) != 8 or not p.isdigit() for p in parts):
            return common.fail(
                'customDate 时 date_range 必须是 YYYYMMDD|YYYYMMDD（例如 20261001|20261007）',
                'invalid_fields')
    elif span:
        return common.fail('只有 date_type=customDate 时才需要传 date_range', 'invalid_fields')
    return _request('items', account_id, (item, dtype, span),
                    {'item_id': item, 'date_type': dtype, 'date_range': span})


def tasks(account_id: str, biz_scene: Any = '', query_scene: Any = '', page: Any = '',
          size: Any = '', item_ids: Any = '') -> Dict[str, Any]:
    """营销任务 / 商品分页（只读）。

    ``bizScene`` / ``queryScene`` 是官方枚举（见协议层 :data:`BIZ_SCENES` /
    :data:`QUERY_SCENES`）；``item_ids`` 是逗号分隔的商品 id。
    """
    scene, failure = _enum(biz_scene, proto.BIZ_SCENES, proto.DEFAULT_BIZ_SCENE, 'biz_scene')
    if failure:
        return failure
    query, failure = _enum(query_scene, proto.QUERY_SCENES, proto.DEFAULT_QUERY_SCENE,
                           'query_scene')
    if failure:
        return failure
    pager, failure = _page(page, size)
    if failure:
        return failure
    ids = _csv(item_ids)
    return _request('tasks', account_id, (scene, query, pager[0], pager[1], ids),
                    {'biz_scene': scene, 'query_scene': query,
                     'page': pager[0], 'page_size': pager[1], 'item_ids': ids})


def price_competitiveness(account_id: str, biz_scene: Any = '', item_id: Any = '',
                          sku_prices: Any = '', commission_rate: Any = '') -> Dict[str, Any]:
    """价格竞争力（只读）。响应是字符串 `trafficBoostStr`，不是数字。

    ``sku_prices`` 是 JSON 对象（``{"<skuId>": <priceYuan>}``）；键名与单位**都未验证**，
    所以服务层只做「是 JSON 对象」的形状校验，不改写值。
    """
    scene, failure = _enum(biz_scene, proto.BIZ_SCENES, proto.DEFAULT_BIZ_SCENE, 'biz_scene')
    if failure:
        return failure
    item, failure = _required(item_id, 'item_id')
    if failure:
        return failure
    prices, failure = _json_object(sku_prices, 'sku_prices')
    if failure:
        return failure
    rate = _text(commission_rate).strip()
    if rate and scene != 'TRAFFIC_ORDER_BOOST':
        return common.fail('只有 biz_scene=TRAFFIC_ORDER_BOOST 才支持 commission_rate',
                           'invalid_fields')
    return _request('price-competitiveness', account_id, (scene, item, prices, rate or None),
                    {'biz_scene': scene, 'item_id': item, 'sku_prices': prices})


def operation_records(account_id: str, task_id: Any = '', biz_scene: Any = '',
                      query_scene: Any = '') -> Dict[str, Any]:
    """任务操作记录（只读）。**无分页、无时间范围** —— 整块由 `taskId` 一次拉全量。"""
    task, failure = _required(task_id, 'task_id')
    if failure:
        return failure
    scene, failure = _enum(biz_scene, proto.BIZ_SCENES, proto.DEFAULT_BIZ_SCENE, 'biz_scene')
    if failure:
        return failure
    query, failure = _enum(query_scene, proto.QUERY_SCENES, proto.RECORD_QUERY_SCENE,
                           'query_scene')
    if failure:
        return failure
    return _request('records', account_id, (task, scene, query),
                    {'task_id': task, 'biz_scene': scene, 'query_scene': query})
