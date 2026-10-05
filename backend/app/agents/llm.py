"""统一 LLM 调用层（OpenAI 兼容 `/chat/completions`）。

设计要点：
- **提供者统一管理**：`providers` 表是唯一来源，key 绝不写进代码。
  角色指定了 `provider_id` 就用它；否则用 `is_default=1`；再否则用第一条 enabled 的。
- 超时 + 重试 1 次；每次调用（成功或失败）都落 `agent_runs`，方便前端看用量与排错。
- 只依赖 httpx（requirements 里已有），不引新依赖。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import httpx

from ..core import db
from . import roles as roles_mod

DEFAULT_TIMEOUT = 60.0
DEFAULT_TEMPERATURE = 0.7
RETRY_TIMES = 1


class LLMError(RuntimeError):
    """LLM 调用失败（无可用提供者、HTTP 错误、超时、返回体不合法）。"""

    def __init__(self, message: str, *, status: int = 0, provider_id: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.provider_id = provider_id


def _setting(key: str, default: str = '') -> str:
    row = db.query_one('SELECT value FROM settings WHERE key = ?', (key,))
    if row is None or row['value'] is None:
        return default
    return str(row['value'])


def _resolve_provider_id(role: Optional[str]) -> Optional[int]:
    """角色 → provider_id；角色没配就看全局设置，最后看 is_default。"""
    if role:
        row = db.query_one('SELECT provider_id FROM ai_roles WHERE role = ?', (role,))
        if row is not None and row['provider_id'] is not None:
            return int(row['provider_id'])
    configured = _setting('ai.default_provider_id', '').strip()
    if configured.isdigit():
        return int(configured)
    return None


def resolve_provider(role: Optional[str] = None, provider_id: Optional[int] = None) -> Dict[str, Any]:
    """解析出这次调用该用哪个提供者。返回 dict（含 api_key，**不要**直接回给前端）。

    顺序：显式 provider_id → 角色配置 → settings.ai.default_provider_id → providers.is_default=1
          → 第一条 enabled 的。
    """
    target_id = provider_id if provider_id is not None else _resolve_provider_id(role)

    row = None
    if target_id is not None:
        row = db.query_one('SELECT * FROM providers WHERE id = ?', (target_id,))
        if row is None:
            raise LLMError(f'提供者 #{target_id} 不存在', provider_id=target_id)
        if not row['enabled']:
            raise LLMError(f'提供者 #{target_id} 已停用', provider_id=target_id)
    if row is None:
        row = db.query_one('SELECT * FROM providers WHERE is_default = 1 AND enabled = 1 ORDER BY id LIMIT 1')
    if row is None:
        row = db.query_one('SELECT * FROM providers WHERE enabled = 1 ORDER BY id LIMIT 1')
    if row is None:
        raise LLMError('还没有可用的 AI 提供者，请先在「AI 设置」里添加一个 API Key')

    model = ''
    if role:
        role_row = db.query_one('SELECT model FROM ai_roles WHERE role = ?', (role,))
        if role_row is not None and role_row['model']:
            model = str(role_row['model']).strip()
    model = model or str(row['default_model'] or '').strip()
    if not model:
        raise LLMError(f'提供者「{row["name"]}」没有可用模型：请填 default_model 或给角色指定 model',
                       provider_id=int(row['id']))

    return {
        'id': int(row['id']),
        'name': row['name'],
        'base_url': str(row['base_url'] or '').rstrip('/'),
        'api_key': row['api_key'] or '',
        'model': model,
        'default_model': row['default_model'] or '',
    }


def _record_run(
    *,
    conversation_id: Optional[int],
    role: str,
    provider_id: Optional[int],
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
    error: str = '',
) -> None:
    try:
        db.execute(
            'INSERT INTO agent_runs(conversation_id, role, provider_id, model, prompt_tokens, '
            'completion_tokens, latency_ms, status, error) VALUES(?,?,?,?,?,?,?,?,?)',
            (
                conversation_id,
                role or '',
                provider_id,
                model or '',
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(latency_ms or 0),
                status,
                (error or '')[:500],
            ),
        )
    except Exception:  # pragma: no cover - 计量失败不该拖垮业务
        pass


def _payload_text(data: Dict[str, Any]) -> str:
    """兼容 OpenAI 风格的 choices[0].message.content（可能是 str 或 parts 列表）。"""
    choices = data.get('choices') or []
    if not choices:
        return ''
    message = choices[0].get('message') or {}
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get('text'), str):
                    parts.append(part['text'])
            elif isinstance(part, str):
                parts.append(part)
        return ''.join(parts)
    return ''


def _payload_usage(data: Dict[str, Any]) -> Dict[str, int]:
    usage = data.get('usage') or {}
    return {
        'prompt_tokens': int(usage.get('prompt_tokens') or 0),
        'completion_tokens': int(usage.get('completion_tokens') or 0),
        'total_tokens': int(usage.get('total_tokens') or 0),
    }


def _clean_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只保留 OpenAI 认识的键，避免把内部字段发给第三方接口。"""
    cleaned: List[Dict[str, Any]] = []
    allowed = {'role', 'content', 'name', 'tool_call_id', 'tool_calls'}
    for message in messages or []:
        item = {k: v for k, v in dict(message).items() if k in allowed and v is not None}
        if not item.get('role'):
            continue
        if item['role'] not in ('assistant',) and item.get('content') is None:
            item['content'] = ''
        cleaned.append(item)
    return cleaned


async def chat(
    messages: List[Dict[str, Any]],
    *,
    role: Optional[str] = None,
    provider_id: Optional[int] = None,
    model: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_TIMEOUT,
    tools: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[int] = None,
) -> Dict[str, Any]:
    """调用一次 LLM。

    返回 `{'text', 'usage', 'model', 'provider_id', 'tool_calls', 'raw'}`。
    失败抛 `LLMError`（同时把失败也写进 `agent_runs`，status='error'）。
    """
    provider = resolve_provider(role, provider_id)
    use_model = (model or provider['model'] or '').strip()
    body: Dict[str, Any] = {
        'model': use_model,
        'messages': _clean_messages(messages),
        'temperature': float(temperature if temperature is not None else DEFAULT_TEMPERATURE),
    }
    if tools:
        body['tools'] = tools
        body['tool_choice'] = 'auto'

    url = f"{provider['base_url']}/chat/completions"
    headers = {'Content-Type': 'application/json'}
    if provider['api_key']:
        headers['Authorization'] = f"Bearer {provider['api_key']}"

    started = time.time()
    last_error = ''
    attempts = RETRY_TIMES + 1
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=float(timeout or DEFAULT_TIMEOUT)) as client:
                response = await client.post(url, json=body, headers=headers)
            latency_ms = int((time.time() - started) * 1000)
            if response.status_code >= 400:
                last_error = f'HTTP {response.status_code}: {response.text[:300]}'
                if response.status_code < 500 and response.status_code not in (408, 429):
                    break  # 4xx 重试没意义（key 错、模型名错、参数错）
                continue
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError):
                last_error = f'返回体不是 JSON: {response.text[:200]}'
                continue
            if isinstance(data, dict) and data.get('error'):
                err = data['error']
                last_error = f"接口返回错误: {err.get('message') if isinstance(err, dict) else err}"
                break
            usage = _payload_usage(data if isinstance(data, dict) else {})
            message = ((data.get('choices') or [{}])[0].get('message') or {}) if isinstance(data, dict) else {}
            _record_run(
                conversation_id=conversation_id,
                role=role or '',
                provider_id=provider['id'],
                model=use_model,
                prompt_tokens=usage['prompt_tokens'],
                completion_tokens=usage['completion_tokens'],
                latency_ms=latency_ms,
                status='ok',
            )
            return {
                'text': _payload_text(data),
                'usage': usage,
                'model': use_model,
                'provider_id': provider['id'],
                'tool_calls': message.get('tool_calls') or [],
                'raw': data,
            }
        except httpx.TimeoutException:
            last_error = f'请求超时（{timeout}s）'
        except httpx.HTTPError as exc:
            last_error = f'网络错误: {exc}'
        except LLMError:
            raise
        except Exception as exc:  # pragma: no cover - 兜底
            last_error = f'{type(exc).__name__}: {exc}'

    latency_ms = int((time.time() - started) * 1000)
    _record_run(
        conversation_id=conversation_id,
        role=role or '',
        provider_id=provider['id'],
        model=use_model,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=latency_ms,
        status='error',
        error=last_error,
    )
    raise LLMError(f'调用模型失败：{last_error}', provider_id=provider['id'])


async def chat_text(
    messages: List[Dict[str, Any]],
    *,
    role: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """只要文本的便捷封装（跨商品协查里用得多）。"""
    result = await chat(messages, role=role, **kwargs)
    return result['text']


def recent_runs(limit: int = 100, role: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = 'SELECT * FROM agent_runs'
    params: List[Any] = []
    if role:
        sql += ' WHERE role = ?'
        params.append(role)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in db.query(sql, params)]


def role_defaults(role: str) -> Dict[str, Any]:
    """给 UI 用：角色当前的温度（没配置就用默认）。"""
    return {'role': role, 'temperature': roles_mod.role_temperature(role)}


__all__ = [
    'DEFAULT_TEMPERATURE',
    'DEFAULT_TIMEOUT',
    'LLMError',
    'chat',
    'chat_text',
    'recent_runs',
    'resolve_provider',
]
