"""风控路由（薄层）。

- 任务卡：`GET /api/risk/status`、`GET /api/risk/events`、`GET/PUT /api/risk/settings`、
  `POST /api/risk/clear`
- CONTRACTS §3：`GET /api/risk/status`、`POST /api/risk/{account_id}/clear`

**清除冷却只在人工验证成功后**：`POST /api/risk/clear` 的 body 要带
`{"account_id": ..., "verified": true}`（或协议层 captcha 已写下 verification_type），
否则返回 409 `verification_required`。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, risk_service
from .deps import current_user

router = APIRouter(prefix='/api/risk', tags=['risk'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


@router.get('/status')
def status(account_id: Optional[str] = Query(default=None), user=Depends(current_user)):
    return _envelope(risk_service.status(account_id))


@router.get('/events')
def events(
    limit: int = Query(default=100, ge=1, le=1000),
    account_id: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    return _envelope(risk_service.events(limit=limit, account_id=account_id))


@router.get('/settings')
def get_settings(user=Depends(current_user)):
    return _envelope(risk_service.settings())


@router.put('/settings')
def put_settings(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """{auto_slider?: bool, cooldown_steps?: [60,180,...]|'60,180', refresh_interval_seconds?: int}"""
    return _envelope(risk_service.update_settings(payload))


@router.post('/clear')
def clear(payload: Dict[str, Any] = Body(default={}), user=Depends(current_user)):
    """body: {account_id, verification_type?}"""
    body = payload or {}
    account_id = str(body.get('account_id') or '')
    return _envelope(risk_service.clear_cooldown(
        account_id, verification_type=str(body.get('verification_type') or ''),
    ))


@router.post('/{account_id}/clear')
def clear_for_account(account_id: str, payload: Dict[str, Any] = Body(default={}), user=Depends(current_user)):
    """CONTRACTS §3：账号在路径里。

    **不接受请求体里的 `verified` 自证**：凭证只能由服务端写下 —— 真实滑块验证
    通过时 `POST /api/accounts/{id}/captcha` 的后台任务会 `mark_verified`。让调用方
    自己传 `verified: true` 等于没有门槛，任何人都能把冷却清掉。
    """
    body = payload or {}
    return _envelope(risk_service.clear_cooldown(
        account_id, verification_type=str(body.get('verification_type') or ''),
    ))
