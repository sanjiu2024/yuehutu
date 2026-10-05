"""商品服务：列表 / 单条 / 同步 / 修改 / 批量修改 / 发布 / 上下架 / 统计。

设计要点：
- **本地优先**：改价、改标题这类操作先落库，再尽力推给协议层。协议层没就绪
  时本地依然生效，只是返回 `synced: false` + warning —— 卖家在 UI 上的操作不该白做。
- **批量逐条**：`batch_update` / `downshelf` / `reshelf` 一条失败不影响其它条，且**每条**都写
  operation_logs。返回
  `{"total": n, "ok": n, "synced": n, "local_only": n, "warnings": [...], "failed": [...], "skipped": [...]}`：
  - `ok` / `synced` **只统计完全成功**（本地已落库 **且** 已推送到协议层）；
  - `warnings` / `local_only` 是「本地改了但没推到平台」（协议层未就绪或调用失败），**不算 ok**；
  - `failed` / `skipped` 是「完全没改」（不存在、字段非法、跨账号）。
  协议层没就绪时 `ok` 必须是 0 —— 否则用户看到「已批量修改 N 个」而实际一条都没推出去。
- 需要协议层才能成立的操作（同步、发布）在协议层缺失时返回
  `{"success": false, "code": "xianyu_not_ready"}`。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core import db
from . import common

# 中文/别名 -> items 表列名（用户口径「价格/标题/描述/库存」都要认）
FIELD_ALIASES: Dict[str, str] = {
    'price': 'price',
    '价格': 'price',
    '售价': 'price',
    'title': 'title',
    '标题': 'title',
    'description': 'description',
    'desc': 'description',
    '描述': 'description',
    '详情': 'description',
    'stock': 'stock',
    '库存': 'stock',
    'original_price': 'original_price',
    '原价': 'original_price',
    'images': 'images',
    '图片': 'images',
    'category_id': 'category_id',
    '分类': 'category_id',
    'status': 'status',
    '状态': 'status',
    'ai_enabled': 'ai_enabled',
}

NUMERIC_FIELDS = {'price', 'original_price', 'stock'}
JSON_FIELDS = {'images'}

# 批量补丁里的计算型字段（CONTRACTS §3 的 batch-edit）
PATCH_KEYS = {'price_delta', 'price_set', 'title_tpl', 'title_prefix', 'title_suffix', 'stock_set'}


def _row_to_item(row: Any) -> Dict[str, Any]:
    item = dict(row)
    images = item.get('images')
    if isinstance(images, str) and images:
        try:
            parsed = json.loads(images)
            item['images'] = parsed if isinstance(parsed, list) else [images]
        except ValueError:
            item['images'] = [images]
    elif not images:
        item['images'] = []
    item['ai_enabled'] = bool(item.get('ai_enabled'))
    return item


def _find_row(item_id: Any) -> Optional[Any]:
    """按闲鱼 item_id 查；查不到且给的是数字时按本地主键再查一次。"""
    text = str(item_id)
    row = db.query_one('SELECT * FROM items WHERE item_id = ?', (text,))
    if row is None and text.isdigit():
        row = db.query_one('SELECT * FROM items WHERE id = ?', (int(text),))
    return row


def _find_row_for_account(account_id: Any, item_id: Any) -> Optional[Any]:
    """按 `(account_id, item_id)` **严格配对**查；数字 item_id 允许按本地主键再查一次。

    唯一的键是 `(account_id, item_id)`（`core/db.py` 的 DDL），只按 item_id 回落会让
    路径里的 `account_id` 形同虚设 —— 拿 A 账号的路径能改到 B 账号的商品。
    """
    text = str(item_id)
    owner = str(account_id)
    row = db.query_one(
        'SELECT * FROM items WHERE account_id = ? AND item_id = ?', (owner, text)
    )
    if row is None and text.isdigit():
        row = db.query_one('SELECT * FROM items WHERE account_id = ? AND id = ?', (owner, int(text)))
    return row


# --------------------------------------------------------------------------- 读


def list_items(
    account_id: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    size: int = 50,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 50), 500))
    where: List[str] = []
    params: List[Any] = []
    if account_id:
        where.append('account_id = ?')
        params.append(account_id)
    if status:
        where.append('status = ?')
        params.append(status)
    if keyword:
        where.append('(title LIKE ? OR item_id LIKE ? OR description LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like, like, like])
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''

    total_row = db.query_one(f'SELECT COUNT(*) AS n FROM items{clause}', params)
    total = int(total_row['n']) if total_row is not None else 0
    rows = db.query(
        f'SELECT * FROM items{clause} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?',
        params + [size, (page - 1) * size],
    )
    return {
        'items': [_row_to_item(r) for r in rows],
        'total': total,
        'page': page,
        'size': size,
        'pages': (total + size - 1) // size,
    }


def get_item(item_id: Any) -> Any:
    row = _find_row(item_id)
    if row is None:
        return common.fail(f'商品不存在: {item_id}', 'not_found')
    return _row_to_item(row)


def get_item_by_account(account_id: str, item_id: Any) -> Any:
    """CONTRACTS §3：`GET /api/items/{account_id}/{item_id}` 的严格配对查询。

    **必须** account_id + item_id 同时命中：同一个 item_id 可能存在于多个账号，
    只按 item_id 回落会让调用方拿到别的账号的商品（MCP `items_get` 直连这条，
    串号是危险的）。数字 item_id 允许按本地主键查一次，但仍要求账号一致。
    """
    row = _find_row_for_account(account_id, item_id)
    if row is None:
        return common.fail(f'商品不存在: {account_id}/{item_id}', 'not_found')
    return _row_to_item(row)


def item_stats() -> Dict[str, Any]:
    by_status = {
        r['status']: int(r['n'])
        for r in db.query('SELECT status, COUNT(*) AS n FROM items GROUP BY status')
    }
    total_row = db.query_one('SELECT COUNT(*) AS n, COALESCE(SUM(price * stock), 0) AS v FROM items')
    accounts = db.query('SELECT account_id, COUNT(*) AS n FROM items GROUP BY account_id')
    return {
        'total': int(total_row['n']) if total_row else 0,
        'by_status': by_status,
        'on_sale': by_status.get('on_sale', 0) + by_status.get('onsale', 0) + by_status.get('在售', 0),
        'by_account': {r['account_id']: int(r['n']) for r in accounts},
        'inventory_value': round(float(total_row['v']), 2) if total_row else 0.0,
    }


def list_operation_logs(account_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 1000))
    if account_id:
        rows = db.query(
            'SELECT * FROM operation_logs WHERE account_id = ? ORDER BY id DESC LIMIT ?',
            (account_id, limit),
        )
    else:
        rows = db.query('SELECT * FROM operation_logs ORDER BY id DESC LIMIT ?', (limit,))
    out = []
    for row in rows:
        log = dict(row)
        log['success'] = bool(log.get('success'))
        out.append(log)
    return out


# --------------------------------------------------------------------------- 字段规范化


def normalize_fields(fields: Optional[Dict[str, Any]]) -> Any:
    """把用户口径的字段名翻成列名并做类型转换；有非法字段时返回失败信封。"""
    if not isinstance(fields, dict) or not fields:
        return common.fail('fields 不能为空', 'invalid_fields')
    patch: Dict[str, Any] = {}
    unknown: List[str] = []
    for key, value in fields.items():
        column = FIELD_ALIASES.get(str(key))
        if column is None:
            if str(key) in PATCH_KEYS:
                patch[str(key)] = value
                continue
            unknown.append(str(key))
            continue
        if column in NUMERIC_FIELDS:
            try:
                patch[column] = float(value) if column != 'stock' else int(float(value))
            except (TypeError, ValueError):
                return common.fail(f'字段 {key} 需要数字，收到: {value!r}', 'invalid_fields')
        elif column in JSON_FIELDS:
            patch[column] = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        elif column == 'ai_enabled':
            patch[column] = 1 if common.truthy(value, True) else 0
        else:
            patch[column] = '' if value is None else str(value)
    if unknown:
        return common.fail(f'不支持的字段: {", ".join(unknown)}', 'invalid_fields')
    if not patch:
        return common.fail('fields 不能为空', 'invalid_fields')
    if 'price' in patch and patch['price'] < 0:
        return common.fail('价格不能为负', 'invalid_fields')
    if 'stock' in patch and patch['stock'] < 0:
        return common.fail('库存不能为负', 'invalid_fields')
    return patch


def _apply_patch(item: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """在单条商品上应用补丁（含 price_delta / title_tpl 这类计算字段）。"""
    resolved: Dict[str, Any] = {}
    for key, value in patch.items():
        if key == 'price_delta':
            resolved['price'] = round(float(item.get('price') or 0) + float(value), 2)
        elif key == 'price_set':
            resolved['price'] = round(float(value), 2)
        elif key == 'stock_set':
            resolved['stock'] = int(float(value))
        elif key == 'title_prefix':
            resolved['title'] = f'{value}{item.get("title") or ""}'
        elif key == 'title_suffix':
            resolved['title'] = f'{item.get("title") or ""}{value}'
        elif key == 'title_tpl':
            resolved['title'] = str(value).format(
                title=item.get('title') or '', price=item.get('price') or 0,
                stock=item.get('stock') or 0, item_id=item.get('item_id') or '',
            )
        else:
            resolved[key] = value
    if resolved.get('price') is not None and float(resolved['price']) < 0:
        resolved['price'] = 0.0
    return resolved


# --------------------------------------------------------------------------- 写


def _push_update(client: Any, account_id: str, item_id: str, resolved: Dict[str, Any]) -> Optional[str]:
    """尽力把改动推给协议层；返回警告文本或 None（协议层未就绪也算警告，不算失败）。"""
    status, value = common.call_method(
        client, ('edit_item', 'update_item', 'modify_item', 'edit'),
        account_id, item_id, resolved,
    )
    if status == common.CALL_OK:
        return None
    if status == common.CALL_MISSING:
        return '协议层尚未实现商品修改，改动仅本地生效'
    return f'协议层修改失败，改动仅本地生效: {value}'


def update_item(item_id: Any, fields: Dict[str, Any], account_id: Optional[str] = None) -> Any:
    """单条修改。给了 `account_id` 就必须 `(account_id, item_id)` 配对命中，否则 not_found。"""
    row = _find_row_for_account(account_id, item_id) if account_id else _find_row(item_id)
    if row is None:
        return common.fail(f'商品不存在: {item_id}', 'not_found')
    item = _row_to_item(row)
    patch = normalize_fields(fields)
    if common.is_failure(patch):
        common.log_operation(item['account_id'], 'update_item', item['item_id'], False, patch['message'])
        return patch
    resolved = _apply_patch(item, patch)
    if not resolved:
        return common.fail('没有可修改的字段', 'invalid_fields')

    sets = ', '.join(f'{column} = ?' for column in resolved)
    db.execute(
        f'UPDATE items SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        list(resolved.values()) + [item['id']],
    )

    warning = None
    client, _err = common.get_client(item['account_id'])
    if client is not None:
        warning = _push_update(client, item['account_id'], item['item_id'], resolved)
    else:
        warning = '协议层尚未就绪，改动仅本地生效'

    # 回读必须按 `(account_id, item_id)` 配对：同一个 item_id 可能存在于多个账号，
    # 用 get_item(item_id) 单键查会把**别的账号**的商品塞进返回值里。
    updated = get_item_by_account(item['account_id'], item['item_id'])
    common.log_operation(
        item['account_id'], 'update_item', item['item_id'], True,
        {'fields': resolved, 'warning': warning},
    )
    common.publish('items.updated', {'item_id': item['item_id'], 'account_id': item['account_id'], 'fields': resolved})
    result: Dict[str, Any] = {'item': updated, 'synced': warning is None, 'updated_fields': list(resolved)}
    if warning:
        result['warning'] = warning
    return result


def _batch_summary(
    total: int,
    synced_ok: int,
    warnings: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """批量结果的统一形状。

    - `ok` / `synced` = 完全成功（本地已改 **且** 已推送到协议层）；
    - `warnings` / `local_only` = 本地改了但没推出去；
    - `failed` = 完全没改的逐条明细，`skipped` = 同一批的 id 索引（一眼看出一条都没动）。
    """
    summary: Dict[str, Any] = {
        'total': total,
        'ok': synced_ok,
        'synced': synced_ok,
        'local_only': len(warnings),
        'warnings': warnings,
        'failed': failed,
        'skipped': [str(f.get('id') or '') for f in failed],
    }
    if extra:
        summary.update(extra)
    return summary


def batch_update(
    item_ids: Sequence[Any], fields: Dict[str, Any], account_id: Optional[str] = None
) -> Dict[str, Any]:
    """批量修改：逐条处理，一条失败不影响其它条，逐条记 operation_logs。

    给了 `account_id`（CONTRACTS §3 的 `/api/items/{account_id}/batch-edit`）时，
    每条都必须 `(account_id, item_id)` 配对命中；属于别的账号的进 `failed`，
    **不整批 400** —— MCP 的 `items_batch_edit` 与前端都靠逐条结果。
    """
    ids = [str(x) for x in (item_ids or []) if str(x).strip()]
    if not ids:
        return _batch_summary(0, 0, [], [{'id': '', 'message': 'item_ids 不能为空', 'code': 'bad_request'}])
    patch = normalize_fields(fields)
    if common.is_failure(patch):
        return _batch_summary(
            len(ids), 0, [],
            [{'id': i, 'message': patch['message'], 'code': patch.get('code', 'invalid_fields')} for i in ids],
            {'message': patch['message'], 'fields': {}},
        )

    synced_ok = 0
    warnings: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    updated_items: List[Dict[str, Any]] = []
    for item_id in ids:
        if account_id:
            row = _find_row_for_account(account_id, item_id)
            if row is None:
                same_id = _find_row(item_id)
                if same_id is None:
                    code, message = 'not_found', '商品不存在'
                else:
                    code, message = 'account_mismatch', f'商品不属于账号 {account_id}，拒绝跨账号批改'
                failed.append({'id': item_id, 'message': message, 'code': code})
                common.log_operation(str(account_id), 'batch_update', item_id, False, message)
                continue
        result = update_item(item_id, fields, account_id=account_id)
        if common.is_failure(result):
            failed.append({'id': item_id, 'message': result.get('message', '失败'),
                           'code': result.get('code', 'error')})
            # update_item 已经在失败路径记过日志；不存在的情况补一条
            if result.get('code') == 'not_found':
                common.log_operation(str(account_id or ''), 'batch_update', item_id, False, result.get('message', ''))
            continue
        updated_items.append(result['item'])
        if result.get('synced'):
            synced_ok += 1
        else:
            warnings.append({'id': item_id, 'code': 'not_pushed',
                             'message': result.get('warning') or '改动仅本地生效，未同步到闲鱼'})

    summary = _batch_summary(
        len(ids), synced_ok, warnings, failed,
        {'updated': [i['item_id'] for i in updated_items], 'fields': patch},
    )
    common.publish('items.batch_done', {'total': len(ids), 'ok': synced_ok,
                                        'local_only': len(warnings), 'failed': len(failed)})
    return summary


def _set_status(
    item_ids: Sequence[Any], status: str, action: str, account_id: Optional[str] = None
) -> Dict[str, Any]:
    """上下架：口径与 `batch_update` 一致 —— `ok` 只算「本地改了 **且** 推到平台了」。"""
    ids = [str(x) for x in (item_ids or []) if str(x).strip()]
    if not ids:
        return _batch_summary(0, 0, [], [{'id': '', 'message': 'item_ids 不能为空', 'code': 'bad_request'}],
                              {'status': status})
    synced_ok = 0
    warnings: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for item_id in ids:
        row = _find_row_for_account(account_id, item_id) if account_id else _find_row(item_id)
        if row is None:
            if account_id and _find_row(item_id) is not None:
                code, message = 'account_mismatch', f'商品不属于账号 {account_id}'
            else:
                code, message = 'not_found', '商品不存在'
            failed.append({'id': item_id, 'message': message, 'code': code})
            common.log_operation(str(account_id or ''), action, item_id, False, message)
            continue
        item = _row_to_item(row)
        db.execute(
            'UPDATE items SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (status, item['id']),
        )
        warning = None
        client, _err = common.get_client(item['account_id'])
        if client is not None:
            call_status, value = common.call_method(
                client, (action, f'{action}_item', 'set_status'), item['account_id'], item['item_id'],
            )
            if call_status == common.CALL_MISSING:
                warning = '协议层尚未实现该操作，仅本地生效'
            elif call_status == common.CALL_ERROR:
                warning = f'协议层调用失败，仅本地生效: {value}'
        else:
            warning = '协议层尚未就绪，仅本地生效'
        common.log_operation(item['account_id'], action, item['item_id'], True, {'status': status, 'warning': warning})
        if warning:
            warnings.append({'id': item['item_id'], 'code': 'not_pushed', 'message': warning})
        else:
            synced_ok += 1
    common.publish('items.status_changed', {'status': status, 'action': action,
                                            'ok': synced_ok, 'total': len(ids)})
    return _batch_summary(len(ids), synced_ok, warnings, failed, {'status': status})


def downshelf(item_ids: Sequence[Any], account_id: Optional[str] = None) -> Dict[str, Any]:
    return _set_status(item_ids, 'off_shelf', 'downshelf', account_id)


def reshelf(item_ids: Sequence[Any], account_id: Optional[str] = None) -> Dict[str, Any]:
    return _set_status(item_ids, 'on_sale', 'reshelf', account_id)


def sync_items(account_id: str) -> Any:
    """从闲鱼拉在售商品入库（upsert）。协议层未就绪 -> xianyu_not_ready。"""
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    client, err = common.get_client(account_id)
    if client is None:
        common.log_operation(account_id, 'sync_items', '', False, (err or {}).get('message', ''))
        return err or common.not_ready()

    status, value = common.call_method(
        client, ('list_items', 'fetch_items', 'get_items', 'my_items'), account_id,
    )
    problem = common.call_failed(status, value)
    if problem is not None:
        common.log_operation(account_id, 'sync_items', '', False, problem.get('message', ''))
        return problem

    raw_items = common.extract_list(value, 'items', 'list', 'data')
    ok = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = common.extract_id(raw, 'item_id', 'id')
        if not item_id:
            continue
        db.execute(
            'INSERT INTO items(account_id, item_id, title, price, original_price, stock, status, '
            'category_id, images, description, synced_at, updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) '
            'ON CONFLICT(account_id, item_id) DO UPDATE SET '
            'title = excluded.title, price = excluded.price, original_price = excluded.original_price, '
            'stock = excluded.stock, status = excluded.status, category_id = excluded.category_id, '
            'images = excluded.images, description = excluded.description, synced_at = CURRENT_TIMESTAMP',
            (
                account_id, item_id,
                str(raw.get('title') or raw.get('item_title') or ''),
                float(raw.get('price') or raw.get('soldPrice') or 0) if str(raw.get('price') or raw.get('soldPrice') or 0).replace('.', '', 1).isdigit() else 0.0,
                float(raw.get('original_price') or 0) if str(raw.get('original_price') or 0).replace('.', '', 1).isdigit() else 0.0,
                int(float(raw.get('stock') or raw.get('quantity') or 1)),
                str(raw.get('status') or 'on_sale'),
                str(raw.get('category_id') or ''),
                json.dumps(raw.get('images') or [], ensure_ascii=False) if not isinstance(raw.get('images'), str) else raw.get('images'),
                str(raw.get('description') or raw.get('desc') or ''),
            ),
        )
        ok += 1

    common.log_operation(account_id, 'sync_items', '', True, {'count': ok})
    common.publish('items.synced', {'account_id': account_id, 'count': ok})
    return {'account_id': account_id, 'count': ok, 'items': list_items(account_id=account_id)['items']}


def publish_item(account_id: str, payload: Dict[str, Any]) -> Any:
    """发布新商品：协议层是唯一真相，未就绪直接返回 xianyu_not_ready。"""
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    if not isinstance(payload, dict) or not payload:
        return common.fail('payload 不能为空', 'bad_request')
    if not str(payload.get('title') or '').strip():
        return common.fail('标题不能为空', 'invalid_fields')

    client, err = common.get_client(account_id)
    if client is None:
        common.log_operation(account_id, 'publish_item', '', False, (err or {}).get('message', ''))
        return err or common.not_ready()

    status, value = common.call_method(client, ('publish_item', 'create_item', 'publish'), account_id, payload)
    problem = common.call_failed(status, value)
    if problem is not None:
        common.log_operation(account_id, 'publish_item', '', False, problem.get('message', ''))
        return problem

    item_id = common.extract_id(value, 'item_id', 'id')
    price = payload.get('price')
    try:
        price_value = float(price) if price not in (None, '') else 0.0
    except (TypeError, ValueError):
        price_value = 0.0
    if item_id:
        db.execute(
            'INSERT INTO items(account_id, item_id, title, price, description, status, images, synced_at) '
            'VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP) '
            'ON CONFLICT(account_id, item_id) DO UPDATE SET title = excluded.title, price = excluded.price, '
            'description = excluded.description, status = excluded.status, synced_at = CURRENT_TIMESTAMP',
            (
                account_id, item_id, str(payload.get('title') or ''), price_value,
                str(payload.get('description') or ''), 'on_sale',
                json.dumps(payload.get('images') or [], ensure_ascii=False)
                if not isinstance(payload.get('images'), str) else payload.get('images'),
            ),
        )
    common.log_operation(account_id, 'publish_item', item_id, True, {'title': payload.get('title')})
    common.publish('items.published', {'account_id': account_id, 'item_id': item_id})
    return {'item_id': item_id, 'raw': value}
