"""提示词三级覆盖：`ai_prompts`（global / account / item）叠加到角色基础提示词上。

规则（`docs/CONTRACTS.md` §4）：
1. 每个层级（global / account / item）各自取**priority 最大**的那条（同优先级取最新的一条）；
2. 三个层级按 `global → account → item` 顺序拼接，**越具体越靠后**（越靠后越能覆盖前面的说法）；
3. 最后再拼在 `ai_roles.system_prompt`（用户没配就用 `defaults.DEFAULT_ROLE_PROMPTS`）**后面**。

所以「item > account > global」体现在**顺序**上，而同一层内「priority 大者胜」。
`ai_prompts.role` 为空串表示「所有角色通用」；有精确角色匹配的行优先于通用行。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core import db
from . import roles as roles_mod

SCOPES = ('global', 'account', 'item')
# 拼接顺序：越靠后越具体，模型读到的最后一段最权威
SCOPE_ORDER = ('global', 'account', 'item')

# owner 的字段名（结构化报错要用它当 key，前端好直接定位到输入框）
_OWNER_FIELD = {'account': 'account_id', 'item': 'item_id'}


class PromptValidationError(ValueError):
    """提示词参数不合法。**结构化**：`invalid_fields` 是 `{字段名: 原因}`。

    为什么单独一个异常类：`upsert_prompt()` 以前对「scope=item 但没给 item_id」
    是照单全收的，于是库里多了一条永远不可能生效的孤儿行（审计 AI-6 实测
    `{"id":10,"scope":"item","account_id":"","item_id":""}`）。现在这种请求直接
    被拒，并且调用方能拿到**哪个字段**错了、**为什么**错。

    路由层（`backend/app/api/agents.py`，不在本次写范围内）目前把异常文本放进
    HTTP 400 的 `message`；要把它摊成信封字段，直接 `return err.as_envelope()` 即可。
    """

    code = 'invalid_fields'

    def __init__(self, message: str, invalid_fields: Optional[Dict[str, str]] = None):
        self.message = str(message)
        self.invalid_fields: Dict[str, str] = dict(invalid_fields or {})
        super().__init__(self.message)

    def as_envelope(self) -> Dict[str, Any]:
        """统一失败信封（`docs/CONTRACTS.md` §2）。"""
        return {
            'success': False,
            'message': self.message,
            'code': self.code,
            'invalid_fields': self.invalid_fields,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_envelope(), ensure_ascii=False)


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


def validate_prompt_fields(scope: str, *, account_id: Optional[str] = None,
                           item_id: Optional[str] = None) -> Dict[str, str]:
    """校验新写提示词的归属字段，返回 `{字段名: 原因}`（空 dict = 通过）。

    单独抽出来是为了让路由层也能先校验、并且能被测试直接驱动。
    """
    errors: Dict[str, str] = {}
    if scope not in SCOPES:
        # scope 本身错了就用 scope 当字段名
        errors['scope'] = f'未知 scope: {scope}（只能是 {" / ".join(SCOPES)}）'
        return errors
    if scope == 'account' and not str(account_id or '').strip():
        errors['account_id'] = 'scope=account 时必须提供 account_id，否则这条提示词永远不生效'
    if scope == 'item' and not str(item_id or '').strip():
        errors['item_id'] = 'scope=item 时必须提供 item_id，否则会变成一条永远不生效的孤儿提示词'
    return errors


def upsert_prompt(scope: str, owner_id: Optional[str] = None, content: str = '',
                  priority: int = 0, role: str = '', name: str = '',
                  enabled: bool = True, *, account_id: Optional[str] = None,
                  item_id: Optional[str] = None) -> Dict[str, Any]:
    """按 **(role, scope, account_id, item_id, name)** 唯一插入或更新。返回落库后的行。

    `name` 是去重键的一部分（审计 AI-5）：以前键是 `(role, scope, owner)`，于是同一个
    `scope=item` 下先存「话术A」再存「话术B」会把 A **静默覆盖**掉（两次 POST 都返回
    同一个 id，A 直接消失）。现在同名 = 同一条（重复保存幂等更新），不同名 = 两条共存；
    端上要「唯一」就自己别起第二个名字，而不是让后端悄悄吃掉数据。

    `account_id` / `item_id` 可以显式指定；不指定时按 `owner_id` 推断（兼容旧调用）。
    `scope` 需要 owner 却没给 → 抛 `PromptValidationError`（带结构化 `invalid_fields`）。
    """
    if scope not in SCOPES:
        raise PromptValidationError(f'未知 scope: {scope}（只能是 {" / ".join(SCOPES)}）',
                                    {'scope': f'scope 只能是 {" / ".join(SCOPES)}'})
    role = str(role or '')
    if role and not roles_mod.is_role(role):
        raise PromptValidationError(f'未知角色: {role}', {'role': f'未知角色: {role}'})

    # owner_id 仍然生效：scope=account 时是 account_id，scope=item 时是 item_id
    if account_id is None and scope == 'account':
        account_id = owner_id
    if item_id is None and scope == 'item':
        item_id = owner_id

    errors = validate_prompt_fields(scope, account_id=account_id, item_id=item_id)
    if errors:
        raise PromptValidationError('；'.join(errors.values()), errors)

    account = str(account_id or '').strip() if scope == 'account' else ''
    item = str(item_id or '').strip() if scope == 'item' else ''
    name = str(name or '')

    existing = db.query_one(
        'SELECT id FROM ai_prompts WHERE role = ? AND scope = ? AND account_id = ? AND item_id = ? '
        'AND name = ? LIMIT 1',
        (role, scope, account, item, name),
    )
    if existing is not None:
        db.execute(
            'UPDATE ai_prompts SET content = ?, priority = ?, name = ?, enabled = ?, '
            'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (content or '', int(priority or 0), name, 1 if enabled else 0, int(existing['id'])),
        )
        prompt_id = int(existing['id'])
    else:
        prompt_id = int(db.execute(
            'INSERT INTO ai_prompts(role, scope, account_id, item_id, name, content, priority, enabled) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (role, scope, account, item, name, content or '', int(priority or 0),
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
