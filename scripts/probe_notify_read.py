# -*- coding: utf-8 -*-
"""真机写探针：验证 `mtop.taobao.idlemessage.pc.notifys.read` 的真实回执形状。

用法：
    python scripts/probe_notify_read.py --account 2217970177348 --message-id 4338118620042.PNM          # 只跑闸门，不写
    python scripts/probe_notify_read.py --account ... --message-id ... --yes                            # **真的**标记已读

为什么单独一个探针：通知已读是「零副作用写阶梯」里第一条拿到平台真回执的写接口
（证据 `logs/notify_read58/07b_raw_mtop_response.json`）。平台对这条 API 会回
`{"success": true, "data": {"success": true}}`；服务层只有在业务体明确 success 时才 saved=true 并落审计，
回执里没有 success 就 saved=false + warning，绝不谎报成功。

**带 --yes 会真的改平台状态**（那条通知变成已读，且不可逆）——不加 --yes 只跑 confirm 闸门。
"""
import argparse
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ap = argparse.ArgumentParser(description='通知标记已读真机探针（不带 --yes 绝不写）')
_ap.add_argument('--account', required=True, help='店铺账号 ID（要在线、有 Cookie）')
_ap.add_argument('--message-id', required=True, help='要标记已读的通知 ID，形如 4338118620042.PNM')
_ap.add_argument('--yes', action='store_true', help='真的写（不加只跑闸门，一个平台请求都不发）')
_ap.add_argument('--out', default='', help='证据落盘目录（默认 logs/notify_read58）')
ARGS = _ap.parse_args()
OUT = pathlib.Path(ARGS.out) if ARGS.out else ROOT / 'logs' / 'notify_read58'


def dump(name, payload):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print('wrote', path)


def redact(value):
    if isinstance(value, dict):
        return {k: ('<redacted>' if k in ('cookies_str', 'cookie') else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def main():
    from backend.app.services import common, notify as svc
    from backend.app.xianyu import notify as proto

    # 0) 闸门：没 confirm 时一个平台请求都不发（不需要平台参与，也不改任何状态）
    gate = svc.mark_read(ARGS.account, ARGS.message_id)
    print('no-confirm ->', json.dumps(gate, ensure_ascii=False))
    dump('07a_gate_no_confirm.json', {'result': gate})
    if not ARGS.yes:
        print('（未带 --yes：只跑了闸门，没有写任何东西）')
        return 0

    client, problem = svc._client(ARGS.account)
    if client is None:
        dump('07b_client_unavailable.json', {'problem': problem})
        print('!! 拿不到客户端:', problem)
        return 1

    # 1) 原始 mtop 回执（逐字，用于确认「平台到底回什么」）
    api, data, version = proto.mark_read_request(ARGS.message_id, confirm=True)
    raw = common.run_awaitable(client.mtop(api, data, v=version))
    dump('07b_raw_mtop_response.json', {
        'request': {'api': api, 'version': version, 'data': data},
        'raw_response': redact(raw),
        'parsed': proto.parse_mark_read(raw),
    })
    print('raw ->', json.dumps(redact(raw), ensure_ascii=False)[:600])

    # 2) 走服务层（真实返回给前端的那份）
    result = svc.mark_read(ARGS.account, ARGS.message_id, confirm=True)
    dump('07c_service_mark_read.json', {'result': redact(result)})
    print('service ->', json.dumps(redact(result), ensure_ascii=False)[:900])
    return 0


if __name__ == '__main__':
    sys.exit(main())
