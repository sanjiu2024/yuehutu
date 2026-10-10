"""运费模版（官方 `post-temple` 页）只读接口。

**为什么单独一个模块**：官方这一项菜单写作「发布模板 / post-temple」，协议层实为
`pro.freight.template.*`（运费模版）。只读镜像页放在自己的前缀下，避免和
`backend/app/api/items.py` 的 `/api/items/{account_id}/{item_id}` 抢路径。

**只读**：这里没有创建/编辑/删除/解绑路由（写接口名与参数形状见
`backend/app/xianyu/item_templates.py` 的 `WRITE_APIS` 与 `docs/contracts/item_templates.md`）。

挂载：本模块需由 Lead 加进 `backend/app/main.py` 的 `ROUTE_MODULES`（该文件不在本次写范围）。
测试里自挂本 router，不依赖 main.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
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


@router.get('/{account_id}/{template_id}')
def template_detail(
    account_id: str,
    template_id: str,
    user: Any = Depends(current_user),
):
    """单个运费模版详情（只读）。"""
    return _envelope(item_templates.template_detail(account_id, template_id))
