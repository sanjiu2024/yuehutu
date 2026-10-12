"""草稿箱路由：列表 / 详情（只读）+ 删除单条 / 批量删除（写）。

协议与口径见 `backend/app/xianyu/draft.py` 与 `backend/app/services/drafts.py`。

**为什么是独立前缀 `/api/drafts`**：草稿箱在官方卖家工作台里不是菜单页，它挂在「商品发布」
流程内。我们的 UI 把它做成「商品」页的第三个 tab（`frontend/src/pages/DraftBox.tsx`），
但接口**不**并进 `/api/items/{account_id}/...` —— 那里 `GET /{account_id}/{item_id}` 会把
任何两段路径的第二段当成商品 ID（`templates` 那次就踩过这个坑），草稿 ID 混进去迟早出事。

**路由顺序**：`POST /{account_id}/batch-delete` 必须声明在 `POST /{account_id}/{draft_id}/delete`
之前 —— 虽然两者段数不同（2 段 vs 3 段）不会真的打架，但保持「集合级动作在前」的写法，
以后给草稿加 `/{draft_id}` 单段动作时不会踩雷。

**写路径**：全部 `Body(default={})` 自由 dict，字段校验在服务层；`confirm` 由调用方显式给
true，服务层第一道闸门就是它（未确认 ⇒ 400 `confirmation_required`，一个平台请求都不发）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, drafts
from .deps import current_user

router = APIRouter(prefix='/api/drafts', tags=['drafts'])


def _envelope(result: Any):
    """统一信封：服务层返回 `{success, data, message, code}`，状态码按 `common.FAIL_STATUS`。"""
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------- 只读


@router.get('/{account_id}')
def list_drafts_for_account(
    account_id: str,
    page: int = Query(default=1, ge=1, description='页码，从 1 起'),
    size: int = Query(default=drafts.DEFAULT_PAGE_SIZE, description='每页条数，官方只支持 10/20/50'),
    user=Depends(current_user),
):
    """草稿列表（`mtop.alibaba.idle.item.pc.draft.search`）。

    平台**没有**返回 `itemSearchResponseList` 时，返回里的 `warnings[]` 会说明
    「这个空列表不等于没有草稿」—— 前端必须把它显示出来，否则等于我们替平台撒了谎。
    """
    return _envelope(drafts.list_drafts(account_id, page=page, size=size))


@router.get('/{account_id}/{draft_id}')
def draft_detail_for_account(account_id: str, draft_id: str, user=Depends(current_user)):
    """草稿详情（`...draft.get`）。

    `fields` 是平台 payload **原样**（字段名没有 bundle 证据 ⇒ 不翻译、不猜）；
    拿不到时是 `null`（不是 `{}`）。
    """
    return _envelope(drafts.draft_detail(account_id, draft_id))


# --------------------------------------------------------------------------- 写（集合级动作在前）


@router.post('/{account_id}/batch-delete')
def batch_delete_drafts(account_id: str, payload: Dict[str, Any] = Body(default={}),
                        user=Depends(current_user)):
    """批量删除草稿：**一次请求删一批**，不可撤销。

    `draft_ids` 两种入参都收（UI 给数组、AI / MCP 给逗号串），服务层统一归一化。
    """
    return _envelope(drafts.batch_delete_drafts(account_id, payload.get('draft_ids'),
                                                confirm=payload.get('confirm')))


@router.post('/{account_id}/{draft_id}/delete')
def delete_draft_for_account(account_id: str, draft_id: str,
                             payload: Dict[str, Any] = Body(default={}),
                             user=Depends(current_user)):
    """删除一件草稿：**会真的删掉闲鱼上的草稿**，不可撤销。"""
    return _envelope(drafts.delete_draft(account_id, draft_id,
                                         confirm=payload.get('confirm')))
