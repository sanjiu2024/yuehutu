"""商品读 5 条接口的只读服务（第 63 批 task-23）。

对应 `docs/contracts/items.md` §7 的五条 GET。三条硬规则：

1. **只读**：不落库、不播事件、没有 `confirm` 闸门；写方法一律 405。
2. **平台没给的字段一律 `None` / 空列表**，**禁止 0 或空串顶替** —— 认不出就是认不出，
   让调用方自己分辨「平台没给」与「链路坏了」（与第 60 批「发布数据源」同口径）。
3. **平台的业务码原样带出**（`platform_code` / `platform_message`）：平台回
   `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` 时**仍是 200** —— 那是平台的有效答复（链路通、
   账号没权限），不是 502。

协议层在 `backend/app/xianyu/items_read.py`（模块级 async 函数，调用方传 client），
通过 `common.protocol_module('items_read')` 懒导入；协议层不存在时给 `not_ready`(503)，
**不是** 200 + 空列表。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core import db
from . import common

#: 协议模块名（`common.protocol_module('items_read')` → `backend.app.xianyu.items_read`）
MODULE = 'items_read'

#: 对外 kind（= 协议层函数名 = 信封里的 `kind`）
KIND_SEARCH = 'search_items'
KIND_CATEGORY = 'category_query'
KIND_OPERATE_REPORT = 'operate_report'
KIND_SEARCH_TERMS = 'search_terms'
KIND_PREGET = 'preget'

#: 分页边界。官方只给 10 / 20 / 50 三档，这里只做上下界校验（不拒绝其它值，
#: 便于真机探针试别的档位）；越界在建客户端**之前**拦下，回 400 `invalid_fields`。
SEARCH_DEFAULT_PAGE_SIZE = 20
SEARCH_MAX_PAGE_SIZE = 50
SEARCH_MAX_PAGE = 100000
SEARCH_ORDER_TYPES = ('asc', 'desc')
#: 与协议层同一份默认值（`bizType` / `itemStatus`）
SEARCH_BIZ_TYPE = 'commonPro'
SEARCH_DEFAULT_STATUS = '0,-9'


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    """Cookie / 登录态前置检查，与 `services/datacenter.py` / `services/promotion.py` 同口径。

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
    """账号存在 → Cookie/登录态 → 建客户端（与 `services/datacenter.py::_client` 逐字同形）。"""
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


class _BoundProtocol:
    """把「模块级协议函数」和「调用它的 client」绑成可交给 `call_method` 的对象。

    与 `services/datacenter.py::_BoundProtocol` **逐字同形**：`common.call_method` 在协议层
    抛登录态过期时要用**第一个参数**去回写 `need_relogin`（读它的 `cookie_id` /
    `account_id`）。直接传协议模块的话，模块身上没有账号身份，掉线就写不回库里 ——
    界面会一直显示「已登录」，用户只能自己猜到要重新扫码。
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


def _prepare(account_id: str):
    """5 个只读接口共用的前置检查：account_id 非空 → 账号存在 → Cookie/登录态 → 建客户端。"""
    if not _text(account_id).strip():
        return None, common.fail('account_id 不能为空', 'bad_request')
    client, failure = _client(account_id)
    if client is None:
        return None, failure or common.not_ready()
    return client, None


def _int_arg(label: str, value: Any, minimum: int, maximum: int):
    """整数参数校验（与 `services/datacenter.py::_int_arg` 同口径）。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None, common.fail(f'{label}必须是整数', 'invalid_fields')
    if number < minimum or number > maximum:
        return None, common.fail(
            f'{label}必须在 {minimum}~{maximum} 之间', 'invalid_fields')
    return number, None


def _json_params(value: Any):
    """查询串里的 `params`：JSON 对象字符串 → dict（空 = `{}`）。"""
    text = _text(value).strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, common.fail('params 必须是 JSON 对象字符串', 'invalid_fields')
    if not isinstance(parsed, dict):
        return None, common.fail('params 必须是 JSON 对象字符串', 'invalid_fields')
    return parsed, None


def _read_call(name: str, client: Any, *args: Any):
    """调协议层的 `name(client, ...)`，返回 `(payload, failure)`。

    * 协议模块不在 → `not_ready`(503)（不是「没有数据」）；
    * 协议层抛异常 → `common.call_failed` 翻信封（登录态过期给 409 + `need_relogin`，
      其余 502 `protocol_error`）；与 `services/datacenter.py::_read_call` 逐字同口径。
    """
    module = common.protocol_module(MODULE)
    if module is None:
        return None, common.not_ready(
            '协议层未提供商品读接口（backend.app.xianyu.items_read）')
    status, value = common.call_method(_BoundProtocol(module, client), (name,), client, *args)
    if status != common.CALL_OK:
        failure = common.call_failed(status, value)
        return None, failure or common.fail('商品读接口调用失败', 'protocol_error')
    if not isinstance(value, dict):
        return None, common.fail('商品读协议层没有返回结果', 'protocol_error')
    return value, None


def _envelope(account_id: str, kind: str, payload: Dict[str, Any],
              fields: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """五条接口共用的信封：账号 + kind + 平台码 + 拉取时间（AI/前端只要认一次）。

    `platform_code` 拿不到时是 `None`（不是空串）；`platform_message` 只在平台给了
    原文时出现 —— 与第 60 批「发布数据源」的信封逐字同口径。
    """
    result: Dict[str, Any] = {
        'account_id': account_id,
        'kind': kind,
        'api': _text(payload.get('api')) or None,
        'platform_code': _text(payload.get('code')) or None,
        'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
        'source': 'platform',
    }
    message = _text(payload.get('message'))
    if message:
        result['platform_message'] = message
    for key in fields:
        result[key] = payload.get(key)
    return result


# --------------------------------------------------------------------------- 五个只读接口
def search_items(account_id: str, keyword: Any = '', page: Any = 1, size: Any = None,
                 item_status: Any = '', biz_type: Any = '', order_field: Any = '',
                 order_type: Any = '', search_request: Any = '') -> Any:
    """商品列表：服务端搜索 + 分页 + 排序（`...pc.common.item.search`）。

    分页/排序**由平台做**，所以不进缓存：翻页时给你上一页的缓存就是「点了没反应」。
    校验全部在建客户端**之前**（纯本地、免费；账号掉线时也该先说「页码不合法」）。
    """
    page_number, failure = _int_arg('页码', page, 1, SEARCH_MAX_PAGE)
    if failure:
        return failure
    size_number, failure = _int_arg(
        '每页条数', SEARCH_DEFAULT_PAGE_SIZE if size in (None, '') else size,
        1, SEARCH_MAX_PAGE_SIZE)
    if failure:
        return failure
    direction = _text(order_type).strip().lower()
    if direction not in ('',) + SEARCH_ORDER_TYPES:
        return common.fail('排序方向只能是 asc 或 desc', 'invalid_fields')
    field = _text(order_field).strip()
    if direction and not field:
        return common.fail('指定排序方向时必须同时给出排序字段', 'invalid_fields')
    client, failure = _prepare(account_id)
    if failure:
        return failure
    payload, failure = _read_call(
        KIND_SEARCH, client, _text(keyword), page_number, size_number,
        _text(item_status), _text(biz_type), _text(search_request), field, direction)
    if failure:
        common.log_operation(account_id, KIND_SEARCH, '', False, failure.get('message', ''))
        return failure
    return _envelope(account_id, KIND_SEARCH, payload,
                     ('rows', 'found_key', 'total', 'sent', 'page', 'size'))


def category_query(account_id: str, params: Any = None) -> Any:
    """类目查询（`...platform.idle.category.query`，入参**透传**）。

    bundle 里只有声明、调用点 0 ⇒ 我们不猜字段名：`params` 是 JSON 对象字符串，
    给什么发什么；平台回什么就原样透出（`data` 是业务体原文）。
    """
    parsed, failure = _json_params(params)
    if failure:
        return failure
    client, failure = _prepare(account_id)
    if failure:
        return failure
    payload, failure = _read_call(KIND_CATEGORY, client, parsed)
    if failure:
        common.log_operation(account_id, KIND_CATEGORY, '', False, failure.get('message', ''))
        return failure
    return _envelope(account_id, KIND_CATEGORY, payload, ('data', 'found', 'sent'))


def operate_report(account_id: str, params: Any = None) -> Any:
    """商品操作报告（`...platform.item.operate.report.get`，入参**透传**，理由同上）。"""
    parsed, failure = _json_params(params)
    if failure:
        return failure
    client, failure = _prepare(account_id)
    if failure:
        return failure
    payload, failure = _read_call(KIND_OPERATE_REPORT, client, parsed)
    if failure:
        common.log_operation(account_id, KIND_OPERATE_REPORT, '', False,
                             failure.get('message', ''))
        return failure
    return _envelope(account_id, KIND_OPERATE_REPORT, payload, ('data', 'found', 'sent'))


def search_terms(account_id: str) -> Any:
    """筛选维度字典（`...item.pc.fix.search.term.query`，**无参**）。

    返回三个分组的 `{key, value}` 选项；平台没给的分组是**空列表**（不是 None）——
    「这个分组没有选项」与「平台改了字段名」靠 `found_groups` 分辨。
    """
    client, failure = _prepare(account_id)
    if failure:
        return failure
    payload, failure = _read_call(KIND_SEARCH_TERMS, client)
    if failure:
        common.log_operation(account_id, KIND_SEARCH_TERMS, '', False,
                             failure.get('message', ''))
        return failure
    return _envelope(account_id, KIND_SEARCH_TERMS, payload, ('terms', 'found_groups', 'sent'))


def preget(account_id: str, item_id: Any = '', source_id: Any = '',
           publish_scene: Any = '') -> Any:
    """发布/编辑前的预请求（`mtop.idle.pc.idleitem.preget`）。

    只提升两个已证实的字段（`need_up_first_hand_item` / `industry_id`），其余原样透出。
    """
    client, failure = _prepare(account_id)
    if failure:
        return failure
    payload, failure = _read_call(KIND_PREGET, client, _text(item_id), _text(source_id),
                                  _text(publish_scene))
    if failure:
        common.log_operation(account_id, KIND_PREGET, '', False, failure.get('message', ''))
        return failure
    return _envelope(account_id, KIND_PREGET, payload,
                     ('need_up_first_hand_item', 'industry_id', 'found', 'data', 'sent'))
