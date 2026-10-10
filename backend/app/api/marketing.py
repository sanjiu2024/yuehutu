"""营销页只读接口（`/marketing`）。

四条 GET，全部只读。**没有 POST / PUT / PATCH / DELETE**：营销页的投放 / 报名 / 批量
操作（协议层 `WRITE_APIS` 里登记的那两个写接口）本任务不实现 —— 花钱的动作不做复刻，
FastAPI 对未注册的方法在这些路径上会自动回 **405**（`tests/test_marketing_api.py`
专门钉住了这一点）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, marketing
from .deps import current_user

router = APIRouter(prefix='/api/marketing', tags=['marketing'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/items')
def items(account_id: str,
          page: str = Query('', description='页码，从 1 开始'),
          size: str = Query('', description='每页条数，1~50，缺省 20'),
          user=Depends(current_user)):
    """营销商品列表（只读）。"""
    return _envelope(marketing.items(account_id, page, size))


@router.get('/{account_id}/tasks')
def tasks(account_id: str, user=Depends(current_user)):
    """营销信息 / 任务（只读）。"""
    return _envelope(marketing.tasks(account_id))


@router.get('/{account_id}/price-competitiveness')
def price_competitiveness(account_id: str,
                          item_ids: str = Query('', description='逗号分隔的商品 id，可空'),
                          user=Depends(current_user)):
    """价格竞争力（只读）。`item_ids` 空则不按商品过滤。"""
    ids = [part.strip() for part in str(item_ids or '').split(',') if part.strip()]
    return _envelope(marketing.price_competitiveness(account_id, ids))


@router.get('/{account_id}/records')
def records(account_id: str,
            page: str = Query('', description='页码，从 1 开始'),
            size: str = Query('', description='每页条数，1~50，缺省 20'),
            user=Depends(current_user)):
    """操作记录（只读流水）。"""
    return _envelope(marketing.records(account_id, page, size))
