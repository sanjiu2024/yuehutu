"""FastAPI 依赖：登录校验、数据库事务体。"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import Header, HTTPException

from ..core import security


def current_user(authorization: str = Header(default='')) -> Dict[str, Any]:
    token = ''
    if authorization and authorization.lower().startswith('bearer '):
        token = authorization[7:].strip()
    user = security.verify_token(token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail='未登录或会话已过期')
    return user
