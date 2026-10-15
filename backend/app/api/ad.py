"""推广（投放）页面缺的那几块：投放包 / 人群包 / 计划日志 / 消息中心（十条 GET + 一条 POST）。

路由命名与 `backend/app/api/promotion.py` / `ad_write.py` 同族：只读全部用 **GET**
（查询串参数，FastAPI 先做类型与范围校验），只有 `message.read` 是写、必须 POST 且带
`confirm` 闸门。响应统一走 `common.respond`：成功
`{"success": true, "data": {...}}`，失败
`{"success": false, "message": "...", "code": "..."}`。

**只读接口一律 GET 是有意的**：`backend/tests/test_write_routes_have_gates.py` 把
「非 GET 路由背后的服务函数必须有确认闸门」当作硬约束，而只读接口本来就不该有闸门 ——
用 GET 表达「这不会改平台状态」，比在闸门白名单里给它开个口子更诚实。

已挂载：`backend/app/main.py` 的 `ROUTE_MODULES` 含 `'ad'`（本任务加的那一行）。
测试自己 `include_router`，见 `backend/tests/test_ad_api.py`。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from ..services import ad, common
from .deps import current_user

router = APIRouter(prefix='/api/promotion/ad', tags=['promotion'])


def _envelope(result: Any):
    status, body = common.respond(result)
    return body if status == 200 else JSONResponse(status_code=status, content=body)


def _query_body(body: Any, payload: str) -> Any:
    """`package-info` / `bid-estimates` 的入参是「整包查询对象」，用 JSON body 表达最诚实。

    但 **GET 带 body 不是所有客户端都发得出来**（MCP / 工具面常见），所以再给一个
    `?payload=<json 对象>` 的兜底入口：body 优先，payload 只在 body 为空时用。
    解析不出对象时返回 `None`，由端点翻成 400 `invalid_fields`（不静默当空对象查）。
    """
    if isinstance(body, dict) and body:
        return body
    text = (payload or '').strip()
    if not text:
        return body if isinstance(body, dict) else {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@router.get('/apis')
def apis(user=Depends(current_user)):
    """本模块提供的接口目录（只读 kind 清单 + 唯一写动作 message_read）。"""
    return _envelope(ad.catalog())


@router.get('/{account_id}/account-info')
def account_info(
    account_id: str,
    order_price: str = Query('', description='订单金额（官方调用点传入）'),
    promotion_mode: str = Query('', description='投放模式（官方枚举值，原样透传）'),
    bid_type: str = Query('', description='出价类型（官方枚举值，原样透传）'),
    scene: str = Query('', description='场景，如 CREATION_REAL_TIME / CREATION_SUCCESS'),
    user=Depends(current_user),
):
    return _envelope(ad.account_info(account_id, order_price, promotion_mode, bid_type, scene))


@router.get('/{account_id}/package-info')
def package_info(
    account_id: str,
    body: Dict[str, Any] = Body(default={}, description='投放包查询对象，原样透传给平台'),
    payload: str = Query('', description='JSON 对象字符串（发不出 GET body 的客户端用这个）'),
    user=Depends(current_user),
):
    """投放包信息。GET 带 body 不常见，但这是查询不是写；没有 body 时按空对象查。"""
    data = _query_body(body, payload)
    if data is None:
        return _envelope(common.fail('payload 必须是 JSON 对象字符串', 'invalid_fields'))
    return _envelope(ad.package_info(account_id, data))


@router.get('/{account_id}/item-search')
def item_search(
    account_id: str,
    item_id: str = Query('', description='商品 ID（与 search_key 至少给一个）'),
    search_key: str = Query('', description='搜索词（与 item_id 至少给一个）'),
    campaign_id: str = Query('', description='计划 ID'),
    need_tabs: str = Query('', description='是否要 tabList（true/false）'),
    sort_field: str = Query('', description='排序字段'),
    sort_type: str = Query('', description='排序方向'),
    page_num: str = Query('', description='页码'),
    page_size: str = Query('', description='每页条数'),
    user=Depends(current_user),
):
    return _envelope(ad.item_search(account_id, need_tabs, item_id, search_key, campaign_id,
                                    sort_field, sort_type, page_num, page_size))


@router.get('/{account_id}/bid-estimates')
def bid_estimates(
    account_id: str,
    body: Dict[str, Any] = Body(default={}, description='{entityType, entityList, bidTarget, ...}'),
    payload: str = Query('', description='JSON 对象字符串（发不出 GET body 的客户端用这个）'),
    user=Depends(current_user),
):
    data = _query_body(body, payload)
    if data is None:
        return _envelope(common.fail('payload 必须是 JSON 对象字符串', 'invalid_fields'))
    return _envelope(ad.customize_bid_estimates(account_id, data))


@router.get('/{account_id}/groups')
def groups(
    account_id: str,
    start_date: str = Query('', description='YYYYMMDD'),
    end_date: str = Query('', description='YYYYMMDD'),
    page_num: str = Query('', description='页码'),
    page_size: str = Query('', description='每页条数'),
    campaign_id: str = Query('', description='计划 ID'),
    group_status: str = Query('', description='投放单元状态（官方枚举）'),
    promotion_mode: str = Query('', description='投放模式（官方枚举）'),
    title_like: str = Query('', description='计划标题模糊搜索'),
    user=Depends(current_user),
):
    return _envelope(ad.group_list(account_id, start_date, end_date, page_num, page_size,
                                   campaign_id, group_status, promotion_mode, title_like))


@router.get('/{account_id}/groups/data')
def group_data(
    account_id: str,
    ad_group_id: str = Query('', description='投放单元 ID（必填）'),
    start_date: str = Query('', description='YYYYMMDD'),
    end_date: str = Query('', description='YYYYMMDD'),
    all_period: str = Query('', description='是否全周期（官方传 false）'),
    user=Depends(current_user),
):
    return _envelope(ad.group_data(account_id, ad_group_id, start_date, end_date, all_period))


@router.get('/{account_id}/campaign-log')
def campaign_log(
    account_id: str,
    start_date: str = Query('', description='YYYYMMDD'),
    end_date: str = Query('', description='YYYYMMDD'),
    campaign_id: str = Query('', description='计划 ID（官方 Number(x)||0）'),
    page_num: str = Query('', description='页码'),
    page_size: str = Query('', description='每页条数'),
    operator_type: str = Query('', description='0=全部 / 1=用户 / 2=系统'),
    operation_type: str = Query('', description='0=全部 / 1=添加 / 2=更新'),
    user=Depends(current_user),
):
    return _envelope(ad.campaign_log(account_id, start_date, end_date, campaign_id, page_num,
                                     page_size, operator_type, operation_type))


@router.get('/{account_id}/messages')
def messages(
    account_id: str,
    page_num: str = Query('', description='页码'),
    page_size: str = Query('', description='每页条数'),
    user=Depends(current_user),
):
    return _envelope(ad.message_list(account_id, page_num, page_size))


@router.get('/{account_id}/messages/unread')
def messages_unread(account_id: str, user=Depends(current_user)):
    """未读数。官方 `data` 为空、不套 requestJson，所以这里不接受任何查询参数。"""
    return _envelope(ad.message_unread(account_id))


@router.get('/{account_id}/messages/{message_id}')
def message_detail(account_id: str, message_id: str, user=Depends(current_user)):
    return _envelope(ad.message_detail(account_id, message_id))


@router.post('/{account_id}/messages/read')
def messages_read(
    account_id: str,
    body: Dict[str, Any] = Body(default={}),
    user=Depends(current_user),
):
    """标记推广消息已读（**写**）。请求体：`{"confirm": true, "message_ids": [1, 2]}`。

    `confirm` 只认字面 `true`；不给就是 400 `confirm_required`，一次协议调用都不发。
    带 `Idempotency-Key` 头时同一键 + 同一指纹只打平台一次。
    """
    payload = body if isinstance(body, dict) else {}
    return _envelope(ad.message_read(account_id, payload.get('message_ids'), payload.get('confirm')))
