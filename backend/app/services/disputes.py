# -*- coding: utf-8 -*-
"""纠纷协商服务：纠纷会话 + 历史留言 + 未读（只读），发文字 / 发图举证 / 催办 / 标记已读（写）。

协议层见 `xianyu/dispute.py`（官方卖家工作台 bundle 静态取证，零平台请求）。这里只做三件事：
拼请求、把平台返回翻成人能看的结果、把写操作的三步顺序钉死。

**三步顺序（写路径）**：确认 → 字段校验 → 建客户端。
- 确认在最前：未确认时连字段错都不该报，否则会误导用户「填对了就能发」；
- 字段校验在建客户端之前：纯本地、免费，账号掉线时也该先说「留言内容不能为空」。

**只读路径的平台拒绝不降级**：这一族三个只读接口都可能被平台拒（真机上
`FAIL_BIZ_IDLE_USER_UNAUTHORIZED` / `NO_PERMISSION` 都见过），被拒就如实 502，由
`services/platform_errors.py` 统一补人话；把「没权限」装成「没有留言」是假数据。

**`content` 两种形态都认**：我们发出去的是 JSON 字符串，平台回来的可能是对象
（官方渲染直接读 `content.text`）—— 解析交给 `proto.normalize_message`，别在这里判。

**确认口径**：服务层用 `_truthy` 认 `True`/`'true'`/`1`（AI 与 MCP 客户端可能传字符串），
但传给协议层前**归一成 `True`** —— 协议层的 `_need_confirm` 是严格的 `is True`。

**诚实边界**：一次真机请求都没发过（第 41 批红线是零平台写请求）；五条接口的形状来自 bundle。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..xianyu import dispute as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读交易服务里的私有助手（同包内，避免两套判定漂移）
from .trade import _client

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = 'confirmation_required'

#: 写成功后的实时刷新话题：投诉页据此静默重拉（前端 `useAsync` 第 4 参订阅它）。
TOPIC = 'disputes.changed'

#: 平台没给留言列表键时的 warning（与「确实没有留言」分开）。
WARN_MESSAGES_MISSING = {
    'code': 'dispute_messages_missing',
    'message': '平台这一次没有返回留言列表字段。下面的「没有留言」不代表真的没有，'
               '别当成结论 —— 点「刷新」再看一次。',
}

#: 平台没给「下一页游标」时的 warning。
WARN_CURRENT_TIME_MISSING = {
    'code': 'dispute_current_time_missing',
    'message': '平台没有返回下一页游标（currentTime），所以「还有更多」只能按条数猜 —— '
               '如果这一页正好 20 条，可能还有更早的留言。',
}

#: 平台没给未读列表键时的 warning。
WARN_UNREAD_MISSING = {
    'code': 'dispute_unread_missing',
    'message': '平台这一次没有返回未读列表字段。下面的「没有未读」不代表真的没有。',
}

#: 平台回执里没有消息 id / sequence 时的 warning（**不是失败**，官方也只 toast 一句成功）。
WARN_NO_ECHO = {
    'code': 'dispute_no_echo',
    'message': '平台接受了这次提交，但没有回消息 id（msgId）和序号（sequence）—— '
               '所以这一条在列表里的位置只能靠刷新确认，别拿本地数字当平台序号。',
}


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload.setdefault('source', 'platform')
    payload.setdefault('fetched_at', _now())
    return payload


def _truthy(value: Any) -> bool:
    """只有字面真 `True` / `'true'` / `1` 才算确认（与 items / drafts / im_actions 同口径）。"""
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
    reason = proto.envelope_error(value) or fallback
    logger.warning(f'[disputes] 平台拒绝 {api}: {reason}')
    return common.fail(
        f'平台接口失败: {reason}',
        'protocol_error',
        platform_errors={
            'api': api,
            'message': proto.envelope_message(value),
            'explain': reason,
        },
    )


def _call(account_id: str, api: str, data: Optional[Dict[str, Any]],
          version: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """发一次 mtop 调用，返回 `(客户端返回值, 失败信封)`；成功时第二个是 `None`。

    账号/凭据检查只有一份：复用只读交易服务里的 `_client` ——
    **账号不存在 → 404 `not_found`**、**没有 Cookie 或已判定要重登 → 409 `verification_required`**，
    两种都是本地判定，**一个平台请求都不发**。
    """
    client, failure = _client(account_id)
    if client is None:
        return None, failure
    status, value = common.call_method(client, ('mtop',), api, data, v=version)
    if status != common.CALL_OK:
        return None, _call_failure(status, value, api)
    return value, None


# --------------------------------------------------------------------------- 只读


def conversation(account_id: str, order_id: Any = '', refund_id: Any = '',
                 dispute_id: Any = '') -> Dict[str, Any]:
    """取（或创建）纠纷会话（`mtop.taobao.idle.shop.dispute.message.get.conversation`，版本 1.0）。

    平台没给 `conversationId` 就如实 502：拿不到会话 id 后面每一步都做不了，
    这时候回一个空串当成功，界面只会显示一个点不动的面板。
    """
    try:
        api, data, version = proto.conversation_request(order_id, refund_id, dispute_id)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回纠纷会话')

    parsed = proto.parse_conversation(value)
    if not parsed['found']:
        return _platform_failure(value, api, '平台没有返回纠纷会话 id')

    return _stamp({
        'account_id': account_id,
        'api': api,
        'submitted': dict(data),
        'conversation_id': parsed['conversation_id'],
        'conversation_id_key': parsed['conversation_id_key'],
        'found': True,
        'body_keys': parsed['body_keys'],
        'warnings': [],
    })


def history(account_id: str, conversation_id: Any, begin_time: Any = '') -> Dict[str, Any]:
    """拉历史留言（`…query.history`，版本 1.0）。

    平台没给列表键 ⇒ `messages=None` + warning（**不是 `[]`**）：`[]` 是「真的没有留言」。
    """
    try:
        api, data, version = proto.history_request(conversation_id, begin_time)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回留言')

    parsed = proto.parse_history(value)
    warnings: List[Dict[str, Any]] = []
    messages: Optional[List[Dict[str, Any]]] = parsed['messages']
    if parsed['list_key'] is None:
        messages = None
        warnings.append(dict(WARN_MESSAGES_MISSING))
    if parsed['current_time'] is None:
        warnings.append(dict(WARN_CURRENT_TIME_MISSING))
    return _stamp({
        'account_id': account_id,
        'api': api,
        'conversation_id': data['conversationId'],
        'begin_time': data['beginTime'],
        'messages': messages,
        'count': parsed['count'],
        'has_more': parsed['has_more'] if messages is not None else None,
        'current_time': parsed['current_time'],
        'list_key': parsed['list_key'],
        'body_keys': parsed['body_keys'],
        'warnings': warnings,
    })


def unread(account_id: str, conversation_id: Any) -> Dict[str, Any]:
    """未读留言（`…query.unread.list`，版本 1.0）；平台没给列表键 ⇒ `messages=None` + warning。"""
    try:
        api, data, version = proto.unread_request(conversation_id)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回未读留言')

    parsed = proto.parse_unread(value)
    warnings: List[Dict[str, Any]] = []
    messages: Optional[List[Dict[str, Any]]] = parsed['messages']
    if parsed['list_key'] is None:
        messages = None
        warnings.append(dict(WARN_UNREAD_MISSING))
    return _stamp({
        'account_id': account_id,
        'api': api,
        'conversation_id': data['conversationId'],
        'messages': messages,
        'count': parsed['count'],
        'list_key': parsed['list_key'],
        'body_keys': parsed['body_keys'],
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- 写


def _write(action: str, label: str, account_id: str, audit: str,
           confirm: Any, **kwargs: Any) -> Dict[str, Any]:
    """四条写接口共用的一条路：确认 → 字段校验 → 建客户端 → 发 → 判回执 → 记日志 + 播事件。

    平台接受了但没回 `msgId`/`sequence` 时给 informational warning（**不是失败**）：
    官方那边也只 toast 一句成功，所以不能因为「没回执」就报错，也不能拿本地数字冒充平台序号。
    """
    problem = _confirm_gate(label, confirm)
    if problem is not None:
        return problem
    try:
        # 归一成 True：服务层已经判过确认，协议层那道 `is True` 只防直接误用
        api, data, version = proto.request_of(action, confirm=True, **kwargs)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, f'平台没有接受这次{label}')

    parsed = proto.parse_send(value)
    if not parsed['saved']:
        # 业务层拒绝（信封 success=True 但 success=false / code 不是 success）时，
        # 把**平台那句原话**透出去：`_platform_failure` 只在客户端信封失败时才有原文。
        return _platform_failure(value, api, parsed['platform_message'] or f'平台没有接受这次{label}')

    conversation_id = data['conversationId']
    detail = {key: item for key, item in data.items() if key != 'conversationId'}
    detail['conversation_id'] = conversation_id
    common.log_operation(account_id, audit, conversation_id, True, detail)
    common.publish(TOPIC, {
        'account_id': account_id,
        'conversation_id': conversation_id,
        'action': action,
    })

    warnings: List[Dict[str, Any]] = []
    if parsed['msg_id'] is None and parsed['sequence'] is None:
        warnings.append(dict(WARN_NO_ECHO))
    payload: Dict[str, Any] = {
        'account_id': account_id,
        'action': action,
        'api': api,
        'conversation_id': conversation_id,
        'saved': True,
        'msg_id': parsed['msg_id'],
        'sequence': parsed['sequence'],
        'tips': parsed['tips'],
        'submitted': dict(data),
        'platform_message': parsed['platform_message'],
        'warnings': warnings,
    }
    return _stamp(payload)


def send_text(account_id: str, conversation_id: Any, text: Any,
              confirm: Any = None) -> Dict[str, Any]:
    """给纠纷会话发一条文字留言（`…dispute.message.send`，版本 1.0）。"""
    return _write('send_text', '发送留言', account_id, 'dispute_send_text',
                  confirm, conversation_id=conversation_id, text=text)


def send_image(account_id: str, conversation_id: Any, url: Any, *,
               ext: Any = proto.IMAGE_EXT_DEFAULT, width: Any = 0, height: Any = 0,
               alt_text: Any = '', thumb_url: Any = '', send_key: Any = '',
               confirm: Any = None) -> Dict[str, Any]:
    """发一张图举证（同一条 send，`contentType:'IMAGE'`）。

    图片地址得是**平台能取到的 URL**（本机上传接口 `POST /api/uploads/images` 给的就是）；
    官方在前端还卡了类型与 3 MiB 大小，我们这一层只保证形状。
    """
    return _write('send_image', '发送图片', account_id, 'dispute_send_image', confirm,
                  conversation_id=conversation_id, url=url, ext=ext, width=width,
                  height=height, alt_text=alt_text, thumb_url=thumb_url, send_key=send_key)


def urge(account_id: str, conversation_id: Any, confirm: Any = None) -> Dict[str, Any]:
    """催办：就是发一句固定文案（`催促处理维权`）的文字留言。"""
    return _write('urge', '催办', account_id, 'dispute_urge', confirm,
                  conversation_id=conversation_id)


def mark_read(account_id: str, conversation_id: Any, msg_id: Any, sequence: Any,
              confirm: Any = None) -> Dict[str, Any]:
    """把某条留言标记已读（`…dispute.message.mark.read`，版本 1.0）。

    官方是拿 `header.sequence` 最大的那条去标记；`sequence` 必须是整数（协议层会校验）。
    """
    return _write('mark_read', '标记已读', account_id, 'dispute_mark_read', confirm,
                  conversation_id=conversation_id, msg_id=msg_id, sequence=sequence)
