# -*- coding: utf-8 -*-
"""粉丝价服务：读当前三组配置（只读）+ 整份写回（写）。

协议层见 `xianyu/fans_price.py`（官方商品管理 bundle 静态取证，零平台请求）。
这里只做三件事：拼请求、把平台返回翻成人能看的结果、把写操作的三步顺序钉死。

**三步顺序（写路径）**：确认 → 字段校验 → 建客户端。
- 确认在最前：未确认时连字段错都不该报，否则会误导用户「填对了就能发」；
- 字段校验在建客户端之前：纯本地、免费，账号掉线时也该先说「商品 id 不能为空」。

**★ 语义是「整份替换」**（官方弹窗预填三组值再一起发）：`fansPriceList` 里没有的组
= 没配置 ⇒ **留空 = 清空该组**。所以返回里带 `submitted`（将要/已经发给平台的完整列表），
前端必须把它原样列进确认框 —— 别让用户以为「没填的组会保持原样」。

**只读路径的平台拒绝不降级**：这一族只有一个只读接口，被拒就如实 502，由
`services/platform_errors.py` 统一补人话；把「没权限」装成「没有粉丝价」是假数据。

**确认口径**：服务层用 `_truthy` 认 `True`/`'true'`/`1`（AI 与 MCP 客户端可能传字符串），
但传给协议层前**归一成 `True`** —— 协议层的 `_need_confirm` 是严格的 `is True`。

**未验证边界**：写接口一次真机请求都没发过（写验证的红线是「零平台写请求」）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..xianyu import fans_price as proto
from . import common
# 账号/凭据检查只有一份，直接复用只读服务里的私有助手（同包内，避免两套判定漂移）
from .trade import _client

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = common.CONFIRM_CODE

#: 写成功后的实时刷新话题：商品页据此静默重拉（前端 `useAsync` 第 4 参订阅它）。
TOPIC = 'items.changed'

#: 平台没返回粉丝价配置时的 warning（与「三个组都没配」分开）。
WARN_CONFIG_MISSING = {
    'code': 'fans_price_config_missing',
    'message': '平台这一次没有返回粉丝价配置。下面显示的空配置**不代表这个商品没有粉丝价**，'
               '别当成结论 —— 点「刷新」再看一次。',
}

#: 平台既没报错、也没回执时的 warning（**既不能说成功也不能说失败**）。
WARN_NO_ECHO = {
    'code': 'fans_price_no_echo',
    'message': '平台没有回执（没说成功也没说失败）。这次改动可能已经生效、也可能没有 —— '
               '请到闲鱼 App 或官方工作台确认一次，别直接当成功。',
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
    """写路径第一道闸门：**只判确认**，不碰账号、不碰平台。"""
    return common.confirm_required(action, confirm)


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
    logger.warning(f'[fans_price] 平台拒绝 {api}: {reason}')
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


def _submitted(entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """协议层的 camelCase 列表 → 我们给前端的 snake_case 列表。"""
    return [{'fans_group': entry['fansGroup'], 'price_string': entry['priceString']}
            for entry in entries]


# --------------------------------------------------------------------------- 只读


def read(account_id: str, item_id: Any) -> Dict[str, Any]:
    """读某个商品当前的三组粉丝价（`…item.operate.fansprice.get`，版本 1.0）。

    * `prices` 为 `None` = **平台没返回配置**（与「三个组都没配」的 `[]` 不同）+ warning；
    * `groups` 三个键恒在，拿不到就是 `None`（前端显示 `-`，**不许补 0**）。
    """
    problem = _missing('商品 id', item_id)
    if problem is not None:
        return problem
    try:
        api, data, version = proto.get_request(item_id)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有返回粉丝价')

    parsed = proto.parse_config(value)
    if parsed['problem']:
        return _platform_failure(value, api, parsed['problem'])

    warnings: List[Dict[str, Any]] = []
    if not parsed['found']:
        warnings.append(dict(WARN_CONFIG_MISSING))
    # 平台键名（`price_key`）只留给排查，不进给前端的契约
    prices = None if parsed['prices'] is None else [
        {'fans_group': entry['fans_group'], 'price_string': entry['price_string']}
        for entry in parsed['prices']
    ]
    return _stamp({
        'account_id': account_id,
        'item_id': data['itemId'],
        'api': api,
        'prices': prices,
        'groups': parsed['groups'],
        'warnings': warnings,
    })


# --------------------------------------------------------------------------- 写


def set_prices(account_id: str, item_id: Any, groups: Any = None,
               prices: Any = None, confirm: Any = None) -> Dict[str, Any]:
    """整份写回粉丝价（`…item.operate.fansprice.set`，版本 1.0）。

    `groups` 传字典（`{'all': '9.9', 'old': '', 'buy': None}`），`prices` 传
    `[{fans_group, price_string}]`；两个都传时以 `prices` 为准（形状更贴近平台）。
    **留空的组不会出现在 `submitted` 里 ⇒ 会被平台清空**。
    """
    problem = _confirm_gate('设置粉丝价', confirm)
    if problem is not None:
        return problem
    problem = _missing('商品 id', item_id)
    if problem is not None:
        return problem

    source = prices if prices is not None else groups
    try:
        # 归一成 True：服务层已经判过确认，协议层那道 `is True` 只防直接误用
        api, data, version = proto.set_request(item_id, source, confirm=True)
    except ValueError as exc:
        return common.fail(str(exc), 'invalid_fields')

    value, failure = _call(account_id, api, data, version)
    if failure is not None:
        return failure
    if proto.envelope_error(value):
        return _platform_failure(value, api, '平台没有接受这次设置粉丝价')

    parsed = proto.parse_set(value)
    warnings: List[Dict[str, Any]] = []
    if not parsed['saved']:
        if parsed['platform_message']:
            # 业务层拒绝时把**平台那句原话**透出去
            return _platform_failure(value, api, parsed['platform_message'])
        # 既没报错也没回执：如实说「不知道」，不当成功也不当失败
        warnings.append(dict(WARN_NO_ECHO))

    item_id_text = data['itemId']
    submitted = _submitted(data['fansPriceConfig']['fansPriceList'])
    if parsed['saved']:
        common.log_operation(account_id, 'fans_price_set', item_id_text, True, {
            'item_id': item_id_text,
            'submitted': submitted,
        })
        common.publish(TOPIC, {
            'account_id': account_id,
            'item_id': item_id_text,
            'action': 'fans_price_set',
        })

    return _stamp({
        'account_id': account_id,
        'action': 'fans_price_set',
        'api': api,
        'item_id': item_id_text,
        'saved': bool(parsed['saved']),
        'submitted': submitted,
        'platform_message': parsed['platform_message'],
        'warnings': warnings,
    })
