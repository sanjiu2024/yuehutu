# -*- coding: utf-8 -*-
"""粉丝价（官方商品管理页的「粉丝价」列）：读当前三组配置 + 整份写回。

**接口来源（静态 bundle 反解，零风控）**：官方卖家工作台「商品管理」微应用 bundle
`https://g.alicdn.com/idle-pc/seller-item/…/item-main.js`（4,402,065 B，单行压缩 JS）。
本地副本 `C:\\Users\\Administrator\\.dsh-tmp\\adapp\\item-main.js`，**证据用字符偏移**。

**它是什么**：给粉丝的专属价，分三组（官方列名逐字：`全部粉-粉丝价` / `老粉-粉丝价` /
`已购粉-粉丝价`，对应 `fansGroup ∈ 'all' | 'old' | 'buy'`）。

**两个接口（都是 v1.0，包装 `gC({api, v:'1.0', data:{...e}, showErrorToast:true})`）**：
* 读 `…platform.item.operate.fansprice.get`（bundle @3080558 名字、@3080828 前一个包装 `G0`）：
  `data:{itemId}`；官方判成功看 `resp.data.code === 'success'`，
  配置在 `resp.data.data.fansPriceConfig.fansPriceList`，元素 `{fansGroup, priceString}`
  （@4086800 的读取调用点：`fansPriceList.forEach(e => b[e.fansGroup] = e.priceString)`）。
* 写 `…platform.item.operate.fansprice.set`（@3080960 名字、@3080828 包装 `K0`）：
  官方载荷**逐字**（@4101069 调用点）：
  `o = {itemId, fansPriceList}` → `i = {itemId, fansPriceConfig: o}` → `K0(i)`，
  即 `data:{itemId, fansPriceConfig:{itemId, fansPriceList:[{fansGroup, priceString}]}}`；
  官方判成功看 `resp.data.data` 是否为真（成功 toast「编辑成功」，失败读 `resp.data.msg`
  兜底「编辑失败，请重试」）。

**★ 语义是「整份替换」而不是「按组增量」**：官方弹窗把当前三组值预填，保存时三组一起发；
`fansPriceList` 里没有的组就等于「没配置」⇒ **某个组留空 = 清空该组**。
所以前端必须先读再预填，确认框必须列出将要提交的完整列表。本模块不替调用方补默认值。

**官方本地校验（@4101083/@4100935，文案逐字）**：小数位 > 2 →「粉丝价最多为2位小数，请修改」；
等于 0 →「粉丝价必须大于0，请修改」；列定义 `min: .01`、`max: 99999999.99`。
我们照这个口径做校验，但**把 `0` 的判定做对**（官方用 `0 === e[o]` 严格比数字，
字符串 `'0'` 反而漏过 —— 这是官方的洞，我们不留）。

**未验证边界**：写接口一次真机请求都没发过（写验证的红线是「零平台写请求」）。
参数形状、成功判据、整份替换语义全部来自 bundle 静态证据。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .trade import body_of, envelope_error, envelope_message

# --------------------------------------------------------------------------- 接口常量

#: 读当前粉丝价（只读）。bundle @3080558：`{api:"…fansprice.get", v:"1.0", data:{itemId}}`。
GET_API = 'mtop.alibaba.idle.seller.platform.item.operate.fansprice.get'

#: 整份写回粉丝价（写）。bundle @3080960 + 调用点 @4101069。
SET_API = 'mtop.alibaba.idle.seller.platform.item.operate.fansprice.set'

#: 这一族两个接口都是 1.0。
DEFAULT_VERSION = '1.0'

#: 三个粉丝组（顺序就是官方列顺序：全部粉 → 老粉 → 已购粉）。
FANS_GROUPS: Tuple[str, ...] = ('all', 'old', 'buy')

#: 官方列名（`title` 字段逐字，用于人话文案）。
GROUP_LABELS: Dict[str, str] = {
    'all': '全部粉',
    'old': '老粉',
    'buy': '已购粉',
}

#: 官方列定义里的价格区间（bundle @4069656 起的三个 `editable` 列）。
PRICE_MIN = Decimal('0.01')
PRICE_MAX = Decimal('99999999.99')

#: 官方校验：最多 2 位小数。
PRICE_DECIMALS = 2

#: 只认十进制写法（`9` / `9.9` / `9.99` / `.9`）；`1e3`、`9.`、`+9` 一律算「不是数字」。
#: **小数位数故意不在这里卡** —— 交给下面的 `PRICE_DECIMALS` 检查，
#: 这样 `9.999` 得到的是「最多为2位小数」而不是笼统的「必须是数字」。
_PRICE_RE = re.compile(r'^(\d+(\.\d+)?|\.\d+)$')

#: 商品 id 长度上限（防呆）。
ITEM_ID_MAX = 64

#: 写能力登记。`data` 是真实字段形状，`version` 是该接口版本，`evidence` 是偏移 + 原文要点。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'fans_price_set': {
        'api': SET_API,
        'version': DEFAULT_VERSION,
        'data': "{itemId, fansPriceConfig:{itemId, fansPriceList:[{fansGroup, priceString}]}}",
        'evidence': 'bundle @4101069 调用点（o={itemId,fansPriceList} → i={itemId,fansPriceConfig:o}）'
                    ' + @3080828 包装 K0',
    },
}

#: 只读接口登记。
READ_APIS: Tuple[str, ...] = (GET_API,)

#: 接口 → 版本。
READ_VERSIONS: Dict[str, str] = {GET_API: DEFAULT_VERSION}
WRITE_VERSIONS: Dict[str, str] = {SET_API: DEFAULT_VERSION}

#: **写接口白名单**：`assert_write` 只认这里面的 api。
#: 不用 `ALL_APIS - READ_APIS` 那种算法 —— 以后往 `ALL_APIS` 里加一个既非读也非写的
#: 接口时，那种算法会把它当写放行（`sub_account.py` 上真栽过）。
WRITE_API_SET = frozenset(entry['api'] for entry in WRITE_APIS.values())

#: 本模块认识的全部接口。
ALL_APIS: Tuple[str, ...] = (GET_API, SET_API)

#: 写动作名（服务层用它做「动作 → 构造器」的映射，也是审计名的一部分）。
ACTIONS: Tuple[str, ...] = tuple(WRITE_APIS)

#: 平台字段别名（官方用 camelCase；别名是为了兼容以后可能改名的返回）。
CONFIG_KEYS: Tuple[str, ...] = ('fansPriceConfig', 'fans_price_config', 'config')
LIST_KEYS: Tuple[str, ...] = ('fansPriceList', 'fans_price_list', 'list')
GROUP_KEYS: Tuple[str, ...] = ('fansGroup', 'fans_group', 'group')
PRICE_KEYS: Tuple[str, ...] = ('priceString', 'price_string', 'price')


class UnknownApi(Exception):
    """动作名不在本模块的写动作表里。"""


class WriteNotConfirmed(Exception):
    """写操作没拿到严格的 `confirm is True`。"""


def assert_write(api: Any) -> str:
    """校验「这个 api 允许写」，返回 api 本身（服务层拿它去查版本）。"""
    name = str(api or '').strip()
    if name not in WRITE_API_SET:
        raise UnknownApi(f'不是本模块的写接口：{api}')
    return name


def assert_read(api: Any) -> str:
    """校验「这个 api 允许读」，返回 api 本身。"""
    name = str(api or '').strip()
    if name not in READ_APIS:
        raise UnknownApi(f'不是本模块的读接口：{api}')
    return name


def _need_confirm(confirm: Any) -> None:
    """写闸门：**只有 `confirm is True` 才算确认**（`'true'`/`1`/`'yes'` 一律不算）。

    判断权在服务层（服务层会先用 `_truthy` 归一成 `True` 再传进来），这里只做最后一道硬闸。
    """
    if confirm is not True:
        raise WriteNotConfirmed('写操作必须显式传 confirm=true')


# --------------------------------------------------------------------------- 取值与校验


def _text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _item_id(value: Any) -> str:
    item_id = _text(value).strip()
    if not item_id:
        raise ValueError('商品 id不能为空')
    if len(item_id) > ITEM_ID_MAX:
        raise ValueError(f'商品 id 太长（上限 {ITEM_ID_MAX}）')
    return item_id


def price_problem(value: Any) -> Optional[str]:
    """价格是否合法；合法返回 `None`，不合法返回**给用户看的中文原话**。

    空值不算问题（空 = 这一组不提交，也就是清空该组 —— 由调用方在确认框里讲清楚）。
    只认十进制写法（`9` / `9.9` / `9.99` / `.9`）：`1e3`、`9.`、`+9` 这类虽然
    `Decimal()` 能算，但原样发给平台不一定是它要的东西 —— 官方那个输入框是
    InputNumber，拿到的一直是十进制数字串。
    """
    text = _text(value).strip()
    if not text:
        return None
    if not _PRICE_RE.match(text):
        return '粉丝价必须是数字'
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return '粉丝价必须是数字'
    if not amount.is_finite():
        return '粉丝价必须是数字'
    if amount <= 0:
        return '粉丝价必须大于0，请修改'
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > PRICE_DECIMALS:
        return '粉丝价最多为2位小数，请修改'
    if amount > PRICE_MAX:
        return f'粉丝价不能超过 {PRICE_MAX}'
    return None


def normalize_groups(groups: Any) -> List[Dict[str, str]]:
    """把三种入参形状归一成官方的 `fansPriceList`：`[{fansGroup, priceString}, …]`。

    接受：
    * 字典 `{'all': '9.9', 'old': '', 'buy': None}`（前端主要走这条；空值 = 不提交这一组）
    * 列表 `[{'fans_group': 'all', 'price_string': '9.9'}]`（也接受官方 camelCase 键）

    顺序固定按 `FANS_GROUPS`（全部粉 → 老粉 → 已购粉）。组名非法或价格不合法都抛
    `ValueError`（中文原话直接给用户看）。返回空列表时由调用方决定怎么报错。
    """
    entries: Dict[str, str] = {}
    if isinstance(groups, dict):
        for key, value in groups.items():
            name = _text(key).strip()
            if name not in FANS_GROUPS:
                raise ValueError(f'不认识的粉丝组：{key}（只能是 all / old / buy）')
            text = _text(value).strip()
            if not text:
                continue
            problem = price_problem(text)
            if problem:
                raise ValueError(f'{GROUP_LABELS[name]}：{problem}')
            entries[name] = text
    elif isinstance(groups, (list, tuple)):
        for raw in groups:
            if not isinstance(raw, dict):
                raise ValueError('粉丝价列表里每一项都得是对象')
            name = ''
            for key in GROUP_KEYS:
                if key in raw:
                    name = _text(raw.get(key)).strip()
                    break
            if name not in FANS_GROUPS:
                raise ValueError(f'不认识的粉丝组：{name or raw}（只能是 all / old / buy）')
            text = ''
            for key in PRICE_KEYS:
                if key in raw:
                    text = _text(raw.get(key)).strip()
                    break
            if not text:
                continue
            problem = price_problem(text)
            if problem:
                raise ValueError(f'{GROUP_LABELS[name]}：{problem}')
            entries[name] = text
    elif groups is None:
        return []
    else:
        raise ValueError('粉丝价格式不对：要么给 groups 字典，要么给 prices 列表')

    return [{'fansGroup': name, 'priceString': entries[name]}
            for name in FANS_GROUPS if name in entries]


# --------------------------------------------------------------------------- 构造器


def get_request(item_id: Any) -> Tuple[str, Dict[str, Any], str]:
    """读当前粉丝价：`{itemId}`（官方包装 `G0`，`data:{...e}`）。"""
    api = assert_read(GET_API)
    return api, {'itemId': _item_id(item_id)}, READ_VERSIONS[api]


def set_request(item_id: Any, groups: Any, *, confirm: Any = None
                ) -> Tuple[str, Dict[str, Any], str]:
    """整份写回粉丝价（**载荷逐字照官方**）。

    `fansPriceList` 里没有的组 = 没配置 ⇒ 留空的组会被清空。全空直接抛 `ValueError`
    （挡住「手滑清空全部粉丝价」）。
    """
    _need_confirm(confirm)
    api = assert_write(SET_API)
    item_id_text = _item_id(item_id)
    entries = normalize_groups(groups)
    if not entries:
        raise ValueError('至少要填一组粉丝价（三组全空会把粉丝价全清掉）')
    config = {'itemId': item_id_text, 'fansPriceList': entries}
    return api, {'itemId': item_id_text, 'fansPriceConfig': config}, WRITE_VERSIONS[api]


def request_of(action: str, **kwargs: Any) -> Tuple[str, Dict[str, Any], str]:
    """按动作名拼请求（服务层只在这里做一次「动作 → 构造器」的映射）。"""
    builders = {'fans_price_set': set_request}
    builder = builders.get(_text(action).strip())
    if builder is None:
        raise UnknownApi(f'不是本模块的写动作：{action}')
    return builder(**kwargs)


# --------------------------------------------------------------------------- 解析


def _body(raw: Any) -> Dict[str, Any]:
    """取出业务体（`trade.body_of` 认得 `data.module` / `data.data` / `data` 三种容器）。"""
    body = body_of(raw)
    return body if isinstance(body, dict) else {}


def _platform(raw: Any) -> Dict[str, Any]:
    """取出平台层（客户端信封里的 `data`）。

    官方那套 `resp.data.code === 'success'` 判的就是这一层，所以解析时要能看到它。
    """
    if isinstance(raw, dict):
        value = raw.get('data')
        if isinstance(value, dict):
            return value
    return {}


def _business_error(raw: Any) -> str:
    """业务层失败文案（客户端信封之外再兜一层）。

    成功判据是客户端信封 + 业务体里的 `code`/`success`；判不出来就返回 ''
    （宁可说成功也不凭空说失败）。
    """
    failure = envelope_error(raw)
    if failure:
        return failure
    for source in (_body(raw), _platform(raw)):
        if not source:
            continue
        code = source.get('code')
        if isinstance(code, str) and code.strip() and code.strip().lower() != 'success':
            return _text(source.get('message') or source.get('msg') or code)
        success = source.get('success')
        if success is False or (isinstance(success, str) and success.strip().lower() == 'false'):
            reason = _text(source.get('failReason') or source.get('message') or source.get('msg'))
            return reason or '平台接口返回失败'
    return ''


def _first_key(source: Dict[str, Any], keys: Sequence[str]) -> Tuple[Any, Optional[str]]:
    for key in keys:
        if key in source:
            return source[key], key
    return None, None


def normalize_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """平台的一条粉丝价 → `{fans_group, price_string}`。

    组名认不出、或价格缺失 ⇒ 返回 `None`（调用方丢掉这一条，**绝不猜组名**）。
    """
    if not isinstance(raw, dict):
        return None
    name, _ = _first_key(raw, GROUP_KEYS)
    group = _text(name).strip()
    if group not in FANS_GROUPS:
        return None
    price, price_key = _first_key(raw, PRICE_KEYS)
    if price is None or _text(price).strip() == '':
        return None
    return {'fans_group': group, 'price_string': _text(price).strip(), 'price_key': price_key}


def parse_config(raw: Any) -> Dict[str, Any]:
    """读接口响应 → `{prices, groups, found, config_key, list_key, body_keys, problem}`。

    * `prices` 为 `None` = **平台没返回配置**（与「三个组都没配」的 `[]` 不同）；
    * `groups` 三个键恒在，拿不到就是 `None`（前端显示 `-`，**不许补 0**）；
    * `problem` 是业务层失败文案（空串表示没看出失败）。
    """
    body = _body(raw)
    platform = _platform(raw)

    config: Optional[Dict[str, Any]] = None
    config_key: Optional[str] = None
    for source in (body, platform):
        if not source:
            continue
        value, key = _first_key(source, CONFIG_KEYS)
        if isinstance(value, dict):
            config = value
            config_key = key
            break

    prices: Optional[List[Dict[str, Any]]] = None
    list_key: Optional[str] = None
    for source in ([config] if config is not None else []) + [body, platform]:
        if not source:
            continue
        value, key = _first_key(source, LIST_KEYS)
        if isinstance(value, list):
            entries = [entry for entry in (normalize_entry(item) for item in value) if entry]
            prices = entries
            list_key = key
            break

    groups: Dict[str, Optional[str]] = {name: None for name in FANS_GROUPS}
    if prices:
        for entry in prices:
            groups[entry['fans_group']] = entry['price_string']

    return {
        'prices': prices,
        'groups': groups,
        'found': prices is not None,
        'config_key': config_key,
        'list_key': list_key,
        'body_keys': sorted(body.keys()) if body else [],
        'problem': _business_error(raw),
    }


#: 业务体里这些键只是「回话」，不算「有回执」（官方的成功判据是 `resp.data.data` 有东西）。
_ECHO_META_KEYS = ('code', 'success', 'message', 'msg', 'failReason')


def parse_set(raw: Any) -> Dict[str, Any]:
    """写接口响应 → `{saved, platform_message, envelope, body_keys, found}`。

    官方判成功看 `resp.data.data` 是否为真（@4101069：`e?.data?.data` 真 ⇒ toast「编辑成功」，
    否则 `$C.error(e.data.msg || '编辑失败，请重试')`）—— 我们照抄这个口径：

    * 业务层已经看出失败（`code` 不是 success / `success` 是假）⇒ `saved=False` + 平台原话；
    * 平台层 `data` 字段存在（或有 `data.module` 这种实质内容）⇒ `saved=True`；
    * 信封成功、业务体只剩 `code`/`msg` 这类「回话」⇒ `saved=False` 且没有原话，
      服务层据此加 `fans_price_no_echo` warning（**既不说成功也不说失败**）。
    """
    problem = _business_error(raw)
    body = _body(raw)
    platform = _platform(raw)
    inner = platform.get('data') if isinstance(platform, dict) else None

    if problem:
        saved = False
    elif inner is not None:
        saved = True
    else:
        # 没有 data.data，但业务体里有实质字段（比如 data.module 的内容）也算有回执
        saved = any(key not in _ECHO_META_KEYS for key in body)
    return {
        'saved': bool(saved),
        'platform_message': problem,
        'envelope': envelope_message(raw),
        'body_keys': sorted(body.keys()) if body else [],
        'found': bool(saved),
    }
