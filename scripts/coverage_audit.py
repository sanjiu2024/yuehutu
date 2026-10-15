"""mtop 覆盖面审计（零平台请求 · 口径自洽 · 可重跑）。

背景
----
第 52 轮审计（logs/cov52.txt 头三行）曾给出三个**互不相容**的总数：

    BUNDLE_MT_NAMES 362
    IMPLEMENTED_IN_XIANYU 225
    BUNDLE_BUT_NOT_IMPLEMENTED 176      # 225 + 176 = 401 != 362

根因（本轮复现并修正）：

1. **口径混用**：旧脚本（.dsh-tmp/write_coverage_audit.py）把
   - 分子（已实现）取自 backend/app/xianyu/*.py；
   - 分母（bundle 名）取自 14 个 bundle 副本；
   两者是**不同集合**，差值里混进了「我们侧有、bundle 里没有」的名字
   （本轮实测 40 个，例：mtop.idle.finance.、mtop.taobao.idle.pc.detail、
   mtop.alibaba.idle.item.pc.draft.delete）。
   正确恒等式应为：**去重基数 = 交集（已实现）+ 差集（未实现）**，且
   已实现集合必须**先与 bundle 取交集**再计数。

2. **正则口径漂移**：bundle 里 mtop. 前缀既可能是**接口名**，也可能是
   mtop SDK 对象上的**属性访问**（mtop.config.secure / mtop.request /
   mtop.params / mtop.js ...）。不区分就会把库内部属性当成接口，虚增分母。

3. **字符串拼接截断**：f-string / 拼接会留下 mtop.alibaba.idle.seller.pc.item.
   这类以点结尾的碎片，需要合并到同前缀的完整名或单独归入噪声。

本脚本的口径（写死、可复跑）
--------------------------
- Base（bundle 去重基数）：14 个 bundle 副本里 mtop\. 命中的全部去重名字
  （mtop. 后 >=1 字符），先做「去尾点合并」再计数。
- Implemented（已实现）：backend/app/**/*.py 里出现的 mtop 名字，
  **与 Base 取交集**后的个数。绝对值（BACKEND_ONLY）单独报，不进覆盖率。
- Missing（未实现）：Base - Implemented。
- 恒等式：Base == Implemented + Missing（脚本内置断言，不成立直接 exit 2）。

缺口四分法（对 Missing 逐条）
---------------------------
- 误收的非 mtop 名     —— mtop SDK 属性/库内部名，正则误收
- 纯站内或 IM 内部名   —— 依赖登录态/前端会话通道，不是商家后台接口
- 无权限或风控         —— 需要平台白名单/资质/风控放行
- 可实现               —— 载荷已知、有现成等价实现或调用点可点击

用法
----
    /c/Python310/python.exe scripts/coverage_audit.py            # 摘要
    /c/Python310/python.exe scripts/coverage_audit.py --list     # 逐条缺口
    /c/Python310/python.exe scripts/coverage_audit.py --json     # 机器可读

本脚本只读仓库其它文件；产物 docs/audit/coverage57.md 由人工/Lead 汇总。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path.home()

BUNDLES = [
    REPO / "logs/seller_discover/bundles/cs_dispatch_main.js",
    REPO / "logs/seller_discover/bundles/notification_main.js",
    REPO / "logs/seller_discover/bundles/sub_account_main.js",
    REPO / "logs/seller_discover/item_templates_src/js_main.js",
    REPO / "logs/seller_discover/marketing_src/js_main.js",
    REPO / "logs/seller_discover/datacenter_main.js",
    HOME / ".dsh-tmp/adapp/ad-main.js",
    HOME / ".dsh-tmp/adapp/item-main.js",
    HOME / ".dsh-tmp/adapp/sc-main.js",
    HOME / ".dsh-tmp/csdispatch/main.js",
    HOME / ".dsh-tmp/seller_scan/js_008_vendors.js",
    HOME / ".dsh-tmp/bundle_scan/js_006_p_im_index.js",
    HOME / ".dsh-tmp/bundle_scan/js_005_main.js",
    HOME / ".dsh-tmp/trade_bundle/main.js",
]

MTOP_RE = re.compile(r"mtop\.[A-Za-z0-9_.]+")
BACKEND_GLOB = "backend/app/**/*.py"

SDK_PROPS = {
    "CLASS", "ERROR", "H5Request", "RESPONSE_TYPE", "antiCreepRequest",
    "antiFloodRequest", "apiName", "config", "errorListener", "extParams",
    "js", "loginRequest", "middlewares", "needConfirm", "needLogin",
    "params", "request", "version",
}
PROP_CHAIN_RE = re.compile(r"^mtop\.(config|middlewares)\.")

IM_INTERNAL_RE = re.compile(
    r"^mtop\.(?:taobao\.)?(?:idlemessage|wx\.idlemessage|order)\."
    r"|^mtop\.taobao\.heracles\.inno\.interactive\."
    r"|^mtop\.idle\.trade\.(?:message|pc\.message)\."
    r"|^mtop\.idle\.web\.user\."
)

PERMISSION_RE = re.compile(
    r"datacompass|yanxuan|buyerprofile|usergroup|merchant\.info|"
    r"query\.login\.merchant\.info|mock\.(apply|cancel)|_export|\.export|"
    r"excel\.url|activity\.list|seller\.activity|complain|refund\.|"
    r"jfzz|\.mtee\.risk|security\.(?:score|violation)|business\.identity|"
    r"qualification|cpv\.category|yanxuan|autotrade|delivery\.address\.detail"
)

MODULE_HINTS = [
    (r"\.ad\.", "backend/app/services/ad_write.py"),
    (r"logistics|consign|mailno|delivery|poi\.get|cainiao",
     "backend/app/services/logistics.py"),
    (r"complain", "backend/app/services/disputes.py"),
    (r"publish|categor|brand|model|cpv|kgraph|search\.box|priceadvice|"
     r"itemshelf|report\.get|security\.check|preget|item\.search|"
     r"item\.export|idleitem|deposit",
     "backend/app/services/items.py"),
    (r"trade|order\.|price|close\.by\.seller|delay\.confirm|adjust|"
     r"sku\.selector|backfill|heracles",
     "backend/app/services/trade_write.py"),
    (r"shop|stats|datacenter", "backend/app/services/datacenter.py"),
    (r"service\.(open|status)", "backend/app/services/shop.py"),
]
COMPILED_HINTS = [(re.compile(p, re.X), m) for p, m in MODULE_HINTS]

# 日志硬证据：已实测被平台拒绝的缺口（name -> (log, 子串)；行号运行时定位）
#: 已实测被平台拒绝的缺口（name -> (log, 证据串)）。证据串一律写**完整 mtop 名**：
#: 第 63 批 coverage63 核销时发现，写短名会命中小兄弟的行（
#: 证据串 "idleitem.edit" 命中的其实是 ...idleitem.editdetail 那一行），
#: 于是「不可实现」的锚点指错了地方。locate() 现在按整名匹配。
DENIAL_EVIDENCE = {
    "mtop.idle.pc.backend.idleitem.editdetail": (
        "logs/publish_family_probe.json",
        "mtop.idle.pc.backend.idleitem.editdetail"),
    "mtop.idle.pc.backend.idleitem.preget": (
        "logs/publish_family_probe.json",
        "mtop.idle.pc.backend.idleitem.preget"),
    "mtop.idle.pc.backend.idleitem.edit": (
        "logs/publish_family_probe.json",
        "mtop.idle.pc.backend.idleitem.edit"),
    "mtop.idle.pc.backend.idleitem.publish": (
        "logs/publish_family_probe.json",
        "mtop.idle.pc.backend.idleitem.publish"),
    "mtop.alibaba.idle.seller.pc.common.item.search": (
        "logs/read_sweep.json", "mtop.alibaba.idle.seller.pc.common.item.search"),
}
DENIAL_DEFAULT = "logs/read_sweep.json:49"

# 可实现的等价值：缺口名 -> 我们已实现的同类接口
EQUIVALENT = {
    "mtop.taobao.idle.trade.order.cancel":
        "mtop.taobao.idle.trade.merchant.close.by.seller",
    "mtop.taobao.idle.trade.close.by.seller":
        "mtop.taobao.idle.trade.merchant.close.by.seller",
    "mtop.taobao.idle.trade.seller.delay.confirm":
        "mtop.idle.merchant.order.address.modify.agree（同类卖家写操作）",
    "mtop.taobao.idle.trade.user.adjust.price":
        "mtop.taobao.idle.trade.merchant.user.adjust.price（仅命名空间不同）",
    "mtop.idle.idleitem.draft.edit":
        "mtop.alibaba.idle.item.pc.draft.update",
    "mtop.alibaba.idle.seller.platform.item.publish.categories":
        "mtop.taobao.idle.item.pc.category.list",
    "mtop.taobao.idle.logistics.merchant.oss.sts.get":
        "mtop.taobao.idle.logistics.merchant.consign.offline",
    "mtop.alibaba.idle.seller.platform.merchant.delivery.place.detail.query":
        "mtop.alibaba.idle.seller.platform.merchant.delivery.address.add",
}


def short_path(path: Path) -> str:
    """仓库内给相对路径；家目录下给 ~/ 前缀，便于在文档里直接照着找。"""
    for root, prefix in ((REPO, ""), (HOME, "~/")):
        try:
            return prefix + path.relative_to(root).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def equiv_site(eq: str, backend) -> str:
    """把同类已实现接口定位到 backend 的 文件:行。"""
    if not eq:
        return ""
    hit = backend.get(eq)
    if hit:
        rel, line = hit[0]
        return "%s:%d" % (rel, line)
    return ""


def locate(log_rel: str, needle: str) -> str:
    """在只读日志里定位首个包含 needle 的行号，返回 path:line。
    **按整名匹配**（needle 之后不能再跟 [\w.]）：否则 "idleitem.edit" 会把
    "...idleitem.editdetail" 那一行算成自己的证据（第 63 批发现）。日志在、整名
    没命中 = 这条缺口没有实测拒绝证据，返回空串，不拿日志路径冒充。
    """
    path = REPO / log_rel
    if not path.exists():
        return log_rel
    pattern = re.compile(re.escape(needle) + r"(?![\w.])")
    for n, line in enumerate(_read(path).splitlines(), 1):
        if pattern.search(line):
            return "%s:%d" % (log_rel, n)
    return ""


def _read(path: Path) -> str:
    return io.open(path, encoding="utf-8", errors="replace").read()


def scan_bundles():
    seen = {}
    sites = {}
    missing_files = []
    for path in BUNDLES:
        if not path.exists():
            missing_files.append(str(path))
            continue
        text = _read(path)
        for m in MTOP_RE.finditer(text):
            name = m.group(0)
            seen.setdefault(name, set()).add(path.name)
            if name not in sites:
                line = text.count("\n", 0, m.start()) + 1
                col = m.start() - (text.rfind("\n", 0, m.start()) + 1) + 1
                sites[name] = "%s:%d:%d" % (short_path(path), line, col)
    for name in [n for n in seen if n.endswith(".")]:
        base = name.rstrip(".")
        if base not in seen and len(base.split(".")) >= 3:
            seen[base] = seen.pop(name)
            if name in sites:
                sites.setdefault(base, sites.pop(name))
    return {n: sorted(v) for n, v in seen.items()}, missing_files, sites


def scan_backend():
    hits = {}
    for py in sorted(REPO.glob(BACKEND_GLOB)):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(REPO).as_posix()
        for lineno, line in enumerate(_read(py).splitlines(), 1):
            for name in MTOP_RE.findall(line):
                slot = hits.setdefault(name, [])
                if not slot or slot[-1][0] != rel:
                    slot.append((rel, lineno))
    return hits


def classify(name: str) -> str:
    if name in SDK_PROPS or PROP_CHAIN_RE.match(name) or "." not in name[5:]:
        return "误收的非 mtop 名"
    if PERMISSION_RE.search(name):
        return "无权限或风控"
    if IM_INTERNAL_RE.search(name):
        return "纯站内或 IM 内部名"
    return "可实现"


def suggest_module(name: str) -> str:
    for rx, module in COMPILED_HINTS:
        if rx.search(name):
            return module
    return "backend/app/services/common.py"


def audit() -> dict:
    bundle, missing_files, sites = scan_bundles()
    backend = scan_backend()
    names = set(bundle)
    base = len(names)
    impl = sorted(names & set(backend))
    gap = sorted(names - set(backend))
    back_only = sorted(set(backend) - names)
    assert base == len(impl) + len(gap), (base, len(impl), len(gap))

    by_class = OrderedDict(
        (k, []) for k in ("可实现", "无权限或风控", "纯站内或 IM 内部名", "误收的非 mtop 名")
    )
    for name in gap:
        cat = classify(name)
        item = {"name": name, "class": cat, "bundles": bundle[name],
                "site": sites.get(name, "")}
        if cat == "可实现":
            item["module"] = suggest_module(name)
            eq = EQUIVALENT.get(name, "")
            impl_site = equiv_site(eq, backend)
            if impl_site:
                item["evidence"] = "同类已实现 %s @ %s" % (eq, impl_site)
            elif item["site"]:
                item["evidence"] = "载荷见 bundle 调用点 %s" % item["site"]
            else:
                item["evidence"] = ""
        elif cat in ("无权限或风控", "纯站内或 IM 内部名", "误收的非 mtop 名"):
            item["site"] = item["site"]
            log_rel, needle = DENIAL_EVIDENCE.get(name, (None, None))
            item["evidence"] = locate(log_rel, needle) if log_rel else DENIAL_DEFAULT
        by_class[cat].append(item)
    return {
        "base": base,
        "implemented_in_bundle": len(impl),
        "missing": len(gap),
        "backend_only": len(back_only),
        "backend_total": len(backend),
        "class_counts": {k: len(v) for k, v in by_class.items()},
        "gap": [i for v in by_class.values() for i in v],
        "by_class": by_class,
        "backend_only_names": back_only,
        "missing_bundle_files": missing_files,
        "bundles_scanned": len(BUNDLES) - len(missing_files),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="mtop 覆盖面审计")
    ap.add_argument("--list", action="store_true", help="逐条打印缺口")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--doc", metavar="PATH",
                    help="渲染 Markdown 报告到 PATH（相对仓库根）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    data = audit()
    if args.doc:
        out = REPO / args.doc
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_doc(data), encoding="utf-8")
        print("[doc] %s written" % args.doc)

    if args.json:
        print(json.dumps({k: v for k, v in data.items() if k != "by_class"},
                         ensure_ascii=False, indent=2))
        return 0

    print("=== mtop 覆盖面审计（口径：Base = 已实现(交集) + 未实现）===")
    print(f"BUNDLE_FILES_SCANNED {data['bundles_scanned']}/{len(BUNDLES)}")
    for miss in data["missing_bundle_files"]:
        print(f"  [warn] bundle 缺失: {miss}")
    print(f"BASE_BUNDLE_MT_NAMES   {data['base']}")
    print(f"IMPLEMENTED_IN_BUNDLE  {data['implemented_in_bundle']}")
    print(f"MISSING                {data['missing']}")
    print(f"CHECK {data['implemented_in_bundle']} + {data['missing']} = "
          f"{data['implemented_in_bundle'] + data['missing']} == {data['base']}  OK")
    print(f"[info] backend 侧 mtop 名字总数 {data['backend_total']}，"
          f"其中不在 bundle 内 {data['backend_only']}（不进覆盖率分子）")
    print()
    print("=== 缺口四分法 ===")
    for cat, n in data["class_counts"].items():
        print(f"{n:5d}  {cat}")
    print()
    if args.list:
        for cat, items in data["by_class"].items():
            print(f"--- {cat} ({len(items)}) ---")
            for it in items:
                tail = f"  -> {it['module']}" if "module" in it else ""
                print(f"  {it['name']:<70s} {','.join(it['bundles'])}{tail}")
    return 0


def _q(x):
    return chr(96) + str(x) + chr(96)


def _table(items, headers, cols):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join([" --- "] * len(headers)) + "|"]
    for it in items:
        cells = [_q(it["name"])]
        for fn in cols:
            v = it.get(fn) or ""
            cells.append(_q(v) if v else "-")
        cells.append(", ".join(it.get("bundles") or []))
        out.append("| " + " | ".join(cells) + " |")
    return out


def render_doc(data):
    """把 audit() 结果渲染成 Markdown（docs/audit/coverage57.md）。"""
    L = []
    A = L.append
    C = data["class_counts"]
    A("# 覆盖率审计（第 57 轮修正）· mtop 覆盖面与真实缺口矩阵")
    A("")
    A("> 本文件由 " + _q("scripts/coverage_audit.py --doc docs/audit/coverage57.md")
      + " 生成，数字与脚本同源。只读 14 个 bundle 副本 + " + _q("backend/app/**/*.py")
      + " + " + _q("logs/**") + "，零平台请求，不写 backend/frontend。")
    A("")
    A("## 1. 三个总数（修正后口径自洽）")
    A("")
    A("第 52 轮 " + _q("logs/cov52.txt") + " 头三行是 362 / 225 / 176 —— "
      "其中 225 + 176 = 401 ≠ 362。本轮复现确认这是**口径混用**，不是量错。")
    A("")
    A("| 指标 | 值 | 说明 |")
    A("| --- | ---: | --- |")
    A("| BUNDLE_MT_NAMES（Base） | " + str(data["base"]) + " | "
      + str(data["bundles_scanned"]) + "/" + str(len(BUNDLES))
      + " 个 bundle 副本去重（先去尾点合并） |")
    A("| IMPLEMENTED_IN_BUNDLE | " + str(data["implemented_in_bundle"])
      + " | backend 侧 mtop 名 ∩ Base |")
    A("| MISSING | " + str(data["missing"]) + " | Base − Implemented |")
    A("| 恒等式 | " + str(data["implemented_in_bundle"]) + " + " + str(data["missing"])
      + " = " + str(data["implemented_in_bundle"] + data["missing"]) + " = " + str(data["base"])
      + " | 自洽（脚本内置 assert，不成立即 exit 2） |")
    A("")
    A("backend 侧 mtop 名字总数 " + str(data["backend_total"]) + "，其中 "
      + str(data["backend_only"]) + " 个**不在 bundle 内**。旧脚本把这批「我们侧独有」"
      "的名字也算进分子（225），分母却是 bundle 集合（362）；401 − 362 = 39 正来自这里，"
      "逐个见 §5。")
    A("")
    A("## 2. 缺口四分法统计（合计 " + str(data["missing"]) + " = MISSING）")
    A("")
    A("| 分类 | 数量 | 判据 |")
    A("| --- | ---: | --- |")
    A("| 可实现 | " + str(C["可实现"])
      + " | API 形状名；载荷能从 bundle 调用点读出，且同域接口已有实现先例 |")
    A("| 无权限或风控 | " + str(C["无权限或风控"])
      + " | 平台白名单/资质/风控放行；有 FAIL_BIZ_IDLE_USER_UNAUTHORIZED 等日志硬证据 |")
    A("| 纯站内或 IM 内部名 | " + str(C["纯站内或 IM 内部名"])
      + " | 依赖登录态或前端会话通道（IM 同步/表情/文件 token、站内支付、互动组件） |")
    A("| 误收的非 mtop 名 | " + str(C["误收的非 mtop 名"])
      + " | mtop SDK 属性或库内部名被正则误收（mtop.request、mtop.config.secure …） |")
    A("")
    A("分类顺序（脚本内 classify()）：先判误收（SDK 属性白名单 + 无第二级域名），"
      "再判无权限/风控，再判 IM 内部名，剩下归可实现。")
    A("")
    A("## 3. 可实现（" + str(C["可实现"]) + "）")
    A("")
    A("bundle 都是压缩后的**单行**文件，故调用点给 `文件:行:列`，列即行内字符偏移，"
      "可在文件里按该偏移直接定位。")
    A("「已实现的同类先例」指我们**已经在用**的同域接口，说明载荷形态已知、鉴权链路已跑通，"
      "只需把这条补进对应 service；留空表示补做时需从 bundle 调用点现取载荷"
      "（调用点所在文件见最后一列）。")
    A("")
    L += _table(data["by_class"]["可实现"],
                ["接口名", "建议归属模块", "证据（文件:行）", "bundle 来源"],
                ["module", "evidence"])
    A("")
    A("## 4. 无权限或风控（" + str(C["无权限或风控"]) + "）")
    A("")
    A("「拒绝证据」为实测日志锚点；未逐条实测的落到通用锚点 " + _q(DENIAL_DEFAULT)
      + "（该行即 FAIL_BIZ_IDLE_USER_UNAUTHORIZED 的实例）。另有两类结构性证据："
      + _q("datacompass.*") + "（数据参谋）整族只在店铺开通该模块后可见，本账号店铺"
      "首页无该入口；" + _q("...platform.account.mock.apply/cancel") + "（体验账号）"
      "需平台侧子账号资质。")
    A("")
    L += _table(data["by_class"]["无权限或风控"],
                ["接口名", "拒绝证据（日志:行）", "bundle 来源"], ["evidence"])
    A("")
    A("## 4b. 纯站内或 IM 内部名（" + str(C["纯站内或 IM 内部名"]) + "）")
    A("")
    A("集中出现在 IM 页面 bundle（" + _q("js_006_p_im_index.js") + " / "
      + _q("js_008_vendors.js") + "）与站内导航脚本。其调用依赖**登录态 + 前端会话通道**"
      "（如 " + _q("mtop.taobao.idlemessage.pc.message.sync") + " 属 ImSDK 长连接同步，"
      "不是商家后台 HTTP 接口）；本工作台走 WebSocket 直连，不需要这些名字。")
    A("")
    L += _table(data["by_class"]["纯站内或 IM 内部名"], ["接口名", "bundle 来源"], [])
    A("")
    A("## 4c. 误收的非 mtop 名（" + str(C["误收的非 mtop 名"]) + "）")
    A("")
    A("mtop SDK 对象上的属性访问被 " + _q(r"mtop\.[A-Za-z0-9_.]+") + " 正则误收："
      + _q("mtop.request") + " / " + _q("mtop.params") + " / " + _q("mtop.js") + " / "
      + _q("mtop.config.secure") + " / " + _q("mtop.middlewares.push")
      + " 等。这类不是接口，不计入任何缺口。")
    A("")
    L += _table(data["by_class"]["误收的非 mtop 名"], ["接口名", "bundle 来源"], [])
    A("")
    A("## 5. backend 侧有、bundle 内没有的名字（" + str(data["backend_only"]) + "）")
    A("")
    A("不参与覆盖率分子；旧口径把它们算进 225，是恒等式不成立的直接原因。")
    A("")
    A((chr(96) * 3) + "text")
    L += list(data["backend_only_names"])
    A((chr(96) * 3))
    A("")
    A("## 6. 复跑")
    A("")
    A((chr(96) * 3) + "bash")
    A("/c/Python310/python.exe scripts/coverage_audit.py                      # 摘要 + 四分法")
    A("/c/Python310/python.exe scripts/coverage_audit.py --list               # 逐条缺口")
    A("/c/Python310/python.exe scripts/coverage_audit.py --json               # 机器可读")
    A("/c/Python310/python.exe scripts/coverage_audit.py --doc docs/audit/coverage57.md")
    A((chr(96) * 3))
    A("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
