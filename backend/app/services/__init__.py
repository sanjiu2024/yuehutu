"""业务服务层：商品 / 会话 / 客服 / 订单 / 风控。

这一层只做业务规则与落库，**不碰 HTTP**（路由在 `backend/app/api/`），
也**不直接依赖协议层**（用 `common.get_client()` 懒导入，未就绪时返回
`{"success": false, "code": "xianyu_not_ready"}`）。

模块地图：
- `common`：信封 / 操作日志 / 事件推送 / 协议层懒导入适配
- `items`：商品增删改查、批量修改、上下架、同步、统计
- `chats`：会话与消息、人工接管、AI 开关
- `cs`：客服意图分类、快捷回复、问候/发货提醒模板
- `orders`：订单、发货、统计
- `risk_service`：风控状态、事件、冷却阶梯、自动滑块
"""

from __future__ import annotations

__all__ = ['common', 'items', 'chats', 'cs', 'orders', 'risk_service']
