"""多 AI 编排的路由：角色、提示词、AI 之间沟通记录、主管 AI 对话。

契约见 `docs/CONTRACTS.md` §3 / §4。薄层：只做参数校验与调度，逻辑在 `backend/app/agents/`。

    GET    /api/agents/roles
    GET    /api/agents/roles/{role}
    PUT    /api/agents/roles/{role}
    GET    /api/agents/prompts
    POST   /api/agents/prompts
    PUT    /api/agents/prompts/{prompt_id}
    DELETE /api/agents/prompts/{prompt_id}
    GET    /api/agents/messages      AI 之间的沟通记录（前端「AI 沟通」页）
    GET    /api/agents/runs          调用用量/耗时
    POST   /api/agents/master/chat   和主管 AI 对话
    POST   /api/agents/reply         内部：给一条买家消息生成 AI 回复（不发送）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..agents import llm, prompts as prompts_mod, roles as roles_mod, runtime
from ..core import db
from .deps import current_user

router = APIRouter(prefix='/api/agents', tags=['agents'])


# --------------------------------------------------------------------------
# 角色
# --------------------------------------------------------------------------

@router.get('/roles')
def list_roles(user=Depends(current_user)):
    return {'success': True, 'data': roles_mod.list_roles()}


@router.get('/roles/{role}')
def get_role(role: str, user=Depends(current_user)):
    try:
        return {'success': True, 'data': roles_mod.get_role(role)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put('/roles/{role}')
def update_role(role: str, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    fields: Dict[str, Any] = {}
    for key in ('provider_id', 'model', 'temperature', 'system_prompt', 'enabled'):
        if key in payload and payload[key] is not None:
            fields[key] = payload[key]
    if 'provider_id' in fields and fields['provider_id'] not in ('', None):
        if db.query_one('SELECT id FROM providers WHERE id = ?', (int(fields['provider_id']),)) is None:
            raise HTTPException(status_code=400, detail=f'提供者 #{fields["provider_id"]} 不存在')
        fields['provider_id'] = int(fields['provider_id'])
    elif 'provider_id' in fields:
        fields['provider_id'] = None
    if 'temperature' in fields:
        try:
            fields['temperature'] = float(fields['temperature'])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='temperature 必须是数字')
    try:
        data = roles_mod.update_role(role, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {'success': True, 'data': data}


# --------------------------------------------------------------------------
# 提示词（三级覆盖）
# --------------------------------------------------------------------------

@router.get('/prompts')
def list_prompts(
    scope: Optional[str] = Query(default=None),
    owner_id: Optional[str] = Query(default=None),
    role: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    if scope and scope not in prompts_mod.SCOPES:
        raise HTTPException(status_code=400, detail=f'scope 只能是 {" / ".join(prompts_mod.SCOPES)}')
    data = prompts_mod.list_prompts(scope=scope, owner_id=owner_id, role=role)
    return {'success': True, 'data': {'items': data, 'scopes': list(prompts_mod.SCOPES),
                                      'roles': list(roles_mod.ROLES)}}


@router.post('/prompts')
def create_prompt(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    scope = str(payload.get('scope') or 'global')
    owner_id = payload.get('item_id') or payload.get('account_id') or payload.get('owner_id')
    try:
        data = prompts_mod.upsert_prompt(
            scope,
            owner_id=owner_id,
            content=str(payload.get('content') or ''),
            priority=int(payload.get('priority') or 0),
            role=str(payload.get('role') or ''),
            name=str(payload.get('name') or ''),
            enabled=bool(payload.get('enabled', True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {'success': True, 'data': data}


@router.put('/prompts/{prompt_id}')
def update_prompt(prompt_id: int, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    current = prompts_mod.get_prompt(prompt_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f'提示词 #{prompt_id} 不存在')
    scope = str(payload.get('scope') or current['scope'])
    owner_id = payload.get('item_id') or payload.get('account_id') or payload.get('owner_id')
    if owner_id is None:
        owner_id = current['item_id'] or current['account_id']
    try:
        data = prompts_mod.upsert_prompt(
            scope,
            owner_id=owner_id,
            content=str(payload.get('content', current['content']) or ''),
            priority=int(payload.get('priority', current['priority']) or 0),
            role=str(payload.get('role', current['role']) or ''),
            name=str(payload.get('name', current['name']) or ''),
            enabled=bool(payload.get('enabled', current['enabled'])),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if data['id'] != int(prompt_id):
        prompts_mod.delete_prompt(prompt_id)  # 改了 scope/owner 导致换了行，清掉旧的
    return {'success': True, 'data': data}


@router.delete('/prompts/{prompt_id}')
def delete_prompt(prompt_id: int, user=Depends(current_user)):
    if prompts_mod.get_prompt(prompt_id) is None:
        raise HTTPException(status_code=404, detail=f'提示词 #{prompt_id} 不存在')
    prompts_mod.delete_prompt(prompt_id)
    return {'success': True, 'data': {'deleted': int(prompt_id)}}


# --------------------------------------------------------------------------
# 运行记录 / AI 之间的沟通
# --------------------------------------------------------------------------

@router.get('/messages')
def agent_messages(
    limit: int = Query(default=100, ge=1, le=500),
    conversation_id: Optional[int] = Query(default=None),
    user=Depends(current_user),
):
    return {'success': True, 'data': runtime.recent_agent_messages(limit=limit, conversation_id=conversation_id)}


@router.get('/runs')
def agent_runs(
    limit: int = Query(default=100, ge=1, le=500),
    role: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    return {'success': True, 'data': llm.recent_runs(limit=limit, role=role)}


# --------------------------------------------------------------------------
# 主管 AI
# --------------------------------------------------------------------------

@router.post('/master/chat')
async def master_chat(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    message = str(payload.get('message') or '').strip()
    if not message:
        raise HTTPException(status_code=400, detail='message 不能为空')
    conversation_id = payload.get('conversation_id')
    context: Dict[str, Any] = {}
    if conversation_id:
        context['conversation_id'] = int(conversation_id)
    if isinstance(payload.get('history'), list):
        context['history'] = payload['history']

    result = await runtime.run_master(message, context or None)
    if not result.get('ok'):
        raise HTTPException(status_code=502, detail=str(result.get('error') or '主管 AI 调用失败'))
    return {
        'success': True,
        'data': {
            'reply': result.get('reply', ''),
            'tool_calls': result.get('tool_calls', []),
            'role': 'master',
            'model': result.get('model', ''),
            'provider_id': result.get('provider_id'),
        },
    }


@router.post('/reply')
async def generate_reply(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    """给一条买家消息生成 AI 回复（**只生成不发送**）。业务层决定要不要发。"""
    conversation_id = payload.get('conversation_id')
    message_id = payload.get('message_id')
    if not conversation_id or not message_id:
        raise HTTPException(status_code=400, detail='conversation_id 和 message_id 必填')
    result = await runtime.reply_for_message(int(conversation_id), int(message_id))
    if not result.get('ok'):
        raise HTTPException(status_code=502, detail=str(result.get('error') or 'AI 回复生成失败'))
    return {'success': True, 'data': result}


@router.get('/roles/{role}/prompt-preview')
def preview_prompt(
    role: str,
    account_id: Optional[str] = Query(default=None),
    item_id: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    """UI 预览：某个角色在某个商品/账号下的最终系统提示词（三级覆盖后的结果）。"""
    if not roles_mod.is_role(role):
        raise HTTPException(status_code=404, detail=f'未知角色: {role}')
    return {'success': True, 'data': {'role': role, 'prompt': prompts_mod.system_prompt(
        role, account_id=account_id, item_id=item_id)}}
