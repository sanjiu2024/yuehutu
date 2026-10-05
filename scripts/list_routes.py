"""列出当前后端已注册的全部路由（集成验收用）。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from backend.app.main import create_app  # noqa: E402

app = create_app()
schema = app.openapi()
rows = []
for path, spec in schema.get("paths", {}).items():
    methods = ",".join(sorted(m.upper() for m in spec if m in {"get", "post", "put", "delete", "patch"}))
    rows.append((path, methods))
for path, methods in sorted(rows):
    print(f"{methods:26} {path}")
print(f"--- {len(rows)} paths")
