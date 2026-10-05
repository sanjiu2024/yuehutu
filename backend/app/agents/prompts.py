"""提示词三级覆盖：`ai_prompts`（global / account / item）叠加到角色基础提示词上。

规则（`docs/CONTRACTS.md` §4）：
1. 每个层级（global / account / item）各自取**priority 最大**的那条（同优先级取最新的一条）；
2. 三个层级按 `global → account → item` 顺序拼接，**越具体越靠后**（越靠后越能覆盖前面的说法）；
3. 最后再拼在 `ai_roles.system_prompt`（用户没配就用 `defaults.DEFAULT_ROLE_PROMPTS`）**后面**。

所以「item > account > global」体现在**顺序**上，而同一层内「priority 大者胜」。
`ai_prompts.role` 为空串表示「所有角色通用」；有精确角色匹配的行优先于通用行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import db
from . import roles as roles_mod

SCOPES = ('global', 'account', 'item')
# 拼接顺序：越靠后越具体，模型读到的最后一段最权威
SCOPE_ORDER = ('global', 'account', 'item')


def _scope_owner(scope: str, owner_id: Optional[str]) -> str:
    return str(owner_id or '') if scope != 'global' else ''


def _account_for_item(item_id: Optional[str]) -> str:
    if not item_id:
        return ''
    row = db.query_one('SELECT account_id FROM items WHERE item_id = ? LIMIT 1', (str(item_id),))
    return str(row['account_id']) if row is not None else ''


def _candidates(role: str, scope: str, owner_id: str) -> List[Any]:
    if scope == 'global':
        return list(db.query(
            'SELECT * FROM ai_prompts WHERE scope = ? AND enabled = 1 AND (role = ? OR role = \'\')',
            (scope, role),
        ))
    return list(db.query(
        'SELECT * FROM ai_prompts WHERE scope = ? AND enabled = 1 AND '
        '(role = ? OR role = \'\') AND '
        f'{"account_id" if scope == "account" else "item_id"} = ?',
        (scope, role, owner_id),
    ))


def _pick(rows: List[Any], role: str) -> Optional[Any]:
    """精确角色 > 通用；priority 大者胜；同分取 id 大的（最新）。"""
    if not rows:
        return None
    return max(rows, key=lambda r: (1 if r['role'] == role else 0, int(r['priority'] or 0), int(r['id'])))


def system_prompt(role: str, *, account_id: Optional[str] = None, item_id: Optional[str] = None) -> str:
    """组装最终系统提示词。永远返回非空字符串（角色基础提示词兜底）。"""
    base = roles_mod.get_role(role)['system_prompt'] if roles_mod.is_role(role) else ''
    base = (base or roles_mod.default_prompt(role)).strip()

    account = str(account_id or '')
    if not account and item_id:
        account = _account_for_item(item_id)

    parts: List[str] = [base] if base else []
    for scope in SCOPE_ORDER:
        owner = _scope_owner(scope, account if scope == 'account' else item_id)
        if scope != 'global' and not owner:
            continue
        row = _pick(_candidates(role, scope, owner), role)
        if row is None:
            continue
        content = str(row['content'] or '').strip()
        if content:
            parts.append(content)
    return '\n\n'.join(parts)


def _row_to_dict(row: Any) -> Dict[str, Any]:
    data = dict(row)
    data['priority'] = int(data.get('priority') or 0)
    data['enabled'] = bool(data.get('enabled'))
    return data


def list_prompts(scope: Optional[str] = None, owner_id: Optional[str] = None,
                 role: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出提示词。`owner_id` 在 scope=account 时是 account_id，scope=item 时是 item_id。"""
    sql = 'SELECT * FROM ai_prompts WHERE 1=1'
    params: List[Any] = []
    if scope:
        sql += ' AND scope = ?'
        params.append(scope)
        if scope == 'account' and owner_id is not None:
            sql += ' AND account_id = ?'
            params.append(str(owner_id))
        elif scope == 'item' and owner_id is not None:
            sql += ' AND item_id = ?'
            params.append(str(owner_id))
    elif owner_id is not None:
        sql += ' AND (account_id = ? OR item_id = ?)'
        params.extend([str(owner_id), str(owner_id)])
    if role:
        sql += ' AND role = ?'
        params.append(role)
    sql += ' ORDER BY scope, role, priority DESC, id DESC'
    return [_row_to_dict(r) for r in db.query(sql, params)]


def get_prompt(prompt_id: int) -> Optional[Dict[str, Any]]:
    row = db.query_one('SELECT * FROM ai_prompts WHERE id = ?', (int(prompt_id),))
    return _row_to_dict(row) if row is not None else None


def upsert_prompt(scope: str, owner_id: Optional[str] = None, content: str = '',
                  priority: int = 0, role: str = '', name: str = '',
                  enabled: bool = True) -> Dict[str, Any]:
    """按 (role, scope, account_id, item_id) 唯一插入或更新。返回落库后的行。"""
    if scope not in SCOPES:
        raise ValueError(f'未知 scope: {scope}（只能是 {" / ".join(SCOPES)}）')
    role = str(role or '')
    if role and not roles_mod.is_role(role):
        raise ValueError(f'未知角色: {role}')
    owner = _scope_owner(scope, owner_id)
    account_id = owner if scope == 'account' else ''
    item_id = owner if scope == 'item' else ''

    existing = db.query_one(
        'SELECT id FROM ai_prompts WHERE role = ? AND scope = ? AND account_id = ? AND item_id = ? LIMIT 1',
        (role, scope, account_id, item_id),
    )
    if existing is not None:
        db.execute(
            'UPDATE ai_prompts SET content = ?, priority = ?, name = ?, enabled = ?, '
            'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (content or '', int(priority or 0), name or '', 1 if enabled else 0, int(existing['id'])),
        )
        prompt_id = int(existing['id'])
    else:
        prompt_id = int(db.execute(
            'INSERT INTO ai_prompts(role, scope, account_id, item_id, name, content, priority, enabled) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (role, scope, account_id, item_id, name or '', content or '', int(priority or 0),
             1 if enabled else 0),
        ))
    result = get_prompt(prompt_id)
    assert result is not None
    return result


def delete_prompt(prompt_id: int) -> bool:
    return bool(db.execute('DELETE FROM ai_prompts WHERE id = ?', (int(prompt_id),)))


def seed_defaults() -> int:
    """把 `defaults.DEFAULT_ROLE_PROMPTS` 写进 ai_roles（只在 system_prompt 为空时）。

    用户改过的不动 —— 这是「默认值可被 UI 覆盖」的落地点。
    """
    written = 0
    for role in roles_mod.ROLES:
        current = roles_mod.get_role(role)['system_prompt']
        if not current:
            db.execute(
                'UPDATE ai_roles SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE role = ?',
                (roles_mod.default_prompt(role), role),
            )
            written += 1
    return written
