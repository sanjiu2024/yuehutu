"""草稿箱服务：列表 / 详情（只读）+ 删除（单条 / 批量，写）。

协议层见 `xianyu/draft.py`（官方 bundle 静态取证，零平台请求）。这里只做三件事：
拼请求、把平台返回翻成人能看的结果、把写操作的三步顺序钉死。

**三步顺序（写路径）**：确认 → 字段校验 → 建客户端。
- 确认在最前：未确认时连字段错都不该报，否则会误导用户「填对了就能发」；
- 字段校验在建客户端之前：纯本地、免费，账号掉线时也该先说「草稿 ID 不能为空」
  而不是「去重新登录」。
这套口径与 `services/items.py`、`services/item_templates.py` 完全一致。

**诚实边界**：
- `draft.update`（保存 / 更新草稿）**故意不实现** —— 它的 payload 是整份发布表单的
  `inputJson`，字段形状没有证据，猜一个字段名就是往用户账号里写脏草稿。
- 一次真机请求都没发过（本机账号只有 `2217970177348` 在线，且它缺部分平台业务权限）。
  字段形状与官方页面逐字一致，但「平台是否接受」仍属未验证。
- 批量删除**没有逐条回报**（官方只读 `sucCount`/`failCount`），所以 `failed[]` 只能是空的
  + 一条 warning 说明「平台没说哪几条失败」——**不编造失败明细**。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..xianyu import draft as proto
from . import common

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = 'confirmation_required'

#: 单次批量删除的 id 上限（请求体是逗号串，给个上限挡住误用）。
BATCH_WRITE_LIMIT = 200

#: 列表默认每页条数（官方草稿 tab 的选项是 10 / 20 / 50）。
DEFAULT_PAGE_SIZE = proto.PAGE_SIZES[1]


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload.setdefault('source', 'platform')
    payload.setdefault('fetched_at', _now())
    return payload


def _truthy(value: Any) -> bool:
    """只有字面量 `True` / `'true'` / `1` 才算确认（与 items / item_templates / MCP 同口径）。"""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value == 1


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰账号、不碰平台。"""
    if _truthy(confirm):
        return None
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交到闲鱼',
        CONFIRM_CODE,
    )


def _missing(label: str, value: Any) -> Optional[Dict[str, Any]]:
    if _text(value):
        return None
    return common.fail(f'{label}不能为空', 'invalid_fields')


def _clean_ids(draft_ids: Any) -> List[str]:
    """两种入参都收：UI 给数组、AI / MCP 给逗号串（`proto.normalize_draft_ids` 统一）。"""
    return proto.draft_id_list(draft_ids)


def _call_failure(call_status: str, value: Any, api: str) -> Dict[str, Any]:
    """协议层没调通（方法缺失 / 抛异常 / 登录态过期）时的失败信封。"""
    failure = common.call_failed(call_status, value) or common.fail('协议层调用失败', 'protocol_error')
    failure.setdefault('platform_errors', {
        'api': api,
        'explain': str(value) or '协议层未提供该方法',
    })
    return failure


def _platform_failure(value: Any, api: str, fallback: str) -> Dict[str, Any]:
    """平台回了信封但业务体不算成功 ⇒ 502，把平台原文透出去。

    **不判登录态**：无权限 / 参数不对都不等于掉线，别让用户白扫一次码。
    """
    reason = proto.failure_reason(value) or fallback
    logger.warning(f'[drafts] 平台拒绝 {api}: {reason}')
    return common.fail(
        f'平台接口失败: {reason}',
        'protocol_error',
        platform_errors={
            'api': api,
            'code': proto.code_of(value),
            'message': proto.message_of(value),
        },
    )


def _call(account_id: str, api: str, data: Dict[str, Any]) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """发一次 mtop 调用，返回 `(客户端返回值, 失败信封)`；成功时第二个是 `None`。"""
    client, failure = common.get_client(account_id)
    if client is None:
        return None, failure
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if status != common.CALL_OK:
        return None, _call_failure(status, value, api)
    return value, None


# --------------------------------------------------------------------------- 只读


def list_drafts(account_id: str, page: Any = 1, size: Any = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
    """草稿列表（`mtop.alibaba.idle.item.pc.draft.search`）。

    平台**明确返回空数组**时就是「一件草稿都没有」；平台连
    `itemSearchResponseList` 这个键都没给时，我们给空列表但**加 warning**，
    免得用户以为草稿被自己删光了。
    """
    try:
        api, data = proto.list_request(page, size)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, api, '平台没有返回草稿列表')

    parsed = proto.parse_list(value)
    total = parsed['total']
    warnings: List[Dict[str, Any]] = []
    if not parsed['found_key']:
        warnings.append({
            'code': 'list_key_missing',
            'message': '平台这一次没有返回草稿列表字段（itemSearchResponseList），'
                       '下面的空列表**不等于**「一件草稿都没有」，别当成 0 条。',
        })
    page_size = data['pageSize']
    return _stamp({
        'account_id': account_id,
        'api': api,
        'items': parsed['items'],
        'count': len(parsed['items']),
        'total': total,
        'page': data['pageNo'],
        'size': page_size,
        'pages': math.ceil(total / page_size) if isinstance(total, int) else None,
        'warnings': warnings,
    })


def draft_detail(account_id: str, draft_id: Any) -> Dict[str, Any]:
    """草稿详情（`...draft.get`）。

    `fields` 是平台 payload **原样**（官方用压缩黑盒 `lC` 解析，我们没有它的逆函数），
    所以键名一律不翻译、不猜。拿不到时是 `null`，不是 `{}` —— 两者含义完全不同。
    """
    problem = _missing('草稿 ID', draft_id)
    if problem is not None:
        return problem
    api, data = proto.detail_request(draft_id)

    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, api, '平台没有返回草稿内容')

    parsed = proto.parse_detail(value)
    warnings: List[Dict[str, Any]] = []
    if parsed['fields'] is None:
        warnings.append({
            'code': 'platform_returned_nothing',
            'message': '平台这一次没有返回草稿内容（契约里它是 null，不是空对象）。',
        })
    else:
        warnings.append({
            'code': 'field_names_not_pinned',
            'message': '草稿内容的字段名没有 bundle 证据，这里原样透传，没有翻译键名。',
        })
    return _stamp({
        'account_id': account_id,
        'api': api,
        'draft_id': _text(draft_id),
        'fields': parsed['fields'],
        'raw': proto.business(value),
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- 写


def delete_draft(account_id: str, draft_id: Any, confirm: Any = None) -> Dict[str, Any]:
    """删除一件草稿（`...pc.draft.delete`）。**会真的删掉闲鱼上的草稿，不可撤销。**"""
    problem = _confirm_gate('删除草稿', confirm)
    if problem is not None:
        return problem
    problem = _missing('草稿 ID', draft_id)
    if problem is not None:
        return problem

    api, data = proto.delete_request(draft_id, confirm=True)
    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, api, '平台没有确认删除')

    draft_id_text = _text(draft_id)
    common.log_operation(account_id, 'draft_delete', draft_id_text, True,
                         {'api': api, 'platform': True})
    common.publish('drafts.changed', {'account_id': account_id, 'deleted': [draft_id_text]})
    return _stamp({
        'account_id': account_id,
        'api': api,
        'draft_id': draft_id_text,
        'deleted': True,
        'warnings': [],
    })


def batch_delete_drafts(account_id: str, draft_ids: Any, confirm: Any = None) -> Dict[str, Any]:
    """批量删除草稿（`...idle.pc.draft.batch.delete`，`draftIds` 是逗号串）。

    **一次请求删一批**，比 N 次单删少 N-1 次平台请求（真机风控对连续请求很敏感）。

    平台只回 `sucCount` / `failCount`、**不逐条回报**，所以：
    - `failed[]` 只放我们**确实知道**的失败项（这里是空的）；
    - `fail_count > 0` 时加一条 warning 说明「平台没说哪几条失败，请刷新核对」；
    - 两个计数都没给时 `ok = None`（不是 0，也不是「全成功」）。
    """
    problem = _confirm_gate('批量删除草稿', confirm)
    if problem is not None:
        return problem
    ids = _clean_ids(draft_ids)
    if not ids:
        return common.fail('draft_ids 不能为空', 'invalid_fields')
    if len(ids) > BATCH_WRITE_LIMIT:
        return common.fail(f'一次最多删除 {BATCH_WRITE_LIMIT} 个草稿（收到 {len(ids)} 个）',
                           'invalid_fields')

    api, data = proto.batch_delete_request(ids, confirm=True)
    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, api, '平台没有确认删除')

    parsed = proto.parse_process(value)
    suc_count = parsed['suc_count']
    fail_count = parsed['fail_count']
    warnings: List[Dict[str, Any]] = []
    if fail_count:
        warnings.append({
            'code': 'no_per_item_report',
            'message': f'平台报 {fail_count} 条删除失败，但**没有说明是哪几条**；'
                       '请刷新草稿箱核对，剩下的草稿还在。',
        })
    if suc_count is None and fail_count is None:
        warnings.append({
            'code': 'no_counts',
            'message': '平台没有返回成功 / 失败条数，这次无法确认结果，请刷新草稿箱核对。',
        })
    ok: Optional[int]
    if suc_count is not None:
        ok = suc_count
    elif fail_count == 0:
        ok = len(ids)
    else:
        ok = None

    common.log_operation(account_id, 'draft_batch_delete', ','.join(ids), ok is not False,
                         {'api': api, 'platform': True, 'suc_count': suc_count,
                          'fail_count': fail_count})
    common.publish('drafts.changed', {'account_id': account_id, 'deleted': ids})
    return _stamp({
        'account_id': account_id,
        'api': api,
        'total': len(ids),
        'ok': ok,
        'suc_count': suc_count,
        'fail_count': fail_count,
        'failed': [],
        'warnings': warnings,
        'per_item_report': False,
    })
