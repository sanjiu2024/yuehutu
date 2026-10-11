"""运费模版（官方 seller-item 微应用的 `post-temple` 页）协议：**只读 + 写**。

**名字的坑（重要）**：官方菜单里这一项写作「发布模板 / post-temple」，但页面文案与协议层
都是**运费模版**（`post` = 邮 / `freight`）——bundle 里 `模板` 出现 0 次、`模版` 出现 49 次，
接口名全是 `mtop.alibaba.idle.seller.platform.pro.freight.template.*`。
平台**没有**「发布模板」这个功能：发布侧的「模板化」实际靠①草稿箱（`draft.search` /
`draft.get`）②发布页选运费模版（`publish/api` 的 `getShippingTemplate`）。
证据与路由表见 `docs/contracts/item_templates.md` 与 `logs/seller_discover/item_templates_bundle.md`。

**站点 CODE 头**：这几个接口服务端直调会回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`
（与安全中心同因），浏览器 SDK 会自动补 `idle_site_biz_code: COMMONPRO` 这类头。所以这里沿用
`XianyuClient._security_headers()`（拿不到就不补，行为与原样直调一致，不假装成功）。

**写接口的真实入参来自官方 bundle 静态提取**（`seller-item/0.0.20/js/main.js`，偏移见下），
不是猜的；但**一次真机写请求都没发过**（本机账号登录态已过期，红线不允许为了验证去改平台数据），
所以「平台是否接受这套字段」仍属未验证，见 `docs/contracts/item_templates.md` 的诚实边界。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

#: 模版列表（分页）。真机请求 `{"pageNo":1,"pageSize":10}`；
#: bundle 里搜索框还会带 `templateId` / `templateName`（@4135731）。
LIST_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.query'
#: 模版详情，`data:{templateId}`（bundle @2204414）。
DETAIL_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.detail'
#: 用了某个模版的商品，`templateId` **必填**（真机缺参报 `FAIL_SYS_BIZPARAM_MISSED`）。
ITEM_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.item.query'
#: 可选地区（无参，bundle @2205110）。
REGION_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.region'
#: 全量模版、不分页（发布页下拉用；bundle @4110816，`data:{}` + `valueType:'string'`）。
ALL_API = 'mtop.alibaba.idle.seller.platform.freight.template.pc.all'
#: 省市区字典（无参）。
DIVISION_API = 'mtop.taobao.idle.division.all.get'

# --------------------------------------------------------------------------- 写接口

#: 新建模版（bundle @2204883 声明 / @2211045 调用点）。
CREATE_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.create'
#: 修改模版（bundle @2204655 声明 / @2244766 调用点）。
MODIFY_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.modify'
#: 删除模版，`data:{templateId}`（bundle @2203984 声明 / @4129523 调用点）。
DELETE_API = 'mtop.alibaba.idle.seller.platform.pro.freight.template.del'
#: 给商品设置运费（官方页面叫「批量解绑/设置运费」；bundle @4114059 调用点）。
#: 名字里带 `unbind`，但它同时承担包邮 / 用模版 / 固定运费三种设置。
UNBIND_API = 'mtop.alibaba.idle.seller.platform.freight.template.pc.item.unbind'

#: 平台业务成功码：bundle 原文判据是 `res.data.code === 'success'`。
SUCCESS_CODE = 'success'

#: 设置运费的三种模式（对应官方页面的三个单选）。
UNBIND_MODES = ('free_ship', 'template', 'fixed')

#: 写能力登记：**已实现**。`data` 写真实字段形状，`evidence` 是 bundle 字符偏移（bundle 是
#: 单行压缩 JS，行号无意义，所以用偏移 + 复现字符串定位）。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'create': {
        'api': CREATE_API,
        'data': '{templateName, itemProv, itemCity, valuation, isDefault(bool), '
                'templateDetails(JSON 字符串)}',
        'evidence': 'bundle @2211045',
    },
    'modify': {
        'api': MODIFY_API,
        'data': 'create 的全部字段 + templateId + version',
        'evidence': 'bundle @2244766',
    },
    'delete': {
        'api': DELETE_API,
        'data': '{templateId}',
        'evidence': 'bundle @4129523',
    },
    'unbind': {
        'api': UNBIND_API,
        'data': '{itemIds(逗号串), supportFreight, onlyTakeSelf, needAutoFreight, isFreeShip, '
                'idleTemplateId?, postPriceInCent?}',
        'evidence': 'bundle @4114059',
    },
}

#: `templateDetails` 里每条明细的字段名（bundle 原文：
#: `{isDefault, receiveDivisionName, firstPrice, excessPrice, first, excess, operate:'add'}`）。
DETAIL_KEY_MAP = {
    'receive_division_name': 'receiveDivisionName',
    'first_price': 'firstPrice',
    'excess_price': 'excessPrice',
    'first': 'first',
    'excess': 'excess',
    'is_default': 'isDefault',
    'operate': 'operate',
}

#: 明细默认操作（bundle 里新增明细恒为 `add`）。
DEFAULT_DETAIL_OPERATE = 'add'


def site_headers(client: Any) -> Optional[Dict[str, str]]:
    """借用客户端的站点 CODE 头；拿不到返回 None（不补头，也不假装）。"""
    factory = getattr(client, '_security_headers', None)
    if not callable(factory):
        return None
    try:
        headers = factory()
    except Exception:  # noqa: BLE001 - 头补不上不该让只读查询失败
        return None
    return headers if isinstance(headers, dict) else None


# --------------------------------------------------------------------------- 纯函数（可单测）

def build_detail_entry(entry: Any) -> Dict[str, Any]:
    """一条运费明细：snake_case 已知键映射成官方字段名，未知键**原样透传**（不丢平台新字段）。

    官方字段名直接传进来也能过（`DETAIL_KEY_MAP.get(key, key)`）。
    """
    source = entry if isinstance(entry, dict) else {}
    out: Dict[str, Any] = {}
    for key, value in source.items():
        out[DETAIL_KEY_MAP.get(key, key)] = value
    out.setdefault('operate', DEFAULT_DETAIL_OPERATE)
    return out


def build_template_details(details: Any) -> str:
    """把明细列表编成平台要的 JSON 字符串（`templateDetails`）。"""
    rows: Iterable[Any] = details if isinstance(details, (list, tuple)) else []
    return json.dumps([build_detail_entry(row) for row in rows], ensure_ascii=False)


def build_template_payload(*, template_name: str, item_prov: str = '', item_city: str = '',
                           valuation: Any = None, is_default: bool = False,
                           details: Any = None, template_details: str = '',
                           template_id: Any = '', version: Any = None) -> Dict[str, Any]:
    """新建 / 修改模版的请求体。

    **空的选填字段直接不发**（官方 bundle 里它们是 `undefined`，`JSON.stringify` 会丢掉），
    而不是发空串 —— 发空串会让平台把「没填」当成「填了空」。
    `template_details` 给了字符串就优先用它（调用方手里已经有平台原串时不再二次加工）。
    """
    data: Dict[str, Any] = {
        'templateName': '' if template_name is None else str(template_name),
        'isDefault': bool(is_default),
        'templateDetails': str(template_details) if template_details
        else build_template_details(details),
    }
    if item_prov not in (None, ''):
        data['itemProv'] = item_prov
    if item_city not in (None, ''):
        data['itemCity'] = item_city
    if valuation is not None:
        data['valuation'] = valuation
    if template_id not in (None, ''):
        data['templateId'] = template_id
    if version is not None:
        data['version'] = version
    return data


def normalize_item_ids(item_ids: Any) -> str:
    """`itemIds` 在官方 bundle 里是**逗号分隔字符串**（不是数组）。"""
    if isinstance(item_ids, str):
        return item_ids.strip()
    if isinstance(item_ids, (list, tuple, set)):
        return ','.join(str(x).strip() for x in item_ids if str(x).strip())
    return ''


def build_unbind_payload(*, item_ids: Any, mode: str, template_id: Any = '',
                         post_price: Any = None) -> Dict[str, Any]:
    """给商品设置运费。三种模式的字段组合**逐字照抄**官方 bundle @4114059：

    - `free_ship`（包邮）：`isFreeShip=True`，其余保持默认；
    - `template`（用模版）：`isFreeShip=False, idleTemplateId=<id>, supportFreight=False`；
    - `fixed`（固定运费）：`isFreeShip=False, postPriceInCent=元*100`（**单位是分**）。
    """
    data: Dict[str, Any] = {
        'itemIds': normalize_item_ids(item_ids),
        'supportFreight': True,
        'onlyTakeSelf': False,
        'needAutoFreight': False,
    }
    if mode == 'free_ship':
        data['isFreeShip'] = True
    elif mode == 'template':
        data['isFreeShip'] = False
        data['idleTemplateId'] = template_id
        data['supportFreight'] = False
    elif mode == 'fixed':
        data['isFreeShip'] = False
        data['postPriceInCent'] = int(round(float(post_price) * 100))
    return data


class ItemTemplatesClient:
    """运费模版门面：只转发 `mtop`，不改字段、不造 0。

    方法名沿用 bundle `post-temple/service` 的导出（`fetchTempleList` → `fetch_temple_list`），
    便于 `common.call_method(client, (...候选名...))` 在真实客户端 / 假客户端之间切换。
    写方法同样沿用 bundle 命名（`createTemple` / `modifyTemple` / `delTemple` / `unbindItem`）。
    """

    def __init__(self, client: Any, account_id: str = ''):
        self._client = client
        self.account_id = account_id
        self._headers = site_headers(client)

    async def _mtop(self, api: str, data: Any = None) -> Dict[str, Any]:
        result = await self._client.mtop(api, data, v='1.0', extra_headers=self._headers)
        return result if isinstance(result, dict) else {'success': True, 'data': result}

    # ---------------------------------------------------------------- 只读
    async def fetch_temple_list(self, page_no: int = 1, page_size: int = 20,
                                template_id: str = '', template_name: str = '') -> Dict[str, Any]:
        data: Dict[str, Any] = {'pageNo': int(page_no), 'pageSize': int(page_size)}
        if template_id not in (None, ''):
            data['templateId'] = template_id
        if template_name:
            data['templateName'] = str(template_name)
        return await self._mtop(LIST_API, data)

    async def fetch_temple_detail(self, template_id: Any) -> Dict[str, Any]:
        return await self._mtop(DETAIL_API, {'templateId': template_id})

    async def fetch_item_list(self, template_id: Any, page_no: int = 1,
                              page_size: int = 20) -> Dict[str, Any]:
        return await self._mtop(ITEM_API, {'templateId': template_id,
                                           'pageNo': int(page_no), 'pageSize': int(page_size)})

    async def fetch_temple_all(self) -> Dict[str, Any]:
        return await self._mtop(ALL_API, {})

    async def fetch_region_all(self) -> Dict[str, Any]:
        return await self._mtop(REGION_API, {})

    async def fetch_division_all(self) -> Dict[str, Any]:
        return await self._mtop(DIVISION_API, {})

    # ---------------------------------------------------------------- 写
    async def create_temple(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mtop(CREATE_API, payload)

    async def modify_temple(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mtop(MODIFY_API, payload)

    async def del_temple(self, template_id: Any) -> Dict[str, Any]:
        return await self._mtop(DELETE_API, {'templateId': template_id})

    async def unbind_item(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._mtop(UNBIND_API, payload)


#: 写方法名登记（`common.call_method` 的候选名；假客户端按这些名字实现即可）。
WRITE_METHODS: Dict[str, str] = {
    'create': 'create_temple',
    'modify': 'modify_temple',
    'delete': 'del_temple',
    'unbind': 'unbind_item',
}

#: 只读方法名（防止写路径误用只读方法）。
READ_METHODS: List[str] = [
    'fetch_temple_list', 'fetch_temple_detail', 'fetch_item_list',
    'fetch_temple_all', 'fetch_region_all', 'fetch_division_all',
]
