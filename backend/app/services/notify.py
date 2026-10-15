"""通知中心服务：读（频道树 + 未读数）+ 写（标记一条通知已读）。

真机证据（2026-10-10，`logs/seller_discover/notify_probe.json` /
`notify_capture.json` / `notify_page2.txt`）：

- 页面 `/notification-center` **没有独立微应用**，是 `idle-seller-workbench` 里的
  一个菜单项；bundle 里那个 `mtop.idle.web.seller.page.my` 真机直调
  `FAIL_SYS_API_NOT_FOUNDED::请求API不存在`（死代码），不能用。
- 页面真实调用的 REST 只有两个：`...pc.notify.sessions.list`（频道树）与
  `...pc.redpoint.query`（未读总数，真机 `{"needDecryptKeys":[],"total":0}`）。
- 页面上那些通知条目（「超时未付款，系统关闭了订单」…）**不是 REST 拉来的**：
  18 秒抓包期间没有任何列表接口被调用，条目由 IM/ACCS 长连接推送。
  所以这里如实给 `items: []` + 中文 warning，绝不造条目。

**写**：标记已读 `...pc.notifys.read`（`notify.WRITE_APIS`）已实现 —— 路由是
`POST /api/notifications/{account_id}/read`，**未确认（confirm 不是字面量 true）一个平台请求都不发**。

写路径的三条硬口径（与 `services/im_actions.py` 的写族同款）：
1. 闸门在函数**第一行**，失败 400 `confirmation_required`；
2. 平台没回执（业务体里没有 `success` 字段）⇒ `saved=False` + warning，
   **不记审计、不声称已读** —— 真机抓包只落了请求没落响应，回执形状未知，
   宁可说「可能没生效」（`WARN_MARK_READ_NO_ECHO`）；
3. 平台拒绝（信封失败 / 业务 `code` 不是 success / `success=false`）⇒ 502
   `protocol_error` + 平台原话，**不改 `login_state`**（无权限 ≠ 掉线）。

真机证据：标记已读的请求原文在 `logs/seller_discover/notify_capture2.json:93-95`
（`data={"messageId":"4338118620042.PNM"}`，版本 1.0）——**一次只标记一条**；
官方页面的「全部标记已读」是逐条 fan-out，我们手里没有能列出条目的 REST 接口
（条目来自 IM 长连接），所以这里只做单条，**不编造一个「全部」接口**。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core import db
from ..xianyu import notify as notify_mod
from . import common

PAGE_SIZE_MAX = 100
DEFAULT_PAGE_SIZE = 20

WARN_NO_LIST_API = (
    '通知条目由 IM 长连接推送，平台没有提供 REST 列表接口，'
    '这里只能给出频道与未读总数'
)
WARN_CHANNELS_MISSING = '平台没有返回通知频道（响应里没有 notifyInfoList）'
WARN_UNREAD_MISSING = '平台没有返回未读数（响应里没有 total），不是 0'

#: 未确认的失败 code：与写族同一口径（`common.FAIL_STATUS` 没有这个键 ⇒ `common.respond` 兜底 400）。
CONFIRM_CODE = common.CONFIRM_CODE

#: 平台没给回执（业务体里没有 `success` 字段）时的 warning —— **绝不谎报已读**。
#: 口径同 `services/im_actions.py::WARN_RECEPTION_NO_ECHO`：没回执就不声称改动生效。
WARN_MARK_READ_NO_ECHO = {
    'code': 'notify_mark_read_no_echo',
    'message': '平台没有明确回执（业务体里没有 success 字段），所以这条通知**可能没有被标记已读**。'
               '刷新通知中心确认 —— 别把它当成已经读过了。',
}


def _text(value: Any) -> str:
    return '' if value is None else str(value)


#: 判据单点：services/common.confirm_truthy（宽松档，全仓唯一一份实现）。
_truthy = common.confirm_truthy


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰账号、不碰平台。"""
    return common.confirm_required(action, confirm)


def _number_or_none(value: Any) -> Optional[float]:
    """整数值一律还原成 int（避免 JSON 出现平台从没给过的 `1.0`）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = _text(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
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


def _client(account_id: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


def _call(client: Any, name: str, *args: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """按名字跑一个「收 client 的协议函数」，异常翻成失败信封。

    协议模块在、函数不在（半升级）时 `getattr` 拿不到就走 `CALL_MISSING` →
    503 `xianyu_not_ready`，不让 `AttributeError` 冒到 FastAPI 变 500。
    """
    func = getattr(notify_mod, name, None)
    if func is None:
        return None, common.call_failed(common.CALL_MISSING, None)
    try:
        return common.run_awaitable(func(client, *args)), None
    except Exception as exc:  # noqa: BLE001 - 协议层异常统一翻信封
        if common._is_session_expired(exc):
            common._note_session_expired(client, exc)
        failure = common.call_failed(common.CALL_ERROR, exc)
        return None, failure or common.fail(f'协议层调用失败: {exc}', 'protocol_error')


def _unwrap(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """拆平台业务信封 `{code, data, msg}`（与 shop.py 同一约定，见那里的说明）。"""
    if not isinstance(data, dict):
        return None, common.fail('平台返回的内容无法识别（不是对象）', 'protocol_error')
    code = _text(data.get('code')).strip()
    if not code:
        # 真机 `notify.sessions.list` 回 `{notifyInfoList, success}`、
        # `redpoint.query` 回 `{needDecryptKeys, total}` —— 都没有业务信封，
        # 整段 data 就是载荷。
        return data, None
    if code != 'success':
        return None, common.fail(
            f'平台返回业务错误: {_text(data.get("msg")) or code}', 'protocol_error',
        )
    inner = data.get('data')
    return (inner if isinstance(inner, dict) else {}), None


def _page(page: Any, size: Any) -> Tuple[Optional[Tuple[int, int]], Optional[Dict[str, Any]]]:
    """分页口径同 shop.py：`page`/`size` 不合法一律 400，不静默吞成 1。"""
    try:
        page_no = int(_text(page).strip() or 1)
        page_size = int(_text(size).strip() or DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        return None, common.fail('分页参数必须是整数', 'invalid_fields')
    if page_no < 1:
        return None, common.fail('页码必须是 1 及以上的整数', 'invalid_fields')
    if page_size < 1 or page_size > PAGE_SIZE_MAX:
        return None, common.fail(f'每页条数必须在 1~{PAGE_SIZE_MAX} 之间', 'invalid_fields')
    return (page_no, page_size), None


def _stamp() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def notifications(account_id: str, page: Any = '', size: Any = '') -> Dict[str, Any]:
    """通知中心：频道树 + 未读总数 + （空的）通知条目。

    `page`/`size` 目前是**空操作**：REST 侧没有可分页的通知列表，
    两个参数只做校验并原样回显，好让前端契约先固定下来。
    """
    params, failure = _page(page, size)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure

    payload, failure = _call(client, 'notify_channels')
    if failure:
        return failure
    inner, failure = _unwrap(payload)
    if failure:
        return failure
    raw_channels = inner.get('notifyInfoList')
    channels: Optional[List[Any]] = raw_channels if isinstance(raw_channels, list) else None

    unread_payload, unread_problem = _call(client, 'unread_counts')
    unread_total: Optional[float] = None
    if unread_problem:
        # 未读数是「次要数据」：一次抖动不该让整页（连频道树一起）502，
        # 与 shop 的 best-effort 口径一致 —— 拿不到就 null + warning。
        warnings_unread = f'未读数接口调用失败（{_text(unread_problem.get("message"))}），未读数为 null'
    else:
        unread_inner, unread_problem = _unwrap(unread_payload)
        if unread_problem:
            warnings_unread = (
                f'未读数接口返回业务错误（{_text(unread_problem.get("message"))}），未读数为 null'
            )
        else:
            unread_total = _number_or_none(unread_inner.get('total'))
            warnings_unread = WARN_UNREAD_MISSING if unread_total is None else ''

    warnings: List[str] = [WARN_NO_LIST_API]
    if not isinstance(raw_channels, list):
        warnings.append(WARN_CHANNELS_MISSING)
    if warnings_unread:
        warnings.append(warnings_unread)

    return {
        'account_id': account_id,
        'page': params[0],
        'page_size': params[1],
        'channels': channels,
        'unread': {'total': unread_total},
        'items': [],
        'warnings': warnings,
        'fetched_at': _stamp(),
        'source': 'platform',
    }


# --------------------------------------------------------------------------- 写


def _write_call(client: Any, api: str, data: Dict[str, Any],
                version: str) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """发一次写接口 mtop，返回 `(客户端返回值, 失败信封)`。

    与只读的 `_call` 的区别：这里要拿**整个客户端信封**（判回执要它），所以不经过
    协议层的 `_data` 抽取；异常 / 方法缺失统一走 `common.call_method` +
    `common.call_failed`（登录态过期在那里已经被记成 `need_relogin`）。
    """
    status, value = common.call_method(client, ('mtop',), api, data, v=version)
    if status == common.CALL_OK:
        return value, None
    failure = common.call_failed(status, value) or common.fail('协议层调用失败', 'protocol_error')
    failure.setdefault('platform_errors', {
        'api': api,
        'explain': str(value) or '协议层未提供该方法',
    })
    return None, failure


def _platform_failure(value: Any, api: str, fallback: str) -> Dict[str, Any]:
    """平台回了信封但这次写不算成功 ⇒ 502，把平台原文透出去。

    **不判登录态**：无权限 / 参数不对都不等于掉线，别让用户白扫一次码。
    """
    reason = notify_mod.envelope_error(value) or fallback
    return common.fail(
        f'平台接口失败: {reason}',
        'protocol_error',
        platform_errors={
            'api': api,
            'message': notify_mod.envelope_message(value),
            'explain': reason,
        },
    )


def mark_read(account_id: str, message_id: Any, confirm: Any = None) -> Dict[str, Any]:
    """把**一条**通知标记为已读（`mtop.taobao.idlemessage.pc.notifys.read`，版本 1.0）。

    顺序与 `services/im_actions.py::_write` 一致：**确认 → 字段校验 → 建客户端 → 发**。
    未确认 ⇒ 400 `confirmation_required`；`message_id` 为空 ⇒ 400 `invalid_fields` ——
    两种情况**一个平台请求都不发**。

    `saved` 只在平台**明确**回了 `success` 字段时才是 `True`；平台没这个字段 ⇒
    `saved=False` + `WARN_MARK_READ_NO_ECHO`，并且**不记审计** —— 不替平台声称已读。
    平台拒绝（信封失败 / 业务 code 不是 success / success=false）⇒ 502 + 平台原话。
    """
    problem = _confirm_gate('标记通知已读', confirm)
    if problem is not None:
        return problem
    try:
        api, data, version = notify_mod.mark_read_request(message_id, confirm=True)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    client, failure = _client(account_id)
    if failure:
        return failure
    value, failure = _write_call(client, api, data, version)
    if failure is not None:
        return failure
    if notify_mod.envelope_error(value):
        return _platform_failure(value, api, '平台没有接受这次标记已读')

    parsed = notify_mod.parse_mark_read(value)
    if not parsed['saved']:
        return _platform_failure(value, api, parsed['platform_message'] or '平台没有接受这次标记已读')

    # 「平台没给回执」= 不知道有没有生效：不记审计、saved 说 false。
    no_echo = parsed['platform_success'] is None
    if not no_echo:
        common.log_operation(account_id, 'notify_mark_read', data['messageId'], True,
                             {'api': api, 'message_id': data['messageId']})
    return {
        'account_id': account_id,
        'action': 'notify_mark_read',
        'api': api,
        'message_id': data['messageId'],
        'saved': not no_echo,
        'submitted': dict(data),
        'platform_success': parsed['platform_success'],
        'platform_message': parsed['platform_message'],
        'warnings': [dict(WARN_MARK_READ_NO_ECHO)] if no_echo else [],
        'fetched_at': _stamp(),
        'source': 'platform',
    }
