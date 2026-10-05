"""DSH 插件：把闲鱼 AI 工作台暴露成 MCP 工具（stdio JSON-RPC）。

入口：`python mcp_server/server.py`（见 docs/MCP.md）。
"""

from .client import BackendClient, canonical_name, tool_specs, tool_specs_source  # noqa: F401

__all__ = ['BackendClient', 'canonical_name', 'tool_specs', 'tool_specs_source']
