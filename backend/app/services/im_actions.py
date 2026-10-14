# -*- coding: utf-8 -*-
"""IM 会话操作服务：拉黑状态（只读）+ 拉黑买家 / 解除拉黑 / 发商品卡片 / 转交会话 /
结束会话 / 重新发起聊天（写）+ 接待状态（读 / 写）。

协议层见 `xianyu/im_actions.py`（官方 IM bundle 静态取证，零平台请求）。这里只做三件事：
拼请求、把平台返回翻成人能看的结果、把写操作的三步顺序钉死。

**三步顺序（写路径）**：确认 → 字段校验 → 建客户端。
- 确认在最前：未确认时连字段错都不该报，否则会误导用户「填对了就能发」；
- 字段校验在建客户端之前：纯本地、免费，账号掉线时也该先说「会话 id 不能为空」。

**只读路径的平台拒绝不降级**：这一族只有一个只读接口，被拒就如实 502，
由 `services/platform_errors.py` 统一补人话；把「没权限」装成「没拉黑」是假数据。

**确认口径**：服务层用 `_truthy` 认 `True`/`'true'`/`1`（AI 与 MCP 客户端可能传字符串），
但传给协议层前**归一成 `True`** —— 协议层的 `_need_confirm` 是严格的 `is True`，
直接把 `'true'` 透下去会出现「服务层说确认过了、协议层说没确认」的自相矛盾。

**诚实边界**：一次真机请求都没发过（第 40 批红线是零平台写请求）；四条接口的形状来自 bundle。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..xianyu import im_actions as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读服务里的私有助手（同包内，避免两套判定漂移）
from .trade import _client
# 平台 cid 的解析也只有一份（`conversations` 表没有 cid 列，cid 只跟着 IM 帧落进 messages.meta）
from . import chats

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = 'confirmation_required'

#: 写成功后的实时刷新话题：会话页据此静默重拉（前端 `useAsync` 第 4 参订阅它）。
TOPIC = 'chats.changed'

#: 平台没给拉黑状态字段时的 warning（与「确实没拉黑」分开）。
WARN_STATUS_MISSING = {
    'code': 'blacklist_status_missing',
    'message': '平台这一次没有返回拉黑状态字段。下面显示的「未拉黑」不代表这个买家没被拉黑，'
               '别当成结论 —— 点「刷新」再看一次。',
}

#: 平台没给 `isInBlack` 时的 warning。
WARN_IS_IN_BLACK_MISSING = {
    'code': 'is_in_black_missing',
    'message': '平台没有返回「是否已拉黑」这个字段（显示为「-」）。这不是「未拉黑」，'
               '两种状态在界面上必须分开。',
}

#: 平台没给 `showBlackList` 时的 warning。
WARN_SHOW_BLACK_LIST_MISSING = {
    'code': 'show_black_list_missing',
    'message': '平台没有返回「是否展示拉黑入口」这个字段（显示为「-」）。',
}

#: 会话还没有平台 cid 时的结构化 code（与 `services/chats.py::MISSING_CID_CODE` 同一口径）。
MISSING_CID_CODE = 'missing_cid'

#: 会话还没有平台 cid 时的原因文本：说清「为什么」和「怎么办」，不糊一句「参数错误」。
MISSING_CID_MESSAGE = ('这个会话还没有平台会话 id（cid）——先在会话页点一次「同步历史会话」，'
                       '拿到 cid 之后才能拉黑 / 发商品卡片。')

#: 平台没给（或给了认不出的）接待状态时的 warning —— **绝不当成「离线」**。
WARN_RECEPTION_MISSING = {
    'code': 'reception_status_missing',
    'message': '平台这一次没有返回当前接待状态（或返回了认不出的值），所以这里显示「状态没查到」。'
               '这不代表你离线了 —— 点「刷新」再看一次。',
}

#: 切换接待状态时平台没回 `success` 字段 ⇒ 不谎报成功（官方的 UI 也把「没有 success」当失败）。
WARN_RECEPTION_NO_ECHO = {
    'code': 'reception_no_echo',
    'message': '平台没有明确回执（没返回 success 字段），所以这次切换**可能没有生效**。'
               '官方界面在同样的情况下也显示「状态切换失败」——请刷新确认。',
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
    """只有字面真 `True` / `'true'` / `1` 才算确认（与 items / drafts / quick_reply 同口径）。"""
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
    logger.warning(f'[im_actions] 平台拒绝 {api}: {reason}')
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


def _resolve_session(account_id: str, session_id: Any,
                     conversation_id: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """把「会话」归一成平台 cid：优先直接用 `session_id`，否则用本地会话行 id 解析。

    为什么需要这一步：`conversations` 表**没有 cid 列**，cid 只跟着 IM 帧落进
    `messages.meta`，所以会话页手里只有本地会话行 id，拿不到 cid。让调用方自己猜一个
    cid 是错的（`peer_user_id` 是买家 user id，不是会话 id），因此这里复用既有的
    `services/chats.py::_platform_cid` 解析；解析不出来就如实报 `missing_cid`，
    **绝不拿别的 id 冒充**。

    返回 `(cid, None)` 或 `('', 失败信封)`。
    """
    text = _text(session_id)
    if text:
        return text, None

    row_id = _text(conversation_id)
    if not row_id:
        return '', _missing('会话 id', '')

    conversation = chats.get_conversation(row_id)
    if common.is_failure(conversation):
        return '', conversation
    if not isinstance(conversation, dict):
        return '', common.fail(f'会话不存在: {row_id}', 'not_found')
    owner = _text(conversation.get('account_id'))
    if owner and owner != _text(account_id):
        # 跨账号的会话 id 一律当成不存在：不让 A 账号的操作落到 B 账号的会话上
        return '', common.fail(f'会话不存在: {row_id}', 'not_found')

    cid = _text(chats._platform_cid(conversation))
    if not cid:
        return '', common.fail(MISSING_CID_MESSAGE, MISSING_CID_CODE)
    return cid, None


def blacklist_status(account_id: str, session_id: Any,
                     conversation_id: Any = None) -> Dict[str, Any]:
    """拉黑状态（`mtop.taobao.idlemessage.pc.blacklist.query`，版本 1.0）。

    平台没给 `isInBlack` 时给 `is_in_black=None` **+ warning** —— 不是 `False`。
    """
    resolved, problem = _resolve_session(account_id, session_id, conversation_id)
    if problem is not None:
        return problem
    try:
        api, data, version = proto.blacklist_query_request(resolved)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回拉黑状态')

    parsed = proto.parse_blacklist(value)
    warnings: List[Dict[str, Any]] = []
    if not parsed['found']:
        warnings.append(dict(WARN_STATUS_MISSING))
    else:
        if parsed['is_in_black'] is None:
            warnings.append(dict(WARN_IS_IN_BLACK_MISSING))
        if parsed['show_black_list'] is None:
            warnings.append(dict(WARN_SHOW_BLACK_LIST_MISSING))
    return _stamp({
        'account_id': account_id,
        'session_id': data['sessionId'],
        'api': api,
        'is_in_black': parsed['is_in_black'],
        'show_black_list': parsed['show_black_list'],
        'warnings': warnings,
    })


def reception_status(account_id: str) -> Dict[str, Any]:
    """当前接待状态（`mtop.taobao.idlemessage.pc.login.query`，版本 1.0）。

    平台没给（或给了认不出的）状态时 `status=None` + warning —— **不许替平台说「离线」**。
    `options` 是官方那三个选项（我们自己常量，不依赖平台返回），界面直接拿它渲染。
    """
    try:
        api, data, version = proto.reception_query_request()
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回接待状态')

    parsed = proto.parse_reception(value)
    warnings: List[Dict[str, Any]] = []
    if not parsed['found'] or parsed['status'] is None:
        warnings.append(dict(WARN_RECEPTION_MISSING))
    return _stamp({
        'account_id': account_id,
        'status': parsed['status'],
        'raw_status': parsed['raw_status'],
        'options': [dict(option) for option in proto.RECEPTION_OPTIONS],
        'api': api,
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- 写


def _write(action: str, label: str, account_id: str, audit: str,
           confirm: Any, parser: Any = None, require_session: bool = True,
           no_echo_warning: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """七条写接口共用的一条路：确认 → 字段校验 → 建客户端 → 发 → 判回执 → 记日志 + 播事件。

    `session_id` / `conversation_id` 在**字段校验**那一步归一成平台 cid（见 `_resolve_session`），
    归一失败就当作字段问题直接返回，不会走到建客户端。
    `require_session=False` 给**账号级**动作用（接待状态不属于任何会话，没有 sessionId 可解析）。

    `parser` 默认用 `proto.parse_action`（只判信封）；会话接管族与接待状态传
    `proto.parse_session_action`（多认一层「平台自己说 success=false」）。

    `no_echo_warning` 给「平台必须给回执才算成功」的动作（接待状态）：解析器说
    `platform_success is None`（平台没这个字段）时，返回 `saved=False` + 这条 warning，
    并且**不记审计、不发事件** —— 与 `fans_price` 那一族同口径：没回执就不声称改动生效了。
    """
    problem = _confirm_gate(label, confirm)
    if problem is not None:
        return problem
    call_kwargs = dict(kwargs)
    if require_session:
        resolved, problem = _resolve_session(account_id, call_kwargs.pop('session_id', None),
                                             call_kwargs.pop('conversation_id', None))
        if problem is not None:
            return problem
        call_kwargs['session_id'] = resolved
    else:
        call_kwargs.pop('session_id', None)
        call_kwargs.pop('conversation_id', None)
    try:
        # 归一成 True：服务层已经判过确认，协议层那道 `is True` 只防直接误用
        api, data, version = proto.request_of(action, confirm=True, **call_kwargs)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, f'平台没有接受这次{label}')

    parsed = (parser or proto.parse_action)(value)
    if not parsed['saved']:
        # 业务层拒绝（信封 success=True 但 code 不是 success）时，把**平台那句原话**透出去：
        # `_platform_failure` 只在客户端信封失败时才有原文，这里把业务原文当兜底文案传进去。
        return _platform_failure(value, api, parsed['platform_message'] or f'平台没有接受这次{label}')

    detail = {key: item for key, item in data.items() if key != 'sessionId'}
    session_id: Any = data.get('sessionId')
    if session_id is not None:
        detail['session_id'] = session_id
    # 「平台没给回执」= 不知道有没有生效：不记审计、不发事件、saved 说 false
    no_echo = no_echo_warning is not None and parsed.get('platform_success') is None
    if not no_echo:
        common.log_operation(account_id, audit, session_id, True, detail)
        common.publish(TOPIC, {
            'account_id': account_id,
            'session_id': session_id,
            'action': action,
        })
    payload: Dict[str, Any] = {
        'account_id': account_id,
        'action': action,
        'api': api,
        'session_id': session_id,
        'saved': not no_echo,
        'submitted': dict(data),
        'platform_message': parsed['platform_message'],
        'warnings': [dict(no_echo_warning)] if no_echo else [],
    }
    if 'itemId' in data:
        payload['item_id'] = data['itemId']
    if 'targetUid' in data:
        payload['target_uid'] = data['targetUid']
    # 会话接管族多一层平台回执：`data.success`（三态）与平台回显的 sessionId。
    # 只在该族出现（`parse_action` 不产这两个键），所以用存在性判断而不是动作白名单。
    if 'platform_success' in parsed:
        payload['platform_success'] = parsed['platform_success']
    if parsed.get('returned_session_id'):
        payload['returned_session_id'] = parsed['returned_session_id']
    return _stamp(payload)


def blacklist_add(account_id: str, session_id: Any, confirm: Any = None,
                  conversation_id: Any = None) -> Dict[str, Any]:
    """拉黑买家（`mtop.taobao.idlemessage.pc.blacklist.add`，**版本 2.0**）。"""
    return _write('blacklist_add', '拉黑买家', account_id, 'im_blacklist_add',
                  confirm, session_id=session_id, conversation_id=conversation_id)


def blacklist_remove(account_id: str, session_id: Any, confirm: Any = None,
                     conversation_id: Any = None) -> Dict[str, Any]:
    """解除拉黑（`mtop.taobao.idlemessage.pc.blacklist.remove`，版本 1.0）。

    返回里多一个 `deleted` —— 与「拉黑」区分开，前端据此说「已解除」而不是「已保存」。
    """
    result = _write('blacklist_remove', '解除拉黑', account_id, 'im_blacklist_remove',
                    confirm, session_id=session_id, conversation_id=conversation_id)
    # 成功载荷是**裸字典**（没有 `success` 键，`common.respond` 才包信封），
    # 失败信封才有 `success`/`code` —— 所以用动作名判断成功，别用 `result.get('success')`。
    if result.get('action') == 'blacklist_remove':
        result['deleted'] = True
    return result


def send_item_card(account_id: str, session_id: Any, item_id: Any,
                   confirm: Any = None, conversation_id: Any = None) -> Dict[str, Any]:
    """给买家发商品卡片（`mtop.taobao.idlemessage.message.card.send`，版本 1.0）。"""
    return _write('card_send', '发送商品卡片', account_id, 'im_card_send',
                  confirm, session_id=session_id, item_id=item_id,
                  conversation_id=conversation_id)


def session_deliver(account_id: str, target_uid: Any, session_id: Any = None,
                    confirm: Any = None, conversation_id: Any = None) -> Dict[str, Any]:
    """把会话**转交**给指定客服（`mtop.taobao.idlemessage.customer.deliver.session`，版本 1.0）。

    `target_uid` 取自客服列表（`GET /api/shop/{account_id}/cs-routing` 的 `agents[]`），
    服务层**不做格式猜测**，只要求非空（见协议层 `_target_uid`）。
    """
    return _write('session_deliver', '转交会话', account_id, 'im_session_deliver',
                  confirm, parser=proto.parse_session_action,
                  session_id=session_id, conversation_id=conversation_id,
                  target_uid=target_uid)


def session_leave(account_id: str, session_id: Any = None, confirm: Any = None,
                  conversation_id: Any = None) -> Dict[str, Any]:
    """**结束**当前会话（`mtop.taobao.idlemessage.customer.leave.session`，版本 1.0）。"""
    return _write('session_leave', '结束会话', account_id, 'im_session_leave',
                  confirm, parser=proto.parse_session_action,
                  session_id=session_id, conversation_id=conversation_id)


def session_rejoin(account_id: str, session_id: Any = None, confirm: Any = None,
                   conversation_id: Any = None) -> Dict[str, Any]:
    """重新发起聊天（`mtop.taobao.idlemessage.customer.rejoin.session`，版本 1.0）。

    这一条**有真回执**：平台回 `data.success` 布尔，所以把它透成
    `platform_success`（三态），`success=false` 时 `saved` 也会是 `False`。
    """
    result = _write('session_rejoin', '重新发起聊天', account_id, 'im_session_rejoin',
                    confirm, parser=proto.parse_session_action,
                    session_id=session_id, conversation_id=conversation_id)
    if result.get('action') == 'session_rejoin':
        # 与「拉黑 / 解除」同一条纪律：动作成功才补这个便于前端说话的名
        result['rejoined'] = True
    return result


def set_reception(account_id: str, status: Any, confirm: Any = None) -> Dict[str, Any]:
    """切换接待状态（`mtop.taobao.idlemessage.pc.repect.status.edit`，版本 1.0）。

    **平台没回 `success` 字段时判成「可能没生效」**（`saved=False` + warning，不记审计不发事件）：
    官方界面在同样的情况下也弹「状态切换失败」。写操作宁可说「可能没生效」，
    也不能替平台宣称成功。平台明确回 `success=false` 时由 `_write` 统一转成 502 并透出平台原话。
    """
    result = _write('reception_set', '切换接待状态', account_id, 'im_reception_set',
                    confirm, parser=proto.parse_session_action, require_session=False,
                    no_echo_warning=WARN_RECEPTION_NO_ECHO, status=status)
    if result.get('action') == 'reception_set':
        result['status'] = result['submitted'].get('status')
    return result
