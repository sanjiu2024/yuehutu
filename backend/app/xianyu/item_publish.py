"""官方「商品发布 / 编辑」表单**数据源**（类目 / 品牌 / 型号 / 属性 / 类目搜索）的只读协议。

## 为什么单开一个模块，而不是塞进 `item_write.py` 或 `client.py`

* `client.py` 在本任务里是冻结文件（写范围外），形态上沿用 `xianyu/datacenter.py`：
  **模块级 async 函数 + 调用方传 client**，服务层用 `common.protocol_module('item_publish')` 懒导入。
* `item_write.py` 文件头自己写明「只登记**写**接口」，并且带 `assert_write(api)` 守卫
  （未登记的 api 一律 `UnknownApi`）。发布数据源是**只读**的，塞进去会让那个守卫的语义
  变成「一半写一半读」，下一个人再想加读接口就不知道该不该走 `assert_write`。
* 与已有的 `client.recommend_category`（`mtop.taobao.idle.kgraph.property.recommend`）
  **不是一回事**：那个是「给标题猜类目」（写入前的推荐），这里是「按类目/品牌拿候选值」
  （表单的 select 数据源）。前者替代不了后者。

## 接口名与入参来自哪里

官方微应用 bundle（单行压缩 JS），下文 `@偏移` 都是**字符偏移**，可直接 grep 复核：

* 商品发布页（含筛选侧栏）：`C:\\Users\\Administrator\\.dsh-tmp\\trade_bundle\\main.js`
  （3,879,584 字符；调用形态 `hO({api, v:'1.0', valueType:'string', data})`）。
  - `publish.categories` 调用点 `@3832720`：请求只有 `{api, v:'1.0', valueType:'string'}`（**无 data**），
    响应取 `res.data.data.selectIdleCategories`，元素直接当 select 选项用。
  - `publish.brands` 调用点 `@3833710`：`data:{channelCategoryId: Number(分类路径最后一级 id)}`，
    成功判据 `res.data.code === 'success'`，响应 `res.data.data.selectIdleBrandValues`，
    映射 `{label: valueName, value: valueId}`。
  - `publish.models` 调用点 `@3834799`：`data:{brandValueId: Number(品牌 id), channelCategoryId: Number(类目 id)}`
    （**两个都必填**，官方任一为空就不发请求），响应 `res.data.data.selectIdleModelValues`，同样映射。
* 商品管理页 bundle：`logs/seller_discover/item_templates_src/js_main.js`（4,402,065 字符；
  调用形态 `gC({api, v:'1.0', data, showErrorToast})`）。
  - `item.pc.category.property.query` 调用点 `@3074608`：`data:{categoryId: <类目 id>}`。
    响应字段**未在 bundle 里证实**（只见到 `data.data.fineness` 这一条线索，没有消费它的
    渲染代码可对照），所以 `category_properties` 的解析**对多种键名容错**，且对外标注「未真机验证」。
  - `item.pc.search.categories` 调用点 `@3073093`：**无 data**；响应取
    `res.data.data.selectIdleCategories`（与 `publish.categories` 同名字段，但那是
    「商品列表筛选页」的用法，是否与发布页一致**未验证**）。

## 真机证据

**没有。** 本轮（第 60 批）只做了 bundle 反查 + 源码级实现，没在真机上打过这 5 个接口
（真机探针由 Lead 在打包版上跑，清单见 `docs/contracts/items.md`）。因此：

* 协议层**不写任何兜底值**：认不出的字段就是 `None` / 空列表，绝不用 0 顶替；
* 解析函数对平台字段名**容错**（多种别名），因为字段名是从压缩 JS 里读出来的，
  不能保证和真机响应逐字一致；解析不出就是空列表，由服务层如实告诉用户「平台没返回候选值」，
  而不是编一个空下拉框让他以为「这个类目下没有品牌」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: 发布页：类目树（无参）
CATEGORIES_API = 'mtop.alibaba.idle.seller.platform.item.publish.categories'
#: 发布页：品牌（需 channelCategoryId）
BRANDS_API = 'mtop.alibaba.idle.seller.platform.item.publish.brands'
#: 发布页：型号（需 brandValueId + channelCategoryId）
MODELS_API = 'mtop.alibaba.idle.seller.platform.item.publish.models'
#: 商品管理页：类目下的属性/成色等候选值（需 categoryId）
CATEGORY_PROPERTY_API = 'mtop.taobao.idle.seller.platform.item.pc.category.property.query'
#: 商品管理页：类目搜索（无参）
SEARCH_CATEGORIES_API = 'mtop.taobao.idle.seller.platform.item.pc.search.categories'

#: 官方页面全部走 v1.0（bundle 里每个调用点的 v 都是 '1.0'）。
DEFAULT_VERSION = '1.0'

#: 只有这两个接口在 bundle 里显式写了 `valueType:'string'`（与 `client.mtop` 的 `value_type` 对应）。
VALUE_TYPE_STRING = 'string'

#: 已知接口名 → (api, 需要 valueType)
KNOWN_APIS: Tuple[str, ...] = (
    CATEGORIES_API, BRANDS_API, MODELS_API, CATEGORY_PROPERTY_API, SEARCH_CATEGORIES_API,
)


def _text(value: Any) -> str:
    return '' if value is None else str(value)


def _as_int(value: Any) -> Optional[int]:
    """平台要的是 Number（bundle 原文 `Number(...)`），能转就转，转不动给 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 入参构造


def categories_data() -> Dict[str, Any]:
    """`publish.categories` 的入参：官方**不带任何字段**（bundle `@3832720`）。"""
    return {}


def brands_data(category_id: Any) -> Dict[str, Any]:
    """`publish.brands` 的入参：`{channelCategoryId}`（分类路径**最后一级** id）。"""
    return {'channelCategoryId': _as_int(category_id)}


def models_data(category_id: Any, brand_id: Any) -> Dict[str, Any]:
    """`publish.models` 的入参：`{brandValueId, channelCategoryId}` —— 两个都必填。"""
    return {'brandValueId': _as_int(brand_id), 'channelCategoryId': _as_int(category_id)}


def property_data(category_id: Any) -> Dict[str, Any]:
    """`category.property.query` 的入参：`{categoryId}`。"""
    return {'categoryId': _as_int(category_id)}


def search_categories_data() -> Dict[str, Any]:
    """`search.categories` 的入参：官方**不带任何字段**（bundle `@3073093`）。"""
    return {}


# --------------------------------------------------------------------------- 响应解析


def envelope(raw: Any) -> Dict[str, Any]:
    """平台业务信封 `{code, msg, data}`。

    `client.mtop` 成功时返回 `{'success': True, 'data': <平台 data>}`，所以业务信封在
    `raw['data']`（与 `item_write.envelope` 同口径）；测试里的假客户端也按这个形状给。
    """
    if not isinstance(raw, dict):
        return {}
    inner = raw.get('data')
    return inner if isinstance(inner, dict) else {}


def business(raw: Any) -> Any:
    """业务体 `res.data.data`（bundle 里品牌/型号就是从这里取 `selectIdle*Values`）。"""
    return envelope(raw).get('data')


def code_of(raw: Any) -> str:
    """平台业务码 `res.data.code`（bundle 的品牌/型号成功判据是 `=== 'success'`）。"""
    return _text(envelope(raw).get('code'))


def message_of(raw: Any) -> str:
    """平台失败原文：`msg` 优先，其次 `message`；客户端层失败时退回 `raw['message']`/`ret[0]`。"""
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

    取不到就回空列表 —— **不造候选值**：服务层据此如实回「平台没返回候选值」。
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


def _id_of(row: Any, *keys: str) -> Any:
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if value not in (None, ''):
            return value
    return None


def _name_of(row: Any, *keys: str) -> str:
    if not isinstance(row, dict):
        return ''
    for key in keys:
        text = _text(row.get(key)).strip()
        if text:
            return text
    return ''


def category_options(rows: Any) -> List[Dict[str, Any]]:
    """类目树 → `[{category_id, category_name, level, leaf, children: [...]}]`。

    子节点键名来自真机/源码证据 `childrenCategoryDOs`（见 `docs/contracts/item_templates.md` §4），
    另外容错 `children` / `childrenCategories`。**层级递归**，因为发布页的类目是多级联动下拉。
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        category_id = _id_of(row, 'categoryId', 'id', 'catId', 'value')
        name = _name_of(row, 'categoryName', 'name', 'label', 'title')
        if category_id in (None, '') and not name:
            continue
        children: List[Dict[str, Any]] = []
        for key in ('childrenCategoryDOs', 'children', 'childrenCategories', 'subCategories'):
            value = row.get(key)
            if isinstance(value, list):
                children = category_options(value)
                if children:
                    break
        level = _id_of(row, 'categoryLevel', 'level')
        out.append({
            'category_id': _text(category_id) or None,
            'category_name': name or None,
            'level': level if isinstance(level, int) else None,
            'leaf': row.get('leaf') if isinstance(row.get('leaf'), bool) else None,
            'children': children,
        })
    return out


def value_options(rows: Any) -> List[Dict[str, Any]]:
    """品牌 / 型号 → `[{value, label}]`（bundle 原文映射 `{label: valueName, value: valueId}`）。

    容错：真机上若出现 `valueId`/`valueName` 之外的键名，退到 `id`/`name`；都取不到的行
    **丢弃**（宁可少一行，也不给一个点不动的空选项）。
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _id_of(row, 'valueId', 'value', 'id', 'brandValueId', 'modelValueId')
        label = _name_of(row, 'valueName', 'label', 'name', 'text')
        if value in (None, '') or not label:
            continue
        out.append({'value': _text(value), 'label': label})
    return out


def property_options(rows: Any) -> List[Dict[str, Any]]:
    """`category.property.query` 的属性候选值 → `[{property_id, property_name, values: [...]}]`。

    **未真机验证**（响应字段名没有消费侧代码可对照，见文件头），所以：
    * 顶层键名在 `list_of` 里给了 `fineness` / `propertyList` / `propertyValueList` 等候选；
    * 每个属性自己的候选值同样按多种键名找（`valueList` / `values` / `propertyValues` …）；
    * 找不到候选值就回空列表，由服务层如实说明「平台没返回属性候选值」。
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        property_id = _id_of(row, 'propertyId', 'pid', 'id', 'propertyValueId')
        name = _name_of(row, 'propertyName', 'name', 'label', 'text', 'valueName')
        values: List[Dict[str, Any]] = []
        for key in ('valueList', 'values', 'propertyValues', 'propertyValueList',
                    'optionList', 'candidates', 'valueItems'):
            found = row.get(key)
            if isinstance(found, list):
                values = value_options(found)
                if values:
                    break
        if property_id in (None, '') and not name:
            continue
        out.append({
            'property_id': _text(property_id) or None,
            'property_name': name or None,
            'values': values,
        })
    return out


def search_category_options(raw: Any) -> List[Dict[str, Any]]:
    """`search.categories` 的候选：优先 `res.data.data.selectIdleCategories`，
    再退到 `res.data.data` 本身是数组的形状（**未真机验证**）。"""
    body = business(raw)
    if isinstance(body, list):
        return category_options(body)
    rows = list_of(body, 'selectIdleCategories', 'categories', 'categoryList', 'list')
    return category_options(rows)


def _response(api: str, raw: Any, options: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统一的协议层返回体：**永远带 `api` / `code` / `options`**。

    `code` 是平台业务码（拿不到就是空串）：服务层据此判「平台说失败」，
    而 `options` 为空时它必须能分清「平台没返回」与「平台返回了空列表」。
    """
    return {
        'api': api,
        'code': code_of(raw),
        'message': message_of(raw),
        'options': options,
        'raw': raw,
    }


# --------------------------------------------------------------------------- 对外接口


async def fetch_categories(client: Any) -> Dict[str, Any]:
    """发布页类目树（无参）。"""
    raw = await client.mtop(CATEGORIES_API, None, DEFAULT_VERSION, value_type=VALUE_TYPE_STRING)
    rows = list_of(business(raw), 'selectIdleCategories', 'categories', 'categoryList', 'list')
    return _response(CATEGORIES_API, raw, category_options(rows))


async def fetch_brands(client: Any, category_id: Any) -> Dict[str, Any]:
    """按类目取品牌（`channelCategoryId` 必填）。"""
    raw = await client.mtop(BRANDS_API, brands_data(category_id), DEFAULT_VERSION,
                            value_type=VALUE_TYPE_STRING)
    rows = list_of(business(raw), 'selectIdleBrandValues', 'brandValues', 'brands', 'list')
    return _response(BRANDS_API, raw, value_options(rows))


async def fetch_models(client: Any, category_id: Any, brand_id: Any) -> Dict[str, Any]:
    """按类目 + 品牌取型号（两个入参都必填，官方任一为空就不发请求）。"""
    raw = await client.mtop(MODELS_API, models_data(category_id, brand_id), DEFAULT_VERSION,
                            value_type=VALUE_TYPE_STRING)
    rows = list_of(business(raw), 'selectIdleModelValues', 'modelValues', 'models', 'list')
    return _response(MODELS_API, raw, value_options(rows))


async def fetch_category_properties(client: Any, category_id: Any) -> Dict[str, Any]:
    """按类目取属性/成色候选值（**未真机验证**，见文件头）。"""
    raw = await client.mtop(CATEGORY_PROPERTY_API, property_data(category_id), DEFAULT_VERSION)
    rows = list_of(business(raw), 'fineness', 'propertyList', 'propertyValueList',
                   'properties', 'list')
    return _response(CATEGORY_PROPERTY_API, raw, property_options(rows))


async def fetch_search_categories(client: Any) -> Dict[str, Any]:
    """类目搜索（无参；响应形状**未真机验证**，见文件头）。"""
    raw = await client.mtop(SEARCH_CATEGORIES_API, None, DEFAULT_VERSION,
                            value_type=VALUE_TYPE_STRING)
    return _response(SEARCH_CATEGORIES_API, raw, search_category_options(raw))
