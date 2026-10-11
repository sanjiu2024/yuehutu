"""商品服务：列表 / 单条 / 同步 / 修改 / 批量修改 / 发布 / 上下架 / 统计。

设计要点：
- **本地优先**：改价、改标题这类操作先落库，再尽力推给协议层。协议层没就绪
  时本地依然生效，只是返回 `synced: false` + warning —— 卖家在 UI 上的操作不该白做。
- **批量逐条**：`batch_update` / `downshelf` / `reshelf` 一条失败不影响其它条，且**每条**都写
  operation_logs。返回
  `{"total": n, "ok": n, "synced": n, "local_only": n, "noop": n, "warnings": [...], "failed": [...], "skipped": [...]}`：
  - `ok` / `synced` **只统计完全成功**（本地已落库 **且** 已推送到协议层）；
  - `warnings` / `local_only` 是「本地改了但没推到平台」（协议层未就绪、调用失败，或字段
    平台根本不支持），**不算 ok**。单条路径同样口径：`synced = 没有 warning **且** 没有
    `local_only` 字段 —— 只推了一半却说「已同步」就是假绿；
  - `noop` 是「本次没有实际改动」（例如只传了 `images: []` 表示不改图），既没改本地也没推
    平台，**不算 ok 也不算 local_only**；
  - `failed` / `skipped` 是「完全没改」（不存在、字段非法、跨账号）。
  协议层没就绪时 `ok` 必须是 0 —— 否则用户看到「已批量修改 N 个」而实际一条都没推出去。
- 需要协议层才能成立的操作（同步、发布）在协议层缺失时返回
  `{"success": false, "code": "xianyu_not_ready"}`。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from ..core import db
from ..xianyu import item_write as proto
from . import common, platform_errors

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
PATCH_KEYS = {'price_delta', 'price_set', 'title_tpl', 'title_prefix', 'title_suffix',
              'stock_set', 'stock_delta'}

#: 补丁里必须是数字的字段。前端「增减」输入框为空时 `Math.max(0, NaN)` 得到 `NaN`，
#: 而 `JSON.stringify(NaN)` 就是 `null` —— 不在这里拦，`_apply_patch` 会
#: `int(float(None))` 抛 TypeError，单条回 500、批量整单 500（连哪条出错都不说）。
NUMERIC_PATCH_KEYS = {'price_delta', 'price_set', 'stock_set', 'stock_delta'}

#: 其中必须是整数的（库存没有半件）。`2.5` 静默截成 `2` 也是编数字。
INTEGER_PATCH_KEYS = {'stock_set', 'stock_delta'}

#: 商品列表的状态分区（前端下拉框发的就是这三个词）。
#:
#: **为什么不能直接 `WHERE status = ?`**：前端发的是 `onsale` / `offline`，而库里的
#: 真值是 `on_sale` / `sold`（见 `xianyu/client.py::canonical_status`）—— 原样透传
#: 时「在售」「已下架」两个筛选**永远返回 0 条**（真机实测：账号 2217970177348
#: 有 45 件 on_sale 商品，`?status=onsale` 回 `total=0`）。
STATUS_BUCKETS = ('onsale', 'offline', 'sold')

#: 库里可能出现的历史写法（中文是早期手工/旧同步留下的）。只加不删。
ON_SALE_STATUSES = ('on_sale', 'onsale', '在售')
SOLD_STATUSES = ('sold', '已售出', '已卖出')

#: 允许点击表头排序的列白名单 → `(主键列, 次键列)`。**绝不把用户输入拼进 SQL**。
SORT_COLUMNS = {
    'updated': ('updated_at', 'id'),
    'price': ('price', 'id'),
    'stock': ('stock', 'id'),
    'title': ('title', 'id'),
    'status': ('status', 'id'),
    'item_id': ('item_id', 'id'),
}
DEFAULT_SORT = 'updated'
DEFAULT_ORDER = 'desc'


def _patch_number(key: str, value: Any) -> float:
    """补丁数字字段 → `float`；非法一律抛 `ValueError`（消息可直接给用户看）。"""
    number: Optional[float] = None
    if isinstance(value, bool) or value is None:
        number = None
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            number = None
    if number is None:
        raise ValueError(f'字段 {key} 需要数字，收到: {value!r}')
    if not math.isfinite(number):
        raise ValueError(f'字段 {key} 需要有限数字，收到: {value!r}')
    if key in INTEGER_PATCH_KEYS and number != int(number):
        raise ValueError(f'字段 {key} 需要整数，收到: {value!r}')
    return number


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


def _in_clause(column: str, values: Sequence[str]) -> Tuple[str, List[Any]]:
    """`column IN (?, ?, …)` + 参数。占位符按值个数生成，**值本身只走参数**。"""
    placeholders = ', '.join('?' for _ in values)
    return f'COALESCE({column}, \'\') IN ({placeholders})', list(values)


def _status_clause(status: str) -> Tuple[str, List[Any]]:
    """前端的状态分区 → SQL 条件（返回值永远只含 `?` 占位符，可直接拼进 WHERE）。

    - `onsale`：平台口径在售 **且库存不为 0**。库存未知（`NULL` / 负数哨兵）算在售 ——
      拿不到库存不等于卖完了，把它塞进「已下架」会让用户以为商品被下架了。
    - `sold`：平台口径已卖出（旧数据里的中文写法一起认）。
    - `offline`：**非在售的全集** —— 平台判下架的 + 售罄（库存 0）的 + 已卖出的。
      用户的原话是「售罄的商品应该在下架分区里面，那样好编辑」：一件卖完的商品挂在
      「在售」里既卖不出也点不动，混在在售里只会让人以为还有货。
    - 其它值：按历史行为精确匹配 `status`（`?status=on_sale` 这类调用不能被破坏）。
    """
    if status == 'onsale':
        clause, params = _in_clause('status', ON_SALE_STATUSES)
        return f'({clause} AND (stock IS NULL OR stock <> 0))', params
    if status == 'sold':
        return _in_clause('status', SOLD_STATUSES)
    if status == 'offline':
        clause, params = _in_clause('status', ON_SALE_STATUSES)
        return f'(NOT {clause} OR stock = 0)', params
    return 'status = ?', [status]


def _order_clause(sort: Optional[str], order: Optional[str]) -> str:
    """排序白名单 → ORDER BY。认不出的 `sort` 落回默认（更新时间倒序）。"""
    key = str(sort or DEFAULT_SORT).strip().lower()
    if key not in SORT_COLUMNS:
        key = DEFAULT_SORT
    ascending = str(order or DEFAULT_ORDER).strip().lower() in ('asc', 'up', '1')
    direction = 'ASC' if ascending else 'DESC'
    primary, secondary = SORT_COLUMNS[key]
    if key == 'stock':
        # 库存未知（NULL / 负数哨兵）**永远沉底**：点「库存」想看的是有货没货，
        # 不管升序降序都不该先翻一堆「-」。
        return f'ORDER BY (stock IS NULL OR stock < 0) ASC, stock {direction}, {secondary} DESC'
    return f'ORDER BY {primary} {direction}, {secondary} DESC'


def _status_counts(clause: str, params: Sequence[Any]) -> Dict[str, int]:
    """同一筛选范围（账号 + 关键词）里三个分区各多少件。

    下拉框直接显示数字，用户点之前就知道点完还剩几条 —— 「在售 42 / 已下架 53 / 已售出 39」
    比点进去发现是空的强。这里的 `clause` 只含账号与关键词，不含状态本身。
    """
    onsale_sql, onsale_params = _status_clause('onsale')
    offline_sql, offline_params = _status_clause('offline')
    sold_sql, sold_params = _status_clause('sold')
    row = db.query_one(
        'SELECT COUNT(*) AS all_count,'
        f' COALESCE(SUM(CASE WHEN {onsale_sql} THEN 1 ELSE 0 END), 0) AS onsale,'
        f' COALESCE(SUM(CASE WHEN {offline_sql} THEN 1 ELSE 0 END), 0) AS offline,'
        f' COALESCE(SUM(CASE WHEN {sold_sql} THEN 1 ELSE 0 END), 0) AS sold'
        f' FROM items{clause}',
        # ⚠️ 参数顺序必须与 SQL 里 `?` 出现的顺序一致：三个 CASE WHEN 在 SELECT 里，
        # **排在 WHERE 之前** —— 把 `params` 放前面会让账号名绑到第一个状态占位符上，
        # 结果永远是「全 0」（这条踩过一次，测试当场抓到）。
        onsale_params + offline_params + sold_params + list(params),
    )
    if row is None:
        return {'all': 0, 'onsale': 0, 'offline': 0, 'sold': 0}
    return {
        # `all` 是 SQL 关键字，别名只能叫 all_count，对外仍旧叫 all。
        'all': int(row['all_count'] or 0),
        'onsale': int(row['onsale'] or 0),
        'offline': int(row['offline'] or 0),
        'sold': int(row['sold'] or 0),
    }


def list_items(
    account_id: Optional[str] = None,
    keyword: Optional[str] = None,
    page: int = 1,
    size: int = 50,
    status: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
) -> Dict[str, Any]:
    """商品列表。

    `status` 认三个分区名（`onsale` / `offline` / `sold`，见 `_status_clause`），
    其它值按精确匹配穿透；`sort` / `order` 走白名单（见 `_order_clause`）。
    返回 `counts` 给前端下拉框显示各分区条数（同一账号 + 关键词范围，不含状态本身）。
    """
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 50), 500))
    where: List[str] = []
    params: List[Any] = []
    if account_id:
        where.append('account_id = ?')
        params.append(account_id)
    if keyword:
        where.append('(title LIKE ? OR item_id LIKE ? OR description LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like, like, like])
    # 分区之外的筛选（账号 / 关键词）单独留一份，用来算 counts —— 带上状态本身
    # 去数就会永远只数出当前分区那几条。
    scope_clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    counts = _status_counts(scope_clause, params)
    if status:
        clause, status_params = _status_clause(status)
        where.append(clause)
        params.extend(status_params)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''

    total_row = db.query_one(f'SELECT COUNT(*) AS n FROM items{clause}', params)
    total = int(total_row['n']) if total_row is not None else 0
    rows = db.query(
        f'SELECT * FROM items{clause} {_order_clause(sort, order)} LIMIT ? OFFSET ?',
        params + [size, (page - 1) * size],
    )
    return {
        'items': [_row_to_item(r) for r in rows],
        'total': total,
        'page': page,
        'size': size,
        'pages': (total + size - 1) // size,
        'counts': counts,
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


def delete_item(account_id: str, item_id: Any) -> Any:
    """**只删本地记录**：闲鱼上架着的商品不会被动。

    平台侧**有**删除接口（`...pc.item.batch.delete`，见本模块的 `batch_delete`），
    这一条故意保持「只动本地」的语义：用途是清掉本地残留（平台已下架/已删除、
    或同步前手动加的记录）。下次「同步商品」如果平台还返回它，会被重新写回 ——
    前端文案已经这么告诉用户了，不要在这里假装「已从闲鱼删除」。会话/消息/订单都不删：
    它们是聊天与交易历史，删了就查不到「这个买家当初问的是哪件商品」。
    """
    row = _find_row_for_account(account_id, item_id)
    if row is None:
        return common.fail(f'商品不存在: {account_id}/{item_id}', 'not_found')
    with db.tx() as conn:
        conn.execute('DELETE FROM items WHERE id = ?', (row['id'],))
    deleted_id = str(row['item_id'])
    _publish_updated(account_id, [deleted_id], 'delete')
    common.log_operation(account_id, 'delete_item', deleted_id, True, '已删除本地记录')
    return {'deleted': True, 'account_id': str(account_id), 'item_id': deleted_id}


def item_stats() -> Dict[str, Any]:
    by_status = {
        r['status']: int(r['n'])
        for r in db.query('SELECT status, COUNT(*) AS n FROM items GROUP BY status')
    }
    total_row = db.query_one(
        'SELECT COUNT(*) AS n, '
        'COALESCE(SUM(CASE WHEN stock >= 0 THEN price * stock ELSE 0 END), 0) AS v FROM items'
    )
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
            name = str(key)
            if name in PATCH_KEYS:
                if name in NUMERIC_PATCH_KEYS:
                    try:
                        patch[name] = _patch_number(name, value)
                    except ValueError as exc:
                        return common.fail(str(exc), 'invalid_fields')
                else:
                    patch[name] = value
                continue
            unknown.append(name)
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


UNKNOWN_STOCK_DELTA_MESSAGE = (
    '该商品库存未知（平台列表没有返回库存），不能用「增加/减少」；'
    '请改用「设为固定值」，或先同步商品把库存取回来'
)


def _stock_delta_blocked(item: Dict[str, Any], patch: Dict[str, Any]) -> Optional[str]:
    """`stock_delta` 只对已知库存成立。

    未知库存库里存 `-1`（列表卡确实没有库存字段），拿它去加减会算出 `-1 + 5 = 4`
    这种**看起来很正常其实是编的**数字，然后被推给平台当真库存 —— 比报错更坏。
    所以未知库存时直接拒绝这一条（`code='unknown_stock'`），前端原样展示原因；
    `stock_set`（用户明确指定固定值）不受影响，那是把未知覆盖成已知。
    """
    if 'stock_delta' not in patch:
        return None
    try:
        current = int(item.get('stock'))
    except (TypeError, ValueError):
        current = -1
    return UNKNOWN_STOCK_DELTA_MESSAGE if current < 0 else None


def _apply_patch(item: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """在单条商品上应用补丁（含 price_delta / title_tpl 这类计算字段）。"""
    resolved: Dict[str, Any] = {}
    for key, value in patch.items():
        if key == 'price_delta':
            resolved['price'] = round(float(item.get('price') or 0) + float(value), 2)
        elif key == 'price_set':
            resolved['price'] = round(float(value), 2)
        elif key == 'stock_set':
            # 固定值也钳到 0：负数库存平台不接受，钳掉比报错更符合用户预期。
            resolved['stock'] = max(0, int(float(value)))
        elif key == 'stock_delta':
            # 增减只对**已知**库存有意义；未知库存在 `_stock_delta_blocked` 里已被拦下，
            # 这里再兜一次 `or 0`（防止有人绕过 update_item 直接调本函数）。
            resolved['stock'] = max(0, int(item.get('stock') or 0) + int(float(value)))
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


#: 协议层商品编辑**真正能改**的字段（``XianyuClient.edit_item`` 的业务字段名）。
#: 交集之外的字段（``ai_enabled`` / ``category_id``）本地照改，但必须如实报
#: ``local_only`` —— 说「已同步」而平台根本没这个字段就是假通。
#: ``images`` 自 task-25 起协议层已支持（``images`` → ``imageInfoDOList``），
#: 所以它在白名单里；它曾因为漏在这一行而被服务层静默过滤掉，端到端改不了图。
PUSHABLE_EDIT_FIELDS = {'price', 'original_price', 'stock', 'title', 'description',
                        'status', 'free_shipping', 'images'}

#: 图片条目里 URL 的候选键。前端上传接口返回 ``{url,width,height}``，历史图是纯
#: URL 字符串（混合数组），协议层 ``_image_entry`` 两种都认。
IMAGE_URL_KEYS = ('url', 'picUrl', 'imageUrl', 'fileUrl')

#: 已卖出商品被编辑时给用户的说明（见 `update_item` 的保守闸门）。
SOLD_WARNING = '该商品已卖出，平台不接受编辑，改动仅本地生效'

#: 平台对商品编辑的**已知**拒绝原因 → 用户能看懂的解释。
#: 真机实例（账号 2221063792739 的商品 1087852912806）：把库存改成 100 时平台回
#: `FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH::亲，您不能发布多库存宝贝哦`
#: —— 原样抛给用户等于没解释，他只看到「改动仅本地生效」。
PLATFORM_EDIT_HINTS: Tuple[Tuple[str, str], ...] = (
    (
        'FAIL_BIZ_ITEM_ERROR_MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH',
        '（这件商品在闲鱼上是按「单件库存」发布的，平台不允许把它改成多件库存：本地库存已经改好了，'
        '但闲鱼上不会变。想在闲鱼上卖多件，需要重新发布一件支持多库存的商品。）',
    ),
)

#: 商品写操作成功后统一发的事件。前端各页订阅同一个 topic 做静默重拉，
#: 所以 payload 里必须带够定位信息（哪个账号、哪些商品、干了什么）。
ITEMS_UPDATED_TOPIC = 'items.updated'


def _publish_updated(account_id: Any, item_ids: Sequence[Any], action: str) -> None:
    """发 ``items.updated``。**事件失败绝不影响主流程**（写操作已经成功落库，
    不能因为一个通知把成功改判成失败）。"""
    try:
        common.publish(ITEMS_UPDATED_TOPIC, {
            'account_id': str(account_id or ''),
            'item_ids': [str(i) for i in (item_ids or []) if str(i).strip()],
            'action': action,
        })
    except Exception:  # noqa: BLE001 - 通知失败与业务结果无关
        logger.exception('发布 items.updated 事件失败（不影响本次操作结果）')


def _image_entries(value: Any) -> List[Any]:
    """``resolved['images']`` → 图片条目列表（URL 字符串 或 ``{url,width,height}``）。

    ``normalize_fields`` 把 ``images`` 归到 ``JSON_FIELDS``，列表会被 ``json.dumps``
    成字符串，所以这里必须能解回列表；纯 URL 字符串当单张图。
    **返回空列表表示「本次不改图」**（见 ``_split_edit_fields``）。
    """
    if value is None or value == '':
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0] in '[{':
            try:
                parsed = json.loads(text)
            except ValueError:
                return [text]
            return list(parsed) if isinstance(parsed, list) else [parsed]
        return [text]
    if isinstance(value, (list, tuple)):
        return [item for item in value if item not in (None, '')]
    return [value]


def _image_url(entry: Any) -> str:
    """一张图条目 → URL 字符串（对象条目按 ``IMAGE_URL_KEYS`` 找）。"""
    if isinstance(entry, dict):
        for key in IMAGE_URL_KEYS:
            url = entry.get(key)
            if url:
                return str(url).strip()
        return ''
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0] or '').strip()
    return str(entry or '').strip()


def _images_for_db(value: Any) -> Optional[str]:
    """写库口径：``items.images`` **只存 URL 字符串数组**（保序、去重）→ JSON 字符串。

    ``{url,width,height}`` 对象进库会让前端 ``parseImages`` 渲染不出缩略图（它只认
    字符串），所以尺寸在写库这一路丢掉 —— 尺寸唯一的用途是推协议层。
    空（或全是空 URL）→ ``None``：调用方据此**不写库**，绝不把已有图抹成空数组。
    """
    urls: List[str] = []
    for entry in _image_entries(value):
        url = _image_url(entry)
        if url and url not in urls:
            urls.append(url)
    if not urls:
        return None
    return json.dumps(urls, ensure_ascii=False)


def _local_only_note(local_only: Sequence[str]) -> str:
    """可读的「仅本地生效」说明（前端会把这句整条展示给用户）。"""
    return f'以下字段仅本地生效，未推送到闲鱼：{", ".join(sorted(local_only))}'


def _split_edit_fields(resolved: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """``resolved`` → ``(推给协议层的 payload, 仅本地生效的字段名列表)``。

    推协议层用**完整条目**（含 ``width``/``height``，协议层拿它填
    ``imageInfoDOList`` 的真实尺寸），写库走 ``_images_for_db`` 的纯 URL ——
    两处口径分别算，**不共用一份**。
    ``images: []`` / ``images: ''`` 是「本次不改图」：既不推、也不算 ``local_only``、
    更不写库（协议层对空数组会抛 ``XianyuError``，服务层先把它剔除）。
    """
    payload: Dict[str, Any] = {}
    local_only: List[str] = []
    for key, value in resolved.items():
        if key not in PUSHABLE_EDIT_FIELDS:
            local_only.append(key)
            continue
        if key == 'images':
            entries = _image_entries(value)
            if not entries:
                continue
            payload[key] = entries
            continue
        payload[key] = value
    return payload, local_only


def _platform_error_hint(value: Any) -> str:
    """把平台对商品编辑的已知拒绝原因翻译成用户能看懂的一句话（认不出就返回空串）。

    **只收录真机/日志里真实出现过的错误码**：没见过的原样透传，不编解释。

    匹配的是**错误码这个词本身**，不做子串包含 —— 平台的错误码在日志/异常文案里
    前面还挂着 `MTOP 调用失败 [api]: ` 这类前缀（分隔符是 `: ` 不是 `::`），所以
    不能只按 `::` 切；但用 `code in text` 又会让将来出现的**同前缀新码**
    （`..._CAN_NOT_PUBLISH_V2`）命中旧解释，等于给用户讲了一个错的理由。
    词边界判定由 `platform_errors.match_code` 统一实现（发布那条路用的是同一份）。
    """
    text = str(value)
    for code, hint in PLATFORM_EDIT_HINTS:
        if platform_errors.match_code(text, code):
            return hint
    # 编辑路径上「风控 / 登录态过期 / 接口下线」这类通用原因交给共用表，
    # 免得用户在编辑弹窗里只看到一句裸错误码。共用表的文案不带括号，
    # 而调用方是直接把它拼在平台原文后面的（`{value}{hint}`），所以这里补一对括号。
    hint = platform_errors.explain(text)
    return f'（{hint}）' if hint else ''


def _push_update(client: Any, account_id: str, item_id: str, payload: Dict[str, Any]) -> Optional[str]:
    """尽力把 ``payload`` 推给协议层；返回警告文本或 None（协议层未就绪也算警告，不算失败）。

    真签名是 ``XianyuClient.edit_item(item_id, payload)``（**两个参数**，没有
    ``account_id``）；多传一个 account_id 会在 Python 层就 ``TypeError``，
    表现为 502 ``protocol_error``，跟平台无关。

    ``payload`` 为空 = 本次没有任何可推的字段（例如 ``images: []``），返回 None。
    """
    if not payload:
        return None
    status, value = common.call_method(client, ('edit_item',), item_id, payload)
    if status == common.CALL_OK:
        return None
    if status == common.CALL_MISSING:
        return '协议层尚未实现商品修改，改动仅本地生效'
    return f'协议层修改失败，改动仅本地生效: {value}{_platform_error_hint(value)}'


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
    blocked = _stock_delta_blocked(item, patch)
    if blocked:
        # 未知库存走「增减」= 会算出假数字，宁可这条不生效并如实报原因。
        common.log_operation(item['account_id'], 'update_item', item['item_id'], False, blocked)
        return common.fail(blocked, 'unknown_stock')
    resolved = _apply_patch(item, patch)
    if not resolved:
        return common.fail('没有可修改的字段', 'invalid_fields')

    payload, local_only = _split_edit_fields(resolved)

    # 已卖出商品（`items.status == 'sold'`）的**保守闸门**：不推平台，只落本地。
    # 平台会接受已卖出商品的 edit 提交并把它重新上架（真机实测），所以这里必须拦在
    # 协议层之前：payload 清空 + 这些字段转记进 local_only，让 `synced` 变成 False、
    # warning 明说「已卖出…改动仅本地生效」。
    sold_not_pushed = str(item.get('status') or '').strip().lower() == 'sold' and bool(payload)
    if sold_not_pushed:
        local_only = sorted(set(local_only) | set(payload))
        payload = {}

    # 写库与推协议层**两套取值**：库里 images 只存 URL 字符串（前端 parseImages
    # 只认字符串），推协议层带完整条目（含 width/height）。空图片两边都跳过。
    db_values: Dict[str, Any] = {}
    for key, value in resolved.items():
        if key == 'images':
            stored = _images_for_db(value)
            if stored is None:
                continue
            db_values[key] = stored
        else:
            db_values[key] = value

    if not db_values and not local_only:
        # 只有 `images: []` 这类「不改图」输入：不动库、不推平台、也不算改动。
        # 不报 invalid_fields —— 前端整表单提交时这是完全合法的请求。
        common.log_operation(item['account_id'], 'update_item', item['item_id'], True,
                             {'fields': {}, 'noop': True})
        return {'item': item, 'synced': True, 'updated_fields': [], 'noop': True}

    sets = ', '.join(f'{column} = ?' for column in db_values)
    db.execute(
        f'UPDATE items SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        list(db_values.values()) + [item['id']],
    )

    warning = None
    client, _err = common.get_client(item['account_id'])
    if client is not None:
        warning = _push_update(client, item['account_id'], item['item_id'], payload)
    elif payload:
        warning = '协议层尚未就绪，改动仅本地生效'
    if sold_not_pushed:
        # 已卖出商品的编辑**绝不能推平台**：真机 1087247101297 提交后回帧带
        # `publishScene="pcMainPublish"`，随后同步它的 status 就从 sold 变回 on_sale
        # —— 相当于把卖掉的商品重新上架了。保守起见只落本地，由用户自己决定。
        warning = f'{warning}；{SOLD_WARNING}' if warning else SOLD_WARNING
    if local_only:
        # 「假绿」修复：以前只看 warning，改标题+改 ai_enabled 会报 synced=True，
        # 用户看到「已修改并同步」而一半只落本地。
        note = _local_only_note(local_only)
        warning = f'{warning}；{note}' if warning else note

    # 回读必须按 `(account_id, item_id)` 配对：同一个 item_id 可能存在于多个账号，
    # 用 get_item(item_id) 单键查会把**别的账号**的商品塞进返回值里。
    updated = get_item_by_account(item['account_id'], item['item_id'])
    common.log_operation(
        item['account_id'], 'update_item', item['item_id'], True,
        {'fields': db_values, 'warning': warning, 'local_only': local_only},
    )
    _publish_updated(item['account_id'], [item['item_id']], 'edit')
    result: Dict[str, Any] = {'item': updated, 'synced': warning is None and not local_only,
                              'updated_fields': list(db_values)}
    if warning:
        result['warning'] = warning
    if local_only:
        result['local_only_fields'] = local_only
    return result


def _batch_summary(
    total: int,
    synced_ok: int,
    warnings: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    extra: Optional[Dict[str, Any]] = None,
    noop: int = 0,
) -> Dict[str, Any]:
    """批量结果的统一形状。

    - `ok` / `synced` = 完全成功（本地已改 **且** 已推送到协议层）；
    - `warnings` / `local_only` = 本地改了但没推出去（协议层未就绪、调用失败，或字段
      平台根本不支持）；
    - `noop` = 本次没有实际改动（例如只传 `images: []` 表示不改图），**不算 ok、也不算
      local_only**；
    - `failed` = 完全没改的逐条明细，`skipped` = 同一批的 id 索引（一眼看出一条都没动）。

    恒等式：`total == ok + local_only + noop + len(failed)`。
    """
    summary: Dict[str, Any] = {
        'total': total,
        'ok': synced_ok,
        'synced': synced_ok,
        'local_only': len(warnings),
        'warnings': warnings,
        'noop': int(noop),
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
    noop = 0
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
        if result.get('noop'):
            # 本次没有实际改动（例如 `images: []`）：不算 ok、不算 local_only，
            # 也不进 `updated` 列表 —— 它确实没被改。
            noop += 1
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
        noop=noop,
    )
    if updated_items:
        _publish_updated(account_id or updated_items[0].get('account_id'),
                         [i['item_id'] for i in updated_items], 'batch_edit')
    common.publish('items.batch_done', {'total': len(ids), 'ok': synced_ok,
                                        'local_only': len(warnings), 'noop': noop,
                                        'failed': len(failed)})
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
    touched: List[str] = []
    touched_account = str(account_id or '')
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
            # 真签名：downshelf(item_id) / reshelf(item_id)（**一个参数**，没有
            # account_id）；上架其实是编辑流程改 itemStatus，没有独立接口。
            action_method = 'reshelf' if action == 'reshelf' else 'downshelf'
            call_status, value = common.call_method(
                client, (action_method,), item['item_id'],
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
        touched.append(item['item_id'])
        touched_account = item['account_id']
    if touched:
        _publish_updated(account_id or touched_account, touched, 'status')
    common.publish('items.status_changed', {'status': status, 'action': action,
                                            'ok': synced_ok, 'total': len(ids)})
    return _batch_summary(len(ids), synced_ok, warnings, failed, {'status': status})


def downshelf(item_ids: Sequence[Any], account_id: Optional[str] = None) -> Dict[str, Any]:
    return _set_status(item_ids, 'off_shelf', 'downshelf', account_id)


def reshelf(item_ids: Sequence[Any], account_id: Optional[str] = None) -> Dict[str, Any]:
    return _set_status(item_ids, 'on_sale', 'reshelf', account_id)


# --------------------------------------------------------------------------- 商品写接口
#
# 上面 `downshelf` / `reshelf` 走的是**老族**（`mtop.taobao.idle.item.downshelf` v2.0，
# 上架干脆没有独立接口、靠整份原表单回提）。官方卖家工作台「商品管理」页用的是
# `mtop.alibaba.idle.seller.pc.item.*` 这一族，能力更全，见 `xianyu/item_write.py` 的
# 模块 docstring。这里补三个：
#
# - `batch_offline`：一次请求下架一批（老路子是 N 次 `downshelf`，N 次风控面）
# - `batch_delete`：**平台侧真删除**（老 `delete_item` 只删本地记录）
# - `quick_edit`：直接改价 / 改库存，**不整份回提表单** ⇒ 对已卖出商品也没有
#   「被重新上架」的副作用
#
# 三个都是写操作，三步顺序统一为 **确认 → 字段校验 → 建客户端**（与
# `services/item_templates.py` 同口径）：确认在最前，否则会误导用户「填对了就能发」；
# 字段校验在建客户端之前，因为纯本地、免费，账号掉线时也该先说「item_ids 不能为空」
# 而不是「去重新登录」。

#: 未确认时返回的业务码（`common.FAIL_STATUS` 里没有它 ⇒ 默认 400）。
CONFIRM_CODE = 'confirmation_required'

#: 单次批量写请求的 id 上限。官方页面一次选几十个没问题，但请求体是逗号串，
#: 给个上限挡住「把整个店铺塞进一次请求」这种误用（真实账号 45 件远在限内）。
BATCH_WRITE_LIMIT = 200


def _truthy(value: Any) -> bool:
    """只有字面量 `True` / `'true'` / `1` 才算确认，别的（含 `'false'`、`'yes'`）都不算。

    与 `services/item_templates.py::_truthy`、`mcp_server/client.py::_truthy` 同一口径 ——
    三条路（HTTP / 主管 AI / MCP）必须对「什么算点头」有完全一样的判断。
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() == 'true'
    return value == 1


def _confirm_gate(action: str, confirm: Any) -> Optional[Dict[str, Any]]:
    """写路径第一道闸门：**只判确认**，不碰账号、不碰平台。"""
    if _truthy(confirm):
        return None
    return common.fail(
        f'{action} 是写操作，需要显式确认（confirm=true）后才会提交到闲鱼',
        CONFIRM_CODE,
    )


def _clean_ids(item_ids: Any) -> List[str]:
    """两种入参都收：UI 给数组、MCP 给逗号串（`proto.normalize_item_ids` 统一）。"""
    return proto.item_id_list(item_ids)


def _write_call_failure(call_status: str, value: Any, api: str) -> Dict[str, Any]:
    """协议层没调通（方法缺失 / 抛异常 / 登录态过期）时的失败信封。"""
    failure = common.call_failed(call_status, value) or common.fail('协议层调用失败', 'protocol_error')
    failure.setdefault('platform_errors', {
        'api': api,
        'explain': str(value) or '协议层未提供该方法',
    })
    return failure


def _platform_failure(value: Any, api: str) -> Dict[str, Any]:
    """平台回了信封但业务体是空的 ⇒ 502，把平台原文透出去。

    **不判登录态**：无权限 / 参数不对都不等于掉线，别让用户白扫一次码。
    """
    reason = proto.failure_reason(value) or '平台没有返回成功'
    logger.warning(f'[items] 平台拒绝 {api}: {reason}')
    return common.fail(
        f'平台接口失败: {reason}',
        'protocol_error',
        platform_errors={
            'api': api,
            'code': proto.code_of(value),
            'message': proto.message_of(value),
        },
    )


def _batch_write(
    account_id: str,
    item_ids: Any,
    *,
    action: str,
    api_action: str,
    confirm: Any,
    status: str,
    delete_local: bool = False,
) -> Dict[str, Any]:
    """批量下架 / 批量删除的公共实现（差别只有本地怎么落库）。

    `ok` 的口径跟着**平台**走（平台逐条回报成功才算 ok）—— 这三个接口的权威在平台，
    本地只是镜像。平台没回报的 id 一律算失败，**绝不默认成功**。
    """
    problem = _confirm_gate(action, confirm)
    if problem is not None:
        return problem

    ids = _clean_ids(item_ids)
    if not ids:
        return common.fail('item_ids 不能为空', 'invalid_fields')
    if len(ids) > BATCH_WRITE_LIMIT:
        return common.fail(f'一次最多处理 {BATCH_WRITE_LIMIT} 个商品（收到 {len(ids)} 个）',
                           'invalid_fields')

    client, failure = common.get_client(account_id)
    if client is None:
        return failure

    api, data = proto.request_of(api_action, item_ids=ids)
    call_status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if call_status != common.CALL_OK:
        return _write_call_failure(call_status, value, api)
    if not proto.is_success(value):
        return _platform_failure(value, api)

    results = proto.process_results(value)
    warnings: List[Dict[str, Any]] = []
    if results:
        ok_ids = [str(r.get('itemId') or '') for r in results if r.get('success')]
        ok_ids = [x for x in ok_ids if x]
        failed = proto.process_failures(value)
        reported = {str(f.get('id') or '') for f in failed} | set(ok_ids)
        for item_id in ids:
            if item_id not in reported:
                failed.append({
                    'id': item_id,
                    'code': 'no_per_item_report',
                    'message': '平台没有回报这条商品的结果',
                })
    else:
        # 平台接受了整批但没给逐条清单：整体按成功处理，同时在返回里如实标注
        # `per_item_report: False` —— 不假装「逐条都确认过了」。
        ok_ids = list(ids)
        failed = []

    for item_id in ok_ids:
        row = _find_row_for_account(account_id, item_id)
        if row is None:
            warnings.append({
                'id': item_id,
                'code': 'local_missing',
                'message': '平台已处理，但本地没有这条商品记录',
            })
            continue
        item = _row_to_item(row)
        if delete_local:
            db.execute('DELETE FROM items WHERE id = ?', (item['id'],))
        else:
            db.execute(
                'UPDATE items SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (status, item['id']),
            )
        common.log_operation(account_id, action, item['item_id'], True,
                             {'status': status, 'platform': True})

    if ok_ids:
        _publish_updated(account_id, ok_ids, 'status')
    common.publish('items.status_changed', {
        'status': status, 'action': action, 'ok': len(ok_ids), 'total': len(ids),
    })

    summary = _batch_summary(len(ids), len(ok_ids), warnings, failed, {'status': status})
    summary['per_item_report'] = bool(results)
    fail_count = proto.failure_count(value)
    if fail_count is not None:
        summary['platform_fail_count'] = fail_count
    return summary


def batch_offline(account_id: str, item_ids: Any, confirm: Any = None) -> Dict[str, Any]:
    """批量下架：一次请求停售一批（`mtop.alibaba.idle.seller.pc.item.batch.offline`）。

    比逐条 `downshelf` 少 N-1 次平台请求 —— 真机风控对连续请求很敏感，这是实打实的收益。
    """
    return _batch_write(account_id, item_ids, action='batch_offline',
                        api_action='batch_offline', confirm=confirm, status='off_shelf')


def batch_delete(account_id: str, item_ids: Any, confirm: Any = None) -> Dict[str, Any]:
    """批量删除：**平台侧真删除**（`...pc.item.batch.delete`），成功后再删本地记录。

    与老 `delete_item` 的区别：那个只删本地（它的语义就是「清本地残留」，现在也仍然是），
    这个真的会把商品从闲鱼上删掉，**不可撤销**。
    """
    return _batch_write(account_id, item_ids, action='batch_delete',
                        api_action='batch_delete', confirm=confirm, status='deleted',
                        delete_local=True)


def quick_edit(
    account_id: str,
    item_id: Any,
    price: Any = None,
    quantity: Any = None,
    confirm: Any = None,
) -> Dict[str, Any]:
    """快捷改价 / 改库存（`...pc.item.info.update`），**不整份回提表单**。

    与 `update_item` 的关键差别：`update_item` 是把原表单整份回提（`edit_item`），
    对**已卖出**商品会把 `itemStatus` 一起写回去 ⇒ 顺手把它重新上架（这是真机踩过的
    缺陷）。这个接口只发 `{itemId, price, quantity}`，没有 itemStatus 字段，所以没有
    那个副作用。

    价格 / 库存至少要给一个；给了就校验（价格 > 0、库存 ≥ 0 的整数）。
    """
    action = 'quick_edit'
    problem = _confirm_gate(action, confirm)
    if problem is not None:
        return problem

    clean_id = str(item_id or '').strip()
    if not clean_id:
        return common.fail('商品 ID 不能为空', 'invalid_fields')

    has_price = price is not None and price != ''
    has_quantity = quantity is not None and quantity != ''
    if not has_price and not has_quantity:
        return common.fail('价格和库存至少要填一个', 'invalid_fields')

    price_value: Optional[float] = None
    if has_price:
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            return common.fail('价格必须是数字', 'invalid_fields')
        if price_value <= 0:
            return common.fail('价格必须大于 0', 'invalid_fields')

    quantity_value: Optional[int] = None
    if has_quantity:
        try:
            quantity_value = int(str(quantity).strip())
        except (TypeError, ValueError):
            return common.fail('库存必须是整数', 'invalid_fields')
        if quantity_value < 0:
            return common.fail('库存不能为负', 'invalid_fields')

    client, failure = common.get_client(account_id)
    if client is None:
        return failure

    api, data = proto.request_of('quick_update', item_id=clean_id,
                                 price=price_value, quantity=quantity_value)
    call_status, value = common.call_method(client, ('mtop',), api, data, v=proto.DEFAULT_VERSION)
    if call_status != common.CALL_OK:
        return _write_call_failure(call_status, value, api)
    if not proto.is_success(value):
        return _platform_failure(value, api)

    # 平台回读优先（它可能把价格四舍五入），拿不到就用我们发出去的值 —— 不造 0。
    echoed = proto.updated_values(value)
    applied_price = echoed.get('price', price_value)
    applied_quantity = echoed.get('quantity', echoed.get('stock', quantity_value))

    warning: Optional[str] = None
    row = _find_row_for_account(account_id, clean_id)
    if row is None:
        warning = '平台已改，但本地没有这条商品记录'
    else:
        item = _row_to_item(row)
        sets: List[str] = []
        params: List[Any] = []
        if price_value is not None:
            sets.append('price = ?')
            params.append(price_value)
        if quantity_value is not None:
            sets.append('stock = ?')
            params.append(quantity_value)
        sets.append('updated_at = CURRENT_TIMESTAMP')
        params.append(item['id'])
        db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", tuple(params))
        _publish_updated(account_id, [item['item_id']], 'edit')

    common.log_operation(account_id, action, clean_id, True,
                         {'price': price_value, 'quantity': quantity_value,
                          'platform': True, 'warning': warning})
    common.publish('items.updated', {'account_id': account_id, 'item_ids': [clean_id],
                                     'action': action})

    return {
        'item_id': clean_id,
        'price': applied_price,
        'quantity': applied_quantity,
        'synced': True,
        'warning': warning,
    }


#: 逐件补详情（拿真库存）的限速与上限。真机 0.6s × 60 件 ≈ 36s 是最坏情况，
#: 实际只有「库存未知」的那几条会走这条路。真机风控对连续请求很敏感，宁可慢也不并发。
#: 上限 60：真实账号 45 件全未知时，**一次同步就要全部补齐**，否则用户得连点两次。
STOCK_REFRESH_INTERVAL = 0.6
STOCK_REFRESH_LIMIT = 60


def _coerce_stock(value: Any) -> Optional[int]:
    """库存字段 -> int；认不出 / 负数 -> None（= 未知，落库写 -1，**不写 1**）。"""
    if value in (None, ''):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _is_risk_or_expired(value: Any) -> bool:
    """这个异常是不是「被风控」或者「登录态过期」——命中就立刻停手，别空转重试。"""
    if value is None:
        return False
    if common._is_session_expired(value):
        return True
    module = common.protocol_module('risk')
    checker = getattr(module, 'is_risk_control_error', None) if module is not None else None
    if callable(checker):
        try:
            if checker(value):
                return True
        except Exception:  # noqa: BLE001 - 判定器本身出错不该把同步带崩
            pass
    client_module = common.protocol_module('client')
    risk_cls = getattr(client_module, 'RiskControlError', None) if client_module is not None else None
    return isinstance(risk_cls, type) and isinstance(value, risk_cls)


def _refresh_unknown_stock(account_id: str, client: Any, interval: Optional[float] = None,
                           limit: Optional[int] = None) -> Dict[str, Any]:
    """给「库存未知」（stock < 0）的商品逐件补详情，写回真实库存 / 图片 / 描述。

    列表接口根本不返回库存，真库存只有详情 `data.itemDO.quantity` 才有，所以要逐件取。
    节流：件与件之间 sleep `STOCK_REFRESH_INTERVAL`（真机连续请求很容易触发风控），
    单次最多 `STOCK_REFRESH_LIMIT` 件。**一碰到风控或登录态过期立刻停手并保留已有
    数据** —— 被拦了还继续打，只会把账号打黑。
    返回 `{'refreshed': n, 'stopped': 'risk'|'session'|'call_error'|None}`。
    """
    if interval is None:
        interval = STOCK_REFRESH_INTERVAL
    if limit is None:
        limit = STOCK_REFRESH_LIMIT
    rows = db.query(
        'SELECT item_id FROM items WHERE account_id = ? AND stock < 0 ORDER BY item_id LIMIT ?',
        (account_id, int(limit)),
    )
    refreshed = 0
    stopped: Optional[str] = None
    for index, row in enumerate(rows):
        item_id = str(row['item_id'])
        if index:
            time.sleep(interval)
        # 复用 `common.call_method`（内部走 run_awaitable / 共享事件循环），
        # **不**另起 asyncio.run 新 loop。
        status, value = common.call_method(client, ('get_item', 'item_detail'), item_id)
        if status != common.CALL_OK:
            if status == common.CALL_ERROR and _is_risk_or_expired(value):
                stopped = 'session' if common._is_session_expired(value) else 'risk'
                logger.warning('补库存被风控/登录态打断 account={} item={} err={}',
                               account_id, item_id, value)
            else:
                stopped = 'call_error'
            break
        detail = value if isinstance(value, dict) else {}
        stock = _coerce_stock(detail.get('stock'))
        images = detail.get('images')
        if isinstance(images, list):
            images_json = json.dumps(images, ensure_ascii=False)
        else:
            images_json = ''
        description = str(detail.get('desc') or detail.get('description') or '')
        with db.tx():
            db.execute(
                'UPDATE items SET stock = CASE WHEN ? < 0 THEN stock ELSE ? END, '
                "images = CASE WHEN ? IN ('[]', '') THEN images ELSE ? END, "
                "description = CASE WHEN ? = '' THEN description ELSE ? END, "
                'synced_at = CURRENT_TIMESTAMP WHERE account_id = ? AND item_id = ?',
                (-1 if stock is None else stock, -1 if stock is None else stock,
                 images_json, images_json, description, description, account_id, item_id),
            )
        if stock is not None:
            refreshed += 1
    if stopped:
        common.add_risk_event(account_id, 'stock_refresh_stopped', str(stopped))
    return {'refreshed': refreshed, 'stopped': stopped}


def sync_items(account_id: str) -> Any:
    """从闲鱼拉在售商品入库（upsert）。协议层未就绪 -> xianyu_not_ready。"""
    if not account_id:
        return common.fail('account_id 不能为空', 'bad_request')
    client, err = common.get_client(account_id)
    if client is None:
        common.log_operation(account_id, 'sync_items', '', False, (err or {}).get('message', ''))
        return err or common.not_ready()

    # 真签名：list_items(page_number=1, page_size=20, max_pages=5)（**没有
    # account_id** —— 账号身份由 client 自带 cookie 决定）。多传一个参数会在
    # Python 层 TypeError，请求根本发不出去，正是「商品拉不过来」的根因。
    status, value = common.call_method(client, ('list_items',))
    problem = common.call_failed(status, value)
    if problem is not None:
        common.log_operation(account_id, 'sync_items', '', False, problem.get('message', ''))
        return problem

    if isinstance(value, dict) and isinstance(value.get('items'), list):
        raw_items = value['items']
    else:
        raw_items = common.extract_list(value, 'items', 'list', 'data')
    ok = 0
    synced_ids: List[str] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = common.extract_id(raw, 'item_id', 'id')
        if not item_id:
            continue
        stock = _coerce_stock(raw.get('stock'))
        if stock is None:
            stock = _coerce_stock(raw.get('quantity'))
        images_raw = raw.get('images')
        if isinstance(images_raw, str):
            images_json = images_raw
        else:
            images_json = json.dumps(images_raw or [], ensure_ascii=False)
        # 未知库存写 -1（**不是 1**）：列表卡根本没有库存字段，拿 1 冒充会让
        # 「已售罄」的商品显示有货。前端把 -1 显示成 `-`。
        db.execute(
            'INSERT INTO items(account_id, item_id, title, price, original_price, stock, status, '
            'category_id, images, description, synced_at, updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) '
            'ON CONFLICT(account_id, item_id) DO UPDATE SET '
            'title = excluded.title, price = excluded.price, original_price = excluded.original_price, '
            # 库存**总是**以本次结果为准（列表给不出就写 -1）。曾经这里是
            # 「-1 时保留库里旧值」，结果用户库里那批旧代码硬编码的假 1 永远自愈不了：
            # 重新同步多少次 excluded 都是 -1 -> 继续保留假 1，而补详情队列只挑
            # stock < 0 的行，假 1 连队列都进不去。**平台才是库存真相**：本地手改过的
            # 库存被同步重置为 -1 是预期行为，紧接着 _refresh_unknown_stock 会把平台
            # 真值（如 66）补回来，而不是留着一个假数字。
            'stock = excluded.stock, '
            'status = excluded.status, category_id = excluded.category_id, '
            # 图片/描述同理：列表没有就别抹掉详情补回来的。
            "images = CASE WHEN excluded.images IN ('[]', '', 'null') THEN items.images "
            'ELSE excluded.images END, '
            "description = CASE WHEN excluded.description = '' THEN items.description "
            'ELSE excluded.description END, '
            'synced_at = CURRENT_TIMESTAMP',
            (
                account_id, item_id,
                str(raw.get('title') or raw.get('item_title') or ''),
                float(raw.get('price') or raw.get('soldPrice') or 0) if str(raw.get('price') or raw.get('soldPrice') or 0).replace('.', '', 1).isdigit() else 0.0,
                float(raw.get('original_price') or 0) if str(raw.get('original_price') or 0).replace('.', '', 1).isdigit() else 0.0,
                -1 if stock is None else stock,
                str(raw.get('status') or 'on_sale'),
                str(raw.get('category_id') or ''),
                images_json,
                str(raw.get('description') or raw.get('desc') or ''),
            ),
        )
        ok += 1
        synced_ids.append(item_id)

    refresh = _refresh_unknown_stock(account_id, client)
    refreshed = refresh['refreshed']
    common.log_operation(account_id, 'sync_items', '', True,
                         {'count': ok, 'stock_refreshed': refreshed,
                          'stock_refresh_stopped': refresh['stopped']})
    common.publish('items.synced', {'account_id': account_id, 'count': ok,
                                    'stock_refreshed': refreshed})
    _publish_updated(account_id, synced_ids, 'sync')
    result: Dict[str, Any] = {'account_id': account_id, 'count': ok,
                              'stock_refreshed': refreshed,
                              'items': list_items(account_id=account_id)['items']}
    if refresh['stopped']:
        result['stock_refresh_stopped'] = refresh['stopped']
    return result


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

    # 真签名：publish_item(payload)（**一个参数**）。多传 account_id 会在 Python
    # 层 TypeError → 502 protocol_error，请求根本没发出去。
    status, value = common.call_method(client, ('publish_item',), payload)
    problem = common.call_failed(status, value)
    if problem is not None:
        # 平台对发布的拒绝理由（`FAIL_BIZ_IDLE_UNKNOWN_THROWABLE::发布失败，请稍后重试吧~`
        # 这种）对用户没有任何可行动信息 —— 翻成人话再返回，原样透传没见过的码。
        problem = platform_errors.decorate(problem, value)
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
    if item_id:
        _publish_updated(account_id, [item_id], 'publish')
    return {'item_id': item_id, 'raw': value}
