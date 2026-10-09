"""超级擦亮只读接口。禁止充值、提现、申诉与投放写操作。"""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from ..services import common, promotion
from .deps import current_user

router = APIRouter(prefix='/api/promotion', tags=['promotion'])

def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)

@router.get('/{account_id}/overview')
def overview(account_id: str, start: str = Query('', description='YYYYMMDD'), end: str = Query('', description='YYYYMMDD'), user=Depends(current_user)):
    return _envelope(promotion.overview(account_id, start, end))

@router.get('/{account_id}/campaigns')
def campaigns(account_id: str, start: str = Query('', description='YYYYMMDD'), end: str = Query('', description='YYYYMMDD'), page: str = Query(''), size: str = Query(''), user=Depends(current_user)):
    return _envelope(promotion.campaigns(account_id, start, end, page, size))

@router.get('/{account_id}/reports')
def reports(account_id: str, start: str = Query('', description='YYYYMMDD'), end: str = Query('', description='YYYYMMDD'), page: str = Query(''), size: str = Query(''), user=Depends(current_user)):
    return _envelope(promotion.reports(account_id, start, end, page, size))

@router.get('/{account_id}/account')
def account(account_id: str, start: str = Query('', description='YYYYMMDD'), end: str = Query('', description='YYYYMMDD'), user=Depends(current_user)):
    return _envelope(promotion.account(account_id, start, end))
