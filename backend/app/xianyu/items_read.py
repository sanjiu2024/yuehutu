"""商品读 5 条接口的只读协议（第 63 批 task-23）。

为什么单开一个模块、而不是往 `client.py` 里加方法：

* `backend/app/xianyu/client.py` 在本任务里是**冻结文件**（Lead 冻结清单），所以这里用
  「模块级 async 函数 + 调用方传入 client」的形态，服务层通过
  `common.protocol_module('items_read')` 懒导入（与 `xianyu/datacenter.py` 第 52 批、
  `xianyu/item_publish.py` 第 60 批同一手法）；
* `backend/app/services/items.py` 被 task-24 占用，本批**不碰**。

接口名、入参形状、响应取值路径都来自官方微应用 bundle（源码级证据，可 grep 复核）：

* `logs/seller_discover/item_templates_src/js_main.js`（4,402,065 字符，商品管理/发布页，
  调用形态 `gC({api, v:'1.0', data:{...}, showErrorToast:true})`）；
* `logs/seller_discover/datacenter_main.js`（数据组，调用形态 `xge({api, v:'1.0', data})`）。

调用形态对应本仓库的 `client.mtop(api, data)`（`client.py:335`）。

| 常量 | api | bundle 偏移 | 请求 data | 响应取值（bundle 原文） |
| --- | --- | --- | --- | --- |
| `SEARCH_API` | `mtop.alibaba.idle.seller.pc.common.item.search` | js `@3071624` | 透传 `{...e}` | `data.data.itemSearchResponseList` + `data.data.total` |
| `CATEGORY_API` | `mtop.alibaba.idle.seller.platform.idle.category.query` | dc `@2494750` | 透传 `{...e}` | **未取证**（调用点 0） |
| `OPERATE_REPORT_API` | `mtop.alibaba.idle.seller.platform.item.operate.report.get` | js `@3081396` | 透传 `{...e}` | **未取证**（调用点 0） |
| `SEARCH_TERM_API` | `mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query` | js `@3075400` | **无 data** | `data.data.{returnService,sendGoodsTime,serviceGuarantee}`，元素 `{key,value}` |
| `PREGET_API` | `mtop.idle.pc.idleitem.preget` | js `@3062151`（另一处 `@4142614`） | `{itemId, sourceId, publishScene:'firstHandPublish'}` | `data.needUpFirstHandItem` / `data.industryId` |

**口径（与第 60 批 `xianyu/item_publish.py` 同一条）**：

1. **不猜字段**：认不出的键一律 `None` / 空列表，**绝不用 0 或空字符串顶替**；
2. 平台的业务码 `code` 与 `msg` **原样带出来**，让调用方自己分辨「平台没给」与「链路坏了」；
3. 三个「透传入参」的接口（search / category / operate_report）在返回值里带 **`sent`**
   —— 探针要能一眼看出我们到底发了什么；
4. 只读：本模块没有任何 `.set` / `.update` / `.delete` 接口，也不落库、不播事件。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- 平台接口
#: 商品列表：服务端搜索 + 分页 + 排序（官方「商品管理」页的主查询）
SEARCH_API = 'mtop.alibaba.idle.seller.pc.common.item.search'
#: 类目查询（数据组 bundle 里只有声明，调用点 0 ⇒ 入参形状按透传处理）
CATEGORY_API = 'mtop.alibaba.idle.seller.platform.idle.category.query'
#: 商品操作报告（bundle 里只有声明，调用点 0 ⇒ 入参形状按透传处理）
OPERATE_REPORT_API = 'mtop.alibaba.idle.seller.platform.item.operate.report.get'
#: 筛选维度字典（退货 / 发货时效 / 服务保障…；**无参**）
SEARCH_TERM_API = 'mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query'
#: 发布/编辑前的预请求（成色、行业 id、是否要上传首件）
PREGET_API = 'mtop.idle.pc.idleitem.preget'

# --------------------------------------------------------------------------- 固定入参
#: `bizType`：官方商品管理页写死 `commonPro`（`docs/contracts/item_templates.md` §4）
SEARCH_BIZ_TYPE = 'commonPro'
#: `itemStatus`：官方 tab 三元组之一「在售 + 已下架」（`0,-9`）
SEARCH_DEFAULT_STATUS = '0,-9'
#: 官方分页只有 10 / 20 / 50 三档
SEARCH_PAGE_SIZES: Tuple[int, ...] = (10, 20, 50)
SEARCH_DEFAULT_PAGE_SIZE = 20
#: 官方排序方向只有 asc / desc
SEARCH_ORDER_TYPES: Tuple[str, ...] = ('asc', 'desc')
#: 发布页预请求的固定 `publishScene`（bundle 原文）
SEARCH_PREGET_SCENE = 'firstHandPublish'
#: 预请求里「要不要上传首件」等字段名（bundle 原文）
PREGET_SCENE_FIELD = 'publishScene'
#: 筛选维度字典的三个分组（bundle 原文）
TERM_GROUPS: Tuple[str, ...] = ('returnService', 'sendGoodsTime', 'serviceGuarantee')
#: 列表键候选（第一个是 bundle 原文，其余是容错）
SEARCH_LIST_KEYS: Tuple[str, ...] = ('itemSearchResponseList', 'itemList', 'list', 'rows', 'items')
#: 总数键候选（第一个是 bundle 原文）
SEARCH_TOTAL_KEYS: Tuple[str, ...] = ('total', 'totalCount', 'count')


def _text(value: Any) -> str:
    """`None` → 空串（平台字段名/值不保证是字符串）。"""
    return '' if value is None else str(value)


# --------------------------------------------------------------------------- 响应解析
def envelope(raw: Any) -> Dict[str, Any]:
    """平台业务信封 `{code, msg, data}`。

    `client.mtop` 成功时返回 `{'success': True, 'data': <平台 data>}`，所以业务信封在
    `raw['data']`（与 `xianyu/item_publish.py::envelope` 同口径）；测试里的假客户端
    也按这个形状给。
    """
    if not isinstance(raw, dict):
        return {}
    inner = raw.get('data')
    return inner if isinstance(inner, dict) else {}


def business(raw: Any) -> Any:
    """业务体 `res.data.data`（bundle 里 `itemSearchResponseList` 就是从这里取）。"""
    return envelope(raw).get('data')


def code_of(raw: Any) -> str:
    """平台业务码 `res.data.code`（`success` / `FAIL_BIZ_IDLE_USER_UNAUTHORIZED` …）。"""
    return _text(envelope(raw).get('code'))


def message_of(raw: Any) -> str:
    """平台失败原文：`msg` 优先，其次 `message`；客户端层失败时退回 `raw['message']` / `ret[0]`。"""
    env = envelope(raw)
    text = _text(env.get('msg')) or _text(env.get('message'))
    if text:
        return text
    if isinstance(raw, dict) and raw.get('success') is False:
        ret = raw.get('ret')
        first = ret[0] if isinstance(ret, list) and ret else ''
        return _text(raw.get('message')) or _text(first)
    return ''


def list_of(body: Any, *keys: str) -> List[Any]:
    """从业务体里按候选键名取列表（键名是从压缩 JS 里读的，所以给一串候选）。

    取不到就回空列表 —— **不造数据**：服务层据此如实回「平台没返回列表」。
    """
    if isinstance(body, list):
        return list(body)
    if not isinstance(body, dict):
        return []
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _first_key(body: Any, keys: Tuple[str, ...]) -> str:
    """返回第一个真的命中的键名（`''` = 一个都没命中）；探针据此分辨「平台改了字段名」。"""
    if not isinstance(body, dict):
        return ''
    for key in keys:
        if isinstance(body.get(key), list):
            return key
    return ''


def search_rows(body: Any) -> Tuple[List[Any], str]:
    """商品列表 → `(rows, found_key)`；`found_key` 空串表示平台没给列表字段。"""
    return list_of(body, *SEARCH_LIST_KEYS), _first_key(body, SEARCH_LIST_KEYS)


def search_total(body: Any) -> Optional[int]:
    """总条数：认不出就是 `None`（**不是 0** —— 0 会让前端显示「一共 0 件」）。"""
    if not isinstance(body, dict):
        return None
    for key in SEARCH_TOTAL_KEYS:
        value = body.get(key)
        if isinstance(value, bool) or value in (None, ''):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def term_options(body: Any) -> Dict[str, List[Dict[str, str]]]:
    """筛选维度字典 → `{分组名: [{key, value}]}`（只收 bundle 里那三个分组）。

    元素形状是 bundle 原文的 `{key, value}`；认不出 `key`/`value` 的行**丢弃**
    （一个点不动的空选项比少一行更坏）。
    """
    out: Dict[str, List[Dict[str, str]]] = {}
    for group in TERM_GROUPS:
        rows = body.get(group) if isinstance(body, dict) else None
        values: List[Dict[str, str]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = _text(row.get('key')).strip()
                label = _text(row.get('value')).strip()
                if not key or not label:
                    continue
                values.append({'key': key, 'value': label})
        out[group] = values
    return out


# --------------------------------------------------------------------------- 入参构造
def _search_request(keyword: str, raw: str = '') -> str:
    """`searchRequest` 是**JSON 字符串**（bundle 原文 `searchRequest:'{}'`）。

    调用方给了原始串就原样发（探针用：官方搜索表单的键名在 bundle 里看不到 ——
    `docs/contracts/item_templates.md` §5 未确定项 5）；只给了关键词时用 `{'title': …}`，
    **键名未取证**，认不出平台会回业务码，不会静默出错。
    """
    text = _text(raw).strip()
    if text:
        return text
    keyword = _text(keyword).strip()
    if not keyword:
        return '{}'
    return json.dumps({'title': keyword}, ensure_ascii=False)


def search_data(keyword: str = '', page: int = 1, size: int = SEARCH_DEFAULT_PAGE_SIZE,
                item_status: str = '', biz_type: str = '', search_request: str = '',
                order_field: str = '', order_type: str = '') -> Dict[str, Any]:
    """商品列表搜索的请求体（bundle 原文：`pageNo` / `pageSize` / `bizType` /
    `searchRequest` / `itemStatus`，排序 `orderField` / `orderType`）。

    排序键**只在两个都给了的时候才发**：只发 `orderType` 平台会报参数错，而我们又不知道
    默认排序字段 —— 不如让平台用它自己的默认。
    """
    data: Dict[str, Any] = {
        'pageNo': int(page),
        'pageSize': int(size),
        'bizType': _text(biz_type).strip() or SEARCH_BIZ_TYPE,
        'searchRequest': _search_request(keyword, search_request),
        'itemStatus': _text(item_status).strip() or SEARCH_DEFAULT_STATUS,
    }
    field = _text(order_field).strip()
    direction = _text(order_type).strip().lower()
    if field and direction:
        data['orderField'] = field
        data['orderType'] = direction
    return data


def preget_data(item_id: Any = '', source_id: Any = '',
                publish_scene: str = '') -> Dict[str, Any]:
    """发布预请求的请求体（bundle 原文逐字：`{itemId, sourceId, publishScene:'firstHandPublish'}`）。"""
    return {
        'itemId': _text(item_id),
        'sourceId': _text(source_id),
        PREGET_SCENE_FIELD: _text(publish_scene).strip() or SEARCH_PREGET_SCENE,
    }


def preget_result(body: Any) -> Dict[str, Any]:
    """预请求响应 → 已证实的两个字段 + 原始业务体。

    真机 / bundle 原文只证实了两个键：`needUpFirstHandItem`（bool）、`industryId`。
    其余字段**一个都不提升**（`body` 里原样透出，前端/AI 想看自己看）。
    """
    need = body.get('needUpFirstHandItem') if isinstance(body, dict) else None
    industry = body.get('industryId') if isinstance(body, dict) else None
    return {
        'need_up_first_hand_item': need if isinstance(need, bool) else None,
        'industry_id': None if industry in (None, '') else _text(industry),
        'found': isinstance(body, (dict, list)),
        'data': body,
    }


# --------------------------------------------------------------------------- 对外接口
async def search_items(client: Any, keyword: str = '', page: int = 1,
                       size: int = SEARCH_DEFAULT_PAGE_SIZE, item_status: str = '',
                       biz_type: str = '', search_request: str = '',
                       order_field: str = '', order_type: str = '') -> Dict[str, Any]:
    """商品列表：服务端搜索 + 分页 + 排序（`SEARCH_API`）。"""
    data = search_data(keyword, page, size, item_status, biz_type, search_request,
                       order_field, order_type)
    raw = await client.mtop(SEARCH_API, data)
    body = business(raw)
    rows, found_key = search_rows(body)
    return {
        'api': SEARCH_API,
        'code': code_of(raw),
        'message': message_of(raw),
        'rows': rows,
        'found_key': found_key,
        'total': search_total(body),
        'sent': data,
    }


async def category_query(client: Any, params: Any = None) -> Dict[str, Any]:
    """类目查询（`CATEGORY_API`，**入参透传**）。

    这个接口在 bundle 里只有声明、调用点 0 ⇒ 我们**不猜**它的入参字段名，
    调用方给什么就发什么（服务层从查询串的 `params` JSON 收）。
    """
    data = dict(params) if isinstance(params, dict) else {}
    raw = await client.mtop(CATEGORY_API, data)
    body = business(raw)
    return {
        'api': CATEGORY_API,
        'code': code_of(raw),
        'message': message_of(raw),
        'data': body,
        'found': isinstance(body, (dict, list)),
        'sent': data,
    }


async def operate_report(client: Any, params: Any = None) -> Dict[str, Any]:
    """商品操作报告（`OPERATE_REPORT_API`，**入参透传**，理由同 `category_query`）。"""
    data = dict(params) if isinstance(params, dict) else {}
    raw = await client.mtop(OPERATE_REPORT_API, data)
    body = business(raw)
    return {
        'api': OPERATE_REPORT_API,
        'code': code_of(raw),
        'message': message_of(raw),
        'data': body,
        'found': isinstance(body, (dict, list)),
        'sent': data,
    }


async def search_terms(client: Any) -> Dict[str, Any]:
    """筛选维度字典（`SEARCH_TERM_API`，**无参**）。

    bundle 原文：`gC({api:'mtop.taobao.idle.seller.platform.item.pc.fix.search.term.query',
    v:'1.0', data:{...e}})`，官方页面上调用点 0 参（`O0()`）。
    """
    raw = await client.mtop(SEARCH_TERM_API, None)
    body = business(raw)
    terms = term_options(body)
    return {
        'api': SEARCH_TERM_API,
        'code': code_of(raw),
        'message': message_of(raw),
        'terms': terms,
        'found_groups': [name for name, values in terms.items() if values],
        'sent': {},
    }


async def preget(client: Any, item_id: Any = '', source_id: Any = '',
                 publish_scene: str = '') -> Dict[str, Any]:
    """发布/编辑前的预请求（`PREGET_API`）：要不要上传首件、行业 id。"""
    data = preget_data(item_id, source_id, publish_scene)
    raw = await client.mtop(PREGET_API, data)
    body = business(raw)
    result = preget_result(body)
    result.update({
        'api': PREGET_API,
        'code': code_of(raw),
        'message': message_of(raw),
        'sent': data,
    })
    return result
