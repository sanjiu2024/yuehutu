"""AI 提供者（API Key）统一管理。

`providers` 表是**唯一**的 Key 来源：任何模块都不许把 base_url / api_key 写进代码，
一律从这里读（见 `backend/app/agents/llm.py::resolve_provider`）。

路由（`docs/CONTRACTS.md` §3）：
    GET    /api/providers
    POST   /api/providers
    PUT    /api/providers/{id}
    DELETE /api/providers/{id}
    POST   /api/providers/{id}/test      真实调一次，验证 Key 可用
    PUT    /api/providers/{id}/default   （另兼容 POST，契约里写的是 POST）
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..agents import llm
from ..core import db
from .deps import current_user

router = APIRouter(prefix='/api/providers', tags=['providers'])

PUBLIC_FIELDS = 'id, name, base_url, default_model, enabled, is_default, created_at, updated_at'


def _mask(key: str) -> str:
    key = str(key or '')
    if not key:
        return ''
    if len(key) <= 8:
        return '*' * len(key)
    return f'{key[:4]}{"*" * 8}{key[-4:]}'


def _public(row: Any, *, with_key: bool = False) -> Dict[str, Any]:
    data = dict(row)
    data.pop('api_key', None)
    data['enabled'] = bool(data.get('enabled'))
    data['is_default'] = bool(data.get('is_default'))
    if with_key:
        data['api_key_masked'] = _mask(row['api_key'] if row is not None else '')
        data['has_key'] = bool(row['api_key']) if row is not None else False
    return data


def _get_row(provider_id: int):
    row = db.query_one('SELECT * FROM providers WHERE id = ?', (int(provider_id),))
    if row is None:
        raise HTTPException(status_code=404, detail=f'提供者 #{provider_id} 不存在')
    return row


def _clear_default(keep_id: int) -> None:
    db.execute('UPDATE providers SET is_default = 0 WHERE id != ?', (int(keep_id),))


@router.get('')
def list_providers(user=Depends(current_user)):
    rows = db.query('SELECT * FROM providers ORDER BY is_default DESC, id')
    return {'success': True, 'data': [_public(row, with_key=True) for row in rows]}


@router.get('/')
def list_providers_slash(user=Depends(current_user)):  # pragma: no cover - 兼容尾斜杠
    return list_providers(user)


@router.post('')
def create_provider(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    name = str(payload.get('name') or '').strip()
    base_url = str(payload.get('base_url') or '').strip().rstrip('/')
    if not name or not base_url:
        raise HTTPException(status_code=400, detail='name 和 base_url 必填')
    enabled = 1 if payload.get('enabled', True) else 0
    is_default = 1 if payload.get('is_default') else 0
    provider_id = int(db.execute(
        'INSERT INTO providers(name, base_url, api_key, default_model, enabled, is_default) '
        'VALUES(?,?,?,?,?,?)',
        (name, base_url, str(payload.get('api_key') or ''), str(payload.get('default_model') or ''),
         enabled, is_default),
    ))
    if is_default:
        _clear_default(provider_id)
    return {'success': True, 'data': _public(_get_row(provider_id), with_key=True)}


@router.put('/{provider_id}')
def update_provider(provider_id: int, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    row = _get_row(provider_id)
    patch: Dict[str, Any] = {}
    for field in ('name', 'base_url', 'default_model'):
        if payload.get(field) is not None:
            patch[field] = str(payload[field]).strip()
    if 'base_url' in patch:
        patch['base_url'] = patch['base_url'].rstrip('/')
    # api_key 只在**显式给了非空值**时覆盖 —— 避免 UI 回显掩码时把真 key 冲掉
    if payload.get('api_key'):
        patch['api_key'] = str(payload['api_key'])
    for field in ('enabled', 'is_default'):
        if payload.get(field) is not None:
            patch[field] = 1 if payload[field] else 0

    if patch:
        columns = ', '.join(f'{k} = ?' for k in patch)
        db.execute(
            f'UPDATE providers SET {columns}, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (*patch.values(), int(provider_id)),
        )
    if patch.get('is_default'):
        _clear_default(provider_id)
    return {'success': True, 'data': _public(_get_row(provider_id), with_key=True)}


@router.delete('/{provider_id}')
def delete_provider(provider_id: int, user=Depends(current_user)):
    _get_row(provider_id)
    db.execute('DELETE FROM providers WHERE id = ?', (int(provider_id),))
    db.execute('UPDATE ai_roles SET provider_id = NULL WHERE provider_id = ?', (int(provider_id),))
    return {'success': True, 'data': {'deleted': int(provider_id)}}


@router.post('/{provider_id}/test')
async def test_provider(provider_id: int, user=Depends(current_user)):
    """**真实调用一次**验证 Key 可用（会花一点点 token，这是用户主动点的）。"""
    _get_row(provider_id)
    started = time.time()
    try:
        result = await llm.chat(
            [{'role': 'user', 'content': '你好'}],
            provider_id=int(provider_id),
            timeout=20,
        )
        return {
            'success': True,
            'data': {
                'ok': True,
                'latency_ms': int((time.time() - started) * 1000),
                'model': result.get('model', ''),
                'sample': (result.get('text') or '')[:100],
                'usage': result.get('usage', {}),
            },
        }
    except llm.LLMError as exc:
        return {
            'success': True,
            'data': {
                'ok': False,
                'latency_ms': int((time.time() - started) * 1000),
                'model': '',
                'error': str(exc),
            },
        }


def _set_default(provider_id: int) -> Dict[str, Any]:
    row = _get_row(provider_id)
    db.execute('UPDATE providers SET is_default = 0')
    db.execute('UPDATE providers SET is_default = 1, enabled = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
               (int(provider_id),))
    db.execute(
        'INSERT INTO settings(key, value, updated_at) VALUES(\'ai.default_provider_id\', ?, CURRENT_TIMESTAMP) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
        (str(provider_id),),
    )
    return {'success': True, 'data': _public(_get_row(row['id']), with_key=True)}


@router.put('/{provider_id}/default')
def set_default_put(provider_id: int, user=Depends(current_user)):
    return _set_default(provider_id)


@router.post('/{provider_id}/default')
def set_default_post(provider_id: int, user=Depends(current_user)):
    """契约 §3 写的是 POST，这里两个动词都支持，前端随便用哪个。"""
    return _set_default(provider_id)
