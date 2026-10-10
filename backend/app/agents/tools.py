"""工具定义与执行 —— 主管 AI 的 function-calling 工具集。

**这份文件是唯一工具真相**（`docs/CONTRACTS.md` §5）：`mcp_server/server.py` 直接
`from backend.app.agents.tools import TOOL_SPECS`，两边名字与 schema 必须一致。

- `TOOL_SPECS`：**全集**（19 个业务工具 + `master_chat`），MCP 侧暴露这套。
- `MASTER_TOOLS`：给主管 AI 自己用的子集 = 全集去掉 `master_chat`
  （`master_chat` 是「把卖家的话转给主管 AI」，在主管 AI 的 tool 集里会递归）。

schema 格式照抄 OpenAI function calling：
    {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
注意参数键叫 **`parameters`**（MCP 侧暴露 `tools/list` 时自己映射成 `inputSchema`）。

执行入口：`async call_tool(name, args) -> dict`。
读类工具直接查库（不依赖其它工作流），写类工具**懒导入** `backend.app.services.*`，
模块还没写完时返回 `{'ok': False, 'error': ...}` 而不是抛异常 —— 并行开发期间不许互相卡死。
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Dict, List, Optional

from ..core import crypto, db, events

# 提供者配置变化的 WS 话题（前端「AI 设置」页收到就静默重拉）。
# 与 `api/providers.py` 用同一个 topic，两条写入路径（HTTP / MCP 工具）都要发，
# 否则从 DSH 插件里改完 Key，网页上的列表一直是旧的。
PROVIDERS_CHANGED = 'providers.changed'


def publish_providers_changed(action: str, provider_id: Any, is_default: bool = False) -> None:
    """发一条 providers.changed。**payload 里绝不放 api_key 明文**，推送失败不影响主流程。"""
    try:
        events.publish(PROVIDERS_CHANGED, {
            'provider_id': int(provider_id) if provider_id not in (None, '') else None,
            'is_default': bool(is_default),
            'action': action,
        })
    except Exception:  # pragma: no cover - 推送失败绝不能影响写入
        pass

TOOL_SPECS: List[Dict[str, Any]] = [
    # ---------------- 账号 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'accounts_list',
            'description': '列出所有闲鱼账号及登录状态。想知道「有哪些号、哪个掉线了」时用。',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'accounts_set_enabled',
            'description': '启用或停用某个闲鱼账号（停用后不再自动回复）。用户说「把某个号关掉/打开」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'enabled': {'type': 'boolean', 'description': 'true=启用，false=停用'},
                },
                'required': ['account_id', 'enabled'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'accounts_relogin',
            'description': '触发某个账号重新扫码登录。账号掉线、Cookie 失效时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '闲鱼账号 id'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 商品 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'items_list',
            'description': '查询在售商品列表，可按账号、状态过滤，或按关键词搜标题。用户问「我有哪些商品/某商品在不在」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '只看这个账号的商品，可省略'},
                    'status': {'type': 'string', 'description': '商品状态，例如 on_sale / sold / delisted，可省略'},
                    'q': {'type': 'string', 'description': '标题关键词，可省略'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_get',
            'description': '查单个商品的完整信息（价格、库存、描述、图片）。要改价或回答买家细节前先用它。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '商品所属账号 id'},
                    'item_id': {'type': 'string', 'description': '商品 id'},
                },
                'required': ['account_id', 'item_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_edit',
            'description': '修改单个商品的价格/标题/描述/库存/图片。用户明确要求改某个商品时用；只传要改的字段。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '商品所属账号 id'},
                    'item_id': {'type': 'string', 'description': '商品 id'},
                    'price': {'type': 'number', 'description': '新价格（元），可省略'},
                    'title': {'type': 'string', 'description': '新标题，可省略'},
                    'description': {'type': 'string', 'description': '新描述，可省略'},
                    'stock': {'type': 'integer', 'description': '新库存，可省略'},
                    'images': {'type': 'array', 'items': {'type': 'string'}, 'description': '图片 URL 列表，可省略'},
                },
                'required': ['account_id', 'item_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_publish',
            'description': '发布一个新商品到闲鱼。用户给了标题和价格让你上架时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '发布到哪个账号'},
                    'title': {'type': 'string', 'description': '商品标题'},
                    'price': {'type': 'number', 'description': '售价（元）'},
                    'description': {'type': 'string', 'description': '商品描述，可省略'},
                    'images': {'type': 'array', 'items': {'type': 'string'}, 'description': '图片 URL 列表，可省略'},
                    'category_id': {'type': 'string', 'description': '类目 id，可省略'},
                },
                'required': ['account_id', 'title', 'price'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_batch_edit',
            'description': '批量改价/改标题。用户说「把某几个商品都降 10 块」这类批量操作时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '商品所属账号 id'},
                    'item_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '要改的商品 id 列表'},
                    'patch': {
                        'type': 'object',
                        'description': '改动内容，例如 {"price_delta": -10} 或 {"price_set": 99} 或 {"title_tpl": "..."}',
                    },
                },
                'required': ['account_id', 'item_ids', 'patch'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_sync',
            'description': '从闲鱼拉取某个账号的在售商品并入库。用户说「同步一下商品」时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '要同步的账号 id'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 聊天 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'chats_list',
            'description': '列出买家会话（谁在问、哪个商品、什么模式）。用户问「有谁在问/有什么新消息」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '只看这个账号的会话，可省略'},
                    'mode': {'type': 'string', 'description': 'auto / manual / off，可省略'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'chats_read',
            'description': '读某个会话的聊天记录。要了解上下文、或用户问「他刚才说了什么」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'conversation_id': {'type': 'integer', 'description': '会话 id'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'required': ['conversation_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'chats_send',
            'description': '以卖家身份给买家发一条消息。用户说「回复他/告诉他…」时用；发送前先想清楚措辞。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'conversation_id': {'type': 'integer', 'description': '会话 id'},
                    'content': {'type': 'string', 'description': '要发送的文本'},
                },
                'required': ['conversation_id', 'content'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'chats_set_mode',
            'description': '切换某个会话的回复模式：auto=AI 自动回，manual=只给建议要人工确认，off=不回复。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'conversation_id': {'type': 'integer', 'description': '会话 id'},
                    'mode': {'type': 'string', 'enum': ['auto', 'manual', 'off'], 'description': '目标模式'},
                },
                'required': ['conversation_id', 'mode'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 订单 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'orders_list',
            'description': '查订单列表（可按账号、状态过滤）。用户问「今天卖了几单/某单什么状态」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '只看这个账号的订单，可省略'},
                    'status': {'type': 'string', 'description': '订单状态，可省略'},
                    'limit': {'type': 'integer', 'description': '最多返回几条，默认 20'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'orders_deliver',
            'description': '给某个订单发货（需要快递公司 + 运单号）。用户明确要求发货时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'order_id': {'type': 'string', 'description': '订单号'},
                    'company': {'type': 'string', 'description': '快递公司，例如 顺丰'},
                    'tracking_no': {'type': 'string', 'description': '运单号'},
                },
                'required': ['order_id', 'company', 'tracking_no'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 风控 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'risk_status',
            'description': '查账号风控状态（是否被限流/需要人工验证）。用户问「为什么没自动回复了」时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '账号 id，可省略则返回全部'}},
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'risk_clear',
            'description': '清除某个账号的风控熔断（**只有人工验证成功后**才能用）。用户说「我已经验证过了」时用。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '账号 id'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 提供者 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'providers_list',
            'description': '列出已配置的 AI 提供者（名字、模型、是否默认）。**不返回 api_key**。',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'providers_upsert',
            'description': '新增或更新一个 AI 提供者（API Key 统一管理）。用户给了新的 base_url/key 时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer', 'description': '要更新的提供者 id；省略则新增'},
                    'name': {'type': 'string', 'description': '显示名，例如 智谱GLM'},
                    'base_url': {'type': 'string', 'description': 'OpenAI 兼容地址，例如 https://open.bigmodel.cn/api/paas/v4'},
                    'api_key': {'type': 'string', 'description': 'API Key'},
                    'default_model': {'type': 'string', 'description': '默认模型名，例如 glm-4-flash'},
                    'enabled': {'type': 'boolean', 'description': '是否启用，默认 true'},
                    'is_default': {'type': 'boolean', 'description': '是否设为默认提供者'},
                },
                'required': ['name', 'base_url'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 交易（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'trade_orders',
            'description': '查该账号闲鱼的订单列表（含待发货/已发货等页签计数）。用户问「我有哪些订单」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」，不会自己挑账号。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 10'},
                    'include_counts': {'type': 'boolean', 'description': 'true 时额外取一次页签计数（多一次平台请求，默认 false）'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_refunds',
            'description': '查该账号的售后/退款申请列表。用户问「我有几个退款单」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 10'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_evaluations',
            'description': '查该账号收到的评价列表。用户问「买家给了我什么评价」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 10'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_complaints',
            'description': '查该账号的举报/投诉工单列表。用户问「有没有人投诉我」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 10'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_addresses',
            'description': '查该账号的收货地址列表。用户问「我有哪些收货地址」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 小铺（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'shop_sub_accounts',
            'description': '查该账号下小铺的子账号（员工号）列表及其状态。用户问「有几个子账号」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'shop_cs_routing',
            'description': '查小铺的客服分流规则（哪个子账号接哪类咨询）。用户问「客服怎么分单」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 数据中心（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'datacenter_overview',
            'description': '查该账号的经营数据总览（曝光、成交等指标）。用户问「我这号数据怎么样」时用。'
                           '平台没给的指标返回 null，不要当成 0。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD，缺省取平台默认区间'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD，缺省取平台默认区间'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'datacenter_fans',
            'description': '查该账号的粉丝数据。用户问「我有多少粉丝、涨没涨」时用。'
                           '平台没给的指标返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'datacenter_customer_service',
            'description': '查该账号的客服数据（响应时长、满意度等）。用户问「客服表现怎么样」时用。'
                           '平台没给的指标返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 通知（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'notifications_list',
            'description': '查该账号的站内通知消息。用户问「平台给我发了什么消息」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~100，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 运费模版（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'item_templates_list',
            'description': '查该账号的运费模版列表。用户问「我有哪些运费模版」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数 1~50，缺省 20'},
                    'template_name': {'type': 'string', 'description': '按模版名称模糊筛选'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'item_templates_get',
            'description': '查单个运费模版的详细内容。用户想看某个运费模版的规则时用，先用 item_templates_list 拿到 id。'
                           'account_id 与 template_id 必填，不传直接返回「缺少参数: ...」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'template_id': {'type': 'string', 'description': '运费模版 id（先用 item_templates_list 查）'},
                },
                'required': ['account_id', 'template_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 财务（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'finance_income',
            'description': '查该账号某账期的收入流水（进账）。用户问「我挣了多少、哪笔到账」时用。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'cycle_from': {'type': 'string', 'description': '起始账期 YYYYMM，缺省按平台默认（上个月）'},
                    'cycle_to': {'type': 'string', 'description': '结束账期 YYYYMM'},
                    'biz_type': {'type': 'string', 'description': '业务类型编码，缺省由平台默认'},
                    'page': {'type': 'integer', 'description': '页码，缺省 1'},
                    'size': {'type': 'integer', 'description': '每页条数，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'finance_expense',
            'description': '查该账号某账期的支出流水（货款/佣金/推广费）。用户问「我花了多少」时用。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'cycle_from': {'type': 'string', 'description': '起始账期 YYYYMM'},
                    'cycle_to': {'type': 'string', 'description': '结束账期 YYYYMM'},
                    'biz_type': {'type': 'string', 'description': '业务类型编码'},
                    'page': {'type': 'integer', 'description': '页码，缺省 1'},
                    'size': {'type': 'integer', 'description': '每页条数，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'finance_invoices',
            'description': '查该账号的开票信息/发票列表。用户问「怎么开发票、开过几」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'integer', 'description': '页码，缺省 1'},
                    'size': {'type': 'integer', 'description': '每页条数，缺省 20'},
                    'biz_types': {'type': 'string', 'description': '按业务类型筛选，缺省全部'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'finance_purchaser',
            'description': '查该账号的付款方/收货方信息。用户问「买家付款人是谁」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'}},
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 推广（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'promotion_overview',
            'description': '查该账号的推广（超级擦亮/曝光）效果总览。用户问「推广效果怎么样」时用。'
                           '平台没给的指标返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'promotion_campaigns',
            'description': '查该账号的推广计划列表。用户问「我投了哪些推广」时用。'
                           'account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD'},
                    'page': {'type': 'string', 'description': '页码，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'promotion_reports',
            'description': '查该账号的推广数据报表。用户想看推广明细数据时用。'
                           '平台没给的指标返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD'},
                    'page': {'type': 'string', 'description': '页码，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'promotion_account',
            'description': '查该账号的推广账户信息（余额/开通状态）。用户问「推广还有多少余额」时用。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'start': {'type': 'string', 'description': '开始日期 YYYYMMDD'},
                    'end': {'type': 'string', 'description': '结束日期 YYYYMMDD'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 安全中心（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'security_center_status',
            'description': '查该账号安全中心的状态（违规、认证、风控等）。用户问「我这号安全吗、有违规吗」时用。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填，不传直接返回「缺少参数: account_id」。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'integer', 'description': '分页页码，缺省 1'},
                    'size': {'type': 'integer', 'description': '每页条数，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 营销（只读） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'marketing_items',
            'description': '查该账号的营销商品列表（平台建议推广哪些商品、商品营销状态）。用户问「哪些商品适合推广」时用。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数，1~50，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'marketing_tasks',
            'description': '查该账号的营销任务 / 营销信息（平台派发的推广任务、可领的营销权益）。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'marketing_price_competitiveness',
            'description': '查商品的价格竞争力（平台对该商品价格的评级与建议）。用户问「我价格是不是高了」时用。'
                           'item_ids 不传则返回平台给的全部；平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'item_ids': {'type': 'string', 'description': '逗号分隔的商品 id，可空；空则不按商品过滤'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'marketing_records',
            'description': '查该账号的营销操作记录（历史推广动作流水）。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数，1~50，缺省 20'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 主管 AI（只给 MCP 侧） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'master_chat',
            'description': '把卖家（你）的一句话转给这个软件的主管 AI，由它理解意图并调用工具干活。这是 DSH 里指挥本软件的总入口。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string', 'description': '要对主管 AI 说的话'},
                    'conversation_id': {'type': 'integer', 'description': '可选，绑定的买家会话 id'},
                },
                'required': ['message'],
                'additionalProperties': False,
            },
        },
    },
]

# 主管 AI 自己的 tool 集：去掉 master_chat（否则会递归调自己）
MASTER_TOOLS: List[Dict[str, Any]] = [
    spec for spec in TOOL_SPECS if spec['function']['name'] != 'master_chat'
]

TOOL_NAMES: List[str] = [spec['function']['name'] for spec in TOOL_SPECS]

# 一次主管 AI 回合里最多执行多少次工具调用（防止模型死循环）
MAX_TOOL_ROUNDS = 4


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    for spec in TOOL_SPECS:
        if spec['function']['name'] == name:
            return spec
    return None


def _require(args: Dict[str, Any], *names: str) -> Optional[str]:
    missing = [n for n in names if args.get(n) in (None, '')]
    return f'缺少参数: {", ".join(missing)}' if missing else None


def _rows(sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    return [dict(r) for r in db.query(sql, params)]


# --------------------------------------------------------------------------
# 读类工具：直接查库，不依赖其它工作流
# --------------------------------------------------------------------------

def _accounts_list(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = db.query(
        'SELECT a.id, a.nickname, a.remark, a.enabled, a.auto_reply, a.updated_at, '
        'c.login_state, c.relogin_reason, c.last_login_at '
        'FROM accounts a LEFT JOIN account_credentials c ON c.account_id = a.id '
        'ORDER BY a.id'
    )
    return {'ok': True, 'accounts': [dict(r) for r in rows]}


def _items_list(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = 'SELECT account_id, item_id, title, price, stock, status, ai_enabled, updated_at FROM items WHERE 1=1'
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND account_id = ?'
        params.append(str(args['account_id']))
    if args.get('status'):
        sql += ' AND status = ?'
        params.append(str(args['status']))
    if args.get('q'):
        sql += ' AND title LIKE ?'
        params.append(f"%{args['q']}%")
    sql += ' ORDER BY updated_at DESC LIMIT ?'
    params.append(int(args.get('limit') or 20))
    return {'ok': True, 'items': _rows(sql, params)}


def _items_get(args: Dict[str, Any]) -> Dict[str, Any]:
    row = db.query_one(
        'SELECT * FROM items WHERE account_id = ? AND item_id = ?',
        (str(args['account_id']), str(args['item_id'])),
    )
    if row is None:
        return {'ok': False, 'error': f'没找到商品 {args["item_id"]}'}
    item = dict(row)
    try:
        item['images'] = json.loads(item.get('images') or '[]')
    except (ValueError, TypeError):
        item['images'] = []
    return {'ok': True, 'item': item}


def _chats_list(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = ('SELECT c.id, c.account_id, c.peer_nickname, c.item_id, c.mode, c.agent_role, '
           'c.unread, c.last_message_at FROM conversations c WHERE 1=1')
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND c.account_id = ?'
        params.append(str(args['account_id']))
    if args.get('mode'):
        sql += ' AND c.mode = ?'
        params.append(str(args['mode']))
    sql += " ORDER BY COALESCE(c.last_message_at, '') DESC, c.id DESC LIMIT ?"
    params.append(int(args.get('limit') or 20))
    return {'ok': True, 'conversations': _rows(sql, params)}


def _chats_read(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get('limit') or 20)
    rows = db.query(
        'SELECT id, direction, sender, content, msg_type, item_id, created_at FROM messages '
        'WHERE conversation_id = ? ORDER BY id DESC LIMIT ?',
        (int(args['conversation_id']), limit),
    )
    return {'ok': True, 'messages': [dict(r) for r in reversed(rows)]}


def _orders_list(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = 'SELECT order_id, account_id, item_id, buyer_id, amount, status, created_at FROM orders WHERE 1=1'
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND account_id = ?'
        params.append(str(args['account_id']))
    if args.get('status'):
        sql += ' AND status = ?'
        params.append(str(args['status']))
    sql += ' ORDER BY created_at DESC LIMIT ?'
    params.append(int(args.get('limit') or 20))
    return {'ok': True, 'orders': _rows(sql, params)}


def _risk_status(args: Dict[str, Any]) -> Dict[str, Any]:
    sql = ('SELECT account_id, consecutive_hits, blocked_until, last_hit_at, verification_type, updated_at '
           'FROM risk_state WHERE 1=1')
    params: List[Any] = []
    if args.get('account_id'):
        sql += ' AND account_id = ?'
        params.append(str(args['account_id']))
    rows = _rows(sql, params)
    import time as _time

    now = _time.time()
    for row in rows:
        row['blocked'] = float(row.get('blocked_until') or 0) > now
    return {'ok': True, 'risk': rows}


def _providers_list(args: Dict[str, Any]) -> Dict[str, Any]:
    rows = db.query(
        'SELECT id, name, base_url, default_model, enabled, is_default, updated_at FROM providers ORDER BY id'
    )
    return {'ok': True, 'providers': [dict(r) for r in rows]}


def _providers_upsert(args: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = args.get('id')
    fields = {
        'name': str(args.get('name') or '').strip(),
        'base_url': str(args.get('base_url') or '').strip().rstrip('/'),
        'default_model': str(args.get('default_model') or '').strip(),
        'enabled': 1 if args.get('enabled', True) else 0,
        'is_default': 1 if args.get('is_default') else 0,
    }
    if provider_id:
        current = db.query_one('SELECT * FROM providers WHERE id = ?', (int(provider_id),))
        if current is None:
            return {'ok': False, 'error': f'提供者 #{provider_id} 不存在'}
        # 没给新 key 就沿用原来的值；`crypto.encrypt()` 对**已是密文**的值原样返回，
        # 所以这里不会把老密文再加密一层。
        api_key = crypto.encrypt(args.get('api_key') or current['api_key'] or '')
        if not fields['name']:
            fields['name'] = current['name']
        if not fields['base_url']:
            fields['base_url'] = current['base_url']
        db.execute(
            'UPDATE providers SET name=?, base_url=?, api_key=?, default_model=?, enabled=?, is_default=?, '
            'updated_at=CURRENT_TIMESTAMP WHERE id=?',
            (fields['name'], fields['base_url'], api_key, fields['default_model'],
             fields['enabled'], fields['is_default'], int(provider_id)),
        )
    else:
        api_key = crypto.encrypt(args.get('api_key') or '')
        if not fields['name'] or not fields['base_url']:
            return {'ok': False, 'error': '新增提供者必须给 name 和 base_url'}
        provider_id = int(db.execute(
            'INSERT INTO providers(name, base_url, api_key, default_model, enabled, is_default) '
            'VALUES(?,?,?,?,?,?)',
            (fields['name'], fields['base_url'], api_key, fields['default_model'],
             fields['enabled'], fields['is_default']),
        ))
    if fields['is_default']:
        db.execute('UPDATE providers SET is_default = 0 WHERE id != ?', (int(provider_id),))
    publish_providers_changed('update' if args.get('id') else 'create', provider_id,
                              bool(fields['is_default']))
    return {'ok': True, 'provider_id': int(provider_id), 'updated': True}


# --------------------------------------------------------------------------
# 写类工具：懒导入 services（并行开发期间可能还没写完）
# --------------------------------------------------------------------------

def _services():
    """懒导入业务层。返回 (module, error)。"""
    import importlib

    try:
        return importlib.import_module('backend.app.services'), None
    except ImportError as exc:
        return None, str(exc)


# 工具名 -> (services 子模块, 子模块里的函数名)。
#
# 为什么要有这张表：`backend/app/services/__init__.py` 只 `__all__` 了子模块名，
# **没有把函数重导出到包顶层**，所以直接 `getattr(backend.app.services, 'xxx')`
# 拿不到东西 —— 早先的写类工具就因此永远回「业务模块还没实现 xxx()」。
# 这里显式写清每个工具打到哪个函数，也是「工具名 ↔ 真实实现」的对账依据。
_SERVICE_TARGETS: Dict[str, List[str]] = {
    # ---- 交易 ----
    'trade_orders': ['trade', 'orders'],
    'trade_refunds': ['trade', 'refunds'],
    'trade_evaluations': ['trade', 'evaluations'],
    'trade_complaints': ['trade', 'complaints'],
    'trade_addresses': ['trade', 'addresses'],
    # ---- 小铺 ----
    'shop_sub_accounts': ['shop', 'sub_accounts'],
    'shop_cs_routing': ['shop', 'cs_routing'],
    # ---- 数据中心 ----
    'datacenter_overview': ['datacenter', 'overview'],
    'datacenter_fans': ['datacenter', 'fans'],
    'datacenter_customer_service': ['datacenter', 'customer_service'],
    # ---- 通知 ----
    'notifications_list': ['notify', 'notifications'],
    # ---- 运费模版 ----
    'item_templates_list': ['item_templates', 'list_templates'],
    'item_templates_get': ['item_templates', 'template_detail'],
    # ---- 财务 ----
    'finance_income': ['finance', 'account_bills'],
    'finance_expense': ['finance', 'account_bills'],
    'finance_invoices': ['finance', 'account_invoices'],
    'finance_purchaser': ['finance', 'account_purchaser'],
    # ---- 推广 ----
    'promotion_overview': ['promotion', 'overview'],
    'promotion_campaigns': ['promotion', 'campaigns'],
    'promotion_reports': ['promotion', 'reports'],
    'promotion_account': ['promotion', 'account'],
    # ---- 安全中心 ----
    'security_center_status': ['security_center', 'read'],
    # ---- 营销 ----
    'marketing_items': ['marketing', 'items'],
    'marketing_tasks': ['marketing', 'tasks'],
    'marketing_price_competitiveness': ['marketing', 'price_competitiveness'],
    'marketing_records': ['marketing', 'records'],
}

# 有额外位置参数（不是直接透传 args 的）在这里补上固定值
_SERVICE_FIXED_ARGS: Dict[str, Dict[str, Any]] = {
    'finance_income': {'kind': 'income'},
    'finance_expense': {'kind': 'expense'},
}


def _resolve_service(fn_name: str):
    """按工具名查到真正的 service 函数。返回 (callable, error)。"""
    import importlib

    target_info = _SERVICE_TARGETS.get(fn_name)
    if target_info is None:
        # 表外的老路径：尝试包顶层属性（如果将来有人在 __init__ 里重导出）
        services, error = _services()
        if services is None:
            return None, f'业务模块还没就绪（backend.app.services 不可用: {error}）'
        target = getattr(services, fn_name, None)
        if target is None:
            return None, f'业务模块还没实现 {fn_name}()'
        return target, None
    module_name, attr = target_info
    try:
        module = importlib.import_module(f'backend.app.services.{module_name}')
    except Exception as exc:  # 模块缺失/导入报错都要变成可读错误
        return None, f'业务模块 backend.app.services.{module_name} 不可用: {type(exc).__name__}: {exc}'
    target = getattr(module, attr, None)
    if target is None:
        return None, f'backend.app.services.{module_name} 没有 {attr}()'
    return target, None


def _filter_args(target: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """只传 target 真正收的参数，避免多余键打成 TypeError（对齐 HTTP 的 Query 白名单）。"""
    if not inspect.isfunction(target) and not inspect.ismethod(target):
        return args
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return args
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(args)
    return {k: v for k, v in args.items() if k in params}


async def _dispatch_service(fn_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    target, error = _resolve_service(fn_name)
    if target is None:
        return {'ok': False, 'error': error or f'业务模块还没实现 {fn_name}()'}
    call_args = dict(_SERVICE_FIXED_ARGS.get(fn_name, {}))
    call_args.update(_filter_args(target, args))
    result = target(**call_args)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict) and 'ok' in result:
        return result
    # services 的失败信封是 {"success": false, "code": ..., "message": ...} —— 必须原样透出，
    # 否则失败会被包成 ok=True，模型会以为成功了。
    if isinstance(result, dict) and result.get('success') is False:
        payload = dict(result)
        payload['ok'] = False
        return payload
    return {'ok': True, 'result': result}


async def _accounts_set_enabled(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = str(args['account_id'])
    enabled = 1 if args.get('enabled') else 0
    if db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,)) is None:
        return {'ok': False, 'error': f'账号 {account_id} 不存在'}
    db.execute('UPDATE accounts SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
               (enabled, account_id))
    events.publish('accounts.changed', {'account_id': account_id, 'enabled': bool(enabled)})
    return {'ok': True, 'account_id': account_id, 'enabled': bool(enabled)}


async def _risk_clear(args: Dict[str, Any]) -> Dict[str, Any]:
    account_id = str(args['account_id'])
    db.execute(
        'UPDATE risk_state SET consecutive_hits = 0, blocked_until = 0, verification_type = \'\', '
        'updated_at = CURRENT_TIMESTAMP WHERE account_id = ?',
        (account_id,),
    )
    events.publish('risk.cleared', {'account_id': account_id})
    return {'ok': True, 'account_id': account_id}


async def _master_chat(args: Dict[str, Any]) -> Dict[str, Any]:
    from . import runtime  # 懒导入，避免 runtime <-> tools 循环

    result = await runtime.run_master(
        str(args['message']),
        context={'conversation_id': args.get('conversation_id')} if args.get('conversation_id') else None,
    )
    return {'ok': True, 'reply': result.get('reply', ''), 'tool_calls': result.get('tool_calls', [])}


def _marketing_price_competitiveness(args: Dict[str, Any]) -> Dict[str, Any]:
    """价格竞争力：AI 侧传的是逗号分隔字符串，服务层要的是**列表**，这里翻译一次。

    不能直接透传：`services/marketing.price_competitiveness` 收 `item_ids` 后会
    `[str(item) for item in item_ids]`，字符串会被逐字符拆开（`'12,34'` → `['1','2',...]`）。
    """
    args = dict(args)
    raw = args.get('item_ids')
    if raw is not None:
        args['item_ids'] = [p.strip() for p in str(raw).split(',') if p.strip()]
    return _dispatch_service('marketing_price_competitiveness', args)


# 工具名 -> 执行函数（参数已校验）
_HANDLERS = {
    'accounts_list': _accounts_list,
    'accounts_set_enabled': _accounts_set_enabled,
    'accounts_relogin': lambda a: _dispatch_service('accounts_relogin', a),
    'items_list': _items_list,
    'items_get': _items_get,
    'items_edit': lambda a: _dispatch_service('items_edit', a),
    'items_publish': lambda a: _dispatch_service('items_publish', a),
    'items_batch_edit': lambda a: _dispatch_service('items_batch_edit', a),
    'items_sync': lambda a: _dispatch_service('items_sync', a),
    'chats_list': _chats_list,
    'chats_read': _chats_read,
    'chats_send': lambda a: _dispatch_service('chats_send', a),
    'chats_set_mode': lambda a: _dispatch_service('chats_set_mode', a),
    'orders_list': _orders_list,
    'orders_deliver': lambda a: _dispatch_service('orders_deliver', a),
    'risk_status': _risk_status,
    'risk_clear': _risk_clear,
    'providers_list': _providers_list,
    'providers_upsert': _providers_upsert,
    'master_chat': _master_chat,
    # ---------------- 新接的只读能力（懒导入 services） ----------------
    'trade_orders': lambda a: _dispatch_service('trade_orders', a),
    'trade_refunds': lambda a: _dispatch_service('trade_refunds', a),
    'trade_evaluations': lambda a: _dispatch_service('trade_evaluations', a),
    'trade_complaints': lambda a: _dispatch_service('trade_complaints', a),
    'trade_addresses': lambda a: _dispatch_service('trade_addresses', a),
    'shop_sub_accounts': lambda a: _dispatch_service('shop_sub_accounts', a),
    'shop_cs_routing': lambda a: _dispatch_service('shop_cs_routing', a),
    'datacenter_overview': lambda a: _dispatch_service('datacenter_overview', a),
    'datacenter_fans': lambda a: _dispatch_service('datacenter_fans', a),
    'datacenter_customer_service': lambda a: _dispatch_service('datacenter_customer_service', a),
    'notifications_list': lambda a: _dispatch_service('notifications_list', a),
    'item_templates_list': lambda a: _dispatch_service('item_templates_list', a),
    'item_templates_get': lambda a: _dispatch_service('item_templates_get', a),
    'finance_income': lambda a: _dispatch_service('finance_income', a),
    'finance_expense': lambda a: _dispatch_service('finance_expense', a),
    'finance_invoices': lambda a: _dispatch_service('finance_invoices', a),
    'finance_purchaser': lambda a: _dispatch_service('finance_purchaser', a),
    'promotion_overview': lambda a: _dispatch_service('promotion_overview', a),
    'promotion_campaigns': lambda a: _dispatch_service('promotion_campaigns', a),
    'promotion_reports': lambda a: _dispatch_service('promotion_reports', a),
    'promotion_account': lambda a: _dispatch_service('promotion_account', a),
    'security_center_status': lambda a: _dispatch_service('security_center_status', a),
    'marketing_items': lambda a: _dispatch_service('marketing_items', a),
    'marketing_tasks': lambda a: _dispatch_service('marketing_tasks', a),
    'marketing_price_competitiveness': _marketing_price_competitiveness,
    'marketing_records': lambda a: _dispatch_service('marketing_records', a),
}


async def call_tool(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一个工具。永远返回 dict（`{'ok': True/False, ...}`），不抛业务异常。"""
    args = dict(args or {})
    spec = get_tool(name)
    if spec is None:
        return {'ok': False, 'error': f'未知工具: {name}'}
    required = (spec['function']['parameters'] or {}).get('required') or []
    missing = _require(args, *required)
    if missing:
        return {'ok': False, 'error': missing}
    handler = _HANDLERS.get(name)
    if handler is None:  # pragma: no cover - 防漏注册
        return {'ok': False, 'error': f'工具 {name} 没有实现'}
    try:
        result = handler(args)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as exc:  # 工具失败要回给模型，不能炸掉整个回合
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


__all__ = ['MASTER_TOOLS', 'MAX_TOOL_ROUNDS', 'TOOL_NAMES', 'TOOL_SPECS', 'call_tool', 'get_tool']
