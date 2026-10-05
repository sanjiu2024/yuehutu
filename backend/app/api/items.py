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
    user=Depends(current_user),
):
    """`q` 是 CONTRACTS 的名字，`keyword` 是服务层的名字；`limit/offset` 折算成 page/size。"""
    if limit:
        size = max(1, min(int(limit), 500))
        if offset:
            page = int(offset) // size + 1
    return _envelope(items.list_items(account_id=account_id, keyword=q or keyword, page=page, size=size, status=status))


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
    """body: {ids: [...], fields: {价格/标题/描述/库存 或 price/title/description/stock}}"""
    ids = payload.get('ids') or payload.get('item_ids') or []
    fields = payload.get('fields') or payload.get('patch') or {}
    return _envelope(items.batch_update(ids, fields))


@router.post('/publish')
def publish_item(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    body = dict(payload or {})
    account_id = str(body.pop('account_id', '') or '')
    return _envelope(items.publish_item(account_id, body))


@router.post('/downshelf')
def downshelf(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    ids = payload.get('ids') or payload.get('item_ids') or []
    return _envelope(items.downshelf(ids))


@router.post('/reshelf')
def reshelf(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    ids = payload.get('ids') or payload.get('item_ids') or []
    return _envelope(items.reshelf(ids))


# --------------------------------------------------------------------------- 带 account_id 的正式路径


@router.post('/{account_id}/sync')
def sync_items_for_account(account_id: str, user=Depends(current_user)):
    return _envelope(items.sync_items(account_id))


@router.post('/{account_id}/batch-edit')
def batch_edit(account_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """CONTRACTS §3：{item_ids:[...], patch:{price_delta|price_set|title_tpl|...}}"""
    ids = payload.get('item_ids') or payload.get('ids') or []
    patch = payload.get('patch') or payload.get('fields') or {}
    return _envelope(items.batch_update(ids, patch))


@router.post('/{account_id}/batch-publish')
def batch_publish(account_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    rows = payload.get('items') or []
    results = []
    for row in rows:
        if not isinstance(row, dict):
            results.append({'success': False, 'message': 'item 必须是对象'})
            continue
        results.append(items.publish_item(account_id, row))
    ok = sum(1 for r in results if not common.is_failure(r))
    return _envelope({'total': len(rows), 'ok': ok,
                      'failed': [{'id': '', 'message': r.get('message', '')} for r in results if common.is_failure(r)]})


@router.post('/{account_id}/publish')
def publish_item_for_account(account_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    return _envelope(items.publish_item(account_id, payload))


@router.get('/{account_id}/operation-logs')
def operation_logs_for_account(
    account_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    user=Depends(current_user),
):
    return _envelope({'logs': items.list_operation_logs(account_id=account_id, limit=limit)})


@router.get('/{account_id}/{item_id}')
def get_item_for_account(account_id: str, item_id: str, user=Depends(current_user)):
    """CONTRACTS §3 的单条商品（MCP 的 items_get 直连这条）。"""
    return _envelope(items.get_item_by_account(account_id, item_id))


@router.post('/{account_id}/{item_id}/edit')
def edit_item(account_id: str, item_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    return _envelope(items.update_item(item_id, payload, account_id=account_id))


@router.post('/{account_id}/{item_id}/delist')
def delist_item(account_id: str, item_id: str, user=Depends(current_user)):
    return _envelope(items.downshelf([item_id]))


@router.post('/{account_id}/{item_id}/relist')
def relist_item(account_id: str, item_id: str, user=Depends(current_user)):
    return _envelope(items.reshelf([item_id]))


# --------------------------------------------------------------------------- 通用单条（放最后）


@router.get('/{item_id}')
def get_item(item_id: str, user=Depends(current_user)):
    return _envelope(items.get_item(item_id))


@router.put('/{item_id}')
def update_item(item_id: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    return _envelope(items.update_item(item_id, payload))
