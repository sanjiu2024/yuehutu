"""``XianyuClient`` —— 一个账号的 mtop / 商品 / 图片 / 聊天门面。

设计取舍：**不引入 aiohttp**。旧项目 mtop 用 aiohttp、上传用 aiohttp、登录用
httpx，三套客户端各自读环境变量，于是 ``NO_PROXY`` 一坏就有三种不同的坏法。
这里统一走 httpx（``net.async_client`` 已经把代理显式关掉），少一个变量。

风控与登录态过期的分工在 ``_check_response`` 里，这是本文件最要紧的一段：
- 命中 ``RISK_CONTROL_MARKERS`` → ``risk.registry.trip()`` 熔断 + 抛 ``RiskControlError``；
- 命中 ``SESSION_EXPIRED_MARKERS`` → **不熔断**，抛 ``SessionExpiredError``，
  由调用方把账号置 ``need_relogin``。
把两者混在一起（旧项目早期就这么干过）会让「重新登录一下就好」的账号白等
20 分钟冷却。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from loguru import logger

from . import cookies as cookie_utils
from . import fingerprint, im, mtop, net, risk, store

# ---------------------------------------------------------------------------
# 接口常量。版本号**必须逐个硬编码**：``*.pc.idleitem.*`` 族是 1.0，
# ``downshelf`` / ``polish`` / ``kgraph`` 族是 2.0。写错时服务端返回
# 「接口不存在」，很容易被误判成签名错误，排查半天。
# ---------------------------------------------------------------------------
ITEM_DETAIL_API = 'mtop.taobao.idle.pc.detail'
ITEM_DETAIL_VERSION = '1.0'
ITEM_DETAIL_SPM = 'a21ybx.im.0.0'

EDIT_API = 'mtop.idle.pc.idleitem.edit'
EDIT_VERSION = '1.0'
EDIT_DETAIL_API = 'mtop.idle.pc.idleitem.editDetail'
EDIT_BIZCODE = 'pcMainPublish'
EDIT_PUBLISH_SCENE = 'pcMainPublish'

PUBLISH_API = 'mtop.idle.pc.idleitem.publish'
PUBLISH_VERSION = '1.0'
PUBLISH_SCENE = 'mainPublish'

# 发布**不是单请求**：漏掉分类推荐 / 注册分类路径任何一步，发布都会报
# `FAIL_BIZ_CHANNEL_CAT_ID_PATH_QUERY_ERROR`（看起来像签名/权限问题，其实缺步骤）。
CATEGORY_RECOMMEND_API = 'mtop.taobao.idle.kgraph.property.recommend'
CATEGORY_RECOMMEND_VERSION = '2.0'
CARDS_API = 'mtop.idle.item.publish.service.cards.list'
CARDS_VERSION = '1.0'
BADWORDS_API = 'mtop.taobao.idleitem.badwords.prepubcheck'
BADWORDS_VERSION = '2.0'

# 商品列表（我的闲鱼）—— 版本 1.0，spm 属于「个人中心」族。
ITEM_LIST_API = 'mtop.idle.web.xyh.item.list'
ITEM_LIST_VERSION = '1.0'
ITEM_LIST_SPM = 'a21ybx.personal.0.0'
ITEM_LIST_PAGE_SIZE = 20
# 翻页上限与翻页间隔：真机一个账号几十件商品，多翻几页才拉得全；页与页之间
# 稍作停顿，避免连续打同一个接口被平台当成脚本刷（风控代价远大于多等这一下）。
ITEM_LIST_MAX_PAGES = 25
ITEM_LIST_PAGE_DELAY = 0.3

# 下架是 2.0 族；上架**没有独立接口**，靠 edit 改 itemStatus。
DOWNSHELF_API = 'mtop.taobao.idle.item.downshelf'
DOWNSHELF_VERSION = '2.0'
ITEM_STATUS_ON_SALE = 'onsale'
ITEM_STATUS_OFF_SHELF = 'downshelf'

DEFAULT_CHANNEL_CATE_ID = '201459411'
DEFAULT_CHANNEL_CATE_NAME = '其他闲置'
DEFAULT_TB_CAT_ID = '50014945'

# 平台侧只认字符串 'false' 的开关集合（旧项目实测：传布尔平台不认）。
USER_RIGHTS_PROTOCOLS = [
    {'enable': False, 'serviceCode': 'FAST_DELIVERY_48_HOUR'},
    {'enable': False, 'serviceCode': 'FAST_DELIVERY_24_HOUR'},
    {'enable': False, 'serviceCode': 'NONCONFORMITY_FREE_REFUND'},
]

IM_TOKEN_API = 'mtop.taobao.idlemessage.pc.login.token'
IM_TOKEN_VERSION = '1.0'
IM_TOKEN_SPM = 'a21ybx.im.0.0'
USER_PROFILE_API = 'mtop.idle.web.user.page.head'

# 「商品数据」（曝光次数 / 浏览次数 / 想要人数）。全平台只有**卖家工作台**
# （seller.goofish.com）有这一个商品级统计接口 —— www.goofish.com 的 27 个页面
# chunk 里没有任何统计页与统计接口（2026-10-07 全量拉取比对）。
# 接口名与参数从工作台 0.1.68 的打包产物里反解出来（`vendors.js`）：
#     wP = function (eu) {
#         return G({api: 'mtop.alibaba.idle.seller.pc.item.stats.query',
#                   v: '1.0', data: {itemId: eu}});
#     }
# 版本号硬编码：本项目铁律是 mtop 版本同族不统一，必须逐个从真机/产物确认取值。
ITEM_STATS_API = 'mtop.alibaba.idle.seller.pc.item.stats.query'
ITEM_STATS_VERSION = '1.0'
ITEM_STATS_SPM = 'a21ybx.im.0.0'


class XianyuError(RuntimeError):
    """协议层通用错误。

    ``ret`` 保存平台原始的 ``ret`` 列表（如 ``['FAIL_BIZ_BADWORD::...']``）。
    调用方**必须**据此判断业务原因，不要去搜索 ``str(exc)`` —— 异常文本里带着
    api 名（``...badwords.prepubcheck``），搜 `BADWORD` 会把接口自身的任何失败
    都误判成「命中违禁词」。
    """

    def __init__(self, message: str, ret: Optional[List[str]] = None):
        super().__init__(message)
        self.ret = list(ret or [])


class SessionExpiredError(XianyuError):
    """登录态过期。**不是风控** —— 调用方应把账号置 need_relogin。"""


class RiskControlError(XianyuError):
    """被风控拦截。调用方应读 ``retry_after`` 决定等待时长。"""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class ItemStatsUnavailable(XianyuError):
    """商品数据接口这次拿不到数据（**不是账号掉线**）。

    `item_stats()` 早先在这三种情况下统一 `return None`，上层看到的是「调用成功
    但没有内容」——于是「卖家工作台会话拿不到」「接口改名/下线」「响应里没有指标」
    在接口和界面上长得一模一样（一律「平台返回的商品数据无法识别: 」），用户拿不到
    任何可行动的信息。现在带上原因抛出来，由 `services/item_stats.py` 如实转达。

    **故意不继承 `SessionExpiredError`**：`seller.pc.*` 这个接口族对 web Cookie
    天然拒绝，账号本身是好的；继承的话 `services/common.py` 会
    `store.mark_need_relogin()`，把好账号标成「需重新登录」。
    """


class XianyuClient:
    """一个闲鱼账号的协议客户端。

    ``cookie_str`` 是完整 Cookie 串（必须含 ``unb``，否则拿不到自己的 IM id）。
    实例是**有状态**的：mtop 响应续期的 ``_m_h5_tk`` 会写回 ``self.cookie_str``，
    IM 连接也会被复用。所以一个账号一个实例，不要跨账号共享。
    """

    def __init__(self, cookie_str: str, client=None, transport=None):
        if not cookie_str:
            raise ValueError('cookie_str 不能为空')
        self.cookie_str = str(cookie_str)
        self.cookie_id = cookie_utils.parse_cookie_string(self.cookie_str).get('unb', '')
        if not self.cookie_id:
            # 没有 unb 就没法定位账号，也没法按 cookie_id 熔断。宁可现在报错，
            # 也不要带着空 id 跑，最后变成「所有账号共用一个熔断桶」。
            raise ValueError('Cookie 里缺少 unb，无法确定账号 ID')
        self._client = client
        self._transport = transport
        # 「这次是替哪个账号调用的」，由服务层（``services/common.get_client``）注入。
        # mtop 续期后的 Cookie 要写回**这个账号**的库行；没有注入时退回 cookie_id
        # （真实账号的 accounts.id 就是 Cookie 里的 unb，两者一致）。
        self.persist_account_id = ''
        self._im: Optional[im.ImSession] = None
        # 注意：这个属性**绝不能叫 `_im_token`** —— 那会遮蔽同名的 `_im_token()` 方法，
        # 实例属性把绑定方法顶掉之后 `await self._im_token()` 就是 `None()`，
        # 报 `TypeError: 'NoneType' object is not callable`。真机后果是账号明明登录着，
        # 后端启动却永远只打一行「IM 入站未挂载」，一条买家消息都收不到。
        self._im_access_token: Optional[str] = None
        self._im_did = ''
        # 「新 IM 会话建好」的钩子。协议层不认识 services，由服务层
        # （`services.im_service.bind`）在这里挂回调，把入站 handler 注入会话。
        self._im_session_hooks: List[Callable[[im.ImSession], Any]] = []

    # -- 基础设施 ---------------------------------------------------------

    async def _http(self):
        if self._client is None:
            self._client = net.async_client(transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._im is not None:
            await self._im.close()
            self._im = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    # -- 风控/登录态分流 --------------------------------------------------

    def _check_response(self, payload: Any, message: str) -> None:
        """按响应内容决定「熔断」「置重登」还是「放行」。

        顺序要紧：先判登录态过期。``FAIL_SYS_SESSION_EXPIRED`` 的文本里
        不含风控标记，但如果将来平台在过期消息里夹带了 ``请稍后重试``，
        先判风控就会把「重登即可」误熔断。
        """
        if risk.is_session_expired(message):
            logger.warning(f'[{self.cookie_id}] 登录态过期，需要重新登录: {message}')
            raise SessionExpiredError(message)
        if risk.is_risk_control_error(message):
            retry_after = self._extract_retry_after(payload)
            cooldown = risk.registry.trip(self.cookie_id, message, retry_after)
            logger.warning(
                f'[{self.cookie_id}] 命中风控，冷却 {cooldown}s: {message}'
            )
            raise RiskControlError(message, retry_after=cooldown)

    @staticmethod
    def _extract_retry_after(payload: Any) -> int:
        """从响应里挖「建议等待秒数」。挖不到就返回 0，由本地阶梯决定。"""
        if not isinstance(payload, dict):
            return 0
        data = payload.get('data')
        if not isinstance(data, dict):
            return 0
        for key in ('retryAfter', 'retry_after', 'waitSeconds'):
            value = data.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return 0

    # -- MTOP -------------------------------------------------------------

    async def mtop(self, api: str, data: Any = None, v: str = '1.0',
                   spm_cnt: str = mtop.DEFAULT_SPM_CNT) -> Dict[str, Any]:
        """调一次 MTOP，返回 ``{"success", "data", "message", "ret", "cookies_str"}``。

        与旧实现的差别：**成功才 reset 熔断计数**。旧实现只在调用方显式
        ``guard.reset()`` 时清零，结果「连续命中」几乎永远在累加，阶梯很快
        爬到 20 分钟。

        另外两处是「令牌握手」：
        * 库里 Cookie 没有 ``_m_h5_tk`` 时，第一次调用必然返回
          ``FAIL_SYS_TOKEN_EMPTY::令牌为空`` —— 失败响应本身就带着新的
          ``_m_h5_tk``，所以这里**带新令牌重签一次**（仅一次）。
        * 换到的令牌要**写回库**，否则每次 ``common.get_client`` 新建客户端
          都要重来一遍，而只调一次的路径（取资料 / 商品同步）永远等不到重试。
        """
        guard = risk.registry.get(self.cookie_id)
        if guard.is_blocked:
            raise RiskControlError(
                f'账号处于风控冷却中，还需 {guard.remaining_seconds}s',
                retry_after=guard.remaining_seconds,
            )

        data_str = mtop.encode_data(data)
        used_token = ''
        for attempt in (1, 2):
            t = str(int(time.time() * 1000))
            used_token = mtop.token_from_cookies(self.cookie_str)
            params = mtop.call_params(api, v, data_str, used_token, t, spm_cnt)
            headers = fingerprint.headers({
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'origin': 'https://www.goofish.com',
                'referer': 'https://www.goofish.com/',
                'cookie': self.cookie_str.replace('\n', '').replace('\r', ''),
            })

            client = await self._http()
            try:
                response = await client.post(
                    mtop.api_url(api, v), params=params, data={'data': data_str},
                    headers=headers,
                )
            except Exception as exc:
                raise XianyuError(f'MTOP 请求异常: {exc}') from exc

            # 无论成败都要吸收续期的 token cookie：失败响应里也常带着新的
            # _m_h5_tk，丢掉它下一次调用还是用旧 token，签名继续失败，死循环。
            self.cookie_str = mtop.merge_token_cookies(self.cookie_str, response)
            self._persist_token_cookies()

            try:
                payload = response.json()
            except Exception:
                text = (response.text or '')[:200]
                raise XianyuError(
                    f'MTOP 响应不是 JSON（HTTP {response.status_code}）: {text}'
                )

            success, message, ret_list = mtop.parse_ret(payload)
            if success:
                guard.reset()
                return {
                    'success': True,
                    'data': payload.get('data') if isinstance(payload, dict) else None,
                    'message': message,
                    'ret': ret_list,
                    'cookies_str': self.cookie_str,
                }

            # 先分流登录态过期/风控（会抛），再判要不要为令牌重试。
            self._check_response(payload, message)
            fresh_token = mtop.token_from_cookies(self.cookie_str)
            retryable = attempt == 1 and (
                mtop.is_token_error(ret_list) or mtop.is_token_error(message)
            )
            if retryable and fresh_token and fresh_token != used_token:
                logger.warning(
                    f'[{self.cookie_id}] MTOP 签名令牌不可用（可自愈），'
                    f'带响应下发的新令牌重试一次: {message}'
                )
                continue
            raise XianyuError(f'MTOP 调用失败 [{api}]: {message}', ret=ret_list)

        # 循环只可能从 continue 走到这里（attempt==2 的分支一定会抛），
        # 保底抛一条，避免静态分析认为「可能返回 None」。
        raise XianyuError(f'MTOP 调用失败 [{api}]: 令牌重试后仍未成功')

    def _persist_token_cookies(self) -> None:
        """把续期后的 Cookie 写回库，让下一个客户端实例直接带上令牌。"""
        account_id = getattr(self, 'persist_account_id', '') or self.cookie_id
        try:
            store.refresh_token_cookies(account_id, self.cookie_str)
        except Exception as exc:  # noqa: BLE001 - 落库失败不能打断业务调用
            logger.debug(f'[{self.cookie_id}] 续期 Cookie 落库失败: {exc}')

    # -- 商品 -------------------------------------------------------------

    async def item_detail(self, item_id: str) -> Dict[str, Any]:
        """商品原始详情（``data`` 段原样返回）。"""
        result = await self.mtop(
            ITEM_DETAIL_API, {'itemId': str(item_id)},
            v=ITEM_DETAIL_VERSION, spm_cnt=ITEM_DETAIL_SPM,
        )
        return result.get('data') or {}

    async def get_item(self, item_id: str) -> Dict[str, Any]:
        """商品详情（归一化：id/标题/价格/图片/状态）。

        归一化放在协议层，是因为不同接口给的字段名不一样（``itemDO`` /
        ``item`` / 扁平），业务层不该关心这些。
        """
        data = await self.item_detail(item_id)
        return normalize_item(data, str(item_id))

    async def edit_item_detail(self, item_id: str) -> Dict[str, Any]:
        """编辑页回填用的详情（与 ``item_detail`` 是两个接口，字段不同）。"""
        result = await self.mtop(EDIT_DETAIL_API, {'itemId': str(item_id)}, v=EDIT_VERSION)
        return result.get('data') or {}

    async def list_items(self, page_number: int = 1, page_size: int = ITEM_LIST_PAGE_SIZE,
                         max_pages: int = ITEM_LIST_MAX_PAGES) -> Dict[str, Any]:
        """拉「我的闲鱼」商品列表（在售/已下架都在 ``itemTopicList`` 里）。

        ``spm_cnt`` 必须是列表族 ``a21ybx.personal.0.0``：发布族的默认 spm 打这个
        接口会返回空 body / 接口不存在。

        第一页必须带 ``needGroupInfo=True`` 才会回 ``itemGroupList``（分组），
        后续翻页要带上分组字段。单页失败不抛出 —— 已拿到的商品照常返回。

        **翻页判据看 ``nextPage`` 的布尔/数字两种形态**（见 ``_has_next_page``），
        不再把布尔 ``true`` 当成页码 1 —— 那正是「永远只显示 20 件」的根因。
        ``totalCount`` 真机恒为 0，所以对外 ``total`` 用实际拿到的条数兜底。
        """
        page_size = max(1, min(int(page_size or ITEM_LIST_PAGE_SIZE), 100))
        max_pages = max(1, int(max_pages or 1))
        items: List[Dict[str, Any]] = []
        seen: set = set()
        groups: List[Dict[str, Any]] = []
        total = 0
        next_page = 0

        for index in range(max_pages):
            number = int(page_number or 1) + index
            body: Dict[str, Any] = {
                'pageNumber': number,
                'pageSize': page_size,
                'userId': self.cookie_id,
            }
            if index == 0:
                body['needGroupInfo'] = True
            else:
                body['needGroupInfo'] = False
                if groups:
                    group = groups[0]
                    body['groupName'] = group.get('groupName') or ''
                    body['groupId'] = str(group.get('groupId') or '')
                    body['defaultGroup'] = bool(group.get('defaultGroup'))
            result = await self.mtop(ITEM_LIST_API, body, v=ITEM_LIST_VERSION,
                                     spm_cnt=ITEM_LIST_SPM)
            data = result.get('data') or {}
            if not isinstance(data, dict):
                break
            if index == 0:
                raw_groups = data.get('itemGroupList') or data.get('groupList') or []
                groups = [g for g in raw_groups if isinstance(g, dict)]
            total = int(data.get('totalCount') or total or 0)
            has_next = _has_next_page(data.get('nextPage'), number)
            next_page = number + 1 if has_next else 0
            page_items = _extract_list_cards(data)
            fresh = 0
            for card in page_items:
                normalized = normalize_list_card(card)
                if not normalized:
                    continue
                key = normalized['item_id']
                if key in seen:
                    continue
                seen.add(key)
                items.append(normalized)
                fresh += 1
            # 没有卡片、没有新商品（平台在重复回同一页）、或平台说没有下一页，就收工。
            if not page_items or not fresh or not has_next:
                break
            if index + 1 < max_pages:
                await asyncio.sleep(ITEM_LIST_PAGE_DELAY)

        return {'items': items, 'total': total or len(items), 'next_page': next_page,
                'groups': groups, 'page': int(page_number or 1), 'size': page_size}

    async def recommend_category(self, title: str, description: str = '',
                                 images: Optional[List[Dict[str, Any]]] = None):
        """分类推荐：返回 ``(transport_data, card_list)``，失败时两者都为 None。

        **失败不阻断发布**（退回默认分类），但 card_list 为空时调用方必须跳过
        «注册分类路径» 那一步。
        """
        stamp = int(time.time() * 1000)
        body = {
            'title': title,
            'lockCpv': False,
            'multiSKU': False,
            'publishScene': PUBLISH_SCENE,
            'scene': 'newPublishChoice',
            'description': description or title,
            'imageInfos': images or [],
            'uniqueCode': f'{stamp}{stamp % 1000}',
        }
        try:
            result = await self.mtop(CATEGORY_RECOMMEND_API, body,
                                     v=CATEGORY_RECOMMEND_VERSION)
        except XianyuError as exc:
            logger.warning(f'[{self.cookie_id}] 分类推荐失败，将使用默认分类: {exc}')
            return None, None
        data = result.get('data')
        if not isinstance(data, dict):
            return None, None
        transport = data.get('transportData')
        cards = data.get('cardList') or data.get('card_list')
        return (transport if isinstance(transport, dict) else None), (
            cards if isinstance(cards, list) else None)

    async def register_publish_cards(self, card_list: List[Any],
                                     item_info: Dict[str, Any]) -> bool:
        """注册分类路径 session。**跳过这步发布必报**
        ``FAIL_BIZ_CHANNEL_CAT_ID_PATH_QUERY_ERROR``。

        ``cpvList`` / ``itemInfoJson`` 是「JSON 再序列化一层塞进字符串」，
        ``multiSkuEditingMode`` 也是字符串 ``'false'``。失败只记日志、不阻断。
        """
        if not card_list:
            return False
        body = {
            'cpvList': json.dumps(card_list, ensure_ascii=False),
            'itemInfoJson': json.dumps(item_info, ensure_ascii=False),
            'param': json.dumps({
                'multiSkuEditingMode': 'false',
                'settingsPreferences': None,
                'supportDefaultOpen': True,
            }, ensure_ascii=False),
        }
        try:
            await self.mtop(CARDS_API, body, v=CARDS_VERSION)
        except XianyuError as exc:
            logger.warning(f'[{self.cookie_id}] 注册分类卡片失败（发布可能因类目路径报错）: {exc}')
            return False
        return True

    async def check_bad_words(self, title: str) -> None:
        """违禁词预检。**预检接口自身失败按通过处理**（发布接口自己也会校验，
        不该因为预检服务抖动就拦住发布）；只有明确命中才抛 ``XianyuError``。"""
        body = {
            'title': title,
            'scene': 'pcPublish',
            'sessionId': f'{int(time.time() * 1000)}{self.cookie_id}',
        }
        try:
            await self.mtop(BADWORDS_API, body, v=BADWORDS_VERSION)
        except XianyuError as exc:
            # 只看平台 ret，**不搜索 str(exc)**：异常文本里带 api 名
            # `...badwords.prepubcheck`，搜 'BADWORD' 会把预检接口自己的任何
            # 失败都误判成「命中违禁词」，把本该继续的发布拦下。
            if _ret_hits_bad_word(exc.ret):
                raise XianyuError(f'标题含违禁词: {"; ".join(exc.ret)}', ret=exc.ret) from exc
            logger.warning(f'[{self.cookie_id}] 违禁词预检异常，按通过处理: {exc}')

    async def _publish_images(self, images: Sequence[Any]) -> List[Dict[str, Any]]:
        """把本地 ``images``（URL 或本地文件路径）转成 ``imageInfoDOList``。"""
        infos: List[Dict[str, Any]] = []
        for index, entry in enumerate(images or []):
            if isinstance(entry, dict):
                info = dict(entry)
                info.setdefault('major', index == 0)
                infos.append(info)
                continue
            text = str(entry or '').strip()
            if not text:
                continue
            if text.startswith(('http://', 'https://')):
                infos.append(build_image_info(text, is_major=(index == 0)))
                continue
            uploaded = await self.upload_image(text)
            infos.append(build_image_info(
                str(uploaded.get('url') or ''), is_major=(index == 0),
                file_id=uploaded.get('fileId'),
                width=int(uploaded.get('width') or 800),
                height=int(uploaded.get('height') or 800),
            ))
        return infos

    async def publish_item(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发布商品（完整流程）：上传图片 → 分类推荐 → 注册分类路径 → 发布。

        入参是**业务层字段名**（``title/price/description/images/stock/...``），
        协议层负责映射成平台字段并补齐发布族必填项。返回
        ``{'item_id', 'itemId', 'raw', ...}``。
        """
        payload = payload or {}
        title = str(payload.get('title') or '').strip()
        if not title:
            raise XianyuError('发布缺少标题')
        raw_images = payload.get('images')
        if isinstance(raw_images, str):
            raw_images = [raw_images]
        images = await self._publish_images(list(raw_images or []))
        if not images:
            raise XianyuError('至少需要一张图片')

        await self.check_bad_words(title)

        description = payload.get('description') or payload.get('desc') or ''
        transport, card_list = await self.recommend_category(title, description, images)

        channel_cate_id = payload.get('channel_cate_id')
        channel_cate_name = payload.get('channel_cate_name')
        tb_cat_id = payload.get('tb_cat_id') or payload.get('category_id')
        user_picked = bool(channel_cate_id and channel_cate_name)
        label = build_label_ext(
            None if user_picked else transport,
            channel_cate_id, channel_cate_name, tb_cat_id,
        )

        body = build_publish_payload(
            title=title,
            price=payload.get('price'),
            desc=description,
            orig_price=payload.get('original_price', payload.get('orig_price')),
            images=images,
            label_ext=label,
            quantity=payload.get('stock', payload.get('quantity', 1)),
            free_shipping=payload.get('free_shipping', True),
            addr=payload.get('addr'),
        )

        # 漏了这步发布必报 FAIL_BIZ_CHANNEL_CAT_ID_PATH_QUERY_ERROR
        if card_list:
            await self.register_publish_cards(card_list, body)

        body['uniqueCode'] = mtop.generate_uuid()
        result = await self.mtop(PUBLISH_API, body, v=PUBLISH_VERSION)
        data = result.get('data') or {}
        item_id = ''
        if isinstance(data, dict):
            item_id = str(data.get('itemId') or data.get('item_id') or '')
        return {'item_id': item_id, 'itemId': item_id, 'raw': data}

    async def edit_item(self, item_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """编辑商品：**取回原表单 → 只打目标字段的补丁 → 提交**。

        绝不用「零构造 payload」直接提交 —— 那会静默丢掉类目属性、地址、
        议价设置等所有没显式传的字段。``payload`` 用业务层字段名
        （``price/title/description/stock/status/original_price/free_shipping``）。

        上架没有独立接口，``reshelf`` 就是 ``status='on_sale'`` 走到这里。
        """
        if not item_id:
            raise XianyuError('缺少商品ID')
        changes = _local_changes_to_edit(payload or {})
        if not changes:
            raise XianyuError('没有可提交到平台的字段')

        detail = await self.edit_item_detail(item_id)
        if not isinstance(detail, dict) or not detail:
            raise XianyuError(f'读取商品详情失败: 商品 {item_id} 的 editDetail 为空')

        body = build_edit_submit(detail, changes)
        body['itemId'] = str(item_id)
        body['sourceId'] = str(item_id)
        result = await self.mtop(EDIT_API, body, v=EDIT_VERSION)
        data = result.get('data')
        return data if isinstance(data, dict) else {}

    async def downshelf(self, item_id: str) -> Dict[str, Any]:
        """下架（``mtop.taobao.idle.item.downshelf`` **v2.0**，不是 1.0）。"""
        if not item_id:
            raise XianyuError('缺少商品ID')
        result = await self.mtop(DOWNSHELF_API, {'itemId': str(item_id)},
                                 v=DOWNSHELF_VERSION)
        data = result.get('data')
        return data if isinstance(data, dict) else {}

    async def reshelf(self, item_id: str) -> Dict[str, Any]:
        """上架。**平台没有独立的上架接口** —— 就是编辑流程改 ``itemStatus``。"""
        return await self.edit_item(item_id, {'status': 'on_sale'})

    async def set_status(self, item_id: str, status: str) -> Dict[str, Any]:
        """按业务状态名分发：``on_sale`` → 上架，其它 → 下架。"""
        text = str(status or '').strip().lower()
        if text in ('on_sale', 'onsale', 'on_sale_item'):
            return await self.reshelf(item_id)
        return await self.downshelf(item_id)

    async def user_profile(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """账号公开资料（昵称 / 头像 / 卖家等级）。"""
        result = await self.mtop(
            USER_PROFILE_API, {'userId': str(user_id or self.cookie_id)}, v='1.0'
        )
        return parse_user_profile(result.get('data') or {})

    async def item_stats(self, item_id: str = '') -> Optional[Dict[str, Any]]:
        """商品数据：曝光次数 / 浏览次数 / 想要人数。拿不到时抛 ``ItemStatsUnavailable``。

        三个字段是平台真名（``data.exposureNum`` / ``data.showNum`` /
        ``data.wantNum``，出处是卖家工作台组件渲染这三行时的读法），这里只做
        「原样解析 + 改写成服务层认的别名」，**不补 0、不猜数**：
        ``0`` 与「平台没给」在前端是两回事，硬塞 0 会让用户以为商品真没人看。

        ⚠️ **为什么必须自己吞掉 ``SessionExpiredError``**：这个接口属于
        ``seller.pc.*`` 族，要的是卖家工作台会话。真机实测（2026-10-07，账号
        ``2217970177348``，共 9 次只读调用）：同一个 Cookie 打
        ``mtop.idle.web.user.page.head`` 回 ``SUCCESS::调用成功``，打这个接口却回
        ``FAIL_SYS_SESSION_EXPIRED::Session过期``（补上工作台的
        ``Origin``/``Referer`` 也一样）。可见被拒的是**接口族**，不是账号登录态。
        而 ``services/common.py`` 收到 ``SessionExpiredError`` 会
        ``store.mark_need_relogin()`` —— 让「商品数据」这种只读的降级调用把好账号
        标成「需重新登录」，用户会莫名其妙被提示掉线。所以这里本地消化掉，
        同时**不累加风控计数**（``mtop`` 对登录态过期本来就不熔断）。

        风控异常照旧往上抛：真被风控了要让上层知道（冷却计数已经记在
        ``risk.registry`` 里，瞒下来反而让用户看不到原因）。

        「本地消化」= 换成 ``ItemStatsUnavailable``（带上原因）而不是
        ``return None``：返回 None 时上层只能看到「调用成功但没内容」，
        于是「会话拿不到 / 接口下线 / 响应没指标」在界面上全变成同一句
        「无法识别」，用户没有任何可行动的信息。
        """
        if not item_id:
            raise ItemStatsUnavailable('缺少 itemId：商品数据接口只能按商品调用')
        try:
            result = await self.mtop(
                ITEM_STATS_API, {'itemId': str(item_id)},
                v=ITEM_STATS_VERSION, spm_cnt=ITEM_STATS_SPM,
            )
        except RiskControlError:
            raise
        except SessionExpiredError:
            logger.info(
                f'[{self.cookie_id}] 商品数据接口需要卖家工作台会话，'
                f'当前 Cookie 拿不到（不是账号掉线），降级为无平台数据'
            )
            raise ItemStatsUnavailable(
                '需要卖家工作台会话：这个接口族不接受网页版 Cookie（账号本身没掉线）'
            ) from None
        except XianyuError as exc:
            logger.info(f'[{self.cookie_id}] 商品数据接口不可用，降级: {exc}')
            raise ItemStatsUnavailable(f'接口不可用: {exc}') from exc
        parsed = parse_item_stats(result.get('data'))
        if parsed is None:
            # 拿到了响应但认不出指标：也要说清是「这一次响应没指标」，不是「平台没有这个接口」
            raise ItemStatsUnavailable(
                f'平台响应里没有可识别的商品数据指标（data={str(result.get("data"))[:200]}）'
            )
        return parsed

    # -- 图片上传 ---------------------------------------------------------

    async def upload_image(self, path: str) -> Dict[str, Any]:
        """上传本地图片到闲鱼 CDN，返回 ``{"url", "fileId", "width", "height"}``。

        ``floderId=0`` + ``appkey=fleamarket`` 是发布场景（聊天场景用 xy_chat）。
        尺寸取响应里的 ``object.pix``（形如 ``"800x800"``），解析不出来退 800x800。
        """
        if not path or not os.path.isfile(path):
            raise XianyuError(f'图片不存在: {path}')
        with open(path, 'rb') as handle:
            raw = handle.read()
        filename = os.path.basename(path)
        if not filename.lower().endswith(('.jpg', '.jpeg')):
            filename = os.path.splitext(filename)[0] + '.jpg'

        client = await self._http()
        headers = fingerprint.headers({
            'cookie': self.cookie_str,
            'referer': 'https://www.goofish.com/',
            'x-requested-with': 'XMLHttpRequest',
            'accept': 'application/json, text/javascript, */*; q=0.01',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
        })
        response = await client.post(
            mtop.UPLOAD_URL,
            params={'floderId': '0', 'appkey': mtop.UPLOAD_APPKEY, '_input_charset': 'utf-8'},
            files={'file': (filename, raw, 'image/jpeg')},
            headers=headers,
        )
        text = response.text or ''
        if '<html' in text.lower():
            # 返回登录页 = Cookie 失效。这里必须报「重登」而不是「上传失败」，
            # 否则用户会一直重试上传，永远看不到「该重新登录了」。
            raise SessionExpiredError('图片上传返回登录页，Cookie 已失效')
        try:
            payload = response.json()
        except Exception as exc:
            raise XianyuError(f'图片上传响应不是 JSON: {text[:200]}') from exc
        return parse_upload_response(payload)

    # -- IM ---------------------------------------------------------------

    async def _im_token(self) -> str:
        """取 IM 的 accessToken（走 mtop，带缓存）。"""
        if self._im_access_token:
            return self._im_access_token
        result = await self.mtop(
            IM_TOKEN_API,
            {'appKey': im.IM_APP_KEY, 'deviceId': self._im_device_id()},
            v=IM_TOKEN_VERSION, spm_cnt=IM_TOKEN_SPM,
        )
        token = ((result.get('data') or {}).get('accessToken')) or ''
        if not token:
            raise XianyuError('IM token 接口未返回 accessToken')
        self._im_access_token = token
        return token

    def _im_device_id(self) -> str:
        """本账号的 IM 设备 ID。

        取 token 时发给平台的 ``deviceId`` 与 ``/reg`` 帧里的 ``did`` **必须是
        同一个值** —— 服务端比对二者，不一致就回
        ``401 device id or appkey is not equal``，表现为「IM 挂载成功但一条消息
        都收不到」。所以这里缓存一次、两处共用。
        """
        if not self._im_did:
            self._im_did = mtop.generate_device_id(self.cookie_id)
        return self._im_did

    async def _ensure_im(self) -> im.ImSession:
        if self._im is not None and self._im.connected:
            return self._im
        token = await self._im_token()
        session = im.ImSession(self.cookie_str, token, self.cookie_id,
                               device_id=self._im_device_id())
        await session.connect()
        self._im = session
        # 会话能收帧了再通知钩子。放在赋值之后：钩子通过 on_im_session 拿到句柄时，
        # 这条会话已经是 `self._im`，重复注册不会造出第二条会话。
        self._notify_im_session(session)
        return session

    async def ensure_im(self) -> im.ImSession:
        """拿（必要时建）IM 会话。服务层用它，避免去调私有名 `_ensure_im`。"""
        return await self._ensure_im()

    def on_im_session(self, hook: Callable[[im.ImSession], Any]) -> Callable[[im.ImSession], Any]:
        """注册「新 IM 会话建好」回调，返回 hook 本身。

        同一个 hook 重复注册只保留一份：钩子是幂等注册入站 handler 的入口，
        重复触发会变成「一封来信触发多次 AI」。
        """
        if hook not in self._im_session_hooks:
            self._im_session_hooks.append(hook)
        return hook

    def _notify_im_session(self, session: im.ImSession) -> None:
        """逐个通知钩子。钩子抛异常只记日志 —— 不能让入站 handler 注册失败连带
        把「建 IM 会话」本身搞挂（那会退化成一条消息都收不到）。"""
        for hook in list(self._im_session_hooks):
            try:
                hook(session)
            except Exception as exc:  # noqa: BLE001 - 钩子是外部注入的，什么都能抛
                logger.warning(f'IM 会话钩子抛异常，已跳过: {exc}')

    async def send_message(self, cid: str, text: str, toid: Optional[str] = None) -> Dict[str, Any]:
        """给会话发文本。``toid`` 省略时用 ``cid``（单聊里两者相同）。"""
        session = await self._ensure_im()
        return await session.send_message(cid, toid or cid, text)

    async def fetch_messages(self, cid: str, start_timestamp: Optional[int] = None,
                             limit: int = 20) -> List[Dict[str, Any]]:
        session = await self._ensure_im()
        return await session.fetch_messages(cid, start_timestamp, limit)

    async def fetch_conversations(self, start_timestamp: int = im.LATEST_TIMESTAMP,
                                  limit: int = 50) -> List[Dict[str, Any]]:
        """拉会话列表（默认从最新往回）。

        ``start_timestamp=0`` 在平台的语义里不是「最新」，旧项目用的是
        ``9007199254740991``（``im.LATEST_TIMESTAMP``），所以默认值就用它。
        """
        session = await self._ensure_im()
        return await session.fetch_conversations(start_timestamp, limit)


# ---------------------------------------------------------------------------
# 响应解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def normalize_item(data: Dict[str, Any], item_id: str = '') -> Dict[str, Any]:
    """把详情接口的响应归一化成业务层好用的形状。

    **库存只能从这里来**：列表卡没有库存字段，真库存在详情
    ``data.itemDO.quantity``（真机 1087247101297 → 1、1087258469593 → 66，
    ``edit_item_detail`` 给的是字符串 ``"66"``）。取不到就是 ``None``，不编数。
    """
    if not isinstance(data, dict):
        return {'id': item_id, 'stock': None}
    # 不同版本的详情接口外层键名不同，逐个试。
    inner = data.get('itemDO') or data.get('item') or data
    if not isinstance(inner, dict):
        inner = data

    # 详情图片：`picDetailDO` 这一族的**精确形状没抓到真机帧**（无真机访问权限），
    # 所以按「dict / list / JSON 字符串」三种形态统一处理，并保留对详情节点本身的
    # 兜底深扫（只认 http(s)/// 开头的 URL），避免再出现「图片全空」。
    images = _merge_images(
        inner.get('picDetailDO'),
        inner.get('picDetail'),
        inner.get('picInfoDO'),
        inner.get('imageInfoDOList'),
        inner.get('imageInfos'),
        inner.get('images'),
        inner.get('imageList'),
        inner.get('picList'),
        data.get('picDetailDO'),
    )
    if not images:
        images = _merge_images(inner)

    price = inner.get('price')
    if isinstance(price, dict):
        price = price.get('price') or price.get('value')

    # 库存：int（详情）与 str（编辑回填 "66"）都要认；认不出就给 None。
    quantity = inner.get('quantity')
    if quantity in (None, ''):
        quantity = inner.get('stock')
    if quantity in (None, ''):
        quantity = data.get('quantity')
    stock: Optional[int] = None
    if quantity not in (None, ''):
        try:
            stock = int(float(quantity))
        except (TypeError, ValueError):
            stock = None
    if stock is not None and stock < 0:
        stock = None

    return {
        'id': str(inner.get('itemId') or item_id),
        'title': inner.get('title') or '',
        'desc': inner.get('desc') or inner.get('description') or '',
        'price': price,
        'images': images,
        # 详情路径过去**原样透传**（`inner.get('itemStatus') or inner.get('status')`），
        # 于是同一个商品列表同步得到 `'sold'`、详情同步却得到裸 `1`（真实库存那条
        # 路径是 0 → `or` 吞掉 → `None`），两条路径写进同一列会互相打架。
        # 统一走 `canonical_status`，并且**按存在性取值**（数字 0 是 falsy，
        # 不能被 `or` 吞掉）。
        'status': canonical_status(
            inner['itemStatus'] if 'itemStatus' in inner else inner.get('status')
        ),
        'stock': stock,
        'raw': data,
    }


def parse_upload_response(payload: Any) -> Dict[str, Any]:
    """从上传响应里挖出图片 URL 与尺寸。

    响应形状有五种（``data.url`` / ``object.url`` / 根 ``url`` / ``result.url`` /
    ``data.fileUrl``），旧项目为此写了五段 if。这里保留全部兼容分支，因为
    不同场景（发布 / 聊天）走的其实是不同后端，形状确实不一样。
    """
    if not isinstance(payload, dict):
        raise XianyuError('图片上传响应不是 JSON 对象')

    url = ''
    for getter in (
        lambda p: (p.get('data') or {}).get('url'),
        lambda p: (p.get('object') or {}).get('url'),
        lambda p: p.get('url'),
        lambda p: (p.get('result') or {}).get('url'),
        lambda p: (p.get('data') or {}).get('fileUrl'),
        lambda p: (p.get('data') or {}).get('file_url'),
    ):
        try:
            candidate = getter(payload)
        except Exception:
            candidate = None
        if candidate:
            url = str(candidate)
            break
    if not url:
        raise XianyuError(f'无法从上传响应提取图片 URL: {str(payload)[:200]}')

    obj = payload.get('object') if isinstance(payload.get('object'), dict) else {}
    width, height = 800, 800
    pix = str(obj.get('pix') or '')
    if 'x' in pix:
        parts = pix.split('x')
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            width, height = int(parts[0]), int(parts[1])
    return {
        'url': _https(url),
        'fileId': obj.get('fileId') or (payload.get('data') or {}).get('fileId'),
        'width': width,
        'height': height,
    }


def parse_user_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    """解析 ``user.page.head`` 的响应。

    昵称在 ``module.base.displayName``，头像在 ``module.base.avatar.avatar``；
    头像接口回的是 http，页面用 https 加载会被浏览器拦掉，必须升级协议。
    信用标签里买家和卖家两个维度都有，只取 ``attributes.role == 'seller'`` 的。
    """
    module = (data or {}).get('module') or {}
    base = module.get('base') or {}
    social = module.get('social') or {}
    avatar = base.get('avatar') or {}
    avatar_url = avatar.get('avatar') if isinstance(avatar, dict) else avatar

    seller_level = None
    for tag in base.get('ylzTags') or []:
        attrs = (tag or {}).get('attributes') or {}
        if attrs.get('role') == 'seller':
            seller_level = tag.get('text') or ''
            break

    def _count(value):
        text = str(value or '').replace(',', '').strip()
        try:
            return int(float(text))
        except ValueError:
            return None

    return {
        'user_id': str(base.get('userId') or ''),
        'nickname': base.get('displayName') or '',
        'avatar_url': _https(str(avatar_url or '')),
        'seller_level': seller_level,
        'followers': _count(social.get('followerNum')),
        'following': _count(social.get('followingNum')),
    }


#: 平台真名 → 服务层别名。``services/item_stats.py::METRIC_ALIASES`` 只认
#: ``exposure`` / ``visits`` / ``visitors``，而 ``exposureNum`` / ``showNum``
#: **不在别名表里** —— 直接把平台响应透传过去会被判「返回内容里没有可识别的
#: 指标」而整条丢掉，所以必须在这里改写键名。
#: ``wantNum``（想要人数）服务层目前没有对应指标，先叫 ``wants``：宁可多带一个
#: 没人消费的键，也不要把「想要」硬塞进「访客数」—— 那是两个口径。
ITEM_STATS_FIELDS = (
    ('exposureNum', 'exposure'),   # 曝光次数
    ('showNum', 'visits'),         # 浏览次数
    ('wantNum', 'wants'),          # 想要人数
)


def _metric_value(value: Any) -> Any:
    """平台计数 → ``int``；不是干净数字就**原样保留字符串**；空值返回 ``None``。

    不做单位换算：``'1.2万'`` 这类文案原样透传（工作台自己就是照字符串渲染的），
    换错比不换更糟。``None`` / ``''`` 一律当「平台没给」，**不折算成 0**。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text.replace(',', '')))
    except ValueError:
        return text


def parse_item_stats(data: Any) -> Optional[Dict[str, Any]]:
    """解析 ``mtop.alibaba.idle.seller.pc.item.stats.query`` 的 ``data``。

    响应形状（卖家工作台组件的读法，取的是 ``resp.data.data``）::

        data.exposureNum   曝光（次）
        data.showNum       浏览（次）
        data.wantNum       想要（人）

    **这个接口没有的**：曝光人数 / 浏览人数 / 下单人数 / 支付人数 / 支付订单数，
    也没有任何时间窗口参数（近 1 天 / 7 天 / 30 天都取不到）—— 它只给三个累计值。
    缺的键一律不放进来：前端显示「—」比显示 0 诚实。

    识别不出任何指标（``data`` 是空 dict 或形状不对）时返回 ``None``。
    """
    if not isinstance(data, dict):
        return None
    stats: Dict[str, Any] = {}
    raw: Dict[str, Any] = {}
    for platform_key, alias in ITEM_STATS_FIELDS:
        if platform_key not in data:
            continue
        value = _metric_value(data.get(platform_key))
        if value is None:
            continue
        stats[alias] = value
        raw[platform_key] = data.get(platform_key)
    if not stats:
        return None
    # 留一份原始键值便于对着真机响应核对（服务层按别名取值，多余的键不影响）。
    stats['raw'] = raw
    return stats


def _https(url: str) -> str:
    if url.startswith('http://'):
        return 'https://' + url[len('http://'):]
    return url


# ---------------------------------------------------------------------------
# 列表接口解析
# ---------------------------------------------------------------------------

#: 平台「命中违禁词」的 ret 标记。只认这些，不认 api 名里的 badwords。
BAD_WORD_MARKERS = ('BADWORD', 'BAD_WORD', '违禁')


def _ret_hits_bad_word(ret: Optional[Sequence[Any]]) -> bool:
    """平台 ret 列表里是否有「命中违禁词」的标记。"""
    for item in ret or []:
        text = str(item).upper()
        if any(marker.upper() in text for marker in BAD_WORD_MARKERS):
            return True
    return False


_STATUS_MAP = {
    'onsale': 'on_sale', 'on_sale': 'on_sale', 'onSale': 'on_sale',
    'downshelf': 'off_shelf', 'off_shelf': 'off_shelf', 'offshelf': 'off_shelf',
}


def canonical_status(value: Any) -> str:
    """平台状态名 → 本地口径（``on_sale`` / ``off_shelf`` / ``sold``）。

    认不出来的**原样返回**（例如 ``'已下架'``），不要猜 —— 把「已卖出」硬说成
    「在售」会让统计页骗人。

    数字映射来自**真机证据（2026-10-06，账号 2217970177348）**：列表帧 45 件里
    ``itemStatus`` 分布是 **44×0、1×1**，唯一那个 ``1``（``1087247101297``）的
    ``item_detail`` 回 ``itemDO.itemStatus=1`` + ``itemDO.itemStatusStr="卖掉了"``。
    ⇒ **0 = 在售、1 = 已卖出（sold）**。

    ⚠️ 本函数此前写的是 ``1 → on_sale``、其余 → ``off_shelf``（注释称「按旧项目
    实测」），与真机**正好相反**；真机那件已卖出的商品一直被显示成「在售」。

    ``None``/``''`` 仍回 ``'on_sale'``：**这只是「平台没报下架」，不等于确认在售**。
    """
    if value is None or value == '':
        return 'on_sale'
    if isinstance(value, bool):
        # bool 是 int 的子类，真机没出现过布尔状态；别让 True/False 掉进数字分支。
        return str(value)
    if isinstance(value, (int, float)):
        number = int(value)
        if number == 0:
            return 'on_sale'
        if number == 1:
            return 'sold'
        # 其它数字（如 2/3）语义未证实 —— 原样返回，不猜。
        return str(value)
    text = str(value)
    return _STATUS_MAP.get(text, _STATUS_MAP.get(text.lower(), text))


def _has_next_page(flag: Any, current_number: int) -> bool:
    """判断平台回帧里的 ``nextPage`` 是否表示「还有下一页」。

    **真机实测（2026-10-06）**：``mtop.idle.web.xyh.item.list`` 回的
    ``nextPage`` 是**布尔** ``true``（而且 ``totalCount`` 恒为 0）。旧实现写的是
    ``next_page = int(data.get('nextPage') or 0)``，于是 ``int(True) == 1``；再拿它
    和当前页码比 ``next_page in (0, -1, number)`` —— 第一页正好 ``number == 1``，
    直接 break。结果：**一个账号不管有多少件商品，永远只入库 20 件**。

    这个字段两种形态都兼容：布尔当「还有下一页」的开关，数字当下一页页码。
    """
    if isinstance(flag, bool):
        return flag
    if flag is None or flag == '':
        return False
    try:
        number = int(flag)
    except (TypeError, ValueError):
        return False
    return number > current_number


def _extract_list_cards(data: Dict[str, Any]) -> List[Any]:
    """从列表响应里挖出商品卡。

    当前平台是 ``itemTopicList``，旧版是 ``cardList``；两个都试，按需往下钻一层
    （``data`` / ``module`` 里再套一层的情况遇到过）。
    """
    for key in ('itemTopicList', 'cardList', 'itemList'):
        raw = data.get(key)
        if isinstance(raw, list) and raw:
            return raw
    for wrapper in ('data', 'module', 'result'):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            found = _extract_list_cards(inner)
            if found:
                return found
    return []


def _first_text(mapping: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return str(value)
    return ''


#: 图片 URL 可能挂在这些键上（真机列表卡：`picInfo.picUrl` /
#: `detailParams.picUrl` / `detailParams.imageInfos[].url`）。
IMAGE_URL_KEYS = ('url', 'picUrl', 'imageUrl', 'imgUrl', 'image', 'pic', 'src',
                  'mainPic', 'mainImage', 'coverImage', 'itemImage')
_MAJOR_IMAGE_KEYS = ('major', 'isMajor', 'isMajorPic')
_IMAGE_RECURSE_SKIP = set(IMAGE_URL_KEYS) | set(_MAJOR_IMAGE_KEYS)


#: 忽略 query/fragment 之后，路径必须以其中之一结尾才算图片。
#: `.heic` 必须留着：真机库里 62 张真图里有 3 张是
#: `..._xy_item.heic`（丢弃会让这 3 件商品一张图都不剩）。
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.heic', '.heif')


def _looks_like_image_url(url: str) -> bool:
    """这个 URL 看起来是**图片**吗（忽略 query/fragment，只看路径后缀）。

    真机踩过：图片字段里会混进闲鱼「以图搜同款」的**网页**，例如
    ``https://h5.m.goofish.com/wow/moyu/moyu-project/idle-photo-search/pages/home?kun=true&wh_ttid=native&opaque=false&extra=%7B%22imageInfo%22%3A...``
    （query 里带着 imageInfo）。它会被深扫当图片收进 `items.images`：编辑弹窗多一张
    打不开的卡片，用户一保存还会把这条网页 URL 当成商品图推给平台（写入事故）。
    所以只认图片后缀（`..._640x640q90.jpg` 这种也认），认不出就丢，**不用网页 URL 凑数**。
    """
    text = (url or '').strip()
    if not text:
        return False
    path = text.split('#', 1)[0].split('?', 1)[0].lower()
    return path.endswith(IMAGE_EXTENSIONS)


def _iter_image_entries(raw: Any, depth: int = 0) -> Iterator[Tuple[bool, str]]:
    """从任意形态的图片字段里挖出 `(是否主图, URL)`。

    真机抓帧证明这个字段有**三种形态**，只认 list 会一张图都拿不到：

    - dict：``cardData.picInfo = {'hasVideo': False, 'picUrl': 'http://...', ...}``
    - JSON 字符串：``cardData.detailParams.imageInfos`` 是
      ``'[{"heightSize":1254,"major":true,"type":0,"url":"http://...","videoCover":false}]'``
    - list：``[{'url': ...}, ...]``（发布/详情接口用这种）

    字符串分支先尝试 ``json.loads`` —— 直接当 URL 用会把整段 JSON 当成图片地址。
    """
    if depth > 8:
        return
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return
        if text[0] in '[{':
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None and not isinstance(parsed, str):
                yield from _iter_image_entries(parsed, depth + 1)
                return
        if text.startswith(('http://', 'https://', '//')) and _looks_like_image_url(text):
            yield (False, text)
        return
    if isinstance(raw, (list, tuple)):
        for entry in raw:
            yield from _iter_image_entries(entry, depth + 1)
        return
    if not isinstance(raw, dict):
        return
    url = _first_text(raw, *IMAGE_URL_KEYS)
    if url and _looks_like_image_url(url):
        major = any(raw.get(key) in (True, 'true', 'True', 1, '1') for key in _MAJOR_IMAGE_KEYS)
        yield (major, url)
    for key, value in raw.items():
        if key in _IMAGE_RECURSE_SKIP:
            continue
        if isinstance(value, (dict, list, tuple, str)):
            yield from _iter_image_entries(value, depth + 1)


def _merge_images(*sources: Any) -> List[str]:
    """把多个来源的图片合并成 https 数组：**主图排首位** + 按 URL 去重。

    去重时保留「出现过 major」的信息：同一张图先在 ``picInfo`` 里以非主图出现、
    又在 ``imageInfos`` 里带 ``major=true``，也该排到首位。
    """
    order: List[str] = []
    is_major: Dict[str, bool] = {}
    for source in sources:
        for major, url in _iter_image_entries(source):
            normalized = _https(url)
            if normalized in is_major:
                if major:
                    is_major[normalized] = True
                continue
            is_major[normalized] = major
            order.append(normalized)
    return [url for url in order if is_major[url]] + [url for url in order if not is_major[url]]


def normalize_list_card(card: Any) -> Optional[Dict[str, Any]]:
    """把列表里的商品卡归一化成业务层形状；取不到 item_id 就丢弃（返回 None）。

    字段名在不同版本里套了三层（``cardData`` → ``itemInfo``/``itemData``/``item``），
    逐个试。
    """
    if not isinstance(card, dict):
        return None
    node = card.get('cardData') if isinstance(card.get('cardData'), dict) else card
    inner: Any = None
    for key in ('itemInfo', 'itemData', 'item', 'auction'):
        if isinstance(node.get(key), dict):
            inner = node[key]
            break
    if inner is None:
        inner = node

    detail = inner.get('detailParams') if isinstance(inner.get('detailParams'), dict) else {}
    item_id = _first_text(detail, 'itemId') or _first_text(
        inner, 'itemId', 'auctionId', 'id') or _first_text(node, 'itemId', 'id')
    if not item_id:
        return None

    price: Any = inner.get('price')
    if isinstance(price, dict):
        price = price.get('price') or price.get('value') or price.get('text')
    if price in (None, ''):
        price_info = inner.get('priceInfo') if isinstance(inner.get('priceInfo'), dict) else {}
        price = price_info.get('price') or price_info.get('value')
    if price in (None, ''):
        price = inner.get('soldPrice') or detail.get('soldPrice')
    price_text = str(price) if price not in (None, '') else ''
    try:
        price_value = float(str(price).replace('¥', '').replace(',', '').strip())
    except (TypeError, ValueError):
        price_value = 0.0

    # 真机抓帧（2026-10-06）证明图片分三处挂着，且形态各不相同：
    #   cardData.picInfo（dict，picUrl 在它里面）
    #   cardData.detailParams.picUrl（dict 里的字符串）
    #   cardData.detailParams.imageInfos（**JSON 字符串**，major=true 的是主图）
    # 旧实现只认 list，所以真机图片全空、商品页只剩文字。
    images = _merge_images(
        node.get('picInfo'),
        detail.get('imageInfos'),
        detail.get('picUrl'),
        inner.get('picInfo'),
        inner.get('imageInfos'),
        inner.get('images'),
        inner.get('imageList'),
        detail.get('images'),
        detail.get('imageList'),
        inner.get('itemImage'),
        inner.get('image'),
    )

    # 库存：**列表卡里根本没有这个字段**（真机 cardData 的键是
    # auctionType/categoryId/detailParams/detailUrl/id/itemLabelDataVO/itemStatus/
    # picInfo/priceInfo/title/trackParams）。拿不到就是「未知」，绝不写 1 冒充 ——
    # 真库存只有详情接口 `data.itemDO.quantity` 才有。
    quantity = inner.get('quantity')
    if quantity in (None, ''):
        quantity = inner.get('stock')
    if quantity in (None, ''):
        quantity = detail.get('quantity') or detail.get('stock')
    stock: Optional[int] = None
    if quantity not in (None, ''):
        try:
            stock = int(float(quantity))
        except (TypeError, ValueError):
            stock = None
    if stock is not None and stock < 0:
        stock = None

    return {
        'item_id': item_id,
        'title': _first_text(inner, 'title', 'itemTitle', 'name') or _first_text(detail, 'title'),
        'price': price_value,
        'price_text': price_text,
        'stock': stock,
        # **按存在性取值，不用 `or` 链**：数字 ``0`` 是 falsy，旧写法
        # `inner.get('itemStatus') or inner.get('status')` 会把它吞掉，落到
        # `canonical_status(None) == 'on_sale'`。改 `canonical_status` 之前这只是
        # 侥幸对（0 和 None 都回 on_sale）；现在 0 必须真的走进数字分支，
        # 否则 0=在售 这个真机结论根本用不上。
        'status': canonical_status(
            inner['itemStatus'] if 'itemStatus' in inner else inner.get('status')
        ),
        'item_status': inner.get('itemStatus'),
        'category_id': _first_text(inner, 'categoryId', 'catId', 'channelCatId'),
        'images': images,
        'description': _first_text(inner, 'desc', 'description'),
    }


# ---------------------------------------------------------------------------
# 发布 / 编辑 payload 构造（纯函数，便于单测）
# ---------------------------------------------------------------------------


def to_cent(value: Any) -> str:
    """元 → 分，**字符串**。平台价格字段要字符串分，传数字会报参数不合法。"""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise XianyuError(f'价格无法解析: {value!r}') from None
    if amount < 0:
        raise XianyuError(f'价格不能为负: {value!r}')
    return str(round(amount * 100))


def build_image_info(url: str, is_major: bool = False, file_id: Optional[str] = None,
                     width: int = 800, height: int = 800) -> Dict[str, Any]:
    """``imageInfoDOList`` 的元素。``extraInfo`` 三个值必须是**字符串** ``'false'``。"""
    info: Dict[str, Any] = {
        'extraInfo': {'isH': 'false', 'isT': 'false', 'raw': 'false'},
        'isQrCode': False,
        'url': url,
        'heightSize': height,
        'widthSize': width,
        'major': is_major,
        'type': 0,
        'status': 'done',
    }
    if file_id:
        info['fileId'] = file_id
    return info


#: 编辑表单 ``imageInfoDOList`` 条目的固定字段值。**真机原样**（2026-10-06 抓帧，
#: ``edit_item_detail('1087258469593')``）：``type="0"``，尺寸是字符串，第一张
#: ``major="true"`` 其余 ``"false"``。
EDIT_IMAGE_TYPE = '0'

#: 尺寸未知时的占位值。**刻意用 '0' 而不是真机看到的 1254**：1254 是那一张图的
#: 实际尺寸，把它当成默认值等于对每张图撒谎，平台若按错尺寸校验/裁切就会出问题。
#: 平台是否接受 '0'（自行按原图处理）**尚未真机验证** —— 前端上传接口拿得到
#: width/height，正常路径会带上真实尺寸，走不到这个默认值。
UNKNOWN_IMAGE_SIZE = '0'

#: ``extraInfo`` 三个值必须是**字符串** ``'false'``（真机原样；传布尔 False 会被
#: 平台当参数非法）。
IMAGE_EXTRA_INFO: Dict[str, str] = {'raw': 'false', 'isT': 'false', 'isH': 'false'}


def _image_entry(entry: Any) -> Dict[str, Any]:
    """把一张图的入参（URL 字符串 / ``{url,width,height}`` 字典）归一成字段。"""
    if isinstance(entry, dict):
        url = (entry.get('url') or entry.get('picUrl') or entry.get('imageUrl')
               or entry.get('fileUrl') or '')
        return {
            'url': str(url).strip(),
            'width': entry.get('width', entry.get('widthSize')),
            'height': entry.get('height', entry.get('heightSize')),
        }
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        # (url, (w, h)) 也认，省得调用方为了成对数据去拼字典。
        url = str(entry[0] or '').strip()
        size = entry[1]
        if isinstance(size, (tuple, list)) and len(size) == 2:
            return {'url': url, 'width': size[0], 'height': size[1]}
        return {'url': url, 'width': None, 'height': None}
    return {'url': str(entry or '').strip(), 'width': None, 'height': None}


def _size_text(value: Any) -> str:
    """尺寸 → 字符串。认不出（None/空/非数字）就返回 ``UNKNOWN_IMAGE_SIZE``。"""
    if value in (None, ''):
        return UNKNOWN_IMAGE_SIZE
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return UNKNOWN_IMAGE_SIZE
    if number <= 0:
        return UNKNOWN_IMAGE_SIZE
    return str(number)


def images_to_image_infos(urls: Any,
                          sizes: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
    """业务层 ``images``（URL 数组）→ 平台 ``imageInfoDOList``。

    **纯函数、只认 URL**：这里不发上传请求，本地文件路径必须先经
    ``upload_image`` 换成闲鱼图床 URL（``edit_item`` 是同步映射，拿不到异步上传
    的结果）。传本地路径会被明确拒绝，而不是静默交给平台去报一个看不懂的参数错误。

    支持的入参形态：
    - ``['https://a.png', 'http://b.png']``
    - ``[{'url': 'https://a.png', 'width': 1254, 'height': 1254}, ...]``
    - ``sizes=[{'width': 1254, 'height': 1254}, ...]`` 与 ``urls`` 按下标对齐

    尺寸取不到时用 ``UNKNOWN_IMAGE_SIZE``（见其注释：**不硬编码 1254**）。
    """
    if isinstance(urls, (str, dict)):
        urls = [urls]
    entries = list(urls or [])
    if not entries:
        # 空数组**必须报错**：平台收到空 imageInfoDOList 会直接拒，但错误信息
        # 对用户毫无意义；更重要的是「图片被清空」不可逆。
        raise XianyuError('修改图片需要至少一张图片：不允许把商品图清空成空列表')

    size_list = list(sizes or [])
    infos: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        parsed = _image_entry(entry)
        url = parsed['url']
        if not url:
            raise XianyuError(f'第 {index + 1} 张图片缺少 URL')
        if not url.startswith(('http://', 'https://')):
            raise XianyuError(
                f'第 {index + 1} 张图片不是 http/https URL: {url[:80]}；'
                '请先用图片上传接口换成闲鱼图床地址'
            )
        width = parsed['width']
        height = parsed['height']
        if index < len(size_list):
            size = size_list[index]
            if isinstance(size, dict):
                width = size.get('width', size.get('widthSize', width))
                height = size.get('height', size.get('heightSize', height))
            elif isinstance(size, (tuple, list)) and len(size) == 2:
                width, height = size[0], size[1]
        infos.append({
            # 第一张是主图。平台按这个选封面，全 false 会导致商品没有封面图。
            'major': 'true' if index == 0 else 'false',
            'widthSize': _size_text(width),
            'heightSize': _size_text(height),
            'type': EDIT_IMAGE_TYPE,
            'url': url,
            'extraInfo': dict(IMAGE_EXTRA_INFO),
        })
    return infos


def _literal_label_ext(channel_cate_id: str, channel_cate_name: str,
                       tb_cat_id: str) -> Dict[str, Any]:
    return {
        'channelCateName': channel_cate_name,
        'valueId': None,
        'channelCateId': channel_cate_id,
        'valueName': None,
        'tbCatId': tb_cat_id,
        'subPropertyId': None,
        'labelType': 'common',
        'subValueId': None,
        'labelId': None,
        'propertyName': '分类',
        'isUserClick': '1',
        'isUserCancel': None,
        'from': 'newPublishChoice',
        'propertyId': '-10000',
        'labelFrom': 'newPublish',
        'text': channel_cate_name,
        'properties': f'-10000##分类:{channel_cate_id}##{channel_cate_name}',
    }


def build_label_ext(transport: Optional[Dict[str, Any]],
                    channel_cate_id: Optional[str] = None,
                    channel_cate_name: Optional[str] = None,
                    tb_cat_id: Optional[str] = None) -> Dict[str, Any]:
    """分类标签：**用户显式指定 > kgraph 推荐 > 默认「其他闲置」**。

    「用户指定」要求 ID 与名字**同时**给出：只给 ID 而名字退回默认值会拼出
    「用户 cateId + 其他闲置名字」这种自相矛盾的标签，还不如整份用默认值。
    用户指定时整份走字面量，不能混进 transport 的 ``valueId``/``labelId``
    （那些指向被推荐的分类）。
    """
    if channel_cate_id and channel_cate_name:
        return _literal_label_ext(channel_cate_id, channel_cate_name,
                                  tb_cat_id or DEFAULT_TB_CAT_ID)
    if transport:
        label = dict(transport)
        label['text'] = transport.get('channelCateName', channel_cate_name)
        label['properties'] = (
            f"-10000##分类:{transport.get('channelCateId', channel_cate_id)}"
            f"##{transport.get('channelCateName', channel_cate_name)}"
        )
        label['isUserClick'] = '1'
        label['labelFrom'] = 'newPublish'
        return label
    return _literal_label_ext(DEFAULT_CHANNEL_CATE_ID, DEFAULT_CHANNEL_CATE_NAME,
                              DEFAULT_TB_CAT_ID)


def build_publish_payload(title: str, price: Any, desc: Optional[str] = None,
                          orig_price: Any = None,
                          images: Optional[List[Dict[str, Any]]] = None,
                          label_ext: Optional[Dict[str, Any]] = None,
                          quantity: Any = 1, free_shipping: bool = True,
                          addr: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """发布 payload。与 ``edit`` 的 payload **同构**。"""
    images = images or []
    if not images:
        raise XianyuError('至少需要一张图片')

    price_cent = to_cent(price)
    orig_cent = to_cent(orig_price) if orig_price is not None else price_cent
    label = label_ext or build_label_ext(None)

    payload: Dict[str, Any] = {
        'freebies': False,
        'itemTypeStr': 'b',            # b = 二手闲置
        'quantity': str(quantity),     # 字符串
        'simpleItem': 'true',          # 字符串
        'imageInfoDOList': images,
        'itemTextDTO': {'title': title, 'desc': desc or title, 'titleDescSeparate': False},
        'itemLabelExtList': [label],
        'itemPriceDTO': {'origPriceInCent': orig_cent, 'priceInCent': price_cent},
        'userRightsProtocols': USER_RIGHTS_PROTOCOLS,
        'itemPostFeeDTO': {
            'canFreeShipping': bool(free_shipping),
            'supportFreight': True,
            'onlyTakeSelf': False,
        },
        'itemCatDTO': {
            'catId': label.get('tbCatId') or DEFAULT_TB_CAT_ID,
            'catName': label.get('channelCateName', DEFAULT_CHANNEL_CATE_NAME),
            'channelCatId': label.get('channelCateId', DEFAULT_CHANNEL_CATE_ID),
            'tbCatId': label.get('tbCatId') or DEFAULT_TB_CAT_ID,
        },
        'publishScene': PUBLISH_SCENE,
    }
    if addr:
        payload['itemAddrDTO'] = addr
    return payload


def _set_path(payload: Dict[str, Any], path: str, value: Any) -> None:
    """按 ``a.b.c`` 写入，中间层缺失补空字典。"""
    parts = path.split('.')
    cursor = payload
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def build_edit_patch(detail: Dict[str, Any], *, price: Any = None, orig_price: Any = None,
                     quantity: Any = None, title: Optional[str] = None,
                     desc: Optional[str] = None, item_status: Optional[str] = None,
                     free_shipping: Optional[bool] = None,
                     image_infos: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """在 ``editDetail`` 原表单上打补丁。**只覆盖显式传入的字段**，
    其余一律沿用原表单 —— 这是不丢类目属性/地址/议价设置的唯一做法。

    ``image_infos`` 直接**整体替换** ``imageInfoDOList``（图片是有序列表，
    逐张合并没有意义），值必须已经由 ``images_to_image_infos`` 转成平台形状。
    """
    payload: Dict[str, Any] = copy.deepcopy(detail)
    if price is not None:
        _set_path(payload, 'itemPriceDTO.priceInCent', to_cent(price))
    if orig_price is not None:
        _set_path(payload, 'itemPriceDTO.origPriceInCent', to_cent(orig_price))
    if quantity is not None:
        payload['quantity'] = str(quantity)
    if title is not None:
        _set_path(payload, 'itemTextDTO.title', title)
    if desc is not None:
        _set_path(payload, 'itemTextDTO.desc', desc)
    if item_status is not None:
        payload['itemStatus'] = item_status
    if free_shipping is not None:
        _set_path(payload, 'itemPostFeeDTO.canFreeShipping', bool(free_shipping))
    if image_infos is not None:
        payload['imageInfoDOList'] = [dict(info) for info in image_infos]
    return payload


def build_edit_submit(detail: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    """打补丁 + 注入编辑族必填字段，得到最终提交体。"""
    payload = build_edit_patch(detail, **changes)
    payload['uniqueCode'] = mtop.generate_uuid()
    payload['bizcode'] = EDIT_BIZCODE
    payload['publishScene'] = EDIT_PUBLISH_SCENE
    return payload


#: 业务层字段名 → 编辑接口可改的字段。**改价没有独立接口**，走 edit。
EDITABLE_FIELDS = {
    'price', 'original_price', 'stock', 'title', 'description', 'status', 'free_shipping',
    'images',
}


def _local_changes_to_edit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """业务层字段名 → ``build_edit_patch`` 的 kwargs。

    不认识的字段**抛错**而不是静默丢掉：静默丢会让用户看到「已同步」而字段
    根本没变（这正是本仓库反复踩的「假通」）。

    ``images`` 例外地映射成 ``image_infos``（平台字段名 ``imageInfoDOList``），
    因为它的值要先经过 ``images_to_image_infos`` 的形状转换，不能原样透传。
    """
    changes: Dict[str, Any] = {}
    unknown: List[str] = []
    for key, value in (payload or {}).items():
        if key == 'price':
            changes['price'] = value
        elif key in ('original_price', 'orig_price'):
            changes['orig_price'] = value
        elif key in ('stock', 'quantity'):
            changes['quantity'] = value
        elif key == 'title':
            changes['title'] = '' if value is None else str(value)
        elif key in ('description', 'desc'):
            changes['desc'] = '' if value is None else str(value)
        elif key == 'status':
            changes['item_status'] = (
                ITEM_STATUS_ON_SALE if canonical_status(value) == 'on_sale'
                else ITEM_STATUS_OFF_SHELF
            )
        elif key == 'free_shipping':
            changes['free_shipping'] = bool(value)
        elif key == 'images':
            # 转成平台形状放在这里，而不是留到 build_edit_patch：
            # 校验要在**取详情之前**发生，参数不合法时不该白跑一次接口调用。
            changes['image_infos'] = images_to_image_infos(value)
        else:
            unknown.append(str(key))
    if unknown:
        raise XianyuError(f'协议层不支持修改字段: {", ".join(sorted(unknown))}')
    return changes
