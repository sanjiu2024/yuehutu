"""健康检查、系统信息、设置读写。"""

from __future__ import annotations

import platform
import sys
from typing import Any, Dict

from fastapi import APIRouter, Depends, Body

from ..core import config, db
from .deps import current_user

router = APIRouter(prefix='/api', tags=['system'])


@router.get('/health')
def health():
    return {'success': True, 'data': {'ok': True, 'version': config.VERSION, 'frozen': config.is_frozen()}}


@router.get('/system/info')
def system_info(user=Depends(current_user)):
    return {
        'success': True,
        'data': {
            'version': config.VERSION,
            'python': sys.version.split()[0],
            'platform': platform.platform(),
            'app_dir': str(config.APP_DIR),
            'db_path': str(config.DB_PATH),
            'frontend_dist': str(config.FRONTEND_DIST),
            'frontend_built': config.FRONTEND_DIST.exists(),
        },
    }


@router.get('/settings')
def get_settings(user=Depends(current_user)) -> Dict[str, Any]:
    rows = db.query('SELECT key, value FROM settings ORDER BY key')
    return {'success': True, 'data': {r['key']: r['value'] for r in rows}}


@router.put('/settings')
def put_settings(payload: Dict[str, str] = Body(...), user=Depends(current_user)):
    for key, value in payload.items():
        db.execute(
            'INSERT INTO settings(key, value, updated_at) VALUES(?,?,CURRENT_TIMESTAMP) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
            (key, str(value)),
        )
    return {'success': True, 'data': {'updated': len(payload)}}
