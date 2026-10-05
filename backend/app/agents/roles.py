"""AI 角色定义与 ai_roles 表的读写。

角色（`docs/CONTRACTS.md` §4）：
    master  主管 AI —— 唯一直接和卖家对话、唯一能调工具的
    item    每个商品一个常驻客服 AI
    cs      通用客服（订单/物流/售后）
    bargain 议价 AI
    cross   跨商品协查 AI
    vision  看图 AI

`ai_roles` 表存每个角色的 provider_id / model / temperature / system_prompt / enabled。
表里没有行时用 `defaults.DEFAULT_ROLE_PROMPTS` 兜底，所以**不需要**预先灌数据。
"""

from __future__ import annotations

import json

from typing import Any, Dict, List, Optional

from ..core import db
from .defaults import DEFAULT_ROLE_PROMPTS, DEFAULT_ROLE_TEMPERATURES

ROLES = ('master', 'item', 'cs', 'bargain', 'cross', 'vision')

ROLE_LABELS: Dict[str, str] = {
    'master': '主管 AI',
    'item': '商品客服 AI',
    'cs': '通用客服 AI',
    'bargain': '议价 AI',
    'cross': '跨商品协查 AI',
    'vision': '看图 AI',
}

ROLE_DESCRIPTIONS: Dict[str, str] = {
    'master': '和卖家（你）对接的唯一 AI，能调用工具干活，把任务派给其它 AI。',
    'item': '每个商品一个常驻 AI，按商品维度的话术回答买家。',
    'cs': '通用客服：订单、物流、退换、售后政策。',
    'bargain': '议价：在卖家设定的底价/幅度内和买家谈价。',
    'cross': '买家在 A 商品里问 B 商品时，负责去问 B 的商品 AI 并汇总答复。',
    'vision': '买家发图问货时看图作答。',
}

_ROLE_UPDATE_FIELDS = ('provider_id', 'model', 'temperature', 'system_prompt', 'enabled')


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """dict / sqlite3.Row / 对象 通用取值。"""
    if obj is None:
        return default
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError):
        value = getattr(obj, key, default)
    return default if value is None else value


def is_role(role: Optional[str]) -> bool:
    return bool(role) and role in ROLES


def default_prompt(role: str) -> str:
    return DEFAULT_ROLE_PROMPTS.get(role, '')


def default_temperature(role: str) -> float:
    return float(DEFAULT_ROLE_TEMPERATURES.get(role, 0.7))


_BARGAIN_WORDS = (
    '便宜', '能少', '少点', '少一点', '少点儿', '优惠', '打折', '降价', '议价',
    '砍价', '底价', '实价', '最低', '包邮吗', '抹零', '小刀',
)


def _message_text(message: Any) -> str:
    """消息里的买家文本。message 可以是 dict/Row，也可以直接是字符串。"""
    if isinstance(message, str):
        return message
    return str(_field(message, 'content', '') or '')


def _message_image(message: Any) -> str:
    """消息里带的图片地址（存在 `messages.meta` 的 json 里）。"""
    if message is None or isinstance(message, str):
        return ''
    meta = _field(message, 'meta', None)
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) if meta.strip() else {}
        except ValueError:
            meta = {}
    if not isinstance(meta, dict):
        return ''
    return str(meta.get('image_url') or meta.get('image') or '')


def role_for_conversation(conv: Any, message: Any = None) -> str:
    """会话（+这条买家消息）→ 该由哪个角色接。

    规则（顺序固定，方便测试）：
    1. `conversations.agent_role` 合法 → 用它（前端/业务可以手工指定）；
    2. 买家发来的是图片 → `vision`（看图 AI）；
    3. 买家在讲价 → `bargain`（议价 AI）；
    4. 没有绑定商品（`item_id` 为空）→ `cs`（通用客服）；
    5. 其余 → `item`（商品客服）。

    第 2/3 条需要 `message`。不传时行为与旧版完全一致，`bargain`/`vision` 就永远轮不到 ——
    这正是它们此前不可达的原因：角色表里有这两个角色，却没有任何一条路径会选中它们。
    """
    role = str(_field(conv, 'agent_role', '') or '').strip()
    if is_role(role):
        return role
    if _message_image(message):
        return 'vision'
    text = _message_text(message)
    if any(word in text for word in _BARGAIN_WORDS):
        return 'bargain'
    if not str(_field(conv, 'item_id', '') or '').strip():
        return 'cs'
    return 'item'


def ensure_roles() -> None:
    """幂等：给 6 个角色各补一行 ai_roles（已存在的行不动）。"""
    for role in ROLES:
        db.execute(
            'INSERT INTO ai_roles(role, provider_id, model, temperature, system_prompt, enabled) '
            'VALUES(?, NULL, \'\', ?, \'\', 1) ON CONFLICT(role) DO NOTHING',
            (role, default_temperature(role)),
        )


def _role_row(role: str):
    ensure_roles()
    return db.query_one('SELECT * FROM ai_roles WHERE role = ?', (role,))


def get_role(role: str) -> Dict[str, Any]:
    """单角色配置（含默认提示词，UI 直接可用）。角色名非法 → ValueError。"""
    if not is_role(role):
        raise ValueError(f'未知角色: {role}')
    row = _role_row(role)
    return {
        'role': role,
        'label': ROLE_LABELS.get(role, role),
        'description': ROLE_DESCRIPTIONS.get(role, ''),
        'provider_id': row['provider_id'] if row else None,
        'model': (row['model'] if row else '') or '',
        'temperature': float(row['temperature']) if row else default_temperature(role),
        'system_prompt': (row['system_prompt'] if row else '') or '',
        'default_prompt': default_prompt(role),
        'enabled': bool(row['enabled']) if row else True,
        'updated_at': row['updated_at'] if row else None,
    }


def list_roles() -> List[Dict[str, Any]]:
    return [get_role(role) for role in ROLES]


def update_role(role: str, **fields: Any) -> Dict[str, Any]:
    """局部更新；未提供的字段保持原值。非法字段忽略。"""
    if not is_role(role):
        raise ValueError(f'未知角色: {role}')
    current = get_role(role)
    patch = {k: v for k, v in fields.items() if k in _ROLE_UPDATE_FIELDS and v is not None}
    merged = {
        'provider_id': patch.get('provider_id', current['provider_id']),
        'model': patch.get('model', current['model']),
        'temperature': patch.get('temperature', current['temperature']),
        'system_prompt': patch.get('system_prompt', current['system_prompt']),
        'enabled': patch.get('enabled', current['enabled']),
    }
    db.execute(
        'UPDATE ai_roles SET provider_id = ?, model = ?, temperature = ?, system_prompt = ?, '
        'enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE role = ?',
        (
            merged['provider_id'],
            merged['model'] or '',
            float(merged['temperature']),
            merged['system_prompt'] or '',
            1 if merged['enabled'] else 0,
            role,
        ),
    )
    return get_role(role)


def role_temperature(role: str) -> float:
    row = db.query_one('SELECT temperature FROM ai_roles WHERE role = ?', (role,))
    return float(row['temperature']) if row else default_temperature(role)
