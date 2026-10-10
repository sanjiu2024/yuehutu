"""营销页只读接口（`/marketing`）。

四条 GET，全部只读：

============================  ==========================================
路径                          对应官方页面
============================  ==========================================
``/{account_id}/items``                  商品数据罗盘（单商品指标）
``/{account_id}/tasks``                  营销任务 / 商品分页
``/{account_id}/price-competitiveness``  价格竞争力
``/{account_id}/records``                任务操作记录
============================  ==========================================

路径名沿用首版（HEAD 的 MCP 路由表 `mcp_server/client.py` 与前端营销页都按这四条
写死）；订正的是**参数与契约键**，见 `docs/contracts/marketing.md`。注意
`/items` 是**单商品**指标（必填 `item_id`），**不吃分页**。

**没有 POST / PUT / PATCH / DELETE**：营销页的投放 / 报名 / 批量操作（协议层
`WRITE_APIS` 里登记的那两个写接口）本任务不实现 —— 花钱的动作不做复刻，FastAPI 对
未注册的方法在这些路径上会自动回 **405**（`tests/test_marketing_api.py` 专门钉住了
这一点）。

查询参数一律收成 ``str``，校验放在服务层（那里才知道官方枚举的全集），这样非法枚举
能给出**带候选值的中文 400**，而不是 FastAPI 自己的 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..services import common, marketing
from .deps import current_user

router = APIRouter(prefix='/api/marketing', tags=['marketing'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


@router.get('/{account_id}/items')
def item_metrics(account_id: str,
                 item_id: str = Query('', description='商品 id（必填，官方键名是 itmId）'),
                 date_type: str = Query('', description='recent1d / recent7d / recent30d / customDate'),
                 date_range: str = Query('', description='customDate 时填 YYYYMMDD|YYYYMMDD'),
                 user=Depends(current_user)):
    """商品数据罗盘指标（只读）。"""
    return _envelope(marketing.item_metrics(account_id, item_id, date_type, date_range))


@router.get('/{account_id}/tasks')
def tasks(account_id: str,
          biz_scene: str = Query('', description='TRAFFIC_DRIVEN_REPRICING / TRAFFIC_ORDER_BOOST'),
          query_scene: str = Query('', description='官方 15 个 queryScene 之一'),
          page: str = Query('', description='页码，从 1 开始'),
          size: str = Query('', description='每页条数，1~50，缺省 10'),
          item_ids: str = Query('', description='逗号分隔的商品 id，可空'),
          user=Depends(current_user)):
    """营销任务 / 商品分页（只读）。"""
    return _envelope(marketing.tasks(account_id, biz_scene, query_scene, page, size, item_ids))


@router.get('/{account_id}/price-competitiveness')
def price_competitiveness(account_id: str,
                          biz_scene: str = Query('', description='TRAFFIC_DRIVEN_REPRICING / TRAFFIC_ORDER_BOOST'),
                          item_id: str = Query('', description='商品 id（必填）'),
                          sku_prices: str = Query('', description='JSON 对象 {"<skuId>": <priceYuan>}'),
                          commission_rate: str = Query('', description='仅 TRAFFIC_ORDER_BOOST 支持'),
                          user=Depends(current_user)):
    """价格竞争力（只读）。响应是字符串，不是数字。"""
    return _envelope(marketing.price_competitiveness(
        account_id, biz_scene, item_id, sku_prices, commission_rate))


@router.get('/{account_id}/records')
def operation_records(account_id: str,
                      task_id: str = Query('', description='任务 id（必填）'),
                      biz_scene: str = Query('', description='TRAFFIC_DRIVEN_REPRICING / TRAFFIC_ORDER_BOOST'),
                      query_scene: str = Query('', description='缺省 ORDER_BOOST_OPERATION_RECORD'),
                      user=Depends(current_user)):
    """任务操作记录（只读）。**无分页、无时间范围**。"""
    return _envelope(marketing.operation_records(account_id, task_id, biz_scene, query_scene))
