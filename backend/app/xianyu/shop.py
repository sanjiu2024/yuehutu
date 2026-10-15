"""小铺组（子账号管理 / 客服分流）协议层 —— **只读**。

官方页面：`/seller-account/sub-account`、`/im-cs-dispatch/customer-routing-service`。
接口名来自官方微应用 bundle 的调用点（2026-10-10 抓取，证据在
`logs/seller_discover/shop_bundles_main.json` 与 `logs/seller_discover/bundles/`）：

- 子账号列表 `mtop.alibaba.idle.seller.common.sub.account.info.list`
  （bundle 里 `Cf({api: "...", v:"1.0", data: e})`，响应判 `data.code === 'success'`）
- 子账号配额 `mtop.alibaba.idle.seller.common.sub.account.quota.query`
- 角色列表   `mtop.alibaba.idle.seller.platform.user.role.list`
- 小铺昵称   `mtop.alibaba.idle.seller.platform.usergroup.nick.get`
- 店铺详情   `mtop.alibaba.idle.shop.manage.detail`
- 客服分流规则 `mtop.taobao.idlemessage.customer.routing.rule.info.get`
- 客服分组   `mtop.taobao.idlemessage.customer.group.get`（带 `page`/`pageSize`）
- 客服列表   `mtop.taobao.idlemessage.customers.info.get`

**这些接口都用 `data:{...}` 直传业务字段**（不像 `idle.ad.*` 那样套 `requestJson`）。

`WRITE_APIS` 是同一批 bundle 里的**写接口**（注册/删除子账号、改手机号/密码/角色、
改分流规则、改客服分组）。本项目只复刻「看」，一个都不许调 ——
`backend/tests/test_shop_api.py` 会拿假客户端记录的每次 mtop 调用去比对这张表。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

SUB_ACCOUNT_LIST_API = 'mtop.alibaba.idle.seller.common.sub.account.info.list'
SUB_ACCOUNT_QUOTA_API = 'mtop.alibaba.idle.seller.common.sub.account.quota.query'
ROLE_LIST_API = 'mtop.alibaba.idle.seller.platform.user.role.list'
SHOP_NICK_API = 'mtop.alibaba.idle.seller.platform.usergroup.nick.get'
SHOP_DETAIL_API = 'mtop.alibaba.idle.shop.manage.detail'
CS_ROUTING_RULE_API = 'mtop.taobao.idlemessage.customer.routing.rule.info.get'
CS_GROUP_API = 'mtop.taobao.idlemessage.customer.group.get'
CS_AGENT_LIST_API = 'mtop.taobao.idlemessage.customers.info.get'

#: 真机探针里子账号接口统一带这个业务标识（`{"bizType":"COMMONPRO"}`，
#: 见 `logs/seller_discover/shop_notify_probe.json`）；缺业务参数平台会直接报
#: `FAIL_SYS_BIZPARAM_MISSED`，所以不能省。
BIZ_TYPE = 'COMMONPRO'

#: 本模块只会用到的只读接口（测试据此断言「没有写接口被调用」）。
READ_APIS = (
    SUB_ACCOUNT_LIST_API,
    SUB_ACCOUNT_QUOTA_API,
    ROLE_LIST_API,
    SHOP_NICK_API,
    SHOP_DETAIL_API,
    CS_ROUTING_RULE_API,
    CS_GROUP_API,
    CS_AGENT_LIST_API,
)

#: 同一批 bundle 里的写接口 —— 列在这里是为了让「**只读路径**没有调写接口」可被断言。
#: 来源：`logs/seller_discover/shop_bundles_main.json`（子账号 bundle 的 main.js）。
#: 图片/OSS/验证/短信/退款一并列全：只要有人把这些名字写进**只读**调用清单，
#: `test_no_write_api_is_ever_called` 就会红。
#:
#: 注意：最后两条（客服分流的 `routing.rule.operate` / `customer.group.operate`）**已经实现**
#: 了 —— 写协议在 `backend/app/xianyu/shop_write.py`、服务层在 `services/shop.py` 的
#: 「写操作」段、HTTP 在 `api/shop.py` 的三条 POST（都要 `confirm=true`）。
#: 它们**故意继续留在**这个黑名单里：只读路径依然一条都不许碰它们。
WRITE_APIS = (
    'mtop.alibaba.idle.seller.platform.common.sub.account.register',
    'mtop.alibaba.idle.seller.platform.sub.account.delete',
    'mtop.alibaba.idle.seller.platform.sub.account.mobilephone.update',
    'mtop.alibaba.idle.seller.platform.sub.account.password.update',
    'mtop.alibaba.idle.seller.platform.sub.account.role.update',
    'mtop.alibaba.idle.seller.platform.sub.account.sub.pk',
    'mtop.alibaba.idle.shop.manage.avatar.update',
    'mtop.alibaba.idle.shop.manage.desc.update',
    'mtop.alibaba.idle.shop.manage.nick.update',
    'mtop.alibaba.idle.shop.manage.phone.update',
    'mtop.alibaba.idle.shop.manage.sms.send',
    'mtop.alibaba.idle.shop.manage.verify.update',
    'mtop.alibaba.idle.seller.platform.image.upload',
    'mtop.alibaba.idle.seller.platform.image.post.signature.url',
    'mtop.alibaba.idle.shop.verify.oss.param.query',
    'mtop.alibaba.idle.shop.verify.pic.url.query',
    'mtop.idle.alipay.verify.url.query',
    'mtop.taobao.idle.refund.subinfo.manage',
    'mtop.taobao.idlemessage.customer.routing.rule.operate',
    'mtop.taobao.idlemessage.customer.group.operate',
)


async def _data(client: Any, api: str, body: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """调一次 mtop 并只取 `data` 段（拿不到就 None，由服务层如实降级）。

    **载荷可能是数组**：bundle 里 `platform.user.role.list` 的 `data` 就是
    `[{roleId, roleName, selected}]`（`logs/seller_discover/shop_discover.md`），
    所以 dict / list 都放行；只有「既不是对象也不是数组」才算拿不到。
    早先只放行 dict，会把整个角色列表静默吞掉（`roles` 变 null 且没有 warning）。

    `client.mtop` 在失败时会**抛**（登录态过期 / 风控 / 平台报错都抛），
    这里不吞异常：服务层要靠异常类型区分「该重新扫码」和「协议出错」。
    """
    result = await client.mtop(api, body if body is not None else {}, v='1.0')
    if not isinstance(result, dict):
        return None
    data = result.get('data')
    return data if isinstance(data, (dict, list)) else None


async def sub_accounts(client: Any, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """子账号列表（含配额/角色时平台会一起回）。"""
    return await _data(client, SUB_ACCOUNT_LIST_API, params)


async def sub_account_quota(client: Any) -> Optional[Any]:
    """子账号名额配额（真机带 `{"bizType":"COMMONPRO"}`）。"""
    return await _data(client, SUB_ACCOUNT_QUOTA_API, {'bizType': BIZ_TYPE})


async def roles(client: Any) -> Optional[Any]:
    """角色列表（子账号可分配的角色）。"""
    return await _data(client, ROLE_LIST_API, {})


async def shop_nick(client: Any) -> Optional[Any]:
    """小铺昵称。"""
    return await _data(client, SHOP_NICK_API, {})


async def shop_detail(client: Any) -> Optional[Any]:
    """店铺详情。"""
    return await _data(client, SHOP_DETAIL_API, {})


async def cs_routing_rules(client: Any) -> Optional[Any]:
    """客服分流规则（只读；写接口是 `...routing.rule.operate`）。"""
    return await _data(client, CS_ROUTING_RULE_API, {})


async def cs_groups(client: Any, page: int = 1, page_size: int = 20) -> Optional[Any]:
    """客服分组（分页；bundle 里传 `{page, pageSize}`）。"""
    return await _data(client, CS_GROUP_API, {'page': int(page), 'pageSize': int(page_size)})


async def cs_agents(client: Any) -> Optional[Any]:
    """客服（子账号）列表。"""
    return await _data(client, CS_AGENT_LIST_API, {})


# --------------------------------------------------------------------------- READ-COVERAGE-61
#: 发布页某项服务（验货宝 / 极速退款 / 包邮 等）的开通状态（**只读**）。
#: bundle 调用点 logs/seller_discover/item_templates_src/js_main.js:1:4141260：
#:   ybe = e => gC({api:"mtop.alibaba.idle.pc.service.status.query", data:{serviceCode:e}})
#: **不确定项**：官方包装**没写 v**（同族 pc.service.open 也没有），而本模块其余接口都带
#: v:"1.0"；未真机验证，所以这里按本模块既有口径让 _data 统一带版本号。若平台要求别的
#: 版本会回 FAIL_SYS_ILLEGAL_ARGUMENT，届时按真机证据改。响应形状也没取证（bundle 只传参
#: 不解析），因此服务层**原样透出** data，不猜字段。
PC_SERVICE_STATUS_API = 'mtop.alibaba.idle.pc.service.status.query'

#: 发布页「开启某项服务」（**写**：会真的给商品/店铺开通一项服务）。bundle 调用点
#: logs/seller_discover/item_templates_src/js_main.js:1:4141359：
#:   bbe = e => gC({api:"mtop.alibaba.idle.pc.service.open", data:{...e}})
#: **本批不接**：语义是变更不是查询，且入参 e 的形状只到调用点这一层（bbe(e) 的调用方
#: 没有取证），接不出可靠的写闸门。登记在这里是为了让「只读路径不碰写接口」可被断言。
PC_SERVICE_OPEN_API = 'mtop.alibaba.idle.pc.service.open'

#: 把第 61 批的两条并进本模块的两张登记表（上面的表是历史取证记录，不改动）。
READ_APIS = READ_APIS + (PC_SERVICE_STATUS_API,)
WRITE_APIS = WRITE_APIS + (PC_SERVICE_OPEN_API,)


async def pc_service_status(client: Any, service_code: Any) -> Any:
    """发布页服务开通状态（只读）。官方入参就是 {serviceCode}（见常量注释的调用点原文）。"""
    return await _data(client, PC_SERVICE_STATUS_API, {'serviceCode': service_code})
