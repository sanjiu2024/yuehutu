"""数据组只读接口：数据总览 / 粉丝数据 / 客服数据 / 商品数据 / 经营卡片（六条 GET）。

参数只有 `start` / `end`（`YYYYMMDD`）等查询串，缺省语义逐页不同（前三页最近 7 天、
商品数据与经营卡片是「今天」—— 与官方页面一致）。响应统一走
`common.respond`：成功 `{"success": true, "data": {...}}`，失败
`{"success": false, "message": "...", "code": "..."}`（`verification_required` 时另带
`login_state: need_relogin`）。**指标缺失时是 `null`，不是 0**，前端显示「-」。

与 `backend/app/api/promotion.py` 同形态（最薄的转发层，不含业务判断）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, datacenter
from .deps import current_user

router = APIRouter(prefix='/api/datacenter', tags=['datacenter'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/overview')
def overview(
    account_id: str,
    start: str = Query('', description='YYYYMMDD'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    return _envelope(datacenter.overview(account_id, start, end))


@router.get('/{account_id}/fans')
def fans(
    account_id: str,
    start: str = Query('', description='YYYYMMDD'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    return _envelope(datacenter.fans(account_id, start, end))


@router.get('/{account_id}/customer-service')
def customer_service(
    account_id: str,
    start: str = Query('', description='YYYYMMDD'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    return _envelope(datacenter.customer_service(account_id, start, end))


@router.get('/{account_id}/commodity')
def commodity(
    account_id: str,
    start: str = Query('', description='YYYYMMDD，不传就是「今天」'),
    end: str = Query('', description='YYYYMMDD'),
    item_id: str = Query('', description='按商品 ID 精确筛选'),
    item_name: str = Query('', description='按商品标题模糊筛选'),
    category_info: str = Query('', description='类目筛选，JSON 数组字符串'),
    is_dp_stk: str = Query('', description='是否掉包（官方下拉值，空=不筛）'),
    mkt_act_id: str = Query('', description='营销活动 ID（官方活动 tab）'),
    page: int = Query(1, ge=1),
    size: int = Query(datacenter.COMMODITY_DEFAULT_PAGE_SIZE, ge=1,
                      le=datacenter.COMMODITY_MAX_PAGE_SIZE),
    order_by: str = Query('', description='排序字段（取指标列定义里的 name）'),
    order: str = Query('', description='asc / desc'),
    user=Depends(current_user),
):
    """商品数据（一行一个商品 + 平台给的指标列）。

    `size` 的上下界用 `Query(ge/le)` 声明而不是只在服务层校验：FastAPI 会先拦下来
    给 422，AI 工具与前端都能从 openapi 里直接看到边界。服务层仍保留同一份校验
    （MCP 之外还有别的调用方）。
    """
    return _envelope(datacenter.commodity(
        account_id, start, end, item_id, item_name, category_info, is_dp_stk,
        mkt_act_id, page, size, order_by, order,
    ))


@router.get('/{account_id}/card/{kind}')
def card(
    account_id: str,
    kind: str,
    start: str = Query('', description='YYYYMMDD，不传就是「今天」'),
    end: str = Query('', description='YYYYMMDD'),
    user=Depends(current_user),
):
    """数据总览页的一张经营卡片：`trade` 交易数据 / `seller` 经营概况 / `repurchase` 复购 /
    `refund` 退款概览。

    **一次只查一张**：官方页面就是四个独立请求，各自失败不影响其它三张（所以这里不做
    「一次拿四张」的聚合接口 —— 那会把「一张卡没有权限」变成「整页空白」）。
    指标名与中文标题都以平台为准（`label` 拿不到就是 `null`，前端显示平台字段名）。
    """
    return _envelope(datacenter.card(account_id, kind, start, end))
