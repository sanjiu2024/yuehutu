"""登录 / 登出 / 当前用户。桌面外壳用 local_secret 自动登录，用户不用输密码。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from ..core import config, security
from .deps import current_user

router = APIRouter(prefix='/api/auth', tags=['auth'])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post('/login')
def login(body: LoginIn):
    user = security.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail='用户名或密码不正确')
    token = security.issue_token(user['id'])
    security.write_mcp_token(token)
    return {'success': True, 'data': {'token': token, 'user': user}}


@router.post('/desktop')
def desktop_login(x_desktop_secret: str = Header(default='')):
    """桌面外壳专用：持有本地密钥即可换 token。"""
    if not x_desktop_secret or x_desktop_secret != security.local_secret():
        raise HTTPException(status_code=401, detail='本地密钥不正确')
    row = security.ensure_admin()
    user = security.authenticate(row[0], row[1])
    if user is None:
        raise HTTPException(status_code=500, detail='管理员账号异常')
    token = security.issue_token(user['id'])
    security.write_mcp_token(token)
    return {'success': True, 'data': {'token': token, 'user': user}}


@router.post('/logout')
def logout(authorization: str = Header(default='')):
    token = authorization[7:].strip() if authorization.lower().startswith('bearer ') else ''
    if token:
        security.delete_token(token)
    return {'success': True}


@router.get('/me')
def me(user=Depends(current_user)):
    return {'success': True, 'data': user}
