"""超级擦亮写接口（HTTP 层）。

- ``GET  /api/promotion/write/actions`` —— 写接口目录：支持哪些动作、对应哪个 mtop 名、
  哪些动作**明确不做**（充值 / 提现：bundle 里没有下单接口名）。
- ``POST /api/promotion/write/{account_id}/{action}`` —— 执行一个写动作。
  请求体：``{"confirm": true, "payload": {...业务参数...}}``。
  ``confirm`` 必须是字面量 ``true``；不给就是 400 ``confirm_required``，一次协议调用都不发。

注：本 router 只在这里定义，**没有**挂进 ``backend/app/main.py``（不在本任务写范围，
挂载由 Lead 统一决定）；测试自己 include_router，见 ``backend/tests/test_ad_write_api.py``。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..services import ad_write, common
from .deps import current_user

router = APIRouter(prefix='/api/promotion/write', tags=['promotion'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/actions')
def actions(user=Depends(current_user)):
    """写接口目录（含明确不做的动作与原因）。"""
    return _envelope(ad_write.actions_catalog())


@router.post('/{account_id}/{action}')
def execute(account_id: str, action: str,
            body: Dict[str, Any] = Body(default={}),
            user=Depends(current_user)):
    """执行写动作。``action`` ∈ ``GET /actions`` 的 ``actions[].action``。

    ``confirm`` 严格判定：只有请求体里的 ``"confirm": true`` 才算确认，
    ``1`` / ``"true"`` / 缺省都不算 —— 写操作不接受任何模糊确认。
    """
    payload = body.get('payload') if isinstance(body, dict) else None
    return _envelope(ad_write.execute(account_id, action, payload, body.get('confirm')))
