# -*- coding: utf-8 -*-
"""按 docs/audit/coverage63.md 二 的口径重算 coverage57 3 的 77 条缺口状态。

用法（仓库根目录）：
    python scripts/coverage63_recompute.py

口径（与 coverage63 二 一致）：
  已实现   = 协议层有 `大写常量 = <mtop 名>` 的定义，且该常量在 backend/app 里有定义行以外的引用，
             且该引用不在只读名单 / 不支持表里（WRITE_APIS / READ_APIS / UNSUPPORTED / READ_KINDS / WRITE_ACTIONS）
  仅登记   = 有常量定义，但引用只出现在上述名单里（没有调用路径）
  未实现   = 连字面量都没有
  不可实现 = 未实现且命中 scripts/coverage_audit.py 的 DENIAL_EVIDENCE（真机拒绝硬证据）
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COVERAGE57 = REPO / 'docs/audit/coverage57.md'
GAP_FIRST_LINE, GAP_LAST_LINE = 36, 112  # coverage57 3 的 77 行（1-based，含端点）
SRC_DIRS = ('backend/app/xianyu', 'backend/app/services', 'backend/app/api', 'backend/app/agents')
NAME_RE = re.compile(r'`(mtop\.[A-Za-z0-9_.]+)`')
REGISTRY_LINE = re.compile(r'^(WRITE_APIS|READ_APIS|UNSUPPORTED|READ_KINDS|WRITE_ACTIONS|DENIAL_EVIDENCE|ALIASES)\b')
EXPECTED = {'impl': 39, 'reg': 2, 'unimpl': 34, 'denial': 2}


def gap_names() -> list[str]:
    lines = io.open(COVERAGE57, encoding='utf-8').read().split('\n')
    names = []
    for ln in lines[GAP_FIRST_LINE - 1:GAP_LAST_LINE]:
        m = NAME_RE.search(ln)
        if m:
            names.append(m.group(1))
    return names


def denial_names() -> set[str]:
    """不可实现 = 证据日志里该 api **整名**出现、且同一段里有 FAIL_BIZ_IDLE_USER_UNAUTHORIZED。

    只看 DENIAL_EVIDENCE 的键太宽：`mtop.idle.pc.backend.idleitem.edit` 在探针日志里其实是
    `FAIL_BIZ_JSON_ERROR`（发布失败），而短名还会误命中兄弟行 `...idleitem.editDetail`。
    """
    txt = io.open(REPO / 'scripts/coverage_audit.py', encoding='utf-8').read()
    block = txt.split('DENIAL_EVIDENCE = {', 1)[1].split('}', 1)[0]
    pairs = re.findall(r'"(mtop\.[A-Za-z0-9_.]+)":\s*\(\s*"([^"]+)",\s*"([^"]+)"\s*\)', block)
    out = set()
    for name, log, needle in pairs:
        p = REPO / log
        if not p.is_file():
            continue
        body = io.open(p, encoding='utf-8').read()
        for m in re.finditer(re.escape(needle) + r'(?![\w.])', body):
            if 'FAIL_BIZ_IDLE_USER_UNAUTHORIZED' in body[m.start():m.start() + 400]:
                out.add(name)
                break
    return out


def load_sources() -> dict[str, str]:
    src = {}
    for d in SRC_DIRS:
        p = REPO / d
        if not p.is_dir():
            continue
        for f in sorted(p.glob('*.py')):
            src[f.relative_to(REPO).as_posix()] = io.open(f, encoding='utf-8').read()
    return src


def classify(name: str, src: dict[str, str], denials: set[str]) -> tuple[str, list[str]]:
    pat = re.compile(r'^([A-Z][A-Z0-9_]*)\s*=\s*[\'\"]' + re.escape(name) + r'[\'\"]', re.M)
    consts, impl_ref = [], []
    for path, text in src.items():
        for m in pat.finditer(text):
            consts.append((m.group(1), path))
    if not consts:
        return ('denial' if name in denials else 'unimpl'), []
    for const, path in consts:
        text = src[path]
        for m in re.finditer(r'\b' + re.escape(const) + r'\b', text):
            line_no = text[:m.start()].count('\n') + 1
            line = text.split('\n')[line_no - 1].strip()
            if re.match(r'^' + re.escape(const) + r'\s*=', line):
                continue
            impl_ref.append('%s:%d' % (path, line_no))
            if not REGISTRY_LINE.match(line):
                return 'impl', impl_ref
    return 'reg', impl_ref


def main() -> int:
    names = gap_names()
    denials = denial_names()
    src = load_sources()
    buckets = {'impl': [], 'reg': [], 'unimpl': [], 'denial': []}
    for name in names:
        kind, refs = classify(name, src, denials)
        buckets[kind].append((name, refs))
    print('GAPS %d  (coverage57 3: 第 %d-%d 行)' % (len(names), GAP_FIRST_LINE, GAP_LAST_LINE))
    print('IMPL %d / REG %d / UNIMPL %d / DENIAL %d = %d'
          % (len(buckets['impl']), len(buckets['reg']), len(buckets['unimpl']),
             len(buckets['denial']), len(names)))
    for kind in ('reg', 'denial'):
        for name, refs in buckets[kind]:
            print('  %-8s %s  %s' % (kind.upper(), name, refs[:2]))
    bad = []
    if len(names) != 77:
        bad.append('缺口行数 %d != 77' % len(names))
    for kind, want in EXPECTED.items():
        got = len(buckets[kind])
        if got != want:
            bad.append('%s %d != %d' % (kind, got, want))
    if bad:
        print('MISMATCH: ' + '; '.join(bad))
        return 1
    print('OK：与 docs/audit/coverage63.md 一 的 39 / 2 / 34 / 2 一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())