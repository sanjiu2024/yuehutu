"""集成冒烟：起着的后端上把契约接口真打一遍（只读 + 不花钱的写）。

用法：
    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& C:\\Python310\\python.exe scripts\\smoke_api.py [port]"
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18090
BASE = f"http://127.0.0.1:{PORT}"

# 凭据路径要跟着后端走：带 XIANYU_APP_DIR 起后端（临时数据目录，CI/冒烟常用）时，
# 硬编码 %APPDATA% 会读到**别的库**的密码，表现成莫名 401 unauthorized。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from backend.app.core import config as _config  # noqa: E402

CRED = _config.CRED_PATH


def call(method: str, path: str, token: str = "", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    cred = json.loads(CRED.read_text(encoding="utf-8"))
    status, raw = call("POST", "/api/auth/login", body={"username": cred["username"], "password": cred["password"]})
    print(f"login -> {status} {raw[:160]}")
    if status != 200:
        return 1
    token = json.loads(raw)["data"]["token"]

    checks = [
        ("GET", "/api/health", None),
        ("GET", "/api/auth/me", None),
        ("GET", "/api/accounts", None),
        ("GET", "/api/items", None),
        ("GET", "/api/items/stats", None),
        ("GET", "/api/chats/conversations", None),
        ("GET", "/api/orders", None),
        ("GET", "/api/risk/status", None),
        ("GET", "/api/risk/events", None),
        ("GET", "/api/agents/roles", None),
        ("GET", "/api/agents/prompts", None),
        ("GET", "/api/agents/messages", None),
        ("GET", "/api/agents/runs", None),
        ("GET", "/api/providers", None),
        ("GET", "/api/settings", None),
        ("GET", "/api/system/info", None),
        ("GET", "/api/items/operation-logs", None),
    ]
    bad = []
    for method, path, body in checks:
        status, raw = call(method, path, token, body)
        ok = status == 200 and '"success":true' in raw.replace(" ", "")
        print(f"{'OK ' if ok else 'BAD'} {status} {method} {path} -> {raw[:120]}")
        if not ok:
            bad.append(path)

    # 无 token 必须 401 + 统一信封
    status, raw = call("GET", "/api/accounts")
    ok401 = status == 401 and '"success":false' in raw.replace(" ", "")
    print(f"{'OK ' if ok401 else 'BAD'} {status} GET /api/accounts (无 token) -> {raw[:120]}")
    if not ok401:
        bad.append("/api/accounts(no-token)")

    print("-" * 50)
    print("SMOKE FAIL: " + ", ".join(bad) if bad else "SMOKE ALL OK")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
