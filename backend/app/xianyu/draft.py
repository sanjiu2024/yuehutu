"""草稿箱（官方 seller-item 微应用的「商品管理 → 草稿箱」tab）：列表 / 详情 / 删除。

**为什么单独开一个模块**：草稿箱在官方卖家工作台里**不是菜单页**，它挂在「商品发布 /
商品管理」的流程内（发布一半的东西存在草稿箱里，官方列表页第二个 tab 就是它）。它走的
是 `mtop.alibaba.idle.item.pc.draft.*` 一族，与商品写接口（`...seller.pc.item.*`，见
`xianyu/item_write.py`）**不是同一族**，字段形状也不同（草稿用 `draftId`，商品用 `itemId`）。

**证据来源**：官方 bundle `https://g.alicdn.com/idle-pc/seller-item/0.0.20/js/main.js`
的静态提取（只读 alicdn，**零平台请求**）。bundle 是 4,402,065 字符的单行压缩 JS，所以证据
用**字符偏移**而不是行号，偏移与调用点原文见每条 `evidence`；脚本
`C:\\Users\\Administrator\\.dsh-tmp\\{draft_evidence3.py,draft_fields.py,draft_fields2.py}`，
报告 `logs/draft_evidence3.txt`。

**逐字证据**（调用点原文）：
- 列表：`x0 = function(e){ return new Promise((t,n) => gC({api:'…draft.search', v:'1.0',
  data:{...e}, showErrorToast:!0, needLoginPC:!0}).then(e => t({data:
  e?.data?.data?.itemSearchResponseList || [], total: e?.data?.data?.total})) …)}`；
  调用点 `(2 === C.current?.type ? x0 : w0)(s)` —— **tab 类型为 2（草稿）时用草稿搜索**，
  否则用商品搜索；`s = {pageNo: current||1, pageSize: size||20}`，草稿 tab 还会把过滤表单
  的值**平铺**进来（`{...s, ...F.current}`），条数选项草稿 tab 是 `[10,20,50]`。
- 详情：`gC({api:'…draft.get', v:'1.0', data:{draftId: e, sourceId: PC.getState().sourceId}})`
  → `lC(e.data)` → `setFieldsValue(t)`；**同一个 `lC` 也用于商品详情**（@3063009），所以它是
  「平台 payload → 表单初值」的公共解析器，我们**不能替它猜键名**，只能原样透传。
- 单删：包装 `wC(e)` → `{draftId: e, sourceId}`。
- 批删：包装 `q0` → 调用点 `q0({draftIds: T.join(",")})`，成功后读 `e.data.sucCount` /
  `e.data.failCount`（文案「N个草稿删除成功」「M个删除失败」）——**逗号串**，不是数组。

**故意不实现**：`draft.update`（新建/保存草稿）。理由：它是把整份发布表单塞进
`inputJson`，而表单字段形状没有证据（`lC` 是压缩后的黑盒），猜一个字段名就等于往用户账号里
写脏草稿。登记在 `NOT_IMPLEMENTED` 里，不是漏了。

**未验证边界**：一次真机请求都没发过（本机账号只有 `2217970177348` 在线，且它没有部分平台的
业务权限）。字段形状与官方页面逐字一致，但「平台是否接受」仍属未验证。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 草稿列表（bundle @3072333 声明 / @4079050 调用点 `x0({pageNo,pageSize,...过滤表单})`）。
SEARCH_API = 'mtop.alibaba.idle.item.pc.draft.search'
#: 草稿详情（bundle @3063462 调用点 `{draftId, sourceId}` → `lC(e.data)`）。
GET_API = 'mtop.alibaba.idle.item.pc.draft.get'
#: 保存 / 更新草稿（bundle @1805953 包装 `_C(e,t)`）—— **故意不实现**，见模块 docstring。
UPDATE_API = 'mtop.alibaba.idle.item.pc.draft.update'
#: 单条删除（bundle @1806143 包装 `wC(e)`）。
DELETE_API = 'mtop.alibaba.idle.item.pc.draft.delete'
#: 批量删除（bundle @3080157 包装 `q0` / 调用点 @4098620 `q0({draftIds: T.join(",")})`）。
BATCH_DELETE_API = 'mtop.alibaba.idle.idle.pc.draft.batch.delete'

#: 版本号（bundle 原文全是 `v:"1.0"`；`draft.update` 那条没写 v，我们不实现它）。
DEFAULT_VERSION = '1.0'

#: 官方草稿 tab 的每页条数选项（bundle @4079050 的三元：草稿 `[10,20,50]` / 商品 `[10,20]`）。
PAGE_SIZES: Tuple[int, ...] = (10, 20, 50)
MAX_PAGE_SIZE = PAGE_SIZES[-1]

#: 只读接口登记（`sourceId` 是页面级状态 `PC.getState().sourceId`，我们没有它也不发）。
READ_APIS: Tuple[str, ...] = (SEARCH_API, GET_API)

#: 写能力登记。`data` 是真实字段形状，`evidence` 是 bundle 字符偏移 + 调用点原文片段。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'delete': {
        'api': DELETE_API,
        'data': '{draftId}',
        'evidence': 'bundle @1806143 `wC(e)` → `{draftId:e, sourceId}`',
    },
    'batch_delete': {
        'api': BATCH_DELETE_API,
        'data': '{draftIds(逗号串)}',
        'evidence': 'bundle @4098620 `q0({draftIds:T.join(",")})` → 读 `sucCount`/`failCount`',
    },
}

#: 本模块登记的**写**接口集合（`assert_write` 用它挡住误用）。
WRITE_API_SET = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 本模块登记的全部接口（读 + 写，不含故意不实现的 `draft.update`）。
ALL_APIS = WRITE_API_SET | frozenset(READ_APIS)

#: 明确**不实现**的相邻接口（证据不足 ⇒ 宁可只读，也不猜字段往用户账号里写）。
NOT_IMPLEMENTED: Dict[str, str] = {
    UPDATE_API:
        '保存 / 更新草稿：bundle @1805953 包装 `_C(e,t)` 的 payload 是 '
        '`{inputJson: JSON.stringify({...整份发布表单, draftId, sourceId, uniqueCode})}`，'
        '而发布表单的字段形状是压缩黑盒 `lC` 的逆运算、没有证据 ⇒ 不猜字段名。',
}


class UnknownApi(RuntimeError):
    """用了没登记的草稿接口 —— 直接拒绝，避免「悄悄发了个没证据的请求」。"""


class WriteNotConfirmed(RuntimeError):
    """写请求没带确认 —— 协议层最后一道守卫（服务层已经判过一次）。"""


def assert_write(api: str) -> str:
    """守卫：只允许本模块登记的两个写接口通过。"""
    if api not in WRITE_API_SET:
        raise UnknownApi(f'未登记的草稿写接口: {api}')
    return api


def assert_read(api: str) -> str:
    """守卫：只允许本模块登记的两个只读接口通过。"""
    if api not in READ_APIS:
        raise UnknownApi(f'未登记的草稿只读接口: {api}')
    return api


# --------------------------------------------------------------------------- 纯函数（可单测）


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _need_confirm(confirm: Any) -> None:
    """`confirm` 必须是**字面 True**（照 `services/item_templates.py` 的口径）。"""
    if confirm is not True:
        raise WriteNotConfirmed('草稿写请求必须显式 confirm=True')


def _need_ids(*pairs: Tuple[str, Any]) -> None:
    """协议层最后一道空值守卫（服务层已经判过一次，这里是防「绕过服务层直接调协议」）。"""
    for label, value in pairs:
        if not _text(value):
            raise ValueError(f'{label}不能为空')


def normalize_draft_ids(draft_ids: Any) -> str:
    """`draftIds` 在官方 bundle 里是**逗号分隔字符串**（`T.join(",")`，不是数组）。

    收串就原样（去两端空白 + 去空项），收 list/tuple/set 就拼；两种入参都收，因为
    HTTP 面 UI 传数组、AI / MCP 面传逗号串（与 `services/items.py` 的 `_clean_ids` 同口径）。
    """
    if isinstance(draft_ids, str):
        parts = [p.strip() for p in draft_ids.split(',')]
    elif isinstance(draft_ids, (list, tuple, set)):
        parts = [_text(x) for x in draft_ids]
    else:
        return ''
    return ','.join(p for p in parts if p)


def draft_id_list(draft_ids: Any) -> List[str]:
    """把两种入参统一成 id 列表（去重且保持顺序），便于逐条记日志。"""
    out: List[str] = []
    for value in normalize_draft_ids(draft_ids).split(','):
        if value and value not in out:
            out.append(value)
    return out


def list_request(page: Any = 1, size: Any = 20) -> Tuple[str, Dict[str, Any]]:
    """草稿列表（bundle @4079050）：`{pageNo, pageSize}`，`pageNo` **从 1 起**。

    官方还会把过滤表单的值平铺进同一个对象；我们**没有钉住那张表单的字段名**，
    所以只发分页参数（不猜字段）。
    """
    try:
        page_no = int(page)
    except (TypeError, ValueError):
        page_no = 1
    try:
        page_size = int(size)
    except (TypeError, ValueError):
        page_size = PAGE_SIZES[1]
    if page_no < 1:
        page_no = 1
    if page_size not in PAGE_SIZES:
        raise ValueError(f'每页条数只支持 {list(PAGE_SIZES)}，收到 {size}')
    return SEARCH_API, {'pageNo': page_no, 'pageSize': page_size}


def detail_request(draft_id: Any) -> Tuple[str, Dict[str, Any]]:
    """草稿详情（bundle @3063462）：`{draftId}`。"""
    _need_ids(('草稿 ID', draft_id))
    return GET_API, {'draftId': _text(draft_id)}


def delete_request(draft_id: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """单条删除（bundle @1806143）：`{draftId}`。"""
    _need_confirm(confirm)
    _need_ids(('草稿 ID', draft_id))
    return DELETE_API, {'draftId': _text(draft_id)}


def batch_delete_request(draft_ids: Any, *, confirm: Any = None) -> Tuple[str, Dict[str, Any]]:
    """批量删除（bundle @4098620）：`{draftIds}` 是**逗号串**。"""
    _need_confirm(confirm)
    joined = normalize_draft_ids(draft_ids)
    if not joined:
        raise ValueError('草稿 ID 不能为空')
    return BATCH_DELETE_API, {'draftIds': joined}


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any]]:
    """按动作名取请求（`WRITE_APIS` 的键），便于服务层统一分发与单测。"""
    if action == 'delete':
        return delete_request(kwargs.get('draft_id'), confirm=kwargs.get('confirm'))
    if action == 'batch_delete':
        return batch_delete_request(kwargs.get('draft_ids'), confirm=kwargs.get('confirm'))
    raise UnknownApi(f'未知的草稿写动作: {action}')


# --------------------------------------------------------------------------- 响应解析
#
# 约定与 `xianyu/item_write.py` 一致：解析函数收的是**客户端返回值**
# `{'success': bool, 'data': <平台 JSON body>, 'message': str, 'ret': [...]}`，
# 平台业务信封 `{code, msg, data}` 在 `raw['data']`。


def envelope(raw: Any) -> Dict[str, Any]:
    """平台业务信封 `{code, msg, data}`。"""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get('data')
    return inner if isinstance(inner, dict) else {}


def business(raw: Any) -> Any:
    """业务体 `res.data.data`。"""
    return envelope(raw).get('data')


def code_of(raw: Any) -> str:
    return _text(envelope(raw).get('code'))


def message_of(raw: Any) -> str:
    """平台失败原文：`msg` → `message` → 客户端层 `message` / `ret[0]`。

    客户端层自己失败时（`mtop` 回 `{'success': False, 'ret': [...]}`）平台信封是空的，
    不退这一步用户只会看到「平台没有返回成功」而看不到真正原因。
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
    """成功判据。

    与 `item_write` 不同：草稿**列表为空是正常结果**（一件草稿都没有 ⇒ 空数组 +
    `total: 0`），所以不能用「业务体为真」当判据，否则空草稿箱会被判成失败。
    规则：客户端没报错，且平台 `code` 是 `success`（没给 code 时看有没有业务体）。
    """
    if isinstance(raw, dict) and raw.get('success') is False:
        return False
    env = envelope(raw)
    if not env:
        return False
    code = code_of(raw)
    if code:
        return code == 'success'
    return 'data' in env


def failure_reason(raw: Any, fallback: str = '') -> str:
    """失败原因：平台 `msg` 原文优先，没有再用兜底文案。"""
    return message_of(raw) or fallback


def _entry(raw_item: Any) -> Dict[str, Any]:
    """把平台草稿行映射成我们的字段名。**拿不到的一律 None**，不造 0 / 空串。

    键来自官方表格的 `dataIndex` / render 读法（逐字取证）：`draftId`（rowKey）、
    `title`、`price` / `maxPrice`、`quantity`、`gmtCreateStr`（草稿用带 Str 的那个）、
    `imageInfoDOList[0].url`（条件：`type===2` 时官方读的就是它）。
    """
    if not isinstance(raw_item, dict):
        return {}
    images = raw_item.get('imageInfoDOList')
    image_url = None
    if isinstance(images, list) and images and isinstance(images[0], dict):
        image_url = _text(images[0].get('url')) or None
    created = raw_item.get('gmtCreateStr') or raw_item.get('gmtCreate')
    return {
        'draft_id': _text(raw_item.get('draftId')) or None,
        'title': _text(raw_item.get('title')) or None,
        'image_url': image_url,
        'price': raw_item.get('price'),
        'max_price': raw_item.get('maxPrice'),
        'quantity': raw_item.get('quantity'),
        'created_at': _text(created) or None,
    }


def parse_list(raw: Any) -> Dict[str, Any]:
    """列表解析：业务体的 `itemSearchResponseList` + `total`（bundle 逐字）。

    `found_key` 表明平台到底有没有给 `itemSearchResponseList` 这个键 —— 服务层据此决定
    要不要加「这可能是空列表而不是没有草稿」的 warning。
    """
    body = business(raw)
    body_dict = body if isinstance(body, dict) else {}
    entries = body_dict.get('itemSearchResponseList')
    found = isinstance(entries, list)
    items: List[Dict[str, Any]] = []
    if found:
        items = [_entry(x) for x in entries if isinstance(x, dict)]
    total = body_dict.get('total')
    return {
        'items': items,
        'total': total if isinstance(total, int) else None,
        'found_key': found,
    }


def parse_detail(raw: Any) -> Dict[str, Any]:
    """详情解析：**键名一律不猜、不翻译**，业务体原样透传。

    官方用压缩黑盒 `lC(e.data)` 把平台 payload 转成表单初值，我们没有它的逆函数，
    所以 `fields` 就是平台给的那个对象（拿不到回 `None`）。
    """
    body = business(raw)
    return {'fields': body if isinstance(body, dict) else None}


def parse_process(raw: Any) -> Dict[str, Any]:
    """批量删除的结果：`res.data.sucCount` / `res.data.failCount`（bundle 逐字）。

    拿不到回 `None` —— `0` 与「平台没给」是两回事。
    """
    body = business(raw)
    body_dict = body if isinstance(body, dict) else {}
    suc = body_dict.get('sucCount')
    fail = body_dict.get('failCount')
    return {
        'suc_count': suc if isinstance(suc, int) else None,
        'fail_count': fail if isinstance(fail, int) else None,
    }


#: 动作名登记（服务层 / AI 工具层共用的稳定名字）。
ACTIONS: Sequence[str] = tuple(WRITE_APIS.keys())
