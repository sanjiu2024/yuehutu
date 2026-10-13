"""对比度审计（深色 + 浅色两套主题，26 页）：把「看不见的字」自动列出来。

为什么要它：主题是两套（`data-theme="dark|light"`），而页面里的灰字（`--text-mute`
之类）在浅色底上很容易糊掉 —— 截图肉眼看不准，算对比度才准。
口径：只算**自己有文字**的元素，背景沿父链找第一个不透明的 background-color，
对比度 < 3.0 记为可疑（WCAG 对大字的门槛），并把「前景≈背景」单列（那种是完全看不见）。
只读，不发任何写请求。

跑法（两种）：

```powershell
# 1) 打正在运行的打包版（默认 --base http://127.0.0.1:18400）
C:\Python310\python.exe scripts\contrast_audit.py

# 2) 从源码起后端（打的是仓库里的 frontend/dist，改完前端要先 npm run build）
C:\Python310\python.exe scripts\contrast_audit.py --port 18391
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

#: 真实数据目录：`XIANYU_APP_DIR` 是**根目录**，数据在 `<root>/data`。
REAL_DATA = pathlib.Path(
    os.environ.get("XIANYU_REAL_DATA")
    or (pathlib.Path.home() / "AppData" / "Roaming" / "xianyu-ai-workbench" / "data")
)
OUT = ROOT / "logs" / "contrast_audit"


def boot_source_backend(port: int) -> tuple[str, pathlib.Path]:
    """把真实数据目录**拷一份**出来，用它起源码版后端。

    拷贝而不是直接用真实目录：打包版可能正在跑并占着那个 app.db，
    两个进程写同一个 SQLite 是没必要的风险；拷贝后内容一模一样。
    """
    tmp = pathlib.Path(tempfile.gettempdir()) / f"xywb_contrast_{port}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    # `XIANYU_APP_DIR` 是**根目录**，数据在 `<root>/data`（core/config.py:19-20）——
    # 拷到根下会得到一份新生成的 local_secret，桌面密钥校验必然 401。
    shutil.copytree(REAL_DATA, tmp / "data")
    logs = ROOT / "logs"
    proc = start_backend(port, tmp, logs)
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
    ("notifications", "通知中心"), ("shop-profile", "店铺资料"),
]

#: 在页面里跑：返回对比度最低的元素。
MEASURE = r"""() => {
  // 注意：Chromium 把 `color-mix()` 算成 `color(srgb 0.93 0.95 0.99)`（分量是 0~1 浮点），
  // 用「抓数字当 0~255」的朴素解析会读成接近黑色，于是 `.pr-note` 被误报成对比度 1.29。
  // 先按格式分流，认不出的格式（oklch/lab）返回 null 不猜。
  const rgba = (c) => {
    c = (c || '').trim();
    if (c.startsWith('color(srgb')) {
      const m = c.match(/-?[\d.]+(?:e-?\d+)?/gi) || [];
      if (m.length < 3) return null;
      const s = m.map(Number);
      return [Math.round(s[0] * 255), Math.round(s[1] * 255), Math.round(s[2] * 255), s.length > 3 ? s[3] : 1];
    }
    if (!c.startsWith('rgb')) return null;
    const m = c.match(/[\d.]+/g);
    if (!m || m.length < 3) return null;
    const a = m.length > 3 ? Number(m[3]) : 1;
    return [Number(m[0]), Number(m[1]), Number(m[2]), a];
  };
  // 半透明背景必须**合成**，否则 badge / notice 那种 14% 透明底会被当实色，
  // 算出来前景≈背景（对比度 1），全是假阳性。
  const over = (top, bottom) => top.slice(0, 3).map((v, i) => Math.round(v * top[3] + bottom[i] * (1 - top[3])));
  const bgOf = (el) => {
    const chain = [];
    let e = el;
    while (e) { chain.push(e); e = e.parentElement; }
    chain.reverse();                        // html → … → el
    let bg = [255, 255, 255];
    for (const node of chain) {
      const c = rgba(getComputedStyle(node).backgroundColor);
      if (c && c[3] > 0) bg = over(c, bg);
    }
    return bg;
  };
  const lum = (v) => {
    const f = v.map((x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2];
  };
  const ratio = (a, b) => {
    const la = lum(a), lb = lum(b);
    const hi = Math.max(la, lb), lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  };
  const out = [];
  document.querySelectorAll('body *').forEach((el) => {
    if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.closest('svg')) return;
    const direct = Array.from(el.childNodes).some((n) => n.nodeType === 3 && (n.textContent || '').trim().length > 1);
    if (!direct) return;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') return;
    if (Number(st.opacity) < 0.35) return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    if (r.bottom < 0 || r.top > document.documentElement.clientHeight) return;   // 屏外不算
    const fg = rgba(st.color);
    if (!fg || fg[3] === 0) return;
    const bg = bgOf(el);
    const c = ratio(fg.slice(0, 3), bg);
    const text = Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join(' ').slice(0, 60);
    out.push({
      ratio: Math.round(c * 100) / 100,
      sel: el.tagName.toLowerCase() + '.' + String(el.className || '').split(' ').filter(Boolean).slice(0, 2).join('.'),
      fg: st.color, bg: 'rgb(' + bg.join(', ') + ')',
      size: st.fontSize, text,
    });
  });
  out.sort((a, b) => a.ratio - b.ratio);
  return {theme: document.documentElement.dataset.theme || '(none)', worst: out.slice(0, 12), total: out.length};
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="给了就从源码起后端（数据目录拷一份），否则打 --base")
    ap.add_argument("--base", default="http://127.0.0.1:18400")
    ap.add_argument("--threshold", type=float, default=3.0, help="低于它算可疑")
    ap.add_argument("--only", default="", help="只跑这些页（逗号分隔），调试用")
    args = ap.parse_args()

    global PAGES
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        PAGES = [p for p in PAGES if p[0] in want]

    if args.port:
        base, data = boot_source_backend(args.port)
    else:
        base, data = args.base, REAL_DATA

    OUT.mkdir(parents=True, exist_ok=True)
    secret = (data / "local_secret.txt").read_text(encoding="utf-8").strip()
    _, raw = call(base, "POST", "/api/auth/desktop", extra={"X-Desktop-Secret": secret})
    token = json.loads(raw)["data"]["token"]
    print(f"target={base} pages={len(PAGES)}")

    from patchright.sync_api import sync_playwright  # noqa: PLC0415

    report: dict = {}
    with sync_playwright() as pw:
        executable, extra_args, note = find_browser()
        print(f"browser: {note} {executable}")
        browser = pw.chromium.launch(headless=True, executable_path=executable, args=extra_args)
        page = browser.new_page(viewport={"width": 1440, "height": 940})
        page.goto(base, wait_until="load")
        page.evaluate("t => localStorage.setItem('xywb_token', t)", token)
        non_get: list[str] = []
        page.on("request", lambda r: non_get.append(f"{r.method} {r.url}") if r.method != "GET" and "/api/" in r.url else None)

        for theme in ("dark", "light"):
            page.evaluate("t => localStorage.setItem('xywb_theme', t)", theme)
            rows = []
            for key, label in PAGES:
                # 只换 hash 是**不会重新加载**的（SPA），主题就永远停在启动时那次；
                # 带一个变化的 query 才会真正重载文档。
                page.goto(f"{base}/?th={theme}#/{key}", wait_until="load")
                page.wait_for_timeout(3200)
                info = page.evaluate(MEASURE)
                assert info["theme"] == theme, f"主题没生效：{key} 期望 {theme} 实际 {info['theme']}"
                bad = [w for w in info["worst"] if w["ratio"] < args.threshold]
                rows.append({"page": key, "label": label, "theme_seen": info["theme"],
                             "texts": info["total"], "bad": bad, "worst": info["worst"][:3]})
                worst = info["worst"][0]["ratio"] if info["worst"] else 9.99
                flag = "!! " if bad else "OK "
                print(f"{flag}{theme:<5} {key:<18} texts={info['total']:<5} worst={worst:<5} bad={len(bad)}")
            report[theme] = rows

        (OUT / "report.json").write_text(json.dumps({"report": report, "non_get": non_get},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    print(f"\nNON_GET_API_REQUESTS={non_get}")
    for theme in ("dark", "light"):
        pages_bad = [r["page"] for r in report[theme] if r["bad"]]
        print(f"[{theme}] BAD_PAGES {len(pages_bad)}/{len(report[theme])}: {pages_bad}")
        seen = {}
        for r in report[theme]:
            for b in r["bad"]:
                seen.setdefault(f"{b['sel']} {b['fg']} on {b['bg']}", []).append(f"{r['page']}:{b['ratio']}")
        for k, v in sorted(seen.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]:
            print(f"    {len(v):<3} {k}   e.g. {v[0]}")
    print(f"REPORT={OUT / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
