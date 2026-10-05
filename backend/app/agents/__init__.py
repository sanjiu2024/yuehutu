"""多 AI 编排包（本项目的核心卖点）。

模块分工：
    defaults.py  默认中文提示词（可被 UI 覆盖）
    roles.py     6 个角色定义 + `ai_roles` 表读写 + 会话 → 角色
    prompts.py   提示词三级覆盖（global < account < item，priority 大者胜）
    llm.py       统一 LLM 调用（提供者解析、OpenAI 兼容、超时重试、用量落 agent_runs）
    tools.py     `TOOL_SPECS` 工具定义（与 mcp_server 同一份）+ `call_tool()`
    runtime.py   运行时：买家消息回复、主管 AI 回合、跨商品协查
"""

from __future__ import annotations

from . import defaults, llm, prompts, roles, runtime, tools  # noqa: F401
from .defaults import DEFAULT_ROLE_PROMPTS  # noqa: F401
from .llm import chat, resolve_provider  # noqa: F401
from .prompts import list_prompts, system_prompt, upsert_prompt  # noqa: F401
from .roles import ROLES, role_for_conversation  # noqa: F401
from .runtime import cross_ask, recent_agent_messages, reply_for_message, run_master  # noqa: F401
from .tools import MASTER_TOOLS, TOOL_NAMES, TOOL_SPECS, call_tool  # noqa: F401

__all__ = [
    'DEFAULT_ROLE_PROMPTS',
    'MASTER_TOOLS',
    'ROLES',
    'TOOL_NAMES',
    'TOOL_SPECS',
    'call_tool',
    'chat',
    'cross_ask',
    'defaults',
    'list_prompts',
    'llm',
    'prompts',
    'recent_agent_messages',
    'reply_for_message',
    'resolve_provider',
    'roles',
    'run_master',
    'runtime',
    'system_prompt',
    'tools',
    'upsert_prompt',
    'role_for_conversation',
]
