"""物流接口（官方卖家工作台「订单管理 → 发货 / 修改物流」的辅助接口）。

为什么单开 `/api/logistics` 而不并进 `/api/trade`：这一族的**读**接口属于平台侧字典
（快递公司表、运单号认公司），跟订单表没关系；写接口只有一条「修改运单号」，
与 `/api/trade/{account_id}/orders/{order_id}/...` 那批写端点共用 `confirm` 口径但来源不同模块
（`xianyu/logistics.py`），路径分开，AI 工具与前端一眼能对上。

**写操作全部要显式 `confirm=true`**：未确认时一个平台请求都不会发（由
`test_logistics_api.py` 钉住）。`confirm` **原样**交服务层判（不做 `bool()`）——
`bool('false')` 是 `True`，会把用户明确的拒绝当成同意（第 222 条缺陷的教训）。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, logistics
from .deps import current_user

router = APIRouter(prefix='/api/logistics', tags=['logistics'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/companies')
def list_companies(account_id: str, user: Any = Depends(current_user)):
    """快递公司列表（只读）。平台没给 `items` 键时是 `[]` + warning，不是「一家都没有」。"""
    return _envelope(logistics.companies(account_id))


@router.get('/{account_id}/guess')
def guess_mailno(account_id: str, mail_no: str = Query(default=''),
                 user: Any = Depends(current_user)):
    """运单号认快递公司（只读）。认不出时 `cp_code`/`cp_name` 为 `null` + warning，**不猜公司**。"""
    return _envelope(logistics.guess_mailno(account_id, mail_no))


@router.post('/{account_id}/orders/{order_id}/resend')
def resend_consign(account_id: str, order_id: str, body: Dict[str, Any] = Body(default={}),
                   user: Any = Depends(current_user)):
    """修改运单号 / 重发（写，`confirm=true` 才发）。`cp_code` 留空用官方默认 `OTHER`。"""
    return _envelope(logistics.resend_consign(
        account_id, order_id, body.get('mail_no'), body.get('cp_code'),
        confirm=body.get('confirm')))
