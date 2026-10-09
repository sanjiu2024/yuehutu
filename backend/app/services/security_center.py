"""安全中心只读服务。平台无权限时如实返回，不生成本地假数据。"""
from __future__ import annotations
from datetime import datetime
from typing import Any
from ..core import db
from . import common

def read(account_id: str, page: int = 1, size: int = 20):
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return common.fail(f'账号 {account_id} 不存在', 'not_found')
    row = db.query_one('SELECT cookie, login_state FROM account_credentials WHERE account_id = ?', (account_id,))
    if row is None or not str(row['cookie'] or '').strip() or str(row['login_state'] or '') == 'need_relogin':
        return common.fail(f'账号 {account_id} 登录态不可用，请先扫码登录', 'verification_required')
    client, failure = common.get_client(account_id)
    if client is None:
        return failure or common.not_ready()
    status, score = common.call_method(client, ('security_score',))
    if status != common.CALL_OK:
        failure = common.call_failed(status, score)
        return failure or common.fail(str(score or '安全中心接口调用失败'), 'protocol_error')
    status, violations = common.call_method(client, ('security_violations',), page, size)
    if status != common.CALL_OK:
        failure = common.call_failed(status, violations)
        return failure or common.fail(str(violations or '安全中心违规记录接口调用失败'), 'protocol_error')
    return {'account_id': account_id, 'score': score, 'violations': violations,
            'source': 'platform', 'fetched_at': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}
