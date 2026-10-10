"""资质管理协议层（超级擦亮 `/seller-ad/qualification`）。

官方「超级擦亮」资质管理页：bundle ``fish-ad-pc@0.0.12``，路由证据
``logs/seller_discover/ad_pages.json:32``（``"#/seller-ad/qualification"``）。

==========  ================================================================
标签          mtop 接口
==========  ================================================================
detail      ``mtop.taobao.idle.ad.qualification.detail.query``
records     ``mtop.taobao.idle.ad.qualification.submittedRecords.batchQuery``
categories  ``mtop.taobao.idle.ad.qualification.allowAuditCategories.query``
templates   ``mtop.taobao.idle.ad.qualification.templates.queryv2``
reusable    ``mtop.taobao.idle.ad.qualification.reusableQualification.query``
==========  ================================================================

写接口（``packages.submit`` / ``get.oss.credential``）在本模块**实现但带硬闸**：
``confirm is not True`` 时直接 ``raise ValueError``，**一次平台请求都不会发**。
上传资质材料会**真实改变账号的资质状态**，不可逆 —— 闸门放在发请求之前，
而不是「发完发现没确认」。

三条硬口径（与 ``xianyu/marketing.py`` / ``xianyu/datacenter.py`` 完全一致）：

1. **平台没返回的字段一律 ``None``**，禁止 0 / [] 顶替（0 和 [] 都是真实值）。
   本模块只做 ``data`` 透传，不做字段翻译 —— 翻译就会引入「猜出来的 0」。
2. **无权限 ≠ 掉线**：``FAIL_BIZ_IDLE_USER_UNAUTHORIZED`` 这类业务拒绝如实透出，
   本模块**不写** ``need_relogin``。
3. 请求形状统一 ``data: {'requestJson': JSON.stringify(<业务对象>)}``，与
   ``client.py:932 _ad_json`` 同形状（``v='1.0'``、``spm_cnt=AD_SPM``）。

**证据缺口（重要）**：``requestJson`` 里业务对象的**具体键名没有拿到 bundle 调用点** ——
仓库 ``logs/seller_discover/`` 下没有 ``fish-ad-pc`` 的 JS 抓取物。所以本模块**不做字段
翻译**：``body`` 由调用方原样给出（服务层/文档里出现过的候选键名都逐条标了 ``# 证据缺口``）。
``{}` 是合法 body（平台侧 detail/templates 等很可能就是空对象），由调用方决定。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: 与 `client.py:130 AD_SPM` 同值。**故意在本地再写一份**：测试会把假的
#: `backend.app.xianyu.client` 模块塞进 sys.modules，`from .client import AD_SPM`
#: 会拿到假模块并 ImportError。
AD_SPM = 'a21ybx.im.0.0'

# --------------------------------------------------------------------------- 只读接口

QUALIFICATION_DETAIL_API = 'mtop.taobao.idle.ad.qualification.detail.query'
SUBMITTED_RECORDS_API = 'mtop.taobao.idle.ad.qualification.submittedRecords.batchQuery'
ALLOW_AUDIT_CATEGORIES_API = 'mtop.taobao.idle.ad.qualification.allowAuditCategories.query'
TEMPLATES_API = 'mtop.taobao.idle.ad.qualification.templates.queryv2'
REUSABLE_QUALIFICATION_API = 'mtop.taobao.idle.ad.qualification.reusableQualification.query'

# --------------------------------------------------------------------------- 写接口

PACKAGES_SUBMIT_API = 'mtop.taobao.idle.ad.qualification.packages.submit'
OSS_CREDENTIAL_API = 'mtop.taobao.idle.ad.qualification.get.oss.credential'

#: 写接口登记表（**全名**，不是 marketing.py 那种后缀）。存在这个元组只是为了
#: 「名字有据可查」+ 测试能钉住「写接口一个都没被偷偷调用」。任何代码路径都不许
#: 绕过 ``confirm`` 闸门去取用它里面的元素发请求。
WRITE_APIS: tuple = (PACKAGES_SUBMIT_API, OSS_CREDENTIAL_API)


def _request_json(body: Optional[Dict[str, Any]]) -> str:
    """业务对象 → ``requestJson`` 字符串（``ensure_ascii=False``，与 client.py 同）。"""
    if body is None:
        return '{}'
    if not isinstance(body, dict):
        raise ValueError('requestJson 的业务对象必须是 dict')
    return json.dumps(body, separators=(',', ':'), ensure_ascii=False)


async def _post(client: Any, api: str, body: Optional[Dict[str, Any]]
                ) -> Optional[Dict[str, Any]]:
    """统一发请求：``data`` 是 dict 就透传，否则 ``None``（**不造空 dict**）。"""
    result = await client.mtop(api, {'requestJson': _request_json(body)}, v='1.0', spm_cnt=AD_SPM)
    payload = result.get('data') if isinstance(result, dict) else None
    return payload if isinstance(payload, dict) else None


def _require_confirm(confirm: Any, action: str) -> None:
    """写操作硬闸：**必须是布尔 True**（``'false'`` 这种字符串不算确认）。"""
    if confirm is not True:
        raise ValueError(f'{action}会真实改变账号的资质状态，必须显式 confirm=True')


async def detail(client: Any, body: Optional[Dict[str, Any]] = None
                 ) -> Optional[Dict[str, Any]]:
    """资质详情。**只读**。"""
    return await _post(client, QUALIFICATION_DETAIL_API, body)


async def submitted_records(client: Any, body: Optional[Dict[str, Any]] = None
                            ) -> Optional[Dict[str, Any]]:
    """已提交的资质记录（批量查询）。**只读**。"""
    return await _post(client, SUBMITTED_RECORDS_API, body)


async def allow_audit_categories(client: Any, body: Optional[Dict[str, Any]] = None
                                 ) -> Optional[Dict[str, Any]]:
    """允许提交资质的审核类目。**只读**。"""
    return await _post(client, ALLOW_AUDIT_CATEGORIES_API, body)


async def templates(client: Any, body: Optional[Dict[str, Any]] = None
                    ) -> Optional[Dict[str, Any]]:
    """资质模板列表。**只读**。"""
    return await _post(client, TEMPLATES_API, body)


async def reusable_qualification(client: Any, body: Optional[Dict[str, Any]] = None
                                 ) -> Optional[Dict[str, Any]]:
    """可复用的资质（历史资质复用）。**只读**。"""
    return await _post(client, REUSABLE_QUALIFICATION_API, body)


async def submit_packages(client: Any, body: Optional[Dict[str, Any]] = None,
                          *, confirm: bool = False) -> Optional[Dict[str, Any]]:
    """**写平台**：提交资质材料。不 ``confirm=True`` 直接 ``ValueError``。"""
    _require_confirm(confirm, '提交资质')
    return await _post(client, PACKAGES_SUBMIT_API, body)


async def oss_credential(client: Any, body: Optional[Dict[str, Any]] = None,
                         *, confirm: bool = False) -> Optional[Dict[str, Any]]:
    """**写平台**：取 OSS 上传凭证（上传资质材料的第一步）。不确认直接 ``ValueError``。"""
    _require_confirm(confirm, '申请 OSS 上传凭证')
    return await _post(client, OSS_CREDENTIAL_API, body)
