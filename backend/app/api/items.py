"""商品路由（薄层：参数校验 + 调度 services.items）。

同时提供两套路径：
- 任务卡（前端早期约定的扁平写法）：`GET /api/items`、`PUT /api/items/{item_id}`、
  `POST /api/items/batch`、`POST /api/items/sync`、`POST /api/items/publish`、
  `POST /api/items/downshelf`、`POST /api/items/reshelf`、`GET /api/items/stats`
- CONTRACTS §3（前端与 MCP 的**正式**路径，account_id 在路径里）：
  `GET /api/items/{account_id}/{item_id}`、`POST /api/items/{account_id}/sync`、
  `POST /api/items/{account_id}/publish`、`POST /api/items/{account_id}/{item_id}/edit`、
  `.../delist|/relist`、`POST /api/items/{account_id}/batch-edit`、
  `GET /api/items/{account_id}/operation-logs`

声明顺序很重要：**字面量路径一律放在 `{item_id}` 之前**，否则 `stats` / `batch` 会被
当成 item_id 吞掉。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, items
from .deps import current_user

router = APIRouter(prefix='/api/items', tags=['items'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------- 列表 / 统计


@router.get('')
@router.get('/')
def list_items(
    account_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    limit: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    order: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    """`q` 是 CONTRACTS 的名字，`keyword` 是服务层的名字；`limit/offset` 折算成 page/size。

    `status` 认 `onsale` / `offline` / `sold` 三个分区（服务层 `_status_clause` 决定口径）；
    `sort` / `order` 走白名单，认不出的值落回「更新时间倒序」——**不报错**，
    前端点表头不会因为一个拼错的列名把整页打成错误态。
    """
    if limit:
        size = max(1, min(int(limit), 500))
        if offset:
            page = int(offset) // size + 1
    return _envelope(items.list_items(account_id=account_id, keyword=q or keyword, page=page, size=size,
                                      status=status, sort=sort, order=order))


@router.get('/stats')
def item_stats(user=Depends(current_user)):
    return _envelope(items.item_stats())


@router.get('/operation-logs')
def operation_logs(
    account_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    user=Depends(current_user),
):
    return _envelope({'logs': items.list_operation_logs(account_id=account_id, limit=limit)})


# --------------------------------------------------------------------------- 字面量动作（必须在 {item_id} 之前）


@router.post('/sync')
def sync_items(payload: Dict[str, Any] = Body(default={}), user=Depends(current_user)):
    account_id = str((payload or {}).get('account_id') or '')
    return _envelope(items.sync_items(account_id))


@router.post('/batch')
def batch_update(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """body: {ids: [...], fields: {...}, confirm: true}（第 52 批起必须显式确认）"""
    ids = payload.get('ids') or payload.get('item_ids') or []
    fields = payload.get('fields') or payload.get('patch') or {}
    return _envelope(items.batch_update(ids, fields, confirm=payload.get('confirm')))


@router.post('/publish')
def publish_item(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    body = dict(payload or {})
    account_id = str(body.pop('account_id', '') or '')
    return _envelope(items.publish_item(account_id, body, confirm=body.pop('confirm', None)))


@router.post('/downshelf')
def downshelf(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    ids = payload.get('ids') or payload.get('item_ids') or []
    return _envelope(items.downshelf(ids, confirm=payload.get('confirm')))


@router.post('/reshelf')
def reshelf(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    ids = payload.get('ids') or payload.get('item_ids') or []
    return _envelope(items.reshelf(ids, confirm=payload.get('confirm')))


# --------------------------------------------------------------------------- 带 account_id 的正式路径


@router.post('/{account_id}/sync')
def sync_items_for_account(account_id: str, user=Depends(current_user)):
    return _envelope(items.sync_items(account_id))


@router.post('/{account_id}/batch-edit')
def batch_edit(account_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """CONTRACTS §3：{item_ids:[...], patch:{price_delta|price_set|title_tpl|...}}

    `account_id` **必须**参与校验：路径里的账号只能改自己的商品，属于别人的进
    `failed`（`code=account_mismatch`），不整批 400 —— 前端与 MCP 都靠逐条结果。
    响应体在统一信封内透出 `ok/synced/local_only/warnings/failed/skipped`，
    协议层没就绪时 `ok` 为 0（本地改了但没推的不算 ok）。
    """
    ids = payload.get('item_ids') or payload.get('ids') or []
    patch = payload.get('patch') or payload.get('fields') or {}
    return _envelope(items.batch_update(ids, patch, account_id=account_id,
                                        confirm=payload.get('confirm')))


@router.post('/{account_id}/batch-publish')
def batch_publish(account_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    rows = payload.get('items') or []
    body_confirm = payload.get('confirm')
    results = []
    for row in rows:
        if not isinstance(row, dict):
            results.append((None, {'success': False, 'message': 'item 必须是对象'}))
            continue
        # 每一行自己也可以带 confirm（逐条确认）；行里的 confirm 必须 pop 掉，
        # 否则会被当成发布载荷的一个字段发给协议层。
        item = dict(row)
        row_confirm = item.pop('confirm', None)
        confirm = row_confirm if row_confirm is not None else body_confirm
        # 失败项要把**是哪一条**带回去，否则前端只能报「有 N 条失败」而说不出是哪条
        results.append((item.get('item_id') or item.get('id') or '',
                        items.publish_item(account_id, item, confirm=confirm)))
    ok = sum(1 for _, r in results if not common.is_failure(r))
    return _envelope({'total': len(rows), 'ok': ok,
                      'failed': [{'id': str(rid or ''), 'message': r.get('message', '')}
                                 for rid, r in results if common.is_failure(r)]})


@router.post('/{account_id}/publish')
def publish_item_for_account(account_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    body = dict(payload or {})
    return _envelope(items.publish_item(account_id, body, confirm=body.pop('confirm', None)))


@router.post('/{account_id}/publish/precheck')
def publish_precheck_for_account(account_id: str, payload: Dict[str, Any] = Body(...),
                                 user=Depends(current_user)):
    """发布前预检（**只读**）：跑到分类推荐为止，不注册分类路径、不发布。"""
    return _envelope(items.publish_precheck(account_id, payload))


@router.get('/{account_id}/operation-logs')
def operation_logs_for_account(
    account_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    user=Depends(current_user),
):
    return _envelope({'logs': items.list_operation_logs(account_id=account_id, limit=limit)})


@router.get('/{account_id}')
def list_items_for_account(
    account_id: str,
    status: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
    sort: Optional[str] = Query(default=None),
    order: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    """某个账号的商品列表。

    缺了这条路由时 `GET /api/items/{account_id}` 会被下面的
    `GET /{item_id}` 吞掉，当成「item_id = 账号名」查，报 `商品不存在` 404 ——
    看起来像账号没商品，其实路由根本不存在。
    """
    return _envelope(items.list_items(account_id=account_id, keyword=q or keyword,
                                      page=page, size=size, status=status,
                                      sort=sort, order=order))


@router.get('/{account_id}/{item_id}')
def get_item_for_account(account_id: str, item_id: str, user=Depends(current_user)):
    """CONTRACTS §3 的单条商品（MCP 的 items_get 直连这条）。"""
    return _envelope(items.get_item_by_account(account_id, item_id))


@router.delete('/{account_id}/{item_id}')
def delete_item_for_account(account_id: str, item_id: str, user=Depends(current_user)):
    """**只删本地记录**，闲鱼上的商品不受影响。

    平台侧**有**删除接口（`mtop.alibaba.idle.seller.pc.item.batch.delete`，见
    `POST /{account_id}/batch-delete`），这一条故意保持「只动本地」的语义，
    给「只想把列表清干净」用；两个入口的区别在 UI 上是两个菜单项
    （「删除」= 真删 / 「只从本地列表移除」= 这一条）。
    """
    return _envelope(items.delete_item(account_id, item_id))


@router.post('/{account_id}/{item_id}/edit')
def edit_item(account_id: str, item_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    body = dict(payload or {})
    return _envelope(items.update_item(item_id, body, account_id=account_id,
                                       confirm=body.pop('confirm', None)))


@router.post('/{account_id}/{item_id}/delist')
def delist_item(account_id: str, item_id: str, payload: Dict[str, Any] = Body(default={}),
                user=Depends(current_user)):
    """下架单条（**平台写**）：第 52 批起 body 必须带 `confirm: true`。"""
    return _envelope(items.downshelf([item_id], account_id=account_id,
                                     confirm=(payload or {}).get('confirm')))


@router.post('/{account_id}/{item_id}/relist')
def relist_item(account_id: str, item_id: str, payload: Dict[str, Any] = Body(default={}),
                user=Depends(current_user)):
    """重新上架单条（**平台写**）：第 52 批起 body 必须带 `confirm: true`。"""
    return _envelope(items.reshelf([item_id], account_id=account_id,
                                   confirm=(payload or {}).get('confirm')))


# --------------------------------------------------------------------------- 商品写接口
#
# 三条都走官方 `mtop.alibaba.idle.seller.pc.item.*` 族（证据见
# `backend/app/xianyu/item_write.py`），与上面的 `delist` / `relist` / `edit` 并存：
# 老的仍在（批量上架、整份编辑还用它们），新的是官方商品管理页的做法。
#
# 全部是 `Body(default={})` 自由 dict —— 字段校验在服务层，路由层只做转发。
# **`confirm` 必须由调用方显式给 true**，服务层第一道闸门就是它。

@router.post('/{account_id}/batch-offline')
def batch_offline_items(account_id: str, payload: Dict[str, Any] = Body(default={}),
                        user=Depends(current_user)):
    """批量下架：一次请求停售一批（`item_ids` 收数组或逗号串）。"""
    return _envelope(items.batch_offline(account_id, payload.get('item_ids'),
                                         confirm=payload.get('confirm')))


@router.post('/{account_id}/batch-delete')
def batch_delete_items(account_id: str, payload: Dict[str, Any] = Body(default={}),
                       user=Depends(current_user)):
    """批量删除：**平台侧真删除**，不可撤销。"""
    return _envelope(items.batch_delete(account_id, payload.get('item_ids'),
                                        confirm=payload.get('confirm')))


@router.post('/{account_id}/{item_id}/quick-edit')
def quick_edit_item(account_id: str, item_id: str, payload: Dict[str, Any] = Body(default={}),
                    user=Depends(current_user)):
    """快捷改价 / 改库存：只发 `{itemId, price, quantity}`，不整份回提表单。

    与 `/edit` 的区别写在 `services.items.quick_edit` 的 docstring：整份回提会把
    `itemStatus` 一起写回去，对已卖出商品等于顺手重新上架。
    """
    return _envelope(items.quick_edit(account_id, item_id,
                                      price=payload.get('price'),
                                      quantity=payload.get('quantity'),
                                      confirm=payload.get('confirm')))


# --------------------------------------------------------------------------- 通用单条（放最后）
#
# **这里曾经是 `GET /api/items/{item_id}`，已删除。** 它与上面的
# `GET /{account_id}`（账号商品列表）形状完全相同，FastAPI 按声明顺序匹配，两条
# 只能活一条：谁在后面谁永远收不到请求。账号列表这条是审计 I7 要求补的，且前端
# 「商品」页按账号拉列表必须走它；扁平单条没有任何生产调用方（MCP 的 items_get
# 走的是带账号上下文的 `/{account_id}/{item_id}`），所以这里删掉扁平那条，
# 单条商品一律要求带账号上下文。CONTRACTS §3 已同步。


@router.put('/{item_id}')
def update_item(item_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    body = dict(payload or {})
    return _envelope(items.update_item(item_id, body, confirm=body.pop('confirm', None)))
