"""运费模版（官方 `post-temple` 页）服务：列表 / 详情（只读）+ 新建 / 修改 / 删除 / 设置运费（写）。

口径（与其它镜像页一致）：

1. **读**：只调 `pro.freight.template.query` / `.detail` / `.item.query` 等只读接口。
   **写**：新建 / 修改 / 删除 / 给商品设置运费（包邮 / 用模版 / 固定运费）—— 全部要
   `confirm=true`，未确认**一个平台请求都不发**。
2. **不造数据**：平台没给的字段一律 `None`（`total` / `has_next` 拿不到就是 `None`，不是 0）；
   平台返回的模版对象原样放在 `raw` 里，只有**已证实**的字段名（`templateId` / `templateName`，
   证据见 `docs/contracts/item_templates.md`）才提升成 `template_id` / `template_name`。
3. **假数据比缺数据更坏**：本机真机账号 `2217970177348` 一个模版都没有（`totalCount: 0`），
   所以模版元素的完整字段表尚未证实 —— 未证实的字段不猜。
4. **写成功以平台业务码为准**：官方 bundle 判据是 `res.data.code === 'success'`，
   传输层 `success:true` 不算成功；失败把平台原文（`data.msg`）原样交出去。
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


# --------------------------------------------------------------------------- 写
#
# 三条硬口径（与其它写服务一致）：
#   1. `confirm` 必须是字面量 `true`，否则**一个平台请求都不发**就返回 400；
#   2. 平台业务码才是判据（官方 bundle 原文 `res.data.code === 'success'`），
#      传输层 `success:true` 不算成功；
#   3. 拿不到的一律 `None`，不拿 `0` / `''` 冒充。

#: 未确认时返回的业务码。
CONFIRM_CODE = 'confirmation_required'

#: 模版名长度上限（官方表单与 bundle 都按 30 字校验）。
TEMPLATE_NAME_MAX = 30


def _confirm_required(action: str) -> Dict[str, Any]:
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交到闲鱼',
        CONFIRM_CODE,
    )


def _truthy(value: Any) -> bool:
    """只有字面量 `True` / `'true'` / `1` 才算确认，别的（含 `'false'`、`'yes'`）都不算。"""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value == 1


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _missing(label: str, value: Any) -> Optional[Dict[str, Any]]:
    return common.fail(f'{label}不能为空', 'invalid_fields') if _blank(value) else None


def _write_call(facade: Any, method: str, *args: Any):
    """调一次写门面方法。协议层没有这个方法（半升级）时给 503 而不是 500。"""
    status, value = common.call_method(facade, (method,), *args)
    if status == common.CALL_OK:
        return value, None
    failure = common.call_failed(status, value)
    if failure:
        return None, failure
    return None, common.fail(_text(value) or '运费模版写接口调用失败', 'protocol_error')


def _outcome(result: Any) -> Dict[str, Any]:
    """把 mtop 回包折成 `{ok, code, message, data}`。

    平台业务体在 `result['data']` 里，形状是 `{code, msg, data}`；
    `code == 'success'` 才算成功（bundle 原文判据）。
    """
    payload = _payload(result)
    body = payload if isinstance(payload, dict) else {}
    code = body.get('code')
    message = body.get('msg')
    if message in (None, ''):
        message = body.get('message')
    return {
        'ok': code == proto.SUCCESS_CODE,
        'code': None if code is None else str(code),
        'message': '' if message is None else str(message),
        'data': body.get('data'),
    }


def _write_result(account_id: str, action: str, outcome: Dict[str, Any],
                  *, template_id: Any = None) -> Dict[str, Any]:
    """成功 → 统一信封；失败 → 502 并把平台原文交出去（不吞、不美化）。"""
    if not outcome['ok']:
        detail = outcome['message'] or outcome['code'] or '平台没有返回业务码，无法判断是否成功'
        failure = common.fail(f'运费模版{action}失败：{detail}', 'protocol_error')
        failure['platform_errors'] = {
            'action': action,
            'code': outcome['code'],
            'message': outcome['message'],
        }
        return failure

    created = outcome['data']
    resolved = template_id
    if resolved in (None, '') and isinstance(created, dict):
        resolved = created.get('templateId')
    return {
        'account_id': account_id,
        'action': action,
        'template_id': None if resolved in (None, '') else resolved,
        'code': outcome['code'],
        'message': outcome['message'],
        'raw': created,
        'submitted_at': _now(),
        'source': 'platform',
    }


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径的第一道闸门：**只判确认**，不碰账号、不碰平台。

    `confirm` 走显式关键字：HTTP 端点把 `body.confirm` 取出来传进来，AI / MCP 工具路径
    直接按服务函数签名传 —— 两条路都用同一个参数，不会一条能确认、另一条永远确认不了。

    三步顺序是 **确认 → 字段校验 → 建客户端**：确认在最前（未确认时连字段错都不该报，
    否则会误导用户以为「填对了就能发」）；字段校验在建客户端之前（纯本地、免费，
    账号掉线时也该先告诉他「名称没填」而不是「去重新登录」）。
    """
    if not _truthy(confirm):
        return _confirm_required(f'运费模版{action}')
    return None


def _write_facade(account_id: str):
    """写路径的最后一步前置：建客户端（会触发凭据检查，可能 409 / 502）。"""
    facade, failure = _facade(account_id)
    if failure:
        return None, failure
    return facade, None


def _template_payload(*, template_name: Any, item_prov: Any = '', item_city: Any = '',
                      valuation: Any = None, is_default: Any = False, details: Any = None,
                      template_details: Any = '', template_id: Any = '',
                      version: Any = None) -> Dict[str, Any]:
    return proto.build_template_payload(
        template_name=_text(template_name).strip(),
        item_prov=_text(item_prov).strip(),
        item_city=_text(item_city).strip(),
        valuation=valuation,
        is_default=bool(is_default),
        details=details,
        template_details=_text(template_details).strip(),
        template_id=template_id,
        version=version,
    )


def _check_template_fields(template_name: Any, details: Any, *, template_id: Any = '',
                           need_id: bool = False) -> Optional[Dict[str, Any]]:
    if need_id:
        problem = _missing('模版 id', template_id)
        if problem:
            return problem
    name = _text(template_name).strip()
    problem = _missing('模版名称', name)
    if problem:
        return problem
    if len(name) > TEMPLATE_NAME_MAX:
        return common.fail(f'模版名称最多 {TEMPLATE_NAME_MAX} 个字，现在 {len(name)} 个',
                           'invalid_fields')
    if details is not None and not isinstance(details, (list, tuple)):
        return common.fail('明细（details）必须是数组', 'invalid_fields')
    return None


def create_template(account_id: str = '', template_name: Any = '', item_prov: Any = '',
                    item_city: Any = '', valuation: Any = None, is_default: Any = False,
                    details: Any = None, template_details: Any = '',
                    confirm: Any = None) -> Dict[str, Any]:
    """新建运费模版。"""
    problem = _confirm_gate('新建', confirm)
    if problem:
        return problem
    problem = _check_template_fields(template_name, details)
    if problem:
        return problem
    facade, failure = _write_facade(account_id)
    if failure:
        return failure
    result, failure = _write_call(
        facade, proto.WRITE_METHODS['create'],
        _template_payload(template_name=template_name, item_prov=item_prov,
                          item_city=item_city, valuation=valuation, is_default=is_default,
                          details=details, template_details=template_details))
    if failure:
        return failure
    return _write_result(account_id, '新建', _outcome(result))


def modify_template(account_id: str = '', template_id: str = '', template_name: Any = '',
                    item_prov: Any = '', item_city: Any = '', valuation: Any = None,
                    is_default: Any = False, details: Any = None, template_details: Any = '',
                    version: Any = None, confirm: Any = None) -> Dict[str, Any]:
    """修改运费模版（官方修改时会带上 `version`，列表里查得到就一起传）。"""
    problem = _confirm_gate('修改', confirm)
    if problem:
        return problem
    problem = _check_template_fields(template_name, details, template_id=template_id, need_id=True)
    if problem:
        return problem
    facade, failure = _write_facade(account_id)
    if failure:
        return failure
    result, failure = _write_call(
        facade, proto.WRITE_METHODS['modify'],
        _template_payload(template_name=template_name, item_prov=item_prov,
                          item_city=item_city, valuation=valuation, is_default=is_default,
                          details=details, template_details=template_details,
                          template_id=template_id, version=version))
    if failure:
        return failure
    return _write_result(account_id, '修改', _outcome(result), template_id=template_id)


def delete_template(account_id: str = '', template_id: str = '',
                    confirm: Any = None) -> Dict[str, Any]:
    """删除运费模版。不可撤销，所以同样要 `confirm`。"""
    problem = _confirm_gate('删除', confirm)
    if problem:
        return problem
    problem = _missing('模版 id', template_id)
    if problem:
        return problem
    facade, failure = _write_facade(account_id)
    if failure:
        return failure
    result, failure = _write_call(facade, proto.WRITE_METHODS['delete'], template_id)
    if failure:
        return failure
    return _write_result(account_id, '删除', _outcome(result), template_id=template_id)


def unbind_items(account_id: str = '', item_ids: Any = None, mode: Any = '',
                 template_id: Any = '', post_price: Any = None,
                 confirm: Any = None) -> Dict[str, Any]:
    """给商品设置运费：包邮 / 用模版 / 固定运费（官方页面同一入口）。"""
    problem = _confirm_gate('设置运费', confirm)
    if problem:
        return problem

    mode = _text(mode).strip()
    if mode not in proto.UNBIND_MODES:
        return common.fail(
            '模式（mode）必须是 %s 之一' % ' / '.join(proto.UNBIND_MODES), 'invalid_fields')

    ids = proto.normalize_item_ids(item_ids)
    problem = _missing('商品 id（item_ids）', ids)
    if problem:
        return problem

    if mode == 'template':
        problem = _missing('模版 id（mode=template 必填）', template_id)
        if problem:
            return problem
    elif mode == 'fixed':
        try:
            amount = float(post_price)
        except (TypeError, ValueError):
            return common.fail('固定运费（post_price）必须是数字', 'invalid_fields')
        if amount <= 0:
            return common.fail('固定运费（post_price）必须大于 0', 'invalid_fields')
        post_price = amount

    facade, failure = _write_facade(account_id)
    if failure:
        return failure

    payload = proto.build_unbind_payload(item_ids=ids, mode=mode,
                                         template_id=template_id, post_price=post_price)
    result, failure = _write_call(facade, proto.WRITE_METHODS['unbind'], payload)
    if failure:
        return failure
    outcome = _outcome(result)
    if not outcome['ok']:
        return _write_result(account_id, '设置运费', outcome)
    return {
        'account_id': account_id,
        'action': '设置运费',
        'mode': mode,
        'item_ids': ids,
        'code': outcome['code'],
        'message': outcome['message'],
        'raw': outcome['data'],
        'submitted_at': _now(),
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
