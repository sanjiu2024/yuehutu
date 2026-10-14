"""IM 会话操作接口（官方闲鱼客服工作台「会话 → 拉黑买家 / 发商品卡片 / 转交 / 结束 /
重新发起」+ 账号级「接待状态（在线 / 小休 / 离线）」）。

为什么单开 `/api/im` 而不并进 `/api/chats`：`/api/chats` 那一族是**消息通道**的只读视图
（会话列表、历史消息、同步），路径里全是 `chats`；这一族是**对某个会话做一件事**
（拉黑 / 解除 / 发卡片 / 转交 / 结束 / 重新发起），协议来源也不同模块（`xianyu/im_actions.py`），
路径分开后 AI 工具与前端一眼能对上，也不会让「消息通道」那组接口混进写操作。

**写操作全部要显式 `confirm=true`**：未确认时一个平台请求都不会发（由
`test_im_actions_api.py` 钉住）。`confirm` **原样**交服务层判（不做 `bool()`）——
`bool('false')` 是 `True`，会把用户明确的拒绝当成同意（第 38 批那条真缺陷的教训）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, im_actions
from .deps import current_user

router = APIRouter(prefix='/api/im', tags=['im'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/blacklist')
def blacklist_status(account_id: str, session_id: str = Query(default=''),
                     conversation_id: str = Query(default=''),
                     user: Any = Depends(current_user)):
    """拉黑状态（只读）。平台没给 `isInBlack` 时 `is_in_black` 是 `null` **+ warning**，不是 `false`。

    `session_id` 是平台会话 id（cid）；会话页手里只有本地会话行 id，所以也可以只传
    `conversation_id`，由服务层用既有的 cid 解析补上（解析不到就 400 `missing_cid`，
    绝不拿买家 id 冒充）。
    """
    return _envelope(im_actions.blacklist_status(account_id, session_id, conversation_id))


@router.post('/{account_id}/blacklist')
def blacklist_add(account_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """拉黑买家（写，`confirm=true` 才发；接口版本 2.0）。"""
    return _envelope(im_actions.blacklist_add(
        account_id, body.get('session_id'), confirm=body.get('confirm'),
        conversation_id=body.get('conversation_id')))


@router.post('/{account_id}/blacklist/remove')
def blacklist_remove(account_id: str, body: Dict[str, Any] = Body(default={}),
                     user: Any = Depends(current_user)):
    """解除拉黑（写，`confirm=true` 才发）。返回里多一个 `deleted`。"""
    return _envelope(im_actions.blacklist_remove(
        account_id, body.get('session_id'), confirm=body.get('confirm'),
        conversation_id=body.get('conversation_id')))


@router.post('/{account_id}/cards')
def send_item_card(account_id: str, body: Dict[str, Any] = Body(default={}),
                   user: Any = Depends(current_user)):
    """给买家发商品卡片（写，`confirm=true` 才发）。"""
    return _envelope(im_actions.send_item_card(
        account_id, body.get('session_id'), body.get('item_id'),
        confirm=body.get('confirm'), conversation_id=body.get('conversation_id')))


# ------------------------------------------------------------------ 客服会话接管族（第 43 批）


@router.post('/{account_id}/sessions/transfer')
def session_deliver(account_id: str, body: Dict[str, Any] = Body(default={}),
                    user: Any = Depends(current_user)):
    """把会话转交给指定客服（写，`confirm=true` 才发）。

    `target_uid` 取自客服列表：`GET /api/shop/{account_id}/cs-routing` 的 `agents[].uid`。
    """
    return _envelope(im_actions.session_deliver(
        account_id, body.get('target_uid'), session_id=body.get('session_id'),
        confirm=body.get('confirm'), conversation_id=body.get('conversation_id')))


@router.post('/{account_id}/sessions/leave')
def session_leave(account_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """结束当前会话（写，`confirm=true` 才发）。官方弹窗文案「确认结束当前会话？」。"""
    return _envelope(im_actions.session_leave(
        account_id, session_id=body.get('session_id'),
        confirm=body.get('confirm'), conversation_id=body.get('conversation_id')))


@router.post('/{account_id}/sessions/rejoin')
def session_rejoin(account_id: str, body: Dict[str, Any] = Body(default={}),
                   user: Any = Depends(current_user)):
    """重新发起聊天（写，`confirm=true` 才发）。回执里带平台的 `data.success`（三态）。"""
    return _envelope(im_actions.session_rejoin(
        account_id, session_id=body.get('session_id'),
        confirm=body.get('confirm'), conversation_id=body.get('conversation_id')))


# ------------------------------------------------------------------ 接待状态（第 46 批）


@router.get('/{account_id}/reception')
def reception_status(account_id: str, user: Any = Depends(current_user)):
    """当前接待状态（只读）：`status` ∈ `ONLINE` / `REST` / `OFFLINE`。

    平台没给（或给了认不出的）状态时 `status` 是 `null` + warning —— 不是「离线」。
    `options` 是官方那三个选项（在线 / 小休 / 离线），界面直接拿它渲染。
    """
    return _envelope(im_actions.reception_status(account_id))


@router.post('/{account_id}/reception')
def set_reception(account_id: str, body: Dict[str, Any] = Body(default={}),
                  user: Any = Depends(current_user)):
    """切换接待状态（写，`confirm=true` 才发；接口名里的 `repect` 是官方拼写）。

    平台没回 `success` 字段时 `saved` 是 `false` + warning（官方界面同样显示「切换失败」）。
    """
    return _envelope(im_actions.set_reception(
        account_id, body.get('status'), confirm=body.get('confirm')))
