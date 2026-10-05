"""账号管理路由：列表 / 扫码登录 / 人工验证 / 修改 / 删除。

路径与方法**严格按 ``docs/CONTRACTS.md`` §3 冻结的清单**（前端已按那份契约写死）：

    GET    /api/accounts              -> [{id,nickname,enabled,auto_reply,login_state,runtime_state,remark}]
    POST   /api/accounts/qr/start     -> {ticket,qr_image_b64,expires_in}
    GET    /api/accounts/qr/{ticket}  -> {state:pending|scanned|confirmed|expired|failed,account_id?}
    PATCH  /api/accounts/{id}         {enabled?,auto_reply?,remark?}
    DELETE /api/accounts/{id}
    POST   /api/accounts/{id}/relogin -> 触发重新扫码/续期
    POST   /api/accounts/{id}/captcha -> 打开人工验证浏览器窗口（滑块）

早先按 task 描述写的 ``POST /api/accounts/qr/poll`` 与
``POST /api/accounts/{id}/toggle`` 保留为**别名**（不删，避免任何已写好的调用
断掉），但契约名才是前端实际在用的，必须存在。

约定：
- 全部需要 ``Authorization: Bearer <token>``（走 ``deps.current_user``）；
- 成功 ``{"success": true, "data": {...}}``，失败由 ``main.py`` 的异常处理器
  统一套成 ``{"success": false, "message": ..., "code": ...}``；
- **列表接口不返回 Cookie 全文**。Cookie 是登录凭证，一旦随列表下发就会被
  前端缓存、被日志记下、被截图带走；只回 ``has_cookie`` / ``has_x5sec``。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from ..core import events
from ..xianyu import login as qr_login
from ..xianyu import risk as risk_mod
from ..xianyu import store
from .deps import current_user

router = APIRouter(prefix='/api/accounts', tags=['accounts'])


def _publish(topic: str, payload: Dict[str, Any]) -> None:
    """账号变更广播。前端账号页/会话页靠它实时刷新。

    推送失败绝不能影响已经写库的变更，所以整体吞异常。
    """
    try:
        events.publish(topic, payload)
    except Exception:  # pragma: no cover
        pass


class PollPayload(BaseModel):
    session_id: str


class CaptchaPayload(BaseModel):
    headless: bool = False


class TogglePayload(BaseModel):
    enabled: bool


# 内部登录状态 → 契约 §3 的 state 枚举。
# 注意 verification_required 映射成 scanned 而不是 failed：它需要用户继续在
# 手机/浏览器上操作，判 failed 会让前端立刻停止轮询，用户就再也等不到成功。
_STATE_MAP = {
    'waiting': 'pending',
    'scanned': 'scanned',
    'verification_required': 'scanned',
    'success': 'confirmed',
    'expired': 'expired',
    'cancelled': 'failed',
}


def _to_contract_state(payload: Dict[str, Any]) -> Dict[str, Any]:
    """把 ``login.poll`` 的返回翻译成契约里的 QR 状态结构。"""
    internal = payload.get('status') or ''
    state = _STATE_MAP.get(internal, 'failed')
    out: Dict[str, Any] = {'state': state}
    if payload.get('user_id'):
        out['account_id'] = payload['user_id']
    if payload.get('message'):
        out['message'] = payload['message']
    return out


def _runtime_state(account: Dict[str, Any]) -> str:
    """运行态（前端 ``runtimeStateLabel`` 认 running/stopped/risk）。

    ``runtime_state`` **不是库里的列** —— 它是「是否启用」+「是否在风控冷却中」
    的派生值，实时算出来比落库更准（风控冷却到点就自动解除，落库反而要额外的
    定时任务去刷）。
    """
    if account.get('blocked'):
        return 'risk'
    return 'running' if account.get('enabled') else 'stopped'


def _decorate(account: Dict[str, Any]) -> Dict[str, Any]:
    state = risk_mod.registry.get(account['id'])
    account['blocked'] = state.is_blocked
    account['remaining_seconds'] = state.remaining_seconds
    account['consecutive_hits'] = state.consecutive_hits
    account['runtime_state'] = _runtime_state(account)
    return account


def _not_found(account_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f'账号不存在: {account_id}')


@router.get('')
def list_accounts(user=Depends(current_user)) -> Dict[str, Any]:
    """账号列表（含登录态、运行态与风控冷却，不含 Cookie 原文）。"""
    return {'success': True, 'data': [_decorate(a) for a in store.list_accounts()]}


# ---------------------------------------------------------------------------
# 扫码登录。**必须注册在 /{account_id}/... 之前**：`/api/accounts/qr/{ticket}`
# 与 `/api/accounts/{account_id}/status` 的形状相同，FastAPI 按注册顺序匹配，
# 反过来的话 `/api/accounts/qr/xxx` 会被当成 account_id='qr' 的 status 请求。
# ---------------------------------------------------------------------------

@router.post('/qr/start')
async def start_qr_login(user=Depends(current_user)) -> Dict[str, Any]:
    """开一次扫码会话，返回二维码（base64 PNG）。"""
    try:
        data = await qr_login.start_qr_login()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'获取二维码失败: {exc}')
    return {'success': True, 'data': {
        'ticket': data.get('session_id') or '',
        'qr_image_b64': data.get('qr_image_base64') or '',
        'expires_in': int(data.get('expire_in') or 0),
    }}


async def _poll_ticket(ticket: str) -> Dict[str, Any]:
    """轮询一次并把成功结果落库。"""
    try:
        data = await qr_login.poll_qr_login(ticket)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'轮询登录状态失败: {exc}')

    if data.get('status') == 'success':
        cookie = data.get('cookie') or ''
        user_id = data.get('user_id') or ''
        # 没有 unb 就没有账号主键，绝不能拿一个空 id 建账号。
        if not user_id:
            raise HTTPException(status_code=502, detail='登录成功但未取得账号 ID (unb)')
        store.upsert_account(
            account_id=user_id,
            nickname=data.get('nickname') or '',
            avatar_url=data.get('avatar_url') or '',
            xianyu_user_id=user_id,
            cookie=cookie,
            login_state=store.STATE_ACTIVE,
        )
        store.log_operation(user_id, 'qr_login', success=True)
        _publish('accounts.changed', {'account_id': user_id, 'reason': 'qr_login'})
    return _to_contract_state(data)


@router.get('/qr/{ticket}')
async def poll_qr_login(ticket: str, user=Depends(current_user)) -> Dict[str, Any]:
    """轮询扫码状态（契约名）。``state == 'confirmed'`` 时**落库并返回 account_id**。"""
    return {'success': True, 'data': await _poll_ticket(ticket)}


@router.post('/qr/poll')
async def poll_qr_login_alias(payload: PollPayload,
                              user=Depends(current_user)) -> Dict[str, Any]:
    """旧名别名（task 描述里的写法）。保留以免已有调用断掉。"""
    return {'success': True, 'data': await _poll_ticket(payload.session_id)}


# ---------------------------------------------------------------------------
# 单个账号
# ---------------------------------------------------------------------------

@router.get('/{account_id}/status')
async def account_status(account_id: str, user=Depends(current_user)) -> Dict[str, Any]:
    """账号状态：登录态 + 是否持有 x5sec + 风控冷却。"""
    account = store.get_account(account_id)
    if account is None:
        raise _not_found(account_id)
    return {'success': True, 'data': _decorate(account)}


@router.patch('/{account_id}')
def patch_account(account_id: str, payload: Dict[str, Any] = Body(...),
                  user=Depends(current_user)) -> Dict[str, Any]:
    """改开关与备注（契约名）。只更新传进来的字段。"""
    if store.get_account(account_id) is None:
        raise _not_found(account_id)

    unknown = set(payload) - {'enabled', 'auto_reply', 'remark'}
    if unknown:
        # 静默忽略拼错的字段会让前端以为改成功了，实际没生效。
        raise HTTPException(status_code=400, detail=f'不支持的字段: {sorted(unknown)}')

    store.update_account(
        account_id,
        enabled=payload.get('enabled'),
        auto_reply=payload.get('auto_reply'),
        remark=payload.get('remark'),
    )
    store.log_operation(account_id, 'patch', success=True, detail=payload)
    _publish('accounts.changed', {'account_id': account_id, 'patch': payload})
    account = store.get_account(account_id)
    return {'success': True, 'data': _decorate(account) if account else {'id': account_id}}


@router.post('/{account_id}/toggle')
def toggle_account(account_id: str, payload: TogglePayload,
                   user=Depends(current_user)) -> Dict[str, Any]:
    """旧名别名（task 描述里的写法）→ 等价于 ``PATCH`` 只带 enabled。"""
    if store.get_account(account_id) is None:
        raise _not_found(account_id)
    store.update_account(account_id, enabled=payload.enabled)
    store.log_operation(account_id, 'toggle', success=True, detail=str(payload.enabled))
    _publish('accounts.changed', {'account_id': account_id, 'enabled': bool(payload.enabled)})
    return {'success': True, 'data': {'id': account_id, 'enabled': bool(payload.enabled)}}


@router.post('/{account_id}/relogin')
async def relogin_account(account_id: str, user=Depends(current_user)) -> Dict[str, Any]:
    """触发重新扫码。

    两件事一起做：① 把登录态标成 ``need_relogin`` 并清掉旧 Cookie —— 否则旧
    Cookie 还「看着可用」，下次调用又会拿它去撞一次风控；② 立刻开一个新的扫码
    会话，把 ticket 与二维码直接回给前端，用户不用再点一次「扫码添加账号」。
    """
    if store.get_account(account_id) is None:
        raise _not_found(account_id)
    store.set_cookie(account_id, '', xianyu_user_id=account_id,
                     login_state=store.STATE_NEED_RELOGIN)
    store.mark_need_relogin(account_id, '用户手动触发重新登录')
    risk_mod.registry.reset(account_id)
    store.log_operation(account_id, 'relogin', success=True)
    try:
        data = await qr_login.start_qr_login()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'已置为需重新登录，但获取二维码失败: {exc}')
    return {'success': True, 'data': {
        'id': account_id,
        'login_state': store.STATE_NEED_RELOGIN,
        'ticket': data.get('session_id') or '',
        'qr_image_b64': data.get('qr_image_base64') or '',
        'expires_in': int(data.get('expire_in') or 0),
    }}


@router.post('/{account_id}/captcha')
async def captcha_account(account_id: str, payload: Optional[CaptchaPayload] = None,
                          user=Depends(current_user)) -> Dict[str, Any]:
    """打开人工验证浏览器窗口（滑块）。

    **立刻返回，不等用户划完**：``manual_verify`` 最长会阻塞 5 分钟（等人操作），
    同步等它会让 HTTP 请求超时、前端转圈卡死。这里后台起一个任务，用户关掉
    浏览器后结果自动落库。

    成功判据仍然是**拿回 ``x5sec``**；没拿到就只记一条失败日志、不覆盖旧
    Cookie —— 绝不把「没通过风控的 Cookie」写回库里。
    """
    cookie = store.get_cookie(account_id)
    if not cookie:
        raise _not_found(account_id)

    headless = bool(payload.headless) if payload else False
    store.log_operation(account_id, 'captcha_open', success=True, detail=f'headless={headless}')

    async def _run() -> None:
        try:
            result = await qr_login.manual_verify(account_id, cookie, headless=headless)
        except Exception as exc:  # 后台任务里抛异常会静默丢失，必须自己记
            store.log_operation(account_id, 'manual_verify', success=False, detail=str(exc))
            return
        if result.get('ok'):
            store.set_cookie(account_id, result.get('cookie') or cookie,
                             xianyu_user_id=account_id, login_state=store.STATE_ACTIVE)
            risk_mod.registry.reset(account_id)
            store.log_operation(account_id, 'manual_verify', success=True)
        else:
            store.log_operation(account_id, 'manual_verify', success=False,
                                detail=result.get('message') or '人工验证失败')

    asyncio.create_task(_run())
    return {'success': True, 'data': {
        'id': account_id,
        'started': True,
        'message': '已打开人工验证窗口，请在弹出的浏览器里完成滑块；完成后凭证会自动保存',
    }}


@router.delete('/{account_id}')
def delete_account(account_id: str, user=Depends(current_user)) -> Dict[str, Any]:
    """删除账号（连带凭证与风控记录）。"""
    if not store.delete_account(account_id):
        raise _not_found(account_id)
    risk_mod.registry.reset(account_id)
    _publish('accounts.changed', {'account_id': account_id, 'deleted': True})
    return {'success': True, 'data': {'id': account_id, 'deleted': True}}
