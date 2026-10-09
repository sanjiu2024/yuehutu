"""安全中心只读接口；不包含申诉及任何写操作。"""
from typing import Any
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from ..services import common, security_center
from .deps import current_user
router = APIRouter(prefix='/api/security-center', tags=['security-center'])
def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)
@router.get('/{account_id}')
def read(account_id: str, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=50), user=Depends(current_user)):
    return _envelope(security_center.read(account_id, page, size))
