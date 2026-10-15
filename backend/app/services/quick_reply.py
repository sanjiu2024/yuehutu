"""话术（官方 IM 工作台的「快捷回复」）服务层：列表读取 + 分组/短语的增删改。

协议层见 `xianyu/quick_reply.py`（官方 bundle 静态取证，零平台请求）。这里只做三件事：
拼请求、把平台返回翻成人能看的结果、把写操作的三步顺序钉死。

**三步顺序（写路径）**：确认 → 字段校验 → 建客户端。
- 确认在最前：未确认时连字段错都不该报，否则会误导用户「填对了就能发」；
- 字段校验在建客户端之前：纯本地、免费，账号掉线时也该先说「分组名称不能为空」
  而不是「去重新登录」。
这套口径与 `services/drafts.py`、`services/shop_profile.py`、`services/items.py` 完全一致。

**诚实边界**：
- 平台「没回显 id」不等于失败，也不等于成功：官方前端靠 `groupInfo.groupId` /
  `contentInfo.contentId` 判成功，所以这里返回 `saved=False` + 一条中文 warning
  （提交**已经发出**，请刷新看平台当前值），**不谎报成功、也不谎报失败**。
- 一次真机请求都没发过（本机账号只有 `2217970177348`，且它缺部分平台业务权限）。
  字段形状与官方页面逐字一致，但「平台是否接受」仍属未验证。
- 只读路径的平台拒绝（无权限）**不做容错降级**：这一族只有一个列表接口，
  平台拒绝时如实 502，并由 `services/platform_errors.py` 统一补人话
  （「不是登录掉线」），不会让用户白扫一次码。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..xianyu import quick_reply as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读服务里的私有助手（同包内，避免两套判定漂移）；
# 分页助手同理（`_paging` 校验 + `paginate` 内存切页，口径与 /api/trade 五个列表端点完全一致）。
from .trade import _client, _paging, paginate

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = common.CONFIRM_CODE

#: 写成功后广播的 topic（前端 `useAsync` 订阅它做静默重拉）。
TOPIC = 'quick_reply.changed'

WARN_LIST_KEY_MISSING = ('平台这一次没有返回话术列表字段（groups），下面的空列表不等于'
                         '「一条话术都没有」，别当成 0 条。')
WARN_NO_GROUP_ID = ('平台没有回显这次操作的分组（groupInfo.groupId），提交**已经发出**但平台没确认；'
                    '请点「刷新」看平台当前值，再决定要不要重试。')
WARN_NO_CONTENT_ID = ('平台没有回显这条短语（contentInfo.contentId），提交**已经发出**但平台没确认；'
                      '请点「刷新」看平台当前值，再决定要不要重试。')

#: 写动作 → (审计名, 接口, 成功回执解析器)
_WRITES: Dict[str, Tuple[str, str, str]] = {
    'group_create': ('quick_reply_group_create', proto.GROUP_API, 'group'),
    'group_update': ('quick_reply_group_update', proto.GROUP_API, 'group'),
    'group_delete': ('quick_reply_group_delete', proto.GROUP_API, 'group'),
    'phrase_create': ('quick_reply_phrase_create', proto.CONTENT_API, 'content'),
    'phrase_update': ('quick_reply_phrase_update', proto.CONTENT_API, 'content'),
    'phrase_delete': ('quick_reply_phrase_delete', proto.CONTENT_API, 'content'),
}


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload.setdefault('source', 'platform')
    payload.setdefault('fetched_at', _now())
    return payload


#: 判据单点：services/common.confirm_truthy（宽松档，全仓唯一一份实现）。
_truthy = common.confirm_truthy


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰库、不碰账号、不碰平台。"""
    return common.confirm_required(action, confirm, message=f'{action} 是写操作，会真的改动闲鱼上的快捷回复（不是只改本地），需要显式确认（confirm=true）')


def _value_message(value: Any) -> str:
    """客户端层失败时的文案（平台信封为空时只剩它）。"""
    if isinstance(value, dict):
        message = _text(value.get('message'))
        if message:
            return message
        ret = value.get('ret')
        if isinstance(ret, list) and ret:
            return _text(ret[0])
    return _text(value)


def _platform_failure(value: Any, api: str) -> Dict[str, Any]:
    """平台回了信封但业务体不算成功 ⇒ 502，把平台原文透出去。

    **不判登录态**：无权限 / 参数不对都不等于掉线，别让用户白扫一次码。
    """
    reason = proto.failure_reason(value) or '平台没有确认这次操作'
    logger.warning(f'[quick_reply] 平台拒绝 {api}: {reason}')
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
    client, failure = _client(account_id)
    if client is None:
        return None, failure
    status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if status != common.CALL_OK:
        failure = common.call_failed(status, value) or common.fail('协议层调用失败', 'protocol_error')
        failure.setdefault('platform_errors', {
            'api': api,
            'explain': _value_message(value) or '协议层未提供该方法',
        })
        return None, failure
    return value, None


# --------------------------------------------------------------------------- 只读


def list_groups(account_id: str, page: Any = '', size: Any = '') -> Dict[str, Any]:
    """话术列表（`mtop.taobao.idlemessage.quickreply.list.get.v1`）。

    ``groups`` 认不出列表键时给 ``None``（不是 ``[]``）并加一条中文 warning ——
    「平台没给」与「真的没有话术」是两件事。

    平台**一次性给全部分组**（官方调用点 ``b$({})`` 不带参数）⇒ 服务端**内存分页**：
    `page`/`size` 都不给时保持向后兼容，一次返回全量。`count` 是**本页**条数，
    `total` 仍是「平台给了就用平台的、否则数出来的条数」（`total_from_platform` 说明是谁给的）。
    """
    page_no, page_size, failure = _paging(page, size)
    if failure is not None:
        return failure
    api, data = proto.list_request()
    value, failure = _call(account_id, api, data)
    if failure is not None:
        return failure
    if not proto.is_success(value):
        return _platform_failure(value, api)

    parsed = proto.parse_groups(value)
    paged = paginate(parsed['groups'], page_no, page_size,
                     total=parsed['total'] if parsed['total_from_platform'] else None)
    warnings: List[Dict[str, Any]] = []
    if not parsed['found_key']:
        warnings.append({'code': 'list_key_missing', 'message': WARN_LIST_KEY_MISSING})
    return _stamp({
        'account_id': account_id,
        'api': api,
        'groups': paged['items'],
        # `items` 是分页列表的标准键；`groups` 是历史键（前端话术页用它），同一份数据。
        'items': paged['items'],
        'count': len(paged['items']) if isinstance(paged['items'], list) else None,
        'total': paged['total'],
        'page': paged['page'],
        'size': paged['size'],
        'pages': paged['pages'],
        'total_from_platform': parsed['total_from_platform'],
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- 写


def _write(account_id: str, action: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """写路径总入口：确认闸门由调用方先过，这里负责字段校验 → 建客户端 → 提交 → 解析。"""
    audit, api, kind = _WRITES[action]
    try:
        api, data = proto.request_of(action, confirm=True, **fields)
    except proto.UnknownApi as exc:
        return common.fail(str(exc), 'invalid_fields')
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data)
    if failure is not None:
        common.log_operation(account_id, audit, None, False, _value_message(failure.get('message')))
        return failure
    if not proto.is_success(value):
        common.log_operation(account_id, audit, None, False, proto.failure_reason(value))
        return _platform_failure(value, api)

    warnings: List[Dict[str, Any]] = []
    payload: Dict[str, Any] = {'account_id': account_id, 'action': action.split('_')[-1], 'api': api}
    if kind == 'group':
        parsed = proto.parse_group_write(value)
        payload['group_id'] = parsed['group_id']
        payload['group_name'] = parsed['group_name'] or fields.get('group_name')
        payload['saved'] = parsed['saved']
        if not parsed['saved']:
            warnings.append({'code': 'no_group_id', 'message': WARN_NO_GROUP_ID})
    else:
        parsed = proto.parse_content_write(value)
        payload['content_id'] = parsed['content_id']
        payload['group_id'] = parsed['group_id'] or fields.get('group_id')
        payload['content'] = parsed['content'] or fields.get('content')
        payload['saved'] = parsed['saved']
        if not parsed['saved']:
            warnings.append({'code': 'no_content_id', 'message': WARN_NO_CONTENT_ID})

    if payload['action'] == 'delete':
        payload['deleted'] = parsed['saved']
    payload['platform_message'] = parsed['platform_message']
    payload['submitted'] = data
    payload['warnings'] = warnings
    ok = bool(parsed['saved'])
    common.log_operation(account_id, audit, None, ok,
                         payload.get('platform_message') or payload['action'])
    if ok:
        common.publish(TOPIC, {'account_id': account_id, 'action': action})
    return _stamp(payload)


def create_group(account_id: str, group_name: Any, confirm: Any = None) -> Dict[str, Any]:
    failure = _confirm_gate('新建分组', confirm)
    if failure:
        return failure
    return _write(account_id, 'group_create', {'group_name': group_name})


def update_group(account_id: str, group_id: Any, group_name: Any, confirm: Any = None) -> Dict[str, Any]:
    failure = _confirm_gate('修改分组', confirm)
    if failure:
        return failure
    return _write(account_id, 'group_update', {'group_id': group_id, 'group_name': group_name})


def delete_group(account_id: str, group_id: Any, confirm: Any = None) -> Dict[str, Any]:
    failure = _confirm_gate('删除分组', confirm)
    if failure:
        return failure
    return _write(account_id, 'group_delete', {'group_id': group_id})


def create_phrase(account_id: str, group_id: Any, content: Any, confirm: Any = None) -> Dict[str, Any]:
    failure = _confirm_gate('新建短语', confirm)
    if failure:
        return failure
    return _write(account_id, 'phrase_create', {'group_id': group_id, 'content': content})


def update_phrase(account_id: str, group_id: Any, content_id: Any, content: Any,
                  confirm: Any = None) -> Dict[str, Any]:
    failure = _confirm_gate('修改短语', confirm)
    if failure:
        return failure
    return _write(account_id, 'phrase_update',
                  {'group_id': group_id, 'content_id': content_id, 'content': content})


def delete_phrase(account_id: str, group_id: Any, content_id: Any, confirm: Any = None) -> Dict[str, Any]:
    failure = _confirm_gate('删除短语', confirm)
    if failure:
        return failure
    return _write(account_id, 'phrase_delete', {'group_id': group_id, 'content_id': content_id})
