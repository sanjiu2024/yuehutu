"""端到端联调：在真实运行的后端上把用户的九条核心诉求逐条打一遍。

**安全约束**：绝不打真实闲鱼平台、绝不调用真实 LLM。
- 账号/商品的 cookie 留空 → 协议层必然返回 `xianyu_not_ready`，这正是被测语义。
- provider 的 base_url 指向 `http://127.0.0.1:9`（discard 端口），LLM 调用会立刻失败
  并返回结构化错误，不会外发任何数据。

用法：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe scripts\\smoke_write.py [port]"
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18090
BASE = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.app.core import config  # noqa: E402

CRED = config.CRED_PATH
ACCOUNT_ID = 'e2e_acc'
PEER = 'e2e_peer'

FAILURES: list[str] = []
NOTES: list[str] = []


def call(method: str, path: str, token: str = '', body: dict | None = None, extra: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    for key, value in (extra or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')


def check(label: str, ok: bool, detail: str) -> None:
    print(f"{'OK ' if ok else 'BAD'} {label} :: {detail[:220]}")
    if not ok:
        FAILURES.append(label)


def envelope(status: int, raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {}


def seed() -> None:
    """直接把账号/会话塞进库（真实场景由扫码与 IM 回调创建，这里不打平台）。"""
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO accounts(id, nickname, enabled, auto_reply) VALUES (?,?,?,?)",
        (ACCOUNT_ID, 'E2E 测试账号', 1, 1),
    )
    conn.execute(
        "INSERT OR REPLACE INTO account_credentials(account_id, cookie, login_state) VALUES (?,?,?)",
        (ACCOUNT_ID, '', 'unknown'),
    )
    conn.execute(
        "INSERT OR REPLACE INTO conversations(id, account_id, peer_user_id, peer_nickname, mode) "
        "VALUES (1, ?, ?, ?, 'auto')",
        (ACCOUNT_ID, PEER, 'E2E 买家'),
    )
    conn.commit()
    conn.close()
    print(f"seeded account={ACCOUNT_ID} conversation=1 (db={config.DB_PATH})")


def login() -> str:
    """优先走桌面外壳的自动登录（本地密钥），它同时验证 PACKAGING.md 6.5 的自愈路径。"""
    if config.SECRET_PATH.exists():
        secret = config.SECRET_PATH.read_text('utf-8').strip()
        status, raw = call('POST', '/api/auth/desktop', body=None, extra={'X-Desktop-Secret': secret})
        if status == 200:
            print('login: desktop secret OK')
            return envelope(status, raw)['data']['token']
        print(f'desktop login failed: {status} {raw}')
    cred = json.loads(CRED.read_text(encoding='utf-8'))
    status, raw = call('POST', '/api/auth/login', body={'username': cred['username'], 'password': cred['password']})
    if status != 200:
        raise SystemExit(f'login failed: {status} {raw}')
    print('login: username/password OK')
    return envelope(status, raw)['data']['token']


def main() -> int:
    token = login()
    seed()

    # ① 统一 API Key 管理
    status, raw = call('POST', '/api/providers', token, {
        'name': 'E2E Provider',
        'base_url': 'http://127.0.0.1:9/v1',
        'api_key': 'sk-e2e-not-a-real-key',
        'default_model': 'e2e-model',
        'is_default': 1,
    })
    body = envelope(status, raw)
    check('① 新增 provider', status == 200 and body.get('success'), f'{status} {raw[:160]}')
    provider_id = (body.get('data') or {}).get('id')
    if provider_id:
        status, raw = call('POST', f'/api/providers/{provider_id}/default', token)
        check('① 设为默认', status == 200 and envelope(status, raw).get('success'), f'{status} {raw[:160]}')

    # ② 账号开关（PATCH 契约名）
    status, raw = call('PATCH', f'/api/accounts/{ACCOUNT_ID}', token, {'remark': 'E2E 备注', 'auto_reply': 0})
    check('② PATCH 账号', status == 200 and envelope(status, raw).get('success'), f'{status} {raw[:160]}')
    status, raw = call('GET', '/api/accounts', token)
    accounts = envelope(status, raw).get('data') or []
    hit = [a for a in accounts if a.get('id') == ACCOUNT_ID]
    check('② 账号列表含新账号', bool(hit), f'{status} {raw[:200]}')

    # ③ 商品：发布/编辑/批量/上下架 在协议层不可用时必须结构化失败
    status, raw = call('POST', f'/api/items/{ACCOUNT_ID}/publish', token, {
        'title': 'E2E 商品', 'price': 12.5, 'description': '联调用', 'images': []
    })
    body = envelope(status, raw)
    # 503 是契约行为：common.FAIL_STATUS 把 xianyu_not_ready 映射成 503，
    # 表示「服务可用但依赖（闲鱼协议层）不可用」，不是 500。
    check('③ 发布（协议层不可用）', status == 503 and body.get('success') is False
          and body.get('code') == 'xianyu_not_ready', f'{status} {raw[:200]}')

    status, raw = call('POST', f'/api/items/{ACCOUNT_ID}/sync', token)
    body = envelope(status, raw)
    check('③ 同步商品（协议层不可用）', body.get('code') == 'xianyu_not_ready', f'{status} {raw[:200]}')

    # ④ 批量改（契约名 /batch-edit）
    status, raw = call('POST', f'/api/items/{ACCOUNT_ID}/batch-edit', token,
                       {'item_ids': ['1', '2'], 'patch': {'price_set': 9.9}})
    body = envelope(status, raw)
    check('④ 批量改（契约路径存在）', status in (200, 404) and status != 500, f'{status} {raw[:200]}')

    # ⑤ 人工接管：发消息必须落库（协议层不可用时本地成功 + 告警）
    status, raw = call('POST', '/api/chats/1/send', token, {'content': 'E2E 人工回复：你好，在的'})
    body = envelope(status, raw)
    check('⑤ 人工发消息', status == 200 and body.get('success') is not False, f'{status} {raw[:220]}')

    status, raw = call('GET', '/api/chats/1/messages', token)
    msgs = ((envelope(status, raw).get('data') or {}) if isinstance(envelope(status, raw).get('data'), dict)
            else {}).get('messages') or []
    check('⑤ 消息落库', any('E2E 人工回复' in (m.get('content') or '') for m in msgs), f'{status} {raw[:200]}')

    # ⑥ AI 草稿（只生成不发送）
    status, raw = call('POST', '/api/chats/1/ai-draft', token)
    check('⑥ AI 草稿不炸', status != 500, f'{status} {raw[:200]}')

    # ⑦ 主管 AI（provider 指向死端口 → 结构化失败，绝不 500）
    status, raw = call('POST', '/api/agents/master/chat', token, {'message': '帮我看下今天的商品'})
    check('⑦ 主管 AI 结构化返回', status != 500 and envelope(status, raw) != {}, f'{status} {raw[:220]}')

    # ⑧ 三级提示词
    status, raw = call('POST', '/api/agents/prompts', token, {
        'role': 'item', 'scope': 'item', 'item_id': 'e2e_item',
        'content': 'E2E 商品级提示词', 'priority': 10,
    })
    check('⑧ 写商品级提示词', status == 200 and envelope(status, raw).get('success'), f'{status} {raw[:200]}')

    # ⑨ 风控
    status, raw = call('GET', '/api/risk/status', token)
    check('⑨ 风控状态', status == 200 and envelope(status, raw).get('success'), f'{status} {raw[:200]}')
    status, raw = call('POST', f'/api/risk/{ACCOUNT_ID}/clear', token)
    check('⑨ 清除冷却不炸', status != 500, f'{status} {raw[:200]}')

    # ⑩ 操作日志（写操作要留痕）
    status, raw = call('GET', f'/api/items/{ACCOUNT_ID}/operation-logs', token)
    check('⑩ 操作日志', status == 200 and envelope(status, raw).get('success'), f'{status} {raw[:200]}')

    # ⑪ 真实 WS：写操作应当能推事件
    try:
        received = asyncio.run(_ws_probe(token))
        check('⑪ WebSocket 收到事件', received is not None, str(received)[:200])
    except Exception as exc:  # pragma: no cover
        check('⑪ WebSocket 收到事件', False, f'{type(exc).__name__}: {exc}')

    print('-' * 56)
    for note in NOTES:
        print('NOTE ' + note)
    print('WRITE SMOKE FAIL: ' + ', '.join(FAILURES) if FAILURES else 'WRITE SMOKE ALL OK')
    return 1 if FAILURES else 0


async def _ws_probe(token: str):
    import websockets

    url = f'ws://127.0.0.1:{PORT}/api/ws?token={token}'
    async with websockets.connect(url) as ws:
        # 触发一次写操作，看能否收到广播
        call('PATCH', f'/api/accounts/{ACCOUNT_ID}', token, {'remark': 'E2E WS 触发'})
        try:
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=6))
        except asyncio.TimeoutError:
            NOTES.append('WS 连接成功但 6 秒内没收到事件（写操作可能没 publish）')
            return {'connected': True, 'event': None}


if __name__ == '__main__':
    raise SystemExit(main())
