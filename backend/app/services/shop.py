"""小铺（子账号管理 / 客服分流）只读服务。

真机证据（2026-10-10，`logs/seller_discover/shop.json` / `shop_notify_probe.json`）：

- 平台是**双层信封**：外层 `{success, ret, message, data}`，业务层在 `data` 里再包
  `{code:"success", data, msg}`。前端判定用 `data.code`，失败弹 `data.msg`
  —— 所以这里必须拆内层，不能只看外层 `success`。
- 子账号列表 `sub.account.info.list` 的 `pageNo` **必传**（不带就
  `FAIL_SYS_BIZPARAM_MISSED::缺少业务参数pageNo`），返回
  `{hasNextPage, list, pageNo, pageSize, rowLimit, rowOffset, total, totalPage}`。
- 客服分流真机：`routingRules` 是**对象**（只有 `serviceTime`），不是数组；
  `customers.info.get` 真机回 `data: {}` —— **连 `customers` 键都没有**。
  「键不存在」和「平台明确给了空数组」是两回事，所以 `agents` 给 `None` 而不是 `[]`：
  拿不到就如实说拿不到，不能用空数组冒充「没有客服」。
- 同理，`list` / `csGroupInfos` / `notifyInfoList` 这些列表字段**缺键**时给 `None`，
  只有平台真的回了空数组才给 `[]`（真机 `csGroupInfos: []`、`list: []` 就是真值）。

只读：本模块只调 `shop.READ_APIS` 里的接口，一个写接口都不碰
（写接口清单见 `backend/app/xianyu/shop.py` 的 `WRITE_APIS`）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core import db
from ..xianyu import shop as shop_mod
from . import common

PAGE_SIZE_MAX = 100
DEFAULT_PAGE_SIZE = 20

WARN_ROUTING_PARTIAL = '平台只返回了部分分流配置（缺离线消息策略/服务范围），缺的字段一律 null'
WARN_ROUTING_MISSING = '平台没有返回分流规则（响应里没有 routingRules 键），缺的字段一律 null'
WARN_AGENTS_MISSING = '平台没有返回客服列表（响应里没有 customers 键），这不等于「没有客服」'
WARN_QUOTA_UNAVAILABLE = '平台未返回子账号名额（接口未授权或未开放），不是 0'
WARN_ITEMS_MISSING = '平台没有返回子账号列表（响应里没有 list 键），这不等于「没有子账号」'
WARN_GROUPS_MISSING = '平台没有返回客服分组（响应里没有 csGroupInfos 键），这不等于「没有分组」'


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _number_or_none(value: Any) -> Optional[float]:
    """能确定是数字才给数字，否则 None —— 绝不把「拿不到」写成 0。

    整数值（含平台回的 int、字符串数字 `"3"`）一律还原成 int，避免 JSON 里
    出现 `1.0` 这种平台从没给过的形状。
    """
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
    """按名字跑一个「收 client 的协议函数」，把异常翻成失败信封。

    协议函数是 `async def`（内部走 `client.mtop`），所以必须交给
    `common.run_awaitable` 跑共享事件循环 —— 直接调只会拿到一个没执行的协程。
    登录态过期额外回写账号状态（与 `common.call_method` 的处理一致）。

    函数查找**放在 try 之外但先判空**：协议模块在、函数不在（半升级）时，
    `getattr` 拿不到就走 `CALL_MISSING` → 503 `xianyu_not_ready`，
    而不是让 `AttributeError` 冒到 FastAPI 变成 500。
    """
    func = getattr(shop_mod, name, None)
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
    """拆平台业务信封 `{code, data, msg}`，返回 (内层 dict, 失败信封)。"""
    if not isinstance(data, dict):
        return None, common.fail('平台返回的内容无法识别（不是对象）', 'protocol_error')
    code = _text(data.get('code')).strip()
    if not code:
        # 有些接口**没有**业务信封：真机 `customer.group.get` 直接回
        # `{csGroupInfos, hasMore, page, pageSize, total}`、`routing.rule.info.get`
        # 直接回 `{routingRules}`。这时整段 data 就是载荷，不能再往里挖一层。
        return data, None
    if code != 'success':
        return None, common.fail(
            f'平台返回业务错误: {_text(data.get("msg")) or code}', 'protocol_error',
        )
    inner = data.get('data')
    return (inner if isinstance(inner, dict) else {}), None


def _page(page: Any, size: Any, default_size: int = DEFAULT_PAGE_SIZE) -> Tuple[Optional[Tuple[int, int]], Optional[Dict[str, Any]]]:
    """校验分页参数。

    口径与 finance/trade/promotion 一致：`page`/`size` 给了但不合法（< 1、非整数）
    一律 400 `invalid_fields`，**不做 `max(1, ...)` 静默吞掉** —— 早先 `page=0`
    被吞成 1，而 `size=0` 报 400，同一类错误两种结果。
    """
    try:
        page_no = int(_text(page).strip() or 1)
        page_size = int(_text(size).strip() or default_size)
    except (TypeError, ValueError):
        return None, common.fail('分页参数必须是整数', 'invalid_fields')
    if page_no < 1:
        return None, common.fail('页码必须是 1 及以上的整数', 'invalid_fields')
    if page_size < 1 or page_size > PAGE_SIZE_MAX:
        return None, common.fail(f'每页条数必须在 1~{PAGE_SIZE_MAX} 之间', 'invalid_fields')
    return (page_no, page_size), None


def _stamp() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _best_effort(client: Any, name: str, *args: Any) -> Tuple[Any, str]:
    """可选接口：失败不影响主结果，返回 (值或 None, 警告文案)。

    **这里不用 `_unwrap`**：有些接口的载荷本身就是数组 —— bundle 里
    `platform.user.role.list` 的 `data` 就是 `[{roleId, roleName, selected}]`
    （`logs/seller_discover/shop_discover.md`），而 `_unwrap` 只认 dict，
    会把整个列表丢掉（`roles` 变成 null）。所以这里只拆「有 `code` 的内层信封」，
    其余（dict 或 list）原样返回。
    """
    value, failure = _call(client, name, *args)
    if failure:
        return None, _text(failure.get('message')) or '平台接口调用失败'
    if isinstance(value, dict):
        code = _text(value.get('code')).strip()
        if code:
            if code != 'success':
                return None, f'平台返回业务错误: {_text(value.get("msg")) or code}'
            value = value.get('data')
    return value, ''


def sub_accounts(account_id: str, page: Any = '', size: Any = '') -> Dict[str, Any]:
    """子账号列表（只读）。行数据**原样透传**，不猜字段名。

    真机这个账号 `list: []`、`total: 0`（接口正常，确实没有子账号），
    所以列表行里到底有哪些字段名**没有真机样例** —— 按「不许编数据」的原则，
    这里不重命名、不补齐任何字段，前端拿到什么渲染什么。
    `total` 是平台明说的数字，可以照用（0 是平台给的，不是我们补的）。
    """
    params, failure = _page(page, size)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure

    payload, failure = _call(client, 'sub_accounts',
                             {'pageNo': params[0], 'pageSize': params[1]})
    if failure:
        return failure
    inner, failure = _unwrap(payload)
    if failure:
        return failure

    rows = inner.get('list')
    items: Optional[List[Any]] = rows if isinstance(rows, list) else None
    warnings: List[str] = []
    if items is None:
        warnings.append(WARN_ITEMS_MISSING)

    quota, why = _best_effort(client, 'sub_account_quota')
    if quota is None:
        warnings.append(f'{WARN_QUOTA_UNAVAILABLE}（{why}）' if why else WARN_QUOTA_UNAVAILABLE)
    roles, why = _best_effort(client, 'roles')
    if not isinstance(roles, list):
        # 口径同 items/groups：**非 list 一律 warning** —— 平台给了数组就用，
        # 给了对象/标量/什么都没给都算「拿不到」，不能静默变成 null。
        warnings.append(f'平台未返回角色列表（不是「没有角色」）: {why}' if why
                        else '平台未返回角色列表（不是「没有角色」）')

    return {
        'account_id': account_id,
        'page': _number_or_none(inner.get('pageNo')) or params[0],
        'page_size': _number_or_none(inner.get('pageSize')) or params[1],
        'total': _number_or_none(inner.get('total')),
        'total_page': _number_or_none(inner.get('totalPage')),
        'has_next_page': inner.get('hasNextPage'),
        'items': items,
        'quota': quota,
        'roles': roles if isinstance(roles, list) else None,
        'warnings': warnings,
        'fetched_at': _stamp(),
        'source': 'platform',
    }


def _routing_view(routing: Any) -> Dict[str, Any]:
    """把真机 `routingRules` 拆成前端好用的形状；**缺的键一律 null**。

    真机只有 `serviceTime`，所以 `service_time` 是唯一恒存在的子对象；
    平台连 `routingRules` 都没给时 `raw` 也给 null，不返回空对象冒充。
    """
    rules = routing if isinstance(routing, dict) else None
    service_time = rules.get('serviceTime') if isinstance(rules, dict) else None
    start = end = None
    if isinstance(service_time, dict):
        start = service_time.get('startTime')
        end = service_time.get('endTime')
        service_view: Optional[Dict[str, Any]] = {'start': start, 'end': end}
    else:
        service_view = None
    return {
        'raw': rules,
        'service_time': service_view,
        'offline_message': rules.get('offlineMessage') if isinstance(rules, dict) else None,
        'service_range': rules.get('serviceRange') if isinstance(rules, dict) else None,
    }


def cs_routing(account_id: str, page: Any = '', size: Any = '') -> Dict[str, Any]:
    """客服分流：分流规则 + 客服分组 + 客服列表（三个只读接口）。"""
    params, failure = _page(page, size)
    if failure:
        return failure
    client, failure = _client(account_id)
    if failure:
        return failure

    payload, failure = _call(client, 'cs_routing_rules')
    if failure:
        return failure
    inner, failure = _unwrap(payload)
    if failure:
        return failure
    routing = _routing_view(inner.get('routingRules'))
    warnings: List[str] = []
    if routing['raw'] is None:
        # 整个 `routingRules` 都没返回 —— 和「只返回部分」是两回事，别共用一条 warning
        warnings.append(WARN_ROUTING_MISSING)
    elif routing['offline_message'] is None or routing['service_range'] is None:
        warnings.append(WARN_ROUTING_PARTIAL)

    group_payload, failure = _call(client, 'cs_groups', params[0], params[1])
    if failure:
        return failure
    group_inner, failure = _unwrap(group_payload)
    if failure:
        return failure
    groups_raw = group_inner.get('csGroupInfos')
    groups: Optional[List[Any]] = groups_raw if isinstance(groups_raw, list) else None
    if groups is None:
        warnings.append(WARN_GROUPS_MISSING)

    agents_payload, failure = _call(client, 'cs_agents')
    if failure:
        return failure
    agents_inner, failure = _unwrap(agents_payload)
    if failure:
        return failure
    agents_raw = agents_inner.get('customers')
    agents: Optional[List[Any]] = agents_raw if isinstance(agents_raw, list) else None
    if agents is None:
        warnings.append(WARN_AGENTS_MISSING)

    return {
        'account_id': account_id,
        'page': params[0],
        'page_size': params[1],
        'routing': routing,
        'groups': groups,
        'groups_total': _number_or_none(group_inner.get('total')),
        'agents': agents,
        'warnings': warnings,
        'fetched_at': _stamp(),
        'source': 'platform',
    }
