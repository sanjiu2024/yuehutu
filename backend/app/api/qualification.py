"""资质管理 HTTP 接口（超级擦亮）。

5 条只读 GET + 2 条写 POST。**写接口的 ``confirm`` 必须在请求体里显式为 ``true``**
（``{"confirm": true, "request": {...}}``），否则 400 ``confirm_required`` —— 服务层在
碰平台、碰库之前就拦下（见 `services/qualification.py::submit_packages`）。

``request`` 是平台的业务对象，字段名**没有 bundle 证据**，所以本层原样透传，不设默认值：
凭空补键 = 编平台协议。字段口径与证据见 ``docs/contracts/qualification.md``。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import common, qualification
from .deps import current_user

router = APIRouter(prefix='/api/qualification', tags=['qualification'])


class SubmitPayload(BaseModel):
    """提交资质。``confirm`` 必须显式为 ``true`` 才会真发。"""
    confirm: bool = False
    request: Optional[Dict[str, Any]] = None


class CredentialPayload(BaseModel):
    """OSS 上传凭证。同样必须显式确认。"""
    confirm: bool = False
    request: Optional[Dict[str, Any]] = None


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/detail')
def detail(account_id: str, user=Depends(current_user)):
    return _envelope(qualification.detail(account_id))


@router.get('/{account_id}/submitted-records')
def submitted_records(account_id: str, page: str = Query('', description='页码，默认 1'),
                      size: str = Query('', description='每页条数 1~50，默认 20'),
                      user=Depends(current_user)):
    return _envelope(qualification.submitted_records(account_id, page, size))


@router.get('/{account_id}/categories')
def categories(account_id: str, user=Depends(current_user)):
    return _envelope(qualification.categories(account_id))


@router.get('/{account_id}/templates')
def templates(account_id: str, user=Depends(current_user)):
    return _envelope(qualification.templates(account_id))


@router.get('/{account_id}/reusable')
def reusable(account_id: str, user=Depends(current_user)):
    return _envelope(qualification.reusable(account_id))


@router.post('/{account_id}/packages/submit')
def submit_packages(account_id: str, payload: SubmitPayload, user=Depends(current_user)):
    return _envelope(qualification.submit_packages(
        account_id, payload.request, payload.confirm))


@router.post('/{account_id}/oss-credential')
def oss_credential(account_id: str, payload: CredentialPayload, user=Depends(current_user)):
    return _envelope(qualification.oss_credential(
        account_id, payload.request, payload.confirm))
