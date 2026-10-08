"""财务路由（薄层：只声明路径，调度 `services.finance`）。

四条只读路径（前端「财务」页四个 tab 各一条）：

- `GET /api/finance/{account_id}/income`   收入账单（汇总 + 流水 + 业务大类）
- `GET /api/finance/{account_id}/expense`  支出账单（同上，换接口族）
- `GET /api/finance/{account_id}/invoices` 申请发票（待申请账单分组 + 已提交申请）
- `GET /api/finance/{account_id}/purchaser` 开票信息（抬头 / 纳税类型 / 票种）

**没有写接口**：开票申请是不可逆的平台操作，本工作台不代发 —— 要开票请去官方卖家
工作台。这一点在前端每个 tab 底部也有明示。

失败码沿用全站口径（`services/common.FAIL_STATUS`）：
`verification_required` → 409（账号要重新扫码）、`protocol_error` → 502（平台接口失败）、
`invalid_fields`/`bad_request` → 400（账期或分页参数不对）、`not_found` → 404（账号不存在）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, finance
from .deps import current_user

router = APIRouter(prefix='/api/finance', tags=['finance'])

CYCLE_QUERY = Query(default='', description='账期，YYYYMM（例如 202609）；留空取本月')
PAGE_QUERY = Query(default=1, description='页码，从 1 开始')
SIZE_QUERY = Query(default=20, description='每页条数，1~50')


def _envelope(result: Any):
    """失败信封带状态码透出（409 让前端弹重新扫码，502 才是平台接口错）。"""
    status_code, body = common.respond(result)
    return body if status_code == 200 else JSONResponse(status_code=status_code, content=body)


@router.get('/{account_id}/income')
def account_income(
    account_id: str,
    cycle_from: str = Query(default='', alias='from', description='开始账期，YYYYMM'),
    cycle_to: str = Query(default='', alias='to', description='结束账期，YYYYMM'),
    biz_type: str = Query(default='', description='业务大类编码；留空用平台默认'),
    page: int = PAGE_QUERY,
    size: int = SIZE_QUERY,
    user: Any = Depends(current_user),
):
    """收入账单：某个账号某个月的收入汇总与流水。"""
    return _envelope(finance.account_bills(
        account_id, 'income', cycle_from=cycle_from, cycle_to=cycle_to,
        biz_type=biz_type, page=page, size=size))


@router.get('/{account_id}/expense')
def account_expense(
    account_id: str,
    cycle_from: str = Query(default='', alias='from', description='开始账期，YYYYMM'),
    cycle_to: str = Query(default='', alias='to', description='结束账期，YYYYMM'),
    biz_type: str = Query(default='', description='业务大类编码；留空用平台默认'),
    page: int = PAGE_QUERY,
    size: int = SIZE_QUERY,
    user: Any = Depends(current_user),
):
    """支出账单：某个账号某个月的扣费汇总与流水。"""
    return _envelope(finance.account_bills(
        account_id, 'expense', cycle_from=cycle_from, cycle_to=cycle_to,
        biz_type=biz_type, page=page, size=size))


@router.get('/{account_id}/invoices')
def account_invoices(
    account_id: str,
    page: int = PAGE_QUERY,
    size: int = SIZE_QUERY,
    biz_types: str = Query(default='', description='业务类型编码，逗号分隔；留空不筛'),
    user: Any = Depends(current_user),
):
    """申请发票：待申请发票的账单分组 + 已提交的申请列表。"""
    return _envelope(finance.account_invoices(
        account_id, page=page, size=size, biz_types=biz_types))


@router.get('/{account_id}/purchaser')
def account_purchaser(account_id: str, user: Any = Depends(current_user)):
    """开票信息：发票抬头 / 纳税类型 / 支持的票种。"""
    return _envelope(finance.account_purchaser(account_id))
