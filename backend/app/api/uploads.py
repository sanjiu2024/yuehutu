"""图片上传路由（薄层：multipart 拆包 + 信封 + 本地图片回显）。

契约（task-23 定，Lead 写进 docs/CONTRACTS.md）：

- `POST /api/uploads/images` — `multipart/form-data`：`file`（必填，单张）+
  可选 `account_id`。成功 200 →
  `{"success": true, "data": {"url", "file_id", "width", "height", "name", "local_path"}}`；
  失败 4xx/5xx → `{"success": false, "message": ..., "code": ...}`
  （协议层不可用 `xianyu_not_ready` / 503，Cookie 失效 `verification_required` / 409）。
- `GET /api/uploads/local/{name}` — 把落盘的原图回给前端做预览；名字先过
  `services.uploads.local_path_for` 的白名单与真实路径判等，穿越一律 404。

鉴权：POST 走 `deps.current_user`（Bearer 头）。GET 是给 `<img src>` 用的 —— 浏览器
不会给 `<img>` 带 `Authorization`，所以这里**额外接受 `?token=<jwt>`**；两种都没有 → 401。

路由挂载在 `backend/app/main.py` 的 `ROUTE_MODULES`（由 Lead 维护），本文件不自挂。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..core import security
from ..services import common, uploads
from .deps import current_user

router = APIRouter(prefix='/api/uploads', tags=['uploads'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


def _image_user(authorization: str = Header(default=''), token: str = Query(default='')) -> Any:
    """回显路由的鉴权：Bearer 头优先，退回 `?token=`（`<img src>` 带不了头）。"""
    raw = ''
    if authorization and authorization.lower().startswith('bearer '):
        raw = authorization[7:].strip()
    if not raw:
        raw = str(token or '').strip()
    user = security.verify_token(raw) if raw else None
    if user is None:
        raise HTTPException(status_code=401, detail='未登录或会话已过期')
    return user


@router.post('/images')
def upload_image(
    file: UploadFile = File(...),
    account_id: str = Form(default=''),
    user=Depends(current_user),
):
    """拖进来的本地图片 → 闲鱼图床 URL。

    同步 `def`（不是 `async def`）：服务层会在共享事件循环上同步等协议层，
    FastAPI 把同步路由丢进线程池，才不会把主事件循环整个堵住。
    """
    raw = b''
    try:
        raw = file.file.read()
    except Exception:  # noqa: BLE001 - 读不出内容按空文件处理（下面给 invalid_fields）
        raw = b''
    result = uploads.handle_upload(raw, file.filename or '', account_id=account_id)
    return _envelope(result)


@router.get('/local/{name}')
def local_image(name: str, user=Depends(_image_user)):
    """把本地保存的图片回给前端做预览（防路径穿越，见 `services.uploads.local_path_for`）。"""
    path = uploads.local_path_for(name)
    if path is None:
        body = common.fail(f'本地图片不存在: {name}', 'not_found')
        return JSONResponse(status_code=404, content=body)
    return FileResponse(
        str(path),
        media_type=uploads.content_type_for(path.name),
        headers={'Cache-Control': 'private, max-age=3600'},
    )
