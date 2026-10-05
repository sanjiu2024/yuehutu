"""订单服务：订单列表 / 单条 / 同步 / 发货 / 统计。

- 同步走协议层懒导入，未就绪返回 `xianyu_not_ready`。
- `update_shipment()` 落 `shipments` 并尽力通知协议层；同时记 operation_logs。
- 发货提醒文案交给 `cs.shipping_reminder()`（订单服务不重复写话术）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..core import db
from . import common

# 已发货/已完成这类终态（统计用）
SHIPPED_STATES = ('shipped', 'delivered', '已发货', '已签收', '已完成')
PENDING_STATES = ('pending', 'paid', 'wait_ship', '待发货', '已付款')


def _row_to_order(row: Any) -> Dict[str, Any]:
    order = dict(row)
    raw = order.get('raw')
    if isinstance(raw, str) and raw:
        try:
            order['raw'] = json.loads(raw)
        except ValueError:
            pass
    return order


def _find_order(order_id: Any) -> Optional[Any]:
    text = str(order_id)
    row = db.query_one('SELECT * FROM orders WHERE order_id = ?', (text,))
    if row is None and text.isdigit():
        row = db.query_one('SELECT * FROM orders WHERE id = ?', (int(text),))
    return row


def _latest_shipment(order_id: str) -> Optional[Dict[str, Any]]:
    row = db.query_one('SELECT * FROM shipments WHERE order_id = ? ORDER BY id DESC LIMIT 1', (order_id,))
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- 读


def list_orders(
    account_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    size: int = 50,
    keyword: Optional[str] = None,
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
        where.append('(order_id LIKE ? OR buyer_id LIKE ? OR item_id LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like, like, like])
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''

    total_row = db.query_one(f'SELECT COUNT(*) AS n FROM orders{clause}', params)
    total = int(total_row['n']) if total_row is not None else 0
    rows = db.query(
        f'SELECT * FROM orders{clause} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?',
        params + [size, (page - 1) * size],
    )
    orders = []
    for row in rows:
        order = _row_to_order(row)
        order['shipment'] = _latest_shipment(order['order_id'])
        orders.append(order)
    return {'orders': orders, 'total': total, 'page': page, 'size': size,
            'pages': (total + size - 1) // size}


def get_order(order_id: Any) -> Any:
    row = _find_order(order_id)
    if row is None:
        return common.fail(f'订单不存在: {order_id}', 'not_found')
    order = _row_to_order(row)
    order['shipment'] = _latest_shipment(order['order_id'])
    order['shipments'] = [
        dict(r) for r in db.query('SELECT * FROM shipments WHERE order_id = ? ORDER BY id DESC', (order['order_id'],))
    ]
    return order


def get_logistics(order_id: Any) -> Any:
    """物流：优先协议层实时查，拿不到就回本地 shipments 记录。"""
    row = _find_order(order_id)
    if row is None:
        return common.fail(f'订单不存在: {order_id}', 'not_found')
    order = _row_to_order(row)
    shipment = _latest_shipment(order['order_id'])

    client, _err = common.get_client(order['account_id'])
    if client is not None and shipment:
        status, value = common.call_method(
            client, ('query_logistics', 'get_logistics', 'logistics'),
            order['account_id'], order['order_id'], shipment.get('tracking_no', ''),
        )
        if status == common.CALL_OK:
            return {'order_id': order['order_id'], 'shipment': shipment, 'logistics': value, 'source': 'xianyu'}
    return {
        'order_id': order['order_id'], 'shipment': shipment,
        'logistics': [], 'source': 'local',
        'warning': None if client is not None else '协议层尚未就绪，仅本地记录',
    }


def order_stats(account_id: Optional[str] = None) -> Dict[str, Any]:
    params: Sequence[Any] = (account_id,) if account_id else ()
    clause = ' WHERE account_id = ?' if account_id else ''
    by_status = {
        r['status']: int(r['n'])
        for r in db.query(f'SELECT status, COUNT(*) AS n FROM orders{clause} GROUP BY status', params)
    }
    row = db.query_one(
        f'SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount FROM orders{clause}', params,
    )
    pending = sum(n for state, n in by_status.items() if state in PENDING_STATES)
    shipped = sum(n for state, n in by_status.items() if state in SHIPPED_STATES)
    shipment_row = db.query_one('SELECT COUNT(*) AS n FROM shipments')
    return {
        'total': int(row['n']) if row else 0,
        'amount': round(float(row['amount']), 2) if row else 0.0,
        'by_status': by_status,
        'pending': pending,
        'shipped': shipped,
        'shipments': int(shipment_row['n']) if shipment_row else 0,
    }


# --------------------------------------------------------------------------- 写


def sync_orders(account_id: str) -> Any:
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    client, err = common.get_client(account_id)
    if client is None:
        common.log_operation(account_id, 'sync_orders', '', False, (err or {}).get('message', ''))
        return err or common.not_ready()

    status, value = common.call_method(client, ('list_orders', 'fetch_orders', 'get_orders'), account_id)
    problem = common.call_failed(status, value)
    if problem is not None:
        common.log_operation(account_id, 'sync_orders', '', False, problem.get('message', ''))
        return problem

    raw_orders = common.extract_list(value, 'orders', 'list', 'data')
    ok = 0
    for raw in raw_orders:
        if not isinstance(raw, dict):
            continue
        order_id = common.extract_id(raw, 'order_id', 'id', 'bizOrderId')
        if not order_id:
            continue
        amount = raw.get('amount') or raw.get('total_fee') or raw.get('price') or 0
        try:
            amount_value = float(amount)
        except (TypeError, ValueError):
            amount_value = 0.0
        db.execute(
            'INSERT INTO orders(account_id, order_id, item_id, buyer_id, amount, status, raw, updated_at) '
            'VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP) '
            'ON CONFLICT(order_id) DO UPDATE SET item_id = excluded.item_id, buyer_id = excluded.buyer_id, '
            'amount = excluded.amount, status = excluded.status, raw = excluded.raw, '
            'updated_at = CURRENT_TIMESTAMP',
            (
                account_id, order_id, str(raw.get('item_id') or ''), str(raw.get('buyer_id') or raw.get('buyerId') or ''),
                amount_value, str(raw.get('status') or 'unknown'),
                json.dumps(raw, ensure_ascii=False, default=str),
            ),
        )
        ok += 1

    common.log_operation(account_id, 'sync_orders', '', True, {'count': ok})
    common.publish('orders.synced', {'account_id': account_id, 'count': ok})
    return {'account_id': account_id, 'count': ok, 'orders': list_orders(account_id=account_id)['orders']}


def update_shipment(order_id: Any, company: str, tracking_no: str) -> Any:
    """填/改发货信息：落 shipments + 记日志 + 尽力通知协议层。"""
    row = _find_order(order_id)
    if row is None:
        return common.fail(f'订单不存在: {order_id}', 'not_found')
    order = _row_to_order(row)
    company = (company or '').strip()
    tracking_no = (tracking_no or '').strip()
    if not company:
        return common.fail('快递公司不能为空', 'invalid_fields')
    if not tracking_no:
        return common.fail('快递单号不能为空', 'invalid_fields')

    existing = _latest_shipment(order['order_id'])
    if existing is not None and existing.get('tracking_no') == tracking_no and existing.get('company') == company:
        shipment_id = existing['id']
        db.execute('UPDATE shipments SET status = ? WHERE id = ?', ('pending', shipment_id))
    else:
        shipment_id = db.execute(
            'INSERT INTO shipments(order_id, company, tracking_no, status) VALUES(?,?,?,?)',
            (order['order_id'], company, tracking_no, 'pending'),
        )
    db.execute(
        'UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        ('shipped', order['id']),
    )

    warning = None
    client, _err = common.get_client(order['account_id'])
    if client is None:
        warning = '协议层尚未就绪，发货信息仅本地留存'
    else:
        call_status, value = common.call_method(
            client, ('ship_order', 'deliver_order', 'update_shipment', 'deliver'),
            order['account_id'], order['order_id'], company, tracking_no,
        )
        if call_status == common.CALL_MISSING:
            warning = '协议层尚未实现发货，发货信息仅本地留存'
        elif call_status == common.CALL_ERROR:
            warning = f'协议层发货失败，发货信息仅本地留存: {value}'

    from . import cs  # 本地导入避免与 cs 形成循环引用的观感
    reminder = cs.shipping_reminder(tracking_no=tracking_no, company=company)

    common.log_operation(
        order['account_id'], 'update_shipment', order['item_id'] or '', True,
        {'order_id': order['order_id'], 'company': company, 'tracking_no': tracking_no, 'warning': warning},
    )
    common.publish('orders.shipment_updated', {
        'order_id': order['order_id'], 'company': company, 'tracking_no': tracking_no,
    })
    result: Dict[str, Any] = {
        'order_id': order['order_id'], 'shipment_id': shipment_id,
        'company': company, 'tracking_no': tracking_no,
        'reminder': reminder.get('content') if isinstance(reminder, dict) else '',
    }
    if warning:
        result['warning'] = warning
    return result


def deliver(order_id: Any) -> Any:
    """标记已发货（不带物流单号时用；CONTRACTS §3 的 POST /api/orders/{id}/deliver）。"""
    row = _find_order(order_id)
    if row is None:
        return common.fail(f'订单不存在: {order_id}', 'not_found')
    order = _row_to_order(row)
    db.execute('UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('delivered', order['id']))
    common.log_operation(order['account_id'], 'deliver', order['item_id'] or '', True, {'order_id': order['order_id']})
    common.publish('orders.delivered', {'order_id': order['order_id']})
    return {'order_id': order['order_id'], 'status': 'delivered'}


def upsert_order(payload: Dict[str, Any], account_id: str = '') -> Any:
    """供编排层/协议层回调落单（幂等）。"""
    if not isinstance(payload, dict):
        return common.fail('payload 必须是对象', 'bad_request')
    order_id = common.extract_id(payload, 'order_id', 'id')
    if not order_id:
        return common.fail('order_id 不能为空', 'invalid_fields')
    account = account_id or str(payload.get('account_id') or '')
    amount = payload.get('amount') or 0
    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        amount_value = 0.0
    db.execute(
        'INSERT INTO orders(account_id, order_id, item_id, buyer_id, amount, status, raw, updated_at) '
        'VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP) '
        'ON CONFLICT(order_id) DO UPDATE SET item_id = excluded.item_id, buyer_id = excluded.buyer_id, '
        'amount = excluded.amount, status = excluded.status, raw = excluded.raw, updated_at = CURRENT_TIMESTAMP',
        (
            account, order_id, str(payload.get('item_id') or ''), str(payload.get('buyer_id') or ''),
            amount_value, str(payload.get('status') or 'unknown'),
            json.dumps(payload, ensure_ascii=False, default=str),
        ),
    )
    common.log_operation(account, 'upsert_order', str(payload.get('item_id') or ''), True, {'order_id': order_id})
    return get_order(order_id)
