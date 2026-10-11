"""商品写接口（官方 seller-item 微应用的「商品管理」页）：下架 / 删除 / 快捷改价改库存。

**为什么单独开一个模块**：`xianyu/client.py` 里的 `downshelf` 走的是
`mtop.taobao.idle.item.downshelf` **v2.0**（老族），`reshelf` 根本没有独立接口、
靠整份原表单回提 `itemStatus`。而官方卖家工作台的「商品管理」页用的是
`mtop.alibaba.idle.seller.pc.item.*` 这一族，能力更全：

- `offline`：单个下架（`{itemId}`）
- `batch.offline`：**一次请求下架一批**（`{itemIds: '逗号串'}`）——老路子是 N 次
  `downshelf`，N 次风控面，这里换成 1 次
- `batch.delete`：**平台侧真删除**（`{itemIds: '逗号串'}`）——老路子只删本地记录
- `info.update`：**直接改价 / 改库存**（`{itemId, price, quantity}`），不整份回提表单
  ⇒ 对已卖出商品也没有「被重新上架」的副作用（这是整份回提的真实缺陷）

**证据来源**：官方 bundle `https://g.alicdn.com/idle-pc/seller-item/0.0.20/js/main.js`
的静态提取（只读 alicdn，**零平台请求**）。bundle 是单行压缩 JS，所以证据用**字符偏移**
而不是行号，偏移与调用点原文见 `WRITE_APIS` 的 `evidence` 字段，完整报告见
`C:\\Users\\Administrator\\.dsh-tmp\\adapp\\item_write_evidence.md`。

**未验证边界**：一次真机写请求都没发过（本机两个账号登录态都已过期，红线不允许为了
验证去改平台数据）。所以「平台是否接受这套字段」仍属未验证，只能保证字段形状与官方
页面逐字一致。

**包装**：四个接口在 bundle 里都走同一个 `gC({api, v:'1.0', data, showErrorToast, needLoginPC})`，
业务字段**直接放 `data`**（不是 `requestJson` 包装），所以这里全部返回 `(api, data)` 二元组，
由服务层统一 `client.mtop(api, data, v='1.0')` 发出去。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 单个下架（bundle @3078164 声明 / @4084449 调用点 `z0({itemId:r})`）。
OFFLINE_API = 'mtop.alibaba.idle.seller.pc.item.offline'
#: 批量下架（bundle @3078561 声明 / @4097558 调用点 `H0({itemIds:T.join(",")})`）。
BATCH_OFFLINE_API = 'mtop.alibaba.idle.seller.pc.item.batch.offline'
#: 批量删除（bundle @3079755 声明 / @4099090 调用点 `W0({itemIds:T.join(",")})`）。
BATCH_DELETE_API = 'mtop.alibaba.idle.seller.pc.item.batch.delete'
#: 快捷改价 / 改库存（bundle @3076546 声明 / @4100255 调用点
#: `N0({itemId, quantity, price})`，有 SKU 时改发 `{itemId, itemSkuListStr}`）。
INFO_UPDATE_API = 'mtop.alibaba.idle.seller.pc.item.info.update'

#: 四个接口的版本号（bundle 原文全是 `v:"1.0"`）。
DEFAULT_VERSION = '1.0'

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 字符偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'offline': {
        'api': OFFLINE_API,
        'data': '{itemId}',
        'evidence': 'bundle @3078164 声明 / @4084449 `z0({itemId:r})`',
    },
    'batch_offline': {
        'api': BATCH_OFFLINE_API,
        'data': '{itemIds(逗号串)}',
        'evidence': 'bundle @3078561 声明 / @4097558 `H0({itemIds:T.join(",")})`',
    },
    'batch_delete': {
        'api': BATCH_DELETE_API,
        'data': '{itemIds(逗号串)}',
        'evidence': 'bundle @3079755 声明 / @4099090 `W0({itemIds:T.join(",")})`',
    },
    'quick_update': {
        'api': INFO_UPDATE_API,
        'data': '{itemId, price?, quantity?}（有 SKU 时是 {itemId, itemSkuListStr}）',
        'evidence': 'bundle @3076546 声明 / @4100255 `N0({itemId, quantity, price})`',
    },
}

#: 本模块**只登记写接口**，全部都在这里（`assert_write` 用它挡住误用）。
ALL_APIS = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 明确**不实现**的相邻接口（证据不足：在本 bundle 里调用点 0，属死代码）。
#: 登记出来是为了让后来者知道「不是漏了，是故意不做」。
NOT_IMPLEMENTED: Dict[str, str] = {
    'mtop.alibaba.idle.seller.pc.item.delete':
        '单个删除：bundle @3078964 声明，但调用点 0（官方页面只提供批量删除）',
    'mtop.alibaba.idle.seller.platform.item.operate.online':
        '上架：bundle @3077754 声明，但调用点 0 —— 官方商品管理页不提供上架，'
        '上架走编辑/发布流程（沿用 client.reshelf）',
    'mtop.alibaba.idle.seller.pc.item.price.update':
        'bundle @3077352 声明，调用点 0；官方用的是 info.update',
    'mtop.alibaba.idle.seller.pc.item.quantity.update':
        'bundle @3076947 声明，调用点 0；官方用的是 info.update',
}


class UnknownApi(RuntimeError):
    """用了没登记的商品写接口 —— 直接拒绝，避免「悄悄发了个没证据的请求」。"""


def assert_write(api: str) -> str:
    """守卫：只允许本模块登记的四个写接口通过。"""
    if api not in ALL_APIS:
        raise UnknownApi(f'未登记的商品写接口: {api}')
    return api


# --------------------------------------------------------------------------- 纯函数（可单测）


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def normalize_item_ids(item_ids: Any) -> str:
    """`itemIds` 在官方 bundle 里是**逗号分隔字符串**（不是数组）。

    收串就原样（去掉两端空白），收 list/tuple/set 就拼；空项丢弃。
    """
    if isinstance(item_ids, str):
        return item_ids.strip()
    if isinstance(item_ids, (list, tuple, set)):
        return ','.join(_text(x) for x in item_ids if _text(x))
    return ''


def item_id_list(item_ids: Any) -> List[str]:
    """把两种入参（逗号串 / 数组）统一成 id 列表，便于逐条记日志。"""
    return [x for x in normalize_item_ids(item_ids).split(',') if x]


def offline_request(item_id: Any) -> Tuple[str, Dict[str, Any]]:
    """单个下架。`itemId` 是单个字符串（bundle @4084449）。"""
    return OFFLINE_API, {'itemId': _text(item_id)}


def batch_offline_request(item_ids: Any) -> Tuple[str, Dict[str, Any]]:
    """批量下架。**逗号拼接**后放 `itemIds`（bundle @4097558）。"""
    return BATCH_OFFLINE_API, {'itemIds': normalize_item_ids(item_ids)}


def batch_delete_request(item_ids: Any) -> Tuple[str, Dict[str, Any]]:
    """批量删除。同上（bundle @4099090）。"""
    return BATCH_DELETE_API, {'itemIds': normalize_item_ids(item_ids)}


def quick_update_request(item_id: Any, price: Any = None, quantity: Any = None,
                         sku_list_str: Any = None) -> Tuple[str, Dict[str, Any]]:
    """快捷改价 / 改库存（bundle @4100255）。

    - 无 SKU：`{itemId, price, quantity}`，**空的那个字段不发**（官方是 `undefined` 不是空串）
    - 有 SKU：`{itemId, itemSkuListStr}`（JSON 字符串），与 price/quantity 互斥
    """
    data: Dict[str, Any] = {'itemId': _text(item_id)}
    if sku_list_str not in (None, ''):
        data['itemSkuListStr'] = sku_list_str
        return INFO_UPDATE_API, data
    if price is not None:
        data['price'] = price
    if quantity is not None:
        data['quantity'] = quantity
    return INFO_UPDATE_API, data


# --------------------------------------------------------------------------- 响应解析


def envelope(raw: Any) -> Dict[str, Any]:
    """平台业务信封 `{code, msg, data}`。

    `client.mtop` 成功时返回 `{'success': True, 'data': <平台 data>}`，所以业务信封在
    `raw['data']`；测试里的假客户端也按这个形状给。
    """
    if not isinstance(raw, dict):
        return {}
    inner = raw.get('data')
    return inner if isinstance(inner, dict) else {}


def business(raw: Any) -> Any:
    """业务体 `res.data.data`（官方 bundle 的成功判据就是它真不真）。"""
    return envelope(raw).get('data')


def code_of(raw: Any) -> str:
    return _text(envelope(raw).get('code'))


def message_of(raw: Any) -> str:
    """平台失败原文：`msg` 优先，其次 `message`。

    客户端层自己失败时（`mtop` 回 `{'success': False, 'message': …, 'ret': [...]}`）
    平台信封是空的，这时退回客户端的 `message` / `ret[0]` —— 否则用户只会看到
    「平台没有返回成功」而看不到真正原因。
    """
    env = envelope(raw)
    text = _text(env.get('msg')) or _text(env.get('message'))
    if text:
        return text
    if isinstance(raw, dict) and raw.get('success') is False:
        ret = raw.get('ret')
        first = ret[0] if isinstance(ret, list) and ret else ''
        return _text(raw.get('message')) or _text(first)
    return ''


def is_success(raw: Any) -> bool:
    """成功判据（逐字照抄 bundle）：`res.data.data` 为真。

    空 dict / 空 list / None / False 都算失败 —— 这三个接口的业务体成功时
    一定带东西（下架回对象、批量回 `itemProcessResultList`、改价回更新后的值）。
    客户端层报错（`raw['success'] is False`）也一律算失败。
    """
    if isinstance(raw, dict) and raw.get('success') is False:
        return False
    return bool(business(raw))


def failure_reason(raw: Any, fallback: str = '') -> str:
    """失败原因：平台 `msg` 原文优先，没有再用兜底文案。"""
    return message_of(raw) or fallback


def process_results(raw: Any) -> List[Dict[str, Any]]:
    """批量接口的逐条结果 `res.data.data.itemProcessResultList`（bundle 原文键名）。

    拿不到就回空列表（**不造假的成功条目**）。
    """
    body = business(raw)
    if not isinstance(body, dict):
        return []
    items = body.get('itemProcessResultList')
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def count_process_results(raw: Any) -> Tuple[int, int]:
    """(成功条数, 失败条数) —— 官方页面就是这么数的（`filter(r => r.success)`）。"""
    results = process_results(raw)
    ok = sum(1 for r in results if r.get('success'))
    return ok, len(results) - ok


def process_failures(raw: Any) -> List[Dict[str, Any]]:
    """逐条失败明细，形状对齐服务层的 `failed[]`：`{id, code, message}`。"""
    out: List[Dict[str, Any]] = []
    for entry in process_results(raw):
        if entry.get('success'):
            continue
        item_id = entry.get('itemId') or entry.get('id') or ''
        out.append({
            'id': _text(item_id),
            'code': _text(entry.get('errorCode') or entry.get('code')) or 'platform_rejected',
            'message': _text(entry.get('errorMsg') or entry.get('message') or entry.get('msg'))
                       or '平台没有给出原因',
        })
    return out


def updated_values(raw: Any) -> Dict[str, Any]:
    """`info.update` 成功后回读业务体里的价格 / 库存（拿不到就不放键，**不造 0**）。"""
    body = business(raw)
    out: Dict[str, Any] = {}
    if isinstance(body, dict):
        for key in ('price', 'quantity', 'stock'):
            if body.get(key) is not None:
                out[key] = body[key]
    return out


def failure_count(raw: Any) -> Optional[int]:
    """平台自报的失败条数（`res.data.failCount`，批量删除那条 bundle 读过它）。

    拿不到回 `None` —— `0` 与「平台没给」是两回事。
    """
    body = business(raw)
    if isinstance(body, dict):
        value = body.get('failCount')
        if isinstance(value, int):
            return value
    return None


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名取请求（`WRITE_APIS` 的键），便于服务层统一分发与单测。"""
    if action == 'offline':
        return offline_request(kwargs.get('item_id'))
    if action == 'batch_offline':
        return batch_offline_request(kwargs.get('item_ids'))
    if action == 'batch_delete':
        return batch_delete_request(kwargs.get('item_ids'))
    if action == 'quick_update':
        return quick_update_request(kwargs.get('item_id'), kwargs.get('price'),
                                    kwargs.get('quantity'), kwargs.get('sku_list_str'))
    raise UnknownApi(f'未知的商品写动作: {action}')


#: 动作名登记（服务层 / AI 工具层共用的稳定名字）。
ACTIONS: Sequence[str] = tuple(WRITE_APIS.keys())
