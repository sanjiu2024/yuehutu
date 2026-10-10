"""运费模版（官方 seller-item 微应用的 `post-temple` 页）**只读**协议。

**名字的坑（重要）**：官方菜单里这一项写作「发布模板 / post-temple」，但页面文案与协议层
都是**运费模版**（`post` = 邮 / `freight`）——bundle `js_main.js` 里 `模板` 出现 0 次、
`模版` 出现 49 次，接口名全是 `mtop.alibaba.idle.seller.platform.pro.freight.template.*`。
平台**没有**「发布模板」这个功能：发布侧的「模板化」实际靠①草稿箱（`draft.search` /
`draft.get`）②发布页选运费模版（`publish/api` 的 `getShippingTemplate`）。
证据与路由表见 `docs/contracts/item_templates.md` 与 `logs/seller_discover/item_templates_bundle.md`。

本模块只包只读接口，写能力（创建/编辑/删除/解绑）**不实现**，名字与参数形状记在 `WRITE_APIS`。

**站点 CODE 头**：这几个接口服务端直调会回 `FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限访问`
（与安全中心同因，见 `test_promotion_api.py` 文件头），浏览器 SDK 会自动补
`idle_site_biz_code: COMMONPRO` 这类头。所以这里沿用 `XianyuClient._security_headers()`
（拿不到就不补，行为与原样直调一致，不假装成功）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

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

#: 写能力：本轮**只记录不实现**（谁要做得先有产品授权 + 二次确认）。
WRITE_APIS: Dict[str, Dict[str, str]] = {
    'create': {'api': 'mtop.alibaba.idle.seller.platform.pro.freight.template.create',
               'data': '表单透传；已知字段 templateName（≤30 字符、禁 emoji）、isDefault'},
    'modify': {'api': 'mtop.alibaba.idle.seller.platform.pro.freight.template.modify',
               'data': '表单透传'},
    'delete': {'api': 'mtop.alibaba.idle.seller.platform.pro.freight.template.del',
               'data': '{templateId}'},
    'unbind': {'api': 'mtop.alibaba.idle.seller.platform.freight.template.pc.item.unbind',
               'data': '透传'},
}


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


class ItemTemplatesClient:
    """运费模版只读门面：只转发 `mtop`，不改字段、不造 0。

    方法名沿用 bundle `post-temple/service` 的导出（`fetchTempleList` → `fetch_temple_list`），
    便于 `common.call_method(client, (...候选名...))` 在真实客户端 / 假客户端之间切换。
    """

    def __init__(self, client: Any, account_id: str = ''):
        self._client = client
        self.account_id = account_id
        self._headers = site_headers(client)

    async def _mtop(self, api: str, data: Any = None) -> Dict[str, Any]:
        result = await self._client.mtop(api, data, v='1.0', extra_headers=self._headers)
        return result if isinstance(result, dict) else {'success': True, 'data': result}

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
