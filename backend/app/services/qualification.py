"""资质管理服务层（超级擦亮「资质管理」页）。

对外契约见 ``docs/contracts/qualification.md``；HTTP 层见 ``api/qualification.py``。

- **只读**：``detail`` / ``submitted_records`` / ``categories`` / ``templates`` / ``reusable``
- **写平台（不可逆）**：``submit_packages`` / ``oss_credential`` —— 入口第一个判断就是
  ``confirm is True``，否则 ``confirm_required``（400），**连库都不查、一次平台请求都不发**。

三条硬规则（与 `services/marketing.py` / `services/promotion.py` 同口径）：

1. **平台没返回的字段一律 ``None``**，前端显示「-」，**禁止 0 顶替**（0 是真实值）。
   列表同理但要分清：平台给了**空列表**就是 ``[]``（「确实一条都没有」，该账号真机就是
   「暂无数据」），**没有这个键**才是 ``None``。``total`` 认不出就是 ``None``，绝不兜 0。
2. **无权限 ≠ 掉线**：``FAIL_BIZ_IDLE_USER_UNAUTHORIZED`` 这类业务拒绝翻成 502
   ``protocol_error`` 如实透出，**不写** ``need_relogin``；只有 Cookie 根本没了 /
   库里已标 ``need_relogin`` 才写（那是登录问题，不是权限问题）。
3. **写操作不可逆**：资质闸门比账号检查还前 —— 没确认时不许有任何副作用（连
   ``SELECT`` 都不做）。

协议层在 `backend/app/xianyu/qualification.py`（模块级 async 函数、调用方传 client），
经 ``common.protocol_module('qualification')`` 懒导入 —— 协议层不存在时给 ``not_ready``
（503），而不是把「没装协议层」当成「资质为空」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core import db
from . import common

#: 协议层函数名 → 用户看得懂的名字（拼 not_ready / 失败文案时用）。
_LABELS = {
    'detail': '资质详情',
    'submitted_records': '已提交资质记录',
    'allow_audit_categories': '允许的审核类目',
    'templates': '资质模板',
    'reusable_qualification': '可复用资质',
    'submit_packages': '提交资质',
    'oss_credential': '申请 OSS 上传凭证',
}

#: 列表字段候选名（做法与 `services/marketing.py::LIST_ALIASES` 一致：认不出就是 None）。
_LIST_KEYS: Tuple[str, ...] = (
    'list', 'records', 'recordList', 'result', 'items', 'data',
    'templates', 'categories', 'qualifications', 'qualificationList',
)

#: 总数候选名。**认不出返回 None**，绝不兜 0。
_TOTAL_KEYS: Tuple[str, ...] = ('total', 'totalCount', 'count')


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """Cookie / 登录态前置检查（与 `services/marketing.py:73` 同一份口径）。"""
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

    **为什么必须包一层**（与 `services/marketing.py:119` 同一理由）：``common.call_method``
    在协议层抛登录态过期时要用**第一个参数**回写 ``need_relogin``（读它的 ``cookie_id`` /
    ``account_id``）。直接传协议模块的话，模块身上没有账号身份，掉线就写不回库里。
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


def _call(name: str, client: Any, *args: Any, **kwargs: Any):
    """调协议层的同名函数。

    协议层缺失 → ``not_ready``（503）；协议层抛异常 → ``call_failed`` 翻信封
    （登录态过期给 409 + ``need_relogin``，其余给 502 ``protocol_error``）。
    """
    module = common.protocol_module('qualification')
    if module is None:
        return None, common.not_ready(
            '协议层未提供资质管理接口（backend.app.xianyu.qualification）')
    bound = _BoundProtocol(module, client)
    status, value = common.call_method(bound, (name,), client, *args, **kwargs)
    if status == common.CALL_OK:
        return value, None
    failure = common.call_failed(status, value)
    label = _LABELS.get(name, name)
    return None, failure or common.fail(f'{label}接口调用失败', 'protocol_error')


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


def _first_list(payload: Any, keys: Tuple[str, ...] = _LIST_KEYS) -> Optional[List[Any]]:
    """按候选键找列表：**找不到返回 ``None``**；找到空列表就返回 ``[]``（真实值）。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for value in payload.values():  # 平台把列表包在 data/xxxVO 里时再挖一层
        if isinstance(value, list):
            return value
    return None


def _first_total(payload: Any) -> Optional[int]:
    """总数：只认整数（或纯数字字符串）。**认不出返回 ``None``，不兜 0**。"""
    if not isinstance(payload, dict):
        return None
    for key in _TOTAL_KEYS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip('-').isdigit():
            return int(value.strip())
    return None


def _wrap(account_id: str, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        'account_id': account_id,
        'source': 'platform',
        'fetched_at': _now(),
    }
    result.update(extra)
    return result


def _business_body(request: Any):
    """写接口的业务对象：**原样透传**，只校验「是 dict 且非空」。

    为什么不在这里补默认键：``requestJson`` 的字段名**没有 bundle 证据**（见协议层
    docstring）。凭空补键 = 编平台协议，宁可让调用方显式给。
    """
    if request is None:
        return None, common.fail(
            '资质业务参数不能为空：平台要求的字段名尚无 bundle 证据，必须由调用方显式给出',
            'invalid_fields')
    if not isinstance(request, dict):
        return None, common.fail('资质业务参数必须是 JSON 对象', 'invalid_fields')
    if not request:
        return None, common.fail(
            '资质业务参数不能是空对象：平台要求的字段名尚无 bundle 证据，必须由调用方显式给出',
            'invalid_fields')
    return dict(request), None


# --------------------------------------------------------------------------- 只读

def detail(account_id: str) -> Dict[str, Any]:
    """资质详情。平台 ``data`` 非 dict → ``detail: None``（不造空 dict）。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call('detail', client, None)
    if failure:
        return failure
    return _wrap(account_id, detail=payload if isinstance(payload, dict) else None)


def submitted_records(account_id: str, page: Any = '', size: Any = '') -> Dict[str, Any]:
    """已提交的资质记录。``records`` 认不出为 ``None``；``total`` 认不出为 ``None``。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    page_args, failure = _page(page, size)
    if failure:
        return failure
    # 证据缺口：pageNum/pageSize 沿用 `client.py:909 ad_campaigns` 的同一命名，
    # 但**没有** qualification 接口的 bundle 调用点证据。
    body = {'pageNum': page_args[0], 'pageSize': page_args[1]}
    payload, failure = _call('submitted_records', client, body)
    if failure:
        return failure
    return _wrap(account_id, records=_first_list(payload), total=_first_total(payload))


def categories(account_id: str) -> Dict[str, Any]:
    """允许提交资质的审核类目。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call('allow_audit_categories', client, None)
    if failure:
        return failure
    return _wrap(account_id, categories=_first_list(payload))


def templates(account_id: str) -> Dict[str, Any]:
    """资质模板列表。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call('templates', client, None)
    if failure:
        return failure
    return _wrap(account_id, templates=_first_list(payload))


def reusable(account_id: str) -> Dict[str, Any]:
    """可复用资质（历史资质复用）。"""
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call('reusable_qualification', client, None)
    if failure:
        return failure
    return _wrap(account_id, qualifications=_first_list(payload))


# --------------------------------------------------------------------------- 写平台

def submit_packages(account_id: str, request: Any = None, confirm: Any = False
                    ) -> Dict[str, Any]:
    """**写平台**：提交资质材料。

    ``confirm is not True`` → ``confirm_required``（400），**没有任何副作用**。
    业务对象原样进 ``requestJson``，服务层不翻译字段（无 bundle 证据）。
    """
    problem = common.confirm_required(
        '提交资质', confirm, strict=True,
        message='提交资质会真实改变账号的资质状态（不可逆），必须显式确认 confirm=true')
    if problem is not None:
        return problem
    body, failure = _business_body(request)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call('submit_packages', client, body, confirm=True)
    if failure:
        return failure
    return _wrap(account_id, submitted=payload if isinstance(payload, dict) else None)


def oss_credential(account_id: str, request: Any = None, confirm: Any = False
                   ) -> Dict[str, Any]:
    """**写平台**：取 OSS 上传凭证（上传资质材料的第一步）。同样需要 ``confirm=True``。"""
    problem = common.confirm_required(
        '申请 OSS 上传凭证', confirm, strict=True,
        message='申请 OSS 上传凭证是提交资质的第一步，必须显式确认 confirm=true')
    if problem is not None:
        return problem
    body, failure = _business_body(request)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call('oss_credential', client, body, confirm=True)
    if failure:
        return failure
    return _wrap(account_id, credential=payload if isinstance(payload, dict) else None)
