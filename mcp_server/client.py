"""MCP 工具 → 本地后端 REST 的薄 HTTP 客户端。

设计约束：
- 只用 Python 3.10 标准库（`urllib`），不引入 requests / mcp SDK。
- **后端不可用时不抛栈**：一律返回结构化错误
  `{"success": False, "code": "...", "message": "...", "detail": {...}}`，
  与 `backend/app/api/system.py` 的信封一致（成功 `{"success":true,"data":..}`）。
- base_url：`XYWB_BASE_URL` 环境变量，默认 `http://127.0.0.1:8080`。
- token：`XYWB_TOKEN` 环境变量优先，否则读
  `%APPDATA%\\xianyu-ai-workbench\\data\\mcp_token.txt`（后端启动时用
  `backend/app/core/security.py::write_mcp_token` 写入）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

DEFAULT_BASE_URL = 'http://127.0.0.1:8080'
DEFAULT_TIMEOUT = 30.0

# 与 backend/app/core/config.py 保持一致。
# 这里不 import backend（MCP server 可能独立部署），只在需要时按同样规则算路径。
_APP_NAME = 'xianyu-ai-workbench'


def _app_dir() -> Path:
    """数据目录的父目录，与 `backend/app/core/config.py:19` 同规则。

    `XIANYU_APP_DIR` 优先（测试/CI 用它把整个数据目录挪到临时位置），否则用 APPDATA。
    """
    override = os.environ.get('XIANYU_APP_DIR')
    if override:
        return Path(override)
    appdata = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
    return Path(appdata) / _APP_NAME


def default_token_path() -> Path:
    return _app_dir() / 'data' / 'mcp_token.txt'


def default_port_path() -> Path:
    """后端把实际监听端口写在这里（`backend/app/main.py::_write_port_file`）。

    桌面形态端口是随机挑的（`desktop/main.py:191` 调 `pick_free_port()`），
    插件不可能猜中 8080，只能读这个文件。
    """
    return _app_dir() / 'data' / 'backend_port.txt'


def _read_port_file() -> Tuple[Optional[int], str]:
    """读 `backend_port.txt`，返回 (端口, 说明)。任何异常都降级为「没有」。"""
    path = default_port_path()
    try:
        if not path.exists():
            return None, '不存在'
        text = path.read_bytes().decode('utf-8', errors='replace').strip()
        if not text:
            return None, '空文件'
        port = int(text.split()[0])
        if not (1 <= port <= 65535):
            return None, f'端口越界({port})'
        return port, f'file:{path}'
    except (OSError, ValueError) as exc:
        return None, f'读取失败({type(exc).__name__})'


def resolve_base_url(explicit: Optional[str] = None) -> Tuple[str, str]:
    """决定后端地址，返回 `(base_url, 来源说明)`。

    优先级：显式参数 > `XYWB_BASE_URL` > `data/backend_port.txt` > 默认 8080。

    端口文件是**桌面形态的关键**：那里端口是随机挑的，用户没法预先填
    `XYWB_BASE_URL`；后端每次启动都会重写这个文件（`backend/app/main.py:81-95`）。
    """
    if explicit:
        return explicit.rstrip('/'), 'arg'
    env = (os.environ.get('XYWB_BASE_URL') or '').strip()
    if env:
        return env.rstrip('/'), 'env:XYWB_BASE_URL'
    port, why = _read_port_file()
    if port:
        return f'http://127.0.0.1:{port}', why
    return DEFAULT_BASE_URL, f'default (端口文件{why})'


# --------------------------------------------------------------------------
# 结构化错误
# --------------------------------------------------------------------------

def error(code: str, message: str, **detail: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {'success': False, 'code': code, 'message': message}
    if detail:
        payload['detail'] = detail
    return payload


def _truthy(value: Any) -> bool:
    """只认真正的真：`'false'` / `0` / `None` 都不算确认。

    与 `backend/app/services/item_templates.py::_truthy` 同一口径 —— 确认这种事
    宁可多问一次，也不能因为模型传了个字符串 `'false'` 就当它点了头。
    """
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return False


def ok(data: Any) -> Dict[str, Any]:
    return {'success': True, 'data': data}


# --------------------------------------------------------------------------
# 路由表：规范工具名 → REST 调用
#
# 规范名以 docs/CONTRACTS.md §5 为准（2026-10-06 lead 裁决）。
# 动词式别名只允许在 tools/call 里出现，不出现在 tools/list。
# --------------------------------------------------------------------------

# kind: query / path / body
TOOL_ROUTES: Dict[str, Dict[str, Any]] = {
    'accounts_list': {
        # 这三个筛选参数在端点上是**可选**的（`backend/app/api/accounts.py:206`
        # 的 `list_accounts` 收 enabled/q/limit），不传时行为与以前一致。
        'method': 'GET', 'path': '/api/accounts',
        'query': ['enabled', 'q', 'limit'],
    },
    'accounts_set_enabled': {
        'method': 'PATCH', 'path': '/api/accounts/{account_id}',
        'path_params': ['account_id'], 'body': ['enabled', 'auto_reply', 'remark'],
    },
    'accounts_relogin': {
        'method': 'POST', 'path': '/api/accounts/{account_id}/relogin',
        'path_params': ['account_id'],
    },
    'items_list': {
        'method': 'GET', 'path': '/api/items',
        'query': ['account_id', 'status', 'q', 'limit', 'offset'],
    },
    'items_get': {
        'method': 'GET', 'path': '/api/items/{account_id}/{item_id}',
        'path_params': ['account_id', 'item_id'],
    },
    'items_edit': {
        'method': 'POST', 'path': '/api/items/{account_id}/{item_id}/edit',
        'path_params': ['account_id', 'item_id'],
        'body': ['price', 'title', 'description', 'stock', 'images', 'category_id'],
    },
    'items_publish': {
        'method': 'POST', 'path': '/api/items/{account_id}/publish',
        'path_params': ['account_id'],
        'body': ['title', 'price', 'description', 'images', 'category_id', 'stock', 'original_price'],
    },
    # 发布前预检：只读，但走 POST（body 是那份表单）——**不带 confirm**，
    # 带了会被 confirm 闸门拦下，只读工具不该要点头。
    'items_listing_precheck': {
        'method': 'POST', 'path': '/api/items/{account_id}/publish/precheck',
        'path_params': ['account_id'],
        'body': ['title', 'price', 'description', 'images', 'category_id'],
    },
    'items_batch_edit': {
        'method': 'POST', 'path': '/api/items/{account_id}/batch-edit',
        'path_params': ['account_id'], 'body': ['item_ids', 'patch'],
    },
    'items_sync': {
        'method': 'POST', 'path': '/api/items/{account_id}/sync',
        'path_params': ['account_id'],
    },
    # ---------------- 草稿箱（2 读 + 2 写）----------------
    # 两条读**不能**带 confirm：带了就会被 confirm 闸门拦下，只读工具不该要点头。
    'drafts_list': {
        'method': 'GET', 'path': '/api/drafts/{account_id}',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    'drafts_get': {
        'method': 'GET', 'path': '/api/drafts/{account_id}/{draft_id}',
        'path_params': ['account_id', 'draft_id'],
    },
    'drafts_delete': {
        'method': 'POST', 'path': '/api/drafts/{account_id}/{draft_id}/delete',
        'path_params': ['account_id', 'draft_id'], 'body': ['confirm'],
    },
    'drafts_batch_delete': {
        'method': 'POST', 'path': '/api/drafts/{account_id}/batch-delete',
        'path_params': ['account_id'], 'body': ['draft_ids', 'confirm'],
    },
    'chats_list': {
        'method': 'GET', 'path': '/api/chats/conversations',
        'query': ['account_id', 'mode', 'limit'],
    },
    'chats_read': {
        'method': 'GET', 'path': '/api/chats/{conversation_id}/messages',
        'path_params': ['conversation_id'], 'query': ['limit', 'before_id'],
    },
    'chats_send': {
        'method': 'POST', 'path': '/api/chats/{conversation_id}/send',
        'path_params': ['conversation_id'], 'body': ['content'],
    },
    'chats_set_mode': {
        'method': 'POST', 'path': '/api/chats/{conversation_id}/mode',
        'path_params': ['conversation_id'], 'body': ['mode'],
    },
    # ---- 话术 / 快捷回复（1 读 + 6 写；写的那六条会真的改动平台上的快捷回复）----
    'quick_reply_list': {
        'method': 'GET', 'path': '/api/quick-reply/{account_id}/groups',
        'path_params': ['account_id'],
    },
    'quick_reply_group_create': {
        'method': 'POST', 'path': '/api/quick-reply/{account_id}/groups',
        'path_params': ['account_id'], 'body': ['group_name', 'confirm'],
    },
    'quick_reply_group_update': {
        'method': 'POST', 'path': '/api/quick-reply/{account_id}/groups/{group_id}',
        'path_params': ['account_id', 'group_id'], 'body': ['group_name', 'confirm'],
    },
    'quick_reply_group_delete': {
        'method': 'POST', 'path': '/api/quick-reply/{account_id}/groups/{group_id}/delete',
        'path_params': ['account_id', 'group_id'], 'body': ['confirm'],
    },
    'quick_reply_phrase_create': {
        'method': 'POST', 'path': '/api/quick-reply/{account_id}/phrases',
        'path_params': ['account_id'], 'body': ['group_id', 'content', 'confirm'],
    },
    'quick_reply_phrase_update': {
        'method': 'POST', 'path': '/api/quick-reply/{account_id}/phrases/{content_id}',
        'path_params': ['account_id', 'content_id'],
        'body': ['group_id', 'content', 'confirm'],
    },
    'quick_reply_phrase_delete': {
        'method': 'POST', 'path': '/api/quick-reply/{account_id}/phrases/{content_id}/delete',
        'path_params': ['account_id', 'content_id'], 'body': ['group_id', 'confirm'],
    },
    # ---- 物流（2 读 + 1 写；写的那条会真的改平台上的运单号）----
    'logistics_companies': {
        'method': 'GET', 'path': '/api/logistics/{account_id}/companies',
        'path_params': ['account_id'],
    },
    'logistics_guess_mailno': {
        'method': 'GET', 'path': '/api/logistics/{account_id}/guess',
        'path_params': ['account_id'], 'query': ['mail_no'],
    },
    'logistics_resend_consign': {
        'method': 'POST', 'path': '/api/logistics/{account_id}/orders/{order_id}/resend',
        'path_params': ['account_id', 'order_id'], 'body': ['mail_no', 'cp_code', 'confirm'],
    },
    # ---- IM 会话操作（1 读 + 3 写；写的那三条会真的改平台）----
    'im_blacklist_status': {
        'method': 'GET', 'path': '/api/im/{account_id}/blacklist',
        'path_params': ['account_id'], 'query': ['session_id', 'conversation_id'],
    },
    'im_blacklist_add': {
        'method': 'POST', 'path': '/api/im/{account_id}/blacklist',
        'path_params': ['account_id'], 'body': ['session_id', 'conversation_id', 'confirm'],
    },
    'im_blacklist_remove': {
        'method': 'POST', 'path': '/api/im/{account_id}/blacklist/remove',
        'path_params': ['account_id'], 'body': ['session_id', 'conversation_id', 'confirm'],
    },
    'im_card_send': {
        'method': 'POST', 'path': '/api/im/{account_id}/cards',
        'path_params': ['account_id'], 'body': ['session_id', 'conversation_id', 'item_id', 'confirm'],
    },
    # ---- 纠纷协商（联系纠纷小二：3 读 + 4 写；写的那四条会真的发到平台）----
    # ---- 客服会话接管（3 条写；转交 / 结束 / 重新发起）----
    'im_session_deliver': {
        'method': 'POST', 'path': '/api/im/{account_id}/sessions/transfer',
        'path_params': ['account_id'],
        'body': ['target_uid', 'session_id', 'conversation_id', 'confirm'],
    },
    'im_session_leave': {
        'method': 'POST', 'path': '/api/im/{account_id}/sessions/leave',
        'path_params': ['account_id'],
        'body': ['session_id', 'conversation_id', 'confirm'],
    },
    'im_session_rejoin': {
        'method': 'POST', 'path': '/api/im/{account_id}/sessions/rejoin',
        'path_params': ['account_id'],
        'body': ['session_id', 'conversation_id', 'confirm'],
    },
    'trade_compensate_detail': {
        'method': 'GET',
        'path': '/api/trade/{account_id}/orders/{order_id}/compensations/{service_code}',
        'path_params': ['account_id', 'order_id', 'service_code'],
    },
    'trade_compensate_reasons': {
        'method': 'GET',
        'path': '/api/trade/{account_id}/compensations/{service_code}/reasons',
        'path_params': ['account_id', 'service_code'],
    },
    'trade_compensate_refuse': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/orders/{order_id}/compensations/{service_code}/refuse',
        'path_params': ['account_id', 'order_id', 'service_code'],
        'body': ['reason_choices', 'reason_desc', 'proof_urls', 'is_manual', 'confirm'],
    },
    'im_reception_status': {
        'method': 'GET', 'path': '/api/im/{account_id}/reception',
        'path_params': ['account_id'],
    },
    'im_set_reception': {
        'method': 'POST', 'path': '/api/im/{account_id}/reception',
        'path_params': ['account_id'], 'body': ['status', 'confirm'],
    },
    'im_session_search': {
        'method': 'GET', 'path': '/api/im/{account_id}/sessions/search',
        'path_params': ['account_id'], 'query': ['keyword', 'page', 'size'],
    },
    'im_item_search': {
        'method': 'GET', 'path': '/api/im/{account_id}/item-search',
        'path_params': ['account_id'],
        'query': ['session_id', 'conversation_id', 'role', 'keyword', 'page', 'size'],
    },
    'im_clear_unread': {
        'method': 'POST', 'path': '/api/im/{account_id}/unread/clean',
        'path_params': ['account_id'], 'body': ['kind', 'confirm'],
    },
    'trade_refund_refuse_reasons': {
        'method': 'GET', 'path': '/api/trade/{account_id}/refunds/{refund_id}/refuse-reasons',
        'path_params': ['account_id', 'refund_id'], 'query': ['order_id', 'refuse_reason_id'],
    },
    'trade_refund_detail': {
        'method': 'GET', 'path': '/api/trade/{account_id}/refunds/{refund_id}/detail',
        'path_params': ['account_id', 'refund_id'], 'query': ['order_id'],
    },
    'trade_postage_refund_detail': {
        'method': 'GET', 'path': '/api/trade/{account_id}/orders/{order_id}/postage-refund/detail',
        'path_params': ['account_id', 'order_id'],
        'query': ['refund_id', 'refund_postage_type'],
    },
    'trade_postage_refund_reasons': {
        'method': 'GET',
        'path': '/api/trade/{account_id}/orders/{order_id}/postage-refund/refuse-reasons',
        'path_params': ['account_id', 'order_id'],
        'query': ['refund_id', 'refund_postage_type', 'default_reason'],
    },
    'disputes_conversation': {
        'method': 'GET', 'path': '/api/disputes/{account_id}/conversation',
        'path_params': ['account_id'], 'query': ['order_id', 'refund_id', 'dispute_id'],
    },
    'disputes_messages': {
        'method': 'GET', 'path': '/api/disputes/{account_id}/{conversation_id}/messages',
        'path_params': ['account_id', 'conversation_id'], 'query': ['begin_time'],
    },
    'disputes_unread': {
        'method': 'GET', 'path': '/api/disputes/{account_id}/{conversation_id}/unread',
        'path_params': ['account_id', 'conversation_id'],
    },
    'disputes_send_text': {
        'method': 'POST', 'path': '/api/disputes/{account_id}/{conversation_id}/messages',
        'path_params': ['account_id', 'conversation_id'], 'body': ['text', 'confirm'],
    },
    'disputes_send_image': {
        'method': 'POST', 'path': '/api/disputes/{account_id}/{conversation_id}/messages/image',
        'path_params': ['account_id', 'conversation_id'],
        'body': ['url', 'ext', 'width', 'height', 'alt_text', 'thumb_url', 'confirm'],
    },
    'disputes_urge': {
        'method': 'POST', 'path': '/api/disputes/{account_id}/{conversation_id}/urge',
        'path_params': ['account_id', 'conversation_id'], 'body': ['confirm'],
    },
    'disputes_mark_read': {
        'method': 'POST', 'path': '/api/disputes/{account_id}/{conversation_id}/mark-read',
        'path_params': ['account_id', 'conversation_id'],
        'body': ['msg_id', 'sequence', 'confirm'],
    },
    # ---- 粉丝价（1 读 + 1 写；写的那条是「整份替换」，留空的组会被清空）----
    'items_fans_price': {
        'method': 'GET', 'path': '/api/items/{account_id}/{item_id}/fans-price',
        'path_params': ['account_id', 'item_id'],
    },
    'items_set_fans_price': {
        'method': 'POST', 'path': '/api/items/{account_id}/{item_id}/fans-price',
        'path_params': ['account_id', 'item_id'], 'body': ['groups', 'prices', 'confirm'],
    },
    'orders_list': {
        'method': 'GET', 'path': '/api/orders',
        'query': ['account_id', 'status', 'limit'],
    },
    'orders_deliver': {
        'method': 'POST', 'path': '/api/orders/{order_id}/deliver',
        'path_params': ['order_id'], 'body': ['company', 'tracking_no'],
    },
    'risk_status': {
        'method': 'GET', 'path': '/api/risk/status',
        'query': ['account_id'],
    },
    'risk_clear': {
        'method': 'POST', 'path': '/api/risk/{account_id}/clear',
        'path_params': ['account_id'],
    },
    'providers_list': {'method': 'GET', 'path': '/api/providers'},
    'providers_upsert': {
        # 带 id 走 PUT /api/providers/{id}，不带 id 走 POST /api/providers
        'method': 'POST', 'path': '/api/providers',
        'body': ['id', 'name', 'base_url', 'api_key', 'default_model', 'enabled', 'is_default'],
    },
    'master_chat': {
        'method': 'POST', 'path': '/api/agents/master/chat',
        'body': ['message', 'conversation_id'],
    },
    # 任务卡额外要求：AI 之间的沟通记录（CONTRACTS §3 已有该端点）
    'agents_messages': {
        'method': 'GET', 'path': '/api/agents/messages',
        'query': ['conversation_id'],
    },
    # ---------------- 交易（只读）----------------
    'trade_orders': {
        'method': 'GET', 'path': '/api/trade/{account_id}/orders',
        'path_params': ['account_id'],
        'query': ['page', 'size', 'include_counts'],
    },
    'trade_refunds': {
        'method': 'GET', 'path': '/api/trade/{account_id}/refunds',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    'trade_evaluations': {
        'method': 'GET', 'path': '/api/trade/{account_id}/evaluations',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    'trade_complaints': {
        'method': 'GET', 'path': '/api/trade/{account_id}/complaints',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    'trade_addresses': {
        'method': 'GET', 'path': '/api/trade/{account_id}/addresses',
        'path_params': ['account_id'],
    },
    # ---------------- 小铺（只读）----------------
    'shop_sub_accounts': {
        'method': 'GET', 'path': '/api/shop/{account_id}/sub-accounts',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    'shop_cs_routing': {
        'method': 'GET', 'path': '/api/shop/{account_id}/cs-routing',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    # ---------------- 客服分流写（POST；confirm 必须在 body 里，否则 MCP 写闸门不生效）----------------
    'shop_cs_routing_rule_save': {
        'method': 'POST', 'path': '/api/shop/{account_id}/cs-routing/rule',
        'path_params': ['account_id'],
        'body': ['distribution_method', 'repeat_customer', 'offline_message',
                 'specified_account_id', 'specified_account_nick', 'service_time', 'confirm'],
    },
    'shop_cs_group_save': {
        'method': 'POST', 'path': '/api/shop/{account_id}/cs-groups',
        'path_params': ['account_id'],
        'body': ['group_name', 'mode', 'operate_type', 'group_id', 'specified_type',
                 'specified_type_name', 'type_data', 'customer_uids', 'confirm'],
    },
    'shop_cs_group_delete': {
        'method': 'POST', 'path': '/api/shop/{account_id}/cs-groups/delete',
        'path_params': ['account_id'],
        'body': ['group_id', 'confirm'],
    },
    # ---------------- 店铺资料（读 + 写）----------------
    'shop_profile_get': {
        'method': 'GET', 'path': '/api/shop/{account_id}/profile',
        'path_params': ['account_id'],
    },
    'shop_profile_update_nick': {
        'method': 'POST', 'path': '/api/shop/{account_id}/profile/nick',
        'path_params': ['account_id'], 'body': ['nick', 'confirm'],
    },
    'shop_profile_update_desc': {
        'method': 'POST', 'path': '/api/shop/{account_id}/profile/desc',
        'path_params': ['account_id'], 'body': ['desc', 'confirm'],
    },
    'shop_profile_update_avatar': {
        'method': 'POST', 'path': '/api/shop/{account_id}/profile/avatar',
        'path_params': ['account_id'], 'body': ['avatar_url', 'confirm'],
    },
    # ---------------- 子账号（读 + 写）----------------
    'sub_accounts_roles': {
        'method': 'GET', 'path': '/api/sub-accounts/{account_id}/roles',
        'path_params': ['account_id'],
    },
    'sub_account_create': {
        'method': 'POST', 'path': '/api/sub-accounts/{account_id}',
        'path_params': ['account_id'],
        'body': ['nick', 'mobile', 'role_ids', 'password', 'custom_name', 'confirm'],
    },
    'sub_account_delete': {
        'method': 'POST', 'path': '/api/sub-accounts/{account_id}/{sub_id}/delete',
        'path_params': ['account_id', 'sub_id'], 'body': ['confirm'],
    },
    'sub_account_update_mobile': {
        'method': 'POST', 'path': '/api/sub-accounts/{account_id}/{sub_id}/mobile',
        'path_params': ['account_id', 'sub_id'], 'body': ['mobile', 'confirm'],
    },
    'sub_account_update_roles': {
        'method': 'POST', 'path': '/api/sub-accounts/{account_id}/{sub_id}/roles',
        'path_params': ['account_id', 'sub_id'], 'body': ['role_ids', 'confirm'],
    },
    'sub_account_update_password': {
        'method': 'POST', 'path': '/api/sub-accounts/{account_id}/{sub_id}/password',
        'path_params': ['account_id', 'sub_id'], 'body': ['password', 'confirm'],
    },
    # ---------------- 数据中心（只读）----------------
    'datacenter_overview': {
        'method': 'GET', 'path': '/api/datacenter/{account_id}/overview',
        'path_params': ['account_id'], 'query': ['start', 'end'],
    },
    'datacenter_fans': {
        'method': 'GET', 'path': '/api/datacenter/{account_id}/fans',
        'path_params': ['account_id'], 'query': ['start', 'end'],
    },
    'datacenter_customer_service': {
        'method': 'GET', 'path': '/api/datacenter/{account_id}/customer-service',
        'path_params': ['account_id'], 'query': ['start', 'end'],
    },
    'datacenter_commodity': {
        'method': 'GET', 'path': '/api/datacenter/{account_id}/commodity',
        'path_params': ['account_id'],
        'query': ['start', 'end', 'item_id', 'item_name', 'page', 'size',
                  'order_by', 'order'],
    },
    'datacenter_card': {
        'method': 'GET', 'path': '/api/datacenter/{account_id}/card/{kind}',
        'path_params': ['account_id', 'kind'], 'query': ['start', 'end'],
    },
    'datacenter_cs_list': {
        'method': 'GET', 'path': '/api/datacenter/{account_id}/cs/{kind}',
        'path_params': ['account_id', 'kind'],
        'query': ['start', 'end', 'page', 'size', 'order_by', 'order'],
    },
    # ---------------- 通知（只读）----------------
    'notifications_list': {
        'method': 'GET', 'path': '/api/notifications/{account_id}',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    # ---------------- 运费模版（只读）----------------
    'item_templates_list': {
        'method': 'GET', 'path': '/api/item-templates/{account_id}',
        'path_params': ['account_id'], 'query': ['page', 'size', 'template_name'],
    },
    'item_templates_get': {
        'method': 'GET', 'path': '/api/item-templates/{account_id}/{template_id}',
        'path_params': ['account_id', 'template_id'],
    },
    # ---------------- 运费模版写（POST；confirm 是必填）----------------
    'item_templates_create': {
        'method': 'POST', 'path': '/api/item-templates/{account_id}/create',
        'path_params': ['account_id'],
        'body': ['confirm', 'template_name', 'item_prov', 'item_city', 'valuation',
                 'is_default', 'details', 'template_details'],
    },
    'item_templates_modify': {
        'method': 'POST', 'path': '/api/item-templates/{account_id}/{template_id}/modify',
        'path_params': ['account_id', 'template_id'],
        'body': ['confirm', 'template_name', 'item_prov', 'item_city', 'valuation',
                 'is_default', 'details', 'template_details', 'version'],
    },
    'item_templates_delete': {
        'method': 'POST', 'path': '/api/item-templates/{account_id}/{template_id}/delete',
        'path_params': ['account_id', 'template_id'],
        'body': ['confirm'],
    },
    'item_templates_unbind': {
        'method': 'POST', 'path': '/api/item-templates/{account_id}/unbind',
        'path_params': ['account_id'],
        'body': ['confirm', 'item_ids', 'mode', 'template_id', 'post_price'],
    },
    # ---------------- 财务（只读）----------------
    # 注意 query_rename：工具参数叫 cycle_from/cycle_to（= 服务层 `finance.account_bills`
    # 的签名），但 HTTP 端点的线上名是 `from`/`to`（`backend/app/api/finance.py:44,60`
    # 用的是 `Query(alias='from'/'to')`）。没有这张表就会发 `?cycle_from=…`，
    # FastAPI 静默忽略未知 query ⇒ 账期悄悄回落默认值，**结果错但不报错**。
    'finance_income': {
        'method': 'GET', 'path': '/api/finance/{account_id}/income',
        'path_params': ['account_id'],
        'query': ['cycle_from', 'cycle_to', 'biz_type', 'page', 'size'],
        'query_rename': {'cycle_from': 'from', 'cycle_to': 'to'},
    },
    'finance_expense': {
        'method': 'GET', 'path': '/api/finance/{account_id}/expense',
        'path_params': ['account_id'],
        'query': ['cycle_from', 'cycle_to', 'biz_type', 'page', 'size'],
        'query_rename': {'cycle_from': 'from', 'cycle_to': 'to'},
    },
    'finance_invoices': {
        'method': 'GET', 'path': '/api/finance/{account_id}/invoices',
        'path_params': ['account_id'], 'query': ['page', 'size', 'biz_types'],
    },
    'finance_purchaser': {
        'method': 'GET', 'path': '/api/finance/{account_id}/purchaser',
        'path_params': ['account_id'],
    },
    # ---------------- 推广（只读）----------------
    'promotion_overview': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/overview',
        'path_params': ['account_id'], 'query': ['start', 'end'],
    },
    'promotion_campaigns': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/campaigns',
        'path_params': ['account_id'], 'query': ['start', 'end', 'page', 'size'],
    },
    'promotion_reports': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/reports',
        'path_params': ['account_id'], 'query': ['start', 'end', 'page', 'size'],
    },
    'promotion_account': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/account',
        'path_params': ['account_id'], 'query': ['start', 'end'],
    },
    'promotion_funds': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/funds',
        'path_params': ['account_id'],
        'query': ['start', 'end', 'page', 'size', 'account_type', 'operate_type'],
    },
    'promotion_fund_operations': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/fund-operations',
        'path_params': ['account_id'], 'query': ['account_type'],
    },
    'promotion_recharge_detail': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/recharge/{order_id}',
        'path_params': ['account_id', 'order_id'], 'query': [],
    },
    'promotion_withdraw_detail': {
        'method': 'GET', 'path': '/api/promotion/{account_id}/withdraw/{order_id}',
        'path_params': ['account_id', 'order_id'], 'query': [],
    },
    # ---------------- 安全中心（只读）----------------
    'security_center_status': {
        'method': 'GET', 'path': '/api/security-center/{account_id}',
        'path_params': ['account_id'], 'query': ['page', 'size'],
    },
    # ---------------- 安全中心申诉（`form` 是只读语义，其余三条会真的提交，body 必含 confirm）----
    'security_appeal_form': {
        'method': 'POST', 'path': '/api/security-center/{account_id}/appeal/form',
        'path_params': ['account_id'], 'query': [], 'body': ['form_id'],
    },
    'security_appeal_submit': {
        'method': 'POST', 'path': '/api/security-center/{account_id}/appeal/submit',
        'path_params': ['account_id'], 'query': [], 'body': ['parameter', 'confirm'],
    },
    'security_appeal_submit_cro': {
        'method': 'POST', 'path': '/api/security-center/{account_id}/appeal/cro/submit',
        'path_params': ['account_id'], 'query': [], 'body': ['punish_id', 'proof_items', 'confirm'],
    },
    'security_appeal_urge': {
        'method': 'POST', 'path': '/api/security-center/{account_id}/appeal/urge',
        'path_params': ['account_id'], 'query': [], 'body': ['punish_id', 'confirm'],
    },
    # ---------------- 营销（只读）----------------
    # 路径名沿用首版（前端与 MCP 都写死了它）；2026-10 订正的是**查询参数**：
    # /items 与 /records 是单对象口径（必给 item_id / task_id，不吃分页），
    # /price-competitiveness 要 item_id + sku_prices。
    'marketing_items': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/items',
        'path_params': ['account_id'], 'query': ['item_id', 'date_type', 'date_range'],
    },
    'marketing_tasks': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/tasks',
        'path_params': ['account_id'],
        'query': ['biz_scene', 'query_scene', 'page', 'size', 'item_ids'],
    },
    'marketing_price_competitiveness': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/price-competitiveness',
        'path_params': ['account_id'],
        'query': ['biz_scene', 'item_id', 'sku_prices', 'commission_rate'],
    },
    'marketing_records': {
        'method': 'GET', 'path': '/api/marketing/{account_id}/records',
        'path_params': ['account_id'], 'query': ['task_id', 'biz_scene', 'query_scene'],
    },
    # ---------------- 交易写（全部 POST；confirm 是必填）----------------
    'trade_write_ship_offline': {
        'method': 'POST', 'path': '/api/trade/{account_id}/orders/ship',
        'path_params': ['account_id'],
        'body': ['trade_ids', 'mail_no', 'cp_code', 'address_id', 'confirm'],
    },
    'trade_write_ship_dummy': {
        'method': 'POST', 'path': '/api/trade/{account_id}/orders/{order_id}/ship-dummy',
        'path_params': ['account_id', 'order_id'],
        'body': ['trade_text', 'pic_urls', 'confirm'],
    },
    'trade_write_adjust_price': {
        'method': 'POST', 'path': '/api/trade/{account_id}/orders/{order_id}/adjust-price',
        'path_params': ['account_id', 'order_id'],
        'body': ['modify_fee', 'new_transport_fee', 'confirm'],
    },
    'trade_write_refund_agree': {
        'method': 'POST', 'path': '/api/trade/{account_id}/refunds/{refund_id}/agree',
        'path_params': ['account_id', 'refund_id'],
        'body': ['auth_token', 'confirm'],
    },
    # 只读：拿同意退款要用的 auth_token（没有 confirm，不该有）
    'trade_refund_verify_token': {
        'method': 'GET', 'path': '/api/trade/{account_id}/refunds/{refund_id}/verify-token',
        'path_params': ['account_id', 'refund_id'],
        'query': [],
    },
    'trade_write_refund_refuse': {
        'method': 'POST', 'path': '/api/trade/{account_id}/refunds/{refund_id}/refuse',
        'path_params': ['account_id', 'refund_id'],
        'body': ['order_id', 'refuse_reason_id', 'proof_desc', 'proof_urls', 'negotiation', 'confirm'],
    },
    'trade_write_rate_create': {
        'method': 'POST', 'path': '/api/trade/{account_id}/evaluations',
        'path_params': ['account_id'],
        'body': ['trade_ids', 'feedback', 'rate', 'image_urls', 'anonymous', 'confirm'],
    },
    'trade_write_complain_refuse': {
        'method': 'POST', 'path': '/api/trade/{account_id}/complaints/{complain_id}/refuse',
        'path_params': ['account_id', 'complain_id'],
        'body': ['proof_desc', 'proof_urls', 'confirm'],
    },
    'trade_write_complain_proof': {
        'method': 'POST', 'path': '/api/trade/{account_id}/complaints/{complain_id}/proof',
        'path_params': ['account_id', 'complain_id'],
        'body': ['proof_desc', 'proof_urls', 'kind', 'confirm'],
    },
    # ---------------- 退货地址写（POST；confirm 是必填）----------------
    'trade_address_add': {
        'method': 'POST', 'path': '/api/trade/{account_id}/addresses',
        'path_params': ['account_id'],
        'body': ['contact_name', 'mobile_phone', 'province_name', 'city_name',
                 'district_name', 'detail_address', 'area_id', 'default_addr', 'confirm'],
    },
    'trade_address_update': {
        'method': 'POST', 'path': '/api/trade/{account_id}/addresses/{contact_id}/update',
        'path_params': ['account_id', 'contact_id'],
        'body': ['contact_name', 'mobile_phone', 'province_name', 'city_name',
                 'district_name', 'detail_address', 'area_id', 'default_addr', 'confirm'],
    },
    'trade_address_delete': {
        'method': 'POST', 'path': '/api/trade/{account_id}/addresses/{contact_id}/delete',
        'path_params': ['account_id', 'contact_id'],
        'body': ['confirm'],
    },
    'trade_address_set_default': {
        'method': 'POST', 'path': '/api/trade/{account_id}/addresses/{contact_id}/default',
        'path_params': ['account_id', 'contact_id'],
        'body': ['confirm'],
    },
    # ---------------- 推广写（POST；目录是只读 GET）----------------
    'promotion_write_actions': {
        'method': 'GET', 'path': '/api/promotion/write/actions',
        'path_params': [], 'query': [],
    },
    'promotion_write_execute': {
        'method': 'POST', 'path': '/api/promotion/write/{account_id}/{action}',
        'path_params': ['account_id', 'action'],
        'body': ['confirm', 'payload'],
    },
    # ---------------- 资质写（POST；confirm 是必填）----------------
    'qualification_submit_packages': {
        'method': 'POST', 'path': '/api/qualification/{account_id}/packages/submit',
        'path_params': ['account_id'],
        'body': ['confirm', 'request'],
    },
    'qualification_oss_credential': {
        'method': 'POST', 'path': '/api/qualification/{account_id}/oss-credential',
        'path_params': ['account_id'],
        'body': ['confirm', 'request'],
    },
    # ---------------- 商品写（POST；confirm 是必填）----------------
    'items_batch_offline': {
        'method': 'POST', 'path': '/api/items/{account_id}/batch-offline',
        'path_params': ['account_id'],
        'body': ['item_ids', 'confirm'],
    },
    'items_batch_delete': {
        'method': 'POST', 'path': '/api/items/{account_id}/batch-delete',
        'path_params': ['account_id'],
        'body': ['item_ids', 'confirm'],
    },
    'items_quick_edit': {
        'method': 'POST', 'path': '/api/items/{account_id}/{item_id}/quick-edit',
        'path_params': ['account_id', 'item_id'],
        'body': ['price', 'quantity', 'confirm'],
    },
    # ---------------- 改收货地址审批（1 读 + 2 写）----------------
    # 读那条**不能**带 confirm：带了就会被 confirm 闸门拦下，只读工具不该要点头。
    'trade_order_address_request': {
        'method': 'GET',
        'path': '/api/trade/{account_id}/orders/{order_id}/modify-address',
        'path_params': ['account_id', 'order_id'],
        'query': [],
    },
    'trade_order_address_agree': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/orders/{order_id}/modify-address/agree',
        'path_params': ['account_id', 'order_id'],
        'body': ['deliver_id', 'confirm'],
    },
    'trade_order_address_refuse': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/orders/{order_id}/modify-address/refuse',
        'path_params': ['account_id', 'order_id'],
        'body': ['deliver_id', 'confirm'],
    },
    'trade_close_reasons': {
        'method': 'GET',
        'path': '/api/trade/{account_id}/orders/{order_id}/close-reasons',
        'path_params': ['account_id', 'order_id'],
        'query': [],
    },
    'trade_close_order': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/orders/{order_id}/close',
        'path_params': ['account_id', 'order_id'],
        'body': ['close_reason', 'confirm'],
    },
    'trade_delay_confirm': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/orders/{order_id}/delay',
        'path_params': ['account_id', 'order_id'],
        'body': ['order_ids', 'days', 'confirm'],
    },
    'trade_remind_confirm': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/orders/{order_id}/remind',
        'path_params': ['account_id', 'order_id'],
        'body': ['order_ids', 'remind_all', 'confirm'],
    },
    'trade_postage_refund_pay': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/refunds/{refund_id}/postage-pay',
        'path_params': ['account_id', 'refund_id'],
        'body': ['order_id', 'pay_type', 'amount', 'confirm'],
    },
    'trade_complain_revoke': {
        'method': 'POST',
        'path': '/api/trade/{account_id}/complaints/{complain_id}/revoke',
        'path_params': ['account_id', 'complain_id'],
        'body': ['confirm'],
    },
}

# 动词式别名（task-6 任务卡里的写法）→ 规范名
ALIASES: Dict[str, str] = {
    'list_accounts': 'accounts_list',
    'set_account_enabled': 'accounts_set_enabled',
    'relogin_account': 'accounts_relogin',
    'list_items': 'items_list',
    'get_item': 'items_get',
    'update_item': 'items_edit',
    'edit_item': 'items_edit',
    'publish_item': 'items_publish',
    'batch_update_items': 'items_batch_edit',
    'batch_edit_items': 'items_batch_edit',
    'sync_items': 'items_sync',
    'list_conversations': 'chats_list',
    'get_messages': 'chats_read',
    'send_message': 'chats_send',
    'set_chat_mode': 'chats_set_mode',
    'list_orders': 'orders_list',
    'deliver_order': 'orders_deliver',
    'get_risk_status': 'risk_status',
    'clear_risk': 'risk_clear',
    'list_providers': 'providers_list',
    'upsert_provider': 'providers_upsert',
    'list_agent_messages': 'agents_messages',
}


def canonical_name(name: str) -> Optional[str]:
    """别名/规范名 → 规范名；未知工具返回 None。"""
    if not name:
        return None
    if name in TOOL_ROUTES:
        return name
    return ALIASES.get(name)


#: MCP 侧独有的工具（不在 agents 的 TOOL_SPECS 里，但 task-6 要求暴露）。
#: `backend/tests/test_mcp_server.py::test_no_routeless_or_unknown_listed` 按这个集合对账。
MCP_ONLY_TOOLS = frozenset({'agents_messages'})


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class BackendClient:
    """极薄的同步 HTTP 客户端。所有失败都变成结构化 dict，绝不抛栈。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url, self.base_url_source = resolve_base_url(base_url)
        if token is not None:
            self._token, self.token_source = token, 'arg'
        else:
            # 只读一次：两次读取之间文件若被后端重写，token 与来源说明会不一致。
            self._token, self.token_source = _read_token()
        if timeout is None:
            raw = os.environ.get('XYWB_TIMEOUT')
            try:
                timeout = float(raw) if raw else DEFAULT_TIMEOUT
            except ValueError:
                timeout = DEFAULT_TIMEOUT
        self.timeout = timeout

    # -- HTTP ------------------------------------------------------------

    def _rediscover(self) -> bool:
        """后端换端口后重新发现地址：改动了自身就返回 True。

        桌面形态每次启动都换随机端口，而插件进程可能比后端先起 —— 地址只在
        `__init__` 解析一次的话，插件会一直指向死端口直到进程重启。这里在**连接
        失败时**重解析一次（只对自动发现的地址做，显式传入的地址不擅自改）。
        """
        if self.base_url_source == 'explicit':
            return False
        changed = False
        new_url, new_source = resolve_base_url(None)
        if new_url != self.base_url:
            self.base_url, self.base_url_source = new_url, new_source
            changed = True
        if not self._token:
            new_token, new_token_source = _read_token()
            if new_token:
                self._token, self.token_source = new_token, new_token_source
                changed = True
        return changed

    def request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        _retried: bool = False,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += '?' + urllib_parse.urlencode(clean, doseq=True)

        headers = {'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token

        req = urllib_request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, 'status', 200) or 200
                raw = resp.read()
        except urllib_error.HTTPError as exc:            # 4xx/5xx：后端有响应
            try:
                raw = exc.read()
            except Exception:                            # pragma: no cover - 极端情况
                raw = b''
            return _http_error_result(exc.code, raw, url)
        except urllib_error.URLError as exc:             # 连不上 / DNS / 超时
            reason = getattr(exc, 'reason', exc)
            if not _retried and self._rediscover():
                return self.request(method, path, query, body, _retried=True)
            code = 'backend_timeout' if 'timed out' in str(reason).lower() else 'backend_unreachable'
            return error(
                code,
                f'无法连接本地后端 {self.base_url}（{reason}）。'
                '请先启动后端：python -m backend.app.main，或用 desktop/main.py 打开桌面应用。',
                base_url=self.base_url, url=url,
            )
        except OSError as exc:                           # socket 层
            if not _retried and self._rediscover():
                return self.request(method, path, query, body, _retried=True)
            return error('backend_unreachable', f'连接后端失败：{exc}', base_url=self.base_url, url=url)
        except Exception as exc:                         # 兜底：绝不抛栈
            return error('client_error', f'{type(exc).__name__}: {exc}', url=url)

        return _decode_result(status, raw, url)

    # -- 工具调用 ---------------------------------------------------------

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = dict(args or {})
        cname = canonical_name(name)
        if cname is None:
            return error(
                'unknown_tool',
                f'未知工具 {name!r}。可用工具见 tools/list。',
                available=sorted(TOOL_ROUTES),
            )

        if cname == 'items_get':
            return self._items_get(args)
        if cname == 'providers_upsert' and args.get('id'):
            # 与 _dispatch_route 同样的白名单 + 转义：模型多给的字段不能原样送到后端，
            # id 也必须转义，否则 '1/../accounts' 这类值能穿越到别的端点。
            allowed = TOOL_ROUTES['providers_upsert']['body']
            body = {k: args[k] for k in allowed if k in args and k != 'id'}
            if not body:
                return error('invalid_args', 'providers_upsert 带 id 时至少需要一个可更新字段：'
                                             + ', '.join(k for k in allowed if k != 'id'))
            return self._dispatch(
                'PUT', '/api/providers/' + urllib_parse.quote(str(args['id']), safe=''), None, body,
            )
        return self._dispatch_route(cname, args)

    # -- 内部 -------------------------------------------------------------

    def _dispatch_route(self, cname: str, args: Dict[str, Any]) -> Dict[str, Any]:
        route = TOOL_ROUTES[cname]
        path = route['path']

        # 写工具先问确认，再问别的。
        # 不这么做的话，模型什么都不带调 `item_templates_delete` 会收到
        # 「至少需要一个请求体字段：confirm」—— 那句话听起来像「随便补个字段就能发」，
        # 而真正缺的是那一步确认。放在最前面还有第二个好处：与 HTTP 侧的口径一致
        # （服务层也是「确认 → 字段校验 → 建客户端」），AI 看到的错误码统一是
        # `confirm_required`，不会一会儿 invalid_args 一会儿 confirm_required。
        if 'confirm' in (route.get('body') or []) and not _truthy(args.get('confirm')):
            return error('confirm_required', '写操作必须显式确认（confirm=true）后才会执行')

        missing: List[str] = []
        for key in route.get('path_params', []):
            if args.get(key) in (None, ''):
                missing.append(key)
            else:
                path = path.replace('{' + key + '}', urllib_parse.quote(str(args[key]), safe=''))
        if missing:
            return error('invalid_args', f'{cname} 缺少必填参数：{", ".join(missing)}', missing=missing)

        # query_rename：工具参数名 → HTTP 线上名（两端不一致时才需要，例如
        # finance 的 cycle_from/cycle_to 在端点上叫 from/to）。
        rename = route.get('query_rename') or {}
        query = {}
        for key in route.get('query', []):
            value = args.get(key)
            if value is not None:
                query[rename.get(key, key)] = value
        body = {k: args.get(k) for k in route.get('body', []) if k in args}
        if 'body' in route and not body and route['method'] != 'GET':
            return error('invalid_args', f'{cname} 至少需要一个请求体字段：{", ".join(route["body"])}')
        return self._dispatch(route['method'], path, query or None, body or None)

    def _dispatch(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]],
        body: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return self.request(method, path, query=query, body=body)

    def _items_get(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """直连 `GET /api/items/{account_id}/{item_id}`（CONTRACTS §3）。

        该端点由 services 工作流新增，落地前可能 404 —— 此时退化为
        「拉列表 + 本地按 item_id 精确过滤」，保证 MCP 侧始终可用。
        """
        account_id, item_id = args.get('account_id'), args.get('item_id')
        if not account_id or not item_id:
            return error('invalid_args', 'items_get 需要 account_id 与 item_id')
        direct = self.request(
            'GET',
            f'/api/items/{urllib_parse.quote(str(account_id), safe="")}'
            f'/{urllib_parse.quote(str(item_id), safe="")}',
        )
        if direct.get('success') or direct.get('code') != 'http_404':
            return direct
        # 兜底是「拉列表 + 本地精确过滤」，后端默认一页 50 条；这里把页撑到上限
        # （`limit` 在 backend/app/api/items.py:52-55 折算成 size，最大 500），
        # 否则目标商品排在第 50 条之后会被误报 not_found。
        listed = self.request('GET', '/api/items',
                              query={'account_id': account_id, 'q': item_id, 'limit': 500})
        if not listed.get('success'):
            return listed
        rows = listed.get('data')
        if isinstance(rows, dict):
            rows = rows.get('items') or rows.get('list') or []
        for row in rows or []:
            if isinstance(row, dict) and str(row.get('item_id')) == str(item_id):
                return ok({'item': row, '_fallback': 'list_filter'})
        return error('not_found', f'商品不存在：account_id={account_id} item_id={item_id}',
                     fallback='list_filter')

    def health(self) -> Dict[str, Any]:
        return self.request('GET', '/api/health')


def _read_token() -> Tuple[Optional[str], str]:
    """返回 (token, 来源说明)。环境变量优先，其次 mcp_token.txt。"""
    env_token = (os.environ.get('XYWB_TOKEN') or '').strip()
    if env_token:
        return env_token, 'env:XYWB_TOKEN'
    path = default_token_path()
    try:
        if path.exists():
            raw = path.read_bytes()
            try:
                token = raw.decode('utf-8').strip()
            except UnicodeDecodeError as exc:
                # 文件被写成 GBK/UTF-16 或被写坏。不能拿替换字符当 token 去发请求
                # （只会换回一个难懂的 401），如实降级为「无 token」。
                return None, f'file:{path} (不是 UTF-8，已忽略: {exc})'
            if token:
                if '\ufffd' in token or any(ord(ch) < 32 for ch in token):
                    return None, f'file:{path} (内容异常，已忽略)'
                return token, f'file:{path}'
            return None, f'file:{path} (空文件)'
        return None, f'file:{path} (不存在)'
    except OSError as exc:
        return None, f'file:{path} (读取失败: {exc})'
    except Exception as exc:      # 任何意外都不该打死 server
        return None, f'file:{path} (读取失败: {type(exc).__name__}: {exc})'


def _decode_result(status: int, raw: bytes, url: str) -> Dict[str, Any]:
    text = raw.decode('utf-8', errors='replace') if raw else ''
    if not text.strip():
        return error('empty_response', f'后端返回空响应（HTTP {status}）', url=url, status=status)
    try:
        payload = json.loads(text)
    except ValueError:
        return error('bad_response', f'后端返回非 JSON（HTTP {status}）', url=url,
                     status=status, body=text[:500])
    if isinstance(payload, dict) and 'success' in payload:
        if payload.get('success'):
            return payload
        return error(payload.get('code') or 'backend_error',
                     payload.get('message') or '后端返回失败',
                     url=url, status=status, body=payload)
    if 200 <= status < 300:
        return ok(payload)
    return error(f'http_{status}', f'HTTP {status}', url=url, status=status, body=payload)


def _http_error_result(status: int, raw: bytes, url: str) -> Dict[str, Any]:
    text = raw.decode('utf-8', errors='replace') if raw else ''
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = payload.get('code') or f'http_{status}'
        message = payload.get('message') or f'HTTP {status}'
        if status in (401, 403) and code == f'http_{status}':
            code = 'unauthorized'
            message = '后端拒绝授权：检查 mcp_token.txt 是否为最新（后端每次启动会重写），或设 XYWB_TOKEN。'
        return error(code, message, url=url, status=status, body=payload)
    hint = ''
    if status == 404:
        hint = '（端点可能还没实现）'
    return error(f'http_{status}', f'HTTP {status}{hint}', url=url, status=status, body=text[:500])


# --------------------------------------------------------------------------
# MCP 工具定义（tools/list 用）
# --------------------------------------------------------------------------

def _prop(kind: str, desc: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {'type': kind, 'description': desc}
    out.update(extra)
    return out


_ACCOUNT_ID = _prop('string', '闲鱼账号 id（unb），用 accounts_list 查')
_CONV_ID = _prop('integer', '会话 id，用 chats_list 查')
_ITEM_ID = _prop('string', '闲鱼商品 id，用 items_list 查')

#: 规范名 → {description, input_schema}
CANONICAL_SPECS: Dict[str, Dict[str, Any]] = {
    'accounts_list': {
        'description': '列出已登录的闲鱼账号（含启用状态、登录态、风控运行时状态）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'enabled': _prop('boolean', '只看启用/停用的账号'),
                'q': _prop('string', '按昵称/备注模糊搜索'),
                'limit': _prop('integer', '返回条数上限'),
            },
        },
    },
    'accounts_set_enabled': {
        'description': '开启/停用某个闲鱼账号，或改自动回复开关与备注。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'enabled': _prop('boolean', '是否启用该账号（启用才会自动同步/回复）'),
                'auto_reply': _prop('boolean', '是否允许 AI 自动回复'),
                'remark': _prop('string', '备注名'),
            },
            'required': ['account_id'],
        },
    },
    'accounts_relogin': {
        'description': '让某个账号重新扫码登录（Cookie 失效时用）。',
        'input_schema': {
            'type': 'object',
            'properties': {'account_id': _ACCOUNT_ID},
            'required': ['account_id'],
        },
    },
    'items_list': {
        'description': '查询商品列表，可按账号/状态/关键词过滤。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'status': _prop('string', '商品状态，如 onsale / sold / delisted'),
                'q': _prop('string', '标题关键词'),
                'limit': _prop('integer', '返回条数上限'),
                'offset': _prop('integer', '分页偏移'),
            },
        },
    },
    'items_get': {
        'description': '查单个商品的完整信息（标题、价格、库存、描述、图片）。',
        'input_schema': {
            'type': 'object',
            'properties': {'account_id': _ACCOUNT_ID, 'item_id': _ITEM_ID},
            'required': ['account_id', 'item_id'],
        },
    },
    'items_edit': {
        'description': '修改单个商品的价格/标题/描述/库存/图片。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'item_id': _ITEM_ID,
                'price': _prop('number', '新价格（元）'),
                'title': _prop('string', '新标题'),
                'description': _prop('string', '新描述'),
                'stock': _prop('integer', '新库存'),
                'images': _prop('array', '图片 URL 列表', items={'type': 'string'}),
                'category_id': _prop('string', '类目 id'),
            },
            'required': ['account_id', 'item_id'],
        },
    },
    'items_publish': {
        'description': '在指定账号下发布一个新商品。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'title': _prop('string', '标题'),
                'price': _prop('number', '价格（元）'),
                'description': _prop('string', '描述'),
                'images': _prop('array', '图片 URL 列表', items={'type': 'string'}),
                'category_id': _prop('string', '类目 id'),
                'stock': _prop('integer', '库存'),
                'original_price': _prop('number', '原价（划线价）'),
            },
            'required': ['account_id', 'title', 'price'],
        },
    },
    'items_batch_edit': {
        'description': '批量改价/改标题。patch 支持 price_delta / price_set / title_tpl 等。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'item_ids': _prop('array', '要改的商品 id 列表', items={'type': 'string'}),
                'patch': _prop('object', '改动规则，如 {"price_delta": -5} 或 {"price_set": 99}'),
            },
            'required': ['account_id', 'item_ids', 'patch'],
        },
    },
    'items_sync': {
        'description': '从闲鱼拉取该账号的在售商品并入库（会走风控限速）。',
        'input_schema': {
            'type': 'object',
            'properties': {'account_id': _ACCOUNT_ID},
            'required': ['account_id'],
        },
    },
    'chats_list': {
        'description': '列出会话（买家对话），可按账号/接管模式过滤。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'mode': _prop('string', '接管模式：auto / manual / off'),
                'limit': _prop('integer', '返回条数上限'),
            },
        },
    },
    'chats_read': {
        'description': '读某个会话的历史消息。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'conversation_id': _CONV_ID,
                'limit': _prop('integer', '返回条数上限'),
                'before_id': _prop('integer', '只取该消息 id 之前的更早消息'),
            },
            'required': ['conversation_id'],
        },
    },
    'chats_send': {
        'description': '以人工身份在某个会话里发消息给买家（会真实发送，谨慎调用）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'conversation_id': _CONV_ID,
                'content': _prop('string', '要发送的文本'),
            },
            'required': ['conversation_id', 'content'],
        },
    },
    'chats_set_mode': {
        'description': '切换会话的 AI 接管模式：auto（AI 自动回）/ manual（人工接管）/ off（不处理）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'conversation_id': _CONV_ID,
                'mode': _prop('string', 'auto | manual | off', enum=['auto', 'manual', 'off']),
            },
            'required': ['conversation_id', 'mode'],
        },
    },
    'orders_list': {
        'description': '查询订单，可按账号/状态过滤。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'account_id': _ACCOUNT_ID,
                'status': _prop('string', '订单状态'),
                'limit': _prop('integer', '返回条数上限'),
            },
        },
    },
    'orders_deliver': {
        'description': '给订单发货（可带快递公司与运单号）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'order_id': _prop('string', '订单号'),
                'company': _prop('string', '快递公司'),
                'tracking_no': _prop('string', '运单号'),
            },
            'required': ['order_id'],
        },
    },
    'risk_status': {
        'description': '查风控状态：连续命中次数、熔断到期时间、是否需要人工验证。',
        'input_schema': {
            'type': 'object',
            'properties': {'account_id': _ACCOUNT_ID},
        },
    },
    'risk_clear': {
        'description': '清除某账号的风控熔断（只有人工验证成功后才该调用）。',
        'input_schema': {
            'type': 'object',
            'properties': {'account_id': _ACCOUNT_ID},
            'required': ['account_id'],
        },
    },
    'providers_list': {
        'description': '列出已配置的 AI 供应商（OpenAI 兼容端点）。',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    'providers_upsert': {
        'description': '新增或更新 AI 供应商配置（带 id 为更新）。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'id': _prop('integer', '供应商 id；带此字段表示更新'),
                'name': _prop('string', '显示名'),
                'base_url': _prop('string', 'OpenAI 兼容 base_url'),
                'api_key': _prop('string', 'API Key'),
                'default_model': _prop('string', '默认模型名'),
                'enabled': _prop('boolean', '是否启用'),
                'is_default': _prop('boolean', '是否设为默认供应商'),
            },
        },
    },
    'master_chat': {
        'description': '把你的话转给本软件的「主管 AI」，它会理解意图并调用工具干活。想指挥这个工作台就用它。',
        'input_schema': {
            'type': 'object',
            'properties': {
                'message': _prop('string', '你要对主管 AI 说的话'),
                'conversation_id': _prop('integer', '可选：关联到某个买家会话的上下文'),
            },
            'required': ['message'],
        },
    },
    'agents_messages': {
        'description': '查看 AI 之间的沟通记录（跨商品协查），用于排查 AI 编排。',
        'input_schema': {
            'type': 'object',
            'properties': {'conversation_id': _CONV_ID},
        },
    },
}


def tool_specs() -> List[Dict[str, Any]]:
    """返回 tools/list 要用的规范工具定义。

    唯一来源是 `backend/app/agents/tools.py::TOOL_SPECS`（懒导入）；
    该文件不存在 / 导入失败 / 结构不认时，退回本模块的 `CANONICAL_SPECS`。
    两边的名字集合始终以 `TOOL_ROUTES` 为准（没有路由的工具不会暴露，
    否则模型会看到一个必然报错的工具）。
    """
    external, source = _load_external_specs()
    specs: List[Dict[str, Any]] = []
    for name in TOOL_ROUTES:
        base = CANONICAL_SPECS.get(name, {'description': name, 'input_schema': {'type': 'object'}})
        spec = {
            'name': name,
            'description': base.get('description', name),
            'inputSchema': base.get('input_schema') or {'type': 'object', 'properties': {}},
        }
        if name in external:
            merged = dict(spec)
            if external[name].get('description'):
                merged['description'] = external[name]['description']
            if external[name].get('inputSchema'):
                merged['inputSchema'] = _merge_schema(spec['inputSchema'], external[name]['inputSchema'])
            spec = merged
        specs.append(spec)
    return specs


def _merge_schema(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """以外部 schema 为准，但**补上内置 schema 里多出来的属性**。

    外部的 `TOOL_SPECS` 比路由窄：如果整块覆盖，会出现「路由认识、`tools/list` 里
    看不到」的参数（模型看得到才敢传），并且 external 常带
    `additionalProperties: false`，等于把这些参数从契约里抹掉。所以做并集，
    内置的 `properties` 只补空缺、不覆盖外部的定义。
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override if isinstance(override, dict) else base
    merged = dict(override)
    base_props = base.get('properties')
    ext_props = override.get('properties')
    if isinstance(base_props, dict):
        if not isinstance(ext_props, dict):
            merged['properties'] = dict(base_props)
        else:
            missing = {k: v for k, v in base_props.items() if k not in ext_props}
            if missing:
                merged['properties'] = {**missing, **ext_props}
    if not merged.get('type'):
        merged['type'] = base.get('type') or 'object'
    return merged


def tool_specs_source() -> str:
    """给日志/README 用：当前工具定义来自哪里。"""
    return _load_external_specs()[1]


def _load_external_specs() -> Tuple[Dict[str, Dict[str, Any]], str]:
    """懒导入 agents 的 TOOL_SPECS，规范化成 {name: {description, inputSchema}}。"""
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from backend.app.agents.tools import TOOL_SPECS  # type: ignore
    except Exception as exc:  # ImportError / 语法错 / 依赖缺失 —— 都退回内置清单
        return {}, f'builtin (TOOL_SPECS 不可用: {type(exc).__name__}: {exc})'
    try:
        return _normalize_specs(TOOL_SPECS), 'backend.app.agents.tools.TOOL_SPECS'
    except Exception as exc:
        return {}, f'builtin (TOOL_SPECS 结构不认: {type(exc).__name__}: {exc})'


def _normalize_specs(raw: Any) -> Dict[str, Dict[str, Any]]:
    """兼容几种可能形态：list[dict] / dict[name]->spec / dict[name]->{parameters..}。"""
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, dict):
        iterable: Iterable[Tuple[Optional[str], Any]] = raw.items()
    elif isinstance(raw, (list, tuple)):
        iterable = [(None, entry) for entry in raw]
    else:
        raise TypeError(f'TOOL_SPECS 类型不支持: {type(raw).__name__}')

    for key, entry in iterable:
        if not isinstance(entry, dict):
            continue
        # agents 工作流确认的形态：{"type":"function","function":{"name","description","parameters"}}
        inner = entry.get('function') if isinstance(entry.get('function'), dict) else {}
        name = entry.get('name') or inner.get('name') or key
        if not isinstance(name, str) or not name:
            continue
        schema = (
            entry.get('inputSchema')
            or entry.get('input_schema')
            or entry.get('parameters')
            or entry.get('args_schema')
            or inner.get('parameters')
        )
        if isinstance(schema, type):       # pydantic 模型类
            schema = _pydantic_schema(schema)
        if not isinstance(schema, dict):
            schema = None
        desc = entry.get('description') or entry.get('desc') or inner.get('description')
        if not isinstance(desc, str):
            desc = None
        out[str(name)] = {'description': desc, 'inputSchema': schema}
    if not out:
        raise ValueError('TOOL_SPECS 里没解析出任何工具')
    return out


def _pydantic_schema(model: Any) -> Optional[Dict[str, Any]]:
    for attr in ('model_json_schema', 'schema'):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                return None
    return None
