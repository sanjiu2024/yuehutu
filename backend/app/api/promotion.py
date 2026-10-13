"""超级擦亮只读接口。

这一族全是只读：投放写操作在 `ad_write.py`，资金侧官方只有**查询**接口
（发起充值/提现在支付宝侧，官方 bundle 里没有对应 mtop 接口），所以这里没有任何资金写操作。
"""
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

@router.get('/{account_id}/funds')
def funds(account_id: str, start: str = Query('', description='YYYYMMDD'), end: str = Query('', description='YYYYMMDD'), page: str = Query(''), size: str = Query(''), account_type: str = Query('', description='0=全部'), operate_type: str = Query('', description='0=全部 1=现金 2=礼金 3=红包'), user=Depends(current_user)):
    return _envelope(promotion.funds(account_id, start, end, page, size, account_type, operate_type))

@router.get('/{account_id}/fund-operations')
def fund_operations(account_id: str, account_type: str = Query('', description='0=全部'), user=Depends(current_user)):
    return _envelope(promotion.fund_operations(account_id, account_type))

@router.get('/{account_id}/recharge/{order_id}')
def recharge_detail(account_id: str, order_id: str, user=Depends(current_user)):
    return _envelope(promotion.recharge_detail(account_id, order_id))

@router.get('/{account_id}/withdraw/{order_id}')
def withdraw_detail(account_id: str, order_id: str, user=Depends(current_user)):
    return _envelope(promotion.withdraw_detail(account_id, order_id))
