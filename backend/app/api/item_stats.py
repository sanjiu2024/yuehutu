"""商品数据路由（薄层：只声明一条路径，调度 `services.item_stats`）。

`GET /api/items/{account_id}/stats` —— 商品页「商品数据」视图的数据源。

**为什么单独一个模块**：`backend/app/api/items.py` 里已经有 `GET /api/items/{account_id}/{item_id}`
（商品详情），而 `stats` 只有一段路径，谁先声明谁赢 —— 所以本模块必须在
`backend/app/main.py` 的 `ROUTE_MODULES` 里排在 `'items'` **之前**，否则
`/api/items/acc1/stats` 会被当成 `item_id='stats'` 的详情请求吞掉。

查询参数：
- `item_id`（可选）：只看某一条商品；给了但库里没有 → 404。
- `source`（可选，默认 `auto`）：
  - `auto` —— 平台能用就用平台，用不了降级为本地统计（+`warnings`）；
  - `local` —— 完全不碰平台、也不校验登录态，纯本地统计；
  - `platform` —— 必须拿到平台数据，失败直接 502 `protocol_error`；
  - `cache` —— **只读后台缓存**（`item_stats_cache`），零平台调用：页面点开就读它，
    不等网络。缓存由 `services/stats_worker.py` 在后台一轮一轮慢慢填。

另有 `GET /api/items/{account_id}/stats/progress` —— 后台拉取进度（共几件 / 已拉到几件 /
失败几件 / 最近一次成功时间），给页面显示「后台还在拉，已拉到 12/45」。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, item_stats, stats_worker
from .deps import current_user

router = APIRouter(prefix='/api/items', tags=['item-stats'])


def _envelope(result: Any):
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


@router.get('/{account_id}/stats/progress')
def account_item_stats_progress(account_id: str, user: Any = Depends(current_user)):
    """后台商品数据拉取进度。

    与 `/stats` 分开是因为它**绝不碰平台**：只读库里的件数与缓存行数，
    页面可以放心地随「商品数据」标签页一起刷新。
    """
    return _envelope(stats_worker.progress(account_id))


@router.get('/{account_id}/stats')
def account_item_stats(
    account_id: str,
    item_id: str = Query(default='', description='只看这一条商品（可选）'),
    source: str = Query(default='', description='auto | local | platform | cache'),
    user: Any = Depends(current_user),
):
    """某个账号（或其中某条商品）的商品数据。"""
    return _envelope(item_stats.account_stats(account_id, item_id=item_id, source=source))
