# -*- coding: utf-8 -*-
"""对**打包版 exe**（已在 --port 运行，默认 18400）跑逐页渲染冒烟。

用法：python scripts/packaged_ui_smoke.py [--port 18400]

复用 scripts/ui_smoke.py 的 run_browser，但不启动后端、不播种演示数据 ——
直接用真实数据目录（账号当前是 need_relogin，正好顺带验证「登录态过期」时页面
是不是给出人话提示而不是崩）。
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'scripts'))

import ui_smoke  # noqa: E402

_ap = argparse.ArgumentParser(description='对**打包版 exe** 逐页渲染冒烟')
_ap.add_argument('--port', type=int, default=18400, help='打包版端口（默认 18400）')
BASE = 'http://127.0.0.1:%d' % _ap.parse_args().port
DATA = pathlib.Path(os.environ['APPDATA']) / 'xianyu-ai-workbench' / 'data'
OUT = REPO / 'logs' / 'packaged_ui_smoke'
OUT.mkdir(parents=True, exist_ok=True)


def login():
    secret = (DATA / 'local_secret.txt').read_text(encoding='utf-8').strip()
    req = urllib.request.Request(
        BASE + '/api/auth/desktop', data=b'{}', method='POST',
        headers={'Content-Type': 'application/json', 'X-Desktop-Secret': secret})
    with urllib.request.urlopen(req, timeout=30) as resp:
        obj = json.loads(resp.read().decode('utf-8'))
    return ((obj or {}).get('data') or {}).get('token') or ''


def main():
    token = login()
    print('token_len=%d' % len(token))
    if not token:
        return 1
    results = ui_smoke.run_browser(BASE, token, OUT)
    bad = [r for r in results if not r['ok']]
    print('-' * 60)
    print('PACKAGED UI SMOKE  %d/%d 页通过' % (len(results) - len(bad), len(results)))
    for row in bad:
        print('BAD %s: %s' % (row['page'], '; '.join(row['problems'])))
    (OUT / 'report.json').write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
