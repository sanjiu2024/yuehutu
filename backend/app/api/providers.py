"""AI 提供者（API Key）统一管理。

`providers` 表是**唯一**的 Key 来源：任何模块都不许把 base_url / api_key 写进代码，
一律从这里读（见 `backend/app/agents/llm.py::resolve_provider`）。

**Key 加密落库**（`core/crypto.py`）：写入时 `crypto.encrypt()`，读出时 `crypto.decrypt()`。
本文件额外负责一件事：把**历史明文行**（本次修复之前写入的）幂等地加密回写，
所以老用户的库升级后不会被要求重新填 Key。

路由（`docs/CONTRACTS.md` §3）：
    GET    /api/providers
    POST   /api/providers
    PUT    /api/providers/{id}
    DELETE /api/providers/{id}
    POST   /api/providers/{id}/test      真实调一次，验证 Key 可用
    PUT    /api/providers/{id}/default   （另兼容 POST，契约里写的是 POST）

任何写操作成功后发 WS 事件 `providers.changed`
（payload `{"provider_id", "is_default", "action"}`，**不含 api_key**）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..agents import llm
from ..agents.tools import PROVIDERS_CHANGED
from ..core import crypto, db, events
from .deps import current_user

router = APIRouter(prefix='/api/providers', tags=['providers'])

PUBLIC_FIELDS = 'id, name, base_url, default_model, enabled, is_default, created_at, updated_at'

# 明文迁移只做一次（模块级标记）；失败也不影响任何请求。
_MIGRATION_DONE = False


def _migrate_plaintext_keys() -> int:
    """把库里遗留的明文 api_key 加密回写。幂等，进程内只跑一次。"""
    global _MIGRATION_DONE
    if _MIGRATION_DONE:
        return 0
    _MIGRATION_DONE = True
    try:
        return crypto.migrate_plaintext('providers', 'api_key', id_column='id')
    except Exception:  # pragma: no cover - 迁移失败不能影响读接口
        return 0


def _publish_changed(action: str, provider_id: Any, is_default: bool = False) -> None:
    """通知前端「AI 设置」页重拉。**payload 里不放 api_key**。"""
    try:
        events.publish(PROVIDERS_CHANGED, {
            'provider_id': int(provider_id) if provider_id not in (None, '') else None,
            'is_default': bool(is_default),
            'action': action,
        })
    except Exception:  # pragma: no cover - 推送失败不能影响主流程
        pass


def _mask(key: str) -> str:
    return crypto.mask(key)


def _public(row: Any, *, with_key: bool = False) -> Dict[str, Any]:
    data = dict(row)
    data.pop('api_key', None)
    data['enabled'] = bool(data.get('enabled'))
    data['is_default'] = bool(data.get('is_default'))
    if with_key:
        raw = row['api_key'] if row is not None else ''
        data['api_key_masked'] = crypto.mask(raw)
        data['has_key'] = bool(raw)
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
    _migrate_plaintext_keys()
    rows = db.query('SELECT * FROM providers ORDER BY is_default DESC, id')
    return {'success': True, 'data': [_public(row, with_key=True) for row in rows]}


@router.get('/')
def list_providers_slash(user=Depends(current_user)):  # pragma: no cover - 兼容尾斜杠
    return list_providers(user)


@router.post('')
def create_provider(payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    _migrate_plaintext_keys()
    name = str(payload.get('name') or '').strip()
    base_url = str(payload.get('base_url') or '').strip().rstrip('/')
    if not name or not base_url:
        raise HTTPException(status_code=400, detail='name 和 base_url 必填')
    enabled = 1 if payload.get('enabled', True) else 0
    is_default = 1 if payload.get('is_default') else 0
    provider_id = int(db.execute(
        'INSERT INTO providers(name, base_url, api_key, default_model, enabled, is_default) '
        'VALUES(?,?,?,?,?,?)',
        (name, base_url, crypto.encrypt(payload.get('api_key') or ''),
         str(payload.get('default_model') or ''), enabled, is_default),
    ))
    if is_default:
        _clear_default(provider_id)
    _publish_changed('create', provider_id, bool(is_default))
    return {'success': True, 'data': _public(_get_row(provider_id), with_key=True)}


@router.put('/{provider_id}')
def update_provider(provider_id: int, payload: Dict[str, Any] = Body(...), user=Depends(current_user)):
    _migrate_plaintext_keys()
    row = _get_row(provider_id)
    patch: Dict[str, Any] = {}
    for field in ('name', 'base_url', 'default_model'):
        if payload.get(field) is not None:
            patch[field] = str(payload[field]).strip()
    if 'base_url' in patch:
        patch['base_url'] = patch['base_url'].rstrip('/')
    # api_key 只在**显式给了非空值**时覆盖 —— 避免 UI 回显掩码时把真 key 冲掉
    # （`crypto.encrypt()` 对已是密文的值幂等，不会二次加密）
    if payload.get('api_key'):
        patch['api_key'] = crypto.encrypt(payload['api_key'])
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
    fresh = _get_row(provider_id)
    _publish_changed('update', provider_id, bool(fresh['is_default']))
    return {'success': True, 'data': _public(fresh, with_key=True)}


@router.delete('/{provider_id}')
def delete_provider(provider_id: int, user=Depends(current_user)):
    _get_row(provider_id)
    db.execute('DELETE FROM providers WHERE id = ?', (int(provider_id),))
    db.execute('UPDATE ai_roles SET provider_id = NULL WHERE provider_id = ?', (int(provider_id),))
    _publish_changed('delete', provider_id, False)
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
    _publish_changed('set_default', provider_id, True)
    return {'success': True, 'data': _public(_get_row(row['id']), with_key=True)}


@router.put('/{provider_id}/default')
def set_default_put(provider_id: int, user=Depends(current_user)):
    return _set_default(provider_id)


@router.post('/{provider_id}/default')
def set_default_post(provider_id: int, user=Depends(current_user)):
    """契约 §3 写的是 POST，这里两个动词都支持，前端随便用哪个。"""
    return _set_default(provider_id)
