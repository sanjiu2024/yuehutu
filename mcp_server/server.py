"""闲鱼 AI 工作台 —— DSH 插件（MCP stdio server）。

手写 JSON-RPC 2.0 over stdio，不依赖 `mcp` SDK（Python 3.10 标准库）。

协议方法：
- `initialize`                → 能力协商（tools）
- `notifications/initialized` → 通知，**不回包**
- `tools/list`                → 工具清单（唯一来源：`backend/app/agents/tools.py::TOOL_SPECS`，
                                不可用时退回 `mcp_server/client.py::CANONICAL_SPECS`）
- `tools/call`                → 转发到本地后端 REST（`mcp_server/client.py`）
- `ping` / `resources/list` / `prompts/list` → 空实现，兼容客户端探测

stdout 只写 JSON-RPC 行（每行一个消息，`\\n` 分隔）；日志一律走 stderr。

单独试跑：
    C:\\Python310\\python.exe mcp_server\\server.py --selftest
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

if __package__ in (None, ''):                       # 允许 `python mcp_server/server.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.client import BackendClient, canonical_name, error, tool_specs, tool_specs_source

SERVER_NAME = 'xianyu-ai-workbench'
SERVER_VERSION = '0.1.0'
PROTOCOL_VERSION = '2024-11-05'
SUPPORTED_PROTOCOL_VERSIONS = ('2024-11-05', '2024-10-07', '2025-03-26', '2025-06-18')

JSONRPC_VERSION = '2.0'
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    """日志只走 stderr —— stdout 是协议通道，多写一个字符都会让客户端解析失败。"""
    print(f'[mcp] {message}', file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# JSON-RPC 信封
# --------------------------------------------------------------------------

def rpc_result(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {'jsonrpc': JSONRPC_VERSION, 'id': msg_id, 'result': result}


def rpc_error(msg_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {'code': code, 'message': message}
    if data is not None:
        payload['data'] = data
    return {'jsonrpc': JSONRPC_VERSION, 'id': msg_id, 'error': payload}


# --------------------------------------------------------------------------
# 方法处理
# --------------------------------------------------------------------------

def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = (params or {}).get('protocolVersion')
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        'protocolVersion': version,
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
        'instructions': (
            '闲鱼 AI 工作台的控制接口。要指挥这个软件，优先用 master_chat 把用户的话转给主管 AI；'
            '其余工具直接操作账号/商品/会话/订单/风控。'
            f'工具定义来源：{tool_specs_source()}。'
        ),
    }


def handle_tools_list(params: Dict[str, Any]) -> Dict[str, Any]:
    return {'tools': tool_specs()}


def handle_tools_call(params: Dict[str, Any], client: BackendClient) -> Dict[str, Any]:
    params = params or {}
    name = params.get('name')
    arguments = params.get('arguments') or {}
    if not isinstance(name, str) or not name:
        return _tool_error('invalid_args', 'tools/call 缺少 name')
    if not isinstance(arguments, dict):
        return _tool_error('invalid_args', 'arguments 必须是对象')
    result = client.call_tool(name, arguments)
    return _tool_error_result(result) if not result.get('success') else {
        'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, indent=2)}],
        'isError': False,
    }


def _tool_error(code: str, message: str) -> Dict[str, Any]:
    return _tool_error_result(error(code, message))


def _tool_error_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, indent=2)}],
        'isError': True,
    }


def dispatch(msg: Dict[str, Any], client: BackendClient) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息；通知（无 id）返回 None。"""
    if not isinstance(msg, dict):
        return rpc_error(None, INVALID_REQUEST, '请求必须是 JSON 对象')
    method = msg.get('method')
    msg_id = msg.get('id')
    params = msg.get('params') or {}
    if not isinstance(params, dict):
        params = {}
    is_notification = 'id' not in msg

    if not isinstance(method, str):
        return None if is_notification else rpc_error(msg_id, INVALID_REQUEST, '缺少 method')

    # 通知：一律不回包
    if method.startswith('notifications/'):
        return None

    try:
        if method == 'initialize':
            result: Any = handle_initialize(params)
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = handle_tools_list(params)
        elif method == 'tools/call':
            result = handle_tools_call(params, client)
        elif method in ('resources/list', 'resources/templates/list'):
            result = {'resources': []}
        elif method == 'prompts/list':
            result = {'prompts': []}
        elif method == 'logging/setLevel':
            result = {}
        else:
            return None if is_notification else rpc_error(
                msg_id, METHOD_NOT_FOUND, f'不支持的方法：{method}')
    except Exception as exc:                          # 任何异常都不许把 server 打死
        log(f'{method} 内部错误：{exc}\n{traceback.format_exc()}')
        return None if is_notification else rpc_error(
            msg_id, INTERNAL_ERROR, f'{type(exc).__name__}: {exc}')

    if is_notification:
        return None
    return rpc_result(msg_id, result)


# --------------------------------------------------------------------------
# stdio 主循环
# --------------------------------------------------------------------------

def _write(message: Dict[str, Any], stream: Any) -> None:
    try:
        stream.write(json.dumps(message, ensure_ascii=False) + '\n')
        stream.flush()
    except (BrokenPipeError, ValueError):
        # 客户端已经断开：让主循环自然收尾，别打栈。
        raise EOFError from None


def _safe_write(message: Dict[str, Any], stream: Any) -> bool:
    """写一帧；客户端已断开时返回 False，让主循环干净退出。"""
    try:
        _write(message, stream)
    except EOFError:
        log('stdout 已关闭，退出')
        return False
    return True


def serve(stdin: Any = None, stdout: Any = None, client: Optional[BackendClient] = None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    client = client or BackendClient()
    log(f'启动：base_url={client.base_url} token来源={client.token_source} '
        f'工具定义={tool_specs_source()}')

    while True:
        # 不用 `for line in stdin`：stdin 上一个非 UTF-8 字节会让迭代抛
        # UnicodeDecodeError，整个 server 直接打栈退出（客户端只看到掉线）。
        try:
            line = stdin.readline()
        except UnicodeDecodeError as exc:
            _safe_write(rpc_error(None, PARSE_ERROR, f'输入不是合法 UTF-8：{exc}'), stdout)
            continue
        except (OSError, ValueError):
            break
        if not line:                       # EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            _safe_write(rpc_error(None, PARSE_ERROR, f'JSON 解析失败：{exc}'), stdout)
            continue
        response = dispatch(msg, client)
        if response is not None and not _safe_write(response, stdout):
            break
    log('stdin 关闭，退出')
    return 0


def selftest() -> int:
    """不接客户端，直接打一遍内部逻辑，方便人肉排查。"""
    client = BackendClient()
    print(f'base_url      = {client.base_url}')
    print(f'地址来源      = {client.base_url_source}')
    print(f'token 来源    = {client.token_source}')
    print(f'工具定义来源  = {tool_specs_source()}')
    specs = tool_specs()
    print(f'工具数量      = {len(specs)}')
    print('工具名        = ' + ', '.join(s['name'] for s in specs))
    print('--- 别名抽查 ---')
    for alias in ('list_items', 'get_item', 'send_message', 'list_agent_messages'):
        print(f'  {alias} -> {canonical_name(alias)}')
    print('--- 后端连通性 ---')
    print(json.dumps(client.health(), ensure_ascii=False))
    return 0


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stdin):
        try:
            # errors='replace'：stdin 上一个坏字节不该打死整个 server。
            stream.reconfigure(encoding='utf-8', errors='replace')  # type: ignore[union-attr]
        except Exception:
            pass
    if '--selftest' in argv:
        return selftest()
    if '--version' in argv:
        print(f'{SERVER_NAME} {SERVER_VERSION} (mcp protocol {PROTOCOL_VERSION})')
        return 0
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    return serve()


if __name__ == '__main__':
    raise SystemExit(main())
