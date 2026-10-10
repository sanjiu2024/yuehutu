"""营销页只读服务（`/marketing`）。

四条只读路由（对外 ``kind`` → 路由）：``items`` → ``items``、``tasks`` → ``tasks``、
``competitiveness`` → ``price-competitiveness``、``records`` → ``records``。

三条硬规则（与 `services/datacenter.py` / `services/promotion.py` 同口径）：

1. **平台没返回的指标一律 `None`**，前端显示「-」，**禁止 0 顶替**。0 会让用户以为
   「真的一个都没有」。协议层的 `find_number` 认不出就返回 `None`。
2. **`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 不是掉线**：它只说明这个账号对营销业务没权限。
   协议层 `_collect` 在「**所有**接口都因无权限失败」时**不抛异常** —— 那是平台给的
   有效答复，如实返回一屏 `None` + 人话 warning（200）。只有出现链路 / 签名 / 登录态
   故障才抛，翻成 502 `protocol_error`（或 409 + `need_relogin`）。本模块据此**绝不**
   因为无权限而写 `need_relogin`。
3. **只读**：协议层 `WRITE_APIS` 里的写接口没有任何调用点；路由层也只注册 GET。

协议层在 `backend/app/xianyu/marketing.py`（模块级 async 函数，调用方传 client），
经 `common.protocol_module('marketing')` 懒导入 —— 协议层不存在时给 `not_ready`（503），
而不是把「没装协议层」当成「指标是 0」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ..core import db
from . import common

#: 四个页面各自的协议函数名 = 对外 kind。
KINDS: Tuple[str, ...] = ('items', 'tasks', 'competitiveness', 'records')

#: kind → 协议模块里「本页契约键」元组的属性名。契约键以协议层为唯一真相，
#: 服务层不再抄一份（抄了就会和别名表一起漂移）。
_METRIC_KEYS_ATTR = {
    'items': 'ITEMS_METRICS',
    'tasks': 'INFO_METRICS',
    'competitiveness': 'COMPETITIVENESS_METRICS',
    'records': 'RECORDS_METRICS',
}

#: kind → 列表字段名（协议层已归一化，服务层只是原样透出给前端）。
_LIST_KEY = {
    'items': 'items',
    'tasks': 'tasks',
    'competitiveness': 'items',
    'records': 'records',
}

#: 协议层的错误标签 → 用户看得懂的名字。warnings 是给用户看的，不能漏内部调用名。
_LABELS = {
    'items': '营销商品列表',
    'tasks': '营销任务',
    'competitiveness': '价格竞争力',
    'records': '操作记录',
}

#: kind → 用户看得懂的名字（拼 not_ready / 失败文案时用）。
_KIND_NAMES = {
    'items': '营销商品列表',
    'tasks': '营销任务',
    'competitiveness': '价格竞争力',
    'records': '操作记录',
}

#: 路由里的 hyphen 形式 → 对外 kind（`price-competitiveness` 不是合法 Python 标识符）。
KIND_ALIASES = {'price-competitiveness': 'competitiveness'}


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


def _page(page: Any, size: Any):
    """分页校验：只做上限保护，避免一次拉爆平台接口。"""
    try:
        page_no = max(1, int(_text(page).strip() or 1))
        page_size = int(_text(size).strip() or 20)
    except (TypeError, ValueError):
        return None, common.fail('分页参数必须是整数', 'invalid_fields')
    if page_size < 1 or page_size > 50:
        return None, common.fail('每页条数必须在 1~50 之间', 'invalid_fields')
    return (page_no, page_size), None


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


def _metrics_problem(module: Any, kind: str, payload: Any):
    """协议层不给 `metrics`、给空 `{}`、或缺契约键 → 协议异常（502），不是「成功但没数据」。

    **为什么必须按契约键校验**：`{}` 是 dict，`isinstance(..., dict)` 挡不住它；而契约
    承诺「平台没给的键**也一定出现**（值为 `null`）」，所以缺键就是协议层坏了，必须 502
    让人看见 —— 200 + 一屏 `-` 会让用户以为「营销页真的一条数据都没有」。
    """
    if not isinstance(payload, dict):
        return common.fail('营销页协议层没有返回指标字段', 'protocol_error')
    metrics = payload.get('metrics')
    if not isinstance(metrics, dict) or not metrics:
        return common.fail('营销页协议层没有返回指标字段', 'protocol_error')
    required = set(getattr(module, _METRIC_KEYS_ATTR.get(kind, ''), None) or ())
    if required and not required <= set(metrics):
        missing = sorted(required - set(metrics))
        return common.fail(f'营销页协议层缺指标字段：{", ".join(missing[:5])}', 'protocol_error')
    return None


def _call(kind: str, client: Any, *args: Any):
    """调协议层的 `kind` 同名函数。

    协议层缺失 → `not_ready`（503）；协议层抛异常 → `call_failed` 翻信封
    （登录态过期给 409 + `need_relogin`，其余给 502 `protocol_error`）。
    """
    module = common.protocol_module('marketing')
    if module is None:
        return None, common.not_ready('协议层未提供营销页接口（backend.app.xianyu.marketing）')
    status, value = common.call_method(_BoundProtocol(module, client), (kind,), client, *args)
    if status == common.CALL_OK:
        problem = _metrics_problem(module, kind, value)
        return (None, problem) if problem is not None else (value, None)
    failure = common.call_failed(status, value)
    return None, failure or common.fail(
        f'{_KIND_NAMES.get(kind, kind)}接口调用失败', 'protocol_error')


def _warnings(errors: Dict[str, str]) -> list:
    """把逐接口错误翻成人话。无权限单独说，因为「一屏 -」的根因就是它。"""
    messages = []
    for label, text in (errors or {}).items():
        name = _LABELS.get(label, label)
        if 'UNAUTHORIZED' in text or '无权限' in text:
            messages.append(
                f'平台拒绝访问「{name}」接口：这个账号没有「营销」业务权限，'
                '对应指标显示 -。换个有营销权限的卖家账号再试。'
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
        'warnings': _warnings(payload.get('errors') or {}),
        'source': 'platform',
        'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
    }
    result[_LIST_KEY.get(kind, 'items')] = payload.get(_LIST_KEY.get(kind, 'items'))
    if payload.get('suspect_keys'):
        result['unrecognized_keys'] = payload['suspect_keys']
    result.update(extra)
    return result


def _resolve(kind: str) -> str:
    return KIND_ALIASES.get(kind, kind)


def _page_request(kind: str, account_id: str, page: Any = '', size: Any = '',
                  seller_id: str = '', item_ids: Any = None) -> Dict[str, Any]:
    kind = _resolve(kind)
    if kind not in KINDS:
        return common.fail(f'未知的营销页接口 {kind}', 'not_found')
    if kind in ('items', 'records'):
        pager, failure = _page(page, size)
        if failure:
            return failure
        args: Tuple[Any, ...] = (pager[0], pager[1], seller_id)
        extra = {'page': pager[0], 'page_size': pager[1]}
    elif kind == 'competitiveness':
        args = (item_ids, seller_id)
        extra = {}
    else:
        args = (seller_id,)
        extra = {}
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(kind, client, *args)
    if failure:
        return failure
    return _wrap(kind, account_id, payload, extra)


def items(account_id: str, page: Any = '', size: Any = '', seller_id: str = '') -> Dict[str, Any]:
    """营销商品列表（只读）。"""
    return _page_request('items', account_id, page, size, seller_id)


def tasks(account_id: str, seller_id: str = '') -> Dict[str, Any]:
    """营销信息 / 任务（只读）。"""
    return _page_request('tasks', account_id, '', '', seller_id)


def price_competitiveness(account_id: str, item_ids: Any = None,
                          seller_id: str = '') -> Dict[str, Any]:
    """价格竞争力（只读）。"""
    return _page_request('price-competitiveness', account_id, '', '', seller_id, item_ids)


def records(account_id: str, page: Any = '', size: Any = '',
            seller_id: str = '') -> Dict[str, Any]:
    """操作记录（只读）。"""
    return _page_request('records', account_id, page, size, seller_id)
