"""通知中心只读服务（频道树 + 未读数）。

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

只读：`notify.WRITE_APIS`（`...pc.notifys.read` 标记已读）故意不实现。
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


def _text(value: Any) -> str:
    return '' if value is None else str(value)


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
