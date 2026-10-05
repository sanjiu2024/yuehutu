"""跑全部后端测试。

每个 tests/test_*.py 是一个独立的 unittest 脚本（本项目不用 pytest、不用 venv），
所以这里用子进程逐个跑，任何一个失败都算整体失败。

用法（仓库根目录）：
    cmd /c "set PYTHONPATH=.&& C:\\Python310\\python.exe backend\\tests\\run_all.py"
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile

TESTS_DIR = pathlib.Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent

_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.M)
_SKIP_RE = re.compile(r"skipped=(\d+)")


def _parse_counts(output: str) -> tuple[int, int]:
    """从 unittest 输出里取（用例数, 跳过数）。取不到用例数就当 0。"""
    ran = _RAN_RE.search(output)
    cases = int(ran.group(1)) if ran else 0
    skipped = sum(int(n) for n in _SKIP_RE.findall(output))
    return cases, skipped


def main() -> int:
    # 控制台可能是 GBK：测试输出里的中文/替换字符会让 print 直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    files = sorted(p for p in TESTS_DIR.glob("test_*.py"))
    if not files:
        print("没有找到任何 test_*.py")
        return 1

    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    total_cases = 0
    total_skipped = 0
    # 每个测试给一个独立的数据目录：否则测试里的临时库会往真实 %APPDATA% 写
    # desktop_credentials.json，把生产管理员的密码覆盖掉（PACKAGING.md 6.5）。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as sandbox:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            env = dict(os.environ)
            env["XIANYU_APP_DIR"] = os.path.join(sandbox, path.stem)
            env["PYTHONPATH"] = "."
            proc = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
            # 只按 returncode 记账会漏掉两类"假绿"：文件里一个用例都没有
            # （unittest 在 3.10 下 0 个用例仍 exit 0），以及根本没调 unittest.main()
            # 的文件（什么都不跑也 exit 0）。所以必须解析出真实用例数。
            # 注意：unittest 把 "Ran N tests" 那行写到 **stderr**，不是 stdout。
            cases, skipped = _parse_counts((proc.stdout or "") + (proc.stderr or ""))
            if proc.returncode != 0:
                failed.append((rel, (proc.stdout or "") + (proc.stderr or "")))
                print(f"FAIL  {rel}  (exit {proc.returncode})")
            elif cases == 0:
                failed.append((rel, (proc.stdout or "") + (proc.stderr or "")
                               + "\n该文件没有跑任何用例（0 tests）—— unittest 在这种情况下依然 exit 0。"))
                print(f"FAIL  {rel}  (0 个用例)")
            else:
                passed.append(rel)
                extra = f", skipped {skipped}" if skipped else ""
                print(f"PASS  {rel}  ({cases} 用例{extra}, {tail[0]})")
                total_cases += cases
                total_skipped += skipped

    print("-" * 60)
    print(f"TOTAL {len(files)}  PASS {len(passed)}  FAIL {len(failed)}"
          f"  CASES {total_cases}  SKIPPED {total_skipped}")
    for rel, out in failed:
        print("=" * 60)
        print(f"----- {rel} -----")
        print(out.strip()[-4000:])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
