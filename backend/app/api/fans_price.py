"""粉丝价接口（官方「商品管理」页的「粉丝价」列：全部粉 / 老粉 / 已购粉）。

为什么单开一个模块而不塞进 `api/items.py`：`items.py` 已经 250+ 行、装的是
「商品列表 / 详情 / 同步 / 批量上下架 / 快捷改价」；粉丝价是**独立一族协议**
（`xianyu/fans_price.py`，来源是商品管理微应用 bundle），路径也是商品下的子资源
（`/{item_id}/fans-price`）。单开模块后 AI 工具与前端一眼能对上，改动也不会互相踩。

**写操作要显式 `confirm=true`**：未确认时一个平台请求都不会发（由
`test_fans_price_api.py` 钉住）。`confirm` **原样**交服务层判（不做 `bool()`）——
`bool('false')` 是 `True`，会把用户明确的拒绝当成同意（第 38 批那条真缺陷的教训）。

**★ 写是「整份替换」**：官方弹窗预填三组值再一起发，`fansPriceList` 里没有的组
= 没配置 ⇒ 留空 = 清空该组。响应里的 `submitted` 就是将要（已）发给平台的完整列表，
前端必须把它原样列进确认框。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ..services import common, fans_price
from .deps import current_user

router = APIRouter(prefix='/api/items', tags=['items'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/{item_id}/fans-price')
def read_fans_price(account_id: str, item_id: str,
                    user: Any = Depends(current_user)):
    """读某个商品当前的三组粉丝价（只读）。

    `prices` 为 `null` = 平台没返回配置（与「三个组都没配」的 `[]` 不同，**必须分开说**）；
    `groups` 三个键恒在，拿不到就是 `null`（前端显示 `-`，不许补 0）。
    """
    return _envelope(fans_price.read(account_id, item_id))


@router.post('/{account_id}/{item_id}/fans-price')
def set_fans_price(account_id: str, item_id: str,
                   body: Dict[str, Any] = Body(default={}),
                   user: Any = Depends(current_user)):
    """整份写回粉丝价（写，`confirm=true` 才发）。

    body 两种形状都收：`{"groups": {"all": "9.9", "old": "", "buy": null}}` 或
    `{"prices": [{"fans_group": "all", "price_string": "9.9"}]}`；
    两个都给时以 `prices` 为准（形状更贴近平台）。**留空的组会被清空。**
    """
    return _envelope(fans_price.set_prices(
        account_id, item_id, groups=body.get('groups'), prices=body.get('prices'),
        confirm=body.get('confirm')))
