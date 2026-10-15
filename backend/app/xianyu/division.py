"""区域码（division）协议层 —— **只读**（第 61 批）。

接口：`mtop.cainiao.geography.frontend.divisionsinfo.get`（`v='1.0'`，`valueType='division'`）。

入参逐字（官方 bundle `~/.dsh-tmp/trade_bundle/main.js` 偏移 3,387,049 起 `DivisionSelect`）：
`{'divisionToken': <token>, 'pageIndex': 1, 'bizEntrance': 'idle_fish'}`；根 token = `'CN'`。

**官方前端映射**（同偏移）：`{label: names.CN, value: divisionId + '/' + names.CN,
divisionToken, divisionId, isLeaf: level >= 4, level, children: []}`。
官方地址提交处（偏移 3,781,718）：`areaId` = **第三级（区）**选项的 `value`；
`provinceName` / `cityName` / `districtName` = 各级 `label`，另附 `division: [省, 市, 区]`。

**真机证据**（2026-10-16，账号 2217970177348，只读；原始响应 `logs/probe61_division/raw.json`）：
`token='CN'` → 31 个省（`totalPage 1 / totalCount 31 / pageSize 100 / parentToken 'CN'`）；
`token='CN^^^北京'` → 1 个市 `CN^^^北京^^^北京市`；
`token='CN^^^北京^^^北京市'` → 17 个区（如 `CN^^^北京^^^北京市^^^东城区`）。

**信封两层**（实测）：客户端返回值 `env['data']` 是 `{'data': {...}, 'message': ..., 'status': ...}`，
真实数据在 `env['data']['data']` —— 用 `trade.body_of` 摊平（它认得 `data.data` 这层容器）。

**不确定项**（如实写在这里，别当成已知）：
1. **分页**：三级都只遇到 `totalPage = 1`（`pageSize = 100`），**没遇到多页** ——
   `page_index` 原样透传；多页时 `divisions` 的拼接口径**未取证**。
2. **叶子判据**：bundle 用 `isLeaf: level >= 4` 推断；而平台字段 `divisionLeaf` 在 level=4 的
   区上实测是 **`false`**（东城区确实没有下级）—— **两者不等价**。本层把 `is_leaf`（平台原字段）
   与 `level` 都透出去，**终止判据用 `level >= 4`（官方口径）**，不要用 `divisionLeaf`。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .trade import body_of

#: 区域码查询（只读）。bundle 偏移 3,387,049 起（`DivisionSelect`）。
DIVISIONS_API = 'mtop.cainiao.geography.frontend.divisionsinfo.get'

#: bundle 原文 `v: '1.0'`。
DEFAULT_VERSION = '1.0'

#: 官方 `valueType`（真机实测要带；不带时平台按默认类型解析）。
VALUE_TYPE = 'division'

#: 根节点 token（实测 `'CN'` → 31 个省）。
ROOT_TOKEN = 'CN'

#: 官方调用点逐字带的业务入口标识。
BIZ_ENTRANCE = 'idle_fish'

#: 官方口径的叶子层级（`isLeaf: level >= 4`）；**不是**平台 `divisionLeaf` 字段。
LEAF_LEVEL = 4

#: 真机实测一页 100 条（31 个省 / 17 个区都一页装下）。
DEFAULT_PAGE_SIZE = 100

#: 平台 token 实测形如 `CN^^^北京^^^北京市`；超过这个长度基本是抄错或被注入。
MAX_TOKEN_LENGTH = 128

#: 本模块**只读**：写路径只有退货地址 add/update（在 `xianyu/address_write.py`）。
READ_APIS: Tuple[str, ...] = (DIVISIONS_API,)
ALL_APIS = frozenset(READ_APIS)


class UnknownApi(ValueError):
    """调用了本模块不认识的接口。"""


def assert_read(api: str) -> str:
    if api not in READ_APIS:
        raise UnknownApi(f'{api} 不是区域码模块的只读接口')
    return api


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _int(value: Any) -> Optional[int]:
    """平台给 int / 数字字符串都认；给不了就给 `None`（**不写 0** —— 0 是合法值）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> Optional[bool]:
    """只认平台**真的给了** bool；没给就是 `None`（「没给」≠「不是」）。"""
    return value if isinstance(value, bool) else None


def divisions_request(token: Any = None, page_index: Any = 1) -> Tuple[str, Dict[str, Any]]:
    """官方入参逐字：`{divisionToken, pageIndex, bizEntrance}`。token 为空 → 根 `'CN'`。

    纯本地参数校验（**不发请求**）：`page_index` 必须是正整数、`token` 不能超长，
    不合格直接 `ValueError`（服务层翻 400 `invalid_fields`）——不拿平台请求换同样的错。
    """
    page_text = _text(page_index)
    if not page_text:
        page = 1
    elif not page_text.isdigit() or int(page_text) < 1:
        raise ValueError('页码（page_index）必须是正整数')
    else:
        page = int(page_text)

    wanted = _text(token) or ROOT_TOKEN
    if len(wanted) > MAX_TOKEN_LENGTH:
        raise ValueError(f'区域码 token 超过 {MAX_TOKEN_LENGTH} 字，像是抄错了')
    return assert_read(DIVISIONS_API), {
        'divisionToken': wanted,
        'pageIndex': page,
        'bizEntrance': BIZ_ENTRANCE,
    }


def normalize_division(entry: Dict[str, Any]) -> Dict[str, Any]:
    """一条区域码 → 前端要的形状（`value` 用官方格式 `divisionId/名称`）。"""
    names = entry.get('names') if isinstance(entry.get('names'), dict) else {}
    ext = entry.get('extAttrs') if isinstance(entry.get('extAttrs'), dict) else {}
    label = _text(names.get('CN')) or _text(names.get('EN'))
    division_id = _int(entry.get('divisionId'))
    return {
        'division_id': division_id,
        'division_token': _text(entry.get('divisionToken')),
        'label': label,
        # 官方 `value = divisionId + '/' + names.CN`；缺 id 或缺名字时给 ''（**不拼半个**）
        'value': f'{division_id}/{label}' if division_id is not None and label else '',
        'level': _int(entry.get('level')),
        'is_leaf': _bool_or_none(entry.get('divisionLeaf')),
        'abb_name': _text(ext.get('abbName')),
        'pinyin': _text(ext.get('pinyin')),
    }


def parse_divisions(raw: Any) -> Dict[str, Any]:
    """把平台信封摊平成归一化结构（真实数据在 `env['data']['data']`，`body_of` 认这层）。"""
    body = body_of(raw)
    data = body if isinstance(body, dict) else {}
    rows = data.get('divisions')
    listed = isinstance(rows, list)
    items = [normalize_division(row) for row in rows if isinstance(row, dict)] if listed else []
    return {
        # 平台没给这个键 → None（与「明确给了空数组」区分开），服务层据此给 warning
        'divisions': items if listed else None,
        'total_count': _int(data.get('totalCount')),
        'total_page': _int(data.get('totalPage')),
        'page_index': _int(data.get('pageIndex')),
        'page_size': _int(data.get('pageSize')),
        'parent_token': _text(data.get('parentToken')) or None,
    }


async def divisions(client: Any, token: Any = ROOT_TOKEN, page_index: Any = 1) -> Dict[str, Any]:
    """拉**一级**区域码（只读）。

    `client.mtop` 在平台报错时**抛** `XianyuError`（带 `FAIL_*` 原文），本层不吞 ——
    服务层要靠原文区分「无权限 / 掉线 / 参数错」。
    """
    api, data = divisions_request(token, page_index)
    raw = await client.mtop(api, data, v=DEFAULT_VERSION, value_type=VALUE_TYPE)
    return parse_divisions(raw)
