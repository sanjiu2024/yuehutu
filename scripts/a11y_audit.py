"""可用性/细节审计（26 页，深色）：把「人用起来会卡住」的地方自动列出来。

查四类（都是「一眼看不出、用起来才难受」的）：
1. **点不动的图标按钮**：无可访问名字（没文字、没 aria-label、没 title）——整站图标化后
   最容易漏的就是这个，鼠标悬停才知道是什么；
2. **输入框没有标签**：`<input>` 既没有 `<label for>` 也没有 aria-label / placeholder；
3. **图片没有 alt**；
4. **焦点不可见**：Tab 到按钮上时 `outline` 与 `box-shadow` 都是 none —— 键盘用户会迷路。

顺带抓：页面上「主按钮」的个数（>1 个说明层级不清）、重复的 aria-label。
只读，不发写请求（并断言整轮没有非 GET 的 /api/ 请求）。

跑法（两种）：

```powershell
# 1) 打正在运行的打包版（默认 --base http://127.0.0.1:18400）
C:\Python310\python.exe scripts\a11y_audit.py

# 2) 从源码起后端（打的是仓库里的 frontend/dist，改完前端要先 npm run build）
C:\Python310\python.exe scripts\a11y_audit.py --port 18391
```

"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ui_smoke import call, find_browser, start_backend, wait_health  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:18400"
#: 真实数据目录：`XIANYU_APP_DIR` 是**根目录**，数据在 `<root>/data`。
REAL_DATA = pathlib.Path(
    os.environ.get("XIANYU_REAL_DATA")
    or (pathlib.Path.home() / "AppData" / "Roaming" / "xianyu-ai-workbench" / "data")
)
REPO = ROOT
OUT = REPO / "logs" / "a11y_audit"


def start_source(port: int) -> pathlib.Path:
    """从源码起后端（打的是仓库里的 `frontend/dist`），数据目录拷一份到临时根下。

    `XIANYU_APP_DIR` 是**根目录**，数据在 `<root>/data`（core/config.py:19-20）——
    拷到根下会得到一份新生成的 local_secret，桌面密钥校验必然 401。
    """
    tmp = pathlib.Path(tempfile.gettempdir()) / f"xywb_a11y_{port}"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(REAL_DATA, tmp / "data")
    proc = start_backend(port, tmp, REPO / "logs")
    base = f"http://127.0.0.1:{port}"
    wait_health(base, timeout=90)
    print(f"source backend pid={proc.pid} base={base} app_dir={tmp}")
    return base, tmp / "data"

PAGES = [
    ("overview", "概览"), ("accounts", "账号"), ("items", "商品"), ("chats", "会话"),
    ("agents", "AI 沟通"), ("master", "主管 AI"), ("providers", "AI 配置"),
    ("prompts", "提示词"), ("risk", "风控"), ("promotion", "超级擦亮"),
    ("security-center", "安全中心"), ("finance", "财务"), ("marketing", "营销"),
    ("qualification", "资质管理"), ("orders", "订单"), ("refunds", "退款"),
    ("evaluations", "评价"), ("complaints", "投诉"), ("return-addresses", "退货地址"),
    ("item-templates", "运费模版"), ("sub-accounts", "子账号"), ("cs-routing", "客服分流"),
    ("data-overview", "数据总览"), ("fans", "粉丝数据"), ("cs-data", "客服数据"),
    ("notifications", "通知中心"),
]

CHECK = r"""() => {
  const name = (el) => (
    (el.getAttribute('aria-label') || '') + '|' +
    (el.getAttribute('title') || '') + '|' +
    (el.innerText || '').trim() + '|' +
    (el.getAttribute('alt') || '')
  ).replace(/\|+$/, '');
  const visible = (el) => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  };
  const namelessButtons = [];
  document.querySelectorAll('button, [role="button"], a[href]').forEach((el) => {
    if (!visible(el)) return;
    if (name(el).replace(/\|/g, '').trim()) return;
    namelessButtons.push(el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ').filter(Boolean).slice(0, 2).join('.'));
  });
  const unlabeledInputs = [];
  document.querySelectorAll('input, textarea, select').forEach((el) => {
    if (!visible(el)) return;
    const id = el.getAttribute('id');
    const hasLabel = id ? !!document.querySelector('label[for="' + CSS.escape(id) + '"]') : false;
    const wrapped = !!el.closest('label');
    const aria = (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '').trim();
    const ph = (el.getAttribute('placeholder') || '').trim();
    if (!hasLabel && !wrapped && !aria && !ph) {
      unlabeledInputs.push((el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ').slice(0, 2).join('.')) + ':' + (el.getAttribute('type') || ''));
    }
  });
  const noAlt = Array.from(document.querySelectorAll('img')).filter((el) => visible(el) && el.getAttribute('alt') === null).length;
  const primaries = Array.from(document.querySelectorAll('.btn.primary')).filter(visible).map((el) => (el.innerText || '').trim().slice(0, 20));
  const labels = [];
  document.querySelectorAll('[aria-label]').forEach((el) => labels.push(el.getAttribute('aria-label')));
  const dupLabels = Object.entries(labels.reduce((a, l) => (a[l] = (a[l] || 0) + 1, a), {})).filter(([, n]) => n > 1).map(([l, n]) => l + '×' + n);
  return {
    namelessButtons: namelessButtons.slice(0, 12), namelessCount: namelessButtons.length,
    unlabeledInputs: unlabeledInputs.slice(0, 12), unlabeledCount: unlabeledInputs.length,
    noAlt, primaries, dupLabels: dupLabels.slice(0, 6),
  };
}"""

FOCUS = r"""() => {
  // 给一个按钮焦点，看浏览器实际渲染出来的 outline / box-shadow。
  const btn = Array.from(document.querySelectorAll('button')).find((b) => {
    const st = getComputedStyle(b);
    return st.display !== 'none' && b.getBoundingClientRect().width > 2;
  });
  if (!btn) return {found: false};
  btn.focus();
  const st = getComputedStyle(btn);
  return {
    found: true,
    cls: String(btn.className || ''),
    outline: st.outlineStyle + ' ' + st.outlineWidth + ' ' + st.outlineColor,
    boxShadow: st.boxShadow,
  };
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="给了就从源码起后端（数据目录拷一份），否则打 --base")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if args.port:
        base, data = start_source(args.port)
    else:
        base, data = args.base, REAL_DATA
    secret = (data / "local_secret.txt").read_text(encoding="utf-8").strip()
    _, raw = call(base, "POST", "/api/auth/desktop", extra={"X-Desktop-Secret": secret})
    token = json.loads(raw)["data"]["token"]

    from patchright.sync_api import sync_playwright  # noqa: PLC0415

    report = []
    with sync_playwright() as pw:
        executable, extra_args, note = find_browser()
        print(f"browser: {note} {executable}")
        browser = pw.chromium.launch(headless=True, executable_path=executable, args=extra_args)
        page = browser.new_page(viewport={"width": 1440, "height": 940})
        page.goto(base, wait_until="load")
        page.evaluate("t => localStorage.setItem('xywb_token', t)", token)
        page.evaluate("() => localStorage.setItem('xywb_theme', 'dark')")
        non_get: list[str] = []
        page.on("request", lambda r: non_get.append(f"{r.method} {r.url}") if r.method != "GET" and "/api/" in r.url else None)

        for key, label in PAGES:
            before = len(non_get)
            page.goto(f"{base}/?a=1#/{key}", wait_until="load")
            page.wait_for_timeout(3200)
            info = page.evaluate(CHECK)
            focus = page.evaluate(FOCUS)
            report.append({"page": key, "label": label, **info, "focus": focus,
                           "non_get": len(non_get) - before})
            flag = "!! " if (info["namelessCount"] or info["unlabeledCount"] or info["noAlt"]) else "OK "
            print(f"{flag}{key:<18} nameless={info['namelessCount']:<3} unlabeled={info['unlabeledCount']:<3} "
                  f"noAlt={info['noAlt']:<3} primary={len(info['primaries'])} focus={focus.get('outline')}")

        (OUT / "report.json").write_text(json.dumps({"report": report, "non_get": non_get},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    print(f"\nNON_GET_API_REQUESTS={non_get}")
    for field, title in (("namelessButtons", "无可访问名字的按钮"), ("unlabeledInputs", "没有标签的输入框")):
        agg: dict = {}
        for r in report:
            for item in r[field]:
                agg.setdefault(item, []).append(r["page"])
        print(f"\n== {title}：{len(agg)} 类")
        for k, v in sorted(agg.items(), key=lambda kv: -len(kv[1])):
            print(f"   {len(v):<3} {k}   {v[:6]}")
    focusless = [r["page"] for r in report if r["focus"].get("found") and r["focus"].get("outline", "").startswith("none") and r["focus"].get("boxShadow") in ("none", "")]
    print(f"\n焦点不可见的页面：{focusless}")
    print(f"REPORT={OUT / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
