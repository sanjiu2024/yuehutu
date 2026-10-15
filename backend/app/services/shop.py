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

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core import db
from ..xianyu import shop as shop_mod
from ..xianyu import shop_write as shop_write_mod
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


# --------------------------------------------------------------------------- 写操作
#
# 与只读段分开：只读走 `_call(client, 协议函数名, *args)`（协议函数自己收 client），
# 写走 `common.call_method(client, ('mtop',), api, data, v='1.0')`（协议层只给
# `(api, data)` 二元组）。
#
# **三步顺序固定：确认 → 字段校验 → 建客户端**（与 `items` / `item_templates` /
# `trade_write` / `drafts` 同口径）：
#
# 1. 确认排在最前 —— 否则用户填对了字段也会被误导成「填对就能发」；
# 2. 字段校验在建客户端之前 —— 它纯本地、免费，掉线时也该先说「名称没填」，
#    而不是「去重新登录」。

#: 未确认时的错误码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = common.CONFIRM_CODE

WARN_NO_GROUP_ID = '平台没有回显新分组的 id，请重新拉一次分组列表确认'
WARN_RULE_NOT_ECHOED = ('平台没有回显保存后的分流规则（官方前端也是重拉列表确认）——'
                        '这次提交已经发出，配置是否生效请点「刷新」看平台当前值')


#: 判据单点：services/common.confirm_truthy（宽松档，全仓唯一一份实现）。
_truthy = common.confirm_truthy


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰库、不碰账号、不碰平台。"""
    return common.confirm_required(action, confirm, message=f'{action} 是写操作，必须显式确认（confirm=true）后才会提交到闲鱼')


def _build_write(action: str, kwargs: Dict[str, Any]
                 ) -> Tuple[Optional[Tuple[str, Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """构造写请求；协议层的 `ValueError`（枚举不合法 / 必填缺失）翻成 400 `invalid_fields`。"""
    try:
        return shop_write_mod.request_of(action, confirm=True, **kwargs), None
    except shop_write_mod.UnknownApi as exc:
        return None, common.fail(f'未知的写动作：{exc}', 'invalid_fields')
    except ValueError as exc:
        return None, common.fail(_text(exc), 'invalid_fields')


def _send_write(client: Any, api: str, data: Dict[str, Any], action: str,
                account_id: str) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """发一个写请求并判成败；失败逐条落 operation_logs。"""
    status, value = common.call_method(
        client, ('mtop',), api, data, v=shop_write_mod.DEFAULT_VERSION)
    failure = common.call_failed(status, value)
    if failure:
        common.log_operation(account_id, action, '', False, failure.get('message', ''))
        return None, failure
    problem = shop_write_mod.envelope_error(value)
    if problem:
        common.log_operation(account_id, action, '', False, problem)
        return None, common.fail(
            f'平台接口失败: {problem}', 'protocol_error',
            platform_errors={'api': api, 'message': problem})
    return value, None


def save_routing_rule(account_id: str, distribution_method: Any = '', repeat_customer: Any = '',
                      offline_message: Any = '', specified_account_id: Any = None,
                      specified_account_nick: Any = None, service_time: Any = None,
                      confirm: Any = False) -> Dict[str, Any]:
    """保存客服分流规则（`mtop.taobao.idlemessage.customer.routing.rule.operate`）。

    会**真的**改动这个账号的客服分流配置。成功判据只看「客户端没报错 且 平台没报错码」，
    平台没回显规则时 `rule` 是 `None` + 一条 warning —— 不拿我们发出去的内容冒充平台回执。
    """
    failure = _confirm_gate('保存客服分流规则', confirm)
    if failure:
        return failure
    built, failure = _build_write('routing_rule', {
        'distribution_method': distribution_method,
        'repeat_customer': repeat_customer,
        'offline_message': offline_message,
        'specified_account_id': specified_account_id,
        'specified_account_nick': specified_account_nick,
        'service_time': service_time,
    })
    if failure:
        return failure
    api, data = built
    client, failure = _client(account_id)
    if failure:
        return failure

    # 幂等（第 63 批，§9）：同键同载荷只打平台一次，重复请求回放上次响应（带
    # `idempotent_replay`）—— 「发请求 + 落审计 + 广播」都在闸门里，回放时不再打平台。
    return common.idempotent_call(
        account_id, 'cs_routing_rule_save', '', data,
        lambda: _save_routing_rule_once(client, api, data, account_id),
    )


def _save_routing_rule_once(client: Any, api: str, data: Dict[str, Any],
                            account_id: str) -> Dict[str, Any]:
    """真正打平台 + 落审计 + 广播的那一次（幂等回放时不会走到这里）。"""
    value, failure = _send_write(client, api, data, 'cs_routing_rule_save', account_id)
    if failure:
        return failure

    try:
        submitted = json.loads(data.get('routingRules') or '{}')
    except (TypeError, ValueError):  # pragma: no cover - 我们自己拼的 JSON，不该坏
        submitted = None
    echoed = shop_write_mod.saved_rule_of(value)
    warnings: List[str] = [] if echoed is not None else [WARN_RULE_NOT_ECHOED]
    common.log_operation(account_id, 'cs_routing_rule_save', '', True, submitted)
    common.publish('shop.changed', {'account_id': account_id, 'action': 'routing_rule'})
    return {
        'account_id': account_id,
        'action': 'routing_rule',
        'api': api,
        'saved': True,
        'submitted': submitted,
        'rule': echoed,
        'warnings': warnings,
        'fetched_at': _stamp(),
        'source': 'platform',
    }


def save_cs_group(account_id: str, group_name: Any = '', mode: Any = '',
                  operate_type: Any = 'create', group_id: Any = None,
                  specified_type: Any = None, specified_type_name: Any = None,
                  type_data: Any = None, customer_uids: Any = None,
                  confirm: Any = False) -> Dict[str, Any]:
    """新建 / 修改客服分组（`mtop.taobao.idlemessage.customer.group.operate`）。

    `operate_type='update'` 必须给 `group_id`；新增时平台若没回显 id，`group_id` 是 `None`
    + 一条 warning（官方前端也是重拉分组列表确认）。
    """
    failure = _confirm_gate('保存客服分组', confirm)
    if failure:
        return failure
    built, failure = _build_write('group_save', {
        'group_name': group_name,
        'mode': mode,
        'operate_type': operate_type,
        'group_id': group_id,
        'specified_type': specified_type,
        'specified_type_name': specified_type_name,
        'type_data': type_data,
        'customer_uids': customer_uids,
    })
    if failure:
        return failure
    api, data = built
    client, failure = _client(account_id)
    if failure:
        return failure

    # 幂等（第 63 批，§9）：同键同载荷只打平台一次，重复请求回放上次响应（带 `idempotent_replay`）。
    return common.idempotent_call(
        account_id, 'cs_group_save', _text(data.get('groupId')), data,
        lambda: _save_cs_group_once(client, api, data, account_id, group_id, mode),
    )


def _save_cs_group_once(client: Any, api: str, data: Dict[str, Any], account_id: str,
                        group_id: Any, mode: Any) -> Dict[str, Any]:
    """真正打平台 + 落审计 + 广播的那一次（幂等回放时不会走到这里）。"""
    value, failure = _send_write(client, api, data, 'cs_group_save', account_id)
    if failure:
        return failure

    kind = _text(data.get('operateType'))
    echoed = shop_write_mod.group_id_of(value)
    resolved = echoed or (_text(group_id) or None)
    warnings: List[str] = []
    if kind == 'create' and not echoed:
        warnings.append(WARN_NO_GROUP_ID)
    detail = {'group_name': _text(data.get('groupName')), 'mode': mode, 'operate_type': kind}
    common.log_operation(account_id, 'cs_group_save', resolved or '', True, detail)
    common.publish('shop.changed', {'account_id': account_id, 'action': 'group_save'})
    return {
        'account_id': account_id,
        'action': 'group_save',
        'api': api,
        'operate_type': kind,
        'group_id': resolved,
        'saved': True,
        'submitted': detail,
        'warnings': warnings,
        'fetched_at': _stamp(),
        'source': 'platform',
    }


def delete_cs_group(account_id: str, group_id: Any = '', confirm: Any = False) -> Dict[str, Any]:
    """删除客服分组（同 `GROUP_API`，`operateType='delete'`）。**不可撤销。**"""
    failure = _confirm_gate('删除客服分组', confirm)
    if failure:
        return failure
    built, failure = _build_write('group_delete', {'group_id': group_id})
    if failure:
        return failure
    api, data = built
    client, failure = _client(account_id)
    if failure:
        return failure

    # 幂等（第 63 批，§9）：同键同载荷只打平台一次，重复请求回放上次响应（带 `idempotent_replay`）。
    return common.idempotent_call(
        account_id, 'cs_group_delete', _text(data.get('groupId')), data,
        lambda: _delete_cs_group_once(client, api, data, account_id),
    )


def _delete_cs_group_once(client: Any, api: str, data: Dict[str, Any],
                          account_id: str) -> Dict[str, Any]:
    """真正打平台 + 落审计 + 广播的那一次（幂等回放时不会走到这里）。"""
    value, failure = _send_write(client, api, data, 'cs_group_delete', account_id)
    if failure:
        return failure

    key = _text(data.get('groupId'))
    common.log_operation(account_id, 'cs_group_delete', key, True, {'group_id': key})
    common.publish('shop.changed', {'account_id': account_id, 'action': 'group_delete'})
    return {
        'account_id': account_id,
        'action': 'group_delete',
        'api': api,
        'operate_type': 'delete',
        'group_id': key,
        'deleted': True,
        'warnings': [],
        'fetched_at': _stamp(),
        'source': 'platform',
    }


# --------------------------------------------------------------------------- READ-COVERAGE-61
def pc_service_status(account_id: str, service_code: Any) -> Dict[str, Any]:
    """发布页服务开通状态（mtop.alibaba.idle.pc.service.status.query，只读）。

    service_code 必填（官方就是拿它查）。平台响应形状未取证，所以 data 原样透出；
    平台没给业务体时是 null + warning，**不补 false**（那会变成「这项服务没开通」的假事实）。
    """
    code = '' if service_code is None else str(service_code).strip()
    if not code:
        return common.fail('service_code 不能为空', 'invalid_fields')
    client, failure = _client(account_id)
    if failure:
        return failure
    payload, failure = _call(client, 'pc_service_status', code)
    if failure:
        return failure
    inner, failure = _unwrap(payload)
    if failure:
        return failure
    warnings: List[str] = []
    if not inner:
        warnings.append('平台没有返回这项服务的状态字段（响应形状未取证），别把空当成「未开通」。')
    return {
        'account_id': account_id,
        'service_code': code,
        'api': shop_mod.PC_SERVICE_STATUS_API,
        'data': inner or None,
        'warnings': warnings,
        'fetched_at': _stamp(),
        'source': 'platform',
    }
