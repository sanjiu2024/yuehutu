"""话术接口（官方 IM 工作台的「快捷回复」：分组 + 短语，读 + 写）。

为什么单开 `/api/quick-reply` 而不并进 `/api/chats`：`/api/chats` 承载的是会话与消息
（`conversations` / `messages` 两张本地表），而话术是**平台侧配置**（`mtop.taobao.idlemessage.
quickreply.*`），跟本地会话数据没有关系；路径分开，AI 工具与前端一眼能对上。

**写操作全部要显式 `confirm=true`**：未确认时一个平台请求都不会发（由
`test_quick_reply_api.py` 钉住）。`confirm` **原样**交服务层判（不做 `bool()`）——
`bool('false')` 是 `True`，会把用户明确的拒绝当成同意。

官方两个上限照抄：分组名 ≤10 字（输入框 `maxLength:10`）、短语内容 ≤200 字
（`maxLength:200` 且 onChange 里再 `slice(0,200)`）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..services import common, quick_reply
from .deps import current_user

router = APIRouter(prefix='/api/quick-reply', tags=['quick-reply'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/groups')
def list_groups(account_id: str, user: Any = Depends(current_user)):
    """话术列表（只读）。平台没给 `groups` 键时是 `null` + warning，不是 `[]`。"""
    return _envelope(quick_reply.list_groups(account_id))


@router.post('/{account_id}/groups')
def create_group(account_id: str, body: Dict[str, Any] = Body(default={}),
                 user: Any = Depends(current_user)):
    """新建分组（写，`confirm=true` 才发）。分组名 ≤10 字。"""
    return _envelope(quick_reply.create_group(
        account_id, body.get('group_name'), confirm=body.get('confirm')))


@router.post('/{account_id}/groups/{group_id}')
def update_group(account_id: str, group_id: str, body: Dict[str, Any] = Body(default={}),
                 user: Any = Depends(current_user)):
    """改分组名（写，`confirm=true` 才发）。"""
    return _envelope(quick_reply.update_group(
        account_id, group_id, body.get('group_name'), confirm=body.get('confirm')))


@router.post('/{account_id}/groups/{group_id}/delete')
def delete_group(account_id: str, group_id: str, body: Dict[str, Any] = Body(default={}),
                 user: Any = Depends(current_user)):
    """删除分组（写，`confirm=true` 才发）。官方提示「是否要删除所筛选分组及分组内的快捷短语？」——**不可撤销**。"""
    return _envelope(quick_reply.delete_group(
        account_id, group_id, confirm=body.get('confirm')))


@router.post('/{account_id}/phrases')
def create_phrase(account_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """新建短语（写，`confirm=true` 才发）。内容 ≤200 字。"""
    return _envelope(quick_reply.create_phrase(
        account_id, body.get('group_id'), body.get('content'), confirm=body.get('confirm')))


@router.post('/{account_id}/phrases/{content_id}')
def update_phrase(account_id: str, content_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """改短语（写，`confirm=true` 才发）。`group_id` 必填（官方 payload 里有它）。"""
    return _envelope(quick_reply.update_phrase(
        account_id, body.get('group_id'), content_id, body.get('content'),
        confirm=body.get('confirm')))


@router.post('/{account_id}/phrases/{content_id}/delete')
def delete_phrase(account_id: str, content_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """删除短语（写，`confirm=true` 才发）。**不可撤销。**"""
    return _envelope(quick_reply.delete_phrase(
        account_id, body.get('group_id'), content_id, confirm=body.get('confirm')))
