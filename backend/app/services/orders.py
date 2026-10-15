"""订单服务：订单列表 / 单条 / 同步 / 发货 / 统计。

- 同步走协议层懒导入，未就绪返回 `xianyu_not_ready`。
- `update_shipment()` 落 `shipments` 并尽力通知协议层；同时记 operation_logs。
  **这是真发货**（会往平台写物流信息），所以第一道闸门是确认（`confirm=true`）。
- 发货提醒文案交给 `cs.shipping_reminder()`（订单服务不重复写话术）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from ..core import db
from . import common

# 已发货/已完成这类终态（统计用）。`交易成功` 是**平台订单状态的中文原文**
# （真机 logs/seller_discover/trade_probe.json 实测有「交易成功」「交易关闭」「未发货退款」）：
# 少了它，同步进来的订单在统计里既不算待发货也不算已发货。
SHIPPED_STATES = ('shipped', 'delivered', '已发货', '已签收', '已完成', '交易成功')
PENDING_STATES = ('pending', 'paid', 'wait_ship', '待发货', '已付款')
#: 关闭/退款这类**没成交**的终态（平台中文原文；真机 `logs/seller_discover/trade_probe.json`
#: 有「交易关闭」「未发货退款」）。`order_stats` 的 `amount` 是历史口径（含关闭单），
#: 排除关闭单看 `amount_paid` / `paid_orders` —— 别去改 `amount`（已有消费方按老口径用）。
CLOSED_STATES = ('closed', 'cancelled', 'canceled',
                 '交易关闭', '已关闭', '未发货退款', '退款成功', '已退款')

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = 'confirmation_required'


def _truthy(value: Any) -> bool:
    """只有字面量 `True` / `'true'` / `1` 才算确认，别的（含 `'false'`、`'yes'`）都不算。

    与 `services/items.py::_truthy`、`services/item_templates.py::_truthy`、
    `mcp_server/client.py::_truthy` 同一口径 —— 三条路（HTTP / 主管 AI / MCP）
    必须对「什么算点头」有完全一样的判断。
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value == 1


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰库、不碰账号、不碰平台。"""
    if _truthy(confirm):
        return None
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交到闲鱼',
        CONFIRM_CODE,
    )



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
    """订单统计。

    `amount` 是**全部订单**的金额合计（含「交易关闭」这类没成交的单，历史口径：真机
    15 单合计 1176.81，其中 5 笔关闭单占 523.70）；实收看 `amount_paid` + `paid_orders`
    （排除 `CLOSED_STATES`），关闭/退款单数看 `closed`。
    """
    params: Sequence[Any] = (account_id,) if account_id else ()
    clause = ' WHERE account_id = ?' if account_id else ''
    by_status = {
        r['status']: int(r['n'])
        for r in db.query(f'SELECT status, COUNT(*) AS n FROM orders{clause} GROUP BY status', params)
    }
    row = db.query_one(
        f'SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount FROM orders{clause}', params,
    )
    closed = tuple(CLOSED_STATES)
    marks = ','.join('?' for _ in closed)
    paid_params: Sequence[Any] = ((account_id,) if account_id else ()) + closed
    paid_row = db.query_one(
        'SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount FROM orders'
        f'{" WHERE account_id = ? AND" if account_id else " WHERE"} status NOT IN ({marks})',
        paid_params,
    )
    pending = sum(n for state, n in by_status.items() if state in PENDING_STATES)
    shipped = sum(n for state, n in by_status.items() if state in SHIPPED_STATES)
    closed_count = sum(n for state, n in by_status.items() if state in CLOSED_STATES)
    shipment_row = db.query_one('SELECT COUNT(*) AS n FROM shipments')
    return {
        'total': int(row['n']) if row else 0,
        'amount': round(float(row['amount'] or 0), 2) if row else 0.0,
        'amount_paid': round(float(paid_row['amount'] or 0), 2) if paid_row else 0.0,
        'paid_orders': int(paid_row['n']) if paid_row else 0,
        'closed': closed_count,
        'by_status': by_status,
        'pending': pending,
        'shipped': shipped,
        'shipments': int(shipment_row['n']) if shipment_row else 0,
    }


# --------------------------------------------------------------------------- 写


def _to_float(value: Any) -> Optional[float]:
    """平台金额是**字符串**（`"39.90"`）。转不出来就是「平台没给」，返回 None。"""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把一行订单翻成 `orders` 表字段，两种形状都认。

    - **协议层归一化结果**（真机路径，`xianyu/trade.py::normalize_order`）：
      `order_id` / `item_id` / `order_status` / `buyer.user_id` / `price.total_price`；
    - **扁平契约**（MCP / 假客户端直接喂的行）：`buyer_id` / `amount`|`total_fee` / `status`。

    真机走的是前一种：`normalize_order` 的输出是嵌套的，没有顶层 `buyer_id`/`amount`/`status`，
    老写法照扁平字段取，会把买家取成空串、金额取成 0。平台没给的字段留空 / None，**不拿 0 顶替**；
    金额只认显式金额字段（`amount` / `total_fee` / `price.total_price`），**不拿单价 × 件数推算**。
    """
    buyer = raw.get('buyer') if isinstance(raw.get('buyer'), dict) else {}
    price = raw.get('price') if isinstance(raw.get('price'), dict) else {}
    amount = None
    for candidate in (raw.get('amount'), raw.get('total_fee'), price.get('total_price')):
        amount = _to_float(candidate)
        if amount is not None:
            break
    # 金额缺就保持 None，**不拿「单价 × 件数」顶替**：真机上两者不是一个口径 ——
    # `logs/seller_discover/trade_probe.json` 里 orderId 3316918549013103465 是
    # `auctionPrice=199.00` / `buyNum=1` 而 `totalPrice=8.00`（优惠后实付），
    # 退路会把实付 8.00 记成 199.00，还会进 `order_stats` 的 `SUM(amount)`。
    return {
        'order_id': common.extract_id(raw, 'order_id', 'id', 'bizOrderId'),
        'item_id': str(raw.get('item_id') or raw.get('itemId') or ''),
        'buyer_id': str(raw.get('buyer_id') or raw.get('buyerId') or buyer.get('user_id') or ''),
        'amount': amount,
        'status': str(raw.get('status') or raw.get('order_status') or 'unknown'),
    }


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
        fields = _order_fields(raw)
        if not fields['order_id']:
            continue
        db.execute(
            'INSERT INTO orders(account_id, order_id, item_id, buyer_id, amount, status, raw, updated_at) '
            'VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP) '
            'ON CONFLICT(order_id) DO UPDATE SET item_id = excluded.item_id, buyer_id = excluded.buyer_id, '
            'amount = excluded.amount, status = excluded.status, raw = excluded.raw, '
            'updated_at = CURRENT_TIMESTAMP',
            (
                account_id, fields['order_id'], fields['item_id'], fields['buyer_id'],
                fields['amount'], fields['status'],
                json.dumps(raw, ensure_ascii=False, default=str),
            ),
        )
        ok += 1

    common.log_operation(account_id, 'sync_orders', '', True, {'count': ok})
    common.publish('orders.synced', {'account_id': account_id, 'count': ok})
    return {'account_id': account_id, 'count': ok, 'orders': list_orders(account_id=account_id)['orders']}


def update_shipment(order_id: Any, company: str, tracking_no: str, *,
                    confirm: Any = None) -> Any:
    """填/改发货信息：落 shipments + 记日志 + 尽力通知协议层。

    **这是真发货**：平台侧会把物流信息写进这笔订单、买家能看到，所以第一道闸门是确认
    （`confirm=true`）。三步顺序与其它写接口一致：**确认 → 字段校验 → 建客户端**。
    """
    problem = _confirm_gate('发货', confirm)
    if problem is not None:
        return problem
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
