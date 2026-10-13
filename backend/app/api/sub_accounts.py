"""子账号管理接口（岗位只读 + 新建 / 删除 / 改手机号 / 改岗位 / 改密码）。

为什么单开 `/api/sub-accounts` 而不并进 `/api/shop`：官方这是**另一个微应用**
（账号管理 `idle-seller-account-management`，菜单里的「子账号管理」），而 `/api/shop`
已经承载「客服分流 + 店铺资料」两个微应用；路径上分开，AI 工具与前端一眼能对上。

**写操作全部要显式 `confirm=true`**：未确认时一个平台请求都不会发（由
`test_sub_accounts_api.py` 钉住）。`confirm` **原样**交服务层判（不做 `bool()`）——
`bool('false')` 是 `True`，会把用户明确的拒绝当成同意。

**改密码的明文只到本机后端**：前端 POST 明文，服务端用平台公钥（`sub.account.sub.pk`）
做 RSAES-PKCS1-v1_5 加密后提交；日志、返回值、事件里都不带密码。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..services import common, sub_accounts
from .deps import current_user

router = APIRouter(prefix='/api/sub-accounts', tags=['sub-accounts'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/roles')
def list_roles(account_id: str, user: Any = Depends(current_user)):
    """岗位列表（只读）。平台没给数组时 `roles` 是 `null` + warning，不是 `[]`。"""
    return _envelope(sub_accounts.list_roles(account_id))


@router.post('/{account_id}')
def create_sub_account(account_id: str, body: Dict[str, Any] = Body(default={}),
                       user: Any = Depends(current_user)):
    """新建子账号（写，`confirm=true` 才发）。密码在本机加密后提交。"""
    return _envelope(sub_accounts.create(
        account_id,
        nick=body.get('nick'),
        mobile=body.get('mobile'),
        role_ids=body.get('role_ids'),
        password=body.get('password'),
        custom_name=body.get('custom_name'),
        ext=body.get('ext'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/{sub_id}/delete')
def delete_sub_account(account_id: str, sub_id: str, body: Dict[str, Any] = Body(default={}),
                       user: Any = Depends(current_user)):
    """删除子账号（写，`confirm=true` 才发）。**不可撤销。**"""
    return _envelope(sub_accounts.delete(account_id, sub_id, confirm=body.get('confirm')))


@router.post('/{account_id}/{sub_id}/mobile')
def update_sub_account_mobile(account_id: str, sub_id: str,
                              body: Dict[str, Any] = Body(default={}),
                              user: Any = Depends(current_user)):
    """改子账号手机号（写，`confirm=true` 才发）。官方要求 11 位大陆手机号。"""
    return _envelope(sub_accounts.update_mobile(
        account_id, sub_id, body.get('mobile'), confirm=body.get('confirm')))


@router.post('/{account_id}/{sub_id}/roles')
def update_sub_account_roles(account_id: str, sub_id: str,
                             body: Dict[str, Any] = Body(default={}),
                             user: Any = Depends(current_user)):
    """改子账号岗位（写，`confirm=true` 才发）。`role_ids` 是整数数组。"""
    return _envelope(sub_accounts.update_roles(
        account_id, sub_id, body.get('role_ids'), confirm=body.get('confirm')))


@router.post('/{account_id}/{sub_id}/password')
def update_sub_account_password(account_id: str, sub_id: str,
                                body: Dict[str, Any] = Body(default={}),
                                user: Any = Depends(current_user)):
    """改子账号密码（写，`confirm=true` 才发）。

    官方要求 8~15 位字母+数字组合。密码明文只在本机进程内出现。
    """
    return _envelope(sub_accounts.update_password(
        account_id, sub_id, body.get('password'), confirm=body.get('confirm')))
