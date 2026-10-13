"""五方对账：`TOOL_SPECS` / `_SERVICE_TARGETS` / `_HANDLERS` / `TOOL_ROUTES` / 真实 FastAPI 路由。

比只比"工具名清单"更强的一点：把每条 MCP 路由声明的 **query / body 键**，跟**真实端点签名**
对账（从 `app.openapi()` 取 `parameters` 与 `requestBody` schema），所以能自动抓住
"路由参数漂移"这一类 bug —— 例如 `finance_income` 曾声明 `cycle_from`/`cycle_to`，而端点
只认 `from`/`to`，发过去会被 FastAPI 静默忽略（**结果错但不报错**）。

跑法（在仓库根）：

    cmd /c "set PYTHONPATH=.&& set PYTHONIOENCODING=utf-8&& set XIANYU_APP_DIR=%TEMP%\\xywb_recon&& C:\\Python310\\python.exe scripts\\reconcile_tools.py"

必须打印 `RECON OK`；退出码 1 表示有漂移（会逐条列出）。
新增写工具后**必须**同步改本文件第 4 步的 `WRITE_PREFIX` 元组，否则会出现假阳性
（"只读工具却带 confirm 参数"）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))

from app.agents import tools as T  # noqa: E402
from app.main import create_app  # noqa: E402
from mcp_server.client import ALIASES, TOOL_ROUTES  # noqa: E402

app = create_app()
spec = app.openapi()
paths = spec['paths']

specs = {s['function']['name'] for s in T.TOOL_SPECS}
master = {s['function']['name'] for s in T.MASTER_TOOLS}
routes = set(TOOL_ROUTES)
targets = set(T._SERVICE_TARGETS)
handlers = set(T._HANDLERS)

print(f'specs={len(specs)} master={len(master)} routes={len(routes)} '
      f'targets={len(targets)} handlers={len(handlers)}')

fail = []

# 1) specs(master) <-> routes：差集只允许既有的 agents_messages / master_chat
ALLOWED_EXTRA = {'agents_messages', 'master_chat'}
missing = sorted(master - routes)
extra = sorted(routes - master - ALLOWED_EXTRA)
print(f'specs-not-in-routes={missing}')
print(f'routes-not-in-specs(allow={sorted(ALLOWED_EXTRA)})={extra}')
if missing:
    fail.append(f'工具在 specs 里但 MCP 路由表没有：{missing}')
if extra:
    fail.append(f'MCP 路由表里有 specs 之外的工具：{extra}')

# 2) 每个 target / handler 都要有对应 spec
for label, s in (('_SERVICE_TARGETS', targets), ('_HANDLERS', handlers)):
    orphan = sorted(s - specs)
    if orphan:
        fail.append(f'{label} 里没有对应 TOOL_SPECS 的项：{orphan}')


# 3) 每条路由的 path 必须真实挂载；query / body 键必须被端点接受
def query_params(path: str, method: str) -> set:
    op = paths[path][method]
    return {p['name'] for p in op.get('parameters', []) if p.get('in') == 'query'}


def body_props(path: str, method: str):
    """返回端点接受的 body 键；**自由 dict 返回 None**（表示"什么都收，跳过检查"）。

    很多写端点的 body 是 `Dict[str, Any] = Body(default={})`，openapi 里就没有
    properties —— 那不是漂移，所以必须跳过，否则全是假阳性。
    """
    op = paths[path][method]
    rb = op.get('requestBody') or {}
    schema = (rb.get('content', {}).get('application/json', {}) or {}).get('schema') or {}
    ref = schema.get('$ref')
    if ref:
        schema = spec['components']['schemas'][ref.split('/')[-1]]
    props = schema.get('properties')
    if not props:
        return None  # 自由 dict，跳过
    return set(props)


checked = 0
for name in sorted(routes):
    route = TOOL_ROUTES[name]
    path, method = route['path'], route['method'].lower()
    if path not in paths:
        fail.append(f'{name}: 路径没挂载 {path}')
        continue
    if method not in paths[path]:
        fail.append(f'{name}: {path} 不支持 {route["method"]}')
        continue
    checked += 1

    # 路径模板里的占位符必须与 path_params 完全一致
    placeholders = {seg[1:-1] for seg in path.split('/') if seg.startswith('{')}
    declared = set(route.get('path_params', []))
    if placeholders != declared:
        fail.append(f'{name}: path 占位符 {sorted(placeholders)} != path_params {sorted(declared)}')

    # query 键必须被端点接受（query_rename 是工具参数名 → HTTP 线上名的翻译表）
    if route.get('query'):
        rename = route.get('query_rename') or {}
        wire = {rename.get(k, k) for k in route['query']}
        accepted = query_params(path, method)
        bad = sorted(wire - accepted)
        if bad:
            fail.append(f'{name}: query {bad} 端点不接受（它接受 {sorted(accepted)}）')

    # body 键必须被端点接受（端点若是自由 dict 则跳过）
    if route.get('body'):
        accepted = body_props(path, method)
        if accepted is not None:
            bad = sorted(set(route['body']) - accepted)
            if bad:
                fail.append(f'{name}: body {bad} 端点不接受（它接受 {sorted(accepted)}）')

# 4) 写工具的硬要求：method=POST、required 含 confirm、description 有中文警示
#    ⚠️ 新增写工具必须把名字前缀加进来，否则第 5 步会把它当只读工具误报。
WRITE_PREFIX = ('trade_write_', 'trade_address_', 'promotion_write_', 'qualification_submit',
                'qualification_oss', 'item_templates_create', 'item_templates_modify',
                'item_templates_delete', 'item_templates_unbind',
                'items_batch_offline', 'items_batch_delete', 'items_quick_edit',
                # 改地址审批：只加这两条**完整写工具名**。故意不加 'trade_order_address_'，
                # 那会把只读的 trade_order_address_request 一起误判成写工具。
                'trade_order_address_agree', 'trade_order_address_refuse',
                # 客服分流写：同样只加完整工具名。故意不加 'shop_cs' 宽前缀，
                # 那会把只读的 shop_cs_routing 一起误判成写工具。
                'shop_cs_routing_rule_save', 'shop_cs_group_save', 'shop_cs_group_delete',
    'shop_profile_update_nick',
    'shop_profile_update_desc',
    'shop_profile_update_avatar',
    'sub_account_create',
    'sub_account_delete',
    'sub_account_update_mobile',
    'sub_account_update_roles',
    'sub_account_update_password',
                'security_appeal_submit', 'security_appeal_urge',
                # 草稿箱：只加两条**完整写工具名**。故意不加 'drafts_'，
                # 那会把只读的 drafts_list / drafts_get 一起误判成写工具。
                'drafts_delete', 'drafts_batch_delete',
                # 话术 / 快捷回复：同样只加六条**完整写工具名**。故意不加 'quick_reply_'，
                # 那会把只读的 quick_reply_list 一起误判成写工具。
                'quick_reply_group_create', 'quick_reply_group_update',
                'quick_reply_group_delete', 'quick_reply_phrase_create',
                'quick_reply_phrase_update', 'quick_reply_phrase_delete',
                # 第三十八批：订单页 5 条写（只读的 trade_close_reasons 故意不在，
                # 它名字里没有写动作，靠**完整写工具名**而不是 'trade_' 宽前缀。）
                'trade_close_order', 'trade_delay_confirm', 'trade_remind_confirm',
                'trade_postage_refund_pay', 'trade_complain_revoke',
                # 第三十九批：物流写只有这一条，同样用完整工具名。
                'logistics_resend_consign',
                # 第四十批：IM 会话操作的三条写，同样只加**完整写工具名**。
                # 故意不加 'im_' 宽前缀，那会把只读的 im_blacklist_status 一起误判成写工具。
                'im_blacklist_add', 'im_blacklist_remove', 'im_card_send')
write_names = [n for n in specs if n.startswith(WRITE_PREFIX) and n != 'promotion_write_actions']
print(f'write_tools={len(write_names)}')
for s in T.TOOL_SPECS:
    fn = s['function']
    n = fn['name']
    if n not in write_names:
        continue
    if TOOL_ROUTES.get(n, {}).get('method') != 'POST':
        fail.append(f'{n}: MCP method 不是 POST')
    if 'confirm' not in fn['parameters'].get('required', []):
        fail.append(f'{n}: required 里没有 confirm')
    if 'confirm=true' not in fn.get('description', ''):
        fail.append(f'{n}: description 没写 confirm=true')

# 5) 只读工具里不应出现 confirm
for s in T.TOOL_SPECS:
    fn = s['function']
    if fn['name'] in write_names:
        continue
    if 'confirm' in fn['parameters'].get('properties', {}):
        fail.append(f'{fn["name"]}: 只读工具却带 confirm 参数')

print(f'route_checked={checked}')
print('ALIASES=%d' % len(ALIASES))
if fail:
    print('\n=== FAIL ===')
    for f in fail:
        print(' -', f)
    print(f'RECON_FAIL={len(fail)}')
    sys.exit(1)
print('RECON OK：五方一致，且每条路由的 path/query/body 都被真实端点接受。')
