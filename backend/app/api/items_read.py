"""商品读 5 条接口的只读路由（第 63 批 task-23）。

前缀 `/api/items-read`：与 `api/items.py` 的 `/api/items` **不同段**，不会被
`GET /api/items/{account_id}/{item_id}` 抢先匹配（那个模式要求第二段字面量是 `items`），
所以挂载顺序无所谓。

五条全是 GET + 登录态依赖（`current_user`），响应统一走 `common.respond`：
成功 `{"success": true, "data": {...}}`，失败 `{"success": false, "message": "...", "code": "..."}`。

参数校验的上下界在 `Query(ge/le)` 里也声明一份：FastAPI 会先拦下来给 422，
AI 工具与前端都能从 openapi 里直接看到边界；服务层仍保留同一份校验（MCP 之外还有别的调用方）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, items_read
from .deps import current_user

router = APIRouter(prefix='/api/items-read', tags=['items-read'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


@router.get('/{account_id}/search')
def search(
    account_id: str,
    keyword: str = Query('', description='关键词（塞进 searchRequest 的 title，键名未取证）'),
    page: int = Query(1, ge=1, le=items_read.SEARCH_MAX_PAGE),
    size: int = Query(items_read.SEARCH_DEFAULT_PAGE_SIZE, ge=1,
                      le=items_read.SEARCH_MAX_PAGE_SIZE,
                      description='官方只给 10 / 20 / 50 三档'),
    item_status: str = Query('', description="商品状态码，默认 '0,-9'（在售 + 已下架）"),
    biz_type: str = Query('', description="官方写死 commonPro"),
    order_field: str = Query('', description='排序字段（官方 searchRequest 之外的那个）'),
    order_type: str = Query('', description='asc / desc（给了方向就必须给字段）'),
    search_request: str = Query('', description='原始 searchRequest JSON 字符串（探针用，给了就原样发）'),
    user=Depends(current_user),
):
    """商品列表：服务端搜索 + 分页 + 排序。列表键以平台为准，认不出就是空列表。"""
    return _envelope(items_read.search_items(
        account_id, keyword, page, size, item_status, biz_type, order_field,
        order_type, search_request))


@router.get('/{account_id}/categories')
def categories(
    account_id: str,
    params: str = Query('', description='透传入参，JSON 对象字符串（入参形状未取证）'),
    user=Depends(current_user),
):
    """类目查询。**入参透传**：bundle 里只有声明、调用点 0，我们不猜字段名。"""
    return _envelope(items_read.category_query(account_id, params))


@router.get('/{account_id}/operate-report')
def operate_report(
    account_id: str,
    params: str = Query('', description='透传入参，JSON 对象字符串（入参形状未取证）'),
    user=Depends(current_user),
):
    """商品操作报告。**入参透传**，理由同 `categories`。"""
    return _envelope(items_read.operate_report(account_id, params))


@router.get('/{account_id}/search-terms')
def search_terms(account_id: str, user=Depends(current_user)):
    """筛选维度字典（退货 / 发货时效 / 服务保障），**无参**。"""
    return _envelope(items_read.search_terms(account_id))


@router.get('/{account_id}/preget')
def preget(
    account_id: str,
    item_id: str = Query('', description='商品 id（新建发布时为空）'),
    source_id: str = Query('', description='来源 id（发布场景）'),
    publish_scene: str = Query('', description="默认 firstHandPublish（bundle 原文）"),
    user=Depends(current_user),
):
    """发布/编辑前的预请求：要不要上传首件、行业 id。"""
    return _envelope(items_read.preget(account_id, item_id, source_id, publish_scene))
