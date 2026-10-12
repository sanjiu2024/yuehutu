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

#: 写类工具描述里必须出现的一句中文告警。抽成常量是为了 11 个写工具逐字一致，
#: 也让「所有写工具都警告过」这件事可以被测试直接断言（见 test_agents_tools_workbench.py）。
_WRITE_HINT = ('这是写操作，会真的改动闲鱼平台上的数据，必须显式传 confirm=true；不确定就不要调。')

TOOL_SPECS: List[Dict[str, Any]] = [
    # ---------------- 账号 ----------------
    {
        'type': 'function',
        'function': {
            'name': 'accounts_list',
            'description': '列出所有闲鱼账号及登录状态。想知道「有哪些号、哪个掉线了」时用。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'enabled': {'type': 'boolean', 'description': '只看启用(true)/停用(false)的账号；不传=全部'},
                    'q': {'type': 'string', 'description': '按昵称 / 备注 / 账号 id 模糊搜索；不传=不筛'},
                    'limit': {'type': 'integer', 'description': '返回条数上限；不传=不限制'},
                },
                'additionalProperties': False,
            },
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
    # ---------------- 运费模版写（会真的改平台数据；required 必含 confirm） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'item_templates_create',
            'description': '新建一个运费模版（真实写平台）。details 是运费明细数组，'
                           '元素形如 {"receive_division_name":"全国","first":"1","first_price":"8",'
                           '"excess":"1","excess_price":"3"}；空字符串字段不会提交。'
                           + _WRITE_HINT + 'account_id 与 template_name 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'template_name': {'type': 'string', 'description': '模版名称，必填，最多 30 个字'},
                    'item_prov': {'type': 'string', 'description': '发货省，例如 浙江省；可空'},
                    'item_city': {'type': 'string', 'description': '发货市，例如 杭州市；可空'},
                    'valuation': {'type': 'string', 'description': '计价方式，可空'},
                    'is_default': {'type': 'boolean', 'description': '是否设为默认模版'},
                    'details': {
                        'type': 'array',
                        'description': '运费明细数组；可空',
                        'items': {'type': 'object'},
                    },
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'template_name', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'item_templates_modify',
            'description': '修改一个运费模版（真实写平台）。官方修改时会带上 version，'
                           'item_templates_list / item_templates_get 里能查到就一起传。'
                           + _WRITE_HINT + 'account_id、template_id 与 template_name 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'template_id': {'type': 'string', 'description': '要改的模版 id'},
                    'template_name': {'type': 'string', 'description': '模版名称，必填，最多 30 个字'},
                    'item_prov': {'type': 'string', 'description': '发货省；可空'},
                    'item_city': {'type': 'string', 'description': '发货市；可空'},
                    'valuation': {'type': 'string', 'description': '计价方式；可空'},
                    'is_default': {'type': 'boolean', 'description': '是否设为默认模版'},
                    'details': {
                        'type': 'array',
                        'description': '运费明细数组；可空',
                        'items': {'type': 'object'},
                    },
                    'version': {'type': 'string', 'description': '平台返回的 version；查不到就留空'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'template_id', 'template_name', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'item_templates_delete',
            'description': '删除一个运费模版（真实写平台，**不可撤销**）。'
                           '正在用这个模版的商品可能受影响，删之前先确认。'
                           + _WRITE_HINT + 'account_id 与 template_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'template_id': {'type': 'string', 'description': '要删的模版 id'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'template_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'item_templates_unbind',
            'description': '给商品设置运费（真实写平台）。mode 三选一：free_ship（包邮）、'
                           'template（用模版，要 template_id）、fixed（固定运费，要 post_price，单位元）。'
                           'item_ids 是商品 id 数组或逗号串。'
                           + _WRITE_HINT + 'account_id、item_ids 与 mode 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'item_ids': {
                        'type': 'string',
                        'description': '商品 id，多个用逗号分隔，例如 "1085757639387,108581070822"',
                    },
                    'mode': {'type': 'string', 'enum': ['free_ship', 'template', 'fixed'],
                             'description': '运费方式'},
                    'template_id': {'type': 'string', 'description': 'mode=template 时必填'},
                    'post_price': {'type': 'number', 'description': 'mode=fixed 时的固定运费，单位元'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'item_ids', 'mode', 'confirm'],
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
            'description': '查**单个商品**的营销数据（曝光/访问/成交/预估花费等一屏指标）。用户问「这个商品最近卖得怎么样」时用，必须给 item_id。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'item_id': {'type': 'string', 'description': '商品 id（itmId）；这个接口是单商品口径，不传会返回带候选值的中文 400'},
                    'date_type': {'type': 'string', 'description': '统计区间：recent1d / recent7d / recent30d / customDate，缺省 recent1d'},
                    'date_range': {'type': 'string', 'description': '仅 customDate 用，格式 YYYYMMDD|YYYYMMDD（最多回溯 90 天）'},
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
                    'biz_scene': {'type': 'string', 'description': '业务场景：TRAFFIC_DRIVEN_REPRICING（智能定价）/ TRAFFIC_ORDER_BOOST（流量推广），缺省 TRAFFIC_ORDER_BOOST'},
                    'query_scene': {'type': 'string', 'description': '查询场景，缺省 ORDER_BOOST_SET；可选值见 GET /api/marketing/{account_id}/tasks 的 400 提示'},
                    'page': {'type': 'string', 'description': '页码，从 1 开始，缺省 1'},
                    'size': {'type': 'string', 'description': '每页条数，1~50，缺省 10'},
                    'item_ids': {'type': 'string', 'description': '逗号分隔的商品 id，可空；空则返回全部'},
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
            'description': '查**单个商品**的价格竞争力（平台对该商品价格的评级与建议）。用户问「我价格是不是高了」时用，必须给 item_id。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'biz_scene': {'type': 'string', 'description': '业务场景：TRAFFIC_DRIVEN_REPRICING / TRAFFIC_ORDER_BOOST，缺省 TRAFFIC_ORDER_BOOST'},
                    'item_id': {'type': 'string', 'description': '商品 id；必填，不传会返回带候选值的中文 400'},
                    'sku_prices': {'type': 'string', 'description': 'SKU 价格表，JSON 对象字符串，形如 {"<skuId>": <价格元>}；不传则不按 SKU 比价'},
                    'commission_rate': {'type': 'string', 'description': '佣金比例，仅 biz_scene=TRAFFIC_ORDER_BOOST 时有意义'},
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
            'description': '查某条推广任务的营销操作记录（历史推广动作流水）。必须给 task_id；没有分页参数。'
                           '平台没给的字段返回 null，不要当成 0。account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'task_id': {'type': 'string', 'description': '推广任务 id；必填，不传会返回带候选值的中文 400'},
                    'biz_scene': {'type': 'string', 'description': '业务场景：TRAFFIC_DRIVEN_REPRICING / TRAFFIC_ORDER_BOOST，缺省 TRAFFIC_ORDER_BOOST'},
                    'query_scene': {'type': 'string', 'description': '查询场景，缺省 ORDER_BOOST_OPERATION_RECORD'},
                },
                'required': ['account_id'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 交易写（会真的改平台数据；required 必含 confirm） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_ship_offline',
            'description': '订单发货（自己联系物流、有真实运单号）：填快递单号后买家能看到物流。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'trade_ids': {'type': 'array', 'items': {'type': 'string'},
                                  'description': '要发货的订单号列表，至少一个'},
                    'mail_no': {'type': 'string', 'description': '运单号'},
                    'cp_code': {'type': 'string', 'description': '物流公司编码（平台口径，如 SF / YTO）'},
                    'address_id': {'type': 'string', 'description': '发货/退货地址 id（trade.address.list.query 拿到的 address_id）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'trade_ids', 'mail_no', 'cp_code', 'address_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_ship_dummy',
            'description': '无需物流发货（虚拟发货，买家看不到运单号）。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'order_id': {'type': 'string', 'description': '订单号'},
                    'trade_text': {'type': 'string', 'description': '发货备注，可空'},
                    'pic_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': '发货凭证图片 URL 列表，可空'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'order_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_adjust_price',
            'description': '订单改价：改商品实付价与运费。**单位是元**（协议层换算成分）。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'order_id': {'type': 'string', 'description': '订单号'},
                    'modify_fee': {'type': 'number', 'description': '改后商品价格，单位元，必须大于 0'},
                    'new_transport_fee': {'type': 'number', 'description': '改后运费，单位元，可为 0'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'order_id', 'modify_fee', 'new_transport_fee', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_refund_agree',
            'description': '同意买家退款。auth_token 是平台下发的操作令牌，必须从退款详情里原样带过来，工具层不会替你猜。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'refund_id': {'type': 'string', 'description': '退款单号'},
                    'auth_token': {'type': 'string', 'description': '平台下发的操作令牌（退款详情/渲染接口里给的）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'refund_id', 'auth_token', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_refund_verify_token',
            'description': '取同意退款要用的操作令牌（auth_token）——**只读**，不会改动任何东西，所以不需要 confirm。'
                           '退款列表里没有这个字段，同意退款前先用它拿一次；返回里的 verify_url 是平台'
                           '要求用户完成支付宝身份验证的页面。平台没给令牌时 token 是 null，不要自己编。'
                           'account_id 与 refund_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'refund_id': {'type': 'string', 'description': '退款单号'},
                },
                'required': ['account_id', 'refund_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_refund_refuse',
            'description': '拒绝买家退款：必须给平台的拒绝原因 id 与举证材料。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'refund_id': {'type': 'string', 'description': '退款单号'},
                    'order_id': {'type': 'string', 'description': '订单号'},
                    'refuse_reason_id': {'type': 'string', 'description': '拒绝原因 id（来自退款拒绝渲染接口的 refuseReasonList）'},
                    'proof_desc': {'type': 'string', 'description': '举证说明，可空'},
                    'proof_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': '举证图片 URL 列表，可空'},
                    'negotiation': {'type': 'string', 'description': '协商说明，可空'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'refund_id', 'order_id', 'refuse_reason_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_rate_create',
            'description': '卖家给买家评价。rate 是评价等级，不需要等级时传 -1。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'trade_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': '要评价的订单号列表'},
                    'feedback': {'type': 'string', 'description': '评价内容'},
                    'rate': {'type': 'integer', 'description': '评价等级，缺省 -1（不指定等级）'},
                    'image_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': '评价图片 URL 列表，可空'},
                    'anonymous': {'type': 'boolean', 'description': '是否匿名评价，缺省 false'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'trade_ids', 'feedback', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_complain_refuse',
            'description': '投诉拒绝（举证反驳）：必须带举证图片。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'complain_id': {'type': 'string', 'description': '投诉单号'},
                    'proof_desc': {'type': 'string', 'description': '举证说明'},
                    'proof_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': '举证图片 URL 列表，至少一张'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'complain_id', 'proof_desc', 'proof_urls', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_write_complain_proof',
            'description': '投诉举证（主动/被动/提交三选一，用 kind 指定）。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'complain_id': {'type': 'string', 'description': '投诉单号'},
                    'proof_desc': {'type': 'string', 'description': '举证说明'},
                    'proof_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': '举证图片 URL 列表，至少一张'},
                    'kind': {'type': 'string', 'description': '举证类型，缺省 sellerSubmitProof；可选值见平台投诉举证接口'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'complain_id', 'proof_desc', 'proof_urls', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 退货地址写（会真的改平台上的退货地址；required 必含 confirm） ----------------
    # 顺序是硬约束：这 4 条必须夹在 trade_write_complain_proof 与 security_appeal_form 之间，
    # 三份硬编码清单（tools.TOOL_NAMES / MCP TOOL_ROUTES / 对账脚本）都按这个顺序对齐。
    {
        'type': 'function',
        'function': {
            'name': 'trade_address_add',
            'description': '新增退货地址（**写操作，会真的在闲鱼上新增一条退货地址**）。'
                           + _WRITE_HINT + 'account_id 与联系人信息必填。'
                           '我们没有区域码来源：area_id 拿不到就不要传，不要自己编一个。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'contact_name': {'type': 'string', 'description': '联系人姓名'},
                    'mobile_phone': {'type': 'string', 'description': '联系电话'},
                    'province_name': {'type': 'string', 'description': '省'},
                    'city_name': {'type': 'string', 'description': '市'},
                    'district_name': {'type': 'string', 'description': '区/县'},
                    'detail_address': {'type': 'string', 'description': '详细地址'},
                    'area_id': {'type': 'string',
                                'description': '区域码；我们没有区域码来源，拿不到就不传，别编'},
                    'default_addr': {'type': 'boolean',
                                     'description': '是否设为默认地址；不传就按平台默认'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'contact_name', 'mobile_phone', 'province_name',
                             'city_name', 'district_name', 'detail_address', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_address_update',
            'description': '修改退货地址（**写操作，会真的改动闲鱼上的退货地址**）。'
                           + _WRITE_HINT + 'account_id、contact_id 与联系人信息必填。'
                           '我们没有区域码来源：area_id 拿不到就不要传，不要自己编一个。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'contact_id': {'type': 'string', 'description': '退货地址 id（从 trade_addresses 拿）'},
                    'contact_name': {'type': 'string', 'description': '联系人姓名'},
                    'mobile_phone': {'type': 'string', 'description': '联系电话'},
                    'province_name': {'type': 'string', 'description': '省'},
                    'city_name': {'type': 'string', 'description': '市'},
                    'district_name': {'type': 'string', 'description': '区/县'},
                    'detail_address': {'type': 'string', 'description': '详细地址'},
                    'area_id': {'type': 'string',
                                'description': '区域码；我们没有区域码来源，拿不到就不传，别编'},
                    'default_addr': {'type': 'boolean',
                                     'description': '是否设为默认地址；不传就按平台默认'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'contact_id', 'contact_name', 'mobile_phone',
                             'province_name', 'city_name', 'district_name', 'detail_address',
                             'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_address_delete',
            'description': '删除退货地址（**写操作且不可撤销**，删掉之后平台上的这条地址就没了）。'
                           + _WRITE_HINT + 'account_id 与 contact_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'contact_id': {'type': 'string', 'description': '退货地址 id（从 trade_addresses 拿）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'contact_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_address_set_default',
            'description': '把某条退货地址设为默认（**写操作**，会顶掉当前的默认地址；'
                           '想改回来只能再调一次，别当成可撤销的开关）。'
                           + _WRITE_HINT + 'account_id 与 contact_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'contact_id': {'type': 'string', 'description': '退货地址 id（从 trade_addresses 拿）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'contact_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 安全中心申诉（提交会真的提交到平台；required 必含 confirm） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'security_appeal_form',
            'description': '获取安全中心的申诉表单内容（**只读**，不会提交任何东西，不需要 confirm）。'
                           '提交申诉前先用它拿到要填的表单字段。account_id 与 form_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'form_id': {'type': 'string', 'description': '表单 id（违规记录里平台给的）'},
                },
                'required': ['account_id', 'form_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'security_appeal_submit',
            'description': '提交安全中心申诉（**会真的向平台提交申诉材料**，不可撤销）。'
                           'parameter 就是要填的表单值对象，工具层不翻译字段名。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'parameter': {'type': 'object', 'description': '申诉表单值对象，原样序列化后提交给平台'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'parameter', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'security_appeal_submit_cro',
            'description': '提交申诉（复核 cro 流程，需要处罚单号与凭证项）。'
                           '参数会拼成 parameter = JSON.stringify({punishId, proofItems}) 后提交。'
                           + _WRITE_HINT + 'account_id 与 punish_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'punish_id': {'type': 'string', 'description': '处罚单号（违规记录里平台给的）'},
                    'proof_items': {'type': 'array', 'items': {'type': 'object'},
                                    'description': '凭证项列表，元素形状 {code,name,index,details:[{value,desc}]}'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'punish_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'security_appeal_urge',
            'description': '催办已提交的申诉（加急处理）。'
                           + _WRITE_HINT + 'account_id 与 punish_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'punish_id': {'type': 'string', 'description': '处罚单号（违规记录里平台给的）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'punish_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 推广写（会真的改平台数据；required 必含 confirm） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'promotion_write_actions',
            'description': '列出推广写接口支持的动作名、对应 mtop 名与参数，以及明确不做的动作（充值/提现）。'
                           '调 promotion_write_execute 之前先用它确认 action 拼写。这是只读工具，不需要 confirm。',
            'parameters': {
                'type': 'object',
                'properties': {},
                'required': [],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'promotion_write_execute',
            'description': '执行一个推广写动作（建/删/暂停/重启推广计划、报名活动等）。action 必须是 promotion_write_actions 列出的动作名。'
                           + _WRITE_HINT + 'account_id 与 action 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'action': {'type': 'string', 'description': '动作名，取自 promotion_write_actions'},
                    'payload': {'type': 'object', 'description': '该动作的业务参数对象，键名按 promotion_write_actions 返回的 params；可空'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'action', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 资质写（会真的改平台数据；required 必含 confirm） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'qualification_submit_packages',
            'description': '提交资质材料（真实改变账号资质状态，不可逆）。业务对象原样进 requestJson，工具层不翻译字段。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'request': {'type': 'object', 'description': '资质业务对象，原样透传给平台；可空'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'qualification_oss_credential',
            'description': '申请 OSS 上传凭证（上传资质材料的第一步）。'
                           + _WRITE_HINT + 'account_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'request': {'type': 'object', 'description': '申请参数对象，原样透传给平台；可空'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 商品写（会真的改平台数据；required 必含 confirm） ----------------
    {
        'type': 'function',
        'function': {
            'name': 'items_batch_offline',
            'description': '批量下架商品（商品从「在售」变成「已下架」，买家就搜不到、也买不了了；'
                           '可以再上架，不是删除）。卖家说「把这几件先下掉/停卖」时用。'
                           + _WRITE_HINT + 'account_id 与 item_ids 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'item_ids': {'type': 'string', 'description': '多个商品 ID 用逗号分隔'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'item_ids', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_batch_delete',
            'description': '批量删除商品（**不可逆**，商品会被真的删掉，想再卖只能重新发布一遍；'
                           '只想暂时不卖请改用 items_batch_offline）。卖家明确说「删掉这几件」时才用。'
                           + _WRITE_HINT + 'account_id 与 item_ids 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'item_ids': {'type': 'string', 'description': '多个商品 ID 用逗号分隔'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'item_ids', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'items_quick_edit',
            'description': '快速改一件商品的售价/库存。price 与 quantity 至少给一个；'
                           '走官方 info.update，只提交价格和数量，不像「编辑」那样整份回提表单，'
                           '所以对已卖出商品没有重新上架的副作用。'
                           + _WRITE_HINT + 'account_id 与 item_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'item_id': {'type': 'string', 'description': '要改的商品 ID'},
                    'price': {'type': 'number', 'description': '新售价（元）；不给就不改价'},
                    'quantity': {'type': 'integer', 'description': '新库存数量；不给就不改库存'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'item_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    # ---------------- 改收货地址审批（1 读 + 2 写） ----------------
    # 读工具名故意**不是**写工具名的前缀（`trade_order_address_request` vs
    # `trade_order_address_agree`），否则加前缀的写工具名单会把它误判成写工具。
    {
        'type': 'function',
        'function': {
            'name': 'trade_order_address_request',
            'description': '只读：问平台这个订单有没有待处理的「买家申请修改收货地址」。'
                           '卖家问「他改地址了吗 / 有没有人申请改地址 / 能不能让他改」时用。'
                           '平台上没有申请时 status 是 NONE（这是正常的，不是报错）。'
                           'account_id 与 order_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'order_id': {'type': 'string', 'description': '订单 id'},
                },
                'required': ['account_id', 'order_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_order_address_agree',
            'description': '同意买家修改收货地址。同意后这个订单就按买家的**新地址**发货了，'
                           '发出去就撤不回来。卖家说「同意改地址 / 让他改吧」时用。'
                           + _WRITE_HINT + 'account_id、order_id、deliver_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'order_id': {'type': 'string', 'description': '订单 id'},
                    'deliver_id': {'type': 'string', 'description': '这条改地址申请的单据 id（从只读工具拿）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'order_id', 'deliver_id', 'confirm'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'trade_order_address_refuse',
            'description': '拒绝买家修改收货地址。拒绝后订单仍按**原来**的地址发货。'
                           '卖家说「不同意改地址 / 还是寄原来那个」时用。'
                           + _WRITE_HINT + 'account_id、order_id、deliver_id 必填。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'account_id': {'type': 'string', 'description': '闲鱼账号 id（unb）'},
                    'order_id': {'type': 'string', 'description': '订单 id'},
                    'deliver_id': {'type': 'string', 'description': '这条改地址申请的单据 id（从只读工具拿）'},
                    'confirm': {'type': 'boolean', 'description': '必须显式传 true 才真的执行'},
                },
                'required': ['account_id', 'order_id', 'deliver_id', 'confirm'],
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
    accounts = [dict(r) for r in rows]
    # 与 `GET /api/accounts` 的三个可选筛选参数保持同一口径（两边都对外宣称支持）。
    enabled = args.get('enabled')
    if enabled is not None:
        accounts = [a for a in accounts if bool(a.get('enabled')) is bool(enabled)]
    keyword = str(args.get('q') or '').strip().lower()
    if keyword:
        accounts = [a for a in accounts
                    if any(keyword in str(a.get(f) or '').lower()
                           for f in ('nickname', 'remark', 'id'))]
    limit = args.get('limit')
    if limit:
        accounts = accounts[:int(limit)]
    return {'ok': True, 'accounts': accounts}


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
    'item_templates_create': ['item_templates', 'create_template'],
    'item_templates_modify': ['item_templates', 'modify_template'],
    'item_templates_delete': ['item_templates', 'delete_template'],
    'item_templates_unbind': ['item_templates', 'unbind_items'],
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
    # ---- 营销（只读）----
    # 服务层函数名是 item_metrics / operation_records；对外 HTTP 路径名仍是
    # /items、/records（首版路径已冻结，MCP 路由表与前端都写死了它，别再改名）。
    'marketing_items': ['marketing', 'item_metrics'],
    'marketing_tasks': ['marketing', 'tasks'],
    'marketing_price_competitiveness': ['marketing', 'price_competitiveness'],
    'marketing_records': ['marketing', 'operation_records'],
    # ---- 交易写（会真的改平台数据）----
    'trade_write_ship_offline': ['trade_write', 'ship_offline'],
    'trade_write_ship_dummy': ['trade_write', 'ship_dummy'],
    'trade_write_adjust_price': ['trade_write', 'adjust_price'],
    'trade_write_refund_agree': ['trade_write', 'refund_agree'],
    'trade_refund_verify_token': ['trade_write', 'refund_verify_token'],
    'trade_write_refund_refuse': ['trade_write', 'refund_refuse'],
    'trade_write_rate_create': ['trade_write', 'rate_create'],
    'trade_write_complain_refuse': ['trade_write', 'complain_refuse'],
    'trade_write_complain_proof': ['trade_write', 'complain_proof'],
    # ---- 退货地址写（会真的改平台上的退货地址）----
    'trade_address_add': ['trade_write', 'address_add'],
    'trade_address_update': ['trade_write', 'address_update'],
    'trade_address_delete': ['trade_write', 'address_delete'],
    'trade_address_set_default': ['trade_write', 'address_set_default'],
    # ---- 安全中心申诉 ----
    'security_appeal_form': ['security_write', 'appeal_form'],
    'security_appeal_submit': ['security_write', 'appeal_submit'],
    'security_appeal_submit_cro': ['security_write', 'appeal_submit_cro'],
    'security_appeal_urge': ['security_write', 'appeal_urge'],
    # ---- 推广写 ----
    'promotion_write_actions': ['ad_write', 'actions_catalog'],
    'promotion_write_execute': ['ad_write', 'execute'],
    # ---- 资质写 ----
    'qualification_submit_packages': ['qualification', 'submit_packages'],
    'qualification_oss_credential': ['qualification', 'oss_credential'],
    # ---- 商品写（会真的改平台数据）----
    'items_batch_offline': ['items', 'batch_offline'],
    'items_batch_delete': ['items', 'batch_delete'],
    'items_quick_edit': ['items', 'quick_edit'],
    # ---- 改收货地址审批（1 读 + 2 写）----
    'trade_order_address_request': ['trade_write', 'order_address_info'],
    'trade_order_address_agree': ['trade_write', 'order_address_agree'],
    'trade_order_address_refuse': ['trade_write', 'order_address_refuse'],
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
    'item_templates_create': lambda a: _dispatch_service('item_templates_create', a),
    'item_templates_modify': lambda a: _dispatch_service('item_templates_modify', a),
    'item_templates_delete': lambda a: _dispatch_service('item_templates_delete', a),
    'item_templates_unbind': lambda a: _dispatch_service('item_templates_unbind', a),
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
    'marketing_price_competitiveness': lambda a: _dispatch_service('marketing_price_competitiveness', a),
    'marketing_records': lambda a: _dispatch_service('marketing_records', a),
    # ---------------- 写能力 ----------------
    # 工具层**不做**确认判定：`confirm` 原样透传给服务层，由服务层返回
    # `confirm_required`（HTTP 400 / 工具 ok=False），错误码不许在这里改写。
    'trade_write_ship_offline': lambda a: _dispatch_service('trade_write_ship_offline', a),
    'trade_write_ship_dummy': lambda a: _dispatch_service('trade_write_ship_dummy', a),
    'trade_write_adjust_price': lambda a: _dispatch_service('trade_write_adjust_price', a),
    'trade_write_refund_agree': lambda a: _dispatch_service('trade_write_refund_agree', a),
    'trade_refund_verify_token': lambda a: _dispatch_service('trade_refund_verify_token', a),
    'trade_write_refund_refuse': lambda a: _dispatch_service('trade_write_refund_refuse', a),
    'trade_write_rate_create': lambda a: _dispatch_service('trade_write_rate_create', a),
    'trade_write_complain_refuse': lambda a: _dispatch_service('trade_write_complain_refuse', a),
    'trade_write_complain_proof': lambda a: _dispatch_service('trade_write_complain_proof', a),
    'trade_address_add': lambda a: _dispatch_service('trade_address_add', a),
    'trade_address_update': lambda a: _dispatch_service('trade_address_update', a),
    'trade_address_delete': lambda a: _dispatch_service('trade_address_delete', a),
    'trade_address_set_default': lambda a: _dispatch_service('trade_address_set_default', a),
    'security_appeal_form': lambda a: _dispatch_service('security_appeal_form', a),
    'security_appeal_submit': lambda a: _dispatch_service('security_appeal_submit', a),
    'security_appeal_submit_cro': lambda a: _dispatch_service('security_appeal_submit_cro', a),
    'security_appeal_urge': lambda a: _dispatch_service('security_appeal_urge', a),
    'promotion_write_actions': lambda a: _dispatch_service('promotion_write_actions', a),
    'promotion_write_execute': lambda a: _dispatch_service('promotion_write_execute', a),
    'qualification_submit_packages': lambda a: _dispatch_service('qualification_submit_packages', a),
    'qualification_oss_credential': lambda a: _dispatch_service('qualification_oss_credential', a),
    # ---- 商品写 ----
    'items_batch_offline': lambda a: _dispatch_service('items_batch_offline', a),
    'items_batch_delete': lambda a: _dispatch_service('items_batch_delete', a),
    'items_quick_edit': lambda a: _dispatch_service('items_quick_edit', a),
    # ---- 改收货地址审批（读那条也走 services，工具层同样不做 confirm 判定）----
    'trade_order_address_request': lambda a: _dispatch_service('trade_order_address_request', a),
    'trade_order_address_agree': lambda a: _dispatch_service('trade_order_address_agree', a),
    'trade_order_address_refuse': lambda a: _dispatch_service('trade_order_address_refuse', a),
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
