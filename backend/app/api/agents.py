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
    POST   /api/agents/master/chat   和主管 AI 对话（**两个回合都落库**）
    GET    /api/agents/chat/messages 主管 AI 对话历史（分页）
    DELETE /api/agents/chat/messages 清空主管 AI 对话历史
    POST   /api/agents/reply         内部：给一条买家消息生成 AI 回复（不发送）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from ..agents import history, llm, prompts as prompts_mod, roles as roles_mod, runtime
from ..core import db, events
from .deps import current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix='/api/agents', tags=['agents'])


def _publish(topic: str, payload: Dict[str, Any]) -> None:
    """提示词/角色变更广播。前端「提示词」「AI 配置」页靠它实时刷新。

    推送失败绝不能影响已经写库的变更，所以整体吞异常。
    """
    try:
        events.publish(topic, payload)
    except Exception:  # pragma: no cover
        pass


def _record(thread: str, role: str, content: str, **fields: Any) -> int:
    """把一回合落库。**落库失败绝不能让对话接口 500** —— 对话已经发生了，
    只是没记住；记一条日志就好。返回新行 id，失败返回 0。"""
    try:
        return history.append(thread, role, content, **fields)
    except Exception:
        log.exception('对话落库失败（thread=%s role=%s），本次对话结果仍正常返回', thread, role)
        return 0


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
    _publish('providers.changed', {'action': 'roles', 'role': role})
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
    except prompts_mod.PromptValidationError as exc:
        # 必须排在 ValueError 之前（它是 ValueError 子类）：main.py 的异常处理器会把
        # HTTPException(detail) 拍平成 {'code': 'bad_request'}，结构化信息（哪个字段错）
        # 就丢了。`as_envelope()` 直接给出 {'code':'invalid_fields','invalid_fields':{...}}。
        return JSONResponse(status_code=400, content=exc.as_envelope())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _publish('prompts.changed', {'action': 'create', 'prompt_id': data.get('id'), 'scope': data.get('scope')})
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
    except prompts_mod.PromptValidationError as exc:
        # 必须排在 ValueError 之前（它是 ValueError 子类）：main.py 的异常处理器会把
        # HTTPException(detail) 拍平成 {'code': 'bad_request'}，结构化信息（哪个字段错）
        # 就丢了。`as_envelope()` 直接给出 {'code':'invalid_fields','invalid_fields':{...}}。
        return JSONResponse(status_code=400, content=exc.as_envelope())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if data['id'] != int(prompt_id):
        prompts_mod.delete_prompt(prompt_id)  # 改了 scope/owner 导致换了行，清掉旧的
    _publish('prompts.changed', {'action': 'update', 'prompt_id': data.get('id'), 'scope': data.get('scope')})
    return {'success': True, 'data': data}


@router.delete('/prompts/{prompt_id}')
def delete_prompt(prompt_id: int, user=Depends(current_user)):
    if prompts_mod.get_prompt(prompt_id) is None:
        raise HTTPException(status_code=404, detail=f'提示词 #{prompt_id} 不存在')
    prompts_mod.delete_prompt(prompt_id)
    _publish('prompts.changed', {'action': 'delete', 'prompt_id': int(prompt_id)})
    return {'success': True, 'data': {'deleted': int(prompt_id)}}


# --------------------------------------------------------------------------
# 运行记录 / AI 之间的沟通
# --------------------------------------------------------------------------

@router.get('/messages')
def agent_messages(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conversation_id: Optional[int] = Query(default=None),
    user=Depends(current_user),
):
    """AI 之间的沟通记录。

    `data` **仍然是裸数组**（前端「AI 沟通」页在用，不能改成对象），分页信息放在同级的
    `total` / `limit` / `offset` 上。
    """
    items = runtime.recent_agent_messages(limit=limit, conversation_id=conversation_id, offset=offset)
    return {
        'success': True,
        'data': items,
        'total': runtime.count_agent_messages(conversation_id=conversation_id),
        'limit': int(limit),
        'offset': int(offset),
    }


@router.get('/runs')
def agent_runs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    role: Optional[str] = Query(default=None),
    user=Depends(current_user),
):
    """调用用量/耗时。分页信息与 `/messages` 同形（`data` 保持裸数组）。"""
    items = llm.recent_runs(limit=limit, role=role, offset=offset)
    return {
        'success': True,
        'data': items,
        'total': llm.count_runs(role=role),
        'limit': int(limit),
        'offset': int(offset),
    }


# --------------------------------------------------------------------------
# 主管 AI 的对话历史（落库后可以回看）
# --------------------------------------------------------------------------

@router.get('/chat/messages')
def chat_messages(
    thread: str = Query(default=history.DEFAULT_THREAD),
    limit: int = Query(default=50, ge=1, le=500),
    before_id: Optional[int] = Query(default=None),
    user=Depends(current_user),
):
    """主管 AI 对话历史，按 id 升序返回一页（最新的在最后，便于直接追加渲染）。

    `before_id` 用于向上翻更早的历史；`data.total` 是该 thread 总数，
    `data.has_more` 表示这一页之前还有没有更早的行。
    """
    return {'success': True, 'data': history.list_messages(thread=thread, limit=limit, before_id=before_id)}


@router.delete('/chat/messages')
def clear_chat_messages(
    thread: str = Query(default=history.DEFAULT_THREAD),
    user=Depends(current_user),
):
    """清空某个 thread 的对话历史，返回删除行数。"""
    deleted = history.clear(thread=thread)
    _publish('agent.chat.cleared', {'thread': thread, 'deleted': deleted})
    return {'success': True, 'data': {'thread': thread, 'deleted': deleted}}


# --------------------------------------------------------------------------
# 主管 AI
# --------------------------------------------------------------------------

@router.post('/master/chat')
async def master_chat(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    message = str(payload.get('message') or '').strip()
    if not message:
        raise HTTPException(status_code=400, detail='message 不能为空')
    thread = str(payload.get('thread') or history.DEFAULT_THREAD)
    account_id = str(payload.get('account_id') or '')
    conversation_id = payload.get('conversation_id')
    context: Dict[str, Any] = {}
    if conversation_id:
        context['conversation_id'] = int(conversation_id)
    if isinstance(payload.get('history'), list):
        context['history'] = payload['history']

    _record(thread, 'user', message, account_id=account_id)

    started = time.perf_counter()
    try:
        result = await runtime.run_master(message, context or None)
    except Exception as exc:  # noqa: BLE001
        # **抛异常也要留痕**（复审 M3）：`run_master` 里读提示词、查温度、读历史都在
        # 它自己的 try 之外，DB 故障时用户只看到一个 500，AI 会话记录里连「这次失败过」
        # 都没有 —— 事后完全无法判断他到底问过什么、为什么没回。
        latency_ms = int((time.perf_counter() - started) * 1000)
        error = f'{type(exc).__name__}: {exc}'
        _record(thread, 'assistant', '', account_id=account_id,
                latency_ms=latency_ms, status='error', error=error)
        log.exception('主管 AI 回合异常')
        raise HTTPException(status_code=502, detail=error)
    latency_ms = int((time.perf_counter() - started) * 1000)

    if not result.get('ok'):
        # 失败也要落库：runtime 在工具已经执行过之后才失败时会把 tool_calls 带回来，
        # 用户因此能看到「它其实已经改了什么 / 为什么失败」。
        error = str(result.get('error') or '主管 AI 调用失败')
        _record(thread, 'assistant', '', account_id=account_id,
                tool_calls=result.get('tool_calls'), provider_id=result.get('provider_id'),
                model=str(result.get('model') or ''), latency_ms=latency_ms,
                status='error', error=error)
        raise HTTPException(status_code=502, detail=error)

    usage = result.get('usage') or {}
    tokens = int(usage.get('prompt_tokens') or 0) + int(usage.get('completion_tokens') or 0)
    reply = str(result.get('reply', '') or '')
    tool_calls = result.get('tool_calls') or []
    _record(thread, 'assistant', reply, account_id=account_id, tool_calls=tool_calls,
            provider_id=result.get('provider_id'), model=str(result.get('model') or ''),
            latency_ms=latency_ms, tokens=tokens, status='ok')

    return {
        'success': True,
        'data': {
            'reply': reply,
            'tool_calls': tool_calls,
            'role': 'master',
            'model': result.get('model', ''),
            'provider_id': result.get('provider_id'),
            # 新增（前端可选用）：让页面能把刚这一回合就地插进历史列表，不必重拉
            'thread': thread,
            'latency_ms': latency_ms,
            'tokens': tokens,
            'usage': usage,
            'truncated': bool(result.get('truncated')),
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
