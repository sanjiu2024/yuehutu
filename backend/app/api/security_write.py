# -*- coding: utf-8 -*-
"""安全中心写接口的 HTTP 层（申诉表单 / 提交申诉 / 催办）。

前缀沿用只读安全中心的 `/api/security-center`（`api/security_center.py:7`）。
body 用 `Dict[str, Any] = Body(default={})` 收，校验交给服务层出统一信封；
用 FastAPI 的必填/ge 会先返回 422 裸 body，破坏 `{success, code, message}` 约定。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..services import common, security_write
from .deps import current_user

router = APIRouter(prefix='/api/security-center', tags=['security-center'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.post('/{account_id}/appeal/form')
def appeal_form(account_id: str,
                body: Dict[str, Any] = Body(default={}, description='{form_id}'),
                user=Depends(current_user)):
    """获取申诉表单。**只读**，不需要 confirm。"""
    return _envelope(security_write.appeal_form(account_id, body.get('form_id')))


@router.post('/{account_id}/appeal/submit')
def appeal_submit(account_id: str,
                  body: Dict[str, Any] = Body(default={}, description='{parameter, confirm}'),
                  user=Depends(current_user)):
    """提交申诉（安全中心 pc 弹窗）。需要 `confirm: true`。"""
    return _envelope(security_write.appeal_submit(
        account_id, body.get('parameter'), confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/appeal/cro/submit')
def appeal_submit_cro(account_id: str,
                      body: Dict[str, Any] = Body(default={}, description='{punish_id, proof_items, confirm}'),
                      user=Depends(current_user)):
    """提交申诉（复核 cro 流程）。需要 `confirm: true`。"""
    return _envelope(security_write.appeal_submit_cro(
        account_id, body.get('punish_id'), body.get('proof_items'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/appeal/urge')
def appeal_urge(account_id: str,
                body: Dict[str, Any] = Body(default={}, description='{punish_id, confirm}'),
                user=Depends(current_user)):
    """申诉催办（加急处理）。需要 `confirm: true`。"""
    return _envelope(security_write.appeal_urge(
        account_id, body.get('punish_id'), confirm=body.get('confirm'),
    ))
