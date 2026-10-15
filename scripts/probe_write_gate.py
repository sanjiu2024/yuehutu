# -*- coding: utf-8 -*-
"""在**打包版**上验证写接口的 confirm 闸门（红线：未确认 → 400 且不碰平台）。

用法：python scripts/probe_write_gate.py [--port 18400]

判别逻辑（很关键）：
    账号当前是 need_relogin，所以任何**穿过闸门**的请求都会停在凭据检查上 → 409。
    于是：
        未确认 → 400 confirmation_required   = 闸门生效（拦在凭据检查之前，零平台请求）
        未确认 → 409 登录态过期              = 闸门失效（被放过去了）
    再补一条 `confirm: true` → 409，证明 400 确实是闸门给的、而不是别的什么原因。

**绝不会真发写请求**：账号已过期，带 confirm 的请求也停在凭据检查。
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_ap = argparse.ArgumentParser(description='打包版写接口 confirm 闸门探针（只读，绝不真写）')
_ap.add_argument('--port', type=int, default=18400, help='打包版端口（默认 18400）')
BASE = 'http://127.0.0.1:%d' % _ap.parse_args().port
DATA = pathlib.Path(os.environ.get('XYWB_PROBE_DATA')
                    or (pathlib.Path(os.environ['APPDATA']) / 'xianyu-ai-workbench' / 'data'))
ACC = '2217970177348'
# 确认那一腿用这个**不存在的账号**：闸门放行后会在本地查库直接 404，零平台请求。
SAFE_ACC = '9999999999999'
OUT = pathlib.Path(__file__).resolve().parent.parent / 'logs' / 'packaged_write_gate'
OUT.mkdir(parents=True, exist_ok=True)


def call(method, path, token='', body=None, extra=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if extra:
        headers.update(extra)
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', 'replace')
    except Exception as exc:  # noqa: BLE001
        return 0, 'EXC %s: %s' % (type(exc).__name__, exc)


def code_of(text):
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        return '?'
    if isinstance(obj, dict):
        return str(obj.get('code') or obj.get('message') or '')[:70]
    return '?'


def login():
    secret = (DATA / 'local_secret.txt').read_text(encoding='utf-8').strip()
    status, text = call('POST', '/api/auth/desktop', body={}, extra={'X-Desktop-Secret': secret})
    if status != 200:
        return ''
    return ((json.loads(text) or {}).get('data') or {}).get('token') or ''


CASES = [
    ('trade.ship', 'POST', '/api/trade/%s/orders/ship' % ACC),
    ('trade.ship_dummy', 'POST', '/api/trade/%s/orders/1/ship-dummy' % ACC),
    ('trade.adjust_price', 'POST', '/api/trade/%s/orders/1/adjust-price' % ACC),
    ('trade.refund_agree', 'POST', '/api/trade/%s/refunds/1/agree' % ACC),
    ('trade.refund_refuse', 'POST', '/api/trade/%s/refunds/1/refuse' % ACC),
    ('trade.rate_create', 'POST', '/api/trade/%s/evaluations' % ACC),
    ('trade.complain_refuse', 'POST', '/api/trade/%s/complaints/1/refuse' % ACC),
    ('trade.complain_proof', 'POST', '/api/trade/%s/complaints/1/proof' % ACC),
    ('ad.campaign_pause', 'POST', '/api/promotion/write/%s/campaign_pause' % ACC),
    ('ad.campaign_delete', 'POST', '/api/promotion/write/%s/campaign_delete' % ACC),
    ('qual.submit_packages', 'POST', '/api/qualification/%s/packages/submit' % ACC),
    ('qual.oss_credential', 'POST', '/api/qualification/%s/oss-credential' % ACC),
    ('sec.appeal_submit', 'POST', '/api/security-center/%s/appeal/submit' % ACC),
    ('sec.appeal_cro_submit', 'POST', '/api/security-center/%s/appeal/cro/submit' % ACC),
    ('sec.appeal_urge', 'POST', '/api/security-center/%s/appeal/urge' % ACC),
    ('tpl.create', 'POST', '/api/item-templates/%s/create' % ACC),
    ('tpl.modify', 'POST', '/api/item-templates/%s/tpl-1/modify' % ACC),
    ('tpl.delete', 'POST', '/api/item-templates/%s/tpl-1/delete' % ACC),
    ('tpl.unbind', 'POST', '/api/item-templates/%s/unbind' % ACC),
    ('items.batch_offline', 'POST', '/api/items/%s/batch-offline' % ACC),
    ('items.batch_delete', 'POST', '/api/items/%s/batch-delete' % ACC),
    ('items.quick_edit', 'POST', '/api/items/%s/1/quick-edit' % ACC),
    ('addr.add', 'POST', '/api/trade/%s/addresses' % ACC),
    ('addr.update', 'POST', '/api/trade/%s/addresses/1/update' % ACC),
    ('addr.delete', 'POST', '/api/trade/%s/addresses/1/delete' % ACC),
    ('addr.set_default', 'POST', '/api/trade/%s/addresses/1/default' % ACC),
    ('order_address.agree', 'POST', '/api/trade/%s/orders/1/modify-address/agree' % ACC),
    ('order_address.refuse', 'POST', '/api/trade/%s/orders/1/modify-address/refuse' % ACC),
    ('drafts.delete', 'POST', '/api/drafts/%s/d1/delete' % ACC),
    ('drafts.batch_delete', 'POST', '/api/drafts/%s/batch-delete' % ACC),
    ('shop.cs_routing_rule', 'POST', '/api/shop/%s/cs-routing/rule' % ACC),
    ('shop.cs_group_save', 'POST', '/api/shop/%s/cs-groups' % ACC),
    ('shop.cs_group_delete', 'POST', '/api/shop/%s/cs-groups/delete' % ACC),
    # ---- 第三十二批：店铺资料 3 条 + 子账号 5 条 ----
    ('shop.profile_nick', 'POST', '/api/shop/%s/profile/nick' % ACC),
    ('shop.profile_desc', 'POST', '/api/shop/%s/profile/desc' % ACC),
    ('shop.profile_avatar', 'POST', '/api/shop/%s/profile/avatar' % ACC),
    ('sub.create', 'POST', '/api/sub-accounts/%s' % ACC),
    ('sub.delete', 'POST', '/api/sub-accounts/%s/9001/delete' % ACC),
    ('sub.mobile', 'POST', '/api/sub-accounts/%s/9001/mobile' % ACC),
    ('sub.roles', 'POST', '/api/sub-accounts/%s/9001/roles' % ACC),
    ('sub.password', 'POST', '/api/sub-accounts/%s/9001/password' % ACC),
    # ---- 第三十七批：话术 / 快捷回复 6 条写 ----
    ('qr.group_create', 'POST', '/api/quick-reply/%s/groups' % ACC),
    ('qr.group_update', 'POST', '/api/quick-reply/%s/groups/g1' % ACC),
    ('qr.group_delete', 'POST', '/api/quick-reply/%s/groups/g1/delete' % ACC),
    ('qr.phrase_create', 'POST', '/api/quick-reply/%s/phrases' % ACC),
    ('qr.phrase_update', 'POST', '/api/quick-reply/%s/phrases/c1' % ACC),
    ('qr.phrase_delete', 'POST', '/api/quick-reply/%s/phrases/c1/delete' % ACC),
    # ---- 第三十八批：订单页的关单 / 延时 / 提醒 / 运费退款 / 撤销投诉 5 条写 ----
    ('trade.close', 'POST', '/api/trade/%s/orders/1/close' % ACC),
    ('trade.delay', 'POST', '/api/trade/%s/orders/1/delay' % ACC),
    ('trade.remind', 'POST', '/api/trade/%s/orders/1/remind' % ACC),
    ('trade.postage_pay', 'POST', '/api/trade/%s/refunds/1/postage-pay' % ACC),
    ('trade.complain_revoke', 'POST', '/api/trade/%s/complaints/1/revoke' % ACC),
    # ---- 第三十九批：物流改运单号 1 条写 ----
    ('logistics.resend', 'POST', '/api/logistics/%s/orders/1/resend' % ACC),
    # ---- 第四十批：IM 会话操作 3 条写 ----
    ('im.blacklist_add', 'POST', '/api/im/%s/blacklist' % ACC),
    ('im.blacklist_remove', 'POST', '/api/im/%s/blacklist/remove' % ACC),
    ('im.card_send', 'POST', '/api/im/%s/cards' % ACC),
    # ---- 第四十一批：纠纷协商 4 条写 ----
    ('disputes.send_text', 'POST', '/api/disputes/%s/cid-1/messages' % ACC),
    ('disputes.send_image', 'POST', '/api/disputes/%s/cid-1/messages/image' % ACC),
    ('disputes.urge', 'POST', '/api/disputes/%s/cid-1/urge' % ACC),
    ('disputes.mark_read', 'POST', '/api/disputes/%s/cid-1/mark-read' % ACC),
    # ---- 第四十二批：粉丝价 1 条写（整份替换）----
    ('items.set_fans_price', 'POST', '/api/items/%s/it-1/fans-price' % ACC),
    ('trade.compensate_refuse', 'POST',
     '/api/trade/%s/orders/1/compensations/S1/refuse' % ACC),
    # ---- 第四十三批：客服会话接管 3 条写 ----
    ('im.session_transfer', 'POST', '/api/im/%s/sessions/transfer' % ACC),
    ('im.session_leave', 'POST', '/api/im/%s/sessions/leave' % ACC),
    ('im.session_rejoin', 'POST', '/api/im/%s/sessions/rejoin' % ACC),
    # ---- 第四十六批：接待状态 1 条写（账号级，不跟会话走）----
    ('im.set_reception', 'POST', '/api/im/%s/reception' % ACC),
    ('im.clear_unread', 'POST', '/api/im/%s/unread/clean' % ACC),
    # ---- 第四十九批：订单备注 1 条写 ----
    ('trade.add_memo', 'POST', '/api/trade/%s/orders/1/memo' % ACC),
    # ---- 第五十二批：商品老一代 5 条写（这批之前**完全没有闸门**，探针也没覆盖，
    #      所以从第 19 批一路绿到现在 —— 补上覆盖）----
    ('items.edit', 'POST', '/api/items/%s/1/edit' % ACC),
    ('items.publish', 'POST', '/api/items/%s/publish' % ACC),
    ('items.batch_edit', 'POST', '/api/items/%s/batch-edit' % ACC),
    ('items.delist', 'POST', '/api/items/%s/1/delist' % ACC),
    ('items.relist', 'POST', '/api/items/%s/1/relist' % ACC),
    # 四条老的无 account 路径：路径里没有 ACC 可替换，靠「空 body + 空 account_id」
    # 在本地就失败（不会发出平台请求）。
    ('items.batch_legacy', 'POST', '/api/items/batch'),
    ('items.publish_legacy', 'POST', '/api/items/publish'),
    ('items.downshelf_legacy', 'POST', '/api/items/downshelf'),
    ('items.reshelf_legacy', 'POST', '/api/items/reshelf'),
    # ---- 第五十三批：真发货 1 条 + 人工回消息 1 条（这批之前**都没有闸门**）----
    # 路径故意用**不存在的 id**：确认腿（confirm=true）会在本地查库失败（404/400），
    # 一个平台请求都不会发出去 —— 探针自己绝不能变成写请求的来源。
    ('orders.ship', 'PUT', '/api/orders/999999999/shipment'),
    ('chats.send', 'POST', '/api/chats/999999999/send'),
]

# 语义只读的那条**故意没有闸门**，用 confirm=true 也应当走到凭据检查（409）
READONLY_SEMANTIC = [('sec.appeal_form', 'POST', '/api/security-center/%s/appeal/form' % ACC)]


def main():
    token = login()
    print('token_len=%d' % len(token))
    if not token:
        print('LOGIN FAILED')
        return 1

    rows = []
    bad = 0
    print('%-24s %-6s %-24s | %-6s | %-6s %s' % (
        'case', 'noconf', 'code(noconf)', 'false', 'true', 'code(true)'))
    for name, method, path in CASES:
        s1, t1 = call(method, path, token=token, body={})
        s2, t2 = call(method, path, token=token, body={'confirm': False})
        # ★ 确认那一腿**故意换成一个不存在的 account_id**：闸门放行后会在本地查库直接 404
        #   `not_found`，**一个平台请求都不会发**。用真账号 + confirm=true 会在账号登录态
        #   正常时真的打到闲鱼（2026-10-12 00:35 就这么发生过一次：假 ID 被平台拒绝、
        #   没有造成改动，但探针本身不该是写请求的来源）。
        safe_path = path.replace(ACC, SAFE_ACC)
        s3, t3 = call(method, safe_path, token=token, body={'confirm': True})
        c1, c3 = code_of(t1), code_of(t3)
        # 红线：未确认必须是 400 且码里含 confirm；确认后必须**不再**是 confirm 类拒绝
        # （换假账号后应当是 404 not_found —— 这正好证明「闸门已经放行、卡在了账号查找」）
        gate_ok = (s1 == 400 and 'confirm' in c1 and s2 == 400
                   and not (s3 == 400 and 'confirm' in c3))
        if not gate_ok:
            bad += 1
        print('%-24s %-6s %-24s | %-6s | %-6s %s%s' % (
            name, s1, c1[:24], s2, s3, c3[:40], '' if gate_ok else '   <== 闸门有问题'))
        rows.append({'case': name, 'no_confirm': s1, 'code_no_confirm': c1,
                     'confirm_false': s2, 'confirm_true_safe_account': s3,
                     'code_confirm_true': c3, 'gate_ok': gate_ok})

    print()
    print('--- 语义只读、故意无闸门 ---')
    for name, method, path in READONLY_SEMANTIC:
        s, t = call(method, path, token=token, body={})
        print('%-24s %-6s %s' % (name, s, code_of(t)))
        rows.append({'case': name, 'no_confirm': s, 'code': code_of(t), 'gate_ok': s == 409})

    print()
    print('GATE FAILURES: %d / %d' % (bad, len(CASES)))
    (OUT / 'report.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT: %s' % (OUT / 'report.json'))
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
