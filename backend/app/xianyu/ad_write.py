"""超级擦亮**写**接口（协议层）。

这里只做一件事：按官方 bundle 里真实存在的接口名与参数形状，发一次 mtop。
业务校验、账号校验、失败信封、留痕都在 ``backend/app/services/ad_write.py``。

接口名与形状的来源：官方超级擦亮微应用 bundle
``https://g.alicdn.com/idle-pc/fish-ad-pc/0.0.12/js/main.js``（2026-10-11 拉取，3.76 MB，
共 86 个 mtop 名）。每个常量下面都写了 bundle 里的原始调用点形状。

**充值 / 提现的下单接口在 bundle 里不存在**：只有 ``.bp.account.recharge.query`` 与
``.bp.account.withdraw.query`` 两个**只读查询**，且官方账户页的提现按钮文案是
「请进入闲鱼App内操作」。所以这个模块里**没有**下单函数 —— 绝不编接口名，见
``docs/contracts/ad_write.md``。

所有写函数都要显式 ``confirm=True``：漏了就抛 ``WriteNotConfirmed``，一个字节都不发出去。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: 投放计划：编辑 / 删除 / 批量删除 / 暂停 / 启动（V2）/ 启动前校验 / 商品计划续投
CAMPAIGN_EDIT_API = 'mtop.taobao.idle.ad.campaign.edit'
CAMPAIGN_DELETE_API = 'mtop.taobao.idle.ad.campaign.delete'
CAMPAIGN_BATCH_DELETE_API = 'mtop.taobao.idle.ad.campaign.batch.delete'
CAMPAIGN_PAUSE_API = 'mtop.taobao.idle.ad.campaign.pause'
CAMPAIGN_START_API = 'mtop.taobao.idle.ad.campaign.startV2'
CAMPAIGN_START_BEFORE_API = 'mtop.taobao.idle.ad.campaign.start.before'
ITEM_CAMPAIGN_RENEW_API = 'mtop.taobao.idle.ad.item.campaign.renew'

#: 活动：报名 / 领奖任务执行
ACTIVITY_APPLY_API = 'mtop.taobao.idle.ad.activity.apply'
ACTIVITY_TASK_EXECUTE_API = 'mtop.taobao.idle.ad.activity.task.execute'

#: 账户流水操作（官方「资金明细」页的操作入口，写接口）
ACCOUNT_LOG_OPERATES_API = 'mtop.taobao.idle.ad.bp.account.log.operates'

#: 动作 -> 接口名与是否带业务参数。``params=False`` 的接口官方请求 ``data`` 是空对象。
WRITE_APIS: Dict[str, Dict[str, Any]] = {
    'campaign_edit': {'api': CAMPAIGN_EDIT_API, 'params': True},
    'campaign_delete': {'api': CAMPAIGN_DELETE_API, 'params': True},
    'campaign_batch_delete': {'api': CAMPAIGN_BATCH_DELETE_API, 'params': True},
    'campaign_pause': {'api': CAMPAIGN_PAUSE_API, 'params': True},
    'campaign_start': {'api': CAMPAIGN_START_API, 'params': True},
    'campaign_start_before': {'api': CAMPAIGN_START_BEFORE_API, 'params': False},
    'item_campaign_renew': {'api': ITEM_CAMPAIGN_RENEW_API, 'params': True},
    'activity_apply': {'api': ACTIVITY_APPLY_API, 'params': True},
    'activity_task_execute': {'api': ACTIVITY_TASK_EXECUTE_API, 'params': True},
    'account_log_operates': {'api': ACCOUNT_LOG_OPERATES_API, 'params': True},
}


class WriteNotConfirmed(RuntimeError):
    """写接口没有拿到显式确认。协议层最后的闸门：宁可不发，也不许误发。"""


def _require_confirm(confirm: Any) -> None:
    if confirm is not True:
        raise WriteNotConfirmed('写接口必须显式 confirm=True 才执行')


async def _ad_write(client: Any, api: str, body: Any, confirm: Any) -> Optional[Dict[str, Any]]:
    """``idle.ad.*`` 统一形状：``data: {requestJson: JSON.stringify(<业务对象>)}``。"""
    _require_confirm(confirm)
    from .client import AD_SPM  # 延迟导入：测试会替换 xianyu.client 模块
    request = json.dumps(body if isinstance(body, dict) else {},
                         separators=(',', ':'), ensure_ascii=False)
    result = await client.mtop(api, {'requestJson': request}, v='1.0', spm_cnt=AD_SPM)
    payload = result.get('data') if isinstance(result, dict) else None
    return payload if isinstance(payload, dict) else None


async def campaign_start_before(client: Any, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """启动前校验：官方调用点 ``data:{}``（**不带** requestJson）。"""
    _require_confirm(confirm)
    from .client import AD_SPM
    result = await client.mtop(CAMPAIGN_START_BEFORE_API, {}, v='1.0', spm_cnt=AD_SPM)
    payload = result.get('data') if isinstance(result, dict) else None
    return payload if isinstance(payload, dict) else None


async def campaign_edit(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """编辑投放计划。官方：``{api:'...campaign.edit', data:{requestJson:JSON.stringify(e)}}``"""
    return await _ad_write(client, CAMPAIGN_EDIT_API, body, confirm)


async def campaign_delete(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """删除投放计划。官方形状同 edit。"""
    return await _ad_write(client, CAMPAIGN_DELETE_API, body, confirm)


async def campaign_batch_delete(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """批量删除投放计划。官方形状同 edit。"""
    return await _ad_write(client, CAMPAIGN_BATCH_DELETE_API, body, confirm)


async def campaign_pause(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """暂停投放计划。官方形状同 edit。"""
    return await _ad_write(client, CAMPAIGN_PAUSE_API, body, confirm)


async def campaign_start(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """启动投放计划（V2）。官方：``{api:'...campaign.startV2', v:'1.0', data:{requestJson:...}}``"""
    return await _ad_write(client, CAMPAIGN_START_API, body, confirm)


async def item_campaign_renew(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """商品计划续投。官方：``{api:'...item.campaign.renew', data:{requestJson:...}}``"""
    return await _ad_write(client, ITEM_CAMPAIGN_RENEW_API, body, confirm)


async def activity_apply(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """活动报名。官方：``{api:'...activity.apply', v:'1.0', data:{requestJson:...}}``"""
    return await _ad_write(client, ACTIVITY_APPLY_API, body, confirm)


async def activity_task_execute(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """活动任务执行（领奖）。官方：``{api:'...activity.task.execute', v:'1.0', data:{requestJson:...}}``"""
    return await _ad_write(client, ACTIVITY_TASK_EXECUTE_API, body, confirm)


async def account_log_operates(client: Any, body: Any = None, confirm: Any = False) -> Optional[Dict[str, Any]]:
    """账户流水操作。官方：``{api:'...bp.account.log.operates', v:'1.0', data:{requestJson:...}}``"""
    return await _ad_write(client, ACCOUNT_LOG_OPERATES_API, body, confirm)


class WriteClient:
    """把上面的模块函数包成对象方法。

    这样服务层就能直接复用 ``common.call_method``（它负责跑协程、识别登录态过期并回写账号
    状态、把异常原样交回），而不用在协议层里 import services —— 层次不反。
    未定义的属性（``account_id`` / ``cookie_id`` 等）透传给真实客户端，登录态回写要靠它。
    """

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, '_client', client)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, '_client'), name)

    async def campaign_edit(self, body: Any = None, confirm: Any = False):
        return await campaign_edit(self._client, body, confirm)

    async def campaign_delete(self, body: Any = None, confirm: Any = False):
        return await campaign_delete(self._client, body, confirm)

    async def campaign_batch_delete(self, body: Any = None, confirm: Any = False):
        return await campaign_batch_delete(self._client, body, confirm)

    async def campaign_pause(self, body: Any = None, confirm: Any = False):
        return await campaign_pause(self._client, body, confirm)

    async def campaign_start(self, body: Any = None, confirm: Any = False):
        return await campaign_start(self._client, body, confirm)

    async def campaign_start_before(self, body: Any = None, confirm: Any = False):
        return await campaign_start_before(self._client, confirm)

    async def item_campaign_renew(self, body: Any = None, confirm: Any = False):
        return await item_campaign_renew(self._client, body, confirm)

    async def activity_apply(self, body: Any = None, confirm: Any = False):
        return await activity_apply(self._client, body, confirm)

    async def activity_task_execute(self, body: Any = None, confirm: Any = False):
        return await activity_task_execute(self._client, body, confirm)

    async def account_log_operates(self, body: Any = None, confirm: Any = False):
        return await account_log_operates(self._client, body, confirm)
