"""小铺组（子账号管理 / 客服分流）接口。

官方页面：`/seller-account/sub-account`、`/im-cs-dispatch/customer-routing-service`。

**只读 + 写**：子账号管理仍是只读；客服分流页复刻了官方的三个写能力 ——
保存分流规则、新建/修改客服分组、删除客服分组（见 `backend/app/xianyu/shop_write.py`
与 `docs/contracts/shop.md`）。三个写接口**都要显式 `confirm=true`**，
未确认时一个平台请求都不会发（由 `test_shop_write_api.py` 钉住）。

`confirm` **原样**交给服务层判（不做 `bool()`）—— `bool('false')` 是 `True`，
会把用户明确的拒绝当成同意。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, shop, shop_profile as shop_profile_service
from .deps import current_user

router = APIRouter(prefix='/api/shop', tags=['shop'])


def _envelope(result: Any):
    """服务层结果 → HTTP。成功 200 直接给信封体，失败按 `code` 映射状态码。"""
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/sub-accounts')
def sub_accounts(account_id: str, page: str = Query(default=''),
                 size: str = Query(default=''), user: Any = Depends(current_user)):
    """子账号列表（只读）。`page`/`size` 是平台 `pageNo`/`pageSize`。"""
    return _envelope(shop.sub_accounts(account_id, page=page, size=size))


@router.get('/{account_id}/cs-routing')
def cs_routing(account_id: str, page: str = Query(default=''),
               size: str = Query(default=''), user: Any = Depends(current_user)):
    """客服分流配置（只读）。`page`/`size` 只作用于客服分组。"""
    return _envelope(shop.cs_routing(account_id, page=page, size=size))


@router.post('/{account_id}/cs-routing/rule')
def save_cs_routing_rule(account_id: str, body: Dict[str, Any] = Body(default={}),
                         user: Any = Depends(current_user)):
    """保存分流规则（写，`confirm=true` 才发）。会真的改动账号的客服分流配置。"""
    return _envelope(shop.save_routing_rule(
        account_id,
        distribution_method=body.get('distribution_method'),
        repeat_customer=body.get('repeat_customer'),
        offline_message=body.get('offline_message'),
        specified_account_id=body.get('specified_account_id'),
        specified_account_nick=body.get('specified_account_nick'),
        service_time=body.get('service_time'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/cs-groups')
def save_cs_group(account_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """新建 / 修改客服分组（写，`confirm=true` 才发）。"""
    return _envelope(shop.save_cs_group(
        account_id,
        group_name=body.get('group_name'),
        mode=body.get('mode'),
        operate_type=body.get('operate_type') or 'create',
        group_id=body.get('group_id'),
        specified_type=body.get('specified_type'),
        specified_type_name=body.get('specified_type_name'),
        type_data=body.get('type_data'),
        customer_uids=body.get('customer_uids'),
        confirm=body.get('confirm'),
    ))


@router.post('/{account_id}/cs-groups/delete')
def delete_cs_group(account_id: str, body: Dict[str, Any] = Body(default={}),
                    user: Any = Depends(current_user)):
    """删除客服分组（写，`confirm=true` 才发）。**不可撤销。**"""
    return _envelope(shop.delete_cs_group(
        account_id, group_id=body.get('group_id'), confirm=body.get('confirm')))


# ------------------------------------------------------------------ 店铺资料


@router.get('/{account_id}/profile')
def shop_profile(account_id: str, user: Any = Depends(current_user)):
    """店铺资料（只读）：昵称 / 简介 / 头像 / 行业 / 支付宝账号 / 认证到期时间。

    平台没返回的字段一律 `null`（前端显示「-」），不补 0。
    """
    return _envelope(shop_profile_service.get_profile(account_id))


@router.post('/{account_id}/profile/nick')
def update_shop_nick(account_id: str, body: Dict[str, Any] = Body(default={}),
                     user: Any = Depends(current_user)):
    """改店铺昵称（写，`confirm=true` 才发）。官方限制 4~12 个字符。"""
    return _envelope(shop_profile_service.update_nick(
        account_id, nick=body.get('nick'), confirm=body.get('confirm')))


@router.post('/{account_id}/profile/desc')
def update_shop_desc(account_id: str, body: Dict[str, Any] = Body(default={}),
                     user: Any = Depends(current_user)):
    """改店铺简介（写，`confirm=true` 才发）。"""
    return _envelope(shop_profile_service.update_desc(
        account_id, desc=body.get('desc'), confirm=body.get('confirm')))


@router.post('/{account_id}/profile/avatar')
def update_shop_avatar(account_id: str, body: Dict[str, Any] = Body(default={}),
                       user: Any = Depends(current_user)):
    """改店铺头像（写，`confirm=true` 才发）。传的是图片 URL，不是二进制。"""
    return _envelope(shop_profile_service.update_avatar(
        account_id, avatar_url=body.get('avatar_url'), confirm=body.get('confirm')))


# --------------------------------------------------------------------------- READ-COVERAGE-61
@router.get('/{account_id}/pc-service-status')
def pc_service_status(account_id: str, service_code: str = Query(default=''),
                      user: Any = Depends(current_user)):
    """发布页服务开通状态（只读）。service_code 必填；平台响应原样透出，不补 false。"""
    return _envelope(shop.pc_service_status(account_id, service_code))
