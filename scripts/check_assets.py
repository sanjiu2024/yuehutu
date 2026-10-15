"""核对打包版发出来的前端资源与 frontend/dist/assets 逐字节一致。

用法：python scripts/check_assets.py [--port 18400]
"""
import argparse
import hashlib
import pathlib
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
REPO = pathlib.Path(__file__).resolve().parent.parent
_ap = argparse.ArgumentParser(description='核对打包版前端资源与 frontend/dist 逐字节一致')
_ap.add_argument('--port', type=int, default=18400, help='打包版端口（默认 18400）')
BASE = 'http://127.0.0.1:%d' % _ap.parse_args().port


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.read()


html = get('/').decode('utf-8', 'replace')
names = sorted(set(re.findall(r'assets/(index-[A-Za-z0-9_\-]+\.(?:js|css))', html)))
print('GET / 引用:', names)
local = sorted(p.name for p in (REPO / 'frontend' / 'dist' / 'assets').iterdir())
print('frontend/dist/assets:', local)
ok = names == local
for n in names:
    remote = hashlib.sha256(get('/assets/' + n)).hexdigest()
    disk = hashlib.sha256((REPO / 'frontend' / 'dist' / 'assets' / n).read_bytes()).hexdigest()
    same = remote == disk
    ok = ok and same
    print('%-26s remote=%s local=%s same=%s' % (n, remote[:12], disk[:12], same))
print('ASSETS_MATCH=%s' % ok)
raise SystemExit(0 if ok else 1)
