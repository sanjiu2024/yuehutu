"""通知中心协议层 —— **只读**。

真机抓包（2026-10-10，`logs/seller_discover/notify_capture.json`，
headless Chrome 打开 `https://seller.goofish.com/?site=COMMONPRO#/notification-center`）：

- 频道树 `mtop.taobao.idlemessage.pc.notify.sessions.list`，body `{}`
  真机响应 `data = {"notifyInfoList": [{"key","title","icon","sessionId","children"?}], "success": true}`
  （全部 / 重要 / 交易履约{订单,售后,物流,评价} / 违规处理{处罚,申诉,举报} /
   小铺经营{资金管理,账号信息,商品信息,工具服务,流量运营,平台活动,政策规则,经营方法,调研问卷}）
- 未读计数 `mtop.taobao.idlemessage.pc.redpoint.query`，
  body `{"sessionTypes": "1,19,15,32,65,3,44,51,52,24", "fetch": 50}`
  真机响应 `data = {"needDecryptKeys": [], "needDecryptKeysV2": [], "total": 0}`

**通知条目本身没有 REST 接口**：抓包 18 秒内页面渲染出了 10 条通知
（`logs/seller_discover/notify_page2.txt`），但全程只有上面两个 mtop 调用，
条目来自 IM 长连接（同批还建了 `...pc.login.token` / `...pc.accs.token`）的推送。
所以复刻只覆盖「频道 + 未读」，条目列表如实返回空数组 + 说明，不编造。

标记已读 `mtop.taobao.idlemessage.pc.notifys.read`（body `{"messageId": "..."}`）
是**写接口**，列在 `WRITE_APIS` 里、故意不实现。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

NOTIFY_SESSIONS_API = 'mtop.taobao.idlemessage.pc.notify.sessions.list'
REDPOINT_QUERY_API = 'mtop.taobao.idlemessage.pc.redpoint.query'

#: 真机抓包里的 `sessionTypes` 原值（页面一次问 10 类未读）。
DEFAULT_SESSION_TYPES = '1,19,15,32,65,3,44,51,52,24'
DEFAULT_FETCH = 50

READ_APIS = (NOTIFY_SESSIONS_API, REDPOINT_QUERY_API)

#: 写接口：标记已读。只读复刻绝不调用。
WRITE_APIS = ('mtop.taobao.idlemessage.pc.notifys.read',)


async def _data(client: Any, api: str, body: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """调一次 mtop 并只取 `data` 段（拿不到就 None，由服务层如实降级）。

    dict / list 都放行 —— 与 `backend/app/xianyu/shop.py` 同一约定：有的接口
    业务载荷本身就是数组，只认 dict 会把整个列表静默吞掉。
    """
    result = await client.mtop(api, body if body is not None else {}, v='1.0')
    if not isinstance(result, dict):
        return None
    data = result.get('data')
    return data if isinstance(data, (dict, list)) else None


async def notify_channels(client: Any) -> Optional[Any]:
    """通知频道树（`notifyInfoList`）。"""
    return await _data(client, NOTIFY_SESSIONS_API, {})


async def unread_counts(client: Any,
                        session_types: str = DEFAULT_SESSION_TYPES,
                        fetch: int = DEFAULT_FETCH) -> Optional[Any]:
    """各类通知的未读计数（`total` / `needDecryptKeys`）。"""
    return await _data(client, REDPOINT_QUERY_API, {
        'sessionTypes': str(session_types),
        'fetch': int(fetch),
    })
