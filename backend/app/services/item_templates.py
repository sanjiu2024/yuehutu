"""运费模版（官方 `post-temple` 页）只读服务：列表 + 详情。

口径（与其它只读镜像页一致）：

1. **只读**：这里只调 `pro.freight.template.query` / `.detail`，不碰 create/modify/del/unbind。
2. **不造数据**：平台没给的字段一律 `None`（`total` / `has_next` 拿不到就是 `None`，不是 0）；
   平台返回的模版对象原样放在 `raw` 里，只有**已证实**的字段名（`templateId` / `templateName`，
   证据见 `docs/contracts/item_templates.md`）才提升成 `template_id` / `template_name`。
3. **假数据比缺数据更坏**：本机真机账号 `2217970177348` 一个模版都没有（`totalCount: 0`），
   所以模版元素的完整字段表尚未证实 —— 未证实的字段不猜。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core import db
from ..xianyu import item_templates as proto
from . import common


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _credential_failure(account_id: str) -> Optional[Dict[str, Any]]:
    row = db.query_one('SELECT cookie, login_state FROM account_credentials WHERE account_id = ?',
                       (account_id,))
    if row is None or not _text(row['cookie']).strip():
        return common.fail(f'账号 {account_id} 还没有 Cookie，请先在「账号」页完成扫码登录',
                           'verification_required', login_state='need_relogin')
    if _text(row['login_state']).strip() == 'need_relogin':
        return common.fail(f'账号 {account_id} 登录态已过期，需要重新扫码登录',
                           'verification_required', login_state='need_relogin')
    return None


def _client(account_id: str):
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return None, common.fail(f'账号 {account_id} 不存在', 'not_found')
    failure = _credential_failure(account_id)
    if failure:
        return None, failure
    value, problem = common.get_client(account_id)
    return (value, None) if value is not None else (None, problem or common.not_ready())


def _facade(account_id: str):
    client, failure = _client(account_id)
    if failure:
        return None, failure
    return proto.ItemTemplatesClient(client, account_id), None


def _call(facade: Any, method: str, *args: Any):
    """调门面方法；`missing` 说明协议层没这个方法，`error` 保留原始异常文案。"""
    status, value = common.call_method(facade, (method,), *args)
    if status == common.CALL_OK:
        return value, None
    failure = common.call_failed(status, value)
    if failure:
        return None, failure
    return None, common.fail(_text(value) or '运费模版接口调用失败', 'protocol_error')


def _payload(result: Any) -> Any:
    """MTOP 返回 `{"success","data","message","ret"}`；业务体在 `data` 里。"""
    if isinstance(result, dict) and 'data' in result:
        return result.get('data')
    return result


def _now() -> str:
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


def _template_entry(raw: Any) -> Dict[str, Any]:
    """平台返回的模版对象 → 我们的条目：**已证实**的字段提升，其余原样留在 `raw`。"""
    item = raw if isinstance(raw, dict) else {}
    template_id = item.get('templateId')
    template_name = item.get('templateName')
    return {
        'template_id': '' if template_id in (None, '') else str(template_id),
        'template_name': '' if template_name in (None, '') else str(template_name),
        'raw': raw,
    }


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def list_templates(account_id: str = '', page: str = '', size: str = '',
                   template_name: str = '') -> Dict[str, Any]:
    """模版列表。分页参数只做上限保护，避免一次拉爆平台接口。"""
    try:
        page_no = max(1, int(_text(page).strip() or 1))
        page_size = int(_text(size).strip() or 20)
    except (TypeError, ValueError):
        return common.fail('分页参数必须是整数', 'invalid_fields')
    if page_size < 1 or page_size > 50:
        return common.fail('每页条数必须在 1~50 之间', 'invalid_fields')

    facade, failure = _facade(account_id)
    if failure:
        return failure
    result, failure = _call(facade, 'fetch_temple_list', page_no, page_size, '',
                            _text(template_name).strip())
    if failure:
        return failure

    payload = _payload(result)
    body = payload if isinstance(payload, dict) else {}
    rows = body.get('data') if isinstance(body.get('data'), list) else []
    entries: List[Dict[str, Any]] = [_template_entry(row) for row in rows]
    return {
        'account_id': account_id,
        'templates': entries,
        'count': len(entries),
        # 平台没返回就是 None —— 不拿 0 冒充「一个都没有」
        'total': _int_or_none(body.get('totalCount')),
        'page': _int_or_none(body.get('currentPage')) or page_no,
        'page_size': _int_or_none(body.get('pageSize')) or page_size,
        'has_next': body.get('hasNextPage') if isinstance(body.get('hasNextPage'), bool) else None,
        'fetched_at': _now(),
        'source': 'platform',
    }


def template_detail(account_id: str = '', template_id: str = '') -> Dict[str, Any]:
    """模版详情。平台没这个模版时如实 404（`not_found`），不返回空壳 200。"""
    wanted = _text(template_id).strip()
    if not wanted:
        return common.fail('缺少模版 id', 'invalid_fields')
    facade, failure = _facade(account_id)
    if failure:
        return failure
    result, failure = _call(facade, 'fetch_temple_detail', wanted)
    if failure:
        return failure

    payload = _payload(result)
    if payload in (None, {}, []):
        return common.fail(f'平台上没有这个运费模版: {wanted}', 'not_found')
    entry = _template_entry(payload)
    if not entry['template_id']:
        entry['template_id'] = wanted
    return {
        'account_id': account_id,
        'template': entry,
        'fetched_at': _now(),
        'source': 'platform',
    }
