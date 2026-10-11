"""运费模版（官方 `post-temple` 页）接口：只读 2 条 + 写 4 条。

**为什么单独一个模块**：官方这一项菜单写作「发布模板 / post-temple」，协议层实为
`pro.freight.template.*`（运费模版）。只读镜像页放在自己的前缀下，避免和
`backend/app/api/items.py` 的 `/api/items/{account_id}/{item_id}` 抢路径。

**读**：`GET /{account_id}`（列表）、`GET /{account_id}/{template_id}`（详情）。
**写**：`POST /{account_id}/create`、`POST /{account_id}/{template_id}/modify`、
`POST /{account_id}/{template_id}/delete`、`POST /{account_id}/unbind`（给商品设置运费）。
四个写接口都要求 body 里**字面量** `confirm: true`，未确认直接 400 且一个平台请求都不发。
写路径不会和上面的只读路径撞：方法不同（POST vs GET），且写路径多一段后缀。

挂载：本模块需由 Lead 加进 `backend/app/main.py` 的 `ROUTE_MODULES`（该文件不在本次写范围）。
测试里自挂本 router，不依赖 main.py。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, item_templates
from .deps import current_user

router = APIRouter(prefix='/api/item-templates', tags=['item-templates'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}')
def list_templates(
    account_id: str,
    page: str = Query('', description='页码，默认 1'),
    size: str = Query('', description='每页条数 1~50，默认 20'),
    template_name: str = Query('', description='按模版名搜索（平台侧支持）'),
    user: Any = Depends(current_user),
):
    """某个账号的运费模版列表（只读）。"""
    return _envelope(item_templates.list_templates(account_id, page, size, template_name))


@router.post('/{account_id}/create')
def create_template(
    account_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{confirm, template_name, item_prov?, item_city?, valuation?, is_default?, '
                    'details?: [{receive_division_name, first_price, excess_price, first, excess, '
                    'is_default?, operate?}], template_details?}'),
    user: Any = Depends(current_user),
):
    """新建运费模版（写，需 confirm）。"""
    return _envelope(item_templates.create_template(
        account_id,
        body.get('template_name'),
        body.get('item_prov', ''),
        body.get('item_city', ''),
        body.get('valuation'),
        body.get('is_default', False),
        body.get('details'),
        body.get('template_details', ''),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/unbind')
def unbind_items(
    account_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='{confirm, item_ids(逗号串或数组), mode: free_ship|template|fixed, '
                    'template_id?(mode=template), post_price?(mode=fixed, 元)}'),
    user: Any = Depends(current_user),
):
    """给商品设置运费：包邮 / 用模版 / 固定运费（写，需 confirm）。"""
    return _envelope(item_templates.unbind_items(
        account_id,
        body.get('item_ids'),
        body.get('mode', ''),
        body.get('template_id', ''),
        body.get('post_price'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/{template_id}/modify')
def modify_template(
    account_id: str,
    template_id: str,
    body: Dict[str, Any] = Body(
        default={},
        description='同 create，另可带 version（官方 bundle 修改时两个都发）'),
    user: Any = Depends(current_user),
):
    """修改运费模版（写，需 confirm）。"""
    return _envelope(item_templates.modify_template(
        account_id,
        template_id,
        body.get('template_name'),
        body.get('item_prov', ''),
        body.get('item_city', ''),
        body.get('valuation'),
        body.get('is_default', False),
        body.get('details'),
        body.get('template_details', ''),
        body.get('version'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/{template_id}/delete')
def delete_template(
    account_id: str,
    template_id: str,
    body: Dict[str, Any] = Body(default={}, description='{confirm}'),
    user: Any = Depends(current_user),
):
    """删除运费模版（写，需 confirm；不可撤销）。"""
    return _envelope(item_templates.delete_template(
        account_id, template_id, confirm=body.get('confirm')))


@router.get('/{account_id}/{template_id}')
def template_detail(
    account_id: str,
    template_id: str,
    user: Any = Depends(current_user),
):
    """单个运费模版详情（只读）。"""
    return _envelope(item_templates.template_detail(account_id, template_id))
